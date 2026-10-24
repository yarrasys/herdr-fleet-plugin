"""``herdr-fleet init`` (design section 13): starter files, Git root detection,
never overwriting, --force, --example, odd directory names, the manifest
action's focused_pane_cwd, no herdr commands, and the pointer to init when
discovery finds no fleet file."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest

import fake_herdr
from support import CALLER_PANE, CALLER_WS, FleetCase
from herdr_fleet import __version__
from herdr_fleet.cli import main
from herdr_fleet.fleetfile import NAME_RE, load_fleet
from herdr_fleet.kickoff import PLACEHOLDERS, compose, roster_table
from herdr_fleet.scaffold import EXAMPLES_DIR, SKIP_NAMES, seat_name

GIT = shutil.which("git")
STARTER = [".herdr/fleet.toml", ".herdr/kickoff/default.md", ".herdr/STATE.md", ".herdr/.gitignore"]
HINT = "run `herdr-fleet init` to create one"


def git_init(path):
    os.makedirs(path, exist_ok=True)
    subprocess.run(["git", "init", "-q", path], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


class InitCase(unittest.TestCase):
    """A temp directory and a fake herdr whose log must stay empty unless notifying."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="herdr-fleet-init-"))
        self.world_path = os.path.join(self.tmp, "world.json")
        self.log_path = os.path.join(self.tmp, "calls.jsonl")
        world = fake_herdr.new_world()
        fake_herdr.add_workspace(world, CALLER_WS, "caller", self.tmp)
        fake_herdr.put_agent(world, CALLER_PANE, "claude", name="caller", status="working")
        with open(self.world_path, "w") as handle:
            json.dump(world, handle)
        self.env = {"PATH": os.environ.get("PATH", "")}  # outside herdr unless a test says otherwise
        self.inside = {"PATH": os.environ.get("PATH", ""), "HERDR_ENV": "1", "HERDR_BIN_PATH": "/fake/herdr",
                       "HERDR_PANE_ID": CALLER_PANE, "HERDR_WORKSPACE_ID": CALLER_WS}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, *argv, cwd, env=None):
        out, err = io.StringIO(), io.StringIO()
        code = main(list(argv), env=self.env if env is None else env,
                    runner=fake_herdr.Fake(self.world_path, self.log_path), stdout=out, stderr=err, cwd=cwd)
        return code, out.getvalue(), err.getvalue()

    def calls(self):
        if not os.path.exists(self.log_path):
            return []
        with open(self.log_path) as handle:
            return [json.loads(line) for line in handle]

    def mkdir(self, *parts):
        path = os.path.join(self.tmp, *parts)
        os.makedirs(path, exist_ok=True)
        return path

    def read(self, *parts):
        with open(os.path.join(*parts), "rb") as handle:
            return handle.read()


