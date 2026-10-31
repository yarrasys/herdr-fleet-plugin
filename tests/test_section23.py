"""Design section 23 (version 0.12.0): mail before reports, senders, debts, and two keys.

23.1: ``send`` prints the inbox note first and its per-target lines last; a plain send
queued to a working seat says when it lands. 23.2: ``inbox --wait [--timeout MS]``.
23.3: an unnamed agent is resolved through the fleet's contacts, else a usage error;
``owner`` only from a pane without an agent or outside herdr. 23.4: ``ack #id --reason``
and the hints. 23.5: ``blocked: a dialog is open``. 23.6: ``peek``. 23.7: one Enter on the
exit confirmation inside ``quit_agent``, and ``interrupt NAME``.

``tests/support.py`` checks every test's log against invariant 21: ``agent send-keys``
only as one ``enter`` from restart, swap or down right after an ``agent read`` showing the
highlighted exit confirmation, or as the kind's interrupt keys from ``interrupt`` to a
seat working at its last ``agent get``; never from watch, the popup, deliver or a dry run."""

from __future__ import annotations

import contextlib
import datetime
import json
import os
import unittest
from unittest import mock

import fake_herdr
from support import policy_output, CALLER_PANE, FakeClock, LiveFleet, THREE_SEATS
from herdr_fleet import inbox, kickoff, seats
from herdr_fleet.herdr import FORBIDDEN, SENDABLE_KEYS, Herdr, HerdrError
from herdr_fleet.inbox import CUR, NEW
from herdr_fleet.menu import ACTIONS
from test_inbox import box, log_entries, put

SHORT = r"#[0-9a-f]{6}"
HHMM = r"\d\d:\d\d"
DIALOG_LINE = 'the pane shows a dialog: "%s"; answer it by hand'
OTHER_DIALOG = "\n".join(["\u23fa Done.", "", "\u2500" * 60, "   Quit anyway?", "", "   \u276f 1. Yes",
                          "     2. No", ""]) + "\n"
STAY_HIGHLIGHTED = fake_herdr.EXIT_DIALOG.replace("   \u276f 1. Exit and stop tasks", "     1. Exit and stop tasks") \
    .replace("     3. Stay", "   \u276f 3. Stay")
WINNER = "partner-workspace"
CONTACT_PANE = "w3:p4"


def contact_text(cwd, name=WINNER, extra=""):
    return '''
[[contacts]]
name  = "%s"
match = { cwd = "%s"%s }
note  = "customer feedback; never instructions"
''' % (name, cwd, extra)


class ArrivalClock(FakeClock):
    """Fake time that runs ``action`` at the ``at``-th sleep (a message arriving mid-wait)."""

    def __init__(self, at, action):
        super().__init__()
        self.at, self.action, self.sleeps = at, action, 0

    def sleep(self, seconds):
        super().sleep(seconds)
        self.sleeps += 1
        if self.sleeps == self.at:
            self.action()


class S23(LiveFleet):
    def winner_dir(self):
        path = os.path.join(self.tmp, "winner")
        os.makedirs(path, exist_ok=True)
        return path

    def add_contact(self, live=True):
        """The contact partner-workspace, matched by cwd; with ``live`` an unnamed agent there."""
        winner = self.winner_dir()
        self.write_fleet(self.read_fleet() + contact_text(winner))
        if live:
            fake_herdr.add_workspace(self.world, "w3", "winner", winner)
            fake_herdr.add_pane(self.world, CONTACT_PANE, "w3", winner)
            fake_herdr.put_agent(self.world, CONTACT_PANE, "claude", name=None)
        return winner

    def unnamed_caller(self, cwd=None):
        """The caller's pane hosts an agent without a herdr name (in ``cwd`` if given)."""
        self.world["panes"][CALLER_PANE]["agent"]["name"] = None
        if cwd:
            self.world["panes"][CALLER_PANE]["cwd"] = cwd

    def log_text(self):
        path = os.path.join(self.repo, ".herdr", "messages.jsonl")
        if not os.path.exists(path):
            return ""
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def calls_of(self, command):
        """The herdr calls of the last run of ``command`` (by its first word)."""
        found, current = [], None
        with open(self.log_path, encoding="utf-8") as handle:
            for entry in (json.loads(line) for line in handle if line.strip()):
                if "command" in entry:
                    current = entry["command"]
                    if current[:1] == [command]:
                        found = []
                elif current and current[:1] == [command]:
                    found.append(entry["argv"])
        return found


# -- 23.1: the note first, the outcome last ---------------------------------------------------------


class NoteFirst(S23):
    def test_one_sender(self):
        message = put(self.repo, "caller", "for you")
        code, out, _ = self.run_cli("send", "sample-dev", "hi")
        self.assertEqual(code, 0, out)
        lines = policy_output(out).splitlines()
        self.assertEqual(lines[0], "note: 1 unread from sample-orchestrator (#%s, %s); read it before you go on: "
                                   "herdr-fleet inbox --read" % (message.short, inbox.time_label(message.time)))
        self.assertRegex(lines[1], r"^sent to sample-dev \(idle\), \d+ B$")
        self.assertEqual(len(lines), 2)  # the outcome is the last line: `send ... | tail -1` sees it

    def test_several_senders_in_the_order_of_their_oldest_message(self):
        now = inbox.now_utc()
        first = put(self.repo, "caller", "a", sender="sample-reviewer", when=now - datetime.timedelta(minutes=30))
        put(self.repo, "caller", "b", sender="sample-orchestrator", when=now - datetime.timedelta(minutes=20))
        put(self.repo, "caller", "c", sender="sample-reviewer", when=now - datetime.timedelta(minutes=10))
        out = self.run_cli("send", "sample-dev", "hi")[1]
        self.assertEqual(policy_output(out).splitlines()[0], "note: 3 unread from sample-reviewer and sample-orchestrator (oldest "
                                              "#%s, %s); read them before you go on: herdr-fleet inbox --read"
                         % (first.short, inbox.time_label(first.time)))
        put(self.repo, "caller", "d", sender="owner")
        self.assertIn("note: 4 unread from sample-reviewer, sample-orchestrator and owner (oldest ",
                      self.run_cli("send", "sample-dev", "hi")[1].splitlines()[0])

    def test_as_names_the_mailbox_in_the_command(self):
        put(self.repo, "sample-dev", "for dev", sender="sample-reviewer")
        self.status_of("w2:p2", "working")  # nothing delivers it meanwhile
        out = self.run_cli("send", "--as", "sample-dev", "sample-orchestrator", "hi")[1]
        self.assertRegex(policy_output(out).splitlines()[0], r"^note: 1 unread from sample-reviewer \(%s, %s\); read it before you "
                                              r"go on: herdr-fleet inbox --as sample-dev --read$" % (SHORT, HHMM))

    def test_dry_runs_print_it_too_and_nothing_moves(self):
        put(self.repo, "caller", "for you")
        code, out, _ = self.run_cli("send", "--dry-run", "sample-dev", "hi")
        lines = policy_output(out).splitlines()
        self.assertTrue(lines[0].startswith("note: 1 unread from sample-orchestrator"), out)
        self.assertRegex(lines[1], r"^would send to sample-dev \(idle\), \d+ B$")
        self.assertEqual(len(box(self.repo, "caller", NEW)), 1)

    def test_no_note_without_mail_or_with_only_expired_mail(self):
        out = self.run_cli("send", "sample-dev", "hi")[1]
        self.assertNotIn("note:", out)
        put(self.repo, "caller", "old", ttl=inbox.now_utc() - datetime.timedelta(minutes=1))
        self.assertNotIn("note:", self.run_cli("send", "sample-dev", "hi")[1])

    def test_a_plain_send_queued_to_a_working_seat_says_when_it_lands(self):
        self.status_of("w2:p2", "working")
        out = self.run_cli("send", "sample-dev", "when you are free")[1]
        self.assertRegex(policy_output(out).strip(), r"^queued in herdr-fleet for sample-dev \(working\) %s, position 1 of 1; "
                                      r"lands at its next idle, or now with --now$" % SHORT)
        dry = self.run_cli("send", "--dry-run", "sample-dev", "x")[1]
        self.assertEqual(policy_output(dry).strip(), "would queue in herdr-fleet for sample-dev (working); lands at its next idle, "
                                      "or now with --now")
        for flags in (["--fyi"], ["--queue"], ["--when-ready", "1000"]):
            with self.subTest(flags=flags):
                line = policy_output(self.run_cli("send", *(flags + ["sample-dev", "y"]))[1]).strip()
                self.assertTrue(line.startswith("queued in herdr-fleet for sample-dev (working"), line)
                self.assertNotIn("lands at", line)
        self.status_of("w2:p2", "idle")
        first = policy_output(self.run_cli("send", "--queue", "sample-dev", "z")[1]).splitlines()[0]
        self.assertRegex(first, r"^queued in herdr-fleet for sample-dev \(idle\) %s, position \d+ of \d+$" % SHORT)


