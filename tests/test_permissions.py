"""Permission preflight in doctor (design 16.6) and status CWD/BRANCH (16.7)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest

from support import FleetCase, LiveFleet
from herdr_fleet.permissions import COMMANDS, covers, missing, suggestion
from herdr_fleet.checks import shorten

SUGGESTION = ('add to .claude/settings.json permissions.allow: "Bash(herdr agent prompt:*)", '
              '"Bash(herdr agent get:*)", "Bash(herdr agent wait:*)", "Bash(herdr agent read:*)", '
              '"Bash(herdr-fleet:*)"')


class Coverage(unittest.TestCase):
    def test_rules(self):
        self.assertTrue(covers("Bash(herdr agent prompt:*)", "herdr agent prompt"))
        self.assertTrue(covers("Bash(herdr agent prompt *)", "herdr agent prompt"))
        self.assertTrue(covers("Bash(herdr agent:*)", "herdr agent wait"))
        self.assertTrue(covers("Bash(herdr:*)", "herdr agent read"))
        self.assertFalse(covers("Bash(herdr:*)", "herdr-fleet"))
        self.assertTrue(covers("Bash(herdr-fleet:*)", "herdr-fleet"))
        self.assertTrue(covers("Bash", "herdr-fleet"))
        self.assertTrue(covers("Bash(*)", "herdr agent get"))
        self.assertFalse(covers("Bash(herdr agent get)", "herdr agent get"))  # exact, no arguments
        self.assertFalse(covers("Bash(herdr agent promptx:*)", "herdr agent prompt"))
        self.assertFalse(covers("Read(~/**)", "herdr agent get"))
        self.assertEqual(missing(["Bash(herdr agent:*)"]), ["herdr-fleet"])
        self.assertEqual(suggestion(COMMANDS), SUGGESTION)


class Doctor(FleetCase):
    """THREE_SEATS: two claude seats and one codex seat. HOME is the test's temp dir."""

    def lines(self, out, prefix):
        return [l for l in out.splitlines() if prefix in l]

    def test_no_settings_warns_with_the_exact_entries(self):
        code, out, _ = self.run_cli("doctor")
        self.assertEqual(code, 0, out)  # advisory: never fails
        self.assertEqual(self.lines(out, "permissions fablyn-dev"), ["warn  permissions fablyn-dev: " + SUGGESTION])
        self.assertEqual(self.lines(out, "permissions fablyn-reviewer"),
                         ["skip  permissions fablyn-reviewer: not checked (codex)"])

    def test_entries_from_all_three_files_count(self):
        self.write_file(".claude/settings.json", json.dumps({"permissions": {"allow": ["Bash(herdr agent:*)"]}}))
        self.write_file(".claude/settings.local.json", json.dumps({"permissions": {"allow": []}}))
        os.makedirs(os.path.join(self.tmp, ".claude"))
        with open(os.path.join(self.tmp, ".claude", "settings.json"), "w") as handle:  # ~/.claude (HOME = tmp)
            json.dump({"permissions": {"allow": ["Bash(herdr-fleet *)"]}}, handle)
        code, out, _ = self.run_cli("doctor")
        self.assertEqual(code, 0)
        self.assertIn("ok    permissions fablyn-orchestrator: herdr agent prompt/get/wait/read and herdr-fleet are "
                      "allowed", out)

    def test_partial_coverage_names_only_what_is_missing(self):
        self.write_file(".claude/settings.local.json", json.dumps({"permissions": {"allow": ["Bash(herdr agent:*)"]}}))
        out = self.run_cli("doctor")[1]
        self.assertIn('warn  permissions fablyn-dev: add to .claude/settings.json permissions.allow: '
                      '"Bash(herdr-fleet:*)"', out)

    def test_unreadable_settings_warn(self):
        self.write_file(".claude/settings.json", "{broken")
        code, out, _ = self.run_cli("doctor")
        self.assertEqual(code, 0)
        self.assertIn("warn  permissions: cannot read %s" % os.path.join(self.repo, ".claude", "settings.json"), out)


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class StatusColumns(LiveFleet):
    def test_cwd_and_branch(self):
        work = os.path.join(self.tmp, "work", "dev-tree")
        os.makedirs(work)
        git = ["git", "-C", work, "-c", "user.name=t", "-c", "user.email=t@example.com"]
        steps = (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "init"],
                 ["checkout", "-q", "-b", "dev/export"])
        for args in steps:
            subprocess.run(git + args, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.world["panes"]["w2:p2"].update(cwd=work)  # fablyn-dev runs in its worktree
        data = json.loads(self.run_cli("status", "--json")[1])
        dev = data["seats"][1]
        self.assertEqual((dev["cwd"], dev["branch"]), (work, "dev/export"))
        self.assertEqual(data["seats"][0]["branch"], "")  # the repo dir is not a git repository
        out = self.run_cli("status")[1]
        self.assertIn("CWD", out)
        self.assertIn("BRANCH", out)
        line = [l for l in out.splitlines() if l.startswith("fablyn-dev ")][0]
        self.assertIn("~/work/dev-tree", line)  # HOME is the test's temp dir
        self.assertIn("dev/export", line)

    def test_shorten(self):
        self.assertEqual(shorten("/home/me/x", "/home/me"), "~/x")
        self.assertEqual(shorten("/home/me", "/home/me/"), "~")
        self.assertEqual(shorten("/home/meow", "/home/me"), "/home/meow")


if __name__ == "__main__":
    unittest.main()
