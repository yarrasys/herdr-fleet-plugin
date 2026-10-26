"""Design section 20 (version 0.9.0): history from mixed sources, debt ages and ids,
``ack --older-than``, where a message sits (send lines, positions, ``--urgent``),
``inbox_print_max`` and ``--full``, ``state_seen`` and ``blocked (4m)``, the watcher's
``notify_blocked_after``, ``queued_by`` and the drain at the first opportunity
(watcher, ``deliver``, the flush before send, ``deliver --now``), the FYI send line,
kickoffs as owed boundaries, cross-fleet logging, queue ages and the inbox hint."""

from __future__ import annotations

import datetime
import json
import os
import re
import unittest
from contextlib import contextmanager
from unittest import mock

import fake_herdr
from support import policy_output, ARGS, CALLER_PANE, THREE_SEATS, FleetCase, LiveFleet, typed_ahead_allowed
from herdr_fleet import inbox, messages, state as state_mod
from herdr_fleet.fleetfile import FleetError, load_fleet
from herdr_fleet.inbox import CUR, NEW, Message
from herdr_fleet.kickoff import ROSTER_SENTENCE, roster_table
from herdr_fleet.messages import FYI_PLACE, URGENT_TYPED, place
from test_inbox import box, log_entries, put
from test_watch import StopClock, WatchCase, events

SHORT = r"#[0-9a-f]{6}"
HHMM = r"\d\d:\d\d"
WORKING = re.escape(place("working"))
WAIT = ["--until", "idle", "--until", "done", "--timeout", "30000"]
AHEAD_WAIT = ["--until", "working", "--until", "idle", "--until", "done", "--timeout", "30000"]
T0 = datetime.datetime(2026, 10, 21, 0, 0, 0, tzinfo=datetime.timezone.utc)


class Clock:
    """Wall time for herdr-fleet (``inbox.now_utc``) that a test moves by hand."""

    def __init__(self, start=T0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, **delta):
        self.now += datetime.timedelta(**delta)


@contextmanager
def wall(clock):
    with mock.patch.object(inbox, "now_utc", clock):
        yield clock


def iso(when):
    return when.astimezone().isoformat(timespec="seconds")


def raw_entry(sender, to, when, kind="send", first="m", ident=None, delivery="sent", **extra):
    entry = {"time": iso(when), "from": sender, "to": to, "kind": kind, "bytes": 1, "first_line": first,
             "delivery": delivery}
    if ident:
        entry["id"] = ident
    entry.update(extra)
    return entry


# -- 20.1 history from mixed sources ------------------------------------------------------------


class History(LiveFleet):
    def test_log_only_messages_render_with_their_first_line_and_a_note(self):
        old = inbox.now_utc() - datetime.timedelta(hours=2)
        for minutes, sender, to, first in ((0, "sample-orchestrator", "sample-dev", "pre-inbox ask"),
                                           (5, "sample-dev", "sample-orchestrator", "pre-inbox answer")):
            messages.append(self.repo, raw_entry(sender, to, old + datetime.timedelta(minutes=minutes), first=first))
        messages.append(self.repo, raw_entry("sample-orchestrator", "sample-dev", old, first="queued, never sent",
                                             delivery="queued"))
        messages.append(self.repo, raw_entry("sample-reviewer", "sample-dev", old, first="another pair"))
        self.run_cli("send", "--as", "sample-orchestrator", "sample-dev", "with a body\nsecond line")
        code, out, _ = self.run_cli("inbox", "--as", "sample-dev", "--history", "sample-orchestrator")
        self.assertEqual(code, 0, out)
        blocks = policy_output(out).rstrip("\n").split("\n\n")
        self.assertRegex(blocks[0], r"^\[from sample-orchestrator to sample-dev at %s, first line only\]\n"
                                    r"pre-inbox ask$" % HHMM)
        self.assertRegex(blocks[1], r"^\[from sample-dev to sample-orchestrator at %s, first line only\]\n"
                                    r"pre-inbox answer$" % HHMM)
        self.assertRegex(blocks[2], r"^\[from sample-orchestrator to sample-dev at %s %s\]\nwith a body\nsecond line$"
                         % (HHMM, SHORT))
        self.assertEqual(blocks[3], "2 of 3 messages have only their first line (sent before the inbox kept bodies)")
        self.assertNotIn("queued, never sent", out)
        self.assertNotIn("another pair", out)
        data = json.loads(self.run_cli("inbox", "--as", "sample-dev", "--history", "sample-orchestrator",
                                       "--json")[1])
        self.assertEqual([(d["first_line"], d["body_available"]) for d in data],
                         [("pre-inbox ask", False), ("pre-inbox answer", False), ("with a body", True)])
        self.assertIsNone(data[0]["body"])

    def test_nothing_at_all(self):
        code, out, _ = self.run_cli("inbox", "--as", "sample-dev", "--history", "sample-reviewer")
        self.assertEqual((code, policy_output(out).strip()), (0, "no messages between sample-dev and sample-reviewer"))


# -- 20.2 debt ages, ids and ack --older-than -----------------------------------------------------


