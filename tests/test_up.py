"""up (design 4.1): empty workspace, idempotence, adopt-on-resume, busy retry,
agent_not_ready, --me, --dry-run, conflicts and kickoffs."""

from __future__ import annotations

import os
import unittest

import fake_herdr
from support import CALLER_PANE, FleetCase, THREE_SEATS

MUTATING = (("workspace", "create"), ("pane", "split"), ("agent", "start"), ("agent", "prompt"),
            ("agent", "rename"), ("pane", "close"), ("workspace", "close"), ("notification", "show"))

NO_LABEL = THREE_SEATS.replace('workspace = "sample"\n', "")


class UpOnEmptyWorkspace(FleetCase):
    def test_creates_workspace_starts_in_order_then_briefs(self):
        code, out, err = self.run_cli("up")
        self.assertEqual(code, 0, out + err)
        create = self.argvs("workspace", "create")
        self.assertEqual(create, [["workspace", "create", "--cwd", self.repo, "--label", "sample", "--no-focus"]])
        ws = [w for w, rec in self.world["workspaces"].items() if rec["label"] == "sample"][0]
        root = ws + ":p1"
        starts = self.argvs("agent", "start")
        self.assertEqual(starts[0], ["agent", "start", "sample-orchestrator", "--kind", "claude", "--pane", root,
                                     "--timeout", "15000", "--", "--model", "claude-opus-5-5"])
        self.assertEqual([s[2] for s in starts], ["sample-orchestrator", "sample-dev", "sample-reviewer"])
        self.assertEqual(starts[2][3:5], ["--kind", "codex"])
        self.assertEqual(starts[2][-3:], ["--", "-m", "gpt-6-astra"])
        splits = self.argvs("pane", "split")
        self.assertEqual(len(splits), 2)
        self.assertEqual(splits[0][2], root)
        self.assertEqual(splits[0][3:5], ["--direction", "right"])
        self.assertIn("--no-focus", splits[0])
        self.assertEqual(splits[1][2], self.pane_of("sample-dev"))
        # kickoffs only after every seat started, in file order
        calls = [c["argv"] for c in self.calls()]
        last_start = max(i for i, a in enumerate(calls) if a[:2] == ["agent", "start"])
        first_prompt = min(i for i, a in enumerate(calls) if a[:2] == ["agent", "prompt"])
        self.assertLess(last_start, first_prompt)
        prompts = self.argvs("agent", "prompt")
        self.assertEqual([p[2] for p in prompts], ["sample-orchestrator", "sample-dev", "sample-reviewer"])
        for prompt in prompts:
            self.assertTrue(prompt[3].startswith("[from herdr-fleet]"))
            for name in ("sample-orchestrator", "sample-dev", "sample-reviewer"):
                self.assertIn("| %s |" % name, prompt[3])
            self.assertIn("on up.", prompt[3])
        state = self.read_state()
        self.assertTrue(state["created_workspace"])
        self.assertEqual(state["workspace_id"], ws)
        self.assertFalse(state["agents"]["sample-orchestrator"]["created_pane"])  # root pane
        self.assertTrue(state["agents"]["sample-dev"]["created_pane"])
        self.assertEqual(state["agents"]["sample-reviewer"]["args"], ["-m", "gpt-6-astra"])
        self.assertTrue(state["agents"]["sample-dev"]["last_kickoff_at"])
        with open(os.path.join(self.repo, ".herdr", ".gitignore")) as handle:
            self.assertIn("fleet.state.json", handle.read().split())
        self.assertIn("SEAT", out)  # status table printed

    def test_new_workspace_root_pane_gets_seat_ones_cwd_and_env(self):
        os.makedirs(os.path.join(self.repo, "app"))
        self.write_fleet(THREE_SEATS.replace(
            'name = "sample-orchestrator"\n', 'name = "sample-orchestrator"\ncwd = "app"\nenv = { ROLE = "orch" }\n'))
        code, out, _ = self.run_cli("up")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.argvs("workspace", "create"),
                         [["workspace", "create", "--cwd", os.path.join(self.repo, "app"), "--label", "sample",
                           "--env", "ROLE=orch", "--no-focus"]])
        root = self.pane_of("sample-orchestrator")
        self.assertEqual(self.world["panes"][root]["env"], {"ROLE": "orch"})
        self.assertEqual(self.argvs("pane", "split")[0][2], root)

    def test_root_pane_is_reserved_for_the_seat_it_was_made_for(self):
        self.write_fleet(THREE_SEATS.replace(
            'name = "sample-dev"\n', 'name = "sample-dev"\nenv = { ROLE = "dev" }\n'))
        code, out, _ = self.run_cli("up", "sample-dev")
        self.assertEqual(code, 0, out)
        self.assertIn("ROLE=dev", self.argvs("workspace", "create")[0])
        self.assertEqual(self.argvs("pane", "split"), [])
        self.assertEqual(self.pane_of("sample-dev"), self.read_state()["workspace_id"] + ":p1")

    def test_second_up_is_a_no_op(self):
        self.assertEqual(self.run_cli("up")[0], 0)
        before = len(self.calls())
        code, out, _ = self.run_cli("up")
        self.assertEqual(code, 0, out)
        later = [c["argv"][:2] for c in self.calls()[before:]]
        for mutating in MUTATING:
            self.assertNotIn(list(mutating), later)
        self.assertIn("already running", out)

    def test_names_limit_the_seats(self):
        code, out, _ = self.run_cli("up", "sample-dev")
        self.assertEqual(code, 0, out)
        self.assertEqual([s[2] for s in self.argvs("agent", "start")], ["sample-dev"])
        self.assertEqual([p[2] for p in self.argvs("agent", "prompt")], ["sample-dev"])

    def test_unknown_seat_name_is_a_usage_error(self):
        code, _, err = self.run_cli("up", "nobody")
        self.assertEqual(code, 2)
        self.assertIn("not a seat", err)

    def test_no_kickoff(self):
        code, out, _ = self.run_cli("up", "--no-kickoff")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.argvs("agent", "prompt"), [])

    def test_note_reaches_the_kickoff(self):
        self.run_cli("up", "--note", "Ship the export dialog today.")
        self.assertIn("Ship the export dialog today.", self.prompts_to("sample-dev")[0])


