"""Section 31: two synthetic fleets, real CLI/mailbox seams, fake Herdr only."""
from __future__ import annotations

import json
import io
import os
import datetime
import threading
import subprocess
import sys
import shutil
import unittest
from unittest import mock

import fake_herdr
from support import CALLER_PANE, LiveFleet
from herdr_fleet import build_identity, crossfleet, gitinfo, inbox, mailroom, messages, watch
from test_inbox import put, log_entries
from test_section24 import BoardCase


class FleetPair(LiveFleet):
    def setUp(self):
        super().setUp()
        self.other = os.path.join(self.tmp, "partner")
        os.makedirs(os.path.join(self.other, ".herdr"))
        self.destination = os.path.join(self.other, ".herdr", "fleet.toml")
        with open(self.destination, "w") as handle:
            handle.write('workspace="partner"\n[[agents]]\nname="peer-lead"\nkind="claude"\n')
        fake_herdr.add_workspace(self.world, "w3", "partner", self.other)
        fake_herdr.put_agent(self.world, "w3:p1", "claude", "peer-lead")
        self.write_fleet(self.read_fleet() + '\n[[contacts]]\nname="partner"\nmatch={name="peer-lead"}\n')

    def tree(self):
        result = {}
        for root in (self.repo, self.other):
            for folder, _, files in os.walk(root):
                for name in files:
                    path = os.path.join(folder, name)
                    with open(path, "rb") as handle:
                        result[path] = handle.read()
        return result

    def pending(self):
        return inbox.read_box(self.other, "peer-lead", inbox.NEW)

    def send_queue(self, *args):
        return self.run_cli("send", "--as", "sample-orchestrator", "--queue", *args, "partner", "Synthetic review")

    def pull(self, *args):
        return self.run_cli("inbox", "--as", "peer-lead", "--read", "--fleet", self.destination, *args)


