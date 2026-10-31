"""Design section 32 (version 0.17.0): switching a seat's agent.

32.1: a start that times out is classified (late, slow, never ran, something else), retried
once only after a process-info read showing the bare shell, and a failed start is announced.
32.2: a quit held by a dialog or menu waits out a short window, then names the dialog and
notifies the owner. 32.3: ``peek`` of a working seat falls back to the visible screen.
32.4: ``inbox --wait`` writes ``waiting.json``, and status and the board show the wait.
32.5: ``restart|swap NAME --handover`` opens a helper pane that swaps the caller's own seat
when its turn ends. 32.6: a kickoff that cannot be sent waits first in the seat's mailbox.
32.7: help that names flags as flags.

``tests/support.py`` checks every test's log as before, and also: a second ``agent start``
to a pane only after a ``process-info`` read showing a bare shell with no successful ``agent
get`` in between, never a third; the handover helper acting only for its recorded seat; and
``agent read --source visible`` only from ``peek`` and the board's ``p``."""

from __future__ import annotations

import datetime
import io
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

import fake_herdr
from support import ARGS, CALLER_PANE, FakeClock, LiveFleet, THREE_SEATS
from herdr_fleet import board, inbox, seats
from herdr_fleet.cli import build_parser
from herdr_fleet.inbox import CUR, NEW
from test_inbox import box, log_entries, put
from test_section25 import PageCase

SHORT = r"#[0-9a-f]{6}"
LOST_TWICE = ("sample-dev: failed - the start command did not run in w2:p2 twice, or the agent exited at once; a quit "
              "agent may have left mouse tracking on there (README: troubleshooting); run herdr-fleet up sample-dev")


class S32(LiveFleet):
    def starts(self, name):
        return [a for a in self.argvs("agent", "start") if a[2] == name]

    def notifications(self):
        return [(n["title"], n["sound"]) for n in self.world["notifications"]]

    def kickoffs_queued(self, name):
        found = []
        for ident in box(self.repo, name, NEW):
            with open(os.path.join(self.repo, ".herdr", "inbox", name, NEW, ident + ".md"), encoding="utf-8") as h:
                if "queued_by: kickoff" in h.read().split("\n\n", 1)[0].splitlines():
                    found.append(ident)
        return found


# -- 32.1 ---------------------------------------------------------------------------------------


