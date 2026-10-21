"""The inbox (design 18.1 to 18.5 and 18.8, version 0.6.0): ids and short forms, the
message file, the batch and its 16 KiB pointer, every row of the send table, flush
before send, ttl, kickoffs carrying {inbox}, --drop-inbox, deliver, the inbox command,
the owner mailbox, owed replies counted from delivery, reply_to and ack #id."""

from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import tempfile
import unittest
from unittest import mock

import fake_herdr
from support import CALLER_PANE, THREE_SEATS, FleetCase, LiveFleet
from herdr_fleet import inbox, messages
from herdr_fleet.context import UsageError
from herdr_fleet.inbox import CUR, NEW, Message
from herdr_fleet.kickoff import DEFAULT_TEMPLATE, compose
from herdr_fleet.messages import FYI_PLACE, place
from test_units import fleet, seat

SHORT = r"#[0-9a-f]{6}"
HHMM = r"\d\d:\d\d"
FYI_LABEL = re.escape(", " + FYI_PLACE)
WORKING = re.escape(place("working"))


def log_entries(repo):
    path = os.path.join(repo, ".herdr", "messages.jsonl")
    if not os.path.exists(path):
        return []
    with open(path) as handle:
        return [json.loads(line) for line in handle if line.strip()]


def box(repo, name, which):
    folder = os.path.join(repo, ".herdr", "inbox", name, which)
    return sorted(f[:-3] for f in os.listdir(folder) if f.endswith(".md")) if os.path.isdir(folder) else []


def put(repo, to, body, sender="fablyn-orchestrator", ident=None, when=None, ttl=None, fyi=False,
        reply_to=None, which=NEW):
    """Write a message file straight into a mailbox, as an earlier send would have."""
    when = when or inbox.now_utc()
    message = Message(id=ident or inbox.new_id(when, inbox.taken_shorts(repo)), sender=sender, to=to,
                      time=inbox.iso_local(when), body=body, fyi=fyi, reply_to=reply_to,
                      ttl=inbox.iso_local(ttl) if ttl else None)
    return inbox.write(repo, message, which)


def local(hour, minute, day=21):
    return datetime.datetime(2026, 10, day, hour, minute).astimezone()


# -- storage, ids, rendering ----------------------------------------------------------------------


class Storage(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.repo, ".herdr"))

    def tearDown(self):
        shutil.rmtree(self.repo)

    def test_id_format_and_redraw(self):
        when = datetime.datetime(2026, 10, 20, 23, 9, 0, tzinfo=datetime.timezone.utc)
        taken = {"3f2a9c"}
        with mock.patch.object(inbox.secrets, "token_hex", side_effect=["3f2a9c", "3f2a9c", "91be02"]):
            ident = inbox.new_id(when, taken)
        self.assertEqual(ident, "20261020T230900Z-91be02")
        self.assertIn("91be02", taken)
        self.assertRegex(inbox.new_id(inbox.now_utc(), set()), r"^\d{8}T\d{6}Z-[0-9a-f]{6}$")

    def test_file_round_trip_keeps_the_body_verbatim(self):
        body = "[from someone] Range for 1.25.1: 90ba47c..a659ceb.\n\nBuild in /tmp/x.\n\n"
        message = put(self.repo, "fablyn-dev", body, ident="20261020T230900Z-3f2a9c", fyi=True,
                      reply_to="20261020T225300Z-91be02", ttl=local(12, 0))
        with open(message.path) as handle:
            text = handle.read()
        self.assertTrue(text.startswith("id: 20261020T230900Z-3f2a9c\nfrom: fablyn-orchestrator\nto: fablyn-dev\n"
                                        "time: "), text)
        self.assertIn("\nfyi: true\nreply_to: 20261020T225300Z-91be02\nttl: ", text)
        self.assertTrue(text.endswith("\n\n" + body))
        (back,) = inbox.read_box(self.repo, "fablyn-dev", NEW)
        self.assertEqual((back.id, back.sender, back.body, back.fyi, back.reply_to),
                         (message.id, "fablyn-orchestrator", body, True, "20261020T225300Z-91be02"))
        self.assertEqual([f for f in os.listdir(os.path.dirname(message.path)) if f.endswith(".tmp")], [])
        with open(os.path.join(self.repo, ".herdr", ".gitignore")) as handle:
            self.assertIn("inbox/", handle.read().split())

    def test_short_forms_unique_ambiguous_unknown(self):
        put(self.repo, "fablyn-dev", "a", ident="20261020T230900Z-3f2a9c")
        put(self.repo, "fablyn-reviewer", "b", ident="20261020T231000Z-3f2a9c", which=CUR)
        put(self.repo, "fablyn-dev", "c", ident="20261020T231100Z-91be02")
        self.assertEqual(inbox.taken_shorts(self.repo), {"3f2a9c", "91be02"})
        for ref in ("#91be02", "91be02", "20261020T231100Z-91be02"):
            self.assertEqual(inbox.resolve(self.repo, ref).body, "c", ref)
        with self.assertRaises(UsageError) as caught:
            inbox.resolve(self.repo, "#3f2a9c")
        self.assertIn("20261020T230900Z-3f2a9c (fablyn-dev/new)", str(caught.exception))
        self.assertIn("20261020T231000Z-3f2a9c (fablyn-reviewer/cur)", str(caught.exception))
        self.assertEqual(inbox.resolve(self.repo, "20261020T230900Z-3f2a9c").body, "a")  # the full id is exact
        for ref in ("#000000", "not-an-id", "#3F2A9C"):
            with self.assertRaises(UsageError):
                inbox.resolve(self.repo, ref)

    def test_same_second_keeps_write_order(self):
        when = inbox.now_utc()
        stamp = inbox.new_id(when, set())[:-6]  # one second, and a hex part that sorts the other way
        first = put(self.repo, "fablyn-dev", "first", ident=stamp + "ffffff", when=when)
        second = put(self.repo, "fablyn-dev", "second", ident=stamp + "000000", when=when)
        os.utime(second.path, ns=(first.mtime_ns + 10 ** 6, first.mtime_ns + 10 ** 6))
        self.assertEqual([m.body for m in inbox.read_box(self.repo, "fablyn-dev", NEW)], ["first", "second"])

    def test_mailbox_names_never_become_paths(self):
        with self.assertRaises(UsageError):
            inbox.box_dir(self.repo, "../etc", NEW)


