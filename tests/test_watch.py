"""The watcher (design 18.6, version 0.6.1): the lock and a stale lock, one waiter per
mailbox, the debounce re-check that backs off when the seat went working, FYI-only
mailboxes, absent agents, owner, pointers, unconfirmed prompts, --once, the status
lines, the fake's scripted `agent wait`, and the safety hook's rules for `watch`.

Waiters run inline (``watch.spawn`` patched) on the fake clock, whose loop sleep
raises KeyboardInterrupt after a number of passes, as Ctrl-C would; one test runs
real threads."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

import fake_herdr
from support import FakeClock, LiveFleet
from herdr_fleet import inbox, watch
from herdr_fleet.inbox import CUR, NEW
from test_inbox import box, log_entries, put

SHORT = r"#[0-9a-f]{6}"
WAIT = ["--until", "idle", "--until", "done", "--timeout", "30000"]


def inline(work):
    work()
    return None


class StopClock(FakeClock):
    """Fake time. The loop's own sleep (CYCLE_S, main thread) raises KeyboardInterrupt
    after ``cycles`` passes, or as soon as ``until()`` is true; ``real`` seconds of real
    sleep per pass let real waiter threads run."""

    def __init__(self, cycles=3, until=None, real=0.0):
        super().__init__()
        self.cycles, self.until, self.real, self.passes = cycles, until, real, 0

    def sleep(self, seconds):
        super().sleep(seconds)
        if threading.current_thread() is threading.main_thread() and seconds == watch.CYCLE_S:
            self.passes += 1
            if self.real:
                time.sleep(self.real)
            finished = self.until() if self.until is not None else False
            if finished or self.passes >= self.cycles:
                raise KeyboardInterrupt


def dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def events(out):
    """The watcher's lines without their HH:MM:SS stamps."""
    lines = out.splitlines()
    for line in lines:
        assert re.match(r"^\d\d:\d\d:\d\d  ", line), line
    return [line[10:] for line in lines]


class WatchCase(LiveFleet):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(watch, "spawn", inline)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.clock = StopClock()

    def behave(self, **behaviors):
        self.world["behaviors"].update(behaviors)

    def watch(self, *args, cycles=3):
        self.clock.cycles, self.clock.passes = cycles, 0
        return self.run_cli("watch", *args)

    def lock(self):
        return os.path.join(self.repo, ".herdr", "watch.lock")