class Starter(InitCase):
    def test_plain_directory(self):
        project = self.mkdir("project")
        code, out, err = self.run_cli("init", cwd=project)
        self.assertEqual(code, 0, out + err)
        self.assertIn("root: %s (not in a Git work tree; using the directory)" % project, out)
        for rel in STARTER:
            self.assertIn("created     %s" % rel, out)
            self.assertTrue(os.path.isfile(os.path.join(project, rel)), rel)
        fleet = load_fleet(os.path.join(project, ".herdr", "fleet.toml"))
        self.assertEqual(fleet.warnings, [])
        self.assertIsNone(fleet.workspace)
        self.assertEqual(len(fleet.seats), 1)
        seat = fleet.seats[0]
        self.assertEqual((seat.name, seat.kind, seat.args), ("project-dev", "claude", []))
        self.assertEqual(seat.kickoff or fleet.kickoff, os.path.join(project, ".herdr", "kickoff", "default.md"))
        self.assertEqual(fleet.state_note, os.path.join(project, ".herdr", "STATE.md"))
        self.assertEqual(self.read(project, ".herdr", "STATE.md"),
                         b"Where things stand: (nothing yet).\n\n## production\n\n## in flight\n\n## next\n\n"
                         b"## backlog\n\n")
        self.assertEqual(self.read(project, ".herdr", ".gitignore"),
                         b"fleet.state.json\nmessages.jsonl\ninbox/\nwatch.lock\nwatch.log\ntimers.state.json\ntimers.lock\n")
        self.assertIn(b"<!--", self.read(project, ".herdr", "kickoff", "default.md").splitlines()[0])
        self.assertEqual(fleet.mode, "chain")
        text = self.read(project, ".herdr", "fleet.toml").decode()
        for key in ("workspace", "cwd", "quit_timeout_ms", "[layout]", "mode", "columns", "direction", "ratio",
                    "args", "split", "from", "env", "kickoff_text", "quit", "notes"):
            self.assertIn(key, text)  # every optional key is shown, commented out
        self.assertIn('# mode = "chain"', text)
        self.assertIn("grid:", text)  # both modes explained
        self.assertEqual(self.calls(), [])

    @unittest.skipUnless(GIT, "git is not installed")
    def test_git_root_from_a_subdirectory(self):
        repo = self.mkdir("repo")
        git_init(repo)
        sub = self.mkdir("repo", "src", "pkg")
        code, out, err = self.run_cli("init", cwd=sub)
        self.assertEqual(code, 0, out + err)
        self.assertIn("root: %s (Git work tree)" % repo, out)
        self.assertTrue(os.path.isfile(os.path.join(repo, ".herdr", "fleet.toml")))
        self.assertFalse(os.path.exists(os.path.join(sub, ".herdr")))
        self.assertEqual(load_fleet(os.path.join(repo, ".herdr", "fleet.toml")).seats[0].name, "repo-dev")
        self.assertEqual(self.calls(), [])

    def test_dir_argument(self):
        self.mkdir("elsewhere", "proj")
        code, out, _ = self.run_cli("init", "elsewhere/proj", cwd=self.tmp)
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "elsewhere", "proj", ".herdr", "fleet.toml")))
        code, _, err = self.run_cli("init", "missing", cwd=self.tmp)
        self.assertEqual(code, 2)
        self.assertIn("not a directory", err)

    def test_never_overwrites(self):
        project = self.mkdir("project")
        self.mkdir("project", ".herdr")
        with open(os.path.join(project, ".herdr", "fleet.toml"), "w") as handle:
            handle.write("# mine\n")
        code, out, _ = self.run_cli("init", cwd=project)
        self.assertEqual(code, 0, out)
        self.assertIn("kept        .herdr/fleet.toml", out)
        self.assertIn("created     .herdr/STATE.md", out)
        self.assertEqual(self.read(project, ".herdr", "fleet.toml"), b"# mine\n")
        code, out, _ = self.run_cli("init", cwd=project)
        self.assertEqual(code, 0)
        self.assertEqual(out.count("kept "), 4)

    def test_force_overwrites(self):
        project = self.mkdir("project")
        self.mkdir("project", ".herdr")
        with open(os.path.join(project, ".herdr", "fleet.toml"), "w") as handle:
            handle.write("# mine\n")
        code, out, _ = self.run_cli("init", "--force", cwd=project)
        self.assertEqual(code, 0, out)
        self.assertIn("overwritten .herdr/fleet.toml", out)
        self.assertIn("created     .herdr/STATE.md", out)
        self.assertIn(b"[[agents]]", self.read(project, ".herdr", "fleet.toml"))

    def test_write_failure_exits_1_and_notifies(self):
        project = self.mkdir("project")
        with open(os.path.join(project, ".herdr"), "w") as handle:
            handle.write("a file where the directory should be")
        code, out, _ = self.run_cli("init", "--notify", cwd=project, env=self.inside)
        self.assertEqual(code, 1)
        self.assertIn("failed      .herdr/fleet.toml: ", out)
        self.assertNotIn("next:", out)
        notes = [c["argv"] for c in self.calls()]
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0][:3], ["notification", "show", "Fleet init failed"])
        self.assertEqual(notes[0][-2:], ["--sound", "request"])


