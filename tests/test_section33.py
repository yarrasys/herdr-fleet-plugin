"""Design section 33 (version 0.18.0): queues that read true.

33.1: an FYI without a ttl of its own is due once the fleet's ``fyi_ttl`` (default 24h,
``"off"`` to keep them) has passed; read-only paths stop counting it, the paths that expire
under the mailbox lock move it, and the watcher sweeps every mailbox at most once every 300 s
without waiting for a lock, running no herdr command. 33.2: FYIs are counted apart in
status, its JSON, the board's queue cell, the fleet page's ``queued:`` line and the unread
note. 33.3: a mailbox that is no seat, contact or owner is flagged in status and doctor, and
``inbox --drop`` moves queued messages to ``cur/``. 33.4: ``ack --older-than D --reason
TEXT`` closes old debts whoever owes them, and status folds debts older than 3 days into one
line. 33.5: printed ``herdr-fleet inbox`` commands keep ``--fleet`` when discovery from the
caller's cwd would not find the fleet.

``tests/support.py`` checks every scenario as before."""

from __future__ import annotations

import datetime
import io
import json
import os
import re
import shutil
import unittest
from unittest import mock

import fake_herdr
from support import CALLER_PANE, FakeClock, LiveFleet, policy_output
from herdr_fleet import board as B, inbox, mailroom, messages, timers, watch
from herdr_fleet.context import Ctx
from herdr_fleet.herdr import Herdr
from herdr_fleet.inbox import CUR, NEW, Message
from test_inbox import box, log_entries, put as put_file
from test_section20 import T0, Clock, raw_entry, wall
from support import ARGS
from test_section24 import BoardCase
from test_section30 import NOW, TimerCase
from test_section31 import FleetPair
from test_watch import WatchCase, events

DAY = datetime.timedelta(days=1)
HHMM = r"\d\d:\d\d"


def ago(**delta):
    return inbox.now_utc() - datetime.timedelta(**delta)


def put(repo, to, body, when=None, **kwargs):
    """A message file as an earlier send left it: its ``time`` and its file's modification
    time both ``when`` (33.1 counts the FYI window from the later of the two)."""
    message = put_file(repo, to, body, when=when, **kwargs)
    if when is not None:
        inbox.touch(message, when)
    return message


def outside(env):
    """The owner's environment outside Herdr: no HERDR_ENV, no HERDR_BIN_PATH."""
    return {k: v for k, v in env.items() if k not in ("HERDR_ENV", "HERDR_BIN_PATH")}


def kickoff(repo, to, body="[from herdr-fleet] You are the developer.", when=None):
    """A queued kickoff (32.6), as up, restart or swap leave it."""
    when = when or inbox.now_utc()
    message = Message(id=inbox.new_id(when, inbox.taken_shorts(repo)), sender="herdr-fleet", to=to,
                      time=inbox.iso_local(when), body=body, queued_by="kickoff", kickoff_reason="restart")
    return inbox.write(repo, message)


def expired(repo):
    return [e for e in log_entries(repo) if e.get("kind") == "expired"]


def no_herdr(argv):
    raise AssertionError("the sweep ran a herdr command: %s" % (argv,))


def tree(root):
    """Every file under ``root`` with its size and mtime: a write shows up as a change."""
    found = {}
    for folder, _, files in os.walk(root):
        for name in files:
            path = os.path.join(folder, name)
            stat = os.stat(path)
            found[path] = (stat.st_size, stat.st_mtime_ns)
    return found


# -- 33.1 FYIs expire after fyi_ttl ---------------------------------------------------------------


