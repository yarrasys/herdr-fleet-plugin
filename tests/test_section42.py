"""Continuous owner conversation: synthetic files and fake Herdr only."""
from __future__ import annotations

import json
import os
import shlex
import threading
import copy
from unittest import mock

from herdr_fleet import inbox, messages
from herdr_fleet.dashboard import DashboardServer
from herdr_fleet.dashboard_terminal import Watches
from herdr_fleet.dashboard_data import Registry, Observer
from herdr_fleet.dashboard_actions import OwnerActions
import fake_herdr
from test_section40 import snapshot
import test_section41 as fixtures


class ReturnGuidance(fixtures.DashboardCase):
    def test_existing_stop_hook_carries_validated_provenance_without_new_native_prompt(self):
        self.status_of("w2:p2", "working"); self.sync()
        payload = self.payload(text="Synthetic hook question")
        self.mark("send", "sample-dev")
        result = self.actions.perform(payload)
        env = dict(self.env, HERDR_PANE_ID="w2:p2", HERDR_WORKSPACE_ID="w2")
        code, output, error = self.run_cli("inbox", "--hook", "stop", env=env,
                                          stdin=json.dumps({"cwd": self.repo}))
        self.assertEqual(code, 0, error)
        text = json.loads(output)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Synthetic hook question", text)
        self.assertIn("--reply-to " + result["id"], text)
        self.assertIn("--as sample-dev", text)
        item = inbox.resolve(self.repo, result["id"], mailbox="sample-dev")
        self.assertEqual(item.box, inbox.CUR)
        self.assertEqual(item.dashboard_chat["intent"], payload["intent"])
        self.assertEqual(self.prompts_to("sample-dev"), [])

    def test_render_print_json_custom_source_and_invalid_provenance_keep_body_compatible(self):
        from herdr_fleet.mailroom import _as_dict
        from herdr_fleet.dashboard_provenance import validated
        custom = self.write_file(".herdr/source team's.toml", self.read_fleet())
        self.registry = Registry([custom], self.repo, self.env)
        self.fleet_id = next(iter(self.registry.paths))
        self.observer = Observer(self.client, self.registry, self.env)
        self.actions = OwnerActions(self.client, self.registry, self.env, self.clock)
        self.status_of("w2:p2", "working"); self.sync()
        payload = self.payload(text="Synthetic body\n" * 50)
        self.mark("send", "sample-dev")
        result = self.actions.perform(payload)
        item = inbox.resolve(self.repo, result["id"], mailbox="sample-dev")
        for text in (inbox.direct_text(item), inbox.render_batch([item]), inbox.render_for_print([item], 20)):
            self.assertIn("--reply-to " + item.id, text)
            words = shlex.split(next(line for line in text.splitlines() if line.startswith("herdr-fleet ")))
            self.assertEqual(words[words.index("--fleet") + 1], os.path.realpath(custom))
        self.assertEqual(_as_dict(item, body=True)["dashboard_chat"], item.dashboard_chat)
        raw = inbox.render_file(item)
        self.assertEqual(inbox.parse_file(raw).body, payload["text"])
        for change in ({"v": 2}, {"source_file": self.peer_file if hasattr(self, "peer_file") else "/missing/fleet.toml"},
                       {"intent": "bad"}):
            invalid = copy.copy(item); invalid.dashboard_chat = dict(item.dashboard_chat, **change)
            self.assertIsNone(validated(invalid))
            self.assertNotIn("--reply-to", inbox.direct_text(invalid))
            self.assertNotIn("dashboard_chat", _as_dict(invalid, body=True))
            self.assertIn(payload["text"], inbox.direct_text(invalid))
        os.unlink(custom)
        self.assertNotIn("--reply-to", inbox.direct_text(item))

    def test_fully_rendered_guidance_crosses_batch_cap_and_pointer_keeps_mail_queued(self):
        self.status_of("w2:p2", "working"); self.sync()
        payload = self.payload(text="x" * (inbox.BATCH_CAP - 200))
        self.mark("send", "sample-dev")
        result = self.actions.perform(payload)
        item = inbox.resolve(self.repo, result["id"], mailbox="sample-dev")
        text, size, pointer = messages.compose_prompt([item])
        self.assertTrue(pointer)
        self.assertGreater(size, inbox.BATCH_CAP)
        self.assertLess(len(text.encode()), 1024)
        self.assertNotIn(item.dashboard_chat["source_file"], text)
        self.assertEqual(item.box, inbox.NEW)
        self.assertEqual(self.prompts_to("sample-dev"), [])

    def test_authenticated_owner_message_keeps_body_and_validated_return_guidance(self):
        payload = self.payload(text="Synthetic owner question\nVerbatim tail.\n")
        self.mark("send", "sample-dev")
        result = self.actions.perform(payload)
        message = inbox.resolve(self.repo, result["id"], mailbox="sample-dev")
        expected = {"v": 1, "source_file": os.path.realpath(self.registry.path(self.fleet_id)),
                    "intent": payload["intent"]}
        self.assertEqual(getattr(message, "dashboard_chat", None), expected)
        self.assertEqual(message.body, payload["text"])
        text = self.prompts_to("sample-dev")[0]
        self.assertIn("ack", text.lower())
        self.assertIn("--reply-to " + result["id"], text)
        command = next(line for line in text.splitlines() if line.startswith("herdr-fleet "))
        words = shlex.split(command)
        self.assertEqual(words[words.index("--fleet") + 1], expected["source_file"])
        self.assertEqual(words[words.index("--as") + 1], "sample-dev")