class Waiters(WatchCase):
    def test_one_waiter_per_mailbox_and_a_batch_after_the_debounce(self):
        put(self.repo, "fablyn-dev", "Task A")
        put(self.repo, "fablyn-dev", "Task B", sender="fablyn-reviewer")
        put(self.repo, "fablyn-reviewer", "Review C")
        self.status_of("w2:p3", "working")
        self.behave(wait_results={"fablyn-dev": ["idle"], "fablyn-reviewer": ["timeout"] * 3})
        code, out, err = self.watch(cycles=3)
        self.assertEqual(code, 0, out + err)
        lines = events(out)
        self.assertRegex(lines[0], r"^watching .*fleet\.toml \(pid %d, debounce 5000 ms\); Ctrl-C stops$" % os.getpid())
        self.assertEqual(lines[1:], ["waiting on fablyn-dev (2 queued)", "fablyn-dev idle; delivering 2",
                                     "delivered 2 to fablyn-dev", "waiting on fablyn-reviewer (1 queued)", "stopped"])
        # one wait per pass for the working reviewer (a timeout ends the thread), none for the empty orchestrator
        self.assertEqual(self.argvs("agent", "wait", "fablyn-dev"), [["agent", "wait", "fablyn-dev"] + WAIT])
        self.assertEqual(self.argvs("agent", "wait", "fablyn-reviewer"), [["agent", "wait", "fablyn-reviewer"] + WAIT] * 3)
        self.assertEqual(self.argvs("agent", "wait", "fablyn-orchestrator"), [])
        (prompt,) = self.prompts_to("fablyn-dev")
        self.assertRegex(prompt, r"^\[from fablyn-orchestrator at \d\d:\d\d %s\]\nTask A\n\n"
                                 r"\[from fablyn-reviewer at \d\d:\d\d %s\]\nTask B$" % (SHORT, SHORT))
        self.assertEqual(self.prompts_to("fablyn-reviewer"), [])
        self.assertEqual((len(box(self.repo, "fablyn-dev", NEW)), len(box(self.repo, "fablyn-dev", CUR))), (0, 2))
        self.assertEqual(len(box(self.repo, "fablyn-reviewer", NEW)), 1)
        self.assertEqual([(e["kind"], e["via"], e["delivery"]) for e in log_entries(self.repo)],
                         [("delivered", "watch", "sent")] * 2)
        note = self.world["notifications"][-1]
        self.assertEqual((note["title"], note["body"], note["sound"]),
                         ("delivered 2 to fablyn-dev", "from fablyn-orchestrator (1), fablyn-reviewer (1)", "none"))
        self.assertGreaterEqual(self.clock.slept, 5.0 + 3 * watch.CYCLE_S)
        self.assertFalse(os.path.exists(self.lock()))

    def test_the_debounce_recheck_backs_off_when_the_seat_went_working(self):
        put(self.repo, "fablyn-dev", "Task A")
        # The first wait sees idle, but the seat starts working during the debounce.
        self.behave(wait_results={"fablyn-dev": ["idle", "idle"]}, status_after_wait={"fablyn-dev": ["working"]})
        code, out, _ = self.watch("--debounce", "1500", cycles=2)
        self.assertEqual(code, 0)
        self.assertEqual(events(out)[1:], ["waiting on fablyn-dev (1 queued)", "fablyn-dev idle; delivering 1",
                                           "fablyn-dev working; still waiting", "fablyn-dev idle; delivering 1",
                                           "delivered 1 to fablyn-dev", "stopped"])
        self.assertEqual(len(self.prompts_to("fablyn-dev")), 1)  # never while working (tearDown checks the state)
        gets = [c for c in self.calls() if c["argv"][:3] == ["agent", "get", "fablyn-dev"]]
        self.assertEqual(gets[0]["info"]["status"], "working")
        self.assertEqual(self.clock.slept, 1.5 * 2 + 2 * watch.CYCLE_S)

    def test_a_blocked_seat_is_never_prompted(self):
        put(self.repo, "fablyn-dev", "Task A")
        self.behave(wait_results={"fablyn-dev": ["idle", "timeout"]}, status_after_wait={"fablyn-dev": ["blocked"]})
        code, out, _ = self.watch(cycles=3)
        self.assertEqual(code, 0)
        self.assertIn("fablyn-dev blocked; still waiting", events(out))
        self.assertEqual(self.prompts_to("fablyn-dev"), [])
        self.assertEqual(len(box(self.repo, "fablyn-dev", NEW)), 1)
        self.assertEqual(len(self.argvs("agent", "wait", "fablyn-dev")), 4)  # idle, timeout, then one per pass
        self.assertNotIn("delivered", out)

    def test_fyi_only_mailboxes_get_no_waiter_and_fyis_ride_along(self):
        put(self.repo, "fablyn-orchestrator", "FYI: round 3 clean", sender="fablyn-reviewer", fyi=True)
        put(self.repo, "fablyn-dev", "FYI: build green", sender="fablyn-reviewer", fyi=True)
        put(self.repo, "fablyn-dev", "Task A")
        self.behave(wait_results={"fablyn-dev": ["idle"]})
        code, out, _ = self.watch(cycles=2)
        self.assertEqual(code, 0)
        self.assertEqual(self.argvs("agent", "wait", "fablyn-orchestrator"), [])
        self.assertEqual(self.prompts_to("fablyn-orchestrator"), [])
        self.assertEqual(len(box(self.repo, "fablyn-orchestrator", NEW)), 1)
        (prompt,) = self.prompts_to("fablyn-dev")
        self.assertIn("FYI: build green", prompt)
        self.assertIn("Task A", prompt)

    def test_nothing_queued_asks_herdr_nothing(self):
        put(self.repo, "fablyn-dev", "FYI: only this", fyi=True)
        before = len(self.calls())
        code, out, _ = self.watch(cycles=3)
        self.assertEqual(code, 0)
        self.assertEqual([c["argv"][:2] for c in self.calls()[before:]], [["agent", "list"], ["--version"]])

    def test_an_absent_agent_gets_no_waiter_and_one_line(self):
        self.world["panes"]["w2:p3"]["agent"] = None
        put(self.repo, "fablyn-reviewer", "Review C")
        code, out, _ = self.watch(cycles=4)
        self.assertEqual(code, 0)
        self.assertEqual(events(out)[1:], ["fablyn-reviewer: absent, 1 left queued", "stopped"])
        self.assertEqual(self.argvs("agent", "wait"), [])
        self.assertEqual(len(box(self.repo, "fablyn-reviewer", NEW)), 1)
        # looked for once per 30 s, not on every pass: preflight, then one list in 8 s of passes
        self.assertEqual(len(self.argvs("agent", "list")), 2)
        self.watch(cycles=17)  # 34 s: looked for again once
        self.assertEqual(len(self.argvs("agent", "list")), 2 + 1 + 2)

    def test_a_contact_is_waited_on_through_its_pane(self):
        self.write_fleet(self.read_fleet() + '\n[[contacts]]\nname = "peer"\nmatch = { name = "peer-agent" }\n')
        fake_herdr.add_pane(self.world, "w2:p9", "w2", self.repo)
        fake_herdr.put_agent(self.world, "w2:p9", "claude", "peer-agent")
        put(self.repo, "peer", "hello peer", sender="fablyn-dev")
        self.behave(wait_results={"peer-agent": ["done"]})
        code, out, _ = self.watch(cycles=2)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.argvs("agent", "wait"), [["agent", "wait", "w2:p9"] + WAIT])
        self.assertIn("peer done; delivering 1", events(out))
        self.assertEqual(len(self.world["panes"]["w2:p9"]["prompts"]), 1)


