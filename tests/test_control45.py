"""Authenticated human-control boundaries, using synthetic native children only."""
from __future__ import annotations

import contextlib
import io
import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from herdr_fleet import permits
from herdr_fleet.herdr import Herdr, HerdrError
import test_section41 as dashboard_tests


class NativeAdapter(unittest.TestCase):
    def test_controller_requires_exclusive_creator_thread_permit_and_fixed_transport(self):
        with tempfile.TemporaryDirectory() as folder:
            domain = permits.endpoints({"HERDR_SOCKET_PATH": folder + "/api.sock"})[0]
            values = [("W", "w7"), ("N", "sample-dev"), ("P", "w7:p1"), ("T", "terminal_sample")]
            calls = []
            def spawn(argv, **kwargs):
                calls.append((argv, kwargs))
                return subprocess.Popen([sys.executable, "-u", "-c",
                    "import sys; print('ready',flush=True); print(sys.stdin.readline(),end='',flush=True)"], **kwargs)
            native = Herdr("/fake/herdr", stream_runner=spawn)
            native.configure({"HERDR_SOCKET_PATH": domain.api})
            native._domain = domain
            with self.assertRaises(HerdrError): native.terminal_control("terminal_sample", 80, 24, None)
            self.assertFalse(calls)
            with permits.Store(folder + "/locks").control(domain, values) as permit:
                errors = []
                def borrow():
                    try: native.terminal_control("terminal_sample", 80, 24, permit)
                    except HerdrError as exc: errors.append(exc.code)
                thread = threading.Thread(target=borrow); thread.start(); thread.join()
                self.assertEqual(errors, ["control_unavailable"])
                stream = native.terminal_control("terminal_sample", 80, 24, permit)
                stop = threading.Event()
                lines = stream.lines(stop)
                self.assertEqual(next(lines), b"ready")
                stream.input("Synthetic intentional text", stop)
                self.assertEqual(json.loads(next(lines)), {"type": "terminal.input", "text": "Synthetic intentional text"})
                lines.close()
                self.assertTrue(stream.teardown())
                stream.process.stdout.close()
                self.assertEqual(calls[0][0], ["/fake/herdr", "terminal", "session", "control", "terminal_sample", "--cols", "80", "--rows", "24"])
                self.assertEqual(calls[0][1]["env"]["HERDR_SOCKET_PATH"], domain.api)
                self.assertEqual(set(calls[0][1]["pass_fds"]), set(permit.descriptors()))
                self.assertEqual(calls[0][1]["stdin"], subprocess.PIPE)


