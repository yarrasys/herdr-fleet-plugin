"""Decision Desk: synthetic files, fake Herdr and existing HTTP seams only."""
from __future__ import annotations

import copy
import datetime
import json
import os
from unittest import mock
import threading

from herdr_fleet import inbox, messages
from herdr_fleet.dashboard_data import Observer, Registry
from herdr_fleet import gitinfo
from herdr_fleet.dashboard_actions import Journal, OwnerActions
import fake_herdr
from test_section40 import snapshot
import test_section41 as fixtures


class Attention(fixtures.DashboardCase):
    def test_selected_owner_inspection_rejects_a_header_id_that_does_not_match_its_filename(self):
        original = fixtures.put(self.repo, "owner", "MISMATCHED PRIVATE BODY", sender="sample-dev")
        renamed = os.path.join(os.path.dirname(original.path), "20261101T100000Z-def123.md")
        os.rename(original.path, renamed)
        with self.assertRaises(fixtures.DataError):
            self.observer.listing(self.fleet_id, "messages", "owner", ref=original.id)

    def test_malformed_metadata_types_are_isolated_without_breaking_healthy_observation(self):
        self.write_file(".herdr/fleet.state.json", json.dumps({"agents": {"sample-dev": {"limited_also": True}}}))
        result = self.observer.refresh()
        self.assertFalse(result["fleets"][0]["stale"])
        for field in ("kind", "origin_fleet", "delivery"):
            record = {"kind": "send", "from": "owner", "to": "sample-dev",
                      "time": inbox.iso_local(inbox.now_utc()), "id": "20261101T100000Z-abc123", "delivery": "unconfirmed"}
            record[field] = []
            self.write_file(".herdr/messages.jsonl", json.dumps(record) + "\n")
            result = self.observer.refresh()
            self.assertFalse(result["fleets"][0]["stale"])
            self.assertTrue(result["attention"]["incomplete"])

    def test_invalid_complete_log_schema_and_partial_tail_are_source_incomplete(self):
        for raw in ('{"kind":"send"}\n', '{"kind":"send",'):
            self.write_file(".herdr/messages.jsonl", raw)
            result = self.observer.refresh()
            self.assertFalse(result["fleets"][0]["stale"])
            self.assertTrue(result["attention"]["incomplete"])

    def test_unrelated_log_corruption_does_not_reopen_an_independently_confirmed_send(self):
        payload = self.payload(); self.mark("send", "sample-dev")
        result = self.actions.perform(payload); self.assertEqual(result["status"], "delivered"); self.mark()
        with open(messages.log_path(self.repo), "a") as handle:
            handle.write("unrelated invalid completed line\n")
        attention = self.observer.refresh()["attention"]
        self.assertTrue(attention["incomplete"])
        self.assertFalse(any(r["type"] == "send_issue" and r.get("message") == result["id"] for r in attention["items"]))

    def test_candidate_limit_is_explicit_and_does_not_hide_another_category(self):
        import herdr_fleet.dashboard_attention as attention
        self.status_of("w2:p2", "blocked"); self.sync()
        for n in range(5):
            fixtures.put(self.repo, "owner", "PRIVATE", ident="20261101T100000Z-%06x" % n)
        with mock.patch.object(attention, "PER_FLEET", 3):
            result = self.observer.refresh()["attention"]
        self.assertTrue(result["counts_capped"]); self.assertTrue(result["incomplete"])
        filtered = self.observer.attention_page("all", category="native_dialog")
        self.assertEqual(filtered["matched_count"], 1)
        self.assertEqual(filtered["counts"]["owner_message"], 3)

    def test_oversized_journal_and_dead_or_permission_denied_watcher_remain_pure(self):
        self.write_file(".herdr/dashboard-intents.json", "x" * 1048577)
        self.write_file(".herdr/watch.lock", json.dumps({"pid": 12345, "since": inbox.iso_local(inbox.now_utc())}))
        before = snapshot(self.repo)
        with mock.patch("os.kill", side_effect=PermissionError("denied")):
            result = self.observer.refresh()["attention"]
        self.assertTrue(result["incomplete"])
        self.assertTrue(any(r["reason"] == "watcher_liveness_unknown" for r in result["items"]))
        with mock.patch("os.kill", side_effect=ProcessLookupError("missing")):
            stopped = self.observer.refresh()["attention"]
        self.assertTrue(any(r["reason"] == "watcher_stopped" for r in stopped["items"]))
        self.assertEqual(snapshot(self.repo), before)

    def test_intentionally_disabled_watcher_is_not_attention_but_stale_observation_still_is(self):
        self.write_file(".herdr/fleet.toml", "watch=false\n" + self.read_fleet())
        result = self.observer.refresh()
        self.assertFalse(result["fleets"][0]["watcher"].get("enabled", True))
        self.assertFalse(any(r["type"] == "observation_issue" for r in result["attention"]["items"]))
        self.write_file(".herdr/fleet.state.json", "corrupt")
        stale = self.observer.refresh()
        self.assertTrue(any(r["reason"] == "fleet_observation_stale" for r in stale["attention"]["items"]))

    def test_limited_also_live_kind_and_truncated_debt_evidence_are_qualified(self):
        now = inbox.iso_local(inbox.now_utc())
        self.write_file(".herdr/fleet.state.json", json.dumps({"agents": {"sample-dev": {
            "limited": {"kind": "kimi", "at": now}, "limited_also": [{"kind": "claude", "at": now}]}}}))
        owner = fixtures.put(self.repo, "owner", "PRIVATE", sender="sample-dev", which=inbox.CUR)
        messages.append(self.repo, messages.send_entry(owner, "sent"))
        read = self.observer.cache.log
        def capped(path):
            entries, _ = read(path)
            return entries, True
        with mock.patch.object(self.observer.cache, "log", side_effect=capped):
            result = self.observer.refresh()["attention"]
        self.assertTrue(result["counts_capped"]); self.assertTrue(result["incomplete"])
        self.assertTrue(next(r for r in result["items"] if r.get("message") == owner.id)["incomplete"])
        self.assertEqual(len([r for r in result["items"] if r["type"] == "provider_hold"]), 1)

    def test_selected_owner_detail_revalidates_body_header_after_filtering(self):
        original = fixtures.put(self.repo, "owner", "Original body", sender="sample-dev")
        original_read = self.observer.cache.bytes
        def replacing(path, *args, **kwargs):
            if os.path.realpath(path) == os.path.realpath(original.path):
                wrong = copy.copy(original); wrong.sender = "sample-reviewer"; wrong.body = "OTHER PRIVATE BODY"
                with open(path, "w") as handle:
                    handle.write(inbox.render_file(wrong))
            return original_read(path, *args, **kwargs)
        with mock.patch.object(self.observer.cache, "bytes", side_effect=replacing):
            result = self.observer.listing(self.fleet_id, "messages", "owner", ref=original.id)
        self.assertIsNone(result["items"][0]["body"])
        self.assertTrue(result["items"][0]["incomplete"])
        self.assertNotIn("OTHER PRIVATE", json.dumps(result))

    def test_recorded_negative_journal_without_body_stays_metadata_only_and_is_never_replayed(self):
        Journal(self.repo).update("negative_synthetic_intent", "a" * 64,
                                  metadata={"action": "send", "source": self.fleet_id,
                                            "fleet": self.fleet_id, "seat": "sample-dev"})
        Journal(self.repo).update("negative_synthetic_intent", "a" * 64,
                                  result={"status": "refused", "certainty": "confirmed", "code": 1})
        before = snapshot(self.repo)
        result = self.observer.refresh()["attention"]
        issue = next(r for r in result["items"] if r["type"] == "send_issue")
        self.assertEqual(issue["status"], "refused"); self.assertTrue(issue["incomplete"])
        self.assertEqual(issue["actions"], [{"kind": "inspect_log", "fleet": self.fleet_id}])
        self.assertNotIn("message", issue); self.assertEqual(self.prompts_to("sample-dev"), [])
        self.assertEqual(snapshot(self.repo), before)

    def test_stable_ids_do_not_depend_on_age_or_snapshot_order_and_missing_time_stays_unknown(self):
        first = fixtures.put(self.repo, "owner", "One", sender="sample-dev")
        second = fixtures.put(self.repo, "owner", "Two", sender="sample-reviewer")
        before = self.observer.refresh()["attention"]
        after = self.observer.refresh()["attention"]
        self.assertEqual([r["id"] for r in before["items"]], [r["id"] for r in after["items"]])
        self.assertEqual({r["message"] for r in after["items"] if r["type"] == "owner_message"}, {first.id, second.id})
    def test_reading_keeps_delivered_debt_and_acknowledged_new_mail_remains_unread_only(self):
        original = fixtures.put(self.repo, "owner", "Synthetic request", sender="sample-dev")
        messages.append(self.repo, messages.send_entry(original, "queued"))
        self.assertEqual(next(r for r in self.observer.refresh()["attention"]["items"]
                              if r.get("message") == original.id)["status"], "unread")
        code, _, error = self.run_cli("inbox", "--as", "owner", "--read", original.id)
        self.assertEqual(code, 0, error); self.mark()
        row = next(r for r in self.observer.refresh()["attention"]["items"] if r.get("message") == original.id)
        self.assertEqual(row["status"], "awaiting_reply")
        unread = fixtures.put(self.repo, "owner", "Already acknowledged, still unread", sender="sample-reviewer")
        messages.append(self.repo, messages.send_entry(unread, "sent"))
        messages.append(self.repo, messages.ack_entry("owner", "sample-reviewer", ident=unread.id))
        row = next(r for r in self.observer.refresh()["attention"]["items"] if r.get("message") == unread.id)
        self.assertEqual(row["status"], "unread")

    def test_missing_conflicting_or_due_owner_headers_cannot_create_a_reply_route(self):
        old = inbox.now_utc() - datetime.timedelta(days=2)
        delivered = fixtures.put(self.repo, "owner", "PRIVATE", sender="sample-dev", when=old,
                                 ttl=old + datetime.timedelta(hours=1), which=inbox.CUR)
        messages.append(self.repo, messages.send_entry(delivered, "sent"))
        conflict = copy.copy(delivered); conflict.sender = "sample-reviewer"
        inbox.write(self.repo, conflict, inbox.NEW)
        due = fixtures.put(self.repo, "owner", "DUE BODY", ttl=old)
        rows = self.observer.refresh()["attention"]["items"]
        row = next(r for r in rows if r.get("message") == delivered.id)
        self.assertEqual(row["status"], "unavailable"); self.assertEqual(row["actions"], [])
        self.assertNotIn(due.id, [r.get("message") for r in rows])
        os.unlink(conflict.path)
        row = next(r for r in self.observer.refresh()["attention"]["items"] if r.get("message") == delivered.id)
        self.assertEqual(row["status"], "awaiting_reply", "queue TTL cannot expire a delivered debt")
        os.unlink(delivered.path)
        row = next(r for r in self.observer.refresh()["attention"]["items"] if r.get("message") == delivered.id)
        self.assertEqual(row["status"], "unavailable"); self.assertEqual(row["actions"], [])

    def test_filter_precedes_response_cap_and_counts_precede_category_filter(self):
        self.status_of("w2:p2", "blocked"); self.sync()
        for n in range(210):
            fixtures.put(self.repo, "owner", "PRIVATE", sender="sample-dev", ident="20261101T100000Z-%06x" % n)
        all_ = self.observer.refresh()["attention"]
        self.assertEqual(len(all_["items"]), 200); self.assertTrue(all_["truncated"])
        filtered = self.observer.attention_page("selected", self.fleet_id, "native_dialog")
        self.assertEqual(len(filtered["items"]), 1)
        self.assertEqual(filtered["matched_count"], 1)
        self.assertEqual(filtered["counts"]["owner_message"], 210)
        self.assertFalse(filtered["counts_capped"])

    def test_recorded_send_issue_uses_exact_id_and_new_confirmed_delivery_supersedes_uncertainty(self):
        original = fixtures.put(self.repo, "sample-dev", "PRIVATE SEND BODY", sender="owner")
        messages.append(self.repo, messages.send_entry(original, "unconfirmed"))
        result = self.observer.refresh()["attention"]
        issues = [r for r in result["items"] if r["type"] == "send_issue"]
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["message"], original.id)
        self.assertEqual(issues[0]["actions"][0]["kind"], "inspect_send")
        self.assertNotIn("PRIVATE", json.dumps(result))
        outcome = messages.delivered_entry(original, "send")
        outcome["time"] = inbox.iso_local(inbox.now_utc() + datetime.timedelta(seconds=1))
        messages.append(self.repo, outcome)
        self.assertFalse(any(r["type"] == "send_issue" for r in self.observer.refresh()["attention"]["items"]))

    def test_owner_attention_keeps_each_delivered_request_and_pending_mail_without_bodies_or_writes(self):
        first = fixtures.put(self.repo, "owner", "PRIVATE FIRST BODY", sender="sample-dev", which=inbox.CUR)
        second = fixtures.put(self.repo, "owner", "PRIVATE SECOND BODY", sender="sample-dev", which=inbox.CUR)
        pending = fixtures.put(self.repo, "owner", "PRIVATE PENDING BODY", sender="sample-reviewer")
        for item in (first, second):
            messages.append(self.repo, messages.send_entry(item, "sent"))
        before = snapshot(self.repo)
        with mock.patch.object(inbox, "load", side_effect=AssertionError("body load")):
            result = self.observer.refresh()
        rows = [r for r in result.get("attention", {}).get("items", []) if r["type"] == "owner_message"]
        self.assertEqual({r["message"] for r in rows}, {first.id, second.id, pending.id})
        self.assertEqual({r["status"] for r in rows}, {"unread", "awaiting_reply"})
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertEqual(snapshot(self.repo), before)
        messages.append(self.repo, messages.ack_entry("owner", "sample-dev", ident=first.id))
        remaining = self.observer.refresh()["attention"]["items"]
        self.assertEqual({r["message"] for r in remaining if r["type"] == "owner_message"}, {second.id, pending.id})

    def test_native_provider_and_observation_causes_are_separate_and_limits_are_active_only(self):
        self.status_of("w2:p2", "blocked"); self.sync()
        now = inbox.now_utc()
        record = {"kind": "claude", "at": inbox.iso_local(now),
                  "resets_at": inbox.iso_local(now + datetime.timedelta(hours=1))}
        self.write_file(".herdr/fleet.state.json", json.dumps({"agents": {"sample-dev": {"limited": record}}}))
        result = self.observer.refresh()["attention"]
        self.assertEqual({r["type"] for r in result["items"]},
                         {"native_dialog", "provider_hold", "observation_issue"})
        native = next(r for r in result["items"] if r["type"] == "native_dialog")
        self.assertEqual(native["actions"][0]["kind"], "watch")
        self.assertEqual(native["seat"], "sample-dev")
        for change in ({"released": True}, {"kind": "kimi"}, {"resets_at": inbox.iso_local(now)},
                       {"resets_at": "bad"}):
            changed = dict(record, **change)
            self.write_file(".herdr/fleet.state.json", json.dumps({"agents": {"sample-dev": {"limited": changed}}}))
            self.assertFalse(any(r["type"] == "provider_hold" for r in self.observer.refresh()["attention"]["items"]))


