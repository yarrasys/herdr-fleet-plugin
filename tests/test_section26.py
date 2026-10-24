"""Design section 26 (version 0.14.0): the orchestrator across the top.

26.1: ``[layout] top`` and ``top_ratio`` in the fleet file. 26.2: ``layout.band_plan`` (the
strip from the band's pane keeping ``1 - [watch] ratio``, the grid's first seat keeping
``top_ratio / (1 - [watch] ratio)``, the rest of the grid from that seat); ``up`` on a
workspace it creates (the root pane for the top seat, started first), with ``--me`` on the
top seat (the caller's pane is the band), with ``--me`` on another seat and beside the
caller (not applied, said so), and on an existing layout (nothing moves; the ``run
herdr-fleet tile`` line when the top seat is not a band); ``tile`` anchoring on the top
seat. Geometry is read from the fake's 200x50 replay of herdr's splits (``fake_herdr.geometry``)."""

from __future__ import annotations

import itertools
import os
import unittest

import fake_herdr
from support import ARGS, CALLER_PANE, FOUR_SEATS, THREE_SEATS, GridCase, grid_fleet
from herdr_fleet import layout
from herdr_fleet.fleetfile import TOP_RATIO, FleetError, load_fleet
from herdr_fleet.layout import STRIP, band_plan, band_ratio, describe_shape, is_top_band, placeholder
from herdr_fleet.scaffold import STARTER_FLEET

TOP = "sample-orchestrator"
TOP_LAYOUT = '[layout]\nmode = "grid"\ntop = "sample-orchestrator"\n'
NO_LABEL = FOUR_SEATS.replace('workspace = "sample"\n', "")
# The orchestrator last in the file (herdup's rule), with an env of its own: the band does not
# depend on where the top seat sits in the file.
TOP_LAST = """
workspace = "sample"

[[agents]]
name = "sample-dev"
kind = "claude"
args = ["--model", "claude-opus-5-5"]

[[agents]]
name = "sample-reviewer"
kind = "codex"
args = ["-m", "gpt-6-astra"]

[[agents]]
name = "sample-designer"
kind = "claude"
args = ["--model", "claude-opus-5-5"]

[[agents]]
name = "sample-orchestrator"
kind = "claude"
args = ["--model", "claude-opus-5-5"]
env = { SAMPLE_ROLE = "orchestrator" }
"""
EXAMPLE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples", "team", ".herdr",
                       "fleet.toml")


def top_fleet(text=FOUR_SEATS, extra=""):
    return grid_fleet(text, TOP_LAYOUT + extra)


def replay(plan, top, area=(0.0, 0.0, 1.0, 1.0)):
    """Shares of the whole area for each pane of a band plan (the strip as STRIP), replaying
    the splits as herdr does: the source keeps ``ratio``, the new pane gets the rest."""
    rects = {placeholder(top): area}
    for step in plan:
        x, y, w, h = rects[step.source]
        r = step.ratio
        if step.direction == "right":
            rects[step.source], rects[placeholder(step.seat)] = (x, y, w * r, h), (x + w * r, y, w * (1 - r), h)
        else:
            rects[step.source], rects[placeholder(step.seat)] = (x, y, w, h * r), (x, y + h * r, w, h * (1 - r))
    return {name[len("<pane of "):-1]: rect for name, rect in rects.items()}