class Deliveries(WatchCase):
    def test_owner_is_notified_at_once_and_only_once(self):
        put(self.repo, "owner", "Please approve the deploy", sender="fablyn-dev")
        code, out, _ = self.watch(cycles=3)
        self.assertEqual(code, 0)
        self.assertEqual(events(out)[1:], ["delivered 1 to owner (notification)", "stopped"])
        self.assertEqual(len(self.world["notifications"]), 1)
        self.assertTrue(self.world["notifications"][0]["title"].startswith("message from fablyn-dev #"))
        self.assertEqual(len(box(self.repo, "owner", CUR)), 1)
        self.assertEqual([e["via"] for e in log_entries(self.repo)], ["notify"])
        self.assertEqual(self.argvs("agent", "wait"), [])

    def test_unconfirmed_is_logged_moved_and_never_redelivered(self):
        put(self.repo, "fablyn-dev", "Task A")
        self.behave(prompt_errors={"fablyn-dev": "timeout"})
        code, out, _ = self.watch(cycles=3)
        self.assertEqual(code, 0)
        self.assertTrue(any(line.startswith("unconfirmed fablyn-dev: herdr error timeout") for line in events(out)))
        self.assertEqual(len(self.prompts_to("fablyn-dev")), 1)
        self.assertEqual(box(self.repo, "fablyn-dev", NEW), [])
        self.assertEqual([e["delivery"] for e in log_entries(self.repo)], ["unconfirmed"])
        self.assertEqual(self.world["notifications"][-1]["title"], "delivery to fablyn-dev unconfirmed")
        self.assertEqual(self.world["notifications"][-1]["sound"], "request")

    def test_a_failed_delivery_waits_a_minute_before_trying_again(self):
        put(self.repo, "fablyn-dev", "Task A")
        self.behave(prompt_errors={"fablyn-dev": "internal_error"})
        code, out, _ = self.watch(cycles=35)  # 70 s of passes
        self.assertEqual(code, 0)
        failed = ("failed fablyn-dev: herdr error internal_error: prompt internal_error for fablyn-dev; "
                  "1 left queued, trying again in 60 s")
        self.assertEqual(events(out).count(failed), 2)  # at once, then once more after the minute
        self.assertEqual(len(self.prompts_to("fablyn-dev")), 2)
        self.assertEqual(self.world["notifications"][-1]["title"], "delivery to fablyn-dev failed")
        self.assertEqual(len(box(self.repo, "fablyn-dev", NEW)), 1)

    def test_a_pointer_is_prompted_once_until_something_new_arrives(self):
        put(self.repo, "fablyn-dev", "x" * (inbox.BATCH_CAP + 10))
        added = []

        def a_new_message_after_three_passes():
            if self.clock.passes == 3 and not added:
                added.append(put(self.repo, "fablyn-dev", "a new task"))
            return False

        self.clock.until = a_new_message_after_three_passes
        code, out, _ = self.watch(cycles=6)
        self.assertEqual(code, 0)
        first, second = self.prompts_to("fablyn-dev")  # passes 2, 3, 5 and 6 are held: no pointer again
        self.assertRegex(first, r"^\[from herdr-fleet\] 1 message waiting \(oldest .*\); run: herdr-fleet inbox --read$")
        self.assertRegex(second, r"^\[from herdr-fleet\] 2 messages waiting")
        self.assertEqual(len(self.argvs("agent", "wait")), 2)
        self.assertEqual(len(box(self.repo, "fablyn-dev", NEW)), 2)  # the seat reads them itself
        self.assertEqual(log_entries(self.repo), [])  # a pointer is not a delivery

    def test_a_message_read_meanwhile_is_not_delivered(self):
        put(self.repo, "fablyn-dev", "Task A")
        self.behave(wait_results={"fablyn-dev": ["idle"]})
        original = watch.Watcher.queued

        def read_first(watcher, name):  # the seat ran `inbox --read` while the wait returned
            for message in inbox.read_box(self.repo, name, NEW):
                inbox.move(self.repo, message, CUR)
            return original(watcher, name)

        with mock.patch.object(watch.Watcher, "queued", read_first):
            code, out, _ = self.watch(cycles=2)
        self.assertEqual(code, 0)
        self.assertEqual(self.prompts_to("fablyn-dev"), [])
        self.assertEqual(events(out)[1:], ["waiting on fablyn-dev (1 queued)", "stopped"])


