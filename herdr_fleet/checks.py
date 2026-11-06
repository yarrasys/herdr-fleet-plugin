"""Read-only inspection: drift (design 4.4), status (4.2, 16.3, 16.4, 16.7, 17.2,
17.4, 18.6's watcher and queued lines, 19.1's watcher strip, 20.2's debt ages,
20.5's ``blocked (4m)``, 20.8's queue ages, 32.4's inbox wait, 32.5's handover pane,
32.6's queued kickoff, 33.2's FYI counts, 33.3's unread mailboxes, 33.4's stale debts) and
doctor (4.7, 16.6, 17.3, 18.7's hooks line, 22.1's pane names line, 33.3's unread
mailboxes).

Nothing here changes herdr. ``collect_status`` may rewrite the state file
when it prunes ids herdr no longer knows, which every command does, and records
each seat's ``state_seen`` (design 20.5). Git is
only read (``gitinfo``), and ``status --context`` only reads transcripts
(``context_size``).
"""

from __future__ import annotations

import datetime
import glob
import os
import shlex
import sys
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from herdr_fleet import (MIN_HERDR_VERSION, contacts as contact_mod, context_size, gitinfo, hooks, inbox, messages,
                         permissions, presence)
from herdr_fleet import build_identity, labels, limits, seen, watchpane
from herdr_fleet.context import Ctx
from herdr_fleet.fleetfile import Fleet, FleetError, Seat, check_kinds
from herdr_fleet.herdr import Herdr, HerdrError, describe
from herdr_fleet.seats import (QUIT_CONFIG_HINT, agent_args, contains_in_order, find_agent_process,
                               process_argv, quit_command)
from herdr_fleet.state import StateError, prune_state, read_agents

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
    on_disk = read_agents(fleet.repo)  # design 35.2: the watcher writes `limited` while this runs
    for seat in fleet.seats:
        agent = live.get(seat.name)
        entry = state.entry(seat.name) or {}
        adopted = bool(agent) and is_adopted(entry)
        failover = failover_record(seat, entry)
        limited = on_disk.get(seat.name, {}).get("limited")
        running = str((agent or {}).get("agent") or entry.get("kind") or seat.kind)
        cwd = str((agent.get("foreground_cwd") or agent.get("cwd")) if agent else seat.cwd or "")
        seats.append({
            "name": seat.name,
            "kind": seat.kind,
            "live_kind": agent.get("agent") if agent else None,
            "pane_id": (agent or {}).get("pane_id") or entry.get("pane_id"),
            "state": str(agent.get("agent_status") or "unknown") if agent else "absent",
            "cwd": cwd,
            "branch": git_branch(cwd),
            "drift": (["failed over from %s" % failover.get("from")] if failover else [])
            + (drift_reasons(ctx, seat, agent, compare_args=not adopted) if agent else []),
            "adopted": adopted,
            "notes": seat.notes,
            "blocked_at_startup": bool(entry.get("blocked_at_startup")),
            "last_kickoff_at": entry.get("last_kickoff_at"),
            "state_since": None,
        })
        if seat.prefer:  # design 35.1 and 35.3
            seats[-1].update(tier=seat.tier, prefer=list(seat.prefer), failover=failover)
        if limited and limits.counts(limited):  # design 35.2: the kind with a limit still recorded
            seats[-1]["limited"] = dict(limited)
            seats[-1]["limit_holds"] = limits.holds(limited, running)
        if context:
            reading = context_size.seat_context(agent, ctx.env) if agent else None
            hint = fleet.restart_hint_tokens
            seats[-1]["context"] = reading
            seats[-1]["restart_hint"] = bool(reading and hint and reading["tokens"] >= hint)
    recorded = seen.observe(ctx, {s["name"]: s["state"] for s in seats})  # design 20.5
    now = inbox.now_utc()
    for item in seats:
        record_ = recorded.get(item["name"]) or {}
        if record_.get("state") == item["state"]:
            item["state_since"] = record_.get("since")
        waiting = inbox.waiting(fleet.repo, item["name"], now) if item["state"] == "working" else None
        if waiting:  # design 32.4: herdr says working, but the seat waits in `inbox --wait`
            item["inbox_wait"] = {"since": waiting.get("since")}
        queued_kickoff = messages.queued_kickoff(fleet.repo, item["name"])
        if queued_kickoff is not None:  # design 32.6
            item["kickoff_queued"] = queued_kickoff.short
    data = {
        "fleet_file": fleet.path,
        "repo": fleet.repo,
        "workspace": {"label": fleet.workspace or state.data.get("workspace_label"),
                      "workspace_id": state.workspace_id,
                      "created_by_fleet": state.created_workspace},
        "seats": seats,
        "contacts": [{"name": r.contact.name, "pane_id": r.pane, "state": r.state, "note": r.contact.note,
                      "problem": r.problem()} for r in contact_mod.resolve_all(fleet, agents)],
        "owed": stale_marked(messages.owed(_log_entries(ctx)), now),
    }
    data.update(queued_summary(fleet, now))
    if state.handover:  # design 32.5 (prune_state above dropped one whose pane is gone)
        data["handover"] = dict(state.handover)
    data["watcher"] = watchpane.watcher_status(ctx)
    data["cli"] = dict(build_identity.current())
    data["watcher"]["comparison"] = (build_identity.compare(data["cli"], data["watcher"]["launch"])
                                       if data["watcher"].get("running") else "unknown")
    allowance = codex_allowance(ctx, live, seats, context)
    if allowance is not None:
        data["codex"] = allowance
    if integrations:
        data["integrations"] = integration_rows(herdr, fleet.kinds())
    return data


