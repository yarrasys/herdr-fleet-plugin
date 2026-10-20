"""status (design 4.2), doctor (4.7), the menu and ui (section 8), and how
the invoking directory and caller pane are found (section 2)."""

from __future__ import annotations

import io
import json
import os
import unittest

import fake_herdr
from support import CALLER_PANE, FleetCase, LiveFleet
from herdr_fleet.cli import main
from herdr_fleet.context import context_cwd, context_id, parse_context


class Status(LiveFleet):
    def test_table_states_drift_notes_and_integrations(self):
        self.write_fleet(self.read_fleet().replace(
            'name = "fablyn-reviewer"', 'name = "fablyn-reviewer"\nnotes = "reviews read-only"')
            + '\n[[agents]]\nname = "helper"\nkind = "pi"\n')
        self.world["panes"]["w2:p2"]["agent"]["agent_status"] = "working"
        self.world["panes"]["w2:p1"]["procs"][0]["argv"] = ["claude", "--model", "old"]
        code, out, _ = self.run_cli("status")
        self.assertEqual(code, 0, out)
        lines = out.splitlines()
        self.assertTrue(any(l.startswith("fablyn-dev ") and "working" in l for l in lines))
        self.assertTrue(any(l.startswith("helper ") and "absent" in l for l in lines))
        self.assertIn("args: running --model old, file says --model claude-opus-5-5", out)
        self.assertIn("reviews read-only", out)
        self.assertIn("integration pi: pi: outdated (v8 < v9)", out)
        self.assertIn("<- outdated", out)
        self.assertIn("integration claude: claude: current (v10)", out)
        self.assertEqual(self.argvs("agent", "prompt"), [])

    def test_json(self):
        code, out, _ = self.run_cli("status", "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual([s["name"] for s in data["seats"]], ["fablyn-orchestrator", "fablyn-dev", "fablyn-reviewer"])
        dev = data["seats"][1]
        self.assertEqual((dev["kind"], dev["live_kind"], dev["pane_id"], dev["state"], dev["drift"]),
                         ("claude", "claude", "w2:p2", "idle", []))
        self.assertEqual(data["workspace"]["workspace_id"], "w2")
        self.assertEqual({r["kind"]: r["status"] for r in data["integrations"]},
                         {"claude": "current", "codex": "current"})

    def test_startup_labels_follow_the_live_state(self):
        self.write_fleet(self.read_fleet() + '\n[[agents]]\nname = "helper"\nkind = "pi"\n')
        state = self.read_state()
        for name in ("fablyn-orchestrator", "fablyn-dev", "fablyn-reviewer"):
            state["agents"][name]["blocked_at_startup"] = True
        state["agents"]["helper"] = {"pane_id": None, "kind": "pi", "blocked_at_startup": True}
        self.write_state(state)
        self.status_of("w2:p1", "blocked")
        self.status_of("w2:p3", "unknown")
        code, out, _ = self.run_cli("status")
        self.assertEqual(code, 0, out)
        rows = {l.split()[0]: l for l in out.splitlines()[2:6]}
        self.assertIn("blocked (blocked at startup)", rows["fablyn-orchestrator"])
        self.assertIn("idle (kickoff pending)", rows["fablyn-dev"])
        self.assertNotIn("(", rows["fablyn-reviewer"])  # unknown: no label
        self.assertNotIn("(", rows["helper"])  # absent: no label
        for state_name in ("working", "done"):
            self.status_of("w2:p2", state_name)
            out = self.run_cli("status")[1]
            self.assertIn("%s (kickoff pending)" % state_name, out)
        # the flag alone decides nothing: a seat blocked later in its life is just blocked
        state = self.read_state()
        state["agents"]["fablyn-orchestrator"]["blocked_at_startup"] = False
        self.write_state(state)
        out = self.run_cli("status")[1]
        self.assertNotIn("blocked at startup", out)
        data = json.loads(self.run_cli("status", "--json")[1])
        self.assertEqual([s["blocked_at_startup"] for s in data["seats"]], [False, True, True, True])
        self.assertEqual(self.argvs("agent", "prompt"), [])

    def test_hold_waits_for_enter(self):
        code, out, _ = self.run_cli("status", "--hold", stdin="\n")
        self.assertEqual(code, 0)
        self.assertIn("Press Enter to close.", out)

    def test_hold_shows_errors_too(self):
        os.remove(os.path.join(self.repo, ".herdr", "fleet.toml"))
        code, out, err = self.run_cli("status", "--hold", stdin="\n")
        self.assertEqual(code, 2)
        self.assertIn("no .herdr/fleet.toml found", out)
        self.assertIn("Press Enter", out)


class Doctor(FleetCase):
    def test_all_good(self):
        code, out, _ = self.run_cli("doctor")
        self.assertEqual(code, 0, out)
        for check in ("ok    python", "ok    herdr version: 0.9.4", "ok    HERDR_ENV", "ok    herdr server",
                      "ok    fleet file", "ok    kinds: claude, codex", "ok    integration claude",
                      "ok    quit fablyn-reviewer: /quit (codex)", "ok    state file"):
            self.assertIn(check, out)

    def test_agy_seat_has_a_known_quit_command(self):
        self.write_fleet('[[agents]]\nname = "fleet-agy"\nkind = "agy"\n')
        self.world["integration"].append("antigravity-cli: current (v4) "
                                          "(/home/u/.gemini/config/hooks/herdr-agent-state.sh)")
        code, out, _ = self.run_cli("doctor")
        self.assertEqual(code, 0, out)
        self.assertIn("ok    quit fleet-agy: /exit (agy)", out)
        self.assertIn("ok    integration agy", out)

    def test_problems_are_reported_without_changing_anything(self):
        self.write_fleet('state_note = "docs/STATE.md"\n[[agents]]\nname = "a"\nkind = "amp"\n'
                         '[[agents]]\nname = "b"\nkind = "zz"\n[[agents]]\nname = "c"\nkind = "pi"\n')
        env = dict(self.env)
        del env["HERDR_ENV"]
        code, out, _ = self.run_cli("doctor", env=env)
        self.assertEqual(code, 0, out)  # warnings only
        self.assertIn("warn  HERDR_ENV", out)
        self.assertIn("warn  kinds", out)
        self.assertIn("'zz' is not a kind", out)
        self.assertIn("warn  quit a: no quit command for kind amp", out)
        self.assertIn("warn  integration pi: pi: outdated", out)
        self.assertIn("warn  state_note", out)
        read_only = (["--version"], ["agent", "list"], ["agent"], ["integration", "status"], ["plugin", "config-dir"])
        self.assertEqual([c["argv"] for c in self.calls() if c["argv"][:2] not in read_only], [])

    def test_failures(self):
        os.remove(os.path.join(self.repo, ".herdr", "fleet.toml"))
        self.world["version"] = "herdr 0.9.3"
        code, out, _ = self.run_cli("doctor", "--notify")
        self.assertEqual(code, 1)
        self.assertIn("fail  herdr version: 0.9.3 is older than 0.9.4", out)
        self.assertIn("fail  fleet file: no .herdr/fleet.toml found", out)
        self.assertEqual(self.world["notifications"][-1]["sound"], "request")
        self.assertIn("failed", self.world["notifications"][-1]["title"])


class Preconditions(FleetCase):
    def test_refuses_outside_herdr(self):
        env = {"PATH": os.environ.get("PATH", "")}
        code, _, err = self.run_cli("status", env=env)
        self.assertEqual(code, 1)
        self.assertIn("not running inside Herdr", err)
        self.assertEqual(self.calls(), [])

    def test_refuses_old_herdr(self):
        self.world["version"] = "herdr 0.9.3"
        code, _, err = self.run_cli("up")
        self.assertEqual(code, 1)
        self.assertIn("herdr 0.9.3 is too old", err)
        self.assertEqual(self.argvs("agent", "start"), [])

    def test_unreachable_server_surfaces_the_error_code(self):
        self.world["canned"] = [{"prefix": ["agent", "list"], "exit": 1,
                                 "stderr": {"error": {"code": "server_unavailable", "message": "no server running"}}}]
        code, _, err = self.run_cli("up")
        self.assertEqual(code, 1)
        self.assertIn("herdr error server_unavailable: no server running", err)
        self.assertNotIn("Traceback", err)


class Invocation(FleetCase):
    def test_action_discovers_from_the_focused_panes_cwd(self):
        sub = os.path.join(self.repo, "src")
        os.makedirs(sub)
        self.world["panes"][CALLER_PANE]["cwd"] = sub
        env = dict(self.env, HERDR_PLUGIN_ID="yarrasys.fleet", HERDR_PLUGIN_ACTION_ID="yarrasys.fleet.up")
        with open(self.world_path, "w") as handle:
            json.dump(self.world, handle)
        out = io.StringIO()
        code = main(["status", "--json"], env=env, runner=fake_herdr.Fake(self.world_path, self.log_path),
                    clock=self.clock, stdout=out, stderr=io.StringIO(), cwd="/")  # plugin root, not the repo
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["repo"], self.repo)

    def run_action(self, argv, context, cwd="/"):
        """Run as a manifest action does: cwd is the plugin root, ids come from herdr."""
        env = dict(self.env, HERDR_PLUGIN_ID="yarrasys.fleet", HERDR_PLUGIN_ACTION_ID="yarrasys.fleet.status",
                   HERDR_PLUGIN_CONTEXT_JSON=json.dumps(context))
        with open(self.world_path, "w") as handle:
            json.dump(self.world, handle)
        out = io.StringIO()
        code = main(argv, env=env, runner=fake_herdr.Fake(self.world_path, self.log_path), clock=self.clock,
                    stdout=out, stderr=io.StringIO(), cwd=cwd)
        return code, out.getvalue()

    def test_action_uses_focused_pane_cwd_from_context_without_pane_get(self):
        sub = os.path.join(self.repo, "src")
        os.makedirs(sub)
        # live 0.9.4 action context (design section 12); the pane's own cwd points elsewhere
        context = {"workspace_id": "w1", "workspace_label": "caller", "workspace_cwd": "/", "tab_id": "w1:t1",
                   "tab_label": "1", "focused_pane_id": CALLER_PANE, "focused_pane_cwd": sub,
                   "focused_pane_agent": "claude", "focused_pane_status": "working",
                   "invocation_source": "keybinding", "correlation_id": "c1"}
        self.world["panes"][CALLER_PANE]["cwd"] = "/"
        code, out = self.run_action(["status", "--json"], context)
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["repo"], self.repo)
        self.assertNotIn(["pane", "get", CALLER_PANE], self.argvs("pane", "get"))

    def test_action_falls_back_to_pane_get_without_focused_pane_cwd(self):
        self.world["panes"][CALLER_PANE]["cwd"] = self.repo
        context = {"focused_pane_id": CALLER_PANE, "workspace_id": "w1"}
        code, out = self.run_action(["status", "--json"], context)
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["repo"], self.repo)
        self.assertIn(["pane", "get", CALLER_PANE], self.argvs("pane", "get"))

    def test_context_json_supplies_the_caller_pane(self):
        ctx = parse_context(json.dumps({"workspace_id": "w3", "focused_pane": {"pane_id": "w3:p2"}}))
        self.assertEqual(context_id(ctx, "pane"), "w3:p2")
        self.assertEqual(context_id(ctx, "workspace"), "w3")
        self.assertEqual(context_id(parse_context('{"focused_pane_id": "w1:p4"}'), "pane"), "w1:p4")
        self.assertEqual(context_cwd({"focused_pane_cwd": "/a"}), "/a")
        self.assertEqual(context_cwd({"focused_pane": {"pane_id": "w1:p1", "cwd": "/b"}}), "/b")
        self.assertIsNone(context_cwd({"workspace_cwd": "/c"}))
        self.assertEqual(parse_context("not json"), {})

    def test_ui_discovery_failure_notifies_and_exits_2(self):
        os.remove(os.path.join(self.repo, ".herdr", "fleet.toml"))
        for entrypoint, env in (("status", dict(self.env, HERDR_PLUGIN_ACTION_ID="yarrasys.fleet.status")),
                                ("manage", self.env)):  # notifies even when not run as an action
            code, _, err = self.run_cli("ui", entrypoint, env=env)
            self.assertEqual(code, 2)
            note = self.world["notifications"][-1]
            self.assertEqual((note["title"], note["sound"]), ("Fleet %s failed" % entrypoint, "request"))
            self.assertIn("no .herdr/fleet.toml found", note["body"])
            self.assertIn("no .herdr/fleet.toml found", err)
        self.assertEqual(self.argvs("plugin", "pane"), [])

    def test_ui_precondition_failure_notifies_and_exits_2(self):
        self.world["version"] = "herdr 0.9.3"
        code, _, _ = self.run_cli("ui", "manage")
        self.assertEqual(code, 2)
        note = self.world["notifications"][-1]
        self.assertEqual((note["title"], note["sound"]), ("Fleet manage failed", "request"))
        self.assertIn("too old", note["body"])

    def test_up_notify_failure_notifies(self):
        os.remove(os.path.join(self.repo, ".herdr", "fleet.toml"))
        code, _, _ = self.run_cli("up", "--notify")
        self.assertEqual(code, 2)
        note = self.world["notifications"][-1]
        self.assertEqual((note["title"], note["sound"]), ("Fleet up failed", "request"))

    def test_no_notification_outside_herdr(self):
        os.remove(os.path.join(self.repo, ".herdr", "fleet.toml"))
        code, _, err = self.run_cli("ui", "status", env={"PATH": os.environ.get("PATH", "")})
        self.assertEqual(code, 2)
        self.assertIn("not running inside Herdr", err)
        self.assertEqual(self.calls(), [])

    def test_up_notify_summary(self):
        code, _, _ = self.run_cli("up", "--notify")
        self.assertEqual(code, 0)
        note = self.world["notifications"][-1]
        self.assertEqual((note["title"], note["sound"]), ("Fleet up: done", "done"))
        self.assertIn("fablyn-dev: started", note["body"])


