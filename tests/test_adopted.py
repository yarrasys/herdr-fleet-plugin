"""Adopted seats and ``up --join`` (design section 15): the ``adopted`` flag,
status, the bare-swap skip, the cross-workspace warning and the pane move."""

from __future__ import annotations

import json
import unittest

import fake_herdr
from support import ARGS, CALLER_PANE, FleetCase, LiveFleet, THREE_SEATS

NO_LABEL = THREE_SEATS.replace('workspace = "fablyn"\n', "")
MOVE = ["pane", "move", CALLER_PANE, "--tab", "w2:t1", "--split", "right", "--ratio", "0.5", "--no-focus"]


class AdoptedFlag(FleetCase):
    fleet_text = NO_LABEL

    def test_me_records_adopted_and_status_shows_it_instead_of_args(self):
        # the caller runs plain `claude`; the file says --model claude-opus-5-5
        code, out, err = self.run_cli("up", "--me", "fablyn-orchestrator")
        self.assertEqual(code, 0, out + err)
        self.assertTrue(self.read_state()["agents"]["fablyn-orchestrator"]["adopted"])
        self.assertFalse(self.read_state()["agents"]["fablyn-dev"]["adopted"])
        code, out, _ = self.run_cli("status", "--json")
        seat = json.loads(out)["seats"][0]
        self.assertEqual((seat["name"], seat["adopted"], seat["drift"]), ("fablyn-orchestrator", True, []))
        self.assertFalse(json.loads(out)["seats"][1]["adopted"])
        code, out, _ = self.run_cli("status")
        line = [l for l in out.splitlines() if l.startswith("fablyn-orchestrator ")][0]
        self.assertIn("adopted", line)
        self.assertNotIn("args:", line)

    def test_adopt_on_resume_records_adopted_and_a_plugin_start_clears_it(self):
        fake_herdr.add_pane(self.world, "w1:p7", "w1", self.repo)
        fake_herdr.put_agent(self.world, "w1:p7", "claude", name=None, args=["--chrome"])
        self.write_state({"version": 1, "workspace_id": "w1", "agents": {"fablyn-dev": {"pane_id": "w1:p7"}}})
        code, out, _ = self.run_cli("up")
        self.assertEqual(code, 0, out)
        self.assertIn("fablyn-dev: adopted", out)
        self.assertTrue(self.read_state()["agents"]["fablyn-dev"]["adopted"])
        code, out, _ = self.run_cli("restart", "fablyn-dev")
        self.assertEqual(code, 0, out)
        entry = self.read_state()["agents"]["fablyn-dev"]
        self.assertFalse(entry["adopted"])
        self.assertEqual(entry["args"], ARGS)


class SwapAndAdopted(LiveFleet):
    def setUp(self):
        super().setUp()
        self.world["panes"]["w2:p2"]["procs"][0]["argv"] = ["claude", "--chrome"]  # hand-started session
        state = self.read_state()
        state["agents"]["fablyn-dev"]["adopted"] = True
        del state["agents"]["fablyn-dev"]["args"]
        self.write_state(state)

    def test_bare_swap_skips_adopted_seats(self):
        code, out, _ = self.run_cli("swap")
        self.assertEqual(code, 0, out)
        self.assertIn("fablyn-dev: skipped - adopted session; name it explicitly to swap it", out)
        self.assertEqual(self.argvs("agent", "prompt"), [])
        self.assertEqual(self.argvs("agent", "start"), [])

    def test_swap_name_acts_on_an_adopted_seat(self):
        code, out, _ = self.run_cli("swap", "fablyn-dev")
        self.assertEqual(code, 0, out)
        self.assertIn("fablyn-dev: drifted - args: running --chrome, file says --model claude-opus-5-5", out)
        self.assertEqual(self.prompts_to("fablyn-dev")[0], "/exit")
        self.assertFalse(self.read_state()["agents"]["fablyn-dev"]["adopted"])

    def test_named_restart_still_follows_the_state_rules(self):
        self.status_of("w2:p2", "blocked")
        code, out, _ = self.run_cli("restart", "fablyn-dev")
        self.assertEqual(code, 3)
        self.assertIn("agent is blocked", out)
        self.assertEqual(self.prompts_to("fablyn-dev"), [])
        code, out, _ = self.run_cli("swap", "fablyn-dev")
        self.assertEqual(code, 3)
        self.assertEqual(self.prompts_to("fablyn-dev"), [])


