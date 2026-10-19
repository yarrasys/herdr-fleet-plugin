"""Operations that change the fleet: up (design 4.1), restart (4.3), swap
(4.4), kickoff (4.5) and down (4.6), plus ``ui`` and ``install-cli``.

Each operation returns a ``Report``; ``cli`` prints it and maps it to an exit
code. Text reaches agents only through ``seats.safe_prompt``; agents start
only through ``seats.start_in_pane``; quitting goes through
``seats.quit_agent``. The checks here decide whether those are called at all:
blocked and unknown agents are refused, working ones need ``--wait``, the
caller's own pane is never started into, restarted or quit, and only panes and
workspaces recorded as created by herdr-fleet are ever closed.
"""

from __future__ import annotations

import os
import shlex
from typing import Dict, List, Optional, Sequence, TextIO

from herdr_fleet import PLUGIN_ID
from herdr_fleet.bringup import (Target, ensure_seat, in_target, label_for, resolve_target, send_kickoffs,
                                split_source, start_seat)
from herdr_fleet.checks import drift_reasons
from herdr_fleet.context import Ctx, Report, UsageError
from herdr_fleet.fleetfile import Fleet, FleetError, Seat, check_kinds
from herdr_fleet.herdr import HerdrError, describe, has_agent, render_cmd, start_argv
from herdr_fleet.seats import (QUIT_CONFIG_HINT, SETTLED, at_shell_prompt, describe_foreground,
                               live_by_name, quit_agent, quit_command)
from herdr_fleet.state import prune_state

WAIT_UNTIL = ("idle", "done", "blocked")


# -- shared helpers ------------------------------------------------------------


def select_seats(fleet: Fleet, names: Sequence[str], required: bool = False) -> List[Seat]:
    """Seats named on the command line, in file order (all seats when none named)."""
    if not names:
        if required:
            raise UsageError("name at least one seat (seats: %s)" % ", ".join(s.name for s in fleet.seats))
        return list(fleet.seats)
    unknown = [n for n in names if fleet.seat(n) is None]
    if unknown:
        raise UsageError("not a seat in %s: %s (seats: %s)"
                         % (fleet.path, ", ".join(unknown), ", ".join(s.name for s in fleet.seats)))
    wanted = set(names)
    return [s for s in fleet.seats if s.name in wanted]


def refuse_unknown_kinds(ctx: Ctx) -> None:
    """Operations that start agents refuse kinds the installed herdr lacks."""
    try:
        kinds = ctx.herdr.kinds()
    except HerdrError:
        kinds = None
    errors = check_kinds(ctx.fleet, kinds)
    if errors:
        raise FleetError(errors)


def prune(ctx: Ctx) -> None:
    for message in prune_state(ctx.state, ctx.herdr):
        ctx.say(message)
    ctx.save()


# -- up ------------------------------------------------------------------------------


def op_up(ctx: Ctx, names: Sequence[str], me: Optional[str] = None, no_kickoff: bool = False,
          note: Optional[str] = None) -> Report:
    report = Report("up")
    fleet = ctx.fleet
    selected = {s.name for s in select_seats(fleet, names)}
    if me and fleet.seat(me) is None:
        raise UsageError("--me %s: not a seat in %s" % (me, fleet.path))
    refuse_unknown_kinds(ctx)
    prune(ctx)
    live = live_by_name(ctx.herdr)
    pending = [s for s in fleet.seats if s.name in selected and s.name != me and s.name not in live]
    target = resolve_target(ctx, report, pending[0] if pending else None)
    if me:
        _adopt_caller(ctx, fleet.seat(me), live, report)
    else:
        _hint_caller(ctx)
    cache: Dict[str, Optional[str]] = {}
    prev: Optional[str] = None
    started: List[Seat] = []
    for seat in fleet.seats:
        if seat.name in selected and seat.name != me:
            pane, now = ensure_seat(ctx, seat, target, prev, live, report)
            if now:
                started.append(seat)
        else:
            pane = (live.get(seat.name) or {}).get("pane_id") or ctx.state.pane_of(seat.name)
        if pane and in_target(ctx, str(pane), target, cache):
            prev = str(pane)
    if started and no_kickoff:
        ctx.say("kickoffs skipped (--no-kickoff)")
    elif started:
        send_kickoffs(ctx, started, "up", note, target.label, report)
    ctx.save()
    return report