class RoutedSend(FleetPair):
    def test_contact_alias_has_one_destination_owned_pending_message(self):
        code, out, err = self.run_cli("send", "--as", "sample-orchestrator", "--queue", "partner", "Synthetic review")
        self.assertEqual(code, 0, out + err)
        queued = inbox.read_box(self.other, "peer-lead", inbox.NEW)
        self.assertEqual(len(queued), 1)
        self.assertEqual((queued[0].to, queued[0].sender, queued[0].body),
                         ("peer-lead", "sample-orchestrator", "Synthetic review"))
        self.assertFalse(inbox.has_queued(self.repo, "partner"))
        self.assertFalse(inbox.has_queued(self.repo, "peer-lead"))
        self.assertIn(self.destination, out)
        self.assertIn("partner -> peer-lead", out)

    def test_dynamic_agent_queues_in_its_declared_fleet(self):
        self.run_cli("send", "--queue", "peer-lead", "Synthetic dynamic target")
        self.assertEqual(len(inbox.read_box(self.other, "peer-lead", inbox.NEW)), 1)
        self.assertFalse(inbox.has_queued(self.repo, "peer-lead"))

    def test_active_destination_wait_receives_fyi_without_native_wake(self):
        original_sleep = self.clock.sleep
        sent = []
        def arrive(seconds):
            if not sent:
                sent.append(True)
                code, out, err = self.run_cli("send", "--fyi", "--queue", "partner", "FYI: Synthetic report")
                self.assertEqual(code, 0, out + err)
            original_sleep(seconds)
        with mock.patch.object(self.clock, "sleep", side_effect=arrive):
            code, out, err = self.run_cli("inbox", "--as", "peer-lead", "--wait", "--timeout", "3000",
                                         "--fleet", self.destination)
        self.assertEqual(code, 0, out + err)
        self.assertIn("FYI: Synthetic report", out)
        self.assertFalse(self.argvs("agent", "prompt"))

    def test_plain_idle_delivery_uses_destination_engine_and_logs_one_debt_each(self):
        code, out, err = self.run_cli("send", "--as", "sample-orchestrator", "partner", "Synthetic review")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(len(self.prompts_to("peer-lead")), 1)
        self.assertFalse(self.pending())
        for repo in (self.repo, self.other):
            entries = log_entries(repo)
            self.assertEqual([(e["kind"], e["to"]) for e in entries], [("send", "peer-lead"), ("delivered", "peer-lead")])
            self.assertEqual(len(messages.owed(entries)), 1)
        self.assertEqual(log_entries(self.repo), log_entries(self.other))

    def test_source_receipt_is_not_pending_delivered_history_or_redeliverable(self):
        self.send_queue()
        message = self.pending()[0]
        receipt = inbox.read_box(self.repo, "peer-lead", inbox.CUR)[0]
        self.assertTrue(receipt.history_only)
        self.assertEqual(receipt.id, message.id)
        data = json.loads(self.run_cli("inbox", "--as", "peer-lead", "--history", "sample-orchestrator", "--json")[1])
        self.assertEqual(data, [])
        self.assertEqual(messages.owed(log_entries(self.repo)), [])
        code, out, err = self.run_cli("inbox", "--redeliver", "#" + message.short)
        self.assertEqual(code, 2, out + err)
        self.assertIn("nonpending source receipt", err)
        self.assertEqual(len(self.pending()), 1)
        self.pull()
        data = json.loads(self.run_cli("inbox", "--as", "peer-lead", "--history", "sample-orchestrator", "--json")[1])
        self.assertEqual(data[0]["body"], "Synthetic review")

    def test_working_blocked_unknown_and_typing_hold_only_at_destination(self):
        for status in ("working", "blocked", "unknown", "typing"):
            with self.subTest(status=status):
                self.status_of("w3:p1", "idle" if status == "typing" else status)
                fake_herdr.type_in(self.world, "w3:p1", "synthetic draft" if status == "typing" else "")
                code, out, err = self.run_cli("send", "partner", "Synthetic held request")
                self.assertEqual(code, 0, out + err)
                self.assertEqual(len(self.pending()), 1)
                self.assertFalse(inbox.has_queued(self.repo, "partner"))
                self.assertFalse(inbox.has_queued(self.repo, "peer-lead"))
                self.assertEqual(len(self.prompts_to("peer-lead")), 0)
                self.pull()

    def test_explicit_now_keeps_ordinary_working_and_blocked_policy(self):
        self.status_of("w3:p1", "working")
        code, out, err = self.run_cli("send", "--now", "partner", "Synthetic explicit request")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(len(self.prompts_to("peer-lead")), 1)
        self.status_of("w3:p1", "blocked")
        code, out, err = self.run_cli("send", "--now", "partner", "Synthetic refused request")
        self.assertEqual(code, 1, out + err)
        self.assertFalse(self.pending())
        for repo in (self.repo, self.other):
            self.assertEqual(log_entries(repo)[-1]["kind"], "dropped")

    def test_fyi_defers_native_delivery_but_reports_pull_availability(self):
        code, out, err = self.run_cli("send", "--fyi", "partner", "FYI: Synthetic report")
        self.assertEqual(code, 0, out + err)
        self.assertIn("pull inbox immediately", out)
        self.assertIn("native delivery remains deferred", out)
        self.assertEqual(len(self.pending()), 1)
        self.assertFalse(self.prompts_to("peer-lead"))

    def test_metadata_and_body_are_identical_in_authoritative_file_and_receipt(self):
        prior = put(self.repo, "sample-orchestrator", "Synthetic request to answer")
        body = "\nSynthetic line one.\r\n\nLine two.  \n"
        code, out, err = self.run_cli("send", "--as", "sample-orchestrator", "--queue", "--fyi", "--urgent",
                                     "--ttl", "2h", "--reply-to", "#" + prior.short, "partner", body)
        self.assertEqual(code, 0, out + err)
        queued = self.pending()[0]
        receipt = inbox.read_box(self.repo, "peer-lead", inbox.CUR)[0]
        self.assertTrue(crossfleet.receipt_matches(receipt, queued))
        self.assertEqual(queued.body, body)
        self.assertEqual(receipt.body, body)
        self.assertEqual((queued.reply_to, queued.priority, queued.fyi, queued.mode), (prior.id, 1, True, "queue"))

    def test_unconfirmed_submission_is_recorded_both_sides_and_never_reprompted(self):
        self.world["behaviors"]["prompt_errors"] = {"peer-lead": "timeout"}
        code, out, err = self.run_cli("send", "partner", "Synthetic ambiguous submission")
        self.assertEqual(code, 1, out + err)
        self.assertFalse(self.pending())
        self.assertEqual(len(self.argvs("agent", "prompt", "peer-lead")), 1)
        for repo in (self.repo, self.other):
            self.assertEqual(log_entries(repo)[-1]["delivery"], "unconfirmed")
            self.assertEqual(len(messages.owed(log_entries(repo))), 1)
        self.run_cli("watch", "--once", "--fleet", self.destination)
        self.pull()
        self.assertEqual(len(self.argvs("agent", "prompt", "peer-lead")), 1)

    def test_ttl_and_hook_pull_kickoff_drop_receipts_reach_origin(self):
        for path in ("expiry", "pull", "hook", "kickoff", "drop"):
            with self.subTest(path=path):
                self.send_queue("--ttl", "1m")
                message = self.pending()[0]
                if path == "expiry":
                    later = inbox.now_utc() + datetime.timedelta(minutes=2)
                    with mock.patch.object(inbox, "now_utc", return_value=later):
                        self.run_cli("deliver", "--fleet", self.destination)
                elif path == "pull":
                    self.pull()
                elif path == "hook":
                    self.run_cli("inbox", "--hook", "stop", "--fleet", self.destination,
                                 env=dict(self.env, HERDR_PANE_ID="w3:p1"),
                                 stdin=json.dumps({"cwd": self.other, "session_id": "synthetic"}))
                else:
                    self.status_of("w3:p1", "idle")
                    with open(self.destination) as handle:
                        configured = handle.read()
                    if "kickoff_text" not in configured:
                        with open(self.destination, "a") as handle:
                            handle.write('kickoff_text="Synthetic kickoff {inbox}"\n')
                    self.run_cli("kickoff", "peer-lead", "--fleet", self.destination,
                                 *(["--drop-inbox"] if path == "drop" else []))
                self.assertFalse(self.pending())
                expected = "expired" if path == "expiry" else "dropped" if path == "drop" else "delivered"
                for repo in (self.repo, self.other):
                    terminal = [e for e in log_entries(repo) if e.get("id") == message.id and e["kind"] == expected]
                    self.assertEqual(len(terminal), 1)

    def test_exact_reply_and_exact_ack_settle_debt_in_both_fleets(self):
        for action in ("reply", "ack"):
            self.send_queue()
            original = self.pending()[0]
            self.pull()
            if action == "reply":
                code, out, err = self.run_cli("send", "--as", "peer-lead", "--fyi", "--reply-to", "#" + original.short,
                                             "sample-orchestrator", "FYI: Synthetic reply", "--fleet", self.destination)
            else:
                code, out, err = self.run_cli("ack", "--as", "peer-lead", "#" + original.short, "--fleet", self.destination)
            self.assertEqual(code, 0, out + err)
            for repo in (self.repo, self.other):
                self.assertEqual(messages.owed(log_entries(repo)), [])

    def test_exact_local_owner_reply_settles_origin_without_routing_owner_mail(self):
        code, out, err = self.run_cli("send", "--as", "owner", "--queue", "partner", "Synthetic owner request")
        self.assertEqual(code, 0, out + err)
        original = self.pending()[0]
        self.pull()
        self.assertEqual(len(messages.owed(log_entries(self.repo))), 1)
        before = self.tree()
        code, out, err = self.run_cli("send", "--as", "peer-lead", "--queue", "--fyi", "--dry-run",
                                     "--reply-to", "#" + original.short, "owner", "Synthetic exact local reply",
                                     "--fleet", self.destination)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.tree(), before)
        code, out, err = self.run_cli("send", "--as", "peer-lead", "--queue", "--fyi",
                                     "--reply-to", "#" + original.short, "owner", "Synthetic exact local reply",
                                     "--fleet", self.destination)
        self.assertEqual(code, 0, out + err)
        local = inbox.read_box(self.other, "owner", inbox.NEW)
        self.assertEqual(len(local), 1)
        self.assertFalse(inbox.has_queued(self.repo, "owner"))
        for root in (self.repo, self.other):
            self.assertEqual(messages.owed(log_entries(root)), [])
        receipt = log_entries(self.repo)[-1]
        self.assertEqual((receipt["kind"], receipt["id"], receipt["via"], receipt["reply_id"]),
                         ("ack", original.id, "reply", local[0].id))
        self.assertFalse(self.argvs("agent", "prompt"))

    def test_route_dry_run_describes_alias_sender_selection_and_has_zero_writes(self):
        before = self.tree()
        code, out, err = self.run_cli("send", "--dry-run", "--queue", "--as", "sample-orchestrator", "partner", "Synthetic dry run")
        self.assertEqual(code, 0, out + err)
        self.assertIn("sender: sample-orchestrator", out)
        self.assertIn("discovered", out)
        self.assertIn("partner -> peer-lead", out)
        self.assertIn(self.destination, out)
        self.assertEqual(self.tree(), before)