class AdoptedInference(LiveFleet):
    """Design 15 amendment: no recorded args means herdr-fleet never started the seat;
    pane ids are not compared, and refreshing a seat's pane keeps its fields."""

    def test_missing_args_means_adopted(self):
        self.world["panes"]["w2:p2"]["procs"][0]["argv"] = ["claude", "--chrome"]
        state = self.read_state()
        del state["agents"]["fablyn-dev"]["args"]  # adopted by an older version: no `adopted` key either
        self.write_state(state)
        code, out, _ = self.run_cli("swap")
        self.assertEqual(code, 0, out)
        self.assertIn("fablyn-dev: skipped - adopted session; name it explicitly to swap it", out)
        self.assertEqual(self.prompts_to("fablyn-dev"), [])
        seat = json.loads(self.run_cli("status", "--json")[1])["seats"][1]
        self.assertEqual((seat["adopted"], seat["drift"]), (True, []))

    def test_started_seats_are_not_adopted(self):
        seats = json.loads(self.run_cli("status", "--json")[1])["seats"]
        self.assertEqual([s["adopted"] for s in seats], [False, False, False])

    def test_adopted_survives_a_pane_id_refresh(self):
        # fablyn-dev now runs in w2:p9 while its recorded pane w2:p2 is still there
        state = self.read_state()
        state["agents"]["fablyn-dev"].update(adopted=True, last_kickoff_at="2026-10-19T20:05:40+11:00")
        self.write_state(state)
        self.world["panes"]["w2:p2"].update(agent=None, procs=[dict(fake_herdr.SHELL)])
        fake_herdr.add_pane(self.world, "w2:p9", "w2", self.repo)
        fake_herdr.put_agent(self.world, "w2:p9", "claude", "fablyn-dev", args=ARGS)
        code, out, _ = self.run_cli("up")
        self.assertEqual(code, 0, out)
        entry = self.read_state()["agents"]["fablyn-dev"]
        self.assertEqual(entry["pane_id"], "w2:p9")
        self.assertTrue(entry["adopted"])
        self.assertEqual((entry["args"], entry["last_kickoff_at"]), (ARGS, "2026-10-19T20:05:40+11:00"))
        self.assertFalse(entry["created_pane"])  # w2:p9 is not a pane herdr-fleet created

    def test_hand_moved_adopted_seat_is_followed_and_skipped_by_bare_swap(self):
        # the owner moved the adopted session to another workspace: its pane id changed
        state = self.read_state()
        state["agents"]["fablyn-dev"].update(adopted=True)
        self.write_state(state)
        fake_herdr.add_workspace(self.world, "w3", "elsewhere", self.repo)
        fake_herdr.add_pane(self.world, "w3:p5", "w3", self.repo)
        fake_herdr.put_agent(self.world, "w3:p5", "claude", "fablyn-dev", args=["--chrome"])
        del self.world["panes"]["w2:p2"]
        code, out, _ = self.run_cli("swap")
        self.assertEqual(code, 0, out)
        self.assertIn("state: fablyn-dev moved from w2:p2 to w3:p5", out)
        self.assertIn("fablyn-dev: skipped - adopted session", out)
        self.assertEqual(self.prompts_to("fablyn-dev"), [])
        entry = self.read_state()["agents"]["fablyn-dev"]
        self.assertEqual((entry["pane_id"], entry["adopted"], entry["created_pane"]), ("w3:p5", True, True))

    def test_hand_moved_started_seat_keeps_its_record(self):
        fake_herdr.add_workspace(self.world, "w3", "elsewhere", self.repo)
        fake_herdr.add_pane(self.world, "w3:p5", "w3", self.repo)
        fake_herdr.put_agent(self.world, "w3:p5", "claude", "fablyn-dev", args=ARGS)
        del self.world["panes"]["w2:p2"]
        code, out, _ = self.run_cli("status", "--json")
        dev = json.loads(out)["seats"][1]
        self.assertEqual((dev["pane_id"], dev["adopted"], dev["drift"]), ("w3:p5", False, []))
        entry = self.read_state()["agents"]["fablyn-dev"]
        self.assertEqual((entry["args"], entry["created_pane"]), (ARGS, True))


class CallerPane(LiveFleet):
    """Preflight resolves the caller with `pane current --current` (stale HERDR_PANE_ID)."""

    def test_stale_pane_id_is_resolved(self):
        self.env["HERDR_PANE_ID"] = "w9:p1"           # inherited before a move
        self.world["aliases"] = {"w9:p1": "w2:p2"}    # the caller's terminal is now w2:p2
        code, out, _ = self.run_cli("restart", "fablyn-dev")
        self.assertEqual(code, 3)
        self.assertIn("fablyn-dev: refused - it is the caller's own agent", out)
        self.assertIn(["pane", "current", "--current"], self.argvs("pane", "current"))
        self.assertEqual(self.prompts_to("fablyn-dev"), [])

    def test_falls_back_to_herdr_pane_id(self):
        self.env["HERDR_PANE_ID"] = "w2:p2"
        self.world["canned"] = [{"prefix": ["pane", "current"], "exit": 1,
                                 "stderr": {"error": {"code": "pane_not_found", "message": "no caller"}}}]
        code, out, _ = self.run_cli("restart", "fablyn-dev")
        self.assertEqual(code, 3)
        self.assertIn("fablyn-dev: refused - it is the caller's own agent", out)
        self.assertEqual(self.prompts_to("fablyn-dev"), [])


