"""Independent OS-process and HTTP race verification; no live native endpoint."""
from __future__ import annotations

import copy
import http.client
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from herdr_fleet import permits
from herdr_fleet.herdr import Herdr, HerdrError
import test_control45 as control_tests
import test_section41 as dashboard_tests

VALUES = [("W", "w7"), ("N", "sample-dev"), ("P", "w7:p1"), ("T", "terminal_sample")]


class DemoControl(unittest.TestCase):
    setUp = dashboard_tests.DemoHTTP.setUp
    tearDown = dashboard_tests.DemoHTTP.tearDown
    request = dashboard_tests.DemoHTTP.request
    login = dashboard_tests.DemoHTTP.login

    def test_all_control_routes_are_unavailable_in_demo_even_with_owner_authentication(self):
        self.login()
        self.assertIsNone(self.server.controls)
        for action in ("prepare", "acquire", "stream", "input", "resize", "heartbeat", "release"):
            with self.subTest(action=action):
                status, _, body = self.request("POST", "/api/control/" + action, {}, authenticated=True)
                self.assertEqual(status, 403)
                self.assertEqual(json.loads(body), {"error": "Demo has no native Control"})


class NativeProcesses(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.domain = permits.endpoints({"HERDR_SOCKET_PATH": self.folder.name + "/api.sock"})[0]
        self.store = permits.Store(self.folder.name + "/locks")
        self.children = []

    def tearDown(self):
        for child in self.children:
            if child.poll() is None: child.kill(); child.wait(timeout=2)
            for pipe in (child.stdin, child.stdout, child.stderr):
                if pipe and not pipe.closed: pipe.close()

    def native(self, script):
        def spawn(argv, **kwargs):
            child = subprocess.Popen([sys.executable, "-u", "-c", script], **kwargs)
            self.children.append(child)
            return child
        native = Herdr("/fake/herdr", stream_runner=spawn)
        native.configure({"HERDR_SOCKET_PATH": self.domain.api}); native._domain = self.domain
        return native

    def test_controller_child_retains_exclusive_descriptors_after_parent_close(self):
        native = self.native("import time; time.sleep(60)")
        permit = self.store.control(self.domain, VALUES)
        stream = native.terminal_control("terminal_sample", 80, 24, permit)
        permit.close()
        with self.assertRaises(permits.Held): self.store.acquire([self.domain], [("N", "sample-dev")])
        with self.assertRaises(permits.Held): self.store.acquire([self.domain], [("W", "w7")], topology=True)
        with self.store.acquire([self.domain], [("W", "w7"), ("N", "another-seat")]): pass
        stream.process.kill(); stream.process.wait(timeout=2)
        with self.store.acquire([self.domain], VALUES): pass

    def test_actual_parent_death_preserves_controller_exclusion_until_exact_child_ends(self):
        script = r'''
import subprocess,sys,time
from herdr_fleet import permits
from herdr_fleet.herdr import Herdr
d=permits.endpoints({'HERDR_SOCKET_PATH':sys.argv[1]})[0]
values=[('W','w7'),('N','sample-dev'),('P','w7:p1'),('T','terminal_sample')]
with permits.Store(sys.argv[2]).control(d,values) as permit:
 def spawn(argv,**kwargs): return subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],**kwargs)
 native=Herdr('/fake/herdr',stream_runner=spawn);native.configure({'HERDR_SOCKET_PATH':d.api});native._domain=d
 child=native.terminal_control('terminal_sample',80,24,permit)
 print(child.process.pid,flush=True)
 time.sleep(60)
'''
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(Path(__file__).resolve().parents[1]))
        parent = subprocess.Popen([sys.executable, "-u", "-c", script, self.domain.api, self.store.root],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.children.append(parent); child_pid = None
        try:
            self.assertTrue(select.select([parent.stdout], [], [], 3)[0])
            child_pid = int(parent.stdout.readline())
            parent.kill(); parent.wait(timeout=2)
            with self.assertRaises(permits.Held): self.store.acquire([self.domain], VALUES)
            with self.store.acquire([self.domain], [("W", "w7"), ("N", "unrelated")]): pass
            os.kill(child_pid, signal.SIGTERM); child_pid = None
            end = time.monotonic() + 2
            while True:
                try:
                    permit = self.store.acquire([self.domain], VALUES); permit.close(); break
                except permits.Held:
                    if time.monotonic() >= end: raise
                    time.sleep(0.01)
        finally:
            if child_pid is not None:
                try: os.kill(child_pid, signal.SIGKILL)
                except ProcessLookupError: pass

    def test_producer_objects_raw_descriptors_and_other_threads_cannot_control(self):
        native = self.native("raise SystemExit('must not execute')")
        with self.store.acquire([self.domain], VALUES) as producer:
            for forged in (producer, producer.descriptors(), {}, None):
                with self.assertRaises(HerdrError): native.terminal_control("terminal_sample", 80, 24, forged)
        with self.store.control(self.domain, VALUES) as owner:
            native.permit = owner
            with self.assertRaises(HerdrError): native.run("terminal", "session", "control", "terminal_sample")
            with self.assertRaises(permits.Unavailable): owner.covers(VALUES)
        self.assertFalse(self.children)

    def test_non_oserror_pipe_setup_failure_also_returns_the_owned_child_for_teardown(self):
        native = self.native("import time; time.sleep(60)")
        with self.store.control(self.domain, VALUES) as permit:
            with mock.patch("os.set_blocking", side_effect=ValueError("synthetic closed file descriptor")):
                stream = native.terminal_control("terminal_sample", 80, 24, permit)
            self.assertTrue(stream.setup_failed)
            self.assertTrue(stream.teardown())

    def test_blocked_stdout_and_stdin_have_bounded_exact_child_cleanup(self):
        for script in ("import os;\nwhile True: os.write(1,b'x'*65536)", "import time; time.sleep(60)"):
            with self.subTest(script=script):
                native = self.native(script)
                with self.store.control(self.domain, VALUES) as permit:
                    stream = native.terminal_control("terminal_sample", 80, 24, permit)
                    start = time.monotonic()
                    self.assertTrue(stream.teardown())
                    self.assertLess(time.monotonic() - start, 1.5)
                    self.assertIsNotNone(stream.process.poll())
                    with self.assertRaises(permits.Held): self.store.acquire([self.domain], VALUES)
                with self.store.acquire([self.domain], VALUES): pass

    def test_nonreading_controller_input_hits_finite_backpressure_without_blocking_release(self):
        native = self.native("import time; time.sleep(60)")
        with self.store.control(self.domain, VALUES) as permit:
            stream = native.terminal_control("terminal_sample", 80, 24, permit)
            stop = threading.Event(); failure = None; start = time.monotonic()
            for _ in range(32):
                try: stream.input("x" * 8192, stop)
                except HerdrError as exc: failure = exc; break
            self.assertIsNotNone(failure); self.assertEqual(failure.code, "control_uncertain")
            self.assertLess(time.monotonic() - start, 2)
            stop.set(); self.assertTrue(stream.teardown())


class HTTPRaces(dashboard_tests.DashboardCase):
    request = control_tests.ControlHTTP.request
    login = control_tests.ControlHTTP.login
    prepare = control_tests.ControlHTTP.prepare
    install_control = control_tests.ControlHTTP.install_control
    acquire = control_tests.ControlHTTP.acquire
    attached = control_tests.ControlHTTP.attached

    def setUp(self):
        super().setUp()
        from herdr_fleet.dashboard import DashboardServer
        from herdr_fleet.dashboard_terminal import Watches
        self.write_fleet('watch=false\n' + self.read_fleet()); self.sync()
        self.server = DashboardServer(("127.0.0.1", 0), self.observer, Watches(self.client, self.registry), self.actions)
        self.worker = threading.Thread(target=self.server.serve_forever); self.worker.start()
        self.cookie = self.csrf = None; self.login()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.worker.join(timeout=5)
        for child in getattr(self, "all_children", []):
            if child.poll() is None: child.kill(); child.wait(timeout=2)
            for pipe in (child.stdin, child.stdout):
                if pipe and not pipe.closed: pipe.close()
        super().tearDown()

    def test_response_loss_after_native_write_revokes_and_never_continues(self):
        from herdr_fleet.dashboard import Handler
        self.install_control(); _, lease = self.acquire()
        worker = self.server.controls.leases[lease["lease"]]
        original = Handler.respond
        def lost(handler, code, value, *args, **kwargs):
            if isinstance(value, dict) and value.get("status") == "submitted":
                raise OSError("synthetic lost response after native submission")
            return original(handler, code, value, *args, **kwargs)
        with self.attached(lease), mock.patch.object(Handler, "respond", lost):
            conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=2)
            try:
                conn.request("POST", "/api/control/input", json.dumps(dict(lease, seq=1, text="Synthetic submitted once")),
                    {"Host": self.server.host, "Origin": self.server.origin, "Content-Type": "application/json",
                     "Cookie": self.cookie, "X-Fleet-CSRF": self.csrf})
                with self.assertRaises(http.client.RemoteDisconnected): conn.getresponse()
            finally: conn.close()
        self.assertTrue(worker.done.wait(2))
        self.assertEqual(self.request("POST", "/api/control/input", dict(lease, seq=2, text="NO CONTINUATION"), authenticated=True)[0], 400)
        with open(self.write_path) as handle: self.assertEqual(len(handle.readlines()), 1)

    def test_duplicate_reversed_http_handlers_cannot_reorder_or_write_a_second_payload(self):
        self.install_control(); _, lease = self.acquire()
        worker = self.server.controls.leases[lease["lease"]]
        entered, release = threading.Event(), threading.Event()
        from herdr_fleet.herdr import ControlStream
        original = ControlStream.input
        def delayed(stream, text, stop, eligible=None):
            entered.set()
            if not release.wait(2): raise AssertionError("writer barrier unreleased")
            return original(stream, text, stop, eligible)
        results = []
        with self.attached(lease), mock.patch.object(ControlStream, "input", delayed):
            first = threading.Thread(target=lambda: results.append(self.request("POST", "/api/control/input", dict(lease, seq=1, text="first"), authenticated=True)))
            first.start()
            try:
                self.assertTrue(entered.wait(2))
                result = self.request("POST", "/api/control/resize", dict(lease, seq=2, cols=81, rows=25), authenticated=True)
                self.assertEqual(result[0], 400)
            finally: release.set(); first.join(timeout=3)
        self.assertFalse(first.is_alive()); self.assertEqual(results[0][0], 400)
        self.assertTrue(worker.done.wait(2)); self.assertFalse(os.path.exists(self.write_path))

    def test_release_signal_during_blocked_write_prevents_late_accepted_bytes(self):
        self.install_control(); _, lease = self.acquire()
        worker = self.server.controls.leases[lease["lease"]]
        entered, proceed = threading.Event(), threading.Event()
        from herdr_fleet.herdr import ControlStream
        original = ControlStream.input
        def delayed(stream, text, stop, eligible=None):
            entered.set(); proceed.wait(2)
            return original(stream, text, stop, eligible)
        results = []
        with self.attached(lease), mock.patch.object(ControlStream, "input", delayed):
            first = threading.Thread(target=lambda: results.append(self.request("POST", "/api/control/input", dict(lease, seq=1, text="MUST NOT ARRIVE LATE"), authenticated=True)))
            first.start(); self.assertTrue(entered.wait(2))
            released = []
            release = threading.Thread(target=lambda: released.append(self.request("POST", "/api/control/release", lease, authenticated=True)))
            release.start()
            self.assertTrue(worker.stop.wait(0.5)); proceed.set(); first.join(timeout=3); release.join(timeout=3)
            self.assertFalse(release.is_alive()); self.assertEqual(json.loads(released[0][2])["state"], "released")
        self.assertEqual(results[0][0], 400); self.assertTrue(worker.done.wait(2))
        self.assertFalse(os.path.exists(self.write_path))

    def test_nonpositive_sequence_cannot_refuse_then_continue_same_lease(self):
        self.install_control(); _, lease = self.acquire()
        worker = self.server.controls.leases[lease["lease"]]
        with self.attached(lease):
            self.assertEqual(self.request("POST", "/api/control/input", dict(lease, seq=0, text="INVALID OLD SEQUENCE"), authenticated=True)[0], 400)
            self.assertEqual(self.request("POST", "/api/control/input", dict(lease, seq=1, text="MUST NOT CONTINUE"), authenticated=True)[0], 400)
        self.assertTrue(worker.done.wait(2)); self.assertFalse(os.path.exists(self.write_path))

    def test_pre_stream_input_is_unavailable_and_never_enables_from_spawn_alone(self):
        self.install_control(); _, lease = self.acquire()
        worker = self.server.controls.leases[lease["lease"]]
        self.assertEqual(self.request("POST", "/api/control/input", dict(lease, seq=1, text="BEFORE STREAM"), authenticated=True)[0], 400)
        self.assertTrue(worker.done.wait(2)); self.assertFalse(os.path.exists(self.write_path))

    def test_prepare_and_lease_capacity_are_bounded_before_native_control_effects(self):
        self.install_control(); self.mark("control_prepare", "term_w2:p2")
        for _ in range(64): self.assertEqual(self.prepare()[0], 200)
        self.assertEqual(self.prepare()[0], 400); self.assertEqual(len(self.server.controls.pending), 64)
        self.assertFalse(self.children)
        nonce = next(iter(self.server.controls.pending))
        self.server.controls.leases = {str(n): SimpleNamespace(tab="occupied-tab-%d" % n) for n in range(4)}
        try:
            code, _, _ = self.request("POST", "/api/control/acquire", dict(nonce=nonce, tab="synthetic_tab_0123456789", confirmed=True, cols=80, rows=24), authenticated=True)
            self.assertEqual(code, 400); self.assertFalse(self.children)
        finally: self.server.controls.leases.clear()

    def test_same_tab_cannot_acquire_another_child_or_takeover_its_current_lease(self):
        self.install_control(); _, lease = self.acquire()
        worker = self.server.controls.leases[lease["lease"]]
        with self.attached(lease):
            nonce = json.loads(self.prepare()[2])["nonce"]
            code, _, _ = self.request("POST", "/api/control/acquire", dict(nonce=nonce, tab=lease["tab"], confirmed=True, cols=80, rows=24), authenticated=True)
            self.assertEqual(code, 400); self.assertEqual(len(self.children), 1)
            self.assertEqual(self.request("POST", "/api/control/input", dict(lease, seq=1, text="Original still active"), authenticated=True)[0], 200)
        self.assertTrue(worker.done.wait(2))

    def test_cancelled_prepare_never_exceeds_used_attempt_budget(self):
        self.install_control(); self.mark("control_prepare", "term_w2:p2")
        nonce = json.loads(self.prepare()[2])["nonce"]
        manager = self.server.controls
        manager.used = {"retired_attempt_%04d" % n: ("old-session", "old-tab", None) for n in range(1024)}
        code, _, result = self.request("POST", "/api/control/release",
            dict(nonce=nonce, tab="synthetic_tab_0123456789"), authenticated=True)
        self.assertEqual(code, 200); self.assertEqual(json.loads(result)["state"], "released")
        self.assertLessEqual(len(manager.used), 1024)
        self.assertNotIn(nonce, manager.pending); self.assertFalse(self.children)

    def test_partial_native_stdin_write_revokes_without_continuation(self):
        self.install_control(); _, lease = self.acquire()
        worker = self.server.controls.leases[lease["lease"]]
        original = os.write; fragments = []
        def partial(fd, data):
            if threading.current_thread().name == "human-control-lease":
                if not fragments:
                    count = original(fd, data[:7]); fragments.append(count); return count
                raise BrokenPipeError("synthetic interruption after partial native stdin")
            return original(fd, data)
        with self.attached(lease), mock.patch("os.write", partial):
            self.assertEqual(self.request("POST", "/api/control/input", dict(lease, seq=1, text="PARTIAL PRIVATE INPUT"), authenticated=True)[0], 400)
            self.assertEqual(self.request("POST", "/api/control/input", dict(lease, seq=2, text="NO REPLAY"), authenticated=True)[0], 400)
        self.assertEqual(fragments, [7]); self.assertTrue(worker.done.wait(2))
        self.assertFalse(os.path.exists(self.write_path))

    def test_native_output_overflow_is_bounded_and_revokes_instead_of_losing_and_continuing(self):
        self.install_control(); original = self.client.stream_runner
        popen = subprocess.Popen
        script = """import sys,os,time,json,base64
print(sys.argv[1],flush=True)
while not os.path.exists(sys.argv[2]+'.overflow-go'): time.sleep(.002)
frame=json.loads(sys.argv[1]);frame['bytes']=base64.b64encode(b'x'*100000).decode();frame['full']=False
for number in range(2,102):
 frame['seq']=number;print(json.dumps(frame),flush=True)
time.sleep(60)
"""
        def overflowing(argv, **kwargs):
            def replace(command, **options):
                command = list(command); command[3] = script
                return popen(command, **options)
            with mock.patch("subprocess.Popen", replace): return original(argv, **kwargs)
        self.client.stream_runner = overflowing
        _, lease = self.acquire(); worker = self.server.controls.leases[lease["lease"]]
        peaks = []; put = worker.output.put_nowait
        def bounded(item): peaks.append(worker.output_bytes + item[1]); return put(item)
        with mock.patch.object(worker.output, "put_nowait", bounded):
            Path(self.write_path + ".overflow-go").write_text("synthetic test barrier")
            self.assertTrue(worker.done.wait(2))
        self.assertTrue(peaks); self.assertLessEqual(max(peaks), 1048576)
        self.assertEqual(worker.state, "uncertain")
        self.assertEqual(self.request("POST", "/api/control/input", dict(lease, seq=1, text="NO CONTINUATION"), authenticated=True)[0], 400)
        self.assertTrue(all(p.poll() is not None for p in self.children))