class RouteFailures(FleetPair):
    def test_custom_source_file_without_conventional_config_stays_source_owned(self):
        custom = os.path.join(self.repo, "custom-fleet.toml")
        os.rename(os.path.join(self.repo, ".herdr", "fleet.toml"), custom)
        for requested in ("partner", "peer-lead"):
            with self.subTest(requested=requested):
                before = self.tree()
                code, out, err = self.run_cli("send", "--as", "sample-orchestrator", "--queue", "--dry-run",
                                             "--fleet", custom, requested, "Synthetic custom-source request")
                self.assertEqual(code, 0, out + err)
                proposed = out
                self.assertEqual(self.tree(), before)
                code, out, err = self.run_cli("send", "--as", "sample-orchestrator", "--queue", "--fleet", custom,
                                             requested, "Synthetic custom-source request")
                self.assertEqual(code, 0, out + err)
                self.assertIn("source-owned", out)
                self.assertIn("source fleet", out)
                self.assertIn("source-owned", proposed)
                self.assertIn("source fleet", proposed)
                self.assertIn(custom, out)
                queued = inbox.read_box(self.repo, requested, inbox.NEW)
                self.assertEqual(len(queued), 1)
                self.assertEqual(queued[0].sender, "sample-orchestrator")
                self.assertFalse(queued[0].origin_fleet)
                self.assertFalse(self.pending())
                destination_before = {path: value for path, value in before.items()
                                      if path.startswith(self.other + os.sep)}
                destination_after = {path: value for path, value in self.tree().items()
                                     if path.startswith(self.other + os.sep)}
                self.assertEqual(destination_after, destination_before)
                self.assertFalse(self.argvs("agent", "prompt"))

    def test_invalid_destination_or_missing_declared_seat_falls_back_with_reason(self):
        for text in ('not valid [', '[[agents]]\nname="another-seat"\nkind="claude"\n'):
            with self.subTest(config=text):
                with open(self.destination, "w") as handle:
                    handle.write(text)
                code, out, err = self.send_queue()
                self.assertEqual(code, 0, out + err)
                self.assertIn("source-owned", out)
                self.assertTrue(inbox.has_queued(self.repo, "partner"))
                self.assertFalse(self.pending())
        self.assertFalse(os.path.exists(os.path.join(self.other, ".herdr", "messages.jsonl")))

    def test_unnamed_contact_and_same_root_do_not_infer_or_invent_a_destination(self):
        self.world["panes"]["w3:p1"]["agent"]["name"] = None
        self.write_fleet(self.read_fleet().replace('match={name="peer-lead"}', 'match={cwd=%s}' % json.dumps(self.other)))
        code, out, err = self.send_queue()
        self.assertEqual(code, 0, out + err)
        self.assertIn("no usable canonical name", out)
        self.assertFalse(self.pending())
        self.assertTrue(inbox.has_queued(self.repo, "partner"))

    def test_selected_local_seat_is_not_rerouted_when_working_elsewhere(self):
        self.world["panes"]["w2:p2"]["agent"]["cwd"] = self.other
        self.world["panes"]["w2:p2"]["cwd"] = self.other
        with open(self.destination, "a") as handle:
            handle.write('[[agents]]\nname="sample-dev"\nkind="claude"\n')
        code, out, err = self.run_cli("send", "--queue", "sample-dev", "Synthetic local request")
        self.assertEqual(code, 0, out + err)
        self.assertTrue(inbox.has_queued(self.repo, "sample-dev"))
        self.assertFalse(inbox.has_queued(self.other, "sample-dev"))

    def test_id_short_collision_is_checked_in_both_trees(self):
        put(self.repo, "sample-dev", "Synthetic prior", ident="20260101T000000Z-abc123")
        put(self.other, "peer-lead", "Synthetic prior", ident="20260102T000000Z-def456")
        with mock.patch.object(inbox.secrets, "token_hex", side_effect=["abc123", "def456", "123abc"]):
            self.send_queue()
        routed = [m for m in self.pending() if m.origin_fleet]
        self.assertEqual([m.short for m in routed], ["123abc"])

    def test_destination_write_failure_never_falls_back_to_source_pending(self):
        real = inbox.write
        def fail(repo, *args, **kwargs):
            if repo == os.path.realpath(self.other):
                raise OSError("synthetic destination failure")
            return real(repo, *args, **kwargs)
        before = self.tree()
        with mock.patch.object(inbox, "write", side_effect=fail):
            code, out, err = self.send_queue()
        self.assertEqual(code, 1, out + err)
        self.assertIn("no source pending copy", out)
        self.assertFalse(self.pending())
        self.assertFalse(inbox.has_queued(self.repo, "peer-lead"))
        self.assertFalse(inbox.has_queued(self.repo, "partner"))
        self.assertFalse(log_entries(self.repo))

    def test_error_after_atomic_destination_write_reports_retained_id_not_false_failure(self):
        real = inbox.write
        def fail(repo, *args, **kwargs):
            result = real(repo, *args, **kwargs)
            if repo == os.path.realpath(self.other):
                raise OSError("synthetic failure after atomic replace")
            return result
        with mock.patch.object(inbox, "write", side_effect=fail):
            code, out, err = self.send_queue()
        self.assertEqual(code, 1, out + err)
        self.assertEqual(len(self.pending()), 1)
        self.assertIn("original destination message retained", out)
        self.assertIn(self.pending()[0].short, out)
        self.assertFalse(inbox.has_queued(self.repo, "peer-lead"))

    def test_existing_unrelated_fleet_header_without_receipt_is_not_write_authority(self):
        self.send_queue()
        message = self.pending()[0]
        unrelated = os.path.join(self.tmp, "unrelated")
        os.makedirs(os.path.join(unrelated, ".herdr"))
        with open(os.path.join(unrelated, ".herdr", "fleet.toml"), "w") as handle:
            handle.write('[[agents]]\nname="unrelated-seat"\nkind="claude"\n')
        message.origin_fleet = os.path.realpath(unrelated)
        with open(message.path, "w") as handle:
            handle.write(inbox.render_file(message))
        code, out, err = self.pull()
        self.assertEqual(code, 0, out + err)
        self.assertIn("cannot mirror", err)
        self.assertFalse(os.path.exists(os.path.join(unrelated, ".herdr", "messages.jsonl")))
        self.assertFalse(os.path.exists(os.path.join(unrelated, ".herdr", "inbox")))

    def test_symlinked_origin_receipt_is_rejected_and_terminal_still_logged_locally(self):
        self.send_queue()
        receipt = inbox.read_box(self.repo, "peer-lead", inbox.CUR)[0]
        elsewhere = os.path.join(self.tmp, "receipt.md")
        os.rename(receipt.path, elsewhere)
        os.symlink(elsewhere, receipt.path)
        code, out, err = self.pull()
        self.assertEqual(code, 0, out + err)
        self.assertIn("redirected source receipt", err)
        self.assertEqual(log_entries(self.other)[-1]["kind"], "delivered")
        self.assertEqual(log_entries(self.repo)[-1]["kind"], "send")

    def test_source_receipt_failure_retains_exactly_one_destination_id(self):
        real = inbox.write
        def fail(repo, *args, **kwargs):
            if repo == os.path.realpath(self.repo):
                raise OSError("synthetic source receipt failure")
            return real(repo, *args, **kwargs)
        with mock.patch.object(inbox, "write", side_effect=fail):
            code, out, err = self.send_queue()
        self.assertEqual(code, 1, out + err)
        chosen = self.pending()[0].id
        self.assertIn(chosen[-6:], out)
        self.assertIn("original destination message retained", out)
        code, out, err = self.pull()
        self.assertEqual(code, 0, out + err)
        self.assertIn("cannot mirror", err)
        self.assertFalse(self.pending())
        self.assertFalse(self.prompts_to("peer-lead"))

    def test_source_log_failure_can_recover_debt_from_valid_receipt(self):
        real = messages.append
        def fail(repo, entry):
            if repo == os.path.realpath(self.repo):
                raise OSError("synthetic source log failure")
            return real(repo, entry)
        with mock.patch.object(messages, "append", side_effect=fail):
            self.assertEqual(self.send_queue()[0], 1)
        self.pull()
        self.assertEqual(len(messages.owed(log_entries(self.repo))), 1)
        self.assertEqual(len(messages.owed(log_entries(self.other))), 1)
        self.assertEqual(log_entries(self.repo), log_entries(self.other))

    def test_receipt_mirror_failure_is_reported_without_reprompt(self):
        self.send_queue()
        real = messages.append
        def fail(repo, entry):
            if repo == os.path.realpath(self.repo) and entry.get("kind") == "delivered":
                raise OSError("synthetic mirror failure")
            return real(repo, entry)
        with mock.patch.object(messages, "append", side_effect=fail):
            code, out, err = self.pull()
        self.assertEqual(code, 0, out + err)
        self.assertIn("cannot mirror route receipt", err)
        self.pull()
        self.assertFalse(self.pending())
        self.assertFalse(self.argvs("agent", "prompt"))

    def test_untrusted_header_root_never_creates_another_fleet_or_mailbox(self):
        self.send_queue()
        message = self.pending()[0]
        unexpected = os.path.join(self.tmp, "unrelated")
        os.makedirs(unexpected)
        message.origin_fleet = unexpected
        with open(message.path, "w") as handle:
            handle.write(inbox.render_file(message))
        code, out, err = self.pull()
        self.assertEqual(code, 0, out + err)
        self.assertIn("cannot mirror", err)
        self.assertFalse(os.path.exists(os.path.join(unexpected, ".herdr")))

    def test_redirected_destination_inbox_falls_back_before_any_external_write(self):
        unrelated = os.path.join(self.tmp, "unrelated")
        os.makedirs(unrelated)
        os.symlink(unrelated, os.path.join(self.other, ".herdr", "inbox"))
        code, out, err = self.send_queue()
        self.assertEqual(code, 0, out + err)
        self.assertIn("source-owned", out)
        self.assertEqual(os.listdir(unrelated), [])