class BandPlan(unittest.TestCase):
    """``layout.band_plan`` and its helpers: pure."""

    def assert_close(self, got, want, msg=None):
        for a, b in zip(got, want):
            self.assertAlmostEqual(a, b, delta=2e-3, msg=msg)

    def test_shape_for_2_to_6_seats_every_column_count_and_ratio(self):
        for count in range(2, 7):
            names = ["s%d" % i for i in range(count)]
            top, others = names[0], names[1:]
            for columns, top_ratio, watch_ratio in itertools.product(
                    range(1, len(others) + 1), (0.1, TOP_RATIO, 0.6), (0.1, 0.2, 0.4, None)):
                label = "%d seats, %d columns, top %s, watch %s" % (count, columns, top_ratio, watch_ratio)
                plan = band_plan(top, names, columns, top_ratio, watch_ratio)
                rects = replay(plan, top)
                strip = watch_ratio or 0.0
                keep, wanted = band_ratio(top_ratio, watch_ratio)
                band = keep * (1 - strip)  # top_ratio unless herdr's 0.9 clamp applied
                if keep == wanted:
                    self.assertAlmostEqual(band, top_ratio, delta=2e-3, msg=label)
                self.assert_close(rects[top], (0, 0, 1, band), label)                     # across the whole width
                if watch_ratio:
                    self.assert_close(rects[STRIP], (0, 1 - strip, 1, strip), label)     # across the bottom
                    self.assertEqual(plan[0], layout.Split(STRIP, placeholder(top), "down", round(1 - strip, 4)))
                grid = 1 - strip - band
                shape = layout.grid_shape(len(others), columns)
                for j, column in enumerate(layout.grid_cells(others, columns)):
                    for i, name in enumerate(column):
                        cell = (j / float(len(shape)), band + grid * i / len(column), 1.0 / len(shape),
                                grid / len(column))
                        self.assert_close(rects[name], cell, "%s: %s" % (label, name))

    def test_order_strip_then_the_grids_first_seat_then_the_grid(self):
        plan = band_plan(TOP, [TOP, "dev", "reviewer", "designer"], None, 0.3, 0.2)
        self.assertEqual([(s.seat, s.source, s.direction, s.ratio) for s in plan], [
            (STRIP, "<pane of sample-orchestrator>", "down", 0.8),
            ("dev", "<pane of sample-orchestrator>", "down", 0.375),
            ("designer", "<pane of dev>", "right", 0.5),
            ("reviewer", "<pane of dev>", "down", 0.5),
        ])

    def test_without_a_strip_the_band_keeps_top_ratio_itself(self):
        plan = band_plan(TOP, ["dev", "reviewer"], None, 0.3, None)
        self.assertEqual([(s.seat, s.source, s.ratio) for s in plan],
                         [("dev", "<pane of sample-orchestrator>", 0.3), ("reviewer", "<pane of dev>", 0.5)])

    def test_the_top_seat_alone_is_a_band_and_an_empty_grid(self):
        self.assertEqual(band_plan(TOP, [TOP], None, 0.3, 0.2), [layout.Split(STRIP, placeholder(TOP), "down", 0.8)])
        self.assertEqual(band_plan(TOP, [], None, 0.3, None), [])

    def test_band_ratio_rounds_and_clamps_to_herdrs_range(self):
        self.assertEqual(band_ratio(0.3, 0.2), (0.375, 0.375))
        self.assertEqual(band_ratio(0.1, 0.1), (0.1111, 0.1111))
        self.assertEqual(band_ratio(0.6, 0.4), (0.9, 1.0))
        self.assertEqual(band_ratio(0.6, 0.35), (0.9, 0.9231))
        self.assertEqual(band_ratio(0.3, None), (0.3, 0.3))
        self.assertEqual(layout.clamp_ratio(0.05), 0.1)

    def test_describe_shape(self):
        names = [TOP, "sample-dev", "sample-reviewer", "sample-designer"]
        self.assertEqual(describe_shape(names, None, top=TOP),
                         "top: sample-orchestrator, then 3 seats in 2 columns (2+1)")
        self.assertEqual(describe_shape(names[1:], 3, top=TOP),
                         "top: sample-orchestrator, then 3 seats in 3 columns (1+1+1)")
        self.assertEqual(describe_shape([TOP], None, top=TOP), "top: sample-orchestrator, then no other seats")
        self.assertEqual(describe_shape(names, None), "4 seats in 2 columns (2+2)")  # unchanged without top

    def test_is_top_band(self):
        band, left, right = (0, 0, 200, 15), (0, 15, 100, 25), (100, 15, 100, 25)
        self.assertTrue(is_top_band(band, [left, right]))
        self.assertTrue(is_top_band((1, 1, 198, 13), [(1, 16, 98, 23), (101, 16, 98, 23)]))  # inside borders
        self.assertFalse(is_top_band((0, 0, 100, 25), [(100, 0, 100, 25), (0, 25, 200, 15)]))  # a grid cell
        self.assertFalse(is_top_band((0, 25, 200, 15), [(0, 0, 200, 25)]))                    # a seat above it
        self.assertTrue(is_top_band(band, []))