class Builds(fixtures.DashboardCase):
    def test_registry_failure_is_unknown_and_does_not_leak_raw_diagnostics_or_stale_fleets(self):
        self.world.setdefault("canned", []).append({"prefix": ["plugin", "list", "--json"], "exit": 13,
                                                    "stderr": "PRIVATE REGISTRY DIAGNOSTIC", "stdout": ""})
        self.sync(); result = self.observer.refresh()
        self.assertEqual(result["build"]["comparison"], "unknown")
        self.assertFalse(result["fleets"][0]["stale"])
        self.assertNotIn("PRIVATE REGISTRY", json.dumps(result))

    def test_running_identity_is_captured_once_and_restart_guidance_does_not_expand_narrow_registration(self):
        initial = {"version": "0.22.0", "commit": "a" * 40}
        with mock.patch("herdr_fleet.build_identity.current", return_value=initial):
            observer = Observer(self.client, self.registry, self.env)
        with mock.patch("herdr_fleet.build_identity.current", side_effect=AssertionError("recaptured running")):
            result = observer.refresh()["build"]
        self.assertEqual(result["running"], initial); self.assertEqual(result["restart"], "original_command")
        registry = Registry([], self.repo, self.env, all_fleets=True)
        self.assertEqual(Observer(self.client, registry, self.env).builds.restart, "all_fleets")
        registry = Registry([self.registry.path(self.fleet_id)], self.repo, self.env, all_fleets=True)
        self.assertEqual(Observer(self.client, registry, self.env).builds.restart, "original_command")

    def test_registry_cache_is_sixty_seconds_and_unknown_never_makes_a_fleet_stale(self):
        import herdr_fleet.dashboard_build as build_module
        clock = [0.0]
        with mock.patch.object(build_module.time, "monotonic", side_effect=lambda: clock[0]):
            first = self.observer.refresh()["build"]
            self.assertEqual(first["comparison"], "unknown")
            self.world["plugins"] = [{"plugin_id": "yarrasys.fleet", "enabled": True,
                                      "version": first["running"]["version"],
                                      "source": {"resolved_commit": "c" * 40}}]
            self.sync(); clock[0] = 59.9
            self.assertEqual(self.observer.refresh()["build"]["installed"], first["installed"])
            clock[0] = 60
            self.assertEqual(self.observer.refresh()["build"]["installed"]["commit"], "c" * 40)
            self.assertEqual(sum(c["argv"] == ["plugin", "list", "--json"] for c in self.calls()), 2)
            self.world["plugins"].append(dict(self.world["plugins"][0])); self.sync(); clock[0] = 120
            result = self.observer.refresh()
            self.assertEqual(result["build"]["comparison"], "unknown")
            self.assertFalse(result["fleets"][0]["stale"])

    def test_registry_wrong_ids_types_and_unbounded_versions_are_not_installed_authority(self):
        cases = [[{"plugin_id": "yarrasys.fleet.preview", "enabled": True, "version": "1.0.0"}],
                 [{"plugin_id": "yarrasys.fleet", "enabled": "true", "version": "1.0.0"}],
                 [{"plugin_id": "yarrasys.fleet", "enabled": True, "version": "x" * 10000,
                   "source": {"resolved_commit": "bad"}}]]
        for records in cases:
            self.world["plugins"] = records; self.sync()
            observer = Observer(self.client, self.registry, self.env)
            self.assertEqual(observer.refresh()["build"]["installed"], {"version": None, "commit": None})

    def test_running_and_installed_differ_without_executing_installed_code_or_failing_fleets(self):
        self.world["plugins"] = [{"plugin_id": "yarrasys.fleet", "enabled": True, "version": "0.21.2",
                                  "source": {"resolved_commit": "b" * 40}, "manifest_path": "/unread/manifest.toml"}]
        self.sync()
        running = {"version": "0.22.0", "commit": "a" * 40}
        with mock.patch("herdr_fleet.build_identity.current", return_value=running):
            observer = Observer(self.client, self.registry, self.env)
        before = snapshot(self.repo)
        with mock.patch.object(gitinfo, "run_git", side_effect=AssertionError("installed Git lookup")):
            result = observer.refresh()
        build = result.get("build", {})
        self.assertEqual(build.get("comparison"), "different")
        self.assertEqual(build["running"], running)
        self.assertEqual(build["installed"], {"version": "0.21.2", "commit": "b" * 40})
        self.assertFalse(result["fleets"][0]["stale"])
        self.assertEqual(snapshot(self.repo), before)
        self.assertNotIn("/unread/manifest.toml", json.dumps(result))