class ControlHTTP(dashboard_tests.DashboardCase):
    request = dashboard_tests.HTTPAuthority.request
    login = dashboard_tests.HTTPAuthority.login

    def setUp(self):
        super().setUp()
        from herdr_fleet.dashboard import DashboardServer
        from herdr_fleet.dashboard_terminal import Watches
        self.write_fleet('watch=false\n' + self.read_fleet())
        self.world["panes"]["w2:p2"]["agent"]["launch_pending"] = False
        self.sync()
        self.server = DashboardServer(("127.0.0.1", 0), self.observer, Watches(self.client, self.registry), self.actions)
        self.worker = threading.Thread(target=self.server.serve_forever)
        self.worker.start()
        self.cookie = self.csrf = None
        self.login()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.worker.join(timeout=5)
        for child in getattr(self, "all_children", []):
            if child.poll() is None: child.kill(); child.wait(timeout=2)
            for pipe in (child.stdin, child.stdout):
                if pipe and not pipe.closed: pipe.close()
        super().tearDown()

    def prepare(self):
        seat = self.seat()
        return self.request("POST", "/api/control/prepare", dict(fleet=self.fleet_id, seat="sample-dev",
                             view=seat["view"], tab="synthetic_tab_0123456789"), authenticated=True)

    def install_control(self, read=True, full=True, first=None):
        self.children = []
        if not hasattr(self, "all_children"): self.all_children = []
        self.write_path = os.path.join(self.tmp, "synthetic-control-metadata.jsonl")
        def spawn(argv, **kwargs):
            transport = permits.native_transport()
            evidence = dict(transport[2])
            evidence["live_fds"] = all(os.fstat(fd).st_ino > 0 for fd in kwargs["pass_fds"])
            with self.server.controls.lock:
                authorized = any(w.session in self.server.authority.sessions and w.state == "acquiring"
                                 for w in self.server.controls.leases.values())
            with open(self.log_path, "a") as handle:
                handle.write(json.dumps({"argv": argv[1:], "exit": 0,
                    "info": {"control_permit": evidence, "authenticated_human": authorized}}) + "\n")
            script = "import sys,json,time;\n"
            if full: script += "print(sys.argv[1],flush=True)\n"
            if read:
                script += "for line in sys.stdin:\n d=json.loads(line)\n with open(sys.argv[2],'a') as f: f.write(json.dumps({'type':d['type'],'size':len(d.get('text','').encode())})+'\\n')\n"
            else: script += "time.sleep(60)\n"
            child = subprocess.Popen([sys.executable, "-u", "-c", script, first or dashboard_tests.frame().decode(), self.write_path], **kwargs)
            self.children.append(child)
            self.all_children.append(child)
            return child
        self.client.stream_runner = spawn

    def acquire(self):
        self.mark("control_prepare", "term_w2:p2")
        code, _, raw = self.prepare()
        self.assertEqual(code, 200, raw)
        nonce = json.loads(raw)["nonce"]
        self.mark("control", "term_w2:p2")
        body = dict(nonce=nonce, tab="synthetic_tab_0123456789", confirmed=True, cols=80, rows=24)
        code, _, raw = self.request("POST", "/api/control/acquire", body, authenticated=True)
        self.assertEqual(code, 200, raw)
        return nonce, dict(lease=json.loads(raw)["lease"], tab=body["tab"])

    @contextlib.contextmanager
    def attached(self, lease):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        connection.request("POST", "/api/control/stream", json.dumps(lease),
                           {"Host": self.server.host, "Origin": self.server.origin, "Content-Type": "application/json",
                            "Cookie": self.cookie, "X-Fleet-CSRF": self.csrf})
        response = connection.getresponse()
        try:
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.readline())["type"], "frame")
            yield response
        finally:
            response.close()
            connection.close()

    def test_duplicate_input_revokes_without_second_write_and_resize_shares_exact_sequence(self):
        self.install_control()
        nonce, lease = self.acquire()
        with self.attached(lease):
            self.assertEqual(self.request("POST", "/api/control/input", dict(lease, seq=1, text="Synthetic intentional text"), authenticated=True)[0], 200)
            self.assertEqual(self.request("POST", "/api/control/resize", dict(lease, seq=2, cols=90, rows=25), authenticated=True)[0], 200)
            self.assertEqual(self.request("POST", "/api/control/input", dict(lease, seq=2, text="MUST NOT REPLAY"), authenticated=True)[0], 400)
            self.assertNotEqual(self.request("POST", "/api/control/input", dict(lease, seq=3, text="MUST NOT CONTINUE"), authenticated=True)[0], 200)
        result = self.request("POST", "/api/control/release", lease, authenticated=True)
        self.assertEqual(json.loads(result[2])["state"], "released")
        self.assertTrue(all(p.poll() is not None for p in self.children))
        with open(self.write_path) as handle: writes = [json.loads(line) for line in handle]
        self.assertEqual([w["type"] for w in writes], ["terminal.input", "terminal.resize"])
        self.assertEqual(len(self.children), 1)
        self.assertNotEqual(self.request("POST", "/api/control/acquire", dict(nonce=nonce, tab=lease["tab"], confirmed=True, cols=80, rows=24), authenticated=True)[0], 200)
        self.assertEqual(len(self.children), 1)

    def test_prepare_is_authenticated_metadata_only_and_discloses_restore_nonrollback(self):
        from test_section40 import snapshot
        before = snapshot(self.repo)
        self.mark("control_prepare", "term_w2:p2")
        code, _, raw = self.prepare()
        self.assertEqual(code, 200, raw)
        result = json.loads(raw)
        self.assertEqual(set(result), {"nonce", "expires_in", "target", "disclosures"})
        self.assertEqual(result["expires_in"], 30)
        self.assertIn("before any input", " ".join(result["disclosures"]))
        self.assertIn("does not undo", " ".join(result["disclosures"]))
        self.assertEqual(snapshot(self.repo), before)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "permits")))

    def test_native_omitted_false_pending_is_accepted_true_and_invalid_are_refused(self):
        self.mark("control_prepare", "term_w2:p2")
        original = self.client.agent_list
        for flag, wanted in (("absent", 200), (True, 400), (None, 400), (0, 400), ("false", 400)):
            def agents():
                records = original()
                for record in records:
                    if flag == "absent": record.pop("launch_pending", None)
                    else: record["launch_pending"] = flag
                return records
            with self.subTest(flag=flag), mock.patch.object(self.client, "agent_list", agents):
                # control_client copies the overridden callable, preserving fake-only reads.
                self.assertEqual(self.prepare()[0], wanted)

    def test_multiple_matching_runtime_pids_are_ambiguous_in_either_order(self):
        import copy
        self.mark("control_prepare", "term_w2:p2")
        original = list(self.world["panes"]["w2:p2"]["procs"])
        duplicate = copy.deepcopy(original[0]); duplicate["pid"] += 123
        for processes in (original + [duplicate], [duplicate] + original):
            self.world["panes"]["w2:p2"]["procs"] = processes; self.sync()
            self.assertEqual(self.prepare()[0], 400)

    def test_unreadable_or_dangling_watcher_is_unknown_but_absent_is_eligible(self):
        self.write_fleet(self.read_fleet().replace('watch=false', 'watch=true'))
        self.mark("control_prepare", "term_w2:p2")
        self.assertEqual(self.prepare()[0], 200)
        path = os.path.join(self.repo, ".herdr/watch.lock")
        os.symlink(os.path.join(self.tmp, "missing-watch-record"), path)
        self.assertEqual(self.prepare()[0], 400)
        os.unlink(path)
        with open(path, "w") as handle: handle.write("malformed")
        self.assertEqual(self.prepare()[0], 400)

    def test_each_pipe_setup_failure_retains_owned_child_until_exact_reap(self):
        from herdr_fleet import dashboard_control
        for failed_call in (1, 2):
            with self.subTest(pipe=failed_call):
                self.install_control(read=False)
                self.mark("control_prepare", "term_w2:p2")
                code, _, raw = self.prepare(); self.assertEqual(code, 200, raw)
                nonce = json.loads(raw)["nonce"]
                spawned = self.client.stream_runner
                allow = threading.Event()
                class Unreapable:
                    def __init__(self, child): self.child, self.stdin, self.stdout = child, child.stdin, child.stdout
                    def poll(self): return self.child.poll() if allow.is_set() else None
                    def wait(self, timeout=None):
                        if not allow.is_set(): raise subprocess.TimeoutExpired("synthetic controller", timeout)
                        return self.child.wait(timeout=timeout)
                    def terminate(self):
                        if allow.is_set(): self.child.terminate()
                    def kill(self):
                        if allow.is_set(): self.child.kill()
                self.client.stream_runner = lambda *a, **kw: Unreapable(spawned(*a, **kw))
                original = os.set_blocking; calls = []
                def fail(fd, value):
                    calls.append(fd)
                    if len(calls) == failed_call: raise OSError("synthetic pipe setup failure")
                    return original(fd, value)
                self.mark("control", "term_w2:p2")
                try:
                    with mock.patch("os.set_blocking", fail):
                        code, _, _ = self.request("POST", "/api/control/acquire", dict(nonce=nonce,
                            tab="synthetic_tab_0123456789", confirmed=True, cols=80, rows=24), authenticated=True)
                    self.assertEqual(code, 400)
                    self.assertEqual(len(self.server.controls.leases), 1, "live child must retain capacity/ownership")
                    worker = next(iter(self.server.controls.leases.values()))
                    self.assertFalse(worker.done.is_set())
                    result = self.request("POST", "/api/control/release", dict(nonce=nonce, tab=worker.tab), authenticated=True)
                    self.assertEqual(json.loads(result[2])["state"], "release_incomplete")
                    self.assertIsNone(self.children[0].poll())
                finally: allow.set()
                self.assertTrue(worker.done.wait(2))
                self.assertFalse(self.server.controls.leases)

    def test_expiry_during_successful_probe_prevents_input_after_blocking_boundary(self):
        for deadline in ("absolute", "heartbeat"):
            with self.subTest(deadline=deadline):
                self.install_control(); _, lease = self.acquire()
                worker = self.server.controls.leases[lease["lease"]]
                try:
                    with self.attached(lease):
                        base = worker.born
                        clock = [base + (599.9 if deadline == "absolute" else 14.9)]
                        worker.heartbeat = clock[0] if deadline == "absolute" else base
                        self.server.controls.now = lambda: clock[0]
                        original = self.server.controls.snapshot
                        entered, proceed, queued = threading.Event(), threading.Event(), threading.Event()
                        def delayed(selection):
                            result = original(selection)
                            entered.set()
                            if not proceed.wait(2): raise AssertionError("probe barrier not released")
                            clock[0] += 1
                            return result
                        with mock.patch.object(self.server.controls, "snapshot", delayed):
                            self.assertTrue(entered.wait(2), "due periodic probe did not enter")
                            results = []
                            put = worker.jobs.put_nowait
                            def enqueue(job): put(job); queued.set()
                            with mock.patch.object(worker.jobs, "put_nowait", enqueue):
                                request = threading.Thread(target=lambda: results.append(self.request("POST", "/api/control/input",
                                    dict(lease, seq=1, text="EXPIRED MUST NOT WRITE"), authenticated=True)))
                                request.start()
                                try: self.assertTrue(queued.wait(2))
                                finally: proceed.set(); request.join(timeout=4)
                            self.assertFalse(request.is_alive())
                        self.assertEqual(results[0][0], 400)
                    self.assertFalse(os.path.exists(self.write_path))
                finally:
                    worker.revoke("released"); worker.done.wait(2)
                    self.server.controls.now = time.monotonic

    def test_before_expiry_input_and_resize_are_submitted_once(self):
        self.install_control(); _, lease = self.acquire()
        worker = self.server.controls.leases[lease["lease"]]
        with self.attached(lease):
            self.server.controls.now = lambda: worker.born + 10
            self.assertEqual(self.request("POST", "/api/control/input", dict(lease, seq=1, text="Synthetic positive"), authenticated=True)[0], 200)
            self.assertEqual(self.request("POST", "/api/control/resize", dict(lease, seq=2, cols=81, rows=25), authenticated=True)[0], 200)
        self.server.controls.now = time.monotonic
        self.assertTrue(worker.done.wait(2))
        with open(self.write_path) as handle: self.assertEqual(len(handle.readlines()), 2)

    def test_nonce_cancel_before_acquire_and_reuse_have_zero_child_effects(self):
        self.install_control(); self.mark("control_prepare", "term_w2:p2")
        nonce = json.loads(self.prepare()[2])["nonce"]
        cancel = dict(nonce=nonce, tab="synthetic_tab_0123456789")
        self.assertEqual(json.loads(self.request("POST", "/api/control/release", cancel, authenticated=True)[2])["state"], "released")
        self.assertEqual(self.request("POST", "/api/control/acquire", dict(cancel, confirmed=True, cols=80, rows=24), authenticated=True)[0], 400)
        self.assertEqual(self.request("POST", "/api/control/release", cancel, authenticated=True)[0], 200)
        self.assertFalse(self.children)

    def test_pending_missing_or_changed_runtime_after_prepare_never_spawns(self):
        self.install_control()
        for change in ("pending", "missing", "ambiguous"):
            with self.subTest(change=change):
                self.mark("control_prepare", "term_w2:p2")
                nonce = json.loads(self.prepare()[2])["nonce"]
                import copy
                original = copy.deepcopy(self.world["panes"]["w2:p2"])
                if change == "pending": self.world["panes"]["w2:p2"]["agent"]["launch_pending"] = True
                elif change == "missing": self.world["panes"]["w2:p2"]["procs"] = []
                else:
                    extra = copy.deepcopy(original["procs"][0]); extra["pid"] += 10
                    self.world["panes"]["w2:p2"]["procs"].append(extra)
                self.sync(); self.mark("control", "term_w2:p2")
                code, _, _ = self.request("POST", "/api/control/acquire", dict(nonce=nonce, tab="synthetic_tab_0123456789", confirmed=True, cols=80, rows=24), authenticated=True)
                self.assertEqual(code, 400); self.assertFalse(self.children)
                self.world["panes"]["w2:p2"] = original; self.sync()

    def test_approved_restore_race_effect_is_not_rolled_back_and_input_is_never_enabled(self):
        self.install_control(); original = self.client.stream_runner; effects = []
        def replacement(*args, **kwargs):
            child = original(*args, **kwargs)
            effects.append("synthetic native saved-process resume before input")
            self.world["panes"]["w2:p2"]["agent"]["agent_session"] = {"source": "fake", "kind": "id", "value": "replacement"}
            self.sync()
            return child
        self.client.stream_runner = replacement
        self.mark("control_prepare", "term_w2:p2")
        nonce = json.loads(self.prepare()[2])["nonce"]; self.mark("control", "term_w2:p2")
        code, _, _ = self.request("POST", "/api/control/acquire", dict(nonce=nonce, tab="synthetic_tab_0123456789", confirmed=True, cols=80, rows=24), authenticated=True)
        self.assertEqual(code, 400); self.assertEqual(len(effects), 1)
        self.assertTrue(all(p.poll() is not None for p in self.children))
        self.assertFalse(os.path.exists(self.write_path)); self.assertFalse(self.argvs("agent", "start"))

    def test_real_controller_hold_covers_delivery_lifecycle_inbox_and_sibling_topology(self):
        from herdr_fleet import dashboard_control, inbox
        from test_inbox import box
        with mock.patch.object(dashboard_control, "PROBE_S", 60):
            self.install_control(); _, lease = self.acquire()
            worker = self.server.controls.leases[lease["lease"]]
            with self.attached(lease):
                code, out, err = self.run_cli("send", "sample-dev", "Synthetic held now", "--now", "--as", "owner")
                self.assertEqual(code, 0, out + err); self.assertIn("owner control", out)
                original_id, = box(self.repo, "sample-dev", inbox.NEW)
                for args in (("restart", "sample-dev"), ("down", "sample-dev"), ("tile",)):
                    self.assertNotEqual(self.run_cli(*args)[0], 0)
                self.assertNotEqual(self.run_cli("inbox", "--read", original_id, "--as", "sample-dev")[0], 0)
                self.assertEqual(box(self.repo, "sample-dev", inbox.NEW), [original_id])
                self.assertEqual(self.run_cli("inbox", "--show", original_id, "--as", "sample-dev")[0], 0)
                self.assertEqual(self.run_cli("send", "sample-reviewer", "Independent target", "--as", "owner")[0], 0)
                self.assertEqual(len(self.prompts_to("sample-reviewer")), 1)
                self.assertFalse(self.prompts_to("sample-dev"))
            self.assertTrue(worker.done.wait(2))
            self.assertEqual(self.run_cli("deliver", "sample-dev")[0], 0)
            self.assertEqual(len(self.prompts_to("sample-dev")), 1)
            self.assertEqual(box(self.repo, "sample-dev", inbox.CUR), [original_id])

    def test_auth_schema_and_url_authority_are_rejected_before_native_reads(self):
        selection = dict(fleet=self.fleet_id, seat="sample-dev", view=self.seat()["view"], tab="synthetic_tab_0123456789")
        before = len(self.calls())
        for headers, auth in (({}, False), ({"X-Fleet-CSRF": "invalid"}, True), ({"Origin": None}, True), ({"Host": "localhost"}, True)):
            self.assertEqual(self.request("POST", "/api/control/prepare", selection, headers=headers, authenticated=auth)[0], 403)
        for body in (dict(selection, argv="denied"), dict(selection, tab="short"), {k:v for k,v in selection.items() if k != "view"}):
            self.assertEqual(self.request("POST", "/api/control/prepare", body, authenticated=True)[0], 400)
        for route in ("/api/control/prepare?nonce=ignored", "/api/control/prepare#ignored",
                      "http://example.invalid/api/control/prepare"):
            self.assertEqual(self.request("POST", route, selection, authenticated=True)[0], 400)
        self.assertEqual(len(self.calls()), before)

    def test_control_stream_is_one_shot_and_unattached_setup_expires(self):
        from herdr_fleet import dashboard_control
        self.install_control(); _, lease = self.acquire()
        worker = self.server.controls.leases[lease["lease"]]
        with self.attached(lease):
            code, _, raw = self.request("POST", "/api/control/stream", lease, authenticated=True)
            self.assertEqual(code, 200); self.assertIn(b'"type": "error"', raw)
        self.assertTrue(worker.done.wait(2))
        with mock.patch.object(dashboard_control, "UNATTACHED_S", 0.08):
            _, lease = self.acquire(); worker = self.server.controls.leases[lease["lease"]]
            self.assertTrue(worker.done.wait(1))
            self.assertFalse(os.path.exists(self.write_path))

    def test_silent_or_malformed_setup_reaps_without_ready_input(self):
        from herdr_fleet import dashboard_control
        for full, first in ((False, None), (True, "not-json")):
            with self.subTest(full=full):
                self.install_control(full=full, first=first)
                self.mark("control_prepare", "term_w2:p2")
                nonce = json.loads(self.prepare()[2])["nonce"]; self.mark("control", "term_w2:p2")
                with mock.patch.object(dashboard_control, "SETUP_S", 0.1):
                    code, _, _ = self.request("POST", "/api/control/acquire", dict(nonce=nonce, tab="synthetic_tab_0123456789", confirmed=True, cols=80, rows=24), authenticated=True)
                self.assertEqual(code, 400); self.assertTrue(all(p.poll() is not None for p in self.children))
                self.assertFalse(self.server.controls.leases)

    def test_control_input_and_output_are_absent_from_fleet_and_transport_files(self):
        self.install_control(); _, lease = self.acquire()
        text = "SYNTHETIC_PRIVATE_KEYBOARD_45"
        with self.attached(lease):
            self.assertEqual(self.request("POST", "/api/control/input", dict(lease, seq=1, text=text), authenticated=True)[0], 200)
            self.assertEqual(self.request("POST", "/api/control/release", lease, authenticated=True)[0], 200)
        for folder, _, names in os.walk(self.tmp):
            for name in names:
                with open(os.path.join(folder, name), "rb") as handle:
                    self.assertNotIn(text.encode(), handle.read())

    def test_stream_never_announces_released_while_exact_child_is_unreapable(self):
        self.install_control(read=False); spawned = self.client.stream_runner
        allow = threading.Event()
        class Stuck:
            def __init__(self, child): self.child, self.stdin, self.stdout = child, child.stdin, child.stdout
            def poll(self): return self.child.poll() if allow.is_set() else None
            def wait(self, timeout=None):
                if not allow.is_set():
                    time.sleep(min(0.1, timeout or 0.1))
                    raise subprocess.TimeoutExpired("synthetic stuck controller", timeout)
                return self.child.wait(timeout=timeout)
            def terminate(self):
                if allow.is_set(): self.child.terminate()
            def kill(self):
                if allow.is_set(): self.child.kill()
        self.client.stream_runner = lambda *a, **kw: Stuck(spawned(*a, **kw))
        _, lease = self.acquire(); worker = self.server.controls.leases[lease["lease"]]
        try:
            with self.attached(lease) as response:
                result = self.request("POST", "/api/control/release", lease, authenticated=True)
                self.assertEqual(json.loads(result[2])["state"], "release_incomplete")
                final = json.loads(response.readline())
                self.assertEqual(final["state"], "release_incomplete")
                self.assertFalse(worker.done.is_set())
        finally:
            allow.set(); self.assertTrue(worker.done.wait(2))

    def test_reversed_input_arrival_and_auth_loss_have_zero_continuation(self):
        self.install_control(); _, lease = self.acquire()
        worker = self.server.controls.leases[lease["lease"]]
        with self.attached(lease):
            self.assertEqual(self.request("POST", "/api/control/input", dict(lease, seq=2, text="REVERSED"), authenticated=True)[0], 400)
            self.assertEqual(self.request("POST", "/api/control/input", dict(lease, seq=1, text="LATE"), authenticated=True)[0], 400)
        self.assertTrue(worker.done.wait(2)); self.assertFalse(os.path.exists(self.write_path))
        _, lease = self.acquire(); worker = self.server.controls.leases[lease["lease"]]
        with self.attached(lease):
            with self.server.authority.lock: self.server.authority.sessions.clear()
            self.assertEqual(self.request("POST", "/api/control/input", dict(lease, seq=1, text="NO AUTH"), authenticated=True)[0], 403)
            self.assertTrue(worker.done.wait(2))
        self.assertFalse(os.path.exists(self.write_path))
