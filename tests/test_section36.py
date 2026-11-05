"""Design section 36 (version 0.19.1): current-screen reads on panes shorter than the request."""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

import fake_herdr
from support import LiveFleet
from herdr_fleet import seats
from herdr_fleet.herdr import Herdr, HerdrError
from test_section24 import BoardCase

NAME = "sample-dev"
PLAIN = ["agent", "read", NAME, "--lines", "16"]
VISIBLE = PLAIN + ["--source", "visible"]
STAY_HIGHLIGHTED = fake_herdr.EXIT_DIALOG.replace("   ❯ 1. Exit and stop tasks", "     1. Exit and stop tasks") \
    .replace("     3. Stay", "   ❯ 3. Stay")
DIALOG = "\n".join(fake_herdr.DIALOG).replace("   1. Yes", "❯ 1. Yes")


def read_pair(name=NAME):
    plain = ["agent", "read", name, "--lines", "16"]
    return [plain, plain + ["--source", "visible"]]


class ReadScreenHelper(unittest.TestCase):
    def test_agent_not_idle_gets_one_visible_read(self):
        calls = []

        def runner(argv):
            calls.append(argv[1:])
            if len(calls) == 1:
                return 1, "", json.dumps({"error": {"code": "agent_not_idle", "message": "history unavailable"}})
            return 0, "current screen\n", ""

        self.assertEqual(seats.read_screen(Herdr("herdr", runner), NAME, 16), "current screen\n")
        self.assertEqual(calls, read_pair())

    def test_another_error_is_raised_without_a_fallback(self):
        calls = []

        def runner(argv):
            calls.append(argv[1:])
            return 1, "", json.dumps({"error": {"code": "server_error", "message": "boom"}})

        with self.assertRaises(HerdrError) as caught:
            seats.read_screen(Herdr("herdr", runner), NAME, 16)
        self.assertEqual(caught.exception.code, "server_error")
        self.assertEqual(calls, [PLAIN])


class S36(LiveFleet):
    def short(self, pane="w2:p2"):
        fake_herdr.viewport_rows(self.world, pane, 13)

    def drift(self):
        self.write_fleet(self.read_fleet().replace('name = "sample-dev"\nkind = "claude"\nargs = ["--model", '
                                                   '"claude-opus-5-5"]', 'name = "sample-dev"\nkind = "claude"\n'
                                                   'args = ["--model", "claude-sonnet-5-5"]'))

    def assert_answered(self, out):
        self.assertIn("sample-dev: quit - answered its exit confirmation (exit and stop tasks)", out)
        self.assertEqual(self.argvs("agent", "read"), read_pair())
        self.assertEqual(self.argvs("agent", "send-keys"), [["agent", "send-keys", NAME, "enter"]])
        calls = [call["argv"] for call in self.calls()]
        self.assertEqual(calls[calls.index(VISIBLE) + 1], ["agent", "send-keys", NAME, "enter"])


