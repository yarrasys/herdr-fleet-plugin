"""Messages (design 16.1 to 16.3, 16.7 and 17.1): send, the log, owed replies,
FYI messages, ack, the send-only exception to the safety hook, and pane-move
lines."""

from __future__ import annotations

import datetime
import io
import json
import os
import re
import unittest

import fake_herdr
from support import policy_output, CALLER_PANE, FleetCase, LiveFleet
from herdr_fleet import messages
from herdr_fleet.cli import main


def log_entries(repo):
    path = os.path.join(repo, ".herdr", "messages.jsonl")
    if not os.path.exists(path):
        return []
    with open(path) as handle:
        return [json.loads(line) for line in handle if line.strip()]


ID_PREFIX = re.compile(r"^\[from ([^\]#]*?) #[0-9a-f]{6}\]")


def strip_ids(texts):
    """``[from X #3f2a9c] text`` -> ``[from X] text`` (design 18.3 adds the short id)."""
    return [ID_PREFIX.sub(r"[from \1]", text) for text in texts]


class Send(LiveFleet):
    """Seats live in w2; the caller is the agent named "caller" in w1:p1."""

    def test_one_seat(self):
        code, out, err = self.run_cli("send", "sample-dev", "Please review the parser example\nDetails follow.")
        self.assertEqual(code, 0, out + err)
        (entry,) = log_entries(self.repo)
        short = entry["id"][-6:]
        text = "[from caller #%s] Please review the parser example\nDetails follow." % short
        self.assertEqual(self.argvs("agent", "prompt"), [["agent", "prompt", "sample-dev", text]])
        self.assertEqual(policy_output(out).strip(), "sent to sample-dev (idle), %d B" % len(text.encode()))
        self.assertIn(["pane", "current", "--current"], self.argvs("pane", "current"))  # sender from your pane
        self.assertEqual({k: entry[k] for k in ("from", "to", "kind", "bytes", "first_line", "delivery")},
                         {"from": "caller", "to": "sample-dev", "kind": "send", "bytes": len(text.encode()),
                          "first_line": "Please review the parser example", "delivery": "sent"})
        self.assertNotIn("file", entry)
        self.assertTrue(os.path.isfile(os.path.join(self.repo, ".herdr", "inbox", "sample-dev", "cur",
                                                    entry["id"] + ".md")))  # direct sends land in cur/
        with open(os.path.join(self.repo, ".herdr", ".gitignore")) as handle:
            self.assertEqual(handle.read().split(), ["fleet.state.json", "messages.jsonl", "inbox/", "watch.lock",
                                                     "watch.log", "timers.state.json", "timers.lock", "handover/"])

    def test_working_target_gets_it_queued(self):  # design 21.1: typed ahead only with --now
        self.status_of("w2:p2", "working")
        code, out, _ = self.run_cli("send", "sample-dev", "when you are free")
        self.assertEqual(code, 0, out)
        self.assertRegex(policy_output(out).strip(), r"^queued in herdr-fleet for sample-dev \(working\) #[0-9a-f]{6}, "
                                      r"position 1 of 1; lands at its next idle, or now with --now$")  # design 23.1
        self.assertEqual(self.prompts_to("sample-dev"), [])
        code, out, _ = self.run_cli("send", "--now", "sample-dev", "now please")
        self.assertEqual(code, 0, out)
        self.assertIn("sent to sample-dev (working: typed into its own input, read after its current turn) "
                      "with 1 queued", out)
        self.assertEqual(len(self.prompts_to("sample-dev")), 1)  # the safety hook allows it for --now only

    def test_blocked_unknown_and_absent_are_queued_and_nothing_is_sent(self):  # design 18.3, third row
        self.status_of("w2:p2", "blocked")
        self.status_of("w2:p3", "unknown")
        self.world["panes"]["w2:p1"]["agent"] = None
        code, out, _ = self.run_cli("send", "sample-dev", "sample-reviewer", "sample-orchestrator", "hello")
        self.assertEqual(code, 0)
        entries = log_entries(self.repo)
        self.assertEqual(policy_output(out).splitlines(), [  # design 20.3: the line names the place and the position
            "queued in herdr-fleet for sample-dev (blocked: a dialog is open) #%s, position 1 of 1"  # design 23.5
            % entries[0]["id"][-6:],
            "queued in herdr-fleet for sample-reviewer (unknown) #%s, position 1 of 1" % entries[1]["id"][-6:],
            "queued in herdr-fleet for sample-orchestrator (absent) #%s, position 1 of 1" % entries[2]["id"][-6:]])
        self.assertEqual(self.argvs("agent", "prompt"), [])
        self.assertEqual([(e["to"], e["delivery"]) for e in entries],
                         [("sample-dev", "queued"), ("sample-reviewer", "queued"), ("sample-orchestrator", "queued")])

    def test_some_sent_some_queued(self):
        self.status_of("w2:p3", "blocked")
        code, out, _ = self.run_cli("send", "sample-dev", "sample-reviewer", "x")
        self.assertEqual(code, 0)
        self.assertEqual([(e["to"], e["delivery"]) for e in log_entries(self.repo)],
                         [("sample-dev", "sent"), ("sample-reviewer", "queued")])

    def test_sender_as_and_owner(self):
        self.run_cli("send", "--as", "sample-orchestrator", "sample-dev", "a")
        self.world["panes"][CALLER_PANE]["procs"] = [dict(fake_herdr.SHELL)]
        self.world["panes"][CALLER_PANE]["agent"] = None           # a shell in your pane (design 23.3)
        self.run_cli("send", "sample-dev", "b")
        env = dict(self.env)
        del env["HERDR_PANE_ID"]                                   # not run from a pane at all
        self.run_cli("send", "--as", "owner", "sample-dev", "c", env=env)
        self.assertEqual(strip_ids(self.prompts_to("sample-dev")),
                         ["[from sample-orchestrator] a", "[from owner] b", "[from owner] c"])
        self.assertEqual(self.run_cli("send", "--as", "Not A Name", "sample-dev", "d")[0], 2)

    def test_existing_prefix_is_kept(self):
        self.run_cli("send", "sample-dev", "[from the owner] please stop")
        self.assertEqual(self.prompts_to("sample-dev"), ["[from the owner] please stop"])

    def test_file(self):
        brief = self.write_file("brief-3.md", "\n\nRound 3 brief\n" + "x" * 3000 + "\n")
        code, out, _ = self.run_cli("send", "--file", brief, "sample-dev", "sample-reviewer")
        self.assertEqual(code, 0, out)
        self.assertIn("KB", out)
        entries = log_entries(self.repo)
        self.assertEqual([(e["to"], e["file"], e["first_line"]) for e in entries],
                         [("sample-dev", brief, "Round 3 brief"), ("sample-reviewer", brief, "Round 3 brief")])
        self.assertEqual(self.run_cli("send", "--file", self.write_file("empty.md", " \n"), "sample-dev")[0], 2)
        self.assertEqual(self.run_cli("send", "--file", os.path.join(self.repo, "missing.md"), "sample-dev")[0], 2)

    def test_all_skips_the_sender(self):
        code, out, _ = self.run_cli("send", "--all", "--as", "sample-dev", "standup in 5")
        self.assertEqual(code, 0, out)
        self.assertEqual([a[2] for a in self.argvs("agent", "prompt")], ["sample-orchestrator", "sample-reviewer"])

    def test_live_agent_names_and_unknown_names(self):
        self.world["panes"]["w1:p1"]["agent"]["agent_status"] = "idle"
        code, _, _ = self.run_cli("send", "--as", "sample-dev", "caller", "you are a live agent, not a seat")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.prompts_to("caller")), 1)
        code, _, err = self.run_cli("send", "sample-dev", "nobody", "hi")
        self.assertEqual(code, 2)
        self.assertIn("not a seat, contact or live agent: nobody", err)
        self.assertEqual(self.prompts_to("sample-dev"), [])  # nothing is sent when a name is unknown

    def test_usage_errors(self):
        self.assertEqual(self.run_cli("send", "sample-dev", "   ")[0], 2)
        self.assertEqual(self.run_cli("send", "only text")[0], 2)
        self.assertEqual(self.run_cli("send")[0], 2)

    def test_when_ready_waits_then_sends(self):
        self.status_of("w2:p2", "working")
        self.world["behaviors"]["wait_status"] = {"sample-dev": "idle"}
        code, out, _ = self.run_cli("send", "--when-ready", "5000", "sample-dev", "your turn")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.argvs("agent", "wait"), [["agent", "wait", "sample-dev", "--until", "idle",
                                                        "--until", "done", "--timeout", "5000"]])
        self.assertIn("sent to sample-dev (idle)", out)

    def test_when_ready_queues_for_a_target_still_busy(self):
        self.status_of("w2:p2", "working")
        code, out, _ = self.run_cli("send", "--when-ready", "5000", "sample-dev", "your turn")
        self.assertEqual(code, 0)
        self.assertRegex(policy_output(out), r"queued in herdr-fleet for sample-dev \(working after --when-ready 5000 ms\) "
                              r"#[0-9a-f]{6}, position 1 of 1")
        self.assertEqual(self.prompts_to("sample-dev"), [])
        code, out, _ = self.run_cli("send", "--now", "--when-ready", "5000", "sample-dev", "your turn")
        self.assertEqual(code, 1)  # --now keeps the 16.1 refusal
        self.assertIn("refused sample-dev (working after --when-ready 5000 ms): nothing sent", out)
        self.assertEqual(self.prompts_to("sample-dev"), [])

    def test_stalled_prompt_is_reported_not_resent_and_logged_unconfirmed(self):
        self.world["behaviors"]["prompt_errors"] = {"sample-dev": "agent_prompt_stalled"}
        code, out, _ = self.run_cli("send", "sample-dev", "sample-reviewer", "x")
        self.assertEqual(code, 1)
        self.assertIn("unconfirmed sample-dev, 23 B: herdr error agent_prompt_stalled", out)
        self.assertIn("not resent; logged as unconfirmed", out)
        self.assertEqual(len(self.prompts_to("sample-dev")), 1)
        self.assertEqual([(e["to"], e["delivery"]) for e in log_entries(self.repo)],
                         [("sample-dev", "unconfirmed"), ("sample-reviewer", "sent")])

    def test_timeout_is_unconfirmed_too_and_other_errors_leave_it_queued(self):
        self.world["behaviors"]["prompt_errors"] = {"sample-dev": "timeout", "sample-reviewer": "agent_not_found"}
        code, out, _ = self.run_cli("send", "sample-dev", "sample-reviewer", "x")
        self.assertEqual(code, 1)
        self.assertRegex(policy_output(out), r"failed sample-reviewer: herdr error agent_not_found: .*; #[0-9a-f]{6} left queued")
        self.assertEqual([(e["to"], e["delivery"]) for e in log_entries(self.repo)],
                         [("sample-dev", "unconfirmed"), ("sample-reviewer", "queued")])

    def test_dry_run(self):
        code, out, _ = self.run_cli("send", "--dry-run", "sample-dev", "x")
        self.assertEqual(code, 0)
        self.assertIn("would send to sample-dev (idle), 23 B", out)  # [from caller #3f2a9c] x
        self.assertEqual(self.argvs("agent", "prompt"), [])
        self.assertEqual(log_entries(self.repo), [])
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".herdr", "inbox")))  # --dry-run writes nothing


