"""Read-only inspection: drift (design 4.4), status (4.2, 16.3, 16.4, 16.7, 17.2,
17.4, 18.6's watcher and queued lines, 19.1's watcher strip, 20.2's debt ages,
20.5's ``blocked (4m)``, 20.8's queue ages) and doctor (4.7, 16.6, 17.3, 18.7's
hooks line).

Nothing here changes herdr. ``collect_status`` may rewrite the state file
when it prunes ids herdr no longer knows, which every command does, and records
each seat's ``state_seen`` (design 20.5). Git is
only read (``gitinfo``), and ``status --context`` only reads transcripts
(``context_size``).
"""

from __future__ import annotations

import os
import shlex
import sys
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from herdr_fleet import MIN_HERDR_VERSION, contacts as contact_mod, context_size, gitinfo, hooks, inbox, messages, permissions
from herdr_fleet import seen, watchpane
from herdr_fleet.context import Ctx
from herdr_fleet.fleetfile import Fleet, FleetError, Seat, check_kinds
from herdr_fleet.herdr import Herdr, HerdrError, describe
from herdr_fleet.seats import (QUIT_CONFIG_HINT, agent_args, contains_in_order, find_agent_process,
                               process_argv, quit_command)
from herdr_fleet.state import StateError, prune_state

# herdr's integration names that differ from the agent kind.
INTEGRATION_ALIASES = {"agy": "antigravity-cli"}
FLAGGED = ("outdated", "not installed")


def _join(args: Sequence[str]) -> str:
    return " ".join(shlex.quote(a) for a in args) if args else "(none)"


def live_argv(herdr: Herdr, pane: Optional[str], kind: str) -> Optional[List[str]]:
    """The running agent's argv from ``pane process-info``; None when unknown."""
    if not pane:
        return None
    try:
        info = herdr.process_info(pane)
    except HerdrError:
        return None
    proc = find_agent_process(info, kind)
    return process_argv(proc) if proc else None


def is_adopted(entry: Optional[Mapping]) -> bool:
    """The seat's live session was not started by herdr-fleet, so its args are its
    owner's business (design section 15): the state entry says ``adopted``, or it
    records no ``args`` (adopted by an older version, or found live by name).
    Pane ids are not compared: a session moved by hand stays adopted."""
    entry = entry or {}
    return bool(entry.get("adopted")) or entry.get("args") is None


def drift_reasons(ctx: Ctx, seat: Seat, agent: Dict, compare_args: bool = True) -> List[str]:
    """Why a live seat differs from the fleet file (empty list: no drift).
    ``compare_args=False`` checks the kind only (status of an adopted seat)."""
    reasons: List[str] = []
    live_kind = str(agent.get("agent") or "")
    pane = agent.get("pane_id")
    if live_kind != seat.kind:
        reasons.append("kind: running %s, file says %s" % (live_kind or "?", seat.kind))
    elif compare_args:
        argv = live_argv(ctx.herdr, pane, live_kind)
        if argv is not None:
            running = agent_args(argv, live_kind)
            if not contains_in_order(running, seat.args):
                reasons.append("args: running %s, file says %s" % (_join(running), _join(seat.args)))
    entry = ctx.state.entry(seat.name) or {}
    if entry.get("pane_id") == pane:
        recorded_kind = entry.get("kind")
        if recorded_kind and recorded_kind != seat.kind and not _has(reasons, "kind"):
            reasons.append("kind: started as %s, file says %s" % (recorded_kind, seat.kind))
        recorded_args = entry.get("args")
        if (compare_args and recorded_args is not None and list(recorded_args) != list(seat.args)
                and not _has(reasons, "args")):
            reasons.append("args: started with %s, file says %s" % (_join(recorded_args), _join(seat.args)))
    return reasons


def _has(reasons: List[str], prefix: str) -> bool:
    return any(r.startswith(prefix + ":") for r in reasons)


