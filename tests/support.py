"""Shared test fixtures: a temp repo with a fleet file, a fake herdr world,
a fake clock, and ``FleetCase.run_cli`` which calls ``herdr_fleet.cli.main``
in-process with the fake herdr as its runner. All identities, paths, transcript
usage records and message bodies are synthetic test data.

Every test's call log is checked in ``tearDown`` against the safety
invariants of design section 7, so each scenario doubles as a safety test.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import contextlib
import copy
from typing import Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if HERE not in sys.path:
    sys.path.insert(0, HERE)


@contextlib.contextmanager
def client_permit_fixture(client, name="sample-dev", pane="w1:p2", topology=False):
    """Transport-unit fixture with real producer locks and synthetic mapping.

    CLI integration uses actual resolver/status checks, never this fixture.
    """
    from herdr_fleet import permits
    with tempfile.TemporaryDirectory() as folder:
        domain = permits.endpoints({"HERDR_SOCKET_PATH": folder + "/api.sock"})[0]
        record = {"name": name, "pane_id": pane, "terminal_id": "term_" + pane,
                  "workspace_id": pane.split(":", 1)[0], "agent": "claude"}
        values = [("W", record["workspace_id"]), ("N", name), ("P", pane), ("T", record["terminal_id"])]
        with permits.Store(folder + "/locks").acquire([domain], values, topology=topology, growth=5 if topology else 0) as permit:
            bound = copy.copy(client)
            bound.configure({"HERDR_SOCKET_PATH": domain.api})
            bound._domain, bound.permit = domain, permit
            bound._agents = {name: record, pane: record}
            bound._panes = {pane: record}
            yield bound

import fake_herdr  # noqa: E402
from herdr_fleet.cli import main  # noqa: E402

CALLER_WS = "w1"
CALLER_PANE = "w1:p1"


def send_outcomes(output: str) -> str:
    """Policy fixtures compare the outcome column; section 31 tests the full identity envelope.

    Explicitly used at assertions, never by run_cli or the safety call-log checks.
    """
    if not any(line.startswith("sender: ") for line in output.splitlines()):
        return output
    return "\n".join(line.split("; mailbox:", 1)[0] for line in output.splitlines()
                     if not line.startswith("sender: ")) + ("\n" if output.endswith("\n") else "")


def inbox_content(output: str) -> str:
    """Separate a human inbox's identity heading from its existing rendered content."""
    return output.split("\n", 1)[1] if output.startswith("inbox: ") else output


def policy_output(output: str) -> str:
    return inbox_content(send_outcomes(output))


# Design 18.5: `inbox` asks herdr only who the caller is (and nothing at all with --as);
# 18.7: `inbox --hook` only `agent get` on its own pane.
INBOX_CALLS = (["pane", "current"], ["agent", "get"], ["pane", "get"], ["pane", "process-info"],
               ["agent", "list"])  # design 38: only CLI Codex's thread lookup (checked below)
HOOK_CALLS = (["agent", "get"],)
# Design 18.6: the watcher runs agent wait/get/list/prompt and notification show, plus the
# local `herdr --version` of the common preconditions; design 21.2: the presence guard's
# pane get, workspace list and pane read.
WATCH_CALLS = (["agent", "wait"], ["agent", "get"], ["agent", "list"], ["agent", "prompt"],
               ["notification", "show"], ["--version"], ["pane", "get"], ["workspace", "list"], ["pane", "read"],
               ["agent", "read"], ["agent", "focus"], ["pane", "zoom"], ["status", "server"])  # 45: bounded endpoint corroboration
# Design 19.3: only `up` (also from the popup menu) and `ui` open plugin panes; only `up` resizes.
# Design 32.5: `restart NAME --handover` and `swap NAME --handover` open the handover pane, in a tab.
PLUGIN_PANE_COMMANDS = ("up", "ui", "menu")
HANDOVER_COMMANDS = ("restart", "swap")
# Design 32.5: the handover helper acts only for its recorded seat (these commands' targets).
SEAT_TARGETED = (["agent", "prompt"], ["agent", "start"], ["agent", "send-keys"], ["agent", "wait"],
                 ["agent", "read"])
RESIZE_COMMANDS = ("up", "menu")
# Design 22.1 and invariant 20: pane names are kept by these commands (the popup runs the
# lifecycle ones), cleared only by `down` (also from the popup); 22.2: only `go` and the
# popup move the owner's focus.
RENAME_COMMANDS = ("up", "restart", "swap", "kickoff", "tile", "status", "down", "menu", "handover")
CLEAR_COMMANDS = ("down", "menu")
FOCUS_COMMANDS = ("go", "menu", "watch")
# Design 24.4, 25.1 and invariant 22: the board's `agent focus` follows a key the owner pressed
# (the fake screen logs each replayed key as {"key": ...}), one focus per `g` or number key; its
# `pane zoom` follows a `z`, on its own pane; its `agent read` of a seat that was not blocked
# follows a `p`.
GO_KEYS = tuple("123456789") + ("g",)
# Design 23.7 and invariant 21: keys reach an agent only as one `enter` on the exit
# confirmation from restart, swap or down, and as the kind's interrupt keys from `interrupt`;
# never from watch, the popup, deliver or a dry run. `agent read` comes only from `peek`,
# `interrupt` and the quit of restart, swap, down (and the popup's, which reads but never presses).
KEYS = ("enter", "esc", "ctrl+c", "ctrl+u")  # ctrl+u: interrupt's cleanup only (design 23.11)
ENTER_COMMANDS = ("restart", "swap", "down", "handover")  # handover: its restart or swap (32.5)
NO_KEYS_COMMANDS = ("watch", "menu", "deliver")
INTERRUPT_DEFAULTS = {"claude": ["ctrl+c"], "agy": ["ctrl+c", "ctrl+c"]}  # the harness's own copy
READ_COMMANDS = ("peek", "restart", "swap", "down", "menu", "interrupt", "watch",  # watch: blocked seats only
                 "handover")
PEEK_CALLS = (["agent", "list"], ["--version"], ["pane", "current"], ["agent", "read"])

THREE_SEATS = """
workspace = "sample"

[[agents]]
name = "sample-orchestrator"
kind = "claude"
args = ["--model", "claude-opus-5-5"]

[[agents]]
name = "sample-dev"
kind = "claude"
args = ["--model", "claude-opus-5-5"]

[[agents]]
name = "sample-reviewer"
kind = "codex"
args = ["-m", "gpt-6-astra"]
"""


