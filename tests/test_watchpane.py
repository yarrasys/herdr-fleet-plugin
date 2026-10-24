"""The watcher strip (design 19, version 0.8.0): `up` starts `herdr-fleet watch` in a plugin
pane below the seats, records it as ``"watch"`` in the state file, sizes it with `pane
resize`, and skips it when a watcher runs, for `--no-watch` and for `watch = false`;
`status` reports it, `tile` moves it under the anchor before the seats come back, `down
--close-panes` closes it after the seats, and a strip that is gone is pruned. Geometry is
read from the fake's `pane layout` replay (herdr 0.9.5's rounding and resize rule)."""

from __future__ import annotations

import json
import os
import re
import unittest

import fake_herdr
from support import ARGS, CALLER_PANE, FleetCase, GridCase, LiveFleet, THREE_SEATS, grid_fleet
from herdr_fleet import inbox
from herdr_fleet.fleetfile import FleetError, load_fleet

NO_LABEL = THREE_SEATS.replace('workspace = "fablyn"\n', "")


def with_top(text, line):
    """Put a top-level line (or table) before the first [[agents]]."""
    head, rest = text.split("[[agents]]", 1)
    return head + line + "\n\n[[agents]]" + rest


class StripCase(GridCase):
    def strip_argv(self, target, cwd=None):
        return ["plugin", "pane", "open", "--plugin", "yarrasys.fleet", "--entrypoint", "watch", "--placement",
                "split", "--target-pane", target, "--direction", "down", "--cwd", cwd or self.repo,
                "--env", "HERDR_FLEET_FILE=%s" % os.path.join(self.repo, ".herdr", "fleet.toml"), "--no-focus"]

    def strip(self):
        return (self.read_state().get("watch") or {}).get("pane_id")

    def geometry(self, pane):
        return fake_herdr.geometry(self.world, self.world["panes"][pane]["tab_id"])[0]

    def index(self, *prefix):
        """Position of the first call starting with ``prefix`` in the log."""
        calls = [c["argv"] for c in self.calls()]
        return next(i for i, argv in enumerate(calls) if argv[:len(prefix)] == list(prefix))

    def write_lock(self, pid=None):
        with open(os.path.join(self.repo, ".herdr", "watch.lock"), "w") as handle:
            json.dump({"pid": pid or os.getpid(), "started": inbox.iso_local(inbox.now_utc()), "fleet": "x"}, handle)


