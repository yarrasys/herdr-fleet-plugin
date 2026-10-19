"""Bringing seats up (design 4.1 steps 2, 4 and 5; section 3).

``resolve_target`` finds or creates the fleet's workspace and the anchor pane
new seats split from. ``ensure_seat`` brings one seat up: skip it when an
agent with its name is live, adopt an unnamed agent of its kind that herdr
resumed in its recorded pane, reuse its recorded pane when that is at a shell
prompt, else split a new pane; then start it through ``seats.start_in_pane``.
``send_kickoffs`` briefs seats in file order once all have started.
Everything here honours ``--dry-run`` by reporting plans instead of acting.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Sequence, Tuple

from herdr_fleet.context import Ctx, OpError, Report
from herdr_fleet.fleetfile import Seat
from herdr_fleet.herdr import (HerdrError, describe, has_agent, render_cmd, split_argv, start_argv,
                               workspace_create_argv)
from herdr_fleet.kickoff import compose, roster_table
from herdr_fleet.seats import at_shell_prompt, describe_foreground, live_by_name, safe_prompt, start_in_pane
from herdr_fleet.state import now_iso


def label_of(ctx: Ctx, workspace_id: str) -> str:
    for ws in ctx.herdr.workspace_list():
        if ws.get("workspace_id") == workspace_id:
            return str(ws.get("label") or workspace_id)
    return workspace_id


@dataclass
class Target:
    """Where seats go in this run."""

    workspace_id: Optional[str]   # None only in --dry-run when it would be created
    label: str
    anchor: Optional[str]         # pane the first new seat splits from
    free_pane: Optional[str] = None   # root pane of a workspace created in this run...
    free_for: Optional[str] = None    # ...reserved for this seat, whose cwd and env it got


def resolve_target(ctx: Ctx, report: Report, first: Optional[Seat] = None) -> Target:
    """Find or create the fleet's workspace (design 4.1 step 2). A new
    workspace's root pane is created with the cwd and env of ``first``, the
    first seat that will need a pane, and is reserved for it."""
    fleet, state, inv = ctx.fleet, ctx.state, ctx.invocation
    if not fleet.workspace:
        if not inv.workspace_id or not inv.pane_id:
            raise OpError("the fleet file sets no workspace, and this was not run from a herdr pane "
                          "(HERDR_WORKSPACE_ID and HERDR_PANE_ID are unset)")
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
    of its active tab (never started into, only split)."""
    active = next((w.get("active_tab_id") for w in ctx.herdr.workspace_list()
                   if w.get("workspace_id") == workspace_id), None)
    panes = ctx.herdr.pane_list(workspace_id)
    for pane in panes:
        if pane.get("tab_id") == active:
            return str(pane["pane_id"])
    return str(panes[0]["pane_id"]) if panes else None


def in_target(ctx: Ctx, pane: str, target: Target, cache: Dict[str, Optional[str]]) -> bool:
    if pane.startswith("<") or target.workspace_id is None:
        return True  # --dry-run placeholders
    if pane not in cache:
        try:
            cache[pane] = ctx.herdr.pane_get(pane).get("workspace_id")
        except HerdrError:
            cache[pane] = None
    return cache[pane] == target.workspace_id


def split_source(ctx: Ctx, seat: Seat, live: Dict[str, Dict], target: Target) -> Optional[str]:
    """The pane of the nearest earlier seat in the target workspace, if any."""
    cache: Dict[str, Optional[str]] = {}
    seats = ctx.fleet.seats
    for earlier in reversed(seats[:seats.index(seat)]):
        pane = (live.get(earlier.name) or {}).get("pane_id") or ctx.state.pane_of(earlier.name)
        if pane and in_target(ctx, pane, target, cache):
            return str(pane)
    return None


def label_for(ctx: Ctx, target: Optional[Target]) -> str:
    if target is not None:
        return target.label
    if ctx.fleet.workspace:
        return ctx.fleet.workspace
    if ctx.state.data.get("workspace_label"):
        return str(ctx.state.data["workspace_label"])
    workspace = ctx.state.workspace_id or ctx.invocation.workspace_id
    return label_of(ctx, workspace) if workspace else ""