class InvocationIdentity(FleetPair):
    def test_missing_inside_herdr_caller_does_not_fall_back_to_owner(self):
        env = dict(self.env)
        env.pop("HERDR_PANE_ID")
        before = self.tree()
        code, out, err = self.run_cli("inbox", env=env)
        self.assertEqual(code, 2, out + err)
        self.assertIn("--as NAME", err)
        self.assertEqual(self.tree(), before)

    def test_focused_owner_pane_is_never_used_as_missing_caller_identity(self):
        env = dict(self.env, HERDR_PLUGIN_CONTEXT_JSON=json.dumps({"focused_pane_id": CALLER_PANE}))
        env.pop("HERDR_PANE_ID")
        code, out, err = self.run_cli("inbox", "--fleet", os.path.join(self.repo, ".herdr", "fleet.toml"), env=env)
        self.assertEqual(code, 2, out + err)
        self.assertIn("focused-pane context is not caller identity", err)
        self.assertFalse(self.argvs("agent", "get"))

    def test_lookup_error_never_impersonates_owner_even_with_a_shell_process(self):
        self.world["panes"][CALLER_PANE]["agent"] = None
        self.world["panes"][CALLER_PANE]["procs"] = [dict(fake_herdr.SHELL)]
        self.world["canned"].append({"prefix": ["agent", "get", CALLER_PANE], "exit": 1,
                                     "stderr": {"error": {"code": "timeout", "message": "synthetic lookup failure"}}})
        before = self.tree()
        code, out, err = self.run_cli("inbox")
        self.assertEqual(code, 2, out + err)
        self.assertIn("lookup failed", err)
        self.assertIn("--as NAME", err)
        self.assertEqual(self.tree(), before)

    def test_owner_is_allowed_outside_herdr_or_at_a_verified_shell(self):
        self.world["panes"][CALLER_PANE]["agent"] = None
        self.world["panes"][CALLER_PANE]["procs"] = [dict(fake_herdr.SHELL)]
        code, out, err = self.run_cli("inbox")
        self.assertEqual(code, 0, out + err)
        self.assertIn("inbox: owner", out)
        self.assertIn("verified shell", out)
        outside = {"HOME": self.tmp, "PATH": self.env["PATH"]}
        code, out, err = self.run_cli("inbox", env=outside)
        self.assertEqual(code, 0, out + err)
        self.assertIn("owner outside Herdr", out)

    def test_stale_pane_and_non_shell_foreground_fail_closed(self):
        for env, shell in ((dict(self.env, HERDR_PANE_ID="w1:p999"), False), (self.env, False)):
            self.world["panes"][CALLER_PANE]["agent"] = None
            code, out, err = self.run_cli("inbox", env=env)
            self.assertEqual(code, 2, out + err)
            self.assertIn("--as NAME", err)

    def test_explicit_override_and_machine_streams_keep_clean_stdout(self):
        env = dict(self.env)
        env.pop("HERDR_PANE_ID")
        for flags, expected in ((["--json"], []), (["--count"], 0)):
            code, out, err = self.run_cli("inbox", "--as", "owner", *flags, env=env)
            self.assertEqual(code, 0, out + err)
            self.assertEqual(json.loads(out), expected)
            self.assertIn("inbox: owner", err)
            self.assertIn(self.repo, err)
        self.assertFalse(self.calls())

    def test_json_wait_timeout_and_hook_fallback_are_clean(self):
        code, out, err = self.run_cli("inbox", "--as", "peer-lead", "--json", "--wait", "--timeout", "0",
                                     "--fleet", self.destination)
        self.assertEqual(code, 3, out + err)
        self.assertEqual(json.loads(out), [])
        self.assertIn("peer-lead", err)
        self.world["canned"].append({"prefix": ["agent", "get", CALLER_PANE], "exit": 1,
                                     "stderr": {"error": {"code": "timeout", "message": "synthetic"}}})
        code, out, err = self.run_cli("inbox", "--hook", "stop", stdin=json.dumps({"cwd": self.repo}))
        self.assertEqual((code, out, err), (0, "", ""))

    def test_unread_note_precedes_selection_and_final_owner_outcome(self):
        put(self.repo, "caller", "Synthetic unread report")
        code, out, err = self.run_cli("send", "--fyi", "partner", "FYI: Synthetic outgoing")
        self.assertEqual(code, 0, out + err)
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("note:"))
        self.assertTrue(lines[1].startswith("sender: caller"))
        self.assertIn("mailbox: " + os.path.realpath(self.destination), lines[-1])
        self.assertIn("partner -> peer-lead", lines[-1])

    def test_missing_full_and_short_reply_ids_stay_scoped_and_ambiguity_is_preserved(self):
        remote = put(self.other, "peer-lead", "Synthetic forwarded request", ident="20260101T000000Z-abc123")
        for ident in (remote.id, "#abc123"):
            before = self.tree()
            code, out, err = self.run_cli("send", "--reply-to", ident, "partner", "Synthetic answer")
            self.assertEqual(code, 2)
            self.assertIn("forwarded", err)
            self.assertIn("--fleet PATH", err)
            self.assertEqual(self.tree(), before)
        put(self.repo, "sample-dev", "Synthetic first", ident="20260101T000000Z-def456")
        put(self.repo, "sample-reviewer", "Synthetic second", ident="20260102T000000Z-def456")
        before = self.tree()
        code, out, err = self.run_cli("send", "--reply-to", "#def456", "sample-dev", "Synthetic ambiguous response")
        self.assertEqual(code, 2)
        self.assertIn("matches several", err)
        self.assertEqual(self.tree(), before)

    def test_reply_id_error_names_selected_namespace_and_guidance(self):
        before = self.tree()
        code, out, err = self.run_cli("send", "--reply-to", "#abcdef", "sample-dev", "Synthetic response")
        self.assertEqual(code, 2)
        self.assertIn("--fleet PATH", err)
        self.assertIn(self.repo, err)
        self.assertIn("does not retain exact reply linkage", err)
        self.assertEqual(self.tree(), before)