class FyiExpiry(WatchCase):
    def queued_json(self):
        data = json.loads(self.run_cli("status", "--json")[1])
        return data["queued"], data.get("queued_fyi")

    def test_an_old_fyi_is_not_counted_and_the_sweep_expires_it(self):
        old = put(self.repo, "sample-dev", "FYI: the parser landed", fyi=True, when=ago(hours=25))
        self.assertEqual(self.queued_json()[0], {})
        self.assertNotIn("queued:", self.run_cli("status")[1])
        row = next(r for r in self.board().rows if r.name == "sample-dev")
        self.assertEqual((row.queued, row.fyi), (0, 0))
        self.assertEqual(box(self.repo, "sample-dev", NEW), [old.id])  # read-only paths move nothing
        self.assertEqual(expired(self.repo), [])
        code, out, err = self.watch(cycles=1)
        self.assertEqual(code, 0, out + err)
        self.assertIn("expired 1 queued: sample-dev 1 (fyi_ttl)", events(out))
        self.assertEqual(box(self.repo, "sample-dev", CUR), [old.id])
        (entry,) = expired(self.repo)
        self.assertEqual((entry["id"], entry["to"], entry["via"], entry["delivery"]),
                         (old.id, "sample-dev", "fyi_ttl", "none"))
        self.assertEqual(self.prompts_to("sample-dev"), [])

    def test_a_younger_fyi_is_kept(self):
        young = put(self.repo, "sample-dev", "FYI: still fresh", fyi=True, when=ago(hours=23))
        self.assertEqual(self.queued_json(), ({"sample-dev": 1}, {"sample-dev": 1}))
        self.watch(cycles=1)
        self.assertEqual(box(self.repo, "sample-dev", NEW), [young.id])
        self.assertEqual(expired(self.repo), [])

    def test_off_keeps_every_fyi(self):
        self.write_fleet('fyi_ttl = "off"\n' + self.read_fleet())
        kept = put(self.repo, "sample-dev", "FYI: from last month", fyi=True, when=ago(days=30))
        self.assertEqual(self.queued_json(), ({"sample-dev": 1}, {"sample-dev": 1}))
        self.watch(cycles=1)
        self.assertEqual(box(self.repo, "sample-dev", NEW), [kept.id])
        self.assertEqual(expired(self.repo), [])

    def test_a_shorter_window_and_bad_values(self):
        self.write_fleet('fyi_ttl = "30m"\n' + self.read_fleet())
        put(self.repo, "sample-dev", "FYI: an hour old", fyi=True, when=ago(hours=1))
        self.assertEqual(self.queued_json()[0], {})
        for value in ('"0m"', '"1w"', '"never"', "24"):
            with self.subTest(value=value):
                self.write_fleet("fyi_ttl = %s\n" % value + self.read_fleet().split("\n", 1)[1])
                code, _, err = self.run_cli("status")
                self.assertEqual(code, 2)
                self.assertIn("fyi_ttl", err)

    def test_its_own_ttl_wins_both_ways(self):
        longer = put(self.repo, "sample-dev", "FYI: kept three days", fyi=True, when=ago(hours=30),
                     ttl=inbox.now_utc() + 2 * DAY)
        shorter = put(self.repo, "sample-dev", "FYI: gone in a minute", fyi=True, when=ago(hours=1),
                      ttl=ago(minutes=1))
        self.assertEqual(self.queued_json(), ({"sample-dev": 1}, {"sample-dev": 1}))
        out = self.watch(cycles=1)[1]
        self.assertIn("expired 1 queued: sample-dev 1 (ttl)", events(out))
        self.assertEqual(box(self.repo, "sample-dev", NEW), [longer.id])
        (entry,) = expired(self.repo)
        self.assertEqual((entry["id"], entry["via"]), (shorter.id, "ttl"))

    def test_the_watcher_names_the_reasons_that_applied(self):
        put(self.repo, "sample-dev", "Review by noon", when=ago(hours=3), ttl=ago(hours=1))
        put(self.repo, "sample-reviewer", "FYI: old news", fyi=True, when=ago(days=2))
        out = self.watch(cycles=1)[1]
        self.assertIn("expired 2 queued: sample-dev 1, sample-reviewer 1 (ttl, fyi_ttl)", events(out))
        self.assertEqual(sorted(e["via"] for e in expired(self.repo)), ["fyi_ttl", "ttl"])

    def test_requests_and_queued_kickoffs_never_expire_by_fyi_ttl(self):
        self.status_of("w2:p2", "blocked")
        request = put(self.repo, "sample-dev", "Review the parser", when=ago(days=10))
        brief = kickoff(self.repo, "sample-dev", when=ago(days=10))
        self.assertEqual(self.queued_json(), ({"sample-dev": 2}, {"sample-dev": 0}))
        self.assertEqual(self.run_cli("deliver")[0], 0)  # expires what is due in every mailbox first
        self.assertEqual(policy_output(self.run_cli("inbox", "--as", "sample-dev", "--count")[1]).strip(), "2")
        self.behave(wait_results={"sample-dev": ["timeout"] * 3})
        self.watch(cycles=1)
        self.assertEqual(sorted(box(self.repo, "sample-dev", NEW)), sorted([request.id, brief.id]))
        self.assertEqual(expired(self.repo), [])

    def test_the_paths_that_expire_under_the_lock_use_fyi_ttl(self):
        old = put(self.repo, "sample-dev", "FYI: old news", fyi=True, when=ago(days=2))
        code, out, _ = self.run_cli("inbox", "--as", "sample-dev", "--read")
        self.assertEqual(policy_output(out).strip(), "nothing queued for sample-dev")
        self.assertEqual([(e["id"], e["via"]) for e in expired(self.repo)], [(old.id, "fyi_ttl")])

    def test_a_redelivered_fyi_gets_a_fresh_window(self):
        old = put(self.repo, "sample-dev", "FYI: worth a second look", fyi=True, when=ago(days=2))
        self.run_cli("deliver")
        self.assertEqual(box(self.repo, "sample-dev", CUR), [old.id])
        clock = Clock(inbox.now_utc())
        ctx = Ctx(Herdr("/fake/bin/herdr", no_herdr), self.env, FakeClock(), io.StringIO(), io.StringIO(), self.repo)
        with wall(clock):
            code, out, _ = self.run_cli("inbox", "--as", "sample-dev", "--redeliver", "#" + old.short)
            self.assertEqual((code, policy_output(out).strip()),
                             (0, "#%s is queued again for sample-dev; the next delivery carries it" % old.short))
            self.assertEqual(mailroom.sweep_expired(ctx), {})  # the next sweep keeps it
            self.assertEqual(json.loads(self.run_cli("status", "--json")[1])["queued_fyi"], {"sample-dev": 1})
            clock.advance(hours=23, minutes=59)
            self.assertEqual(mailroom.sweep_expired(ctx), {})
            clock.advance(minutes=2)  # its fresh window has passed: it expires again
            self.assertEqual(mailroom.sweep_expired(ctx), {"sample-dev": ["fyi_ttl"]})
            self.assertEqual([(e["id"], e["via"]) for e in expired(self.repo)], [(old.id, "fyi_ttl")] * 2)
            self.run_cli("inbox", "--as", "sample-dev", "--redeliver", "#" + old.short)
            code, out, _ = self.run_cli("inbox", "--as", "sample-dev", "--read")  # and a read gets it
        self.assertIn("FYI: worth a second look", out)
        self.assertEqual(box(self.repo, "sample-dev", CUR), [old.id])
        self.assertEqual(len(expired(self.repo)), 2)
        self.assertEqual(log_entries(self.repo)[-1]["kind"], "delivered")

    def test_a_message_past_its_own_ttl_keeps_it_when_redelivered(self):
        late = put(self.repo, "sample-dev", "Review by noon", when=ago(hours=3), ttl=ago(hours=1), which=CUR)
        code, out, _ = self.run_cli("inbox", "--as", "sample-dev", "--redeliver", "#" + late.short)
        self.assertEqual((code, policy_output(out).strip()),
                         (0, "#%s is queued again for sample-dev, but it is past its ttl, so the next delivery or "
                             "read expires it again" % late.short))
        self.run_cli("inbox", "--as", "sample-dev", "--read")
        self.assertEqual([(e["id"], e["via"]) for e in expired(self.repo)], [(late.id, "ttl")])

    def test_a_redelivered_message_sorts_after_its_same_second_siblings(self):
        when = ago(hours=1)
        first = put(self.repo, "sample-dev", "first", when=when, which=CUR)
        second = put(self.repo, "sample-dev", "second", when=when, which=CUR)
        inbox.touch(second, when + datetime.timedelta(microseconds=1))  # written just after, in the same second
        self.assertEqual([m.id for m in inbox.read_box(self.repo, "sample-dev", CUR)], [first.id, second.id])
        for message in (second, first):
            self.run_cli("inbox", "--as", "sample-dev", "--redeliver", "#" + message.short)
        # the id's second decides; within it, the later modification time (the redelivery) goes last
        self.assertEqual([m.id for m in inbox.read_box(self.repo, "sample-dev", NEW)], [second.id, first.id])

    def board(self):
        with open(self.world_path, "w", encoding="utf-8") as handle:
            json.dump(self.world, handle)
        runner = fake_herdr.Fake(self.world_path, self.log_path, CALLER_PANE)
        ctx = Ctx(Herdr("herdr", runner), self.env, self.clock, io.StringIO(), io.StringIO(), self.repo)
        return B.build(ctx)


