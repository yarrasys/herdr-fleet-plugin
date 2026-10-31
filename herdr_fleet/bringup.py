"""Bringing seats up (design 4.1 steps 2, 4 and 5; sections 3 and 14).

``resolve_target`` finds or creates the fleet's workspace and the anchor pane
new seats split from. ``claim_seat`` classifies a seat: skip it when an agent
with its name is live, adopt an unnamed agent of its kind that herdr resumed
in its recorded pane, reuse its recorded pane when that is at a shell prompt,
or mark it as needing a pane, which ``placement`` then splits (chain or
grid). ``start_claimed`` starts it through ``seats.start_in_pane``, and
``send_kickoffs`` briefs seats in file order once all have started, each
kickoff carrying the seat's queued messages through ``{inbox}`` (design 18.5); for
up, restart and swap a kickoff that cannot be sent waits first in the seat's mailbox
(``queued_by: kickoff``, design 32.6), and a failed start is notified (32.1).
Everything here honours ``--dry-run`` by reporting plans instead of acting.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Sequence

from herdr_fleet.context import Ctx, OpError, Report
from herdr_fleet.fleetfile import Seat
from herdr_fleet.herdr import HerdrError, describe, has_agent, render_cmd, start_argv, workspace_create_argv
from herdr_fleet.contacts import contacts_block
from herdr_fleet.kickoff import compose, roster_table
from herdr_fleet import inbox
from herdr_fleet.messages import drop_kickoffs, inbox_block, log_kickoff, queue_kickoff, settle
from herdr_fleet.placement import Placed, Target, existing_panes, place_chain
from herdr_fleet.seats import (KICKOFF_QUEUED, KICKOFF_REMEDY, SLOW_REMEDY, at_shell_prompt, describe_foreground,
                               safe_prompt, start_in_pane)
from herdr_fleet.state import now_iso, pane_moved, refresh_pane


def label_of(ctx: Ctx, workspace_id: str) -> str:
    for ws in ctx.herdr.workspace_list():
        if ws.get("workspace_id") == workspace_id:
            return str(ws.get("label") or workspace_id)
    return workspace_id


def require_caller_context(ctx: Ctx) -> None:
    """A fleet without ``workspace`` lives beside the caller, so it needs one."""
    inv = ctx.invocation
    if not ctx.fleet.workspace and (not inv.workspace_id or not inv.pane_id):
        raise OpError("the fleet file sets no workspace, and this was not run from a herdr pane "
                      "(HERDR_WORKSPACE_ID and HERDR_PANE_ID are unset)")


def resolve_target(ctx: Ctx, report: Report, first: Optional[Seat] = None) -> Target:
    """Find or create the fleet's workspace (design 4.1 step 2). A new
    workspace's root pane is created with the cwd and env of ``first``, the
    first seat that will need a pane (the ``[layout] top`` seat when it needs
    one, design 26.2), and is reserved for it."""
    fleet, state, inv = ctx.fleet, ctx.state, ctx.invocation
    if not fleet.workspace:
        require_caller_context(ctx)
        created = state.created_workspace and state.workspace_id == inv.workspace_id
        label = label_of(ctx, inv.workspace_id)
        state.set_workspace(inv.workspace_id, label, created)
        return Target(inv.workspace_id, label, inv.pane_id)
    label = fleet.workspace
    ids = [str(w.get("workspace_id")) for w in ctx.herdr.workspace_list() if w.get("label") == label]
    chosen = next((c for c in (state.workspace_id, inv.workspace_id) if c and c in ids), None)
    if chosen is None and len(ids) > 1:
        raise OpError("several workspaces are labelled %r (%s); rename all but one" % (label, ", ".join(ids)))
    if chosen is None and ids:
        chosen = ids[0]
    if chosen:
        state.set_workspace(chosen, label, state.created_workspace and state.workspace_id == chosen)
        anchor = inv.pane_id if inv.workspace_id == chosen and inv.pane_id else _first_pane(ctx, chosen)
        return Target(chosen, label, anchor)
    cwd, env = (first.cwd, first.env) if first else (fleet.cwd, {})
    owner = first.name if first else None
    if ctx.dry_run:
        report.add("workspace", "plan", render_cmd(workspace_create_argv(cwd, label, env)))
        root = "<root pane of %s>" % label
        return Target(None, label, root, root, owner)
    workspace_id, root = ctx.herdr.workspace_create(cwd, label, env)
    state.set_workspace(workspace_id, label, True)
    ctx.save()
    report.add("workspace", "created", "%s (%s), root pane %s" % (label, workspace_id, root))
    return Target(workspace_id, label, root, root, owner)


def _first_pane(ctx: Ctx, workspace_id: str) -> Optional[str]:
    """A pane to split from in a workspace the caller is not in: the first pane
    of its active tab (never started into, only split), never the watcher strip or
    the handover helper (design 32.5)."""
    active = next((w.get("active_tab_id") for w in ctx.herdr.workspace_list()
                   if w.get("workspace_id") == workspace_id), None)
    plugin = {ctx.state.watch_pane, (ctx.state.handover or {}).get("pane_id")} - {None}
    panes = [p for p in ctx.herdr.pane_list(workspace_id) if p.get("pane_id") not in plugin]
    for pane in panes:
        if pane.get("tab_id") == active:
            return str(pane["pane_id"])
    return str(panes[0]["pane_id"]) if panes else None


def label_for(ctx: Ctx, target: Optional[Target]) -> str:
    if target is not None:
        return target.label
    if ctx.fleet.workspace:
        return ctx.fleet.workspace
    if ctx.state.data.get("workspace_label"):
        return str(ctx.state.data["workspace_label"])
    workspace = ctx.state.workspace_id or ctx.invocation.workspace_id
    return label_of(ctx, workspace) if workspace else ""


@dataclass
class Claim:
    """What a seat needs before placement (design 4.1 step 4)."""

    state: str                  # live | adopted | conflict | recorded | needs
    pane: Optional[str] = None
    created: bool = False


def claim_seat(ctx: Ctx, seat: Seat, live: Dict[str, Dict], report: Report) -> Claim:
    """Skip a live seat, adopt an unnamed resumed agent of its kind in its
    recorded pane, reuse its recorded pane at a shell prompt, or say it needs a pane."""
    agent = live.get(seat.name)
    if agent:
        return Claim("live", _already_live(ctx, seat, agent, report))
    recorded = ctx.state.pane_of(seat.name)
    if recorded:
        verdict = _check_recorded(ctx, seat, recorded, report)
        if verdict == "adopted":
            return Claim("adopted", recorded)
        if verdict == "conflict":
            return Claim("conflict")
        if verdict == "use":
            return Claim("recorded", recorded, bool((ctx.state.entry(seat.name) or {}).get("created_pane")))
    return Claim("needs")


def start_claimed(ctx: Ctx, seat: Seat, claim: Claim, placed: Placed, report: Report) -> str:
    """Start a seat in its recorded or newly placed pane: ``start_seat``'s answer
    (``started`` is ready for a kickoff, ``blocked`` gets it queued), "" when nothing started."""
    if claim.state == "recorded":
        pane, created = claim.pane, claim.created
    elif claim.state == "needs" and seat.name in placed:
        pane, created = placed[seat.name]
    else:
        return ""
    if pane == ctx.invocation.pane_id:
        report.add(seat.name, "refused", "will not start an agent in the caller's own pane %s" % pane, "fail")
        return ""
    return start_seat(ctx, seat, str(pane), created, report)


def ensure_seat(ctx: Ctx, seat: Seat, target: Target, live: Dict[str, Dict], report: Report) -> str:
    """Bring one seat up on its own (restart of an absent seat): claim, place
    with the chain rule (grid mode appends after the last placed seat), start."""
    claim = claim_seat(ctx, seat, live, report)
    placed: Placed = {}
    if claim.state == "needs":
        existing = existing_panes(ctx, [seat], live, target) if ctx.fleet.mode == "grid" else []
        placed = place_chain(ctx, [seat], target, live, report, fallback=existing[-1] if existing else None)
    return start_claimed(ctx, seat, claim, placed, report)


def follow_pane(ctx: Ctx, name: str, pane: str, kind: Optional[str] = None) -> Dict:
    """Record that a live seat is in ``pane`` now (keeping its entry) and say so
    when that differs from the recorded pane."""
    old = ctx.state.pane_of(name)
    entry = refresh_pane(ctx.state, name, pane, kind=kind)
    if old and old != pane:
        ctx.say(pane_moved(name, old, pane))
    return entry


def _already_live(ctx: Ctx, seat: Seat, agent: Dict, report: Report) -> str:
    pane = str(agent.get("pane_id"))
    entry = follow_pane(ctx, seat.name, pane, kind=agent.get("agent"))
    status = agent.get("agent_status") or "unknown"
    if entry.get("blocked_at_startup"):
        report.add(seat.name, "live", "in %s (%s) but never briefed; run `herdr-fleet kickoff %s` once it is idle"
                   % (pane, status, seat.name), "partial")
    else:
        report.add(seat.name, "live", "already running in %s (%s)" % (pane, status))
    return pane


def _check_recorded(ctx: Ctx, seat: Seat, pane: str, report: Report) -> str:
    """Decide what to do with the seat's recorded pane: adopted | conflict | use | split."""
    try:
        info = ctx.herdr.pane_get(pane)
        procs = None if has_agent(info) else ctx.herdr.process_info(pane)
    except HerdrError as exc:
        ctx.say("%s: recorded pane %s is unusable (%s); splitting a new pane" % (seat.name, pane, describe(exc)))
        return "split"
    if has_agent(info):
        kind, name = info.get("agent"), ctx.herdr.pane_agent(pane).get("name")
        if not name and kind == seat.kind:
            return _adopt_resumed(ctx, seat, pane, str(kind), report)
        who = "agent %s (%s)" % (name, kind) if name else "an unnamed %s agent" % kind
        report.add(seat.name, "conflict", "recorded pane %s hosts %s; seat skipped" % (pane, who), "partial")
        return "conflict"
    if at_shell_prompt(procs or {}):
        return "use"
    ctx.say("%s: recorded pane %s is busy (foreground: %s); splitting a new pane"
            % (seat.name, pane, describe_foreground(procs or {})))
    return "split"