def _adopt_caller(ctx: Ctx, seat: Seat, live: Dict[str, Dict], report: Report) -> None:
    """``--me NAME``: name the caller's own agent as a seat; never start or brief it."""
    pane = ctx.invocation.pane_id
    if not pane:
        report.add(seat.name, "failed", "--me needs to run inside a herdr pane (HERDR_PANE_ID)", "fail")
        return
    try:
        info = ctx.herdr.pane_get(pane)
    except HerdrError as exc:
        report.add(seat.name, "failed", "--me: %s" % describe(exc), "fail")
        return
    kind = info.get("agent")
    if not kind:
        report.add(seat.name, "failed", "--me: no agent is running in your pane %s" % pane, "fail")
        return
    if kind != seat.kind:
        report.add(seat.name, "failed",
                   "--me: your pane runs %s but seat %s is %s" % (kind, seat.name, seat.kind), "fail")
        return
    holder = live.get(seat.name)
    if holder and holder.get("pane_id") != pane:
        report.add(seat.name, "conflict",
                   "--me: %s is already live in %s" % (seat.name, holder.get("pane_id")), "partial")
        return
    if ctx.herdr.pane_agent(pane).get("name") != seat.name:
        if ctx.dry_run:
            report.add(seat.name, "plan", render_cmd(["agent", "rename", pane, seat.name]) + " (you; no kickoff)")
            live[seat.name] = dict(info, name=seat.name, pane_id=pane)
            return
        try:
            ctx.herdr.agent_rename(pane, seat.name)
        except HerdrError as exc:
            report.add(seat.name, "failed", "--me: rename: %s" % describe(exc), "fail")
            return
    if (ctx.state.entry(seat.name) or {}).get("pane_id") != pane:
        ctx.state.drop(seat.name)
    ctx.state.update(seat.name, pane_id=pane, created_pane=False, kind=kind, blocked_at_startup=False)
    ctx.save()
    live[seat.name] = dict(info, name=seat.name, pane_id=pane)
    report.add(seat.name, "adopted", "your pane %s is now %s; no kickoff sent" % (pane, seat.name))


def _hint_caller(ctx: Ctx) -> None:
    pane = ctx.invocation.pane_id
    if not pane:
        return
    try:
        info = ctx.herdr.pane_get(pane)
    except HerdrError:
        return
    if has_agent(info) and not ctx.herdr.pane_agent(pane).get("name"):
        ctx.say("hint: your pane %s runs an unnamed %s agent; herdr-fleet leaves it alone. "
                "Run `herdr-fleet up --me <seat>` to make it a seat." % (pane, info.get("agent")))


# -- restart and swap --------------------------------------------------------------


def settle(ctx: Ctx, seat: Seat, agent: Dict, wait_ms: Optional[int], report: Report) -> Optional[Dict]:
    """Return the agent if it may be quit now (idle or done), else report why not."""
    status = str(agent.get("agent_status") or "unknown")
    if status == "working" and wait_ms:
        if ctx.dry_run:
            report.add(seat.name, "plan", "wait up to %d ms for it to settle, then re-check" % wait_ms)
            return agent
        try:
            ctx.herdr.agent_wait(seat.name, WAIT_UNTIL, wait_ms)
        except HerdrError as exc:
            report.add(seat.name, "refused",
                       "still working after --wait %d ms (%s)" % (wait_ms, describe(exc)), "partial")
            return None
        try:
            agent = ctx.herdr.agent_get(seat.name)
        except HerdrError as exc:
            report.add(seat.name, "failed", "agent vanished while waiting (%s)" % describe(exc), "fail")
            return None
        status = str(agent.get("agent_status") or "unknown")
    if status in ("blocked", "unknown"):
        report.add(seat.name, "refused", "agent is %s; herdr-fleet never prompts, quits or restarts a %s agent"
                   % (status, status), "partial")
        return None
    if status == "working":
        report.add(seat.name, "refused", "agent is working; pass --wait MS to let it settle first", "partial")
        return None
    if status not in SETTLED:
        report.add(seat.name, "refused", "agent state %r is not idle or done" % status, "partial")
        return None
    return agent