class FreshWorkspace(StripCase):
    """`up` creates the workspace: the strip is split from the root pane before any seat."""

    def test_strip_from_the_root_before_the_seats_then_resized(self):
        code, out, err = self.run_cli("up")
        self.assertEqual(code, 0, out + err)
        root = self.read_state()["workspace_id"] + ":p1"
        strip = self.strip()
        self.assertEqual(self.argvs("plugin", "pane", "open"), [self.strip_argv(root)])
        self.assertLess(self.index("plugin", "pane", "open"), self.index("pane", "split"))
        self.assertLess(self.index("pane", "resize"), self.index("pane", "split"))
        # herdr split 50/50; the pane above grows by 0.3 so the strip keeps 0.2 (design 25)
        self.assertEqual(self.argvs("pane", "resize"),
                         [["pane", "resize", "--pane", root, "--direction", "down", "--amount", "0.3"]])
        self.assertIn("watcher: started - herdr-fleet watch in pane %s below %s, 20%% of the area" % (strip, root),
                      out)
        cells = self.geometry(root)
        self.assertEqual(cells[strip], (0, 40, 200, 10))  # the full width, 10 of 50 rows
        for name in ("fablyn-orchestrator", "fablyn-dev", "fablyn-reviewer"):
            x, y, w, h = cells[self.pane_of(name)]
            self.assertLessEqual(y + h, 40, name)  # every seat sits above the strip
        entry = self.read_state()["watch"]
        self.assertEqual((entry["pane_id"], entry["created_pane"]), (strip, True))
        self.assertTrue(entry["started"])
        self.assertNotIn(strip, [a.get("pane_id") for a in self.read_state()["agents"].values()])
        self.assertEqual(self.world["panes"][strip]["env"],
                         {"HERDR_FLEET_FILE": os.path.join(self.repo, ".herdr", "fleet.toml")})
        self.assertEqual(code, 0)

    def test_grid_is_built_inside_the_area_above_the_strip(self):
        self.write_fleet(grid_fleet())
        code, out, err = self.run_cli("up")
        self.assertEqual(code, 0, out + err)
        root = self.read_state()["workspace_id"] + ":p1"
        self.assertLess(self.index("pane", "resize"), self.index("pane", "split"))
        cells = self.geometry(root)
        self.assertEqual(cells[self.strip()], (0, 40, 200, 10))
        self.assertEqual(cells[root], (0, 0, 100, 20))  # orchestrator: top left quarter of the area above
        self.assertEqual(cells[self.pane_of("fablyn-designer")], (100, 20, 100, 20))

    def test_status_line_and_json(self):
        self.run_cli("up")
        strip = self.strip()
        code, out, _ = self.run_cli("status")
        self.assertIn("\nwatcher: starting (pane %s)\n" % strip, out)  # the pane runs it; no lock yet
        self.write_lock()
        out = self.run_cli("status")[1]
        self.assertRegex(out, r"\nwatcher: running \(pid %d, pane %s, since \d\d:\d\d\)\n"
                         % (os.getpid(), re.escape(strip)))
        data = json.loads(self.run_cli("status", "--json")[1])
        self.assertEqual((data["watcher"]["running"], data["watcher"]["pane"], data["watcher"]["pane_state"]),
                         (True, strip, "watcher"))
        self.world["panes"][strip]["procs"] = [dict(fake_herdr.SHELL)]  # the watcher exited; a shell is left
        os.remove(os.path.join(self.repo, ".herdr", "watch.lock"))
        out = self.run_cli("status")[1]
        self.assertIn("\nwatcher: not running (watcher pane %s idle)\n" % strip, out)
        self.assertNotIn(strip, out.split("\nwatcher:")[0])  # never a row of the seat table
        self.assertEqual([s["name"] for s in json.loads(self.run_cli("status", "--json")[1])["seats"]],
                         ["fablyn-orchestrator", "fablyn-dev", "fablyn-reviewer"])

    def test_second_up_opens_no_second_strip(self):
        self.run_cli("up")
        strip = self.strip()
        code, out, _ = self.run_cli("up")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.argvs("plugin", "pane", "open")), 1)
        self.assertIn("watcher: starting - in pane %s" % strip, out)
        self.write_lock()
        code, out, _ = self.run_cli("up")
        self.assertIn("watcher: already running (pid %d)\n" % os.getpid(), out)
        self.assertEqual(len(self.argvs("plugin", "pane", "open")), 1)

    def test_idle_recorded_strip_needs_attention(self):
        self.run_cli("up")
        strip = self.strip()
        self.world["panes"][strip]["procs"] = [dict(fake_herdr.SHELL)]
        code, out, _ = self.run_cli("up")
        self.assertEqual(code, 3, out)
        self.assertIn("watcher: not running - its pane %s is idle; run `herdr-fleet watch` there" % strip, out)
        self.assertEqual(len(self.argvs("plugin", "pane", "open")), 1)

    def test_an_error_inspecting_the_strip_opens_no_second_one(self):
        self.run_cli("up")
        strip = self.strip()
        self.world["canned"] = [{"prefix": ["pane", "process-info", "--pane", strip], "exit": 1,
                                 "stderr": {"error": {"code": "server_busy", "message": "try later"}}}]
        code, out, _ = self.run_cli("up")
        self.assertEqual(code, 3, out)
        self.assertIn("watcher: not started - cannot inspect its pane %s (herdr error server_busy: try later)"
                      % strip, out)
        self.assertEqual(len(self.argvs("plugin", "pane", "open")), 1)
        self.assertEqual(self.strip(), strip)

    def test_dry_run_prints_the_plan_and_opens_nothing(self):
        code, out, _ = self.run_cli("up", "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("watcher: plan - herdr plugin pane open --plugin yarrasys.fleet --entrypoint watch --placement "
                      "split --target-pane '<root pane of fablyn>' --direction down --cwd %s --env "
                      "HERDR_FLEET_FILE=%s --no-focus, then herdr pane resize --pane '<root pane of fablyn>' "
                      "--direction down --amount 0.3 (the strip gets 20%% of <root pane of fablyn>)"
                      % (self.repo, os.path.join(self.repo, ".herdr", "fleet.toml")), out)
        for prefix in (("plugin", "pane"), ("pane", "resize"), ("pane", "layout")):
            self.assertEqual(self.argvs(*prefix), [])
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".herdr", "fleet.state.json")))

    def test_ratio_from_the_watch_table(self):
        self.write_fleet(with_top(THREE_SEATS, '[watch]\nratio = 0.25\nposition = "bottom"'))
        code, out, _ = self.run_cli("up")
        self.assertEqual(code, 0, out)
        root = self.read_state()["workspace_id"] + ":p1"
        self.assertEqual(self.argvs("pane", "resize")[0][-2:], ["--amount", "0.25"])
        self.assertIn("25% of the area", out)
        self.assertEqual(self.geometry(root)[self.strip()][3], 12)  # round(50 * 0.75) = 38 above, 12 below

    def test_a_failed_resize_is_a_warning(self):
        self.world["canned"] = [{"prefix": ["pane", "resize"], "exit": 1,
                                 "stderr": {"error": {"code": "pane_not_found", "message": "pane not found"}}}]
        code, out, _ = self.run_cli("up")
        self.assertEqual(code, 0, out)
        self.assertIn("watcher: warning - pane resize: herdr error pane_not_found: pane not found; the strip keeps "
                      "herdr's 50%", out)
        self.assertIn("50% of the area (not resized)", out)

    def test_an_unreadable_layout_assumes_fifty_fifty(self):
        self.world["canned"] = [{"prefix": ["pane", "layout"], "stdout": {"id": "cli:pane:layout", "result": {
            "type": "pane_layout", "layout": {}}}},
                                {"prefix": ["pane", "resize"], "stdout": {"id": "cli:pane:resize", "result": {
                                    "type": "pane_resize", "resize": {"changed": True}}}}]
        code, out, _ = self.run_cli("up")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.argvs("pane", "resize")[0][-2:], ["--amount", "0.3"])
        self.assertIn("about 20% of the area (pane layout unreadable; assumed herdr's 50/50 split)", out)

    def test_focus_is_reported_when_herdr_moves_it(self):
        pane = {"pane_id": "w101:p102", "focused": True}
        self.world["canned"] = [{"prefix": ["plugin", "pane", "open"], "stdout": {"id": "cli:plugin", "result": {
            "type": "plugin_pane_opened", "plugin_pane": {"entrypoint": "watch", "pane": pane}}}}]
        code, out, _ = self.run_cli("up", "--no-kickoff")
        self.assertIn("herdr moved the focus to it", out)

    def test_a_failed_open_is_reported_and_the_seats_come_up(self):
        self.world["canned"] = [{"prefix": ["plugin", "pane", "open"], "exit": 1,
                                 "stderr": {"error": {"code": "plugin_not_found", "message": "not installed"}}}]
        code, out, _ = self.run_cli("up")
        self.assertEqual(code, 3, out)
        self.assertIn("watcher: failed - plugin pane open: herdr error plugin_not_found: not installed; start one by "
                      "hand with `herdr-fleet watch`", out)
        self.assertEqual(len(self.prompts_to("fablyn-dev")), 1)
        self.assertNotIn("watch", self.read_state())