def failover_record(seat: Seat, entry: Mapping) -> Optional[Dict]:
    """The state entry's ``failover`` (design 35.3) for a tier seat that runs the kind it names."""
    record = entry.get("failover") if isinstance(entry, Mapping) else None
    if seat.prefer and isinstance(record, dict) and record.get("to") == seat.kind:
        return dict(record)
    return None


def codex_allowance(ctx: Ctx, live: Mapping[str, Mapping], seats: Sequence[Dict], context: bool) -> Optional[Dict]:
    """Design 35.2: the Codex allowance line's data, when any seat runs Codex. ``status`` reads no
    transcript without ``--context`` (29.2), so the reading is the one the watcher recorded in the
    state file (``codex_allowance``); with ``--context`` the live Codex seats' rollouts are read
    now, the first that yields a record winning. None when no seat runs Codex."""
    running = [s["name"] for s in seats if (s.get("live_kind") or s.get("kind")) == "codex"]
    if not running:
        return None
    reading = None
    if context:
        for name in running:
            reading = context_size.seat_allowance(live.get(name), ctx.env)
            if reading is not None:
                break
    if reading is None:
        recorded = ctx.state.data.get("codex_allowance")
        reading = dict(recorded) if isinstance(recorded, dict) and recorded.get("used_percent") is not None else None
    return {"seats": running, "reading": reading, "warn_at": ctx.fleet.failover_at_percent}


def allowance_line(info: Mapping, now: Optional[datetime.datetime] = None) -> str:
    """``codex: 83% of the weekly allowance (resets Thu 20:44)`` (design 35.2); the reading's
    window names the allowance (a week, five hours), and a missing reading says how to get one."""
    reading = info.get("reading")
    if not reading:
        return "codex: allowance not read yet (the watcher reads it every 10 minutes; status --context reads it now)"
    minutes = reading.get("window_minutes")
    window = {10080: "weekly", 300: "5-hour"}.get(minutes, "%d-minute" % minutes if minutes else "")
    when = limits.epoch_time(reading.get("resets_at"))
    percent = reading["used_percent"]
    text = "codex: %s%% of the %s allowance" % (("%d" % percent) if float(percent).is_integer() else "%.1f" % percent,
                                                 window or "account")
    if when is not None:
        text += " (resets %s)" % limits.reset_label(when)
    return text + (" - at or above %d%%" % info["warn_at"] if percent >= info.get("warn_at", 101) else "")