class SendArguments(LiveFleet):
    """0.4.2: options may sit anywhere among the positional items (argparse splits them)."""

    def test_option_between_target_and_text(self):  # the live bug
        code, out, err = self.run_cli("send", "sample-dev", "--dry-run", "Example review is ready")
        self.assertEqual(code, 0, out + err)
        self.assertIn("would send to sample-dev (idle)", out)

    def test_options_before_between_and_after(self):
        for argv in (["--as", "owner", "sample-dev", "sample-reviewer", "hi"],
                     ["sample-dev", "--as", "owner", "sample-reviewer", "hi"],
                     ["sample-dev", "sample-reviewer", "--as", "owner", "hi"],
                     ["sample-dev", "sample-reviewer", "hi", "--as", "owner"]):
            self.assertEqual(self.run_cli("send", *argv)[0], 0, argv)
        self.assertEqual(strip_ids(self.prompts_to("sample-dev")), ["[from owner] hi"] * 4)
        self.assertEqual(strip_ids(self.prompts_to("sample-reviewer")), ["[from owner] hi"] * 4)

    def test_all_with_only_a_text(self):
        code, out, _ = self.run_cli("send", "--all", "--as", "sample-dev", "standup")
        self.assertEqual(code, 0, out)
        self.assertEqual([a[2] for a in self.argvs("agent", "prompt")], ["sample-orchestrator", "sample-reviewer"])
        self.assertEqual(strip_ids(self.prompts_to("sample-reviewer")), ["[from sample-dev] standup"])

    def test_file_with_several_targets_around_options(self):
        brief = self.write_file("brief.md", "Brief\n")
        code, out, _ = self.run_cli("send", "sample-dev", "--file", brief, "sample-reviewer", "--as", "owner",
                                    "sample-orchestrator")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.argvs("agent", "prompt")), 3)

    def test_two_items_minimum_without_file(self):
        code, _, err = self.run_cli("send", "sample-dev")
        self.assertEqual(code, 2)
        self.assertIn("send needs a target and a text, or --file", err)
        self.assertEqual(self.run_cli("send", "--dry-run", "only text")[0], 2)
        self.assertEqual(self.run_cli("send", "--file", self.write_file("b.md", "x\n"))[0], 2)  # no target
        self.assertEqual(self.argvs("agent", "prompt"), [])

    def test_double_dash_and_unknown_options(self):
        code, out, _ = self.run_cli("send", "sample-dev", "--dry-run", "--", "- a bullet")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.run_cli("send", "sample-dev", "--bogus", "x")[0], 2)

    def test_other_commands_with_name_lists_and_state_set(self):
        code, out, _ = self.run_cli("restart", "sample-dev", "--dry-run", "sample-reviewer")
        self.assertEqual(code, 0, out)
        self.assertIn("quit sample-reviewer", out)
        self.assertEqual(self.run_cli("status", "stray")[0], 2)
        self.write_fleet('state_note = ".herdr/STATE.md"\n' + self.read_fleet())
        fleet = os.path.join(self.repo, ".herdr", "fleet.toml")
        self.assertEqual(self.run_cli("state", "set", "next", "--fleet", fleet, "ship it")[0], 0)
        with open(os.path.join(self.repo, ".herdr", "STATE.md")) as handle:
            self.assertIn("ship it", handle.read())