# -- 23.2: inbox --wait ---------------------------------------------------------------------------------


class InboxWait(S23):
    def test_something_already_waiting_returns_at_once(self):
        message = put(self.repo, "caller", "Review parser change")
        code, out, _ = self.run_cli("inbox", "--wait", "--timeout", "600000")
        self.assertEqual(code, 0, out)
        self.assertRegex(policy_output(out), r"^\[from sample-orchestrator at %s %s\]\nReview parser change\n$" % (HHMM, SHORT))
        self.assertEqual(self.clock.slept, 0)
        self.assertEqual(box(self.repo, "caller", CUR), [message.id])
        self.assertEqual([(e["kind"], e["via"]) for e in log_entries(self.repo)], [("delivered", "inbox")])

    def test_it_waits_on_the_files_and_reads_what_arrives(self):
        self.clock = ArrivalClock(7, lambda: put(self.repo, "caller", "the reply", sender="sample-reviewer"))
        code, out, _ = self.run_cli("inbox", "--wait")
        self.assertEqual(code, 0, out)
        self.assertIn("the reply", out)
        self.assertEqual(self.clock.slept, 7.0)  # one look per second
        self.assertEqual(box(self.repo, "caller", NEW), [])
        calls = self.calls_of("inbox")  # no herdr command after the caller lookup
        self.assertEqual([argv[:2] for argv in calls], [["pane", "current"], ["agent", "get"]])

    def test_timeout(self):
        code, out, _ = self.run_cli("inbox", "--wait", "--timeout", "600000")
        self.assertEqual((code, policy_output(out)), (3, "nothing arrived in 10m\n"))
        self.assertEqual(self.clock.slept, 600.0)
        code, out, _ = self.run_cli("inbox", "--wait", "--timeout", "30000")
        self.assertEqual((code, policy_output(out)), (3, "nothing arrived in 30 s\n"))

    def test_fyis_count(self):
        put(self.repo, "caller", "cleanup done", fyi=True)
        code, out, _ = self.run_cli("inbox", "--wait", "--timeout", "5000")
        self.assertEqual(code, 0)
        self.assertIn("cleanup done", out)

    def test_a_message_past_its_ttl_does_not_end_the_wait(self):
        put(self.repo, "caller", "stale", ttl=inbox.now_utc() - datetime.timedelta(minutes=1))
        code, out, _ = self.run_cli("inbox", "--wait", "--timeout", "5000")
        self.assertEqual((code, policy_output(out)), (3, "nothing arrived in 5 s\n"))
        self.clock = ArrivalClock(2, lambda: put(self.repo, "caller", "fresh"))
        code, out, _ = self.run_cli("inbox", "--wait", "--timeout", "5000")
        self.assertEqual(code, 0)
        self.assertIn("fresh", out)
        self.assertNotIn("stale", out)
        self.assertEqual([e["kind"] for e in log_entries(self.repo)], ["expired", "delivered"])

    def test_as_json_and_full(self):
        put(self.repo, "sample-dev", "for dev")
        code, out, _ = self.run_cli("inbox", "--as", "sample-dev", "--wait", "--json")
        self.assertEqual(code, 0)
        (item,) = json.loads(out)
        self.assertEqual(item["body"], "for dev")
        put(self.repo, "sample-dev", "x" * 9000)
        out = self.run_cli("inbox", "--as", "sample-dev", "--wait", "--full")[1]
        self.assertIn("x" * 9000, out)

    def test_usage_errors(self):
        for argv in (["--wait", "--count"], ["--wait", "--history", "sample-dev"], ["--wait", "--redeliver", "#abcdef"],
                     ["--timeout", "5000"], ["--wait", "--timeout", "-1"], ["--wait", "--hook", "stop"]):
            with self.subTest(argv=argv):
                self.assertEqual(self.run_cli("inbox", *argv)[0], 2)

    def test_the_watcher_and_the_wait_share_the_mailbox_lock(self):
        """A message the watcher delivered between the wait's look and its locked read is not
        read again: the wait goes on until the next one."""
        first = put(self.repo, "caller", "taken by the watcher")
        real = inbox.locked
        entered = []

        @contextlib.contextmanager
        def watcher_first(repo, name):
            entered.append(name)
            if len(entered) == 1:  # the watcher held this lock and delivered the file
                inbox.move(repo, inbox.read_box(repo, name, NEW)[0], CUR)
            with real(repo, name):
                yield

        self.clock = ArrivalClock(3, lambda: put(self.repo, "caller", "the next one"))
        with mock.patch.object(inbox, "locked", watcher_first):
            code, out, _ = self.run_cli("inbox", "--wait")
        self.assertEqual(code, 0)
        self.assertEqual(entered, ["caller", "caller"])
        self.assertIn("the next one", out)
        self.assertNotIn("taken by the watcher", out)
        self.assertNotIn(first.id, [e.get("id") for e in log_entries(self.repo)])  # the watcher logs its own

    def test_the_roster_sentence(self):
        self.assertIn("To wait for a reply, run `herdr-fleet inbox --wait --timeout 600000` instead of sleeping or "
                      "polling: it returns with the reply inside your turn and costs nothing while it waits.",
                      kickoff.ROSTER_SENTENCE)
        self.assertIn(kickoff.ROSTER_SENTENCE, kickoff.roster_table([("a", "claude", "w1:p1", "/r")]))


# -- 23.3: who the sender is ----------------------------------------------------------------------------