class FleetFile(GridCase):
    """26.1: parsing ``top`` and ``top_ratio``."""

    def load(self, text):
        self.write_fleet(text)
        return load_fleet(os.path.join(self.repo, ".herdr", "fleet.toml"))

    def errors(self, text):
        with self.assertRaises(FleetError) as caught:
            self.load(text)
        return "\n".join(caught.exception.errors)

    def test_keys_and_defaults(self):
        fleet = self.load(top_fleet())
        self.assertEqual((fleet.top, fleet.top_ratio, fleet.warnings), (TOP, 0.3, []))
        fleet = self.load(top_fleet(extra="top_ratio = 0.25\ncolumns = 3\n"))
        self.assertEqual((fleet.top, fleet.top_ratio, fleet.columns), (TOP, 0.25, 3))
        fleet = self.load(grid_fleet())
        self.assertEqual((fleet.top, fleet.top_ratio), (None, 0.3))

    def test_unknown_seat_is_an_error(self):
        text = self.errors(grid_fleet(FOUR_SEATS, '[layout]\nmode = "grid"\ntop = "sample-boss"\n'))
        self.assertIn("layout.top: 'sample-boss' is not a seat in this file (seats: sample-orchestrator, sample-dev, "
                      "sample-reviewer, sample-designer)", text)
        code, _, err = self.run_cli("up")
        self.assertEqual(code, 2)
        self.assertIn("is not a seat in this file", err)
        self.assertEqual(self.argvs("workspace", "create"), [])

    def test_chain_mode_is_an_error(self):
        text = self.errors(grid_fleet(FOUR_SEATS, '[layout]\ntop = "sample-orchestrator"\n'))
        self.assertIn('layout.top needs mode = "grid"', text)
        text = self.errors(grid_fleet(FOUR_SEATS, '[layout]\nmode = "chain"\ntop = "sample-orchestrator"\n'))
        self.assertIn('layout.top needs mode = "grid"', text)

    def test_top_ratio_follows_the_ratio_grammar_and_is_clamped(self):
        fleet = self.load(top_fleet(extra="top_ratio = 0.05\n"))
        self.assertEqual(fleet.top_ratio, 0.1)
        self.assertIn("layout.top_ratio: 0.05 is outside 0.1-0.6; using 0.1", fleet.warnings)
        fleet = self.load(top_fleet(extra="top_ratio = 0.7\n"))
        self.assertEqual(fleet.top_ratio, 0.6)
        self.assertIn("layout.top_ratio: 0.7 is outside 0.1-0.6; using 0.6", fleet.warnings)
        for value in ("1.5", "0", "true", '"0.3"'):
            self.assertIn("layout.top_ratio: ", self.errors(top_fleet(extra="top_ratio = %s\n" % value)))

    def test_top_ratio_without_top_warns(self):
        fleet = self.load(grid_fleet(FOUR_SEATS, '[layout]\nmode = "grid"\ntop_ratio = 0.4\n'))
        self.assertIn("layout.top_ratio: ignored without layout.top", fleet.warnings)
        self.assertIsNone(fleet.top)

    def test_top_with_one_seat_gives_a_band_and_an_empty_grid(self):
        fleet = self.load(grid_fleet('workspace = "sample"\n\n[[agents]]\nname = "sample-orchestrator"\n'
                                     'kind = "claude"\n', TOP_LAYOUT))
        self.assertEqual(fleet.top, TOP)
        code, out, err = self.run_cli("up")
        self.assertEqual(code, 0, out + err)
        root = self.read_state()["workspace_id"] + ":p1"
        self.assertIn("layout: grid - top: sample-orchestrator, then no other seats in %s" % root, out)
        self.assertEqual(self.argvs("pane", "split"), [])
        cells = fake_herdr.geometry(self.world, self.world["panes"][root]["tab_id"])[0]
        self.assertEqual(cells[root], (0, 0, 200, 40))
        self.assertEqual(cells[self.read_state()["watch"]["pane_id"]], (0, 40, 200, 10))

    def test_example_and_starter(self):
        fleet = load_fleet(EXAMPLE)
        self.assertEqual((fleet.mode, fleet.top, fleet.top_ratio, fleet.warnings), ("grid", TOP, 0.3, []))
        self.assertIn('# top = "<a seat>"', STARTER_FLEET)
        self.assertIn("# top_ratio = 0.3", STARTER_FLEET)


class BandCase(GridCase):
    fleet_text = top_fleet(TOP_LAST)

    def cells(self, pane):
        return fake_herdr.geometry(self.world, self.world["panes"][pane]["tab_id"])[0]

    def strip(self):
        return (self.read_state().get("watch") or {}).get("pane_id")

    def index(self, *prefix):
        calls = [c["argv"] for c in self.calls()]
        return next(i for i, argv in enumerate(calls) if argv[:len(prefix)] == list(prefix))

    def layout_lines(self, out):
        return [line for line in out.splitlines() if line.startswith("layout: ")]

    def last_report_line(self, out):
        """The report's last line (``up`` prints the status table after a blank line)."""
        return out.split("\n\n")[0].strip().splitlines()[-1]