class DebtAges(LiveFleet):
    def test_status_and_log_owed_show_age_and_id(self):
        clock = Clock()
        messages.append(self.repo, raw_entry("sample-orchestrator", "sample-dev", T0, first="Review X",
                                             ident="20261021T000000Z-a1b2c3"))
        messages.append(self.repo, raw_entry("sample-orchestrator", "sample-reviewer", T0, first="pre-inbox"))
        clock.advance(hours=5, minutes=12)
        with wall(clock):
            owed = self.run_cli("log", "--owed")[1].splitlines()
            status = self.run_cli("status")[1]
        self.assertRegex(owed[0], r"^awaiting: sample-dev owes sample-orchestrator for 5h12m \(#a1b2c3, since "
                                  r"(%s|\d{4}-\d\d-\d\d %s)\): Review X$" % (HHMM, HHMM))
        self.assertRegex(owed[1], r"^awaiting: sample-reviewer owes sample-orchestrator for 5h12m \(no id, since .*\): "
                                  r"pre-inbox$")
        self.assertIn(owed[0], status)
        self.assertEqual([inbox.age_label(s) for s in (30, 180, 3900, 2 * 86400 + 3 * 3600 + 59)],
                         ["<1m", "3m", "1h05m", "2d03h"])


class AckOlderThan(LiveFleet):
    def setUp(self):
        super().setUp()
        self.clock = Clock()
        for when, sender, ident, first in ((T0, "sample-orchestrator", "20261021T000000Z-aaaaaa", "old ask"),
                                           (T0 + datetime.timedelta(hours=4, minutes=50), "sample-orchestrator",
                                            "20261021T045000Z-bbbbbb", "fresh ask"),
                                           (T0 + datetime.timedelta(hours=1), "sample-reviewer", None, "no id")):
            messages.append(self.repo, raw_entry(sender, "sample-dev", when, first=first, ident=ident))
        self.clock.advance(hours=5)

    def ack(self, *argv):
        with wall(self.clock):
            return self.run_cli("ack", "--as", "sample-dev", *argv)

    def test_dry_run_lists_and_writes_nothing(self):
        before = len(log_entries(self.repo))
        code, out, _ = self.ack("--older-than", "2h", "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(len(policy_output(out).splitlines()), 2, out)
        self.assertRegex(policy_output(out), r"would ack #aaaaaa from sample-orchestrator since .*, 5h00m ago \(old ask\)")
        self.assertRegex(policy_output(out), r"would ack sample-reviewer since .*, 4h00m ago \(no id\)")
        self.assertEqual(len(log_entries(self.repo)), before)
        self.assertEqual(self.calls(), [])  # with --as: no herdr command at all

    def test_acks_each_old_message_by_id_and_keeps_the_fresh_one(self):
        code, out, _ = self.ack("--older-than", "2h")
        self.assertEqual(code, 0, out)
        acks = [e for e in log_entries(self.repo) if e["kind"] == "ack"]
        self.assertEqual([(a["to"], a.get("id")) for a in acks],
                         [("sample-orchestrator", "20261021T000000Z-aaaaaa"), ("sample-reviewer", None)])
        with wall(self.clock):
            owed = json.loads(self.run_cli("log", "--owed", "--json")[1])
        self.assertEqual([(o["from"], o["first_line"]) for o in owed], [("sample-orchestrator", "fresh ask")])

    def test_names_restrict_and_no_id_messages_are_never_overreached(self):
        code, out, _ = self.ack("sample-orchestrator", "--older-than", "2h")
        self.assertEqual(code, 0)
        self.assertEqual([e.get("id") for e in log_entries(self.repo) if e["kind"] == "ack"],
                         ["20261021T000000Z-aaaaaa"])
        messages.append(self.repo, raw_entry("sample-reviewer", "sample-dev", T0 + datetime.timedelta(hours=4,
                                             minutes=59), first="newer, no id"))
        code, out, _ = self.ack("--older-than", "2h", "sample-reviewer")
        self.assertIn("skipped a message from sample-reviewer", out)
        self.assertIn("ack sample-reviewer would also settle newer ones", out)
        self.assertEqual(len([e for e in log_entries(self.repo) if e["kind"] == "ack"]), 1)
        code, out, _ = self.ack("--older-than", "6h")
        self.assertEqual(policy_output(out).strip(), "nothing owed by sample-dev older than 6h")
        self.assertEqual(self.ack("--older-than", "5x")[0], 2)
        self.assertEqual(self.run_cli("ack", "--as", "sample-dev")[0], 2)  # needs something to ack


# -- 20.3 where a message sits, --urgent ----------------------------------------------------------


class WhereItSits(LiveFleet):
    def test_the_three_places(self):
        code, out, _ = self.run_cli("send", "--as", "sample-orchestrator", "sample-dev", "now")
        self.assertRegex(policy_output(out).strip(), r"^sent to sample-dev \(idle\), \d+ B$")
        self.status_of("w2:p3", "working")  # design 21.1: typed into a working seat only with --now
        code, out, _ = self.run_cli("send", "--now", "--as", "sample-orchestrator", "sample-reviewer", "typed")
        self.assertRegex(policy_output(out).strip(), r"^sent to sample-reviewer \(%s\), \d+ B$" % WORKING)
        self.world["panes"]["w2:p2"]["agent"] = None
        self.run_cli("send", "--as", "sample-orchestrator", "sample-dev", "first")
        code, out, _ = self.run_cli("send", "--as", "sample-orchestrator", "sample-dev", "second")
        self.assertRegex(policy_output(out).strip(), r"^queued in herdr-fleet for sample-dev \(absent\) %s, position 2 of 2$" % SHORT)

    def test_urgent_goes_first_in_listing_and_batch(self):
        self.status_of("w2:p2", "blocked")
        for text in ("a", "b"):
            self.run_cli("send", "--as", "sample-orchestrator", "sample-dev", text)
        code, out, _ = self.run_cli("send", "--as", "sample-reviewer", "--urgent", "sample-dev", "c")
        self.assertRegex(policy_output(out).strip(), r"^queued in herdr-fleet for sample-dev \(blocked: a dialog is open\) %s, "
                                      r"position 1 of 3$" % SHORT)
        (urgent,) = [m for m in inbox.read_box(self.repo, "sample-dev", NEW) if m.urgent]
        with open(urgent.path) as handle:
            self.assertIn("\npriority: 1\n", handle.read())
        lines = policy_output(self.run_cli("inbox", "--as", "sample-dev")[1]).splitlines()
        self.assertRegex(lines[0], r"^1\. %s  %s  sample-reviewer  \[urgent\]  \[on refusal\]  c$" % (SHORT, HHMM))
        self.assertRegex(lines[1], r"^2\. .*  a$")
        self.assertRegex(lines[2], r"^3\. .*  b$")
        self.status_of("w2:p2", "idle")
        self.run_cli("deliver", "sample-dev")
        (prompt,) = self.prompts_to("sample-dev")
        self.assertEqual([line.split("]\n")[-1] for line in prompt.split("\n\n")], ["c", "a", "b"])

    def test_urgent_to_a_working_seat_says_it_cannot_reorder(self):
        self.status_of("w2:p2", "working")
        code, out, _ = self.run_cli("send", "--urgent", "sample-dev", "pause the example")  # design 21.1: queued
        self.assertEqual(code, 0)
        self.assertRegex(policy_output(out).strip(), r"^queued in herdr-fleet for sample-dev \(working\) %s, position 1 of 1; lands "
                                      r"at its next idle, or now with --now$" % SHORT)
        self.assertEqual(self.prompts_to("sample-dev"), [])
        code, out, _ = self.run_cli("send", "--now", "--urgent", "sample-dev", "stop it now")
        self.assertEqual(code, 0)
        self.assertRegex(policy_output(out).strip(), r"^sent to sample-dev \(%s\) with 1 queued, \d+ B; %s$"
                         % (WORKING, re.escape(URGENT_TYPED)))


# -- 20.4 inbox_print_max and --full --------------------------------------------------------------


class PrintMax(LiveFleet):
    fleet_text = "inbox_print_max = 100\n" + THREE_SEATS

    def test_read_and_history_cut_long_messages(self):
        body = "\n".join("line %02d...." % i for i in range(30))  # 30 lines, 359 bytes
        self.status_of("w2:p2", "blocked")
        self.run_cli("send", "--as", "sample-orchestrator", "sample-dev", body)
        self.run_cli("send", "--as", "sample-orchestrator", "sample-dev", "short one")
        code, out, _ = self.run_cli("inbox", "--as", "sample-dev", "--read")
        self.assertEqual(code, 0)
        first, second = policy_output(out).rstrip("\n").split("\n\n")
        lines = first.splitlines()
        self.assertEqual(lines[1:11], ["line %02d...." % i for i in range(10)])
        (path,) = [m.path for m in inbox.read_box(self.repo, "sample-dev", CUR) if "line 29" in m.body]
        self.assertEqual(lines[11], "… %d B in full: %s" % (len(body.encode()), path))
        self.assertEqual(len(lines), 12)
        self.assertTrue(second.endswith("\nshort one"))
        self.assertEqual(len(box(self.repo, "sample-dev", NEW)), 0)  # the delivery is still recorded
        self.assertEqual([e["via"] for e in log_entries(self.repo) if e["kind"] == "delivered"], ["inbox", "inbox"])
        history = self.run_cli("inbox", "--as", "sample-dev", "--history", "sample-orchestrator")[1]
        self.assertIn("… %d B in full: %s" % (len(body.encode()), path), history)
        self.assertNotIn("line 29", history)
        full = self.run_cli("inbox", "--as", "sample-dev", "--history", "sample-orchestrator", "--full")[1]
        self.assertIn("line 29....", full)
        self.assertEqual(self.run_cli("inbox", "--as", "sample-dev", "--full")[0], 2)  # --full needs --read/--history

    def test_validation(self):
        for value in ("0", "-1", '"8k"', "true"):
            self.write_fleet("inbox_print_max = %s\n" % value + THREE_SEATS)
            with self.assertRaises(FleetError):
                load_fleet(os.path.join(self.repo, ".herdr", "fleet.toml"))
        self.write_fleet(THREE_SEATS)
        self.assertEqual(load_fleet(os.path.join(self.repo, ".herdr", "fleet.toml")).inbox_print_max, 8192)


# -- 20.5 state_seen and blocked (4m) ---------------------------------------------------------------


class StateSeen(LiveFleet):
    def seen(self, name):
        return self.read_state()["agents"][name].get("state_seen")

    def test_recorded_by_status_send_deliver_and_inbox(self):
        clock = Clock()
        with wall(clock):
            self.status_of("w2:p2", "blocked")
            self.run_cli("status")
            self.assertEqual(self.seen("sample-dev"), {"state": "blocked", "since": iso(T0)})
            clock.advance(minutes=4)
            out = self.run_cli("status")[1]
            self.assertIn("blocked (4m)", out)
            self.assertEqual(self.seen("sample-dev")["since"], iso(T0))  # unchanged state, unchanged since
            data = json.loads(self.run_cli("status", "--json")[1])
            self.assertEqual({s["name"]: s["state_since"] for s in data["seats"]}["sample-dev"], iso(T0))
            self.status_of("w2:p2", "idle")
            clock.advance(minutes=1)
            self.run_cli("send", "--queue", "sample-dev", "x")  # send records what it saw
            self.assertEqual(self.seen("sample-dev"), {"state": "idle", "since": iso(clock.now)})
            self.status_of("w2:p3", "unknown")
            clock.advance(minutes=1)
            self.run_cli("deliver")
            self.assertEqual(self.seen("sample-reviewer"), {"state": "unknown", "since": iso(clock.now)})
            clock.advance(minutes=12)
            self.assertIn("unknown (12m)", self.run_cli("status")[1])
            self.assertNotIn("idle (", self.run_cli("status")[1])  # other states print as before
            env = dict(self.env, HERDR_PANE_ID="w2:p1")  # inbox records the caller's own state
            self.status_of("w2:p1", "working")
            self.run_cli("inbox", env=env)
            self.assertEqual(self.seen("sample-orchestrator"), {"state": "working", "since": iso(clock.now)})

    def test_save_keeps_the_record_on_disk(self):
        state = state_mod.load_state(self.repo)
        state_mod.record_seen(self.repo, {"sample-dev": "blocked"}, T0)
        state.update("sample-dev", note="x")
        state_mod.save_state(state)
        self.assertEqual(self.seen("sample-dev"), {"state": "blocked", "since": iso(T0)})
        self.assertEqual(self.read_state()["agents"]["sample-dev"]["note"], "x")


class StateSeenWithoutStateFile(FleetCase):
    def test_nothing_is_written(self):
        self.fleet_ws()
        fake_herdr.put_agent(self.world, "w2:p1", "claude", "sample-orchestrator", status="blocked")
        self.assertIn("blocked", self.run_cli("status")[1])
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".herdr", "fleet.state.json")))


