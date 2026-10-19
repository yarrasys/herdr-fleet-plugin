"""End to end: run ``bin/herdr-fleet`` as a real process against the fake
herdr installed as an executable on HERDR_BIN_PATH, exactly as herdr would."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from support import ROOT, THREE_SEATS, install_fake_herdr, read_log

BIN = os.path.join(ROOT, "bin", "herdr-fleet")
EXAMPLE = os.path.join(ROOT, "examples", "fablyn")
READ_ONLY = (["--version"], ["agent", "list"], ["agent"], ["agent", "get"], ["pane", "get"], ["pane", "list"],
             ["pane", "process-info"], ["workspace", "list"], ["workspace", "get"], ["integration", "status"],
             ["plugin", "config-dir"])


class Subprocess(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="herdr-fleet-e2e-"))
        self.env = install_fake_herdr(self.tmp, self.tmp)
        self.world = self.env["FAKE_HERDR_WORLD"]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_bin(self, *args, cwd=None, env=None):
        proc = subprocess.run([sys.executable, BIN] + list(args), cwd=cwd or self.tmp, env=env or self.env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                              stdin=subprocess.DEVNULL, timeout=60)
        self.assertNotIn("Traceback", proc.stderr)
        return proc

    def calls(self):
        return read_log(self.env)

    def test_help(self):
        proc = self.run_bin("--help")
        self.assertEqual(proc.returncode, 0)
        for command in ("up", "status", "restart", "swap", "kickoff", "down", "doctor", "install-cli"):
            self.assertIn(command, proc.stdout)
        self.assertEqual(self.calls(), [])

    def test_doctor_on_the_example(self):
        proc = self.run_bin("doctor", cwd=os.path.join(EXAMPLE, ".herdr", "kickoff"))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("ok    fleet file: %s" % os.path.join(EXAMPLE, ".herdr", "fleet.toml"), proc.stdout)
        self.assertIn("ok    kickoff fablyn-reviewer", proc.stdout)

    def test_up_dry_run_on_the_example_changes_nothing(self):
        proc = self.run_bin("up", "--dry-run", "--file", os.path.join(EXAMPLE, ".herdr", "fleet.toml"))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("herdr agent start fablyn-reviewer --kind codex", proc.stdout)
        self.assertIn("--env FABLYN_ROLE=orchestrator", proc.stdout)
        for argv in self.calls():
            self.assertIn(argv[:2] if argv[:2] != ["agent"] else ["agent"], [list(r) for r in READ_ONLY], argv)
        self.assertFalse(os.path.exists(os.path.join(EXAMPLE, ".herdr", "fleet.state.json")))

    def test_up_then_status_json(self):
        repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(repo, ".herdr"))
        with open(os.path.join(repo, ".herdr", "fleet.toml"), "w") as handle:
            handle.write(THREE_SEATS)
        proc = self.run_bin("up", cwd=repo)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        proc = self.run_bin("status", "--json", cwd=repo)
        data = json.loads(proc.stdout)
        self.assertEqual([s["state"] for s in data["seats"]], ["idle", "idle", "idle"])
        for argv in self.calls():
            self.assertNotIn(argv[:2], (["pane", "send-keys"], ["pane", "send-text"], ["agent", "send-keys"],
                                        ["pane", "run"]))

    def test_herdr_errors_surface_code_and_message(self):
        with open(self.world) as handle:
            world = json.load(handle)
        world["canned"] = [{"prefix": ["agent", "list"], "exit": 1,
                            "stderr": {"error": {"code": "server_unavailable", "message": "no server"}}}]
        with open(self.world, "w") as handle:
            json.dump(world, handle)
        proc = self.run_bin("status", cwd=EXAMPLE)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stderr.strip(), "herdr-fleet: error: herdr error server_unavailable: no server")

    def test_missing_herdr_binary(self):
        env = dict(self.env, HERDR_BIN_PATH=os.path.join(self.tmp, "nope"))
        proc = self.run_bin("status", cwd=EXAMPLE, env=env)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("herdr error herdr_not_found", proc.stderr)

    def test_invalid_fleet_file_exits_2(self):
        bad = os.path.join(self.tmp, "bad.toml")
        with open(bad, "w") as handle:
            handle.write('[[agents]]\nname = "X"\nkind = "claude"\n')
        proc = self.run_bin("up", "--file", bad)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("must match [a-z][a-z0-9_-]{0,31}", proc.stderr)

    def test_usage_error_exits_2(self):
        self.assertEqual(self.run_bin("restart").returncode, 2)
        self.assertEqual(self.run_bin("frobnicate").returncode, 2)


if __name__ == "__main__":
    unittest.main()