class Skips(StripCase):
    def test_no_watch_flag(self):
        code, out, _ = self.run_cli("up", "--no-watch")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.argvs("plugin", "pane", "open"), [])
        self.assertNotIn("watch", self.read_state())
        self.assertNotIn("watcher", out)

    def test_watch_false_in_the_fleet_file(self):
        self.write_fleet(with_top(THREE_SEATS, "watch = false"))
        code, out, _ = self.run_cli("up")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.argvs("plugin", "pane", "open"), [])
        self.assertEqual(self.argvs("pane", "resize"), [])

    def test_live_lock(self):
        self.write_lock()
        code, out, _ = self.run_cli("up")
        self.assertEqual(code, 0, out)
        self.assertIn("watcher: already running (pid %d)" % os.getpid(), out)
        self.assertEqual(self.argvs("plugin", "pane", "open"), [])

    def test_nothing_to_bring_up_and_none_live(self):
        self.write_fleet(THREE_SEATS.replace('kind = "codex"', 'kind = "claude"'))
        self.fleet_ws()
        # every recorded pane hosts an unnamed agent of another kind: every seat conflicts,
        # nothing is placed and nothing is live
        for pane, name in (("w2:p7", "fablyn-orchestrator"), ("w2:p8", "fablyn-dev"), ("w2:p9", "fablyn-reviewer")):
            fake_herdr.add_pane(self.world, pane, "w2", self.repo)
            fake_herdr.put_agent(self.world, pane, "codex")
        self.write_state({"version": 1, "workspace_id": "w2", "agents": {
            "fablyn-orchestrator": {"pane_id": "w2:p7"}, "fablyn-dev": {"pane_id": "w2:p8"},
            "fablyn-reviewer": {"pane_id": "w2:p9"}}})
        code, out, _ = self.run_cli("up")
        self.assertIn("watcher: not started - no seat is up", out)
        self.assertEqual(self.argvs("plugin", "pane", "open"), [])