class UpInCallerWorkspace(FleetCase):
    fleet_text = NO_LABEL

    def test_splits_from_caller_and_never_starts_into_it(self):
        code, out, err = self.run_cli("up")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.argvs("workspace", "create"), [])
        splits = self.argvs("pane", "split")
        self.assertEqual(len(splits), 3)
        self.assertEqual(splits[0][2], CALLER_PANE)
        for start in self.argvs("agent", "start"):
            self.assertNotEqual(start[start.index("--pane") + 1], CALLER_PANE)
        self.assertFalse(self.read_state()["created_workspace"])

    def test_me_adopts_the_caller_without_kickoff(self):
        code, out, err = self.run_cli("up", "--me", "sample-orchestrator")
        self.assertEqual(code, 0, out + err)
        self.assertIn(["agent", "rename", CALLER_PANE, "sample-orchestrator"], self.argvs("agent", "rename"))
        self.assertEqual([s[2] for s in self.argvs("agent", "start")], ["sample-dev", "sample-reviewer"])
        self.assertEqual(self.prompts_to("sample-orchestrator"), [])
        self.assertEqual(self.argvs("pane", "split")[0][2], CALLER_PANE)
        entry = self.read_state()["agents"]["sample-orchestrator"]
        self.assertEqual(entry["pane_id"], CALLER_PANE)
        self.assertFalse(entry["created_pane"])

    def test_me_renames_only_when_the_agent_get_name_differs(self):
        self.world["panes"][CALLER_PANE]["agent"]["name"] = "sample-orchestrator"
        code, out, err = self.run_cli("up", "--me", "sample-orchestrator")
        self.assertEqual(code, 0, out + err)
        self.assertIn(["agent", "get", CALLER_PANE], self.argvs("agent", "get"))
        self.assertEqual(self.argvs("agent", "rename"), [])
        self.assertIn("sample-orchestrator: adopted", out)
        self.assertEqual(self.prompts_to("sample-orchestrator"), [])
        self.assertEqual(self.read_state()["agents"]["sample-orchestrator"]["pane_id"], CALLER_PANE)

    def test_me_refuses_a_different_kind(self):
        code, out, _ = self.run_cli("up", "--me", "sample-reviewer")
        self.assertEqual(code, 1)
        self.assertIn("your pane runs claude but seat sample-reviewer is codex", out)
        self.assertEqual(self.argvs("agent", "rename"), [])

    def test_hint_for_an_unnamed_caller(self):
        self.world["panes"][CALLER_PANE]["agent"]["name"] = None
        code, out, _ = self.run_cli("up")
        self.assertEqual(code, 0, out)
        self.assertIn("hint: your pane %s runs an unnamed claude agent" % CALLER_PANE, out)
        self.assertEqual(self.argvs("agent", "rename"), [])

    def test_no_hint_when_the_callers_agent_is_named(self):
        code, out, _ = self.run_cli("up")  # the caller is named "caller" (set up in support)
        self.assertEqual(code, 0, out)
        self.assertNotIn("hint:", out)
        self.assertIn(["agent", "get", CALLER_PANE], self.argvs("agent", "get"))

    def test_refuses_without_caller_context(self):
        env = dict(self.env)
        del env["HERDR_PANE_ID"]
        del env["HERDR_WORKSPACE_ID"]
        code, _, err = self.run_cli("up", env=env)
        self.assertEqual(code, 1)
        self.assertIn("sets no workspace", err)