class LogCommand(LiveFleet):
    def test_log_lists_filters_and_skips_corrupt_lines(self):
        self.run_cli("send", "--as", "sample-orchestrator", "sample-dev", "first\nsecond line")
        self.run_cli("send", "--as", "sample-dev", "sample-orchestrator", "done")
        self.run_cli("send", "--as", "sample-orchestrator", "sample-reviewer", "review please")
        with open(os.path.join(self.repo, ".herdr", "messages.jsonl"), "a") as handle:
            handle.write("{not json\n")
        code, out, err = self.run_cli("log")
        self.assertEqual(code, 0)
        lines = policy_output(out).splitlines()
        self.assertEqual(len(lines), 3)
        self.assertRegex(lines[0], r"^\S+  sample-orchestrator → sample-dev  \d+  #[0-9a-f]{6}  first$")
        self.assertIn("corrupt line skipped", err)
        self.assertEqual(len(self.run_cli("log", "--seat", "sample-reviewer")[1].splitlines()), 1)
        self.assertEqual(len(self.run_cli("log", "--last", "2")[1].splitlines()), 2)
        data = json.loads(self.run_cli("log", "--json")[1])
        self.assertEqual([e["to"] for e in data], ["sample-dev", "sample-orchestrator", "sample-reviewer"])
        owed = self.run_cli("log", "--owed")[1].splitlines()  # same-second sends: log order decides
        self.assertEqual(len(owed), 2)
        self.assertTrue(owed[0].startswith("awaiting: sample-orchestrator owes sample-dev for "))
        self.assertTrue(owed[0].endswith("): done"))
        self.assertTrue(owed[1].startswith("awaiting: sample-reviewer owes sample-orchestrator for "))
        self.assertTrue(owed[1].endswith("): review please"))

    def test_log_needs_no_herdr(self):
        before = len(self.calls())
        self.assertEqual(self.run_cli("log")[0], 0)
        self.assertEqual(len(self.calls()), before)