class FreshWorkspace(BandCase):
    """26.2: ``up`` creates the workspace; the root pane is the band."""

    def test_root_strip_band_then_grid(self):
        code, out, err = self.run_cli("up")
        self.assertEqual(code, 0, out + err)
        ws = self.read_state()["workspace_id"]
        root, strip = ws + ":p1", self.strip()
        create = self.argvs("workspace", "create")[0]
        self.assertIn("SAMPLE_ROLE=orchestrator", create)        # the root pane got the top seat's env
        self.assertEqual(self.pane_of(TOP), root)
        self.assertFalse(self.read_state()["agents"][TOP]["created_pane"])
        # the strip from the root before any seat, resized to 20%, then the grid's first seat from the root
        self.assertEqual(self.argvs("plugin", "pane", "open")[0][10], root)
        self.assertLess(self.index("pane", "resize"), self.index("pane", "split"))
        dev, reviewer, designer = (self.pane_of(n) for n in ("sample-dev", "sample-reviewer", "sample-designer"))
        self.assertEqual([a[2:7] for a in self.argvs("pane", "split")], [
            [root, "--direction", "down", "--ratio", "0.375"],
            [dev, "--direction", "right", "--ratio", "0.5"],
            [dev, "--direction", "down", "--ratio", "0.5"]])
        self.assertIn("layout: grid - top: sample-orchestrator, then 3 seats in 2 columns (2+1) in %s" % root, out)
        cells = self.cells(root)
        self.assertEqual(cells[root], (0, 0, 200, 15))           # 30% of 50 rows, the whole width
        self.assertEqual(cells[strip], (0, 40, 200, 10))         # 20% at the bottom
        self.assertEqual(cells[dev], (0, 15, 100, 12))           # the grid in the 50% between
        self.assertEqual(cells[reviewer], (0, 27, 100, 13))
        self.assertEqual(cells[designer], (100, 15, 100, 25))
        # the top seat starts first; kickoffs stay in file order
        self.assertEqual([a[2] for a in self.argvs("agent", "start")],
                         [TOP, "sample-dev", "sample-reviewer", "sample-designer"])
        self.assertEqual([a[2] for a in self.argvs("agent", "prompt")],
                         ["sample-dev", "sample-reviewer", "sample-designer", TOP])
        self.assertNotIn("run herdr-fleet tile", out)
        self.assertEqual(self.argvs("pane", "move"), [])

    def test_other_ratios(self):
        self.write_fleet(grid_fleet(TOP_LAST, TOP_LAYOUT + "top_ratio = 0.4\ncolumns = 3\n\n[watch]\nratio = 0.25\n"))
        code, out, err = self.run_cli("up")
        self.assertEqual(code, 0, out + err)
        root = self.read_state()["workspace_id"] + ":p1"
        self.assertEqual(self.argvs("pane", "split")[0][2:7], [root, "--direction", "down", "--ratio", "0.5333"])
        self.assertIn("then 3 seats in 3 columns (1+1+1)", out)
        cells = self.cells(root)
        self.assertEqual(cells[root], (0, 0, 200, 20))           # round(38 * 0.5333) = 20 rows: 40%
        self.assertEqual(cells[self.strip()], (0, 38, 200, 12))  # 25%
        for name in ("sample-dev", "sample-reviewer", "sample-designer"):
            x, y, w, h = cells[self.pane_of(name)]
            self.assertEqual((y, h), (20, 18), name)

    def test_without_the_strip_the_band_keeps_top_ratio(self):
        code, out, err = self.run_cli("up", "--no-watch")
        self.assertEqual(code, 0, out + err)
        root = self.read_state()["workspace_id"] + ":p1"
        self.assertEqual(self.argvs("pane", "split")[0][2:7], [root, "--direction", "down", "--ratio", "0.3"])
        cells = self.cells(root)
        self.assertEqual(cells[root], (0, 0, 200, 15))
        self.assertEqual(cells[self.pane_of("sample-designer")], (100, 15, 100, 35))

    def test_a_ratio_herdr_would_clamp_is_warned_about(self):
        self.write_fleet(grid_fleet(TOP_LAST, TOP_LAYOUT + "top_ratio = 0.6\n\n[watch]\nratio = 0.4\n"))
        code, out, err = self.run_cli("up")
        self.assertEqual(code, 0, out + err)
        self.assertIn("layout.top_ratio: the grid's first seat under the band would keep 1.0 of the band's pane, "
                      "outside herdr's 0.1-0.9; using 0.9", err)
        self.assertEqual(self.argvs("pane", "split")[0][5:7], ["--ratio", "0.9"])

    def test_dry_run(self):
        code, out, _ = self.run_cli("up", "--dry-run")
        self.assertEqual(code, 0, out)
        lines = out.splitlines()
        plans = [line for line in lines if line.startswith(("watcher: plan", "sample-"))]
        self.assertTrue(plans[0].startswith("watcher: plan - herdr plugin pane open"))
        self.assertIn("--target-pane '<root pane of sample>'", plans[0])
        self.assertTrue(plans[1].startswith("sample-dev: plan - herdr pane split '<root pane of sample>' "
                                            "--direction down --ratio 0.375"))
        self.assertTrue(plans[2].startswith("sample-designer: plan - herdr pane split '<pane of sample-dev>' "
                                            "--direction right --ratio 0.5"))
        self.assertTrue(plans[4].startswith("sample-orchestrator: plan - herdr agent start sample-orchestrator "
                                            "--kind claude --pane '<root pane of sample>'"))
        self.assertIn("layout: grid - top: sample-orchestrator, then 3 seats in 2 columns (2+1) in "
                      "<root pane of sample>", out)
        for prefix in (("workspace", "create"), ("pane", "split"), ("plugin", "pane"), ("agent", "start")):
            self.assertEqual(self.argvs(*prefix), [])

    def test_top_not_placed_in_this_run(self):
        code, out, err = self.run_cli("up", "sample-dev", "sample-reviewer")
        self.assertEqual(code, 0, out + err)
        root = self.read_state()["workspace_id"] + ":p1"
        self.assertEqual(self.pane_of("sample-dev"), root)
        self.assertIn("layout: top seat sample-orchestrator is not placed in this run; placed without the band", out)
        self.assertIn("layout: grid - 2 seats in 2 columns (1+1) in %s" % root, out)
        self.assertIsNone(self.pane_of(TOP))

    def test_status_and_doctor_count_the_band_like_any_seat(self):
        self.run_cli("up")
        code, out, _ = self.run_cli("status")
        self.assertEqual(code, 0, out)
        root = self.read_state()["workspace_id"] + ":p1"
        self.assertRegex(out, r"\nsample-orchestrator +claude +claude +%s +idle " % root)
        code, out, _ = self.run_cli("doctor")
        self.assertEqual(code, 0, out)
        self.assertIn("ok    fleet file", out)