def _quit_live(ctx: Ctx, seat: Seat, agent: Dict, report: Report, then: str = "") -> Optional[str]:
    """Quit a settled live agent. Returns its pane when it is gone."""
    pane = str(agent.get("pane_id"))
    live_kind = str(agent.get("agent") or "")
    command = quit_command(seat, live_kind, ctx.config.quit)
    if not command:
        report.add(seat.name, "refused", "no quit command for kind %s; %s" % (live_kind, QUIT_CONFIG_HINT), "partial")
        return None
    if ctx.dry_run:
        report.add(seat.name, "plan", "quit %s (%s, %s in %s) with herdr agent prompt %s %s%s"
                   % (seat.name, live_kind, agent.get("agent_status"), pane, seat.name,
                      shlex.quote(command), then))
        return pane
    outcome = quit_agent(ctx.herdr, ctx.clock, seat.name, pane, command, ctx.quit_timeout_ms())
    if not outcome.ok:
        report.add(seat.name, outcome.status, outcome.detail, "partial" if outcome.status == "refused" else "fail")
        return None
    report.add(seat.name, "quit", outcome.detail)
    return pane


def op_restart(ctx: Ctx, names: Sequence[str], note: Optional[str] = None, wait_ms: Optional[int] = None,
               reason: str = "restart") -> Report:
    report = Report(reason)
    seats = select_seats(ctx.fleet, names, required=True)
    refuse_unknown_kinds(ctx)
    prune(ctx)
    live = live_by_name(ctx.herdr)
    target: Optional[Target] = None
    to_brief: List[Seat] = []
    for seat in seats:
        agent = live.get(seat.name)
        if agent is None:
            if target is None:
                target = resolve_target(ctx, report, seat)
            _, started = ensure_seat(ctx, seat, target, split_source(ctx, seat, live, target), live, report)
            if started:
                to_brief.append(seat)
            continue
        if agent.get("pane_id") == ctx.invocation.pane_id:
            report.add(seat.name, "refused", "it is the caller's own agent; restart it from another pane", "partial")
            continue
        agent = settle(ctx, seat, agent, wait_ms, report)
        if agent is None:
            continue
        then = ", then " + render_cmd(start_argv(seat.name, seat.kind, str(agent.get("pane_id")), seat.args))
        pane = _quit_live(ctx, seat, agent, report, then)
        if pane is None or ctx.dry_run:
            if pane is not None:
                to_brief.append(seat)
            continue
        entry = ctx.state.entry(seat.name) or {}
        created = bool(entry.get("created_pane")) and entry.get("pane_id") == pane
        if start_seat(ctx, seat, pane, created, report):
            to_brief.append(seat)
    if to_brief:
        send_kickoffs(ctx, to_brief, reason, note, label_for(ctx, target), report)
    ctx.save()
    return report


def op_swap(ctx: Ctx, names: Sequence[str], note: Optional[str] = None,
            wait_ms: Optional[int] = None) -> Report:
    """Restart exactly the seats that drifted from the file; start absent ones."""
    report = Report("swap")
    seats = select_seats(ctx.fleet, names)
    refuse_unknown_kinds(ctx)
    prune(ctx)
    live = live_by_name(ctx.herdr)
    changed: List[str] = []
    for seat in seats:
        agent = live.get(seat.name)
        if agent is None:
            changed.append(seat.name)
            report.add(seat.name, "would start" if ctx.dry_run else "absent", "not running")
            continue
        reasons = drift_reasons(ctx, seat, agent)
        if reasons:
            changed.append(seat.name)
            report.add(seat.name, "would restart" if ctx.dry_run else "drifted", "; ".join(reasons))
        else:
            report.add(seat.name, "unchanged", "matches the fleet file")
    if changed and not ctx.dry_run:
        report.extend(op_restart(ctx, changed, note, wait_ms, reason="swap"))
    return report


# -- kickoff and down --------------------------------------------------------------


def op_kickoff(ctx: Ctx, names: Sequence[str], note: Optional[str] = None) -> Report:
    """Resend kickoffs. safe_prompt refuses blocked, unknown, working and absent agents."""
    report = Report("kickoff")
    seats = select_seats(ctx.fleet, names, required=True)
    prune(ctx)
    send_kickoffs(ctx, seats, "kickoff", note, label_for(ctx, None), report)
    return report