class Rendering(unittest.TestCase):
    def message(self, body, short, hour=10, minute=9, sender="fablyn-orchestrator", reply_to=None, day=21):
        return Message(id="20261020T230900Z-" + short, sender=sender, to="fablyn-dev",
                       time=inbox.iso_local(local(hour, minute, day)), body=body, reply_to=reply_to)

    def test_batch_format_and_reply_to(self):
        today = local(10, 9).date()
        batch = inbox.render_batch([self.message("Range for 1.25.1\n", "3f2a9c"),
                                    self.message("done", "91be02", 10, 31, "fablyn-dev",
                                                 reply_to="20261020T225300Z-aaaaaa")], today=today)
        self.assertEqual(batch, "[from fablyn-orchestrator at 10:09 #3f2a9c]\nRange for 1.25.1\n\n"
                                "[from fablyn-dev at 10:31 #91be02 re #aaaaaa]\ndone")
        other_day = inbox.render_batch([self.message("x", "3f2a9c", day=20)], today=today)
        self.assertTrue(other_day.startswith("[from fablyn-orchestrator at 2026-10-20 10:09 #3f2a9c]"))
        history = inbox.render_batch([self.message("x", "3f2a9c")], direction=True, today=today)
        self.assertTrue(history.startswith("[from fablyn-orchestrator to fablyn-dev at 10:09 #3f2a9c]\nx"))

    def test_direct_text_and_pointer(self):
        self.assertEqual(inbox.direct_text(self.message("hi", "3f2a9c")), "[from fablyn-orchestrator #3f2a9c] hi")
        self.assertEqual(inbox.direct_text(self.message("[from the owner] stop", "3f2a9c")), "[from the owner] stop")
        items = [self.message("x", "3f2a9c"), self.message("y", "91be02", 11, 0)]
        self.assertEqual(inbox.pointer_text(items, 41234, today=local(10, 9).date()),
                         "[from herdr-fleet] 2 messages waiting (oldest 10:09, 41.2 KB); run: herdr-fleet inbox --read")

    def test_compose_prompt_alone_batched_and_capped(self):
        one, two = self.message("a", "3f2a9c"), self.message("b", "91be02")
        self.assertEqual(messages.compose_prompt([], one)[0], "[from fablyn-orchestrator #3f2a9c] a")
        text, _, pointer = messages.compose_prompt([one], two)
        self.assertFalse(pointer)
        self.assertTrue(text.startswith("[from fablyn-orchestrator at ") and text.endswith("#91be02]\nb"), text)
        big = self.message("x" * inbox.BATCH_CAP, "aaaaaa")
        text, size, pointer = messages.compose_prompt([], big)
        self.assertTrue(pointer)
        self.assertGreater(size, inbox.BATCH_CAP)
        self.assertTrue(text.startswith("[from herdr-fleet] 1 message waiting (oldest "), text)

    def test_log_lines_for_the_new_kinds(self):
        message = self.message("Range for 1.25.1", "3f2a9c")
        delivered = messages.delivered_entry(message, "kickoff", "unconfirmed")
        self.assertRegex(messages.format_entry(delivered),
                         r"  herdr-fleet → fablyn-dev  delivered\? #3f2a9c via kickoff  Range for 1.25.1$")
        self.assertRegex(messages.format_entry(messages.disposal_entry("expired", message, "ttl")),
                         r"  herdr-fleet → fablyn-dev  expired #3f2a9c  Range for 1.25.1$")
        self.assertRegex(messages.format_entry(messages.ack_entry("fablyn-dev", "x", ident=message.id)),
                         r"  fablyn-dev → x  ack #3f2a9c$")
        queued = messages.send_entry(message, "queued")
        self.assertRegex(messages.format_entry(queued), r"  \d+  #3f2a9c  \[queued\]  Range for 1.25.1$")
        self.assertEqual(messages.first_line("[from a at 10:09 #3f2a9c re #91be02]\nbody line"), "body line")


class KickoffTemplate(unittest.TestCase):
    """{inbox}: with nothing queued the kickoff reads exactly as before 0.6.0."""

    def test_empty_inbox_leaves_the_text_as_before(self):
        old_default = DEFAULT_TEMPLATE.replace("{inbox}\n", "")
        plain = seat()
        old = compose(seat(kickoff_text=old_default), fleet([plain]), "w", "ROSTER", "up")
        self.assertEqual(compose(plain, fleet([plain]), "w", "ROSTER", "up"), old)
        template = "[from herdr-fleet] Hi.\nTeam:\n{roster}\n\n{inbox}\n\nWhere things stand:\n{state}\n"
        custom = seat(kickoff_text=template)
        self.assertEqual(compose(custom, fleet([custom]), "w", "R", "up", state_text="S"),
                         "[from herdr-fleet] Hi.\nTeam:\nR\n\nWhere things stand:\nS")
        self.assertEqual(compose(custom, fleet([custom]), "w", "R", "up", state_text="S", inbox="BLOCK"),
                         "[from herdr-fleet] Hi.\nTeam:\nR\n\nBLOCK\n\nWhere things stand:\nS")

    def test_a_template_without_the_placeholder_gets_the_block_appended(self):
        hello = seat(kickoff_text="[from herdr-fleet] Hello {name}.")
        self.assertEqual(compose(hello, fleet([hello]), "w", "R", "up", inbox="BLOCK"),
                         "[from herdr-fleet] Hello fablyn-dev.\n\nBLOCK")
        literal = seat(kickoff_text="Show {{inbox}} literally")
        self.assertTrue(compose(literal, fleet([literal]), "w", "R", "up", inbox="B").endswith("\n\nB"))


# -- send: the table of design 18.3 -----------------------------------------------------------------

TABLE = {  # state -> {flag: outcome}; the working row as design 21.1 amends it
    "idle": {"": "sent", "--queue": "queued", "--now": "sent", "--fyi": "queued"},
    "done": {"": "sent", "--queue": "queued", "--now": "sent", "--fyi": "queued"},
    "working": {"": "queued", "--queue": "queued", "--now": "sent", "--fyi": "queued"},
    "blocked": {"": "queued", "--queue": "queued", "--now": "refused", "--fyi": "queued"},
    "unknown": {"": "queued", "--queue": "queued", "--now": "refused", "--fyi": "queued"},
    "absent": {"": "queued", "--queue": "queued", "--now": "refused", "--fyi": "queued"},
}