class QuitScreens(S36):
    def test_short_pane_restart_answers_the_exit_confirmation(self):
        self.short()
        fake_herdr.exit_dialog_on_quit(self.world, NAME)
        code, out, _ = self.run_cli("restart", NAME)
        self.assertEqual(code, 0, out)
        self.assert_answered(out)

    def test_short_pane_swap_answers_the_exit_confirmation(self):
        self.short()
        self.drift()
        fake_herdr.exit_dialog_on_quit(self.world, NAME)
        code, out, _ = self.run_cli("swap", NAME)
        self.assertEqual(code, 0, out)
        self.assert_answered(out)

    def test_tall_pane_restart_keeps_its_single_read(self):
        fake_herdr.exit_dialog_on_quit(self.world, NAME)
        code, out, _ = self.run_cli("restart", NAME)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.argvs("agent", "read"), [PLAIN])
        self.assertEqual(self.argvs("agent", "send-keys"), [["agent", "send-keys", NAME, "enter"]])

    def test_tall_pane_swap_keeps_its_single_read(self):
        self.drift()
        fake_herdr.exit_dialog_on_quit(self.world, NAME)
        code, out, _ = self.run_cli("swap", NAME)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.argvs("agent", "read"), [PLAIN])
        self.assertEqual(self.argvs("agent", "send-keys"), [["agent", "send-keys", NAME, "enter"]])

    def test_short_pane_unhighlighted_exit_gets_no_enter_and_names_the_dialog(self):
        self.short()
        fake_herdr.exit_dialog_on_quit(self.world, NAME, STAY_HIGHLIGHTED)
        code, out, _ = self.run_cli("down", NAME)
        self.assertEqual(code, 1, out)
        self.assertIn('the pane shows a dialog: "Background work is running"; answer it by hand', out)
        self.assertEqual(self.argvs("agent", "read"), read_pair() + read_pair())
        self.assertEqual(self.argvs("agent", "send-keys"), [])

    def test_tall_pane_unhighlighted_exit_keeps_its_two_reads(self):
        fake_herdr.exit_dialog_on_quit(self.world, NAME, STAY_HIGHLIGHTED)
        code, out, _ = self.run_cli("down", NAME)
        self.assertEqual(code, 1, out)
        self.assertIn('the pane shows a dialog: "Background work is running"; answer it by hand', out)
        self.assertEqual(self.argvs("agent", "read"), [PLAIN, PLAIN])
        self.assertEqual(self.argvs("agent", "send-keys"), [])

    def test_short_pane_held_quit_names_the_dialog(self):
        reviewer = "w2:p3"
        self.short(reviewer)
        fake_herdr.show_menu(self.world, reviewer, 1000)
        original = fake_herdr.Sim._agent_get
        checked = []

        def working_after_the_prompt_check(sim, args):
            result = original(sim, args)
            if sim.info.get("name") == "sample-reviewer":
                checked.append(args[0])
                if len(checked) == 1:
                    sim.w["panes"][reviewer]["agent"]["agent_status"] = "working"
            return result

        with mock.patch.object(fake_herdr.Sim, "_agent_get", working_after_the_prompt_check):
            code, out, _ = self.run_cli("restart", "sample-reviewer")
        self.assertEqual(code, 3, out)
        self.assertIn('a dialog or a menu is on screen in w2:p3 ("Approaching rate limits")', out)
        self.assertEqual(self.argvs("agent", "read"), read_pair("sample-reviewer"))

    def test_tall_pane_held_quit_keeps_its_single_read(self):
        fake_herdr.show_menu(self.world, "w2:p3", 1000)
        code, out, _ = self.run_cli("restart", "sample-reviewer")
        self.assertEqual(code, 3, out)
        self.assertIn('a dialog or a menu is on screen in w2:p3 ("Approaching rate limits")', out)
        self.assertEqual(self.argvs("agent", "read"), [read_pair("sample-reviewer")[0]])


class InterruptScreens(S36):
    def reviewer_with_dialog(self):
        self.write_fleet(self.read_fleet().replace('kind = "codex"', 'kind = "pi"')
                         + '\n[interrupt]\npi = ["ctrl+c"]\n')
        fake_herdr.put_agent(self.world, "w2:p3", "pi", "sample-reviewer", status="working")
        fake_herdr.set_screen(self.world, "w2:p3", DIALOG)

    def test_short_pane_without_a_dialog_presses_the_keys(self):
        self.short()
        self.status_of("w2:p2", "working")
        code, out, _ = self.run_cli("interrupt", NAME)
        self.assertEqual((code, out), (0, "interrupted sample-dev (done)\n"))
        self.assertEqual(self.argvs("agent", "read")[:2], read_pair())
        self.assertEqual(self.argvs("agent", "send-keys"), [["agent", "send-keys", NAME, "ctrl+c"]])
        self.assertEqual(len([argv for argv in self.argvs("agent", "read") if "--source" in argv]), 1)

    def test_tall_pane_without_a_dialog_keeps_its_reads(self):
        self.status_of("w2:p2", "working")
        code, out, _ = self.run_cli("interrupt", NAME)
        self.assertEqual((code, out), (0, "interrupted sample-dev (done)\n"))
        self.assertEqual(self.argvs("agent", "read"),
                         [PLAIN, ["agent", "read", NAME, "--lines", "12", "--format", "ansi"]])

    def test_short_pane_dialog_is_named_and_gets_no_keys(self):
        self.reviewer_with_dialog()
        self.short("w2:p3")
        code, out, _ = self.run_cli("interrupt", "sample-reviewer")
        self.assertEqual(code, 3, out)
        self.assertIn('a dialog is on screen ("Bash command"); no keys sent', out)
        self.assertEqual(self.argvs("agent", "read"), read_pair("sample-reviewer"))
        self.assertEqual(self.argvs("agent", "send-keys"), [])

    def test_tall_pane_dialog_keeps_its_single_read(self):
        self.reviewer_with_dialog()
        code, out, _ = self.run_cli("interrupt", "sample-reviewer")
        self.assertEqual(code, 3, out)
        self.assertIn('a dialog is on screen ("Bash command"); no keys sent', out)
        self.assertEqual(self.argvs("agent", "read"), [read_pair("sample-reviewer")[0]])
        self.assertEqual(self.argvs("agent", "send-keys"), [])