class Sweep(LiveFleet):
    def ctx(self):
        return Ctx(Herdr("/fake/bin/herdr", no_herdr), self.env, FakeClock(), io.StringIO(), io.StringIO(),
                   self.repo)

    def test_the_sweep_skips_a_locked_mailbox_and_runs_no_herdr_command(self):
        dev = put(self.repo, "sample-dev", "FYI: dev news", fyi=True, when=ago(days=2))
        stray = put(self.repo, "stray-lead", "FYI: for a mailbox that is no seat or contact", fyi=True, when=ago(days=2))
        owner = put(self.repo, "owner", "FYI: for the owner", fyi=True, when=ago(days=2), sender="sample-dev")
        ctx = self.ctx()
        with inbox.locked(self.repo, "sample-dev"):  # a delivery holds it: the sweep never waits
            self.assertEqual(mailroom.sweep_expired(ctx), {"stray-lead": ["fyi_ttl"], "owner": ["fyi_ttl"]})
        self.assertEqual(box(self.repo, "sample-dev", NEW), [dev.id])
        self.assertEqual(mailroom.sweep_expired(ctx), {"sample-dev": ["fyi_ttl"]})
        self.assertEqual(sorted(e["id"] for e in expired(self.repo)), sorted([dev.id, stray.id, owner.id]))
        self.assertEqual(self.calls(), [])

    def test_a_fleet_without_an_inbox_gets_no_directory(self):
        inbox_dir = os.path.join(self.repo, ".herdr", "inbox")
        self.assertFalse(os.path.exists(inbox_dir))
        self.assertEqual(mailroom.sweep_expired(self.ctx()), {})
        self.assertFalse(os.path.exists(inbox_dir))
        os.makedirs(os.path.join(inbox_dir, "sample-dev", CUR))  # a mailbox with nothing queued
        before = sorted(os.listdir(os.path.join(inbox_dir, "sample-dev")))
        self.assertEqual(mailroom.sweep_expired(self.ctx()), {})
        self.assertEqual(sorted(os.listdir(os.path.join(inbox_dir, "sample-dev"))), before)

    def test_the_watcher_sweeps_at_most_once_every_300_s(self):
        ctx = self.ctx()
        watcher = watch.Watcher(ctx, 0)
        put(self.repo, "sample-dev", "FYI: first", fyi=True, when=ago(days=2))
        watcher.sweep()
        self.assertEqual(len(expired(self.repo)), 1)
        put(self.repo, "sample-dev", "FYI: second", fyi=True, when=ago(days=2))
        ctx.clock.sleep(watch.EXPIRE_SWEEP_S - 1)
        watcher.sweep()
        self.assertEqual(len(expired(self.repo)), 1)
        ctx.clock.sleep(1)
        watcher.sweep()
        self.assertEqual(len(expired(self.repo)), 2)
        self.assertEqual(watch.EXPIRE_SWEEP_S, 300)


class RoutedFyiSweep(FleetPair):
    def test_the_sweep_expires_a_routed_fyi_and_the_source_log_gains_it(self):
        code, out, err = self.run_cli("send", "--as", "sample-orchestrator", "--fyi", "partner",
                                      "FYI: synthetic report")
        self.assertEqual(code, 0, out + err)
        (message,) = self.pending()  # the destination holds the one pending copy (31.1)
        ctx = Ctx(Herdr("/fake/bin/herdr", no_herdr), self.env, FakeClock(), io.StringIO(), io.StringIO(),
                  self.other)
        later = inbox.now_utc() + datetime.timedelta(hours=25)
        with mock.patch.object(inbox, "now_utc", return_value=later):
            self.assertEqual(mailroom.sweep_expired(ctx), {"peer-lead": ["fyi_ttl"]})
        self.assertEqual(self.pending(), [])
        for repo in (self.other, self.repo):
            with self.subTest(repo=repo):
                (entry,) = [e for e in log_entries(repo) if e.get("id") == message.id and e["kind"] == "expired"]
                self.assertEqual((entry["via"], entry["delivery"]), ("fyi_ttl", "none"))


