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

- ``place_band`` (design 26): with ``[layout] top``, the top seat's pane (the
  root pane of a workspace created in this run, or the caller's pane with
  ``--me <top seat>``) is a band across the whole width: the strip is split
  from it first, then the grid's first seat ``down`` so that the band keeps
  ``top_ratio`` of the area, then the rest of the grid from that seat
  (``layout.band_plan``). ``band_anchor`` decides whether a run builds the band,
  and ``band_check`` whether a live layout already is one.

All take ``on_area``: called once with the first pane of a fresh layout (the
root pane, the grid area, the first chained seat's pane, or the band's pane)
before anything else splits it, so ``up`` can open the watcher strip below the
whole layout (design 19.1). It answers whether a strip was opened (or planned).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from herdr_fleet.context import Ctx, Report
from herdr_fleet.fleetfile import Seat
from herdr_fleet.herdr import HerdrError, describe, render_cmd, split_argv
from herdr_fleet.layout import (Rect, band_ratio, band_warning, describe_shape, grid_plan, is_top_band,
                                placeholder)

GRID_AREA_RATIO = 0.5  # share the anchor keeps when [layout] ratio is unset

Placed = Dict[str, Tuple[str, bool]]  # seat name -> (pane, created by this run)
AreaHook = Optional[Callable[[str], Optional[bool]]]


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
                fallback: Optional[str] = None, on_area: AreaHook = None) -> Placed:
    """Chain rule, in file order. ``fallback`` is used when no earlier seat has
    a pane (grid append: the last placed seat's pane), then the anchor.
    ``on_area`` gets the first pane placed, before the next seat splits it."""
    placed: Placed = {}
    cache: Dict[str, Optional[str]] = {}
    for seat in needs:
        if target.free_pane and target.free_for == seat.name:
            placed[seat.name] = (target.free_pane, False)  # root pane: never closed by --close-panes
            target.free_pane = None
        else:
            source = chain_source(ctx, seat, live, target, placed, cache) or fallback or target.anchor
            pane = split_pane(ctx, seat, source, report)
            if not pane:
                continue
            placed[seat.name] = (pane, True)
        if on_area is not None:
            on_area(placed[seat.name][0])
            on_area = None
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


def place_grid(ctx: Ctx, needs: Sequence[Seat], target: Target, live: Dict[str, Dict], report: Report,
               on_area: AreaHook = None) -> Placed:
    """Design 14.2: an even grid for the seats placed in this run. ``on_area`` gets
    the grid area before the grid is split out of it (not when appending)."""
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
    if on_area is not None:
        on_area(area)
    placed: Placed = {first.name: (area, created)}
    _fill_grid(ctx, names, area, report, placed)
    return placed


def _fill_grid(ctx: Ctx, names: Sequence[str], area: str, report: Report, placed: Placed) -> None:
    """Split the rest of an even grid of ``names`` out of ``area``, the first seat's pane."""
    fleet = ctx.fleet
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


# -- the top band (design 26) ------------------------------------------------------------------

NOT_PLACED = "top seat %s is not placed in this run; placed without the band"
NOT_HERE = "top seat %s is not in the fleet's workspace; placed without the band"
NEEDS = "top needs the fleet's own workspace or --me %s"
RUN_TILE = "run herdr-fleet tile for the top band"