class Unconfirmed(LiveFleet):
    def test_log_marks_unconfirmed_and_owed_counts_them(self):
        self.world["behaviors"]["prompt_errors"] = {"sample-reviewer": "agent_prompt_stalled"}
        self.run_cli("send", "--as", "sample-orchestrator", "sample-reviewer", "Review parser tests")
        self.world["behaviors"]["prompt_errors"] = {}
        self.run_cli("send", "--as", "sample-orchestrator", "sample-dev", "Task 4")
        lines = self.run_cli("log")[1].splitlines()
        self.assertRegex(lines[0], r"  sample-orchestrator → sample-reviewer  \d+\?  #[0-9a-f]{6}  Review parser tests$")
        self.assertRegex(lines[1], r"  sample-orchestrator → sample-dev  \d+  #[0-9a-f]{6}  Task 4$")
        owed = [(o["to"], o["first_line"]) for o in json.loads(self.run_cli("log", "--owed", "--json")[1])]
        self.assertEqual(owed, [("sample-reviewer", "Review parser tests"), ("sample-dev", "Task 4")])

    def test_entries_written_before_0_4_1_read_as_sent(self):
        old = {"time": "2026-10-20T10:31:04+11:00", "from": "a", "to": "b", "kind": "send", "bytes": 3,
               "first_line": "hi"}
        self.assertEqual(messages.format_entry(old), "2026-10-20T10:31:04+11:00  a → b  3  hi")
        self.assertEqual(len(messages.owed([old])), 1)


class FleetOption(LiveFleet):
    """--fleet PATH everywhere; --file stays an alias where it meant the fleet file."""

    def setUp(self):
        super().setUp()
        self.elsewhere = os.path.join(self.tmp, "elsewhere")  # no fleet file here or above
        os.makedirs(self.elsewhere)
        self.fleet_path = os.path.join(self.repo, ".herdr", "fleet.toml")
        self.write_fleet('state_note = ".herdr/STATE.md"\n' + self.read_fleet())

    def run_elsewhere(self, *argv):
        with open(self.log_path, "a") as handle:
            handle.write(json.dumps({"command": list(argv)}) + "\n")
        with open(self.world_path, "w") as handle:
            json.dump(self.world, handle)
        out, err = io.StringIO(), io.StringIO()
        code = main(list(argv), env=self.env, runner=fake_herdr.Fake(self.world_path, self.log_path, CALLER_PANE,
                                                                   fleet=self.fleet_path),
                    clock=self.clock, stdout=out, stderr=err, cwd=self.elsewhere)
        return code, out.getvalue(), err.getvalue()

    def test_every_fleet_command_takes_fleet(self):
        for argv in (["status", "--fleet", self.fleet_path], ["status", "--file", self.fleet_path],
                     ["log", "--fleet", self.fleet_path], ["doctor", "--fleet", self.fleet_path],
                     ["tile", "--dry-run", "--fleet", self.fleet_path],
                     ["kickoff", "sample-dev", "--fleet", self.fleet_path],
                     ["state", "keys", "--fleet", self.fleet_path], ["state", "show", "--fleet", self.fleet_path]):
            code, out, err = self.run_elsewhere(*argv)
            self.assertIn(code, (0,), (argv, out, err))
        self.assertEqual(self.run_elsewhere("status")[0], 2)  # and without it, nothing is found

    def test_send_and_state_set_keep_file_for_their_text(self):
        brief = self.write_file("brief.md", "Round 4 brief\n")
        code, out, _ = self.run_elsewhere("send", "--fleet", self.fleet_path, "--file", brief, "sample-dev")
        self.assertEqual(code, 0, out)
        self.assertEqual(strip_ids(self.prompts_to("sample-dev")), ["[from caller] Round 4 brief\n"])
        note = self.write_file("next.md", "ship it\n")
        code, out, _ = self.run_elsewhere("state", "set", "next", "--fleet", self.fleet_path, "--file", note)
        self.assertEqual(code, 0, out)
        with open(os.path.join(self.repo, ".herdr", "STATE.md")) as handle:
            self.assertIn("ship it", handle.read())

    def test_herdr_fleet_file_still_works_and_init_has_no_fleet_option(self):
        self.env["HERDR_FLEET_FILE"] = self.fleet_path
        self.assertEqual(self.run_elsewhere("send", "sample-dev", "via the environment")[0], 0)
        self.assertEqual(self.run_elsewhere("init", "--fleet", self.fleet_path)[0], 2)