class TimerMailSweep(TimerCase):
    def test_the_sweep_leaves_timer_mail_to_the_timer_cycle(self):
        self.status_of("w2:p2", "working")
        code, out, err = self.run_cli("watch", "--once")
        self.assertEqual(code, 0, out + err)
        (timer_mail,) = self.queued()
        plain = put(self.repo, "sample-dev", "Review by noon", when=NOW - datetime.timedelta(hours=1),
                    ttl=NOW + datetime.timedelta(hours=1))
        sweeper = Ctx(Herdr("/fake/bin/herdr", no_herdr), self.env, FakeClock(), io.StringIO(), io.StringIO(),
                      self.repo)
        with mock.patch.object(inbox, "now_utc", return_value=NOW.replace(hour=16, minute=0)):  # both past ttl
            self.assertEqual(mailroom.sweep_expired(sweeper), {"sample-dev": ["ttl"]})
            self.assertEqual([m.id for m in self.queued()], [timer_mail.id])  # left to the cycle
            self.assertEqual([e["id"] for e in expired(self.repo)], [plain.id])
            with timers.cycle(BoardCase.make_ctx(self)):
                pass
        self.assertEqual(self.queued(), [])
        (entry,) = [e for e in expired(self.repo) if e["id"] == timer_mail.id]
        self.assertEqual((entry["via"], entry["timer"]), ("ttl", "daily-check"))
        self.assertIn("timer_event", entry)  # the cycle's record of it
        (record,) = self.journal()["timers"]["daily-check"]["occurrences"].values()
        self.assertEqual(record["state"], "expired")


# -- 33.2 counts tell FYIs from requests ----------------------------------------------------------


class SplitCounts(LiveFleet):
    def setUp(self):
        super().setUp()
        self.first = put(self.repo, "sample-dev", "Review the parser", when=ago(minutes=9))
        self.second = put(self.repo, "sample-dev", "Then the lexer", when=ago(minutes=5))
        self.note = put(self.repo, "sample-dev", "FYI: CI is green", fyi=True, sender="sample-reviewer",
                        when=ago(minutes=12))
        put(self.repo, "sample-reviewer", "FYI: docs updated", fyi=True, when=ago(minutes=1))

    def build(self):
        with open(self.world_path, "w", encoding="utf-8") as handle:
            json.dump(self.world, handle)
        runner = fake_herdr.Fake(self.world_path, self.log_path, CALLER_PANE)
        ctx = Ctx(Herdr("herdr", runner), self.env, self.clock, io.StringIO(), io.StringIO(), self.repo)
        return ctx, B.build(ctx)

    def test_status_and_json(self):
        lines = self.run_cli("status")[1].splitlines()
        self.assertIn("queued: sample-dev 2 (oldest 9m, +1 fyi), sample-reviewer 1 fyi", lines)
        data = json.loads(self.run_cli("status", "--json")[1])
        self.assertEqual(data["queued"], {"sample-dev": 3, "sample-reviewer": 1})
        self.assertEqual(data["queued_fyi"], {"sample-dev": 1, "sample-reviewer": 1})
        self.assertEqual(data["queued_oldest"]["sample-dev"], self.note.time)  # every queued message, as before

    def test_the_board_cell_and_the_fleet_page(self):
        ctx, model = self.build()
        dev = next(r for r in model.rows if r.name == "sample-dev")
        reviewer = next(r for r in model.rows if r.name == "sample-reviewer")
        self.assertEqual((dev.queued, dev.fyi, dev.next_id), (2, 1, self.first.short))
        self.assertEqual((reviewer.queued, reviewer.fyi), (0, 1))
        spans = B.cell_spans("queue", dev, 70, True, False, False)
        text = "".join(t for t, _ in spans).rstrip()
        self.assertEqual(text, '2 queued, oldest 9m  #%s "Review the parser" +1 fyi' % self.first.short)
        self.assertIn((" +1 fyi", B.style_of("dim", True)), spans)
        spans = B.cell_spans("queue", reviewer, 70, True, False, False)
        self.assertEqual(spans[0], ("1 fyi", B.style_of("dim", True)))
        self.assertEqual(B.cell_spans("count", reviewer, 12, True, False, False)[0], ("1 fyi", B.style_of("dim", True)))
        self.assertNotEqual(B.urgency(reviewer)[0], B.urgency(dev)[0])  # an FYI is no waiting work
        narrow = B.cell_spans("queue", dev, 30, True, False, False)
        self.assertLessEqual(sum(B.width_of(t) for t, _ in narrow), 30)
        page = B.FleetPage()
        page.load(ctx, model, B.Cache(), B.WatcherState())
        self.assertIn("queued: sample-dev 2 (oldest 9m, +1 fyi), sample-reviewer 1 fyi",
                      [row.text for row in page.rows])

    def test_the_unread_note(self):
        out = self.run_cli("send", "--as", "sample-dev", "sample-orchestrator", "status report")[1]
        self.assertRegex(out.splitlines()[0],
                         r"^note: 2 unread \(\+1 fyi\) from sample-reviewer and sample-orchestrator \(oldest #%s, %s\); "
                         r"read them before you go on: herdr-fleet inbox --as sample-dev --read$"
                         % (self.first.short, HHMM))

    def test_an_fyi_only_note(self):
        out = self.run_cli("send", "--as", "sample-reviewer", "sample-orchestrator", "status report")[1]
        self.assertRegex(out.splitlines()[0],
                         r"^note: 1 unread fyi from sample-orchestrator \(#[0-9a-f]{6}, %s\); read it before you go "
                         r"on: herdr-fleet inbox --as sample-reviewer --read$" % HHMM)


# -- 33.3 unread mailboxes and inbox --drop ------------------------------------------------------