class WatcherEvidence(unittest.TestCase):
    def test_real_owned_process_generation_must_match_before_control_is_eligible(self):
        from herdr_fleet.dashboard_control import watcher_eligible, ControlError
        from herdr_fleet.process_identity import identity
        with tempfile.TemporaryDirectory() as folder:
            os.mkdir(folder + "/.herdr")
            fleet = SimpleNamespace(watch=True, repo=folder, path=folder + "/.herdr/fleet.toml")
            path = Path(folder + "/.herdr/watch.lock")
            child = subprocess.Popen([sys.executable, "-u", "-c", "import time; print('ready',flush=True); time.sleep(60)"],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            try:
                self.assertTrue(select.select([child.stdout], [], [], 2)[0])
                self.assertEqual(child.stdout.readline(), b"ready\n")
                fingerprint = identity(child.pid)
                self.assertIsNotNone(fingerprint)
                valid = dict(pid=child.pid, control_gate_protocol=1,
                    launch=dict(version="0.23.0", commit="a"*40), process_identity=fingerprint, fleet=fleet.path)
                path.write_text(json.dumps(valid)); watcher_eligible(fleet)
                path.write_text(json.dumps(dict(valid, process_identity="c"*64)))
                with self.assertRaises(ControlError): watcher_eligible(fleet)
                child.terminate(); child.wait(timeout=2)
                watcher_eligible(fleet)  # A positively stopped process is distinct from unknown live evidence.
            finally:
                if child.poll() is None: child.kill(); child.wait(timeout=2)
                child.stdout.close()

    def test_disabled_absent_stopped_current_and_unknown_evidence_are_distinct(self):
        from herdr_fleet.dashboard_control import watcher_eligible, ControlError
        with tempfile.TemporaryDirectory() as folder:
            os.mkdir(folder + "/.herdr")
            fleet = SimpleNamespace(watch=True, repo=folder, path=folder + "/.herdr/fleet.toml")
            path = folder + "/.herdr/watch.lock"
            watcher_eligible(fleet)
            valid = dict(pid=12345, control_gate_protocol=1, launch=dict(version="0.23.0", commit="a"*40),
                         process_identity="b"*64, fleet=fleet.path)
            Path(path).write_text(json.dumps(valid))
            with mock.patch("herdr_fleet.dashboard_control.os.kill", side_effect=ProcessLookupError): watcher_eligible(fleet)
            with mock.patch("herdr_fleet.dashboard_control.os.kill", return_value=None), \
                    mock.patch("herdr_fleet.dashboard_control.identity", return_value="b"*64):
                watcher_eligible(fleet)
                for field, value in (("control_gate_protocol", None), ("control_gate_protocol", True),
                                     ("process_identity", None), ("process_identity", "c"*64),
                                     ("launch", {}), ("launch", dict(version="not-a-version", commit="a"*40))):
                    Path(path).write_text(json.dumps(dict(valid, **{field:value})))
                    with self.subTest(field=field, value=value), self.assertRaises(ControlError): watcher_eligible(fleet)
            Path(path).write_text(json.dumps(valid))
            with mock.patch("herdr_fleet.dashboard_control.os.kill", side_effect=PermissionError), self.assertRaises(ControlError):
                watcher_eligible(fleet)
            fleet.watch = False
            Path(path).write_text("unknown")
            watcher_eligible(fleet)