class SendTable(LiveFleet):
    def set_dev(self, state):
        if state == "absent":
            self.world["panes"]["w2:p2"].update(agent=None, procs=[dict(fake_herdr.SHELL)])
        else:
            fake_herdr.put_agent(self.world, "w2:p2", "claude", "fablyn-dev", status=state,
                                 args=["--model", "claude-opus-5-5"])

    def test_every_row(self):
        for state, row in TABLE.items():
            for flag, outcome in row.items():
                with self.subTest(state=state, flag=flag):
                    shutil.rmtree(os.path.join(self.repo, ".herdr", "inbox"), ignore_errors=True)
                    if os.path.exists(os.path.join(self.repo, ".herdr", "messages.jsonl")):
                        os.remove(os.path.join(self.repo, ".herdr", "messages.jsonl"))
                    self.set_dev(state)
                    before = len(self.prompts_to("fablyn-dev"))
                    argv = ["send", "--as", "fablyn-orchestrator"] + ([flag] if flag else []) + ["fablyn-dev", "go"]
                    code, out, err = self.run_cli(*argv)
                    (entry,) = log_entries(self.repo)
                    prompted = len(self.prompts_to("fablyn-dev")) - before
                    fyi = FYI_LABEL if flag == "--fyi" else ""
                    if outcome == "sent":
                        self.assertEqual((code, prompted, entry["delivery"]), (0, 1, "sent"), out + err)
                        self.assertTrue(out.startswith("sent to fablyn-dev (%s), " % place(state)), out)
                        self.assertEqual(box(self.repo, "fablyn-dev", CUR), [entry["id"]])
                    elif outcome == "queued":
                        self.assertEqual((code, prompted, entry["delivery"]), (0, 0, "queued"), out + err)
                        self.assertRegex(out.strip(), r"^queued in herdr-fleet for fablyn-dev \(%s%s\) #%s, "
                                                      r"position 1 of 1$" % (state, fyi, entry["id"][-6:]))
                        self.assertEqual(box(self.repo, "fablyn-dev", NEW), [entry["id"]])
                    else:
                        self.assertEqual((code, prompted, entry["delivery"]), (1, 0, "refused"), out + err)
                        self.assertEqual(out.strip(), "refused fablyn-dev (%s): nothing sent" % state)
                        self.assertEqual(box(self.repo, "fablyn-dev", CUR), [entry["id"]])
                        self.assertEqual(box(self.repo, "fablyn-dev", NEW), [])

    def test_queue_and_now_together_and_bad_options(self):
        self.assertEqual(self.run_cli("send", "--queue", "--now", "fablyn-dev", "x")[0], 2)
        self.assertEqual(self.run_cli("send", "--ttl", "5x", "fablyn-dev", "x")[0], 2)
        self.assertEqual(self.run_cli("send", "--ttl", "0m", "fablyn-dev", "x")[0], 2)
        code, _, err = self.run_cli("send", "--reply-to", "#000000", "fablyn-dev", "x")
        self.assertEqual(code, 2)
        self.assertIn("no message #000000", err)
        self.assertEqual(self.argvs("agent", "prompt"), [])
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".herdr", "inbox")))

    def test_a_failed_write_prompts_nothing(self):
        os.makedirs(os.path.join(self.repo, ".herdr", "inbox"))
        with open(os.path.join(self.repo, ".herdr", "inbox", "fablyn-dev"), "w") as handle:
            handle.write("not a directory")
        code, out, _ = self.run_cli("send", "fablyn-dev", "fablyn-reviewer", "x")
        self.assertEqual(code, 1)
        self.assertRegex(out.splitlines()[0], r"^failed fablyn-dev: cannot write the message file \(.*\); "
                                              r"nothing sent$")
        self.assertTrue(out.splitlines()[1].startswith("sent to fablyn-reviewer (idle)"))
        self.assertEqual(self.prompts_to("fablyn-dev"), [])
        self.assertEqual([e["to"] for e in log_entries(self.repo)], ["fablyn-reviewer"])

    def test_an_fyi_never_wakes_a_seat_that_turned_idle(self):
        working = {"id": "cli:agent:get", "result": {"type": "agent_info", "agent": {
            "agent": "claude", "agent_status": "working", "name": "fablyn-dev", "pane_id": "w2:p2"}}}
        self.world["canned"] = [{"prefix": ["agent", "get", "fablyn-dev"], "stdout": working, "times": 1}]
        code, out, _ = self.run_cli("send", "--fyi", "fablyn-dev", "report")  # working when looked at, idle at prompt
        self.assertEqual(code, 0)  # design 21.1: a working seat is not even tried
        self.assertRegex(out.strip(), r"^queued in herdr-fleet for fablyn-dev \(working%s\) %s, position 1 of 1$"
                         % (FYI_LABEL, SHORT))
        self.assertEqual(self.argvs("agent", "prompt"), [])

    def test_dry_run_writes_nothing(self):
        self.status_of("w2:p2", "blocked")
        self.run_cli("send", "fablyn-dev", "queued one")
        self.status_of("w2:p2", "idle")
        for argv, line in ((["fablyn-dev", "x"], r"^would send to fablyn-dev \(idle\) with 1 queued, \d+ B$"),
                           (["--queue", "fablyn-dev", "x"],
                            r"^would queue in herdr-fleet for fablyn-dev \(idle\); would deliver what was queued "
                            r"on refusal$"),
                           (["--fyi", "fablyn-dev", "x"], r"^would send to fablyn-dev \(idle\) with 1 queued, \d+ B$")):
            code, out, _ = self.run_cli("send", "--dry-run", *argv)
            self.assertEqual(code, 0)
            self.assertRegex(out.strip(), line)
        self.assertEqual(len(box(self.repo, "fablyn-dev", NEW)), 1)
        self.assertEqual(len(log_entries(self.repo)), 1)
        self.assertEqual(self.prompts_to("fablyn-dev"), [])


