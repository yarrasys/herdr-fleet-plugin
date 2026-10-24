"""Operations that change the fleet: up (design 4.1, with the watcher strip of
section 19 and the top band of section 26), restart (4.3), swap (4.4), kickoff (4.5) and down (4.6), plus
``go`` (22.2), ``peek`` (23.6), ``interrupt`` (23.7), ``ui`` and ``install-cli``.
Each of up, restart, swap and kickoff ends by naming the seats' panes
(``labels.apply``, design 22.1); ``down`` clears the name of a seat pane it
leaves open at a shell prompt.

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
from typing import Callable, Dict, List, Optional, Sequence, Set, TextIO, Tuple

from herdr_fleet import PLUGIN_ID, inbox, labels, presence, watchpane
from herdr_fleet.bringup import (claim_seat, ensure_seat, follow_pane, label_for, label_of, require_caller_context,
                                resolve_target, send_kickoffs, start_claimed, start_seat)
from herdr_fleet.checks import drift_reasons, is_adopted
from herdr_fleet.context import EXIT_FAIL, EXIT_OK, EXIT_PARTIAL, Ctx, Report, UsageError
from herdr_fleet.fleetfile import Fleet, FleetError, Seat, check_kinds
from herdr_fleet.herdr import HerdrError, describe, has_agent, pane_move_argv, render_cmd, start_argv
from herdr_fleet.messages import OWNER, drop_queued, interrupt_entry, record, resolve_sender, resolve_targets
from herdr_fleet.placement import Target, band_anchor, band_check, existing_panes, place_band, place_chain, place_grid
from herdr_fleet.seats import (DEFAULT_INTERRUPT_WAIT_MS, EXIT_DIALOG_LINES, QUIT_CONFIG_HINT, SETTLED,
                               at_shell_prompt, describe_foreground, dialog_first_line, interrupt_agent,
                               interrupt_keys, live_by_name, quit_agent, quit_command, state_words)
from herdr_fleet.state import pane_moved, prune_state

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


def drop_inboxes(ctx: Ctx, seats: Sequence[Seat], report: Report) -> None:
    """``--drop-inbox`` (design 18.5): before anything else, move every queued message of
    these seats to ``cur/``, logged ``dropped``, so the kickoff starts clean."""
    for seat in seats:
        if ctx.dry_run:
            count = len(inbox.queued(ctx.fleet.repo).get(seat.name, []))
            if count:
                report.add(seat.name, "plan", "drop %d queued message%s" % (count, "" if count == 1 else "s"))
            continue
        count = drop_queued(ctx, seat.name)
        if count:
            report.add(seat.name, "dropped", "%d queued message%s moved to cur/ (inbox --redeliver brings one back)"
                       % (count, "" if count == 1 else "s"))


# -- up ------------------------------------------------------------------------------


def op_up(ctx: Ctx, names: Sequence[str], me: Optional[str] = None, no_kickoff: bool = False,
          note: Optional[str] = None, join: bool = False, no_watch: bool = False) -> Report:
    """Design 4.1, 14, 19 and 26: classify every seat, find or create the workspace,
    place the seats that need panes (chain, grid, or the grid under a top band), start
    them, brief them, and start the watcher in a strip below them unless one runs already."""
    report = Report("up")
    fleet = ctx.fleet
    selected = {s.name for s in select_seats(fleet, names)}
    if me and fleet.seat(me) is None:
        raise UsageError("--me %s: not a seat in %s" % (me, fleet.path))
    if join and not me:
        raise UsageError("--join moves your own pane, so it needs --me NAME")
    refuse_unknown_kinds(ctx)
    require_caller_context(ctx)
    prune(ctx)
    strip = watchpane.prepare(ctx, no_watch, report)
    live = live_by_name(ctx.herdr)
    caller = _adopt_caller(ctx, fleet.seat(me), live, report) if me else None
    if not me:
        _hint_caller(ctx)
    order = [s for s in fleet.seats if s.name in selected and s.name != me]
    claims = {s.name: claim_seat(ctx, s, live, report) for s in order}
    needs = [s for s in order if claims[s.name].state == "needs"]
    # Design 26.2: a workspace created for a top band gets its root pane for the top seat.
    top = next((s for s in needs if s.name == fleet.top), None) if fleet.mode == "grid" else None
    target = resolve_target(ctx, report, top or (needs[0] if needs else None))
    if caller is not None:
        _join_or_warn(ctx, fleet.seat(me), caller, target, live, join, report)
    # Design 19.1: on a fresh layout the strip is split from its first pane before
    # anything else is, so it spans every seat; otherwise it opens after the kickoffs.
    layout_fresh = bool(needs) and not existing_panes(ctx, needs, live, target)
    fresh = strip is not None and layout_fresh
    band, check = band_anchor(ctx, me, caller is not None, needs, target, layout_fresh, report)
    on_area = strip.open_at if fresh else None
    if band is not None:
        placed = place_band(ctx, needs, target, report, band, on_area=on_area)
        order = sorted(order, key=lambda s: s.name != fleet.top)  # the top seat starts first
    else:
        place = place_grid if fleet.mode == "grid" else place_chain
        placed = place(ctx, needs, target, live, report, on_area=on_area)
    started = [s for s in order if start_claimed(ctx, s, claims[s.name], placed, report)]
    if started and no_kickoff:
        ctx.say("kickoffs skipped (--no-kickoff)")
    elif started:
        send_kickoffs(ctx, started, "up", note, target.label, report)
    if strip is not None:
        up = bool(started) or any(s.name in live for s in fleet.seats) \
            or any(c.state in ("live", "adopted") for c in claims.values())
        strip.open_after(live, placed, target, up)
    ctx.save()
    now = live_by_name(ctx.herdr) if check else None
    labels.apply(ctx, now, report)  # design 22.1: every live seat, adopted and --me seats included
    if now is not None:
        band_check(ctx, now, target, report)  # design 26.2: the report ends with the tile hint
    return report


def _adopt_caller(ctx: Ctx, seat: Seat, live: Dict[str, Dict], report: Report) -> Optional[Dict]:
    """``--me NAME``: name the caller's own agent as a seat; never start or brief it.
    Returns the caller's pane record once adopted, else None."""
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
    if not kind:  # design 14.4: say what to do instead; the other seats still come up (exit 3)
        report.add(seat.name, "failed",
                   "--me: no agent is running in your pane %s; start %s as a seat instead with "
                   "`herdr-fleet up %s`, or start a %s agent in this pane first and run "
                   "`herdr-fleet up --me %s` from inside it" % (pane, seat.name, seat.name, seat.kind, seat.name),
                   "partial")
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
            return info
        try:
            ctx.herdr.agent_rename(pane, seat.name)
        except HerdrError as exc:
            report.add(seat.name, "failed", "--me: rename: %s" % describe(exc), "fail")
            return
    if (ctx.state.entry(seat.name) or {}).get("pane_id") != pane:
        ctx.state.drop(seat.name)
    ctx.state.update(seat.name, pane_id=pane, created_pane=False, kind=kind, blocked_at_startup=False,
                     adopted=True)
    ctx.save()
    live[seat.name] = dict(info, name=seat.name, pane_id=pane)
    report.add(seat.name, "adopted", "your pane %s is now %s; no kickoff sent" % (pane, seat.name))
    return info