class Conversation(fixtures.DashboardCase):
    def test_conflicting_new_cur_headers_keep_selected_message_uncertain_despite_positive_journal(self):
        payload = self.payload(text="Confirmed original body")
        self.mark("send", "sample-dev")
        sent = self.actions.perform(payload)
        original = inbox.resolve(self.repo, sent["id"], mailbox="sample-dev")
        self.assertEqual(original.box, inbox.CUR)
        healthy = fixtures.put(self.repo, "sample-dev", "Independent healthy message", sender="owner")
        messages.append(self.repo, messages.send_entry(healthy, "sent"))
        baseline = self.observer.conversation(self.fleet_id, self.fleet_id, "sample-dev")
        self.assertEqual(next(r for r in baseline["messages"] if r["id"] == original.id)["certainty"], "confirmed")
        conflict = copy.copy(original)
        conflict.reply_to = healthy.id
        conflict.body = "CONFLICTING BODY MUST NOT BE RETURNED"
        inbox.write(self.repo, conflict, inbox.NEW)
        before, calls = snapshot(self.repo), len(self.calls())
        result = self.observer.conversation(self.fleet_id, self.fleet_id, "sample-dev")
        row = next(r for r in result["messages"] if r["id"] == original.id)
        self.assertTrue(result["incomplete"])
        self.assertTrue(row["incomplete"])
        self.assertEqual(row["certainty"], "unconfirmed")
        self.assertIsNone(row["body"])
        self.assertNotIn(conflict.body, json.dumps(result))
        self.assertEqual(next(r for r in result["messages"] if r["id"] == healthy.id),
                         next(r for r in baseline["messages"] if r["id"] == healthy.id))
        self.assertTrue(any(a["intent"] == payload["intent"] and a["id"] == original.id for a in result["intents"]))
        self.assertEqual(snapshot(self.repo), before)
        self.assertEqual(len(self.calls()), calls)
        # If the first conflicting header changes recipient, log-only fallback
        # must not turn the original's positive send record into certainty.
        conflict.to = "sample-reviewer"
        path = os.path.join(inbox.box_dir(self.repo, "sample-dev", inbox.NEW), original.id + ".md")
        with open(path, "w") as handle:
            handle.write(inbox.render_file(conflict))
        before = snapshot(self.repo)
        fallback = self.observer.conversation(self.fleet_id, self.fleet_id, "sample-dev")
        row = next(r for r in fallback["messages"] if r["id"] == original.id)
        self.assertEqual(row["certainty"], "unconfirmed")
        self.assertTrue(row["incomplete"])
        self.assertIsNone(row["body"])
        self.assertEqual(snapshot(self.repo), before)

    def test_exact_ack_is_visible_while_queued_without_claiming_delivery_or_counting_generic_ack(self):
        item = fixtures.put(self.repo, "sample-dev", "Queued original", sender="owner")
        messages.append(self.repo, messages.send_entry(item, "queued"))
        messages.append(self.repo, messages.ack_entry("sample-dev", "owner"))
        messages.append(self.repo, messages.ack_entry("sample-reviewer", "owner", ident=item.id))
        messages.append(self.repo, messages.ack_entry("sample-dev", "owner", ident=item.id))
        result = self.observer.conversation(self.fleet_id, self.fleet_id, "sample-dev")
        row = result["messages"][0]
        self.assertEqual(row["status"], "queued")
        self.assertFalse(any(r["kind"] == "delivered" for r in row["receipts"]))
        acks = [r for r in row["receipts"] if r["kind"] == "ack"]
        self.assertEqual(len(acks), 1)
        self.assertTrue(acks[0]["exact"])
        self.assertEqual(acks[0]["by"], "sample-dev")

    def test_corrupt_journal_is_visible_as_incomplete_without_effects_or_untrusted_association(self):
        from herdr_fleet.dashboard_actions import Journal
        payload = self.payload(); self.mark("send", "sample-dev")
        self.actions.perform(payload)
        with open(Journal(self.repo).path, "w") as handle:
            handle.write("not a journal")
        before = snapshot(self.repo)
        result = self.observer.conversation(self.fleet_id, self.fleet_id, "sample-dev")
        self.assertTrue(result["incomplete"])
        self.assertEqual(result["intents"], [])
        self.assertEqual(snapshot(self.repo), before)

    def test_utf8_body_limit_counts_returned_bytes_without_replacement_expansion(self):
        item = fixtures.put(self.repo, "sample-dev", "€" * 20000, sender="owner")
        messages.append(self.repo, messages.send_entry(item, "queued"))
        result = self.observer.conversation(self.fleet_id, self.fleet_id, "sample-dev")
        self.assertLessEqual(len(result["messages"][0]["body"].encode()), 32768)
        self.assertTrue(result["messages"][0]["body_truncated"])

    def test_new_cur_move_during_selected_body_read_is_one_row_and_never_consumes_other_mail(self):
        item = fixtures.put(self.repo, "sample-dev", "Race-safe body", sender="owner")
        other = fixtures.put(self.repo, "sample-dev", "Other private body", sender="sample-reviewer")
        messages.append(self.repo, messages.send_entry(item, "queued"))
        read = self.observer.cache.bytes
        moved = [False]
        def moving(path, *args, **kwargs):
            if path.endswith(item.id + ".md") and not moved[0]:
                inbox.move(self.repo, item, inbox.CUR)
                moved[0] = True
            return read(path, *args, **kwargs)
        with mock.patch.object(self.observer.cache, "bytes", side_effect=moving):
            result = self.observer.conversation(self.fleet_id, self.fleet_id, "sample-dev")
        self.assertEqual(len(result["messages"]), 1)
        self.assertEqual(result["messages"][0]["body"], "Race-safe body")
        self.assertTrue(os.path.isfile(other.path))

    def test_conflicting_uncertain_receipt_is_not_downgraded_by_later_queue_or_kickoff(self):
        item = fixtures.put(self.repo, "sample-dev", "Uncertain request", sender="owner")
        messages.append(self.repo, messages.send_entry(item, "unconfirmed"))
        messages.append(self.repo, messages.send_entry(item, "queued"))
        messages.append(self.repo, messages.make_entry("herdr-fleet", "sample-dev", "kickoff", "A new kickoff"))
        result = self.observer.conversation(self.fleet_id, self.fleet_id, "sample-dev")
        self.assertEqual(result["messages"][0]["status"], "unconfirmed")
        self.assertEqual(result["messages"][0]["certainty"], "unconfirmed")
        self.assertFalse(any(r["kind"] == "ack" for r in result["messages"][0]["receipts"]))

    def test_exact_full_id_collision_with_other_participant_never_changes_selected_body(self):
        item = fixtures.put(self.repo, "sample-dev", "Selected body", sender="owner")
        messages.append(self.repo, messages.send_entry(item, "queued"))
        unrelated = copy.copy(item); unrelated.to = "sample-reviewer"; unrelated.body = "OTHER PRIVATE BODY"
        messages.append(self.repo, messages.send_entry(unrelated, "sent"))
        result = self.observer.conversation(self.fleet_id, self.fleet_id, "sample-dev")
        self.assertEqual(result["messages"][0]["status"], "queued")
        self.assertEqual(result["messages"][0]["body"], "Selected body")
        self.assertNotIn("OTHER PRIVATE BODY", json.dumps(result))

    def test_message_body_budget_and_recent_window_are_explicitly_capped(self):
        for index in range(10):
            item = fixtures.put(self.repo, "sample-dev", str(index) + "x" * 40000, sender="owner")
            messages.append(self.repo, messages.send_entry(item, "queued"))
        result = self.observer.conversation(self.fleet_id, self.fleet_id, "sample-dev", 100)
        self.assertLessEqual(sum(len((m["body"] or "").encode()) for m in result["messages"]), 262144)
        self.assertTrue(result["truncated"])
        recent = self.observer.conversation(self.fleet_id, self.fleet_id, "sample-dev", 2)
        self.assertEqual(len(recent["messages"]), 2)
        self.assertTrue(recent["truncated"])

    def test_body_replacement_after_header_filter_never_exposes_new_participants_body(self):
        original = fixtures.put(self.repo, "sample-dev", "Original owner body", sender="owner")
        messages.append(self.repo, messages.send_entry(original, "queued"))
        read = self.observer.cache.bytes
        def replaced(path, *args, **kwargs):
            if path.endswith(original.id + ".md"):
                replacement = copy.copy(original)
                replacement.sender = "sample-reviewer"
                replacement.body = "PRIVATE REPLACEMENT BODY"
                with open(path, "w") as handle:
                    handle.write(inbox.render_file(replacement))
            return read(path, *args, **kwargs)
        with mock.patch.object(self.observer.cache, "bytes", side_effect=replaced):
            result = self.observer.conversation(self.fleet_id, self.fleet_id, "sample-dev")
        self.assertNotIn("PRIVATE REPLACEMENT BODY", json.dumps(result))
        self.assertTrue(result["messages"][0]["incomplete"])
        self.assertEqual(result["messages"][0]["certainty"], "unconfirmed")
        self.assertIsNone(result["messages"][0]["body"])

    def test_absent_seat_history_and_bounded_bodies_do_not_need_live_agent_or_new_writes(self):
        body = "Synthetic long body\n" * 4000
        item = fixtures.put(self.repo, "sample-dev", body, sender="owner")
        messages.append(self.repo, messages.send_entry(item, "queued"))
        self.world["panes"]["w2:p2"]["agent"] = None; self.sync()
        before, calls = snapshot(self.repo), len(self.calls())
        result = self.observer.conversation(self.fleet_id, self.fleet_id, "sample-dev", 1)
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["messages"][0]["body"].encode()), 32768)
        self.assertEqual(result["messages"][0]["status"], "queued")
        self.assertEqual(snapshot(self.repo), before)
        self.assertEqual(len(self.calls()), calls)

    def test_exact_ack_requires_participants_and_generic_ack_keeps_actual_order_semantics(self):
        item = fixtures.put(self.repo, "sample-dev", "Owned", sender="owner")
        wrong = messages.ack_entry("sample-reviewer", "owner", ident=item.id)
        messages.append(self.repo, wrong)
        early = messages.ack_entry("sample-dev", "owner")
        messages.append(self.repo, early)
        messages.append(self.repo, messages.send_entry(item, "sent"))
        result = self.observer.conversation(self.fleet_id, self.fleet_id, "sample-dev")
        self.assertFalse(any(r["kind"] == "ack" for r in result["messages"][0]["receipts"]))
        messages.append(self.repo, messages.ack_entry("sample-dev", "owner"))
        result = self.observer.conversation(self.fleet_id, self.fleet_id, "sample-dev")
        acks = [r for r in result["messages"][0]["receipts"] if r["kind"] == "ack"]
        self.assertEqual(len(acks), 1)
        self.assertFalse(acks[0]["exact"])

    def test_log_only_history_is_visible_as_incomplete_without_inventing_a_body(self):
        original = fixtures.put(self.repo, "sample-dev", "Missing stored body", sender="owner")
        messages.append(self.repo, messages.send_entry(original, "sent"))
        os.unlink(original.path)
        before = snapshot(self.repo)
        result = self.observer.conversation(self.fleet_id, self.fleet_id, "sample-dev")
        self.assertEqual(len(result["messages"]), 1)
        self.assertIsNone(result["messages"][0]["body"])
        self.assertTrue(result["messages"][0]["incomplete"])
        self.assertEqual(result["messages"][0]["certainty"], "unconfirmed")
        self.assertEqual(snapshot(self.repo), before)

    def test_other_participants_full_ids_and_unrelated_reply_ids_never_load_bodies(self):
        original = fixtures.put(self.repo, "sample-dev", "Other sender private", sender="sample-reviewer")
        answer = fixtures.put(self.repo, "owner", "Other agent private", sender="sample-reviewer")
        unrelated = fixtures.put(self.repo, "owner", "Unrelated exact reply", sender="sample-dev")
        unrelated.reply_to = original.id
        with open(unrelated.path, "w") as handle:
            handle.write(inbox.render_file(unrelated))
        result = self.observer.conversation(self.fleet_id, self.fleet_id, "sample-dev")
        self.assertEqual(result["messages"], [])

    def test_local_ack_and_real_reply_join_without_consuming_any_mail(self):
        payload = self.payload(text="Synthetic question")
        self.mark("send", "sample-dev")
        result = self.actions.perform(payload)
        original = inbox.resolve(self.repo, result["id"], mailbox="sample-dev")
        messages.append(self.repo, messages.ack_entry("sample-dev", "owner", ident=original.id))
        reply = fixtures.put(self.repo, "owner", "Synthetic actual answer", sender="sample-dev")
        reply.reply_to = original.id
        with open(reply.path, "w") as handle:
            handle.write(inbox.render_file(reply))
        messages.append(self.repo, messages.send_entry(reply, "queued"))
        before, calls = snapshot(self.repo), len(self.calls())
        thread = self.observer.conversation(self.fleet_id, self.fleet_id, "sample-dev")
        self.assertEqual([m["id"] for m in thread["messages"]], [original.id, reply.id])
        sent, answer = thread["messages"]
        self.assertEqual(sent["status"], "delivered")
        self.assertTrue(any(r["kind"] == "ack" and r["exact"] for r in sent["receipts"]))
        self.assertEqual(sent["reply_ids"], [reply.id])
        self.assertEqual(answer["body"], "Synthetic actual answer")
        self.assertEqual(answer["reply_to"], original.id)
        self.assertEqual(thread["intents"][0]["intent"], payload["intent"])
        self.assertEqual(thread["intents"][0]["id"], original.id)
        self.assertEqual(snapshot(self.repo), before)
        self.assertEqual(len(self.calls()), calls)