class Examples(InitCase):
    def test_team_copies_every_file(self):
        project = self.mkdir("project")
        code, out, err = self.run_cli("init", "--example", "team", cwd=project)
        self.assertEqual(code, 0, out + err)
        source = os.path.join(EXAMPLES_DIR, "team")
        shipped = []
        for folder, _, names in os.walk(source):
            shipped += [os.path.relpath(os.path.join(folder, n), source) for n in names if n not in SKIP_NAMES]
        self.assertGreaterEqual(len(shipped), 6)
        for rel in shipped:
            self.assertEqual(self.read(project, rel), self.read(source, rel), rel)
            self.assertIn("created     %s" % rel.replace(os.sep, "/"), out)
        self.assertEqual(out.count("created     "), len(shipped))  # the example only, no starter files
        fleet = load_fleet(os.path.join(project, ".herdr", "fleet.toml"))
        self.assertEqual([s.name for s in fleet.seats], ["sample-orchestrator", "sample-dev", "sample-reviewer", "sample-designer"])

    def test_team_initializes_and_renders_every_role_without_external_documents(self):
        project = self.mkdir("portable-team")
        code, out, err = self.run_cli("init", "--example", "team", cwd=project)
        self.assertEqual(code, 0, out + err)
        fleet = load_fleet(os.path.join(project, ".herdr", "fleet.toml"))
        self.assertEqual(fleet.warnings, [])
        roster = roster_table([(seat.name, seat.kind, "no pane", seat.cwd) for seat in fleet.seats])
        for seat in fleet.seats:
            with self.subTest(role=seat.name):
                self.assertEqual(seat.args, [])  # no account-specific model is required
                self.assertTrue(os.path.isfile(seat.kickoff))
                rendered = compose(seat, fleet, "sample", roster, "up", note="Example note",
                                   state_text="Example state")
                self.assertTrue(rendered.startswith("[from herdr-fleet] You are " + seat.name))
                self.assertIn("Example note", rendered)
                self.assertIn("Example state", rendered)
                self.assertIn("explicit owner authorization before merging, deploying, publishing or pushing",
                              rendered)
                for key in PLACEHOLDERS:
                    self.assertNotIn("{" + key + "}", rendered)
                self.assertEqual(self.calls(), [])  # initialization/rendering never invokes Herdr

    def test_unknown_example_exits_2_and_lists_the_examples(self):
        project = self.mkdir("project")
        code, _, err = self.run_cli("init", "--example", "nope", cwd=project)
        self.assertEqual(code, 2)
        self.assertIn("unknown example 'nope'; available: team", err)
        self.assertEqual(os.listdir(project), [])


class SeatNames(InitCase):
    CASES = {
        "My Project": "my-project-dev",
        "UPPER case": "upper-case-dev",
        "123start": "start-dev",
        "ünïcödé": "unicode-dev",
        "日本語": "fleet-dev",
        "---": "fleet-dev",
        "a.b c": "a-b-c-dev",
        "_under_score_": "under_score-dev",
    }

    def test_names_are_valid_herdr_names(self):
        for raw, expected in self.CASES.items():
            self.assertEqual(seat_name("/x/" + raw), expected, raw)
        long_name = seat_name("/x/" + "Very Long Directory Name " * 4)
        self.assertLessEqual(len(long_name), 32)
        for raw in list(self.CASES) + ["Very Long Directory Name " * 4, "9", "x" * 80, "a-" * 20]:
            self.assertTrue(NAME_RE.match(seat_name("/x/" + raw)), raw)

    def test_starter_loads_for_odd_directory_names(self):
        for raw in ("My Project", "123start", "ünïcödé", "x" * 80, "日本語"):
            project = self.mkdir(raw)
            code, out, err = self.run_cli("init", cwd=project)
            self.assertEqual(code, 0, out + err)
            fleet = load_fleet(os.path.join(project, ".herdr", "fleet.toml"))
            self.assertEqual(fleet.seats[0].name, seat_name(project))


