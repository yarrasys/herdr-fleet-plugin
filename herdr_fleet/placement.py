"""Where new panes go (design 4.1 step 4 and section 14).

Placement runs after each seat has been classified (live, adopted, recorded
pane reused, or needing a new pane) and before any seat starts. It only
splits panes; ``bringup`` starts the agents.

- ``place_chain``: each seat splits from the previous seat's pane (or its
  ``from`` seat's pane), in its own ``split`` direction and ``ratio``.
- ``place_grid``: the seats placed in this run form an even grid
  (``layout.grid_plan``) in the grid area: the root pane of a workspace
  created in this run, else a pane split off the anchor with [layout]
  ``direction`` and ``ratio`` (the anchor keeps ``ratio``). When seats placed
  by an earlier run are already in the workspace, new seats are appended with
  the chain rule instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from herdr_fleet.context import Ctx, Report
from herdr_fleet.fleetfile import Seat
from herdr_fleet.herdr import HerdrError, describe, render_cmd, split_argv
from herdr_fleet.layout import describe_shape, grid_plan, placeholder

GRID_AREA_RATIO = 0.5  # share the anchor keeps when [layout] ratio is unset

Placed = Dict[str, Tuple[str, bool]]  # seat name -> (pane, created by this run)


@dataclass
class Target:
    """Where seats go in this run."""

    workspace_id: Optional[str]   # None only in --dry-run when it would be created
    label: str
    anchor: Optional[str]         # pane the first new seat (or the grid area) splits from
    free_pane: Optional[str] = None   # root pane of a workspace created in this run...
    free_for: Optional[str] = None    # ...reserved for this seat, whose cwd and env it got


def in_target(ctx: Ctx, pane: str, target: Target, cache: Dict[str, Optional[str]]) -> bool:
    if pane.startswith("<") or target.workspace_id is None:
        return True  # --dry-run placeholders
    if pane not in cache:
        try:
            cache[pane] = ctx.herdr.pane_get(pane).get("workspace_id")
        except HerdrError:
            cache[pane] = None
    return cache[pane] == target.workspace_id


def pane_of(ctx: Ctx, name: str, live: Dict[str, Dict], placed: Placed) -> Optional[str]:
    """A seat's pane as known now: placed this run, live, or recorded."""
    if name in placed:
        return placed[name][0]
    pane = (live.get(name) or {}).get("pane_id") or ctx.state.pane_of(name)
    return str(pane) if pane else None


def chain_source(ctx: Ctx, seat: Seat, live: Dict[str, Dict], target: Target, placed: Placed,
                 cache: Dict[str, Optional[str]]) -> Optional[str]:
    """The pane ``seat`` splits from under the chain rule: its ``from`` seat's
    pane, else the nearest earlier seat's pane in the target workspace."""
    if seat.from_seat:
        pane = pane_of(ctx, seat.from_seat, live, placed)
        if pane and in_target(ctx, pane, target, cache):
            return pane
        ctx.say("%s: from = %r has no pane in the workspace; splitting from the previous seat instead"
                % (seat.name, seat.from_seat))
    seats = ctx.fleet.seats
    for earlier in reversed(seats[:seats.index(seat)]):
        pane = pane_of(ctx, earlier.name, live, placed)
        if pane and in_target(ctx, pane, target, cache):
            return pane
    return None


def split_pane(ctx: Ctx, seat: Seat, source: Optional[str], report: Report, direction: Optional[str] = None,
               ratio: Optional[float] = None) -> Optional[str]:
    """Split ``source`` to make ``seat``'s pane (seat's cwd and env); record it."""
    if not source:
        report.add(seat.name, "failed", "no pane to split from", "fail")
        return None
    direction = direction or seat.split
    ratio = seat.ratio if ratio is None else ratio
    argv = split_argv(source, direction, ratio, seat.cwd, seat.env)
    if ctx.dry_run:
        report.add(seat.name, "plan", render_cmd(argv))
        return placeholder(seat.name)
    try:
        pane = ctx.herdr.pane_split(source, direction, ratio, seat.cwd, seat.env)
    except HerdrError as exc:
        report.add(seat.name, "failed", "pane split: %s" % describe(exc), "fail")
        return None
    ctx.state.update(seat.name, pane_id=pane, created_pane=True)
    ctx.save()
    return pane


def place_chain(ctx: Ctx, needs: Sequence[Seat], target: Target, live: Dict[str, Dict], report: Report,
                fallback: Optional[str] = None) -> Placed:
    """Chain rule, in file order. ``fallback`` is used when no earlier seat has
    a pane (grid append: the last placed seat's pane), then the anchor."""
    placed: Placed = {}
    cache: Dict[str, Optional[str]] = {}
    for seat in needs:
        if target.free_pane and target.free_for == seat.name:
            placed[seat.name] = (target.free_pane, False)  # root pane: never closed by --close-panes
            target.free_pane = None
            continue
        source = chain_source(ctx, seat, live, target, placed, cache) or fallback or target.anchor
        pane = split_pane(ctx, seat, source, report)
        if pane:
            placed[seat.name] = (pane, True)
    return placed


def existing_panes(ctx: Ctx, needs: Sequence[Seat], live: Dict[str, Dict], target: Target) -> List[str]:
    """Panes of other seats already in the target workspace, in file order
    (the caller's own pane, e.g. after --me, does not count)."""
    skip = {s.name for s in needs}
    cache: Dict[str, Optional[str]] = {}
    panes = []
    for seat in ctx.fleet.seats:
        pane = None if seat.name in skip else pane_of(ctx, seat.name, live, {})
        if pane and pane != ctx.invocation.pane_id and in_target(ctx, pane, target, cache):
            panes.append(pane)
    return panes


def place_grid(ctx: Ctx, needs: Sequence[Seat], target: Target, live: Dict[str, Dict], report: Report) -> Placed:
    """Design 14.2: an even grid for the seats placed in this run."""
    if not needs:
        return {}
    fleet = ctx.fleet
    existing = existing_panes(ctx, needs, live, target)
    if existing:
        report.add("layout", "grid", "seats already placed in the workspace; appending %d with the chain rule"
                   % len(needs))
        return place_chain(ctx, needs, target, live, report, fallback=existing[-1])
    first = needs[0]
    if target.free_pane and target.free_for == first.name:
        area, created = target.free_pane, False
        target.free_pane = None
    else:
        ratio = fleet.ratio if fleet.ratio is not None else GRID_AREA_RATIO
        area = split_pane(ctx, first, target.anchor, report, fleet.direction, ratio)
        if area is None:
            return {}
        created = True
    names = [s.name for s in needs]
    report.add("layout", "grid", "%s in %s" % (describe_shape(names, fleet.columns), area))
    placed: Placed = {first.name: (area, created)}
    refs = {area: area}
    for step in grid_plan(names, area, fleet.columns):
        seat = fleet.seat(step.seat)
        source = refs.get(step.source)
        if source is None:
            report.add(step.seat, "failed", "not placed: the pane it splits from was not created", "fail")
            continue
        pane = split_pane(ctx, seat, source, report, step.direction, step.ratio)
        if pane:
            placed[seat.name] = (pane, True)
            refs[placeholder(seat.name)] = pane
    return placed