class Sender(S23):
    NO_NAME = "the agent in w1:p1 has no herdr name; pass --as NAME or run: herdr agent rename w1:p1 NAME"

    def test_a_named_agent_is_its_name(self):
        self.run_cli("send", "sample-dev", "hi")
        self.assertRegex(self.prompts_to("sample-dev")[0], r"^\[from caller %s\] hi$" % SHORT)

    def test_an_unnamed_agent_that_a_contact_matches_is_that_contact(self):
        winner = self.add_contact(live=False)
        self.unnamed_caller(winner)
        code, out, err = self.run_cli("send", "sample-dev", "hello from the customer")
        self.assertEqual(code, 0, out + err)
        self.assertRegex(self.prompts_to("sample-dev")[0], r"^\[from partner-workspace %s\] " % SHORT)
        self.assertEqual(log_entries(self.repo)[-1]["from"], WINNER)
        put(self.repo, WINNER, "for the contact")
        out = self.run_cli("inbox")[1]
        self.assertRegex(policy_output(out), r"^1\. %s  %s  sample-orchestrator  \[queued\]  for the contact$" % (SHORT, HHMM))

    def test_an_unnamed_agent_with_no_contact_is_a_usage_error(self):
        self.unnamed_caller()
        code, out, err = self.run_cli("send", "sample-dev", "hi")
        self.assertEqual(code, 2)
        self.assertIn(self.NO_NAME, err)
        self.assertEqual(self.argvs("agent", "prompt"), [])
        self.assertFalse(os.path.isdir(os.path.join(self.repo, ".herdr", "inbox", "sample-dev")))

    def test_two_matching_contacts_are_a_usage_error_too(self):
        winner = self.add_contact(live=False)
        self.write_fleet(self.read_fleet() + contact_text(winner, name="winner-two"))
        self.unnamed_caller(winner)
        code, _, err = self.run_cli("send", "sample-dev", "hi")
        self.assertEqual(code, 2)
        self.assertIn("has no herdr name (it matches the contacts partner-workspace and winner-two); pass --as", err)

    def test_owner_only_from_a_shell_pane_or_outside_herdr(self):
        self.world["panes"][CALLER_PANE]["procs"] = [dict(fake_herdr.SHELL)]
        self.world["panes"][CALLER_PANE]["agent"] = None  # the owner at a verified shell
        self.run_cli("send", "sample-dev", "from the shell")
        self.assertRegex(self.prompts_to("sample-dev")[0], r"^\[from owner %s\] " % SHORT)
        put(self.repo, "owner", "for the owner")
        outside = {k: v for k, v in self.env.items() if k not in ("HERDR_ENV", "HERDR_BIN_PATH")}
        code, out, _ = self.run_cli("inbox", env=outside)
        self.assertEqual(code, 0)
        self.assertIn("for the owner", out)

    def test_each_command_that_resolves_the_caller(self):
        self.unnamed_caller()
        self.status_of("w2:p2", "working")
        for argv in (["send", "sample-dev", "hi"], ["ack", "--all"], ["inbox"], ["inbox", "--read"],
                     ["interrupt", "sample-dev"]):
            with self.subTest(argv=argv):
                code, _, err = self.run_cli(*argv)
                self.assertEqual(code, 2)
                self.assertIn(self.NO_NAME, err)
        self.assertEqual(self.argvs("agent", "send-keys"), [])
        self.assertEqual(self.run_cli("ack", "--as", "sample-dev", "--all")[0], 0)  # --as still works
        self.assertEqual(self.run_cli("log")[0], 0)  # log and go resolve no caller
        self.assertEqual(self.run_cli("go", "sample-dev")[1].strip(), "focused sample-dev (w2:p2, working)")

    def test_a_contact_caller_acks_and_interrupts_as_the_contact(self):
        winner = self.add_contact(live=False)
        self.run_cli("send", "--as", "sample-orchestrator", WINNER, "please confirm")  # queued: no live contact
        message = inbox.read_box(self.repo, WINNER, NEW)[0]
        self.unnamed_caller(winner)
        self.run_cli("inbox", "--read")  # delivered via inbox: now a debt of the contact
        out = self.run_cli("ack", "#" + message.short)[1]
        self.assertTrue(out.startswith("acked #%s from sample-orchestrator" % message.short), out)
        self.assertEqual(log_entries(self.repo)[-1]["from"], WINNER)
        self.status_of("w2:p2", "working")
        self.assertEqual(self.run_cli("interrupt", "sample-dev")[0], 0)
        self.assertEqual(log_entries(self.repo)[-1]["from"], WINNER)


# -- 23.4: debts anyone can close ------------------------------------------------------------------------