def active_tab(ctx: Ctx, workspace_id: str) -> Optional[str]:
    """A workspace's active tab: ``workspace list`` row, else its first tab."""
    for ws in ctx.herdr.workspace_list():
        if ws.get("workspace_id") == workspace_id and ws.get("active_tab_id"):
            return str(ws["active_tab_id"])
    tabs = ctx.herdr.tab_list(workspace_id)
    return str(tabs[0]["tab_id"]) if tabs else None


def _join_or_warn(ctx: Ctx, seat: Seat, caller: Dict, target: Target, live: Dict[str, Dict], join: bool,
                  report: Report) -> None:
    """Design 15.2: an adopted caller outside the fleet's workspace splits the
    team. Say so with the exact move; with --join, make it and record the new pane."""
    pane = str(caller.get("pane_id") or ctx.invocation.pane_id)
    here = str(caller.get("workspace_id") or ctx.invocation.workspace_id or "")
    if target.workspace_id is not None and here == target.workspace_id:
        if join:
            report.add(seat.name, "joined", "already in the fleet's workspace %s" % here)
        return
    there = target.workspace_id or "(new)"
    tab = active_tab(ctx, target.workspace_id) if target.workspace_id else "<active tab of %s>" % target.label
    if not tab:
        report.add(seat.name, "warning", "cannot find a tab in the fleet's workspace %s" % there, "partial")
        return
    argv = pane_move_argv(pane, tab, "right", 0.5)
    if not join:
        report.add(seat.name, "warning",
                   "your pane %s is in workspace %s (%s) but the fleet's workspace is %s (%s), where the other "
                   "seats open; to join them run `%s`, or rerun up with --join"
                   % (pane, here, label_of(ctx, here) if here else "?", there, target.label, render_cmd(argv)))
        return
    if ctx.dry_run:
        report.add(seat.name, "plan", "%s (join the fleet's workspace)" % render_cmd(argv))
        moved = "<pane of %s after --join>" % seat.name
    else:
        try:
            moved = ctx.herdr.pane_move_to_tab(pane, tab, "right", 0.5)
        except HerdrError as exc:
            report.add(seat.name, "failed", "--join: %s; your pane stays where it is" % describe(exc), "fail")
            return
        ctx.state.update(seat.name, pane_id=moved)
        ctx.save()
        ctx.say(pane_moved(seat.name, pane, moved))
        report.add(seat.name, "joined", "moved your pane %s into %s %s; it is now %s" % (pane, there, tab, moved))
    # From here on the caller is in the fleet's workspace: guard its new pane id,
    # and in an existing workspace let new seats split off it (design 14.2).
    ctx.invocation.pane_id, ctx.invocation.workspace_id = moved, target.workspace_id
    live[seat.name] = dict(live.get(seat.name) or caller, pane_id=moved, workspace_id=target.workspace_id)
    if not target.free_pane:
        target.anchor = moved


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
                   % (state_words(status), status), "partial")
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
        hold = presence.would_hold(ctx.herdr, agent=agent)  # design 21.2: the guard only reads
        if hold:
            report.add(seat.name, "plan", "would hold: %s (the quit is refused while the owner types there)" % hold)
            return None
        report.add(seat.name, "plan", "quit %s (%s, %s in %s) with herdr agent prompt %s %s%s"
                   % (seat.name, live_kind, agent.get("agent_status"), pane, seat.name,
                      shlex.quote(command), then))
        return pane
    outcome = quit_agent(ctx.herdr, ctx.clock, seat.name, pane, command, ctx.quit_timeout_ms(),
                         answer_exit=ctx.keys_allowed)  # design 23.7: never from a popup
    if not outcome.ok:
        report.add(seat.name, outcome.status, outcome.detail, "partial" if outcome.status == "refused" else "fail")
        return None
    report.add(seat.name, "quit", outcome.detail)
    return pane


