"""Stage-one cooperation: fake native clients and isolated OS advisory locks."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
import contextlib
import fcntl
from unittest import mock
import threading
import signal
import io
from pathlib import Path

from support import LiveFleet
from test_inbox import put, box
from herdr_fleet import inbox
from test_section31 import FleetPair
import test_section41 as dashboard_tests
import test_watch as watch_tests
import test_section21 as presence_tests
from test_section40 import snapshot
from test_inbox import log_entries


def retained_now(case, repo, name, route, pane, unavailable=False):
    """A temporary gate hold never turns an accepted enqueue into disposal."""
    for state in ("idle", "working"):
        case.world["panes"][pane]["agent"]["agent_status"] = state
        before = len(case.prompts_to(name))
        from herdr_fleet import permits
        domain = next(d for d in permits.endpoints(case.env) if "/herdr/" in d.api)
        gate = (mock.patch.object(permits.Store, "acquire", side_effect=permits.Unavailable("synthetic storage failure"))
                if unavailable else holder(os.path.join(case.tmp, "permits"), domain, [("N", name)]))
        with gate:
            code, out, err = case.run_cli("send", route, "Synthetic held " + state, "--now", "--as", "owner")
            case.assertEqual(code, 0, out + err)
            case.assertIn("synthetic storage failure" if unavailable else "owner control", out)
            pending = inbox.read_box(repo, name)
            case.assertEqual(len(pending), 1)
            message = pending[0]
            case.assertNotIn(message.id, box(repo, name, inbox.CUR))
            case.assertEqual(len(case.prompts_to(name)), before)
            for root in {case.repo, repo}:
                entries = [e for e in log_entries(root) if e.get("id") == message.id]
                case.assertFalse(any(e.get("kind") in ("dropped", "refused", "delivered") or
                                     e.get("delivery") == "refused" for e in entries), entries)
            content = Path(message.path).read_bytes()
        args = ("deliver", name, "--fleet", os.path.join(repo, ".herdr/fleet.toml"))
        if state == "working":
            case.run_cli(*args)
            case.assertEqual(box(repo, name, inbox.NEW), [message.id])
            case.assertEqual(Path(message.path).read_bytes(), content)
            case.assertEqual(len(case.prompts_to(name)), before)
        case.world["panes"][pane]["agent"]["agent_status"] = "done"
        code, out, err = case.run_cli(*args)
        case.assertEqual(code, 0, out + err)
        case.run_cli(*args)
        case.assertEqual(box(repo, name, inbox.NEW), [])
        case.assertEqual(len(case.prompts_to(name)), before + 1)
        for root in {case.repo, repo}:
            case.assertEqual(len([e for e in log_entries(root) if e.get("id") == message.id and
                                  e.get("kind") == "delivered"]), 1)


@contextlib.contextmanager
def exclusive(store, domains, values):
    """Raw OS lock fixture only; production cannot mint a human permit."""
    with store.acquire(domains, values) as permit:
        for fd in permit.descriptors():
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield permit


class Resolver(unittest.TestCase):
    def test_same_override_ignores_home_xdg_tmp_and_socket_inode_changes(self):
        from herdr_fleet import permits
        with tempfile.TemporaryDirectory() as folder:
            api = folder + "/api.sock"; Path(api).write_text("synthetic inode one")
            one = permits.endpoints({"HERDR_SOCKET_PATH": api, "HOME": "/profile-one", "TMPDIR": "/one"})[0]
            os.unlink(api); Path(api).write_text("synthetic inode two")
            two = permits.endpoints({"HERDR_SOCKET_PATH": api, "XDG_CONFIG_HOME": "/profile-two", "TMPDIR": "/two"})[0]
            self.assertEqual(one.key, two.key)
            with mock.patch.dict(os.environ, {"HOME": "/elsewhere", "XDG_CONFIG_HOME": "/other", "TMPDIR": "/other"}):
                self.assertEqual(permits.default_root(), os.path.realpath("/tmp") + "/herdr-fleet-control-%d" % os.geteuid())

    def test_home_and_equivalent_xdg_default_candidates_agree(self):
        from herdr_fleet import permits
        one = permits.endpoints({"HOME": "/sample", "HERDR_SESSION": "default"})
        two = permits.endpoints({"XDG_CONFIG_HOME": "/sample/.config"})
        self.assertEqual([d.key for d in one], [d.key for d in two])

    def test_client_mismatch_does_not_retarget_or_disable_api_only_producer(self):
        from herdr_fleet import permits
        domain = permits.endpoints({"HOME": "/sample", "HERDR_CLIENT_SOCKET_PATH": "/unrelated/client.sock"})[0]
        chosen = permits.selected([domain], {"status": "running", "running": True, "socket": domain.api})
        self.assertEqual(chosen.frozen({})["HERDR_SOCKET_PATH"], domain.api)
        self.assertFalse(chosen.client_matches)
        with self.assertRaises(permits.Unavailable): chosen.frozen({}, client_protocol=True)

    def test_override_shadows_invalid_session_and_client_override(self):
        from herdr_fleet import permits
        domains = permits.endpoints({"HERDR_SOCKET_PATH": "/sample/api.sock", "HERDR_SESSION": "../bad",
                                     "HERDR_CLIENT_SOCKET_PATH": "/other/client.sock"})
        self.assertEqual(len(domains), 1)
        frozen = domains[0].frozen({"HERDR_CLIENT_SOCKET_PATH": "/other/client.sock"})
        self.assertEqual(frozen["HERDR_SOCKET_PATH"], "/sample/api.sock")
        self.assertNotIn("HERDR_CLIENT_SOCKET_PATH", frozen)

    def test_unusable_roots_sessions_and_api_never_fall_back(self):
        from herdr_fleet import permits
        for env in ({}, {"HOME": ""}, {"HOME": "/sample", "XDG_CONFIG_HOME": ""},
                    {"HOME": "/sample", "HERDR_SESSION": ""}, {"HOME": "/sample", "HERDR_SESSION": ".."},
                    {"HERDR_SOCKET_PATH": "relative"}, {"HERDR_SOCKET_PATH": ""}):
            with self.subTest(env=env), self.assertRaises(permits.Unavailable):
                permits.endpoints(env)

    def test_renamed_socket_symlink_needs_matching_original_client(self):
        from herdr_fleet import permits
        with tempfile.TemporaryDirectory() as folder:
            api = folder + "/api.sock"
            alias = folder + "/alias.sock"
            os.symlink(api, alias)
            self.assertFalse(permits.endpoints({"HERDR_SOCKET_PATH": alias})[0].client_matches)
            os.symlink(folder + "/api-client.sock", folder + "/alias-client.sock")
            domain = permits.endpoints({"HERDR_SOCKET_PATH": alias})[0]
            self.assertTrue(domain.client_matches)
            self.assertEqual(domain.api, os.path.realpath(api))

    def test_selected_status_is_top_level_exact_running_path_not_rpc_or_remote(self):
        from herdr_fleet import permits
        domains = permits.endpoints({"HOME": "/sample"})
        self.assertEqual(permits.selected(domains, {"socket": domains[1].api, "running": True,
                                                   "status": "running"}), domains[1])
        self.assertEqual(permits.selected(domains, {"socket": domains[0].api, "running": False,
                                                   "status": "not_running"}), domains[0])
        for status in ({"result": {"socket": domains[0].api, "running": True}},
                       {"socket": "remote:sample", "running": True, "status": "running"},
                       {"socket": "/wrong/api.sock", "running": True, "status": "running"},
                       {"socket": domains[0].api, "running": True, "status": "not_running"}):
            with self.assertRaises(permits.Unavailable):
                permits.selected(domains, status)

    def test_defaults_keep_both_build_profiles_without_creating_config(self):
        from herdr_fleet import permits
        with tempfile.TemporaryDirectory() as folder:
            candidates = permits.endpoints({"HOME": folder, "HERDR_SESSION": "sample"})
            canonical = os.path.realpath(folder)
            self.assertEqual([d.api for d in candidates], sorted([
                canonical + "/.config/herdr/sessions/sample/herdr.sock",
                canonical + "/.config/herdr-dev/sessions/sample/herdr.sock"]))
            self.assertFalse(os.path.exists(folder + "/.config"))


class NativeClassifier(unittest.TestCase):
    def test_fixed_discovery_and_reads_remain_supported_and_notification_is_exempt(self):
        from herdr_fleet.herdr import Herdr
        seen = []
        client = Herdr("/fake/herdr", lambda argv: (seen.append(argv[1:]) or (0, '{"result":{}}', "")))
        reads = (("--version",), ("agent",), ("agent", "start", "--help"), ("agent", "list"),
                 ("agent", "get", "sample-dev"), ("integration", "status"),
                 ("plugin", "config-dir", "yarrasys.fleet"), ("plugin", "list", "--json"),
                 ("pane", "current", "--current"), ("pane", "get", "w2:p2"),
                 ("pane", "read", "w2:p2", "--lines", "12", "--format", "ansi"),
                 ("agent", "read", "sample-dev", "--lines", "16", "--source", "visible"),
                 ("agent", "wait", "sample-dev", "--until", "idle", "--until", "done", "--timeout", "100"),
                 ("workspace", "list"), ("workspace", "get", "w2"),
                 ("tab", "list", "--workspace", "w2"),
                 ("notification", "show", "Synthetic report", "--body", "Metadata", "--sound", "done"))
        for argv in reads: client.run(*argv); client.text(*argv)
        self.assertEqual(len(seen), 2 * len(reads))

    def test_unknown_commands_remain_forbidden_with_a_broad_real_producer_permit(self):
        from herdr_fleet.herdr import Herdr, HerdrError
        from support import client_permit_fixture
        seen = []
        client = Herdr("/fake/herdr", lambda argv: (seen.append(argv) or (0, "{}", "")))
        with client_permit_fixture(client, topology=True) as bound:
            for argv in (("tab", "create"), ("api", "call", "agent.prompt"), ("command", "invoke", "quit"),
                         ("--session", "other", "agent", "get", "sample-dev"),
                         ("session", "attach", "sample"), ("terminal", "session", "control", "term_sample")):
                for method in (bound.run, bound.text):
                    with self.assertRaises(HerdrError): method(*argv)
        self.assertEqual(seen, [])

    def test_valid_mutations_without_coverage_and_duplicate_options_never_execute(self):
        from herdr_fleet.herdr import Herdr, HerdrError
        seen = []
        client = Herdr("/fake/herdr", lambda argv: (seen.append(argv) or (0, "{}", "")))
        for argv in (("agent", "prompt", "sample-dev", "request"), ("pane", "close", "w2:p2"),
                     ("agent", "wait", "sample-dev", "--until", "idle"),
                     ("agent", "get", "sample-dev", "--json"),
                     ("pane", "read", "w2:p2", "--lines", "12", "--lines", "16")):
            for method in (client.run, client.text):
                with self.assertRaises(HerdrError): method(*argv)
        self.assertEqual(seen, [])

    def test_unknown_dispatch_and_malformed_shapes_never_reach_either_runner(self):
        from herdr_fleet.herdr import Herdr, HerdrError
        seen = []
        client = Herdr("/fake/herdr", lambda argv: (seen.append(argv) or (0, "{}", "")))
        for argv in (("tab", "create"), ("command", "invoke", "arbitrary"), ("api", "call", "arbitrary"),
                     ("agent", "future-operation", "sample-dev"), ("--json", "pane", "close", "w2:p2"),
                     ("agent", "get", "sample-dev", "--unknown"), ("pane", "read", "w2:p2", "--lines")):
            for method in (client.run, client.text):
                with self.subTest(argv=argv, method=method.__name__), self.assertRaises(HerdrError): method(*argv)
        self.assertEqual(seen, [])


class Processes(unittest.TestCase):
    def test_blocked_output_child_retains_exclusion_until_exact_cleanup(self):
        from herdr_fleet import permits
        with tempfile.TemporaryDirectory() as folder:
            domain = permits.endpoints({"HERDR_SOCKET_PATH": folder + "/api.sock"})[0]
            store = permits.Store(folder + "/locks")
            permit = store.acquire([domain], [("N", "sample-dev")])
            child = subprocess.Popen([sys.executable, "-B", "-c", "import os; os.write(1,b'x'*1048576)"],
                                     stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, pass_fds=permit.descriptors())
            try:
                permit.close()
                with self.assertRaises(BlockingIOError):
                    with exclusive(store, [domain], [("N", "sample-dev")]): pass
                child.terminate(); child.wait(timeout=5)
                with exclusive(store, [domain], [("N", "sample-dev")]): pass
            finally:
                if child.poll() is None: child.kill(); child.wait(timeout=5)
                child.stdout.close()

    def test_late_accepted_work_is_not_recalled_when_submission_child_has_exited(self):
        from herdr_fleet import permits
        with tempfile.TemporaryDirectory() as folder:
            domain = permits.endpoints({"HERDR_SOCKET_PATH": folder + "/api.sock"})[0]
            store = permits.Store(folder + "/locks")
            # This independent worker models work already accepted upstream. It
            # deliberately receives no submission descriptor and is not cancelled.
            worker = subprocess.Popen([sys.executable, "-B", "-c", "import sys; print('accepted',flush=True); sys.stdin.read(); print('completed',flush=True)"],
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            try:
                with store.acquire([domain], [("N", "sample-dev")]):
                    self.assertEqual(worker.stdout.readline(), b"accepted\n")
                with exclusive(store, [domain], [("N", "sample-dev")]):
                    worker.stdin.close()
                    self.assertEqual(worker.stdout.readline(), b"completed\n")
                    worker.wait(timeout=5)
            finally:
                if worker.poll() is None: worker.kill(); worker.wait(timeout=5)
                worker.stdout.close()

    def test_recycled_numeric_ident_cannot_borrow_another_thread_objects_permit(self):
        from herdr_fleet import permits
        with tempfile.TemporaryDirectory() as folder:
            domain = permits.endpoints({"HERDR_SOCKET_PATH": folder + "/api.sock"})[0]
            first, reused = threading.Thread(), threading.Thread()
            with mock.patch.object(threading, "get_ident", return_value=123), mock.patch.object(threading, "current_thread", return_value=first) as current:
                with permits.Store(folder + "/locks").acquire([domain], [("N", "sample-dev")]) as permit:
                    current.return_value = reused
                    with self.assertRaises(permits.Unavailable): permit.descriptors()
                    current.return_value = first

    def test_cumulative_growth_refuses_before_opening_and_retains_old_exclusion(self):
        from herdr_fleet import permits
        with tempfile.TemporaryDirectory() as folder:
            domain = permits.endpoints({"HERDR_SOCKET_PATH": folder + "/api.sock"})[0]
            store = permits.Store(folder + "/locks")
            with store.acquire([domain], [("W", "w2"), ("N", "sample-dev")], topology=True, growth=400) as permit:
                permit.derive("w2", [("P", "w2:p%d" % n) for n in range(300)])
                before = os.listdir(os.path.join(store.root, domain.key))
                with self.assertRaises(permits.Unavailable):
                    permit.derive("w2", [("T", "term%d" % n) for n in range(300)])
                self.assertEqual(os.listdir(os.path.join(store.root, domain.key)), before)
                self.assertTrue(permit.covers([("P", "w2:p1")]))
                with self.assertRaises(BlockingIOError):
                    with exclusive(store, [domain], [("N", "sample-dev")]): pass

    def test_native_producer_child_inherits_permits_after_parent_is_killed(self):
        from herdr_fleet import permits
        import time
        with tempfile.TemporaryDirectory() as folder:
            root, api, marker = folder + "/locks", folder + "/api.sock", folder + "/submitted"
            binary = folder + "/fake-native"
            native = """#!%s
