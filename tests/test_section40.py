"""Section 40: scoped message inspection, envelopes, state updates and expiry history.

CLI scenarios keep the existing fake-Herdr safety oracle active.
"""
from __future__ import annotations

import datetime
import contextlib
import io
import json
import os
import fcntl
import select
import subprocess
import sys
import unittest
from unittest import mock

from support import LiveFleet
from herdr_fleet import inbox, messages
from herdr_fleet import board
from herdr_fleet.cli import build_parser
from herdr_fleet.inbox import CUR, NEW
from test_inbox import box, log_entries, put
from test_section31 import FleetPair


def snapshot(root):
    result = {}
    for folder, dirs, files in os.walk(root):
        result[os.path.relpath(folder, root)] = tuple(sorted(dirs + files))
        for name in files:
            path = os.path.join(folder, name)
            with open(path, "rb") as handle:
                result[os.path.relpath(path, root)] = (os.stat(path).st_mtime_ns, handle.read())
    return result


# Real processes, only file commands. Signal the flock boundary so the parent
# can arrange a deterministic race without sleeps or live Herdr access.
LOCKING_CLI = '''
import fcntl, sys, os
from herdr_fleet.cli import main
from herdr_fleet import permits
fleet = sys.argv[sys.argv.index('--fleet') + 1]
fixture = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(fleet))))
_Store = permits.Store
class IsolatedStore(_Store):
    def __init__(self, root=None):
        super().__init__(root if root is not None else os.path.join(fixture, 'permits'))
permits.Store = IsolatedStore
original = fcntl.flock
def lock(fd, operation):
    if operation == fcntl.LOCK_EX:
        print("LOCKING", file=sys.stderr, flush=True)
    return original(fd, operation)
fcntl.flock = lock
def no_herdr(argv):
    raise AssertionError("file-only child attempted Herdr: %r" % (argv,))
sys.exit(main(sys.argv[1:], env={'HERDR_SOCKET_PATH': os.path.join(fixture, 'synthetic-herdr.sock')}, runner=no_herdr))
'''


def child_cli(args):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    return subprocess.Popen([sys.executable, "-c", LOCKING_CLI] + list(args),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, env=env)


def wait_for_lock(case, child):
    case.assertTrue(select.select([child.stderr], [], [], 10)[0], "child never attempted the lock")
    case.assertEqual(child.stderr.readline().strip(), "LOCKING")


def finish_child(child):
    try:
        return child.communicate(timeout=15)
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate()


class DirectEnvelope(LiveFleet):
    def test_ordinary_direct_envelope_uses_metadata_and_keeps_body_verbatim(self):
        body = "[from hand-written sender] original\n\n"
        code, out, err = self.run_cli("send", "--as", "sample-orchestrator", "sample-dev", body)
        self.assertEqual(code, 0, out + err)
        (message,) = inbox.read_box(self.repo, "sample-dev", CUR)
        self.assertEqual(message.body, body)
        self.assertEqual(self.prompts_to("sample-dev"),
                         ["[from sample-orchestrator #%s] %s" % (message.short, body)])

    def test_unprefixed_and_indented_prefixed_bodies_keep_stored_bytes(self):
        for body in ("ordinary body\n", "  [from example] original\n\n"):
            self.status_of("w2:p2", "idle")
            code, out, err = self.run_cli("send", "--as", "sample-orchestrator", "sample-dev", body)
            self.assertEqual(code, 0, out + err)
            entry = log_entries(self.repo)[-1]
            message = inbox.resolve(self.repo, entry["id"])
            with open(message.path, "rb") as handle:
                self.assertTrue(handle.read().endswith(b"\n\n" + body.encode("utf-8")))
            self.assertEqual(self.prompts_to("sample-dev")[-1],
                             "[from sample-orchestrator #%s] %s" % (message.short, body))

    def test_timer_batch_and_queued_kickoff_keep_their_composition(self):
        from test_section33 import kickoff
        timer = put(self.repo, "sample-dev", "[from handwritten] timer")
        timer.timer = "sample-daily"
        timer.scheduled_for = "2026-11-01T10:00:00Z"
        self.assertEqual(inbox.direct_text(timer), inbox.item_header(timer) + " " + timer.body)
        ordinary = put(self.repo, "sample-dev", "[from handwritten] ordinary")
        self.assertEqual(messages.compose_prompt([timer, ordinary])[0], inbox.render_batch([timer, ordinary]))
        brief = kickoff(self.repo, "sample-dev", "[from herdr-fleet] verbatim kickoff\n")
        self.assertEqual(messages.compose_prompt([brief])[0], brief.body)
        text = messages.compose_prompt([brief, ordinary])[0]
        self.assertTrue(text.startswith(brief.body.rstrip("\n") + "\n\n"))
        self.assertEqual(text.count("[from herdr-fleet]"), 1)