class Lock(WatchCase):
    def test_a_live_lock_refuses_and_a_dead_one_is_replaced(self):
        with open(self.lock(), "w") as handle:
            json.dump({"pid": os.getpid(), "started": "2026-10-21T10:00:00+11:00", "fleet": "x"}, handle)
        before = len(self.calls())
        code, out, err = self.watch()
        self.assertEqual(code, 1)
        self.assertIn("herdr-fleet: error: watcher already running (pid %d)" % os.getpid(), err)
        self.assertEqual(watch.read_lock(self.repo)["fleet"], "x")  # left alone
        self.assertEqual([c["argv"][:2] for c in self.calls()[before:]], [["agent", "list"], ["--version"]])
        stale = dead_pid()
        with open(self.lock(), "w") as handle:
            json.dump({"pid": stale, "started": "2026-10-21T10:00:00+11:00", "fleet": "x"}, handle)
        seen = []
        self.clock.until = lambda: seen.append(watch.read_lock(self.repo)) or False
        code, out, err = self.watch(cycles=1)
        self.assertEqual(code, 0, err)
        self.assertEqual(seen[0]["pid"], os.getpid())
        self.assertEqual(seen[0]["fleet"], os.path.join(self.repo, ".herdr", "fleet.toml"))
        self.assertRegex(seen[0]["started"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$")
        self.assertFalse(os.path.exists(self.lock()))  # removed on exit (Ctrl-C)
        with open(os.path.join(self.repo, ".herdr", ".gitignore")) as handle:
            self.assertIn("watch.lock", handle.read().split())

    def test_an_unreadable_lock_is_replaced(self):
        with open(self.lock(), "w") as handle:
            handle.write("{half")
        self.assertEqual(self.watch(cycles=1)[0], 0)
        self.assertFalse(os.path.exists(self.lock()))

    def test_the_lock_names_only_the_running_watcher(self):
        data = watch.acquire(self.repo, "fleet.toml", pid=os.getpid())
        with self.assertRaises(watch.OpError):
            watch.acquire(self.repo, "fleet.toml")
        watch.release(self.repo, dead_pid())  # not ours: kept
        self.assertTrue(os.path.exists(self.lock()))
        watch.release(self.repo, data["pid"])
        self.assertFalse(os.path.exists(self.lock()))
        self.assertEqual([f for f in os.listdir(os.path.join(self.repo, ".herdr")) if f.endswith(".tmp")], [])

    def test_usage(self):
        self.assertEqual(self.run_cli("watch", "--debounce", "-1")[0], 2)
        self.assertEqual(self.calls(), [])


class Once(WatchCase):
    def test_once_delivers_what_it_can_now_without_waiters_or_lock(self):
        put(self.repo, "fablyn-dev", "Task A")
        put(self.repo, "fablyn-reviewer", "Review C")
        put(self.repo, "fablyn-orchestrator", "FYI: x", fyi=True)
        put(self.repo, "owner", "for you", sender="fablyn-dev")
        self.status_of("w2:p3", "working")
        with mock.patch.object(watch, "acquire", side_effect=AssertionError("--once takes no lock")):
            code, out, err = self.run_cli("watch", "--once")
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertRegex(lines[0], r"^delivered 1 to fablyn-dev \(idle\), \d+ B$")
        self.assertEqual(lines[1:], ["fablyn-reviewer: working, 1 left queued", "delivered 1 to owner (notification)"])
        self.assertEqual(self.argvs("agent", "wait"), [])
        self.assertEqual(self.prompts_to("fablyn-orchestrator"), [])  # FYI only
        self.assertEqual([(e["to"], e["via"]) for e in log_entries(self.repo)],
                         [("fablyn-dev", "watch"), ("owner", "notify")])
        self.assertEqual([n["title"] for n in self.world["notifications"]][0], "delivered 1 to fablyn-dev")
        self.assertEqual(self.clock.slept, 0)
        code, out, _ = self.run_cli("watch", "--once")
        self.assertEqual((code, out.strip()), (0, "fablyn-reviewer: working, 1 left queued"))
        self.status_of("w2:p3", "idle")
        self.behave(prompt_errors={"fablyn-reviewer": "agent_prompt_stalled"})
        self.assertEqual(self.run_cli("watch", "--once")[0], 1)  # as deliver: may not have arrived

    def test_once_with_nothing_to_deliver(self):
        self.assertEqual(self.run_cli("watch", "--once")[1].strip(), "nothing to deliver")


class RealThreads(LiveFleet):
    def test_waiters_in_threads_deliver_and_stop_cleanly(self):
        put(self.repo, "fablyn-dev", "Task A")
        put(self.repo, "fablyn-reviewer", "Review C")
        self.world["behaviors"]["wait_results"] = {"fablyn-dev": ["idle"], "fablyn-reviewer": ["done"]}
        done = lambda: not box(self.repo, "fablyn-dev", NEW) and not box(self.repo, "fablyn-reviewer", NEW)  # noqa
        self.clock = StopClock(cycles=500, until=done, real=0.01)
        code, out, err = self.run_cli("watch", "--debounce", "0")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(len(self.prompts_to("fablyn-dev")), 1)
        self.assertEqual(len(self.prompts_to("fablyn-reviewer")), 1)
        self.assertIn("delivered 1 to fablyn-dev", out)
        self.assertIn("delivered 1 to fablyn-reviewer", out)
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".herdr", "watch.lock")))
        self.assertEqual([t for t in threading.enumerate() if t.name == "herdr-fleet-waiter" and t.is_alive()], [])


