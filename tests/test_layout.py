"""The grid plan (design 14.2), proved by simulating herdr's split semantics:
``pane split SRC --direction D --ratio r`` keeps share r for SRC and gives
1 - r to the new pane (right of SRC, or below it)."""

from __future__ import annotations

import unittest

import support  # noqa: F401  (puts the plugin root on sys.path)
from herdr_fleet.layout import Split, describe_shape, grid_cells, grid_plan, grid_shape, placeholder

AREA = "w9:p1"
TOL = 1e-3  # ratios are rounded to 4 decimal places


def simulate(plan, first, area=AREA):
    """Rectangles (x, y, w, h) in a unit grid area after applying ``plan``.
    Written independently of the code under test."""
    rects = {area: (0.0, 0.0, 1.0, 1.0)}
    for step in plan:
        x, y, w, h = rects[step.source]
        if step.direction == "right":
            rects[step.source] = (x, y, w * step.ratio, h)
            rects[placeholder(step.seat)] = (x + w * step.ratio, y, w * (1 - step.ratio), h)
        else:
            rects[step.source] = (x, y, w, h * step.ratio)
            rects[placeholder(step.seat)] = (x, y + h * step.ratio, w, h * (1 - step.ratio))
    by_seat = {first: rects.pop(area)}
    for key, rect in rects.items():
        by_seat[key[len("<pane of "):-1]] = rect
    return by_seat


def names(n):
    return ["s%d" % i for i in range(1, n + 1)]


class Shape(unittest.TestCase):
    def test_rows_per_column(self):
        expected = {1: [1], 2: [1, 1], 3: [2, 1], 4: [2, 2], 5: [2, 2, 1], 6: [2, 2, 2], 7: [3, 2, 2],
                    9: [3, 3, 3], 10: [3, 3, 2, 2]}
        for count, shape in expected.items():
            self.assertEqual(grid_shape(count), shape, count)
        self.assertEqual(grid_shape(0), [])

    def test_columns_override_and_cap(self):
        self.assertEqual(grid_shape(4, 1), [4])
        self.assertEqual(grid_shape(5, 2), [3, 2])
        self.assertEqual(grid_shape(2, 5), [1, 1])  # never an empty column

    def test_column_major_in_file_order(self):
        self.assertEqual(grid_cells(names(5)), [["s1", "s2"], ["s3", "s4"], ["s5"]])
        self.assertEqual(describe_shape(names(5)), "5 seats in 3 columns (2+2+1)")


class Plan(unittest.TestCase):
    def test_one_to_six_seats_give_equal_cells(self):
        for count in range(1, 7):
            seats = names(count)
            plan = grid_plan(seats, AREA)
            self.assertEqual(len(plan), count - 1, count)
            self.assertNotIn("s1", [step.seat for step in plan])  # s1 takes the area itself
            rects = simulate(plan, "s1")
            self.assertEqual(sorted(rects), sorted(seats))
            cells = grid_cells(seats)
            for j, column in enumerate(cells):
                for i, seat in enumerate(column):
                    x, y, w, h = rects[seat]
                    self.assertAlmostEqual(w, 1.0 / len(cells), delta=TOL, msg=(count, seat))
                    self.assertAlmostEqual(h, 1.0 / len(column), delta=TOL, msg=(count, seat))
                    self.assertAlmostEqual(x, j / float(len(cells)), delta=TOL, msg=(count, seat))
                    self.assertAlmostEqual(y, i / float(len(column)), delta=TOL, msg=(count, seat))
            self.assertAlmostEqual(sum(w * h for _, _, w, h in rects.values()), 1.0, delta=TOL)

    def test_heads_first_then_rows_with_shrinking_ratios(self):
        self.assertEqual(grid_plan(names(6), AREA), [
            Split("s3", AREA, "right", 0.3333),
            Split("s5", "<pane of s3>", "right", 0.5),
            Split("s2", AREA, "down", 0.5),
            Split("s4", "<pane of s3>", "down", 0.5),
            Split("s6", "<pane of s5>", "down", 0.5),
        ])
        self.assertEqual(grid_plan(names(3), AREA), [
            Split("s3", AREA, "right", 0.5),
            Split("s2", AREA, "down", 0.5),
        ])
        self.assertEqual(grid_plan(names(1), AREA), [])

    def test_single_column_of_three(self):
        plan = grid_plan(names(3), AREA, columns=1)
        self.assertEqual([(s.seat, s.direction, s.ratio) for s in plan],
                         [("s2", "down", 0.3333), ("s3", "down", 0.5)])
        rects = simulate(plan, "s1")
        for seat in names(3):
            self.assertAlmostEqual(rects[seat][3], 1 / 3.0, delta=TOL)

    def test_larger_grids_stay_even(self):
        for count in (7, 8, 9, 10, 12):
            for columns in (None, 2, 3, 4):
                seats = names(count)
                rects = simulate(grid_plan(seats, AREA, columns), "s1")
                for column in grid_cells(seats, columns):
                    heights = {round(rects[s][3], 2) for s in column}
                    self.assertEqual(len(heights), 1, (count, columns, column))
                widths = {round(r[2], 2) for r in rects.values()}
                self.assertEqual(len(widths), 1, (count, columns))


if __name__ == "__main__":
    unittest.main()
