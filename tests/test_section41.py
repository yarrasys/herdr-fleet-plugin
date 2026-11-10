"""Local dashboard boundaries; synthetic files, fake Herdr and isolated HTTP only."""
from __future__ import annotations

import io
import json
import subprocess
import base64
import copy
import contextlib
import http.client
import os
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest import mock
import unittest

from herdr_fleet.cli import build_parser
from herdr_fleet.herdr import Herdr, HerdrError
from herdr_fleet.dashboard import DashboardServer, Authority
from herdr_fleet.dashboard_data import Registry, Observer, DataError, FileCache
from herdr_fleet.dashboard_actions import OwnerActions, Journal, Conflict
from herdr_fleet.dashboard_terminal import Frames, Watches
from herdr_fleet.dashboard_demo import Demo
from herdr_fleet import inbox, messages
import fake_herdr
from support import LiveFleet
from test_section40 import snapshot
from test_inbox import put, log_entries, box


def frame(seq=1, full=True, text="\x1b[32mSynthetic ANSI\x1b[0m\r\n", **extra):
    doc = {"type": "terminal.frame", "encoding": "ansi", "seq": seq, "full": full,
           "bytes": base64.b64encode(text.encode()).decode(), "width": 80, "height": 24}
    doc.update(extra)
    return json.dumps(doc).encode()


class DashboardCase(LiveFleet):
    def setUp(self):
        super().setUp()
        self.sync()
        self.client = Herdr("/fake/herdr", fake_herdr.Fake(self.world_path, self.log_path,
                             inbox=os.path.join(self.repo, ".herdr", "inbox"),
                             fleet=os.path.join(self.repo, ".herdr", "fleet.toml")))
        self.registry = Registry([os.path.join(self.repo, ".herdr", "fleet.toml")], self.repo, self.env)
        self.fleet_id = next(iter(self.registry.paths))
        self.observer = Observer(self.client, self.registry, self.env)
        self.actions = OwnerActions(self.client, self.registry, self.env, self.clock)
        self.mark()

    def sync(self):
        with open(self.world_path, "w") as handle:
            json.dump(self.world, handle)

    def mark(self, action=None, target=None):
        with open(self.log_path, "a") as handle:
            handle.write(json.dumps({"command": ["dashboard"], "dashboard_action": action,
                                     "dashboard_target": target}) + "\n")

    def seat(self, name="sample-dev"):
        data = self.observer.refresh()
        fleet = next(f for f in data["fleets"] if f["id"] == self.fleet_id)
        return next(s for s in fleet["seats"] if s["name"] == name)

    def payload(self, **changes):
        seat = self.seat()
        payload = {"action": "send", "intent": "intent_synthetic_0001", "source": self.fleet_id,
                   "fleet": self.fleet_id, "seat": seat["name"], "view": seat["view"], "text": "Synthetic owner request"}
        payload.update(changes)
        return payload


class Observation(DashboardCase):
    def test_invalid_registration_and_nonfleet_agents_do_not_hide_healthy_fleet(self):
        bad=self.write_file("bad-fleet.toml","not = [valid")
        registry=Registry([self.registry.path(self.fleet_id),bad],self.repo,self.env,True)
        observer=Observer(self.client,registry,self.env)
        before=snapshot(self.repo)
        result=observer.refresh()
        self.assertEqual(len(result["fleets"]),2)
        self.assertEqual(sum(not f["stale"] for f in result["fleets"]),1)
        self.assertEqual(snapshot(self.repo),before)

    def test_permission_denied_watcher_liveness_is_unknown_not_stopped(self):
        self.write_file(".herdr/watch.lock",'{"pid":12345,"since":"2026-11-01T10:00:00Z"}')
        with mock.patch("os.kill",side_effect=PermissionError("synthetic denied")):
            self.assertIsNone(self.observer.refresh()["fleets"][0]["watcher"]["running"])
        with mock.patch("os.kill",side_effect=ProcessLookupError("synthetic absent")):
            self.assertIs(self.observer.refresh()["fleets"][0]["watcher"]["running"],False)

    def test_observation_never_writes_or_reads_bodies_or_updates_labels_seen(self):
        message = put(self.repo, "sample-dev", "BODY MUST NOT BE READ")
        owner = put(self.repo, "owner", "OWNER BODY MUST NOT BE READ")
        before = snapshot(self.repo)
        with mock.patch.object(inbox, "load", side_effect=AssertionError("body read")):
            result = self.observer.refresh()
        self.assertEqual(snapshot(self.repo), before)
        text = json.dumps(result)
        self.assertNotIn("MUST NOT", text)
        self.assertEqual(result["fleets"][0]["seats"][1]["queued"], 1)
        self.assertEqual(result["fleets"][0]["owner"][0]["id"], owner.id)
        self.assertEqual([call["argv"] for call in self.calls()], [["agent", "list"]])

    def test_invalid_state_retains_last_success_and_then_recovers(self):
        first = self.observer.refresh()["fleets"][0]
        self.write_file(".herdr/fleet.state.json", "partial{")
        before = snapshot(self.repo)
        second = self.observer.refresh()["fleets"][0]
        self.assertTrue(second["stale"])
        self.assertEqual(second["seats"], first["seats"])
        self.assertEqual(snapshot(self.repo), before)
        self.write_file(".herdr/fleet.state.json", '{"agents":{}}')
        self.assertFalse(self.observer.refresh()["fleets"][0]["stale"])

    def test_missing_state_and_partial_log_are_safe_and_bounded(self):
        os.unlink(os.path.join(self.repo, ".herdr", "fleet.state.json"))
        path = self.write_file(".herdr/messages.jsonl", '{"kind":"send",')
        before = snapshot(self.repo)
        self.assertFalse(self.observer.refresh()["fleets"][0]["stale"])
        self.assertEqual(snapshot(self.repo), before)
        with open(path, "w") as handle:
            handle.write(json.dumps({"id":"20261101T100000Z-abc123","time":"2026-11-01T10:00:00Z",
                                      "from":"sample-dev","to":"owner","kind":"send","delivery":"queued"}) + "\n")
            handle.write('{"partial":')
        result = self.observer.listing(self.fleet_id, "log")
        self.assertEqual(len(result["items"]), 1)

    def test_listing_is_explicit_pure_inspection_with_inert_untrusted_body(self):
        body = '<script>throw "not executable"</script>\n<img src=x onerror=alert(1)>'
        message = put(self.repo, "owner", body)
        before = snapshot(self.repo)
        result = self.observer.listing(self.fleet_id, "messages", "owner", ref=message.id)
        self.assertEqual(result["items"][0]["body"], body)
        self.assertEqual(snapshot(self.repo), before)
        with self.assertRaises(DataError):
            self.observer.listing(self.fleet_id, "messages", "../outside")

    def test_roster_deduplicates_canonical_roots_and_opt_in_discovery(self):
        alternate = self.write_file(".herdr/alternate.toml", self.read_fleet())
        registry = Registry([os.path.join(self.repo, ".herdr", "fleet.toml"), alternate], self.repo, self.env)
        self.assertEqual(len(registry.paths), 1)
        other = os.path.join(self.tmp, "partner")
        os.makedirs(os.path.join(other, ".herdr"))
        with open(os.path.join(other, ".herdr", "fleet.toml"), "w") as handle:
            handle.write('[[agents]]\nname="peer-lead"\nkind="claude"\n')
        agent = {"name": "peer-lead", "cwd": other}
        registry.reconcile([agent])
        self.assertEqual(len(registry.paths), 1)
        registry.all_fleets = True
        before = snapshot(other)
        registry.reconcile([agent])
        self.assertEqual(len(registry.paths), 2)
        self.assertEqual(snapshot(other), before)
        registry.reconcile([])
        self.assertEqual(len(registry.paths), 1)

    def test_context_is_opt_in_and_only_numbers_leave_the_reader(self):
        with mock.patch("herdr_fleet.context_size.seat_context", return_value={"tokens": 123, "window": 1000,
                        "at": "2026-11-01T00:00:00Z", "source": "synthetic", "path": "/private/transcript", "body": "secret"}) as reader:
            self.observer.refresh()
            reader.assert_not_called()
            self.observer.context = True
            result = self.observer.refresh()
        self.assertNotIn("transcript", json.dumps(result))
        self.assertNotIn("secret", json.dumps(result))
        self.assertEqual(result["fleets"][0]["seats"][0]["context"]["tokens"], 123)


