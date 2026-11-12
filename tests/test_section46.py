"""Deterministic watcher identity; fake OS boundaries and isolated owned children."""
from __future__ import annotations

import os
import json
from pathlib import Path
import tempfile
import select
import subprocess
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from herdr_fleet import process_identity, watch
from herdr_fleet.dashboard_control import ControlError, watcher_eligible
from herdr_fleet import dashboard_control
import test_section41 as dashboard_tests
from test_section40 import snapshot

RAW = b"1001 Thu Jan  1 00:00:00 1970 /usr/bin/python3 -m sample_watcher --synthetic-long-argument=unchanged"
GOLDEN = "2b220c23c02578b8b7dba0613caec0e933930046add5486a101f9f074392384f"


class DeterministicIdentity(unittest.TestCase):
    def test_one_process_has_one_digest_across_inherited_formatting_environments(self):
        def ps(argv, **kwargs):
            env = kwargs.get("env", os.environ)
            stamp = ("Thu Jan  1 00:00:00 1970" if env.get("LC_ALL") == "C" and env.get("TZ") == "UTC0"
                     else "Thu  1 Jan 1970 11:00:00" if env.get("LC_ALL") == "en_AU.UTF-8"
                     else "Thu Jan  1 11:00:00 1970")
            command = "/usr/bin/python3 -m sample_watcher --synthetic-long-argument=unchanged"
            if "-ww" not in argv and env.get("COLUMNS") == "40": command = command[:12]
            return SimpleNamespace(returncode=0, stdout=("1001 " + stamp + " " + command + "\n").encode())
        with mock.patch.object(process_identity.subprocess, "run", side_effect=ps), \
                mock.patch.object(process_identity.os, "geteuid", return_value=1001):
            with mock.patch.dict(os.environ, LC_ALL="en_US.UTF-8", LC_TIME="en_US.UTF-8", LANG="en_US.UTF-8", TZ="UTC", COLUMNS="4000"):
                captured = process_identity.identity(12345)
            with mock.patch.dict(os.environ, LC_ALL="en_AU.UTF-8", LC_TIME="en_AU.UTF-8", LANG="en_AU.UTF-8", TZ="Australia/Melbourne", COLUMNS="40"):
                verified = process_identity.identity(12345)
        self.assertIsNotNone(captured)
        self.assertEqual(captured, verified, "An unchanged process must not lose eligibility because ps formatting differs")

    def test_fixed_recipe_is_child_only_and_hashes_the_versioned_complete_output(self):
        parent = dict(LC_ALL="en_AU.UTF-8", LC_TIME="en_AU.UTF-8", LANG="en_AU.UTF-8",
                      TZ="Australia/Melbourne", COLUMNS="40", SYNTHETIC_PROFILE="retained")
        with mock.patch.dict(os.environ, parent, clear=True), \
                mock.patch.object(process_identity.os, "geteuid", return_value=1001), \
                mock.patch.object(process_identity.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=b"  " + RAW + b"\n")) as run:
            self.assertEqual(process_identity.identity(12345), GOLDEN)
            self.assertEqual(dict(os.environ), parent)
        self.assertEqual(process_identity.SCHEME, "ps-c-utc-v1")
        run.assert_called_once_with(["/bin/ps", "-ww", "-p", "12345", "-o", "uid=,lstart=,command="],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=1,
            env=dict(LC_ALL="C", LC_TIME="C", LANG="C", TZ="UTC0", SYNTHETIC_PROFILE="retained"))

    def test_invalid_pid_never_executes_a_process_query(self):
        with mock.patch.object(process_identity.subprocess, "run") as run:
            for pid in (None, True, False, 0, -1, 1.5, "12345", [], {}):
                with self.subTest(pid=pid): self.assertIsNone(process_identity.identity(pid))
        run.assert_not_called()

    def test_missing_wrong_owner_failed_and_oversized_process_evidence_is_unknown(self):
        outputs = [(0, b""), (0, b" \n\t"), (0, RAW.replace(b"1001", b"1002", 1)),
                   (1, RAW), (0, RAW + b"x" * 65536), (0, b" " * 65536 + RAW)]
        with mock.patch.object(process_identity.os, "geteuid", return_value=1001):
            for code, output in outputs:
                with self.subTest(code=code, size=len(output)), \
                        mock.patch.object(process_identity.subprocess, "run", return_value=SimpleNamespace(returncode=code, stdout=output)):
                    self.assertIsNone(process_identity.identity(12345))

    def test_os_error_and_timeout_are_unknown_without_using_partial_output(self):
        for error in (OSError("synthetic denied"), subprocess.TimeoutExpired("ps", 1, output=RAW)):
            with self.subTest(error=type(error).__name__), mock.patch.object(process_identity.subprocess, "run", side_effect=error):
                self.assertIsNone(process_identity.identity(12345))

    def test_start_command_tail_and_internal_spacing_changes_are_distinguished(self):
        variants = [RAW.replace(b"00:00:00", b"00:00:01"), RAW + b" --other-command-evidence",
                    RAW.replace(b"1970 ", b"1970  ")]
        with mock.patch.object(process_identity.os, "geteuid", return_value=1001):
            for raw in variants:
                with self.subTest(raw=raw), mock.patch.object(process_identity.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=raw)):
                    changed = process_identity.identity(12345)
                    self.assertIsNotNone(changed)
                    self.assertNotEqual(changed, GOLDEN)


