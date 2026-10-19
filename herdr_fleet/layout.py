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
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

RATIO_DIGITS = 4


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


def describe_shape(names: Sequence[str], columns: Optional[int] = None) -> str:
    shape = grid_shape(len(names), columns)
    return "%d seat%s in %d column%s (%s)" % (len(names), "" if len(names) == 1 else "s", len(shape),
                                              "" if len(shape) == 1 else "s", "+".join(str(r) for r in shape))