class UnreadMailbox(LiveFleet):
    WARN = ("warn  inbox: stray-lead holds 2 queued (oldest 7d00h); it is no seat or contact here, so the watcher "
            "does not deliver it. Read it with herdr-fleet inbox%s --as stray-lead --read, deliver it with herdr-fleet "
            "deliver%s stray-lead if stray-lead is a live agent, or (stray-lead or the owner) drop it with herdr-fleet "
            "inbox%s --as stray-lead --drop")

    def setUp(self):
        super().setUp()
        put(self.repo, "stray-lead", "Status of the importer?", when=ago(days=7))
        put(self.repo, "stray-lead", "Any news?", when=ago(days=6))
        put(self.repo, "old-peer", "FYI: shipped", fyi=True, when=ago(days=2))  # due: neither counted nor flagged
        put(self.repo, "owner", "Done with the parser", sender="sample-dev", which=CUR)

    def test_status_flags_it(self):
        lines = self.run_cli("status")[1].splitlines()
        self.assertIn("queued: stray-lead 2 (oldest 7d00h) (no seat or contact)", lines)
        data = json.loads(self.run_cli("status", "--json")[1])
        self.assertEqual(data["queued_unread"], ["stray-lead"])

    def test_doctor_warns_and_writes_nothing(self):
        before = tree(self.repo)
        code, out, _ = self.run_cli("doctor")
        self.assertIn(self.WARN % ("", "", ""), out.splitlines())
        self.assertNotIn("old-peer", out)
        self.assertNotIn("inbox: owner", out)
        self.assertEqual(tree(self.repo), before)

    def test_seats_contacts_and_owner_are_never_flagged(self):
        self.write_fleet(self.read_fleet() + '\n[[contacts]]\nname = "peer-lead"\nmatch = { name = "peer-lead" }\n')
        put(self.repo, "peer-lead", "For the contact")
        put(self.repo, "owner", "For the owner", sender="sample-dev")
        put(self.repo, "sample-dev", "For a seat")
        out = self.run_cli("doctor")[1]
        self.assertEqual([l for l in out.splitlines() if " inbox: " in l], [self.WARN % ("", "", "")])
        self.assertEqual(json.loads(self.run_cli("status", "--json")[1])["queued_unread"], ["stray-lead"])


class InboxDrop(LiveFleet):
    def setUp(self):
        super().setUp()
        self.request = put(self.repo, "sample-dev", "Review the parser", when=ago(hours=3))
        self.old_fyi = put(self.repo, "sample-dev", "FYI: CI was red", fyi=True, when=ago(hours=2))
        self.new_fyi = put(self.repo, "sample-dev", "FYI: CI is green", fyi=True, when=ago(minutes=10))
        self.brief = kickoff(self.repo, "sample-dev", when=ago(hours=1))

    def drop(self, *argv):
        """``inbox --drop --as sample-dev`` run by the owner outside Herdr (33.3)."""
        code, out, err = self.run_cli("inbox", "--as", "sample-dev", "--drop", *argv, env=outside(self.env))
        return code, policy_output(out).strip().splitlines(), err

    def test_dry_run_lists_and_moves_nothing(self):
        code, lines, _ = self.drop("--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(lines, [
            "would drop #%s from herdr-fleet (1h00m): You are the developer. (queued kickoff)"
            % self.brief.short,
            "would drop #%s from sample-orchestrator (3h00m): Review the parser" % self.request.short,
            "would drop #%s from sample-orchestrator (2h00m): FYI: CI was red" % self.old_fyi.short,
            "would drop #%s from sample-orchestrator (10m): FYI: CI is green" % self.new_fyi.short])
        self.assertEqual(len(box(self.repo, "sample-dev", NEW)), 4)
        self.assertEqual(log_entries(self.repo), [])
        self.assertEqual(self.calls(), [])

    def test_fyi_and_older_than_narrow_it_then_all_go_and_one_comes_back(self):
        code, lines, _ = self.drop("--fyi", "--older-than", "1h")
        self.assertEqual((code, lines), (0, ["dropped #%s from sample-orchestrator (2h00m): FYI: CI was red"
                                             % self.old_fyi.short]))
        code, lines, _ = self.drop("--older-than", "2h")
        self.assertEqual(lines, ["dropped #%s from sample-orchestrator (3h00m): Review the parser"
                                 % self.request.short])
        code, lines, _ = self.drop()
        self.assertEqual(lines, [
            "dropped #%s from herdr-fleet (1h00m): You are the developer. (queued kickoff)"
            % self.brief.short,
            "dropped #%s from sample-orchestrator (10m): FYI: CI is green" % self.new_fyi.short])
        self.assertEqual(box(self.repo, "sample-dev", NEW), [])
        entries = log_entries(self.repo)
        self.assertEqual([(e["kind"], e["via"], e["delivery"]) for e in entries],
                         [("dropped", "inbox --drop", "none")] * 4)
        self.assertEqual(self.drop()[1], ["nothing to drop for sample-dev"])
        code, out, _ = self.run_cli("inbox", "--as", "sample-dev", "--redeliver", "#" + self.request.short)
        self.assertEqual(code, 0, out)
        self.assertEqual(box(self.repo, "sample-dev", NEW), [self.request.id])
        self.assertEqual(self.calls(), [])

    def test_the_options_belong_to_drop(self):
        for argv in (["--drop", "--read"], ["--drop", "--count"], ["--drop", "--wait"], ["--drop", "--history", "x"],
                     ["--drop", "--redeliver", "#000000"], ["--fyi"], ["--older-than", "1h"], ["--dry-run"],
                     ["--drop", "--older-than", "5x"]):
            with self.subTest(argv=argv):
                self.assertEqual(self.run_cli("inbox", "--as", "sample-dev", *argv, env=outside(self.env))[0], 2)
        self.assertEqual(len(box(self.repo, "sample-dev", NEW)), 4)

    def test_drop_inbox_keeps_its_via(self):
        ctx = Ctx(Herdr("/fake/bin/herdr", no_herdr), self.env, FakeClock(), io.StringIO(), io.StringIO(),
                  self.repo)
        self.assertEqual(messages.drop_queued(ctx, "sample-dev"), 4)
        self.assertEqual([(e["kind"], e["via"]) for e in log_entries(self.repo)], [("dropped", "drop-inbox")] * 4)
        self.assertEqual(box(self.repo, "sample-dev", NEW), [])


