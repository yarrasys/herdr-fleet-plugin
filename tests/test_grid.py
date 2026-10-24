"""Grid mode, ``from`` and the ``--me`` hint (design section 14), end to end
against the fake herdr. Cell sizes are checked from the splits herdr received."""

from __future__ import annotations

import os
import unittest

import fake_herdr
from support import ARGS, CALLER_PANE, FOUR_SEATS, GridCase, THREE_SEATS, grid_fleet
from herdr_fleet.fleetfile import FleetError, load_fleet

class GridInCreatedWorkspace(GridCase):
    fleet_text = grid_fleet()

    def test_root_pane_is_the_first_cell_and_cells_are_equal(self):
        code, out, err = self.run_cli("up")
        self.assertEqual(code, 0, out + err)
        ws = self.read_state()["workspace_id"]
        root = ws + ":p1"
        self.assertEqual(self.pane_of("sample-orchestrator"), root)
        self.assertIn("layout: grid - 4 seats in 2 columns (2+2) in %s" % root, out)
        splits = [(a[2], a[4], a[6]) for a in self.argvs("pane", "split")]
        reviewer, dev, designer = (self.pane_of(n) for n in ("sample-reviewer", "sample-dev", "sample-designer"))
        self.assertEqual(splits, [(root, "right", "0.5"), (root, "down", "0.5"), (reviewer, "down", "0.5")])
        rects = self.rects(root)
        self.assert_cell(rects[root], 0, 0, 0.5, 0.5)       # orchestrator
        self.assert_cell(rects[dev], 0, 0.5, 0.5, 0.5)
        self.assert_cell(rects[reviewer], 0.5, 0, 0.5, 0.5)
        self.assert_cell(rects[designer], 0.5, 0.5, 0.5, 0.5)
        state = self.read_state()["agents"]
        self.assertFalse(state["sample-orchestrator"]["created_pane"])  # root pane
        self.assertTrue(state["sample-designer"]["created_pane"])
        starts = [a[2] for a in self.argvs("agent", "start")]
        self.assertEqual(starts, ["sample-orchestrator", "sample-dev", "sample-reviewer", "sample-designer"])
        self.assertEqual(len(self.argvs("agent", "prompt")), 4)

    def test_five_seats_three_columns(self):
        self.write_fleet(grid_fleet(FOUR_SEATS + '\n[[agents]]\nname = "sample-qa"\nkind = "codex"\n'))
        code, out, _ = self.run_cli("up")
        self.assertEqual(code, 0, out)
        self.assertIn("5 seats in 3 columns (2+2+1)", out)
        root = self.read_state()["workspace_id"] + ":p1"
        rects = self.rects(root)
        for name, (x, y, h) in {"sample-orchestrator": (0, 0, 0.5), "sample-dev": (0, 0.5, 0.5),
                                "sample-reviewer": (1 / 3.0, 0, 0.5), "sample-designer": (1 / 3.0, 0.5, 0.5),
                                "sample-qa": (2 / 3.0, 0, 1.0)}.items():
            self.assert_cell(rects[self.pane_of(name)], x, y, 1 / 3.0, h)

    def test_dry_run_prints_every_split_with_its_ratio(self):
        code, out, _ = self.run_cli("up", "--dry-run")
        self.assertEqual(code, 0, out)
        for line in ("sample-reviewer: plan - herdr pane split '<root pane of sample>' --direction right --ratio 0.5",
                     "sample-dev: plan - herdr pane split '<root pane of sample>' --direction down --ratio 0.5",
                     "sample-designer: plan - herdr pane split '<pane of sample-reviewer>' --direction down "
                     "--ratio 0.5"):
            self.assertIn(line, out)
        self.assertEqual(self.argvs("pane", "split"), [])

    def test_failed_column_head_skips_the_cells_below_it(self):
        self.world["canned"] = [{"prefix": ["pane", "split"], "times": 1, "exit": 1,
                                 "stderr": {"error": {"code": "split_failed", "message": "no room"}}}]
        code, out, _ = self.run_cli("up")
        self.assertEqual(code, 1)
        self.assertIn("sample-reviewer: failed - pane split: herdr error split_failed: no room", out)
        self.assertIn("sample-designer: failed - not placed: the pane it splits from was not created", out)
        self.assertEqual([a[2] for a in self.argvs("agent", "start")], ["sample-orchestrator", "sample-dev"])