class Status(LiveFleet):
    def test_watcher_line_and_json(self):
        data = json.loads(self.run_cli("status", "--json")[1])
        self.assertEqual(data["watcher"], {"running": False, "pid": None, "since": None})
        self.assertNotIn("watcher:", self.run_cli("status")[1])  # nothing queued: no line
        put(self.repo, "fablyn-dev", "a")
        put(self.repo, "fablyn-dev", "b")
        lines = self.run_cli("status")[1].splitlines()
        index = lines.index("watcher: not running")
        self.assertRegex(lines[index + 1], r"^queued: fablyn-dev 2 \(oldest (<1m|\d+m)\)$")  # an age (20.8)
        with open(os.path.join(self.repo, ".herdr", "watch.lock"), "w") as handle:
            json.dump({"pid": os.getpid(), "started": inbox.iso_local(inbox.now_utc()), "fleet": "x"}, handle)
        self.assertRegex(self.run_cli("status")[1], r"\nwatcher: running \(pid %d since \d\d:\d\d\)\nqueued: "
                         % os.getpid())
        data = json.loads(self.run_cli("status", "--json")[1])
        self.assertEqual((data["watcher"]["running"], data["watcher"]["pid"]), (True, os.getpid()))
        self.assertEqual(data["queued"], {"fablyn-dev": 2})
        self.assertIn("fablyn-dev", data["queued_oldest"])
        with open(os.path.join(self.repo, ".herdr", "watch.lock"), "w") as handle:
            json.dump({"pid": dead_pid(), "started": "2026-10-21T10:00:00+11:00"}, handle)
        self.assertIn("\nwatcher: not running\n", self.run_cli("status")[1])