class LostStart(S32):
    def test_the_start_passes_the_ready_timeout(self):
        code, out, _ = self.run_cli("restart", "sample-dev")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.starts("sample-dev"), [["agent", "start", "sample-dev", "--kind", "claude", "--pane",
                                                      "w2:p2", "--timeout", "15000", "--"] + ARGS])

    def test_a_lost_first_start_is_retried_once_and_briefed(self):
        self.world["behaviors"]["start_lost"] = {"sample-dev": 1}
        code, out, _ = self.run_cli("restart", "sample-dev")
        self.assertEqual(code, 0, out)
        self.assertIn("sample-dev: started - in w2:p2 (second attempt; the first start left a bare shell)", out)
        self.assertIn("sample-dev: briefed", out)
        self.assertEqual(len(self.starts("sample-dev")), 2)
        self.assertEqual(self.notifications(), [])

    def test_a_lost_second_start_fails_with_the_remedy_and_no_third(self):
        self.world["behaviors"]["start_lost"] = {"sample-dev": 5}
        code, out, _ = self.run_cli("restart", "sample-dev")
        self.assertEqual(code, 1, out)
        self.assertIn(LOST_TWICE + "\n", out)  # the remedy is already there: no second suffix
        self.assertEqual(len(self.starts("sample-dev")), 2)
        self.assertEqual(self.notifications(), [("Fleet: sample-dev did not start", "request")])
        self.assertIn("run herdr-fleet up sample-dev", self.world["notifications"][0]["body"])
        self.assertEqual(self.prompts_to("sample-dev"), ["/exit"])  # nothing typed into the bare shell

    def test_up_retries_too_and_notifies_a_failure(self):
        self.world["panes"]["w2:p2"]["agent"] = None
        self.world["panes"]["w2:p2"]["procs"] = [dict(fake_herdr.SHELL, cwd=self.repo)]
        self.world["behaviors"]["start_lost"] = {"sample-dev": 2}
        code, out, _ = self.run_cli("up", "sample-dev", "--no-watch")
        self.assertEqual(code, 1, out)
        self.assertIn(LOST_TWICE, out)
        self.assertNotIn("the pane is at a shell", out)
        self.assertEqual(self.notifications(), [("Fleet: sample-dev did not start", "request")])

    def test_a_slow_start_is_waited_for_and_briefed(self):
        self.world["behaviors"]["start_slow"] = {"sample-dev": 6}
        code, out, _ = self.run_cli("restart", "sample-dev")
        self.assertEqual(code, 0, out)
        self.assertIn("sample-dev: started - in w2:p2\n", out)
        self.assertIn("sample-dev: briefed", out)
        self.assertEqual(len(self.starts("sample-dev")), 1)

    def test_a_slow_start_past_the_deadline_fails_without_a_retry_and_queues_its_kickoff(self):
        self.world["behaviors"]["start_slow"] = {"sample-dev": 100000}
        code, out, _ = self.run_cli("restart", "sample-dev")
        self.assertEqual(code, 1, out)
        (queued,) = self.kickoffs_queued("sample-dev")  # review N5: it usually comes up later
        short = queued.rsplit("-", 1)[1]
        self.assertIn("sample-dev: failed - its process runs in w2:p2 but herdr has not seen it ready after 45 s; "
                      "look at the pane; its kickoff is queued (#%s) and goes when it is idle\n" % short, out)
        self.assertIn("sample-dev: kickoff queued #%s: delivered when sample-dev is next idle or done" % short, out)
        self.assertEqual(len(self.starts("sample-dev")), 1)
        self.assertGreaterEqual(self.clock.slept, seats.START_SLOW_S - 1)
        self.assertEqual(self.notifications(), [("Fleet: sample-dev did not start", "request")])

    def test_up_without_kickoff_keeps_the_remedies(self):
        self.world["panes"]["w2:p2"]["agent"] = None
        self.world["panes"]["w2:p2"]["procs"] = [dict(fake_herdr.SHELL, cwd=self.repo)]
        self.world["behaviors"]["start_not_ready"] = ["sample-dev"]
        code, out, _ = self.run_cli("up", "sample-dev", "--no-watch", "--no-kickoff")
        self.assertEqual(code, 3, out)
        self.assertIn("answer the prompt there, then run `herdr-fleet kickoff sample-dev`", out)
        self.assertEqual(self.kickoffs_queued("sample-dev"), [])

    def second_attempt_sees(self, procs, pending=None):
        """start_lost once; the retry's fresh process-info read finds ``procs`` (the first
        command ran late, or something else took the pane) instead of the bare shell."""
        self.world["behaviors"]["start_lost"] = {"sample-dev": 1}
        original = fake_herdr.Sim._pane_process_info
        reads = {"after_lost": 0}

        def process_info(sim, args):
            lost = not sim.w["behaviors"]["start_lost"].get("sample-dev")  # the first start was lost already
            if lost and args[-1:] == ["w2:p2"]:
                reads["after_lost"] += 1
                if reads["after_lost"] == 2:  # the classification read saw the shell; this is the retry's
                    sim.w["panes"]["w2:p2"]["procs"] = procs
                    if pending:
                        sim.w["panes"]["w2:p2"]["pending"] = pending
            return original(sim, args)
        return mock.patch.object(fake_herdr.Sim, "_pane_process_info", process_info)

    def test_the_second_attempt_finds_the_agent_coming_up_and_polls(self):
        proc = fake_herdr.agent_proc("claude", ARGS, self.repo)
        pending = {"agent": {"agent": "claude", "agent_status": "idle", "name": "sample-dev"}, "reads": 3}
        with self.second_attempt_sees([proc], pending):
            code, out, _ = self.run_cli("restart", "sample-dev")
        self.assertEqual(code, 0, out)
        self.assertIn("sample-dev: started - in w2:p2\n", out)
        self.assertIn("sample-dev: briefed", out)
        self.assertEqual(len(self.starts("sample-dev")), 1)

    def test_the_second_attempt_finds_something_else_and_stops(self):
        with self.second_attempt_sees([{"name": "less", "argv0": "less", "pid": 801}]):
            code, out, _ = self.run_cli("restart", "sample-dev")
        self.assertEqual(code, 1, out)
        self.assertIn("sample-dev: failed - pane w2:p2 is not at a shell prompt for the second start (foreground: "
                      "less); not retried\n", out)
        self.assertEqual(len(self.starts("sample-dev")), 1)

    def test_a_herdr_without_start_timeout_gets_the_start_without_it(self):
        self.world["behaviors"]["no_start_timeout"] = True
        code, out, _ = self.run_cli("restart", "sample-dev")
        self.assertEqual(code, 0, out)
        self.assertEqual([a for a in self.starts("sample-dev")],
                         [["agent", "start", "sample-dev", "--kind", "claude", "--pane", "w2:p2", "--timeout", "15000",
                           "--"] + ARGS, ["agent", "start", "sample-dev", "--kind", "claude", "--pane", "w2:p2",
                                          "--"] + ARGS])
        self.assertIn("sample-dev: briefed", out)

    def test_without_start_timeout_a_later_timeout_is_classified_as_before(self):
        self.world["behaviors"]["no_start_timeout"] = True
        self.world["behaviors"]["start_lost"] = {"sample-dev": 1}
        code, out, _ = self.run_cli("restart", "sample-dev")
        self.assertEqual(code, 0, out)
        self.assertIn("(second attempt; the first start left a bare shell)", out)
        plain = ["agent", "start", "sample-dev", "--kind", "claude", "--pane", "w2:p2", "--"] + ARGS
        self.assertEqual(self.starts("sample-dev")[1:], [plain, plain])  # no --timeout once herdr rejected it

    def test_a_timeout_with_a_blocked_agent_is_blocked_at_startup(self):
        self.world["behaviors"]["start_timeout"] = {"sample-dev": "blocked"}
        code, out, _ = self.run_cli("restart", "sample-dev")
        self.assertEqual(code, 3, out)
        self.assertIn("sample-dev: blocked - blocked at startup in w2:p2", out)
        self.assertEqual(len(self.starts("sample-dev")), 1)
        self.assertGreaterEqual(self.clock.slept, seats.START_SLOW_S - 1)
        self.assertTrue(self.read_state()["agents"]["sample-dev"]["blocked_at_startup"])

    def test_a_timeout_with_a_working_agent_is_a_start(self):
        self.world["behaviors"]["start_timeout"] = {"sample-dev": "idle"}
        code, out, _ = self.run_cli("restart", "sample-dev")
        self.assertEqual(code, 0, out)
        self.assertIn("sample-dev: started - in w2:p2\n", out)
        self.assertEqual(len(self.starts("sample-dev")), 1)

    def test_something_else_in_the_foreground_is_not_retried(self):
        self.world["behaviors"]["start_lost"] = {"sample-dev": 1}
        original = fake_herdr.Sim._agent_start

        def start(sim, args):
            result = original(sim, args)
            if sim.info.get("lost"):  # the garbled command left a pager running instead
                sim.w["panes"]["w2:p2"]["procs"] = [{"name": "less", "argv0": "less", "pid": 801}]
            return result
        with mock.patch.object(fake_herdr.Sim, "_agent_start", start):
            code, out, _ = self.run_cli("restart", "sample-dev")
        self.assertEqual(code, 1, out)
        self.assertIn("sample-dev: failed - herdr error timeout: timed out waiting for agent startup; foreground "
                      "in w2:p2: less; not retried\n", out)
        self.assertEqual(len(self.starts("sample-dev")), 1)

    def test_a_dry_run_plans_no_retry(self):
        code, out, _ = self.run_cli("restart", "sample-dev", "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.argvs("agent", "start"), [])


# -- 32.2 ---------------------------------------------------------------------------------------


class HeldQuit(S32):
    def test_a_menu_that_clears_within_the_window_lets_the_quit_go(self):
        fake_herdr.show_menu(self.world, "w2:p3", 4)  # the seat has just turned done: still repainting
        code, out, _ = self.run_cli("restart", "sample-reviewer")
        self.assertEqual(code, 0, out)
        self.assertIn("sample-reviewer: quit - quit with /quit", out)
        self.assertIn("sample-reviewer: started", out)
        self.assertLessEqual(self.clock.slept, 60)
        self.assertEqual(self.notifications(), [])

    def test_a_menu_that_stays_is_named_and_the_owner_told(self):
        fake_herdr.show_menu(self.world, "w2:p3", 1000)
        code, out, _ = self.run_cli("restart", "sample-reviewer")
        self.assertEqual(code, 3, out)
        self.assertIn('sample-reviewer: refused - a dialog or a menu is on screen in w2:p3 ("Approaching rate '
                      'limits"); answer or close it there, then run the command again\n', out)
        self.assertEqual(self.prompts_to("sample-reviewer"), [])
        self.assertEqual(self.argvs("agent", "read"), [["agent", "read", "sample-reviewer", "--lines", "16"]])
        self.assertEqual(self.notifications(), [("Fleet: sample-reviewer needs you", "request")])
        reads = len(self.argvs("pane", "read"))
        self.assertGreaterEqual(reads, 8)  # every 500 ms for 5 s
        self.assertLessEqual(reads, 12)

    def test_a_dialog_without_a_numbered_choice_keeps_todays_words(self):
        fake_herdr.show_menu(self.world, "w2:p2", 1000, "\n".join(["⏺ Working on it", "", "  Loading…"]))
        code, out, _ = self.run_cli("restart", "sample-dev")
        self.assertEqual(code, 3, out)
        self.assertIn("sample-dev: refused - a dialog or a menu is on screen in w2:p2; answer or close it there\n", out)
        self.assertEqual(self.notifications(), [("Fleet: sample-dev needs you", "request")])

    def test_text_in_the_input_is_still_refused_at_once(self):
        fake_herdr.type_in(self.world, "w2:p2", "half a sentence")
        code, out, _ = self.run_cli("restart", "sample-dev")
        self.assertEqual(code, 3, out)
        self.assertIn("sample-dev: refused - text in its input (w2:p2); press Ctrl-U there to clear it", out)
        self.assertEqual(len(self.argvs("pane", "read")), 1)
        self.assertEqual(self.argvs("agent", "read"), [])
        self.assertEqual(self.clock.slept, 0)
        self.assertEqual(self.notifications(), [])


# -- 32.3 ---------------------------------------------------------------------------------------


class ScreenFormat(S32):
    def test_a_herdr_without_format_ansi_is_read_as_plain_text(self):
        self.world["behaviors"]["no_ansi_format"] = True
        code, out, _ = self.run_cli("send", "sample-dev", "--as", "sample-orchestrator", "Look at #12")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.argvs("pane", "read"), [["pane", "read", "w2:p2", "--lines", "12", "--format", "ansi"],
                                                       ["pane", "read", "w2:p2", "--lines", "12"]])
        self.assertEqual(len(self.prompts_to("sample-dev")), 1)

    def test_any_other_read_error_sends_nothing(self):
        self.world["behaviors"]["pane_read_errors"] = {"w2:p2": "server_error"}
        code, out, _ = self.run_cli("restart", "sample-dev")
        self.assertEqual(code, 1, out)
        self.assertIn("sample-dev: failed - presence check: herdr error server_error", out)
        self.assertIn("; nothing sent", out)
        self.assertEqual(len(self.argvs("pane", "read")), 1)
        self.assertEqual(self.prompts_to("sample-dev"), [])