class BesideTheCaller(StripCase):
    """No workspace label: the layout splits off the caller's pane, and the strip is split
    from the first pane of that layout, never from the caller's own pane."""

    fleet_text = NO_LABEL

    def test_chain_strip_under_the_first_seat_before_the_rest(self):
        code, out, err = self.run_cli("up")
        self.assertEqual(code, 0, out + err)
        first = self.pane_of("fablyn-orchestrator")
        self.assertEqual(self.argvs("plugin", "pane", "open"), [self.strip_argv(first)])
        splits = [c["argv"] for c in self.calls() if c["argv"][:2] in (["pane", "split"], ["plugin", "pane"])]
        self.assertEqual([a[:3] for a in splits],
                         [["pane", "split", CALLER_PANE], ["plugin", "pane", "open"],
                          ["pane", "split", first], ["pane", "split", self.pane_of("fablyn-dev")]])

    def test_grid_strip_under_the_grid_area(self):
        self.write_fleet(grid_fleet(NO_LABEL, '[layout]\nmode = "grid"\nratio = 0.4\n'))
        code, out, err = self.run_cli("up", "--me", "fablyn-orchestrator")
        self.assertEqual(code, 0, out + err)
        area = self.pane_of("fablyn-dev")
        self.assertEqual(self.argvs("plugin", "pane", "open"), [self.strip_argv(area)])
        cells = self.geometry(CALLER_PANE)
        strip = cells[self.strip()]
        self.assertEqual((strip[0], strip[2]), (80, 120))  # spans the grid area, not the caller's 40%
        self.assertEqual(cells[CALLER_PANE], (0, 0, 80, 50))