class AckReason(S23):
    def debt(self):
        """sample-orchestrator asks sample-dev something: sample-dev owes it."""
        self.run_cli("send", "--as", "sample-orchestrator", "sample-dev", "please review the parser")
        (entry,) = [e for e in log_entries(self.repo) if e["kind"] == "send"]
        return entry["id"]

    def test_a_third_party_closes_it_with_a_reason(self):
        ident = self.debt()
        short = ident[-6:]
        code, out, _ = self.run_cli("ack", "#" + short, "--reason", "superseded")
        self.assertEqual(code, 0, out)
        self.assertRegex(policy_output(out).strip(), r"^acked #%s from sample-orchestrator since %s \(please review the parser\), owed "
                                      r"by sample-dev; reason: superseded$" % (short, HHMM))
        entry = log_entries(self.repo)[-1]
        self.assertEqual({k: entry[k] for k in ("from", "to", "kind", "id", "by", "reason")},
                         {"from": "sample-dev", "to": "sample-orchestrator", "kind": "ack", "id": ident, "by": "caller",
                          "reason": "superseded"})
        self.assertEqual(self.run_cli("log", "--owed")[1].strip(), "nothing awaiting a reply")
        last = self.run_cli("log")[1].splitlines()[-1]
        self.assertTrue(last.endswith("sample-dev → sample-orchestrator  ack #%s  acked by caller: superseded"
                                      % short), last)

    def test_without_a_reason_someone_elses_debt_is_refused(self):
        ident = self.debt()
        before = self.log_text()
        code, _, err = self.run_cli("ack", "#" + ident[-6:])
        self.assertEqual(code, 2)
        self.assertIn("#%s is owed by sample-dev, not you; add --reason to close it anyway" % ident[-6:], err)
        self.assertEqual(self.log_text(), before)
        code, out, _ = self.run_cli("ack", "--as", "sample-dev", "#" + ident[-6:])  # the debtor still can
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("acked #%s from sample-orchestrator" % ident[-6:]), out)

    def test_the_debtor_may_give_a_reason_too_and_a_dry_run_writes_nothing(self):
        ident = self.debt()
        before = self.log_text()
        out = self.run_cli("ack", "--as", "sample-dev", "#" + ident[-6:], "--reason", "done", "--dry-run")[1]
        self.assertRegex(policy_output(out).strip(), r"^would ack #%s from sample-orchestrator since %s \(please review the parser\); "
                                      r"reason: done$" % (ident[-6:], HHMM))
        self.assertEqual(self.log_text(), before)
        self.run_cli("ack", "--as", "sample-dev", "#" + ident[-6:], "--reason", "done")
        self.assertEqual(log_entries(self.repo)[-1]["by"], "sample-dev")

    def test_reason_goes_with_ids_only(self):
        ident = self.debt()
        for argv in (["sample-orchestrator", "--reason", "x"], ["--all", "--reason", "x"],
                     ["--older-than", "1m", "--reason", "x"], ["#" + ident[-6:], "--reason", " "]):
            with self.subTest(argv=argv):
                self.assertEqual(self.run_cli("ack", "--as", "sample-dev", *argv)[0], 2)

    def test_status_says_how_to_settle_each_debt(self):
        ident = self.debt()
        lines = [l for l in self.run_cli("status")[1].splitlines() if l.startswith("awaiting:")]
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], r"^awaiting: sample-dev owes sample-orchestrator for <1m \(#%s, since %s\): please "
                                   r"review the parser \(ack sample-orchestrator, or ack #%s\)$"
                         % (ident[-6:], HHMM, ident[-6:]))
        self.assertNotIn("(ack ", self.run_cli("log", "--owed")[1])  # `log --owed` is unchanged

    def test_a_debt_without_an_id_is_settled_by_name(self):
        entry = {"time": inbox.iso_local(inbox.now_utc()), "from": "sample-orchestrator", "to": "sample-dev",
                 "kind": "send", "bytes": 5, "first_line": "old style", "delivery": "sent"}
        with open(os.path.join(self.repo, ".herdr", "messages.jsonl"), "w", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
        (line,) = [l for l in self.run_cli("status")[1].splitlines() if l.startswith("awaiting:")]
        self.assertTrue(line.endswith("old style (ack sample-orchestrator)"), line)

    def test_the_roster_sentence(self):
        self.assertIn("`ack NAME` settles everything NAME asked you; `ack --older-than 2h` the stale ones; "
                      "`ack #id --reason TEXT` any debt at all.", kickoff.ROSTER_SENTENCE)


# -- 23.5: wording ------------------------------------------------------------------------------------


class Wording(S23):
    BLOCKED = "blocked: a dialog is open"

    def test_send_deliver_and_the_lifecycle_commands(self):
        self.status_of("w2:p2", "blocked")
        out = self.run_cli("send", "sample-dev", "a")[1]
        self.assertRegex(policy_output(out).strip(), r"^queued in herdr-fleet for sample-dev \(%s\) %s, position 1 of 1$"
                         % (self.BLOCKED, SHORT))
        self.assertEqual(policy_output(self.run_cli("send", "--now", "sample-dev", "b")[1]).strip(),
                         "refused sample-dev (%s): nothing sent" % self.BLOCKED)
        self.assertEqual(self.run_cli("deliver", "sample-dev")[1].strip(),
                         "sample-dev: %s, 1 left queued" % self.BLOCKED)
        refused = "sample-dev: refused - agent is %s; herdr-fleet never prompts, quits or restarts a blocked agent" \
            % self.BLOCKED
        self.assertIn(refused, self.run_cli("restart", "sample-dev")[1])
        self.assertIn(refused, self.run_cli("down", "sample-dev")[1])
        self.write_fleet(self.read_fleet().replace('name = "sample-dev"\nkind = "claude"\nargs = ["--model", '
                                                   '"claude-opus-5-5"]', 'name = "sample-dev"\nkind = "claude"\n'
                                                   'args = ["--model", "claude-sonnet-5-5"]'))
        self.assertIn(refused, self.run_cli("swap", "sample-dev")[1])
        self.assertIn("sample-dev: not briefed - agent sample-dev is %s; nothing sent" % self.BLOCKED,
                      self.run_cli("kickoff", "sample-dev")[1])
        self.assertEqual(self.argvs("agent", "prompt"), [])

    def test_when_ready(self):
        self.status_of("w2:p2", "blocked")
        out = self.run_cli("send", "--when-ready", "1000", "sample-dev", "a")[1]
        self.assertRegex(policy_output(out).strip(), r"^queued in herdr-fleet for sample-dev \(blocked after --when-ready 1000 ms: a "
                                      r"dialog is open\) %s, position 1 of 1$" % SHORT)

    def test_the_status_column_keeps_the_bare_state(self):
        self.status_of("w2:p2", "blocked")
        out = self.run_cli("status")[1]
        (row,) = [l for l in policy_output(out).splitlines() if l.startswith("sample-dev ")]
        self.assertIn(" blocked", row)
        self.assertNotIn("dialog", out)


# -- 23.6: peek -------------------------------------------------------------------------------------------


class Peek(S23):
    def test_a_seat(self):
        before = os.listdir(os.path.join(self.repo, ".herdr"))
        state = self.read_state()
        code, out, _ = self.run_cli("peek", "sample-dev")
        self.assertEqual(code, 0)
        self.assertEqual(out, fake_herdr.prompt_screen("claude"))
        self.assertEqual(self.argvs("agent", "read"), [["agent", "read", "sample-dev", "--lines", "30"]])
        self.assertEqual(sorted(os.listdir(os.path.join(self.repo, ".herdr"))), sorted(before))  # nothing logged
        self.assertEqual(self.read_state(), state)  # no state_seen either

    def test_lines(self):
        fake_herdr.set_screen(self.world, "w2:p2", "".join("line %d\n" % i for i in range(1, 41)))
        out = self.run_cli("peek", "sample-dev", "--lines", "2")[1]
        self.assertEqual(out, "line 39\nline 40\n")
        self.assertEqual(len(self.run_cli("peek", "sample-dev")[1].splitlines()), 30)
        self.assertEqual(self.run_cli("peek", "sample-dev", "--lines", "0")[0], 2)

    def test_a_contact_and_a_live_agent(self):
        self.add_contact()
        fake_herdr.set_screen(self.world, CONTACT_PANE, "customer screen\n")
        code, out, _ = self.run_cli("peek", WINNER)
        self.assertEqual((code, out), (0, "customer screen\n"))
        self.assertEqual(self.argvs("agent", "read")[-1], ["agent", "read", CONTACT_PANE, "--lines", "30"])
        fake_herdr.add_pane(self.world, "w3:p5", "w3", self.tmp)
        fake_herdr.put_agent(self.world, "w3:p5", "codex", name="stranger")
        self.assertEqual(self.run_cli("peek", "stranger")[1], fake_herdr.prompt_screen("codex"))

    def test_absent_unknown_and_owner(self):
        self.world["panes"]["w2:p2"]["agent"] = None
        self.assertEqual(self.run_cli("peek", "sample-dev"), (3, "sample-dev is absent; nothing to read\n", ""))
        self.add_contact(live=False)
        code, out, _ = self.run_cli("peek", WINNER)
        self.assertEqual(code, 3)
        self.assertTrue(out.startswith("partner-workspace is absent (no live agent matches cwd "), out)
        self.assertEqual(self.run_cli("peek", "nobody")[0], 2)
        self.assertEqual(self.run_cli("peek", "owner")[0], 2)
        self.assertEqual(self.argvs("agent", "read"), [])


# -- 23.7: the exit confirmation -------------------------------------------------------------------------


class ExitConfirmation(S23):
    ANSWERED = "sample-dev: quit - answered its exit confirmation (exit and stop tasks)"

    def assert_answered_once(self):
        keys = [c for c in self.calls() if c["argv"][:2] == ["agent", "send-keys"]]
        self.assertEqual([c["argv"] for c in keys], [["agent", "send-keys", "sample-dev", "enter"]])
        calls = [c["argv"] for c in self.calls()]
        index = calls.index(["agent", "send-keys", "sample-dev", "enter"])
        self.assertEqual(calls[index - 1], ["agent", "read", "sample-dev", "--lines", "16"])

    def test_restart_answers_it_once_and_goes_on(self):
        fake_herdr.exit_dialog_on_quit(self.world, "sample-dev")
        code, out, _ = self.run_cli("restart", "sample-dev")
        self.assertEqual(code, 0, out)
        self.assertIn(self.ANSWERED, out)
        self.assertIn("sample-dev: started", out)
        self.assertIn("sample-dev: briefed", out)
        self.assert_answered_once()
        self.assertGreaterEqual(self.clock.slept, seats.EXIT_DIALOG_AFTER_S)
        self.assertEqual([e["kind"] for e in log_entries(self.repo)], ["kickoff"])  # the key is not logged

    def test_down_and_swap(self):
        fake_herdr.exit_dialog_on_quit(self.world, "sample-dev")
        code, out, _ = self.run_cli("down", "sample-dev")
        self.assertEqual(code, 0, out)
        self.assertIn(self.ANSWERED, out)
        self.assert_answered_once()
        self.assertIsNone(self.world["panes"]["w2:p2"]["agent"])

    def test_swap(self):
        fake_herdr.exit_dialog_on_quit(self.world, "sample-dev")
        self.write_fleet(self.read_fleet().replace('name = "sample-dev"\nkind = "claude"\nargs = ["--model", '
                                                   '"claude-opus-5-5"]', 'name = "sample-dev"\nkind = "claude"\n'
                                                   'args = ["--model", "claude-sonnet-5-5"]'))
        code, out, _ = self.run_cli("swap", "sample-dev")
        self.assertEqual(code, 0, out)
        self.assertIn(self.ANSWERED, out)
        self.assert_answered_once()

    def test_another_dialog_gets_no_key_and_is_reported(self):
        fake_herdr.exit_dialog_on_quit(self.world, "sample-dev", OTHER_DIALOG)
        code, out, _ = self.run_cli("down", "sample-dev")
        self.assertEqual(code, 1)
        self.assertIn("sample-dev: failed - did not exit within 20000 ms after /exit; %s"
                      % (DIALOG_LINE % "Quit anyway?"), out)
        self.assertEqual(self.argvs("agent", "send-keys"), [])

    def test_the_exit_dialog_with_another_choice_highlighted_gets_no_key(self):
        fake_herdr.exit_dialog_on_quit(self.world, "sample-dev", STAY_HIGHLIGHTED)
        out = self.run_cli("down", "sample-dev")[1]
        self.assertIn(DIALOG_LINE % "Background work is running", out)
        self.assertEqual(self.argvs("agent", "send-keys"), [])

    def test_no_dialog_keeps_todays_line(self):
        self.world["behaviors"]["ignore_quit"] = ["sample-dev"]
        out = self.run_cli("down", "sample-dev")[1]
        self.assertIn("sample-dev: failed - agent sample-dev did not exit within 20000 ms after /exit; not retried, no "
                      "keys sent, seat left alone", out)
        self.assertEqual(self.argvs("agent", "send-keys"), [])

    def test_a_kind_without_an_entry_is_never_answered(self):
        fake_herdr.exit_dialog_on_quit(self.world, "sample-reviewer")  # codex: no EXIT_DIALOGS entry
        self.assertNotIn("codex", seats.EXIT_DIALOGS)
        out = self.run_cli("down", "sample-reviewer")[1]
        self.assertIn("sample-reviewer: failed - did not exit within 20000 ms after /quit; %s"
                      % (DIALOG_LINE % "Background work is running"), out)
        self.assertEqual(self.argvs("agent", "send-keys"), [])
        self.assertEqual(len(self.argvs("agent", "read")), 1)  # only for the report, at the deadline

    def test_a_dry_run_reads_nothing_and_presses_nothing(self):
        fake_herdr.exit_dialog_on_quit(self.world, "sample-dev")
        code, out, _ = self.run_cli("restart", "--dry-run", "sample-dev")
        self.assertEqual(code, 0, out)
        self.assertEqual((self.argvs("agent", "read"), self.argvs("agent", "send-keys")), ([], []))

    def test_the_popup_never_answers_it(self):
        fake_herdr.exit_dialog_on_quit(self.world, "sample-dev")
        code, out, _ = self.run_cli("menu", stdin="2\ndown\nn\ny\n\n")
        self.assertIn("sample-dev: failed - did not exit within 20000 ms after /exit; %s"
                      % (DIALOG_LINE % "Background work is running"), out)
        self.assertEqual(self.argvs("agent", "send-keys"), [])

    def test_a_failed_enter(self):
        fake_herdr.exit_dialog_on_quit(self.world, "sample-dev")
        self.world["canned"] = [{"prefix": ["agent", "send-keys"], "exit": 1,
                                 "stderr": {"error": {"code": "server_error", "message": "boom"}}}]
        out = self.run_cli("down", "sample-dev")[1]
        self.assertIn("sample-dev: failed - its exit confirmation is on screen, but the Enter failed: herdr error "
                      "server_error: boom; answer it by hand", out)

    def test_answered_but_still_there(self):
        fake_herdr.exit_dialog_on_quit(self.world, "sample-dev")
        self.world["canned"] = [{"prefix": ["agent", "send-keys"], "stdout": {"result": {"type": "ok"}}}]
        out = self.run_cli("down", "sample-dev")[1]
        self.assertIn("sample-dev: failed - did not exit within 20000 ms after /exit, though its exit confirmation "
                      "was answered once; not retried, no more keys sent, seat left alone", out)
        self.assertEqual(len(self.argvs("agent", "send-keys")), 1)

    def test_the_signature_and_the_first_line_are_pure(self):
        self.assertTrue(seats.shows_exit_dialog("claude", fake_herdr.EXIT_DIALOG))
        self.assertFalse(seats.shows_exit_dialog("claude", STAY_HIGHLIGHTED))
        self.assertFalse(seats.shows_exit_dialog("claude", OTHER_DIALOG))
        self.assertFalse(seats.shows_exit_dialog("codex", fake_herdr.EXIT_DIALOG))
        self.assertFalse(seats.shows_exit_dialog("claude", fake_herdr.EXIT_DIALOG.replace("Background work is running",
                                                                                         "Background")))
        self.assertEqual(seats.dialog_first_line(fake_herdr.EXIT_DIALOG), "Background work is running")
        self.assertEqual(seats.dialog_first_line(OTHER_DIALOG), "Quit anyway?")
        self.assertIsNone(seats.dialog_first_line(fake_herdr.prompt_screen("claude")))  # the empty `❯` line
        self.assertIsNone(seats.dialog_first_line(fake_herdr.prompt_screen("claude", "typed by the owner")))
        boxed = "\n".join(fake_herdr.DIALOG).replace("   1. Yes", "\u276f 1. Yes")
        self.assertEqual(seats.dialog_first_line(boxed), "Bash command")


# -- 23.7: interrupt --------------------------------------------------------------------------------------


class Interrupt(S23):
    AGY = THREE_SEATS + '''
[[agents]]
name = "sample-agy"
kind = "agy"
'''

    def add_agy(self):
        self.write_fleet(self.AGY)
        fake_herdr.add_pane(self.world, "w2:p4", "w2", self.repo, created=True)
        fake_herdr.put_agent(self.world, "w2:p4", "agy", "sample-agy", status="working")

    def keys(self):
        return [argv[3:] for argv in self.argvs("agent", "send-keys")]

    def test_a_working_claude_seat_gets_one_ctrl_c(self):
        self.status_of("w2:p2", "working")
        code, out, _ = self.run_cli("interrupt", "sample-dev")
        self.assertEqual((code, out), (0, "interrupted sample-dev (done)\n"))
        self.assertEqual(self.keys(), [["ctrl+c"]])
        calls = [c["argv"] for c in self.calls()]
        index = calls.index(["agent", "send-keys", "sample-dev", "ctrl+c"])
        self.assertEqual(calls[index - 1], ["agent", "read", "sample-dev", "--lines", "16"])  # invariant 21
        self.assertEqual(self.argvs("agent", "wait"), [["agent", "wait", "sample-dev", "--until", "idle", "--until",
                                                        "done", "--timeout", "10000"]])
        entry = log_entries(self.repo)[-1]
        self.assertEqual({k: v for k, v in entry.items() if k != "time"},
                         {"kind": "interrupt", "from": "caller", "to": "sample-dev", "keys": ["ctrl+c"],
                          "result": "done"})
        line = self.run_cli("log")[1].splitlines()[-1]
        self.assertTrue(line.endswith("  caller  interrupt → sample-dev (done)"), line)
        self.assertEqual(self.run_cli("log", "--owed")[1].strip(), "nothing awaiting a reply")  # not a message

    def test_agy_gets_two_ctrl_c_300_ms_apart(self):
        self.add_agy()
        self.world["behaviors"]["interrupt_results"] = {"sample-agy": ["working", "idle"]}
        code, out, _ = self.run_cli("interrupt", "sample-agy")
        self.assertEqual((code, out), (0, "interrupted sample-agy (idle)\n"))
        self.assertEqual(self.keys(), [["ctrl+c"], ["ctrl+c"]])
        self.assertAlmostEqual(self.clock.slept, seats.INTERRUPT_GAP_S)
        self.assertEqual(seats.INTERRUPT_KEYS, {"claude": ("ctrl+c",), "agy": ("ctrl+c", "ctrl+c")})

    def test_codex_has_no_keys_until_configured(self):
        self.status_of("w2:p3", "working")
        code, out, _ = self.run_cli("interrupt", "sample-reviewer")
        self.assertEqual((code, policy_output(out).strip()), (3, 'no interrupt keys known for codex; set [interrupt] codex = '
                                                 '["ctrl+c"]'))
        self.assertEqual(self.keys(), [])
        self.write_fleet(self.read_fleet() + '\n[interrupt]\ncodex = ["ctrl+c"]\n')
        self.assertEqual(self.run_cli("interrupt", "sample-reviewer")[1], "interrupted sample-reviewer (done)\n")
        self.assertEqual(self.keys(), [["ctrl+c"]])

    def test_the_plugin_config_overrides_and_the_fleet_file_wins(self):
        folder = os.path.join(self.tmp, "config")
        os.makedirs(folder)
        with open(os.path.join(folder, "config.toml"), "w") as handle:
            handle.write('[interrupt]\ncodex = ["esc"]\nclaude = ["esc"]\n')
        self.env["HERDR_PLUGIN_CONFIG_DIR"] = folder
        self.status_of("w2:p3", "working")
        self.assertEqual(self.run_cli("interrupt", "sample-reviewer")[1],  # the fake ignores esc
                         "sample-reviewer still working after 10 s; nothing more sent\n")
        self.assertEqual(self.keys(), [["esc"]])
        self.write_fleet(self.read_fleet() + '\n[interrupt]\nclaude = ["ctrl+c", "esc"]\n')
        self.status_of("w2:p2", "working")
        self.world["behaviors"]["interrupt_results"] = {"sample-dev": ["working"]}
        self.run_cli("interrupt", "sample-dev")
        self.assertEqual(self.keys()[1:], [["ctrl+c"], ["esc"]])

    def test_the_key_whitelist(self):
        for table, error in (('codex = ["ctrl+d"]', "'ctrl+d' not allowed; the keys are limited to ctrl+c, esc, enter"),
                             ('claude = ["ctrl+c", "ctrl+c"]', "a Claude seat gets one ctrl+c at most"),
                             ('codex = []', "must be a non-empty list of keys"),
                             ('codex = "ctrl+c"', "must be a non-empty list of keys")):
            with self.subTest(table=table):
                self.write_fleet(THREE_SEATS + "\n[interrupt]\n%s\n" % table)
                code, _, err = self.run_cli("interrupt", "sample-dev")
                self.assertEqual(code, 2)
                self.assertIn(error, err)
        self.assertEqual(self.keys(), [])
        with self.assertRaises(HerdrError):
            Herdr("herdr", lambda argv: (0, "{}", "")).agent_send_keys("sample-dev", ["ctrl+d"])

    def test_a_bad_config_entry_is_left_out_and_doctor_says_so(self):
        folder = os.path.join(self.tmp, "config")
        os.makedirs(folder)
        with open(os.path.join(folder, "config.toml"), "w") as handle:
            handle.write('[interrupt]\ncodex = ["ctrl+z"]\n')
        self.env["HERDR_PLUGIN_CONFIG_DIR"] = folder
        self.status_of("w2:p3", "working")
        self.assertEqual(self.run_cli("interrupt", "sample-reviewer")[0], 3)
        self.assertEqual(self.keys(), [])
        out = self.run_cli("doctor")[1]
        self.assertRegex(policy_output(out), r"warn  plugin config: .*config\.toml: \[interrupt\]\.codex: 'ctrl\+z' not allowed")

    def test_every_other_state_is_refused(self):
        for state, line in (("idle", "nothing to interrupt: sample-dev is idle"),
                            ("done", "nothing to interrupt: sample-dev is done"),
                            ("unknown", "nothing to interrupt: sample-dev is unknown"),
                            ("blocked", "nothing to interrupt: sample-dev is blocked (a dialog is open)")):
            with self.subTest(state=state):
                self.status_of("w2:p2", state)
                self.assertEqual(self.run_cli("interrupt", "sample-dev"), (3, line + "\n", ""))
        self.world["panes"]["w2:p2"]["agent"] = None
        self.assertEqual(self.run_cli("interrupt", "sample-dev")[1], "nothing to interrupt: sample-dev is absent\n")
        self.assertEqual(self.keys(), [])

    def test_still_working_and_gone(self):
        self.status_of("w2:p2", "working")
        self.world["behaviors"]["interrupt_results"] = {"sample-dev": ["working", "absent"]}
        code, out, _ = self.run_cli("interrupt", "sample-dev")
        self.assertEqual((code, policy_output(out)), (3, "sample-dev still working after 10 s; nothing more sent\n"))
        self.assertEqual(log_entries(self.repo)[-1]["result"], "working")
        code, out, _ = self.run_cli("interrupt", "sample-dev", "--wait", "2500")
        self.assertEqual((code, policy_output(out)), (3, "sample-dev is absent after the keys; nothing more sent\n"))
        self.assertEqual(self.argvs("agent", "wait")[0][-1], "10000")
        self.assertEqual(self.keys(), [["ctrl+c"], ["ctrl+c"]])

    def test_wait_ms_and_its_line(self):
        self.status_of("w2:p2", "working")
        self.world["behaviors"]["interrupt_results"] = {"sample-dev": ["working"]}
        out = self.run_cli("interrupt", "sample-dev", "--wait", "2500")[1]
        self.assertEqual(out, "sample-dev still working after 2.5 s; nothing more sent\n")
        self.assertEqual(self.argvs("agent", "wait")[0][-1], "2500")
        self.assertEqual(self.run_cli("interrupt", "sample-dev", "--wait", "0")[0], 2)

    def test_only_this_fleets_seats(self):
        self.add_contact()
        fake_herdr.add_pane(self.world, "w3:p5", "w3", self.tmp)
        fake_herdr.put_agent(self.world, "w3:p5", "claude", name="stranger", status="working")
        for name, why in ((WINNER, "partner-workspace is a contact"), ("stranger", "stranger is not a seat of"),
                          ("owner", "owner is not a seat"), ("nobody", "nobody is not a seat of")):
            with self.subTest(name=name):
                code, _, err = self.run_cli("interrupt", name)
                self.assertEqual(code, 2)
                self.assertIn("interrupt is for this fleet's seats (%s" % why, err)
        self.assertEqual(self.keys(), [])

    def test_the_callers_own_seat_and_the_owner_typing(self):
        self.status_of("w2:p2", "working")
        env = dict(self.env, HERDR_PANE_ID="w2:p2")
        self.assertEqual(self.run_cli("interrupt", "sample-dev", env=env)[1],
                         "refused sample-dev: it is the caller's own agent; interrupt it yourself\n")
        fake_herdr.focus(self.world, "w2:p2")
        fake_herdr.type_in(self.world, "w2:p2", "half typed")
        self.assertEqual(self.run_cli("interrupt", "sample-dev"),
                         (3, "refused sample-dev: owner typing in w2:p2; no keys sent\n", ""))
        self.assertEqual(self.keys(), [])

    def test_a_dialog_on_screen_gets_no_key(self):
        """A dialog can be on screen a moment before herdr reports the seat blocked (design
        21): the presence guard holds it (23.11), and for a kind the guard cannot read, the
        screen read just before the keys refuses it, so no key answers it."""
        self.status_of("w2:p2", "working")
        dialog = "\n".join(fake_herdr.DIALOG).replace("   1. Yes", "\u276f 1. Yes")
        fake_herdr.set_screen(self.world, "w2:p2", dialog)
        self.assertEqual(self.run_cli("interrupt", "sample-dev"),
                         (3, "refused sample-dev: a dialog or a menu is on screen in w2:p2; answer or close it there; "
                             "no keys sent\n", ""))
        self.write_fleet(self.read_fleet().replace('kind = "codex"', 'kind = "pi"')
                         + '\n[interrupt]\npi = ["ctrl+c"]\n')
        fake_herdr.put_agent(self.world, "w2:p3", "pi", "sample-reviewer", status="working")
        fake_herdr.set_screen(self.world, "w2:p3", dialog)
        self.assertEqual(self.run_cli("interrupt", "sample-reviewer"),
                         (3, 'refused sample-reviewer: a dialog is on screen ("Bash command"); no keys sent\n', ""))
        self.assertEqual(self.keys(), [])
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".herdr", "messages.jsonl")))

    def test_cli_only(self):
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "herdr-plugin.toml")) as h:
            manifest = h.read()
        self.assertNotIn("interrupt", manifest)
        self.assertNotIn("interrupt", ACTIONS)
        self.assertNotIn("peek", ACTIONS)