class Action(InitCase):
    @unittest.skipUnless(GIT, "git is not installed")
    def test_action_writes_into_the_focused_panes_root_and_notifies(self):
        repo = self.mkdir("repo")
        git_init(repo)
        sub = self.mkdir("repo", "app")
        plugin_root = self.mkdir("plugin-root")  # actions run in the plugin root
        context = {"workspace_id": CALLER_WS, "focused_pane_id": CALLER_PANE, "focused_pane_cwd": sub}
        env = dict(self.inside, HERDR_PLUGIN_ID="yarrasys.fleet", HERDR_PLUGIN_ACTION_ID="yarrasys.fleet.init",
                   HERDR_PLUGIN_CONTEXT_JSON=json.dumps(context))
        code, out, err = self.run_cli("init", "--notify", cwd=plugin_root, env=env)
        self.assertEqual(code, 0, out + err)
        self.assertTrue(os.path.isfile(os.path.join(repo, ".herdr", "fleet.toml")))
        self.assertFalse(os.path.exists(os.path.join(plugin_root, ".herdr")))
        calls = [c["argv"] for c in self.calls()]
        self.assertEqual(len(calls), 1, calls)  # the notification is the only herdr command
        self.assertEqual(calls[0][:3], ["notification", "show", "Fleet init: 4 file(s) written in %s" % repo])
        self.assertIn("created     .herdr/fleet.toml", calls[0][4])
        self.assertEqual(calls[0][-2:], ["--sound", "done"])

    def test_action_without_focused_cwd_exits_2_and_notifies(self):
        env = dict(self.inside, HERDR_PLUGIN_ACTION_ID="yarrasys.fleet.init",
                   HERDR_PLUGIN_CONTEXT_JSON=json.dumps({"workspace_id": CALLER_WS}))
        code, _, err = self.run_cli("init", "--notify", cwd=self.mkdir("plugin-root"), env=env)
        self.assertEqual(code, 2)
        self.assertIn("focused_pane_cwd", err)
        self.assertEqual([c["argv"][2] for c in self.calls()], ["Fleet init failed"])

    def test_no_herdr_command_inside_herdr_without_notify(self):
        code, _, _ = self.run_cli("init", cwd=self.mkdir("project"), env=self.inside)
        self.assertEqual(code, 0)
        self.assertEqual(self.calls(), [])

    def test_notify_is_skipped_outside_herdr(self):
        code, _, _ = self.run_cli("init", "--notify", cwd=self.mkdir("project"))
        self.assertEqual(code, 0)
        self.assertEqual(self.calls(), [])


class Pointers(InitCase):
    def test_version(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["--version"], env=self.env)
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue().strip(), "herdr-fleet %s" % __version__)

    def test_no_fleet_file_points_at_init(self):
        empty = self.mkdir("empty")
        for argv in (["up"], ["status"], ["restart", "x"], ["swap"], ["kickoff", "x"], ["down"], ["ui", "status"]):
            code, out, err = self.run_cli(*argv, cwd=empty, env=self.inside)
            self.assertEqual(code, 2, argv)
            self.assertIn(HINT, err, argv)
        code, out, _ = self.run_cli("doctor", cwd=empty, env=self.inside)
        self.assertEqual(code, 1)
        self.assertIn("fail  fleet file: no .herdr/fleet.toml found", out)
        self.assertIn(HINT, out)


class InitThenUp(FleetCase):
    """The starter works end to end: doctor passes and up briefs the seat."""

    fleet_text = None

    def test_init_doctor_up(self):
        code, out, err = self.run_cli("init")
        self.assertEqual(code, 0, out + err)
        code, out, _ = self.run_cli("doctor")
        self.assertEqual(code, 0, out)
        self.assertNotIn("warn  fleet file", out)
        code, out, _ = self.run_cli("up")
        self.assertEqual(code, 0, out)
        name = seat_name(self.repo)
        self.assertEqual(self.argvs("pane", "split")[0][2], CALLER_PANE)  # no workspace key: beside the caller
        kickoff = self.prompts_to(name)[0]
        self.assertTrue(kickoff.startswith("[from herdr-fleet] You are %s, a claude agent" % name), kickoff)
        self.assertNotIn("<!--", kickoff)
        self.assertNotIn("Placeholders", kickoff)
        self.assertIn("Where things stand: (nothing yet).", kickoff)


if __name__ == "__main__":
    unittest.main()
