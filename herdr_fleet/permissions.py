"""Claude Code permission preflight for ``doctor`` (design 16.6). Advisory only.

A Claude seat that has to ask before each ``herdr agent prompt`` stalls the
team. This reads ``permissions.allow`` from the repository's
``.claude/settings.json`` and ``.claude/settings.local.json`` and the user's
``~/.claude/settings.json`` and reports which of the commands the team uses no
entry covers. An entry covers a command when it is ``Bash(<prefix>:*)`` or
``Bash(<prefix> *)`` with ``<prefix>`` the command or a shorter word prefix of
it (``Bash(herdr agent:*)`` covers ``herdr agent prompt``), or a bare ``Bash``
/ ``Bash(*)``. Claude Code also honours per-session "don't ask again" choices
the plugin cannot see, so this never fails.
"""

from __future__ import annotations

import json
import os
import re
from typing import List, Optional, Sequence, Tuple

COMMANDS = ("herdr agent prompt", "herdr agent get", "herdr agent wait", "herdr agent read", "herdr-fleet")
TARGET_FILE = ".claude/settings.json"
ENTRY = re.compile(r"^Bash(?:\((.*)\))?$")


def settings_files(repo: str, home: str) -> List[str]:
    return [os.path.join(repo, ".claude", "settings.json"),
            os.path.join(repo, ".claude", "settings.local.json"),
            os.path.join(home, ".claude", "settings.json")]


def load_allow(paths: Sequence[str]) -> Tuple[List[str], List[str]]:
    """(allow entries from every readable file, problems). Missing files are fine."""
    entries: List[str] = []
    problems: List[str] = []
    for path in paths:
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError) as exc:
            problems.append("cannot read %s: %s" % (path, exc))
            continue
        allow = ((data or {}).get("permissions") or {}).get("allow") if isinstance(data, dict) else None
        if isinstance(allow, list):
            entries.extend(str(item) for item in allow)
    return entries, problems


def covers(entry: str, command: str) -> bool:
    match = ENTRY.match(entry.strip())
    if not match:
        return False
    inner = match.group(1)
    if inner is None or inner.strip() in ("", "*", ":*"):
        return True  # all Bash commands
    inner = inner.strip()
    for suffix in (":*", " *"):
        if inner.endswith(suffix):
            prefix = inner[:-len(suffix)].split()
            return bool(prefix) and command.split()[:len(prefix)] == prefix
    return False


def missing(entries: Sequence[str], commands: Sequence[str] = COMMANDS) -> List[str]:
    return [command for command in commands if not any(covers(entry, command) for entry in entries)]


def suggestion(commands: Sequence[str]) -> str:
    return "add to %s permissions.allow: %s" % (
        TARGET_FILE, ", ".join('"Bash(%s:*)"' % command for command in commands))


def check(repo: str, home: str) -> Tuple[List[str], List[str]]:
    """(commands not covered, problems reading the settings)."""
    entries, problems = load_allow(settings_files(repo, home))
    return missing(entries), problems


def home_dir(env_home: Optional[str]) -> str:
    return env_home or os.path.expanduser("~")