def queued_summary(fleet: Fleet, now: Optional[datetime.datetime] = None) -> Dict:
    """The ``queued*`` keys of ``status --json`` for every mailbox holding a message that is
    not due, seats in file order, then contacts, other names, and ``owner`` last:
    ``queued`` (every such message) and ``queued_oldest`` (the oldest one's time), as
    before; design 33.2's ``queued_fyi`` (the FYIs among them) and
    ``queued_oldest_actionable`` (the oldest message that is no FYI, for mailboxes that hold
    one); 33.3's ``queued_unread`` (mailboxes that are no seat, contact or owner).
    Read-only: due messages are not counted, and nothing moves."""
    found = inbox.queued(fleet.repo, fleet.fyi_ttl, now)
    order = [s.name for s in fleet.seats] + [c.name for c in fleet.contacts]
    names = [n for n in order if n in found] + sorted(n for n in found if n not in order and n != messages.OWNER)
    if messages.OWNER in found:
        names.append(messages.OWNER)
    oldest = {n: min(found[n], key=inbox.Message.sort_key).time for n in names}
    actionable = {n: [m for m in found[n] if not m.fyi] for n in names}
    return {"queued": {n: len(found[n]) for n in names}, "queued_oldest": oldest,
            "queued_fyi": {n: len(found[n]) - len(actionable[n]) for n in names},
            "queued_oldest_actionable": {n: min(actionable[n], key=inbox.Message.sort_key).time
                                         for n in names if actionable[n]},
            "queued_unread": [n for n in names if n not in order and n != messages.OWNER]}


def queued_line(data: Dict) -> Optional[str]:
    """``queued: sample-dev 2 (oldest 25m, +1 fyi), sample-reviewer 1 fyi, owner 1`` (ages,
    design 20.8; FYIs apart, 33.2; an unread mailbox ends ``(no seat or contact)``, 33.3);
    None when every mailbox is empty."""
    counts, oldest = data.get("queued") or {}, data.get("queued_oldest") or {}
    if not counts:
        return None
    fyis, since = data.get("queued_fyi") or {}, data.get("queued_oldest_actionable") or {}
    unread = set(data.get("queued_unread") or ())
    now = inbox.now_utc()
    parts = []
    for name, count in counts.items():
        fyi = fyis.get(name, 0)
        age = inbox.age_since(since.get(name) or oldest.get(name), now)
        parts.append(inbox.count_label(name, count - fyi, fyi, age, name in unread))
    return "queued: " + ", ".join(parts)


STALE_DEBT_S = 3 * 86400  # design 33.4: status folds debts older than this into one line
STALE_DEBT = "3d"


def stale_marked(items: List[Dict], now: datetime.datetime) -> List[Dict]:
    """The owed list with ``stale`` on each debt (design 33.4): its request was delivered
    more than STALE_DEBT_S ago."""
    for item in items:
        when = inbox.parse_time(item.get("since"))
        item["stale"] = when is not None and (now - when).total_seconds() > STALE_DEBT_S
    return items


def stale_line(items: Sequence[Dict], now: Optional[datetime.datetime] = None) -> Optional[str]:
    """``awaiting: N older than 3d (oldest AGE), not listed; …`` for the stale debts, or None."""
    stale = [o for o in items if o.get("stale")]
    if not stale:
        return None
    ages = [inbox.parse_time(o.get("since")) for o in stale]
    oldest = min((a for a in ages if a is not None), default=None)
    age = inbox.age_label(((now or inbox.now_utc()) - oldest).total_seconds()) if oldest else "?"
    return ("awaiting: %d older than %s (oldest %s), not listed; see herdr-fleet log --owed; the owner closes them "
            "with herdr-fleet ack --older-than %s --reason TEXT (--dry-run first)" % (len(stale), STALE_DEBT, age,
                                                                                   STALE_DEBT))