class BlockedNotification(WatchCase):
    fleet_text = THREE_SEATS + '\n[watch]\nnotify_blocked_after = "3m"\n'

    def test_once_per_blocked_episode(self):
        self.status_of("w2:p2", "blocked")
        start = self.clock.now
        moments = {}

        def world_edit(passes, status):  # change the seat's state in the fake world mid-run
            with open(self.world_path) as handle:
                world = json.load(handle)
            world["panes"]["w2:p2"]["agent"]["agent_status"] = status
            with open(self.world_path, "w") as handle:
                json.dump(world, handle)
            moments[passes] = status

        def script():
            if self.clock.passes == 130 and 130 not in moments:
                world_edit(130, "idle")
            if self.clock.passes == 150 and 150 not in moments:
                world_edit(150, "blocked")
            return False

        self.clock.until = script
        wall_now = lambda: T0 + datetime.timedelta(seconds=self.clock.now - start)  # noqa: E731
        with mock.patch.object(inbox, "now_utc", wall_now):
            code, out, _ = self.watch(cycles=260)
        self.assertEqual(code, 0, out)
        notes = [n for n in self.world["notifications"] if "blocked" in n["title"]]
        self.assertEqual(len(notes), 2, notes)
        self.assertRegex(notes[0]["title"], r"^sample-dev blocked 3m \(w2:p2\): answer its prompt$")
        self.assertEqual(notes[0]["sound"], "request")
        self.assertEqual(self.prompts_to("sample-dev"), [])  # never touched
        self.assertEqual([c["argv"][:2] for c in self.calls() if c["argv"][:2] not in (
            ["agent", "list"], ["--version"], ["notification", "show"])], [])  # no wait, get or prompt
        self.assertTrue(self.read_state()["agents"]["sample-dev"]["state_seen"]["notified"])
        # a restarted watcher does not notify the same episode again
        self.clock.until = None
        with mock.patch.object(inbox, "now_utc", wall_now):
            self.watch(cycles=20)
        self.assertEqual(len([n for n in self.world["notifications"] if "blocked" in n["title"]]), 2)

    def test_the_setting(self):
        path = os.path.join(self.repo, ".herdr", "fleet.toml")
        self.assertEqual(load_fleet(path).notify_blocked_after, 180)
        self.write_fleet(THREE_SEATS + '\n[watch]\nnotify_blocked_after = "soon"\n')
        with self.assertRaises(FleetError):
            load_fleet(path)
        self.write_fleet(THREE_SEATS)
        self.assertIsNone(load_fleet(path).notify_blocked_after)