class ConversationHTTP(fixtures.DashboardCase):
    request = fixtures.HTTPAuthority.request
    login = fixtures.HTTPAuthority.login

    def setUp(self):
        super().setUp()
        self.observer.refresh()
        self.server = DashboardServer(("127.0.0.1", 0), self.observer, Watches(self.client, self.registry), self.actions)
        thread = threading.Thread(target=self.server.serve_forever)
        thread.start()
        self.cookie = self.csrf = None
        def stop():
            self.server.shutdown()
            self.server.server_close()
            thread.join(timeout=5)
        self.addCleanup(stop)

    def test_authenticated_registered_history_endpoint_is_pure_and_strict(self):
        query = "/api/conversation?source=%s&fleet=%s&seat=sample-dev" % (self.fleet_id, self.fleet_id)
        before, calls = snapshot(self.repo), len(self.calls())
        self.assertEqual(self.request("GET", query)[0], 403)
        self.login()
        status, headers, raw = self.request("GET", query, authenticated=True)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["messages"], [])
        self.assertEqual(headers["Cache-Control"], "no-store")
        for suffix in ("&author=owner", "&source=duplicate", "&limit=101", "&path=private"):
            self.assertEqual(self.request("GET", query + suffix, authenticated=True)[0], 400)
        self.assertEqual(snapshot(self.repo), before)
        self.assertEqual(len(self.calls()), calls)