def op_restart(ctx: Ctx, names: Sequence[str], note: Optional[str] = None, wait_ms: Optional[int] = None,
               reason: str = "restart", drop_inbox: bool = False, name_panes: bool = True) -> Report:
    """Design 4.3. ``name_panes=False`` leaves the pane names to the caller (``swap``
    names them once, at its end)."""
    report = Report(reason)
    seats = select_seats(ctx.fleet, names, required=True)
    refuse_unknown_kinds(ctx)
    prune(ctx)
    if drop_inbox:
        drop_inboxes(ctx, seats, report)
    live = live_by_name(ctx.herdr)
    target: Optional[Target] = None
    to_brief: List[Seat] = []
    for seat in seats:
        agent = live.get(seat.name)
        if agent is None:
            if target is None:
                target = resolve_target(ctx, report, seat)
            if ensure_seat(ctx, seat, target, live, report):
                to_brief.append(seat)
            continue
        if agent.get("pane_id") == ctx.invocation.pane_id:
            report.add(seat.name, "refused", "it is the caller's own agent; restart it from another pane", "partial")
            continue
        agent = settle(ctx, seat, agent, wait_ms, report)
        if agent is None:
            continue
        if not ctx.dry_run:
            follow_pane(ctx, seat.name, str(agent.get("pane_id")))
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
    if name_panes:
        labels.apply(ctx, None, report)
    return report