def band_anchor(ctx: Ctx, me: Optional[str], adopted: bool, needs: Sequence[Seat], target: Target,
                fresh: bool, report: Report) -> Tuple[Optional[str], bool]:
    """Design 26.2: ``(pane, check)``. ``pane`` is the band's pane when this run builds the
    band: the root pane of a workspace created in this run when the top seat takes it, or
    the caller's pane in the fleet's workspace with ``--me <top seat>``. Otherwise None,
    with a ``layout:`` line saying why on a fresh layout. ``check``: whether ``up`` should
    look at the live layout afterwards (``band_check``): always but when a line said that
    the band was not applied."""
    top = ctx.fleet.top
    if top is None or ctx.fleet.mode != "grid":
        return None, False
    if not fresh:
        return None, True  # an existing layout: nothing moves (19.3); band_check says whether to tile
    if me == top:
        here = ctx.invocation.workspace_id == target.workspace_id  # both None: a dry run that would --join
        if not adopted:
            report.add("layout", NOT_PLACED % top, "")
            return None, False
        if not here:
            report.add("layout", NOT_HERE % top, "")
            return None, False
        if target.free_pane:  # joined a workspace created in this run: its root pane is taken by a seat
            return None, True
        return ctx.invocation.pane_id, True
    if target.free_pane:
        if target.free_for == top:
            return target.free_pane, True
        report.add("layout", NOT_PLACED % top, "")
        return None, False
    report.add("layout", NEEDS % top, "")
    return None, False


def place_band(ctx: Ctx, needs: Sequence[Seat], target: Target, report: Report, anchor: str,
               on_area: AreaHook = None) -> Placed:
    """Design 26.2: ``anchor`` is the top seat's pane (taken here when it is the root pane
    reserved for the top seat). The strip is split from it first (``on_area``), then the
    grid's first seat ``down`` so that the band keeps ``top_ratio`` of the area, then the
    rest of the grid from that seat, as 14.2 does."""
    fleet = ctx.fleet
    top = str(fleet.top)
    placed: Placed = {}
    if target.free_pane and target.free_pane == anchor and target.free_for == top:
        placed[top] = (anchor, False)  # root pane: never closed by --close-panes
        target.free_pane = None
    others = [s for s in needs if s.name != top]
    names = [s.name for s in others]
    report.add("layout", "grid", "%s in %s" % (describe_shape(names, fleet.columns, top=top), anchor))
    opened = bool(on_area(anchor)) if on_area is not None else False
    if not others:
        return placed
    ratio, wanted = band_ratio(fleet.top_ratio, fleet.watch_ratio if opened else None)
    if ratio != wanted:
        ctx.warn(band_warning(ratio, wanted))
    first = others[0]
    area = split_pane(ctx, first, anchor, report, "down", ratio)
    if area is None:
        for seat in others[1:]:
            report.add(seat.name, "failed", "not placed: the pane it splits from was not created", "fail")
        return placed
    placed[first.name] = (area, True)
    _fill_grid(ctx, names, area, report, placed)
    return placed


def band_check(ctx: Ctx, live: Dict[str, Dict], target: Target, report: Report) -> None:
    """Design 26.2: on a layout ``up`` did not build as a band, add ``layout: run
    herdr-fleet tile for the top band`` when the top seat is live in the fleet's workspace
    but its pane is not a band across the seats above all of them, judged from ``pane
    layout`` (read-only). Nothing is said when the layout cannot be read."""
    top = ctx.fleet.top
    agent = live.get(top or "")
    if not top or not agent or not agent.get("pane_id"):
        return
    if target.workspace_id is None or agent.get("workspace_id") not in (None, target.workspace_id):
        return
    pane = str(agent["pane_id"])
    try:
        layout = ctx.herdr.pane_layout(pane)
    except HerdrError:
        return
    rects = {str(p.get("pane_id")): _rect(p.get("rect")) for p in (layout.get("panes") or [])
             if isinstance(p, dict)}
    mine = rects.get(pane)
    if mine is None:
        return
    others: List[Rect] = []
    for seat in ctx.fleet.seats:
        other = (live.get(seat.name) or {}).get("pane_id")
        rect = rects.get(str(other)) if other and seat.name != top else None
        if rect is not None:
            others.append(rect)
    if not is_top_band(mine, others):
        report.add("layout", RUN_TILE, "")


def _rect(value: object) -> Optional[Rect]:
    if not isinstance(value, dict):
        return None
    try:
        return (float(value["x"]), float(value["y"]), float(value["width"]), float(value["height"]))
    except (KeyError, TypeError, ValueError):
        return None
