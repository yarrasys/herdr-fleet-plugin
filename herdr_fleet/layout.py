"""Grid layout plans (design section 14). Pure functions: no herdr, no files.

``herdr pane split SRC --direction D --ratio r`` keeps share ``r`` for SRC and
gives ``1 - r`` to the new pane (verified live on herdr 0.9.4). An even grid
therefore comes from chained splits whose ratio is ``1 / parts`` where
``parts`` counts the cells still to be carved out of SRC, SRC's own included:

- column heads: split the previous head ``right`` with ``1 / (columns remaining)``;
- rows: split the previous cell of the column ``down`` with ``1 / (rows remaining)``.

All column heads are made before any rows, so every column spans the full
height of the grid area. Seats fill the grid column-major in file order;
rows are spread evenly with the extra rows in the first columns.

``band_plan`` (design 26) puts one seat, ``[layout] top``, across the whole
width above the grid: from the top seat's pane, the watcher strip is split
``down`` keeping ``1 - watch_ratio``, then the grid's first seat is split
``down`` keeping ``top_ratio / (1 - watch_ratio)`` (so the band is
``top_ratio`` of the whole area), and the rest of the grid is ``grid_plan``
with that first seat's pane as the grid area. ``is_top_band`` judges from
``pane layout`` rects whether a live layout already has that shape.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

RATIO_DIGITS = 4
HERDR_RATIO_RANGE = (0.1, 0.9)  # herdr clamps every split ratio to this (design 19.4)
STRIP = "(watcher strip)"       # band_plan's name for the strip's step; never a seat name
Rect = Tuple[float, float, float, float]  # x, y, width, height


@dataclass(frozen=True)
class Split:
    """One step of a plan: split ``source`` to create the pane of ``seat``."""

    seat: str
    source: str     # the grid area, or placeholder(name) for a pane made earlier in the plan
    direction: str  # right | down
    ratio: float    # share ``source`` keeps


def placeholder(name: str) -> str:
    """How plans and --dry-run refer to a seat's pane before it exists."""
    return "<pane of %s>" % name


def grid_shape(count: int, columns: Optional[int] = None) -> List[int]:
    """Rows per column for ``count`` seats: ``columns`` columns (default
    ceil(sqrt(count)), never more than ``count``), extra rows to the first columns."""
    if count <= 0:
        return []
    cols = columns if columns else int(math.ceil(math.sqrt(count)))
    cols = max(1, min(cols, count))
    base, extra = divmod(count, cols)
    return [base + 1 if j < extra else base for j in range(cols)]


def grid_cells(names: Sequence[str], columns: Optional[int] = None) -> List[List[str]]:
    """Seat names per column, column-major in the given order."""
    cells: List[List[str]] = []
    start = 0
    for rows in grid_shape(len(names), columns):
        cells.append(list(names[start:start + rows]))
        start += rows
    return cells


def share(parts: int) -> float:
    """The ratio that leaves the split pane one of ``parts`` equal parts."""
    return round(1.0 / parts, RATIO_DIGITS)


def grid_plan(names: Sequence[str], area: str, columns: Optional[int] = None) -> List[Split]:
    """The ordered splits that turn ``area`` into an even grid of ``names``.
    ``names[0]`` takes ``area`` itself (the first cell), so it has no split."""
    cells = grid_cells(names, columns)
    if not cells:
        return []
    refs: Dict[str, str] = {names[0]: area}

    def pane(name: str) -> str:
        return refs.get(name) or placeholder(name)

    plan: List[Split] = []
    heads = [column[0] for column in cells]
    for j in range(1, len(heads)):
        plan.append(Split(heads[j], pane(heads[j - 1]), "right", share(len(heads) - (j - 1))))
    for column in cells:
        for i in range(1, len(column)):
            plan.append(Split(column[i], pane(column[i - 1]), "down", share(len(column) - (i - 1))))
    return plan


