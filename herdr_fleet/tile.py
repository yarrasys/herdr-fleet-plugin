"""``tile`` (design 15.1): re-tile the live seats in place, without restarting them.

The seats live in the fleet's workspace are laid out again: the ``[layout]``
grid (``layout.grid_plan``), or in chain mode the chain order with each seat's
``split``, ``ratio`` and ``from``. The first seat's pane stays put and anchors
the result. Every other seat makes one out-and-back move: parked in a tab of
its own (``pane move --new-tab``), then moved back beside its planned source
pane (``pane move --tab --split --target-pane --ratio``). All seats are parked
before any comes back, so the first seat's pane is the whole area when the
plan's ratios are applied and the cells come out exact whatever the starting
layout. (Moving a pane to the tab it is already in is a no-op in herdr 0.9.4,
hence the round trip.) Processes, agent names and pane ids survive; the
temporary tabs vanish once empty. Only ``pane move`` runs: no agent is
prompted, quit or started.

The watcher strip ``up`` opened (design 19.1), when it is in the same tab, is
parked after the seats and brought back under the first seat's pane, which
keeps ``1 - [watch] ratio``, before any seat comes back; so it spans the whole
layout afterwards. A strip in another tab or workspace is reported and left
alone; a failed strip move is reported and the seats are tiled anyway.

Afterwards every live seat's pane is named after its seat (``labels.apply``, design
22.1), which is a display name only and moves nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from herdr_fleet import labels, watchpane
from herdr_fleet.context import Ctx, OpError, Report
from herdr_fleet.fleetfile import Seat
from herdr_fleet.herdr import HerdrError, describe, pane_move_argv, pane_park_argv, render_cmd
from herdr_fleet.layout import describe_shape, grid_plan, placeholder
from herdr_fleet.seats import live_by_name
from herdr_fleet.state import pane_moved

PARK_LABEL = "park"


@dataclass
class Move:
    """Re-attach ``seat`` beside ``source_seat``'s pane."""

    seat: str
    source_seat: str
    direction: str
    ratio: Optional[float]


def fleet_workspace(ctx: Ctx) -> str:
    """The fleet's workspace, without creating anything."""
    fleet, state, inv = ctx.fleet, ctx.state, ctx.invocation
    if not fleet.workspace:
        if not inv.workspace_id:
            raise OpError("the fleet file sets no workspace, and this was not run from a herdr pane")
        return inv.workspace_id
    ids = [str(w.get("workspace_id")) for w in ctx.herdr.workspace_list() if w.get("label") == fleet.workspace]
    if not ids:
        raise OpError("no workspace is labelled %r; run `herdr-fleet up` first" % fleet.workspace)
    chosen = next((c for c in (state.workspace_id, inv.workspace_id) if c and c in ids), None)
    if chosen is None and len(ids) > 1:
        raise OpError("several workspaces are labelled %r (%s); rename all but one" % (fleet.workspace, ", ".join(ids)))
    return chosen or ids[0]


def plan_moves(ctx: Ctx, seats: List[Seat]) -> List[Move]:
    """The moves for every seat after the first, in the order they must happen."""
    names = [s.name for s in seats]
    if ctx.fleet.mode == "grid":
        area = placeholder(names[0])
        return [Move(step.seat, names[0] if step.source == area else step.source[len("<pane of "):-1],
                     step.direction, step.ratio)
                for step in grid_plan(names, area, ctx.fleet.columns)]
    moves = []
    for index, seat in enumerate(seats[1:], 1):
        source = seat.from_seat if seat.from_seat in names[:index] else names[index - 1]
        moves.append(Move(seat.name, source, seat.split, seat.ratio))
    return moves


def op_tile(ctx: Ctx) -> Report:
    report = Report("tile")
    _tile(ctx, report)
    labels.apply(ctx, None, report)  # design 22.1; never on a dry run
    return report