def _adopt_resumed(ctx: Ctx, seat: Seat, pane: str, kind: str, report: Report) -> str:
    """An unnamed agent of the right kind in the recorded pane: herdr resumed it
    after a restart. Name it; no kickoff (it keeps its conversation)."""
    argv = ["agent", "rename", pane, seat.name]
    if ctx.dry_run:
        report.add(seat.name, "plan",
                   "%s (adopt the unnamed %s agent herdr resumed; no kickoff)" % (render_cmd(argv), kind))
        return "adopted"
    try:
        ctx.herdr.agent_rename(pane, seat.name)
    except HerdrError as exc:
        report.add(seat.name, "failed", "rename: %s" % describe(exc), "fail")
        return "conflict"
    ctx.state.update(seat.name, pane_id=pane, kind=kind, blocked_at_startup=False, adopted=True)
    ctx.save()
    report.add(seat.name, "adopted", "named the unnamed %s agent in %s (resumed by herdr); no kickoff sent"
               % (kind, pane))
    return "adopted"


AT_SHELL_REMEDY = "the pane is at a shell; run herdr-fleet up %s"  # design 32.1: a failed start after a quit


def start_seat(ctx: Ctx, seat: Seat, pane: str, created: bool, report: Report, after_quit: bool = False) -> str:
    """Start the seat in a pane; record it. Returns ``started`` (ready for a kickoff),
    ``blocked`` (its kickoff is to be queued, 32.6: blocked at startup, or a slow start whose
    agent process runs but was not seen ready by the deadline, which is also ``failed``) or
    "" (failed). A failed start is notified like a blocked one (32.1); ``after_quit``
    (restart, swap) names the remedy when the pane was left at its shell."""
    if ctx.dry_run:
        report.add(seat.name, "plan", render_cmd(start_argv(seat.name, seat.kind, pane, seat.args)))
        return "started"
    ctx.state.update(seat.name, pane_id=pane, created_pane=created)
    ctx.save()
    outcome = start_in_pane(ctx.herdr, ctx.clock, seat, pane)
    record = dict(kind=seat.kind, args=list(seat.args), started_at=now_iso(), adopted=False)
    if outcome.status == "started":
        ctx.state.update(seat.name, blocked_at_startup=False, **record)
        ctx.save()
        report.add(seat.name, "started", outcome.detail)
        return "started"
    if outcome.status == "blocked":
        ctx.state.update(seat.name, blocked_at_startup=True, **record)
        ctx.save()
        report.add(seat.name, "blocked", outcome.detail, "partial")
        ctx.notify("Fleet: %s is waiting for you" % seat.name, outcome.detail, "request")
        return "blocked"
    detail = outcome.detail
    remedy = AT_SHELL_REMEDY % seat.name
    if after_quit and outcome.shell and remedy.split("; ", 1)[1] not in detail:
        detail = "%s; %s" % (detail, remedy)
    report.add(seat.name, "failed", detail, "fail")
    ctx.notify("Fleet: %s did not start" % seat.name, detail, "request")
    if outcome.slow:  # its agent usually comes up later: brief it then (32.1 as the review amended it)
        ctx.state.update(seat.name, blocked_at_startup=True, **record)
        ctx.save()
        return "blocked"
    return ""


