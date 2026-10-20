"""Messages (design 16.1 to 16.3 and 16.7): send, the log, owed replies, the
send-only exception to the safety hook, and pane-move lines."""

from __future__ import annotations

import datetime
import json
import os
import unittest

import fake_herdr
from support import CALLER_PANE, FleetCase, LiveFleet
from herdr_fleet import messages


def log_entries(repo):
    path = os.path.join(repo, ".herdr", "messages.jsonl")
    if not os.path.exists(path):
        return []
    with open(path) as handle:
        return [json.loads(line) for line in handle if line.strip()]


class Send(LiveFleet):
    """Seats live in w2; the caller is the agent named "caller" in w1:p1."""

    def test_one_seat(self):
        code, out, err = self.run_cli("send", "fablyn-dev", "Review round 3: commits a1b2c3..d4e5f6\nDetails follow.")
        self.assertEqual(code, 0, out + err)
        text = "[from caller] Review round 3: commits a1b2c3..d4e5f6\nDetails follow."
        self.assertEqual(self.argvs("agent", "prompt"), [["agent", "prompt", "fablyn-dev", text]])
        self.assertEqual(out.strip(), "sent to fablyn-dev (idle), %d B" % len(text.encode()))
        self.assertIn(["pane", "current", "--current"], self.argvs("pane", "current"))  # sender from your pane
        (entry,) = log_entries(self.repo)
        self.assertEqual({k: entry[k] for k in ("from", "to", "kind", "bytes", "first_line")},
                         {"from": "caller", "to": "fablyn-dev", "kind": "send", "bytes": len(text.encode()),
                          "first_line": "Review round 3: commits a1b2c3..d4e5f6"})
        self.assertNotIn("file", entry)
        with open(os.path.join(self.repo, ".herdr", ".gitignore")) as handle:
            self.assertIn("messages.jsonl", handle.read().split())

    def test_working_target_gets_it_queued(self):
        self.status_of("w2:p2", "working")
        code, out, _ = self.run_cli("send", "fablyn-dev", "when you are free")
        self.assertEqual(code, 0, out)
        self.assertIn("sent to fablyn-dev (working)", out)
        self.assertEqual(len(self.prompts_to("fablyn-dev")), 1)  # the safety hook allows it for send only

    def test_blocked_unknown_and_absent_are_refused_and_not_logged(self):
        self.status_of("w2:p2", "blocked")
        self.status_of("w2:p3", "unknown")
        self.world["panes"]["w2:p1"]["agent"] = None
        code, out, _ = self.run_cli("send", "fablyn-dev", "fablyn-reviewer", "fablyn-orchestrator", "hello")
        self.assertEqual(code, 3)
        self.assertEqual(out.splitlines(), ["refused fablyn-dev (blocked): nothing sent",
                                            "refused fablyn-reviewer (unknown): nothing sent",
                                            "refused fablyn-orchestrator (absent): nothing sent"])
        self.assertEqual(self.argvs("agent", "prompt"), [])
        self.assertEqual(log_entries(self.repo), [])

    def test_some_sent_some_refused(self):
        self.status_of("w2:p3", "blocked")
        code, out, _ = self.run_cli("send", "fablyn-dev", "fablyn-reviewer", "x")
        self.assertEqual(code, 3)
        self.assertEqual([e["to"] for e in log_entries(self.repo)], ["fablyn-dev"])

    def test_sender_as_and_owner(self):
        self.run_cli("send", "--as", "fablyn-orchestrator", "fablyn-dev", "a")
        self.world["panes"][CALLER_PANE]["agent"]["name"] = None   # an unnamed agent in your pane
        self.run_cli("send", "fablyn-dev", "b")
        env = dict(self.env)
        del env["HERDR_PANE_ID"]                                   # not run from a pane at all
        self.run_cli("send", "fablyn-dev", "c", env=env)
        self.assertEqual(self.prompts_to("fablyn-dev"),
                         ["[from fablyn-orchestrator] a", "[from owner] b", "[from owner] c"])
        self.assertEqual(self.run_cli("send", "--as", "Not A Name", "fablyn-dev", "d")[0], 2)

    def test_existing_prefix_is_kept(self):
        self.run_cli("send", "fablyn-dev", "[from the owner] please stop")
        self.assertEqual(self.prompts_to("fablyn-dev"), ["[from the owner] please stop"])

    def test_file(self):
        brief = self.write_file("brief-3.md", "\n\nRound 3 brief\n" + "x" * 3000 + "\n")
        code, out, _ = self.run_cli("send", "--file", brief, "fablyn-dev", "fablyn-reviewer")
        self.assertEqual(code, 0, out)
        self.assertIn("KB", out)
        entries = log_entries(self.repo)
        self.assertEqual([(e["to"], e["file"], e["first_line"]) for e in entries],
                         [("fablyn-dev", brief, "Round 3 brief"), ("fablyn-reviewer", brief, "Round 3 brief")])
        self.assertEqual(self.run_cli("send", "--file", self.write_file("empty.md", " \n"), "fablyn-dev")[0], 2)
        self.assertEqual(self.run_cli("send", "--file", os.path.join(self.repo, "missing.md"), "fablyn-dev")[0], 2)

    def test_all_skips_the_sender(self):
        code, out, _ = self.run_cli("send", "--all", "--as", "fablyn-dev", "standup in 5")
        self.assertEqual(code, 0, out)
        self.assertEqual([a[2] for a in self.argvs("agent", "prompt")], ["fablyn-orchestrator", "fablyn-reviewer"])

    def test_live_agent_names_and_unknown_names(self):
        code, _, _ = self.run_cli("send", "--as", "fablyn-dev", "caller", "you are a live agent, not a seat")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.prompts_to("caller")), 1)
        code, _, err = self.run_cli("send", "fablyn-dev", "nobody", "hi")
        self.assertEqual(code, 2)
        self.assertIn("not a seat, contact or live agent: nobody", err)
        self.assertEqual(self.prompts_to("fablyn-dev"), [])  # nothing is sent when a name is unknown

    def test_usage_errors(self):
        self.assertEqual(self.run_cli("send", "fablyn-dev", "   ")[0], 2)
        self.assertEqual(self.run_cli("send", "only text")[0], 2)
        self.assertEqual(self.run_cli("send")[0], 2)

    def test_when_ready_waits_then_sends(self):
        self.status_of("w2:p2", "working")
        self.world["behaviors"]["wait_status"] = {"fablyn-dev": "idle"}
        code, out, _ = self.run_cli("send", "--when-ready", "5000", "fablyn-dev", "your turn")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.argvs("agent", "wait"), [["agent", "wait", "fablyn-dev", "--until", "idle",
                                                        "--until", "done", "--timeout", "5000"]])
        self.assertIn("sent to fablyn-dev (idle)", out)

    def test_when_ready_refuses_a_target_still_busy(self):
        self.status_of("w2:p2", "working")
        code, out, _ = self.run_cli("send", "--when-ready", "5000", "fablyn-dev", "your turn")
        self.assertEqual(code, 3)
        self.assertIn("refused fablyn-dev (working after --when-ready 5000 ms): nothing sent", out)
        self.assertEqual(self.prompts_to("fablyn-dev"), [])

    def test_stalled_prompt_is_reported_not_resent_not_logged(self):
        self.world["behaviors"]["prompt_errors"] = {"fablyn-dev": "agent_prompt_stalled"}
        code, out, _ = self.run_cli("send", "fablyn-dev", "fablyn-reviewer", "x")
        self.assertEqual(code, 1)
        self.assertIn("failed fablyn-dev: herdr error agent_prompt_stalled", out)
        self.assertIn("not resent", out)
        self.assertEqual(len(self.prompts_to("fablyn-dev")), 1)
        self.assertEqual([e["to"] for e in log_entries(self.repo)], ["fablyn-reviewer"])

    def test_dry_run(self):
        code, out, _ = self.run_cli("send", "--dry-run", "fablyn-dev", "x")
        self.assertEqual(code, 0)
        self.assertIn("would send to fablyn-dev (idle), 15 B", out)
        self.assertEqual(self.argvs("agent", "prompt"), [])
        self.assertEqual(log_entries(self.repo), [])