def install_fake_herdr(directory: str, cwd: str) -> Dict[str, str]:
    """Write an executable fake ``herdr`` and a world with a caller pane into
    ``directory``; return the environment a herdr pane would have. The fake and
    a ``python3`` on PATH run under the interpreter running the tests."""
    herdr = os.path.join(directory, "herdr")
    with open(herdr, "w") as handle:
        handle.write('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, os.path.join(HERE, "fake_herdr.py")))
    os.chmod(herdr, 0o755)
    bindir = os.path.join(directory, "pybin")
    os.makedirs(bindir, exist_ok=True)
    os.symlink(sys.executable, os.path.join(bindir, "python3"))
    # Python child fixtures use an internal constructor substitution. Production
    # has no environment/CLI lock-root override, and tests never create its root.
    with open(os.path.join(bindir, "sitecustomize.py"), "w") as handle:
        handle.write("import sys\nsys.path.insert(0, %r)\nfrom herdr_fleet import permits\n"
                     "_Store = permits.Store\nclass IsolatedStore(_Store):\n"
                     "    def __init__(self, root=None):\n"
                     "        super().__init__(root if root is not None else %r)\n"
                     "permits.Store = IsolatedStore\n" % (ROOT, os.path.join(directory, "permits")))
    world = fake_herdr.new_world()
    fake_herdr.add_workspace(world, CALLER_WS, "caller", cwd)
    fake_herdr.put_agent(world, CALLER_PANE, "claude", name="caller", status="working")
    with open(os.path.join(directory, "world.json"), "w") as handle:
        json.dump(world, handle)
    return {"PATH": bindir + os.pathsep + os.environ.get("PATH", ""), "HOME": directory, "PYTHONPATH": bindir,
            "HERDR_ENV": "1", "HERDR_BIN_PATH": herdr, "HERDR_PANE_ID": CALLER_PANE,
            "HERDR_WORKSPACE_ID": CALLER_WS, "FAKE_HERDR_WORLD": os.path.join(directory, "world.json"),
            "FAKE_HERDR_LOG": os.path.join(directory, "calls.jsonl"),
            "FAKE_HERDR_FLEET": os.path.join(cwd, ".herdr", "fleet.toml")}


def read_log(env: Dict[str, str]) -> List[List[str]]:
    path = env["FAKE_HERDR_LOG"]
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as handle:
        return [json.loads(line)["argv"] for line in handle if line.strip()]


class FakeClock:
    """Instant time: ``sleep`` advances ``monotonic``."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.slept += seconds


class FleetCase(unittest.TestCase):
    """Base class. ``self.world`` is edited in memory and saved before each run."""

    fleet_text: Optional[str] = THREE_SEATS

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="herdr-fleet-test-")
        self.permit_root_patch = mock.patch("herdr_fleet.permits.default_root", return_value=os.path.join(self.tmp, "permits"))
        self.permit_root_patch.start()
        self.addCleanup(self.permit_root_patch.stop)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.repo, ".herdr"))
        self.world_path = os.path.join(self.tmp, "world.json")
        self.log_path = os.path.join(self.tmp, "calls.jsonl")
        self.world = fake_herdr.new_world()
        # The caller: an agent pane in its own workspace, like an orchestrator running the CLI.
        fake_herdr.add_workspace(self.world, CALLER_WS, "caller", self.repo)
        fake_herdr.put_agent(self.world, CALLER_PANE, "claude", name="caller", status="working")
        self.clock = FakeClock()
        self.env: Dict[str, str] = {"HERDR_ENV": "1", "HERDR_BIN_PATH": "/fake/bin/herdr", "HOME": self.tmp,
                                    "HERDR_PANE_ID": CALLER_PANE, "HERDR_WORKSPACE_ID": CALLER_WS,
                                    "PATH": os.environ.get("PATH", "")}
        if self.fleet_text is not None:
            self.write_fleet(self.fleet_text)

    def tearDown(self) -> None:
        try:
            self.assert_safety()
        finally:
            shutil.rmtree(self.tmp, ignore_errors=True)

    # -- setup helpers ---------------------------------------------------------

    def write_fleet(self, text: str) -> str:
        path = os.path.join(self.repo, ".herdr", "fleet.toml")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def read_fleet(self) -> str:
        with open(os.path.join(self.repo, ".herdr", "fleet.toml"), "r", encoding="utf-8") as handle:
            return handle.read()

    def write_file(self, rel: str, text: str) -> str:
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def write_state(self, data: Dict) -> None:
        with open(os.path.join(self.repo, ".herdr", "fleet.state.json"), "w", encoding="utf-8") as handle:
            json.dump(data, handle)

    def read_state(self) -> Dict:
        with open(os.path.join(self.repo, ".herdr", "fleet.state.json"), "r", encoding="utf-8") as handle:
            return json.load(handle)

    def fleet_ws(self, ws: str = "w2", label: str = "sample") -> str:
        """Add an existing workspace for the fleet; returns its root pane."""
        return fake_herdr.add_workspace(self.world, ws, label, self.repo)

    # -- running ------------------------------------------------------------------

    def run_cli(self, *argv: str, stdin: str = "", env: Optional[Dict[str, str]] = None,
                cwd: Optional[str] = None) -> Tuple[int, str, str]:
        with open(self.world_path, "w", encoding="utf-8") as handle:
            json.dump(self.world, handle)
        out, err = io.StringIO(), io.StringIO()
        env = env if env is not None else self.env
        marker: Dict = {"command": list(argv)}
        # Independent invariant 6 oracle: the CLI's Codex thread, never its daemon's pane.
        thread = (env.get("CODEX_THREAD_ID") if not env.get("HERDR_PLUGIN_ACTION_ID")
                  and not env.get("HERDR_PLUGIN_ENTRYPOINT_ID") else None)
        caller = env.get("HERDR_PANE_ID")
        protected = {caller} if caller else set()
        if thread:
            panes = [pid for pid, p in self.world["panes"].items()
                     if isinstance((p.get("agent") or {}).get("agent_session"), dict)
                     and p["agent"]["agent_session"].get("source") == "herdr:codex"
                     and p["agent"]["agent_session"].get("value") == thread]
            caller = panes[0] if len(panes) == 1 else None
            protected = ({caller} if caller else {pid for pid, p in self.world["panes"].items()
                                                  if (p.get("agent") or {}).get("agent") == "codex"})
        marker.update(caller_pane=caller, caller_panes=sorted(protected), codex_thread=thread)
        if env.get("HERDR_FLEET_HANDOVER"):  # design 32.5: whose helper made the calls below, and its pane
            marker.update(handover=env["HERDR_FLEET_HANDOVER"], pane=env.get("HERDR_PANE_ID"))
        with open(self.log_path, "a", encoding="utf-8") as handle:  # marks which command made the calls below
            handle.write(json.dumps(marker) + "\n")
        runner = fake_herdr.Fake(self.world_path, self.log_path, env.get("HERDR_PANE_ID"),
                                 os.path.join(self.repo, ".herdr", "inbox"), fleet_file(argv, env, self.repo))
        code = main(list(argv), env=env, runner=runner, clock=self.clock,
                    stdout=out, stderr=err, stdin=io.StringIO(stdin), cwd=cwd or self.repo)
        with open(self.world_path, "r", encoding="utf-8") as handle:
            self.world = json.load(handle)
        return code, out.getvalue(), err.getvalue()

    # -- log inspection -----------------------------------------------------------

    def oracle_permitted_event(self, entry):
        """Give oracle-unit events real isolated resource evidence, never an exemption.

        This constructs log facts only; it submits no native command. Negative
        key/state fixtures still receive valid permit evidence so those guards,
        rather than a missing-permit shortcut, must reject them.
        """
        argv = entry.get("argv")
        if not argv:
            return entry
        required = fake_herdr.permit_need(self.world, argv)
        if required is None:
            return entry
        from herdr_fleet import permits
        values = required["keys"] or [("N", "sample-dev")]
        with permits.Store().acquire(permits.endpoints(self.env)[:1], values, topology=required.get("topology", False)) as permit:
            evidence = {"pid": os.getpid(), "live_fds": True, "fds": list(permit.descriptors()),
                        "claims": [[k, v, "exclusive" if k == "W" and required.get("topology") else "shared"]
                                   for k, v in permit.requests]}
            return dict(entry, info=dict(entry.get("info", {}), permit_required=required, permit=evidence))

    def log_key(self, key: str) -> None:
        """Mark a key the fake screen replays in the call log (design 24.5), so the safety
        check can tell a focus the owner asked for from one nobody did."""
        with open(self.log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"key": key}) + "\n")

    def calls(self) -> List[Dict]:
        if not os.path.exists(self.log_path):
            return []
        with open(self.log_path, "r", encoding="utf-8") as handle:
            return [entry for entry in (json.loads(line) for line in handle if line.strip()) if "argv" in entry]

    def argvs(self, *prefix: str) -> List[List[str]]:
        return [c["argv"] for c in self.calls() if c["argv"][:len(prefix)] == list(prefix)]

    def prompts_to(self, name: str) -> List[str]:
        return [argv[3] for argv in self.argvs("agent", "prompt", name)]

    def pane_of(self, name: str) -> Optional[str]:
        for pid, pane in self.world["panes"].items():
            if pane["agent"] and pane["agent"].get("name") == name:
                return pid
        return None

    def limit_read_allowed(self, argv: List[str], listed: Dict[str, Optional[str]]) -> bool:
        """Design 35.6 (invariant 22): the watcher's `agent read NAME --lines 12` of a seat that was
        idle, done or working at its last `agent list` (the quiet seats' limit check); never the
        visible screen, never more than twelve lines."""
        plain = argv[:4] == ["agent", "read", argv[2], "--lines"] and argv[4:5] == ["12"] and len(argv) == 5
        return plain and listed.get(argv[2]) in ("idle", "done", "working")

    def assert_safety(self) -> None:
        """Design section 7 invariants, checked against the full call log. The exception
        (design 21.1): a prompt from `send --now` or `deliver --now` may reach a working
        agent, and nothing else may; no prompt ever reaches a pane that was then the
        owner's with text on its prompt line or no prompt line on screen (invariant 19,
        recorded by the fake as `owner_typing`); kickoffs reach idle or done seats only (invariant 11),
        `inbox` runs nothing but the caller lookup (`inbox --hook` only
        `agent get`), `watch` only its five commands and never an endless wait
        (invariants 12, 13), and `hooks` runs no herdr command at all. Design 19.3:
        `plugin pane open` comes from `up` or `ui` only, and a watcher pane is never
        prompted, started into or sent anything. Invariant 20 (design 22): `pane rename`
        names only a pane hosting one of the fleet's seats, only to that seat's wanted
        name (its `label`, else its name), and `--clear` comes from `down` alone, for a
        pane hosting no agent; `agent focus` comes from `go` or the popup only.
        Invariant 21 (design 23.7): `agent send-keys` only as one `enter` from `restart`,
        `swap` or `down`, to an agent whose screen then showed the highlighted exit
        confirmation, right after an `agent read` of it, once per command per agent; or as
        the kind's interrupt keys from `interrupt`, to a seat `working` at its last `agent
        get`; never from `watch`, the popup, `deliver` or a dry run. Invariant 22 (design 24,
        25): the board's `agent read` only of a seat whose state at the command's last `agent
        list` was `blocked`, or right after a `p` the fake screen replayed; its `agent focus`
        only right after a `g` or a number key; its `pane zoom` only right after a `z`, and
        only on its own pane (`--current`, or a plugin pane); one command per key."""
        caller = self.env.get("HERDR_PANE_ID")
        command: List[str] = []
        strips = set()
        entries = []
        if os.path.exists(self.log_path):
            with open(self.log_path, "r", encoding="utf-8") as handle:
                entries = [json.loads(line) for line in handle if line.strip()]
        keys = KeysCheck(self, command)
        previous: Optional[List[str]] = None
        previous_exit = 0
        previous_info: Dict = {}
        listed: Dict[str, Optional[str]] = {}  # the states the command's last `agent list` answered
        pressed: Optional[str] = None          # the last key the fake screen replayed, until a focus uses it
        peeked: Optional[str] = None           # the seat a `p` read, whose visible screen may follow (32.3)
        limited: Dict[str, bool] = {}          # design 35.6: pane -> its last read showed a limit line, input empty
        timer_reads = 0
        starts = StartsCheck(self, command)
        helper: Optional[Dict] = None          # design 32.5: the handover helper's seat and pane
        codex_thread = None
        protected = set()
        dashboard_action = None
        dashboard_target = None
        for call in entries:
            if "command" in call:
                keys.finish()
                command = call["command"]
                dashboard_action = call.get("dashboard_action")
                dashboard_target = call.get("dashboard_target")
                keys = KeysCheck(self, command)
                previous = None
                previous_exit, previous_info = 0, {}
                listed, pressed, peeked, limited = {}, None, None, {}
                timer_reads = 0
                starts = StartsCheck(self, command)
                helper = None
                caller = call.get("caller_pane", call.get("pane") or self.env.get("HERDR_PANE_ID"))
                codex_thread = call.get("codex_thread")
                protected = set(call.get("caller_panes", [caller] if caller else []))
                if command[:1] == ["handover"] and call.get("handover"):
                    helper = {"seat": json.loads(call["handover"]).get("seat"), "pane": call.get("pane")}
                continue
            if "key" in call:
                pressed = call["key"]
                continue
            argv, info = call["argv"], call["info"]
            required = info.get("permit_required")
            mutation = (tuple(argv[:2]) in (("agent", "start"), ("agent", "prompt"), ("agent", "send-keys"),
                                           ("agent", "rename"), ("agent", "focus"), ("workspace", "create"), ("workspace", "close"))
                        or (argv[:1] == ["pane"] and argv[1:2] in (["split"], ["move"], ["resize"], ["close"], ["rename"], ["zoom"]))
                        or argv[:3] == ["plugin", "pane", "open"])
            if mutation and argv != ["agent", "start", "--help"]:
                self.assertIsNotNone(required, "mutation omitted independent permit inventory: %s" % argv)
            if required is not None:
                evidence = info.get("permit") or {}
                self.assertTrue(evidence.get("live_fds"), "mutation lacked live inherited-permit descriptors: %s" % argv)
                self.assertEqual(evidence.get("pid"), os.getpid())
                claims = {(k, v): mode for k, v, mode in evidence.get("claims", [])}
                if required.get("creation"):
                    self.assertTrue(any(k == "N" for k, _ in claims), "workspace creation reserved no names")
                for key in required["keys"]:
                    self.assertIn(tuple(key), claims, "mutation lacked target resource %s: %s" % (key, argv))
                    if key[0] == "W" and required["topology"]:
                        self.assertEqual(claims[tuple(key)], "exclusive", "topology ran without exclusive W")
            if argv[:2] == ["status", "server"]:
                self.assertEqual(argv, ["status", "server", "--json"])
            if codex_thread and argv[:2] in (["agent", "send-keys"], ["agent", "prompt"]):
                if argv[:2] == ["agent", "send-keys"] or (command[:1] in (
                        ["restart"], ["swap"], ["down"], ["handover"], ["menu"])):
                    self.assertNotIn(info.get("pane"), protected,
                                     "key or quit prompt reached a possible caller's pane: %s" % argv)
            self.assertTrue(argv, "bare herdr was run")
            if argv[:1] == ["terminal"]:
                self.assertEqual(command[:1], ["dashboard"], "terminal streams are dashboard-only")
                if argv[:3] == ["terminal", "session", "observe"]:
                    self.assertEqual(dashboard_action, "watch")
                else:
                    self.assertEqual(argv[:3], ["terminal", "session", "control"], "unapproved terminal command")
                    self.assertEqual(dashboard_action, "control", "only the named authenticated human seam controls")
            if command[:1] == ["dashboard"]:
                if dashboard_action == "read":
                    self.fail("selected owner reading ran Herdr: %s" % argv)
                if dashboard_action == "send":
                    allowed = (["agent", "list"], ["agent", "get"], ["pane", "read"], ["pane", "get"],
                               ["pane", "current"], ["workspace", "list"], ["pane", "process-info"])
                    if argv[:2] == ["agent", "prompt"]:
                        self.assertEqual(argv[2], dashboard_target, "dashboard prompted an unapproved target")
                    else:
                        self.assertTrue(argv in (["--version"], ["plugin", "list", "--json"],
                                                ["status", "server", "--json"]) or argv[:2] in allowed, argv)
                elif dashboard_action in ("control_prepare", "control") and argv[:2] in (
                        ["agent", "list"], ["pane", "get"], ["pane", "process-info"], ["status", "server"]):
                    if argv[:2] == ["status", "server"]:
                        self.assertEqual(argv, ["status", "server", "--json"])
                    elif argv[:2] in (["pane", "get"], ["pane", "process-info"]):
                        pane = argv[argv.index("--pane") + 1] if "--pane" in argv else argv[2]
                        self.assertIn(dashboard_target, (pane, "term_" + pane))
                elif argv[:3] == ["terminal", "session", "control"]:
                    self.assertEqual(dashboard_action, "control")
                    self.assertEqual(argv[3], dashboard_target)
                    self.assertEqual(argv[4::2], ["--cols", "--rows"])
                    self.assertEqual(len(argv), 8)
                    evidence = info.get("control_permit") or {}
                    self.assertIs(info.get("authenticated_human"), True)
                    self.assertIs(evidence.get("human_control"), True)
                    self.assertIs(evidence.get("live_fds"), True)
                    pane = next((pid for pid in self.world["panes"] if "term_" + pid == argv[3]), None)
                    self.assertIsNotNone(pane)
                    record = self.world["panes"][pane]
                    expected = {("W", record["workspace_id"], "shared"),
                                ("N", record["agent"]["name"], "exclusive"),
                                ("P", pane, "exclusive"), ("T", argv[3], "exclusive")}
                    self.assertEqual({tuple(c) for c in evidence.get("claims", [])}, expected)
                    self.assertEqual(len(evidence.get("fds", [])), 4)
                elif argv[:3] == ["terminal", "session", "observe"]:
                    self.assertEqual(dashboard_action, "watch", "observer was not explicitly selected")
                    self.assertEqual(argv[3], dashboard_target)
                    self.assertEqual(argv[4::2], ["--cols", "--rows"])
                    self.assertEqual(len(argv), 8)
                else:
                    reads = [["--version"], ["agent", "list"], ["plugin", "list", "--json"]]
                    if dashboard_action == "watch":
                        reads.append(["status", "server", "--json"])
                    self.assertIn(argv, reads,
                                  "dashboard observation issued a non-read-only command")
            if command[:1] == ["timers"]:
                self.assertNotIn("--dry-run", command, "offline timers called Herdr")
                self.assertEqual(argv, ["agent", "list"], "timer inspection may only list agents")
                timer_reads += 1
                self.assertLessEqual(timer_reads, 1, "timer inspection listed agents more than once")
            if argv[:2] == ["agent", "list"] and "states" in info:
                listed = dict(info["states"])
            if argv[:2] in (["agent", "read"], ["pane", "read"]) and info.get("pane"):
                limited[info["pane"]] = bool(info.get("limit_shown")) and not info.get("input_text")
            if argv[:2] == ["agent", "get"] and info.get("name"):
                keys.seen(str(info["name"]), info.get("status"))
            if argv[:2] == ["agent", "send-keys"]:
                keys.check(argv, info, previous, previous_exit, call["exit"])
            starts.see(argv, info, call["exit"])
            if helper is not None:
                self.assert_helper(helper, argv, info)
            if argv[:2] == ["agent", "read"]:
                self.assertIn(command[:1] and command[0], READ_COMMANDS, "%s read a screen: %s" % (command[:1], argv))
                visible = "--source" in argv and argv[argv.index("--source") + 1:][:1] == ["visible"]
                after_peek = visible and peeked == argv[2] and (previous or [])[:3] == ["agent", "read", argv[2]]
                fallback = (visible and command[:1] in (["restart"], ["swap"], ["down"], ["interrupt"], ["handover"],
                                                        ["menu"], ["watch"])
                            and previous == ["agent", "read", argv[2], "--lines", argv[4]]
                            and previous_exit == 1 and previous_info.get("error") == "agent_not_idle")
                if visible:  # 32.3: peek and the board's `p`; 36: one fallback after agent_not_idle
                    self.assertTrue(command[:1] == ["peek"] or after_peek or fallback,
                                    "%s read the visible screen without its allowed fallback: %s" % (command[:1], argv))
                if command[:1] == ["watch"]:
                    seats = fake_herdr.fleet_names(fleet_file(command, self.env, self.repo))
                    if pressed == "p":  # design 25.2: the owner asked for the seat's screen
                        pressed = None
                        peeked = argv[2]
                    elif after_peek:  # 32.3: the same `p`, read again with --source visible
                        peeked = None
                    elif self.limit_read_allowed(argv, listed):
                        pass
                    else:
                        self.assertIn(argv[2], seats, "the board read the screen of %s, not a seat: %s"
                                      % (argv[2], argv))
                        self.assertEqual(listed.get(argv[2]), "blocked", "the board read the screen of %s, which "
                                         "was %s at the last agent list: %s" % (argv[2], listed.get(argv[2]), argv))
                if call["exit"] == 0:  # 36: a failed read is not a screen a key decision can trust
                    keys.read.add(argv[2])
                    keys.text[argv[2]] = bool(info.get("input_text"))
            if command[:1] == ["peek"]:
                self.assertIn(argv[:2] if argv[:1] != ["--version"] else argv[:1], PEEK_CALLS, "peek ran %s" % argv)
            previous = argv
            previous_exit, previous_info = call["exit"], info
            if command[:1] == ["inbox"]:
                self.assertIn(argv[:2], INBOX_CALLS, "inbox ran more than the caller lookup: %s" % argv)
                if argv[:2] == ["agent", "list"]:
                    self.assertTrue(codex_thread, "inbox listed agents without a Codex thread")
                if "--drop" not in command:  # design 33.3: `inbox --drop --as NAME` looks the caller up
                    self.assertNotIn("--as", command, "inbox --as ran a herdr command: %s" % argv)
                if "--hook" in command:
                    self.assertIn(argv[:2], HOOK_CALLS, "inbox --hook ran more than agent get: %s" % argv)
            if command[:1] == ["watch"]:
                self.assertIn(argv[:2] if argv[:1] != ["--version"] else argv[:1], WATCH_CALLS,
                              "watch ran %s" % argv)
                if argv[:2] == ["agent", "wait"]:
                    self.assertIn("--timeout", argv, "watch waited without a timeout: %s" % argv)
            self.assertNotEqual(command[:1], ["hooks"], "hooks ran a herdr command: %s" % argv)
            if argv[:3] == ["plugin", "pane", "open"]:
                if command[:1] and command[0] in HANDOVER_COMMANDS:  # design 32.5: the helper, in a new tab
                    self.assertIn("--handover", command, "%s opened a plugin pane: %s" % (command[:1], argv))
                    self.assertEqual(argv[argv.index("--entrypoint") + 1], "handover", argv)
                    self.assertEqual(argv[argv.index("--placement") + 1], "tab", argv)
                    self.assertIn("--no-focus", argv)
                else:
                    self.assertIn(command[0], PLUGIN_PANE_COMMANDS, "%s opened a plugin pane: %s"
                                  % (command[:1], argv))
                if info.get("plugin_pane"):
                    strips.add(info["plugin_pane"])
            if argv[:2] == ["pane", "resize"]:
                self.assertIn(command[0], RESIZE_COMMANDS, "%s resized a pane: %s" % (command[:1], argv))
            if argv[:2] == ["pane", "rename"]:
                self.assert_rename(command, argv, info)
            if argv[:2] == ["pane", "zoom"]:
                self.assertEqual(command[:1], ["watch"], "%s zoomed a pane: %s" % (command[:1], argv))
                self.assertEqual(pressed, "z", "the board zoomed without a z: %s" % argv)
                self.assertNotIn("--current", argv, "the board zoomed --current, which herdr resolves to the owner's "
                                 "focused pane (25.7): %s" % argv)
                self.assertTrue(info.get("own") or info.get("plugin") == "watch",
                                "the board zoomed a pane that is not its own: %s" % argv)
                pressed = None
            if argv[:2] == ["agent", "focus"]:
                self.assertIn(command[0], FOCUS_COMMANDS, "%s moved the owner's focus: %s" % (command[:1], argv))
                if command[:1] == ["watch"]:
                    self.assertIn(pressed, GO_KEYS, "the board moved the owner's focus without a key: %s" % argv)
                    pressed = None
            if argv[:2] == ["agent", "prompt"] or argv[:2] == ["agent", "rename"]:
                self.assertNotIn(argv[2], strips, "a watcher pane was addressed as an agent: %s" % argv)
            if argv[:2] == ["agent", "start"] and "--pane" in argv:
                self.assertNotIn(argv[argv.index("--pane") + 1], strips, "agent started into a watcher pane")
            self.assertNotIn(tuple(argv[:2]), fake_herdr.FORBIDDEN, "forbidden command: %s" % argv)
            if argv[:2] != ["agent", "send-keys"]:  # the one key command, checked by KeysCheck
                self.assertFalse(any(a in ("send-keys", "send-text") for a in argv[:2]), argv)
            self.assertNotEqual(argv[:2], ["pane", "run"], argv)
            if argv[:2] == ["agent", "prompt"]:
                self.assertFalse(info.get("owner_typing"), "%s prompted the pane the owner is typing in: %s"
                                 % (command[:1], argv))
                self.assertFalse(info.get("input_held"), "%s prompted a pane whose input held text or showed no "
                                 "prompt line (design 23.11): %s" % (command[:1], argv))
                quit_of_limited = (command[:1] in (["restart"], ["swap"], ["down"], ["handover"])
                                   and argv[3:4] in (["/exit"], ["/quit"]) and limited.get(info.get("pane")))
                allowed = ("idle", "done", None) + (("working",) if typed_ahead_allowed(command, argv, info)
                                                    or quit_of_limited else ())
                self.assertIn(info.get("target_status"), allowed,
                              "%s prompted an agent in state %s: %s" % (command[:1], info.get("target_status"), argv))
            if argv[:2] == ["agent", "start"] and "--help" not in argv:
                pane = argv[argv.index("--pane") + 1]
                self.assertNotIn(pane, protected, "started into the caller's pane or a possible caller's pane")
                if call["exit"] == 0 or "at_shell" in info:
                    self.assertTrue(info.get("at_shell"), "agent start into a pane not at a shell: %s" % argv)
            if argv[:2] == ["pane", "close"] and call["exit"] == 0:
                self.assertTrue(info.get("created"), "closed a pane herdr-fleet did not create: %s" % argv)
                self.assertFalse(info.get("had_agent"), "closed a pane hosting an agent: %s" % argv)
            if argv[:2] == ["workspace", "close"] and call["exit"] == 0:
                self.assertTrue(info.get("created"), "closed a workspace herdr-fleet did not create")
        keys.finish()

    def assert_helper(self, helper: Dict, argv: List[str], info: Dict) -> None:
        """Design 32.5: the handover helper acts for its recorded seat alone: every prompt,
        start, key, wait and screen read names that seat, every agent rename gives that name,
        a pane rename only names a live seat's pane after that seat (as ``labels.apply`` does
        for every live seat; review N6), and the only pane it closes is its own."""
        seat = helper["seat"]
        if argv[:2] in SEAT_TARGETED:
            self.assertEqual(argv[2], seat, "the handover helper of %s acted for another: %s" % (seat, argv))
        if argv[:2] == ["agent", "rename"]:
            self.assertEqual(argv[3:4], [seat], "the handover helper of %s renamed another: %s" % (seat, argv))
        if argv[:2] == ["pane", "rename"]:
            self.assertFalse(info.get("clear"), "the handover helper cleared a pane name: %s" % argv)
            self.assertTrue(info.get("seat"), "the handover helper renamed a pane with no seat: %s" % argv)
            self.assertEqual(info.get("label"), info.get("wanted"), "the handover helper misnamed a pane: %s" % argv)
        if argv[:2] == ["pane", "close"]:
            self.assertEqual(argv[2], helper["pane"], "the handover helper closed a pane not its own: %s" % argv)
        self.assertNotEqual(argv[:3], ["plugin", "pane", "open"], "the handover helper opened a pane: %s" % argv)

    def interrupt_keys(self, command: List[str], kind: str) -> Optional[List[str]]:
        """The keys `interrupt` may press for ``kind``: the fleet file's ``[interrupt]``
        entry, else the plugin config's, else the harness's own copy of the defaults (the
        tables are read here with the fake's TOML reader, not the plugin's)."""
        fleet = fake_herdr.interrupt_table(fleet_file(command, self.env, self.repo))
        folder = self.env.get("HERDR_PLUGIN_CONFIG_DIR") or self.world.get("config_dir")
        config = fake_herdr.interrupt_table(os.path.join(folder, "config.toml")) if folder else {}
        for table in (fleet, config, INTERRUPT_DEFAULTS):
            if kind in table:
                return list(table[kind])
        return None

    def assert_rename(self, command: List[str], argv: List[str], info: Dict) -> None:
        """Invariant 20: a `pane rename` names a pane hosting a seat of the fleet, to that
        seat's wanted name (as the fake judged both from the fleet file); `--clear` only
        from `down` (or the popup, whose `down` runs it) and only for a pane with no agent."""
        self.assertIn(command[0], RENAME_COMMANDS, "%s renamed a pane: %s" % (command[:1], argv))
        self.assertFalse(info.get("plugin"), "a plugin pane was renamed: %s" % argv)
        if info.get("clear"):
            self.assertIn(command[0], CLEAR_COMMANDS, "%s cleared a pane name: %s" % (command[:1], argv))
            self.assertFalse(info.get("has_agent"), "cleared the name of a pane hosting an agent: %s" % argv)
            return
        self.assertTrue(info.get("fleet_known"), "pane rename without a readable fleet file: %s" % argv)
        self.assertTrue(info.get("seat"), "renamed a pane that hosts no seat of the fleet: %s" % argv)
        self.assertEqual(info.get("label"), info.get("wanted"),
                         "renamed %s's pane to a name that is not its wanted name: %s" % (info.get("seat"), argv))


class StartsCheck:
    """Design 32.1 for one command's calls: a second ``agent start`` to a pane only right after
    a ``process-info`` read of it that showed a bare shell, with no successful ``agent get`` of
    it in between, and never a third. Within ``swap --fallback`` the budget is per (pane, kind),
    and the first start of each later kind also follows that read (37.2). Starts herdr refused as
    busy, or rejected as a usage error, started nothing and do not count."""

    def __init__(self, case: "FleetCase", command: List[str]):
        self.case = case
        self.per_kind = command[:1] == ["swap"] and "--fallback" in command
        self.count: Dict[Tuple[str, str], int] = {}
        self.earlier: Dict[str, int] = {}  # pane -> starts of earlier kinds in this fallback command
        self.shell: Dict[str, bool] = {}   # pane -> its last process-info read showed a bare shell

    def see(self, argv: List[str], info: Dict, code: int) -> None:
        if argv[:2] == ["pane", "process-info"] and code == 0 and info.get("pane"):
            self.shell[info["pane"]] = bool(info.get("shell"))
        elif argv[:2] == ["agent", "get"] and code == 0 and info.get("pane"):
            self.shell[info["pane"]] = False
        elif (argv[:2] == ["agent", "start"] and "--pane" in argv and "--help" not in argv and not info.get("busy")
              and not info.get("usage")):
            pane = argv[argv.index("--pane") + 1]
            kind = argv[argv.index("--kind") + 1] if self.per_kind else ""
            key = (pane, kind)
            earlier = self.earlier.get(pane, 0)
            self.earlier[pane] = earlier + 1
            self.count[key] = self.count.get(key, 0) + 1
            if self.count[key] > 1 or (self.per_kind and earlier):
                self.case.assertLessEqual(self.count[key], 2, "a third agent start for %s in %s: %s"
                                          % (kind or "the kind", pane, argv))
                self.case.assertTrue(self.shell.get(pane), "another agent start in %s without a process-info read "
                                     "showing its bare shell first: %s" % (pane, argv))
            self.shell[pane] = False


class KeysCheck:
    """Invariant 21 for one command's calls (design 23.7, 23.9)."""

    def __init__(self, case: "FleetCase", command: List[str]):
        self.case = case
        self.command = list(command)
        self.status: Dict[str, Optional[str]] = {}   # agent name -> state at its last `agent get`
        self.read: set = set()                       # targets this command read the screen of
        self.text: Dict[str, bool] = {}              # target -> its last `agent read` showed text in the input
        self.cleared: Dict[str, int] = {}            # interrupt: ctrl+u per target (design 23.11)
        self.enters: Dict[str, int] = {}
        self.pressed: Dict[str, Dict] = {}           # interrupt: name -> {kind, keys, failed}

    def seen(self, name: str, status: Optional[str]) -> None:
        self.status[name] = status

    def check(self, argv: List[str], info: Dict, previous: Optional[List[str]], previous_exit: int, code: int) -> None:
        case, command = self.case, self.command
        what = command[:1] and command[0]
        case.assertNotIn(what, NO_KEYS_COMMANDS, "%s sent keys: %s" % (command[:1], argv))
        case.assertNotIn("--dry-run", command, "a dry run sent keys: %s" % argv)
        sent = info.get("keys") or argv[3:]
        case.assertTrue(sent, "send-keys without a key: %s" % argv)
        for key in sent:
            case.assertIn(key, KEYS, "a key outside enter, esc, ctrl+c: %s" % argv)
        name = str(info.get("name") or info.get("target") or "")
        if what in ENTER_COMMANDS:
            case.assertEqual(sent, ["enter"], "%s pressed %s: %s" % (what, sent, argv))
            case.assertTrue(info.get("exit_dialog"), "enter without the highlighted exit confirmation on screen: %s"
                            % argv)
            case.assertEqual((previous or [])[:3], ["agent", "read", argv[2]],
                             "enter not right after an `agent read` of %s: %s" % (argv[2], argv))
            case.assertEqual(previous_exit, 0, "enter after a failed agent read of %s: %s" % (argv[2], argv))
            self.enters[name] = self.enters.get(name, 0) + 1
            case.assertEqual(self.enters[name], 1, "a second enter for %s in one command" % name)
            return
        case.assertEqual(what, "interrupt", "%s sent keys: %s" % (command[:1], argv))
        if "ctrl+u" in sent:  # design 23.11 and 39.2: never to a boxed kind
            case.assertNotIn(str(info.get("kind") or ""), fake_herdr.BOXED,
                             "ctrl+u reached a boxed kind: %s" % argv)
            case.assertEqual(sent, ["ctrl+u"], "ctrl+u with other keys: %s" % argv)
            case.assertTrue(self.text.get(argv[2]), "ctrl+u without an agent read showing text in %s's input"
                            % argv[2])
            self.cleared[name] = self.cleared.get(name, 0) + 1
            case.assertEqual(self.cleared[name], 1, "a second ctrl+u for %s in one command" % name)
            return
        record = self.pressed.setdefault(name, {"kind": info.get("kind"), "keys": [], "failed": False})
        if not record["keys"]:
            case.assertEqual(self.status.get(name), "working",
                             "interrupt pressed keys in %s, which was %s at its last agent get"
                             % (name, self.status.get(name)))
            case.assertIn(argv[2], self.read, "interrupt pressed keys in %s without reading its screen" % name)
            case.assertEqual((previous or [])[:3], ["agent", "read", argv[2]],
                             "interrupt keys not right after an agent read of %s: %s" % (argv[2], argv))
            case.assertEqual(previous_exit, 0, "interrupt keys after a failed agent read of %s: %s" % (argv[2], argv))
        if code == 0:
            record["keys"].extend(sent)
        else:
            record["failed"] = True

    def finish(self) -> None:
        for name, record in self.pressed.items():
            expected = self.case.interrupt_keys(self.command, str(record["kind"]))
            self.case.assertIsNotNone(expected, "interrupt pressed keys for kind %s, which has none" % record["kind"])
            if record["failed"]:
                self.case.assertEqual(record["keys"], expected[:len(record["keys"])], name)
            else:
                self.case.assertEqual(record["keys"], expected, "interrupt pressed %s in %s, not the kind's %s"
                                      % (record["keys"], name, expected))
            if record["kind"] == "claude":
                self.case.assertLessEqual(record["keys"].count("ctrl+c"), 1, "two ctrl+c to a Claude seat")
        self.pressed = {}


def fleet_file(argv, env: Dict[str, str], repo: str) -> str:
    """The fleet file a command uses, for the fake's `pane rename` record: `--fleet PATH`
    (or `--file PATH` where it is the alias), else HERDR_FLEET_FILE, else the repo's."""
    argv = list(argv)
    options = ("--fleet",) if argv[:1] in (["send"], ["state"]) else ("--fleet", "--file")
    for index, item in enumerate(argv[:-1]):
        if item in options:
            return argv[index + 1]
    return env.get("HERDR_FLEET_FILE") or os.path.join(repo, ".herdr", "fleet.toml")


def typed_ahead_allowed(command: List[str], argv: List[str], info: Dict) -> bool:  # noqa: ARG001
    """May this prompt reach a working agent? Only from `send --now` and `deliver NAME
    --now` (design 21.1). Not a plain `send`, the flush before it, `deliver`, `watch`
    or a kickoff, whatever the mailbox holds."""
    return command[:1] in (["send"], ["deliver"]) and "--now" in command


ARGS = ["--model", "claude-opus-5-5"]


class LiveFleet(FleetCase):
    """A fleet already up in workspace w2: orchestrator p1, dev p2, reviewer p3."""

    def setUp(self):
        super().setUp()
        self.fleet_ws()
        fake_herdr.put_agent(self.world, "w2:p1", "claude", "sample-orchestrator", args=ARGS)
        for pane, kind, name, args in (("w2:p2", "claude", "sample-dev", ARGS),
                                       ("w2:p3", "codex", "sample-reviewer", ["-m", "gpt-6-astra"])):
            fake_herdr.add_pane(self.world, pane, "w2", self.repo, created=True)
            fake_herdr.put_agent(self.world, pane, kind, name, args=args)
        self.write_state({"version": 1, "workspace_id": "w2", "workspace_label": "sample",
                          "created_workspace": False, "agents": {
                              "sample-orchestrator": {"pane_id": "w2:p1", "created_pane": False,
                                                      "kind": "claude", "args": ARGS},
                              "sample-dev": {"pane_id": "w2:p2", "created_pane": True, "kind": "claude", "args": ARGS},
                              "sample-reviewer": {"pane_id": "w2:p3", "created_pane": True, "kind": "codex",
                                                  "args": ["-m", "gpt-6-astra"]}}})

    def status_of(self, pane, status):
        self.world["panes"][pane]["agent"]["agent_status"] = status


FOUR_SEATS = THREE_SEATS + '''
[[agents]]
name = "sample-designer"
kind = "claude"
args = ["--model", "claude-opus-5-5"]
'''
GRID = '[layout]\nmode = "grid"\n'
TOL = 1e-3


def grid_fleet(text=FOUR_SEATS, layout=GRID):
    """Insert a [layout] table before the first [[agents]]."""
    head, rest = text.split("[[agents]]", 1)
    return head + layout + "\n[[agents]]" + rest


class GridCase(FleetCase):
    def rects(self, area):
        """Rebuild each pane's rectangle from the splits the fake herdr received, as shares
        of ``area``. The watcher strip (a plugin pane, design 19) is left out: cells are
        shares of the grid area above it."""
        rects = {area: (0.0, 0.0, 1.0, 1.0)}
        for pid, pane in self.world["panes"].items():  # creation order
            split = pane.get("split")
            if not split or split["from"] not in rects or pane.get("plugin"):
                continue
            x, y, w, h = rects[split["from"]]
            r = float(split["ratio"])
            if split["direction"] == "right":
                rects[split["from"]], rects[pid] = (x, y, w * r, h), (x + w * r, y, w * (1 - r), h)
            else:
                rects[split["from"]], rects[pid] = (x, y, w, h * r), (x, y + h * r, w, h * (1 - r))
        return rects

    def assert_cell(self, rect, x, y, w, h):
        for got, want in zip(rect, (x, y, w, h)):
            self.assertAlmostEqual(got, want, delta=TOL)


GIT = shutil.which("git")
READ_ONLY = {"rev-parse", "symbolic-ref", "worktree list", "log", "status", "merge-base", "ls-files", "cat-file"}


class GitCase(FleetCase):
    """A FleetCase whose repo is a real git repository on branch main with the fleet file
    committed (design 17.6). Git runs with HOME set to the test's temp dir and no system
    config. Every git call the plugin makes goes through a logging shim on PATH, and
    tearDown asserts invariant 8 on it: only read-only subcommands, no fetch, no prune.
    Subclasses need ``@unittest.skipUnless(GIT, ...)``."""

    def setUp(self):
        super().setUp()
        self.real = os.path.realpath(self.tmp)  # git prints real paths (/private/var/... on macOS)
        self.git_env = {"PATH": os.environ.get("PATH", ""), "HOME": self.tmp, "GIT_CONFIG_NOSYSTEM": "1",
                        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com", "LANG": "C"}
        self.git("init", "-q")
        self.git("symbolic-ref", "HEAD", "refs/heads/main")
        self.write_file("README.md", "hello\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "first")
        self.shim_log = os.path.join(self.tmp, "git-calls.log")
        shim_dir = os.path.join(self.tmp, "shim")
        os.makedirs(shim_dir)
        with open(os.path.join(shim_dir, "git"), "w") as handle:
            handle.write('#!/bin/sh\nprintf "%%s\\n" "$*" >> "%s"\nexec "%s" "$@"\n' % (self.shim_log, GIT))
        os.chmod(os.path.join(shim_dir, "git"), 0o755)
        self.env["PATH"] = shim_dir + os.pathsep + self.env["PATH"]

    def tearDown(self):
        try:
            self.assert_git_read_only()
        finally:
            super().tearDown()

    def git(self, *args, cwd=None):
        proc = subprocess.run([GIT, "-C", cwd or self.repo] + list(args), env=self.git_env, check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        return proc.stdout.strip()

    def commit(self, message, cwd=None, date=None):
        env = dict(self.git_env)
        if date:
            env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
        subprocess.run([GIT, "-C", cwd or self.repo, "commit", "-q", "--allow-empty", "-m", message], env=env,
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return self.git("rev-parse", "HEAD", cwd=cwd)

    def set_origin(self, sha, head=True, name="main"):
        self.git("update-ref", "refs/remotes/origin/%s" % name, sha)
        if head:
            self.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/%s" % name)

    def plugin_git_calls(self):
        if not os.path.exists(self.shim_log):
            return []
        with open(self.shim_log) as handle:
            return [line.split() for line in handle if line.strip()]

    def assert_git_read_only(self):
        for argv in self.plugin_git_calls():
            self.assertEqual(argv[0], "-C", argv)
            sub = argv[2]
            if sub == "worktree":
                sub = "worktree " + argv[3]
            self.assertIn(sub, READ_ONLY, "the plugin ran git %s" % " ".join(argv))
            self.assertNotIn("fetch", argv)
            self.assertNotIn("prune", argv)