# -- 20.8: queued_by and the drain at the first opportunity --------------------------------------------


class QueuedBy(LiveFleet):
    def header(self, name):
        (message,) = inbox.read_box(self.repo, name, NEW)
        with open(message.path) as handle:
            return handle.read().split("\n\n", 1)[0].splitlines()

    def test_how_each_send_waits(self):
        cases = ((["sample-dev", "x"], "refusal"), (["--queue", "sample-dev", "x"], "queue"),
                 (["--fyi", "sample-dev", "x"], "fyi"), (["sample-dev", "FYI: by its prefix"], "fyi"),
                 (["--when-ready", "100", "sample-dev", "x"], "queue"))
        for argv, expected in cases:
            with self.subTest(argv=argv):
                for folder in (NEW, CUR):
                    for f in box(self.repo, "sample-dev", folder):
                        os.remove(os.path.join(self.repo, ".herdr", "inbox", "sample-dev", folder, f + ".md"))
                self.status_of("w2:p2", "blocked")
                self.run_cli("send", *argv)
                self.assertIn("queued_by: %s" % expected, self.header("sample-dev"))

    def test_files_without_the_header_count_as_queued(self):
        put(self.repo, "sample-dev", "old plain")
        put(self.repo, "sample-dev", "old fyi", fyi=True)
        lines = policy_output(self.run_cli("inbox", "--as", "sample-dev")[1]).splitlines()
        self.assertTrue(lines[0].endswith("  [queued]  old plain"), lines)
        self.assertTrue(lines[1].endswith("  [fyi]  old fyi"), lines)
        self.status_of("w2:p2", "working")
        self.assertEqual(self.run_cli("deliver")[1].strip(), "sample-dev: working, 2 left queued")
        self.assertEqual(self.prompts_to("sample-dev"), [])

    def test_the_fyi_line_says_when_it_goes(self):
        code, out, _ = self.run_cli("send", "--fyi", "sample-dev", "build green")
        self.assertEqual(code, 0)
        self.assertRegex(policy_output(out).strip(), r"^queued in herdr-fleet for sample-dev \(idle, %s\) %s, position 1 of 1$"
                         % (re.escape(FYI_PLACE), SHORT))
        self.assertEqual(self.prompts_to("sample-dev"), [])