class FlushBeforeSend(LiveFleet):
    def test_the_queue_goes_in_front_in_order_with_one_delivered_entry_each(self):
        self.status_of("w2:p2", "blocked")
        self.run_cli("send", "--as", "fablyn-orchestrator", "fablyn-dev", "first")
        self.run_cli("send", "--as", "fablyn-reviewer", "fablyn-dev", "second")
        self.status_of("w2:p2", "idle")
        code, out, _ = self.run_cli("send", "--as", "fablyn-orchestrator", "fablyn-dev", "third")
        self.assertEqual(code, 0, out)
        self.assertRegex(out.strip(), r"^sent to fablyn-dev \(idle\) with 2 queued, \d+ B$")
        (prompt,) = self.prompts_to("fablyn-dev")
        self.assertRegex(prompt, r"^\[from fablyn-orchestrator at %s %s\]\nfirst\n\n"
                                 r"\[from fablyn-reviewer at %s %s\]\nsecond\n\n"
                                 r"\[from fablyn-orchestrator at %s %s\]\nthird$" % ((HHMM, SHORT) * 3))
        entries = log_entries(self.repo)
        self.assertEqual([(e["kind"], e["delivery"], e.get("via"), e["first_line"]) for e in entries],
                         [("send", "queued", None, "first"), ("send", "queued", None, "second"),
                          ("delivered", "sent", "send", "first"), ("delivered", "sent", "send", "second"),
                          ("send", "sent", None, "third")])
        self.assertEqual(entries[2]["id"], entries[0]["id"])
        self.assertEqual(box(self.repo, "fablyn-dev", NEW), [])
        self.assertEqual(len(box(self.repo, "fablyn-dev", CUR)), 3)

    def test_a_working_seat_gets_the_batch_typed_ahead_only_with_now(self):
        self.status_of("w2:p2", "blocked")
        self.run_cli("send", "fablyn-dev", "first")
        self.status_of("w2:p2", "working")
        code, out, _ = self.run_cli("send", "fablyn-dev", "second")  # design 21.1: queued, nothing flushed
        self.assertEqual(code, 0)
        self.assertRegex(out.strip(), r"^queued in herdr-fleet for fablyn-dev \(working\) %s, position 2 of 2$" % SHORT)
        self.assertEqual(self.prompts_to("fablyn-dev"), [])
        code, out, _ = self.run_cli("send", "--now", "fablyn-dev", "third")
        self.assertEqual(code, 0)
        self.assertIn("sent to fablyn-dev (%s) with 2 queued" % place("working"), out)
        self.assertIn("\nfirst\n\n[from caller at", self.prompts_to("fablyn-dev")[0])

    def test_a_refused_prompt_leaves_everything_queued(self):
        self.status_of("w2:p2", "blocked")
        self.run_cli("send", "fablyn-dev", "first")
        self.status_of("w2:p2", "idle")
        self.world["canned"] = [{"prefix": ["agent", "prompt", "fablyn-dev"], "exit": 1, "times": 1,
                                 "stderr": {"error": {"code": "agent_blocked", "message": "blocked"}}}]
        code, out, _ = self.run_cli("send", "fablyn-dev", "second")
        self.assertEqual(code, 0)
        self.assertRegex(out.strip(), r"^queued in herdr-fleet for fablyn-dev \(blocked\) %s, position 2 of 2$" % SHORT)
        self.assertEqual(len(box(self.repo, "fablyn-dev", NEW)), 2)
        self.assertEqual([e["kind"] for e in log_entries(self.repo)], ["send", "send"])

    def test_an_unconfirmed_batch_is_moved_and_logged_unconfirmed(self):
        self.status_of("w2:p2", "blocked")
        self.run_cli("send", "fablyn-dev", "first")
        self.status_of("w2:p2", "idle")
        self.world["behaviors"]["prompt_errors"] = {"fablyn-dev": "agent_prompt_stalled"}
        code, out, _ = self.run_cli("send", "fablyn-dev", "second")
        self.assertEqual(code, 1)
        self.assertIn("unconfirmed fablyn-dev with 1 queued", out)
        self.assertEqual([(e["kind"], e["delivery"]) for e in log_entries(self.repo)[1:]],
                         [("delivered", "unconfirmed"), ("send", "unconfirmed")])
        self.assertEqual(box(self.repo, "fablyn-dev", NEW), [])


class Pointer(LiveFleet):
    def test_a_prompt_over_16_kib_becomes_a_pointer_and_the_files_stay(self):
        code, out, _ = self.run_cli("send", "fablyn-dev", "x" * 17000)
        self.assertEqual(code, 0, out)
        self.assertRegex(out.strip(), r"^queued in herdr-fleet for fablyn-dev \(idle\) %s, position 1 of 1; prompted a "
                                      r"pointer instead: 1 message waiting \(oldest %s, 17\.0 KB\); "
                                      r"run: herdr-fleet inbox --read$" % (SHORT, HHMM))
        (prompt,) = self.prompts_to("fablyn-dev")
        self.assertRegex(prompt, r"^\[from herdr-fleet\] 1 message waiting \(oldest %s, 17\.0 KB\); "
                                 r"run: herdr-fleet inbox --read$" % HHMM)
        self.assertEqual(len(box(self.repo, "fablyn-dev", NEW)), 1)
        self.assertEqual([e["delivery"] for e in log_entries(self.repo)], ["queued"])
        code, out, _ = self.run_cli("inbox", "--as", "fablyn-dev", "--read")
        self.assertIn("x" * 17000, out)
        self.assertEqual(box(self.repo, "fablyn-dev", NEW), [])

    def test_deliver_prompts_a_pointer_for_a_large_queue(self):
        self.status_of("w2:p2", "blocked")
        self.run_cli("send", "fablyn-dev", "a" * 9000)
        self.run_cli("send", "fablyn-dev", "b" * 9000)
        self.status_of("w2:p2", "idle")
        code, out, _ = self.run_cli("deliver")
        self.assertEqual(code, 0)
        self.assertRegex(out.strip(), r"^prompted fablyn-dev \(idle\) with a pointer: 2 messages waiting")
        self.assertTrue(self.prompts_to("fablyn-dev")[0].startswith("[from herdr-fleet] 2 messages waiting"))
        self.assertEqual(len(box(self.repo, "fablyn-dev", NEW)), 2)
        self.assertNotIn("delivered", [e["kind"] for e in log_entries(self.repo)])


class ReplyToAndTtl(LiveFleet):
    def test_reply_to_is_stored_logged_and_rendered(self):
        self.run_cli("send", "--as", "fablyn-orchestrator", "fablyn-dev", "Task 4")
        ask = log_entries(self.repo)[0]["id"]
        self.status_of("w2:p1", "blocked")
        self.run_cli("send", "--as", "fablyn-dev", "--reply-to", "#" + ask[-6:], "fablyn-orchestrator", "done")
        reply = log_entries(self.repo)[-1]
        self.assertEqual(reply["reply_to"], ask)
        (message,) = inbox.read_box(self.repo, "fablyn-orchestrator", NEW)
        self.assertEqual(message.reply_to, ask)
        self.status_of("w2:p1", "idle")
        self.run_cli("deliver", "fablyn-orchestrator")
        self.assertIn(" re #%s]\ndone" % ask[-6:], self.prompts_to("fablyn-orchestrator")[0])

    def test_ttl_is_absolute_and_expires_at_delivery(self):
        self.status_of("w2:p2", "blocked")
        self.run_cli("send", "--ttl", "30m", "fablyn-dev", "still fresh")
        (message,) = inbox.read_box(self.repo, "fablyn-dev", NEW)
        delta = inbox.parse_time(message.ttl) - inbox.parse_time(message.time)
        self.assertEqual(delta, datetime.timedelta(minutes=30))
        stale = put(self.repo, "fablyn-dev", "outdated", ttl=inbox.now_utc() - datetime.timedelta(minutes=1))
        data = json.loads(self.run_cli("status", "--json")[1])
        self.assertEqual(data["queued"], {"fablyn-dev": 1})  # status counts only live messages and moves nothing
        self.assertIn(stale.id, box(self.repo, "fablyn-dev", NEW))
        self.status_of("w2:p2", "idle")
        code, out, _ = self.run_cli("deliver")
        self.assertEqual(code, 0, out)
        self.assertRegex(out.strip(), r"^delivered 1 to fablyn-dev \(idle\)")
        self.assertNotIn("outdated", self.prompts_to("fablyn-dev")[0])
        self.assertIn(stale.id, box(self.repo, "fablyn-dev", CUR))
        expired = [e for e in log_entries(self.repo) if e["kind"] == "expired"]
        self.assertEqual([(e["id"], e["via"], e["delivery"]) for e in expired], [(stale.id, "ttl", "none")])

    def test_ttl_expires_in_inbox(self):
        stale = put(self.repo, "fablyn-dev", "stale", ttl=inbox.now_utc() - datetime.timedelta(seconds=1))
        code, out, _ = self.run_cli("inbox", "--as", "fablyn-dev")
        self.assertEqual((code, out.strip()), (0, "nothing queued for fablyn-dev"))
        self.assertEqual(box(self.repo, "fablyn-dev", CUR), [stale.id])
        self.assertEqual([e["kind"] for e in log_entries(self.repo)], ["expired"])