class KickoffsAreLogged(FleetCase):
    def test_up_logs_one_kickoff_per_seat(self):
        self.assertEqual(self.run_cli("up")[0], 0)
        entries = log_entries(self.repo)
        self.assertEqual([(e["from"], e["to"], e["kind"], e["delivery"]) for e in entries],
                         [("herdr-fleet", n, "kickoff", "sent") for n in ("sample-orchestrator", "sample-dev",
                                                                          "sample-reviewer")])
        self.assertTrue(entries[0]["first_line"].startswith("You are sample-orchestrator"))
        self.assertEqual(messages.owed(entries), [])  # kickoffs never create an obligation

    def test_roster_tells_seats_to_send_reports_with_fyi(self):  # design 17.1
        self.assertEqual(self.run_cli("up")[0], 0)
        for name in ("sample-orchestrator", "sample-dev", "sample-reviewer"):
            (kickoff,) = self.prompts_to(name)
            self.assertIn("| sample-reviewer | codex |", kickoff)
            self.assertIn("status reports go with `--fyi`; a message that asks for something does not.", kickoff)


def entry(sender, to, minute, kind="send", first="m", fyi=False):
    item = {"time": "2026-10-20T10:%02d:00+11:00" % minute, "from": sender, "to": to, "kind": kind,
            "bytes": 1, "first_line": first}
    if fyi:
        item["fyi"] = True
    return item


class Owed(unittest.TestCase):
    def test_latest_message_awaits_unless_replied_later(self):
        log = [entry("orch", "dev", 1, first="task 1"), entry("dev", "orch", 2),
               entry("orch", "dev", 3, first="task 2"), entry("orch", "rev", 4, first="review"),
               entry("rev", "orch", 4)]
        self.assertEqual(messages.owed(log), [  # rev's reply at the same minute comes later in the log
            {"from": "orch", "to": "dev", "since": "2026-10-20T10:03:00+11:00", "first_line": "task 2"},
            {"from": "rev", "to": "orch", "since": "2026-10-20T10:04:00+11:00", "first_line": "m"},
        ])
        log.append(entry("herdr-fleet", "dev", 5, "kickoff"))  # design 20.8: a kickoff of dev settles dev's debts
        self.assertEqual([(o["from"], o["to"]) for o in messages.owed(log)], [("rev", "orch")])

    def test_equal_times_follow_log_order(self):
        same = [entry("a", "b", 1, first="ask"), entry("b", "a", 1, first="answer")]
        self.assertEqual([(o["from"], o["first_line"]) for o in messages.owed(same)], [("b", "answer")])

    def test_reply_settles_and_kickoffs_never_count(self):
        self.assertEqual(messages.owed([entry("a", "b", 1), entry("b", "a", 2), entry("a", "b", 1)]),
                         [{"from": "b", "to": "a", "since": "2026-10-20T10:02:00+11:00", "first_line": "m"}])
        self.assertEqual(messages.owed([entry("herdr-fleet", "b", 1, "kickoff")]), [])

    def test_lines(self):
        item = {"from": "sample-orchestrator", "to": "sample-reviewer", "since": "2026-10-20T10:31:04+11:00",
                "first_line": "Review parser tests"}
        when = datetime.datetime.fromisoformat(item["since"]).astimezone()
        later = when + datetime.timedelta(hours=5, minutes=12)
        self.assertEqual(messages.owed_lines([item], today=when.date(), now=later),
                         ["awaiting: sample-reviewer owes sample-orchestrator for 5h12m (no id, since %s): "
                          "Review parser tests" % when.strftime("%H:%M")])
        item["id"] = "20261019T233104Z-a1b2c3"
        self.assertEqual(messages.owed_lines([item], today=when.date(), now=later),
                         ["awaiting: sample-reviewer owes sample-orchestrator for 5h12m (#a1b2c3, since %s): "
                          "Review parser tests" % when.strftime("%H:%M")])
        self.assertIn(when.strftime("%Y-%m-%d"), messages.owed_lines([item], today=datetime.date(2000, 1, 1))[0])

    def test_first_line_and_sizes(self):
        self.assertEqual(messages.first_line("[from a] \n\n  hello world  \nmore"), "hello world")
        self.assertEqual(len(messages.first_line("[from a] " + "x" * 300)), 120)
        self.assertEqual((messages.size_label(999), messages.size_label(3412)), ("999 B", "3.4 KB"))