class GridBesideTheCaller(GridCase):
    """No workspace label: the grid area is split off the caller's pane."""

    fleet_text = grid_fleet(FOUR_SEATS.replace('workspace = "sample"\n', ""),
                            '[layout]\nmode = "grid"\nratio = 0.4\n')

    def test_me_keeps_ratio_and_the_others_fill_the_grid(self):
        code, out, err = self.run_cli("up", "--me", "sample-orchestrator")
        self.assertEqual(code, 0, out + err)
        splits = self.argvs("pane", "split")
        self.assertEqual(splits[0][2:7], [CALLER_PANE, "--direction", "right", "--ratio", "0.4"])  # the area
        area = self.pane_of("sample-dev")
        self.assertIn("layout: grid - 3 seats in 2 columns (2+1) in %s" % area, out)
        rects = self.rects(CALLER_PANE)
        self.assert_cell(rects[CALLER_PANE], 0, 0, 0.4, 1)               # the orchestrator keeps 40%
        self.assert_cell(rects[area], 0.4, 0, 0.3, 0.5)                   # dev
        self.assert_cell(rects[self.pane_of("sample-reviewer")], 0.4, 0.5, 0.3, 0.5)
        self.assert_cell(rects[self.pane_of("sample-designer")], 0.7, 0, 0.3, 1)
        self.assertNotIn(CALLER_PANE, [a[a.index("--pane") + 1] for a in self.argvs("agent", "start")])
        self.assertEqual(self.prompts_to("sample-orchestrator"), [])

    def test_without_me_the_caller_keeps_ratio_too(self):
        code, out, _ = self.run_cli("up")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.argvs("pane", "split")[0][2:7], [CALLER_PANE, "--direction", "right", "--ratio", "0.4"])
        self.assertIn("4 seats in 2 columns (2+2)", out)


class GridInExistingWorkspace(GridCase):
    fleet_text = grid_fleet()

    def test_area_splits_off_the_first_pane(self):
        root = self.fleet_ws()
        code, out, _ = self.run_cli("up")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.argvs("pane", "split")[0][2:7], [root, "--direction", "right", "--ratio", "0.5"])
        self.assertNotIn(root, [a[a.index("--pane") + 1] for a in self.argvs("agent", "start")])

    def test_partial_run_appends_with_the_chain_rule(self):
        self.fleet_ws()
        for pane, name in (("w2:p2", "sample-orchestrator"), ("w2:p3", "sample-dev")):
            fake_herdr.add_pane(self.world, pane, "w2", self.repo, created=True)
            fake_herdr.put_agent(self.world, pane, "claude", name, args=ARGS)
        self.write_state({"version": 1, "workspace_id": "w2", "workspace_label": "sample", "agents": {
            "sample-orchestrator": {"pane_id": "w2:p2", "created_pane": True},
            "sample-dev": {"pane_id": "w2:p3", "created_pane": True}}})
        code, out, _ = self.run_cli("up")
        self.assertEqual(code, 0, out)
        self.assertIn("appending 2 with the chain rule", out)
        reviewer = self.pane_of("sample-reviewer")
        self.assertEqual([a[2:5] for a in self.argvs("pane", "split")],
                         [["w2:p3", "--direction", "right"], [reviewer, "--direction", "right"]])
        self.assertNotIn("seats in", out)  # no fresh grid

    def test_restart_of_an_absent_seat_appends_after_the_last_placed_seat(self):
        self.fleet_ws()
        fake_herdr.add_pane(self.world, "w2:p2", "w2", self.repo, created=True)
        fake_herdr.put_agent(self.world, "w2:p2", "claude", "sample-dev", args=ARGS)
        self.write_state({"version": 1, "workspace_id": "w2", "agents": {
            "sample-dev": {"pane_id": "w2:p2", "created_pane": True}}})
        code, out, _ = self.run_cli("restart", "sample-orchestrator")  # first seat: nothing earlier
        self.assertEqual(code, 0, out)
        self.assertEqual(self.argvs("pane", "split")[0][2], "w2:p2")