class DropAuthority(LiveFleet):
    """33.3: ``inbox --drop --as NAME`` looks the caller up, ignoring ``--as``."""

    def setUp(self):
        super().setUp()
        self.dev = put(self.repo, "sample-dev", "Review the parser", when=ago(hours=1))
        self.stray = put(self.repo, "stray-lead", "Status of the importer?", when=ago(days=7))
        put(self.repo, "owner", "Parser done", sender="sample-dev", when=ago(hours=1))
        put(self.repo, "sample-reviewer", "Check the docs", when=ago(hours=1))

    def as_seat(self, pane):
        return dict(self.env, HERDR_PANE_ID=pane)

    def test_a_seat_cannot_drop_another_seats_or_the_owners_mailbox(self):
        for name, error in (("sample-dev", "only sample-dev or the owner drops sample-dev's mailbox"),
                            ("owner", "only the owner drops owner's mailbox")):
            for argv in (["--drop"], ["--drop", "--dry-run"]):
                with self.subTest(name=name, argv=argv):
                    code, _, err = self.run_cli("inbox", "--as", name, *argv, env=self.as_seat("w2:p3"))
                    self.assertEqual(code, 2)
                    self.assertIn(error, err)
        self.assertEqual(box(self.repo, "sample-dev", NEW), [self.dev.id])
        self.assertEqual(len(box(self.repo, "owner", NEW)), 1)
        self.assertEqual(log_entries(self.repo), [])

    def test_the_seat_itself_and_the_owner_may(self):
        code, out, err = self.run_cli("inbox", "--as", "sample-dev", "--drop", env=self.as_seat("w2:p2"))
        self.assertEqual(code, 0, err)
        self.assertEqual(policy_output(out).strip(), "dropped #%s from sample-orchestrator (1h00m): Review the parser"
                         % self.dev.short)
        code, out, err = self.run_cli("inbox", "--as", "sample-reviewer", "--drop", env=outside(self.env))
        self.assertEqual(code, 0, err)
        self.assertEqual(box(self.repo, "sample-reviewer", NEW), [])
        self.world["panes"][CALLER_PANE]["agent"] = None  # the owner at a verified shell
        self.world["panes"][CALLER_PANE]["procs"] = [dict(fake_herdr.SHELL)]
        code, out, err = self.run_cli("inbox", "--as", "owner", "--drop")
        self.assertEqual(code, 0, err)
        self.assertEqual(box(self.repo, "owner", NEW), [])

    def test_an_unread_mailbox_too_only_by_its_name_or_the_owner(self):
        for argv in (["--drop"], ["--drop", "--dry-run"]):
            with self.subTest(argv=argv):
                code, _, err = self.run_cli("inbox", "--as", "stray-lead", *argv, env=self.as_seat("w2:p3"))
                self.assertEqual(code, 2)
                self.assertIn("only stray-lead or the owner drops stray-lead's mailbox", err)
        self.assertEqual(box(self.repo, "stray-lead", NEW), [self.stray.id])
        self.assertEqual(log_entries(self.repo), [])
        code, out, err = self.run_cli("inbox", "--as", "stray-lead", "--drop", env=outside(self.env))
        self.assertEqual(code, 0, err)
        self.assertEqual(box(self.repo, "stray-lead", NEW), [])

    def test_a_live_agent_without_a_fleet_keeps_its_queue(self):
        """31 queues mail for a live agent that has no fleet of its own in the sender's
        fleet, under the agent's name: an unread mailbox, but a live queue."""
        scratch = os.path.join(self.tmp, "scratch")  # no .herdr anywhere above it
        os.makedirs(scratch)
        fake_herdr.add_workspace(self.world, "w4", "scratch", scratch)
        fake_herdr.put_agent(self.world, "w4:p1", "claude", "helper", status="working")

        def ask():
            code, out, err = self.run_cli("send", "--as", "sample-dev", "helper", "Synthetic question")
            self.assertEqual(code, 0, out + err)
            (queued,) = inbox.read_box(self.repo, "helper", NEW)  # working: it waits here
            return queued

        first = ask()
        self.assertIn("helper", json.loads(self.run_cli("status", "--json")[1])["queued_unread"])
        before = log_entries(self.repo)
        for argv in (["--drop"], ["--drop", "--dry-run"]):
            with self.subTest(argv=argv):
                code, _, err = self.run_cli("inbox", "--as", "helper", *argv, env=self.as_seat("w2:p3"))
                self.assertEqual(code, 2)
                self.assertIn("only helper or the owner drops helper's mailbox", err)
        self.assertEqual(box(self.repo, "helper", NEW), [first.id])
        self.assertEqual(log_entries(self.repo), before)
        code, out, err = self.run_cli("inbox", "--as", "helper", "--drop", env=self.as_seat("w4:p1"))
        self.assertEqual(code, 0, err)  # helper itself
        self.assertIn("dropped #%s from sample-dev" % first.short, out)
        second = ask()
        code, out, err = self.run_cli("inbox", "--as", "helper", "--drop", env=outside(self.env))
        self.assertEqual(code, 0, err)  # the owner
        self.assertIn("dropped #%s from sample-dev" % second.short, out)
        self.assertEqual(box(self.repo, "helper", NEW), [])
        self.assertEqual([(e["id"], e["via"]) for e in log_entries(self.repo) if e["kind"] == "dropped"],
                         [(first.id, "inbox --drop"), (second.id, "inbox --drop")])

    def test_without_as_it_is_your_own(self):
        code, out, err = self.run_cli("inbox", "--drop", env=self.as_seat("w2:p3"))
        self.assertEqual(code, 0, err)
        self.assertEqual(box(self.repo, "sample-reviewer", NEW), [])
        self.assertEqual(box(self.repo, "sample-dev", NEW), [self.dev.id])