class QuitLock(S32):
    def test_a_delivery_during_the_quit_waits_and_rides_the_kickoff(self):
        import threading
        from herdr_fleet import messages, ops
        from herdr_fleet.context import Ctx
        from herdr_fleet.herdr import Herdr
        message = put(self.repo, "sample-dev", "Please look at the parser", sender="sample-reviewer")
        drops = []

        def deliver():  # what the watcher does for an idle seat, from another thread
            runner = fake_herdr.Fake(self.world_path, self.log_path, CALLER_PANE)
            ctx = Ctx(Herdr("herdr", runner), self.env, FakeClock(), io.StringIO(), io.StringIO(), self.repo)
            with inbox.locked(self.repo, "sample-dev"):
                drops.append(messages.prompt_mailbox(ctx, "sample-dev", "sample-dev", seats.SETTLED, "watch"))
        thread = threading.Thread(target=deliver)
        real_quit, real_start = ops.quit_agent, seats.start_in_pane

        def quit_agent(*args, **kwargs):
            thread.start()
            thread.join(0.5)  # it cannot take the mailbox while the quit holds it
            return real_quit(*args, **kwargs)

        def start_in_pane(*args, **kwargs):
            thread.join(10)  # the delivery goes first, to an agent that is gone
            return real_start(*args, **kwargs)
        with mock.patch("herdr_fleet.ops.quit_agent", quit_agent), \
                mock.patch("herdr_fleet.bringup.start_in_pane", start_in_pane):
            code, out, _ = self.run_cli("restart", "sample-dev")
        self.assertEqual(code, 0, out)
        self.assertEqual([d.status for d in drops], ["refused"])
        self.assertEqual(self.prompts_to("sample-dev")[0], "/exit")
        kickoff = self.prompts_to("sample-dev")[1]
        self.assertIn("Please look at the parser", kickoff)
        self.assertIn("sample-dev: briefed - kickoff sent (restart) with 1 queued", out)
        delivered = [e for e in log_entries(self.repo) if e["kind"] == "delivered" and e["id"] == message.id]
        self.assertEqual([e["via"] for e in delivered], ["kickoff"])


class PeekWorking(S32):
    def setUp(self):
        super().setUp()
        self.world["behaviors"]["agent_not_idle"] = True
        self.status_of("w2:p2", "working")

    def test_a_working_seat_falls_back_to_the_visible_screen(self):
        code, out, err = self.run_cli("peek", "sample-dev")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(out, fake_herdr.prompt_screen("claude"))
        self.assertEqual(err, "(working: the visible screen only)\n")
        self.assertEqual(self.argvs("agent", "read"), [["agent", "read", "sample-dev", "--lines", "30"],
                                                        ["agent", "read", "sample-dev", "--lines", "30",
                                                         "--source", "visible"]])

    def test_source_visible_reads_it_at_once(self):
        code, out, err = self.run_cli("peek", "sample-dev", "--source", "visible", "--lines", "5")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(err, "")
        self.assertEqual(self.argvs("agent", "read"), [["agent", "read", "sample-dev", "--lines", "5",
                                                        "--source", "visible"]])

    def test_source_recent_never_falls_back(self):
        code, out, err = self.run_cli("peek", "sample-dev", "--source", "recent")
        self.assertEqual(code, 1, out + err)
        self.assertIn("agent_not_idle", err)
        self.assertEqual(self.argvs("agent", "read"), [["agent", "read", "sample-dev", "--lines", "30",
                                                        "--source", "recent"]])

    def test_an_idle_seat_reads_as_before(self):
        self.status_of("w2:p2", "idle")
        code, out, err = self.run_cli("peek", "sample-dev")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.argvs("agent", "read"), [["agent", "read", "sample-dev", "--lines", "30"]])

    def test_interrupt_keeps_its_read(self):
        code, out, _ = self.run_cli("interrupt", "sample-dev")
        self.assertEqual(code, 1, out)
        self.assertIn("reading its screen", out)
        self.assertNotIn("--source", [a for argv in self.argvs("agent", "read") for a in argv])
        self.assertEqual(self.argvs("agent", "send-keys"), [])