class Warnings(GridCase):
    fleet_text = grid_fleet(FOUR_SEATS.replace('name = "sample-dev"\n',
                                               'name = "sample-dev"\nsplit = "down"\nratio = 0.3\n'
                                               'from = "sample-orchestrator"\n'))

    def test_per_seat_split_ratio_and_from_are_ignored_in_grid_mode(self):
        fleet = load_fleet(os.path.join(self.repo, ".herdr", "fleet.toml"))
        self.assertIn("agents[1] (sample-dev): split, ratio, from ignored in grid mode", fleet.warnings)
        dev = fleet.seat("sample-dev")
        self.assertEqual((dev.split, dev.ratio, dev.from_seat), ("right", None, None))
        code, out, err = self.run_cli("up", "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("split, ratio, from ignored in grid mode", err)
        self.assertIn("sample-dev: plan - herdr pane split '<root pane of sample>' --direction down --ratio 0.5",
                      out)

    def test_columns_in_chain_mode_warns(self):
        self.write_fleet(grid_fleet(THREE_SEATS, "[layout]\ncolumns = 2\n"))
        fleet = load_fleet(os.path.join(self.repo, ".herdr", "fleet.toml"))
        self.assertIn('layout.columns: ignored in chain mode (it applies to mode = "grid")', fleet.warnings)

    def test_bad_mode_and_columns_are_errors(self):
        self.write_fleet(grid_fleet(THREE_SEATS, '[layout]\nmode = "tiles"\ncolumns = 0\n'))
        with self.assertRaises(FleetError) as caught:
            load_fleet(os.path.join(self.repo, ".herdr", "fleet.toml"))
        text = "\n".join(caught.exception.errors)
        self.assertIn("layout.mode: 'tiles' must be chain or grid", text)
        self.assertIn("layout.columns: 0 must be a positive integer", text)

    def test_columns_option(self):
        self.write_fleet(grid_fleet(FOUR_SEATS, '[layout]\nmode = "grid"\ncolumns = 1\n'))
        code, out, _ = self.run_cli("up", "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("4 seats in 1 column (4)", out)
        self.assertNotIn("--direction right", out)


class From(GridCase):
    def test_from_must_name_an_earlier_seat(self):
        for value, earlier in (('"sample-reviewer"', "sample-orchestrator"), ('"nobody"', "sample-orchestrator"),
                               ('"sample-dev"', "sample-orchestrator")):
            self.write_fleet(THREE_SEATS.replace('name = "sample-dev"\n', 'name = "sample-dev"\nfrom = %s\n' % value))
            with self.assertRaises(FleetError) as caught:
                load_fleet(os.path.join(self.repo, ".herdr", "fleet.toml"))
            self.assertIn("from: %s must name a seat earlier in the file (%s)" % (value.replace('"', "'"), earlier),
                          caught.exception.errors[0])
        code, _, err = self.run_cli("up")
        self.assertEqual(code, 2)
        self.assertIn("must name a seat earlier in the file", err)

    def test_chain_seat_splits_from_its_from_seat(self):
        reviewer = 'name = "sample-reviewer"\nfrom = "sample-orchestrator"\nsplit = "down"\n'
        self.write_fleet(THREE_SEATS.replace('name = "sample-reviewer"\n', reviewer))
        code, out, _ = self.run_cli("up")
        self.assertEqual(code, 0, out)
        orchestrator = self.pane_of("sample-orchestrator")
        splits = self.argvs("pane", "split")
        self.assertEqual(splits[0][2:5], [orchestrator, "--direction", "right"])   # dev: previous seat
        self.assertEqual(splits[1][2:5], [orchestrator, "--direction", "down"])    # reviewer: from
        self.assertEqual(load_fleet(os.path.join(self.repo, ".herdr", "fleet.toml")).warnings, [])

    def test_from_seat_without_a_pane_falls_back_to_the_previous_seat(self):
        self.fleet_ws()
        self.write_fleet(THREE_SEATS.replace('name = "sample-reviewer"\n',
                                             'name = "sample-reviewer"\nfrom = "sample-orchestrator"\n'))
        code, out, _ = self.run_cli("up", "sample-dev", "sample-reviewer")
        self.assertEqual(code, 0, out)
        self.assertIn("sample-reviewer: from = 'sample-orchestrator' has no pane in the workspace", out)
        self.assertEqual(self.argvs("pane", "split")[1][2], self.pane_of("sample-dev"))


class MeHint(GridCase):
    fleet_text = THREE_SEATS.replace('workspace = "sample"\n', "")

    def test_me_without_an_agent_explains_and_continues(self):
        self.world["panes"][CALLER_PANE]["agent"] = None
        self.world["panes"][CALLER_PANE]["procs"] = [dict(fake_herdr.SHELL)]
        code, out, _ = self.run_cli("up", "--me", "sample-orchestrator")
        self.assertEqual(code, 3, out)
        self.assertIn("--me: no agent is running in your pane %s; start sample-orchestrator as a seat instead "
                      "with `herdr-fleet up sample-orchestrator`, or start a claude agent in this pane first and "
                      "run `herdr-fleet up --me sample-orchestrator` from inside it" % CALLER_PANE, out)
        self.assertEqual([a[2] for a in self.argvs("agent", "start")], ["sample-dev", "sample-reviewer"])
        self.assertEqual(self.argvs("agent", "rename"), [])


if __name__ == "__main__":
    unittest.main()
