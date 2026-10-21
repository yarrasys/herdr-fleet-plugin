"""Claude Code hooks (design 18.7, opt-in): ``hooks install [--remove]`` and the doctor line.

``hooks install`` adds two entries to ``<repo>/.claude/settings.local.json``: a
``Stop`` hook running ``herdr-fleet inbox --hook stop`` (a seat finishing a turn
reads its own queued messages and carries on with them) and a
``UserPromptSubmit`` hook running ``herdr-fleet inbox --hook prompt`` (a seat
the owner prompts by hand learns that messages wait). The hook side lives in
``mailroom.op_inbox_hook``.

The entries are recognised by their command text, so installing twice changes
nothing and ``--remove`` deletes exactly those, leaving every other key, hook
and ``permissions.allow`` entry as it was. Pure file work: no herdr command.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from typing import Dict, List, Tuple

from herdr_fleet.context import EXIT_OK, Ctx, OpError

SETTINGS = os.path.join(".claude", "settings.local.json")
COMMANDS = (("Stop", "herdr-fleet inbox --hook stop"), ("UserPromptSubmit", "herdr-fleet inbox --hook prompt"))
TIMEOUT_S = 10


def settings_path(repo: str) -> str:
    return os.path.join(repo, SETTINGS)


def load(path: str) -> Dict:
    """The settings object; {} when the file does not exist. OpError when it is not a JSON
    object (the file is then left alone)."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise OpError("cannot read %s: %s" % (path, exc))
    try:
        data = json.loads(text) if text.strip() else {}
    except ValueError as exc:
        raise OpError("%s is not valid JSON (%s); fix it first, nothing was changed" % (path, exc))
    if not isinstance(data, dict):
        raise OpError("%s does not hold a JSON object; nothing was changed" % path)
    return data


def _ours(hook: object, command: str) -> bool:
    return isinstance(hook, dict) and str(hook.get("command", "")).strip() == command


def _groups(data: Dict, event: str) -> List:
    hooks = data.get("hooks")
    groups = hooks.get(event) if isinstance(hooks, dict) else None
    return groups if isinstance(groups, list) else []


def installed(data: Dict) -> Dict[str, bool]:
    """Per event, whether the herdr-fleet entry is there."""
    return {event: any(_ours(h, command) for g in _groups(data, event) if isinstance(g, dict)
                       for h in (g.get("hooks") if isinstance(g.get("hooks"), list) else []))
            for event, command in COMMANDS}


def install(data: Dict) -> List[str]:
    """Add the missing entries in place; returns the events added."""
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise OpError('"hooks" in %s is not an object; nothing was changed' % SETTINGS)
    present = installed(data)
    added = []
    for event, command in COMMANDS:
        if present[event]:
            continue
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise OpError('"hooks.%s" in %s is not a list; nothing was changed' % (event, SETTINGS))
        groups.append({"hooks": [{"type": "command", "command": command, "timeout": TIMEOUT_S}]})
        added.append(event)
    return added


def remove(data: Dict) -> List[str]:
    """Delete exactly the herdr-fleet entries in place (and a group, event list or ``hooks``
    object that only they filled); returns the events they were removed from."""
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return []
    removed = []
    for event, command in COMMANDS:
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        kept_groups = []
        for group in groups:
            entries = group.get("hooks") if isinstance(group, dict) else None
            if isinstance(entries, list) and any(_ours(h, command) for h in entries):
                kept = [h for h in entries if not _ours(h, command)]
                if event not in removed:
                    removed.append(event)
                if not kept:
                    continue
                group = dict(group, hooks=kept)
            kept_groups.append(group)
        if event in removed:
            if kept_groups:
                hooks[event] = kept_groups
            else:
                del hooks[event]
    if removed and not hooks:
        del data["hooks"]
    return removed


def write(path: str, data: Dict) -> None:
    """Atomic: a temp file beside it, then rename. Keeps the file's mode when it exists."""
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    try:
        mode = os.stat(path).st_mode & 0o777
    except FileNotFoundError:
        mode = 0o644
    fd, tmp = tempfile.mkstemp(prefix=".settings.", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def op_hooks(ctx: Ctx, remove_them: bool = False) -> int:
    """``hooks install [--remove]`` for the fleet's repository."""
    fleet = ctx.fleet
    path = settings_path(fleet.repo)
    data = load(path)
    if remove_them:
        events = remove(data)
        if not events:
            ctx.say("nothing to remove: no herdr-fleet hooks in %s" % path)
            return EXIT_OK
        write(path, data)
        ctx.say("removed the %s hook%s from %s" % (" and ".join(events), "s" if len(events) > 1 else "", path))
        return EXIT_OK
    events = install(data)
    if not events:
        ctx.say("already installed in %s" % path)
    else:
        write(path, data)
        ctx.say("installed the %s hook%s in %s" % (" and ".join(events), "s" if len(events) > 1 else "", path))
        ctx.say("a running Claude Code session may start running new hook entries at once; after --remove, a running session can keep them until it restarts")
    if not any(seat.kind == "claude" for seat in fleet.seats):
        ctx.warn("no seat of kind claude in %s; only Claude Code runs these hooks" % fleet.path)
    if not shutil.which("herdr-fleet", path=ctx.env.get("PATH")):
        ctx.warn("herdr-fleet is not on PATH, so the hooks cannot run it; run `herdr-fleet install-cli`")
    return EXIT_OK


def doctor_check(repo: str) -> Tuple[str, str, str]:
    """``ok hooks: installed`` / ``info hooks: not installed (herdr-fleet hooks install)``."""
    try:
        found = installed(load(settings_path(repo)))
    except OpError as exc:
        return ("warn", "hooks", str(exc))
    have = [event for event, ok in found.items() if ok]
    if len(have) == len(COMMANDS):
        return ("ok", "hooks", "installed")
    if have:
        return ("warn", "hooks", "partly installed (%s only); run herdr-fleet hooks install" % ", ".join(have))
    return ("info", "hooks", "not installed (herdr-fleet hooks install)")