def op_swap(ctx: Ctx, names: Sequence[str], note: Optional[str] = None,
            wait_ms: Optional[int] = None) -> Report:
    """Restart exactly the seats that drifted from the file; start absent ones.
    Without names, adopted sessions are skipped (design section 15)."""
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
        if not names and is_adopted(ctx.state.entry(seat.name)):
            report.add(seat.name, "skipped", "adopted session; name it explicitly to swap it")
            continue
        reasons = drift_reasons(ctx, seat, agent)
        if reasons:
            changed.append(seat.name)
            report.add(seat.name, "would restart" if ctx.dry_run else "drifted", "; ".join(reasons))
        else:
            report.add(seat.name, "unchanged", "matches the fleet file")
    if changed and not ctx.dry_run:
        report.extend(op_restart(ctx, changed, note, wait_ms, reason="swap", name_panes=False))
    labels.apply(ctx, None, report)
    return report


# -- kickoff and down --------------------------------------------------------------


def op_kickoff(ctx: Ctx, names: Sequence[str], note: Optional[str] = None, drop_inbox: bool = False) -> Report:
    """Resend kickoffs. safe_prompt refuses blocked, unknown, working and absent agents."""
    report = Report("kickoff")
    seats = select_seats(ctx.fleet, names, required=True)
    prune(ctx)
    if drop_inbox:
        drop_inboxes(ctx, seats, report)
    send_kickoffs(ctx, seats, "kickoff", note, label_for(ctx, None), report)
    labels.apply(ctx, None, report)
    return report


def op_down(ctx: Ctx, names: Sequence[str], wait_ms: Optional[int] = None, close_panes: bool = False,
            close_workspace: bool = False) -> Report:
    report = Report("down")
    seats = select_seats(ctx.fleet, names)
    prune(ctx)
    live = live_by_name(ctx.herdr)
    left: List[str] = []  # panes whose agent quit: open at a shell prompt (design 22.1)
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
            follow_pane(ctx, seat.name, pane)
        if pane and not ctx.dry_run:
            left.append(pane)
    ctx.save()
    closed: Set[str] = set()
    if close_panes:
        closed = _close_panes(ctx, seats, report)
        if not names:  # design 19.1: after the seats; `down NAME...` never touches the strip
            watchpane.close_strip(ctx, report)
    for pane in left:  # design 22.1: an empty shell is not labelled as a seat
        if pane not in closed:
            labels.clear(ctx, pane, report)
    if close_workspace:
        _close_workspace(ctx, report)
    ctx.save()
    return report


def _close_panes(ctx: Ctx, seats: Sequence[Seat], report: Report) -> Set[str]:
    """Close panes herdr-fleet created, and only once they are back at a shell. Returns
    the panes closed."""
    closed: Set[str] = set()
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
        closed.add(str(pane))
        report.add(seat.name, "pane closed", pane)
    return closed


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


# -- go ------------------------------------------------------------------------------------


def op_go(ctx: Ctx, name: str) -> Tuple[int, str]:
    """Design 22.2: focus the pane of NAME, resolved as a ``send`` target (a seat, a contact
    or a live herdr agent name), with ``agent focus``. Returns (exit code, line):
    ``focused sample-orchestrator (w2:p5, working)``; an absent seat or a contact that
    cannot be reached is exit 3 and nothing is focused; an unknown name is a usage error.
    Writes nothing: no ``state_seen``, no log entry, no file. The pane and state in the
    line come from ``agent get``, read just before the focus."""
    if name == OWNER:
        raise UsageError("owner is not a pane; nothing to focus")
    target = resolve_targets(ctx, [name], ctx.herdr.agent_list())[0]
    if target.problem or not target.herdr:
        state, _, why = (target.problem or "absent").partition(": ")
        return EXIT_PARTIAL, "%s is %s%s; nothing to focus" % (name, state, " (%s)" % why if why else "")
    try:
        agent = ctx.herdr.agent_get(target.herdr)
    except HerdrError as exc:
        if "not_found" in exc.code:
            return EXIT_PARTIAL, "%s is absent; nothing to focus" % name
        raise
    ctx.herdr.agent_focus(target.herdr)
    return EXIT_OK, "focused %s (%s, %s)" % (name, agent.get("pane_id") or target.herdr,
                                             agent.get("agent_status") or "unknown")


# -- peek and interrupt --------------------------------------------------------------------