class LogCommand(LiveFleet):
    def test_log_lists_filters_and_skips_corrupt_lines(self):
        self.run_cli("send", "--as", "fablyn-orchestrator", "fablyn-dev", "first\nsecond line")
        self.run_cli("send", "--as", "fablyn-dev", "fablyn-orchestrator", "done")
        self.run_cli("send", "--as", "fablyn-orchestrator", "fablyn-reviewer", "review please")
        with open(os.path.join(self.repo, ".herdr", "messages.jsonl"), "a") as handle:
            handle.write("{not json\n")
        code, out, err = self.run_cli("log")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(len(lines), 3)
        self.assertRegex(lines[0], r"^\S+  fablyn-orchestrator → fablyn-dev  \d+  first$")
        self.assertIn("corrupt line skipped", err)
        self.assertEqual(len(self.run_cli("log", "--seat", "fablyn-reviewer")[1].splitlines()), 1)
        self.assertEqual(len(self.run_cli("log", "--last", "2")[1].splitlines()), 2)
        data = json.loads(self.run_cli("log", "--json")[1])
        self.assertEqual([e["to"] for e in data], ["fablyn-dev", "fablyn-orchestrator", "fablyn-reviewer"])
        owed = self.run_cli("log", "--owed")[1].splitlines()  # same-second sends: log order decides
        self.assertEqual(len(owed), 2)
        self.assertTrue(owed[0].startswith("awaiting: fablyn-orchestrator owes fablyn-dev since "))
        self.assertTrue(owed[0].endswith("(done)"))
        self.assertTrue(owed[1].startswith("awaiting: fablyn-reviewer owes fablyn-orchestrator since "))
        self.assertTrue(owed[1].endswith("(review please)"))

    def test_log_needs_no_herdr(self):
        before = len(self.calls())
        self.assertEqual(self.run_cli("log")[0], 0)
        self.assertEqual(len(self.calls()), before)