# -- the client and the harness ----------------------------------------------------------------------------


class Client(unittest.TestCase):
    def herdr(self):
        self.seen = []

        def runner(argv):
            self.seen.append(argv[1:])
            return 0, json.dumps({"result": {"type": "ok"}}), ""
        return Herdr("herdr", runner)

    def test_send_keys_only_through_its_method_and_only_four_keys(self):
        herdr = self.herdr()
        self.assertEqual(SENDABLE_KEYS, ("enter", "esc", "ctrl+c", "ctrl+u"))
        herdr.agent_send_keys("sample-dev", ["enter"])
        herdr.agent_send_keys("sample-dev", ["ctrl+c", "esc"])
        self.assertEqual(self.seen, [["agent", "send-keys", "sample-dev", "enter"],
                                     ["agent", "send-keys", "sample-dev", "ctrl+c", "esc"]])
        for target, keys in (("sample-dev", ["ctrl+d"]), ("sample-dev", []), ("sample-dev", ["enter", "y"]),
                             ("--help", ["enter"]), ("", ["enter"])):
            with self.subTest(target=target, keys=keys):
                with self.assertRaises(HerdrError) as caught:
                    herdr.agent_send_keys(target, keys)
                self.assertEqual(caught.exception.code, "forbidden_command")
        for argv in (["agent", "send-keys", "sample-dev", "enter"], ["pane", "send-keys", "w2:p2", "enter"],
                     ["pane", "send-text", "w2:p2", "x"], ["pane", "run", "w2:p2", "ls"]):
            with self.subTest(argv=argv):
                with self.assertRaises(HerdrError):
                    herdr.run(*argv)
                with self.assertRaises(HerdrError):
                    herdr.text(*argv)
        self.assertEqual(len(self.seen), 2)
        self.assertIn(("agent", "send-keys"), FORBIDDEN)  # refused everywhere but agent_send_keys

    def test_agent_read_is_plain_text(self):
        seen = []

        def runner(argv):
            seen.append(argv[1:])
            return 0, "line 1\nline 2\n", ""
        self.assertEqual(Herdr("herdr", runner).agent_read("sample-dev", 16), "line 1\nline 2\n")
        self.assertEqual(seen, [["agent", "read", "sample-dev", "--lines", "16"]])