class RuntimeIdentity(FleetPair):
    def test_watcher_lock_records_launch_and_status_compares_cli_identity(self):
        lock = watch.acquire(self.repo, os.path.join(self.repo, ".herdr", "fleet.toml"))
        try:
            self.assertIn("version", lock.get("launch", {}))
            data = json.loads(self.run_cli("status", "--json")[1])
            self.assertIn("cli", data)
            self.assertIn("launch", data["watcher"])
            self.assertIn(data["watcher"]["comparison"], ("current", "stale", "unknown"))
        finally:
            watch.release(self.repo, lock["pid"])

    def test_old_lock_identity_is_unknown_not_inferred_from_cli(self):
        path = watch.lock_path(self.repo)
        with open(path, "w") as handle:
            json.dump({"pid": os.getpid(), "started": "2026-01-01T00:00:00Z", "fleet": "synthetic"}, handle)
        data = json.loads(self.run_cli("status", "--json")[1])
        self.assertEqual(data["watcher"]["launch"], {"version": None, "commit": None})
        self.assertEqual(data["watcher"]["comparison"], "unknown")
        self.assertIn("watcher launch: version unknown, commit unknown; unknown", self.run_cli("status")[1])

    def test_stale_launch_has_explicit_watcher_only_reload_and_no_auto_actions(self):
        launch = {"version": "0.15.0", "commit": "1" * 40}
        cli = {"version": "0.16.0", "commit": "2" * 40}
        with mock.patch.object(build_identity, "current", return_value=launch):
            lock = watch.acquire(self.repo, self.destination)
        with mock.patch.object(build_identity, "current", return_value=cli):
            data = json.loads(self.run_cli("status", "--json")[1])
            self.assertEqual(data["cli"], cli)
            self.assertEqual(data["watcher"]["launch"], launch)
            self.assertEqual(data["watcher"]["comparison"], "stale")
            out = self.run_cli("status")[1]
            self.assertIn("watcher-only reload", out)
            self.assertIn("leave coding-agent panes/sessions running", out)
            self.assertIn("herdr-fleet watch --fleet", out)
        self.assertFalse(self.argvs("agent", "prompt"))
        self.assertFalse(self.argvs("agent", "focus"))
        self.assertFalse(self.argvs("pane", "close"))
        self.assertEqual(watch.read_lock(self.repo)["launch"], launch)
        watch.release(self.repo, lock["pid"] + 1)
        self.assertIsNotNone(watch.read_lock(self.repo))
        watch.release(self.repo, lock["pid"])