PEEK_LINES = 30  # design 23.6


def op_peek(ctx: Ctx, name: str, lines: int = PEEK_LINES) -> Tuple[int, str]:
    """Design 23.6: the last ``lines`` screen lines of a seat, a contact or a live agent
    (resolved as ``send`` resolves a target), through ``agent read``, and nothing else:
    nothing is logged, recorded or moved. Returns (exit code, text): an absent seat or a
    contact that cannot be reached is exit 3; an unknown name or ``owner`` a usage error."""
    if lines < 1:
        raise UsageError("--lines takes a number of lines, 1 or more")
    if name == OWNER:
        raise UsageError("owner is not a pane; nothing to read")
    target = resolve_targets(ctx, [name], ctx.herdr.agent_list())[0]
    if target.problem or not target.herdr:
        state, _, why = (target.problem or "absent").partition(": ")
        return EXIT_PARTIAL, "%s is %s%s; nothing to read" % (name, state, " (%s)" % why if why else "")
    try:
        text = ctx.herdr.agent_read(target.herdr, lines)
    except HerdrError as exc:
        if "not_found" in exc.code:
            return EXIT_PARTIAL, "%s is absent; nothing to read" % name
        raise
    return EXIT_OK, text.rstrip("\n")


def op_interrupt(ctx: Ctx, name: str, wait_ms: Optional[int] = None, as_name: Optional[str] = None) -> Tuple[int, str]:
    """Design 23.7: press the kind's interrupt keys (``seats.INTERRUPT_KEYS``, else
    ``[interrupt]`` in the fleet file, then the plugin config) in a seat of this fleet that
    is ``working`` right now, then wait up to ``wait_ms`` for it to be idle or done. Every
    other state is refused (exit 3), and so are the caller's own seat, a kind without keys,
    a seat whose pane the owner is typing in (the presence guard of 21.2) and, from the
    screen read just before the keys (``agent read``, invariant 21), a seat showing a
    highlighted numbered choice: a dialog appearing before herdr reports it ``blocked``,
    which a key could answer. Contacts and other agents are a usage error. Design 23.11:
    after a successful interrupt the prompt line is read again, and when Claude Code put
    the interrupted prompt back into it, one ``ctrl+u`` clears it (exit 3 when text still
    remains). Logged as an ``interrupt`` entry. CLI only: no manifest action, no popup,
    never the watcher. Returns (exit code, line)."""
    wait_ms = DEFAULT_INTERRUPT_WAIT_MS if wait_ms is None else wait_ms
    if wait_ms <= 0:
        raise UsageError("--wait takes milliseconds, more than 0")
    fleet = ctx.fleet
    seat = fleet.seat(name)
    if seat is None:
        if name == OWNER:
            what = "owner is not a seat"
        elif fleet.contact(name) is not None:
            what = "%s is a contact" % name
        else:
            what = "%s is not a seat of %s" % (name, fleet.path)
        raise UsageError("interrupt is for this fleet's seats (%s)" % what)
    caller = resolve_sender(ctx, as_name)
    try:
        agent = ctx.herdr.agent_get(name)
    except HerdrError as exc:
        if "not_found" in exc.code:
            return EXIT_PARTIAL, "nothing to interrupt: %s is absent" % name
        raise
    status = str(agent.get("agent_status") or "unknown")
    pane = str(agent.get("pane_id") or "")
    if pane and pane == ctx.invocation.pane_id:
        return EXIT_PARTIAL, "refused %s: it is the caller's own agent; interrupt it yourself" % name
    if status != "working":
        why = " (a dialog is open)" if status == "blocked" else ""
        return EXIT_PARTIAL, "nothing to interrupt: %s is %s%s" % (name, status, why)
    kind = str(agent.get("agent") or seat.kind)
    keys = interrupt_keys(kind, fleet.interrupt, ctx.config.interrupt)
    if not keys:
        return EXIT_PARTIAL, 'no interrupt keys known for %s; set [interrupt] %s = ["ctrl+c"]' % (kind, kind)
    try:
        held = presence.typing_at(ctx.herdr, agent)
    except HerdrError as exc:
        return EXIT_FAIL, "failed %s: presence check: %s; no keys sent" % (name, describe(exc))
    if held:
        return EXIT_PARTIAL, "refused %s: %s; no keys sent" % (name, held)
    try:
        dialog = dialog_first_line(ctx.herdr.agent_read(name, EXIT_DIALOG_LINES))
    except HerdrError as exc:
        if "not_found" in exc.code:
            return EXIT_PARTIAL, "nothing to interrupt: %s is absent" % name
        return EXIT_FAIL, "failed %s: reading its screen: %s; no keys sent" % (name, describe(exc))
    if dialog is not None:  # a key could answer it: never another agent's prompt
        return EXIT_PARTIAL, "refused %s: a dialog is on screen (\"%s\"); no keys sent" % (name, dialog)
    outcome = interrupt_agent(ctx.herdr, ctx.clock, name, keys, wait_ms)
    sent = list(outcome.agent.get("sent") or [])
    cleanup = _clear_input(ctx, name, kind) if outcome.ok else None
    if cleanup and cleanup != "untouched":
        sent.append("ctrl+u")
    if sent:
        record(ctx, interrupt_entry(caller, name, sent, "failed" if outcome.status == "failed" else outcome.status))
    if outcome.status == "failed":
        return EXIT_FAIL, "failed %s: %s; %d of %d keys sent" % (name, outcome.detail, len(sent), len(keys))
    if outcome.ok:
        line = "interrupted %s (%s)" % (name, outcome.status)
        if cleanup == "cleared":
            return EXIT_OK, line + "; cleared the interrupted prompt from its input"
        if cleanup in ("held", "untouched"):
            return EXIT_PARTIAL, line + "; its input still holds text; press Ctrl-U there before sending it anything"
        return EXIT_OK, line
    after = "%s s" % format(wait_ms / 1000.0, "g")
    if outcome.status == "working":
        return EXIT_PARTIAL, "%s still working after %s; nothing more sent" % (name, after)
    if outcome.status == "absent":
        return EXIT_PARTIAL, "%s is absent after the keys; nothing more sent" % name
    return EXIT_PARTIAL, "%s is %s after %s; nothing more sent" % (name, state_words(outcome.status), after)