class OwnerIntents(DashboardCase):
    def test_local_send_and_completed_retry_do_not_duplicate_prompt_or_store_body(self):
        payload = self.payload()
        self.mark("send", "sample-dev")
        result = self.actions.perform(payload)
        self.assertEqual(result["status"], "delivered")
        again = self.actions.perform(payload)
        self.assertTrue(again["replayed"])
        self.assertEqual(again["id"], result["id"])
        self.assertEqual(len(self.prompts_to("sample-dev")), 1)
        with open(Journal(self.repo).path) as handle:
            raw = handle.read()
        self.assertNotIn(payload["text"], raw)
        self.assertNotIn("first_line", raw)
        with self.assertRaises(Conflict):
            self.actions.perform(dict(payload, text="changed request"))

    def test_in_progress_intent_never_reissues_after_interruption(self):
        from herdr_fleet.dashboard_data import digest
        payload = self.payload()
        Journal(self.repo).update(payload["intent"], digest(payload))
        before = snapshot(self.repo)
        result = self.actions.perform(payload)
        self.assertEqual(result["certainty"], "unconfirmed")
        self.assertTrue(result["replayed"])
        self.assertEqual(self.prompts_to("sample-dev"), [])
        self.assertEqual(snapshot(self.repo), before)

    def test_stale_target_and_unknown_flags_fail_before_any_intent_or_mail(self):
        payload = self.payload()
        before = snapshot(self.repo)
        for changed in (dict(payload, view="stale"), dict(payload, argv=["agent", "start"]),
                        dict(payload, author="owner"), dict(payload, now=True), dict(payload, read=True)):
            with self.assertRaises(DataError):
                self.actions.perform(changed)
        self.assertEqual(snapshot(self.repo), before)
        self.assertEqual(self.prompts_to("sample-dev"), [])

    def test_working_blocked_and_typed_input_use_existing_queued_holds(self):
        for i, status in enumerate(("working", "blocked", "typing")):
            self.status_of("w2:p2", "idle" if status == "typing" else status)
            fake_herdr.type_in(self.world, "w2:p2", "owner draft" if status == "typing" else "")
            self.sync()
            payload = self.payload(intent="intent_synthetic_hold_%d" % i)
            self.mark("send", "sample-dev")
            result = self.actions.perform(payload)
            self.assertEqual(result["status"], "queued")
        self.assertEqual(self.prompts_to("sample-dev"), [])

    def test_selected_owner_read_does_not_consume_other_mail(self):
        one = put(self.repo, "owner", "first")
        two = put(self.repo, "owner", "second")
        self.mark("read")
        result = self.actions.perform({"action":"read","source":self.fleet_id,"message":one.id,"intent":"intent_owner_read_001"})
        self.assertEqual(result["status"], "read")
        self.assertEqual(box(self.repo, "owner", inbox.NEW), [two.id])
        self.assertEqual(self.calls(), [])