def _tile(ctx: Ctx, report: Report) -> None:
    fleet = ctx.fleet
    workspace = fleet_workspace(ctx)
    live = live_by_name(ctx.herdr)
    here: List[Tuple[Seat, Dict]] = []
    for seat in fleet.seats:
        agent = live.get(seat.name)
        if agent is None:
            report.add(seat.name, "absent", "not running; not tiled")
        elif agent.get("workspace_id") != workspace:
            report.add(seat.name, "elsewhere", "in workspace %s, not %s; left alone"
                       % (agent.get("workspace_id"), workspace))
        else:
            here.append((seat, agent))
    if not here:
        report.add("tile", "nothing to do", "no seat is live in workspace %s" % workspace)
        return
    tabs = sorted({str(agent.get("tab_id")) for _, agent in here})
    if len(tabs) > 1:
        raise OpError("the seats in workspace %s span %d tabs, and tile works within one tab: %s"
                      % (workspace, len(tabs), ", ".join("%s (%s)" % (s.name, a.get("tab_id")) for s, a in here)))
    tab = tabs[0]
    seats = [seat for seat, _ in here]
    panes = {seat.name: str(agent["pane_id"]) for seat, agent in here}
    moves = plan_moves(ctx, seats)
    shape = describe_shape([s.name for s in seats], fleet.columns) if fleet.mode == "grid" \
        else "chain of %d seats" % len(seats)
    report.add("layout", fleet.mode, "%s in tab %s" % (shape, tab))
    anchor = seats[0].name
    report.add(anchor, "kept", "anchors the layout in %s" % panes[anchor])
    strip = watchpane.tile_strip(ctx, workspace, tab, report)
    if ctx.dry_run:
        if strip:
            report.add("watcher", "plan", watchpane.tile_plan(ctx, strip, anchor, panes[anchor], workspace, tab,
                                                              PARK_LABEL))
        for move in moves:
            pane, source = panes[move.seat], panes[move.source_seat]
            report.add(move.seat, "plan", "%s, then %s" % (
                render_cmd(pane_park_argv(pane, workspace, PARK_LABEL)),
                render_cmd(pane_move_argv(pane, tab, move.direction, move.ratio, source))))
        return
    _execute(ctx, moves, panes, workspace, tab, report, (strip, anchor) if strip else None)


def _execute(ctx: Ctx, moves: List[Move], panes: Dict[str, str], workspace: str, tab: str, report: Report,
             strip: Optional[Tuple[str, str]] = None) -> None:
    """Park every seat, move the watcher strip (pane, anchor seat) under the anchor, then
    bring each seat back in plan order. Stop at the first failed seat move."""
    original = dict(panes)
    for index, move in enumerate(moves):
        try:
            panes[move.seat] = ctx.herdr.pane_move_new_tab(panes[move.seat], workspace, PARK_LABEL)
        except HerdrError as exc:
            _stop(moves, moves[:index], move, "parking", exc, panes, tab, report)
            return
    if strip:
        pane, anchor = strip
        watchpane.tile_move(ctx, pane, anchor, panes[anchor], workspace, tab, PARK_LABEL, report)
    for index, move in enumerate(moves):
        source = panes[move.source_seat]
        before = original[move.seat]
        try:
            panes[move.seat] = ctx.herdr.pane_move_to_tab(panes[move.seat], tab, move.direction, move.ratio, source)
        except HerdrError as exc:
            _stop(moves[index:], moves[index:], move, "moving back", exc, panes, tab, report)
            return
        if panes[move.seat] != before:
            ctx.say(pane_moved(move.seat, before, panes[move.seat]))
            ctx.state.update(move.seat, pane_id=panes[move.seat])
            ctx.save()
        report.add(move.seat, "moved", "%s %s (%s), ratio %s"
                   % ("right of" if move.direction == "right" else "below", move.source_seat, source,
                      move.ratio if move.ratio is not None else "herdr's default"))


def _stop(untiled: List[Move], parked: List[Move], failed: Move, step: str, exc: HerdrError,
          panes: Dict[str, str], tab: str, report: Report) -> None:
    report.add(failed.seat, "failed", "%s: %s" % (step, describe(exc)), "fail")
    names = [m.seat for m in untiled]
    detail = "stopped; not re-tiled: %s" % ", ".join(names)
    if parked:
        detail += ("; parked in tabs of their own (bring one back with `%s`)"
                   % render_cmd(pane_move_argv(panes[parked[0].seat], tab, "right", None)))
    report.add("tile", "stopped", detail, "fail")