# -- 33.4 old debts -------------------------------------------------------------------------------


class OldDebts(LiveFleet):
    def setUp(self):
        super().setUp()
        self.clock_ = Clock(T0 + 5 * DAY)
        for when, sender, to, ident, first in (
                (T0, "sample-orchestrator", "sample-dev", "20261021T000000Z-aaaaaa", "old ask"),
                (T0 + DAY, "sample-orchestrator", "sample-reviewer", "20261022T000000Z-bbbbbb", "older review"),
                (T0 + DAY, "sample-reviewer", "sample-dev", None, "pre-inbox ask"),
                (T0 + 5 * DAY - datetime.timedelta(hours=1), "owner", "sample-orchestrator",
                 "20261025T230000Z-cccccc", "fresh ask")):
            messages.append(self.repo, raw_entry(sender, to, when, first=first, ident=ident))

    def ack(self, *argv, env=None):
        """``ack`` by the owner outside Herdr, unless ``env`` says otherwise."""
        with wall(self.clock_):
            code, out, err = self.run_cli("ack", *argv, env=env or outside(self.env))
        return code, policy_output(out).strip().splitlines(), err

    def test_dry_run_lists_and_writes_nothing(self):
        before = log_entries(self.repo)
        code, lines, _ = self.ack("--older-than", "3d", "--reason", "closed in the queue audit", "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(len(lines), 3, lines)
        self.assertRegex(lines[0], r"^would ack #aaaaaa from sample-orchestrator since .*, 5d00h ago \(old ask\), "
                                   r"owed by sample-dev; reason: closed in the queue audit$")
        self.assertRegex(lines[1], r"^would ack #bbbbbb from sample-orchestrator since .*, 4d00h ago "
                                   r"\(older review\), owed by sample-reviewer; reason: closed in the queue audit$")
        self.assertRegex(lines[2], r"^would ack sample-reviewer since .*, 4d00h ago \(pre-inbox ask\), owed by "
                                   r"sample-dev; reason: closed in the queue audit$")
        self.assertEqual(log_entries(self.repo), before)
        self.assertEqual(self.calls(), [])

    def test_closes_old_debts_whoever_owes_them_and_keeps_young_ones(self):
        code, lines, _ = self.ack("--older-than", "3d", "--reason", "closed in the queue audit")
        self.assertEqual(code, 0, lines)
        acks = [e for e in log_entries(self.repo) if e["kind"] == "ack"]
        self.assertEqual([(a["from"], a["to"], a.get("id"), a["by"], a["reason"]) for a in acks], [
            ("sample-dev", "sample-orchestrator", "20261021T000000Z-aaaaaa", "owner", "closed in the queue audit"),
            ("sample-reviewer", "sample-orchestrator", "20261022T000000Z-bbbbbb", "owner",
             "closed in the queue audit"),
            ("sample-dev", "sample-reviewer", None, "owner", "closed in the queue audit")])
        with wall(self.clock_):
            owed = json.loads(self.run_cli("log", "--owed", "--json")[1])
        self.assertEqual([o["first_line"] for o in owed], ["fresh ask"])
        self.assertEqual(self.ack("--older-than", "3d", "--reason", "again")[1], ["nothing owed older than 3d"])

    def test_only_the_owner_closes_debts_in_bulk(self):
        before = log_entries(self.repo)
        seat = dict(self.env, HERDR_PANE_ID="w2:p2")
        for argv in ([], ["--as", "owner"], ["--dry-run"]):
            with self.subTest(argv=argv):
                code, _, err = self.ack("--older-than", "3d", "--reason", "x", *argv, env=seat)
                self.assertEqual(code, 2)
                self.assertIn("closing others' debts in bulk is the owner's; ack #id --reason TEXT closes one", err)
        self.assertEqual(log_entries(self.repo), before)
        self.world["panes"][CALLER_PANE]["agent"] = None  # the owner at a verified shell
        self.world["panes"][CALLER_PANE]["procs"] = [dict(fake_herdr.SHELL)]
        code, lines, err = self.ack("--older-than", "3d", "--reason", "closed in the queue audit", env=self.env)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(lines), 3, lines)
        self.assertEqual({e["by"] for e in log_entries(self.repo) if e["kind"] == "ack"}, {"owner"})

    def test_a_name_or_an_id_in_that_form_is_a_usage_error(self):
        before = log_entries(self.repo)
        for argv in (["sample-dev"], ["#aaaaaa"]):
            with self.subTest(argv=argv):
                code, _, err = self.ack("--older-than", "3d", "--reason", "x", *argv)
                self.assertEqual(code, 2, err)
        self.assertEqual(log_entries(self.repo), before)
        self.assertEqual(self.ack("--older-than", "3d", "--reason", " ")[0], 2)

    def test_without_a_reason_it_keeps_its_meaning(self):
        with wall(self.clock_):
            code, out, _ = self.run_cli("ack", "--as", "sample-dev", "--older-than", "3d")
        self.assertEqual(code, 0)
        acks = [e for e in log_entries(self.repo) if e["kind"] == "ack"]
        self.assertEqual({a["from"] for a in acks}, {"sample-dev"})
        self.assertNotIn("by", acks[0])

    def test_status_folds_stale_debts_into_one_line_and_json_flags_them(self):
        with wall(self.clock_):
            lines = self.run_cli("status")[1].splitlines()
            data = json.loads(self.run_cli("status", "--json")[1])
        awaiting = [l for l in lines if l.startswith("awaiting:")]
        self.assertEqual(len(awaiting), 2, awaiting)
        self.assertRegex(awaiting[0], r"^awaiting: sample-orchestrator owes owner for 1h00m \(#cccccc, "
                                      r"since .*\): fresh ask \(ack owner, or ack #cccccc\)$")
        self.assertEqual(awaiting[1], "awaiting: 3 older than 3d (oldest 5d00h), not listed; see herdr-fleet log "
                                      "--owed; the owner closes them with herdr-fleet ack --older-than 3d --reason "
                                      "TEXT (--dry-run first)")
        self.assertEqual([(o["first_line"], o.get("stale")) for o in data["owed"]],
                         [("old ask", True), ("older review", True), ("pre-inbox ask", True), ("fresh ask", False)])


