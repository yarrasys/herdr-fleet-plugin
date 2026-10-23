"""restart (design 4.3), kickoff (4.5) and the quit procedure (section 6)."""

from __future__ import annotations

import os
import unittest

import fake_herdr
from support import ARGS, LiveFleet

class Restart(LiveFleet):
    def test_idle_seat_is_quit_restarted_in_place_and_briefed(self):
        code, out, err = self.run_cli("restart", "fablyn-dev", "--note", "Pick up the export dialog.")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.prompts_to("fablyn-dev")[0], "/exit")
        self.assertEqual(self.argvs("agent", "start"),
                         [["agent", "start", "fablyn-dev", "--kind", "claude", "--pane", "w2:p2", "--"] + ARGS])
        self.assertEqual(self.argvs("pane", "split"), [])
        kickoff = self.prompts_to("fablyn-dev")[1]
        self.assertIn("on restart.", kickoff)
        self.assertIn("Pick up the export dialog.", kickoff)
        self.assertEqual(len(self.prompts_to("fablyn-dev")), 2)
        self.assertEqual(self.pane_of("fablyn-dev"), "w2:p2")
        entry = self.read_state()["agents"]["fablyn-dev"]
        self.assertTrue(entry["created_pane"])
        self.assertTrue(entry["started_at"])
        # the quit procedure polled agent get on the pane, then checked the shell
        self.assertIn(["agent", "get", "w2:p2"], self.argvs("agent", "get"))

    def test_agent_not_ready_that_clears_by_itself_is_briefed_once(self):
        self.world["behaviors"]["start_not_ready_transient"] = {"fablyn-dev": 2}
        code, out, err = self.run_cli("restart", "fablyn-dev")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.prompts_to("fablyn-dev")[0], "/exit")
        kickoffs = self.prompts_to("fablyn-dev")[1:]
        self.assertEqual(len(kickoffs), 1)
        self.assertIn("on restart.", kickoffs[0])
        entry = self.read_state()["agents"]["fablyn-dev"]
        self.assertFalse(entry["blocked_at_startup"])
        self.assertTrue(entry["last_kickoff_at"])
        self.assertNotIn("blocked at startup", out)
        self.assertEqual(self.world["notifications"], [])
        # between the agent_not_ready start and the first idle answer only agent get ran
        calls = self.calls()
        start = next(i for i, c in enumerate(calls) if c["argv"][:2] == ["agent", "start"])
        self.assertNotEqual(calls[start]["exit"], 0)
        after = [(i, c) for i, c in enumerate(calls) if i > start]
        ready = next(i for i, c in after if c["argv"] == ["agent", "get", "w2:p2"] and c["info"].get("status") == "idle")
        between = [c["argv"][:2] for i, c in after if i < ready]
        self.assertEqual(between, [["agent", "get"], ["agent", "get"]])
        prompts = [i for i, c in after if c["argv"][:2] == ["agent", "prompt"]]
        self.assertEqual(len(prompts), 1)
        self.assertGreater(prompts[0], ready)

    def test_codex_quits_with_slash_quit(self):
        code, out, _ = self.run_cli("restart", "fablyn-reviewer")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.prompts_to("fablyn-reviewer")[0], "/quit")

    def test_blocked_and_unknown_are_refused_and_never_prompted(self):
        self.status_of("w2:p2", "blocked")
        self.status_of("w2:p3", "unknown")
        code, out, _ = self.run_cli("restart", "fablyn-dev", "fablyn-reviewer", "fablyn-orchestrator")
        self.assertEqual(code, 3, out)
        self.assertEqual(self.prompts_to("fablyn-dev"), [])
        self.assertEqual(self.prompts_to("fablyn-reviewer"), [])
        self.assertIn("agent is blocked", out)
        self.assertIn("agent is unknown", out)
        self.assertEqual(self.prompts_to("fablyn-orchestrator")[0], "/exit")  # the others continue

    def test_working_needs_wait(self):
        self.status_of("w2:p2", "working")
        code, out, _ = self.run_cli("restart", "fablyn-dev")
        self.assertEqual(code, 3)
        self.assertIn("pass --wait MS", out)
        self.assertEqual(self.prompts_to("fablyn-dev"), [])
        self.assertEqual(self.argvs("agent", "wait"), [])

    def test_wait_lets_a_working_agent_settle(self):
        self.status_of("w2:p2", "working")
        self.world["behaviors"]["wait_status"] = {"fablyn-dev": "idle"}
        code, out, _ = self.run_cli("restart", "fablyn-dev", "--wait", "60000")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.argvs("agent", "wait"),
                         [["agent", "wait", "fablyn-dev", "--until", "idle", "--until", "done",
                           "--until", "blocked", "--timeout", "60000"]])
        self.assertEqual(self.prompts_to("fablyn-dev")[0], "/exit")

    def test_wait_that_ends_blocked_still_refuses(self):
        self.status_of("w2:p2", "working")
        self.world["behaviors"]["wait_status"] = {"fablyn-dev": "blocked"}
        code, out, _ = self.run_cli("restart", "fablyn-dev", "--wait", "1000")
        self.assertEqual(code, 3)
        self.assertEqual(self.prompts_to("fablyn-dev"), [])
        self.assertIn("agent is blocked", out)

    def test_wait_timeout_refuses(self):
        self.status_of("w2:p2", "working")
        code, out, _ = self.run_cli("restart", "fablyn-dev", "--wait", "1000")
        self.assertEqual(code, 3)
        self.assertIn("still working after --wait 1000 ms", out)
        self.assertIn("timeout", out)
        self.assertEqual(self.prompts_to("fablyn-dev"), [])

    def test_quit_polls_until_agent_gone_and_shell_back(self):
        self.world["behaviors"].update({"quit_polls": 5, "linger_polls": 3})
        code, out, _ = self.run_cli("restart", "fablyn-dev")
        self.assertEqual(code, 0, out)
        gets = self.argvs("agent", "get", "w2:p2")
        self.assertEqual(len(gets), 6 + 3)  # 5 still there, then gone on each of 4 shell checks
        self.assertEqual(len(self.argvs("agent", "start")), 1)

    def test_quit_timeout_is_reported_not_retried(self):
        self.world["behaviors"]["ignore_quit"] = ["fablyn-dev"]
        code, out, _ = self.run_cli("restart", "fablyn-dev")
        self.assertEqual(code, 1)
        self.assertIn("did not exit within 20000 ms", out)
        self.assertEqual(self.prompts_to("fablyn-dev"), ["/exit"])
        self.assertEqual(self.argvs("agent", "start"), [])
        self.assertGreaterEqual(self.clock.slept, 20.0)
        self.assertLess(self.clock.slept, 20.6)

    def test_quit_timeout_from_fleet_file(self):
        self.write_fleet("quit_timeout_ms = 2000\n" + self.read_fleet())
        self.world["behaviors"]["ignore_quit"] = ["fablyn-dev"]
        code, out, _ = self.run_cli("restart", "fablyn-dev")
        self.assertEqual(code, 1)
        self.assertIn("within 2000 ms", out)

    def test_quit_timeout_from_plugin_config(self):
        config = os.path.join(self.tmp, "config")
        os.makedirs(config)
        with open(os.path.join(config, "config.toml"), "w") as handle:
            handle.write('[quit]\nquit_timeout_ms = 3000\ncodex = "/quit"\n')
        self.env["HERDR_PLUGIN_CONFIG_DIR"] = config
        self.world["behaviors"]["ignore_quit"] = ["fablyn-dev"]
        code, out, _ = self.run_cli("restart", "fablyn-dev")
        self.assertEqual(code, 1)
        self.assertIn("within 3000 ms", out)

    def test_stalled_quit_is_not_resent(self):
        self.world["behaviors"]["prompt_errors"] = {"fablyn-dev": "agent_prompt_stalled"}
        code, out, _ = self.run_cli("restart", "fablyn-dev")
        self.assertEqual(code, 1)
        self.assertEqual(self.prompts_to("fablyn-dev"), ["/exit"])
        self.assertIn("not resent", out)
        self.assertEqual(self.argvs("agent", "start"), [])

    def test_agent_blocked_race_is_a_refusal(self):
        # idle at the check, blocked by the time the prompt lands: herdr refuses and sends nothing
        self.world["canned"] = [{"prefix": ["agent", "prompt", "fablyn-dev"], "exit": 1,
                                 "stderr": {"error": {"code": "agent_blocked", "message": "agent is blocked"}}}]
        code, out, _ = self.run_cli("restart", "fablyn-dev")
        self.assertEqual(code, 3)
        self.assertIn("refused - herdr error agent_blocked: agent is blocked; nothing sent", out)
        self.assertEqual(self.argvs("agent", "start"), [])

    def test_absent_seat_starts_in_its_recorded_pane(self):
        self.world["panes"]["w2:p2"]["agent"] = None
        self.world["panes"]["w2:p2"]["procs"] = [dict(fake_herdr.SHELL)]
        code, out, _ = self.run_cli("restart", "fablyn-dev")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.argvs("agent", "start")[0][6], "w2:p2")
        self.assertEqual(self.argvs("pane", "split"), [])
        self.assertIn("on restart.", self.prompts_to("fablyn-dev")[0])

    def test_absent_seat_without_pane_gets_a_fresh_split(self):
        del self.world["panes"]["w2:p2"]
        code, out, _ = self.run_cli("restart", "fablyn-dev")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.argvs("pane", "split")[0][2], "w2:p1")  # after the previous seat

    def test_refuses_the_callers_own_agent(self):
        self.env["HERDR_PANE_ID"] = "w2:p2"
        code, out, _ = self.run_cli("restart", "fablyn-dev")
        self.assertEqual(code, 3)
        self.assertIn("caller's own agent", out)
        self.assertEqual(self.prompts_to("fablyn-dev"), [])

    def test_kind_without_quit_command_is_refused_with_hint(self):
        self.write_fleet('[[agents]]\nname = "helper"\nkind = "amp"\n')
        fake_herdr.add_pane(self.world, "w2:p9", "w2", self.repo)
        fake_herdr.put_agent(self.world, "w2:p9", "amp", "helper")
        code, out, _ = self.run_cli("restart", "helper")
        self.assertEqual(code, 3)
        self.assertIn("no quit command for kind amp", out)
        self.assertIn("[quit]", out)
        self.assertEqual(self.prompts_to("helper"), [])

    def test_quit_command_from_plugin_config(self):
        self.write_fleet('[[agents]]\nname = "helper"\nkind = "amp"\n')
        fake_herdr.add_pane(self.world, "w2:p9", "w2", self.repo)
        fake_herdr.put_agent(self.world, "w2:p9", "amp", "helper")
        config = os.path.join(self.tmp, "config")
        os.makedirs(config)
        with open(os.path.join(config, "config.toml"), "w") as handle:
            handle.write('[quit]\namp = "/bye"\n')
        self.env["HERDR_PLUGIN_CONFIG_DIR"] = config
        self.world["behaviors"]["quit_texts"] = ["/bye"]
        code, out, _ = self.run_cli("restart", "helper")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.prompts_to("helper")[0], "/bye")

    def test_note_file_and_state_note(self):
        self.write_fleet('state_note = "docs/STATE.md"\n' + self.read_fleet())
        self.write_file("docs/STATE.md", "main is green.")
        note = self.write_file("note.txt", "Rebase onto main first.")
        code, out, _ = self.run_cli("restart", "fablyn-dev", "--note-file", note)
        self.assertEqual(code, 0, out)
        kickoff = self.prompts_to("fablyn-dev")[1]
        self.assertIn("main is green.", kickoff)
        self.assertIn("Rebase onto main first.", kickoff)

    def test_dry_run_changes_nothing(self):
        code, out, _ = self.run_cli("restart", "fablyn-dev", "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.argvs("agent", "prompt"), [])
        self.assertEqual(self.argvs("agent", "start"), [])
        self.assertIn("quit fablyn-dev (claude, idle in w2:p2) with herdr agent prompt fablyn-dev /exit", out)

    def test_restart_needs_a_name(self):
        code, _, _ = self.run_cli("restart")
        self.assertEqual(code, 2)


class Kickoff(LiveFleet):
    def test_resends_to_idle_with_reason_kickoff(self):
        code, out, _ = self.run_cli("kickoff", "fablyn-dev", "--note", "hello")
        self.assertEqual(code, 0, out)
        text = self.prompts_to("fablyn-dev")[0]
        self.assertIn("on kickoff.", text)
        self.assertIn("hello", text)
        self.assertEqual(self.argvs("agent", "start"), [])

    def test_refuses_working_blocked_unknown_and_absent(self):
        self.status_of("w2:p1", "working")
        self.status_of("w2:p2", "blocked")
        self.status_of("w2:p3", "unknown")
        code, out, _ = self.run_cli("kickoff", "fablyn-orchestrator", "fablyn-dev", "fablyn-reviewer")
        self.assertEqual(code, 3)
        self.assertEqual(self.argvs("agent", "prompt"), [])
        for state in ("working", "blocked: a dialog is open", "unknown"):  # design 23.5
            self.assertIn("is %s; nothing sent" % state, out)

    def test_clears_blocked_at_startup(self):
        state = self.read_state()
        state["agents"]["fablyn-dev"]["blocked_at_startup"] = True
        self.write_state(state)
        self.assertEqual(self.run_cli("kickoff", "fablyn-dev")[0], 0)
        self.assertFalse(self.read_state()["agents"]["fablyn-dev"]["blocked_at_startup"])

    def test_custom_template_with_braces(self):
        self.write_file("docs/kickoff/dev.md", '[from owner] {name}: run `jq "{a: 1}"` and {unknown}.')
        text = self.read_fleet().replace('name = "fablyn-dev"\n',
                                         'name = "fablyn-dev"\nkickoff = "docs/kickoff/dev.md"\n')
        self.write_fleet(text)
        self.assertEqual(self.run_cli("kickoff", "fablyn-dev")[0], 0)
        self.assertEqual(self.prompts_to("fablyn-dev")[0], '[from owner] fablyn-dev: run `jq "{a: 1}"` and {unknown}.')


if __name__ == "__main__":
    unittest.main()
