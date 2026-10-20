"""Shared test fixtures: a temp repo with a fleet file, a fake herdr world,
a fake clock, and ``FleetCase.run_cli`` which calls ``herdr_fleet.cli.main``
in-process with the fake herdr as its runner.

Every test's call log is checked in ``tearDown`` against the safety
invariants of design section 7, so each scenario doubles as a safety test.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from typing import Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import fake_herdr  # noqa: E402
from herdr_fleet.cli import main  # noqa: E402

CALLER_WS = "w1"
CALLER_PANE = "w1:p1"

THREE_SEATS = """
workspace = "fablyn"

[[agents]]
name = "fablyn-orchestrator"
kind = "claude"
args = ["--model", "claude-opus-5-5"]

[[agents]]
name = "fablyn-dev"
kind = "claude"
args = ["--model", "claude-opus-5-5"]

[[agents]]
name = "fablyn-reviewer"
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
    world = fake_herdr.new_world()
    fake_herdr.add_workspace(world, CALLER_WS, "caller", cwd)
    fake_herdr.put_agent(world, CALLER_PANE, "claude", name="caller", status="working")
    with open(os.path.join(directory, "world.json"), "w") as handle:
        json.dump(world, handle)
    return {"PATH": bindir + os.pathsep + os.environ.get("PATH", ""), "HOME": directory,  # never the real ~/.claude
            "HERDR_ENV": "1", "HERDR_BIN_PATH": herdr, "HERDR_PANE_ID": CALLER_PANE,
            "HERDR_WORKSPACE_ID": CALLER_WS, "FAKE_HERDR_WORLD": os.path.join(directory, "world.json"),
            "FAKE_HERDR_LOG": os.path.join(directory, "calls.jsonl")}


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

    def fleet_ws(self, ws: str = "w2", label: str = "fablyn") -> str:
        """Add an existing workspace for the fleet; returns its root pane."""
        return fake_herdr.add_workspace(self.world, ws, label, self.repo)

    # -- running ------------------------------------------------------------------

    def run_cli(self, *argv: str, stdin: str = "", env: Optional[Dict[str, str]] = None) -> Tuple[int, str, str]:
        with open(self.world_path, "w", encoding="utf-8") as handle:
            json.dump(self.world, handle)
        out, err = io.StringIO(), io.StringIO()
        env = env if env is not None else self.env
        with open(self.log_path, "a", encoding="utf-8") as handle:  # marks which command made the calls below
            handle.write(json.dumps({"command": list(argv)}) + "\n")
        runner = fake_herdr.Fake(self.world_path, self.log_path, env.get("HERDR_PANE_ID"))
        code = main(list(argv), env=env, runner=runner, clock=self.clock,
                    stdout=out, stderr=err, stdin=io.StringIO(stdin), cwd=self.repo)
        with open(self.world_path, "r", encoding="utf-8") as handle:
            self.world = json.load(handle)
        return code, out.getvalue(), err.getvalue()

    # -- log inspection -----------------------------------------------------------

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

    def assert_safety(self) -> None:
        """Design section 7 invariants, checked against the full call log. The one
        exception (design 16.1): a prompt issued by `send` may reach a working agent."""
        caller = self.env.get("HERDR_PANE_ID")
        command: List[str] = []
        entries = []
        if os.path.exists(self.log_path):
            with open(self.log_path, "r", encoding="utf-8") as handle:
                entries = [json.loads(line) for line in handle if line.strip()]
        for call in entries:
            if "command" in call:
                command = call["command"]
                continue
            argv, info = call["argv"], call["info"]
            self.assertTrue(argv, "bare herdr was run")
            self.assertNotIn(tuple(argv[:2]), fake_herdr.FORBIDDEN, "forbidden command: %s" % argv)
            self.assertFalse(any(a in ("send-keys", "send-text") for a in argv[:2]), argv)
            self.assertNotEqual(argv[:2], ["pane", "run"], argv)
            if argv[:2] == ["agent", "prompt"]:
                allowed = ("idle", "done", None) + (("working",) if command[:1] == ["send"] else ())
                self.assertIn(info.get("target_status"), allowed,
                              "%s prompted an agent in state %s: %s" % (command[:1], info.get("target_status"), argv))
            if argv[:2] == ["agent", "start"] and "--help" not in argv:
                pane = argv[argv.index("--pane") + 1]
                self.assertNotEqual(pane, caller, "started into the caller's pane")
                if call["exit"] == 0 or "at_shell" in info:
                    self.assertTrue(info.get("at_shell"), "agent start into a pane not at a shell: %s" % argv)
            if argv[:2] == ["pane", "close"] and call["exit"] == 0:
                self.assertTrue(info.get("created"), "closed a pane herdr-fleet did not create: %s" % argv)
                self.assertFalse(info.get("had_agent"), "closed a pane hosting an agent: %s" % argv)
            if argv[:2] == ["workspace", "close"] and call["exit"] == 0:
                self.assertTrue(info.get("created"), "closed a workspace herdr-fleet did not create")


ARGS = ["--model", "claude-opus-5-5"]


class LiveFleet(FleetCase):
    """A fleet already up in workspace w2: orchestrator p1, dev p2, reviewer p3."""

    def setUp(self):
        super().setUp()
        self.fleet_ws()
        fake_herdr.put_agent(self.world, "w2:p1", "claude", "fablyn-orchestrator", args=ARGS)
        for pane, kind, name, args in (("w2:p2", "claude", "fablyn-dev", ARGS),
                                       ("w2:p3", "codex", "fablyn-reviewer", ["-m", "gpt-6-astra"])):
            fake_herdr.add_pane(self.world, pane, "w2", self.repo, created=True)
            fake_herdr.put_agent(self.world, pane, kind, name, args=args)
        self.write_state({"version": 1, "workspace_id": "w2", "workspace_label": "fablyn",
                          "created_workspace": False, "agents": {
                              "fablyn-orchestrator": {"pane_id": "w2:p1", "created_pane": False,
                                                      "kind": "claude", "args": ARGS},
                              "fablyn-dev": {"pane_id": "w2:p2", "created_pane": True, "kind": "claude", "args": ARGS},
                              "fablyn-reviewer": {"pane_id": "w2:p3", "created_pane": True, "kind": "codex",
                                                  "args": ["-m", "gpt-6-astra"]}}})

    def status_of(self, pane, status):
        self.world["panes"][pane]["agent"]["agent_status"] = status


FOUR_SEATS = THREE_SEATS + '''
[[agents]]
name = "fablyn-designer"
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
        """Rebuild each pane's rectangle from the splits the fake herdr received."""
        rects = {area: (0.0, 0.0, 1.0, 1.0)}
        for pid, pane in self.world["panes"].items():  # creation order
            split = pane.get("split")
            if not split or split["from"] not in rects:
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
