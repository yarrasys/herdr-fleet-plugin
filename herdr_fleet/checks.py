"""Read-only inspection: drift (design 4.4), status (4.2) and doctor (4.7).

Nothing here changes herdr. ``collect_status`` may rewrite the state file
when it prunes ids herdr no longer knows, which every command does.
"""

from __future__ import annotations

import os
import shlex
import sys
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from herdr_fleet import MIN_HERDR_VERSION
from herdr_fleet.context import Ctx
from herdr_fleet.fleetfile import Fleet, FleetError, Seat, check_kinds
from herdr_fleet.herdr import Herdr, HerdrError, describe
from herdr_fleet.seats import (QUIT_CONFIG_HINT, agent_args, contains_in_order, find_agent_process,
                               live_by_name, process_argv, quit_command)
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


def collect_status(ctx: Ctx, integrations: bool = True) -> Dict:
    """One object describing every seat (the ``status --json`` payload)."""
    fleet, state, herdr = ctx.fleet, ctx.state, ctx.herdr
    prune_state(state, herdr)
    if state.existed:
        ctx.save()
    live = live_by_name(herdr)
    seats = []
    for seat in fleet.seats:
        agent = live.get(seat.name)
        entry = state.entry(seat.name) or {}
        adopted = bool(agent) and is_adopted(entry)
        seats.append({
            "name": seat.name,
            "kind": seat.kind,
            "live_kind": agent.get("agent") if agent else None,
            "pane_id": (agent or {}).get("pane_id") or entry.get("pane_id"),
            "state": str(agent.get("agent_status") or "unknown") if agent else "absent",
            "drift": drift_reasons(ctx, seat, agent, compare_args=not adopted) if agent else [],
            "adopted": adopted,
            "notes": seat.notes,
            "blocked_at_startup": bool(entry.get("blocked_at_startup")),
            "last_kickoff_at": entry.get("last_kickoff_at"),
        })
    data = {
        "fleet_file": fleet.path,
        "repo": fleet.repo,
        "workspace": {"label": fleet.workspace or state.data.get("workspace_label"),
                      "workspace_id": state.workspace_id,
                      "created_by_fleet": state.created_workspace},
        "seats": seats,
    }
    if integrations:
        data["integrations"] = integration_rows(herdr, fleet.kinds())
    return data


def render_status(data: Dict) -> str:
    header = ("SEAT", "KIND", "LIVE", "PANE", "STATE", "DRIFT", "NOTES")
    rows = []
    for s in data["seats"]:
        state = s["state"] + (" (blocked at startup)" if s.get("blocked_at_startup") and s["state"] != "absent" else "")
        rows.append((s["name"], s["kind"], s["live_kind"] or "-", s["pane_id"] or "-", state,
                     "; ".join((["adopted"] if s.get("adopted") else []) + s["drift"]) or "-", s["notes"] or ""))
    widths = [max(len(str(r[i])) for r in [header] + rows) for i in range(len(header) - 1)]
    lines = []
    for row in [header] + rows:
        cells = [str(cell).ljust(widths[i]) for i, cell in enumerate(row[:-1])] + [str(row[-1])]
        lines.append("  ".join(cells).rstrip())
    ws = data["workspace"]
    lines.insert(0, "fleet %s  workspace %s (%s)" % (data["fleet_file"], ws["label"] or "(caller's)",
                                                      ws["workspace_id"] or "not recorded"))
    for row in data.get("integrations", []):
        mark = "  <- %s" % row["status"] if row["flag"] else ""
        lines.append("integration %s: %s%s" % (row["kind"], row["line"] or row["status"], mark))
    return "\n".join(lines)


Check = Tuple[str, str, str]  # (ok|warn|fail, check name, detail)


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
    _check_kinds(ctx, fleet, add)
    _check_files(fleet, add)
    for row in integration_rows(ctx.herdr, fleet.kinds()):
        level = "warn" if row["flag"] else "ok"
        add((level, "integration %s" % row["kind"], row["line"] or row["status"]))
    _check_quit(ctx, fleet, add)
    try:
        state = ctx.state
        add(("ok", "state file", state.path if state.existed else "%s (created on first up)" % state.path))
    except StateError as exc:
        add(("fail", "state file", str(exc)))
    return checks


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
