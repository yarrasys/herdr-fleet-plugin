"""Unit tests: kickoff rendering, the state file, the herdr client and the
process-info helpers."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

import support
from herdr_fleet.fleetfile import Fleet, Seat
from herdr_fleet.herdr import Herdr, HerdrError, parse_integration_status, parse_version
from herdr_fleet.kickoff import DEFAULT_TEMPLATE, compose, render, roster_table, strip_comments
from herdr_fleet.seats import (agent_args, at_shell_prompt, contains_in_order, find_agent_process,
                               process_argv, quit_command)
from herdr_fleet.state import State, StateError, load_state, prune_state, save_state


def seat(**kw):
    base = dict(name="fablyn-dev", kind="claude", args=["--model", "m x"], cwd="/repo", split="right", ratio=None,
                env={}, kickoff=None, kickoff_text=None, quit=None, notes="", index=0)
    base.update(kw)
    return Seat(**base)


def fleet(seats, **kw):
    base = dict(path="/repo/.herdr/fleet.toml", repo="/repo", workspace="fablyn", cwd="/repo", state_note=None,
                kickoff=None, direction="right", ratio=None, quit_timeout_ms=None, seats=seats)
    base.update(kw)
    return Fleet(**base)


class KickoffRendering(unittest.TestCase):
    def test_known_placeholders_and_format_semantics(self):
        self.assertEqual(render("{name} {{literal}} {unknown} {name!r}", {"name": "a"}), "a {literal} {unknown} 'a'")

    def test_code_with_braces_is_left_alone(self):
        text = 'run `jq "{a: 1}"` then {name}; json {"k": "v"}'
        self.assertEqual(render(text, {"name": "x"}), 'run `jq "{a: 1}"` then x; json {"k": "v"}')

    def test_unbalanced_braces_fall_back_to_plain_replacement(self):
        self.assertEqual(render("oops } {name} {", {"name": "x"}), "oops } x {")

    def test_default_template_and_prefix(self):
        s = seat()
        text = compose(s, fleet([s]), "fablyn", roster_table([("fablyn-dev", "claude", "w2:p2", "/repo")]), "up")
        self.assertTrue(text.startswith(
            '[from herdr-fleet] You are fablyn-dev, a claude agent in the "fablyn" fleet for /repo.'))
        self.assertIn("| fablyn-dev | claude | w2:p2 | /repo |", text)
        self.assertIn("on up.", text)
        self.assertEqual(text, text.strip())
        self.assertTrue(DEFAULT_TEMPLATE.startswith("[from herdr-fleet]"))

    def test_prefix_added_unless_template_starts_with_from(self):
        s = seat(kickoff_text="Hello {name}, args {args}")
        self.assertEqual(compose(s, fleet([s]), "w", "", "kickoff"),
                         "[from herdr-fleet] Hello fablyn-dev, args --model 'm x'")
        s = seat(kickoff_text="[from the owner] hi")
        self.assertEqual(compose(s, fleet([s]), "w", "", "kickoff"), "[from the owner] hi")

    def test_whole_line_html_comments_are_not_sent(self):
        s = seat(kickoff_text="<!-- note for humans: {name} -->\n  <!-- indented too -->\n"
                              "Hi {name} <!-- inline stays -->")
        self.assertEqual(compose(s, fleet([s]), "w", "", "up"),
                         "[from herdr-fleet] Hi fablyn-dev <!-- inline stays -->")
        self.assertEqual(strip_comments("a\n<!-- x -->\nb\n"), "a\nb\n")

    def test_state_note_and_note(self):
        tmp = tempfile.mkdtemp()
        try:
            note = os.path.join(tmp, "STATE.md")
            with open(note, "w") as handle:
                handle.write("main is green\n")
            s = seat(kickoff_text="{state}|{note}|{reason}|{workspace}|{cwd}|{repo}|{kind}")
            text = compose(s, fleet([s], state_note=note), "fablyn", "", "restart", note="go")
            self.assertEqual(text, "[from herdr-fleet] main is green|go|restart|fablyn|/repo|/repo|claude")
            missing = fleet([s], state_note=os.path.join(tmp, "nope.md"))
            self.assertEqual(compose(s, missing, "w", "", "up"), "[from herdr-fleet] ||up|w|/repo|/repo|claude")
        finally:
            shutil.rmtree(tmp)


class StateFile(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.repo)

    def test_missing_is_empty_and_save_creates_gitignore(self):
        state = load_state(self.repo)
        self.assertFalse(state.existed)
        self.assertEqual(state.agents, {})
        state.update("a", pane_id="w1:p2", created_pane=True)
        save_state(state)
        with open(os.path.join(self.repo, ".herdr", "fleet.state.json")) as handle:
            data = json.load(handle)
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["repo"], self.repo)
        self.assertEqual(data["agents"]["a"]["pane_id"], "w1:p2")
        with open(os.path.join(self.repo, ".herdr", ".gitignore")) as handle:
            self.assertEqual(handle.read(), "fleet.state.json\n")
        self.assertEqual([f for f in os.listdir(os.path.join(self.repo, ".herdr")) if f.endswith(".tmp")], [])

    def test_gitignore_line_appended_once(self):
        os.makedirs(os.path.join(self.repo, ".herdr"))
        with open(os.path.join(self.repo, ".herdr", ".gitignore"), "w") as handle:
            handle.write("other")
        state = load_state(self.repo)
        state.dirty = True
        save_state(state)
        save_state(state)
        with open(os.path.join(self.repo, ".herdr", ".gitignore")) as handle:
            self.assertEqual(handle.read(), "other\nfleet.state.json\n")

    def test_corrupt_state_is_an_error(self):
        os.makedirs(os.path.join(self.repo, ".herdr"))
        with open(os.path.join(self.repo, ".herdr", "fleet.state.json"), "w") as handle:
            handle.write("{nope")
        with self.assertRaises(StateError):
            load_state(self.repo)

    def test_prune_drops_only_not_found(self):
        class FakeHerdr:
            def pane_get(self, pane):
                if pane == "gone":
                    raise HerdrError("pane_not_found", "pane gone not found")
                if pane == "flaky":
                    raise HerdrError("server_unreachable", "socket closed")
                return {"pane_id": pane}

            def workspace_get(self, ws):
                raise HerdrError("workspace_not_found", "nope")

            def agent_list(self):
                return [{"name": "m", "pane_id": "w5:p3"}]

        state = State("/x", {"workspace_id": "w9", "created_workspace": True,
                             "agents": {"a": {"pane_id": "gone"}, "b": {"pane_id": "here"},
                                        "m": {"pane_id": "gone", "created_pane": True, "adopted": True,
                                              "kind": "claude"}}}, True)
        messages = prune_state(state, FakeHerdr())
        self.assertEqual(sorted(state.agents), ["b", "m"])
        # m's pane was moved (the old id is gone, m is live elsewhere): re-pointed, every field kept
        self.assertEqual(state.agents["m"], {"pane_id": "w5:p3", "created_pane": True, "adopted": True,
                                             "kind": "claude"})
        self.assertIn("state: m moved from gone to w5:p3", messages)
        self.assertIsNone(state.workspace_id)
        self.assertFalse(state.created_workspace)
        self.assertEqual(len(messages), 3)
        state.agents["c"] = {"pane_id": "flaky"}
        with self.assertRaises(HerdrError):
            prune_state(state, FakeHerdr())


class Client(unittest.TestCase):
    def herdr(self, code=0, out="", err=""):
        self.seen = []

        def runner(argv):
            self.seen.append(argv)
            return code, out, err
        return Herdr("/bin/herdr", runner)

    def test_result_and_error_json(self):
        h = self.herdr(out='{"id": "x", "result": {"pane": {"pane_id": "w1:p1"}}}')
        self.assertEqual(h.pane_get("w1:p1"), {"pane_id": "w1:p1"})
        self.assertEqual(self.seen, [["/bin/herdr", "pane", "get", "w1:p1"]])
        # verbatim from `herdr pane get w0:p0` on herdr 0.9.4 (stderr, exit 1)
        live = '{"error":{"code":"pane_not_found","message":"pane w0:p0 not found"},"id":"cli:pane:get"}'
        h = self.herdr(1, err=live)
        with self.assertRaises(HerdrError) as caught:
            h.pane_get("w0:p0")
        self.assertEqual((caught.exception.code, caught.exception.message), ("pane_not_found", "pane w0:p0 not found"))

    def test_usage_and_garbage(self):
        with self.assertRaises(HerdrError) as caught:
            self.herdr(2, err="error: unexpected argument\nusage...").agent_list()
        self.assertEqual(caught.exception.code, "usage")
        with self.assertRaises(HerdrError) as caught:
            self.herdr(0, out="not json").agent_list()
        self.assertEqual(caught.exception.code, "bad_output")
        with self.assertRaises(HerdrError) as caught:
            self.herdr(7, err="segfault").agent_list()
        self.assertEqual(caught.exception.code, "exit_7")

    def test_missing_binary(self):
        def runner(argv):
            raise FileNotFoundError(2, "No such file or directory")
        with self.assertRaises(HerdrError) as caught:
            Herdr("/nope/herdr", runner).agent_list()
        self.assertEqual(caught.exception.code, "herdr_not_found")

    def test_forbidden_commands_never_reach_the_binary(self):
        h = self.herdr(out="{}")
        for argv in (["agent", "send-keys", "x", "enter"], ["pane", "send-keys", "w1:p1", "enter"],
                     ["pane", "send-text", "w1:p1", "hi"], ["pane", "run", "w1:p1", "ls"], []):
            with self.assertRaises(HerdrError) as caught:
                h.run(*argv)
            self.assertEqual(caught.exception.code, "forbidden_command")
        self.assertEqual(self.seen, [])

    def test_kinds_from_usage_on_stderr_then_help(self):
        h = self.herdr(2, err="herdr agent commands:\n  kinds: pi|claude|codex\n")
        self.assertEqual(h.kinds(), ["pi", "claude", "codex"])
        answers = [(2, "", "no kinds here"), (0, "  [possible values: pi, claude]\n", "")]
        h = Herdr("/bin/herdr", lambda argv: answers.pop(0))
        self.assertEqual(h.kinds(), ["pi", "claude"])

    def test_start_argv_and_split_argv(self):
        h = self.herdr(out='{"result": {"pane": {"pane_id": "w1:p9"}}}')
        self.assertEqual(h.pane_split("w1:p1", "down", 0.3, "/r", {"B": "2", "A": "1"}), "w1:p9")
        self.assertEqual(self.seen[0][1:], ["pane", "split", "w1:p1", "--direction", "down", "--ratio", "0.3",
                                            "--cwd", "/r", "--env", "A=1", "--env", "B=2", "--no-focus"])

    def test_parsers(self):
        self.assertEqual(parse_version("herdr 0.9.4\n"), (0, 9, 4))
        self.assertIsNone(parse_version("herdr dev"))
        rows = parse_integration_status("pi: outdated (v8 < v9) (/p)\nclaude: current (v10) (/c)\n"
                                        "letta (experimental): not installed (/l)\nnoise\n")
        self.assertEqual([(r["kind"], r["status"]) for r in rows],
                         [("pi", "outdated"), ("claude", "current"), ("letta", "not installed")])


class FakeMatchesLive(unittest.TestCase):
    """The fake must mirror live herdr 0.9.4 where the code depends on it."""

    def test_name_only_on_agent_records(self):
        sup = support
        tmp = tempfile.mkdtemp()
        try:
            world = sup.fake_herdr.new_world()
            sup.fake_herdr.add_workspace(world, "w1", "x", tmp)
            sup.fake_herdr.put_agent(world, "w1:p1", "claude", name="dev")
            world_path, log_path = os.path.join(tmp, "w.json"), os.path.join(tmp, "l.jsonl")
            with open(world_path, "w") as handle:
                json.dump(world, handle)
            h = Herdr("herdr", sup.fake_herdr.Fake(world_path, log_path))
            self.assertNotIn("name", h.pane_get("w1:p1"))
            self.assertNotIn("name", h.pane_list("w1")[0])
            self.assertEqual(h.pane_get("w1:p1")["agent"], "claude")
            self.assertEqual(h.agent_get("w1:p1")["name"], "dev")
            self.assertEqual(h.agent_list()[0]["name"], "dev")
            self.assertEqual(h.pane_agent("w1:p1")["name"], "dev")
            sup.fake_herdr.add_pane(world, "w1:p2", "w1", tmp)
            with open(world_path, "w") as handle:
                json.dump(world, handle)
            self.assertEqual(h.pane_agent("w1:p2"), {})
            self.assertEqual(h.pane_agent("w9:p9"), {})
            # pane move, both forms (live 0.9.4): ids survive within a workspace, change across
            # workspaces; moving a pane to the tab it is already in changes nothing
            sup.fake_herdr.add_workspace(world, "w2", "y", tmp)
            with open(world_path, "w") as handle:
                json.dump(world, handle)
            self.assertEqual(h.pane_move_to_tab("w1:p1", "w1:t1", "down", 0.5, "w1:p2"), "w1:p1")
            self.assertEqual(h.pane_move_new_tab("w1:p1", "w1", "park"), "w1:p1")
            self.assertNotEqual(h.pane_get("w1:p1")["tab_id"], "w1:t1")
            self.assertEqual(h.pane_move_to_tab("w1:p1", "w1:t1", "down", 0.5, "w1:p2"), "w1:p1")
            self.assertEqual(h.pane_get("w1:p1")["tab_id"], "w1:t1")
            moved = h.pane_move_to_tab("w1:p1", "w2:t1", "right", 0.5)
            self.assertTrue(moved.startswith("w2:p"))
            self.assertEqual(h.agent_get(moved)["name"], "dev")
            self.assertEqual(h.tab_list("w2")[0]["tab_id"], "w2:t1")
        finally:
            shutil.rmtree(tmp)


class ProcessInfo(unittest.TestCase):
    # Shapes copied from `herdr pane process-info` on herdr 0.9.4.
    CLAUDE = {"argv": ["claude", "--model", "claude-fable-5-1"], "argv0": "claude",
              "cmdline": "claude --model claude-fable-5-1", "name": "2.1.299", "pid": 77043}
    MCP = {"argv0": "chrome-devtools-mcp", "name": "node", "pid": 41361}

    def test_shell_prompt(self):
        self.assertTrue(at_shell_prompt({"foreground_processes": [{"name": "zsh", "pid": 1}]}))
        self.assertTrue(at_shell_prompt({"foreground_processes": [{"name": "-zsh", "argv0": "-zsh"}]}))
        self.assertTrue(at_shell_prompt({"foreground_processes": [{"name": "/bin/bash"}]}))
        self.assertFalse(at_shell_prompt({"foreground_processes": [{"name": "zsh"}, {"name": "node"}]}))
        self.assertFalse(at_shell_prompt({"foreground_processes": [self.CLAUDE]}))
        self.assertFalse(at_shell_prompt({"foreground_processes": []}))
        self.assertFalse(at_shell_prompt({}))

    def test_agent_process_and_args(self):
        info = {"foreground_processes": [self.MCP, self.CLAUDE]}
        proc = find_agent_process(info, "claude")
        self.assertIs(proc, self.CLAUDE)
        self.assertEqual(agent_args(process_argv(proc), "claude"), ["--model", "claude-fable-5-1"])
        node = {"argv": ["node", "/opt/bin/codex", "-m", "x"], "argv0": "node", "name": "node"}
        self.assertIs(find_agent_process({"foreground_processes": [node]}, "codex"), node)
        self.assertEqual(agent_args(process_argv(node), "codex"), ["-m", "x"])
        self.assertEqual(process_argv({"cmdline": "codex -m 'a b'"}), ["codex", "-m", "a b"])
        self.assertIsNone(process_argv(self.MCP))

    def test_contains_in_order(self):
        self.assertTrue(contains_in_order(["--resume", "--model", "m", "-v"], ["--model", "m"]))
        self.assertFalse(contains_in_order(["--model", "x", "m"], ["--model", "m"]))
        self.assertTrue(contains_in_order(["a"], []))
        self.assertFalse(contains_in_order([], ["a"]))

    def test_quit_command_precedence(self):
        s = seat(quit="/bye")
        self.assertEqual(quit_command(s, "claude", {}), "/bye")
        self.assertEqual(quit_command(s, "codex", {}), "/quit")  # seat override is for its own kind
        self.assertEqual(quit_command(seat(), "claude", {"claude": "/q"}), "/q")
        self.assertIsNone(quit_command(seat(kind="amp"), "amp", {}))


if __name__ == "__main__":
    unittest.main()