def git_branch(cwd: str) -> str:
    """``git -C <cwd> rev-parse --abbrev-ref HEAD``; blank when not a repository or on error."""
    return gitinfo.current_branch(cwd)


def shorten(path: str, home: Optional[str]) -> str:
    """``/home/example/projects/x`` -> ``~/projects/x``."""
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
    blocked, or past the prompt and waiting for ``kickoff`` (design 16.12); a kickoff that
    waits in its mailbox (32.6) is named instead of ``kickoff pending``."""
    blocked = seat.get("blocked_at_startup")
    if blocked and seat["state"] == "blocked":
        return " (blocked at startup)"
    if seat.get("kickoff_queued") and seat["state"] != "absent":
        return " (kickoff queued #%s)" % seat["kickoff_queued"]
    if blocked and seat["state"] in ("idle", "done", "working"):
        return " (kickoff pending)"
    return ""


def state_label(seat: Dict, now: Optional[datetime.datetime] = None) -> str:
    """The STATE cell: the state with its age (``blocked (4m)``), or ``working (inbox wait
    3m)`` while the seat waits in ``inbox --wait`` (32.4), then the startup suffix."""
    wait = seat.get("inbox_wait")
    if wait:
        age = inbox.age_since(wait.get("since"), now)
        label = "%s (inbox wait%s)" % (seat["state"], " " + age if age else "")
    else:
        label = seen.label(seat["state"], {"state": seat["state"], "since": seat.get("state_since")})
    return label + startup_label(seat) + limited_suffix(seat)


def limited_suffix(seat: Mapping) -> str:
    """``, limited (claude, since 10:41)`` for a seat whose running kind has a limit recorded
    that still holds its queue (design 35.2)."""
    return ", " + limits.status_label(seat["limited"]) if seat.get("limited") and seat.get("limit_holds") else ""


def handover_line(record: Dict) -> str:
    """``handover: swap sample-orchestrator waiting in w2:p20 since 11:40`` (design 32.5)."""
    when = inbox.parse_time(record.get("started"))
    since = when.astimezone().strftime("%H:%M") if when else "?"
    return "handover: %s %s waiting in %s since %s" % (record.get("op"), record.get("seat"), record.get("pane_id"),
                                                       since)


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
                 "STATE": state_label(s), "CONTEXT": context_size.context_label(s.get("context")),
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
    owed = data.get("owed", [])
    lines.extend(messages.owed_lines([o for o in owed if not o.get("stale")], hint=True))  # 23.4: how to settle it
    stale = stale_line(owed)  # design 33.4: the old ones in one line
    if stale:
        lines.append(stale)
    if data.get("handover"):
        lines.append(handover_line(data["handover"]))
    if data.get("codex"):
        lines.append(allowance_line(data["codex"]))
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
    if data.get("cli"):
        lines.append("CLI build: " + build_identity.label(data["cli"]))
        lines.append("watcher launch: %s; %s" % (build_identity.label(watcher.get("launch") or {}),
                                                watcher.get("comparison", "unknown")))
        if watcher.get("comparison") == "stale":
            lines.append("watcher-only reload: stop only the watcher with Ctrl-C in its pane, then run "
                         "herdr-fleet watch --fleet %s there; leave coding-agent panes/sessions running."
                         % shlex.quote(str(data.get("fleet_file") or "PATH")))
    return "\n".join(lines)


Check = Tuple[str, str, str]  # (ok|warn|fail|skip|info, check name, detail)


def stranded_mail(repo: str, env: Optional[Mapping[str, str]] = None) -> List[Check]:
    """Design 39.1: queued files in linked worktrees no watcher delivers from."""
    paths = gitinfo.worktree_paths(repo, env)
    if not paths:
        return []
    found: List[Check] = []
    for worktree in paths:
        if os.path.realpath(worktree) == os.path.realpath(repo):
            continue
        herdr = os.path.join(worktree, ".herdr")
        if os.path.exists(os.path.join(herdr, "fleet.state.json")):
            continue
        counts: Dict[str, int] = {}
        pattern = os.path.join(glob.escape(herdr), "inbox", "*", "new", "*.md")
        for path in glob.glob(pattern):
            if os.path.isfile(path):
                name = os.path.basename(os.path.dirname(os.path.dirname(path)))
                counts[name] = counts.get(name, 0) + 1
        if not counts:
            continue
        total = sum(counts.values())
        names = ", ".join("%s %d" % (name, counts[name]) for name in sorted(counts))
        found.append(("warn", "stranded mail", "%d queued in %s (%s); nothing delivers them: "
                      "herdr-fleet inbox --fleet %s --as NAME --read"
                      % (total, worktree, names, os.path.join(herdr, "fleet.toml"))))
    return found


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
    checks.extend(stranded_mail(fleet.repo, ctx.env))
    _check_kinds(ctx, fleet, add)
    _check_files(fleet, add)
    for row in integration_rows(ctx.herdr, fleet.kinds()):
        level = "warn" if row["flag"] else "ok"
        add((level, "integration %s" % row["kind"], row["line"] or row["status"]))
    _check_quit(ctx, fleet, add)
    _check_tiers(ctx, fleet, add)
    _check_presence(ctx, fleet, add)
    _check_pane_names(ctx, add)
    _check_permissions(ctx, fleet, add)
    if any("claude" in seat.kinds() for seat in fleet.seats):  # design 18.7: the hooks are Claude Code's
        add(hooks.doctor_check(fleet.repo))
    _check_unread(ctx, fleet, add)
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
        agents = ctx.herdr.agent_list()
        add(("ok", "herdr server", "reachable (%d live agents)" % len(agents)))
        if ctx.invocation.codex_thread:
            from herdr_fleet.context import resolve_caller
            resolve_caller(ctx, agents)
            inv = ctx.invocation
            detail = ("%s (%s)" % (inv.pane_id, inv.caller_reason) if inv.pane_id else inv.caller_reason)
            add(("info", "caller", "by Codex thread %s: %s" % (inv.codex_thread, detail)))
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


def _check_unread(ctx: Ctx, fleet: Fleet, add) -> None:
    """Design 33.3: a mailbox holding messages that are not due, whose name is no seat,
    contact or ``owner`` here: the watcher does not deliver it, though it can be 31's
    queue for a live agent with no fleet of its own, which ``deliver NAME`` delivers.
    Read-only; warns, never fails."""
    try:
        summary = queued_summary(fleet)
    except OSError as exc:
        add(("warn", "inbox", "cannot read the mailboxes: %s" % exc))
        return
    now = inbox.now_utc()
    flag = messages.fleet_flag(ctx)
    for name in summary["queued_unread"]:
        add(("warn", "inbox", "%s holds %d queued (oldest %s); it is no seat or contact here, so the watcher does "
             "not deliver it. Read it with herdr-fleet inbox%s --as %s --read, deliver it with herdr-fleet deliver%s "
             "%s if %s is a live agent, or (%s or the owner) drop it with herdr-fleet inbox%s --as %s --drop"
             % (name, summary["queued"][name], inbox.age_since(summary["queued_oldest"][name], now) or "?",
                flag, name, flag, name, name, name, flag, name)))


def _check_permissions(ctx: Ctx, fleet: Fleet, add) -> None:
    """Design 16.6: advisory; warns, never fails."""
    missing: List[str] = []
    if any("claude" in seat.kinds() for seat in fleet.seats):  # design 35.1: a fallback may be Claude too
        missing, problems = permissions.check(fleet.repo, permissions.home_dir(ctx.env.get("HOME")))
        for problem in problems:
            add(("warn", "permissions", problem))
    for seat in fleet.seats:
        label = "permissions %s" % seat.name
        if "claude" not in seat.kinds():
            add(("skip", label, "not checked (%s)" % "/".join(seat.kinds())))
        elif missing:
            add(("warn", label, permissions.suggestion(missing)))
        else:
            add(("ok", label, "herdr agent prompt/get/wait/read and herdr-fleet are allowed"))


def _check_tiers(ctx: Ctx, fleet: Fleet, add) -> None:
    """Design 35.1 and 35.3: one line per tier seat naming its kinds in order and, after a failover,
    the kind it runs and how to return; one when a limit is recorded; one for a kind without limit
    patterns (it is never read for a limit, 35.2). Informational."""
    from herdr_fleet.seats import LIMIT_PATTERNS
    recorded = read_agents(fleet.repo)
    for seat in fleet.seats:
        if not seat.prefer:
            continue
        entry = recorded.get(seat.name, {})
        failover = failover_record(seat, entry)
        text = " > ".join(seat.prefer) + (" (tier %s)" % seat.tier)
        if failover:
            text += "; failed over from %s to %s, `swap %s` returns it" % (failover.get("from"), seat.kind, seat.name)
        add(("info", "tier %s" % seat.name, text))
        limited = entry.get("limited")
        if limits.counts(limited):
            add(("info", "limit %s" % seat.name, limits.status_label(limited)))
    unread = sorted({k for seat in fleet.seats for k in seat.kinds() if seat.prefer and k not in LIMIT_PATTERNS})
    for kind in unread:
        add(("info", "limits", "no limit pattern for kind %s; its seats are never read for a provider limit" % kind))


def _check_presence(ctx: Ctx, fleet: Fleet, add) -> None:
    """Design 21.2 item 3: the presence guard cannot check a kind with no prompt glyph, so
    its seats are not held while the owner types. The kind is the live agent's, else the
    fleet file's. Informational only."""
    try:
        live = {str(a["name"]): str(a.get("agent") or "") for a in ctx.herdr.agent_list() if a.get("name")}
    except HerdrError:
        live = {}
    kinds = [live.get(seat.name) or seat.kind for seat in fleet.seats] + [
        kind for seat in fleet.seats for kind in seat.kinds()]  # design 35.1: the fallbacks too
    for kind in presence.unchecked_kinds(kinds):
        add(("info", "presence", "no prompt glyph for kind %s; its seats are not held while the owner types" % kind))