class Me(BandCase):
    """26.2: ``--me`` in the caller's workspace (the fleet sets no workspace)."""

    fleet_text = top_fleet(NO_LABEL)

    def test_me_on_the_top_seat_makes_the_callers_pane_the_band(self):
        code, out, err = self.run_cli("up", "--me", TOP)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.pane_of(TOP), CALLER_PANE)
        strip = self.strip()
        self.assertEqual(self.argvs("plugin", "pane", "open")[0][10], CALLER_PANE)
        self.assertEqual(self.argvs("pane", "resize")[0][2:], ["--pane", CALLER_PANE, "--direction", "down",
                                                                "--amount", "0.3"])
        self.assertLess(self.index("pane", "resize"), self.index("pane", "split"))
        dev = self.pane_of("sample-dev")
        self.assertEqual([a[2:7] for a in self.argvs("pane", "split")], [
            [CALLER_PANE, "--direction", "down", "--ratio", "0.375"],   # no side share (14.4) with top
            [dev, "--direction", "right", "--ratio", "0.5"],
            [dev, "--direction", "down", "--ratio", "0.5"]])
        cells = self.cells(CALLER_PANE)
        self.assertEqual(cells[CALLER_PANE], (0, 0, 200, 15))
        self.assertEqual(cells[strip], (0, 40, 200, 10))
        self.assertEqual(cells[dev], (0, 15, 100, 12))
        self.assertEqual(cells[self.pane_of("sample-designer")], (100, 15, 100, 25))
        self.assertNotIn(CALLER_PANE, [a[a.index("--pane") + 1] for a in self.argvs("agent", "start")])
        self.assertEqual(self.prompts_to(TOP), [])
        self.assertEqual(self.layout_lines(out),
                         ["layout: grid - top: sample-orchestrator, then 3 seats in 2 columns (2+1) in %s"
                          % CALLER_PANE])

    def test_me_on_the_top_seat_inside_the_labelled_workspace(self):
        self.write_fleet(top_fleet(FOUR_SEATS))
        self.world["workspaces"]["w1"]["label"] = "sample"  # the owner's orchestrator runs in the fleet's workspace
        code, out, err = self.run_cli("up", "--me", TOP)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.argvs("workspace", "create"), [])
        self.assertEqual(self.argvs("plugin", "pane", "open")[0][10], CALLER_PANE)
        self.assertEqual(self.argvs("pane", "split")[0][2:7], [CALLER_PANE, "--direction", "down", "--ratio", "0.375"])
        self.assertEqual(self.cells(CALLER_PANE)[CALLER_PANE], (0, 0, 200, 15))
        self.assertNotIn("run herdr-fleet tile", out)

    def test_me_on_another_seat_keeps_14_4_and_says_top_was_not_applied(self):
        code, out, err = self.run_cli("up", "--me", "sample-dev")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.argvs("pane", "split")[0][2:7], [CALLER_PANE, "--direction", "right", "--ratio", "0.5"])
        self.assertIn("layout: top needs the fleet's own workspace or --me sample-orchestrator", out)
        self.assertIn("layout: grid - 3 seats in 2 columns (2+1)", out)
        self.assertNotIn("run herdr-fleet tile", out)

    def test_me_on_another_seat_with_a_workspace_up_creates_gets_the_band_there(self):
        self.write_fleet(top_fleet(FOUR_SEATS))
        code, out, err = self.run_cli("up", "--me", "sample-dev")
        self.assertEqual(code, 0, out + err)
        root = self.read_state()["workspace_id"] + ":p1"
        self.assertEqual(self.pane_of(TOP), root)
        self.assertIn("layout: grid - top: sample-orchestrator, then 2 seats in 2 columns (1+1) in %s" % root, out)
        self.assertNotIn("top needs", out)
        self.assertEqual(self.cells(root)[root], (0, 0, 200, 15))
        self.assertEqual(self.pane_of("sample-dev"), CALLER_PANE)  # you stay where you are (15, --join)

    def test_without_me_beside_the_caller_top_is_not_applied(self):
        code, out, err = self.run_cli("up")
        self.assertEqual(code, 0, out + err)
        self.assertIn("layout: top needs the fleet's own workspace or --me sample-orchestrator", out)
        self.assertIn("layout: grid - 4 seats in 2 columns (2+2)", out)

    def test_me_on_the_top_seat_without_an_agent(self):
        self.world["panes"][CALLER_PANE].update(agent=None, procs=[dict(fake_herdr.SHELL)])
        code, out, _ = self.run_cli("up", "--me", TOP)
        self.assertEqual(code, 3, out)
        self.assertIn("layout: top seat sample-orchestrator is not placed in this run; placed without the band", out)
        self.assertEqual(self.argvs("pane", "split")[0][2:5], [CALLER_PANE, "--direction", "right"])

    def test_me_on_the_top_seat_outside_the_fleets_workspace(self):
        self.write_fleet(top_fleet(FOUR_SEATS))
        root = self.fleet_ws()
        code, out, _ = self.run_cli("up", "--me", TOP)
        self.assertEqual(code, 0, out)
        self.assertIn("layout: top seat sample-orchestrator is not in the fleet's workspace; placed without the "
                      "band", out)
        self.assertEqual(self.argvs("pane", "split")[0][2:5], [root, "--direction", "right"])
        self.assertEqual(self.argvs("pane", "move"), [])

    def test_join_into_a_workspace_up_creates_then_tile(self):
        self.write_fleet(top_fleet(FOUR_SEATS))
        code, out, err = self.run_cli("up", "--me", TOP, "--join")
        self.assertEqual(code, 0, out + err)
        me = self.read_state()["agents"][TOP]["pane_id"]
        self.assertIn("joined - moved your pane", out)
        self.assertEqual(self.last_report_line(out), "layout: run herdr-fleet tile for the top band")
        code, out, err = self.run_cli("tile")
        self.assertEqual(code, 0, out + err)
        self.assertIn("sample-orchestrator: kept - anchors the layout in %s" % me, out)
        cells = self.cells(me)
        self.assertEqual(cells[me], (0, 0, 200, 15))
        self.assertEqual(cells[self.strip()], (0, 40, 200, 10))
        code, out, _ = self.run_cli("up")
        self.assertNotIn("run herdr-fleet tile", out)