class BuildIdentity(unittest.TestCase):
    def tearDown(self):
        build_identity.current.cache_clear()

    def test_identity_cached_once_and_git_absence_keeps_package_version(self):
        build_identity.current.cache_clear()
        with mock.patch.object(gitinfo, "run_git", return_value=None) as git:
            first = build_identity.current()
            second = build_identity.current()
            self.assertEqual(first, second)
            self.assertIsNone(first["commit"])
            self.assertEqual(first["version"], "0.17.0")
            self.assertEqual(git.call_count, 1)

    def test_only_full_checkout_commit_is_retained_and_ancestor_git_is_not_invented(self):
        build_identity.current.cache_clear()
        with mock.patch.object(gitinfo, "run_git", side_effect=[
            gitinfo.GitResult(0, build_identity.PLUGIN_ROOT + "\n"), gitinfo.GitResult(0, "a" * 40 + "\n")]) as git:
            self.assertEqual(build_identity.current()["commit"], "a" * 40)
            self.assertEqual(git.call_count, 2)
        build_identity.current.cache_clear()
        with mock.patch.object(gitinfo, "run_git", return_value=gitinfo.GitResult(0, "/synthetic/parent")) as git:
            self.assertIsNone(build_identity.current()["commit"])
            self.assertEqual(git.call_count, 1)

    def test_comparison_current_stale_and_partial_evidence_unknown(self):
        identity = {"version": "0.16.0", "commit": "a" * 40}
        self.assertEqual(build_identity.compare(identity, identity), "current")
        self.assertEqual(build_identity.compare(identity, {"version": "0.15.0", "commit": None}), "stale")
        self.assertEqual(build_identity.compare(identity, {"version": "0.16.0", "commit": "b" * 40}), "stale")
        self.assertEqual(build_identity.compare(identity, {"version": "0.16.0", "commit": None}), "unknown")
        self.assertEqual(build_identity.validated({"version": "0.16.0", "commit": "abc123"})["commit"], None)