class HTTPAuthority(DashboardCase):
    def setUp(self):
        super().setUp()
        self.observer.refresh()
        self.server = DashboardServer(("127.0.0.1", 0), self.observer, Watches(self.client, self.registry), self.actions)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.cookie = None
        self.csrf = None

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        super().tearDown()

    def request(self, method, path, body=None, headers=None, authenticated=False):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        request_headers = {"Host": self.server.host, "Origin": self.server.origin}
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        if authenticated:
            request_headers.update({"Cookie": self.cookie, "X-Fleet-CSRF": self.csrf})
        request_headers.update(headers or {})
        request_headers={key:value for key,value in request_headers.items() if value is not None}
        connection.request(method, path, None if body is None else json.dumps(body).encode(), request_headers)
        response = connection.getresponse()
        data = response.read()
        result = (response.status, dict(response.getheaders()), data)
        connection.close()
        return result

    def login(self):
        status, headers, raw = self.request("POST", "/api/session", {"capability": self.server.authority.launch})
        self.assertEqual(status, 200)
        self.cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.csrf = json.loads(raw)["csrf"]
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])

    def test_valid_capability_reuses_same_browser_session_and_ports_have_distinct_cookies(self):
        self.login()
        old_cookie,old_csrf=self.cookie,self.csrf
        status,headers,raw=self.request("POST","/api/session",{"capability":self.server.authority.launch},
                                       {"Cookie":old_cookie})
        self.assertEqual(status,200)
        self.assertEqual(headers["Set-Cookie"].split(";",1)[0],old_cookie)
        self.assertEqual(json.loads(raw)["csrf"],old_csrf)
        self.assertEqual(self.request("GET","/api/snapshot",authenticated=True)[0],200)
        self.assertNotEqual(Authority().cookie_name,self.server.authority.cookie_name)

    def test_overlapping_first_exchanges_converge_in_both_response_orders(self):
        authority=self.server.authority
        barrier=threading.Barrier(2)
        pairs=[]
        def exchange():
            barrier.wait()
            pairs.append(authority.exchange(authority.launch))
        threads=[threading.Thread(target=exchange) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=2)
        self.assertEqual(len(pairs),2)
        for responses in (pairs,list(reversed(pairs))):
            cookie=authority.cookie_name+"="+responses[-1][0]
            for _,csrf in responses:
                authority.authenticate(cookie,csrf)
        self.assertEqual(len(authority.sessions),1)
        before=dict(authority.sessions)
        with self.assertRaises(PermissionError):
            authority.exchange("not the capability")
        self.assertEqual(authority.sessions,before)
        token,csrf=pairs[0]
        authority.sessions[token]=(csrf,0)
        replacement=authority.exchange(authority.launch)
        self.assertNotEqual(replacement,pairs[0])
        with self.assertRaises(PermissionError):
            authority.authenticate(authority.cookie_name+"="+token,csrf)

    def test_missing_null_origin_bad_json_type_and_oversized_body_cannot_create_intents(self):
        self.login()
        payload=self.payload()
        before=snapshot(self.repo)
        for headers,status in (({"Origin":None},403),({"Origin":"null"},403),({"Content-Type":"text/plain"},400)):
            self.assertEqual(self.request("POST","/api/action",payload,headers,authenticated=True)[0],status)
        self.assertEqual(self.request("POST","/api/action",{"text":"x"*50000},authenticated=True)[0],400)
        self.assertEqual(snapshot(self.repo),before)

    def test_duplicate_host_and_anti_forgery_headers_are_rejected(self):
        self.login()
        for duplicate in ("Host","X-Fleet-CSRF"):
            connection=http.client.HTTPConnection("127.0.0.1",self.server.server_address[1],timeout=5)
            connection.putrequest("GET","/api/snapshot",skip_host=True)
            for key,value in {"Host":self.server.host,"Origin":self.server.origin,"Cookie":self.cookie,
                              "X-Fleet-CSRF":self.csrf}.items():
                connection.putheader(key,value)
                if key==duplicate: connection.putheader(key,value)
            connection.endheaders()
            response=connection.getresponse()
            self.assertEqual(response.status,403)
            response.read();connection.close()

    def test_favicon_is_fixed_empty_and_asset_symlink_escape_is_denied(self):
        self.assertEqual(self.request("GET","/favicon.ico")[0],204)
        asset_dir=os.path.join(self.tmp,"assets")
        os.mkdir(asset_dir)
        outside=self.write_file("not-public.txt","private synthetic fixture")
        os.symlink(outside,os.path.join(asset_dir,"escaped.txt"))
        with mock.patch("herdr_fleet.dashboard.ASSET_ROOT",asset_dir),mock.patch("herdr_fleet.dashboard.ASSETS",
                          {"/escaped.txt":("escaped.txt","text/plain")}):
            status,_,raw=self.request("GET","/escaped.txt")
        self.assertEqual(status,400)
        self.assertNotIn(b"private synthetic fixture",raw)

    def test_assets_bootstrap_and_authenticated_snapshot_have_restrictive_headers(self):
        status, headers, raw = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertNotIn(self.server.authority.launch.encode(), raw)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("unsafe-inline", headers["Content-Security-Policy"])
        self.login()
        before = snapshot(self.repo)
        status, headers, raw = self.request("GET", "/api/snapshot", authenticated=True)
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(raw)["demo"])
        self.assertEqual(snapshot(self.repo), before)

    def test_failed_auth_origin_host_and_forgery_have_zero_side_effects(self):
        self.login()
        payload = self.payload()
        before = snapshot(self.repo)
        calls = len(self.calls())
        variants = [({}, False), ({"Origin":"http://foreign.example"},True),
                    ({"Host":"localhost:%d" % self.server.server_address[1]},True),
                    ({"X-Fleet-CSRF":"wrong"},True), ({"Sec-Fetch-Site":"cross-site"},True)]
        for headers, auth in variants:
            status, _, _ = self.request("POST", "/api/action", payload, headers, authenticated=auth)
            self.assertEqual(status, 403)
        self.assertEqual(snapshot(self.repo), before)
        self.assertEqual(len(self.calls()), calls)
        self.assertFalse(os.path.exists(Journal(self.repo).path))

    def test_foreign_and_wrong_capability_exchanges_fail_and_restart_invalidates_sessions(self):
        self.assertEqual(self.request("POST", "/api/session", {"capability":"incorrect"})[0], 403)
        self.assertEqual(self.request("POST", "/api/session", {"capability":self.server.authority.launch},
                                      {"Origin":"null"})[0], 403)
        self.assertEqual(self.request("POST", "/api/session", {"capability":"unicode-\u03bb"})[0], 403)
        self.login()
        self.server.authority = Authority()
        self.assertEqual(self.request("GET", "/api/snapshot", authenticated=True)[0], 403)

    def test_traversal_and_arbitrary_command_surfaces_are_rejected(self):
        self.login()
        before = snapshot(self.repo)
        for path in ("/../herdr_fleet/state.py", "/%2e%2e/AGENTS.md", "/vendor/../app.js", "/api/execute"):
            self.assertNotEqual(self.request("GET", path, authenticated=True)[0], 200)
        for path in ("/api/terminal/input", "/api/control", "/api/attach", "/api/restart"):
            self.assertEqual(self.request("POST", path, {"command":"ignored"}, authenticated=True)[0], 404)
        self.assertEqual(snapshot(self.repo), before)

    def test_only_authenticated_validated_action_can_send(self):
        self.login()
        payload = self.payload()
        self.mark("send", "sample-dev")
        status, _, raw = self.request("POST", "/api/action", payload, authenticated=True)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["status"], "delivered")
        self.assertEqual(len(self.prompts_to("sample-dev")), 1)