class ExistingLayout(BandCase):
    """26.2: ``up`` on a layout already placed moves nothing; it says when to tile."""

    def test_a_band_is_left_alone_and_nothing_is_said(self):
        self.run_cli("up")
        before = len(self.calls())
        code, out, _ = self.run_cli("up")
        self.assertEqual(code, 0, out)
        calls = [c["argv"] for c in self.calls()[before:]]
        for argv in calls:
            self.assertNotIn(argv[:2], (["pane", "split"], ["pane", "move"], ["pane", "resize"], ["agent", "start"]))
        self.assertIn(["pane", "layout", "--pane", self.pane_of(TOP)], calls)
        self.assertNotIn("run herdr-fleet tile", out)

    def test_a_layout_without_the_band_gets_the_tile_line_last(self):
        self.write_fleet(grid_fleet(TOP_LAST))  # the plain grid of 14.2 first
        self.run_cli("up")
        self.write_fleet(top_fleet(TOP_LAST))
        before = len(self.calls())
        code, out, _ = self.run_cli("up")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.last_report_line(out), "layout: run herdr-fleet tile for the top band")
        for argv in [c["argv"] for c in self.calls()[before:]]:
            self.assertNotIn(argv[:2], (["pane", "split"], ["pane", "move"], ["pane", "resize"]))
        code, out, err = self.run_cli("tile")
        self.assertEqual(code, 0, out + err)
        code, out, _ = self.run_cli("up")
        self.assertNotIn("run herdr-fleet tile", out)  # tile made the band

    def test_a_strip_opened_later_goes_under_the_grid_not_the_band(self):
        self.run_cli("up", "--no-watch")  # the orchestrator is last in the file
        designer = self.pane_of("sample-designer")
        code, out, _ = self.run_cli("up")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.argvs("plugin", "pane", "open")[0][10], designer)
        self.assertIn("below %s" % designer, out)

    def test_an_absent_top_seat_says_nothing(self):
        self.write_fleet(grid_fleet(TOP_LAST))
        self.run_cli("up")
        top = self.pane_of(TOP)
        self.world["panes"][top].update(agent=None, procs=[dict(fake_herdr.SHELL)])
        self.write_fleet(top_fleet(TOP_LAST))
        code, out, _ = self.run_cli("up", "sample-dev")
        self.assertEqual(code, 0, out)
        self.assertNotIn("run herdr-fleet tile", out)
        self.assertEqual(self.layout_lines(out), [])