class LauncherProvenance(LiveFleet):
    """Execute generated launchers with an isolated fake registry, never live Herdr."""
    def setUp(self):
        super().setUp()
        from herdr_fleet.ops import launcher_text
        self.marker = os.path.join(self.tmp, "development-ran")
        self.fallback = os.path.join(self.tmp, "development.py")
        with open(self.fallback, "w") as handle:
            handle.write("open(%r, 'w').write('unexpected')\nprint('development')\n" % self.marker)
        self.launcher = os.path.join(self.tmp, "launcher")
        with open(self.launcher, "w") as handle:
            handle.write(launcher_text(self.fallback))
        os.chmod(self.launcher, 0o755)
        self.registry = os.path.join(self.tmp, "fake-registry")
        self.fake_bin = os.path.join(self.tmp, "fake-bin")
        os.makedirs(self.fake_bin)
        self.installed = os.path.join(self.tmp, "installed plugin")
        os.makedirs(os.path.join(self.installed, "bin"))
        os.makedirs(os.path.join(self.installed, "herdr_fleet"))
        self.manifest = os.path.join(self.installed, "herdr-plugin.toml")
        with open(self.manifest, "w") as handle:
            handle.write('id="yarrasys.fleet"\n')
        with open(os.path.join(self.installed, "herdr_fleet", "hooks.py"), "w") as handle:
            handle.write("# Synthetic hook-capable installation.\n")
        self.entry = os.path.join(self.installed, "bin", "herdr-fleet")
        with open(self.entry, "w") as handle:
            handle.write("#!%s\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n" % sys.executable)
        os.chmod(self.entry, 0o755)
        self.launch_env = dict(os.environ)
        for key in ("HERDR_FLEET_BIN", "HERDR_BIN_PATH"):
            self.launch_env.pop(key, None)
        self.launch_env["HERDR_BIN_PATH"] = self.registry
        self.launch_env["PATH"] = self.fake_bin + os.pathsep + self.launch_env.get("PATH", "")
        self.registry_result()

    def registry_result(self, plugins=None, raw=None, code=0):
        if plugins is None:
            plugins = [{"plugin_id": "yarrasys.fleet", "enabled": True, "manifest_path": self.manifest}]
        payload = raw if raw is not None else json.dumps({"result": {"plugins": plugins}})
        with open(self.registry, "w") as handle:
            handle.write("#!%s\nimport sys\nassert sys.argv[1:] == ['plugin', 'list', '--json']\n" % sys.executable)
            handle.write("sys.stdout.write(%r)\nsys.stderr.write(%r)\nsys.exit(%d)\n" %
                         (payload, "registry permission denied\n" if code else "", code))
        os.chmod(self.registry, 0o755)
        shutil.copy(self.registry, os.path.join(self.fake_bin, "herdr"))

    def launch(self, *args):
        result = subprocess.run([self.launcher] + list(args), env=self.launch_env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=10)
        self.assertFalse(os.path.exists(self.marker), "implicit development fallback executed")
        return result

    def test_registry_permission_failure_preserves_status_and_never_falls_back(self):
        self.registry_result(code=13)
        result = self.launch("status", "--json")
        self.assertEqual(result.returncode, 13)
        self.assertEqual(result.stdout, "")
        self.assertIn("registry permission denied", result.stderr)
        self.assertIn("registry", result.stderr)

    def test_malformed_disabled_missing_and_misleading_registration_fail_closed(self):
        cases = [dict(raw="invalid JSON"), dict(raw="[]"), dict(plugins=[]),
                 dict(plugins=[{"plugin_id": "yarrasys.fleet", "enabled": False, "manifest_path": self.manifest}]),
                 dict(plugins=[{"plugin_id": "other.plugin", "description": "yarrasys.fleet", "enabled": True,
                                "manifest_path": self.manifest}]),
                 dict(plugins=[{"plugin_id": "yarrasys.fleet.preview", "enabled": True, "manifest_path": self.manifest}])]
        for case in cases:
            with self.subTest(case=case):
                self.registry_result(**case)
                result = self.launch("inbox", "--count")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertIn("herdr-fleet launcher", result.stderr)

    def test_exact_enabled_install_honors_configured_binary_and_preserves_arguments(self):
        self.registry_result(plugins=[{"plugin_id": "other.plugin", "description": "yarrasys.fleet", "enabled": True,
                                      "manifest_path": "/synthetic/missing/manifest.toml"},
                                     {"plugin_id": "yarrasys.fleet", "enabled": True, "manifest_path": self.manifest}])
        for args in (("status", "--json"), ("inbox", "--count"), ("send", "peer-lead", "ordinary --hook text")):
            result = self.launch(*args)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), list(args))
            self.assertEqual(result.stderr, "")

    def test_invalid_registered_path_and_missing_executable_are_errors(self):
        for path in ("relative/manifest.toml", "/synthetic/missing/manifest.toml"):
            self.registry_result(plugins=[{"plugin_id": "yarrasys.fleet", "enabled": True, "manifest_path": path}])
            self.assertNotEqual(self.launch("status").returncode, 0)
        self.registry_result()
        os.unlink(self.entry)
        self.assertNotEqual(self.launch("status").returncode, 0)

    def test_hooks_silently_handle_all_selection_failures_and_old_installs(self):
        cases = [dict(code=13), dict(raw="invalid JSON"), dict(plugins=[]),
                 dict(plugins=[{"plugin_id": "yarrasys.fleet", "enabled": True,
                                "manifest_path": "/synthetic/missing/manifest.toml"}])]
        for case in cases:
            self.registry_result(**case)
            result = self.launch("inbox", "--hook", "stop")
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.registry_result()
        os.unlink(os.path.join(self.installed, "herdr_fleet", "hooks.py"))
        result = self.launch("inbox", "--hook", "prompt")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        os.unlink(self.entry)
        result = self.launch("inbox", "--hook", "stop")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_equals_form_hook_silences_registry_failure_without_silencing_send_text(self):
        self.registry_result(code=13)
        for flag in ("--hook=stop", "--hook=prompt"):
            result = self.launch("inbox", flag)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        result = self.launch("send", "peer-lead", "--hook=stop")
        self.assertEqual(result.returncode, 13)
        self.assertTrue(result.stderr)

    def test_explicit_override_is_deliberate_and_bypasses_registry(self):
        self.registry_result(code=13)
        self.launch_env["HERDR_FLEET_BIN"] = self.entry
        result = self.launch("status", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), ["status", "--json"])

    def test_non_utf8_registry_output_is_malformed_and_hooks_stay_silent(self):
        with open(self.registry, "w") as handle:
            handle.write("#!%s\nimport sys\nsys.stdout.buffer.write(bytes([255]))\n" % sys.executable)
        result = self.launch("status", "--json")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("malformed", result.stderr)
        result = self.launch("inbox", "--hook", "stop")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_explicit_dev_symlink_executes_only_the_chosen_checkout(self):
        from herdr_fleet.ops import op_install_cli
        target = os.path.join(self.tmp, "dev-bin")
        self.assertEqual(op_install_cli(self.installed, target, io.StringIO(), "", dev=True), 0)
        result = subprocess.run([os.path.join(target, "herdr-fleet"), "status", "--json"],
                                env=self.launch_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                universal_newlines=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), ["status", "--json"])
        self.assertFalse(os.path.exists(self.marker))

    def test_missing_registry_binary_and_ordinary_hook_text_are_not_silenced(self):
        self.launch_env["HERDR_BIN_PATH"] = os.path.join(self.tmp, "missing-herdr")
        result = self.launch("send", "peer-lead", "--hook")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(result.stderr)
        result = self.launch("inbox", "--hook", "stop")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))