class UpInExistingWorkspace(FleetCase):
    def test_splits_from_the_first_pane_without_starting_into_it(self):
        root = self.fleet_ws()
        code, out, _ = self.run_cli("up")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.argvs("workspace", "create"), [])
        self.assertEqual(self.argvs("pane", "split")[0][2], root)
        self.assertNotIn(root, [s[s.index("--pane") + 1] for s in self.argvs("agent", "start")])

    def test_ambiguous_label_is_refused(self):
        self.fleet_ws("w2")
        self.fleet_ws("w3")
        code, _, err = self.run_cli("up")
        self.assertEqual(code, 1)
        self.assertIn("several workspaces are labelled 'sample'", err)
        self.assertEqual(self.argvs("agent", "start"), [])

    def test_reuses_a_recorded_pane_at_a_shell_prompt(self):
        self.fleet_ws()
        fake_herdr.add_pane(self.world, "w2:p7", "w2", self.repo)
        self.write_state({"version": 1, "workspace_id": "w2", "workspace_label": "sample",
                          "created_workspace": False,
                          "agents": {"sample-dev": {"pane_id": "w2:p7", "created_pane": True}}})
        code, out, _ = self.run_cli("up")
        self.assertEqual(code, 0, out)
        self.assertIn(["agent", "start", "sample-dev", "--kind", "claude", "--pane", "w2:p7", "--timeout", "15000",
                       "--", "--model", "claude-opus-5-5"], self.argvs("agent", "start"))
        self.assertEqual(len(self.argvs("pane", "split")), 2)

    def test_adopts_an_unnamed_resumed_agent(self):
        self.fleet_ws()
        fake_herdr.add_pane(self.world, "w2:p7", "w2", self.repo)
        fake_herdr.put_agent(self.world, "w2:p7", "claude", name=None, args=["--model", "claude-opus-5-5"])
        self.write_state({"version": 1, "workspace_id": "w2", "agents": {"sample-dev": {"pane_id": "w2:p7"}}})
        code, out, _ = self.run_cli("up")
        self.assertEqual(code, 0, out)
        self.assertIn(["agent", "get", "w2:p7"], self.argvs("agent", "get"))
        self.assertEqual(self.argvs("agent", "rename"), [["agent", "rename", "w2:p7", "sample-dev"]])
        self.assertEqual(self.world["panes"]["w2:p7"]["agent"]["name"], "sample-dev")
        self.assertNotIn("sample-dev", [s[2] for s in self.argvs("agent", "start")])
        self.assertEqual(self.prompts_to("sample-dev"), [])
        self.assertEqual(len(self.prompts_to("sample-reviewer")), 1)
        self.assertIn("adopted", out)

    def test_named_agent_of_the_right_kind_in_recorded_pane_is_a_conflict(self):
        # pane records carry no name (live herdr); only `agent get` reveals it
        self.fleet_ws()
        fake_herdr.add_pane(self.world, "w2:p7", "w2", self.repo)
        fake_herdr.put_agent(self.world, "w2:p7", "claude", name="someone-else", args=["--model", "claude-opus-5-5"])
        self.write_state({"version": 1, "workspace_id": "w2", "agents": {"sample-dev": {"pane_id": "w2:p7"}}})
        code, out, _ = self.run_cli("up")
        self.assertEqual(code, 3, out)
        self.assertIn("sample-dev: conflict - recorded pane w2:p7 hosts agent someone-else (claude); seat skipped", out)
        self.assertIn(["agent", "get", "w2:p7"], self.argvs("agent", "get"))
        self.assertEqual(self.argvs("agent", "rename"), [])
        self.assertNotIn("sample-dev", [s[2] for s in self.argvs("agent", "start")])
        self.assertEqual(self.prompts_to("someone-else"), [])
        self.assertEqual(self.world["panes"]["w2:p7"]["agent"]["name"], "someone-else")

    def test_conflict_when_recorded_pane_hosts_another_kind(self):
        self.fleet_ws()
        fake_herdr.add_pane(self.world, "w2:p7", "w2", self.repo)
        fake_herdr.put_agent(self.world, "w2:p7", "codex", name=None)
        self.write_state({"version": 1, "workspace_id": "w2", "agents": {"sample-dev": {"pane_id": "w2:p7"}}})
        code, out, _ = self.run_cli("up")
        self.assertEqual(code, 3, out)
        self.assertIn("conflict", out)
        self.assertEqual(self.argvs("agent", "rename"), [])
        self.assertNotIn("sample-dev", [s[2] for s in self.argvs("agent", "start")])

    def test_stale_state_is_pruned(self):
        self.fleet_ws()
        self.write_state({"version": 1, "workspace_id": "w99", "created_workspace": True,
                          "agents": {"sample-dev": {"pane_id": "w99:p3", "created_pane": True}}})
        code, out, _ = self.run_cli("up")
        self.assertEqual(code, 0, out)
        self.assertIn("dropped sample-dev", out)
        state = self.read_state()
        self.assertEqual(state["workspace_id"], "w2")
        self.assertFalse(state["created_workspace"])
        self.assertNotEqual(state["agents"]["sample-dev"]["pane_id"], "w99:p3")