def _check_pane_names(ctx: Ctx, add) -> None:
    """Design 22.1: are the live seats' panes named as wanted? Read-only (``agent list``
    and one ``pane get`` per live seat); ``status`` names them. Silent when herdr
    cannot be asked (the ``herdr server`` line already says so)."""
    try:
        missing = labels.unnamed(ctx)
    except HerdrError:
        return
    if missing:
        add(("info", "pane names", "%d seat%s unnamed (%s); run herdr-fleet status"
             % (len(missing), "" if len(missing) == 1 else "s", ", ".join(missing))))
    else:
        add(("ok", "pane names", "every live seat's pane is named"))


def _check_quit(ctx: Ctx, fleet: Fleet, add) -> None:
    config = ctx.config
    if config.error:
        add(("fail", "plugin config", config.error))
    for problem in config.problems:  # design 23.7: an [interrupt] entry left out
        add(("warn", "plugin config", problem))
    for seat in fleet.seats:
        for kind in seat.kinds():  # design 35.1: a tier seat may run any kind of its prefer list
            command = quit_command(seat, kind, config.quit)
            where = seat.name if len(seat.kinds()) == 1 else "%s/%s" % (seat.name, kind)
            if command:
                add(("ok", "quit %s" % where, "%s (%s)" % (command, kind)))
            else:
                add(("warn", "quit %s" % where, "no quit command for kind %s; restart and down will "
                     "refuse it: %s" % (kind, QUIT_CONFIG_HINT)))