# -- 32.4 ---------------------------------------------------------------------------------------


def waiting_file(repo, name):
    return os.path.join(repo, ".herdr", "inbox", name, "waiting.json")


class WatchClock(FakeClock):
    """Fake time that calls ``action`` at the ``at``-th sleep."""

    def __init__(self, at, action):
        super().__init__()
        self.at, self.action, self.sleeps = at, action, 0

    def sleep(self, seconds):
        super().sleep(seconds)
        self.sleeps += 1
        if self.sleeps == self.at:
            self.action()


def dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


class InboxWaitFile(S32):
    def seen_during(self, at, then=None):
        seen = {}

        def look():
            path = waiting_file(self.repo, "caller")
            seen["exists"] = os.path.exists(path)
            if seen["exists"]:
                with open(path, encoding="utf-8") as handle:
                    seen["data"] = json.load(handle)
            if then:
                then()
        self.clock = WatchClock(at, look)
        return seen

    def test_written_while_waiting_and_removed_on_reply(self):
        seen = self.seen_during(3, lambda: put(self.repo, "caller", "the reply", sender="sample-reviewer"))
        code, out, _ = self.run_cli("inbox", "--wait", "--timeout", "600000")
        self.assertEqual(code, 0, out)
        self.assertIn("the reply", out)
        self.assertTrue(seen["exists"])
        self.assertEqual(seen["data"]["pid"], os.getpid())
        self.assertEqual(seen["data"]["timeout_ms"], 600000)
        self.assertIsNotNone(inbox.parse_time(seen["data"]["since"]))
        self.assertFalse(os.path.exists(waiting_file(self.repo, "caller")))

    def test_removed_on_timeout(self):
        seen = self.seen_during(2)
        code, out, _ = self.run_cli("inbox", "--wait", "--timeout", "5000")
        self.assertEqual(code, 3, out)
        self.assertTrue(seen["exists"])
        self.assertFalse(os.path.exists(waiting_file(self.repo, "caller")))

    def test_removed_on_interrupt(self):
        def stop():
            raise KeyboardInterrupt
        seen = self.seen_during(2, stop)
        code, _, err = self.run_cli("inbox", "--wait")
        self.assertEqual(code, 130, err)
        self.assertTrue(seen["exists"])
        self.assertFalse(os.path.exists(waiting_file(self.repo, "caller")))

    def test_no_file_without_wait(self):
        self.run_cli("inbox")
        self.assertFalse(os.path.exists(waiting_file(self.repo, "caller")))

    def write_waiting(self, name, pid, minutes=3, timeout_ms=600000):
        since = inbox.iso_local(inbox.now_utc() - datetime.timedelta(minutes=minutes, seconds=5))
        os.makedirs(os.path.dirname(waiting_file(self.repo, name)), exist_ok=True)
        with open(waiting_file(self.repo, name), "w", encoding="utf-8") as handle:
            json.dump({"pid": pid, "since": since, "timeout_ms": timeout_ms}, handle)
        return since

    def test_status_json_and_board_show_the_wait(self):
        self.status_of("w2:p2", "working")
        since = self.write_waiting("sample-dev", os.getpid())
        code, out, _ = self.run_cli("status")
        self.assertEqual(code, 0, out)
        row = next(line for line in out.splitlines() if line.startswith("sample-dev "))
        self.assertIn("working (inbox wait 3m)", row)
        code, out, _ = self.run_cli("status", "--json")
        seat = next(s for s in json.loads(out)["seats"] if s["name"] == "sample-dev")
        self.assertEqual(seat["inbox_wait"], {"since": since})
        other = next(s for s in json.loads(out)["seats"] if s["name"] == "sample-orchestrator")
        self.assertNotIn("inbox_wait", other)
        fake_herdr.set_title(self.world, "w2:p2", "Refactoring the parser")
        model = self.board()
        row = next(r for r in model.rows if r.name == "sample-dev")
        self.assertEqual(row.doing, "waiting for mail 3m")

    def board(self):
        from herdr_fleet.context import Ctx
        from herdr_fleet.herdr import Herdr
        with open(self.world_path, "w", encoding="utf-8") as handle:
            json.dump(self.world, handle)
        runner = fake_herdr.Fake(self.world_path, self.log_path, CALLER_PANE)
        ctx = Ctx(Herdr("herdr", runner), self.env, self.clock, io.StringIO(), io.StringIO(), self.repo)
        return board.build(ctx)

    def test_a_dead_pid_an_expired_wait_and_an_idle_seat_are_ignored(self):
        self.status_of("w2:p2", "working")
        self.write_waiting("sample-dev", dead_pid())
        self.status_of("w2:p1", "working")
        self.write_waiting("sample-orchestrator", os.getpid(), minutes=30, timeout_ms=60000)
        self.write_waiting("sample-reviewer", os.getpid())  # idle: herdr's state wins
        code, out, _ = self.run_cli("status")
        self.assertEqual(code, 0, out)
        self.assertNotIn("inbox wait", out)
        code, out, _ = self.run_cli("status", "--json")
        self.assertFalse(any("inbox_wait" in s for s in json.loads(out)["seats"]))


# -- 32.5 ---------------------------------------------------------------------------------------

DRIFTED = THREE_SEATS.replace('name = "sample-orchestrator"\nkind = "claude"\nargs = ["--model", "claude-opus-5-5"]',
                              'name = "sample-orchestrator"\nkind = "codex"\nargs = ["-m", "gpt-6-astra"]')