def integration_rows(herdr: Herdr, kinds: Sequence[str]) -> List[Dict]:
    """``herdr integration status`` for the given kinds, flagging problems."""
    try:
        listed = herdr.integration_status()
    except HerdrError as exc:
        return [{"kind": k, "status": "error", "line": describe(exc), "flag": True} for k in kinds]
    by_kind = {row["kind"]: row for row in listed}
    rows = []
    for kind in kinds:
        row = by_kind.get(kind) or by_kind.get(INTEGRATION_ALIASES.get(kind, ""))
        if row is None:
            rows.append({"kind": kind, "status": "not listed", "line": "", "flag": False})
        else:
            rows.append({"kind": kind, "status": row["status"], "line": row["line"],
                         "flag": row["status"] in FLAGGED})
    return rows


def collect_status(ctx: Ctx, integrations: bool = True, context: bool = False) -> Dict:
    """One object describing every seat (the ``status --json`` payload). With
    ``context``, each seat also gets ``context`` (design 17.4) and ``restart_hint``."""
    fleet, state, herdr = ctx.fleet, ctx.state, ctx.herdr
    prune_state(state, herdr)
    if state.existed:
        ctx.save()
    agents = herdr.agent_list()
    live = {str(a["name"]): a for a in agents if a.get("name")}
    seats = []
    for seat in fleet.seats:
        agent = live.get(seat.name)
        entry = state.entry(seat.name) or {}
        adopted = bool(agent) and is_adopted(entry)
        cwd = str((agent.get("foreground_cwd") or agent.get("cwd")) if agent else seat.cwd or "")
        seats.append({
            "name": seat.name,
            "kind": seat.kind,
            "live_kind": agent.get("agent") if agent else None,
            "pane_id": (agent or {}).get("pane_id") or entry.get("pane_id"),
            "state": str(agent.get("agent_status") or "unknown") if agent else "absent",
            "cwd": cwd,
            "branch": git_branch(cwd),
            "drift": drift_reasons(ctx, seat, agent, compare_args=not adopted) if agent else [],
            "adopted": adopted,
            "notes": seat.notes,
            "blocked_at_startup": bool(entry.get("blocked_at_startup")),
            "last_kickoff_at": entry.get("last_kickoff_at"),
            "state_since": None,
        })
        if context:
            reading = context_size.seat_context(agent, ctx.env) if agent else None
            hint = fleet.restart_hint_tokens
            seats[-1]["context"] = reading
            seats[-1]["restart_hint"] = bool(reading and hint and reading["tokens"] >= hint)
    recorded = seen.observe(ctx, {s["name"]: s["state"] for s in seats})  # design 20.5
    for item in seats:
        record_ = recorded.get(item["name"]) or {}
        if record_.get("state") == item["state"]:
            item["state_since"] = record_.get("since")
    data = {
        "fleet_file": fleet.path,
        "repo": fleet.repo,
        "workspace": {"label": fleet.workspace or state.data.get("workspace_label"),
                      "workspace_id": state.workspace_id,
                      "created_by_fleet": state.created_workspace},
        "seats": seats,
        "contacts": [{"name": r.contact.name, "pane_id": r.pane, "state": r.state, "note": r.contact.note,
                      "problem": r.problem()} for r in contact_mod.resolve_all(fleet, agents)],
        "owed": messages.owed(_log_entries(ctx)),
    }
    data["queued"], data["queued_oldest"] = queued_summary(fleet)
    data["watcher"] = watchpane.watcher_status(ctx)
    if integrations:
        data["integrations"] = integration_rows(herdr, fleet.kinds())
    return data


def queued_summary(fleet: Fleet) -> Tuple[Dict[str, int], Dict[str, str]]:
    """({mailbox: queued count}, {mailbox: time of the oldest}) for every non-empty mailbox,
    seats in file order, then contacts, other names, and ``owner`` last. Read-only:
    messages past their ttl are not counted, and nothing moves."""
    found = inbox.queued(fleet.repo)
    order = [s.name for s in fleet.seats] + [c.name for c in fleet.contacts]
    names = [n for n in order if n in found] + sorted(n for n in found if n not in order and n != messages.OWNER)
    if messages.OWNER in found:
        names.append(messages.OWNER)
    return ({n: len(found[n]) for n in names},
            {n: min(found[n], key=inbox.Message.sort_key).time for n in names})