def _input_line(ctx: Ctx, name: str, kind: str) -> Optional[str]:
    """The seat's prompt line (``agent read NAME --lines 12``), or None when none is on
    screen or herdr cannot say."""
    try:
        return presence.prompt_line(ctx.herdr.agent_read(name, presence.SCREEN_LINES), kind)
    except HerdrError:
        return None


def _clear_input(ctx: Ctx, name: str, kind: str) -> Optional[str]:
    """Design 23.11: after an interrupt Claude Code may put the interrupted prompt back into
    the input line, where the next paste would join it. Read the line; when it holds text,
    press ``ctrl+u`` once (the only place that key is sent) and read again. Returns None
    (nothing to clear, or a kind whose line cannot be read), ``cleared``, ``held`` (text is
    still there after the key) or ``untouched`` (the key itself failed)."""
    if not presence.checkable(kind) or not presence.holds_text(_input_line(ctx, name, kind), kind):
        return None
    try:
        ctx.herdr.agent_send_keys(name, ["ctrl+u"])
    except HerdrError:
        return "untouched"
    after = _input_line(ctx, name, kind)
    return "cleared" if after is not None and not presence.holds_text(after, kind) else "held"


# -- ui and install-cli ------------------------------------------------------------------


UI_PLACEMENT = {"watch": "split"}  # design 18.6: the watcher opens as a split; the others as the manifest says


def op_ui(ctx: Ctx, entrypoint: str) -> None:
    """Open a plugin pane with its cwd at the repo root so it can find the fleet file."""
    repo = ctx.fleet.repo
    plugin = ctx.env.get("HERDR_PLUGIN_ID") or PLUGIN_ID
    ctx.herdr.plugin_pane_open(plugin, entrypoint, repo, UI_PLACEMENT.get(entrypoint))