class AttentionHTTP(fixtures.DashboardCase):
    request = fixtures.HTTPAuthority.request
    login = fixtures.HTTPAuthority.login

    def setUp(self):
        super().setUp(); self.observer.refresh()
        self.server = fixtures.DashboardServer(("127.0.0.1", 0), self.observer,
                                               fixtures.Watches(self.client, self.registry), self.actions)
        self.thread = threading.Thread(target=self.server.serve_forever); self.thread.start()
        self.cookie = self.csrf = None

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=5)
        super().tearDown()

    def test_attention_auth_strict_query_and_snapshot_are_zero_effect_reads(self):
        query = "/api/attention?scope=selected&fleet=" + self.fleet_id + "&category=native_dialog"
        self.assertEqual(self.request("GET", query)[0], 403)
        self.login(); before, calls = snapshot(self.repo), len(self.calls())
        for path in (query, "/api/snapshot"):
            status, headers, raw = self.request("GET", path, authenticated=True)
            self.assertEqual(status, 200); self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertIsInstance(json.loads(raw), dict)
        for bad in ("&category=owner_message", "&path=private", "&limit=500"):
            self.assertEqual(self.request("GET", query + bad, authenticated=True)[0], 400)
        self.assertEqual(self.request("GET", "/api/attention?scope=selected", authenticated=True)[0], 400)
        self.assertEqual(snapshot(self.repo), before); self.assertEqual(len(self.calls()), calls)