def ensure_seat(ctx: Ctx, seat: Seat, target: Target, prev: Optional[str],
                live: Dict[str, Dict], report: Report) -> Tuple[Optional[str], bool]:
    """Design 4.1 step 4 for one seat. Returns (pane, started in this run)."""
    agent = live.get(seat.name)
    if agent:
        return _already_live(ctx, seat, agent, report), False
    pane, created = None, False
    recorded = ctx.state.pane_of(seat.name)
    if recorded:
        verdict = _check_recorded(ctx, seat, recorded, report)
        if verdict == "adopted":
            return recorded, False
        if verdict == "conflict":
            return None, False
        if verdict == "use":
            pane, created = recorded, bool((ctx.state.entry(seat.name) or {}).get("created_pane"))
    if pane is None and target.free_pane and target.free_for == seat.name:
        pane, target.free_pane = target.free_pane, None
    elif pane is None:
        pane = _split(ctx, seat, prev or target.anchor, report)
        if pane is None:
            return None, False
        created = True
    if pane == ctx.invocation.pane_id:
        report.add(seat.name, "refused", "will not start an agent in the caller's own pane %s" % pane, "fail")
        return None, False
    return pane, start_seat(ctx, seat, pane, created, report)


def _already_live(ctx: Ctx, seat: Seat, agent: Dict, report: Report) -> str:
    pane = str(agent.get("pane_id"))
    entry = ctx.state.entry(seat.name)
    if not entry or entry.get("pane_id") != pane:
        ctx.state.drop(seat.name)
        entry = ctx.state.update(seat.name, pane_id=pane, created_pane=False, kind=agent.get("agent"))
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
    ctx.state.update(seat.name, pane_id=pane, kind=kind, blocked_at_startup=False)
    ctx.save()
    report.add(seat.name, "adopted", "named the unnamed %s agent in %s (resumed by herdr); no kickoff sent"
               % (kind, pane))
    return "adopted"


def _split(ctx: Ctx, seat: Seat, source: Optional[str], report: Report) -> Optional[str]:
    if not source:
        report.add(seat.name, "failed", "no pane to split from", "fail")
        return None
    argv = split_argv(source, seat.split, seat.ratio, seat.cwd, seat.env)
    if ctx.dry_run:
        report.add(seat.name, "plan", render_cmd(argv))
        return "<pane of %s>" % seat.name
    try:
        pane = ctx.herdr.pane_split(source, seat.split, seat.ratio, seat.cwd, seat.env)
    except HerdrError as exc:
        report.add(seat.name, "failed", "pane split: %s" % describe(exc), "fail")
        return None
    ctx.state.update(seat.name, pane_id=pane, created_pane=True)
    ctx.save()
    return pane


def start_seat(ctx: Ctx, seat: Seat, pane: str, created: bool, report: Report) -> bool:
    """Start the seat in a pane; record it. True when it is ready for a kickoff."""
    if ctx.dry_run:
        report.add(seat.name, "plan", render_cmd(start_argv(seat.name, seat.kind, pane, seat.args)))
        return True
    ctx.state.update(seat.name, pane_id=pane, created_pane=created)
    ctx.save()
    outcome = start_in_pane(ctx.herdr, ctx.clock, seat, pane)
    record = dict(kind=seat.kind, args=list(seat.args), started_at=now_iso())
    if outcome.status == "started":
        ctx.state.update(seat.name, blocked_at_startup=False, **record)
        ctx.save()
        report.add(seat.name, "started", outcome.detail)
        return True
    if outcome.status == "blocked":
        ctx.state.update(seat.name, blocked_at_startup=True, **record)
        ctx.save()
        report.add(seat.name, "blocked", outcome.detail, "partial")
        ctx.notify("Fleet: %s is waiting for you" % seat.name, outcome.detail, "request")
        return False
    report.add(seat.name, "failed", outcome.detail, "fail")
    return False


def send_kickoffs(ctx: Ctx, seats: Sequence[Seat], reason: str, note: Optional[str],
                  label: str, report: Report) -> None:
    """Brief seats in file order, after every seat has started (design 3)."""
    ordered = sorted(seats, key=lambda s: s.index)
    if ctx.dry_run:
        report.add("kickoff", "plan", "herdr agent prompt (reason %s) to %s"
                   % (reason, ", ".join(s.name for s in ordered)))
        return
    live = live_by_name(ctx.herdr)
    rows = [(s.name, (live.get(s.name) or {}).get("agent") or s.kind,
             (live.get(s.name) or {}).get("pane_id") or ctx.state.pane_of(s.name) or "-", s.cwd)
            for s in ctx.fleet.seats]
    roster = roster_table(rows)
    for seat in ordered:
        try:
            text = compose(seat, ctx.fleet, label, roster, reason, note)
        except OSError as exc:
            report.add(seat.name, "not briefed", "cannot read kickoff template: %s" % exc, "fail")
            continue
        outcome = safe_prompt(ctx.herdr, seat.name, text)
        if outcome.ok:
            ctx.state.update(seat.name, last_kickoff_at=now_iso(), blocked_at_startup=False)
            ctx.save()
            report.add(seat.name, "briefed", "kickoff sent (%s)" % reason)
        else:
            report.add(seat.name, "not briefed", outcome.detail, "partial")