class Handover(S32):
    def setUp(self):
        super().setUp()
        self.status_of("w2:p1", "working")  # the orchestrator runs the command inside its own turn
        self.env.update(HERDR_PANE_ID="w2:p1", HERDR_WORKSPACE_ID="w2")
        self.write_fleet(DRIFTED)

    def request(self, *extra):
        return self.run_cli("swap", "sample-orchestrator", "--handover", *extra)

    def helper_env(self):
        record = self.read_state()["handover"]
        pane = self.world["panes"][record["pane_id"]]
        env = dict(self.env, HERDR_PANE_ID=record["pane_id"], HERDR_WORKSPACE_ID="w2",
                   HERDR_PLUGIN_ENTRYPOINT_ID="handover")
        env.update(pane["env"])
        return env

    def test_it_opens_the_helper_in_a_tab_with_its_env_and_returns(self):
        code, out, _ = self.request()
        self.assertEqual(code, 0, out)
        opened = self.argvs("plugin", "pane", "open")
        self.assertEqual(len(opened), 1)
        argv = opened[0]
        for pair in (["--entrypoint", "handover"], ["--placement", "tab"], ["--workspace", "w2"],
                     ["--cwd", self.repo]):
            self.assertIn(pair, [argv[i:i + 2] for i in range(len(argv) - 1)])
        self.assertIn("--no-focus", argv)
        record = self.read_state()["handover"]
        pane = record["pane_id"]
        self.assertEqual((record["seat"], record["op"], record["created_pane"]), ("sample-orchestrator", "swap", True))
        env = self.world["panes"][pane]["env"]
        self.assertEqual(env["HERDR_FLEET_FILE"], os.path.join(self.repo, ".herdr", "fleet.toml"))
        self.assertEqual(json.loads(env["HERDR_FLEET_HANDOVER"]),
                         {"op": "swap", "seat": "sample-orchestrator", "wait_ms": 3600000})
        self.assertNotEqual(self.world["panes"][pane]["tab_id"], "w2:t1")
        self.assertEqual(out.splitlines()[-1],
                         "sample-orchestrator: handover - waiting in %s (new tab); you are swapped when this turn "
                         "ends, so end your turn now and do not start inbox --wait" % pane)
        self.assertEqual(self.argvs("agent", "prompt"), [])
        self.assertEqual(self.argvs("agent", "wait"), [])
        code, out, _ = self.run_cli("status")
        self.assertIn("handover: swap sample-orchestrator waiting in %s since " % pane, out)

    def test_a_note_and_wait_go_with_it(self):
        code, out, _ = self.run_cli("restart", "sample-orchestrator", "--handover", "--note", "New provider.",
                                    "--wait", "60000")
        self.assertEqual(code, 0, out)
        record = self.read_state()["handover"]
        data = json.loads(self.world["panes"][record["pane_id"]]["env"]["HERDR_FLEET_HANDOVER"])
        note = os.path.join(self.repo, ".herdr", "handover", "sample-orchestrator-note.md")
        self.assertEqual(data, {"op": "restart", "seat": "sample-orchestrator", "wait_ms": 60000, "note_file": note})
        with open(note, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "New provider.")
        self.assertIn("you are restarted when this turn ends", out)

    def test_refused_for_another_seat_and_outside_a_seat(self):
        code, _, err = self.run_cli("restart", "sample-dev", "--handover")
        self.assertEqual(code, 2)
        self.assertIn("--handover is for your own seat; run restart NAME from another pane for any other", err)
        self.env.update(HERDR_PANE_ID=CALLER_PANE, HERDR_WORKSPACE_ID="w1")
        code, _, err = self.request()
        self.assertEqual(code, 2)
        self.assertIn("--handover is for your own seat; run swap NAME from another pane for any other", err)
        self.assertEqual(self.argvs("plugin", "pane", "open"), [])

    def test_without_handover_the_own_pane_refusal_names_it(self):
        code, out, _ = self.run_cli("restart", "sample-orchestrator")
        self.assertEqual(code, 3, out)
        self.assertIn("sample-orchestrator: refused - it is the caller's own agent; restart it from another pane, or "
                      "add --handover to be replaced when this turn ends", out)

    def test_a_swap_without_drift_opens_nothing(self):
        self.write_fleet(THREE_SEATS)
        code, out, _ = self.request()
        self.assertEqual(code, 0, out)
        self.assertIn("sample-orchestrator: unchanged", out)
        self.assertEqual(self.argvs("plugin", "pane", "open"), [])
        self.assertNotIn("handover", self.read_state())

    def test_a_second_handover_is_refused(self):
        self.request()
        pane = self.read_state()["handover"]["pane_id"]
        code, out, _ = self.request()
        self.assertEqual(code, 3, out)
        self.assertIn("sample-orchestrator: refused - a handover is already waiting in %s" % pane, out)
        self.assertEqual(len(self.argvs("plugin", "pane", "open")), 1)

    def test_a_dry_run_says_what_it_would_open_and_run(self):
        code, out, _ = self.request("--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("herdr plugin pane open --plugin yarrasys.fleet --entrypoint handover --placement tab "
                      "--workspace w2", out)
        self.assertIn("then herdr-fleet swap sample-orchestrator once it is idle or done (up to 1h00m)", out)
        self.assertEqual(self.argvs("plugin", "pane", "open"), [])
        self.assertNotIn("handover", self.read_state())

    def test_the_helper_waits_swaps_briefs_notifies_and_closes_its_pane(self):
        self.request()
        pane = self.read_state()["handover"]["pane_id"]
        self.world["behaviors"]["wait_results"] = {"sample-orchestrator": ["done"]}
        code, out, _ = self.run_cli("handover", env=self.helper_env())
        self.assertEqual(code, 0, out)
        self.assertIn("handover: waiting for sample-orchestrator to end its turn", out)
        self.assertIn("sample-orchestrator: started", out)
        self.assertIn("sample-orchestrator: briefed", out)
        self.assertEqual(self.world["panes"]["w2:p1"]["agent"]["agent"], "codex")
        self.assertEqual(self.notifications(), [("Fleet: sample-orchestrator swapped", "done")])
        self.assertNotIn(pane, self.world["panes"])
        self.assertEqual(self.argvs("pane", "close"), [["pane", "close", pane]])
        self.assertNotIn("handover", self.read_state())
        wait = self.argvs("agent", "wait")[0]
        self.assertEqual(wait[:7], ["agent", "wait", "sample-orchestrator", "--until", "idle", "--until", "done"])

    def test_the_helper_waits_again_after_the_seat_turned_working(self):
        self.request()
        self.world["behaviors"]["wait_results"] = {"sample-orchestrator": ["idle", "idle"]}
        self.world["behaviors"]["status_after_wait"] = {"sample-orchestrator": ["working", "idle"]}
        code, out, _ = self.run_cli("handover", env=self.helper_env())
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.argvs("agent", "wait")), 2)
        self.assertIn("sample-orchestrator: started", out)
        self.assertEqual(self.notifications(), [("Fleet: sample-orchestrator swapped", "done")])

    def test_the_helper_stops_on_a_blocked_seat_and_keeps_its_pane_until_enter(self):
        self.request()
        pane = self.read_state()["handover"]["pane_id"]
        self.world["behaviors"]["wait_results"] = {"sample-orchestrator": ["idle"]}
        self.world["behaviors"]["status_after_wait"] = {"sample-orchestrator": ["blocked"]}
        at_enter = []

        def enter(ctx, stdin):  # the pane and its record are still there while it waits for Enter
            with open(self.world_path, encoding="utf-8") as handle:
                at_enter.append((pane in json.load(handle)["panes"], "handover" in self.read_state()))
        with mock.patch("herdr_fleet.handover.wait_for_enter", enter):
            code, out, _ = self.run_cli("handover", env=self.helper_env())
        self.assertEqual(code, 3, out)
        self.assertIn("sample-orchestrator: refused - agent is blocked: a dialog is open", out)
        self.assertEqual(self.notifications(), [("Fleet: handover of sample-orchestrator stopped", "request")])
        self.assertEqual(self.argvs("agent", "prompt"), [])
        self.assertEqual(at_enter, [(True, True)])
        self.assertNotIn(pane, self.world["panes"])
        self.assertNotIn("handover", self.read_state())

    def test_the_helper_runs_only_in_its_recorded_pane(self):
        self.request()
        env = self.helper_env()
        env["HERDR_PANE_ID"] = "w2:p2"
        code, _, err = self.run_cli("handover", env=env)
        self.assertEqual(code, 1)
        self.assertIn("not the recorded handover pane", err)
        self.assertEqual(self.argvs("agent", "wait"), [])

    def stopped(self, **behaviors):
        """Request the swap, run the helper with ``behaviors``, and capture whether its pane and
        record were still there when it waited for Enter."""
        self.request()
        pane = self.read_state()["handover"]["pane_id"]
        self.world["behaviors"].update(behaviors)
        at_enter = []

        def enter(ctx, stdin):
            with open(self.world_path, encoding="utf-8") as handle:
                at_enter.append((pane in json.load(handle)["panes"], "handover" in self.read_state()))
        with mock.patch("herdr_fleet.handover.wait_for_enter", enter):
            code, out, _ = self.run_cli("handover", env=self.helper_env())
        self.assertEqual(at_enter, [(True, True)])  # kept until Enter
        self.assertNotIn(pane, self.world["panes"])  # then closed, with its record
        self.assertNotIn("handover", self.read_state())
        return code, out

    def test_the_helper_stops_when_the_wait_times_out(self):
        code, out = self.stopped(wait_results={"sample-orchestrator": ["timeout"]})
        self.assertEqual(code, 3, out)
        self.assertIn("sample-orchestrator: stopped - sample-orchestrator did not end its turn within 1h00m", out)
        self.assertEqual(len(self.argvs("agent", "wait")), 1)
        self.assertEqual(self.notifications(), [("Fleet: handover of sample-orchestrator stopped", "request")])

    def test_the_helper_stops_when_the_wait_fails(self):
        code, out = self.stopped(wait_results={"sample-orchestrator": ["not_found"]})
        self.assertEqual(code, 3, out)
        self.assertIn("sample-orchestrator: stopped - waiting for sample-orchestrator: herdr error agent_not_found", out)
        self.assertEqual(len(self.argvs("agent", "wait")), 1)
        self.assertEqual(self.argvs("agent", "prompt"), [])

    def test_the_helper_stops_on_a_held_quit(self):
        fake_herdr.type_in(self.world, "w2:p1", "half a sentence")
        code, out = self.stopped(wait_results={"sample-orchestrator": ["done"]})
        self.assertEqual(code, 3, out)
        self.assertIn("sample-orchestrator: refused - text in its input (w2:p1)", out)
        self.assertEqual(len(self.argvs("agent", "wait")), 1)  # stopped, not waited for again
        self.assertEqual(self.argvs("agent", "prompt"), [])
        self.assertEqual(self.notifications(), [("Fleet: handover of sample-orchestrator stopped", "request")])

    def test_the_helper_stops_on_a_failed_start(self):
        code, out = self.stopped(wait_results={"sample-orchestrator": ["done"]},
                                 start_lost={"sample-orchestrator": 2})
        self.assertEqual(code, 1, out)
        self.assertIn("sample-orchestrator: failed - the start command did not run in w2:p1 twice", out)
        self.assertEqual(self.notifications(), [("Fleet: sample-orchestrator did not start", "request"),
                                                ("Fleet: handover of sample-orchestrator stopped", "request")])

    def test_the_helper_waits_briefly_for_its_record(self):
        self.request()
        env = self.helper_env()
        record = self.read_state()["handover"]
        state = self.read_state()
        del state["handover"]
        self.write_state(state)  # the request has not saved it yet

        class Late(FakeClock):
            def sleep(clock, seconds):
                super().sleep(seconds)
                if clock.slept >= 0.5 and "handover" not in self.read_state():
                    data = self.read_state()
                    data["handover"] = record
                    self.write_state(data)
        self.clock = Late()
        self.world["behaviors"]["wait_results"] = {"sample-orchestrator": ["done"]}
        code, out, _ = self.run_cli("handover", env=env)
        self.assertEqual(code, 0, out)
        self.assertIn("sample-orchestrator: started", out)

    def test_status_drops_a_record_whose_pane_is_gone(self):
        self.request()
        pane = self.read_state()["handover"]["pane_id"]
        del self.world["panes"][pane]
        code, out, _ = self.run_cli("status")
        self.assertNotIn("handover:", out)
        self.assertNotIn("handover", self.read_state())