class ExpiryHistory(LiveFleet):
    def test_malformed_metadata_ids_and_times_are_ignored_without_crashing(self):
        message = put(self.repo, "sample-dev", "FYI: valid expiry", fyi=True)
        base = messages.send_entry(message, "queued")
        for ident in (["malformed-id"], {"malformed": "id"}, None, 42, "not-a-full-id"):
            messages.append(self.repo, dict(base, id=ident))
            messages.append(self.repo, dict(messages.disposal_entry("expired", message, "ttl"), id=ident))
        event = messages.disposal_entry("expired", message, "ttl")
        for when in (None, [], {}, 42, "invalid"):
            messages.append(self.repo, dict(event, time=when))
        messages.append(self.repo, event)
        before = snapshot(self.repo)
        code, out, err = self.run_cli("log", "--expired-fyi", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual([m["id"] for m in json.loads(out)], [message.id])
        self.assertEqual(snapshot(self.repo), before)
        self.assertEqual(self.calls(), [])
        data = json.loads(self.run_cli("status", "--json")[1])
        self.assertEqual([m["id"] for m in data["expired_fyi"]], [message.id])

    def test_expiry_log_does_not_scan_message_files_even_with_action_context(self):
        message = put(self.repo, "sample-dev", "FYI: body must stay unread", fyi=True)
        messages.append(self.repo, messages.disposal_entry("expired", message, "fyi_ttl"))
        before = snapshot(self.repo)
        with mock.patch.object(inbox, "load", side_effect=AssertionError("body scan")):
            code, out, err = self.run_cli("log", "--expired-fyi", "--json",
                                         env=dict(self.env, HERDR_PLUGIN_ACTION_ID="stale-action"))
        self.assertEqual(code, 0, err)
        self.assertEqual(len(json.loads(out)), 1)
        self.assertEqual(snapshot(self.repo), before)
        self.assertEqual(self.calls(), [])

    def test_new_expiry_metadata_works_without_original_send(self):
        message = put(self.repo, "sample-dev", "FYI: synthetic note", sender="sample-reviewer", fyi=True,
                      ttl=inbox.now_utc() - datetime.timedelta(minutes=1))
        self.run_cli("inbox", "--as", "sample-dev", "--read")
        (entry,) = log_entries(self.repo)
        self.assertEqual(entry["from"], "herdr-fleet")
        self.assertEqual((entry.get("sender"), entry.get("fyi")), ("sample-reviewer", True))
        code, out, err = self.run_cli("log", "--expired-fyi", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out), [{"id": message.id, "sender": "sample-reviewer", "to": "sample-dev",
                                           "at": entry["time"], "via": "ttl"}])

    def test_legacy_join_deduplicates_and_keeps_history_after_delivery(self):
        now = inbox.now_utc()
        message = put(self.repo, "sample-dev", "FYI: old note", fyi=True)
        messages.append(self.repo, messages.send_entry(message, "queued"))
        entry = messages.disposal_entry("expired", message, "fyi_ttl")
        entry.pop("sender", None)
        entry.pop("fyi", None)
        entry["time"] = inbox.iso_local(now - datetime.timedelta(days=1))
        messages.append(self.repo, entry)
        messages.append(self.repo, entry)
        messages.append(self.repo, messages.delivered_entry(message, "inbox"))
        before = snapshot(self.repo)
        code, out, err = self.run_cli("log", "--expired-fyi", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(json.loads(out)), 1)
        self.assertEqual(snapshot(self.repo), before)
        data = json.loads(self.run_cli("status", "--json")[1])
        self.assertEqual(data["expired_fyi"][0]["sender"], "sample-orchestrator")
        self.assertIn("expired fyi: 1 messages in the last 7d; some may since have been redelivered",
                      self.run_cli("status")[1])
        # status's existing observations may update state; the metadata log command is strictly read-only.
        self.assertEqual(self.run_cli("log", "--expired-fyi", "--json")[0], 0)
        self.assertEqual([e for e in log_entries(self.repo) if e["kind"] == "expired"], [entry, entry])

    def test_seven_day_boundaries_invalid_future_times_and_latest_valid_expiry(self):
        now = inbox.now_utc().replace(microsecond=0)
        for index, delta in enumerate((datetime.timedelta(days=7), datetime.timedelta(0),
                                       datetime.timedelta(days=7, seconds=1), datetime.timedelta(seconds=-1))):
            message = put(self.repo, "sample-dev", "unread body", fyi=True,
                          ident="20261101T100000Z-%06x" % index)
            entry = messages.disposal_entry("expired", message, "fyi_ttl")
            entry["time"] = inbox.iso_local(now - delta)
            messages.append(self.repo, entry)
        first = messages.disposal_entry("expired", inbox.read_box(self.repo, "sample-dev")[0], "ttl")
        first["time"] = "invalid"
        messages.append(self.repo, first)
        first["time"] = inbox.iso_local(now - datetime.timedelta(days=1))
        messages.append(self.repo, first)
        first["time"] = inbox.iso_local(now - datetime.timedelta(days=2))
        messages.append(self.repo, first)  # log order is not time order
        with mock.patch.object(inbox, "now_utc", return_value=now):
            result = json.loads(self.run_cli("log", "--expired-fyi", "--json")[1])
        self.assertEqual([m["id"][-6:] for m in result], ["000000", "000001"])
        self.assertEqual(result[0]["at"], inbox.iso_local(now - datetime.timedelta(days=1)))
        self.assertEqual(result[0]["via"], "ttl")

    def test_metadata_excludes_nonfyi_timer_kickoff_and_unidentified_expiries(self):
        now = inbox.now_utc()
        ordinary = put(self.repo, "sample-dev", "regular request")
        messages.append(self.repo, messages.disposal_entry("expired", ordinary, "ttl"))
        timer = put(self.repo, "sample-dev", "FYI: timer body", fyi=True)
        timer.timer = "sample-daily"
        timer.scheduled_for = inbox.iso_local(now)
        messages.append(self.repo, messages.send_entry(timer, "queued"))
        timer_event = messages.disposal_entry("expired", timer, "ttl")
        self.assertNotIn("fyi", timer_event)
        messages.append(self.repo, dict(timer_event, fyi=True, sender="sample-orchestrator"))
        brief = put(self.repo, "sample-dev", "FYI: kickoff body", fyi=True)
        brief.queued_by = "kickoff"
        self.assertNotIn("fyi", messages.disposal_entry("expired", brief, "ttl"))
        messages.append(self.repo, messages.disposal_entry("expired", brief, "ttl"))
        unidentified = put(self.repo, "sample-dev", "FYI: cannot classify from body", fyi=True)
        legacy = messages.disposal_entry("expired", unidentified, "fyi_ttl")
        legacy.pop("sender")
        legacy.pop("fyi")
        messages.append(self.repo, legacy)
        self.assertEqual(json.loads(self.run_cli("log", "--expired-fyi", "--json")[1]), [])
        self.assertEqual(json.loads(self.run_cli("status", "--json")[1])["expired_fyi"], [])
        self.assertNotIn("expired fyi:", self.run_cli("status")[1])

    def test_filter_then_last_text_json_and_nonpositive_last(self):
        for i, sender in enumerate(("sample-orchestrator", "sample-reviewer", "sample-orchestrator")):
            message = put(self.repo, "sample-dev", "FYI: secret body %d" % i, fyi=True, sender=sender)
            messages.append(self.repo, messages.disposal_entry("expired", message, "ttl"))
        before = snapshot(self.repo)
        args = ("log", "--expired-fyi", "--seat", "sample-orchestrator")
        data = json.loads(self.run_cli(*args, "--last", "1", "--json")[1])
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["sender"], "sample-orchestrator")
        out = self.run_cli(*args, "--last", "1")[1]
        self.assertIn("sample-orchestrator → sample-dev", out)
        self.assertIn("#" + inbox.short_of(data[0]["id"]), out)
        self.assertIn("(ttl)", out)
        self.assertNotIn("secret body", out)
        for last in ("0", "-1"):
            self.assertEqual(len(json.loads(self.run_cli(*args, "--last", last, "--json")[1])), 2)
        self.assertEqual(len(json.loads(self.run_cli("log", "--expired-fyi", "--seat", "sample-dev", "--json")[1])), 3)
        self.assertEqual(self.run_cli("log", "--expired-fyi", "--owed")[0], 2)
        self.assertEqual(snapshot(self.repo), before)
        self.assertEqual(self.calls(), [])

    def test_status_reuses_log_read_and_does_not_add_herdr_calls(self):
        self.run_cli("status", "--json")  # initial labels belong to status's existing behavior
        start = len(self.calls())
        code, out, err = self.run_cli("status", "--json")
        self.assertEqual(code, 0, err)
        calls = [c["argv"] for c in self.calls()][start:]
        start = len(self.calls())
        message = put(self.repo, "sample-dev", "FYI: synthetic metadata", fyi=True)
        messages.append(self.repo, messages.disposal_entry("expired", message, "fyi_ttl"))
        real_read = messages.read
        with mock.patch.object(messages, "read", wraps=real_read) as reader:
            self.run_cli("status", "--json")
        self.assertEqual(reader.call_count, 1)
        self.assertEqual([c["argv"] for c in self.calls()][start:], calls)

    def test_status_summary_names_explicit_fleet_when_discovery_differs(self):
        message = put(self.repo, "sample-dev", "FYI: external fleet", fyi=True)
        messages.append(self.repo, messages.disposal_entry("expired", message, "ttl"))
        code, out, err = self.run_cli("status", "--fleet", os.path.join(self.repo, ".herdr", "fleet.toml"), cwd=self.tmp)
        self.assertEqual(code, 0, err)
        self.assertIn("see herdr-fleet log --fleet ", out)
        self.assertIn("--expired-fyi", out)