QUEUE_ON = ("working", "blocked", "unknown", "typing")  # design 32.6: refusals that queue a kickoff


def send_kickoffs(ctx: Ctx, seats: Sequence[Seat], reason: str, note: Optional[str],
                  label: str, report: Report, blocked: Sequence[Seat] = (), queue: bool = False) -> None:
    """Brief seats in file order, after every seat has started (design 3). A kickoff
    carries the seat's queued messages; they move to ``cur/`` once herdr took the
    prompt (or may have: unconfirmed), and stay queued when it was refused.

    Design 32.6, with ``queue`` (up, restart, swap): a kickoff that cannot be sent (a
    ``blocked`` seat, blocked at startup, or a refusal by ``safe_prompt``: not idle or
    done, or held by the guard) is written into the seat's mailbox without its {inbox}
    block, ahead of everything queued, replacing an older one. An unconfirmed one is not
    (it may have arrived). A sent or unconfirmed kickoff replaces a queued one."""
    ordered = sorted(seats, key=lambda s: s.index)
    waiting = sorted(blocked, key=lambda s: s.index)
    if ctx.dry_run:
        waiting = {s.name: len(inbox.queued(ctx.fleet.repo).get(s.name, [])) for s in ordered}
        carries = ["%s carries %d queued" % (n, c) for n, c in waiting.items() if c]
        report.add("kickoff", "plan", "herdr agent prompt (reason %s) to %s%s"
                   % (reason, ", ".join(s.name for s in ordered), "; " + ", ".join(carries) if carries else ""))
        return
    agents = ctx.herdr.agent_list()
    live = {str(a["name"]): a for a in agents if a.get("name")}
    rows = [(s.name, (live.get(s.name) or {}).get("agent") or s.kind,
             (live.get(s.name) or {}).get("pane_id") or ctx.state.pane_of(s.name) or "-", s.cwd)
            for s in ctx.fleet.seats]
    roster = roster_table(rows)
    contacts = contacts_block(ctx.fleet, agents)
    stuck = {s.name for s in waiting}
    for seat in sorted(ordered + waiting, key=lambda s: s.index):
        if seat.name in stuck:  # blocked at startup: nothing can be sent now
            _queue(ctx, seat, label, roster, contacts, reason, note, report, held=False)
        elif inbox.has_queued(ctx.fleet.repo, seat.name):
            with inbox.locked(ctx.fleet.repo, seat.name):
                _brief(ctx, seat, label, roster, contacts, reason, note, report, with_inbox=True, queue=queue)
        else:
            _brief(ctx, seat, label, roster, contacts, reason, note, report, with_inbox=False, queue=queue)