class OwedWithFyiAndAck(unittest.TestCase):
    """Design 17.1: an FYI never creates an obligation but still settles one; ack settles."""

    def test_fyi_never_owes(self):
        self.assertEqual(messages.owed([entry("dev", "orch", 1, first="cleanup done", fyi=True)]), [])

    def test_fyi_reply_settles(self):
        self.assertEqual(messages.owed([entry("orch", "dev", 1, first="task"),
                                        entry("dev", "orch", 2, first="FYI: committed", fyi=True)]), [])

    def test_a_later_fyi_from_the_asker_keeps_the_ask_open(self):
        owed = messages.owed([entry("orch", "dev", 1, first="task 4"),
                              entry("orch", "dev", 2, first="FYI: main is green", fyi=True)])
        self.assertEqual([(o["from"], o["to"], o["first_line"], o["since"][11:16]) for o in owed],
                         [("orch", "dev", "task 4", "10:01")])

    def test_ack_settles_and_never_owes(self):
        self.assertEqual(messages.owed([entry("orch", "dev", 1), entry("dev", "orch", 2, "ack", "ack")]), [])
        self.assertEqual(messages.owed([entry("dev", "orch", 1, "ack", "ack")]), [])
        early = messages.owed([entry("dev", "orch", 1, "ack", "ack"), entry("orch", "dev", 2, first="new task")])
        self.assertEqual([o["first_line"] for o in early], ["new task"])
        same_minute = messages.owed([entry("orch", "dev", 1), entry("dev", "orch", 1, "ack", "ack")])
        self.assertEqual(same_minute, [])  # log order decides within the same time

    def test_is_fyi(self):
        for text in ("FYI: holding", "fyi committed a1b2", "  Fyi: x", "[from the owner] FYI: main is green",
                     "[from a]   fYi done", "\nFYI: leading newline"):
            self.assertTrue(messages.is_fyi(text), text)
        for text in ("FYIs are not this", "fyi-ish", "Not FYI: x", "FYI", "[from a] please FYI: x", "fyi\tx"):
            self.assertFalse(messages.is_fyi(text), text)


class Fyi(LiveFleet):
    def test_flag_logs_fyi_and_keeps_it_queued_for_an_idle_seat(self):  # design 18.3: never wakes an idle seat
        code, out, _ = self.run_cli("send", "--fyi", "--as", "sample-dev", "sample-orchestrator", "cleanup done")
        self.assertEqual(code, 0, out)
        self.assertRegex(policy_output(out).strip(), r"^queued in herdr-fleet for sample-orchestrator \(idle, fyi: delivered with the "
                                      r"next plain send, kickoff or deliver\) #[0-9a-f]{6}, position 1 of 1$")
        self.assertEqual(self.prompts_to("sample-orchestrator"), [])
        (item,) = log_entries(self.repo)
        self.assertIs(item["fyi"], True)
        self.assertEqual((item["kind"], item["delivery"]), ("send", "queued"))
        self.assertEqual(json.loads(self.run_cli("log", "--owed", "--json")[1]), [])

    def test_fyi_to_a_working_seat_stays_queued_and_now_types_it_as_written(self):  # design 21.1
        self.status_of("w2:p1", "working")
        code, out, _ = self.run_cli("send", "--fyi", "--as", "sample-dev", "sample-orchestrator", "cleanup done")
        self.assertEqual(code, 0, out)
        self.assertIn("queued in herdr-fleet for sample-orchestrator (working, fyi: ", out)
        self.assertEqual(self.prompts_to("sample-orchestrator"), [])
        self.run_cli("inbox", "--as", "sample-orchestrator", "--read")
        code, out, _ = self.run_cli("send", "--fyi", "--now", "--as", "sample-dev", "sample-orchestrator", "pushed")
        self.assertEqual(code, 0, out)
        self.assertEqual(strip_ids(self.prompts_to("sample-orchestrator")), ["[from sample-dev] pushed"])

    def test_prefix_marks_fyi_and_other_messages_carry_no_key(self):
        self.run_cli("send", "--as", "sample-dev", "sample-orchestrator", "FYI: holding until review")
        self.run_cli("send", "--as", "sample-dev", "sample-orchestrator", "fyi committed a1b2c3")
        self.run_cli("send", "--as", "sample-dev", "sample-orchestrator", "[from the owner] Fyi: main is green")
        self.run_cli("send", "--as", "sample-dev", "sample-orchestrator", "FYIs pile up; can you look?")
        items = [i for i in log_entries(self.repo) if i["kind"] == "send"]
        self.assertEqual([i.get("fyi") for i in items], [True, True, True, None])
        self.assertNotIn("fyi", items[3])
        (prompt,) = self.prompts_to("sample-orchestrator")  # the three FYIs ride along with the request
        self.assertRegex(prompt, r"^\[from sample-dev at \d\d:\d\d #[0-9a-f]{6}\]\nFYI: holding until review\n\n")
        self.assertTrue(prompt.endswith("]\nFYIs pile up; can you look?"), prompt)
        owed = json.loads(self.run_cli("log", "--owed", "--json")[1])
        self.assertEqual([o["first_line"] for o in owed], ["FYIs pile up; can you look?"])

    def test_log_tags_fyi_and_a_report_settles(self):
        self.run_cli("send", "--as", "sample-orchestrator", "sample-dev", "Parser example review")
        self.run_cli("send", "--fyi", "--as", "sample-dev", "sample-orchestrator", "Task 4 committed")
        lines = self.run_cli("log")[1].splitlines()
        self.assertRegex(lines[0], r"  sample-orchestrator → sample-dev  \d+  #[0-9a-f]{6}  Parser example review$")
        self.assertRegex(lines[1], r"  sample-dev → sample-orchestrator  \d+  #[0-9a-f]{6}  \[queued\]  \[fyi\]  "
                                   r"Task 4 committed$")
        self.assertNotIn("awaiting:", self.run_cli("status")[1])  # a queued report still answers