class BoardScreens(BoardCase):
    def test_short_pane_shows_the_blocked_seats_dialog(self):
        self.blocked_orchestrator()
        fake_herdr.viewport_rows(self.world, "w2:p1", 13)
        result = self.build()
        self.assertEqual(result.rows[0].doing, "Allow Bash(pnpm test)?")
        self.assertEqual(self.argvs("agent", "read"), read_pair("sample-orchestrator"))

    def test_tall_pane_keeps_the_blocked_seats_single_read(self):
        self.blocked_orchestrator()
        result = self.build()
        self.assertEqual(result.rows[0].doing, "Allow Bash(pnpm test)?")
        self.assertEqual(self.argvs("agent", "read"), [read_pair("sample-orchestrator")[0]])


class FakeViewport(S36):
    def test_a_visible_read_returns_only_the_viewport(self):
        self.status_of("w2:p2", "working")
        self.short()
        fake_herdr.set_screen(self.world, "w2:p2", "\n".join("line %d" % n for n in range(1, 21)) + "\n")
        code, out, _ = self.run_cli("peek", NAME, "--source", "visible", "--lines", "16")
        self.assertEqual(code, 0, out)
        self.assertEqual(out, "\n".join("line %d" % n for n in range(8, 21)) + "\n")


class ReadFailures(S36):
    def test_interrupt_plain_server_error_has_no_fallback_or_keys(self):
        self.status_of("w2:p2", "working")
        self.world["behaviors"]["agent_read_errors"] = {NAME: {"plain": "server_error"}}
        code, out, _ = self.run_cli("interrupt", NAME)
        self.assertEqual(code, 1, out)
        self.assertIn("reading its screen", out)
        self.assertEqual(self.argvs("agent", "read"), [PLAIN])
        self.assertEqual(self.argvs("agent", "send-keys"), [])

    def test_interrupt_visible_server_error_has_no_keys(self):
        self.short()
        self.status_of("w2:p2", "working")
        self.world["behaviors"]["agent_read_errors"] = {NAME: {"visible": "server_error"}}
        code, out, _ = self.run_cli("interrupt", NAME)
        self.assertEqual(code, 1, out)
        self.assertIn("reading its screen", out)
        self.assertEqual(self.argvs("agent", "read"), read_pair())
        self.assertEqual(self.argvs("agent", "send-keys"), [])

    def test_quit_plain_server_error_never_presses_enter_and_times_out_generically(self):
        fake_herdr.exit_dialog_on_quit(self.world, NAME)
        self.world["behaviors"]["agent_read_errors"] = {NAME: {"plain": "server_error"}}
        code, out, _ = self.run_cli("down", NAME)
        self.assertEqual(code, 1, out)
        self.assertIn("failed - agent sample-dev did not exit within 20000 ms after /exit; not retried, no keys sent, "
                      "seat left alone", out)
        self.assertEqual(self.argvs("agent", "read"), [PLAIN, PLAIN])
        self.assertEqual(self.argvs("agent", "send-keys"), [])

    def test_quit_visible_server_error_never_presses_enter_and_times_out_generically(self):
        self.short()
        fake_herdr.exit_dialog_on_quit(self.world, NAME)
        self.world["behaviors"]["agent_read_errors"] = {NAME: {"visible": "server_error"}}
        code, out, _ = self.run_cli("down", NAME)
        self.assertEqual(code, 1, out)
        self.assertIn("failed - agent sample-dev did not exit within 20000 ms after /exit; not retried, no keys sent, "
                      "seat left alone", out)
        self.assertEqual(self.argvs("agent", "read"), read_pair() + read_pair())
        self.assertEqual(self.argvs("agent", "send-keys"), [])


class WrapperScreens(S36):
    def test_short_pane_swap_handover_answers_the_exit_confirmation(self):
        name = "sample-orchestrator"
        self.status_of("w2:p1", "working")
        self.env.update(HERDR_PANE_ID="w2:p1", HERDR_WORKSPACE_ID="w2")
        self.write_fleet(self.read_fleet().replace(
            'name = "sample-orchestrator"\nkind = "claude"\nargs = ["--model", "claude-opus-5-5"]',
            'name = "sample-orchestrator"\nkind = "codex"\nargs = ["-m", "gpt-6-astra"]'))
        self.short("w2:p1")
        fake_herdr.exit_dialog_on_quit(self.world, name)
        code, out, _ = self.run_cli("swap", name, "--handover")
        self.assertEqual(code, 0, out)
        record = self.read_state()["handover"]
        pane = self.world["panes"][record["pane_id"]]
        env = dict(self.env, HERDR_PANE_ID=record["pane_id"], HERDR_WORKSPACE_ID="w2",
                   HERDR_PLUGIN_ENTRYPOINT_ID="handover")
        env.update(pane["env"])
        self.world["behaviors"]["wait_results"] = {name: ["done"]}
        code, out, _ = self.run_cli("handover", env=env)
        self.assertEqual(code, 0, out)
        self.assertIn("sample-orchestrator: quit - answered its exit confirmation (exit and stop tasks)", out)
        self.assertEqual(self.argvs("agent", "read"), read_pair(name))
        self.assertEqual(self.argvs("agent", "send-keys"), [["agent", "send-keys", name, "enter"]])

    def test_short_pane_manage_menu_names_a_held_dialog_without_a_key(self):
        self.short()
        fake_herdr.show_menu(self.world, "w2:p2", 1000)
        original = fake_herdr.Sim._agent_get
        checked = []

        def working_after_the_prompt_check(sim, args):
            result = original(sim, args)
            if sim.info.get("name") == NAME:
                checked.append(args[0])
                if len(checked) == 1:
                    sim.w["panes"]["w2:p2"]["agent"]["agent_status"] = "working"
            return result

        with mock.patch.object(fake_herdr.Sim, "_agent_get", working_after_the_prompt_check):
            code, out, _ = self.run_cli("menu", stdin="2\nrestart\nn\ny\n\n")
        self.assertIn('a dialog or a menu is on screen in w2:p2 ("Approaching rate limits")', out)
        self.assertEqual(self.argvs("agent", "read"), read_pair())
        self.assertEqual(self.argvs("agent", "send-keys"), [])