class DeliverDrains(LiveFleet):
    def test_refusal_and_queue_mailboxes_wait_alike_for_a_working_seat(self):  # design 21.1 withdrew the drain
        self.status_of("w2:p2", "blocked")
        self.status_of("w2:p3", "blocked")
        self.run_cli("send", "--as", "sample-orchestrator", "sample-dev", "needs you")
        self.run_cli("send", "--as", "sample-orchestrator", "--queue", "sample-reviewer", "when idle")
        self.status_of("w2:p2", "working")
        self.status_of("w2:p3", "working")
        code, out, _ = self.run_cli("deliver")
        self.assertEqual(code, 0, out)
        self.assertEqual(policy_output(out).splitlines(), ["sample-dev: working, 1 left queued",
                                            "sample-reviewer: working, 1 left queued"])
        self.assertEqual(self.argvs("agent", "prompt"), [])
        self.status_of("w2:p2", "idle")
        self.status_of("w2:p3", "done")
        code, out, _ = self.run_cli("deliver")
        self.assertRegex(policy_output(out).splitlines()[0], r"^delivered 1 to sample-dev \(idle\), \d+ B$")
        self.assertRegex(policy_output(out).splitlines()[1], r"^delivered 1 to sample-reviewer \(done\), \d+ B$")

    def test_now_types_a_whole_mailbox_ahead_by_hand(self):
        self.status_of("w2:p3", "working")
        self.run_cli("send", "--queue", "sample-reviewer", "one")
        self.run_cli("send", "--queue", "sample-reviewer", "two")
        self.assertEqual(self.run_cli("deliver", "--now")[0], 2)
        code, out, _ = self.run_cli("deliver", "sample-reviewer", "--now")
        self.assertEqual(code, 0, out)
        self.assertRegex(policy_output(out).strip(), r"^delivered 2 to sample-reviewer \(%s\), \d+ B$" % WORKING)
        self.status_of("w2:p3", "blocked")
        self.run_cli("send", "--queue", "sample-reviewer", "three")
        code, out, _ = self.run_cli("deliver", "sample-reviewer", "--now")
        self.assertEqual(policy_output(out).strip(), "sample-reviewer: blocked: a dialog is open, 1 left queued")  # never blocked
        self.assertEqual(len(self.prompts_to("sample-reviewer")), 1)