class KickoffsAreLogged(FleetCase):
    def test_up_logs_one_kickoff_per_seat(self):
        self.assertEqual(self.run_cli("up")[0], 0)
        entries = log_entries(self.repo)
        self.assertEqual([(e["from"], e["to"], e["kind"]) for e in entries],
                         [("herdr-fleet", n, "kickoff") for n in ("fablyn-orchestrator", "fablyn-dev",
                                                                  "fablyn-reviewer")])
        self.assertTrue(entries[0]["first_line"].startswith("You are fablyn-orchestrator"))
        self.assertEqual(messages.owed(entries), [])  # kickoffs never create an obligation


def entry(sender, to, minute, kind="send", first="m"):
    return {"time": "2026-10-20T10:%02d:00+11:00" % minute, "from": sender, "to": to, "kind": kind,
            "bytes": 1, "first_line": first}


class Owed(unittest.TestCase):
    def test_latest_message_awaits_unless_replied_later(self):
        log = [entry("orch", "dev", 1, first="task 1"), entry("dev", "orch", 2),
               entry("orch", "dev", 3, first="task 2"), entry("orch", "rev", 4, first="review"),
               entry("rev", "orch", 4), entry("herdr-fleet", "dev", 5, "kickoff")]
        self.assertEqual(messages.owed(log), [  # rev's reply at the same minute comes later in the log
            {"from": "orch", "to": "dev", "since": "2026-10-20T10:03:00+11:00", "first_line": "task 2"},
            {"from": "rev", "to": "orch", "since": "2026-10-20T10:04:00+11:00", "first_line": "m"},
        ])

    def test_equal_times_follow_log_order(self):
        same = [entry("a", "b", 1, first="ask"), entry("b", "a", 1, first="answer")]
        self.assertEqual([(o["from"], o["first_line"]) for o in messages.owed(same)], [("b", "answer")])

    def test_reply_settles_and_kickoffs_never_count(self):
        self.assertEqual(messages.owed([entry("a", "b", 1), entry("b", "a", 2), entry("a", "b", 1)]),
                         [{"from": "b", "to": "a", "since": "2026-10-20T10:02:00+11:00", "first_line": "m"}])
        self.assertEqual(messages.owed([entry("herdr-fleet", "b", 1, "kickoff")]), [])

    def test_lines(self):
        item = {"from": "fablyn-orchestrator", "to": "fablyn-reviewer", "since": "2026-10-20T10:31:04+11:00",
                "first_line": "Review round 3"}
        when = datetime.datetime.fromisoformat(item["since"]).astimezone()
        self.assertEqual(messages.owed_lines([item], today=when.date()),
                         ["awaiting: fablyn-reviewer owes fablyn-orchestrator since %s (Review round 3)"
                          % when.strftime("%H:%M")])
        self.assertIn(when.strftime("%Y-%m-%d"), messages.owed_lines([item], today=datetime.date(2000, 1, 1))[0])

    def test_first_line_and_sizes(self):
        self.assertEqual(messages.first_line("[from a] \n\n  hello world  \nmore"), "hello world")
        self.assertEqual(len(messages.first_line("[from a] " + "x" * 300)), 120)
        self.assertEqual((messages.size_label(999), messages.size_label(3412)), ("999 B", "3.4 KB"))