# -- 33.5 printed inbox commands keep the fleet ---------------------------------------------------


class WatcherHint(WatchCase):
    def test_the_unconfirmed_notification_keeps_the_fleet(self):
        path = os.path.join(self.repo, ".herdr", "fleet.toml")
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.makedirs(elsewhere)
        put(self.repo, "sample-dev", "Task A")
        self.behave(prompt_errors={"sample-dev": "timeout"})
        self.clock.cycles, self.clock.passes = 1, 0
        self.run_cli("watch", "--fleet", path, cwd=elsewhere)
        self.assertTrue(self.world["notifications"][-1]["body"].endswith(
            "1 not resent (herdr-fleet inbox --fleet %s --redeliver ID)" % path))
        put(self.repo, "sample-dev", "Task B")
        self.watch("--fleet", path, cycles=1)
        self.assertTrue(self.world["notifications"][-1]["body"].endswith(
            "1 not resent (herdr-fleet inbox --redeliver ID)"))


class FleetFlag(LiveFleet):
    def setUp(self):
        super().setUp()
        self.path = os.path.join(self.repo, ".herdr", "fleet.toml")
        self.other = os.path.join(self.tmp, "other")
        os.makedirs(os.path.join(self.other, ".herdr"))
        with open(os.path.join(self.other, ".herdr", "fleet.toml"), "w", encoding="utf-8") as handle:
            handle.write('workspace = "other"\n\n[[agents]]\nname = "other-dev"\nkind = "claude"\n')
        self.nowhere = os.path.join(self.tmp, "nowhere")
        os.makedirs(self.nowhere)
        put(self.repo, "sample-orchestrator", "Review the parser", sender="sample-dev")

    def note(self, *argv, **kwargs):
        out = self.run_cli("send", "--as", "sample-orchestrator", *argv, "sample-dev", "hi", **kwargs)[1]
        return out.splitlines()[0]

    def test_the_unread_note(self):
        flag = " --fleet %s" % self.path
        self.assertTrue(self.note().endswith(": herdr-fleet inbox --as sample-orchestrator --read"))
        self.assertTrue(self.note("--fleet", self.path, cwd=self.other)
                        .endswith(": herdr-fleet inbox%s --as sample-orchestrator --read" % flag))
        env = dict(self.env, HERDR_FLEET_FILE=self.path)
        self.assertTrue(self.note(env=env, cwd=self.nowhere)
                        .endswith(": herdr-fleet inbox%s --as sample-orchestrator --read" % flag))
        # only when needed: no flag where discovery agrees, however the fleet was chosen
        self.assertTrue(self.note("--fleet", self.path).endswith(": herdr-fleet inbox --as sample-orchestrator --read"))
        self.assertTrue(self.note(env=env).endswith(": herdr-fleet inbox --as sample-orchestrator --read"))

    def test_a_symlinked_cwd_gets_no_flag_and_a_sibling_without_a_fleet_does(self):
        bare = ": herdr-fleet inbox --as sample-orchestrator --read"
        link = os.path.join(self.tmp, "link")
        os.symlink(self.repo, link)
        linked = os.path.join(link, ".herdr", "fleet.toml")
        for argv, cwd in (([], link), (["--fleet", self.path], link), (["--fleet", linked], self.repo)):
            with self.subTest(argv=argv, cwd=cwd):  # the same file by its real path
                self.assertTrue(self.note(*argv, cwd=cwd).endswith(bare))
        sibling = os.path.join(self.tmp, "sibling")  # beside the repository, no .herdr above it
        os.makedirs(sibling)
        self.assertTrue(self.note("--fleet", self.path, cwd=sibling)
                        .endswith(": herdr-fleet inbox --fleet %s --as sample-orchestrator --read" % self.path))

    def test_the_doctor_lines(self):
        put(self.repo, "stray-lead", "Status of the importer?", when=ago(days=7))
        put(self.repo, "stray-lead", "Any news?", when=ago(days=6))
        flag = " --fleet %s" % self.path
        out = self.run_cli("doctor")[1]
        self.assertIn(UnreadMailbox.WARN % ("", "", ""), out.splitlines())
        out = self.run_cli("doctor", "--fleet", self.path, cwd=self.other)[1]
        self.assertIn(UnreadMailbox.WARN % (flag, flag, flag), out.splitlines())
        out = self.run_cli("doctor", "--fleet", self.path)[1]
        self.assertIn(UnreadMailbox.WARN % ("", "", ""), out.splitlines())

    def test_the_owner_history_hint(self):
        self.run_cli("send", "--fleet", self.path, "--as", "sample-dev", "owner", "parser done", cwd=self.other)
        (note,) = self.world["notifications"]
        self.assertTrue(note["body"].endswith("\nread: herdr-fleet inbox --fleet %s --as owner --history sample-dev"
                                              % self.path), note)
        self.world["notifications"] = []
        self.run_cli("send", "--fleet", self.path, "--as", "sample-dev", "owner", "lexer done")
        (note,) = self.world["notifications"]
        self.assertTrue(note["body"].endswith("\nread: herdr-fleet inbox --as owner --history sample-dev"), note)


if __name__ == "__main__":
    unittest.main()