class FlushDrains(LiveFleet):
    def test_a_later_queue_send_takes_refusal_messages_along_only_to_an_idle_seat(self):  # 20.9 as 21.1 amends it
        self.status_of("w2:p2", "blocked")
        self.run_cli("send", "--as", "sample-orchestrator", "sample-dev", "first")
        self.status_of("w2:p2", "working")
        code, out, _ = self.run_cli("send", "--as", "sample-orchestrator", "--queue", "sample-dev", "second")
        self.assertEqual(code, 0, out)
        self.assertRegex(policy_output(out).strip(), r"^queued in herdr-fleet for sample-dev \(working\) %s, position 2 of 2$" % SHORT)
        self.assertEqual(self.prompts_to("sample-dev"), [])  # a working seat: nothing flushed
        self.status_of("w2:p2", "idle")
        code, out, _ = self.run_cli("send", "--as", "sample-orchestrator", "--queue", "sample-dev", "third")
        self.assertEqual(code, 0, out)
        lines = policy_output(out).splitlines()
        self.assertRegex(lines[0], r"^queued in herdr-fleet for sample-dev \(idle\) %s, position 1 of 1$" % SHORT)
        self.assertRegex(lines[1], r"^delivered 2 queued on refusal to sample-dev \(idle\), \d+ B$")
        (prompt,) = self.prompts_to("sample-dev")  # the refusal message takes its mailbox along (20.9)
        self.assertLess(prompt.index("first"), prompt.index("second"))
        self.assertNotIn("third", prompt)
        self.assertEqual([m.body for m in inbox.read_box(self.repo, "sample-dev", NEW)], ["third"])

    def test_queue_messages_are_not_flushed(self):
        self.status_of("w2:p2", "blocked")
        self.run_cli("send", "--queue", "sample-dev", "first")
        self.status_of("w2:p2", "working")
        code, out, _ = self.run_cli("send", "--queue", "sample-dev", "second")
        self.assertRegex(policy_output(out).strip(), r"^queued in herdr-fleet for sample-dev \(working\) %s, position 2 of 2$" % SHORT)
        self.assertEqual(self.prompts_to("sample-dev"), [])

    def test_an_fyi_rides_along_with_a_refusal_message_to_an_idle_seat(self):
        self.status_of("w2:p2", "blocked")
        self.run_cli("send", "sample-dev", "first")
        self.status_of("w2:p2", "idle")
        code, out, _ = self.run_cli("send", "--fyi", "sample-dev", "report")
        self.assertRegex(policy_output(out).strip(), r"^sent to sample-dev \(idle\) with 1 queued, \d+ B$")
        self.assertEqual(box(self.repo, "sample-dev", NEW), [])


class WatcherDrains(WatchCase):
    def queue_on_refusal(self, name, pane, text):
        self.status_of(pane, "blocked")
        self.run_cli("send", "--as", "sample-orchestrator", name, text)

    def test_blocked_to_working_is_not_typed_ahead(self):  # design 21.1: the --until working wait went
        self.queue_on_refusal("sample-dev", "w2:p2", "unblock then read")
        self.behave(wait_results={"sample-dev": ["working", "idle"]})
        code, out, err = self.watch(cycles=3)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(events(out)[1:], ["waiting on sample-dev (1 queued)", "sample-dev idle; delivering 1",
                                           "delivered 1 to sample-dev", "stopped"])
        self.assertEqual(self.argvs("agent", "wait", "sample-dev"), [["agent", "wait", "sample-dev"] + WAIT] * 2)
        (call,) = [c for c in self.calls() if c["argv"][:2] == ["agent", "prompt"]]
        self.assertEqual(call["info"]["target_status"], "idle")
        self.assertIn("unblock then read", call["argv"][3])
        self.assertEqual([e["via"] for e in log_entries(self.repo) if e["kind"] == "delivered"], ["watch"])

    def test_idle_or_done_still_delivers_after_the_debounce(self):
        self.queue_on_refusal("sample-dev", "w2:p2", "x")
        self.behave(wait_results={"sample-dev": ["done"]})
        before = self.clock.slept
        code, out, _ = self.watch(cycles=1)
        self.assertIn("sample-dev done; delivering 1", events(out))
        self.assertEqual(self.clock.slept - before, 5.0 + 2.0)

    def test_every_mailbox_waits_for_idle_or_done_only_and_mixed_ones_go_whole(self):
        self.status_of("w2:p3", "blocked")
        self.run_cli("send", "--queue", "sample-reviewer", "wait for idle")
        self.status_of("w2:p3", "working")
        self.behave(wait_results={"sample-reviewer": ["timeout"]})
        self.watch(cycles=1)
        self.assertEqual(self.argvs("agent", "wait", "sample-reviewer"), [["agent", "wait", "sample-reviewer"] + WAIT])
        self.assertEqual(self.prompts_to("sample-reviewer"), [])
        self.queue_on_refusal("sample-reviewer", "w2:p3", "go now")
        self.behave(wait_results={"sample-reviewer": ["working", "idle"]})
        code, out, _ = self.watch(cycles=2)
        self.assertIn("sample-reviewer idle; delivering 2", events(out))
        self.assertNotIn(AHEAD_WAIT, [a[3:] for a in self.argvs("agent", "wait")])
        (prompt,) = self.prompts_to("sample-reviewer")
        self.assertLess(prompt.index("wait for idle"), prompt.index("go now"))  # order is never inverted

    def test_once_leaves_a_working_seat_alone(self):  # design 21.1: watch --once follows deliver
        self.queue_on_refusal("sample-dev", "w2:p2", "x")
        self.status_of("w2:p2", "working")
        code, out, _ = self.run_cli("watch", "--once")
        self.assertEqual((code, policy_output(out).splitlines()[0]), (0, "sample-dev: working, 1 left queued"))
        self.assertEqual(self.prompts_to("sample-dev"), [])


# -- 20.8: kickoffs settle debts ---------------------------------------------------------------------