# -- kickoffs -------------------------------------------------------------------------------------

WITH_TEMPLATE = THREE_SEATS.replace('name = "fablyn-dev"\n', 'name = "fablyn-dev"\nkickoff = ".herdr/kickoff/dev.md"\n')
DEV_TEMPLATE = "[from herdr-fleet] You are {name}.\nTeam:\n{roster}\n\n{inbox}\n\nWhere things stand:\n{state}\n"


class KickoffCarriesTheInbox(LiveFleet):
    def setUp(self):
        super().setUp()
        self.write_file(".herdr/kickoff/dev.md", DEV_TEMPLATE)
        self.write_fleet(WITH_TEMPLATE)
        self.status_of("w2:p2", "blocked")
        self.run_cli("send", "--as", "fablyn-orchestrator", "fablyn-dev", "Range for 1.25.1")
        self.run_cli("send", "--as", "fablyn-reviewer", "--fyi", "fablyn-dev", "round 3 clean")
        self.status_of("w2:p2", "idle")

    def test_the_block_sits_where_the_placeholder_is(self):
        code, out, _ = self.run_cli("kickoff", "fablyn-dev")
        self.assertEqual(code, 0, out)
        self.assertIn("fablyn-dev: briefed - kickoff sent (kickoff) with 2 queued", out)
        (text,) = self.prompts_to("fablyn-dev")
        self.assertRegex(text, r"\n\nMessages that arrived while you were away:\n\[from fablyn-orchestrator at %s %s\]"
                               r"\nRange for 1\.25\.1\n\n\[from fablyn-reviewer at %s %s\]\nround 3 clean\n\n"
                               r"Where things stand:$" % (HHMM, SHORT, HHMM, SHORT))
        self.assertEqual(box(self.repo, "fablyn-dev", NEW), [])
        kinds = [(e["kind"], e.get("via")) for e in log_entries(self.repo)]
        self.assertEqual(kinds[-3:], [("delivered", "kickoff"), ("delivered", "kickoff"), ("kickoff", None)])

    def test_without_the_placeholder_the_block_is_appended(self):
        self.write_fleet(THREE_SEATS.replace('name = "fablyn-dev"\n',
                                             'name = "fablyn-dev"\nkickoff_text = "[from herdr-fleet] Hello {name}."\n'))
        self.run_cli("kickoff", "fablyn-dev")
        (text,) = self.prompts_to("fablyn-dev")
        self.assertTrue(text.startswith("[from herdr-fleet] Hello fablyn-dev.\n\nMessages that arrived while you "
                                        "were away:\n[from fablyn-orchestrator at "), text)

    def test_a_refused_kickoff_leaves_the_messages_queued(self):
        self.status_of("w2:p2", "working")
        code, out, _ = self.run_cli("kickoff", "fablyn-dev")
        self.assertEqual(code, 3)
        self.assertEqual(len(box(self.repo, "fablyn-dev", NEW)), 2)
        self.assertNotIn("delivered", [e["kind"] for e in log_entries(self.repo)])

    def test_drop_inbox_on_kickoff_and_restart(self):
        code, out, _ = self.run_cli("kickoff", "--drop-inbox", "fablyn-dev")
        self.assertEqual(code, 0, out)
        self.assertIn("fablyn-dev: dropped - 2 queued messages moved to cur/", out)
        self.assertNotIn("Messages that arrived", self.prompts_to("fablyn-dev")[0])
        self.assertIn("\n\nWhere things stand:", self.prompts_to("fablyn-dev")[0])
        dropped = [e for e in log_entries(self.repo) if e["kind"] == "dropped"]
        self.assertEqual([(e["via"], e["delivery"]) for e in dropped], [("drop-inbox", "none")] * 2)
        self.assertEqual(box(self.repo, "fablyn-dev", NEW), [])
        self.status_of("w2:p2", "blocked")
        self.run_cli("send", "fablyn-dev", "again")
        self.status_of("w2:p2", "idle")
        code, out, _ = self.run_cli("restart", "--drop-inbox", "fablyn-dev")
        self.assertEqual(code, 0, out)
        self.assertIn("fablyn-dev: dropped - 1 queued message moved to cur/", out)
        self.assertNotIn("Messages that arrived", self.prompts_to("fablyn-dev")[-1])