class Harness(S23):
    """The checks ``tests/support.py`` adds for invariant 21 catch what they must."""

    def write_log(self, *entries):
        with open(self.log_path, "a") as handle:
            for entry in entries:
                handle.write(json.dumps(entry) + "\n")

    def assert_caught(self, *entries):
        if os.path.exists(self.log_path):
            os.remove(self.log_path)
        self.write_log(*entries)
        with self.assertRaises(AssertionError):
            self.assert_safety()
        os.remove(self.log_path)

    def test_the_rules(self):
        read = {"argv": ["agent", "read", "sample-dev", "--lines", "16"], "exit": 0, "info": {}}
        enter = {"argv": ["agent", "send-keys", "sample-dev", "enter"], "exit": 0,
                 "info": {"keys": ["enter"], "name": "sample-dev", "kind": "claude", "exit_dialog": True}}
        working = {"argv": ["agent", "get", "sample-dev"], "exit": 0, "info": {"name": "sample-dev",
                                                                               "status": "working"}}
        ctrl_c = {"argv": ["agent", "send-keys", "sample-dev", "ctrl+c"], "exit": 0,
                  "info": {"keys": ["ctrl+c"], "name": "sample-dev", "kind": "claude"}}
        self.write_log({"command": ["down"]}, read, enter,
                       {"command": ["interrupt", "sample-dev"]}, working, read, ctrl_c)
        self.assert_safety()  # the two allowed shapes pass
        os.remove(self.log_path)
        no_dialog = dict(enter, info=dict(enter["info"], exit_dialog=False))
        self.assert_caught({"command": ["down"]}, read, no_dialog)                          # no confirmation on screen
        self.assert_caught({"command": ["down"]}, enter)                                    # not after an agent read
        self.assert_caught({"command": ["down"]}, read, enter, read, enter)                 # twice in one command
        self.assert_caught({"command": ["restart", "--dry-run"]}, read, enter)              # a dry run
        for command in (["watch"], ["menu"], ["deliver"], ["send", "sample-dev", "x"], ["status"]):
            with self.subTest(command=command):
                self.assert_caught({"command": command}, read, enter)                       # nobody else
        self.assert_caught({"command": ["down"]}, read, dict(enter, argv=enter["argv"][:3] + ["ctrl+c"],
                                                            info=dict(enter["info"], keys=["ctrl+c"])))
        idle = dict(working, info={"name": "sample-dev", "status": "idle"})
        interrupt = {"command": ["interrupt", "sample-dev"]}
        self.assert_caught(interrupt, idle, read, ctrl_c)                                   # not working
        self.assert_caught(interrupt, working, ctrl_c)                                      # the screen not read
        self.assert_caught(interrupt, working, read, ctrl_c, ctrl_c)                        # two to Claude
        self.assert_caught(interrupt, working, read,
                           dict(ctrl_c, argv=ctrl_c["argv"][:3] + ["esc"], info=dict(ctrl_c["info"], keys=["esc"])))
        self.assert_caught(interrupt, working, read,
                           dict(ctrl_c, argv=ctrl_c["argv"][:3] + ["ctrl+d"], info=dict(ctrl_c["info"],
                                                                                    keys=["ctrl+d"])))
        self.assert_caught({"command": ["status"]}, read)                                   # agent read elsewhere

    def test_the_fake_judges_the_screen(self):
        self.assertTrue(fake_herdr.shows_exit_dialog(fake_herdr.EXIT_DIALOG))
        self.assertFalse(fake_herdr.shows_exit_dialog(STAY_HIGHLIGHTED))
        fake_herdr.show_exit_dialog(self.world, "w2:p2")
        self.assertEqual(self.world["panes"]["w2:p2"]["agent"]["agent_status"], "blocked")
        self.assertEqual(self.run_cli("peek", "sample-dev", "--lines", "16")[1], fake_herdr.EXIT_DIALOG)