import sys,os,json,signal
from pathlib import Path
args=sys.argv[1:]
record={'name':'sample-dev','pane_id':'w2:p2','terminal_id':'term_sample','workspace_id':'w2'}
if args[:2]==['status','server']: print(json.dumps({'status':'running','running':True,'socket':os.environ['HERDR_SOCKET_PATH']}))
elif args[:2]==['agent','get']: print(json.dumps({'result':{'agent':record}}))
elif args[:2]==['agent','list']: print(json.dumps({'result':{'agents':[record]}}))
elif args[:2]==['agent','prompt']:
 Path(os.environ['FAKE_SUBMITTED']).write_text(str(os.getpid()))
 signal.pause()
else: sys.exit(2)
""" % sys.executable
            with open(binary, "w") as handle: handle.write(native)
            os.chmod(binary, 0o700)
            parent_code = """import sys
from unittest import mock
from herdr_fleet.herdr import Herdr
client=Herdr(sys.argv[1]); client.configure({'HERDR_SOCKET_PATH':sys.argv[2],'FAKE_SUBMITTED':sys.argv[4]})
with mock.patch('herdr_fleet.permits.default_root',return_value=sys.argv[3]):
 with client.target_operation('sample-dev') as bound:
  bound.agent_prompt('sample-dev','synthetic request')