class StatusShowsOwed(LiveFleet):
    def test_status_lines_and_json(self):
        self.run_cli("send", "--as", "fablyn-orchestrator", "fablyn-reviewer", "Review round 3: commits a..b")
        code, out, _ = self.run_cli("status")
        lines = out.splitlines()
        owes = "awaiting: fablyn-reviewer owes fablyn-orchestrator"
        awaiting = [i for i, l in enumerate(lines) if l.startswith(owes)]
        integration = [i for i, l in enumerate(lines) if l.startswith("integration ")]
        self.assertEqual(len(awaiting), 1)
        self.assertLess(awaiting[0], integration[0])  # after the table, before the integrations
        data = json.loads(self.run_cli("status", "--json")[1])
        self.assertEqual([(o["from"], o["to"], o["first_line"]) for o in data["owed"]],
                         [("fablyn-orchestrator", "fablyn-reviewer", "Review round 3: commits a..b")])


class SafetyHookStaysStrict(LiveFleet):
    def test_a_prompt_to_a_working_agent_outside_send_still_fails(self):
        with open(self.log_path, "a") as handle:
            handle.write(json.dumps({"command": ["kickoff", "fablyn-dev"]}) + "\n")
            handle.write(json.dumps({"argv": ["agent", "prompt", "fablyn-dev", "x"], "exit": 0,
                                     "info": {"target_status": "working"}}) + "\n")
        with self.assertRaises(AssertionError):
            self.assert_safety()
        os.remove(self.log_path)

    def test_blocked_is_never_allowed_even_for_send(self):
        with open(self.log_path, "a") as handle:
            handle.write(json.dumps({"command": ["send", "fablyn-dev", "x"]}) + "\n")
            handle.write(json.dumps({"argv": ["agent", "prompt", "fablyn-dev", "x"], "exit": 1,
                                     "info": {"target_status": "blocked"}}) + "\n")
        with self.assertRaises(AssertionError):
            self.assert_safety()
        os.remove(self.log_path)


class PaneMoveLines(LiveFleet):
    def test_a_seat_found_in_another_pane_is_reported(self):
        self.world["panes"]["w2:p2"].update(agent=None, procs=[dict(fake_herdr.SHELL)])
        fake_herdr.add_pane(self.world, "w2:p9", "w2", self.repo)
        fake_herdr.put_agent(self.world, "w2:p9", "claude", "fablyn-dev")
        code, out, _ = self.run_cli("up")
        self.assertIn("fablyn-dev: pane w2:p2 → w2:p9", out)

    def test_restart_follows_the_live_pane(self):
        del self.world["panes"]["w2:p2"]
        fake_herdr.add_workspace(self.world, "w3", "other", self.repo)
        fake_herdr.add_pane(self.world, "w3:p4", "w3", self.repo)
        fake_herdr.put_agent(self.world, "w3:p4", "claude", "fablyn-dev", args=["--model", "claude-opus-5-5"])
        code, out, _ = self.run_cli("restart", "fablyn-dev")
        self.assertEqual(code, 0, out)
        self.assertIn("fablyn-dev: pane w2:p2 → w3:p4", out)


class JoinPrintsTheMove(FleetCase):
    def test_join(self):
        self.fleet_ws("w2", "fablyn")
        code, out, _ = self.run_cli("up", "--me", "fablyn-orchestrator", "--join")
        self.assertEqual(code, 0, out)
        self.assertIn("fablyn-orchestrator: pane w1:p1 → %s" % self.pane_of("fablyn-orchestrator"), out)


if __name__ == "__main__":
    unittest.main()