class ObserveTransport(unittest.TestCase):
    def test_agent_attach_and_takeover_never_reach_runner(self):
        calls=[]
        client=Herdr("/fake/herdr",lambda argv:(calls.append(argv) or (0,"{}","")))
        for tail in ((),("--takeover",)):
            with self.subTest(tail=tail),self.assertRaises(HerdrError):
                client.run("agent","attach","sample-dev",*tail)
        self.assertEqual(calls,[])
    def test_control_attach_and_unchecked_observe_are_rejected_before_execution(self):
        calls = []
        client = Herdr("/fake/herdr", lambda argv: (calls.append(argv) or (0, "{}", "")))
        for argv in (("terminal", "attach", "sample"), ("terminal", "session", "control", "sample"),
                     ("terminal", "session", "attach", "sample"),
                     ("terminal", "session", "observe", "sample", "--cols", "80", "--rows", "24")):
            with self.subTest(argv=argv), self.assertRaises(HerdrError) as caught:
                client.run(*argv)
            self.assertEqual(caught.exception.code, "forbidden_command")
        self.assertEqual(calls, [])

    def test_observe_validates_target_and_dimensions_before_spawning(self):
        calls = []
        client = Herdr("/fake/herdr", stream_runner=lambda *a, **kw: calls.append((a, kw)))
        for target, cols, rows in (("--current", 80, 24), ("sample; run", 80, 24),
                                   ("term_sample", 0, 24), ("term_sample", 80, 10000)):
            with self.subTest(target=target, cols=cols, rows=rows), self.assertRaises(HerdrError):
                client.terminal_observe(target, cols, rows)
        self.assertEqual(calls, [])


class DashboardCLI(unittest.TestCase):
    def test_repeated_explicit_fleets_and_demo_are_first_class_cli_options(self):
        args = build_parser().parse_args(["dashboard", "--fleet", "examples/team/.herdr/fleet.toml",
                                          "--fleet", "other/.herdr/fleet.toml", "--all-fleets", "--port", "0"])
        self.assertEqual(args.fleets, ["examples/team/.herdr/fleet.toml", "other/.herdr/fleet.toml"])
        self.assertTrue(args.all_fleets)
        demo = build_parser().parse_args(["dashboard", "--demo"])
        self.assertTrue(demo.demo)


class FrameValidation(unittest.TestCase):
    def test_full_delta_and_closed_keep_real_ansi_bytes(self):
        decoder = Frames()
        first = decoder.decode(frame())
        self.assertIn(b"\x1b[32m", base64.b64decode(first["bytes"]))
        self.assertEqual(decoder.decode(frame(seq=2, full=False))["seq"], 2)
        self.assertEqual(decoder.decode(b'{"type":"terminal.closed","undocumented":"ignored"}')["type"], "closed")

    def test_malformed_encoding_base64_dimensions_and_oversized_frames_fail(self):
        bad = [b"not json", b"[]", frame(encoding="base64"), frame(bytes="!invalid"),
               frame(width=100000), frame(height=False), frame(seq=True), b"x"*262145]
        for record in bad:
            with self.subTest(record=record[:100]), self.assertRaises(DataError):
                Frames().decode(record)

    def test_initial_delta_duplicate_and_gapped_delta_are_not_replayed(self):
        with self.assertRaises(DataError):
            Frames().decode(frame(full=False))
        for bad in (frame(seq=1, full=False), frame(seq=3, full=False)):
            decoder = Frames()
            decoder.decode(frame())
            with self.assertRaises(DataError):
                decoder.decode(bad)
        decoder = Frames()
        decoder.decode(frame())
        self.assertEqual(decoder.decode(frame(seq=4, full=True))["seq"], 4)