class RoutedConversation(fixtures.DashboardCase):
    def test_excluded_unbound_question_warns_collection_without_downgrading_validated_rows(self):
        original = self.send()
        before = self.observer.conversation(self.fleet_id, self.peer, "peer-lead")
        self.assertFalse(before["messages"][0]["incomplete"])
        self.assertEqual(before["messages"][0]["certainty"], "confirmed")
        fixtures.put(self.repo, "owner", "UNBOUND QUESTION BODY MUST STAY EXCLUDED", sender="peer-lead")
        unchanged = snapshot(self.repo)
        after = self.observer.conversation(self.fleet_id, self.peer, "peer-lead")
        self.assertTrue(after["incomplete"])
        self.assertEqual(after["messages"], before["messages"])
        self.assertNotIn("UNBOUND QUESTION BODY", json.dumps(after))
        self.assertEqual(after["intents"], before["intents"])
        self.assertEqual(snapshot(self.repo), unchanged)

    def test_routed_recovered_exact_ack_remains_visible_when_delivery_receipt_is_missing(self):
        self.status_of("w3:p1", "working"); self.sync()
        original = self.send()
        # Give the ordinary CLI an actual delivered debt to settle, then model
        # missing/recovered delivery evidence while retaining its real exact ack.
        messages.append(self.other, messages.delivered_entry(original, "send"))
        env = dict(self.env, HERDR_PANE_ID="w3:p1", HERDR_WORKSPACE_ID="w3")
        code, _, error = self.run_cli("ack", original.id, "--fleet", self.peer_file,
                                     "--as", "peer-lead", env=env, cwd=self.other)
        self.assertEqual(code, 0, error)
        entries = [entry for entry in messages.read(self.other) if entry.get("kind") != "delivered"]
        with open(messages.log_path(self.other), "w") as handle:
            for entry in entries:
                handle.write(json.dumps(entry) + "\n")
        before = (snapshot(self.repo), snapshot(self.other))
        row = self.observer.conversation(self.fleet_id, self.peer, "peer-lead")["messages"][0]
        self.assertEqual(row["status"], "queued")
        self.assertTrue(any(r["kind"] == "ack" and r["exact"] for r in row["receipts"]))
        self.assertEqual(self.prompts_to("peer-lead"), [])
        self.assertEqual((snapshot(self.repo), snapshot(self.other)), before)

    def test_mixed_source_requests_ordinary_mail_and_kickoff_keep_individual_return_tuples(self):
        self.status_of("w3:p1", "working"); self.sync()
        first = self.send()
        third = os.path.join(self.tmp, "third team")
        third_file = os.path.join(third, ".herdr", "fleet.toml")
        os.makedirs(os.path.dirname(third_file))
        with open(third_file, "w") as handle:
            handle.write('workspace="third"\n[[agents]]\nname="spare-lead"\nkind="claude"\n')
        self.registry = Registry([self.registry.path(self.fleet_id), self.peer_file, third_file], self.repo, self.env)
        third_id = next(i for i in self.registry.paths if os.path.realpath(self.registry.fleet(i).repo) == os.path.realpath(third))
        self.observer = Observer(self.client, self.registry, self.env)
        self.actions = OwnerActions(self.client, self.registry, self.env, self.clock)
        target = next(f for f in self.observer.refresh()["fleets"] if f["id"] == self.peer)["seats"][0]
        self.mark("send", "peer-lead")
        result = self.actions.perform({"action":"send","source":third_id,"fleet":self.peer,"seat":"peer-lead",
                    "view":target["view"],"intent":"third_source_synthetic_intent","text":"Third source request"})
        plain = fixtures.put(self.other, "peer-lead", "Ordinary mail", sender="sample-reviewer")
        kickoff = fixtures.put(self.other, "peer-lead", "Synthetic kickoff", sender="herdr-fleet")
        kickoff.queued_by = "kickoff"
        with open(kickoff.path, "w") as handle:
            handle.write(inbox.render_file(kickoff))
        queued = inbox.read_box(self.other, "peer-lead", inbox.NEW)
        text, _, pointer = messages.compose_prompt(queued)
        self.assertFalse(pointer)
        self.assertTrue(text.startswith("Synthetic kickoff"))
        commands = [shlex.split(line) for line in text.splitlines() if line.startswith("herdr-fleet send owner")]
        self.assertEqual({words[words.index("--reply-to")+1] for words in commands}, {first.id, result["id"]})
        self.assertEqual({words[words.index("--fleet")+1] for words in commands},
                         {os.path.realpath(self.registry.path(self.fleet_id)), os.path.realpath(third_file)})
        self.assertIn("Ordinary mail", text)
        self.assertNotIn("--reply-to " + plain.id, text)
        self.assertNotIn("--reply-to " + kickoff.id, text)

    def test_alias_history_uses_canonical_participants_and_deduplicates_mirrored_receipts(self):
        self.write_fleet(self.read_fleet() + '\n[[contacts]]\nname="partner"\nmatch={name="peer-lead"}\n')
        self.mark("send", "peer-lead")
        ctx = self.actions.context(self.registry.fleet(self.fleet_id))
        captured = []
        code = messages.op_send(ctx, ["partner", "Synthetic alias request"], as_name="owner", on_result=captured.append)
        self.assertEqual(code, 0)
        ident = captured[0]["id"]
        result = self.observer.conversation(self.fleet_id, self.peer, "peer-lead")
        self.assertEqual(len(result["messages"]), 1)
        self.assertEqual(result["messages"][0]["id"], ident)
        self.assertEqual(result["messages"][0]["to"], "peer-lead")
        signatures = [json.dumps(r, sort_keys=True) for r in result["messages"][0]["receipts"]]
        self.assertEqual(len(signatures), len(set(signatures)))

    def test_generated_tool_reply_returns_to_source_owner_thread_and_not_target_owner_mailbox(self):
        original = self.send()
        source_file = os.path.realpath(self.registry.path(self.fleet_id))
        env = dict(self.env, HERDR_PANE_ID="w3:p1", HERDR_WORKSPACE_ID="w3")
        command = next(line for line in self.prompts_to("peer-lead")[0].splitlines() if line.startswith("herdr-fleet "))
        words = shlex.split(command)[1:]
        words[words.index("ANSWER_FILE")] = self.write_file("answer.txt", "Synthetic agent-tool answer")
        code, _, error = self.run_cli(*words, env=env, cwd=self.other)
        self.assertEqual(code, 0, error)
        self.assertEqual(inbox.read_box(self.other, "owner", inbox.NEW) + inbox.read_box(self.other, "owner", inbox.CUR), [])
        answer, = inbox.read_box(self.repo, "owner", inbox.NEW) + inbox.read_box(self.repo, "owner", inbox.CUR)
        self.assertEqual((answer.sender, answer.reply_to), ("peer-lead", original.id))
        # Pure GET needs no new native operation after the ordinary fake CLI reply.
        before = (snapshot(self.repo), snapshot(self.other))
        result = self.observer.conversation(self.fleet_id, self.peer, "peer-lead")
        self.assertEqual([m["id"] for m in result["messages"]], [original.id, answer.id])
        self.assertEqual(result["messages"][1]["body"], "Synthetic agent-tool answer")
        self.assertEqual((snapshot(self.repo), snapshot(self.other)), before)

    def test_invalid_legacy_reference_never_scans_unapproved_owner_mailbox(self):
        original = self.send()
        outside = os.path.join(self.tmp, "unapproved")
        secret = fixtures.put(outside, "owner", "UNAPPROVED PRIVATE BODY", sender="peer-lead")
        secret.reply_to = original.id
        receipt = messages.ack_entry("peer-lead", "owner", ident=original.id)
        receipt.update(via="reply", reply_id=secret.id, reply_fleet=os.path.realpath(outside))
        messages.append(self.repo, receipt)
        header = self.observer.conversations.header
        def checked(path, box):
            self.assertFalse(path.startswith(outside), "followed an unapproved reference")
            return header(path, box)
        with mock.patch.object(self.observer.conversations, "header", side_effect=checked):
            result = self.observer.conversation(self.fleet_id, self.peer, "peer-lead")
        self.assertTrue(result["incomplete"])
        self.assertNotIn("UNAPPROVED PRIVATE BODY", json.dumps(result))
        self.assertEqual(len(result["messages"]), 1)
        self.assertFalse(any(r["kind"] == "ack" for r in result["messages"][0]["receipts"]))

    def test_current_remote_membership_never_imports_unlinked_local_owner_history(self):
        item = fixtures.put(self.repo, "owner", "PRIVATE LOCAL HISTORY", sender="peer-lead")
        messages.append(self.repo, messages.send_entry(item, "queued"))
        for file_present in (True, False):
            if not file_present:
                os.unlink(item.path)
            result = self.observer.conversation(self.fleet_id, self.peer, "peer-lead")
            self.assertEqual(result["messages"], [])
            self.assertNotIn("PRIVATE LOCAL HISTORY", json.dumps(result))

    def test_route_free_same_id_send_in_peer_log_cannot_confirm_routed_queue(self):
        self.status_of("w3:p1", "working"); self.sync()
        original = self.send()
        local = copy.copy(original)
        local.origin_fleet = local.destination_fleet = local.requested_to = None
        local.dashboard_chat = None
        messages.append(self.other, messages.send_entry(local, "sent"))
        result = self.observer.conversation(self.fleet_id, self.peer, "peer-lead")
        self.assertEqual(result["messages"][0]["status"], "queued")
        self.assertFalse(any(r["kind"] == "delivered" for r in result["messages"][0]["receipts"]))
        self.assertTrue(result["messages"][0]["incomplete"])

    def test_both_counterparts_require_strict_supported_provenance_version(self):
        from herdr_fleet.dashboard_provenance import validated
        original = self.send()
        target = inbox.resolve(self.other, original.id, mailbox="peer-lead")
        for invalid in (True, 1.0, "1"):
            changed = copy.copy(original)
            changed.dashboard_chat = dict(original.dashboard_chat, v=invalid)
            with open(original.path, "w") as handle:
                handle.write(inbox.render_file(changed))
            self.assertIsNone(validated(target), invalid)
            self.assertNotIn("--reply-to", inbox.direct_text(target))

    def test_changed_paired_provenance_suppresses_guidance_and_unapproved_reference_never_reads_root(self):
        from herdr_fleet.dashboard_provenance import validated
        original = self.send()
        authoritative = inbox.resolve(self.other, original.id, mailbox="peer-lead")
        authoritative.dashboard_chat = dict(authoritative.dashboard_chat, intent="different_synthetic_intent_01")
        with open(authoritative.path, "w") as handle:
            handle.write(inbox.render_file(authoritative))
        self.assertIsNone(validated(original))
        self.assertNotIn("--reply-to", inbox.render_for_print([original], None))
        result = self.observer.conversation(self.fleet_id, self.peer, "peer-lead")
        self.assertTrue(result["incomplete"])
        self.assertTrue(all(m["body"] is None for m in result["messages"]))

    def setUp(self):
        super().setUp()
        self.other = os.path.join(self.tmp, "partner's sample")
        os.makedirs(os.path.join(self.other, ".herdr"))
        self.peer_file = os.path.join(self.other, ".herdr", "fleet.toml")
        with open(self.peer_file, "w") as handle:
            handle.write('workspace="partner"\n[[agents]]\nname="peer-lead"\nkind="claude"\n')
        fake_herdr.add_workspace(self.world, "w3", "partner", self.other)
        fake_herdr.put_agent(self.world, "w3:p1", "claude", "peer-lead")
        self.sync()
        self.registry = Registry([self.registry.path(self.fleet_id), self.peer_file], self.repo, self.env)
        self.observer = Observer(self.client, self.registry, self.env)
        self.actions = OwnerActions(self.client, self.registry, self.env, self.clock)
        self.peer = next(i for i in self.registry.paths if i != self.fleet_id)

    def send(self):
        fleet = next(f for f in self.observer.refresh()["fleets"] if f["id"] == self.peer)
        payload = {"action": "send", "intent": "routed_synthetic_intent_001", "source": self.fleet_id,
                   "fleet": self.peer, "seat": "peer-lead", "view": fleet["seats"][0]["view"],
                   "text": "Synthetic routed question"}
        self.mark("send", "peer-lead")
        result = self.actions.perform(payload)
        return inbox.resolve(self.repo, result["id"], mailbox="peer-lead")

    def test_routed_return_and_legacy_exact_answer_are_joined_once_without_other_mail(self):
        original = self.send()
        text = self.prompts_to("peer-lead")[0]
        words = shlex.split(next(line for line in text.splitlines() if line.startswith("herdr-fleet ")))
        self.assertEqual(words[words.index("--fleet") + 1], os.path.realpath(self.registry.path(self.fleet_id)))
        reply = fixtures.put(self.other, "owner", "Legacy actual answer", sender="peer-lead")
        reply.reply_to = original.id
        with open(reply.path, "w") as handle:
            handle.write(inbox.render_file(reply))
        messages.append(self.other, messages.send_entry(reply, "queued"))
        receipt = messages.ack_entry("peer-lead", "owner", ident=original.id)
        receipt.update(via="reply", reply_id=reply.id, reply_fleet=os.path.realpath(self.other))
        messages.append(self.repo, receipt)
        fixtures.put(self.other, "owner", "Unrelated private owner mail", sender="peer-lead")
        before = (snapshot(self.repo), snapshot(self.other))
        thread = self.observer.conversation(self.fleet_id, self.peer, "peer-lead")
        self.assertEqual([m["id"] for m in thread["messages"]], [original.id, reply.id])
        self.assertEqual(thread["messages"][1]["body"], "Legacy actual answer")
        self.assertNotIn("source_file", json.dumps(thread))
        self.assertEqual((snapshot(self.repo), snapshot(self.other)), before)