class Tile(BandCase):
    """26.2: ``tile`` anchors on the top seat: the strip, then the grid's first seat under it."""

    def setUp(self):
        super().setUp()
        self.fleet_ws("w2", "sample")
        # dev in the root pane, the orchestrator third, the strip under the designer: a mess
        for pane, kind, name in (("w2:p1", "claude", "sample-dev"), ("w2:p2", "codex", "sample-reviewer"),
                                 ("w2:p3", "claude", TOP), ("w2:p4", "claude", "sample-designer")):
            if pane not in self.world["panes"]:
                fake_herdr.add_pane(self.world, pane, "w2", self.repo, created=True)
            fake_herdr.put_agent(self.world, pane, kind, name, args=ARGS)
        for pane, source, direction in (("w2:p2", "w2:p1", "right"), ("w2:p3", "w2:p1", "down"),
                                        ("w2:p4", "w2:p2", "down")):
            self.world["panes"][pane]["split"] = {"from": source, "direction": direction, "ratio": "0.5"}
        fake_herdr.add_pane(self.world, "w2:p5", "w2", self.repo, created=True)
        self.world["panes"]["w2:p5"].update(plugin="watch",
                                            split={"from": "w2:p4", "direction": "down", "ratio": "0.5"})
        self.write_state({"version": 1, "workspace_id": "w2", "workspace_label": "sample", "agents": {},
                          "watch": {"pane_id": "w2:p5", "created_pane": True, "started": "2026-10-24T10:00:00+11:00"}})

    def moves(self):
        return self.argvs("pane", "move")

    def test_strip_then_the_grids_first_seat_then_the_rest(self):
        code, out, err = self.run_cli("tile")
        self.assertEqual(code, 0, out + err)
        park = ["--new-tab", "--workspace", "w2", "--label", "park", "--no-focus"]

        def back(pane, direction, source, ratio):
            return ["pane", "move", pane, "--tab", "w2:t1", "--split", direction, "--target-pane", source,
                    "--ratio", ratio, "--no-focus"]

        self.assertEqual(self.moves(), [
            ["pane", "move", "w2:p1"] + park, ["pane", "move", "w2:p4"] + park, ["pane", "move", "w2:p2"] + park,
            ["pane", "move", "w2:p5"] + park, back("w2:p5", "down", "w2:p3", "0.8"),   # the strip first
            back("w2:p1", "down", "w2:p3", "0.375"),                                  # the grid's first seat
            back("w2:p4", "right", "w2:p1", "0.5"), back("w2:p2", "down", "w2:p1", "0.5")])
        self.assertIn("layout: grid - top: sample-orchestrator, then 3 seats in 2 columns (2+1) in tab w2:t1", out)
        self.assertIn("sample-orchestrator: kept - anchors the layout in w2:p3", out)
        self.assertIn("watcher: moved - below sample-orchestrator (w2:p3), ratio 0.8", out)
        self.assertIn("sample-dev: moved - below sample-orchestrator (w2:p3), ratio 0.375", out)
        cells = self.cells("w2:p3")
        self.assertEqual(cells["w2:p3"], (0, 0, 200, 15))
        self.assertEqual(cells["w2:p5"], (0, 40, 200, 10))
        self.assertEqual(cells["w2:p1"], (0, 15, 100, 12))
        self.assertEqual(cells["w2:p2"], (0, 27, 100, 13))
        self.assertEqual(cells["w2:p4"], (100, 15, 100, 25))
        for argv in [c["argv"] for c in self.calls()]:
            self.assertNotIn(argv[:2], (["agent", "prompt"], ["agent", "start"], ["pane", "split"],
                                        ["pane", "close"], ["pane", "resize"]))

    def test_dry_run(self):
        code, out, _ = self.run_cli("tile", "--dry-run")
        self.assertEqual(code, 0, out)
        plans = [line for line in out.splitlines() if ": plan - " in line]
        self.assertEqual([line.split(":")[0] for line in plans],
                         ["watcher", "sample-dev", "sample-designer", "sample-reviewer"])
        self.assertIn("--target-pane w2:p3 --ratio 0.8", plans[0])
        self.assertTrue(plans[1].endswith("herdr pane move w2:p1 --tab w2:t1 --split down --target-pane w2:p3 "
                                          "--ratio 0.375 --no-focus"))
        self.assertEqual(self.moves(), [])

    def test_without_a_strip_the_band_keeps_top_ratio(self):
        self.write_state({"version": 1, "workspace_id": "w2", "workspace_label": "sample", "agents": {}})
        del self.world["panes"]["w2:p5"]
        code, out, err = self.run_cli("tile")
        self.assertEqual(code, 0, out + err)
        self.assertIn(["pane", "move", "w2:p1", "--tab", "w2:t1", "--split", "down", "--target-pane", "w2:p3",
                       "--ratio", "0.3", "--no-focus"], self.moves())
        cells = self.cells("w2:p3")
        self.assertEqual(cells["w2:p3"], (0, 0, 200, 15))
        self.assertEqual(cells["w2:p4"], (100, 15, 100, 35))

    def test_top_seat_not_live(self):
        self.world["panes"]["w2:p3"].update(agent=None, procs=[dict(fake_herdr.SHELL)])
        code, out, err = self.run_cli("tile")
        self.assertEqual(code, 0, out + err)
        self.assertIn("layout: top seat sample-orchestrator is not live; tiled without the band", out)
        self.assertIn("layout: grid - 3 seats in 2 columns (2+1) in tab w2:t1", out)
        self.assertIn("sample-dev: kept - anchors the layout in w2:p1", out)
        self.assertIn(["pane", "move", "w2:p5", "--tab", "w2:t1", "--split", "down", "--target-pane", "w2:p1",
                       "--ratio", "0.8", "--no-focus"], self.moves())

    def test_top_seat_in_another_workspace(self):
        fake_herdr.add_workspace(self.world, "w3", "other", self.repo)
        self.world["panes"]["w2:p3"].update(workspace_id="w3", tab_id="w3:t1")
        code, out, _ = self.run_cli("tile")
        self.assertEqual(code, 0, out)
        self.assertIn("sample-orchestrator: elsewhere - in workspace w3, not w2; left alone", out)
        self.assertIn("layout: top seat sample-orchestrator is in workspace w3; tiled without the band", out)

    def test_the_top_seat_alone(self):
        for pane in ("w2:p1", "w2:p2", "w2:p4"):
            self.world["panes"][pane].update(agent=None, procs=[dict(fake_herdr.SHELL)])
        code, out, _ = self.run_cli("tile")
        self.assertEqual(code, 0, out)
        self.assertIn("layout: grid - top: sample-orchestrator, then no other seats in tab w2:t1", out)
        self.assertEqual([m[2] for m in self.moves()], ["w2:p5", "w2:p5"])