class Ack(LiveFleet):
    def setUp(self):
        super().setUp()
        self.run_cli("send", "--as", "sample-orchestrator", "sample-dev", "Parser example review")
        self.run_cli("send", "--as", "owner", "sample-dev", "please check the example")
        self.world["panes"]["w1:p1"]["agent"]["agent_status"] = "idle"  # a working caller would only queue (21.1)
        self.run_cli("send", "--as", "sample-orchestrator", "caller", "are you there?")
        self.run_cli("send", "--as", "sample-reviewer", "caller", "parser review ready")
        self.world["panes"]["w1:p1"]["agent"]["agent_status"] = "working"
        self.sent = len(self.argvs("agent", "prompt"))

    def owed_pairs(self):
        return [(o["from"], o["to"]) for o in json.loads(self.run_cli("log", "--owed", "--json")[1])]

    def test_named_seats_with_as(self):
        before = len(self.calls())
        code, out, err = self.run_cli("ack", "--as", "sample-dev", "sample-orchestrator")
        self.assertEqual(code, 0, out + err)
        self.assertRegex(policy_output(out).strip(), r"^acked sample-orchestrator since \d\d:\d\d \(Parser example review\)$")
        self.assertEqual(len(self.calls()), before)  # with --as, herdr is not asked anything
        item = log_entries(self.repo)[-1]
        self.assertEqual({k: v for k, v in item.items() if k != "time"},
                         {"from": "sample-dev", "to": "sample-orchestrator", "kind": "ack", "bytes": 0,
                          "first_line": "ack", "delivery": "logged"})
        self.assertRegex(item["time"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$")
        self.assertNotIn(("sample-orchestrator", "sample-dev"), self.owed_pairs())
        self.assertIn(("owner", "sample-dev"), self.owed_pairs())
        self.assertRegex(self.run_cli("log")[1].splitlines()[-1], r"  sample-dev → sample-orchestrator  ack$")
        self.assertEqual(len(self.argvs("agent", "prompt")), self.sent)  # ack sends nothing

    def test_owner_and_options_anywhere(self):
        code, out, _ = self.run_cli("ack", "owner", "--as", "sample-dev", "sample-orchestrator")
        self.assertEqual(code, 0, out)
        self.assertEqual([l.split()[1] for l in policy_output(out).splitlines()], ["owner", "sample-orchestrator"])
        self.assertEqual([p for p in self.owed_pairs() if p[1] == "sample-dev"], [])

    def test_all_resolves_the_caller_from_its_pane(self):
        before = len(self.calls())
        code, out, err = self.run_cli("ack", "--all")
        self.assertEqual(code, 0, out + err)
        self.assertEqual([l.split()[1] for l in policy_output(out).splitlines()], ["sample-orchestrator", "sample-reviewer"])
        self.assertEqual([(e["from"], e["to"]) for e in log_entries(self.repo)[-2:]],
                         [("caller", "sample-orchestrator"), ("caller", "sample-reviewer")])
        self.assertEqual([p for p in self.owed_pairs() if p[1] == "caller"], [])
        ack_calls = [c["argv"] for c in self.calls()[before:]]
        self.assertEqual(ack_calls, [["agent", "list"], ["--version"], ["pane", "current", "--current"],
                                     ["agent", "get", "w1:p1"]])  # read-only: who is calling
        self.assertEqual(len(self.argvs("agent", "prompt")), self.sent)

    def test_all_plus_names_and_nothing_owed(self):
        code, out, _ = self.run_cli("ack", "--as", "sample-dev", "--all", "sample-reviewer")
        self.assertEqual(code, 0)
        self.assertEqual(policy_output(out).splitlines()[0], "nothing owed to sample-reviewer")
        self.assertEqual([l.split()[1] for l in policy_output(out).splitlines()[1:]], ["sample-orchestrator", "owner"])
        written = len(log_entries(self.repo))
        code, out, _ = self.run_cli("ack", "--as", "sample-dev", "--all")
        self.assertEqual((code, policy_output(out).strip()), (0, "nothing owed by sample-dev"))
        code, out, _ = self.run_cli("ack", "--as", "sample-dev", "sample-orchestrator")
        self.assertEqual((code, policy_output(out).strip()), (0, "nothing owed to sample-orchestrator"))
        self.assertEqual(len(log_entries(self.repo)), written)  # nothing owed: no entry

    def test_unknown_names_and_usage(self):
        written = len(log_entries(self.repo))
        code, _, err = self.run_cli("ack", "--as", "sample-dev", "sample-orchestrator", "nobody")
        self.assertEqual(code, 2)
        self.assertIn("not a seat, contact, message id or name in the message log: nobody", err)
        self.assertEqual(len(log_entries(self.repo)), written)  # nothing written when a name is unknown
        self.assertEqual(self.run_cli("ack", "--as", "sample-dev")[0], 2)
        self.assertEqual(self.run_cli("ack", "--as", "Not A Name", "owner")[0], 2)
        self.assertEqual(self.run_cli("ack", "--as", "sample-dev", "--bogus", "owner")[0], 2)

    def test_status_drops_what_was_acked(self):
        self.run_cli("ack", "--as", "sample-dev", "sample-orchestrator", "owner")
        out = self.run_cli("status")[1]
        self.assertNotIn("awaiting: sample-dev owes", out)
        self.assertIn("awaiting: caller owes sample-orchestrator", out)


class StatusShowsOwed(LiveFleet):
    def test_status_lines_and_json(self):
        self.run_cli("send", "--as", "sample-orchestrator", "sample-reviewer", "Review parser commits")
        code, out, _ = self.run_cli("status")
        lines = policy_output(out).splitlines()
        owes = "awaiting: sample-reviewer owes sample-orchestrator"
        awaiting = [i for i, l in enumerate(lines) if l.startswith(owes)]
        integration = [i for i, l in enumerate(lines) if l.startswith("integration ")]
        self.assertEqual(len(awaiting), 1)
        self.assertLess(awaiting[0], integration[0])  # after the table, before the integrations
        data = json.loads(self.run_cli("status", "--json")[1])
        self.assertEqual([(o["from"], o["to"], o["first_line"]) for o in data["owed"]],
                         [("sample-orchestrator", "sample-reviewer", "Review parser commits")])


class SafetyHookStaysStrict(LiveFleet):
    def test_a_prompt_to_a_working_agent_outside_send_still_fails(self):
        with open(self.log_path, "a") as handle:
            handle.write(json.dumps({"command": ["kickoff", "sample-dev"]}) + "\n")
            handle.write(json.dumps({"argv": ["agent", "prompt", "sample-dev", "x"], "exit": 0,
                                     "info": {"target_status": "working"}}) + "\n")
        with self.assertRaises(AssertionError):
            self.assert_safety()
        os.remove(self.log_path)

    def test_blocked_is_never_allowed_even_for_send(self):
        with open(self.log_path, "a") as handle:
            handle.write(json.dumps({"command": ["send", "sample-dev", "x"]}) + "\n")
            handle.write(json.dumps({"argv": ["agent", "prompt", "sample-dev", "x"], "exit": 1,
                                     "info": {"target_status": "blocked"}}) + "\n")
        with self.assertRaises(AssertionError):
            self.assert_safety()
        os.remove(self.log_path)


class PaneMoveLines(LiveFleet):
    def test_a_seat_found_in_another_pane_is_reported(self):
        self.world["panes"]["w2:p2"].update(agent=None, procs=[dict(fake_herdr.SHELL)])
        fake_herdr.add_pane(self.world, "w2:p9", "w2", self.repo)
        fake_herdr.put_agent(self.world, "w2:p9", "claude", "sample-dev")
        code, out, _ = self.run_cli("up")
        self.assertIn("sample-dev: pane w2:p2 → w2:p9", out)

    def test_restart_follows_the_live_pane(self):
        del self.world["panes"]["w2:p2"]
        fake_herdr.add_workspace(self.world, "w3", "other", self.repo)
        fake_herdr.add_pane(self.world, "w3:p4", "w3", self.repo)
        fake_herdr.put_agent(self.world, "w3:p4", "claude", "sample-dev", args=["--model", "claude-opus-5-5"])
        code, out, _ = self.run_cli("restart", "sample-dev")
        self.assertEqual(code, 0, out)
        self.assertIn("sample-dev: pane w2:p2 → w3:p4", out)


class JoinPrintsTheMove(FleetCase):
    def test_join(self):
        self.fleet_ws("w2", "sample")
        code, out, _ = self.run_cli("up", "--me", "sample-orchestrator", "--join")
        self.assertEqual(code, 0, out)
        self.assertIn("sample-orchestrator: pane w1:p1 → %s" % self.pane_of("sample-orchestrator"), out)


if __name__ == "__main__":
    unittest.main()