class IntentConcurrency(DashboardCase):
    def test_confirmed_concurrent_read_is_eligible_but_uncertain_history_is_not(self):
        original=put(self.repo,"owner","Concurrent question",sender="sample-dev")
        messages.append(self.repo,messages.send_entry(original,"queued"))
        payload=self.payload(reply_to=original.id,read=True)
        update=Journal.update
        moved=[]
        def concurrent_read(journal,intent,fingerprint,result=None,metadata=None,progress=None):
            value=update(journal,intent,fingerprint,result,metadata,progress)
            if metadata is not None and not moved:
                with inbox.locked(self.repo,"owner"):
                    inbox.move(self.repo,original,inbox.CUR)
                    messages.append(self.repo,messages.delivered_entry(original,"inbox"))
                moved.append(True)
            return value
        self.mark("send","sample-dev")
        with mock.patch.object(Journal,"update",concurrent_read):
            self.assertEqual(self.actions.perform(payload)["status"],"delivered")
        self.mark()
        unknown=put(self.repo,"owner","Uncertain history",sender="sample-dev",which=inbox.CUR)
        payload=self.payload(reply_to=unknown.id,intent="uncertain_history_intent_01")
        before=snapshot(self.repo)
        with self.assertRaises(DataError): self.actions.perform(payload)
        self.assertEqual(snapshot(self.repo),before)

    def test_journal_corrupt_schema_and_durability_failure_fail_before_effects(self):
        payload=self.payload()
        for entry in (None,{},{"fingerprint":"wrong","metadata":{},"result":None}):
            self.write_file(".herdr/dashboard-intents.json",json.dumps({"version":1,"intents":{"other_intent_000000":entry}}))
            before=snapshot(self.repo)
            with self.assertRaises(DataError): self.actions.perform(payload)
            self.assertEqual(snapshot(self.repo),before)
        os.unlink(Journal(self.repo).path)
        before=snapshot(self.repo)
        with mock.patch("os.fsync",side_effect=OSError("synthetic durability failure")):
            with self.assertRaises(OSError): self.actions.perform(payload)
        self.assertEqual(snapshot(self.repo),before)
        self.assertEqual(self.prompts_to("sample-dev"),[])

    def test_failed_selected_read_never_claims_success_or_runs_reply(self):
        for operation in ("read","send"):
            for failure in ("returned_failure","move_and_receipt_failure","move_only"):
                with self.subTest(operation=operation,failure=failure):
                    self.mark()
                    original=put(self.repo,"owner","Question requiring an explicit read",sender="sample-dev")
                    messages.append(self.repo,messages.send_entry(original,"queued"))
                    payload=(self.payload(reply_to=original.id,read=True,intent="read_failure_"+operation+"_"+failure)
                             if operation=="send" else {"action":"read","source":self.fleet_id,"message":original.id,
                                  "intent":"read_failure_"+operation+"_"+failure})
                    self.mark("send","sample-dev") if operation=="send" else self.mark("read")
                    move=inbox.move
                    append=messages.append
                    def failed_move(repo,message,box):
                        if message.id==original.id: raise OSError("synthetic move failure")
                        return move(repo,message,box)
                    def failed_append(repo,entry):
                        if entry.get("id")==original.id: raise OSError("synthetic receipt failure")
                        return append(repo,entry)
                    with contextlib.ExitStack() as stack:
                        if failure=="returned_failure":
                            stack.enter_context(mock.patch("herdr_fleet.mailroom.op_inbox",return_value=1))
                        else:
                            stack.enter_context(mock.patch.object(inbox,"move",side_effect=failed_move))
                            if failure=="move_and_receipt_failure":
                                stack.enter_context(mock.patch.object(messages,"append",side_effect=failed_append))
                        result=self.actions.perform(payload)
                    self.assertEqual(result["certainty"],"unconfirmed",result)
                    self.assertNotEqual(result["status"],"read")
                    with open(Journal(self.repo).path) as handle:
                        journal=json.load(handle)["intents"][payload["intent"]]
                    self.assertNotEqual(journal["metadata"].get("read_status"),"read")
        self.assertEqual(self.prompts_to("sample-dev"),[])

    def test_null_retained_intent_is_corruption_not_an_unused_id(self):
        payload=self.payload()
        self.write_file(".herdr/dashboard-intents.json",json.dumps({"version":1,"intents":{payload["intent"]:None}}))
        before=snapshot(self.repo)
        self.mark("send","sample-dev")
        with self.assertRaises(DataError):
            self.actions.perform(payload)
        self.assertEqual(snapshot(self.repo),before)
        self.assertEqual(self.prompts_to("sample-dev"),[])

    def test_expiry_after_reservation_cannot_be_misreported_read_or_send_reply(self):
        for operation in ("read","send"):
            self.mark()
            original=put(self.repo,"owner","Synthetic question",sender="sample-dev")
            messages.append(self.repo,messages.send_entry(original,"queued"))
            payload=(self.payload(reply_to=original.id,read=True,intent="race_expiry_reply_"+operation)
                     if operation=="send" else {"action":"read","source":self.fleet_id,
                        "message":original.id,"intent":"race_expiry_owner_read_01"})
            update=Journal.update
            expired=[]
            def race(journal,intent,fingerprint,result=None,metadata=None,progress=None):
                value=update(journal,intent,fingerprint,result,metadata,progress)
                if result is None and metadata is not None and not expired:
                    with inbox.locked(self.repo,"owner"):
                        inbox.move(self.repo,original,inbox.CUR)
                        messages.append(self.repo,messages.disposal_entry("expired",original,"ttl"))
                    expired.append(True)
                return value
            self.mark("send","sample-dev") if operation=="send" else self.mark("read")
            with mock.patch.object(Journal,"update",race):
                result=self.actions.perform(payload)
            self.assertEqual(result["status"],"expired",result)
        self.assertEqual(self.prompts_to("sample-dev"),[])

    def test_two_threads_reserve_one_send_and_retained_result_survives_stale_view(self):
        payload = self.payload()
        self.mark("send", "sample-dev")
        gate = threading.Barrier(2)
        results, errors = [], []
        def submit():
            try:
                gate.wait(timeout=5)
                results.append(self.actions.perform(copy.deepcopy(payload)))
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=submit) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(len(self.prompts_to("sample-dev")), 1)
        # A completed intent is an outcome query, even after its old view becomes stale.
        self.world["panes"]["w2:p2"]["agent"]["agent_status"] = "working"
        self.world["panes"]["w2:p2"]["agent"]["agent_session"] = {"source":"herdr:claude","value":"synthetic-replacement"}
        self.sync()
        self.assertTrue(self.actions.perform(payload)["replayed"])
        self.assertEqual(len(self.prompts_to("sample-dev")), 1)

    def test_failed_terminal_journal_write_is_uncertain_and_never_resends(self):
        payload = self.payload()
        self.mark("send", "sample-dev")
        update = Journal.update
        def fail_final(journal, intent, fingerprint, result=None, metadata=None):
            if result is not None:
                raise OSError("synthetic final journal failure")
            return update(journal, intent, fingerprint, result, metadata)
        with mock.patch.object(Journal, "update", fail_final):
            result = self.actions.perform(payload)
        self.assertEqual(result["certainty"], "unconfirmed")
        self.assertEqual(self.actions.perform(payload)["certainty"], "unconfirmed")
        self.assertEqual(len(self.prompts_to("sample-dev")), 1)

    def test_strict_reservation_lock_failure_precedes_mailbox_work(self):
        import fcntl
        payload = self.payload()
        before = snapshot(self.repo)
        with mock.patch.object(fcntl, "flock", side_effect=OSError("synthetic lock failure")):
            with self.assertRaises(OSError):
                self.actions.perform(payload)
        self.assertEqual(snapshot(self.repo), before)
        self.assertEqual(self.prompts_to("sample-dev"), [])