def queued_line(data: Dict) -> Optional[str]:
    """``queued: fablyn-dev 2 (oldest 25m), owner 1`` (ages, design 20.8); None when every
    mailbox is empty."""
    counts, oldest = data.get("queued") or {}, data.get("queued_oldest") or {}
    if not counts:
        return None
    now = inbox.now_utc()
    parts = []
    for name, count in counts.items():
        age = inbox.age_since(oldest.get(name), now) if count > 1 else None
        parts.append("%s %d%s" % (name, count, " (oldest %s)" % age if age else ""))
    return "queued: " + ", ".join(parts)


def git_branch(cwd: str) -> str:
    """``git -C <cwd> rev-parse --abbrev-ref HEAD``; blank when not a repository or on error."""
    return gitinfo.current_branch(cwd)


def shorten(path: str, home: Optional[str]) -> str:
    """``/Users/me/work/x`` -> ``~/work/x``."""
    home = (home or os.path.expanduser("~")).rstrip("/")
    if path == home:
        return "~"
    if home and path.startswith(home + "/"):
        return "~" + path[len(home):]
    return path


def _log_entries(ctx: Ctx) -> List[Dict]:
    warnings: List[str] = []
    entries = messages.read(ctx.fleet.repo, warnings)
    for warning in warnings:
        ctx.warn(warning)
    return entries


def startup_label(seat: Dict) -> str:
    """STATE suffix for a seat flagged ``blocked_at_startup`` (never briefed): still
    blocked, or past the prompt and waiting for ``kickoff`` (design 16.12)."""
    if not seat.get("blocked_at_startup"):
        return ""
    if seat["state"] == "blocked":
        return " (blocked at startup)"
    if seat["state"] in ("idle", "done", "working"):
        return " (kickoff pending)"
    return ""


def folded(seats: Sequence[Dict], key: str) -> Optional[str]:
    """The one value every seat that is not ``absent`` shares for ``key`` (``cwd`` or
    ``branch``), so the column can go (design 17.2); None when fewer than two seats
    are live or any value differs."""
    live = [s for s in seats if s["state"] != "absent"]
    values = {str(s.get(key) or "") for s in live}
    return values.pop() if len(live) >= 2 and len(values) == 1 else None


def render_status(data: Dict, home: Optional[str] = None, worktrees_hint: bool = False) -> str:
    """The status table. CWD and BRANCH fold into the header line when every live seat
    shares the value; CONTEXT appears when the data carries ``--context`` readings."""
    seats = data["seats"]
    common_cwd, common_branch = folded(seats, "cwd"), folded(seats, "branch")
    with_context = any("context" in s for s in seats)
    header = ["SEAT", "KIND", "LIVE", "PANE", "STATE"] + (["CONTEXT"] if with_context else []) \
        + (["CWD"] if common_cwd is None else []) + (["BRANCH"] if common_branch is None else []) + ["DRIFT", "NOTES"]
    rows = []
    for s in seats:
        notes = [s["notes"]] if s["notes"] else []
        if s.get("restart_hint"):
            notes.append("consider restart")
        cells = {"SEAT": s["name"], "KIND": s["kind"], "LIVE": s["live_kind"] or "-", "PANE": s["pane_id"] or "-",
                 "STATE": seen.label(s["state"], {"state": s["state"], "since": s.get("state_since")})
                 + startup_label(s), "CONTEXT": context_size.context_label(s.get("context")),
                 "CWD": shorten(s.get("cwd") or "", home) or "-", "BRANCH": s.get("branch") or "",
                 "DRIFT": "; ".join((["adopted"] if s.get("adopted") else []) + s["drift"]) or "-",
                 "NOTES": "; ".join(notes)}
        rows.append([str(cells[column]) for column in header])
    widths = [max(len(r[i]) for r in [header] + rows) for i in range(len(header) - 1)]
    lines = []
    for row in [header] + rows:
        cells = [cell.ljust(widths[i]) for i, cell in enumerate(row[:-1])] + [row[-1]]
        lines.append("  ".join(cells).rstrip())
    ws = data["workspace"]
    title = "fleet %s  workspace %s (%s)" % (data["fleet_file"], ws["label"] or "(caller's)",
                                             ws["workspace_id"] or "not recorded")
    if common_cwd:
        title += "  cwd %s" % shorten(common_cwd, home)
    if common_branch:
        title += "  branch %s" % common_branch
    lines.insert(0, title)
    if data.get("contacts"):
        lines.append("contacts:")
        for c in data["contacts"]:
            where = c["pane_id"] or "-"
            lines.append("  %s  %s  %s%s" % (c["name"], where, c["state"],
                                              "  (%s)" % c["note"] if c["note"] else ""))
    lines.extend(messages.owed_lines(data.get("owed", [])))
    queued = queued_line(data)
    watcher = data.get("watcher") or {}
    if queued or watchpane.shown(watcher):  # design 18.6 and 19.1: who delivers, then what waits
        lines.append(watchpane.status_line(watcher))
    if queued:
        lines.append(queued)
    for row in data.get("integrations", []):
        mark = "  <- %s" % row["status"] if row["flag"] else ""
        lines.append("integration %s: %s%s" % (row["kind"], row["line"] or row["status"], mark))
    if worktrees_hint:
        lines.append("worktrees: see herdr-fleet worktrees")
    return "\n".join(lines)