class KickoffBoundary(LiveFleet):
    def test_a_kickoff_settles_debts_both_ways_but_not_what_it_carries(self):
        self.run_cli("send", "--as", "sample-orchestrator", "sample-dev", "task for dev")
        self.run_cli("send", "--as", "sample-dev", "sample-reviewer", "question from dev")
        self.run_cli("send", "--as", "sample-reviewer", "sample-orchestrator", "unrelated")
        self.status_of("w2:p1", "blocked")
        self.run_cli("send", "--as", "sample-reviewer", "sample-orchestrator", "carried in")
        owed = json.loads(self.run_cli("log", "--owed", "--json")[1])
        self.assertEqual(len(owed), 3)
        code, out, _ = self.run_cli("kickoff", "sample-dev")
        self.assertEqual(code, 0, out)
        owed = json.loads(self.run_cli("log", "--owed", "--json")[1])
        self.assertEqual([(o["from"], o["to"]) for o in owed], [("sample-reviewer", "sample-orchestrator")])
        self.status_of("w2:p1", "idle")
        self.run_cli("kickoff", "sample-orchestrator")  # carries "carried in": that one is still owed
        owed = json.loads(self.run_cli("log", "--owed", "--json")[1])
        self.assertEqual([(o["from"], o["first_line"]) for o in owed], [("sample-reviewer", "carried in")])

    def test_unit_rule(self):
        t = T0
        log = [raw_entry("a", "s", t, first="to s"), raw_entry("s", "b", t + datetime.timedelta(minutes=1),
                                                               first="from s"),
               raw_entry("herdr-fleet", "s", t + datetime.timedelta(minutes=2), kind="kickoff"),
               raw_entry("c", "s", t + datetime.timedelta(minutes=3), first="after")]
        self.assertEqual([o["first_line"] for o in messages.owed(log)], ["after"])


# -- 20.8: cross-fleet logging ------------------------------------------------------------------------

OTHER_FLEET = """
[[agents]]
name = "peer-coordinator"
kind = "claude"
"""


class CrossFleet(LiveFleet):
    def setUp(self):
        super().setUp()
        self.other = os.path.join(self.tmp, "winner")
        os.makedirs(os.path.join(self.other, ".herdr"))
        with open(os.path.join(self.other, ".herdr", "fleet.toml"), "w") as handle:
            handle.write(OTHER_FLEET)
        fake_herdr.add_workspace(self.world, "w3", "winner", os.path.join(self.other))
        fake_herdr.put_agent(self.world, "w3:p1", "claude", "peer-coordinator")
        self.lonely = os.path.join(self.tmp, "nowhere")
        os.makedirs(self.lonely)
        fake_herdr.add_pane(self.world, "w3:p2", "w3", self.lonely)
        fake_herdr.put_agent(self.world, "w3:p2", "claude", "loner")

    def other_log(self):
        return log_entries(self.other)

    def test_both_sides_see_the_message_the_debt_and_the_reply(self):
        code, out, _ = self.run_cli("send", "--as", "sample-orchestrator", "peer-coordinator", "Review the API")
        self.assertEqual(code, 0, out)
        here = log_entries(self.repo)[0]
        self.assertEqual(log_entries(self.repo), self.other_log())
        self.assertEqual([e["kind"] for e in self.other_log()], ["send", "delivered"])
        stored = os.path.join(self.other, ".herdr", "inbox", "peer-coordinator", CUR, here["id"] + ".md")
        with open(stored) as handle:
            self.assertTrue(handle.read().endswith("\n\nReview the API"))
        other_cli = dict(cwd=self.other)
        owed = self.run_cli("log", "--owed", **other_cli)[1]
        self.assertRegex(owed, r"^awaiting: peer-coordinator owes sample-orchestrator for .*: Review the API")
        history = self.run_cli("inbox", "--as", "peer-coordinator", "--history", "sample-orchestrator", **other_cli)[1]
        self.assertIn("\nReview the API", history)
        code, out, _ = self.run_cli("send", "--as", "peer-coordinator", "--fyi", "--reply-to", "#" + here["id"][-6:],
                                    "sample-orchestrator", "Reviewed: two notes", **other_cli)
        self.assertEqual(code, 0, out)
        self.assertEqual(log_entries(self.repo)[-1]["reply_to"], here["id"])  # landed in this fleet's log too
        self.assertEqual(json.loads(self.run_cli("log", "--owed", "--json")[1]), [])
        self.assertEqual(json.loads(self.run_cli("log", "--owed", "--json", **other_cli)[1]), [])

    def test_a_queued_message_is_mirrored_and_so_is_its_later_delivery(self):
        self.status_of("w3:p1", "blocked")
        self.run_cli("send", "--as", "sample-orchestrator", "peer-coordinator", "when you can")
        self.assertEqual([e["delivery"] for e in self.other_log()], ["queued"])
        self.assertEqual(self.run_cli("log", "--owed", cwd=self.other)[1].strip(), "nothing awaiting a reply")
        self.status_of("w3:p1", "idle")
        self.run_cli("deliver", "peer-coordinator", "--fleet", os.path.join(self.other, ".herdr", "fleet.toml"))
        self.assertEqual([(e["kind"], e.get("via")) for e in self.other_log()], [("send", None), ("delivered", "deliver")])
        self.assertIn("awaiting: peer-coordinator owes sample-orchestrator", self.run_cli("log", "--owed",
                                                                                         cwd=self.other)[1])

    def test_no_fleet_and_same_fleet_are_logged_here_only(self):
        self.run_cli("send", "loner", "hello")
        self.assertFalse(os.path.exists(os.path.join(self.lonely, ".herdr")))
        fake_herdr.add_pane(self.world, "w2:p9", "w2", os.path.join(self.repo))
        fake_herdr.put_agent(self.world, "w2:p9", "claude", "helper")
        self.run_cli("send", "helper", "hello")
        self.assertEqual(len(log_entries(self.repo)), 2)
        self.assertEqual(self.other_log(), [])

    def test_a_new_contact_route_is_owned_and_logged_at_its_canonical_destination(self):
        self.write_fleet(self.read_fleet() + '\n[[contacts]]\nname = "winner"\nmatch = { name = "peer-coordinator" }\n')
        self.run_cli("send", "winner", "via the contact")
        self.assertEqual(log_entries(self.repo), self.other_log())
        self.assertEqual({e["to"] for e in self.other_log()}, {"peer-coordinator"})