class ExistingLayout(StripCase):
    """Seats already placed: the strip opens under the last seat's pane, after the kickoffs."""

    def test_under_the_last_seat_after_the_kickoffs(self):
        self.run_cli("up", "--no-watch")
        reviewer, dev = self.pane_of("fablyn-reviewer"), self.pane_of("fablyn-dev")
        self.world["panes"][dev].update(agent=None, procs=[dict(fake_herdr.SHELL)])  # dev exited: up starts it again
        before = len(self.calls())
        code, out, err = self.run_cli("up")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.argvs("plugin", "pane", "open"), [self.strip_argv(reviewer)])
        calls = [c["argv"] for c in self.calls()[before:]]
        opened = next(i for i, a in enumerate(calls) if a[:3] == ["plugin", "pane", "open"])
        prompts = [i for i, a in enumerate(calls) if a[:2] == ["agent", "prompt"]]
        self.assertTrue(prompts)
        self.assertLess(max(prompts), opened)  # the strip came after the kickoff
        self.assertEqual(self.argvs("pane", "resize")[0][2:], ["--pane", reviewer, "--direction", "down",
                                                                "--amount", "0.3"])
        cells = self.geometry(reviewer)
        x, y, w, h = cells[self.strip()]
        rx, ry, rw, rh = cells[reviewer]
        self.assertEqual((x, w, y), (rx, rw, ry + rh))  # right under the reviewer, as wide as its cell

    def test_live_seats_without_a_lock(self):
        self.run_cli("up", "--no-watch")
        code, out, _ = self.run_cli("up")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.argvs("plugin", "pane", "open")[0][10], self.pane_of("fablyn-reviewer"))
        self.assertIn("watcher: started", out)

    def test_live_seats_with_a_live_lock(self):
        self.run_cli("up", "--no-watch")
        self.write_lock()
        code, out, _ = self.run_cli("up")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.argvs("plugin", "pane", "open"), [])

    def test_the_strip_is_never_the_anchor(self):
        root = self.fleet_ws()
        self.world["panes"][root].update(plugin="watch", created=True,
                                         procs=[dict(fake_herdr.SHELL)])
        fake_herdr.add_pane(self.world, "w2:p2", "w2", self.repo)
        self.write_state({"version": 1, "workspace_id": "w2", "workspace_label": "fablyn", "agents": {},
                          "watch": {"pane_id": root, "created_pane": True, "started": "2026-10-21T10:00:00+11:00"}})
        code, out, _ = self.run_cli("up", "--no-watch")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.argvs("pane", "split")[0][2], "w2:p2")


class Tile(StripCase):
    fleet_text = grid_fleet()

    def setUp(self):
        super().setUp()
        self.run_cli("up")
        self.ws = self.read_state()["workspace_id"]
        self.root = self.ws + ":p1"

    def test_strip_moves_under_the_anchor_first_and_seat_order_is_kept(self):
        strip = self.strip()
        before = len(self.calls())
        code, out, err = self.run_cli("tile")
        self.assertEqual(code, 0, out + err)
        moves = [c["argv"] for c in self.calls()[before:] if c["argv"][:2] == ["pane", "move"]]
        parks = [m for m in moves if "--new-tab" in m]
        backs = [m for m in moves if "--tab" in m]
        self.assertEqual(parks[-1][2], strip)  # parked after the seats
        self.assertEqual(backs[0], ["pane", "move", strip, "--tab", self.ws + ":t1", "--split", "down",
                                    "--target-pane", self.root, "--ratio", "0.8", "--no-focus"])
        self.assertEqual([m[2] for m in backs[1:]], [self.pane_of(n) for n in
                                                     ("fablyn-reviewer", "fablyn-dev", "fablyn-designer")])
        self.assertIn("watcher: moved - below fablyn-orchestrator (%s), ratio 0.8" % self.root, out)
        cells = self.geometry(self.root)
        self.assertEqual(cells[strip], (0, 40, 200, 10))
        self.assertEqual(cells[self.root], (0, 0, 100, 20))
        self.assertEqual(cells[self.pane_of("fablyn-designer")], (100, 20, 100, 20))
        self.assertEqual(self.read_state()["watch"]["pane_id"], strip)

    def test_dry_run(self):
        strip = self.strip()
        code, out, _ = self.run_cli("tile", "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("watcher: plan - herdr pane move %s --new-tab --workspace %s --label park --no-focus, then "
                      "herdr pane move %s --tab %s:t1 --split down --target-pane %s --ratio 0.8 --no-focus "
                      "(below fablyn-orchestrator, before the seats come back)" % (strip, self.ws, strip, self.ws,
                                                                                  self.root), out)

    def test_strip_in_another_tab_is_left_alone(self):
        strip = self.strip()
        self.world["panes"][strip]["tab_id"] = self.ws + ":t9"
        before = len(self.calls())
        code, out, _ = self.run_cli("tile")
        self.assertEqual(code, 0, out)
        self.assertIn("watcher: elsewhere - pane %s is in tab %s:t9 of workspace %s, not tab %s:t1; left alone"
                      % (strip, self.ws, self.ws, self.ws), out)
        self.assertNotIn(strip, [c["argv"][2] for c in self.calls()[before:] if c["argv"][:2] == ["pane", "move"]])

    def test_a_failed_strip_move_does_not_stop_the_seats(self):
        strip = self.strip()
        self.world["canned"] = [{"prefix": ["pane", "move", strip, "--tab"], "exit": 1,
                                 "stderr": {"error": {"code": "pane_move_failed", "message": "busy"}}}]
        code, out, _ = self.run_cli("tile")
        self.assertEqual(code, 3, out)
        self.assertIn("watcher: failed - moving back: herdr error pane_move_failed: busy; it is parked in a tab of "
                      "its own", out)
        self.assertEqual(out.count(": moved - "), 3)