class RoutedOwner(DashboardCase):
    def test_later_confirmed_terminal_receipt_resolves_uncertainty_and_queue_only_is_not_delivery(self):
        original=put(self.repo,"sample-dev","Synthetic outcome")
        ctx=self.actions.context(self.registry.fleet(self.source))
        target=SimpleNamespace(destination=SimpleNamespace(repo=self.other),name="sample-dev")
        entry=messages.send_entry(original,"unconfirmed");entry["time"]="2026-11-01T10:00:00+00:00"
        messages.append(self.repo,entry)
        entry=messages.delivered_entry(original,"inbox");entry["time"]="2026-11-01T10:00:01+00:00"
        messages.append(self.other,entry)
        self.assertEqual(messages.send_result(ctx,target,original,1)["status"],"delivered")
        queued=put(self.repo,"sample-dev","Queued only")
        messages.append(self.repo,messages.send_entry(queued,"queued"))
        self.assertEqual(messages.send_result(ctx,target,queued,0)["status"],"queued")

    def test_unconfirmed_engine_callback_and_consumer_failure_do_not_repeat_submission(self):
        self.world["behaviors"]["prompt_errors"]={"sample-dev":"timeout"}
        self.sync()
        ctx=self.actions.context(self.registry.fleet(self.source))
        captured=[]
        self.mark("send","sample-dev")
        code=messages.op_send(ctx,["sample-dev","Synthetic uncertain"],as_name="owner",on_result=captured.append)
        self.assertEqual(code,1)
        self.assertEqual(captured[0]["certainty"],"unconfirmed")
        self.assertEqual(len(self.prompts_to("sample-dev")),1)
        self.world["behaviors"]["prompt_errors"]={}
        self.sync()
        def broken(_): raise OSError("consumer failed")
        code=messages.op_send(ctx,["sample-dev","Synthetic consumer failure"],as_name="owner",on_result=broken)
        self.assertEqual(code,1)
        self.assertEqual(len(self.prompts_to("sample-dev")),2)

    def test_stale_queued_receipt_cannot_downgrade_unconfirmed_in_either_root(self):
        original=put(self.repo,"sample-dev","Synthetic result")
        ctx=self.actions.context(self.registry.fleet(self.source))
        target=SimpleNamespace(destination=SimpleNamespace(repo=self.other),name="sample-dev")
        for source_status,destination_status in (("unconfirmed","queued"),("queued","unconfirmed")):
            for root,status in ((self.repo,source_status),(self.other,destination_status)):
                self.write_file(".herdr/messages.jsonl","") if root==self.repo else None
                entry=messages.send_entry(original,status)
                entry["time"]="2026-11-01T10:00:00Z"
                path=os.path.join(root,".herdr","messages.jsonl")
                with open(path,"w") as handle: handle.write(json.dumps(entry)+"\n")
            result=messages.send_result(ctx,target,original,1)
            self.assertEqual((result["status"],result["certainty"]),("unconfirmed","unconfirmed"))
    def setUp(self):
        super().setUp()
        self.other = os.path.join(self.tmp, "partner")
        os.makedirs(os.path.join(self.other, ".herdr"))
        other_path = os.path.join(self.other, ".herdr", "fleet.toml")
        with open(other_path, "w") as handle:
            handle.write('workspace="partner"\n[[agents]]\nname="peer-lead"\nkind="claude"\n')
        fake_herdr.add_workspace(self.world, "w3", "partner", self.other)
        fake_herdr.put_agent(self.world, "w3:p1", "claude", "peer-lead")
        self.sync()
        self.registry = Registry([os.path.join(self.repo,".herdr","fleet.toml"),other_path], self.repo,self.env)
        self.observer = Observer(self.client,self.registry,self.env)
        self.actions = OwnerActions(self.client,self.registry,self.env,self.clock)
        self.observer.refresh()
        self.source = next(i for i in self.registry.paths if os.path.realpath(self.registry.fleet(i).repo) == os.path.realpath(self.repo))
        self.destination = next(i for i in self.registry.paths if os.path.realpath(self.registry.fleet(i).repo) == os.path.realpath(self.other))

    def test_routed_callback_reports_canonical_owner_and_receipt_based_delivery(self):
        ctx = self.actions.context(self.registry.fleet(self.source))
        captured = []
        self.mark("send", "peer-lead")
        code = messages.op_send(ctx, ["peer-lead","Synthetic routed request"], as_name="owner", on_result=captured.append)
        self.assertEqual(code, 0)
        item, = captured
        self.assertEqual((item["recipient"],item["status"],item["certainty"]),("peer-lead","delivered","confirmed"))
        self.assertEqual(item["source"],os.path.realpath(self.repo))
        self.assertEqual(item["destination"],os.path.realpath(self.other))
        self.assertNotIn("body",item)

    def test_exact_owner_reply_and_explicit_read_use_original_engine_and_debt(self):
        original = put(self.repo,"owner","Synthetic question",sender="sample-dev")
        messages.append(self.repo,messages.send_entry(original,"queued"))
        seat = next(s for f in self.observer.refresh()["fleets"] if f["id"]==self.source for s in f["seats"] if s["name"]=="sample-dev")
        payload={"action":"send","intent":"intent_reply_synthetic_001","source":self.source,"fleet":self.source,
                 "seat":"sample-dev","view":seat["view"],"text":"Synthetic answer","reply_to":original.id,"read":True}
        self.mark("send","sample-dev")
        result=self.actions.perform(payload)
        self.assertEqual(result["status"],"delivered")
        self.assertEqual(box(self.repo,"owner",inbox.NEW),[])
        self.assertFalse(any(d.get("id")==original.id for d in messages.owed(log_entries(self.repo))))
        send=[e for e in log_entries(self.repo) if e.get("from")=="owner" and e.get("kind")=="send"][-1]
        self.assertEqual(send["reply_to"],original.id)

    def test_outside_roster_and_wrong_reply_author_do_not_create_intents(self):
        original=put(self.repo,"owner","Question",sender="sample-reviewer")
        payload=self.payload(reply_to=original.id)
        before=snapshot(self.repo)
        with self.assertRaises(DataError):
            self.actions.perform(payload)
        self.assertEqual(snapshot(self.repo),before)