class UpStartupProblems(FleetCase):
    def test_retries_agent_pane_busy(self):
        self.fleet_ws()
        self.world["behaviors"]["split_busy"] = 3
        code, out, _ = self.run_cli("up", "sample-dev")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.argvs("agent", "start")), 4)
        self.assertAlmostEqual(self.clock.slept, 0.75)

    def test_gives_up_on_busy_after_ten_seconds(self):
        self.fleet_ws()
        self.world["behaviors"]["split_busy"] = 1000
        code, out, _ = self.run_cli("up", "sample-dev")
        self.assertEqual(code, 1, out)
        self.assertIn("agent_pane_busy", out)
        self.assertGreaterEqual(self.clock.slept, 10.0)
        self.assertLess(self.clock.slept, 10.6)
        self.assertEqual(self.argvs("agent", "prompt"), [])

    def test_agent_not_ready_is_recorded_notified_and_never_prompted(self):
        self.world["behaviors"]["start_not_ready"] = ["sample-dev"]
        code, out, _ = self.run_cli("up")
        self.assertEqual(code, 3, out)
        self.assertEqual(self.prompts_to("sample-dev"), [])
        # still blocked for the whole start budget: polled with agent get, then given up on
        pane = self.read_state()["agents"]["sample-dev"]["pane_id"]
        polls = [c for c in self.calls() if c["argv"] == ["agent", "get", pane]]
        self.assertGreater(len(polls), 1)
        self.assertEqual({c["info"].get("status") for c in polls}, {"blocked"})
        self.assertGreaterEqual(self.clock.slept, 10.0)
        self.assertLess(self.clock.slept, 10.6)
        self.assertEqual(len(self.prompts_to("sample-reviewer")), 1)
        entry = self.read_state()["agents"]["sample-dev"]
        self.assertTrue(entry["blocked_at_startup"])
        self.assertTrue(entry["pane_id"])
        sounds = [n["sound"] for n in self.world["notifications"] if "sample-dev" in n["title"]]
        self.assertEqual(sounds, ["request"])
        # design 32.6 as the designer's live smoke amended it: the queued kickoff, not `kickoff NAME`
        self.assertRegex(out, r"answer the prompt there; its kickoff is queued \(#[0-9a-f]{6}\) and goes when it is idle")
        # a later up leaves the blocked seat alone and says it was never briefed
        code, out, _ = self.run_cli("up")
        self.assertEqual(code, 3)
        self.assertIn("never briefed", out)
        self.assertEqual(self.prompts_to("sample-dev"), [])

    def test_agent_not_ready_that_clears_by_itself_counts_as_started(self):
        # live (herdr 0.9.5, agy 1.4.3): the first launch answered agent_not_ready over a
        # one-time notice, yet agent get showed idle within a second; nobody answered anything
        self.world["behaviors"]["start_not_ready_transient"] = {"sample-dev": 3}
        code, out, _ = self.run_cli("up")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.prompts_to("sample-dev")), 1)
        self.assertIn("on up.", self.prompts_to("sample-dev")[0])
        entry = self.read_state()["agents"]["sample-dev"]
        self.assertFalse(entry["blocked_at_startup"])
        self.assertTrue(entry["last_kickoff_at"])
        self.assertEqual([n for n in self.world["notifications"] if "sample-dev" in n["title"]], [])
        self.assertNotIn("blocked at startup", out)
        # read-only polls only: nothing reached the agent before agent get answered idle
        calls = self.calls()
        pane = entry["pane_id"]
        start = next(i for i, c in enumerate(calls) if c["argv"][:3] == ["agent", "start", "sample-dev"])
        self.assertNotEqual(calls[start]["exit"], 0)
        polls = [(i, c["info"].get("status")) for i, c in enumerate(calls) if c["argv"] == ["agent", "get", pane]]
        self.assertEqual([status for _, status in polls[:4]], ["blocked", "blocked", "blocked", "idle"])
        ready = polls[3][0]
        prompts = [i for i, c in enumerate(calls) if c["argv"][:2] == ["agent", "prompt"]]
        self.assertTrue(prompts)
        self.assertTrue(all(i > ready for i in prompts), "a prompt was sent before agent get answered idle")
        self.assertAlmostEqual(self.clock.slept, 0.75)

    def test_agent_not_ready_then_working_counts_as_started(self):
        # past startup but already busy: started, and the kickoff follows the normal
        # safe_prompt rules (a working agent is not briefed, nothing is sent); since 32.6 it waits queued
        working = {"id": "cli:agent:get", "result": {"type": "agent_info", "agent": {
            "agent": "claude", "agent_status": "working", "name": "sample-dev", "pane_id": "w2:p101"}}}
        self.world["behaviors"]["start_not_ready"] = ["sample-dev"]
        self.world["canned"] = [{"prefix": ["agent", "get", "w2:p101"], "times": 1, "stdout": working},
                                {"prefix": ["agent", "get", "sample-dev"], "times": 1, "stdout": working}]
        self.fleet_ws()
        code, out, _ = self.run_cli("up", "sample-dev")
        self.assertEqual(code, 3, out)
        entry = self.read_state()["agents"]["sample-dev"]
        self.assertEqual(entry["pane_id"], "w2:p101")  # the pane the canned answers name
        self.assertIn("sample-dev: started - in w2:p101", out)
        self.assertRegex(out, r"sample-dev: kickoff queued #[0-9a-f]{6}: delivered when sample-dev is next idle "
                         r"or done")
        self.assertFalse(entry["blocked_at_startup"])
        self.assertEqual(self.prompts_to("sample-dev"), [])
        self.assertEqual(self.world["notifications"], [])

    def test_other_start_errors_surface_code_and_message(self):
        self.world["behaviors"]["start_errors"] = {"sample-dev": {"code": "agent_start_failed", "message": "boom"}}
        code, out, _ = self.run_cli("up")
        self.assertEqual(code, 1)
        self.assertIn("herdr error agent_start_failed: boom", out)

    def test_unknown_kind_refuses_before_any_change(self):
        self.write_fleet(THREE_SEATS.replace('kind = "codex"', 'kind = "nonesuch"'))
        code, _, err = self.run_cli("up")
        self.assertEqual(code, 2)
        self.assertIn("'nonesuch' is not a kind this herdr supports", err)
        for argv in [c["argv"] for c in self.calls()]:
            self.assertNotIn(tuple(argv[:2]), MUTATING)

    def test_stalled_kickoff_is_reported_and_not_resent(self):
        self.world["behaviors"]["prompt_errors"] = {"sample-dev": "agent_prompt_stalled"}
        code, out, _ = self.run_cli("up")
        self.assertEqual(code, 3, out)
        self.assertEqual(len(self.prompts_to("sample-dev")), 1)
        self.assertIn("not resent", out)


class UpDryRun(FleetCase):
    def test_dry_run_changes_nothing(self):
        code, out, err = self.run_cli("up", "--dry-run")
        self.assertEqual(code, 0, out + err)
        for argv in [c["argv"] for c in self.calls()]:
            self.assertNotIn(tuple(argv[:2]), MUTATING, argv)
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".herdr", "fleet.state.json")))
        self.assertIn("herdr workspace create --cwd %s --label sample --no-focus" % self.repo, out)
        self.assertIn("herdr agent start sample-orchestrator --kind claude --pane '<root pane of sample>' "
                      "-- --model claude-opus-5-5", out)
        # seat 1 occupies the new root pane, so seat 2 splits from it
        self.assertIn("herdr pane split '<root pane of sample>' --direction right", out)
        self.assertIn("herdr pane split '<pane of sample-dev>' --direction right", out)
        self.assertIn("reason up) to sample-orchestrator, sample-dev, sample-reviewer", out)
        self.assertIn("dry run: nothing was changed", out)


if __name__ == "__main__":
    unittest.main()