# -- 32.6 ---------------------------------------------------------------------------------------


class QueuedKickoff(S32):
    def test_a_blocked_start_queues_the_kickoff_first(self):
        older = put(self.repo, "sample-dev", "Older request", sender="sample-orchestrator")
        self.world["behaviors"]["start_not_ready"] = ["sample-dev"]
        code, out, _ = self.run_cli("restart", "sample-dev", "--note", "Pick up the parser.")
        self.assertEqual(code, 3, out)
        queued = self.kickoffs_queued("sample-dev")
        self.assertEqual(len(queued), 1)
        short = queued[0].rsplit("-", 1)[1]
        self.assertIn("sample-dev: kickoff queued #%s: delivered when sample-dev is next idle or done" % short, out)
        self.assertRegex(out, r"sample-dev: blocked - blocked at startup in w2:p2 \([^)]*\); answer the prompt there; "
                         r"its kickoff is queued \(#%s\) and goes when it is idle\n" % short)  # not `kickoff NAME`
        messages = inbox.read_box(self.repo, "sample-dev", NEW)
        self.assertEqual([m.id for m in messages], [queued[0], older.id])
        self.assertEqual(messages[0].sender, "herdr-fleet")
        self.assertIn("Pick up the parser.", messages[0].body)
        self.assertNotIn("Older request", messages[0].body)  # without its {inbox} block
        code, out, _ = self.run_cli("status")
        row = next(line for line in out.splitlines() if line.startswith("sample-dev "))
        self.assertIn("(blocked at startup)", row)
        self.status_of("w2:p2", "idle")
        code, out, _ = self.run_cli("status")
        row = next(line for line in out.splitlines() if line.startswith("sample-dev "))
        self.assertIn("(kickoff queued #%s)" % short, row)

    def test_a_refused_kickoff_is_queued_and_the_watcher_delivers_it_once_idle(self):
        code, out, _ = self.run_cli("send", "sample-orchestrator", "--as", "sample-dev", "What is the status?")
        self.assertEqual(code, 0, out)  # a request sample-dev made before the restart
        self.world["behaviors"]["start_timeout"] = {"sample-dev": "working"}
        code, out, _ = self.run_cli("restart", "sample-dev")
        self.assertEqual(code, 3, out)
        self.assertRegex(out, r"sample-dev: kickoff queued %s: delivered when sample-dev is next idle or done"
                         % SHORT)
        self.assertEqual(self.prompts_to("sample-dev"), ["/exit"])
        code, out, _ = self.run_cli("send", "sample-dev", "--as", "sample-reviewer", "A later request")
        self.assertEqual(code, 0, out)  # queued: sample-dev is working
        later = next(m for m in inbox.read_box(self.repo, "sample-dev", NEW) if m.sender == "sample-reviewer")
        self.status_of("w2:p2", "idle")
        code, out, _ = self.run_cli("watch", "--once", "--log")
        self.assertEqual(code, 0, out)
        prompt = self.prompts_to("sample-dev")[-1]
        self.assertTrue(prompt.startswith("[from herdr-fleet] You are sample-dev"), prompt[:80])
        self.assertIn("A later request", prompt)
        self.assertLess(prompt.index("on restart."), prompt.index("A later request"))
        self.assertEqual(self.kickoffs_queued("sample-dev"), [])
        agent = self.read_state()["agents"]["sample-dev"]
        self.assertFalse(agent["blocked_at_startup"])
        self.assertTrue(agent.get("last_kickoff_at"))
        entries = log_entries(self.repo)
        kinds = [(e["kind"], e.get("to")) for e in entries]
        kick = kinds.index(("kickoff", "sample-dev"))
        self.assertEqual(entries[kick]["reason"], "restart")
        later_at = next(i for i, e in enumerate(entries) if e.get("id") == later.id and e["kind"] == "delivered")
        self.assertLess(kick, later_at)
        code, out, _ = self.run_cli("log", "--owed", "--json")
        owed = json.loads(out)
        self.assertFalse(any(o["from"] == "sample-dev" for o in owed))  # settled by the kickoff (20.8)
        self.assertTrue(any(o.get("id") == later.id for o in owed))     # carried with it: still owed

    def test_a_newer_kickoff_replaces_it_and_kickoff_drops_it(self):
        self.world["behaviors"]["start_not_ready"] = ["sample-dev"]
        self.run_cli("restart", "sample-dev")
        first = self.kickoffs_queued("sample-dev")
        self.world["panes"]["w2:p2"]["agent"]["agent_status"] = "idle"
        self.world["behaviors"]["start_not_ready"] = []
        self.world["behaviors"]["start_timeout"] = {"sample-dev": "working"}
        self.run_cli("restart", "sample-dev")
        second = self.kickoffs_queued("sample-dev")
        self.assertEqual(len(second), 1)
        self.assertNotEqual(first, second)
        self.assertIn(first[0], box(self.repo, "sample-dev", CUR))
        self.status_of("w2:p2", "idle")
        code, out, _ = self.run_cli("kickoff", "sample-dev")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.kickoffs_queued("sample-dev"), [])
        prompt = self.prompts_to("sample-dev")[-1]
        self.assertEqual(prompt.count("You are sample-dev"), 1)
        dropped = [e for e in log_entries(self.repo) if e["kind"] == "dropped"]
        self.assertEqual([e["id"] for e in dropped], [first[0], second[0]])

    def test_kickoff_refused_keeps_the_queued_one(self):
        self.world["behaviors"]["start_timeout"] = {"sample-dev": "working"}
        self.run_cli("restart", "sample-dev")
        queued = self.kickoffs_queued("sample-dev")
        code, out, _ = self.run_cli("kickoff", "sample-dev")
        self.assertEqual(code, 3, out)
        self.assertEqual(self.kickoffs_queued("sample-dev"), queued)

    def test_drop_inbox_drops_it(self):
        self.world["behaviors"]["start_timeout"] = {"sample-dev": "working"}
        self.run_cli("restart", "sample-dev")
        self.status_of("w2:p2", "idle")
        code, out, _ = self.run_cli("kickoff", "sample-dev", "--drop-inbox")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.kickoffs_queued("sample-dev"), [])

    def test_an_unconfirmed_kickoff_is_not_queued(self):
        self.world["behaviors"]["prompt_errors"] = {"sample-dev": "timeout"}
        self.world["panes"]["w2:p2"]["agent"] = None
        self.world["panes"]["w2:p2"]["procs"] = [dict(fake_herdr.SHELL, cwd=self.repo)]
        code, out, _ = self.run_cli("up", "sample-dev", "--no-watch")
        self.assertIn("sample-dev: not briefed", out)
        self.assertEqual(self.kickoffs_queued("sample-dev"), [])

    def test_send_now_leaves_it_queued(self):
        self.world["behaviors"]["start_timeout"] = {"sample-dev": "working"}
        self.run_cli("restart", "sample-dev")
        queued = self.kickoffs_queued("sample-dev")
        code, out, _ = self.run_cli("send", "sample-dev", "--now", "--as", "sample-orchestrator", "Look at #12")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.kickoffs_queued("sample-dev"), queued)
        self.assertNotIn("You are sample-dev", self.prompts_to("sample-dev")[-1])

    def test_a_dry_run_queues_nothing(self):
        self.world["behaviors"]["start_not_ready"] = ["sample-dev"]
        self.run_cli("restart", "sample-dev", "--dry-run")
        self.assertEqual(self.kickoffs_queued("sample-dev"), [])