def _queue(ctx: Ctx, seat: Seat, label: str, roster: str, contacts: str, reason: str, note: Optional[str],
           report: Report, held: bool) -> None:
    """Design 32.6: the kickoff, without its {inbox} block, into the seat's mailbox. ``held``:
    the caller holds the mailbox lock already."""
    try:
        text = compose(seat, ctx.fleet, label, roster, reason, note, contacts=contacts, inbox="")
    except OSError as exc:
        report.add(seat.name, "not briefed", "cannot read kickoff template: %s" % exc, "fail")
        return
    try:
        if held:
            message = queue_kickoff(ctx, seat.name, text, reason)
        else:
            with inbox.locked(ctx.fleet.repo, seat.name):
                message = queue_kickoff(ctx, seat.name, text, reason)
    except OSError as exc:
        report.add(seat.name, "not briefed", "cannot queue the kickoff: %s" % exc, "fail")
        return
    report.add(seat.name, "kickoff queued #%s: delivered when %s is next idle or done" % (message.short, seat.name),
               "", "partial")
    for result in report.results:  # the start's line names the queued kickoff instead of `kickoff NAME`
        if result.name == seat.name and result.status in ("blocked", "failed"):
            for remedy in (KICKOFF_REMEDY, SLOW_REMEDY):
                result.detail = result.detail.replace(remedy % seat.name, KICKOFF_QUEUED % message.short)