# -- 20.8: queue ages, the inbox hint, the roster sentence, the harness --------------------------------


class QueueAgesAndHint(LiveFleet):
    def test_queue_ages(self):
        clock = Clock()
        with wall(clock):
            self.status_of("w2:p2", "blocked")
            self.run_cli("send", "sample-dev", "a")
            clock.advance(minutes=20)
            self.run_cli("send", "sample-dev", "b")
            self.run_cli("send", "--queue", "owner", "c")
            clock.advance(minutes=5)
            (line,) = [l for l in self.run_cli("status")[1].splitlines() if l.startswith("queued:")]
        self.assertEqual(line, "queued: sample-dev 2 (oldest 25m), owner 1")

    def test_the_hint_names_the_senders_own_mailbox(self):  # design 23.1: now a note, printed first
        self.run_cli("send", "--queue", "--as", "sample-dev", "sample-orchestrator", "for you")
        code, out, _ = self.run_cli("send", "--as", "sample-orchestrator", "sample-dev", "hi")
        self.assertRegex(policy_output(out).splitlines()[0], r"^note: 1 unread from sample-dev \(#[0-9a-f]{6}, \d\d:\d\d\); read it "
                                              r"before you go on: herdr-fleet inbox --as sample-orchestrator --read$")
        self.run_cli("send", "--queue", "--as", "sample-dev", "caller", "for the caller")
        code, out, _ = self.run_cli("send", "sample-dev", "hi")
        self.assertRegex(policy_output(out).splitlines()[0], r"^note: 1 unread from sample-dev \(#[0-9a-f]{6}, \d\d:\d\d\); read it "
                                              r"before you go on: herdr-fleet inbox --read$")
        self.run_cli("inbox", "--read")
        self.assertNotIn("waiting", self.run_cli("send", "sample-dev", "hi")[1])


class Roster(unittest.TestCase):
    def test_the_sentence_says_how_to_settle_and_how_to_order(self):
        text = roster_table([("a", "claude", "w1:p1", "/r")])
        self.assertTrue(text.endswith(ROSTER_SENTENCE))
        self.assertIn("When you have acted on a request, reply with `--fyi` or `ack #id`, since the plugin cannot "
                      "see what you did.", ROSTER_SENTENCE)
        self.assertIn("To control order, queue (`--queue`) and let the watcher or the next kickoff deliver in one "
                      "batch.", ROSTER_SENTENCE)


class Harness(LiveFleet):
    def check(self, command, argv, info):
        return typed_ahead_allowed(command, argv, info)

    def test_who_may_prompt_a_working_seat(self):  # design 21.1: send --now and deliver NAME --now only
        prompt = ["agent", "prompt", "sample-dev", "[from x at 10:09 #abcdef]\nhi"]
        self.assertTrue(self.check(["send", "--now", "sample-dev", "hi"], prompt, {}))
        self.assertFalse(self.check(["send", "sample-dev", "hi"], prompt, {}))
        self.assertFalse(self.check(["send", "--queue", "sample-dev", "hi"], prompt, {}))
        self.assertTrue(self.check(["deliver", "sample-dev", "--now"], prompt, {}))
        self.assertFalse(self.check(["deliver"], prompt, {"target_name": "sample-dev"}))
        self.assertFalse(self.check(["deliver"], prompt, {"target_name": "sample-dev", "refusal_boxes": ["sample-dev"]}))
        self.assertFalse(self.check(["watch"], prompt, {"target_name": "w2:p9", "refusal_ids": ["abcdef"]}))
        self.assertFalse(self.check(["kickoff", "sample-dev"], prompt, {"refusal_boxes": ["sample-dev"]}))
        with open(self.log_path, "a") as handle:
            handle.write(json.dumps({"command": ["kickoff", "sample-dev"]}) + "\n")
            handle.write(json.dumps({"argv": prompt, "exit": 0, "info": {"target_status": "working",
                                                                        "refusal_boxes": ["sample-dev"]}}) + "\n")
        with self.assertRaises(AssertionError):
            self.assert_safety()
        os.remove(self.log_path)


if __name__ == "__main__":
    unittest.main()