class BoardPeek(PageCase):
    def test_p_on_a_working_seat_reads_the_visible_screen_and_says_so(self):
        self.world["behaviors"]["agent_not_idle"] = True
        self.world["panes"]["w2:p2"]["agent"]["agent_status"] = "working"
        self.page(["2", "p"])
        self.assertEqual(self.argvs("agent", "read"), [["agent", "read", "sample-dev", "--lines", "12"],
                                                        ["agent", "read", "sample-dev", "--lines", "12",
                                                         "--source", "visible"]])
        footers = [self.screen.text(i)[-1] for i in range(len(self.screen.draws)) if self.screen.text(i)]
        self.assertTrue(any("(working: the visible screen only)" in f for f in footers), footers[-3:])


# -- 32.7 ---------------------------------------------------------------------------------------


class Help(unittest.TestCase):
    def help_of(self, command):
        parser = build_parser()
        sub = next(a for a in parser._actions if a.dest == "command")
        return {c.dest: c.help for c in sub._choices_actions}[command], sub.choices[command].format_help()

    def test_inbox_names_its_flags(self):
        line, _ = self.help_of("inbox")
        self.assertEqual(line, "your own queued messages: --read, --wait, --count, --history")

    def test_peek_names_source(self):
        _, text = self.help_of("peek")
        self.assertIn("--source", text)


