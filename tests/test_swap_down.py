"""swap (design 4.4: drift from process-info argv, live kind and the state
file) and down (4.6: quit rules, closing only what herdr-fleet created)."""

from __future__ import annotations

import unittest

from support import FleetCase, LiveFleet


class Swap(LiveFleet):
    def test_restarts_only_the_seat_whose_argv_drifted(self):
        self.write_fleet(self.read_fleet().replace(
            'name = "fablyn-dev"\nkind = "claude"\nargs = ["--model", "claude-opus-5-5"]',
            'name = "fablyn-dev"\nkind = "claude"\nargs = ["--model", "claude-opus-6"]'))
        code, out, err = self.run_cli("swap")
        self.assertEqual(code, 0, out + err)
        self.assertIn("fablyn-dev: drifted - args: running --model claude-opus-5-5, "
                      "file says --model claude-opus-6", out)
        self.assertIn("fablyn-orchestrator: unchanged", out)
        self.assertEqual(self.argvs("agent", "start"),
                         [["agent", "start", "fablyn-dev", "--kind", "claude", "--pane", "w2:p2", "--",
                           "--model", "claude-opus-6"]])
        self.assertEqual(self.prompts_to("fablyn-orchestrator"), [])
        self.assertEqual(self.prompts_to("fablyn-reviewer"), [])
        self.assertIn("on swap.", self.prompts_to("fablyn-dev")[1])
        self.assertEqual(self.read_state()["agents"]["fablyn-dev"]["args"], ["--model", "claude-opus-6"])

    def test_argv_drift_detected_from_cmdline_when_argv_absent(self):
        self.world["panes"]["w2:p2"]["procs"][0].pop("argv")
        self.world["panes"]["w2:p2"]["procs"][0]["cmdline"] = "claude --model claude-old"
        state = self.read_state()
        del state["agents"]["fablyn-dev"]["args"]  # no recorded args: treated as adopted (design 15)
        self.write_state(state)
        code, out, _ = self.run_cli("swap", "fablyn-dev", "--dry-run")  # named, so it is compared
        self.assertEqual(code, 0)
        self.assertIn("fablyn-dev: would restart - args: running --model claude-old", out)

    def test_kind_swap_quits_with_the_live_kinds_command(self):
        self.write_fleet(self.read_fleet().replace('kind = "codex"\nargs = ["-m", "gpt-6-astra"]',
                                                    'kind = "claude"\nargs = ["--model", "claude-opus-5-5"]'))
        code, out, _ = self.run_cli("swap")
        self.assertEqual(code, 0, out)
        self.assertIn("kind: running codex, file says claude", out)
        self.assertEqual(self.prompts_to("fablyn-reviewer")[0], "/quit")
        self.assertEqual(self.argvs("agent", "start")[0][3:5], ["--kind", "claude"])

    def test_state_file_records_drift_the_argv_cannot_show(self):
        # The file dropped its args: the running argv still "contains" [], but
        # the state file says the seat was started with a model.
        self.write_fleet(self.read_fleet().replace(
            'name = "fablyn-dev"\nkind = "claude"\nargs = ["--model", "claude-opus-5-5"]',
            'name = "fablyn-dev"\nkind = "claude"'))
        code, out, _ = self.run_cli("swap", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("fablyn-dev: would restart - args: started with --model claude-opus-5-5, file says (none)", out)

    def test_dry_run_lists_and_changes_nothing(self):
        del self.world["panes"]["w2:p3"]
        code, out, _ = self.run_cli("swap", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("fablyn-reviewer: would start - not running", out)
        self.assertEqual(self.argvs("agent", "prompt"), [])
        self.assertEqual(self.argvs("agent", "start"), [])
        self.assertEqual(self.argvs("pane", "split"), [])

    def test_absent_seat_is_started(self):
        del self.world["panes"]["w2:p3"]
        code, out, _ = self.run_cli("swap")
        self.assertEqual(code, 0, out)
        self.assertEqual([s[2] for s in self.argvs("agent", "start")], ["fablyn-reviewer"])
        self.assertEqual(self.argvs("pane", "split")[0][2], "w2:p2")

    def test_no_drift_changes_nothing(self):
        code, out, _ = self.run_cli("swap")
        self.assertEqual(code, 0)
        self.assertEqual(self.argvs("agent", "prompt"), [])
        self.assertEqual(out.count("unchanged"), 3)

    def test_drifted_but_blocked_is_refused(self):
        self.write_fleet(self.read_fleet().replace('"-m", "gpt-6-astra"', '"-m", "gpt-7"'))
        self.status_of("w2:p3", "blocked")
        code, out, _ = self.run_cli("swap")
        self.assertEqual(code, 3)
        self.assertEqual(self.prompts_to("fablyn-reviewer"), [])


class Down(LiveFleet):
    def test_quits_every_idle_seat_and_keeps_state(self):
        code, out, _ = self.run_cli("down")
        self.assertEqual(code, 0, out)
        for name, command in (("fablyn-orchestrator", "/exit"), ("fablyn-dev", "/exit"), ("fablyn-reviewer", "/quit")):
            self.assertEqual(self.prompts_to(name), [command])
        self.assertEqual(self.argvs("pane", "close"), [])
        state = self.read_state()
        self.assertEqual(state["agents"]["fablyn-dev"]["pane_id"], "w2:p2")
        # up afterwards reuses the recorded panes instead of splitting
        code, out, _ = self.run_cli("up")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.argvs("pane", "split"), [])
        self.assertEqual([s[6] for s in self.argvs("agent", "start")], ["w2:p1", "w2:p2", "w2:p3"])

    def test_blocked_seat_is_refused_and_its_pane_kept(self):
        self.status_of("w2:p2", "blocked")
        code, out, _ = self.run_cli("down", "--close-panes")
        self.assertEqual(code, 3)
        self.assertEqual(self.prompts_to("fablyn-dev"), [])
        self.assertIn("w2:p2", self.world["panes"])
        self.assertNotIn("w2:p3", self.world["panes"])  # created and quit: closed
        self.assertIn("w2:p1", self.world["panes"])     # not created by herdr-fleet: kept

    def test_close_panes_only_closes_created_panes_at_a_shell(self):
        self.world["behaviors"]["ignore_quit"] = []
        code, out, _ = self.run_cli("down", "--close-panes")
        self.assertEqual(code, 0, out)
        self.assertEqual(sorted(a[2] for a in self.argvs("pane", "close")), ["w2:p2", "w2:p3"])
        state = self.read_state()
        self.assertNotIn("fablyn-dev", state["agents"])
        self.assertEqual(state["agents"]["fablyn-orchestrator"]["pane_id"], "w2:p1")

    def test_working_needs_wait(self):
        self.status_of("w2:p2", "working")
        code, out, _ = self.run_cli("down", "fablyn-dev")
        self.assertEqual(code, 3)
        self.assertIn("pass --wait MS", out)
        self.assertEqual(self.prompts_to("fablyn-dev"), [])

    def test_close_workspace_refused_when_not_created(self):
        code, out, _ = self.run_cli("down", "--close-workspace")
        self.assertEqual(code, 3)
        self.assertIn("was not created by herdr-fleet", out)
        self.assertEqual(self.argvs("workspace", "close"), [])

    def test_absent_seats_are_already_down(self):
        del self.world["panes"]["w2:p3"]
        code, out, _ = self.run_cli("down", "fablyn-reviewer")
        self.assertEqual(code, 0)
        self.assertIn("fablyn-reviewer: down - not running", out)


class DownCreatedWorkspace(FleetCase):
    def test_close_workspace_after_everything_is_gone(self):
        self.assertEqual(self.run_cli("up")[0], 0)
        ws = self.read_state()["workspace_id"]
        code, out, _ = self.run_cli("down", "--close-panes", "--close-workspace")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.argvs("workspace", "close"), [["workspace", "close", ws]])
        self.assertNotIn(ws, self.world["workspaces"])
        state = self.read_state()
        self.assertIsNone(state["workspace_id"])
        self.assertFalse(state["created_workspace"])
        # the root pane (seat 1) was never closed on its own: it went with the workspace
        root_closes = [a for a in self.argvs("pane", "close") if a[2] == ws + ":p1"]
        self.assertEqual(root_closes, [])

    def test_close_workspace_kept_while_a_seat_is_live(self):
        self.assertEqual(self.run_cli("up")[0], 0)
        pane = self.pane_of("fablyn-dev")
        self.world["panes"][pane]["agent"]["agent_status"] = "blocked"
        code, out, _ = self.run_cli("down", "--close-workspace")
        self.assertEqual(code, 3)
        self.assertIn("agents still live: fablyn-dev", out)
        self.assertEqual(self.argvs("workspace", "close"), [])


if __name__ == "__main__":
    unittest.main()