class Join(FleetCase):
    """A labelled fleet; the caller (w1:p1) runs in workspace w1, the fleet's is w2."""

    def setUp(self):
        super().setUp()
        self.root = self.fleet_ws("w2", "fablyn")

    def test_me_outside_the_fleet_workspace_warns_with_the_move(self):
        code, out, _ = self.run_cli("up", "--me", "fablyn-orchestrator")
        self.assertEqual(code, 0, out)
        self.assertIn("fablyn-orchestrator: warning - your pane w1:p1 is in workspace w1 (caller) but the fleet's "
                      "workspace is w2 (fablyn), where the other seats open; to join them run "
                      "`herdr pane move w1:p1 --tab w2:t1 --split right --ratio 0.5 --no-focus`, "
                      "or rerun up with --join", out)
        self.assertEqual(self.argvs("pane", "move"), [])
        self.assertEqual(self.world["panes"][CALLER_PANE]["workspace_id"], "w1")

    def test_join_moves_once_and_records_the_new_pane(self):
        code, out, err = self.run_cli("up", "--me", "fablyn-orchestrator", "--join")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.argvs("pane", "move"), [MOVE])
        moved = self.pane_of("fablyn-orchestrator")
        self.assertNotEqual(moved, CALLER_PANE)
        self.assertEqual(self.world["panes"][moved]["workspace_id"], "w2")
        self.assertIn("fablyn-orchestrator: joined - moved your pane w1:p1 into w2 w2:t1; it is now %s" % moved, out)
        entry = self.read_state()["agents"]["fablyn-orchestrator"]
        self.assertEqual((entry["pane_id"], entry["adopted"]), (moved, True))
        self.assertEqual(self.argvs("pane", "split")[0][2], moved)  # the next seat splits off you
        starts = [a[a.index("--pane") + 1] for a in self.argvs("agent", "start")]
        self.assertNotIn(moved, starts)
        self.assertEqual(self.prompts_to("fablyn-orchestrator"), [])

    def test_join_into_a_workspace_created_this_run(self):
        del self.world["workspaces"]["w2"]
        del self.world["panes"]["w2:p1"]
        code, out, _ = self.run_cli("up", "--me", "fablyn-orchestrator", "--join")
        self.assertEqual(code, 0, out)
        ws = self.read_state()["workspace_id"]
        self.assertEqual(self.argvs("pane", "move")[0][3:5], ["--tab", ws + ":t1"])
        self.assertEqual(self.pane_of("fablyn-dev"), ws + ":p1")  # the root pane still goes to the first new seat

    def test_after_join_the_moved_caller_is_still_recognised(self):
        self.assertEqual(self.run_cli("up", "--me", "fablyn-orchestrator", "--join")[0], 0)
        moved = self.pane_of("fablyn-orchestrator")
        self.world["panes"][moved]["agent"]["agent_status"] = "idle"  # even when idle
        code, out, _ = self.run_cli("restart", "fablyn-orchestrator")  # HERDR_PANE_ID is still w1:p1
        self.assertEqual(code, 3)
        self.assertIn("refused - it is the caller's own agent", out)
        self.assertEqual(self.prompts_to("fablyn-orchestrator"), [])

    def test_no_move_without_join(self):
        self.run_cli("up", "--me", "fablyn-orchestrator")
        self.assertEqual(self.argvs("pane", "move"), [])

    def test_join_needs_me(self):
        code, _, err = self.run_cli("up", "--join")
        self.assertEqual(code, 2)
        self.assertIn("--join moves your own pane, so it needs --me NAME", err)

    def test_dry_run_plans_the_move(self):
        code, out, _ = self.run_cli("up", "--me", "fablyn-orchestrator", "--join", "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("plan - herdr pane move w1:p1 --tab w2:t1 --split right --ratio 0.5 --no-focus", out)
        self.assertEqual(self.argvs("pane", "move"), [])

    def test_failed_join_leaves_the_pane_and_continues(self):
        self.world["canned"] = [{"prefix": ["pane", "move"], "exit": 1,
                                 "stderr": {"error": {"code": "pane_move_failed", "message": "nope"}}}]
        code, out, _ = self.run_cli("up", "--me", "fablyn-orchestrator", "--join")
        self.assertEqual(code, 1)
        self.assertIn("--join: herdr error pane_move_failed: nope; your pane stays where it is", out)
        self.assertEqual(self.read_state()["agents"]["fablyn-orchestrator"]["pane_id"], CALLER_PANE)
        self.assertEqual([a[2] for a in self.argvs("agent", "start")], ["fablyn-dev", "fablyn-reviewer"])

    def test_join_when_already_there_does_nothing(self):
        self.env.update(HERDR_PANE_ID="w2:p9", HERDR_WORKSPACE_ID="w2")
        fake_herdr.add_pane(self.world, "w2:p9", "w2", self.repo)
        fake_herdr.put_agent(self.world, "w2:p9", "claude", name="caller")
        code, out, _ = self.run_cli("up", "--me", "fablyn-orchestrator", "--join")
        self.assertEqual(code, 0, out)
        self.assertIn("already in the fleet's workspace w2", out)
        self.assertEqual(self.argvs("pane", "move"), [])


if __name__ == "__main__":
    unittest.main()