# -- 23.11: text left in a seat's input, and the interrupt that leaves it there -------------------------


IN_INPUT = "text in its input (w2:p2); press Ctrl-U there to clear it"


class TextInTheInput(S23):
    """The presence guard reads every target's prompt line, focused or not (23.11 point 1)."""

    def setUp(self):
        super().setUp()
        fake_herdr.type_in(self.world, "w2:p2", "Using the Bash tool, run: sleep 60")  # nobody's focus is there

    def test_the_wording_depends_on_the_focus(self):
        from herdr_fleet import presence
        herdr = Herdr("/fake/bin/herdr", runner=self.fake())
        self.assertEqual(presence.typing(herdr, "w2:p2", "claude"), IN_INPUT)
        self.assertEqual([c["argv"][:2] for c in self.calls()], [["pane", "read"], ["pane", "get"]])
        fake_herdr.focus(self.world, "w2:p2")
        herdr = Herdr("/fake/bin/herdr", runner=self.fake())
        self.assertEqual(presence.typing(herdr, "w2:p2", "claude"), "owner typing in w2:p2")
        fake_herdr.unfocus(self.world)
        fake_herdr.show_dialog(self.world, "w2:p2")
        herdr = Herdr("/fake/bin/herdr", runner=self.fake())
        self.assertEqual(presence.typing(herdr, "w2:p2", "claude"),
                         "a dialog or a menu is on screen in w2:p2; answer or close it there")
        fake_herdr.type_in(self.world, "w2:p2", "")
        before = len(self.calls())
        herdr = Herdr("/fake/bin/herdr", runner=self.fake())
        self.assertIsNone(presence.typing(herdr, "w2:p2", "claude"))
        self.assertEqual([c["argv"][:2] for c in self.calls()[before:]], [["pane", "read"]])  # one call per paste

    def fake(self):
        with open(self.world_path, "w", encoding="utf-8") as handle:
            json.dump(self.world, handle)
        return fake_herdr.Fake(self.world_path, self.log_path)

    def test_send_queues_and_now_refuses(self):
        code, out, _ = self.run_cli("send", "sample-dev", "ping")
        self.assertEqual(code, 0)
        self.assertRegex(policy_output(out).strip(), r"^queued in herdr-fleet for sample-dev \(idle, text in its input \(w2:p2\); "
                                      r"press Ctrl-U there to clear it\) %s, position 1 of 1$" % SHORT)
        code, out, _ = self.run_cli("send", "--now", "sample-dev", "now")
        self.assertEqual((code, policy_output(out).strip()), (1, "refused sample-dev (idle, %s): nothing sent" % IN_INPUT))
        self.assertEqual(self.prompts_to("sample-dev"), [])

    def test_deliver_and_kickoff_hold(self):
        put(self.repo, "sample-dev", "Task A")
        self.assertEqual(self.run_cli("deliver", "sample-dev")[1].strip(), "sample-dev: %s, 1 left queued" % IN_INPUT)
        self.assertIn("sample-dev: not briefed - %s" % IN_INPUT, self.run_cli("kickoff", "sample-dev")[1])
        self.assertEqual(self.prompts_to("sample-dev"), [])

    def test_restart_and_down_name_the_cure(self):  # 23.11 point 3
        for command in ("restart", "down"):
            with self.subTest(command=command):
                code, out, _ = self.run_cli(command, "sample-dev")
                self.assertEqual(code, 3)
                self.assertIn("sample-dev: refused - text in its input (w2:p2); press Ctrl-U there to clear it", out)
        self.assertEqual(self.prompts_to("sample-dev"), [])  # no `/exit` joins the text
        fake_herdr.type_in(self.world, "w2:p2", "")
        self.assertIn("sample-dev: quit - quit with /exit", self.run_cli("down", "sample-dev")[1])

    def test_the_watcher_holds_and_releases(self):
        from test_section21 import HoldClock
        from test_watch import events, inline
        from herdr_fleet import watch
        put(self.repo, "sample-dev", "Task A")

        def change(n):
            if n == 2:
                with open(self.world_path, encoding="utf-8") as handle:
                    world = json.load(handle)
                fake_herdr.type_in(world, "w2:p2", "")  # someone pressed Ctrl-U in the pane
                with open(self.world_path, "w", encoding="utf-8") as handle:
                    json.dump(world, handle)
        self.world["behaviors"]["wait_results"] = {"sample-dev": ["idle"]}
        self.clock = HoldClock(change, cycles=1)
        with mock.patch.object(watch, "spawn", inline):
            code, out, _ = self.run_cli("watch")
        self.assertEqual(code, 0, out)
        self.assertEqual(events(out)[1:], ["waiting on sample-dev (1 queued)", "sample-dev idle; delivering 1",
                                           "sample-dev idle; %s, holding 1" % IN_INPUT,
                                           "delivered 1 to sample-dev", "stopped"])
        self.assertEqual(self.clock.holds, 2)