def _brief(ctx: Ctx, seat: Seat, label: str, roster: str, contacts: str, reason: str, note: Optional[str],
           report: Report, with_inbox: bool, queue: bool = False) -> None:
    """One kickoff. With ``with_inbox`` the caller holds the seat's mailbox lock. With
    ``queue``, one ``safe_prompt`` refuses is queued instead (32.6)."""
    block, carried, left = inbox_block(ctx, seat.name) if with_inbox else ("", [], [])
    try:
        text = compose(seat, ctx.fleet, label, roster, reason, note, contacts=contacts, inbox=block)
    except OSError as exc:
        report.add(seat.name, "not briefed", "cannot read kickoff template: %s" % exc, "fail")
        return
    outcome = safe_prompt(ctx.herdr, seat.name, text)
    if outcome.ok or outcome.status == "unconfirmed":
        settle(ctx, carried, "kickoff", "sent" if outcome.ok else "unconfirmed")
        if with_inbox:  # this kickoff replaces one still queued (32.6)
            drop_kickoffs(ctx, seat.name)
    elif queue and outcome.status in QUEUE_ON:
        _queue(ctx, seat, label, roster, contacts, reason, note, report, held=with_inbox)
        return
    queued = ""
    if carried:
        queued = " with %d queued" % len(carried)
    elif left:
        queued = "; %d queued left in the inbox (over 16 KiB; the kickoff says to run %s)" % (
            len(left), inbox.POINTER_COMMAND)
    if outcome.ok:
        ctx.state.update(seat.name, last_kickoff_at=now_iso(), blocked_at_startup=False)
        ctx.save()
        problem = log_kickoff(ctx.fleet.repo, seat.name, text, reason)
        if problem:
            ctx.warn(problem)
        report.add(seat.name, "briefed", "kickoff sent (%s)%s" % (reason, queued))
    elif outcome.status == "unconfirmed" and carried:
        report.add(seat.name, "not briefed", "%s; %d queued logged as delivered (unconfirmed), not resent"
                   % (outcome.detail, len(carried)), "partial")
    else:
        report.add(seat.name, "not briefed", outcome.detail, "partial")