class Down(StripCase):
    def setUp(self):
        super().setUp()
        self.run_cli("up")
        self.strip_pane = self.strip()

    def test_close_panes_closes_the_strip_after_the_seats(self):
        before = len(self.calls())
        code, out, err = self.run_cli("down", "--close-panes")
        self.assertEqual(code, 0, out + err)
        closes = [c["argv"][2] for c in self.calls()[before:] if c["argv"][:2] == ["pane", "close"]]
        self.assertEqual(closes[-1], self.strip_pane)
        self.assertEqual(len(closes), 3)  # dev, reviewer (created), then the strip; the root stays
        self.assertIn("watcher: pane closed - %s (the watcher stopped)" % self.strip_pane, out)
        self.assertNotIn("watch", self.read_state())
        self.assertNotIn(self.strip_pane, self.world["panes"])

    def test_without_close_panes_the_strip_stays(self):
        code, out, _ = self.run_cli("down")
        self.assertEqual(code, 0, out)
        self.assertIn(self.strip_pane, self.world["panes"])
        self.assertEqual(self.read_state()["watch"]["pane_id"], self.strip_pane)

    def test_down_names_never_touches_it(self):
        code, out, _ = self.run_cli("down", "fablyn-dev", "--close-panes")
        self.assertEqual(code, 0, out)
        self.assertIn(self.strip_pane, self.world["panes"])

    def test_an_idle_strip_is_closed(self):
        self.world["panes"][self.strip_pane]["procs"] = [dict(fake_herdr.SHELL)]
        code, out, _ = self.run_cli("down", "--close-panes")
        self.assertEqual(code, 0, out)
        self.assertIn("watcher: pane closed - %s\n" % self.strip_pane, out)

    def test_a_strip_running_something_else_is_kept(self):
        self.world["panes"][self.strip_pane]["procs"] = [{"name": "vim", "argv0": "vim", "argv": ["vim"], "pid": 9}]
        code, out, _ = self.run_cli("down", "--close-panes")
        self.assertEqual(code, 3, out)
        self.assertIn("watcher: pane kept - %s runs vim, not the watcher" % self.strip_pane, out)
        self.assertIn(self.strip_pane, self.world["panes"])

    def test_a_strip_it_did_not_create_is_never_closed(self):
        state = self.read_state()
        state["watch"]["created_pane"] = False
        self.write_state(state)
        self.world["panes"][self.strip_pane]["created"] = False
        code, out, _ = self.run_cli("down", "--close-panes")
        self.assertIn(self.strip_pane, self.world["panes"])
        self.assertNotIn(self.strip_pane, [c["argv"][2] for c in self.calls() if c["argv"][:2] == ["pane", "close"]])

    def test_restart_and_swap_ignore_it(self):
        self.write_fleet(THREE_SEATS.replace('args = ["-m", "gpt-6-astra"]', 'args = ["-m", "gpt-7"]'))
        code, out, _ = self.run_cli("swap")
        self.assertEqual(code, 0, out)
        code, out, _ = self.run_cli("restart", "fablyn-dev")
        self.assertEqual(code, 0, out)
        self.assertNotIn("watcher", out)
        self.assertEqual(len(self.argvs("plugin", "pane", "open")), 1)
        self.assertEqual(self.read_state()["watch"]["pane_id"], self.strip_pane)