class InterruptCleanup(S23):
    """``interrupt`` clears the prompt Claude Code put back into the input (23.11 point 2)."""

    def setUp(self):
        super().setUp()
        self.run_cli("send", "--as", "owner", "sample-dev", "Using the Bash tool, run: sleep 60")
        self.status_of("w2:p2", "working")
        self.world["behaviors"]["restore_prompt_on_interrupt"] = True

    def keys(self):
        return [argv[3:] for argv in self.argvs("agent", "send-keys")]

    def test_the_restored_prompt_is_cleared_with_one_ctrl_u(self):
        code, out, _ = self.run_cli("interrupt", "sample-dev")
        self.assertEqual((code, out), (0, "interrupted sample-dev (done); cleared the interrupted prompt from its "
                                          "input\n"))
        self.assertEqual(self.keys(), [["ctrl+c"], ["ctrl+u"]])
        calls = [c["argv"] for c in self.calls()]
        index = calls.index(["agent", "send-keys", "sample-dev", "ctrl+u"])
        read = ["agent", "read", "sample-dev", "--lines", "12", "--format", "ansi"]  # design 32.8
        self.assertEqual(calls[index - 1], read)
        self.assertEqual(calls[index + 1], read)
        self.assertEqual(fake_herdr.prompt_of("claude", fake_herdr.screen_of(self.world, "w2:p2")), "")
        entry = log_entries(self.repo)[-1]
        self.assertEqual((entry["keys"], entry["result"]), (["ctrl+c", "ctrl+u"], "done"))
        self.assertEqual(self.run_cli("send", "sample-dev", "next")[0], 0)  # the next paste lands alone
        self.assertRegex(self.prompts_to("sample-dev")[-1], r"^\[from caller %s\] next$" % SHORT)

    def test_nothing_to_clear_presses_nothing_more(self):
        self.world["behaviors"]["restore_prompt_on_interrupt"] = False
        self.assertEqual(self.run_cli("interrupt", "sample-dev")[1], "interrupted sample-dev (done)\n")
        self.assertEqual(self.keys(), [["ctrl+c"]])

    def test_text_that_stays_is_reported(self):
        self.world["canned"] = [{"prefix": ["agent", "send-keys", "sample-dev", "ctrl+u"],
                                 "stdout": {"result": {"type": "ok"}}}]  # the key changes nothing
        code, out, _ = self.run_cli("interrupt", "sample-dev")
        self.assertEqual((code, policy_output(out)), (3, "interrupted sample-dev (done); its input still holds text; press Ctrl-U "
                                          "there before sending it anything\n"))
        self.assertEqual(self.keys(), [["ctrl+c"], ["ctrl+u"]])
        self.assertEqual(log_entries(self.repo)[-1]["keys"], ["ctrl+c", "ctrl+u"])

    def test_a_failed_ctrl_u_is_reported_the_same(self):
        self.world["canned"] = [{"prefix": ["agent", "send-keys", "sample-dev", "ctrl+u"], "exit": 1,
                                 "stderr": {"error": {"code": "server_error", "message": "boom"}}}]
        code, out, _ = self.run_cli("interrupt", "sample-dev")
        self.assertEqual(code, 3)
        self.assertTrue(out.endswith("; its input still holds text; press Ctrl-U there before sending it "
                                     "anything\n"), out)
        self.assertEqual(log_entries(self.repo)[-1]["keys"], ["ctrl+c"])

    def test_no_cleanup_while_still_working_or_for_a_kind_without_a_prompt_glyph(self):
        self.world["behaviors"]["interrupt_results"] = {"sample-dev": ["working"]}
        self.assertEqual(self.run_cli("interrupt", "sample-dev")[1],
                         "sample-dev still working after 10 s; nothing more sent\n")
        self.write_fleet(self.read_fleet().replace('kind = "codex"', 'kind = "pi"')
                         + '\n[interrupt]\npi = ["ctrl+c"]\n')
        fake_herdr.put_agent(self.world, "w2:p3", "pi", "sample-reviewer", status="working")
        self.assertEqual(self.run_cli("interrupt", "sample-reviewer")[1], "interrupted sample-reviewer (done)\n")
        self.assertEqual(self.keys(), [["ctrl+c"], ["ctrl+c"]])

    def test_the_fake_restores_and_clears(self):
        self.run_cli("interrupt", "sample-dev")
        fake_herdr.type_in(self.world, "w2:p2", "typed")
        fake_herdr.clear_prompt(self.world, "w2:p2")
        self.assertEqual(fake_herdr.prompt_of("claude", fake_herdr.screen_of(self.world, "w2:p2")), "")


class HarnessFor2311(S23):
    def write_log(self, *entries):
        with open(self.log_path, "a") as handle:
            for entry in entries:
                handle.write(json.dumps(entry) + "\n")

    def assert_caught(self, *entries):
        if os.path.exists(self.log_path):
            os.remove(self.log_path)
        self.write_log(*entries)
        with self.assertRaises(AssertionError):
            self.assert_safety()
        os.remove(self.log_path)

    def test_ctrl_u_only_from_interrupt_after_a_read_showing_text(self):
        working = {"argv": ["agent", "get", "sample-dev"], "exit": 0, "info": {"name": "sample-dev",
                                                                               "status": "working"}}
        read = {"argv": ["agent", "read", "sample-dev", "--lines", "16"], "exit": 0, "info": {}}
        ctrl_c = {"argv": ["agent", "send-keys", "sample-dev", "ctrl+c"], "exit": 0,
                  "info": {"keys": ["ctrl+c"], "name": "sample-dev", "kind": "claude"}}
        text = {"argv": ["agent", "read", "sample-dev", "--lines", "12"], "exit": 0, "info": {"input_text": True}}
        empty = dict(text, info={"input_text": False})
        ctrl_u = {"argv": ["agent", "send-keys", "sample-dev", "ctrl+u"], "exit": 0,
                  "info": {"keys": ["ctrl+u"], "name": "sample-dev", "kind": "claude"}}
        interrupt = {"command": ["interrupt", "sample-dev"]}
        self.write_log(interrupt, working, read, ctrl_c, text, ctrl_u, empty)
        self.assert_safety()
        os.remove(self.log_path)
        self.assert_caught(interrupt, working, read, ctrl_c, empty, ctrl_u)           # no text was shown
        self.assert_caught(interrupt, working, read, ctrl_c, ctrl_u)                  # no read before it
        self.assert_caught(interrupt, working, read, ctrl_c, text, ctrl_u, text, ctrl_u)  # twice
        self.assert_caught({"command": ["down"]}, text, ctrl_u)                        # not interrupt
        self.assert_caught({"command": ["send", "sample-dev", "x"]}, text, ctrl_u)

    def test_a_prompt_to_an_unfocused_pane_with_text_fails(self):
        prompt = {"argv": ["agent", "prompt", "sample-dev", "x"], "exit": 0,
                  "info": {"target_status": "idle", "input_held": True}}
        self.assert_caught({"command": ["send", "sample-dev", "x"]}, prompt)
        fake_herdr.type_in(self.world, "w2:p2", "left behind")
        with open(self.world_path, "w", encoding="utf-8") as handle:
            json.dump(self.world, handle)
        fake_herdr.Fake(self.world_path, self.log_path)(["herdr", "agent", "prompt", "sample-dev", "x"])
        (call,) = [c for c in self.calls() if c["argv"][:2] == ["agent", "prompt"]]
        self.assertTrue(call["info"]["input_held"])
        self.assertFalse(call["info"]["owner_typing"])  # nobody's focus: 21.2 alone would have let it in
        os.remove(self.log_path)


if __name__ == "__main__":
    unittest.main()
