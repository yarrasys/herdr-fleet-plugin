"""Fleet file discovery and validation (design section 2)."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

import support  # noqa: F401  (puts the plugin root on sys.path)
from herdr_fleet.fleetfile import FleetError, check_kinds, find_fleet_file, load_fleet, repo_root_for


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="fleetfile-"))
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.repo, ".herdr"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, text, rel=".herdr/fleet.toml"):
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            handle.write(text)
        return path

    def errors(self, text):
        with self.assertRaises(FleetError) as caught:
            load_fleet(self.write(text))
        return "\n".join(caught.exception.errors)


class Discovery(Base):
    def test_walks_up_to_the_repo_root(self):
        path = self.write('[[agents]]\nname = "a"\nkind = "claude"\n')
        deep = os.path.join(self.repo, "src", "pkg")
        os.makedirs(deep)
        self.assertEqual(find_fleet_file(deep, env={}), path)
        self.assertEqual(repo_root_for(path), self.repo)

    def test_not_found(self):
        with self.assertRaises(FleetError) as caught:
            find_fleet_file(self.tmp, env={})
        self.assertIn("no .herdr/fleet.toml found", str(caught.exception))

    def test_explicit_file_and_env_override(self):
        other = self.write('[[agents]]\nname = "a"\nkind = "claude"\n', "teams/one.toml")
        self.assertEqual(find_fleet_file(self.tmp, "repo/teams/one.toml", env={}), other)
        self.assertEqual(find_fleet_file(self.tmp, env={"HERDR_FLEET_FILE": other}), other)
        self.assertEqual(repo_root_for(other), os.path.join(self.repo, "teams"))
        with self.assertRaises(FleetError):
            find_fleet_file(self.tmp, "missing.toml", env={})


class Parsing(Base):
    def test_example_fields_and_defaults(self):
        self.write("x", "docs/kickoff/o.md")
        fleet = load_fleet(self.write("""
workspace = "fablyn"
state_note = "docs/STATE.md"
[layout]
direction = "down"
ratio = 0.4
[[agents]]
name = "fablyn-orchestrator"
kind = "claude"
args = ["--model", "m"]
cwd = "~/elsewhere"
split = "right"
env = { ROLE = "o" }
kickoff = "docs/kickoff/o.md"
quit = "/exit"
notes = "n"
[[agents]]
name = "fablyn-dev"
kind = "codex"
cwd = "/abs/path"
"""))
        first, second = fleet.seats
        self.assertEqual(fleet.workspace, "fablyn")
        self.assertEqual(fleet.repo, self.repo)
        self.assertEqual(fleet.cwd, self.repo)
        self.assertEqual(fleet.state_note, os.path.join(self.repo, "docs", "STATE.md"))
        self.assertEqual(first.cwd, os.path.expanduser("~/elsewhere"))
        self.assertEqual(first.split, "right")
        self.assertEqual(first.ratio, 0.4)  # from [layout]
        self.assertEqual(first.env, {"ROLE": "o"})
        self.assertEqual(first.kickoff, os.path.join(self.repo, "docs", "kickoff", "o.md"))
        self.assertEqual(second.split, "down")
        self.assertEqual(second.args, [])
        self.assertEqual(second.cwd, "/abs/path")
        self.assertEqual(fleet.kinds(), ["claude", "codex"])
        self.assertTrue(any("does not exist" in w for w in fleet.warnings))  # cwd warnings

    def test_all_errors_are_reported_together(self):
        text = self.errors("""
[layout]
direction = "left"
[[agents]]
name = "Bad Name"
kind = "claude"
[[agents]]
name = "dup"
kind = "claude"
args = "--model x"
ratio = 1
split = "up"
kickoff = "missing.md"
[[agents]]
name = "dup"
kind = "codex"
kickoff = "missing.md"
kickoff_text = "hi"
[[agents]]
name = "nokind"
""")
        for expected in ("layout.direction", "must match [a-z][a-z0-9_-]{0,31}", "args: must be a list of strings",
                         "ratio: 1 must be a number between 0 and 1", "split: 'up' must be right or down",
                         "kickoff file does not exist", "duplicate name 'dup'", "set kickoff or kickoff_text, not both",
                         "kind: required string"):
            self.assertIn(expected, text)

    def test_no_agents_and_bad_toml(self):
        self.assertIn("declares no [[agents]]", self.errors('workspace = "x"\n'))
        self.assertIn("fleet.toml", self.errors("workspace = \n"))

    def test_ratio_zero_and_bool_rejected(self):
        self.assertIn("between 0 and 1", self.errors('[[agents]]\nname = "a"\nkind = "claude"\nratio = 0\n'))
        self.assertIn("between 0 and 1", self.errors('[[agents]]\nname = "a"\nkind = "claude"\nratio = true\n'))

    def test_unknown_keys_warn(self):
        fleet = load_fleet(self.write('kickof = "x"\n[[agents]]\nname = "a"\nkind = "claude"\nmodel = "m"\n'))
        self.assertIn("top level: unknown key 'kickof' ignored", fleet.warnings)
        self.assertTrue(any("unknown key 'model'" in w for w in fleet.warnings))

    def test_name_length_limit(self):
        ok = "a" * 32
        load_fleet(self.write('[[agents]]\nname = "%s"\nkind = "claude"\n' % ok))
        self.assertIn("must match", self.errors('[[agents]]\nname = "%s"\nkind = "claude"\n' % (ok + "a")))

    def test_check_kinds(self):
        fleet = load_fleet(self.write('[[agents]]\nname = "a"\nkind = "claude"\n[[agents]]\nname = "b"\nkind = "zz"\n'))
        self.assertEqual(check_kinds(fleet, None), [])
        errors = check_kinds(fleet, ["claude", "codex"])
        self.assertEqual(len(errors), 1)
        self.assertIn("'zz' is not a kind", errors[0])


if __name__ == "__main__":
    unittest.main()