class Harness(S36):
    def write_log(self, *entries):
        with open(self.log_path, "a", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(json.dumps(entry) + "\n")

    def assert_caught(self, *entries):
        if os.path.exists(self.log_path):
            os.remove(self.log_path)
        self.write_log(*entries)
        with self.assertRaises(AssertionError):
            self.assert_safety()
        os.remove(self.log_path)

    def test_fallback_reads_can_authorize_enter_interrupt_keys_and_the_board(self):
        refused = {"argv": PLAIN, "exit": 1, "info": {"error": "agent_not_idle"}}
        visible = {"argv": VISIBLE, "exit": 0, "info": {}}
        enter = {"argv": ["agent", "send-keys", NAME, "enter"], "exit": 0,
                 "info": {"keys": ["enter"], "name": NAME, "kind": "claude", "exit_dialog": True}}
        working = {"argv": ["agent", "get", NAME], "exit": 0, "info": {"name": NAME, "status": "working"}}
        ctrl_c = {"argv": ["agent", "send-keys", NAME, "ctrl+c"], "exit": 0,
                  "info": {"keys": ["ctrl+c"], "name": NAME, "kind": "claude"}}
        listing = {"argv": ["agent", "list"], "exit": 0, "info": {"states": {NAME: "blocked"}}}
        self.write_log({"command": ["down"]}, refused, visible, enter,
                       {"command": ["interrupt", NAME]}, working, refused, visible, ctrl_c,
                       {"command": ["watch"]}, listing, refused, visible)
        self.assert_safety()
        os.remove(self.log_path)

    def test_visible_reads_without_the_exact_refusal_are_caught(self):
        visible = {"argv": VISIBLE, "exit": 0, "info": {}}
        successful = {"argv": PLAIN, "exit": 0, "info": {}}
        other_error = {"argv": PLAIN, "exit": 1, "info": {"error": "server_error"}}
        refused = {"argv": PLAIN, "exit": 1, "info": {"error": "agent_not_idle"}}
        self.assert_caught({"command": ["down"]}, visible)
        self.assert_caught({"command": ["restart"]}, successful, visible)
        self.assert_caught({"command": ["swap"]}, other_error, visible)
        self.assert_caught({"command": ["interrupt"]}, refused,
                           dict(visible, argv=VISIBLE[:4] + ["12", "--source", "visible"]))
        listing = {"argv": ["agent", "list"], "exit": 0, "info": {"states": {NAME: "idle"}}}
        self.assert_caught({"command": ["watch"]}, listing, refused, visible)

    def test_failed_reads_never_authorize_keys(self):
        working = {"argv": ["agent", "get", NAME], "exit": 0, "info": {"name": NAME, "status": "working"}}
        refused = {"argv": PLAIN, "exit": 1, "info": {"error": "agent_not_idle"}}
        failed = {"argv": PLAIN, "exit": 1, "info": {"error": "server_error"}}
        ctrl_c = {"argv": ["agent", "send-keys", NAME, "ctrl+c"], "exit": 0,
                  "info": {"keys": ["ctrl+c"], "name": NAME, "kind": "claude"}}
        enter = {"argv": ["agent", "send-keys", NAME, "enter"], "exit": 0,
                 "info": {"keys": ["enter"], "name": NAME, "kind": "claude", "exit_dialog": True}}
        self.assert_caught({"command": ["interrupt", NAME]}, working, refused, ctrl_c)
        self.assert_caught({"command": ["interrupt", NAME]}, working, failed, ctrl_c)
        self.assert_caught({"command": ["down"]}, refused, enter)


if __name__ == "__main__":
    unittest.main()