class UpDeliversAtKickoff(FleetCase):
    def test_a_message_to_an_absent_seat_arrives_with_its_kickoff(self):
        code, out, _ = self.run_cli("send", "--as", "owner", "fablyn-dev", "welcome back")
        self.assertEqual(code, 0)
        self.assertRegex(out.strip(), r"^queued in herdr-fleet for fablyn-dev \(absent\) %s, position 1 of 1$" % SHORT)
        code, out, _ = self.run_cli("up")
        self.assertEqual(code, 0, out)
        kickoff = self.prompts_to("fablyn-dev")[0]
        self.assertRegex(kickoff, r"Messages that arrived while you were away:\n\[from owner at %s %s\]\n"
                                  r"welcome back" % (HHMM, SHORT))
        self.assertNotIn("Messages that arrived", self.prompts_to("fablyn-reviewer")[0])
        self.assertEqual(box(self.repo, "fablyn-dev", NEW), [])
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".herdr", "inbox", "fablyn-reviewer")))

    def test_a_seat_blocked_at_startup_keeps_its_messages(self):
        self.run_cli("send", "--as", "owner", "fablyn-dev", "welcome back")
        self.world["behaviors"]["start_not_ready"] = ["fablyn-dev"]
        code, out, _ = self.run_cli("up")
        self.assertEqual(code, 3)
        self.assertEqual(self.prompts_to("fablyn-dev"), [])
        self.assertEqual(len(box(self.repo, "fablyn-dev", NEW)), 1)

    def test_dry_run_says_what_the_kickoff_carries(self):
        self.run_cli("send", "--as", "owner", "fablyn-dev", "welcome back")
        code, out, _ = self.run_cli("up", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("fablyn-dev carries 1 queued", out)


# -- deliver ---------------------------------------------------------------------------------------


class Deliver(LiveFleet):
    def setUp(self):
        super().setUp()
        self.status_of("w2:p2", "blocked")
        self.status_of("w2:p3", "blocked")
        self.run_cli("send", "--as", "fablyn-orchestrator", "fablyn-dev", "Task A")
        self.run_cli("send", "--as", "fablyn-orchestrator", "--queue", "fablyn-reviewer", "Review B")
        self.run_cli("send", "--as", "fablyn-dev", "--fyi", "fablyn-orchestrator", "FYI only")
        self.status_of("w2:p2", "idle")
        self.status_of("w2:p3", "working")

    def test_without_names(self):
        code, out, _ = self.run_cli("deliver")
        self.assertEqual(code, 0, out)
        lines = out.splitlines()
        self.assertRegex(lines[0], r"^delivered 1 to fablyn-dev \(idle\), \d+ B$")
        self.assertEqual(lines[1], "fablyn-reviewer: working, 1 left queued")
        self.assertEqual(len(lines), 2)  # the FYI-only mailbox is not taken
        self.assertRegex(self.prompts_to("fablyn-dev")[0], r"^\[from fablyn-orchestrator at %s %s\]\nTask A$"
                         % (HHMM, SHORT))
        self.assertEqual(self.prompts_to("fablyn-reviewer"), [])
        self.assertEqual(self.prompts_to("fablyn-orchestrator"), [])
        self.assertEqual([(e["kind"], e["via"]) for e in log_entries(self.repo) if e["kind"] == "delivered"],
                         [("delivered", "deliver")])
        code, out, _ = self.run_cli("deliver", "--fyi")  # seats in file order: the orchestrator comes first
        self.assertEqual(code, 0)
        self.assertRegex(out.splitlines()[0], r"^delivered 1 to fablyn-orchestrator \(idle\)")
        self.assertRegex(out.splitlines()[1], r"^fablyn-reviewer: working, 1 left queued$")

    def test_names_blocked_unknown_and_nothing(self):
        self.status_of("w2:p3", "blocked")
        code, out, _ = self.run_cli("deliver", "fablyn-reviewer", "fablyn-orchestrator")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[0], "fablyn-reviewer: blocked, 1 left queued")
        self.assertRegex(out.splitlines()[1], r"^delivered 1 to fablyn-orchestrator")  # named: FYIs too
        self.assertEqual(self.prompts_to("fablyn-reviewer"), [])
        code, out, _ = self.run_cli("deliver", "fablyn-orchestrator")
        self.assertEqual(out.strip(), "nothing queued for fablyn-orchestrator")
        self.assertEqual(self.run_cli("deliver", "nobody")[0], 2)

    def test_only_fyis_and_dry_run(self):
        self.run_cli("deliver", "fablyn-dev")
        self.status_of("w2:p3", "idle")
        self.run_cli("deliver", "fablyn-reviewer")
        code, out, _ = self.run_cli("deliver")
        self.assertEqual(out.strip(), "nothing to deliver (only FYIs are queued; deliver --fyi takes them)")
        code, out, _ = self.run_cli("deliver", "--fyi", "--dry-run")
        self.assertRegex(out.strip(), r"^would deliver 1 to fablyn-orchestrator \(idle\), \d+ B$")
        self.assertEqual(self.prompts_to("fablyn-orchestrator"), [])
        self.assertEqual(len(box(self.repo, "fablyn-orchestrator", NEW)), 1)

    def test_unconfirmed_exits_1_and_is_never_redelivered(self):
        self.world["behaviors"]["prompt_errors"] = {"fablyn-dev": "timeout"}
        code, out, _ = self.run_cli("deliver", "fablyn-dev")
        self.assertEqual(code, 1)
        self.assertIn("unconfirmed fablyn-dev: herdr error timeout", out)
        self.assertEqual(box(self.repo, "fablyn-dev", NEW), [])
        self.assertEqual([e["delivery"] for e in log_entries(self.repo) if e["kind"] == "delivered"], ["unconfirmed"])
        self.assertEqual(self.run_cli("deliver", "fablyn-dev")[1].strip(), "nothing queued for fablyn-dev")
        self.assertEqual(len(self.prompts_to("fablyn-dev")), 1)


# -- inbox -----------------------------------------------------------------------------------------


class InboxCommand(LiveFleet):
    def setUp(self):
        super().setUp()
        self.status_of("w2:p2", "blocked")
        self.run_cli("send", "--as", "fablyn-orchestrator", "fablyn-dev", "Task A\nwith details")
        self.run_cli("send", "--as", "fablyn-reviewer", "--fyi", "fablyn-dev", "round 3 clean")
        self.ids = [e["id"] for e in log_entries(self.repo)]

    def test_list_count_read(self):
        before = len(self.calls())
        code, out, _ = self.run_cli("inbox", "--as", "fablyn-dev")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertRegex(lines[0], r"^1\. #%s  %s  fablyn-orchestrator  \[on refusal\]  Task A$"
                         % (self.ids[0][-6:], HHMM))
        self.assertRegex(lines[1], r"^2\. #%s  %s  fablyn-reviewer  \[fyi\]  round 3 clean$" % (self.ids[1][-6:], HHMM))
        self.assertEqual(self.run_cli("inbox", "--as", "fablyn-dev", "--count")[1], "2\n")
        self.assertEqual(json.loads(self.run_cli("inbox", "--as", "fablyn-dev", "--count", "--json")[1]),
                         {"name": "fablyn-dev", "count": 2})
        data = json.loads(self.run_cli("inbox", "--as", "fablyn-dev", "--json")[1])
        self.assertEqual([(d["id"], d["from"], d["fyi"]) for d in data],
                         [(self.ids[0], "fablyn-orchestrator", False), (self.ids[1], "fablyn-reviewer", True)])
        self.assertEqual(len(box(self.repo, "fablyn-dev", NEW)), 2)  # listing moves nothing
        code, out, _ = self.run_cli("inbox", "--as", "fablyn-dev", "--read")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"^\[from fablyn-orchestrator at %s #%s\]\nTask A\nwith details\n\n"
                              r"\[from fablyn-reviewer at %s #%s\]\nround 3 clean\n$"
                         % (HHMM, self.ids[0][-6:], HHMM, self.ids[1][-6:]))
        self.assertEqual(box(self.repo, "fablyn-dev", NEW), [])
        self.assertEqual([(e["kind"], e["via"], e["id"]) for e in log_entries(self.repo)[2:]],
                         [("delivered", "inbox", self.ids[0]), ("delivered", "inbox", self.ids[1])])
        self.assertEqual(self.run_cli("inbox", "--as", "fablyn-dev", "--count")[1], "0\n")
        self.assertEqual(len(self.calls()), before)  # with --as, no herdr command at all
        self.assertEqual(self.prompts_to("fablyn-dev"), [])

    def test_history_and_redeliver(self):
        self.run_cli("inbox", "--as", "fablyn-dev", "--read")
        self.run_cli("send", "--as", "fablyn-dev", "--reply-to", self.ids[0], "fablyn-orchestrator", "Task A done")
        put(self.repo, "fablyn-dev", "dropped one", which=CUR)  # no log entry: shown
        self.run_cli("send", "--as", "fablyn-orchestrator", "--ttl", "1m", "fablyn-dev", "expires")
        expiring = log_entries(self.repo)[-1]["id"]
        messages.append(self.repo, {"time": "2026-10-21T10:00:00+11:00", "from": "herdr-fleet", "to": "fablyn-dev",
                                    "kind": "expired", "id": expiring, "via": "ttl", "bytes": 0, "first_line": "x",
                                    "delivery": "none"})
        os.rename(os.path.join(self.repo, ".herdr", "inbox", "fablyn-dev", NEW, expiring + ".md"),
                  os.path.join(self.repo, ".herdr", "inbox", "fablyn-dev", CUR, expiring + ".md"))
        code, out, _ = self.run_cli("inbox", "--as", "fablyn-dev", "--history", "fablyn-orchestrator")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"^\[from fablyn-orchestrator to fablyn-dev at %s #%s\]\nTask A\nwith details\n\n"
                              r"\[from fablyn-dev to fablyn-orchestrator at %s %s re #%s\]\nTask A done\n\n"
                              r"\[from fablyn-orchestrator to fablyn-dev at %s %s\]\ndropped one\n$"
                         % (HHMM, self.ids[0][-6:], HHMM, SHORT, self.ids[0][-6:], HHMM, SHORT))
        self.assertNotIn("expires", out)
        self.assertNotIn("round 3", out)  # from another sender
        code, out, _ = self.run_cli("inbox", "--as", "fablyn-dev", "--redeliver", "#" + self.ids[0][-6:])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "#%s is queued again for fablyn-dev; the next delivery carries it"
                         % self.ids[0][-6:])
        self.assertEqual(box(self.repo, "fablyn-dev", NEW), [self.ids[0]])
        self.assertEqual(self.run_cli("inbox", "--as", "fablyn-dev", "--redeliver", self.ids[0])[0], 2)  # queued
        self.assertEqual(self.run_cli("inbox", "--as", "fablyn-dev", "--redeliver", "#000000")[0], 2)
        self.assertEqual(self.run_cli("inbox", "--as", "fablyn-dev", "--history", "Not A Name")[0], 2)

    def test_the_caller_is_found_from_its_pane_or_is_owner(self):
        self.run_cli("send", "--as", "fablyn-dev", "--queue", "caller", "for the caller")
        before = len(self.calls())
        code, out, _ = self.run_cli("inbox")
        self.assertEqual(code, 0)
        self.assertRegex(out.strip(), r"^1\. %s  %s  fablyn-dev  \[queued\]  for the caller$" % (SHORT, HHMM))
        self.assertEqual([c["argv"][:2] for c in self.calls()[before:]], [["pane", "current"], ["agent", "get"]])
        self.run_cli("send", "--as", "fablyn-dev", "--queue", "owner", "for you")
        env = {"PATH": os.environ.get("PATH", ""), "HOME": self.tmp}  # outside herdr: the caller is owner
        code, out, _ = self.run_cli("inbox", env=env)
        self.assertRegex(out.strip(), r"^1\. %s  %s  fablyn-dev  \[queued\]  for you$" % (SHORT, HHMM))
        self.assertRegex(self.run_cli("inbox", "--as", "owner")[1].strip(), r"for you$")
        self.assertEqual(self.run_cli("inbox", "--as", "Not A Name")[0], 2)
        self.assertEqual(self.run_cli("inbox", "--read", "--count")[0], 2)