# -- 32.8 ---------------------------------------------------------------------------------------

SUGGESTION = "rerun the sample parser checks"


class FaintSuggestion(S32):
    def test_the_guard_reads_ansi(self):
        fake_herdr.suggest(self.world, "w2:p2", SUGGESTION)
        code, out, _ = self.run_cli("send", "sample-dev", "--as", "sample-orchestrator", "Look at #12")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.argvs("pane", "read"), [["pane", "read", "w2:p2", "--lines", "12", "--format", "ansi"]])

    def test_a_faint_suggestion_is_an_empty_line_for_send_and_restart(self):
        fake_herdr.suggest(self.world, "w2:p2", SUGGESTION)
        code, out, _ = self.run_cli("send", "sample-dev", "--as", "sample-orchestrator", "Look at #12")
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"sent to sample-dev \(idle\)")
        self.assertEqual(self.prompts_to("sample-dev")[-1][-11:], "Look at #12")
        code, out, _ = self.run_cli("restart", "sample-dev")
        self.assertEqual(code, 0, out)
        self.assertIn("sample-dev: quit - quit with /exit", out)

    def test_typed_text_holds(self):
        fake_herdr.type_in(self.world, "w2:p2", "half a sentence")
        code, out, _ = self.run_cli("send", "sample-dev", "--as", "sample-orchestrator", "Look at #12")
        self.assertIn("text in its input (w2:p2)", out)
        self.assertEqual(self.prompts_to("sample-dev"), [])

    def test_typed_text_with_a_faint_completion_holds(self):
        fake_herdr.suggest(self.world, "w2:p2", " parser tests", typed="fix the")
        code, out, _ = self.run_cli("send", "sample-dev", "--as", "sample-orchestrator", "Look at #12")
        self.assertIn("text in its input (w2:p2)", out)
        self.assertEqual(self.prompts_to("sample-dev"), [])

    def test_a_faint_try_suggestion_passes(self):
        fake_herdr.suggest(self.world, "w2:p2", 'Try "fix typecheck errors"')
        code, out, _ = self.run_cli("send", "sample-dev", "--as", "sample-orchestrator", "Look at #12")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.prompts_to("sample-dev")), 1)

    def test_a_codex_line_with_a_faint_run_is_unchanged(self):
        fake_herdr.suggest(self.world, "w2:p3", "pasted words", kind="codex")
        code, out, _ = self.run_cli("send", "sample-reviewer", "--as", "sample-orchestrator", "Look at #12")
        self.assertIn("text in its input (w2:p3)", out)
        self.assertEqual(self.prompts_to("sample-reviewer"), [])

    def test_interrupt_presses_no_ctrl_u_for_a_suggestion(self):
        self.status_of("w2:p2", "working")
        self.world["behaviors"]["suggest_on_interrupt"] = {"sample-dev": SUGGESTION}
        code, out, _ = self.run_cli("interrupt", "sample-dev")
        self.assertEqual(code, 0, out)
        self.assertEqual(out, "interrupted sample-dev (done)\n")
        self.assertEqual(self.argvs("agent", "send-keys"), [["agent", "send-keys", "sample-dev", "ctrl+c"]])
        self.assertIn(["agent", "read", "sample-dev", "--lines", "12", "--format", "ansi"], self.argvs("agent", "read"))

    def test_prompt_line_is_pure_on_the_live_shapes(self):
        from herdr_fleet import presence
        live = "\u276f\xa0\x1b[0m\x1b[2msummarise the sample fixture results\x1b[0m"  # the live escape shape
        empty = "\x1b[0m\x1b[38;2;153;153;153m\u276f\xa0\x1b[0m"
        self.assertEqual(presence.prompt_line(live, "claude"), "")
        self.assertEqual(presence.prompt_line(empty, "claude"), "")
        self.assertEqual(presence.prompt_line("\u276f \x1b[38;2;1;2;3mtyped\x1b[0m", "claude"), "typed")
        self.assertEqual(presence.prompt_line("\u276f fix \x1b[2mit\x1b[22m now", "claude"), "fix  now")
        self.assertEqual(presence.prompt_line("\u203a \x1b[2mpasted\x1b[0m", "codex"), "pasted")
        self.assertEqual(presence.prompt_line(live, "codex"), None)
        self.assertEqual(presence.strip_escapes(live), "\u276f\xa0summarise the sample fixture results")


if __name__ == "__main__":
    unittest.main()