"""
            parent = subprocess.Popen([sys.executable, "-B", "-c", parent_code, binary, api, root, marker],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            child_pid = None
            try:
                deadline = time.monotonic() + 5
                while not os.path.exists(marker):
                    if parent.poll() is not None or time.monotonic() > deadline:
                        self.fail("fake native child failed: " + parent.stderr.read().decode())
                    time.sleep(0.01)
                with open(marker) as handle: child_pid = int(handle.read())
                parent.kill(); parent.wait(timeout=5)
                domain = permits.endpoints({"HERDR_SOCKET_PATH": api})[0]
                with self.assertRaises(BlockingIOError):
                    with exclusive(permits.Store(root), [domain], [("N", "sample-dev")]): pass
            finally:
                if child_pid:
                    try: os.kill(child_pid, signal.SIGKILL)
                    except ProcessLookupError: pass
                if parent.poll() is None:
                    parent.kill(); parent.wait(timeout=5)
                parent.stdout.close(); parent.stderr.close()

    def test_budget_fails_before_creating_any_gate_file(self):
        from herdr_fleet import permits
        with tempfile.TemporaryDirectory() as folder:
            root = folder + "/locks"
            domain = permits.endpoints({"HERDR_SOCKET_PATH": folder + "/api.sock"})[0]
            with mock.patch.object(permits.resource, "getrlimit", return_value=(32, 32)):
                with self.assertRaises(permits.Unavailable):
                    permits.Store(root).acquire([domain], [("N", "sample-dev")])
            self.assertFalse(os.path.exists(root))

    def test_parent_death_keeps_producer_exclusion_until_exact_child_is_killed(self):
        from herdr_fleet import permits
        with tempfile.TemporaryDirectory() as folder:
            root, api = folder + "/locks", folder + "/api.sock"
            code = """import sys,subprocess,json