class RoutedAttention(fixtures.DashboardCase):
    def setUp(self):
        super().setUp()
        self.other = os.path.join(self.tmp, "partner sample")
        os.makedirs(os.path.join(self.other, ".herdr"))
        path = os.path.join(self.other, ".herdr", "fleet.toml")
        with open(path, "w") as handle:
            handle.write('workspace="partner"\n[[agents]]\nname="peer-lead"\nkind="claude"\n')
        fake_herdr.add_workspace(self.world, "w3", "partner", self.other)
        fake_herdr.put_agent(self.world, "w3:p1", "claude", "peer-lead"); self.sync()
        self.registry = Registry([self.registry.path(self.fleet_id), path], self.repo, self.env)
        self.observer = Observer(self.client, self.registry, self.env)
        self.peer = next(k for k in self.registry.paths if k != self.fleet_id)

    def test_valid_routed_owner_request_uses_authoritative_destination_not_history_mirror(self):
        from herdr_fleet import crossfleet
        original = inbox.Message("20261101T100000Z-abc123", "sample-dev", "owner",
                                 inbox.iso_local(inbox.now_utc()), "PRIVATE OWNER ROUTED BODY",
                                 origin_fleet=os.path.realpath(self.repo), destination_fleet=os.path.realpath(self.other),
                                 requested_to="owner", route_bytes=128, origin_send_order=0, destination_send_order=0)
        destination = inbox.write(self.other, original, inbox.NEW)
        history = copy.copy(original); history.history_only = True
        source = inbox.write(self.repo, history, inbox.CUR)
        self.assertEqual(crossfleet.counterpart(self.repo, source), os.path.realpath(self.other))
        for root in (self.repo, self.other): messages.append(root, messages.send_entry(original, "sent"))
        before = (snapshot(self.repo), snapshot(self.other))
        rows = [r for r in self.observer.refresh()["attention"]["items"] if r["type"] == "owner_message"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["fleet"], self.peer)
        self.assertEqual(rows[0]["actions"], [{"kind": "inspect_owner", "source": self.peer, "message": destination.id}])
        self.assertEqual((snapshot(self.repo), snapshot(self.other)), before)

    def test_routed_send_issues_are_one_source_owned_item_and_pair_conflict_cannot_confirm(self):
        code, _, error = self.run_cli("send", "peer-lead", "PRIVATE ROUTED BODY", "--as", "owner", "--queue")
        self.assertEqual(code, 0, error); self.mark()
        original, = inbox.read_box(self.repo, "peer-lead", inbox.CUR)
        negative = messages.delivered_entry(original, "send", "unconfirmed")
        for root in (self.repo, self.other): messages.append(root, negative)
        before = (snapshot(self.repo), snapshot(self.other))
        result = self.observer.refresh()["attention"]
        issues = [r for r in result["items"] if r["type"] == "send_issue"]
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["fleet"], self.fleet_id)
        self.assertEqual(issues[0]["actions"][0]["fleet"], self.peer)
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertEqual((snapshot(self.repo), snapshot(self.other)), before)
        destination = inbox.resolve(self.other, original.id, mailbox="peer-lead")
        destination.reply_to = "20261101T100000Z-ffffff"
        with open(destination.path, "w") as handle: handle.write(inbox.render_file(destination))
        positive = messages.delivered_entry(original, "send")
        positive["time"] = inbox.iso_local(inbox.now_utc() + datetime.timedelta(seconds=2))
        messages.append(self.repo, positive)
        issue = next(r for r in self.observer.refresh()["attention"]["items"] if r["type"] == "send_issue")
        self.assertTrue(issue["incomplete"]); self.assertEqual(issue["status"], "unconfirmed")
        self.assertEqual(issue["actions"][0]["kind"], "inspect_log")
        # An unvalidated origin label cannot silently suppress the peer's
        # recorded uncertain outcome as though it were a proved mirror.
        peer_issues = [r for r in self.observer.attention_page("selected", self.peer)["items"] if r["type"] == "send_issue"]
        self.assertTrue(peer_issues)
        self.assertTrue(all(r["actions"][0]["kind"] == "inspect_log" for r in peer_issues))

    def test_bad_journal_or_log_is_source_local_and_never_marks_healthy_fleet_stale(self):
        fixtures.put(self.repo, "owner", "PRIVATE SOURCE BODY", sender="sample-dev")
        fixtures.put(self.other, "owner", "PRIVATE PEER BODY", sender="peer-lead")
        self.write_file(".herdr/dashboard-intents.json", "corrupt")
        with open(messages.log_path(self.repo), "w") as handle: handle.write("invalid complete log line\n")
        result = self.observer.refresh()
        self.assertFalse(any(f["stale"] for f in result["fleets"]))
        self.assertTrue(self.observer.attention_page("selected", self.fleet_id)["incomplete"])
        self.assertFalse(self.observer.attention_page("selected", self.peer)["incomplete"])
        self.assertEqual(sum(r["type"] == "owner_message" for r in result["attention"]["items"]), 2)
        self.assertNotIn("PRIVATE", json.dumps(result["attention"]))