class StatusAndDoctorUnchanged(GridCase):
    """26.2: the band's pane is a seat's pane like any other; ``status`` and ``doctor`` read
    the same with or without ``top``."""

    fleet_text = grid_fleet(THREE_SEATS)

    def setUp(self):
        super().setUp()
        self.fleet_ws("w2", "sample")
        for pane, kind, name in (("w2:p1", "claude", TOP), ("w2:p2", "claude", "sample-dev"),
                                 ("w2:p3", "codex", "sample-reviewer")):
            if pane not in self.world["panes"]:
                fake_herdr.add_pane(self.world, pane, "w2", self.repo, created=True)
            fake_herdr.put_agent(self.world, pane, kind, name, args=ARGS if kind == "claude" else ["-m", "gpt-6-astra"])

    def run_both(self, *argv):
        self.run_cli("status")  # names the seats' panes (22.1) once, so both runs below find them named
        results = []
        for text in (grid_fleet(THREE_SEATS), top_fleet(THREE_SEATS, "top_ratio = 0.4\n")):
            self.write_fleet(text)
            before = len(self.calls())
            code, out, err = self.run_cli(*argv)
            results.append((code, out, err, [c["argv"] for c in self.calls()[before:]]))
        return results

    def test_status(self):
        plain, band = self.run_both("status")
        self.assertEqual(plain, band)
        self.assertIn("sample-orchestrator", band[1])

    def test_status_json(self):
        plain, band = self.run_both("status", "--json")
        self.assertEqual(plain, band)

    def test_doctor(self):
        plain, band = self.run_both("doctor")
        self.assertEqual(plain, band)
        self.assertEqual(band[0], 0, band[1])


if __name__ == "__main__":
    unittest.main()