class ObserveProcesses(DashboardCase):
    def test_quiet_healthy_observer_has_no_age_or_silence_cutoff(self):
        seat=self.seat()
        self.install_stream([frame()],linger=True)
        self.mark("watch","term_w2:p2")
        watches=Watches(self.client,self.registry)
        iterator=watches.frames(self.fleet_id,"sample-dev",seat["view"],80,24,lambda:False)
        next(iterator)
        ticks=[]
        def tick():
            ticks.append(True)
            return len(ticks)<3
        # Advancing the logical clock does not require a slow live wait.
        with mock.patch("herdr_fleet.herdr.time.monotonic",side_effect=lambda:len(ticks)*1000):
            self.assertEqual(list(next(iter(watches.active.values()))[1].lines(threading.Event(),on_tick=tick)),[])
        self.assertEqual(len(ticks),3)
        iterator.close()
        self.assertTrue(all(p.poll() is not None for p in self.spawned))
    def test_idle_rebinding_and_shutdown_release_observers_without_input(self):
        seat=self.seat()
        self.install_stream([frame()],linger=True)
        self.mark("watch","term_w2:p2")
        watches=Watches(self.client,self.registry)
        iterator=watches.frames(self.fleet_id,"sample-dev",seat["view"],80,24,lambda:False)
        next(iterator)
        self.world["panes"]["w2:p2"]["agent"]["agent_session"]={"source":"herdr:claude","value":"replacement"}
        self.sync()
        with self.assertRaises(DataError): list(iterator)
        self.assertTrue(all(p.poll() is not None for p in self.spawned))
        self.mark()
        seat=self.seat()
        self.mark("watch","term_w2:p2")
        iterator=watches.frames(self.fleet_id,"sample-dev",seat["view"],80,24,lambda:False)
        next(iterator);watches.close();iterator.close()
        self.assertEqual(watches.active,{})
        self.assertTrue(all(p.poll() is not None for p in self.spawned))

    def test_observer_capacity_is_bounded_and_malformed_frames_reap_children(self):
        seat=self.seat()
        self.install_stream([frame()],linger=True)
        self.mark("watch","term_w2:p2")
        watches=Watches(self.client,self.registry,max_observers=1)
        first=watches.frames(self.fleet_id,"sample-dev",seat["view"],80,24,lambda:False)
        next(first)
        with self.assertRaises(DataError):
            next(watches.frames(self.fleet_id,"sample-dev",seat["view"],80,24,lambda:False))
        first.close()
        self.install_stream([b"invalid frame"])
        with self.assertRaises(DataError):
            list(watches.frames(self.fleet_id,"sample-dev",seat["view"],80,24,lambda:False))
        self.assertTrue(all(p.poll() is not None for p in self.spawned))
    def install_stream(self, records, exit_code=0, linger=False):
        self.spawned=[]
        def factory(argv, **kwargs):
            self.assertEqual(kwargs["stdin"],subprocess.DEVNULL)
            self.assertEqual(kwargs["stderr"],subprocess.DEVNULL)
            self.assertEqual(argv[1:4],["terminal","session","observe"])
            with open(self.log_path,"a") as handle:
                handle.write(json.dumps({"argv":argv[1:],"exit":0,"info":{}})+"\n")
            script="import sys,time;sys.stdout.write(sys.argv[1]);sys.stdout.flush();"
            script += "time.sleep(60);" if linger else ""
            script += "sys.exit(%d)" % exit_code
            child=subprocess.Popen([sys.executable,"-u","-c",script,b"\n".join(records).decode()+"\n"],
                                   stdin=kwargs["stdin"],stdout=kwargs["stdout"],stderr=kwargs["stderr"],bufsize=0)
            self.spawned.append(child)
            return child
        self.client.stream_runner=factory

    def test_full_delta_closed_stream_reaps_child_and_sends_no_input(self):
        seat=self.seat()
        self.install_stream([frame(),frame(seq=2,full=False),b'{"type":"terminal.closed"}'])
        self.mark("watch","term_w2:p2")
        watches=Watches(self.client,self.registry)
        result=list(watches.frames(self.fleet_id,"sample-dev",seat["view"],80,24,lambda:False))
        self.assertEqual([r["type"] for r in result],["frame","frame","closed"])
        self.assertTrue(all(p.poll() is not None for p in self.spawned))
        self.assertEqual(watches.active,{})

    def test_disconnect_stops_and_reaps_idle_observer(self):
        seat=self.seat()
        self.install_stream([frame()],linger=True)
        self.mark("watch","term_w2:p2")
        watches=Watches(self.client,self.registry)
        connection=[True]
        iterator=watches.frames(self.fleet_id,"sample-dev",seat["view"],80,24,lambda:not connection[0])
        self.assertEqual(next(iterator)["type"],"frame")
        connection[0]=False
        self.assertEqual(list(iterator)[-1]["type"],"ended")
        self.assertTrue(all(p.poll() is not None for p in self.spawned))

    def test_stale_target_never_starts_an_observer(self):
        self.install_stream([frame()])
        self.mark("watch","term_w2:p2")
        with self.assertRaises(DataError):
            list(Watches(self.client,self.registry).frames(self.fleet_id,"sample-dev","old-view",80,24,lambda:False))
        self.assertEqual(self.spawned,[])

    def test_unsupported_observer_is_visibly_unavailable_without_affecting_observation(self):
        seat=self.seat()
        self.install_stream([],exit_code=2)
        self.mark("watch","term_w2:p2")
        result=list(Watches(self.client,self.registry).frames(self.fleet_id,"sample-dev",seat["view"],80,24,lambda:False))
        self.assertEqual(result[-1]["type"],"error")
        self.assertIn("unavailable",result[-1]["message"].lower())
        self.mark()
        self.assertFalse(self.observer.refresh()["fleets"][0]["stale"])