def op_down(ctx: Ctx, names: Sequence[str], wait_ms: Optional[int] = None, close_panes: bool = False,
            close_workspace: bool = False) -> Report:
    report = Report("down")
    seats = select_seats(ctx.fleet, names)
    prune(ctx)
    live = live_by_name(ctx.herdr)
    for seat in seats:
        agent = live.get(seat.name)
        if agent is None:
            report.add(seat.name, "down", "not running")
            continue
        if agent.get("pane_id") == ctx.invocation.pane_id:
            report.add(seat.name, "refused", "it is the caller's own agent; quit it yourself", "partial")
            continue
        agent = settle(ctx, seat, agent, wait_ms, report)
        if agent is None:
            continue
        pane = _quit_live(ctx, seat, agent, report)
        if pane and not ctx.dry_run and ctx.state.pane_of(seat.name) != pane:
            ctx.state.drop(seat.name)
            ctx.state.update(seat.name, pane_id=pane, created_pane=False)
    ctx.save()
    if close_panes:
        _close_panes(ctx, seats, report)
    if close_workspace:
        _close_workspace(ctx, report)
    ctx.save()
    return report


def _close_panes(ctx: Ctx, seats: Sequence[Seat], report: Report) -> None:
    """Close panes herdr-fleet created, and only once they are back at a shell."""
    live = live_by_name(ctx.herdr)
    for seat in seats:
        entry = ctx.state.entry(seat.name) or {}
        pane = entry.get("pane_id")
        if not pane or not entry.get("created_pane"):
            continue
        if seat.name in live:
            report.add(seat.name, "pane kept", "%s still hosts the live agent" % pane, "partial")
            continue
        try:
            info = ctx.herdr.pane_get(pane)
            procs = ctx.herdr.process_info(pane)
        except HerdrError as exc:
            report.add(seat.name, "pane kept", describe(exc), "fail")
            continue
        if has_agent(info) or not at_shell_prompt(procs):
            report.add(seat.name, "pane kept", "%s is busy (foreground: %s)" % (pane, describe_foreground(procs)),
                       "partial")
            continue
        try:
            ctx.herdr.pane_close(pane)
        except HerdrError as exc:
            report.add(seat.name, "pane kept", describe(exc), "fail")
            continue
        ctx.state.drop(seat.name)
        ctx.save()
        report.add(seat.name, "pane closed", pane)


def _close_workspace(ctx: Ctx, report: Report) -> None:
    """Close the workspace only if herdr-fleet created it and no agent is left."""
    state = ctx.state
    workspace = state.workspace_id
    if not workspace or not state.created_workspace:
        report.add("workspace", "kept", "%s was not created by herdr-fleet, so it is never closed"
                   % (workspace or "the fleet workspace"), "partial")
        return
    seat_names = {s.name for s in ctx.fleet.seats}
    remaining = [str(a.get("name") or a.get("pane_id")) for a in ctx.herdr.agent_list()
                 if a.get("name") in seat_names or a.get("workspace_id") == workspace]
    if remaining:
        report.add("workspace", "kept", "%s: agents still live: %s" % (workspace, ", ".join(remaining)), "partial")
        return
    try:
        ctx.herdr.workspace_close(workspace)
    except HerdrError as exc:
        report.add("workspace", "kept", describe(exc), "fail")
        return
    state.set_workspace(None, state.data.get("workspace_label"), False)
    prune_state(state, ctx.herdr)
    ctx.save()
    report.add("workspace", "closed", workspace)


# -- ui and install-cli ------------------------------------------------------------------


def op_ui(ctx: Ctx, entrypoint: str) -> None:
    """Open a plugin pane with its cwd at the repo root so it can find the fleet file."""
    repo = ctx.fleet.repo
    plugin = ctx.env.get("HERDR_PLUGIN_ID") or PLUGIN_ID
    ctx.herdr.plugin_pane_open(plugin, entrypoint, repo)


def op_install_cli(root: str, directory: Optional[str], out: TextIO, path_env: str) -> int:
    """Symlink bin/herdr-fleet into DIR (default ~/.local/bin)."""
    source = os.path.join(root, "bin", "herdr-fleet")
    folder = os.path.abspath(os.path.expanduser(directory or "~/.local/bin"))
    link = os.path.join(folder, "herdr-fleet")
    out.write("plugin root: %s\n" % root)
    if os.path.islink(link):
        if os.path.realpath(link) != os.path.realpath(source):
            out.write("herdr-fleet: error: %s already points to %s; remove it first\n" % (link, os.readlink(link)))
            return 1
        out.write("already linked: %s -> %s\n" % (link, source))
    elif os.path.exists(link):
        out.write("herdr-fleet: error: %s exists and is not a symlink; remove it first\n" % link)
        return 1
    else:
        os.makedirs(folder, exist_ok=True)
        os.symlink(source, link)
        out.write("linked %s -> %s\n" % (link, source))
    if folder not in path_env.split(os.pathsep):
        out.write("note: %s is not on PATH\n" % folder)
    return 0