# -- owner -----------------------------------------------------------------------------------------


class Owner(LiveFleet):
    def test_inside_herdr_the_owner_gets_a_notification(self):
        code, out, _ = self.run_cli("send", "--as", "fablyn-dev", "owner", "Please approve the deploy\nDetails.")
        self.assertEqual(code, 0, out)
        self.assertRegex(out.strip(), r"^sent to owner \(notification\), \d+ B$")
        ident = log_entries(self.repo)[0]["id"]
        note = self.world["notifications"][-1]
        self.assertEqual(note["title"], "message from fablyn-dev #%s" % ident[-6:])
        self.assertTrue(note["body"].startswith("Please approve the deploy\n"), note)
        self.assertIn("herdr-fleet inbox --as owner --history fablyn-dev", note["body"])
        self.assertEqual(box(self.repo, "owner", CUR), [ident])
        self.assertEqual([(e["kind"], e["delivery"], e.get("via")) for e in log_entries(self.repo)],
                         [("send", "queued", None), ("delivered", "sent", "notify")])
        self.assertEqual(self.argvs("agent", "prompt"), [])

    def test_outside_herdr_it_stays_queued_until_delivered(self):
        env = {"PATH": os.environ.get("PATH", ""), "HOME": self.tmp}
        before = len(self.calls())
        code, out, err = self.run_cli("send", "--as", "fablyn-dev", "owner", "Please approve", env=env)
        self.assertEqual(code, 0, out + err)
        self.assertRegex(out.strip(), r"^queued in herdr-fleet for owner \(not in herdr\) %s, position 1 of 1$" % SHORT)
        self.assertEqual(len(self.calls()), before)  # no herdr command at all
        self.assertEqual(self.world["notifications"], [])
        self.assertEqual(self.run_cli("send", "--as", "fablyn-dev", "owner", "fablyn-orchestrator", "x",
                                      env=env)[0], 1)  # a seat needs herdr
        self.assertEqual(len(box(self.repo, "owner", NEW)), 1)
        code, out, _ = self.run_cli("deliver")
        self.assertEqual((code, out.strip()), (0, "delivered 1 to owner (notification)"))
        self.assertEqual(self.world["notifications"][-1]["title"][:21], "message from fablyn-d")

    def test_queue_and_read_as_owner(self):
        code, out, _ = self.run_cli("send", "--as", "fablyn-dev", "--queue", "owner", "later")
        self.assertRegex(out.strip(), r"^queued in herdr-fleet for owner %s, position 1 of 1$" % SHORT)
        self.assertEqual(self.world["notifications"], [])
        self.assertRegex(self.run_cli("inbox", "--as", "owner", "--read")[1], r"^\[from fablyn-dev at .*\]\nlater\n$")


# -- owed replies (design 18.8) ---------------------------------------------------------------------------


def entry(sender, to, minute, kind="send", first="m", ident=None, delivery=None, fyi=False, reply_to=None):
    item = {"time": "2026-10-20T10:%02d:00+11:00" % minute, "from": sender, "to": to, "kind": kind, "bytes": 1,
            "first_line": first}
    for key, value in (("id", ident), ("delivery", delivery), ("reply_to", reply_to)):
        if value:
            item[key] = value
    if fyi:
        item["fyi"] = True
    return item


def delivered(to, minute, ident, via="deliver"):
    return {"time": "2026-10-20T10:%02d:00+11:00" % minute, "from": "herdr-fleet", "to": to, "kind": "delivered",
            "id": ident, "via": via, "bytes": 0, "first_line": "x", "delivery": "sent"}