LAUNCHER_MARKER = "herdr-fleet launcher"
LAUNCHER_TEMPLATE = """#!/bin/sh
# herdr-fleet launcher, written by `herdr-fleet install-cli`. Runs the copy of herdr-fleet that herdr has
# installed (plugin yarrasys.fleet), so fleet seats never run a half-edited development checkout.
# HERDR_FLEET_BIN=<path to a bin/herdr-fleet> runs that copy instead. Falls back to FALLBACK when herdr
# cannot be asked.
FALLBACK=__FALLBACK__
if [ -n "$HERDR_FLEET_BIN" ]; then exec python3 "$HERDR_FLEET_BIN" "$@"; fi
ROOT=$(herdr plugin list --json 2>/dev/null | python3 -c '
import json, os, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for plugin in data.get("result", {}).get("plugins", []):
    if "yarrasys.fleet" in json.dumps(plugin) and plugin.get("enabled", True):
        manifest = plugin.get("manifest_path") or ""
        if manifest:
            print(os.path.dirname(manifest))
            break
' 2>/dev/null)
# A hook call (`inbox --hook ...`) must never disturb the agent: when the installed copy predates
# hooks (no herdr_fleet/hooks.py), exit silently instead of failing on the unknown option.
case " $* " in *" --hook "*) if [ -n "$ROOT" ] && [ ! -f "$ROOT/herdr_fleet/hooks.py" ]; then exit 0; fi;; esac
if [ -n "$ROOT" ] && [ -f "$ROOT/bin/herdr-fleet" ]; then exec python3 "$ROOT/bin/herdr-fleet" "$@"; fi
exec python3 "$FALLBACK" "$@"
"""


def launcher_text(fallback: str) -> str:
    """The launcher ``install-cli`` writes: it runs the plugin copy herdr installed, or
    ``$HERDR_FLEET_BIN``, or ``fallback`` (the bin/herdr-fleet that ran install-cli)."""
    return LAUNCHER_TEMPLATE.replace("__FALLBACK__", shlex.quote(fallback))


def _is_launcher(path: str) -> bool:
    try:
        with open(path, "rb") as handle:
            return LAUNCHER_MARKER.encode("ascii") in handle.read(64 * 1024)
    except OSError:
        return False


def op_install_cli(root: str, directory: Optional[str], out: TextIO, path_env: str, dev: bool = False) -> int:
    """Put ``herdr-fleet`` into DIR (default ~/.local/bin). By default a launcher script
    that runs the copy herdr installed (written atomically, mode 0755); with ``dev`` a
    symlink to this checkout. Replaces a symlink or an earlier launcher, never another file."""
    source = os.path.join(root, "bin", "herdr-fleet")
    folder = os.path.abspath(os.path.expanduser(directory or "~/.local/bin"))
    target = os.path.join(folder, "herdr-fleet")
    out.write("plugin root: %s\n" % root)
    if os.path.lexists(target) and not os.path.islink(target) and not _is_launcher(target):
        out.write("herdr-fleet: error: %s exists and is not a herdr-fleet launcher or symlink; remove it first\n"
                  % target)
        return 1
    os.makedirs(folder, exist_ok=True)
    if dev:
        if os.path.islink(target) and os.path.realpath(target) == os.path.realpath(source):
            out.write("already linked (--dev): %s -> %s\n" % (target, source))
        else:
            _atomic(folder, target, lambda tmp: os.symlink(source, tmp))
            out.write("linked %s -> %s (--dev: runs this checkout as it is)\n" % (target, source))
    else:
        text = launcher_text(source)
        if not os.path.islink(target) and os.path.isfile(target) and _read(target) == text:
            out.write("launcher already installed: %s\n" % target)
        else:
            _atomic(folder, target, lambda tmp: _write_launcher(tmp, text))
            out.write("wrote launcher %s: runs the herdr-fleet that herdr installed (falls back to %s; "
                      "HERDR_FLEET_BIN overrides)\n" % (target, source))
    if folder not in path_env.split(os.pathsep):
        out.write("note: %s is not on PATH\n" % folder)
    return 0


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def _write_launcher(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(path, 0o755)


def _atomic(folder: str, target: str, make: "Callable[[str], None]") -> None:
    """Create the new launcher or link beside ``target`` and rename it over it."""
    tmp = os.path.join(folder, ".herdr-fleet.%d.tmp" % os.getpid())
    if os.path.lexists(tmp):
        os.unlink(tmp)
    try:
        make(tmp)
        os.replace(tmp, target)
    except BaseException:
        if os.path.lexists(tmp):
            os.unlink(tmp)
        raise