Check = Tuple[str, str, str]  # (ok|warn|fail|skip|info, check name, detail)


def run_doctor(ctx: Ctx) -> List[Check]:
    """Every doctor check, in order. Never changes anything."""
    checks: List[Check] = []
    add = checks.append
    py = sys.version_info
    add(("ok" if py >= (3, 8) else "fail", "python", "%d.%d.%d (%s)" % (py[0], py[1], py[2], sys.executable)))
    _check_herdr(ctx, add)
    try:
        fleet = ctx.fleet
    except FleetError as exc:
        for error in exc.errors:
            add(("fail", "fleet file", error))
        return checks
    add(("ok", "fleet file", "%s (%d seats)" % (fleet.path, len(fleet.seats))))
    for warning in fleet.warnings:
        add(("warn", "fleet file", warning))
    add(fleet_vs_git(fleet.path, fleet.repo, ctx.env))
    _check_kinds(ctx, fleet, add)
    _check_files(fleet, add)
    for row in integration_rows(ctx.herdr, fleet.kinds()):
        level = "warn" if row["flag"] else "ok"
        add((level, "integration %s" % row["kind"], row["line"] or row["status"]))
    _check_quit(ctx, fleet, add)
    _check_permissions(ctx, fleet, add)
    if any(seat.kind == "claude" for seat in fleet.seats):  # design 18.7: the hooks are Claude Code's
        add(hooks.doctor_check(fleet.repo))
    try:
        state = ctx.state
        add(("ok", "state file", state.path if state.existed else "%s (created on first up)" % state.path))
    except StateError as exc:
        add(("fail", "state file", str(exc)))
    return checks


def fleet_vs_git(path: str, repo: str, env: Optional[Mapping[str, str]] = None) -> Check:
    """Design 17.3: the fleet file in the working tree against ``HEAD`` and the default
    branch, read-only and without ``git fetch``. Drift warns, never fails: the file
    in the working tree is the one that runs."""
    name = "fleet file vs git"
    if not gitinfo.is_work_tree(repo, env):
        return ("skip", name, "not a git repository")
    rel = os.path.relpath(path, repo)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError as exc:
        return ("warn", name, "cannot read %s: %s" % (path, exc))
    default = gitinfo.default_branch(repo, env)
    tracked = gitinfo.is_tracked(repo, rel, env)
    if tracked is None:
        return ("warn", name, "git did not answer in %s" % repo)
    problems: List[str] = []
    if not tracked:
        problems.append("untracked in this checkout (branch %s)" % (gitinfo.current_branch(repo, env) or "?"))
    else:
        head = gitinfo.show_file(repo, "HEAD", rel, env)
        if head is None:
            problems.append("no copy in HEAD")
        elif head != text:
            problems.append("modified since HEAD: %s" % _diff_label(head, text))
    if default is None:
        if problems:
            problems.append("no default branch found (origin/HEAD, origin/main, origin/master)")
        else:
            return ("ok", name, "committed, same as HEAD; no default branch found to compare with")
    else:
        copy = gitinfo.show_file(repo, default, rel, env)
        if copy is None:
            problems.append("no copy on %s" % default)
        elif copy != text:
            problems.append("%s copy differs: %s" % (default, _diff_label(copy, text)))
        elif problems:
            problems.append("same as %s" % default)
    if problems:
        return ("warn", name, "; ".join(problems))
    return ("ok", name, "committed, same as %s" % default)