class RegistryTransport(fixtures.unittest.TestCase):
    def test_demo_has_all_five_synthetic_types_and_filtered_metadata_without_native_registry(self):
        from herdr_fleet.dashboard_attention import TYPES
        with mock.patch.object(fixtures.Herdr, "plugin_list", side_effect=AssertionError("Demo registry")):
            demo = fixtures.Demo(); result = demo.snapshot()
            self.assertEqual({r["type"] for r in result["attention"]["items"]}, set(TYPES))
            native = demo.attention_page("all", category="native_dialog")
            self.assertEqual(native["matched_count"], 1); self.assertEqual(native["counts"]["owner_message"], 1)
            self.assertEqual(native["items"][0]["actions"][0]["kind"], "watch")
        self.assertNotIn("body", json.dumps(result["attention"]))

    def test_bounded_registry_reader_closes_stdin_and_kills_reaps_oversized_output(self):
        from herdr_fleet.herdr import registry_runner, HerdrError
        import herdr_fleet.herdr as transport
        process, selector = mock.Mock(), mock.Mock()
        process.stdout.fileno.return_value = 17; process.poll.return_value = None
        selector.select.return_value = [(object(), 1)]
        with mock.patch.object(transport.subprocess, "Popen", return_value=process) as spawn, \
                mock.patch.object(transport.selectors, "DefaultSelector", return_value=selector), \
                mock.patch.object(transport.os, "read", return_value=b"x" * 1048577):
            with self.assertRaises(HerdrError): registry_runner(["/fake/herdr", "plugin", "list", "--json"])
        self.assertEqual(spawn.call_args.kwargs["stdin"], transport.subprocess.DEVNULL)
        process.kill.assert_called_once(); process.wait.assert_called_once_with(timeout=1)
        process.stdout.close.assert_called_once(); selector.close.assert_called_once()

    def test_registry_timeout_releases_its_child_without_reading_another_surface(self):
        import herdr_fleet.herdr as transport
        process, selector = mock.Mock(), mock.Mock()
        process.poll.return_value = None; selector.select.return_value = []
        with mock.patch.object(transport.subprocess, "Popen", return_value=process), \
                mock.patch.object(transport.selectors, "DefaultSelector", return_value=selector):
            with self.assertRaises(transport.HerdrError):
                transport.registry_runner(["/fake/herdr", "plugin", "list", "--json"])
        process.kill.assert_called_once(); process.wait.assert_called_once_with(timeout=1)
        process.stdout.close.assert_called_once()