class WatcherCompatibility(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name); (root / ".herdr").mkdir()
        self.fleet = SimpleNamespace(watch=True, repo=str(root), path=str(root / ".herdr/fleet.toml"))
        self.path = root / ".herdr/watch.lock"
        self.record = dict(pid=12345, fleet=self.fleet.path, control_gate_protocol=1,
                           launch=dict(version="0.23.1", commit="a" * 40),
                           process_identity_scheme="ps-c-utc-v1", process_identity="b" * 64)

    def test_legacy_matching_digest_refuses_with_restart_guidance_without_rewriting_metadata(self):
        legacy = dict(self.record); del legacy["process_identity_scheme"]
        self.path.write_text(json.dumps(legacy)); before = self.path.read_bytes()
        with mock.patch("herdr_fleet.dashboard_control.os.kill", return_value=None), \
                mock.patch("herdr_fleet.dashboard_control.identity", return_value="b" * 64):
            with self.assertRaisesRegex(ControlError, "restart.*watcher.*current plugin"):
                watcher_eligible(self.fleet)
        self.assertEqual(self.path.read_bytes(), before)

    def test_watch_capture_and_cross_context_verification_share_the_versioned_scheme(self):
        raw = (str(os.geteuid()) + " Thu Jan  1 00:00:00 1970 /usr/bin/python3 -m sample_watcher\n").encode()
        with mock.patch.object(process_identity.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=raw)), \
                mock.patch("herdr_fleet.build_identity.current", return_value=self.record["launch"]), \
                mock.patch("herdr_fleet.dashboard_control.os.kill", return_value=None):
            with mock.patch.dict(os.environ, LC_ALL="en_US.UTF-8", TZ="America/New_York", COLUMNS="40"):
                captured = watch.acquire(self.fleet.repo, self.fleet.path, pid=12345)
            self.assertEqual(captured.get("process_identity_scheme"), "ps-c-utc-v1")
            self.assertEqual(captured["control_gate_protocol"], 1)
            before = self.path.read_bytes()
            with mock.patch.dict(os.environ, LC_ALL="en_AU.UTF-8", TZ="Australia/Melbourne", COLUMNS="4000"):
                watcher_eligible(self.fleet)
        self.assertEqual(self.path.read_bytes(), before)

    def test_unknown_and_malformed_schemes_cannot_be_inferred_from_a_matching_digest(self):
        for scheme in (None, True, False, 0, 1, [], {}, "ps-c-utc-v0", "ps-c-utc-v2", " ps-c-utc-v1", "ps-c-utc-v1\n"):
            self.path.write_text(json.dumps(dict(self.record, process_identity_scheme=scheme)))
            before = self.path.read_bytes()
            with self.subTest(scheme=scheme), mock.patch("herdr_fleet.dashboard_control.os.kill", return_value=None), \
                    mock.patch("herdr_fleet.dashboard_control.identity", return_value="b" * 64):
                with self.assertRaisesRegex(ControlError, "restart.*watcher.*current plugin"):
                    watcher_eligible(self.fleet)
            self.assertEqual(self.path.read_bytes(), before)

    def test_current_scheme_keeps_all_other_identity_and_metadata_guards(self):
        variants = [dict(self.record), dict(self.record, process_identity="c" * 64),
                    dict(self.record, process_identity=None), dict(self.record, fleet=self.fleet.path + ".other"),
                    dict(self.record, control_gate_protocol=True), dict(self.record, launch={})]
        with mock.patch("herdr_fleet.dashboard_control.os.kill", return_value=None), \
                mock.patch("herdr_fleet.dashboard_control.identity", return_value="b" * 64):
            self.path.write_text(json.dumps(variants[0])); watcher_eligible(self.fleet)
            for value in variants[1:]:
                with self.subTest(value=value):
                    self.path.write_text(json.dumps(value)); before = self.path.read_bytes()
                    with self.assertRaises(ControlError): watcher_eligible(self.fleet)
                    self.assertEqual(self.path.read_bytes(), before)
            self.path.write_text(json.dumps(self.record)); self.path.chmod(0o666)
            with self.assertRaises(ControlError): watcher_eligible(self.fleet)

    def test_disabled_absent_and_positively_dead_watchers_stay_distinct_from_unknown(self):
        watcher_eligible(self.fleet)
        self.path.write_text(json.dumps(dict(self.record, process_identity_scheme=None)))
        with mock.patch("herdr_fleet.dashboard_control.os.kill", side_effect=ProcessLookupError):
            watcher_eligible(self.fleet)
        with mock.patch("herdr_fleet.dashboard_control.os.kill", side_effect=PermissionError), self.assertRaises(ControlError):
            watcher_eligible(self.fleet)
        self.path.write_text("not-json"); self.fleet.watch = False
        watcher_eligible(self.fleet)

    @unittest.skipUnless(os.name == "posix" and Path("/bin/ps").is_file(), "POSIX ps inspection unavailable")
    def test_isolated_owned_child_capture_verifies_across_environments_without_lock_changes(self):
        child = subprocess.Popen([sys.executable, "-u", "-c",
            "import sys;print('ready',flush=True);sys.stdin.buffer.read()", "synthetic-argument-" + "x" * 8192],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            self.assertTrue(select.select([child.stdout], [], [], 3)[0])
            self.assertEqual(child.stdout.readline(), b"ready\n")
            with mock.patch("herdr_fleet.build_identity.current", return_value=self.record["launch"]):
                with mock.patch.dict(os.environ, LC_ALL="en_US.UTF-8", LC_TIME="en_US.UTF-8", TZ="America/New_York", COLUMNS="40"):
                    captured = watch.acquire(self.fleet.repo, self.fleet.path, pid=child.pid)
                self.assertIsNotNone(captured["process_identity"])
                self.assertEqual(captured["process_identity_scheme"], "ps-c-utc-v1")
                before = self.path.read_bytes()
                with mock.patch.dict(os.environ, LC_ALL="en_AU.UTF-8", LC_TIME="en_AU.UTF-8", TZ="Australia/Melbourne", COLUMNS="4000"):
                    watcher_eligible(self.fleet)
                    self.assertEqual(process_identity.identity(child.pid), captured["process_identity"])
                self.assertEqual(self.path.read_bytes(), before)
            child.stdin.close(); child.wait(timeout=3)
            watcher_eligible(self.fleet)
        finally:
            if child.poll() is None: child.kill(); child.wait(timeout=3)
            for pipe in (child.stdin, child.stdout):
                if pipe and not pipe.closed: pipe.close()


class ProbeGuidance(unittest.TestCase):
    def test_only_the_fixed_upgrade_condition_reaches_the_probe_caller(self):
        upgrade = dashboard_control.WatcherUpgradeRequired()
        upgrade.args = ("SYNTHETIC_PRIVATE_DETAIL_46",)
        for error in (upgrade, RuntimeError("SYNTHETIC_PRIVATE_DETAIL_46")):
            manager = SimpleNamespace(probe_slots=threading.BoundedSemaphore(1), snapshot=mock.Mock(side_effect=error))
            probe = dashboard_control.Probe(manager, {})
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(ControlError) as found: probe.wait(threading.Event())
                self.assertNotIn("SYNTHETIC_PRIVATE_DETAIL_46", str(found.exception))
                if isinstance(error, dashboard_control.WatcherUpgradeRequired):
                    self.assertIsInstance(found.exception, dashboard_control.WatcherUpgradeRequired)
                    self.assertRegex(str(found.exception), "restart.*watcher")
                self.assertTrue(manager.probe_slots.acquire(False)); manager.probe_slots.release()

    def test_cancelled_probe_keeps_the_existing_generic_refusal(self):
        manager = SimpleNamespace(probe_slots=threading.BoundedSemaphore(1),
                                  snapshot=mock.Mock(side_effect=dashboard_control.WatcherUpgradeRequired()))
        stopped = threading.Event(); stopped.set()
        probe = dashboard_control.Probe(manager, {})
        with self.assertRaises(ControlError) as found: probe.wait(stopped)
        self.assertNotIsInstance(found.exception, dashboard_control.WatcherUpgradeRequired)
        self.assertNotIn("restart", str(found.exception))


class AuthenticatedGuidance(dashboard_tests.DashboardCase):
    request = dashboard_tests.HTTPAuthority.request
    login = dashboard_tests.HTTPAuthority.login

    def setUp(self):
        super().setUp()
        from herdr_fleet.dashboard import DashboardServer
        from herdr_fleet.dashboard_terminal import Watches
        self.write_fleet("watch=true\n" + self.read_fleet())
        self.server = DashboardServer(("127.0.0.1", 0), self.observer, Watches(self.client, self.registry), self.actions)
        self.worker = threading.Thread(target=self.server.serve_forever); self.worker.start()
        self.cookie = self.csrf = None; self.login()
        self.client.stream_runner = mock.Mock(side_effect=AssertionError("No controller on read-only refusal"))

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.worker.join(timeout=5)
        super().tearDown()

    def body(self):
        seat = self.seat(); self.mark("control_prepare", "term_w2:p2")
        return dict(fleet=self.fleet_id, seat=seat["name"], view=seat["view"], tab="synthetic_tab_0123456789")

    def test_legacy_prepare_returns_restart_guidance_without_nonce_controller_or_writes(self):
        path = Path(self.repo, ".herdr/watch.lock")
        path.write_text(json.dumps(dict(pid=12345, fleet=str(Path(self.repo, ".herdr/fleet.toml")),
            control_gate_protocol=1, launch=dict(version="0.23.0", commit="a" * 40), process_identity="b" * 64)))
        before = path.read_bytes(); body = self.body(); files = snapshot(self.repo)
        with mock.patch("herdr_fleet.dashboard_control.os.kill", return_value=None), \
                mock.patch("herdr_fleet.permits.Store", side_effect=AssertionError("No gate files during prepare")):
            code, _, raw = self.request("POST", "/api/control/prepare", body, authenticated=True)
            self.assertTrue(watch.watcher_status(self.repo)["running"])
        self.assertEqual(code, 400)
        self.assertEqual(json.loads(raw), {"error": "Control unavailable: restart this fleet's watcher with the current plugin before using Control."})
        self.assertIn("current plugin", json.loads(raw)["error"])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(snapshot(self.repo), files)
        self.assertFalse(self.server.controls.pending); self.assertFalse(self.server.controls.leases)
        self.client.stream_runner.assert_not_called()

    def test_auth_origin_and_schema_fail_before_watcher_probe_or_guidance(self):
        body = self.body()
        with mock.patch.object(self.server.controls, "snapshot", side_effect=AssertionError("Invalid request reached probe")) as probe:
            for headers, authenticated in (({}, False), ({"Origin": "http://foreign.example"}, True),
                                           ({"X-Fleet-CSRF": None}, True)):
                with self.subTest(headers=headers, authenticated=authenticated):
                    code, _, raw = self.request("POST", "/api/control/prepare", body, headers, authenticated=authenticated)
                    self.assertEqual(code, 403); self.assertNotIn("restart", raw.decode())
            code, _, raw = self.request("POST", "/api/control/prepare", dict(body, arbitrary_target="forbidden"), authenticated=True)
            self.assertEqual(code, 400); self.assertNotIn("restart", raw.decode())
            probe.assert_not_called()
        self.assertFalse(self.server.controls.pending); self.assertFalse(self.server.controls.leases)

    def test_authority_expiring_during_probe_does_not_receive_upgrade_details(self):
        body = self.body(); authority = self.server.authority
        token = self.cookie.split("=", 1)[1]
        def expired(selection):
            with authority.lock:
                entry = authority.sessions[token]; authority.sessions[token] = (entry[0], 0)
            raise dashboard_control.WatcherUpgradeRequired()
        with mock.patch.object(self.server.controls, "snapshot", side_effect=expired):
            code, _, raw = self.request("POST", "/api/control/prepare", body, authenticated=True)
        self.assertEqual(code, 400); self.assertNotIn("restart", raw.decode())
        self.assertFalse(self.server.controls.pending); self.assertFalse(self.server.controls.leases)
        self.client.stream_runner.assert_not_called()

    def test_unrelated_sensitive_exceptions_keep_the_existing_generic_response(self):
        body = self.body()
        for error in (RuntimeError("SYNTHETIC_PRIVATE_DETAIL_46"), ControlError("SYNTHETIC_PRIVATE_DETAIL_46")):
            with self.subTest(error=type(error).__name__), mock.patch.object(self.server.controls, "snapshot", side_effect=error):
                code, _, raw = self.request("POST", "/api/control/prepare", body, authenticated=True)
            self.assertEqual(code, 400)
            self.assertNotIn("SYNTHETIC_PRIVATE_DETAIL_46", raw.decode())
            self.assertNotIn("restart", raw.decode())
        self.assertFalse(self.server.controls.pending); self.assertFalse(self.server.controls.leases)
        self.client.stream_runner.assert_not_called()
