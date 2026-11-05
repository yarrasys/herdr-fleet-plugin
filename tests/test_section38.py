"""Section 38: a Codex command's thread identifies its pane, using fake Herdr only."""

from __future__ import annotations

import json
import os
from unittest import mock

import fake_herdr
from support import LiveFleet
from herdr_fleet import messages, tui
from test_inbox import box, log_entries, put


THREAD = "sample-codex-thread"
PANE = "w2:p3"
OTHER = "w2:p2"
UNKNOWN = "a Codex command whose thread herdr does not know"
SEVERAL = "a Codex command whose thread herdr records on several panes"


class CodexCaller(LiveFleet):
    def setUp(self):
        super().setUp()
        self.env.update(CODEX_THREAD_ID=THREAD, HERDR_PANE_ID=OTHER, HERDR_WORKSPACE_ID="w1")
        self.world["panes"][PANE]["agent"]["agent_session"] = fake_herdr.claude_session(THREAD, "codex")

    def snapshot(self):
        result = {}
        for root, _, files in os.walk(self.repo):
            for name in files:
                path = os.path.join(root, name)
                with open(path, "rb") as handle:
                    result[os.path.relpath(path, self.repo)] = handle.read()
        return result

    def no_current(self):
        self.assertFalse(self.argvs("pane", "current"))

    def unknown(self):
        self.env["CODEX_THREAD_ID"] = "sample-unknown-thread"

    def several(self):
        fake_herdr.put_agent(self.world, OTHER, "codex", "sample-dev", session=fake_herdr.claude_session(THREAD, "codex"))

    def assert_identity_failure(self, reason, *args):
        before = self.snapshot()
        code, out, err = self.run_cli(*args)
        self.assertEqual(code, 2, out + err)
        self.assertIn(reason, err)
        self.assertIn("--as NAME", err)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.argvs("agent", "prompt"))
        self.no_current()

    def test_send_uses_thread_seat_and_reuses_preflight_list(self):
        code, out, err = self.run_cli("send", "sample-dev", "Synthetic report", "--queue")
        self.assertEqual(code, 0, out + err)
        self.assertIn("sender: sample-reviewer", out)
        self.assertEqual(log_entries(self.repo)[0]["from"], "sample-reviewer")
        self.no_current()
        self.assertEqual(len(self.argvs("agent", "list")), 1)

    def test_inbox_reason_names_both_panes(self):
        code, out, err = self.run_cli("inbox")
        self.assertEqual(code, 0, out + err)
        self.assertIn("named caller in w2:p3 (by its Codex thread; HERDR_PANE_ID w2:p2 is another pane)", out)
        self.assertEqual(len(self.argvs("agent", "list")), 1)
        self.no_current()

    def test_unknown_send_writes_nothing(self):
        self.unknown()
        self.assert_identity_failure(UNKNOWN, "send", "sample-dev", "Synthetic report", "--queue")

    def test_several_send_writes_nothing(self):
        self.several()
        self.assert_identity_failure(SEVERAL, "send", "sample-dev", "Synthetic report", "--queue")

    def test_unknown_inbox_writes_nothing(self):
        self.unknown()
        self.assert_identity_failure(UNKNOWN, "inbox", "--read")

    def test_several_inbox_writes_nothing(self):
        self.several()
        self.assert_identity_failure(SEVERAL, "inbox", "--read")

    def test_unknown_ack_writes_nothing(self):
        self.unknown()
        self.assert_identity_failure(UNKNOWN, "ack", "sample-dev")

    def test_several_ack_writes_nothing(self):
        self.several()
        self.assert_identity_failure(SEVERAL, "ack", "sample-dev")

    def test_explicit_sender_survives_unknown_thread(self):
        self.unknown()
        code, out, err = self.run_cli("send", "sample-dev", "Synthetic report", "--queue", "--as", "sample-reviewer")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(log_entries(self.repo)[0]["from"], "sample-reviewer")
        self.no_current()

    def test_explicit_inbox_stays_offline(self):
        self.unknown()
        code, out, err = self.run_cli("inbox", "--as", "sample-reviewer")
        self.assertEqual(code, 0, out + err)
        self.assertFalse(self.calls())

    def test_restart_refuses_thread_seat(self):
        code, out, err = self.run_cli("restart", "sample-reviewer")
        self.assertEqual(code, 3, out + err)
        self.assertIn("caller's own agent", out)
        self.assertFalse(self.argvs("agent", "prompt"))
        self.assertFalse(self.argvs("agent", "start"))
        self.no_current()

    def test_restart_allows_inherited_pane_seat(self):
        code, out, err = self.run_cli("restart", "sample-dev")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.argvs("agent", "start")[0][2], "sample-dev")
        self.no_current()

    def test_swap_refuses_thread_seat(self):
        self.world["panes"][PANE]["procs"] = [fake_herdr.agent_proc("codex", [], self.repo)]
        code, out, err = self.run_cli("swap", "sample-reviewer")
        self.assertEqual(code, 3, out + err)
        self.assertIn("caller's own agent", out)
        self.assertFalse(self.argvs("agent", "prompt"))

    def test_down_refuses_thread_seat(self):
        code, out, err = self.run_cli("down", "sample-reviewer")
        self.assertEqual(code, 3, out + err)
        self.assertIn("caller's own agent", out)
        self.assertFalse(self.argvs("agent", "prompt"))

    def test_up_me_adopts_thread_pane(self):
        self.world["panes"][PANE]["agent"]["name"] = None
        code, out, err = self.run_cli("up", "--me", "sample-reviewer", "--no-watch")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.argvs("agent", "rename"), [["agent", "rename", PANE, "sample-reviewer"]])
        self.assertFalse(self.argvs("agent", "start"))
        self.assertFalse(self.argvs("agent", "prompt"))
        self.no_current()

    def test_up_without_workspace_uses_thread_workspace(self):
        self.write_fleet(self.read_fleet().replace('workspace = "sample"', ""))
        code, out, err = self.run_cli("up", "--no-watch")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.read_state()["workspace_id"], "w2")
        self.no_current()

    def test_unknown_up_without_workspace_stops_before_writes(self):
        self.unknown()
        self.write_fleet(self.read_fleet().replace('workspace = "sample"', ""))
        before = self.snapshot()
        code, out, err = self.run_cli("up", "--no-watch")
        self.assertEqual(code, 2, out + err)
        self.assertIn(UNKNOWN, err)
        self.assertIn("set workspace", err)
        self.assertEqual(self.snapshot(), before)
        self.no_current()

    def test_unknown_me_stops_even_with_explicit_workspace(self):
        self.unknown()
        before = self.snapshot()
        code, out, err = self.run_cli("up", "--me", "sample-reviewer", "--no-watch")
        self.assertEqual(code, 2, out + err)
        self.assertIn(UNKNOWN, err)
        self.assertEqual(self.snapshot(), before)

    def test_unknown_join_stops_even_with_explicit_workspace(self):
        self.unknown()
        before = self.snapshot()
        code, out, err = self.run_cli("up", "--me", "sample-reviewer", "--join", "--no-watch")
        self.assertEqual(code, 2, out + err)
        self.assertIn(UNKNOWN, err)
        self.assertEqual(self.snapshot(), before)

    def test_inbox_reads_only_thread_mailbox(self):
        own = put(self.repo, "sample-reviewer", "Synthetic own request")
        other = put(self.repo, "sample-dev", "Synthetic other request")
        code, out, err = self.run_cli("inbox", "--read")
        self.assertEqual(code, 0, out + err)
        self.assertIn("Synthetic own request", out)
        self.assertNotIn("Synthetic other request", out)
        self.assertIn(own.id, box(self.repo, "sample-reviewer", "cur"))
        self.assertIn(other.id, box(self.repo, "sample-dev", "new"))
        self.no_current()

    def test_ack_settles_only_thread_seat_debt(self):
        for target in ("sample-reviewer", "sample-dev"):
            message = put(self.repo, target, "Synthetic review request", which="cur")
            messages.append(self.repo, messages.send_entry(message, delivery="sent"))
        code, out, err = self.run_cli("ack", "sample-orchestrator")
        self.assertEqual(code, 0, out + err)
        ack = log_entries(self.repo)[-1]
        self.assertEqual((ack["kind"], ack["from"]), ("ack", "sample-reviewer"))
        self.no_current()

    def test_handover_accepts_thread_seat(self):
        code, out, err = self.run_cli("restart", "sample-reviewer", "--handover")
        self.assertEqual(code, 0, out + err)
        self.assertIn("handover - waiting", out)
        self.assertEqual(self.read_state()["handover"]["seat"], "sample-reviewer")
        self.assertFalse(self.argvs("agent", "prompt"))
        self.no_current()

    def test_handover_refuses_inherited_pane_seat(self):
        code, out, err = self.run_cli("restart", "sample-dev", "--handover")
        self.assertEqual(code, 2, out + err)
        self.assertIn("for your own seat", err)
        self.assertFalse(self.argvs("plugin", "pane", "open"))

    def test_plugin_action_ignores_thread(self):
        self.env["HERDR_PLUGIN_ACTION_ID"] = "sample-status"
        code, out, err = self.run_cli("inbox")
        self.assertEqual(code, 0, out + err)
        self.assertIn("inbox: sample-dev", out)
        self.assertFalse(self.argvs("agent", "list"))
        self.assertTrue(self.argvs("pane", "current"))

    def test_plugin_pane_ignores_thread(self):
        self.env["HERDR_PLUGIN_ENTRYPOINT_ID"] = "manage"
        code, out, err = self.run_cli("inbox")
        self.assertEqual(code, 0, out + err)
        self.assertIn("inbox: sample-dev", out)
        self.assertFalse(self.argvs("agent", "list"))

    def test_absent_thread_keeps_existing_caller(self):
        self.env.pop("CODEX_THREAD_ID")
        code, out, err = self.run_cli("inbox")
        self.assertEqual(code, 0, out + err)
        self.assertIn("inbox: sample-dev", out)
        self.assertTrue(self.argvs("pane", "current"))

    def test_matching_pane_needs_no_mismatch_diagnostic(self):
        self.env["HERDR_PANE_ID"] = PANE
        code, out, err = self.run_cli("inbox")
        self.assertEqual(code, 0, out + err)
        self.assertIn("inbox: sample-reviewer", out)
        self.assertNotIn("is another pane", out)
        self.no_current()

    def test_thread_does_not_require_inherited_pane_or_workspace(self):
        self.env.pop("HERDR_PANE_ID")
        self.env.pop("HERDR_WORKSPACE_ID")
        code, out, err = self.run_cli("inbox")
        self.assertEqual(code, 0, out + err)
        self.assertIn("inbox: sample-reviewer", out)
        self.no_current()

    def test_wrong_session_source_is_unknown(self):
        self.world["panes"][PANE]["agent"]["agent_session"]["source"] = "herdr:claude"
        self.assert_identity_failure(UNKNOWN, "inbox")

    def test_null_session_is_unknown(self):
        self.world["panes"][PANE]["agent"]["agent_session"] = None
        self.assert_identity_failure(UNKNOWN, "inbox")

    def test_codex_hook_cannot_read_inherited_claude_mailbox(self):
        queued = put(self.repo, "sample-dev", "Synthetic Claude request")
        before = self.snapshot()
        code, out, err = self.run_cli("inbox", "--hook", "stop", stdin=json.dumps({"cwd": self.repo}))
        self.assertEqual((code, out, err), (0, "", ""))
        self.assertEqual(self.snapshot(), before)
        self.assertIn(queued.id, box(self.repo, "sample-dev", "new"))
        self.assertFalse(self.calls())

    def test_doctor_explains_thread_resolution(self):
        code, out, err = self.run_cli("doctor")
        self.assertEqual(code, 0, out + err)
        self.assertIn("caller: by Codex thread", out)
        self.assertIn(PANE, out)
        self.no_current()

    def test_doctor_explains_unknown_thread(self):
        self.unknown()
        code, out, err = self.run_cli("doctor")
        self.assertEqual(code, 0, out + err)
        self.assertIn("caller: by Codex thread", out)
        self.assertIn(UNKNOWN, out)
        self.no_current()

    def unrecorded_caller(self):
        self.unknown()
        self.world["panes"][PANE]["agent"].pop("agent_session", None)

    def assert_possible_caller_refused(self, *args, **kwargs):
        code, out, err = self.run_cli(*args, **kwargs)
        self.assertEqual(code, 3, out + err)
        self.assertIn("refused - sample-reviewer may be the caller's own agent (%s); "
                      "run this from another pane" % UNKNOWN, out)
        self.assertFalse(self.argvs("agent", "prompt"))
        self.assertFalse(self.argvs("agent", "start"))
        self.assertFalse(self.argvs("agent", "wait"))
        self.assertFalse(self.argvs("agent", "send-keys"))

    def test_unknown_interrupt_as_self_cannot_key_possible_caller(self):
        self.unrecorded_caller()
        self.status_of(PANE, "working")
        self.write_fleet(self.read_fleet() + '\n[interrupt]\ncodex = ["esc"]\n')
        self.assert_possible_caller_refused("interrupt", "sample-reviewer", "--as", "sample-reviewer")

    def test_unknown_restart_wait_cannot_quit_idle_possible_caller(self):
        self.unrecorded_caller()
        self.assert_possible_caller_refused("restart", "sample-reviewer", "--wait", "1000")

    def test_unknown_restart_cannot_quit_unrecorded_codex(self):
        self.unrecorded_caller()
        self.assert_possible_caller_refused("restart", "sample-reviewer")

    def test_unknown_swap_refuses_unchanged_possible_caller(self):
        self.unrecorded_caller()
        self.assert_possible_caller_refused("swap", "sample-reviewer")

    def test_unknown_fallback_refuses_possible_caller(self):
        self.unrecorded_caller()
        fleet = self.read_fleet().replace('kind = "codex"\nargs = ["-m", "gpt-6-astra"]',
                                         'tier = "sample"\nprefer = ["codex", "claude"]')
        self.write_fleet(fleet + '\n[tiers.sample]\ncodex = ["-m", "gpt-6-astra"]\nclaude = []\n')
        self.assert_possible_caller_refused("swap", "sample-reviewer", "--fallback", "--wait", "1000")

    def test_unknown_down_cannot_quit_possible_caller(self):
        self.unrecorded_caller()
        self.assert_possible_caller_refused("down", "sample-reviewer", "--wait", "1000")

    def test_unknown_restart_refuses_codex_record_for_another_thread(self):
        self.unknown()  # THREAD may be a stale record left by this pane's previous thread
        self.assert_possible_caller_refused("restart", "sample-reviewer", "--wait", "1000")

    def test_unknown_interrupt_refuses_codex_record_for_another_thread(self):
        self.unknown()
        self.status_of(PANE, "working")
        self.write_fleet(self.read_fleet() + '\n[interrupt]\ncodex = ["esc"]\n')
        self.assert_possible_caller_refused("interrupt", "sample-reviewer", "--as", "sample-reviewer")

    def test_unknown_thread_keeps_claude_seat_reachable(self):
        self.unknown()
        code, out, err = self.run_cli("restart", "sample-dev")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.prompts_to("sample-dev")[0], "/exit")

    def test_several_threads_keep_claude_seat_reachable(self):
        self.several()
        code, out, err = self.run_cli("restart", "sample-orchestrator")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.prompts_to("sample-orchestrator")[0], "/exit")

    def test_several_threads_protect_codex_seat_with_another_record(self):
        self.several()
        fake_herdr.add_pane(self.world, "w2:p4", "w2", self.repo, created=True)
        fake_herdr.put_agent(self.world, "w2:p4", "codex", "sample-other",
                             session=fake_herdr.claude_session("sample-older-thread", "codex"))
        self.write_fleet(self.read_fleet() + '\n[[agents]]\nname = "sample-other"\nkind = "codex"\n')
        code, out, err = self.run_cli("restart", "sample-dev", "sample-reviewer", "sample-other", "--wait", "1000")
        self.assertEqual(code, 3, out + err)
        for name in ("sample-dev", "sample-reviewer", "sample-other"):
            self.assertIn("refused - %s may be the caller's own agent (%s); "
                          "run this from another pane" % (name, SEVERAL), out)
        self.assertFalse(self.argvs("agent", "prompt"))
        self.assertFalse(self.argvs("agent", "wait"))
        self.assertFalse(self.argvs("agent", "start"))

    def test_several_matches_are_each_refused(self):
        self.several()
        code, out, err = self.run_cli("restart", "sample-dev", "sample-reviewer", "--wait", "1000")
        self.assertEqual(code, 3, out + err)
        for name in ("sample-dev", "sample-reviewer"):
            self.assertIn("refused - %s may be the caller's own agent (%s); "
                          "run this from another pane" % (name, SEVERAL), out)
        self.assertFalse(self.argvs("agent", "prompt"))
        self.assertFalse(self.argvs("agent", "wait"))
        self.assertFalse(self.argvs("agent", "start"))

    def test_unknown_non_codex_session_is_a_possible_caller(self):
        self.unknown()
        self.world["panes"][PANE]["agent"]["agent_session"] = fake_herdr.claude_session(THREAD)
        self.assert_possible_caller_refused("restart", "sample-reviewer")

    def test_unknown_bulk_ack_says_to_use_owners_shell(self):
        self.unknown()
        before = self.snapshot()
        code, out, err = self.run_cli("ack", "--older-than", "2h", "--reason", "Synthetic cleanup",
                                     "--as", "owner")
        self.assertEqual(code, 2, out + err)
        self.assertIn(UNKNOWN, err)
        self.assertIn("owner's shell", err)
        self.assertNotIn("pass --as", err)
        self.assertEqual(self.snapshot(), before)

    def test_unknown_tile_without_workspace_gives_codex_reason(self):
        self.unknown()
        self.write_fleet(self.read_fleet().replace('workspace = "sample"', ""))
        before = self.snapshot()
        code, out, err = self.run_cli("tile")
        self.assertEqual(code, 2, out + err)
        self.assertIn(UNKNOWN, err)
        self.assertIn("set workspace", err)
        self.assertNotIn("not run from a herdr pane", err)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.argvs("pane", "move"))

    def test_cli_codex_zoom_uses_recorded_strip(self):
        herdr = mock.Mock()
        self.assertEqual(tui.zoom(herdr, self.env, "w2:p4"), "w2:p4")
        herdr.pane_zoom_toggle.assert_called_once_with("w2:p4")

    def test_cli_codex_zoom_without_recorded_strip_does_nothing(self):
        herdr = mock.Mock()
        self.assertIsNone(tui.zoom(herdr, self.env, None))
        herdr.pane_zoom_toggle.assert_not_called()

    def test_plugin_codex_environment_keeps_own_strip_for_zoom(self):
        herdr = mock.Mock()
        env = dict(self.env, HERDR_PLUGIN_ENTRYPOINT_ID="watch")
        self.assertEqual(tui.zoom(herdr, env, "w2:p4"), OTHER)
        herdr.pane_zoom_toggle.assert_called_once_with(OTHER)

    def test_harness_rejects_quit_prompt_to_unknown_possible_caller(self):
        self.unrecorded_caller()
        with mock.patch("herdr_fleet.ops._caller_refusal", return_value=None):
            self.run_cli("restart", "sample-reviewer")
        try:
            with self.assertRaisesRegex(AssertionError, "quit prompt reached a possible caller"):
                self.assert_safety()
        finally:
            os.remove(self.log_path)

    def test_harness_rejects_keys_to_unknown_possible_caller(self):
        self.unrecorded_caller()
        self.status_of(PANE, "working")
        self.write_fleet(self.read_fleet() + '\n[interrupt]\ncodex = ["esc"]\n')
        with mock.patch("herdr_fleet.ops._caller_refusal", return_value=None):
            self.run_cli("interrupt", "sample-reviewer", "--as", "sample-reviewer")
        try:
            with self.assertRaisesRegex(AssertionError, "key or quit prompt reached a possible caller"):
                self.assert_safety()
        finally:
            os.remove(self.log_path)

    def test_harness_rejects_keys_to_possible_caller_with_another_record(self):
        self.unknown()
        self.status_of(PANE, "working")
        self.write_fleet(self.read_fleet() + '\n[interrupt]\ncodex = ["esc"]\n')
        with mock.patch("herdr_fleet.ops._caller_refusal", return_value=None):
            self.run_cli("interrupt", "sample-reviewer", "--as", "sample-reviewer")
        try:
            with self.assertRaisesRegex(AssertionError, "key or quit prompt reached a possible caller"):
                self.assert_safety()
        finally:
            os.remove(self.log_path)

    def test_harness_rejects_start_into_each_ambiguous_caller_pane(self):
        self.several()
        for pane in (OTHER, PANE):
            self.run_cli("restart", "sample-dev", "sample-reviewer")
            with open(self.log_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps({"argv": ["agent", "start", "sample-reviewer", "--kind", "codex",
                                                  "--pane", pane, "--"], "exit": 1, "info": {}}) + "\n")
            try:
                with self.assertRaisesRegex(AssertionError, "started into the caller's pane"):
                    self.assert_safety()
            finally:
                os.remove(self.log_path)