class SelectiveInbox(LiveFleet):
    def test_explicit_inspection_ignores_action_context_without_herdr(self):
        message = put(self.repo, "sample-dev", "pure file inspection")
        before = snapshot(self.repo)
        env = dict(self.env, HERDR_PLUGIN_ACTION_ID="stale-action")
        self.assertEqual(self.run_cli("inbox", "--as", "sample-dev", "--show", message.id, env=env)[0], 0)
        self.assertEqual(self.calls(), [])
        self.assertEqual(snapshot(self.repo), before)

    def test_show_is_scoped_nonconsuming_and_offline(self):
        one = put(self.repo, "sample-dev", "first\n", ident="20261101T100000Z-aabbcc")
        put(self.repo, "sample-reviewer", "other mailbox", ident="20261102T100000Z-aabbcc")
        before = snapshot(self.repo)
        for ref in (one.id, one.short, "#" + one.short):
            code, out, err = self.run_cli("inbox", "--as", "sample-dev", "--show", ref, "--json")
            self.assertEqual(code, 0, err)
            data = json.loads(out)
            self.assertEqual((data["id"], data["body"], data["box"]), (one.id, "first\n", NEW))
        self.assertEqual(snapshot(self.repo), before)
        self.assertEqual(self.calls(), [])

    def test_unknown_ambiguous_and_other_mailbox_references_write_nothing(self):
        one = put(self.repo, "sample-dev", "a", ident="20261101T100000Z-aabbcc")
        put(self.repo, "sample-dev", "b", ident="20261102T100000Z-aabbcc", which=CUR)
        other = put(self.repo, "sample-reviewer", "private")
        before = snapshot(self.repo)
        for mode in ("--show", "--read"):
            for ref in ("#aabbcc", "#000000", other.id, "../outside"):
                code, out, err = self.run_cli("inbox", "--as", "sample-dev", mode, ref)
                self.assertEqual(code, 2, out + err)
                self.assertIn("sample-dev", err)
        self.assertEqual(snapshot(self.repo), before)
        self.assertEqual(self.run_cli("inbox", "--as", "absent-mailbox", "--show", one.id)[0], 2)
        self.assertEqual(snapshot(self.repo), before)

    def test_show_due_expired_and_dropped_files_never_expires_or_delivers(self):
        past = inbox.now_utc() - datetime.timedelta(hours=1)
        one = put(self.repo, "sample-dev", "due", ttl=past)
        two = put(self.repo, "sample-dev", "expired", which=CUR)
        three = put(self.repo, "sample-dev", "dropped", which=CUR)
        messages.append(self.repo, messages.disposal_entry("expired", two, "ttl"))
        messages.append(self.repo, messages.disposal_entry("dropped", three, "inbox --drop"))
        before = snapshot(self.repo)
        for message in (one, two, three):
            code, out, err = self.run_cli("inbox", "--as", "sample-dev", "--show", message.id, "--json")
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)["body"], message.body)
        self.assertEqual(snapshot(self.repo), before)
        self.assertEqual(self.calls(), [])

    def test_caps_full_and_json_body(self):
        body = "\n".join("line %d é" % i for i in range(15)) + "\n"
        self.write_fleet('inbox_print_max = 40\n' + self.read_fleet())
        message = put(self.repo, "sample-dev", body)
        for mode in ("--show", "--read"):
            code, out, err = self.run_cli("inbox", "--as", "sample-dev", mode, message.id)
            self.assertEqual(code, 0, err)
            self.assertIn("in full: ", out)
            self.assertNotIn("line 14", out)
            if mode == "--show":
                self.assertIn(message.path, out)
        code, out, err = self.run_cli("inbox", "--as", "sample-dev", "--show", message.id, "--full")
        self.assertEqual(code, 0, err)
        self.assertIn("line 14 é", out)
        self.assertNotIn("in full:", out)
        data = json.loads(self.run_cli("inbox", "--as", "sample-dev", "--show", message.id, "--json")[1])
        self.assertEqual(data["body"], body)

    def test_invalid_modes_write_nothing(self):
        message = put(self.repo, "sample-dev", "note")
        before = snapshot(self.repo)
        for args in (("--show", message.id, "--wait"), ("--read", message.id, "--wait"),
                     ("--show", message.id, "--timeout", "1"), ("--show", message.id, "--dry-run"),
                     ("--show", message.id, "--fyi"), ("--show", message.id, "--older-than", "1h"),
                     ("--show", message.id, "--read"), ("--show", message.id, "--count"),
                     ("--show", message.id, "--history", "sample-reviewer"),
                     ("--show", message.id, "--redeliver", message.id), ("--ack", message.id)):
            code, out, err = self.run_cli("inbox", "--as", "sample-dev", *args)
            self.assertEqual(code, 2, out + err)
        self.assertEqual(snapshot(self.repo), before)
        self.assertEqual(self.calls(), [])

    def test_implicit_show_does_not_record_state_seen(self):
        message = put(self.repo, "sample-dev", "unread")
        before = snapshot(self.repo)
        env = dict(self.env, HERDR_PANE_ID="w2:p2")
        self.assertEqual(self.run_cli("inbox", "--show", message.id, env=env)[0], 0)
        self.assertEqual(snapshot(self.repo), before)
        self.assertTrue(self.calls())  # identity lookups remain read-only

    def test_bulk_and_wait_remain_bulk_arrays(self):
        for args in (("--read",), ("--wait", "--timeout", "100")):
            expected = [put(self.repo, "sample-dev", body).id for body in ("first", "second")]
            code, out, err = self.run_cli("inbox", "--as", "sample-dev", *args, "--json")
            self.assertEqual(code, 0, err)
            self.assertEqual([m["id"] for m in json.loads(out)], expected)
        self.assertEqual(self.calls(), [])