class OwedFromDelivery(unittest.TestCase):
    def pairs(self, log):
        return [(o["from"], o["to"], o["first_line"], o["since"][14:16]) for o in messages.owed(log)]

    def test_a_queued_message_owes_nothing_until_delivered(self):
        queued = entry("orch", "dev", 1, first="task", ident="A", delivery="queued")
        self.assertEqual(messages.owed([queued]), [])
        self.assertEqual(self.pairs([queued, delivered("dev", 5, "A")]), [("orch", "dev", "task", "05")])
        expired = {"time": "2026-10-20T10:05:00+11:00", "from": "herdr-fleet", "to": "dev", "kind": "expired",
                   "id": "A", "via": "ttl", "delivery": "none"}
        self.assertEqual(messages.owed([queued, expired]), [])
        refused = entry("orch", "dev", 1, first="task", ident="A", delivery="refused")
        self.assertEqual(messages.owed([refused]), [])

    def test_an_answer_written_before_the_delivery_does_not_settle_it(self):
        log = [entry("orch", "dev", 1, first="task", ident="A", delivery="queued"), entry("dev", "orch", 2, ident="B"),
               delivered("dev", 3, "A")]
        self.assertEqual(self.pairs(log), [("dev", "orch", "m", "02"), ("orch", "dev", "task", "03")])

    def test_a_queued_answer_still_answers_and_a_refused_one_does_not(self):
        ask = entry("orch", "dev", 1, first="task", ident="A")
        self.assertEqual(messages.owed([ask, entry("dev", "orch", 2, fyi=True, ident="B", delivery="queued")]), [])
        self.assertEqual(len(messages.owed([ask, entry("dev", "orch", 2, fyi=True, ident="B",
                                                       delivery="refused")])), 1)

    def test_reply_to_settles_exactly_that_message(self):
        log = [entry("orch", "dev", 1, first="task 1", ident="A"), entry("orch", "dev", 2, first="task 2", ident="B"),
               entry("dev", "orch", 3, fyi=True, ident="C", reply_to="A")]
        self.assertEqual(self.pairs(log), [("orch", "dev", "task 2", "02")])  # the newer one still awaits
        log.append(entry("dev", "orch", 4, fyi=True, ident="D", reply_to="B"))
        self.assertEqual(messages.owed(log), [])

    def test_an_exact_answer_to_the_latest_leaves_the_older_awaiting(self):
        log = [entry("orch", "dev", 1, first="task 1", ident="A"), entry("orch", "dev", 2, first="task 2", ident="B"),
               entry("dev", "orch", 3, fyi=True, ident="C", reply_to="B")]
        self.assertEqual(self.pairs(log), [("orch", "dev", "task 1", "01")])
        log.append(entry("dev", "orch", 4, "ack", "ack"))  # a plain ack settles everything before it
        self.assertEqual(messages.owed(log), [])

    def test_ack_with_an_id_settles_exactly(self):
        log = [entry("orch", "dev", 1, first="task 1", ident="A"), entry("orch", "dev", 2, first="task 2", ident="B"),
               entry("dev", "orch", 3, "ack", "ack", ident="B")]
        self.assertEqual([o["id"] for o in messages.owed(log)], ["A"])


class OwedCommands(LiveFleet):
    def test_reply_to_and_ack_by_id(self):
        self.run_cli("send", "--as", "fablyn-orchestrator", "fablyn-dev", "task 1")
        self.run_cli("send", "--as", "fablyn-orchestrator", "fablyn-dev", "task 2")
        first, second = [e["id"] for e in log_entries(self.repo)]
        self.run_cli("send", "--as", "fablyn-dev", "--fyi", "--reply-to", second, "fablyn-orchestrator", "task 2 done")
        owed = json.loads(self.run_cli("log", "--owed", "--json")[1])
        self.assertEqual([(o["first_line"], o["id"]) for o in owed], [("task 1", first)])
        code, out, _ = self.run_cli("ack", "--as", "fablyn-dev", "#" + first[-6:])
        self.assertEqual(code, 0)
        self.assertRegex(out.strip(), r"^acked #%s from fablyn-orchestrator since %s \(task 1\)$" % (first[-6:], HHMM))
        self.assertEqual(log_entries(self.repo)[-1]["id"], first)
        self.assertEqual(json.loads(self.run_cli("log", "--owed", "--json")[1]), [])
        code, out, _ = self.run_cli("ack", "--as", "fablyn-dev", first[-6:])  # the hex part without #
        self.assertEqual(out.strip(), "nothing owed for #%s" % first[-6:])
        self.assertEqual(self.run_cli("ack", "--as", "fablyn-dev", "#000000")[0], 2)

    def test_a_queued_message_owes_nothing_until_it_is_delivered(self):
        self.status_of("w2:p2", "blocked")
        self.run_cli("send", "--as", "fablyn-orchestrator", "fablyn-dev", "task")
        self.assertNotIn("awaiting:", self.run_cli("status")[1])
        self.status_of("w2:p2", "idle")
        self.run_cli("deliver")
        self.assertIn("awaiting: fablyn-dev owes fablyn-orchestrator for ", self.run_cli("status")[1])


# -- status and the safety hook ------------------------------------------------------------------------


class StatusQueued(LiveFleet):
    def test_queued_line_and_json(self):
        self.assertNotIn("queued:", self.run_cli("status")[1])
        self.assertEqual(json.loads(self.run_cli("status", "--json")[1])["queued"], {})
        self.status_of("w2:p2", "blocked")
        for text in ("a", "b", "c"):
            self.run_cli("send", "fablyn-dev", text)
        self.run_cli("send", "--queue", "owner", "d")
        lines = self.run_cli("status")[1].splitlines()
        (queued,) = [l for l in lines if l.startswith("queued:")]
        self.assertRegex(queued, r"^queued: fablyn-dev 3 \(oldest (<1m|\d+m)\), owner 1$")
        self.assertLess(lines.index(queued), [i for i, l in enumerate(lines) if l.startswith("integration ")][0])
        data = json.loads(self.run_cli("status", "--json")[1])
        self.assertEqual(data["queued"], {"fablyn-dev": 3, "owner": 1})
        self.assertEqual(sorted(data["queued_oldest"]), ["fablyn-dev", "owner"])


class SafetyHookLearnedTheNewCommands(LiveFleet):
    def check_fails(self, command, argv, info):
        with open(self.log_path, "a") as handle:
            handle.write(json.dumps({"command": command}) + "\n")
            handle.write(json.dumps({"argv": argv, "exit": 0, "info": info}) + "\n")
        with self.assertRaises(AssertionError):
            self.assert_safety()
        os.remove(self.log_path)

    def test_deliver_never_prompts_a_working_agent(self):
        self.check_fails(["deliver"], ["agent", "prompt", "fablyn-dev", "x"], {"target_status": "working"})

    def test_inbox_runs_only_the_caller_lookup(self):
        self.check_fails(["inbox"], ["agent", "list"], {})
        self.check_fails(["inbox", "--as", "fablyn-dev"], ["agent", "get", CALLER_PANE], {})


if __name__ == "__main__":
    unittest.main()