class OwnershipRaces(FleetPair):
    def contexts(self):
        from herdr_fleet.context import Ctx
        source = BoardCase.make_ctx(self)
        destination = Ctx(source.herdr, self.env, self.clock, source.stdout, source.stderr,
                          self.other, file=self.destination)
        return source, destination

    def race(self, operations):
        barrier = threading.Barrier(len(operations))
        results, errors = [], []
        def run(operation):
            try:
                barrier.wait(timeout=3)
                results.append(operation())
            except BaseException as exc:
                errors.append(exc)
        workers = [threading.Thread(target=run, args=(operation,), daemon=True) for operation in operations]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=5)
        self.assertFalse([worker for worker in workers if worker.is_alive()], "mailbox/root lock inversion")
        self.assertFalse(errors, errors)
        with open(self.world_path) as handle:
            self.world = json.load(handle)
        return results

    def test_two_watchers_and_pull_share_one_destination_mailbox(self):
        self.send_queue()
        _, ctx = self.contexts()
        first, second = watch.Watcher(ctx, 0), watch.Watcher(ctx, 0)
        results = self.race((lambda: first.deliver("peer-lead", "peer-lead"),
                             lambda: second.deliver("peer-lead", "peer-lead"),
                             lambda: mailroom._take(ctx, "peer-lead", True)))
        self.assertFalse(self.pending())
        native = len(self.prompts_to("peer-lead"))
        pulled = sum(len(result) for result in results if isinstance(result, list))
        self.assertEqual(native + pulled, 1)
        for root in (self.repo, self.other):
            self.assertEqual(len([e for e in log_entries(root) if e["kind"] == "delivered"]), 1)
        self.assertFalse(inbox.has_queued(self.repo, "peer-lead"))

    def test_two_pull_readers_consume_once(self):
        self.send_queue("--fyi")
        _, ctx = self.contexts()
        results = self.race((lambda: mailroom._take(ctx, "peer-lead", True),
                             lambda: mailroom._take(ctx, "peer-lead", True)))
        self.assertEqual(sorted(len(result) for result in results), [0, 1])
        self.assertFalse(self.argvs("agent", "prompt"))
        self.assertEqual(len([e for e in log_entries(self.repo) if e["kind"] == "delivered"]), 1)

    def test_simultaneous_reverse_routes_have_distinct_ids_and_no_lock_inversion(self):
        source, destination = self.contexts()
        with open(self.log_path, "a") as handle:
            handle.write(json.dumps({"command": ["send", "--queue"]}) + "\n")
        results = self.race((lambda: messages.op_send(source, ["peer-lead", "Synthetic request"],
                                                     as_name="sample-orchestrator", queue=True),
                             lambda: messages.op_send(destination, ["sample-orchestrator", "Synthetic reply"],
                                                     as_name="peer-lead", queue=True)))
        self.assertEqual(results, [0, 0])
        inbound = inbox.read_box(self.repo, "sample-orchestrator", inbox.NEW)
        outbound = self.pending()
        self.assertEqual((len(inbound), len(outbound)), (1, 1))
        self.assertNotEqual(inbound[0].short, outbound[0].short)
        for root in (self.repo, self.other):
            self.assertEqual(len([e for e in log_entries(root) if e["kind"] == "send"]), 2)
        self.assertFalse(self.argvs("agent", "prompt"))

    def test_legacy_pending_stays_source_owned_and_cur_is_not_replayed(self):
        legacy = put(self.repo, "partner", "Synthetic legacy queue", fyi=True)
        history = put(self.other, "peer-lead", "Synthetic legacy history", which=inbox.CUR)
        with open(history.path, "rb") as handle:
            before = handle.read()
        self.run_cli("watch", "--once", "--fleet", self.destination)
        self.assertTrue(os.path.isfile(legacy.path))
        self.assertFalse(self.pending())
        code, out, err = self.run_cli("deliver", "partner", "--fyi")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(len(self.argvs("agent", "prompt")), 1)
        self.assertFalse(inbox.has_queued(self.repo, "partner"))
        self.assertFalse(self.pending())
        with open(history.path, "rb") as handle:
            self.assertEqual(handle.read(), before)
        self.send_queue()
        self.assertEqual(len(self.pending()), 1)  # only the new request was routed

    def test_foreground_and_real_path_discovery_but_matching_cwd_is_not_identity(self):
        linked = os.path.join(self.tmp, "linked-partner")
        os.symlink(self.other, linked)
        agent = self.world["panes"]["w3:p1"]["agent"]
        agent["cwd"] = os.path.join(self.tmp, "no-fleet")
        os.makedirs(agent["cwd"])
        agent["foreground_cwd"] = linked
        self.world["panes"]["w3:p1"]["cwd"] = agent["cwd"]
        self.world["panes"]["w3:p1"]["foreground_cwd"] = linked
        code, out, err = self.send_queue()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.pending()[0].destination_fleet, os.path.realpath(self.other))
        # An exact named live record is still mandatory; a cwd alone cannot route.
        self.world["panes"]["w3:p1"]["agent"]["name"] = None
        code, out, err = self.send_queue()
        self.assertEqual(code, 0, out + err)
        self.assertIn("source-owned", out)
        self.assertEqual(len(self.pending()), 1)
        self.assertTrue(inbox.has_queued(self.repo, "partner"))