class UiAndMenu(LiveFleet):
    def test_ui_opens_the_plugin_pane_at_the_repo_root(self):
        code, _, err = self.run_cli("ui", "manage")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.argvs("plugin", "pane", "open"),
                         [["plugin", "pane", "open", "--plugin", "yarrasys.fleet", "--entrypoint", "manage",
                           "--cwd", self.repo]])

    def test_menu_restart_one_seat(self):
        code, out, _ = self.run_cli("menu", stdin="2\n1\nfresh start\ny\n\n")
        self.assertEqual(code, 0, out)
        self.assertIn("2) fablyn-dev", out)
        self.assertEqual(self.prompts_to("fablyn-dev")[0], "/exit")
        self.assertIn("fresh start", self.prompts_to("fablyn-dev")[1])
        self.assertIn("Press Enter to close.", out)
        self.assertEqual(self.prompts_to("fablyn-orchestrator"), [])

    def test_menu_kickoff_all(self):
        code, out, _ = self.run_cli("menu", stdin="a\n3\n\ny\n\n")
        self.assertEqual(code, 0, out)
        for name in ("fablyn-orchestrator", "fablyn-dev", "fablyn-reviewer"):
            self.assertEqual(len(self.prompts_to(name)), 1)

    def test_menu_quit_and_decline_do_nothing(self):
        for stdin in ("q\n", "", "1\nq\n", "1\n4\nn\nn\n\n"):
            code, _, _ = self.run_cli("menu", stdin=stdin)
            self.assertEqual(code, 0)
        self.assertEqual(self.argvs("agent", "prompt"), [])


class InstallCli(FleetCase):
    fleet_text = None

    def test_links_and_is_idempotent(self):
        target = os.path.join(self.tmp, "bin")
        code, out, _ = self.run_cli("install-cli", "--dir", target)
        self.assertEqual(code, 0, out)
        link = os.path.join(target, "herdr-fleet")
        self.assertTrue(os.path.islink(link))
        self.assertTrue(os.path.realpath(link).endswith(os.path.join("bin", "herdr-fleet")))
        self.assertIn("plugin root:", out)
        self.assertIn("is not on PATH", out)
        code, out, _ = self.run_cli("install-cli", "--dir", target)
        self.assertEqual(code, 0)
        self.assertIn("already linked", out)
        os.remove(link)
        with open(link, "w") as handle:
            handle.write("x")
        self.assertEqual(self.run_cli("install-cli", "--dir", target)[0], 1)
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