def _diff_label(old: str, new: str) -> str:
    """``+7/-1 lines, first at line 42`` (counts only; no diff body is printed)."""
    added, removed, first = gitinfo.line_diff(old, new)
    if first is None:  # same lines, different line endings or final newline
        return "only line endings or the final newline differ"
    return "+%d/-%d lines, first at line %d" % (added, removed, first)


def _check_herdr(ctx: Ctx, add) -> None:
    try:
        version = ctx.herdr.version()
    except HerdrError as exc:
        add(("fail", "herdr binary", "%s (%s)" % (describe(exc), ctx.herdr.binary)))
        version = None
    else:
        need = ".".join(str(v) for v in MIN_HERDR_VERSION)
        if version is None:
            add(("fail", "herdr version", "cannot parse `herdr --version` (%s)" % ctx.herdr.binary))
        elif version < MIN_HERDR_VERSION:
            add(("fail", "herdr version", "%s is older than %s" % (".".join(map(str, version)), need)))
        else:
            add(("ok", "herdr version", "%s (%s)" % (".".join(map(str, version)), ctx.herdr.binary)))
    if ctx.env.get("HERDR_ENV") == "1":
        add(("ok", "HERDR_ENV", "1"))
    else:
        add(("warn", "HERDR_ENV", "not set; operations other than doctor must run inside a herdr pane"))
    try:
        add(("ok", "herdr server", "reachable (%d live agents)" % len(ctx.herdr.agent_list())))
    except HerdrError as exc:
        add(("fail", "herdr server", describe(exc)))


def _check_kinds(ctx: Ctx, fleet: Fleet, add) -> None:
    try:
        kinds = ctx.herdr.kinds()
    except HerdrError as exc:
        add(("warn", "kinds", "cannot list herdr agent kinds: %s" % describe(exc)))
        return
    if kinds is None:
        add(("warn", "kinds", "cannot parse the kind list from `herdr agent`"))
        return
    errors = check_kinds(fleet, kinds)
    for error in errors:
        add(("warn", "kinds", error + " (up refuses this fleet)"))
    if not errors:
        add(("ok", "kinds", ", ".join(fleet.kinds())))


def _check_files(fleet: Fleet, add) -> None:
    paths = [("kickoff (default)", fleet.kickoff)] + [("kickoff %s" % s.name, s.kickoff) for s in fleet.seats]
    for label, path in paths:
        if path:
            readable = os.path.isfile(path) and os.access(path, os.R_OK)
            add(("ok" if readable else "fail", label, path if readable else "not readable: %s" % path))
    if fleet.state_note:
        if os.path.isfile(fleet.state_note) and os.access(fleet.state_note, os.R_OK):
            add(("ok", "state_note", fleet.state_note))
        else:
            add(("warn", "state_note", "not readable yet: %s ({state} will be empty)" % fleet.state_note))


def _check_permissions(ctx: Ctx, fleet: Fleet, add) -> None:
    """Design 16.6: advisory; warns, never fails."""
    missing: List[str] = []
    if any(seat.kind == "claude" for seat in fleet.seats):
        missing, problems = permissions.check(fleet.repo, permissions.home_dir(ctx.env.get("HOME")))
        for problem in problems:
            add(("warn", "permissions", problem))
    for seat in fleet.seats:
        label = "permissions %s" % seat.name
        if seat.kind != "claude":
            add(("skip", label, "not checked (%s)" % seat.kind))
        elif missing:
            add(("warn", label, permissions.suggestion(missing)))
        else:
            add(("ok", label, "herdr agent prompt/get/wait/read and herdr-fleet are allowed"))


def _check_quit(ctx: Ctx, fleet: Fleet, add) -> None:
    config = ctx.config
    if config.error:
        add(("fail", "plugin config", config.error))
    for seat in fleet.seats:
        command = quit_command(seat, seat.kind, config.quit)
        if command:
            add(("ok", "quit %s" % seat.name, "%s (%s)" % (command, seat.kind)))
        else:
            add(("warn", "quit %s" % seat.name, "no quit command for kind %s; restart and down will "
                 "refuse it: %s" % (seat.kind, QUIT_CONFIG_HINT)))