from herdr_fleet import permits
d=permits.endpoints({'HERDR_SOCKET_PATH':sys.argv[2]})[0]
p=permits.Store(sys.argv[1]).acquire([d],[('N','sample-dev')])
child=subprocess.Popen([sys.executable,'-B','-c','import signal; signal.pause()'],pass_fds=p.descriptors(),stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
print(child.pid,flush=True)
sys.stdin.read()
"""
            parent = subprocess.Popen([sys.executable, "-B", "-c", code, root, api],
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            child_pid = None
            try:
                child_pid = int(parent.stdout.readline())
                parent.kill(); parent.wait(timeout=5)
                domain = permits.endpoints({"HERDR_SOCKET_PATH": api})[0]
                with self.assertRaises(BlockingIOError):
                    with exclusive(permits.Store(root), [domain], [("N", "sample-dev")]):
                        pass
                os.kill(child_pid, signal.SIGKILL)
                # The child has no parent we can reap. Synchronize on the lock,
                # not a finite sleep claimed as a native drain barrier.
                import time
                deadline = time.monotonic() + 5
                while True:
                    try:
                        with exclusive(permits.Store(root), [domain], [("N", "sample-dev")]):
                            break
                    except BlockingIOError:
                        if time.monotonic() > deadline:
                            raise
                        time.sleep(0.01)
            finally:
                if parent.poll() is None:
                    parent.kill(); parent.wait(timeout=5)
                if child_pid:
                    try: os.kill(child_pid, signal.SIGKILL)
                    except ProcessLookupError: pass
                parent.stdin.close(); parent.stdout.close(); parent.stderr.close()

    def test_symlink_hardlink_and_insecure_roots_are_refused_without_repair(self):
        from herdr_fleet import permits
        with tempfile.TemporaryDirectory() as folder:
            domain = permits.endpoints({"HERDR_SOCKET_PATH": folder + "/api.sock"})[0]
            real = folder + "/real"; os.mkdir(real, 0o700)
            os.symlink(real, folder + "/alias")
            with self.assertRaises(permits.Unavailable):
                permits.Store(folder + "/alias").acquire([domain], [("N", "sample-dev")])
            os.chmod(real, 0o755)
            with self.assertRaises(permits.Unavailable):
                permits.Store(real).acquire([domain], [("N", "sample-dev")])
            store = permits.Store(folder + "/locks")
            with store.acquire([domain], [("N", "sample-dev")]): pass
            directory = os.path.join(store.root, domain.key)
            path = os.path.join(directory, os.listdir(directory)[0])
            os.link(path, folder + "/hardlink")
            with self.assertRaises(permits.Unavailable): store.acquire([domain], [("N", "sample-dev")])
            self.assertEqual(os.stat(path).st_nlink, 2)

    def test_all_or_release_and_thread_scope(self):
        from herdr_fleet import permits
        with tempfile.TemporaryDirectory() as folder:
            domain = permits.endpoints({"HERDR_SOCKET_PATH": folder + "/api.sock"})[0]
            store = permits.Store(folder + "/locks")
            with exclusive(store, [domain], [("N", "z")]):
                with self.assertRaises(permits.Held):
                    store.acquire([domain], [("N", "z"), ("N", "a")])
                with exclusive(store, [domain], [("N", "a")]): pass
            with store.acquire([domain], [("N", "a")]) as permit:
                errors = []
                def borrow():
                    try: permit.descriptors()
                    except permits.Unavailable: errors.append(True)
                thread = threading.Thread(target=borrow); thread.start(); thread.join(timeout=5)
                self.assertEqual(errors, [True])

    def test_parent_close_does_not_unlock_an_inheriting_live_child(self):
        from herdr_fleet import permits
        with tempfile.TemporaryDirectory() as folder:
            store = permits.Store(folder + "/locks")
            domain = permits.endpoints({"HERDR_SOCKET_PATH": folder + "/api.sock"})[0]
            held = store.acquire([domain], [("N", "sample-dev")])
            for fd in held.descriptors():
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            child = subprocess.Popen([sys.executable, "-B", "-c", "import sys; print('ready',flush=True); sys.stdin.read()"],
                                     pass_fds=held.descriptors(), stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            try:
                self.assertEqual(child.stdout.readline(), b"ready\n")
                held.close()
                with self.assertRaises(permits.Held):
                    store.acquire([domain], [("N", "sample-dev")])
                child.stdin.close(); child.wait(timeout=5)
                with store.acquire([domain], [("N", "sample-dev")]):
                    pass
            finally:
                if child.poll() is None:
                    child.kill(); child.wait(timeout=5)
                child.stdout.close()

    def test_lock_inodes_survive_release_and_security_errors_are_not_repaired(self):
        from herdr_fleet import permits
        with tempfile.TemporaryDirectory() as folder:
            store = permits.Store(folder + "/locks")
            domain = permits.endpoints({"HERDR_SOCKET_PATH": folder + "/api.sock"})[0]
            with store.acquire([domain], [("N", "sample-dev")]):
                path = os.path.join(store.root, domain.key, os.listdir(os.path.join(store.root, domain.key))[0])
                inode = os.stat(path).st_ino
            with store.acquire([domain], [("N", "sample-dev")]):
                self.assertEqual(os.stat(path).st_ino, inode)
            os.chmod(path, 0o644)
            with self.assertRaises(permits.Unavailable):
                store.acquire([domain], [("N", "sample-dev")])
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o644)

    def test_exclusive_name_holds_another_process_but_not_another_name(self):
        from herdr_fleet import permits
        with tempfile.TemporaryDirectory() as folder:
            root = os.path.join(folder, "locks")
            domain = permits.endpoints({"HERDR_SOCKET_PATH": folder + "/api.sock"})[0]
            store = permits.Store(root)
            with exclusive(store, [domain], [("N", "sample-dev")]):
                script = """import sys
from herdr_fleet import permits
d=permits.endpoints({'HERDR_SOCKET_PATH':sys.argv[2]})[0]
try:
 with permits.Store(sys.argv[1]).acquire([d],[('N',sys.argv[3])]): print('available')
except permits.Held: print('held')
"""
                for name, expected in (("sample-dev", "held"), ("sample-reviewer", "available")):
                    answer = subprocess.run([sys.executable, "-B", "-c", script, root, domain.api, name],
                                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5)
                    self.assertEqual(answer.returncode, 0, answer.stderr)
                    self.assertEqual(answer.stdout.strip(), expected)


@contextlib.contextmanager
def holder(root, domain, values):
    """Independent process models human lock modes, without native Control."""
    code = """import sys,json,fcntl
from herdr_fleet import permits
d=permits.endpoints({'HERDR_SOCKET_PATH':sys.argv[2]})[0]
keys=json.loads(sys.argv[3])
with permits.Store(sys.argv[1]).acquire([d],keys) as p:
 for fd,key in zip(p.descriptors(), permits.resources(keys)):
  if key[0]!='W': fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
 print('ready',flush=True)
 sys.stdin.read()
"""
    process = subprocess.Popen([sys.executable, "-B", "-c", code, root, domain.api, json.dumps(values)],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        if process.stdout.readline() != b"ready\n":
            raise AssertionError(process.stderr.read().decode())
        yield process
    finally:
        if not process.stdin.closed:
            process.stdin.close()
        if process.poll() is None:
            process.wait(timeout=5)
        process.stdout.close(); process.stderr.close()


@contextlib.contextmanager
def file_holder(path):
    code = "import os,sys,fcntl; f=os.open(sys.argv[1],os.O_RDONLY); fcntl.flock(f,fcntl.LOCK_EX); print('ready',flush=True); sys.stdin.read(); os.close(f)"
    child = subprocess.Popen([sys.executable, "-B", "-c", code, path], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        if child.stdout.readline() != b"ready\n": raise AssertionError(child.stderr.read().decode())
        yield child
    finally:
        child.stdin.close()
        if child.poll() is None: child.wait(timeout=5)
        child.stdout.close(); child.stderr.close()


class Operations(LiveFleet):
    def test_client_pair_mismatch_still_allows_api_only_delivery(self):
        env = dict(self.env, HERDR_CLIENT_SOCKET_PATH=os.path.join(self.tmp, "other-client.sock"))
        env.pop("HERDR_SOCKET_PATH", None)
        code, out, err = self.run_cli("send", "sample-dev", "Synthetic API-only request", "--as", "owner", env=env)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(len(self.prompts_to("sample-dev")), 1)

    def test_selected_read_holds_one_permit_through_stdout_flush(self):
        import fake_herdr
        from herdr_fleet.context import Ctx
        from herdr_fleet.herdr import Herdr
        from herdr_fleet.mailroom import _selected
        message = put(self.repo, "sample-dev", "Synthetic selected output")
        started, released = threading.Event(), threading.Event()
        errors = []
        class Flushing(io.StringIO):
            def flush(stream):
                started.set()
                if not released.wait(5): raise AssertionError("flush was not released")
        # Preflight is unnecessary for this explicit file-only operation.
        ctx = Ctx(Herdr("/fake/herdr", fake_herdr.Fake(self.world_path, self.log_path)),
                  self.env, self.clock, Flushing(), io.StringIO(), self.repo)
        def read():
            try: _selected(ctx, "sample-dev", message.id, False, False, None)
            except BaseException as exc: errors.append(exc)
        worker = threading.Thread(target=read)
        worker.start()
        try:
            self.assertTrue(started.wait(5), errors)
            self.assertEqual(box(self.repo, "sample-dev", inbox.CUR), [message.id])
            with self.assertRaises(AssertionError):
                with self.held([("N", "sample-dev")]): pass
        finally:
            released.set(); worker.join(timeout=5)
        self.assertFalse(errors)
        self.assertFalse(worker.is_alive())
        with self.held([("N", "sample-dev")]): pass

    def test_local_now_hold_retains_original_for_ordinary_delivery(self):
        retained_now(self, self.repo, "sample-dev", "sample-dev", "w2:p2")

    def test_local_now_unavailable_retains_original_without_reason_matching(self):
        retained_now(self, self.repo, "sample-dev", "sample-dev", "w2:p2", unavailable=True)

    def test_now_blocked_and_typing_keep_existing_refusal_policy(self):
        import fake_herdr
        for state in ("blocked", "idle"):
            self.world["panes"]["w2:p2"]["agent"]["agent_status"] = state
            if state == "idle": fake_herdr.type_in(self.world, "w2:p2", "Synthetic owner draft")
            code, out, err = self.run_cli("send", "sample-dev", "Synthetic refused " + state, "--now", "--as", "owner")
            self.assertEqual(code, 1, out + err)
            self.assertIn("refused", out)
            self.assertEqual(box(self.repo, "sample-dev", inbox.NEW), [])
        self.assertEqual(len(box(self.repo, "sample-dev", inbox.CUR)), 2)
        self.assertFalse(self.prompts_to("sample-dev"))

    def test_selected_invalid_and_cur_prevalidate_without_gate_or_fleet_writes(self):
        put(self.repo, "sample-dev", "Synthetic", ident="20261111T010000Z-abc123")
        put(self.repo, "sample-dev", "Synthetic duplicate short", ident="20261111T020000Z-abc123")
        bad = put(self.repo, "sample-dev", "Synthetic bad header")
        Path(bad.path).write_text(Path(bad.path).read_text().replace("to: sample-dev", "to: other-seat"))
        old = put(self.repo, "sample-dev", "Synthetic history", which=inbox.CUR)
        for ref, diagnostic, expected in (("not-an-id", "not a message id", 2),
                                          ("#ffffff", "no message", 2),
                                          ("#abc123", "matches several", 2),
                                          (bad.id, "invalid message header", 2),
                                          (old.id, "already in cur/", 3)):
            with self.subTest(ref=ref):
                fleet_before = snapshot(self.repo)
                gate_before = snapshot(os.path.join(self.tmp, "permits"))
                with mock.patch("herdr_fleet.permits.Store.acquire", side_effect=AssertionError("gate consulted")), \
                        mock.patch.object(inbox, "locked", side_effect=AssertionError("mailbox lock consulted")):
                    code, out, err = self.run_cli("inbox", "--read", ref, "--as", "sample-dev")
                self.assertEqual(code, expected, out + err)
                self.assertIn(diagnostic, out + err)
                self.assertEqual(snapshot(self.repo), fleet_before)
                self.assertEqual(snapshot(os.path.join(self.tmp, "permits")), gate_before)
                self.assertFalse(self.calls())
        # Selection errors must win even when coordination storage is unusable.
        with mock.patch("herdr_fleet.permits.Store.acquire", side_effect=AssertionError("gate consulted")):
            self.assertEqual(self.run_cli("inbox", "--read", "not-an-id", "--as", "sample-dev")[0], 2)

    def test_selected_valid_new_hold_does_not_load_body_or_settle(self):
        message = put(self.repo, "sample-dev", "Synthetic selected private payload")
        before = snapshot(self.repo)
        with self.held([("N", "sample-dev")]), mock.patch.object(inbox, "load", side_effect=AssertionError("body read")):
            code, out, err = self.run_cli("inbox", "--read", message.id, "--as", "sample-dev")
        self.assertNotEqual(code, 0)
        self.assertNotIn(message.body, out + err)
        self.assertEqual(snapshot(self.repo), before)
        self.assertFalse(self.calls())

    def test_selected_revalidates_header_id_and_box_under_mailbox(self):
        original = inbox.locked
        for change in ("header", "id", "cur"):
            with self.subTest(change=change):
                message = put(self.repo, "sample-dev", "Synthetic race " + change)
                @contextlib.contextmanager
                def changed(repo, name):
                    with original(repo, name):
                        if change == "cur": inbox.move(repo, message, inbox.CUR)
                        else:
                            text = Path(message.path).read_text()
                            text = text.replace("to: sample-dev", "to: other-seat") if change == "header" else text.replace(message.id, "20261111T010000Z-123456")
                            Path(message.path).write_text(text)
                        yield
                with mock.patch.object(inbox, "locked", changed):
                    code, out, err = self.run_cli("inbox", "--read", message.id, "--as", "sample-dev")
                self.assertEqual(code, 3 if change == "cur" else 2, out + err)
                self.assertNotIn(message.body, out + err)
                self.assertFalse(any(e.get("id") == message.id for e in log_entries(self.repo)))

    def test_hook_output_error_releases_permit_without_replaying_settlement(self):
        import fake_herdr
        from herdr_fleet.context import Ctx
        from herdr_fleet.herdr import Herdr
        from herdr_fleet.mailroom import op_inbox_hook
        message = put(self.repo, "sample-dev", "PRIVATE HOOK OUTPUT")
        self.run_cli("inbox", "--count", "--as", "sample-dev")
        with open(self.log_path, "a") as log: log.write(json.dumps({"command": ["inbox", "--hook", "stop"]}) + "\n")
        class Broken(io.StringIO):
            def flush(self): raise OSError("synthetic broken output")
        ctx = Ctx(Herdr("/fake/herdr", fake_herdr.Fake(self.world_path, self.log_path, "w2:p2")),
                  dict(self.env, HERDR_PANE_ID="w2:p2"), self.clock, Broken(), io.StringIO(), self.repo)
        self.assertEqual(op_inbox_hook(ctx, "stop", io.StringIO("{}")), 0)
        with self.held([("N", "sample-dev")]): pass
        self.assertEqual(box(self.repo, "sample-dev", inbox.CUR), [message.id])
        from test_inbox import log_entries
        before = log_entries(self.repo)
        self.assertEqual(op_inbox_hook(ctx, "stop", io.StringIO("{}")), 0)
        self.assertEqual(log_entries(self.repo), before)
    def test_file_only_consumer_conservatively_covers_debug_profile_same_name(self):
        from herdr_fleet import permits
        put(self.repo, "sample-dev", "PRIVATE DEFAULT PROFILE")
        debug = next(d for d in permits.endpoints(self.env) if "/herdr-dev/" in d.api)
        with holder(os.path.join(self.tmp, "permits"), debug, [("N", "sample-dev")]):
            code, out, err = self.run_cli("inbox", "--read", "--as", "sample-dev")
            self.assertNotEqual(code, 0)
            self.assertNotIn("PRIVATE DEFAULT PROFILE", out)
            self.assertFalse(self.calls())
    def test_stop_hook_holds_same_name_permit_through_actual_output_flush(self):
        import fake_herdr
        from herdr_fleet.context import Ctx
        from herdr_fleet.herdr import Herdr
        from herdr_fleet.mailroom import op_inbox_hook
        from herdr_fleet import permits
        put(self.repo, "sample-dev", "PRIVATE HOOK OUTPUT")
        self.run_cli("inbox", "--count", "--as", "sample-dev")
        with open(self.log_path, "a") as log: log.write(json.dumps({"command": ["inbox", "--hook", "stop"]}) + "\n")
        started, released = threading.Event(), threading.Event()
        class Flushing(io.StringIO):
            def flush(self):
                started.set()
                if not released.wait(5): raise AssertionError("flush was not released")
        stream = Flushing()
        env = dict(self.env, HERDR_PANE_ID="w2:p2")
        native = Herdr("/fake/herdr", fake_herdr.Fake(self.world_path, self.log_path, "w2:p2"))
        ctx = Ctx(native, env, self.clock, stream, io.StringIO(), self.repo)
        worker = threading.Thread(target=op_inbox_hook, args=(ctx, "stop", io.StringIO("{}")))
        worker.start()
        try:
            self.assertTrue(started.wait(5))
            domain = next(d for d in permits.endpoints(env) if "/herdr/" in d.api)
            # A separate process actually attempts exclusive N while flush stalls.
            with self.assertRaises(AssertionError):
                with holder(os.path.join(self.tmp, "permits"), domain, [("N", "sample-dev")]): pass
        finally:
            released.set(); worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        with self.held([("N", "sample-dev")]): pass
    def test_user_preliminary_wait_does_not_exclude_owner_control(self):
        import fake_herdr
        self.world["panes"]["w2:p2"]["agent"]["agent_status"] = "working"
        self.world["behaviors"]["wait_results"] = {"sample-dev": ["done"]}
        original = fake_herdr.Sim._agent_wait
        checks = []
        def waiting(sim, args):
            self.assertFalse(os.path.exists(os.path.join(self.tmp, "permits")))
            with self.held([("N", "sample-dev"), ("W", "w2")]): checks.append(True)
            return original(sim, args)
        with mock.patch.object(fake_herdr.Sim, "_agent_wait", waiting):
            code, out, err = self.run_cli("restart", "sample-dev", "--wait", "100")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(checks, [True])
    def test_sender_inbox_hint_is_held_but_unrelated_outgoing_send_remains_available(self):
        put(self.repo, "sample-dev", "PRIVATE WAITING REQUEST", sender="sample-reviewer")
        with self.held([("N", "sample-dev")]):
            code, out, err = self.run_cli("send", "sample-reviewer", "outgoing", "--as", "sample-dev")
            self.assertEqual(code, 0, err)
            self.assertIn("inbox held", out)
            self.assertNotIn("PRIVATE WAITING REQUEST", out)
            self.assertEqual(len(self.prompts_to("sample-reviewer")), 1)
            self.assertEqual(len(box(self.repo, "sample-dev", inbox.NEW)), 1)
    def test_control_in_other_workspace_does_not_pause_selected_workspace_topology(self):
        with self.held([("W", "w1"), ("N", "caller"), ("P", "w1:p1"), ("T", "term_w1:p1")]):
            code, out, err = self.run_cli("tile")
            self.assertEqual(code, 0, out + err)
            self.assertTrue(self.argvs("pane", "move"))
    def test_now_and_interrupt_cannot_bypass_a_process_hold(self):
        self.world["panes"]["w2:p2"]["agent"]["agent_status"] = "working"
        with self.held([("N", "sample-dev"), ("P", "w2:p2"), ("T", "term_w2:p2"), ("W", "w2")]):
            code, out, err = self.run_cli("send", "sample-dev", "must not type ahead", "--now", "--as", "owner")
            self.assertEqual(code, 0, out + err)
            self.assertIn("owner control", out + err)
            code, out, err = self.run_cli("interrupt", "sample-dev", "--as", "owner")
            self.assertNotEqual(code, 0)
            self.assertFalse(self.argvs("agent", "prompt"))
            self.assertFalse(self.argvs("agent", "send-keys"))

    def test_hook_hint_and_stop_are_silent_and_keep_held_mail(self):
        message = put(self.repo, "sample-dev", "PRIVATE HOOK CONTEXT")
        env = dict(self.env, HERDR_PANE_ID="w2:p2")
        with self.held([("N", "sample-dev")]):
            for kind in ("prompt", "stop"):
                code, out, err = self.run_cli("inbox", "--hook", kind, stdin="{}", env=env)
                self.assertEqual((code, out, err), (0, "", ""))
            self.assertEqual(len(box(self.repo, "sample-dev", inbox.NEW)), 1)
            self.assertEqual(self.argvs("status", "server"), [])

    def test_wait_has_no_permit_until_batch_and_releases_it_after_printing(self):
        self.run_cli("inbox", "--wait", "--timeout", "0", "--as", "sample-dev")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "permits")))
        put(self.repo, "sample-dev", "PRIVATE BATCH")
        code, out, err = self.run_cli("inbox", "--wait", "--timeout", "0", "--as", "sample-dev")
        self.assertEqual(code, 0, err)
        self.assertIn("PRIVATE BATCH", out)
        # Exclusive independent holder can start immediately after body output.
        with self.held([("N", "sample-dev")]): pass

    def test_nested_restart_retains_one_permit_through_quit_start_and_kickoff(self):
        code, out, err = self.run_cli("restart", "sample-dev")
        self.assertEqual(code, 0, out + err)
        mutations = [c for c in self.calls() if c["argv"][:2] in (["agent", "start"], ["agent", "prompt"])]
        self.assertEqual(len(mutations), 3)
        descriptors = [c["info"]["permit"]["fds"] for c in mutations]
        self.assertEqual(descriptors[0], descriptors[1])
        self.assertEqual(descriptors[1], descriptors[2])

    def test_watch_launch_records_participation_without_control_adapter(self):
        from herdr_fleet import watch
        from herdr_fleet.herdr import Herdr
        lock = watch.acquire(self.repo, os.path.join(self.repo, ".herdr/fleet.toml"))
        self.assertEqual(lock["control_gate_protocol"], 1)
        self.assertTrue(callable(Herdr.terminal_control))  # Stage two adds only its opaque human seam.

    def held(self, values):
        from herdr_fleet import permits
        domain = next(d for d in permits.endpoints(self.env) if "/herdr/" in d.api)
        return holder(os.path.join(self.tmp, "permits"), domain, values)

    def test_other_process_control_keeps_send_queued_and_unrelated_seat_runs(self):
        with self.held([("N", "sample-dev"), ("P", "w2:p2"), ("T", "term_w2:p2"), ("W", "w2")]):
            code, out, _ = self.run_cli("send", "sample-dev", "held request", "--as", "owner")
            self.assertEqual(code, 0, out)
            self.assertIn("owner control", out)
            self.assertFalse(self.prompts_to("sample-dev"))
            self.assertEqual(len(box(self.repo, "sample-dev", inbox.NEW)), 1)
            code, out, _ = self.run_cli("send", "sample-reviewer", "independent", "--as", "owner")
            self.assertEqual(code, 0, out)
            self.assertEqual(len(self.prompts_to("sample-reviewer")), 1)

    def test_other_process_holds_complete_lifecycle_and_sibling_topology(self):
        with self.held([("N", "sample-dev"), ("P", "w2:p2"), ("T", "term_w2:p2"), ("W", "w2")]):
            for argv in (("restart", "sample-dev"), ("swap", "sample-dev"),
                         ("down", "sample-dev"), ("tile",)):
                code, out, err = self.run_cli(*argv)
                self.assertNotEqual(code, 0, (argv, out, err))
            self.assertFalse(self.argvs("agent", "start"))
            self.assertFalse(self.argvs("agent", "prompt"))
            self.assertFalse(self.argvs("pane", "move"))

    def test_name_only_explicit_consumer_is_held_without_native_lookup_or_settlement(self):
        message = put(self.repo, "sample-dev", "PRIVATE MUST STAY QUEUED")
        with self.held([("N", "sample-dev")]):
            code, out, err = self.run_cli("inbox", "--read", "--as", "sample-dev")
            self.assertNotEqual(code, 0)
            self.assertNotIn(message.body, out)
            self.assertEqual(len(box(self.repo, "sample-dev", inbox.NEW)), 1)
            self.assertFalse(self.calls())

    def test_pure_explicit_inspection_and_count_do_not_create_permit_storage(self):
        message = put(self.repo, "sample-dev", "private")
        for argv in (("--show", message.id), ("--count",), ("--history", "owner")):
            self.run_cli("inbox", "--as", "sample-dev", *argv)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "permits")))


class RoutedAcquisition(FleetPair):
    def test_routed_now_hold_retains_original_and_paired_receipts(self):
        retained_now(self, self.other, "peer-lead", "partner", "w3:p1")

    def test_routed_now_unavailable_retains_original_and_paired_receipts(self):
        retained_now(self, self.other, "peer-lead", "partner", "w3:p1", unavailable=True)

    def test_persistent_mailbox_contention_and_no_backoff_with_existing_operation(self):
        from herdr_fleet import crossfleet
        root = os.path.realpath(self.other)
        fd = inbox.open_mailbox_lock(root, "peer-lead", prepare=True); os.close(fd)
        path = os.path.join(root, ".herdr/inbox/peer-lead/.lock")
        with file_holder(path):
            code, out, err = self.send_queue()
            self.assertEqual(code, 1, out + err)
            self.assertIn("destination busy", out)
            before = self.clock.slept
            with self.assertRaises(crossfleet.ReservationBusy):
                with crossfleet.routed_reservation((os.path.realpath(self.repo), root), root, "peer-lead", self.clock, allow_backoff=False): pass
            self.assertEqual(self.clock.slept, before)
        self.assertAlmostEqual(self.clock.slept, 5)
        self.assertFalse(self.pending())
        self.assertFalse(inbox.all_messages(self.repo))

    def test_opposite_direction_cli_senders_finish_with_exact_paired_queues(self):
        source, destination = os.path.realpath(self.repo), os.path.realpath(self.other)
        with open(self.destination, "a") as handle:
            handle.write('\n[[contacts]]\nname="back"\nmatch={name="sample-dev"}\n')
        script = """import sys,json,io
from pathlib import Path
from unittest import mock
sys.path.insert(0,'tests')
import fake_herdr
from herdr_fleet.cli import main
from herdr_fleet.seats import Clock
c=json.loads(Path(sys.argv[1]).read_text());out=io.StringIO();err=io.StringIO()
print('ready',flush=True);sys.stdin.read(1)
with mock.patch('herdr_fleet.permits.default_root',return_value=c['locks']):
 code=main(['send','--queue','--as','owner',c['target'],'Synthetic reverse route'],env=c['env'],runner=fake_herdr.Fake(c['world'],c['log']),clock=Clock(),stdout=out,stderr=err,cwd=c['source'])
print(json.dumps({'code':code,'out':out.getvalue(),'err':err.getvalue()}),flush=True)
"""
        children = []
        try:
            for index, (root, target) in enumerate(((source, "partner"), (destination, "back"))):
                world = os.path.join(self.tmp, "reverse-world-%d.json" % index)
                Path(world).write_text(json.dumps(self.world))
                config = {"source": root, "target": target, "world": world, "log": self.log_path,
                          "locks": os.path.join(self.tmp, "permits"), "env": self.env}
                path = os.path.join(self.tmp, "reverse-%d.json" % index); Path(path).write_text(json.dumps(config))
                child = subprocess.Popen([sys.executable, "-B", "-c", script, path], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                children.append(child)
                self.assertEqual(child.stdout.readline(), b"ready\n")
            for child in children: child.stdin.write(b"g"); child.stdin.flush()
            for child in children:
                out, err = child.communicate(timeout=5)
                self.assertEqual(child.returncode, 0, err.decode())
                result = json.loads(out); self.assertEqual(result["code"], 0, result)
            one = inbox.read_box(destination, "peer-lead", inbox.NEW)
            two = inbox.read_box(source, "sample-dev", inbox.NEW)
            self.assertEqual((len(one), len(two)), (1, 1))
            self.assertEqual([m.id for m in inbox.read_box(source, "peer-lead", inbox.CUR)], [one[0].id])
            self.assertEqual([m.id for m in inbox.read_box(destination, "sample-dev", inbox.CUR)], [two[0].id])
            self.assertNotEqual(one[0].short, two[0].short)
        finally:
            for child in children:
                if child.poll() is None: child.kill(); child.wait(timeout=5)
                if not child.stdin.closed: child.stdin.close()
                child.stdout.close(); child.stderr.close()

    def test_persistent_root_contention_exhausts_without_allocating_or_writing_messages(self):
        with file_holder(os.path.join(self.other, ".herdr")):
            code, out, err = self.send_queue()
        self.assertEqual(code, 1, out + err)
        self.assertIn("destination busy", out)
        self.assertAlmostEqual(self.clock.slept, 5.0)
        for repo in (self.repo, self.other):
            self.assertEqual(inbox.all_messages(repo), [])
            self.assertFalse(os.path.exists(os.path.join(repo, ".herdr/messages.jsonl")))

    def test_missing_mailbox_is_prepared_once_with_stable_lock_before_one_enqueue(self):
        folder = os.path.join(self.other, ".herdr/inbox/peer-lead")
        self.assertFalse(os.path.exists(folder))
        self.assertEqual(self.send_queue()[0], 0)
        inode = os.stat(os.path.join(folder, ".lock")).st_ino
        self.assertEqual(len(self.pending()), 1)
        self.assertEqual(self.send_queue()[0], 0)
        self.assertEqual(os.stat(os.path.join(folder, ".lock")).st_ino, inode)
        self.assertEqual(len(self.pending()), 2)

    def test_post_first_destination_write_error_is_not_acquisition_retry(self):
        actual = inbox.write
        attempts = []
        def write(repo, message, which=inbox.NEW):
            attempts.append((repo, which))
            result = actual(repo, message, which)
            if os.path.realpath(repo) == os.path.realpath(self.other) and which == inbox.NEW:
                raise BlockingIOError(11, "synthetic error after authoritative write")
            return result
        with mock.patch.object(inbox, "write", side_effect=write):
            code, out, err = self.send_queue()
        self.assertEqual(code, 1, out + err)
        self.assertIn("original destination message retained", out)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(len(self.pending()), 1)
        self.assertFalse(inbox.has_queued(self.repo, "peer-lead"))
        self.assertEqual(self.clock.slept, 0)

    def test_permission_error_is_not_retried_and_never_falls_back_to_source_pending(self):
        with mock.patch.object(inbox, "open_mailbox_lock", side_effect=PermissionError("synthetic denied")) as acquire:
            code, out, err = self.send_queue()
        self.assertEqual(code, 1, out + err)
        self.assertEqual(acquire.call_count, 1)
        self.assertFalse(inbox.has_queued(self.repo, "peer-lead"))
        self.assertFalse(self.pending())
        self.assertEqual(self.clock.slept, 0)

    def test_actual_kickoff_state_and_routed_enqueue_do_not_form_a_root_mailbox_cycle(self):
        import time
        source, destination = os.path.realpath(self.repo), os.path.realpath(self.other)
        state_path = os.path.join(destination, ".herdr", "fleet.state.json")
        Path(state_path).write_text(json.dumps({"version": 1, "agents": {"peer-lead": {"pane_id": "w3:p1"}}}))
        kickoff = inbox.Message(id="20261101T100000Z-abc123", sender="herdr-fleet", to="peer-lead",
                                time=inbox.iso_local(inbox.now_utc()), body="Synthetic queued kickoff",
                                queued_by="kickoff", kickoff_reason="up")
        inbox.write(destination, kickoff)
        Path(self.world_path).write_text(json.dumps(self.world))
        config = {"source": source, "destination": destination, "root": self.tmp,
                  "world": self.world_path, "log": self.log_path, "env": self.env}
        setup = os.path.join(self.tmp, "setup.json"); Path(setup).write_text(json.dumps(config))
        owner_code = """import sys,json,io
from pathlib import Path
from herdr_fleet import inbox,messages,permits
from herdr_fleet.context import Ctx
from herdr_fleet.herdr import Herdr
from herdr_fleet.seats import Clock
c=json.loads(Path(sys.argv[1]).read_text()); repo=c['destination']
ctx=Ctx(Herdr('/fake/herdr',lambda args:(0,'{}','')),c['env'],Clock(),io.StringIO(),io.StringIO(),repo,file=repo+'/.herdr/fleet.toml')
with permits.Store(c['root']+'/permits').acquire(permits.endpoints(c['env']),[('N','peer-lead')]):
 with inbox.locked(repo,'peer-lead'):
  print('ready',flush=True);sys.stdin.readline()
  Path(c['root']+'/state-attempt').write_text('ready')
  messages.settle(ctx,inbox.read_box(repo,'peer-lead'),'watch','sent')
print('complete',flush=True)
"""
        sender_code = """import sys,json,os,io,time,fcntl
from pathlib import Path
from unittest import mock
sys.path.insert(0,'tests')
import fake_herdr
from herdr_fleet.cli import main
from herdr_fleet.seats import Clock
c=json.loads(Path(sys.argv[1]).read_text()); original=fcntl.flock; first=[]
lock=c['destination']+'/.herdr/inbox/peer-lead/.lock'; identity=os.stat(lock)
def flock(fd,mode):
 value=os.fstat(fd)
 if not first and value.st_ino==identity.st_ino and value.st_dev==identity.st_dev:
  first.append(True);Path(c['root']+'/roots-held').write_text('ready');end=time.monotonic()+5
  while not Path(c['root']+'/state-attempt').exists():
   if time.monotonic()>end: raise RuntimeError('state barrier timed out')
   time.sleep(0.005)
 return original(fd,mode)
out=io.StringIO();err=io.StringIO()
with mock.patch('fcntl.flock',side_effect=flock),mock.patch('herdr_fleet.permits.default_root',return_value=c['root']+'/permits'):
 result=main(['send','--queue','--as','owner','partner','Synthetic routed request'],env=c['env'],runner=fake_herdr.Fake(c['world'],c['log']),clock=Clock(),stdout=out,stderr=err,cwd=c['source'])
print(json.dumps({'code':result,'out':out.getvalue(),'err':err.getvalue()}),flush=True)
"""
        owner = subprocess.Popen([sys.executable, "-B", "-c", owner_code, setup], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        sender = None
        try:
            self.assertEqual(owner.stdout.readline(), b"ready\n")
            sender = subprocess.Popen([sys.executable, "-B", "-c", sender_code, setup], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            end = time.monotonic() + 5
            while not os.path.exists(os.path.join(self.tmp, "roots-held")):
                if sender.poll() is not None or time.monotonic() > end:
                    self.fail("routed reservation barrier was not reached: " + sender.stderr.read().decode())
                time.sleep(0.005)
            owner.stdin.write(b"go\n"); owner.stdin.flush()
            try:
                raw, errors = sender.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                self.fail("root/mailbox cycle: routed send retained roots while waiting on queued kickoff mailbox")
            self.assertEqual(sender.returncode, 0, errors.decode())
            result = json.loads(raw)
            self.assertEqual(result["code"], 0, result)
            owner.wait(timeout=3)
            self.assertEqual(owner.returncode, 0, owner.stderr.read().decode())
            self.assertTrue(json.loads(Path(state_path).read_text())["agents"]["peer-lead"]["last_kickoff_at"])
            pending = inbox.read_box(destination, "peer-lead", inbox.NEW)
            history = inbox.read_box(source, "peer-lead", inbox.CUR)
            self.assertEqual(len(pending), 1)
            self.assertEqual([m.id for m in history], [pending[0].id])
        finally:
            for child in (sender, owner):
                if child is None: continue
                if child.poll() is None: child.kill(); child.wait(timeout=5)
                if child.stdin: child.stdin.close()
                child.stdout.close(); child.stderr.close()


class StreamEligibility(dashboard_tests.DashboardCase):
    install_stream = dashboard_tests.ObserveProcesses.install_stream

    def mismatch(self, kind):
        env = dict(self.env)
        env.pop("HERDR_SOCKET_PATH", None)
        if kind == "legacy":
            env["HERDR_CLIENT_SOCKET_PATH"] = os.path.join(self.tmp, "unrelated-client.sock")
        else:
            api, alias = os.path.join(self.tmp, "api.sock"), os.path.join(self.tmp, "alias.sock")
            os.symlink(api, alias)
            env["HERDR_SOCKET_PATH"] = alias
        self.client.configure(env)

    def test_watches_client_mismatches_are_bounded_errors_and_slot_is_reusable(self):
        from herdr_fleet.dashboard_terminal import Watches
        for kind in ("legacy", "alias"):
            with self.subTest(kind=kind):
                seat = self.seat()
                self.install_stream([dashboard_tests.frame()])
                self.mismatch(kind)
                self.mark("watch", "term_w2:p2")
                watches = Watches(self.client, self.registry, max_observers=1)
                from herdr_fleet.herdr import HerdrError
                with self.assertRaises(HerdrError) as error:
                    self.client.terminal_observe("term_w2:p2", 80, 24)
                self.assertEqual(error.exception.code, "observe_unavailable")
                result = list(watches.frames(self.fleet_id, "sample-dev", seat["view"], 80, 24, lambda: False))
                self.assertEqual([r["type"] for r in result], ["error"])
                self.assertLess(len(json.dumps(result)), 400)
                self.assertEqual(self.spawned, [])
                self.assertFalse(self.client.domain().client_matches)
                self.assertEqual(self.client.domain().frozen({})["HERDR_SOCKET_PATH"], self.client.domain().api)
                self.assertEqual(watches.active, {})
                self.client.configure(self.env)
                result = list(watches.frames(self.fleet_id, "sample-dev", seat["view"], 80, 24, lambda: False))
                self.assertEqual([r["type"] for r in result], ["frame", "ended"])
                self.assertEqual(len(self.spawned), 1)
                self.assertTrue(all(p.poll() is not None for p in self.spawned))
                watches.close()

    request = dashboard_tests.HTTPAuthority.request
    login = dashboard_tests.HTTPAuthority.login

    def test_authenticated_http_watch_reports_mismatch_and_reuses_slot(self):
        from herdr_fleet.dashboard import DashboardServer
        from herdr_fleet.dashboard_terminal import Watches
        self.server = DashboardServer(("127.0.0.1", 0), self.observer,
                                      Watches(self.client, self.registry, max_observers=1), self.actions)
        worker = threading.Thread(target=self.server.serve_forever)
        worker.start()
        self.cookie = self.csrf = None
        try:
            self.login()
            for kind in ("legacy", "alias"):
                with self.subTest(kind=kind):
                    seat = self.seat()
                    self.install_stream([dashboard_tests.frame()])
                    # Fresh alias per subtest; the direct-stream test owns other paths.
                    self.mismatch(kind)
                    self.mark("watch", "term_w2:p2")
                    body = dict(fleet=self.fleet_id, seat="sample-dev", view=seat["view"], cols=80, rows=24)
                    status, _, raw = self.request("POST", "/api/watch", body, authenticated=True)
                    self.assertEqual(status, 200)
                    self.assertEqual([json.loads(line)["type"] for line in raw.splitlines()], ["error"])
                    self.assertLess(len(raw), 400)
                    self.assertEqual(self.spawned, [])
                    self.assertEqual(self.server.watches.active, {})
                    self.client.configure(self.env)
                    status, _, raw = self.request("POST", "/api/watch", body, authenticated=True)
                    self.assertEqual(status, 200)
                    self.assertEqual([json.loads(line)["type"] for line in raw.splitlines()], ["frame", "ended"])
                    self.assertEqual(len(self.spawned), 1)
        finally:
            self.server.shutdown(); self.server.server_close(); worker.join(timeout=5)


class GateWatcher(watch_tests.WatchCase):
    def test_coordination_hold_rechecks_gate_without_presence_reads_and_delivers_after_release(self):
        from herdr_fleet import permits
        message = put(self.repo, "sample-dev", "Synthetic watched queue")
        domain = next(d for d in permits.endpoints(self.env) if "/herdr/" in d.api)
        gate = holder(os.path.join(self.tmp, "permits"), domain, [("N", "sample-dev")])
        gate.__enter__()
        released = []
        def on_hold(number):
            self.assertFalse(self.prompts_to("sample-dev"))
            self.assertFalse(self.argvs("pane", "read", "w2:p2"))
            if number == 3:
                gate.__exit__(None, None, None)
                released.append(True)
        self.clock = presence_tests.HoldClock(on_hold)
        self.behave(wait_results={"sample-dev": ["idle"]})
        try:
            code, out, err = self.watch(cycles=1)
        finally:
            if not released: gate.__exit__(None, None, None)
        self.assertEqual(code, 0, out + err)
        self.assertIn("owner control, holding 1", out)
        self.assertEqual(self.clock.holds, 3)
        self.assertEqual(len(self.prompts_to("sample-dev")), 1)
        self.assertEqual(box(self.repo, "sample-dev", inbox.CUR), [message.id])