class Pruning(StripCase):
    def test_a_strip_that_is_gone_is_dropped(self):
        self.run_cli("up")
        strip = self.strip()
        recorded = self.read_state()
        del self.world["panes"][strip]  # closed by hand
        self.run_cli("status")  # status prunes silently
        self.assertNotIn("watch", self.read_state())
        self.write_state(recorded)
        code, out, _ = self.run_cli("up")  # up says so, then opens a new strip
        self.assertEqual(code, 0, out)
        self.assertIn("state: dropped the watcher pane %s (no longer exists)" % strip, out)
        self.assertEqual(len(self.argvs("plugin", "pane", "open")), 2)
        self.assertNotEqual(self.strip(), strip)


class FleetFile(FleetCase):
    def load(self, line):
        return load_fleet(self.write_fleet(with_top(THREE_SEATS, line)))

    def test_defaults(self):
        fleet = load_fleet(self.write_fleet(THREE_SEATS))
        self.assertEqual((fleet.watch, fleet.watch_ratio, fleet.watch_position), (True, 0.2, "bottom"))

    def test_off_and_table(self):
        self.assertFalse(self.load("watch = false").watch)
        fleet = self.load('[watch]\nratio = 0.2')
        self.assertEqual((fleet.watch, fleet.watch_ratio), (True, 0.2))

    def test_ratio_is_clamped_with_a_warning(self):
        fleet = self.load('[watch]\nratio = 0.05')
        self.assertEqual(fleet.watch_ratio, 0.1)
        self.assertIn("watch.ratio: 0.05 is outside 0.1-0.4; using 0.1", fleet.warnings)
        self.assertEqual(self.load('[watch]\nratio = 0.6').watch_ratio, 0.4)

    def test_errors(self):
        for line, message in (('[watch]\nposition = "top"', "watch.position: 'top' is not supported (only \"bottom\")"),
                              ('[watch]\nratio = 1.5', "watch.ratio: 1.5 must be a number between 0 and 1"),
                              ('watch = "yes"', "watch: must be true, false or a [watch] table")):
            with self.assertRaises(FleetError) as caught:
                self.load(line)
            self.assertIn(message, "\n".join(caught.exception.errors))

    def test_unknown_key_warns(self):
        self.assertIn("[watch]: unknown key 'height' ignored", self.load("[watch]\nheight = 3").warnings)


class StatusWithoutStrip(LiveFleet):
    def test_a_held_lock_shows_the_line_even_with_nothing_queued(self):
        with open(os.path.join(self.repo, ".herdr", "watch.lock"), "w") as handle:
            json.dump({"pid": os.getpid(), "started": inbox.iso_local(inbox.now_utc()), "fleet": "x"}, handle)
        out = self.run_cli("status")[1]
        self.assertRegex(out, r"\nwatcher: running \(pid %d since \d\d:\d\d\)\n" % os.getpid())
        self.assertNotIn("queued:", out)
        self.assertNotIn("pane", json.loads(self.run_cli("status", "--json")[1])["watcher"])


class SafetyHookKnowsTheStrip(LiveFleet):
    def check_fails(self, command, entries):
        with open(self.log_path, "a") as handle:
            handle.write(json.dumps({"command": command}) + "\n")
            for argv, info in entries:
                handle.write(json.dumps({"argv": argv, "exit": 0, "info": info}) + "\n")
        with self.assertRaises(AssertionError):
            self.assert_safety()
        os.remove(self.log_path)

    def test_only_up_and_ui_open_plugin_panes(self):
        self.check_fails(["restart", "x"], [(["plugin", "pane", "open", "--entrypoint", "watch"], {})])
        self.check_fails(["tile"], [(["pane", "resize", "--pane", "w2:p1", "--direction", "down"], {})])

    def test_the_strip_is_never_prompted_or_started_into(self):
        opened = (["plugin", "pane", "open", "--entrypoint", "watch"], {"plugin_pane": "w2:p9"})
        self.check_fails(["up"], [opened, (["agent", "prompt", "w2:p9", "x"], {"target_status": "idle"})])
        self.check_fails(["up"], [opened, (["agent", "start", "x", "--kind", "claude", "--pane", "w2:p9"],
                                           {"at_shell": True})])


if __name__ == "__main__":
    unittest.main()