class DemoIsolation(unittest.TestCase):
    def test_authored_hold_reply_replay_and_frames_are_isolated(self):
        from herdr_fleet.dashboard_demo import TEAM, REV, HOLD_AT, M_ASK
        demo=Demo()
        with mock.patch("herdr_fleet.dashboard_demo.time.monotonic",return_value=100):
            demo.replay()
        with mock.patch("herdr_fleet.dashboard_demo.time.monotonic",return_value=105):
            demo.reply()
            self.assertFalse(demo.snapshot()["hold"])
        with mock.patch("herdr_fleet.dashboard_demo.time.monotonic",return_value=100+HOLD_AT+50):
            data=demo.snapshot()
            self.assertTrue(data["hold"])
            self.assertEqual(data["stage"],2)
            self.assertEqual(data["fleets"][0]["owner"][0]["id"],M_ASK)
            self.assertTrue(demo.listing(TEAM,"messages","owner")["items"])
            iterator=demo.frames(TEAM,REV,"demo-view-"+REV,80,24,lambda:True)
            first=next(iterator)
            self.assertIn(b"SYNTHETIC DEMO",base64.b64decode(first["bytes"]))
            self.assertEqual(list(iterator),[])
            demo.reply()
        with mock.patch("herdr_fleet.dashboard_demo.time.monotonic",return_value=100+HOLD_AT+80):
            done=demo.snapshot()
            self.assertFalse(done["hold"])
            self.assertEqual(done["stage"],6)
            self.assertTrue(done["fleets"][0]["owner"][0]["fyi"])
            self.assertGreater(len(demo.listing(TEAM,"log")["items"]),4)
            demo.replay()
            self.assertEqual(demo.snapshot()["stage"],0)
        with self.assertRaises(ValueError): next(demo.frames(TEAM,REV,"wrong",80,24,lambda:False))
        with self.assertRaises(ValueError): next(demo.frames("other",REV,"demo-view-"+REV,80,24,lambda:False))
        demo.close()
        self.assertEqual(list(demo.frames(TEAM,REV,"demo-view-"+REV,80,24,lambda:False)),[])

    def test_cli_demo_avoids_preflight_files_and_live_adapters_and_closes_foreground_server(self):
        from herdr_fleet.dashboard import serve
        output=io.StringIO()
        ctx=SimpleNamespace(stdout=output,say=lambda line:output.write(line+"\n"))
        args=build_parser().parse_args(["dashboard","--demo"])
        server=mock.Mock(launch_url="http://127.0.0.1:1/#synthetic")
        server.serve_forever.side_effect=KeyboardInterrupt
        with mock.patch("herdr_fleet.dashboard.DashboardServer",return_value=server), \
                mock.patch("herdr_fleet.dashboard.preflight",side_effect=AssertionError("no live preflight")), \
                mock.patch("herdr_fleet.dashboard.Registry",side_effect=AssertionError("no live registry")), \
                mock.patch("herdr_fleet.dashboard.OwnerActions",side_effect=AssertionError("no live action")):
            self.assertEqual(serve(ctx,args),0)
        server.server_close.assert_called_once()
        self.assertIn("synthetic",output.getvalue())


class DemoHTTP(unittest.TestCase):
    request=HTTPAuthority.request
    login=HTTPAuthority.login

    def setUp(self):
        demo=Demo()
        self.server=DashboardServer(("127.0.0.1",0),demo,demo,demo=True)
        self.thread=threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.cookie=self.csrf=None

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=5)

    def test_demo_empty_actions_are_auth_gated_and_never_accept_live_text(self):
        for path in ("/api/demo/replay","/api/demo/reply"):
            self.assertEqual(self.request("POST",path,{})[0],403)
        self.login()
        for path in ("/api/demo/replay","/api/demo/reply"):
            self.assertEqual(self.request("POST",path,{"text":"must not leave browser"},authenticated=True)[0],404)
            status,_,raw=self.request("POST",path,{},authenticated=True)
            self.assertEqual((status,json.loads(raw)),(200,{"demo":True}))
        self.assertEqual(self.request("POST","/api/action",{},authenticated=True)[0],403)


class RetainedIntentProgress(DashboardCase):
    def test_crash_retains_known_read_and_outgoing_ids_without_reissuing(self):
        from herdr_fleet.dashboard_data import digest
        payload=self.payload()
        journal=Journal(self.repo)
        fingerprint=digest(payload)
        journal.update(payload["intent"],fingerprint)
        journal.update(payload["intent"],fingerprint,progress={"read_id":"20261101T100000Z-aabbcc",
                       "read_status":"read","outgoing_id":"20261101T100001Z-ddeeff"})
        result=self.actions.perform(payload)
        self.assertEqual(result["read_id"],"20261101T100000Z-aabbcc")
        self.assertEqual(result["id"],"20261101T100001Z-ddeeff")
        self.assertEqual(result["certainty"],"unconfirmed")
        self.assertEqual(self.prompts_to("sample-dev"),[])


class WatchAuthorityLifetime(unittest.TestCase):
    def test_silent_watch_rechecks_authority_disconnect_and_shutdown_without_age_limit(self):
        from herdr_fleet.dashboard import Handler
        handler=Handler.__new__(Handler)
        authority=Authority()
        token,_=authority.exchange(authority.launch)
        authority.sessions[token]=(authority.sessions[token][0],10**12)
        handler.server=SimpleNamespace(authority=authority,stop_event=threading.Event())
        handler.connection=object()
        handler.wfile=io.BytesIO()
        handler.send_response=mock.Mock()
        handler.headers_safe=mock.Mock()
        handler.send_header=mock.Mock()
        handler.end_headers=mock.Mock()
        checked=[]
        closed=[]
        def frames(fleet,seat,view,cols,rows,disconnected):
            try:
                checked.append(not disconnected())
                yield {"type":"frame"}
                # A selected but silent terminal may be watched for days.
                checked.append(not disconnected())
                authority.sessions[token]=(authority.sessions[token][0],0)
                checked.append(disconnected())
                authority.sessions[token]=(authority.sessions[token][0],10**12)
                handler.server.stop_event.set()
                checked.append(disconnected())
                handler.server.stop_event.clear()
                with mock.patch("select.select",return_value=([handler.connection],[],[])):
                    handler.connection=mock.Mock()
                    handler.connection.recv.return_value=b""
                    checked.append(disconnected())
            finally:
                closed.append(True)
        handler.server.watches=SimpleNamespace(frames=frames)
        with mock.patch("select.select",return_value=([],[],[])), \
                mock.patch("herdr_fleet.dashboard.time.monotonic",return_value=10**9):
            handler.stream({"fleet":"sample","seat":"sample-dev","view":"view","cols":80,"rows":24},token)
        self.assertEqual(checked,[True]*5)
        self.assertEqual(closed,[True])