class FakeWait(LiveFleet):
    """The fake's scripted `agent wait` answers as live herdr 0.9.5 does."""

    def run_fake(self, *argv):
        with open(self.world_path, "w") as handle:
            json.dump(self.world, handle)
        result = fake_herdr.Fake(self.world_path, self.log_path)(["herdr"] + list(argv))
        with open(self.world_path) as handle:
            self.world = json.load(handle)
        return result

    def test_outcomes(self):
        self.world["behaviors"]["wait_results"] = {"fablyn-dev": ["done", "blocked", "timeout", "not_found", "timeout"]}
        code, out, _ = self.run_fake("agent", "wait", "fablyn-dev", *WAIT)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["result"]["agent"]["agent_status"], "done")
        self.assertEqual(json.loads(out)["result"]["type"], "agent_info")
        for expected in ("timeout", "timeout", "agent_not_found"):  # blocked is not awaited: it times out
            code, _, err = self.run_fake("agent", "wait", "fablyn-dev", *WAIT)
            self.assertEqual((code, json.loads(err)["error"]["code"]), (1, expected))
        self.assertEqual(self.world["panes"]["w2:p2"]["agent"]["agent_status"], "blocked")
        code, _, err = self.run_fake("agent", "wait", "fablyn-dev", "--until", "idle")
        self.assertEqual(json.loads(err)["error"]["code"], "fake_wait_forever")
        info = [c["info"] for c in self.calls() if c["argv"][:2] == ["agent", "wait"]]
        self.assertEqual((info[0]["until"], info[0]["timeout"]), (["idle", "done"], "30000"))
        self.assertEqual((info[-1]["until"], info[-1]["timeout"]), (["idle"], None))


class SafetyHookKnowsWatch(LiveFleet):
    def check_fails(self, command, argv, info=None):
        with open(self.log_path, "a") as handle:
            handle.write(json.dumps({"command": command}) + "\n")
            handle.write(json.dumps({"argv": argv, "exit": 0, "info": info or {}}) + "\n")
        with self.assertRaises(AssertionError):
            self.assert_safety()
        os.remove(self.log_path)

    def test_watch_runs_only_its_commands(self):
        self.check_fails(["watch"], ["agent", "start", "x", "--kind", "claude", "--pane", "w2:p9"], {"at_shell": True})
        self.check_fails(["watch"], ["pane", "current", "--current"])
        self.check_fails(["watch"], ["agent", "wait", "fablyn-dev", "--until", "idle"])  # no endless wait
        self.check_fails(["watch"], ["agent", "prompt", "fablyn-dev", "x"], {"target_status": "working"})
        self.check_fails(["watch"], ["agent", "prompt", "fablyn-dev", "x"], {"target_status": "blocked"})

    def test_hooks_and_inbox_hook(self):
        self.check_fails(["hooks", "install"], ["agent", "list"])
        self.check_fails(["inbox", "--hook", "stop"], ["pane", "current", "--current"])


if __name__ == "__main__":
    unittest.main()