def describe_shape(names: Sequence[str], columns: Optional[int] = None, top: Optional[str] = None) -> str:
    """``4 seats in 2 columns (2+2)``; with ``top`` (design 26), ``top: NAME, then 3 seats
    in 2 columns (2+1)``, the top seat never counted in the grid."""
    if top is not None:
        others = [n for n in names if n != top]
        rest = describe_shape(others, columns) if others else "no other seats"
        return "top: %s, then %s" % (top, rest)
    shape = grid_shape(len(names), columns)
    return "%d seat%s in %d column%s (%s)" % (len(names), "" if len(names) == 1 else "s", len(shape),
                                              "" if len(shape) == 1 else "s", "+".join(str(r) for r in shape))


# -- the top band (design 26) --------------------------------------------------------------------


def clamp_ratio(value: float) -> float:
    """``value`` rounded to RATIO_DIGITS and kept within herdr's 0.1-0.9."""
    low, high = HERDR_RATIO_RANGE
    return round(min(max(value, low), high), RATIO_DIGITS)


def strip_keep(watch_ratio: float) -> float:
    """The share the band's pane keeps when the strip is split from it (19.4's ``1 - ratio``)."""
    return clamp_ratio(1.0 - watch_ratio)


def band_ratio(top_ratio: float, watch_ratio: Optional[float]) -> Tuple[float, float]:
    """``(ratio, wanted)``: the share the band's pane keeps when the grid's first seat is
    split from it, ``top_ratio / (1 - watch_ratio)`` (``top_ratio`` without a strip), as
    herdr will take it (clamped to 0.1-0.9) and as computed. They differ only when clamped."""
    keep = 1.0 - watch_ratio if watch_ratio else 1.0
    wanted = round(top_ratio / keep, RATIO_DIGITS)
    return clamp_ratio(wanted), wanted


def band_warning(ratio: float, wanted: float) -> str:
    """The warning when ``band_ratio`` had to clamp (a large ``top_ratio`` with a large strip)."""
    return ("layout.top_ratio: the grid's first seat under the band would keep %s of the band's pane, outside "
            "herdr's %s-%s; using %s" % (wanted, HERDR_RATIO_RANGE[0], HERDR_RATIO_RANGE[1], ratio))


def band_plan(top: str, names: Sequence[str], columns: Optional[int] = None, top_ratio: float = 0.3,
              watch_ratio: Optional[float] = None) -> List[Split]:
    """The ordered splits that make ``top``'s pane a band across the whole width above an
    even grid of ``names`` (``top`` is left out of the grid if listed), with the watcher
    strip at the bottom when ``watch_ratio`` is given: the strip (``STRIP``) from the
    band's pane keeping ``1 - watch_ratio``, the grid's first seat from it keeping
    ``top_ratio / (1 - watch_ratio)``, then ``grid_plan`` with that seat's pane as the
    area. Sources are placeholders (``placeholder(top)`` is the band's pane)."""
    others = [n for n in names if n != top]
    anchor = placeholder(top)
    plan: List[Split] = []
    if watch_ratio:
        plan.append(Split(STRIP, anchor, "down", strip_keep(watch_ratio)))
    if not others:
        return plan
    plan.append(Split(others[0], anchor, "down", band_ratio(top_ratio, watch_ratio)[0]))
    plan.extend(grid_plan(others, placeholder(others[0]), columns))
    return plan


def is_top_band(top: Rect, others: Sequence[Rect], tolerance: float = 1.0) -> bool:
    """Whether ``top`` is a band across the seats' whole width above all of them (design
    26.2): it spans the left and right edges of every seat's rect (within ``tolerance``
    cells, as pane rects are drawn inside borders) and no seat starts above it."""
    if not others:
        return True
    left = min([top[0]] + [r[0] for r in others])
    right = max([top[0] + top[2]] + [r[0] + r[2] for r in others])
    if top[0] - left > tolerance or right - (top[0] + top[2]) > tolerance:
        return False
    return all(r[1] >= top[1] + top[3] - tolerance for r in others)
