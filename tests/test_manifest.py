"""The plugin manifest (design section 8, amended in section 12).

Checks the manifest's contents and runs its commands the way herdr does:
actions from the plugin root, pane entrypoints through ``sh -c`` from the
``--cwd`` that ``ui`` passes (the repo root). Pane commands use
``$HERDR_PLUGIN_ROOT`` because a pane opened with ``--cwd`` does not run in the
plugin root, so the relative ``bin/herdr-fleet`` of the first build failed.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import unittest

from support import ROOT, THREE_SEATS, install_fake_herdr, read_log
from herdr_fleet import MIN_HERDR_VERSION, PLUGIN_ID, __version__
from herdr_fleet.cli import build_parser
from herdr_fleet.fleetfile import load_toml

MANIFEST = os.path.join(ROOT, "herdr-plugin.toml")
PANE_PREFIX = 'exec python3 "$HERDR_PLUGIN_ROOT/bin/herdr-fleet" '


class ManifestContents(unittest.TestCase):
    def setUp(self):
        self.manifest = load_toml(MANIFEST)

    def test_metadata(self):
        m = self.manifest
        self.assertEqual(m["id"], PLUGIN_ID)
        self.assertEqual(m["version"], __version__)
        self.assertEqual(m["min_herdr_version"], ".".join(str(v) for v in MIN_HERDR_VERSION))
        self.assertEqual(m["platforms"], ["linux", "macos"])

    def test_actions_run_the_cli_from_the_plugin_root(self):
        actions = {a["id"]: a for a in self.manifest["actions"]}
        self.assertEqual(actions["up"]["command"], ["python3", "bin/herdr-fleet", "up", "--notify"])
        self.assertEqual(actions["status"]["command"], ["python3", "bin/herdr-fleet", "ui", "status"])
        self.assertEqual(actions["manage"]["command"], ["python3", "bin/herdr-fleet", "ui", "manage"])
        self.assertEqual(actions["doctor"]["command"], ["python3", "bin/herdr-fleet", "doctor", "--notify"])
        self.assertEqual(actions["doctor"]["contexts"], ["global"])
        for action in actions.values():
            build_parser().parse_args(action["command"][2:])  # still valid CLI arguments

    def test_panes_run_through_sh_with_the_plugin_root(self):
        panes = {p["id"]: p for p in self.manifest["panes"]}
        self.assertEqual(panes["status"]["placement"], "overlay")
        self.assertEqual(panes["status"]["command"], ["sh", "-c", PANE_PREFIX + "status --hold"])
        self.assertEqual(panes["manage"]["placement"], "popup")
        self.assertEqual((panes["manage"]["width"], panes["manage"]["height"]), ("70%", "60%"))
        self.assertEqual(panes["manage"]["command"], ["sh", "-c", PANE_PREFIX + "menu"])
        for pane in panes.values():
            args = shlex.split(pane["command"][2])[3:]
            build_parser().parse_args(args)
        self.assertEqual(set(panes), {a["command"][3] for a in self.manifest["actions"] if a["command"][2] == "ui"})


class ManifestCommandsRun(unittest.TestCase):
    """Run each manifest command as herdr would, against the fake herdr."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="herdr-fleet-manifest-"))
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.repo, ".herdr"))
        with open(os.path.join(self.repo, ".herdr", "fleet.toml"), "w") as handle:
            handle.write(THREE_SEATS)
        self.env = install_fake_herdr(self.tmp, self.repo)
        self.env.update(HERDR_PLUGIN_ID=PLUGIN_ID, HERDR_PLUGIN_ROOT=ROOT)
        self.manifest = load_toml(MANIFEST)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cmd(self, argv, cwd, stdin="", **env):
        proc = subprocess.run(argv, cwd=cwd, env=dict(self.env, **env), input=stdin, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, universal_newlines=True, timeout=60)
        self.assertNotIn("Traceback", proc.stderr)
        return proc

    def pane_command(self, pane_id):
        return next(p["command"] for p in self.manifest["panes"] if p["id"] == pane_id)

    def action_command(self, action_id):
        return next(a["command"] for a in self.manifest["actions"] if a["id"] == action_id)

    def test_status_pane_runs_from_the_repo_cwd(self):
        # `ui status` opens the pane with --cwd <repo root>; herdr runs the argv there.
        proc = self.run_cmd(self.pane_command("status"), self.repo, stdin="\n", HERDR_PLUGIN_ENTRYPOINT_ID="status")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("fablyn-orchestrator", proc.stdout)
        self.assertIn("Press Enter to close.", proc.stdout)

    def test_manage_pane_runs_from_the_repo_cwd(self):
        proc = self.run_cmd(self.pane_command("manage"), self.repo, stdin="q\n", HERDR_PLUGIN_ENTRYPOINT_ID="manage")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("1) fablyn-orchestrator", proc.stdout)

    def test_relative_bin_path_does_not_work_from_the_repo_cwd(self):
        # Why the pane commands go through sh -c and $HERDR_PLUGIN_ROOT.
        proc = subprocess.run(["python3", "bin/herdr-fleet", "status", "--hold"], cwd=self.repo, env=self.env,
                              input="\n", stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertNotEqual(proc.returncode, 0)

    def test_ui_action_opens_the_pane_at_the_focused_panes_repo(self):
        context = {"workspace_id": "w1", "focused_pane_id": "w1:p1", "focused_pane_cwd": self.repo}
        proc = self.run_cmd(self.action_command("status"), ROOT, HERDR_PLUGIN_ACTION_ID="yarrasys.fleet.status",
                            HERDR_PLUGIN_CONTEXT_JSON=json.dumps(context))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(["plugin", "pane", "open", "--plugin", PLUGIN_ID, "--entrypoint", "status", "--cwd", self.repo],
                      read_log(self.env))

    def test_doctor_action_from_the_plugin_root(self):
        context = {"workspace_id": "w1", "focused_pane_id": "w1:p1", "focused_pane_cwd": self.repo}
        proc = self.run_cmd(self.action_command("doctor"), ROOT, HERDR_PLUGIN_ACTION_ID="yarrasys.fleet.doctor",
                            HERDR_PLUGIN_CONTEXT_JSON=json.dumps(context))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("ok    fleet file: %s" % os.path.join(self.repo, ".herdr", "fleet.toml"), proc.stdout)
        notes = [a for a in read_log(self.env) if a[:2] == ["notification", "show"]]
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0][2], "Fleet doctor: all checks ok")


if __name__ == "__main__":
    unittest.main()
