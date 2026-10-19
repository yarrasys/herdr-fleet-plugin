"""``tile`` (design 15.1): re-tile live seats with out-and-back pane moves;
nothing is prompted, quit or started."""

from __future__ import annotations

import unittest

import fake_herdr
from support import ARGS, FleetCase, GridCase, THREE_SEATS, grid_fleet

NOT_TILE = (["agent", "prompt"], ["agent", "start"], ["pane", "split"], ["pane", "close"], ["agent", "rename"])


def park(pane):
    return ["pane", "move", pane, "--new-tab", "--workspace", "w2", "--label", "park", "--no-focus"]


def back(pane, direction, source, ratio=None):
    argv = ["pane", "move", pane, "--tab", "w2:t1", "--split", direction, "--target-pane", source]
    return argv + (["--ratio", ratio] if ratio else []) + ["--no-focus"]


class TileCase(GridCase):
    """The fleet's workspace w2 with the given seats live in one tab, in a messy layout."""

    seats = (("fablyn-orchestrator", "claude"), ("fablyn-dev", "claude"), ("fablyn-reviewer", "codex"),
             ("fablyn-designer", "claude"))

    def setUp(self):
        super().setUp()
        self.fleet_ws("w2", "fablyn")
        for index, (name, kind) in enumerate(self.seats, 1):
            pane = "w2:p%d" % index
            if pane not in self.world["panes"]:
                fake_herdr.add_pane(self.world, pane, "w2", self.repo, created=True)
            fake_herdr.put_agent(self.world, pane, kind, name, args=ARGS)

    def assert_nothing_else(self):
        for argv in [c["argv"] for c in self.calls()]:
            self.assertNotIn(argv[:2], NOT_TILE, argv)


class GridTile(TileCase):
    fleet_text = grid_fleet()

    def test_three_out_and_back_pairs_in_plan_order(self):
        code, out, err = self.run_cli("tile")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.argvs("pane", "move"), [
            park("w2:p3"), park("w2:p2"), park("w2:p4"),            # everyone after the first is parked first
            back("w2:p3", "right", "w2:p1", "0.5"),                 # then re-attached in grid-plan order
            back("w2:p2", "down", "w2:p1", "0.5"),
            back("w2:p4", "down", "w2:p3", "0.5"),
        ])
        self.assertIn("fablyn-orchestrator: kept - anchors the layout in w2:p1", out)
        self.assertEqual(out.count(": moved - "), 3)
        self.assertIn("fablyn-designer: moved - below fablyn-reviewer (w2:p3), ratio 0.5", out)
        rects = self.rects("w2:p1")
        self.assert_cell(rects["w2:p1"], 0, 0, 0.5, 0.5)
        self.assert_cell(rects["w2:p2"], 0, 0.5, 0.5, 0.5)
        self.assert_cell(rects["w2:p3"], 0.5, 0, 0.5, 0.5)
        self.assert_cell(rects["w2:p4"], 0.5, 0.5, 0.5, 0.5)
        for index, (name, _) in enumerate(self.seats, 1):  # processes, names and pane ids survive
            self.assertEqual(self.pane_of(name), "w2:p%d" % index)
        self.assert_nothing_else()

    def test_dry_run_prints_the_moves_and_makes_none(self):
        code, out, _ = self.run_cli("tile", "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("fablyn-designer: plan - herdr pane move w2:p4 --new-tab --workspace w2 --label park "
                      "--no-focus, then herdr pane move w2:p4 --tab w2:t1 --split down --target-pane w2:p3 "
                      "--ratio 0.5 --no-focus", out)
        self.assertEqual(self.argvs("pane", "move"), [])
        self.assert_nothing_else()

    def test_seats_elsewhere_or_absent_are_left_alone(self):
        fake_herdr.add_workspace(self.world, "w3", "other", self.repo)
        self.world["panes"]["w2:p3"].update(workspace_id="w3", tab_id="w3:t1")
        self.world["panes"]["w2:p4"]["agent"] = None
        code, out, _ = self.run_cli("tile")
        self.assertEqual(code, 0, out)
        self.assertIn("fablyn-reviewer: elsewhere - in workspace w3, not w2; left alone", out)
        self.assertIn("fablyn-designer: absent - not running; not tiled", out)
        self.assertIn("layout: grid - 2 seats in 2 columns (1+1) in tab w2:t1", out)
        self.assertEqual(self.argvs("pane", "move"), [park("w2:p2"), back("w2:p2", "right", "w2:p1", "0.5")])

    def test_refuses_when_the_seats_span_two_tabs(self):
        self.world["panes"]["w2:p4"]["tab_id"] = "w2:t2"
        code, _, err = self.run_cli("tile")
        self.assertEqual(code, 1)
        self.assertIn("span 2 tabs, and tile works within one tab", err)
        self.assertIn("fablyn-designer (w2:t2)", err)
        self.assertEqual(self.argvs("pane", "move"), [])

    def test_stops_at_the_first_failed_move(self):
        self.world["canned"] = [{"prefix": ["pane", "move", "w2:p2"], "exit": 1,
                                 "stderr": {"error": {"code": "pane_move_failed", "message": "busy"}}}]
        code, out, _ = self.run_cli("tile")
        self.assertEqual(code, 1)
        self.assertIn("fablyn-dev: failed - parking: herdr error pane_move_failed: busy", out)
        self.assertIn("stopped; not re-tiled: fablyn-reviewer, fablyn-dev, fablyn-designer; parked in tabs of "
                      "their own (bring one back with `herdr pane move w2:p3 --tab w2:t1 --split right --no-focus`)",
                      out)
        self.assertEqual([a for a in self.argvs("pane", "move") if "--tab" in a], [])  # nothing came back
        self.assert_nothing_else()

    def test_menu_offers_tile(self):
        code, out, _ = self.run_cli("menu", stdin="1\n6\ny\n\n")
        self.assertEqual(code, 0, out)
        self.assertIn("6) tile", out)
        self.assertEqual(len(self.argvs("pane", "move")), 6)


class ChainTile(TileCase):
    fleet_text = THREE_SEATS.replace('name = "fablyn-reviewer"\n',
                                     'name = "fablyn-reviewer"\nfrom = "fablyn-orchestrator"\nsplit = "down"\n'
                                     'ratio = 0.6\n')
    seats = TileCase.seats[:3]

    def test_chain_order_with_split_ratio_and_from(self):
        code, out, err = self.run_cli("tile")
        self.assertEqual(code, 0, out + err)
        self.assertIn("layout: chain - chain of 3 seats in tab w2:t1", out)
        self.assertEqual(self.argvs("pane", "move"), [
            park("w2:p2"), park("w2:p3"),
            back("w2:p2", "right", "w2:p1"),                 # previous seat, [layout] defaults (no ratio)
            back("w2:p3", "down", "w2:p1", "0.6"),           # its `from` seat, its own split and ratio
        ])
        self.assert_nothing_else()


class NothingToTile(FleetCase):
    def test_no_live_seat(self):
        self.fleet_ws("w2", "fablyn")
        code, out, _ = self.run_cli("tile")
        self.assertEqual(code, 0, out)
        self.assertIn("tile: nothing to do - no seat is live in workspace w2", out)
        self.assertEqual(self.argvs("pane", "move"), [])

    def test_missing_workspace(self):
        code, _, err = self.run_cli("tile")
        self.assertEqual(code, 1)
        self.assertIn("no workspace is labelled 'fablyn'; run `herdr-fleet up` first", err)


if __name__ == "__main__":
    unittest.main()