class StateSections(LiveFleet):
    def setUp(self):
        super().setUp()
        self.write_fleet('state_note = ".herdr/STATE.md"\n' + self.read_fleet())
        self.note = self.write_file(".herdr/STATE.md", "Introduction\n\n## next\nold\n### nested\nkept\n\n# Boundary\nrest\n\n## other\nunchanged\n")

    def test_get_prints_body_only_and_preserves_nested_headings(self):
        before = snapshot(self.repo)
        code, out, err = self.run_cli("state", "get", "next")
        self.assertEqual((code, out), (0, "old\n### nested\nkept\n"), err)
        self.assertEqual(snapshot(self.repo), before)
        self.assertEqual(self.calls(), [])

    def test_append_preserves_sections_and_creates_missing_section_from_file(self):
        code, out, err = self.run_cli("state", "append", "next", "\nadded\n")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.run_cli("state", "get", "next")[1], "old\n### nested\nkept\nadded\n")
        path = self.write_file("addition.txt", "from file\n")
        self.assertEqual(self.run_cli("state", "append", "new", "--file", path)[0], 0)
        self.assertEqual(self.run_cli("state", "get", "new")[1], "from file\n")
        with open(self.note) as handle:
            text = handle.read()
        self.assertTrue(text.startswith("Introduction\n\n"))
        self.assertIn("# Boundary\nrest\n\n## other\nunchanged\n", text)
        self.assertIn("## next (updated ", text)

    def test_missing_and_duplicate_get_and_append_fail_without_writes(self):
        before = snapshot(self.repo)
        self.assertEqual(self.run_cli("state", "get", "absent")[0], 2)
        self.assertEqual(snapshot(self.repo), before)
        self.write_file(".herdr/STATE.md", "## next\na\n## next (updated yesterday)\nb\n")
        before = snapshot(self.repo)
        for args in (("get", "next"), ("append", "next", "more")):
            code, out, err = self.run_cli("state", *args)
            self.assertEqual(code, 2, out + err)
            self.assertIn("duplicate", err)
        self.assertEqual(snapshot(self.repo), before)

    def test_strict_lock_failure_leaves_note_and_tree_unchanged_for_both_writers(self):
        before = snapshot(self.repo)
        for action in ("set", "append"):
            with mock.patch.object(fcntl, "flock", side_effect=OSError("lock refused")):
                code, out, err = self.run_cli("state", action, "next", "new body")
            self.assertEqual(code, 1, out + err)
            self.assertIn("lock", err)
        self.assertEqual(snapshot(self.repo), before)
        self.assertEqual(self.calls(), [])

    def test_appends_from_separate_processes_preserve_both_and_read_under_lock(self):
        children = []
        fd = os.open(os.path.dirname(self.note), os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            for text in ("first addition", "second addition"):
                child = child_cli(["state", "append", "next", text, "--fleet",
                                   os.path.join(self.repo, ".herdr", "fleet.toml")])
                children.append(child)
                wait_for_lock(self, child)
            self.write_file(".herdr/STATE.md", "## next\nwritten while children waited\n")
            fcntl.flock(fd, fcntl.LOCK_UN)
            for child in children:
                out, err = finish_child(child)
                self.assertEqual(child.returncode, 0, out + err)
            result = self.run_cli("state", "get", "next")[1]
            self.assertTrue(result.startswith("written while children waited\n"))
            self.assertEqual(sorted(result.splitlines()[1:]), ["first addition", "second addition"])
        finally:
            os.close(fd)
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.communicate()

    def test_set_shares_append_lock_and_preserves_other_fresh_sections(self):
        fd = os.open(os.path.dirname(self.note), os.O_RDONLY)
        child = None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            child = child_cli(["state", "set", "next", "replacement", "--fleet",
                               os.path.join(self.repo, ".herdr", "fleet.toml")])
            wait_for_lock(self, child)
            self.write_file(".herdr/STATE.md", "## next\nstale\n\n## fresh\nadded while waiting\n")
            fcntl.flock(fd, fcntl.LOCK_UN)
            out, err = finish_child(child)
            self.assertEqual(child.returncode, 0, out + err)
            self.assertEqual(self.run_cli("state", "get", "fresh")[1], "added while waiting\n")
            self.assertEqual(self.run_cli("state", "get", "next")[1], "replacement\n")
        finally:
            os.close(fd)
            if child is not None and child.poll() is None:
                child.kill()
                child.communicate()

    def test_append_trims_only_newlines_and_preserves_leading_old_body(self):
        self.write_file(".herdr/STATE.md", "## next\n\n  old  \n\n")
        self.assertEqual(self.run_cli("state", "append", "next", "\n  new  \n")[0], 0)
        self.assertEqual(self.run_cli("state", "get", "next")[1], "\n  old  \n  new  \n")

    def test_empty_section_and_set_empty_remain_supported(self):
        self.assertEqual(self.run_cli("state", "set", "next", "")[0], 0)
        self.assertEqual(self.run_cli("state", "get", "next")[:2], (0, ""))
        self.assertEqual(self.run_cli("state", "append", "next", "x")[0], 0)
        self.assertEqual(self.run_cli("state", "get", "next")[1], "x\n")

    def test_invalid_writes_and_missing_get_create_nothing(self):
        self.write_fleet('state_note = "missing/STATE.md"\n' + self.read_fleet().split("\n", 1)[1])
        bad = self.write_file("bad-utf8", "placeholder")
        with open(bad, "wb") as handle:
            handle.write(b"\xff")
        before = snapshot(self.repo)
        for args in (("get", "next"), ("append", "next", "  \t\n"), ("append", "next"),
                     ("append", "\r", "text"), ("set", "\n", "text"),
                     ("append", "next", "text", "--file", bad), ("append", "next", "--file", bad),
                     ("set", "next", "--file", bad)):
            self.assertEqual(self.run_cli("state", *args)[0], 2, args)
        self.assertEqual(snapshot(self.repo), before)
        self.assertEqual(self.calls(), [])

    def test_short_and_shell_like_values_are_literal(self):
        for action, value in (("set", "x"), ("append", "$(literal) `text` ; > file")):
            self.assertEqual(self.run_cli("state", action, "next", value)[0], 0)
        self.assertEqual(self.run_cli("state", "get", "next")[1], "x\n$(literal) `text` ; > file\n")

    def test_get_ignores_action_context_and_never_calls_herdr(self):
        env = dict(self.env, HERDR_PLUGIN_ACTION_ID="stale-action")
        self.assertEqual(self.run_cli("state", "get", "next", env=env)[0], 0)
        self.assertEqual(self.calls(), [])


class SelectedReading(LiveFleet):
    def test_mismatched_selected_header_cannot_write_another_mailbox(self):
        message = put(self.repo, "sample-dev", "stored under the selected mailbox")
        with open(message.path) as handle:
            text = handle.read()
        with open(message.path, "w") as handle:
            handle.write(text.replace("to: sample-dev\n", "to: sample-reviewer\n"))
        before = snapshot(self.repo)
        code, out, err = self.run_cli("inbox", "--as", "sample-dev", "--read", message.id)
        self.assertEqual(code, 2, out + err)
        self.assertIn("sample-dev", err)
        self.assertEqual(snapshot(self.repo), before)

    def test_empty_selected_id_is_an_error_and_never_becomes_bulk_read(self):
        put(self.repo, "sample-dev", "must remain queued")
        before = snapshot(self.repo)
        code, out, err = self.run_cli("inbox", "--as", "sample-dev", "--read", "")
        self.assertEqual(code, 2, out + err)
        self.assertEqual(snapshot(self.repo), before)

    def test_read_fresh_leaves_other_due_mail_and_repeated_read_writes_nothing(self):
        other = put(self.repo, "sample-dev", "due", ttl=inbox.now_utc() - datetime.timedelta(minutes=1))
        fresh = put(self.repo, "sample-dev", "fresh")
        code, out, err = self.run_cli("inbox", "--as", "sample-dev", "--read", fresh.id, "--full")
        self.assertEqual(code, 0, err)
        self.assertEqual(box(self.repo, "sample-dev", NEW), [other.id])
        before = snapshot(self.repo)
        self.assertEqual(self.run_cli("inbox", "--as", "sample-dev", "--read", fresh.id)[0], 3)
        self.assertEqual(snapshot(self.repo), before)

    def test_selected_due_fyi_uses_fleet_window_and_enriches_only_its_expiry(self):
        self.write_fleet('fyi_ttl = "1m"\n' + self.read_fleet())
        past = inbox.now_utc() - datetime.timedelta(minutes=2)
        one = put(self.repo, "sample-dev", "FYI: old", fyi=True, when=past)
        inbox.touch(one, past)
        other = put(self.repo, "sample-dev", "FYI: another old", fyi=True, when=past)
        inbox.touch(other, past)
        self.assertEqual(self.run_cli("inbox", "--as", "sample-dev", "--read", one.short)[0], 3)
        self.assertEqual(box(self.repo, "sample-dev", NEW), [other.id])
        entry, = log_entries(self.repo)
        self.assertEqual((entry["id"], entry["sender"], entry["via"], entry["fyi"]),
                         (one.id, "sample-orchestrator", "fyi_ttl", True))

    def test_read_only_selected_and_reading_does_not_acknowledge(self):
        one = put(self.repo, "sample-dev", "do first")
        two = put(self.repo, "sample-dev", "do second")
        for message in (one, two):
            messages.append(self.repo, messages.send_entry(message, "queued"))
        code, out, err = self.run_cli("inbox", "--as", "sample-dev", "--read", "#" + two.short, "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual([m["id"] for m in json.loads(out)], [two.id])
        self.assertEqual(box(self.repo, "sample-dev", NEW), [one.id])
        self.assertEqual(box(self.repo, "sample-dev", CUR), [two.id])
        self.assertEqual([d["id"] for d in messages.owed(log_entries(self.repo))], [two.id])
        self.assertEqual(self.calls(), [])

    def test_selected_expiry_leaves_other_due_messages_untouched(self):
        past = inbox.now_utc() - datetime.timedelta(days=2)
        one = put(self.repo, "sample-dev", "expired one", ttl=past)
        two = put(self.repo, "sample-dev", "expired two", ttl=past)
        code, out, err = self.run_cli("inbox", "--as", "sample-dev", "--read", one.id)
        self.assertEqual(code, 3, out + err)
        self.assertIn("expired", out)
        self.assertIn("--show", out)
        self.assertEqual(box(self.repo, "sample-dev", NEW), [two.id])
        self.assertEqual([(e["kind"], e["id"]) for e in log_entries(self.repo)], [("expired", one.id)])

    def test_show_history_and_repeated_read_do_not_add_receipts(self):
        one = put(self.repo, "sample-dev", "stored history", which=CUR)
        before = snapshot(self.repo)
        code, out, err = self.run_cli("inbox", "--as", "sample-dev", "--show", one.id)
        self.assertEqual(code, 0, err)
        self.assertIn("cur/", out)
        self.assertIn("does not mark it read", out)
        code, out, err = self.run_cli("inbox", "--as", "sample-dev", "--read", one.id)
        self.assertEqual(code, 3, out + err)
        self.assertIn("already in cur/; use inbox --show", out)
        self.assertEqual(snapshot(self.repo), before)

    def test_two_processes_read_once_after_revalidation(self):
        message = put(self.repo, "sample-dev", "one request")
        messages.append(self.repo, messages.send_entry(message, "queued"))
        children = []
        try:
            with inbox.locked(self.repo, "sample-dev"):
                for _ in range(2):
                    child = child_cli(["inbox", "--fleet", os.path.join(self.repo, ".herdr", "fleet.toml"),
                                       "--as", "sample-dev", "--read", message.id])
                    children.append(child)
                    wait_for_lock(self, child)
            outputs = [finish_child(child) for child in children]
            self.assertEqual(sorted(c.returncode for c in children), [0, 3], outputs)
            self.assertIn("already in cur/", "".join(out for out, _ in outputs))
            self.assertEqual(len([e for e in log_entries(self.repo) if e["kind"] == "delivered"]), 1)
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.communicate()

    def test_selected_kickoff_updates_kickoff_state_without_consuming_request(self):
        from test_section33 import kickoff
        brief = kickoff(self.repo, "sample-dev")
        other = put(self.repo, "sample-dev", "later request")
        self.assertEqual(self.run_cli("inbox", "--as", "sample-dev", "--read", brief.id)[0], 0)
        self.assertEqual(box(self.repo, "sample-dev", NEW), [other.id])
        self.assertTrue(self.read_state()["agents"]["sample-dev"]["last_kickoff_at"])
        self.assertEqual([e["kind"] for e in log_entries(self.repo)], ["kickoff", "delivered"])


class RoutedSelection(FleetPair):
    def test_other_fleet_ids_are_not_searched(self):
        message = put(self.other, "sample-dev", "other fleet only")
        before = self.tree()
        for action in ("--show", "--read"):
            self.assertEqual(self.run_cli("inbox", "--as", "sample-dev", action, message.id)[0], 2)
        self.assertEqual(self.tree(), before)

    def test_mirrored_fyi_expiry_metadata_is_counted_once_in_each_fleet(self):
        self.run_cli("send", "--as", "sample-orchestrator", "--fyi", "--queue", "--ttl", "1m", "partner", "note")
        message, = self.pending()
        with mock.patch.object(inbox, "now_utc", return_value=inbox.now_utc() + datetime.timedelta(minutes=2)):
            self.assertEqual(self.pull(message.id)[0], 3)
        results = []
        for root in (self.repo, self.other):
            entries = log_entries(root)
            event, = [e for e in entries if e["kind"] == "expired"]
            self.assertEqual((event["sender"], event["fyi"]), ("sample-orchestrator", True))
            messages.append(root, event)  # a mirrored/recovered duplicate does not add another item
            results.append(json.loads(self.run_cli("log", "--fleet", os.path.join(root, ".herdr", "fleet.toml"),
                                                  "--expired-fyi", "--json")[1]))
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(results[0]), 1)

    def test_selected_crossfleet_receipt_and_ack_propagate_once(self):
        self.send_queue()
        one = self.pending()[0]
        self.send_queue()
        code, out, err = self.pull(one.id, "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual([m["id"] for m in json.loads(out)], [one.id])
        self.assertEqual(len(self.pending()), 1)
        for root in (self.repo, self.other):
            self.assertEqual([d["id"] for d in messages.owed(log_entries(root))], [one.id])
        code, out, err = self.run_cli("ack", "--fleet", self.destination, "--as", "peer-lead", "#" + one.short)
        self.assertEqual(code, 0, out + err)
        for root in (self.repo, self.other):
            self.assertEqual(messages.owed(log_entries(root)), [])

    def test_selected_crossfleet_expiry_mirrors_only_selected(self):
        for _ in range(2):
            self.run_cli("send", "--as", "sample-orchestrator", "--queue", "--ttl", "1m", "partner", "short request")
        one, other = self.pending()
        future = inbox.now_utc() + datetime.timedelta(minutes=2)
        with mock.patch.object(inbox, "now_utc", return_value=future):
            code, out, err = self.pull(one.id)
        self.assertEqual(code, 3, out + err)
        self.assertEqual([m.id for m in self.pending()], [other.id])
        for root in (self.repo, self.other):
            self.assertEqual([e["id"] for e in log_entries(root) if e["kind"] == "expired"], [one.id])

    def test_crossfleet_alias_is_named_only_when_different_including_dry_run(self):
        for target in ("partner", "peer-lead"):
            for dry in ((), ("--dry-run",)):
                code, out, err = self.run_cli("send", "--as", "sample-orchestrator", "--queue", *dry, target, "note")
                self.assertEqual(code, 0, out + err)
                self.assertIn("sender: sample-orchestrator", out)
                self.assertIn("mailbox: %s :: peer-lead" % os.path.realpath(self.destination), out)
                self.assertIn("origin " + os.path.join(self.repo, ".herdr", "fleet.toml"), out)
                if target == "partner":
                    self.assertIn("alias partner -> peer-lead", out)
                else:
                    self.assertNotIn("alias", out)
                    self.assertNotIn("peer-lead -> peer-lead", out)


class LabelsAndHelp(LiveFleet):
    def test_persisted_reason_is_unchanged_while_inbox_and_board_display_plain_send(self):
        self.status_of("w2:p2", "working")
        self.run_cli("send", "--as", "sample-orchestrator", "sample-dev", "queued by a plain send")
        message, = inbox.read_box(self.repo, "sample-dev", NEW)
        self.assertEqual(message.queued_by, "refusal")
        self.assertIn("[plain send]", self.run_cli("inbox", "--as", "sample-dev")[1])
        self.assertEqual(board.messages_to(self.repo, "sample-dev", log_entries(self.repo))[0].state,
                         "queued [plain send]")
        with open(message.path) as handle:
            self.assertIn("queued_by: refusal\n", handle.read())

    def test_generated_help_explains_selected_bulk_read_and_acknowledgement(self):
        parser = build_parser()
        for args, phrases in ((("inbox", "--help"), ("--show", "--read [ID]", "without ID", "herdr-fleet ack")),
                              (("send", "--help"), ("[plain send]", "alias ALIAS -> CANONICAL", "envelope")),
                              (("state", "set", "--help"), ("replace", "append")),
                              (("state", "append", "--help"), ("retaining", "UTF-8")),
                              (("log", "--help"), ("--expired-fyi", "redelivery"))):
            output = io.StringIO()
            with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as caught:
                parser.parse_args(args)
            self.assertEqual(caught.exception.code, 0)
            text = " ".join(output.getvalue().split())
            for phrase in phrases:
                self.assertIn(phrase, text)


class FileOnlyErrors(LiveFleet):
    def leaked_action(self):
        return dict(self.env, HERDR_PLUGIN_ACTION_ID="synthetic-action")

    def test_unknown_explicit_inbox_ids_fail_without_herdr_or_writes_under_leaked_action(self):
        before = snapshot(self.repo)
        for action in ("--show", "--read"):
            code, out, err = self.run_cli("inbox", "--as", "sample-dev", action, "#000000",
                                         "--json", env=self.leaked_action())
            self.assertEqual((code, out), (2, ""), err)
            self.assertIn("no message #000000", err)
            self.assertIn("sample-dev", err)
        self.assertEqual(self.calls(), [])
        self.assertEqual(snapshot(self.repo), before)

    def test_missing_state_get_fails_without_herdr_or_writes_under_leaked_action(self):
        self.write_fleet('state_note = ".herdr/STATE.md"\n' + self.read_fleet())
        self.write_file(".herdr/STATE.md", "## next\nkept\n")
        before = snapshot(self.repo)
        code, out, err = self.run_cli("state", "get", "missing", env=self.leaked_action())
        self.assertEqual((code, out), (2, ""), err)
        self.assertIn("no section ## missing", err)
        self.assertEqual(self.calls(), [])
        self.assertEqual(snapshot(self.repo), before)

    def test_invalid_expiry_log_mode_fails_without_herdr_or_writes_under_leaked_action(self):
        before = snapshot(self.repo)
        code, out, err = self.run_cli("log", "--expired-fyi", "--owed", env=self.leaked_action())
        self.assertEqual((code, out), (2, ""), err)
        self.assertIn("--expired-fyi cannot be combined with --owed", err)
        self.assertEqual(self.calls(), [])
        self.assertEqual(snapshot(self.repo), before)

    def test_file_operations_with_missing_fleet_do_not_notify_under_leaked_action(self):
        path = os.path.join(self.repo, "missing-fleet.toml")
        before = snapshot(self.repo)
        for args in (("inbox", "--as", "sample-dev", "--show", "#000000", "--fleet", path),
                     ("inbox", "--as", "sample-dev", "--read", "#000000", "--fleet", path),
                     ("state", "get", "missing", "--fleet", path),
                     ("log", "--expired-fyi", "--fleet", path)):
            code, out, err = self.run_cli(*args, env=self.leaked_action())
            self.assertEqual((code, out), (2, ""), err)
            self.assertIn("fleet file", err)
        self.assertEqual(self.calls(), [])
        self.assertEqual(snapshot(self.repo), before)

    def test_implicit_show_failure_keeps_read_only_identity_calls_without_notification(self):
        before = snapshot(self.repo)
        env = dict(self.leaked_action(), HERDR_PANE_ID="w2:p2")
        code, out, err = self.run_cli("inbox", "--show", "#000000", "--json", env=env)
        self.assertEqual((code, out), (2, ""), err)
        self.assertIn("sample-dev", err)
        self.assertTrue(self.calls())
        self.assertTrue(all(call["argv"][:2] in (["pane", "current"], ["pane", "get"], ["agent", "get"])
                            for call in self.calls()))
        self.assertEqual(snapshot(self.repo), before)

    def test_legitimate_lifecycle_action_error_still_notifies(self):
        code, out, err = self.run_cli("restart", "missing", env=self.leaked_action())
        self.assertEqual(code, 2, out + err)
        self.assertEqual(len(self.argvs("notification", "show")), 1)
        self.assertEqual(self.world["notifications"][-1]["title"], "Fleet restart failed")

    def test_explicit_lifecycle_notify_still_notifies(self):
        self.world["version"] = "herdr 0.9.3"
        code, out, err = self.run_cli("up", "--notify")
        self.assertEqual(code, 1, out + err)
        self.assertEqual(len(self.argvs("notification", "show")), 1)
        self.assertEqual(self.world["notifications"][-1]["title"], "Fleet up failed")

    def test_manifest_ui_error_still_notifies(self):
        self.world["version"] = "herdr 0.9.3"
        code, out, err = self.run_cli("ui", "status", env=self.leaked_action())
        self.assertEqual(code, 2, out + err)
        self.assertEqual(len(self.argvs("notification", "show")), 1)
        self.assertEqual(self.world["notifications"][-1]["title"], "Fleet status failed")


class ResolutionRace(LiveFleet):
    def test_move_between_new_and_cur_scans_is_history_not_false_ambiguity(self):
        for mode in ("--show", "--read"):
            for reference in ("full", "short", "hash"):
                with self.subTest(mode=mode, reference=reference):
                    message = put(self.repo, "sample-dev", "one real file")
                    initial = snapshot(self.repo)
                    read_box = inbox.read_box
                    moved = []

                    def move_after_new(repo, name, box=NEW, warnings=None, headers_only=False):
                        found = read_box(repo, name, box, warnings, headers_only=headers_only)
                        if repo == self.repo and name == "sample-dev" and box == NEW and not moved:
                            # Another reader holds the real lock while delivering after
                            # this scan, before resolve scans cur/. Return the old NEW observation.
                            with inbox.locked(repo, name):
                                inbox.move(repo, message, CUR)
                                messages.append(repo, messages.delivered_entry(message, "inbox"))
                            moved.append(snapshot(self.repo))
                        return found

                    ref = {"full": message.id, "short": message.short, "hash": "#" + message.short}[reference]
                    with mock.patch.object(inbox, "read_box", side_effect=move_after_new):
                        code, out, err = self.run_cli("inbox", "--as", "sample-dev", mode, ref)
                    self.assertEqual(code, 0 if mode == "--show" else 3, out + err)
                    self.assertIn("cur/", out)
                    self.assertNotIn("matches several", err)
                    self.assertEqual(snapshot(self.repo), moved[0])
                    self.assertNotEqual(initial, moved[0])
                    self.assertEqual(len([e for e in log_entries(self.repo)
                                          if e.get("kind") == "delivered" and e["id"] == message.id]), 1)
        self.assertEqual(self.calls(), [])

    def test_real_duplicate_files_stay_ambiguous_and_write_nothing(self):
        message = put(self.repo, "sample-dev", "real new duplicate")
        put(self.repo, "sample-dev", "real cur duplicate", ident=message.id, which=CUR)
        before = snapshot(self.repo)
        for mode in ("--show", "--read"):
            for ref in (message.id, message.short, "#" + message.short):
                code, out, err = self.run_cli("inbox", "--as", "sample-dev", mode, ref)
                self.assertEqual(code, 2, out + err)
                self.assertIn("matches several", err)
        self.assertEqual(snapshot(self.repo), before)
        self.assertEqual(self.calls(), [])
