"""Daily timers: public commands and real queue/delivery recovery with synthetic data."""
from __future__ import annotations

import datetime
import json
import os
import threading
import time
import unittest
from unittest import mock

import fake_herdr
from herdr_fleet import inbox, messages, timers, timer_calendar, watch
from herdr_fleet.context import OpError
from herdr_fleet.fleetfile import FleetError, load_fleet
from test_watch import WatchCase
from test_inbox import box, log_entries, put
from test_section24 import BoardCase

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 1, 2, 10, 1, tzinfo=UTC)
BODY = "Check the synthetic sample.\n\nKeep this body verbatim.\n"


def timer_text(**changes):
    values = dict(name="daily-check", to="sample-dev", file="timer.md", at="10:00", tz="UTC",
                  starts_at="2026-01-01T00:00:00+00:00", until="2026-01-10T00:00:00+00:00")
    values.update(changes)
    return '\n[[timers]]\n' + ''.join('%s = %s\n' % (k, json.dumps(v)) for k, v in values.items())


class TimerCase(WatchCase):
    def setUp(self):
        super().setUp()
        self.base = self.read_fleet()
        with open(os.path.join(self.repo, "timer.md"), "w", encoding="utf-8") as handle:
            handle.write(BODY)
        self.write_fleet(self.base + timer_text())
        patcher = mock.patch.object(inbox, "now_utc", return_value=NOW)
        patcher.start()
        self.addCleanup(patcher.stop)

    def config(self, **changes):
        self.write_fleet(self.base + timer_text(**changes))

    def queued(self, to="sample-dev"):
        return inbox.read_box(self.repo, to, inbox.NEW)

    def journal(self):
        with open(os.path.join(self.repo, ".herdr", "timers.state.json"), encoding="utf-8") as handle:
            return json.load(handle)

    def tree(self):
        result = {}
        for folder, _, files in os.walk(self.repo):
            for name in files:
                path = os.path.join(folder, name)
                with open(path, "rb") as handle:
                    result[os.path.relpath(path, self.repo)] = handle.read()
        return result


class TimerIntegration(TimerCase):

    def test_public_offline_dry_run_has_no_writes_or_herdr_calls(self):
        before = self.tree()
        self.env.pop("HERDR_ENV", None)
        self.env.pop("HERDR_BIN_PATH", None)
        code, out, err = self.run_cli("timers", "daily-check", "--dry-run", "--next", "2", "--json")
        self.assertEqual(code, 0, out + err)
        data = json.loads(out)
        self.assertEqual(data["timers"][0]["name"], "daily-check")
        self.assertEqual(len(data["timers"][0]["upcoming"]), 2)
        self.assertNotIn(BODY, out)
        self.assertEqual(self.tree(), before)
        self.assertEqual(self.calls(), [])

    def test_parser_rejects_timer_target_outside_declared_seats(self):
        self.write_fleet(self.base + timer_text(to="sample-contact"))
        with self.assertRaises(FleetError):
            load_fleet(os.path.join(self.repo, ".herdr", "fleet.toml"))

    def test_watch_once_queues_and_delivers_one_owner_request_with_metadata(self):
        code, out, err = self.run_cli("watch", "--once")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(len(self.prompts_to("sample-dev")), 1)
        delivered = inbox.read_box(self.repo, "sample-dev", inbox.CUR)
        self.assertEqual(len(delivered), 1)
        item = delivered[0]
        self.assertEqual((item.sender, item.fyi, item.mode, item.body), ("owner", False, "queue", BODY))
        self.assertEqual((item.timer, item.scheduled_for), ("daily-check", "2026-01-02T10:00:00+00:00"))
        self.assertIn("daily-check", self.prompts_to("sample-dev")[0])
        self.assertEqual([(e["from"], e["to"]) for e in messages.owed(log_entries(self.repo))],
                         [("owner", "sample-dev")])
        self.run_cli("watch", "--once")
        self.assertEqual(len(self.prompts_to("sample-dev")), 1)

    def test_prepared_occurrence_survives_enqueue_failure_and_retries_same_id(self):
        self.status_of("w2:p2", "working")
        with mock.patch.object(inbox, "write", side_effect=OSError("synthetic enqueue failure")):
            self.run_cli("watch", "--once")
        path = os.path.join(self.repo, ".herdr", "timers.state.json")
        self.assertTrue(os.path.isfile(path))
        with open(path, encoding="utf-8") as handle:
            before = handle.read()
        self.assertNotIn(BODY, before)
        self.run_cli("watch", "--once")
        queued = inbox.read_box(self.repo, "sample-dev", inbox.NEW)
        self.assertEqual(len(queued), 1)
        self.assertIn(queued[0].id, before)
        self.run_cli("watch", "--once")
        self.assertEqual(len(box(self.repo, "sample-dev", inbox.NEW)), 1)


class TimerConfig(TimerCase):
    def test_valid_defaults_and_unknown_keys(self):
        self.write_fleet(self.base + timer_text(extra="ignored"))
        fleet = load_fleet(os.path.join(self.repo, ".herdr", "fleet.toml"))
        timer = fleet.timers[0]
        self.assertEqual((timer.grace, timer.enabled, timer.to), (21600, True, "sample-dev"))
        self.assertTrue(any("unknown key 'extra'" in w for w in fleet.warnings))
        self.assertEqual(timer.file, os.path.join(self.repo, "timer.md"))
        self.assertEqual(timer.starts_at.utcoffset(), datetime.timedelta(0))

    def test_invalid_values_accumulate_and_names_cannot_escape(self):
        self.config(name="../bad", to="contact", at="24:00", tz="../UTC", enabled="true",
                    starts_at="2026-01-01T00:00:00", until="bad", grace="25h", file="missing.md")
        with self.assertRaises(FleetError) as raised:
            load_fleet(os.path.join(self.repo, ".herdr", "fleet.toml"))
        self.assertGreaterEqual(len(raised.exception.errors), 9)

    def test_strict_time_grace_enabled_and_aware_bounds(self):
        invalid = {"at": ["1:00", "01:60", "12:01:00", "12:01\n", 1201],
                   "grace": ["0h", "25h", "2d", 6, True], "enabled": ["false", 0, 1],
                   "starts_at": ["2026-01-01", "2026-01-01T01:00:00", "bad"],
                   "until": ["2025-01-01T00:00:00Z", "2026-01-01T00:00:00Z"]}
        for key, values in invalid.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    self.config(**{key: value})
                    with self.assertRaises(FleetError):
                        load_fleet(os.path.join(self.repo, ".herdr", "fleet.toml"))
        self.config(grace="1d", starts_at="2026-01-01T00:00:00Z")
        self.assertEqual(load_fleet(os.path.join(self.repo, ".herdr", "fleet.toml")).timers[0].grace, 86400)

    def test_duplicate_names_bad_table_and_missing_required_fields(self):
        self.write_fleet(self.base + timer_text() + timer_text())
        with self.assertRaises(FleetError):
            load_fleet(os.path.join(self.repo, ".herdr", "fleet.toml"))
        for raw in ('timers = "bad"\n' + self.base, self.base + '\n[[timers]]\nname="daily-check"\n'):
            self.write_fleet(raw)
            with self.assertRaises(FleetError):
                load_fleet(os.path.join(self.repo, ".herdr", "fleet.toml"))

    def test_empty_or_non_utf8_prompt_is_invalid_but_body_is_not_interpreted(self):
        for body in (b"", b" \n", b"\xff"):
            with open(os.path.join(self.repo, "timer.md"), "wb") as handle:
                handle.write(body)
            with self.assertRaises(FleetError):
                load_fleet(os.path.join(self.repo, ".herdr", "fleet.toml"))
        with open(os.path.join(self.repo, "timer.md"), "w") as handle:
            handle.write('[[timers]]\nname="text-is-data"\n')
        self.assertEqual(len(load_fleet(os.path.join(self.repo, ".herdr", "fleet.toml")).timers), 1)

    def test_public_names_next_empty_and_explicit_fleet(self):
        for args in (("missing",), ("--next", "0"), ("--next", "21")):
            self.assertEqual(self.run_cli("timers", "--dry-run", *args)[0], 2)
        path = os.path.join(self.repo, ".herdr", "fleet.toml")
        code, out, err = self.run_cli("timers", "--fleet", path, "--json", "--dry-run", "daily-check", "--next", "1")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(len(json.loads(out)["timers"][0]["upcoming"]), 1)
        self.write_fleet(self.base)
        self.assertIn("no timers", self.run_cli("timers", "--dry-run")[1])
        self.assertEqual(json.loads(self.run_cli("timers", "--dry-run", "--json")[1]), {"timers": []})

    def test_dry_run_ignores_action_context_even_on_errors(self):
        self.env["HERDR_PLUGIN_ACTION_ID"] = "synthetic-action"
        before = self.tree()
        self.assertEqual(self.run_cli("timers", "--dry-run")[0], 0)
        self.assertEqual(self.run_cli("timers", "--dry-run", "missing")[0], 2)
        self.assertEqual(self.tree(), before)
        self.assertEqual(self.calls(), [])

    def test_listing_uses_at_most_one_agent_list_and_never_writes(self):
        self.status_of("w2:p2", "working")
        self.run_cli("watch", "--once")
        before = self.tree()
        os.remove(self.log_path)
        code, out, err = self.run_cli("timers", "--json")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(json.loads(out)["timers"][0]["pending"]["state"], "working")
        self.assertEqual([c["argv"] for c in self.calls()], [["agent", "list"]])
        self.assertEqual(self.tree(), before)

    def test_future_start_months_ahead_and_exclusive_end_project_correctly(self):
        self.config(starts_at="2026-06-01T10:00:00Z", until="2026-06-04T10:00:00Z")
        data = json.loads(self.run_cli("timers", "--dry-run", "--json")[1])["timers"][0]
        self.assertEqual([r["utc"] for r in data["upcoming"]], [
            "2026-06-01T10:00:00+00:00", "2026-06-02T10:00:00+00:00", "2026-06-03T10:00:00+00:00"])
        self.config(starts_at="2026-06-01T09:00:00Z", until="2026-06-01T10:00:00Z")
        data = json.loads(self.run_cli("timers", "--dry-run", "--json")[1])["timers"][0]
        self.assertIsNone(data["next"])

    def test_listing_gracefully_handles_herdr_unavailable_without_writes(self):
        self.world["canned"].append({"prefix": ["agent", "list"], "exit": 1,
                                     "stderr": {"error": {"code": "unavailable", "message": "synthetic offline"}}})
        before = self.tree()
        self.assertEqual(self.run_cli("timers", "--json")[0], 0)
        self.assertEqual(self.tree(), before)

    def test_public_dry_run_warns_about_unknown_timer_keys(self):
        self.config(enabled_typo=False)
        code, out, err = self.run_cli("timers", "--dry-run", "--json")
        self.assertEqual(code, 0, out + err)
        self.assertIn("unknown key 'enabled_typo'", err)
        self.assertEqual(self.calls(), [])

    def test_public_dry_run_calendar_timeout_fails_cleanly_without_writes_or_calls(self):
        import subprocess
        before = self.tree()
        with mock.patch.object(timers, "CALENDAR", timer_calendar.Calendar("native")), \
                mock.patch.object(timer_calendar.subprocess, "run", side_effect=subprocess.TimeoutExpired("calendar", 15)):
            code, out, err = self.run_cli("timers", "--dry-run", "--json")
        self.assertEqual(code, 1, out + err)
        self.assertIn("calendar error", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.tree(), before)

    def test_inspection_preserves_newer_skipped_stopped_or_error_decision_after_delivery(self):
        for outcome in ("skipped", "stopped", "error"):
            with self.subTest(outcome=outcome):
                self.config(name="latest-" + outcome)
                self.assertEqual(self.run_cli("watch", "--once")[0], 0)
                instant = NOW + datetime.timedelta(days=1, hours=6) if outcome == "skipped" else (
                    datetime.datetime(2026, 1, 10, tzinfo=UTC) if outcome == "stopped" else NOW + datetime.timedelta(days=1))
                with mock.patch.object(inbox, "now_utc", return_value=instant):
                    if outcome == "error":
                        with mock.patch.object(timers.CALENDAR, "window", side_effect=ValueError("synthetic calendar failure")):
                            self.assertEqual(self.run_cli("watch", "--once")[0], 1)
                    else:
                        self.assertEqual(self.run_cli("watch", "--once")[0], 0)
                    row = json.loads(self.run_cli("timers", "--json")[1])["timers"][0]
                self.assertEqual(row["last"]["outcome"], outcome)
                self.assertEqual(row["last"]["at"], timers.iso(instant))


class CalendarTests(unittest.TestCase):
    def test_same_calendar_interface_for_fold_gap_and_non_hour_zone(self):
        cases = [("Australia/Sydney", "02:30", "2026-04-05", "2026-04-04T15:30:00+00:00"),
                 ("Australia/Sydney", "02:30", "2026-10-04", None),
                 ("Asia/Kathmandu", "10:00", "2026-01-02", "2026-01-02T04:15:00+00:00")]
        for backend in ("zoneinfo", "native"):
            calendar = timer_calendar.Calendar(backend)
            for zone, at, date, expected in cases:
                with self.subTest(backend=backend, zone=zone, date=date):
                    anchor = datetime.datetime.fromisoformat(date).replace(tzinfo=UTC)
                    row = next(r for r in calendar.window(zone, at, anchor) if r["date"] == date)
                    self.assertEqual(row["utc"], expected)

    def test_native_fallback_batches_and_caches_without_global_tz_mutation(self):
        old_tz, local = os.environ.get("TZ"), time.localtime(0)
        real = timer_calendar.subprocess.run
        with mock.patch.object(timer_calendar, "ZoneInfo", None), \
                mock.patch.object(time, "tzset", side_effect=AssertionError("parent timezone mutation")), \
                mock.patch.object(timer_calendar.subprocess, "run", wraps=real) as child:
            calendar = timer_calendar.Calendar()
            self.assertEqual(calendar.backend, "native")
            for second in range(100):
                rows = calendar.window("Australia/Sydney", "02:30", NOW + datetime.timedelta(seconds=second))
                self.assertEqual(len(rows), 64)
            self.assertEqual(child.call_count, 1)
            for zone in ("../UTC", "/etc/passwd", "UTC;bad", "Unavailable/Example"):
                self.assertFalse(timer_calendar.valid_zone(zone))
        self.assertEqual((os.environ.get("TZ"), time.localtime(0)), (old_tz, local))

    def test_unavailable_native_zone_fails_instead_of_using_utc(self):
        calendar = timer_calendar.Calendar("native")
        with self.assertRaises(ValueError):
            calendar.window("Unavailable/Example", "10:00", NOW)

    def test_native_historical_fold_local_offset_matches_chosen_instant(self):
        cases = [("Europe/Moscow", "2014-10-26", "2014-10-25T21:30:00+00:00"),
                 ("Europe/Kyiv", "1990-07-01", "1990-06-30T21:30:00+00:00")]
        for backend in ("zoneinfo", "native"):
            for zone, date, expected in cases:
                with self.subTest(backend=backend, zone=zone):
                    row = next(r for r in timer_calendar.Calendar(backend).window(
                        zone, "01:30", datetime.datetime.fromisoformat(date).replace(tzinfo=UTC)) if r["date"] == date)
                    self.assertEqual(row["utc"], expected)
                    self.assertEqual(timers.aware(row["local"]), timers.aware(row["utc"]))

    def test_native_cache_retains_more_than_sixteen_active_schedules(self):
        calendar = timer_calendar.Calendar("native")
        schedules = [("UTC", "10:%02d" % minute) for minute in range(17)]
        real = timer_calendar.subprocess.run
        with mock.patch.object(timer_calendar.subprocess, "run", wraps=real) as children:
            for seconds in (0, 2):
                calendar.retain(schedules)
                for zone, at in schedules:
                    calendar.window(zone, at, NOW + datetime.timedelta(seconds=seconds))
            self.assertEqual(children.call_count, 17)
            calendar.retain(schedules[:1])
            self.assertEqual(len(calendar.cache), 1)

    def test_native_child_failure_is_a_calendar_error_not_utc_fallback(self):
        import subprocess
        with mock.patch.object(timer_calendar.subprocess, "run", side_effect=subprocess.TimeoutExpired("calendar", 15)):
            with self.assertRaisesRegex(ValueError, "calendar failed"):
                timer_calendar.Calendar("native").window("UTC", "10:00", NOW)


class TimerDecisions(TimerCase):
    def test_before_start_and_disabled_do_not_plan_or_enqueue(self):
        for changes in (dict(starts_at="2026-01-03T00:00:00Z"), dict(enabled=False)):
            self.config(**changes)
            with mock.patch.object(timers.CALENDAR, "window", side_effect=AssertionError("calendar before activation")):
                self.run_cli("watch", "--once")
            self.assertEqual(self.queued(), [])
            self.assertEqual(self.prompts_to("sample-dev"), [])

    def test_latest_catchup_only_and_backward_clock_does_not_replay(self):
        self.status_of("w2:p2", "working")
        self.run_cli("watch", "--once")
        first = self.queued()[0]
        self.assertEqual(first.scheduled_for, "2026-01-02T10:00:00+00:00")
        self.assertTrue(any(e.get("reason") == "older missed interval" for e in log_entries(self.repo)))
        with mock.patch.object(inbox, "now_utc", return_value=NOW - datetime.timedelta(days=1)):
            self.run_cli("watch", "--once")
        self.assertEqual([m.id for m in self.queued()], [first.id])
        with mock.patch.object(inbox, "now_utc", return_value=NOW + datetime.timedelta(days=3)):
            self.run_cli("watch", "--once")
        self.assertEqual(len(self.queued()), 1)
        self.assertEqual(self.queued()[0].scheduled_for, "2026-01-05T10:00:00+00:00")
        self.assertEqual(len(inbox.read_box(self.repo, "sample-dev", inbox.CUR)), 1)

    def test_expired_catchup_is_skipped_once_without_backlog(self):
        late = NOW.replace(hour=16, minute=0)
        with mock.patch.object(inbox, "now_utc", return_value=late):
            self.run_cli("watch", "--once")
            first = self.tree()
            self.run_cli("watch", "--once")
        self.assertEqual(self.queued(), [])
        self.assertEqual(len(self.prompts_to("sample-dev")), 0)
        skips = [e for e in log_entries(self.repo) if e.get("reason") == "outside grace"]
        self.assertEqual(len(skips), 1)
        self.assertEqual(skips[0]["scheduled_for"], "2026-01-02T10:00:00+00:00")
        self.assertEqual(first, self.tree())

    def test_inclusive_start_exclusive_cutoff_caps_ttl_and_stops_once(self):
        self.status_of("w2:p2", "working")
        self.config(starts_at="2026-01-02T10:00:00Z", until="2026-01-02T10:10:00Z")
        self.run_cli("watch", "--once")
        message = self.queued()[0]
        self.assertEqual(message.ttl, "2026-01-02T10:10:00+00:00")
        with mock.patch.object(inbox, "now_utc", return_value=NOW.replace(minute=10)):
            self.run_cli("watch", "--once")
            first = self.tree()
            self.run_cli("watch", "--once")
        self.assertEqual(self.queued(), [])
        self.assertEqual(first, self.tree())
        self.assertEqual(len([e for e in log_entries(self.repo) if e.get("outcome") == "stopped"]), 1)
        self.assertFalse(self.prompts_to("sample-dev"))

    def test_pending_expiry_while_blocked_working_unknown_absent_or_typing(self):
        for status in ("blocked", "working", "unknown", "absent", "typing"):
            with self.subTest(status=status):
                self.config(name="check-" + status)
                if status == "typing":
                    if self.world["panes"]["w2:p2"]["agent"] is None:
                        fake_herdr.put_agent(self.world, "w2:p2", "claude", name="sample-dev", status="idle")
                    self.status_of("w2:p2", "idle")
                    fake_herdr.type_in(self.world, "w2:p2", "synthetic owner input")
                elif status == "absent":
                    self.world["panes"]["w2:p2"]["agent"] = None
                else:
                    if self.world["panes"]["w2:p2"]["agent"] is None:
                        fake_herdr.put_agent(self.world, "w2:p2", "claude", name="sample-dev", status=status)
                    fake_herdr.type_in(self.world, "w2:p2", "")
                    self.status_of("w2:p2", status)
                code, out, err = self.run_cli("watch", "--once")
                self.assertEqual(code, 0, out + err)
                self.assertIn("timer check-%s waiting: %s" % (status, status), out)
                self.assertEqual(len(self.queued()), 1)
                with mock.patch.object(inbox, "now_utc", return_value=NOW.replace(hour=16, minute=0)):
                    self.run_cli("watch", "--once")
                self.assertFalse(self.queued())
                expired = [e for e in log_entries(self.repo) if e.get("timer") == "check-" + status
                           and e.get("kind") == "expired"]
                self.assertEqual(len(expired), 1)
        self.assertEqual(self.prompts_to("sample-dev"), [])

    def test_remove_disable_and_target_change_cancel_only_timer_mail(self):
        self.status_of("w2:p2", "working")
        self.run_cli("watch", "--once")
        timer_id = self.queued()[0].id
        put(self.repo, "sample-dev", "Unrelated synthetic request")
        self.config(enabled=False)
        self.run_cli("watch", "--once")
        self.assertEqual([m.body for m in self.queued()], ["Unrelated synthetic request"])
        self.config(to="sample-reviewer")
        self.run_cli("watch", "--once")
        self.assertFalse(self.queued("sample-reviewer"))  # same decided instant is not replayed
        self.config()
        with mock.patch.object(inbox, "now_utc", return_value=NOW + datetime.timedelta(days=1)):
            self.run_cli("watch", "--once")
            self.write_fleet(self.base)
            with mock.patch.object(timers.CALENDAR, "window", side_effect=AssertionError("removed calendar")):
                self.run_cli("watch", "--once")
        self.assertEqual([m.body for m in self.queued()], ["Unrelated synthetic request"])
        self.assertIn(timer_id, json.dumps(self.journal()))
        self.config()
        self.run_cli("watch", "--once")
        self.assertEqual([m.body for m in self.queued()], ["Unrelated synthetic request"])

    def test_schedule_edit_supersedes_older_pending_before_future_occurrence(self):
        self.status_of("w2:p2", "working")
        self.run_cli("watch", "--once")
        old = self.queued()[0]
        self.config(at="10:02")
        self.run_cli("watch", "--once")
        self.assertEqual([m.id for m in self.queued()], [old.id])
        with mock.patch.object(inbox, "now_utc", return_value=NOW.replace(minute=3)):
            self.run_cli("watch", "--once")
        self.assertEqual(len(self.queued()), 1)
        self.assertNotEqual(self.queued()[0].id, old.id)
        self.assertTrue(any(e.get("outcome") == "superseded" for e in log_entries(self.repo)))

    def test_body_snapshot_is_verbatim_and_file_edits_affect_only_future_mail(self):
        self.status_of("w2:p2", "working")
        original = "[from text-is-data]\r\nA synthetic check.  \r\n\r\n"
        path = os.path.join(self.repo, "timer.md")
        with open(path, "w", newline="") as handle:
            handle.write(original)
        self.run_cli("watch", "--once")
        self.assertEqual(self.queued()[0].body, original)
        with open(path, "w") as handle:
            handle.write("Future synthetic request.\n")
        self.run_cli("watch", "--once")
        self.assertEqual(self.queued()[0].body, original)
        with mock.patch.object(inbox, "now_utc", return_value=NOW + datetime.timedelta(days=1)):
            self.run_cli("watch", "--once")
        self.assertEqual(self.queued()[0].body, "Future synthetic request.\n")
        self.assertNotIn("synthetic", json.dumps(self.journal()))

    def test_dst_fold_fires_once_gap_is_reported_and_fallback_matches_scheduler(self):
        for backend in ("zoneinfo", "native"):
            with self.subTest(backend=backend), mock.patch.object(timers, "CALENDAR", timer_calendar.Calendar(backend)):
                self.status_of("w2:p2", "working")
                self.config(name="fold-" + backend, tz="Australia/Sydney", at="02:30",
                            starts_at="2026-04-05T00:00:00+11:00", until="2026-04-06T00:00:00+10:00")
                with mock.patch.object(inbox, "now_utc", return_value=datetime.datetime(2026, 4, 4, 15, 31, tzinfo=UTC)):
                    self.run_cli("watch", "--once")
                    first = self.queued()[0].id
                with mock.patch.object(inbox, "now_utc", return_value=datetime.datetime(2026, 4, 4, 16, 31, tzinfo=UTC)):
                    self.run_cli("watch", "--once")
                self.assertEqual([m.id for m in self.queued()], [first])
                self.config(name="gap-" + backend, tz="Australia/Sydney", at="02:30",
                            starts_at="2026-10-04T00:00:00+10:00", until="2026-10-05T00:00:00+11:00")
                with mock.patch.object(inbox, "now_utc", return_value=datetime.datetime(2026, 10, 3, 16, 5, tzinfo=UTC)):
                    self.run_cli("watch", "--once")
                self.assertFalse(self.queued())
                self.assertTrue(any(e.get("timer") == "gap-" + backend and e.get("reason") == "daylight-saving gap"
                                    for e in log_entries(self.repo)))


class TimerRecovery(TimerCase):
    def setUp(self):
        super().setUp()
        self.status_of("w2:p2", "working")

    def ctx(self):
        return BoardCase.make_ctx(self)

    def schedule(self):
        with timers.cycle(self.ctx()):
            pass

    def test_recover_after_atomic_file_creation_does_not_resnapshot_or_duplicate(self):
        real = inbox.write
        def crash(*args, **kwargs):
            real(*args, **kwargs)
            raise OSError("synthetic crash after file")
        with mock.patch.object(inbox, "write", side_effect=crash):
            self.assertEqual(self.run_cli("watch", "--once")[0], 1)
        first = self.queued()[0]
        with open(os.path.join(self.repo, "timer.md"), "w") as handle:
            handle.write("Edited future body")
        self.run_cli("watch", "--once")
        self.assertEqual([(m.id, m.body) for m in self.queued()], [(first.id, BODY)])
        self.assertEqual(len([e for e in log_entries(self.repo) if e["kind"] == "send"]), 1)

    def test_recover_crash_after_log_append_before_journal_commit(self):
        class Crash(BaseException):
            pass
        real = messages.append
        def crash(repo, entry):
            real(repo, entry)
            if entry["kind"] == "send":
                raise Crash()
        with mock.patch.object(messages, "append", side_effect=crash):
            with self.assertRaises(Crash):
                self.schedule()
        chosen = self.queued()[0].id
        self.schedule()
        self.schedule()
        self.assertEqual([m.id for m in self.queued()], [chosen])
        self.assertEqual(len([e for e in log_entries(self.repo) if e["kind"] == "send"]), 1)

    def test_prepared_occurrence_expired_during_downtime_is_never_created(self):
        with mock.patch.object(inbox, "write", side_effect=OSError("synthetic write failure")):
            self.run_cli("watch", "--once")
        with mock.patch.object(inbox, "now_utc", return_value=NOW.replace(hour=16, minute=0)):
            self.run_cli("watch", "--once")
        self.assertFalse(self.queued())
        self.assertFalse([e for e in log_entries(self.repo) if e["kind"] == "send"])

    def test_collision_in_another_mailbox_fails_closed_without_replacing_file(self):
        with mock.patch.object(inbox, "write", side_effect=OSError("synthetic write failure")):
            self.run_cli("watch", "--once")
        record = next(iter(self.journal()["timers"]["daily-check"]["occurrences"].values()))
        collision = inbox.Message(record["id"], "sample-reviewer", "sample-orchestrator", record["scheduled_for"], "Unrelated")
        inbox.write(self.repo, collision)
        code, out, err = self.run_cli("watch", "--once")
        self.assertEqual(code, 1, out + err)
        self.assertIn("collision", err)
        self.assertEqual(inbox.load(collision.path, inbox.NEW).body, "Unrelated")
        self.assertFalse(self.queued())
        self.assertFalse(self.prompts_to("sample-dev"))

    def test_corrupt_or_incompatible_journal_is_preserved_and_blocks_delivery(self):
        path = timers.state_path(self.repo)
        for raw in ('{broken', '{"version":999,"timers":{}}', '{"version":1,"timers":{"daily-check":[]}}'):
            with open(path, "w") as handle:
                handle.write(raw)
            code, out, err = self.run_cli("watch", "--once")
            self.assertEqual(code, 1, out + err)
            self.assertIn("preserved", err)
            with open(path) as handle:
                self.assertEqual(handle.read(), raw)
        self.assertFalse(self.prompts_to("sample-dev"))

    def test_two_concurrent_schedulers_choose_one_id_file_and_logical_send(self):
        contexts = [self.ctx(), self.ctx()]
        barrier = threading.Barrier(2)
        errors = []
        def run(ctx):
            try:
                barrier.wait(timeout=3)
                with timers.cycle(ctx):
                    pass
            except BaseException as exc:
                errors.append(exc)
        threads = [threading.Thread(target=run, args=(ctx,)) for ctx in contexts]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive(), "timer lock deadlocked")
        self.assertEqual(errors, [])
        self.assertEqual(len(self.queued()), 1)
        self.assertEqual(len([e for e in log_entries(self.repo) if e["kind"] == "send"]), 1)

    def test_lock_order_is_timer_then_mailbox_including_watch_once_delivery(self):
        import contextlib
        local = threading.local()
        timer_lock, mailbox_lock = timers.locked, inbox.locked
        @contextlib.contextmanager
        def outer(repo):
            with timer_lock(repo):
                local.held = True
                try:
                    yield
                finally:
                    local.held = False
        @contextlib.contextmanager
        def inner(repo, name):
            self.assertTrue(getattr(local, "held", False), "mailbox acquired before timer")
            with mailbox_lock(repo, name):
                yield
        self.status_of("w2:p2", "idle")
        with mock.patch.object(timers, "locked", outer), mock.patch.object(inbox, "locked", inner):
            self.assertEqual(self.run_cli("watch", "--once")[0], 0)
        self.assertEqual(len(self.prompts_to("sample-dev")), 1)

    def test_recovery_from_cur_and_unconfirmed_never_reprompts(self):
        self.status_of("w2:p2", "idle")
        # The existing fake's canned timeout exercises an ambiguous prompt result.
        self.world["canned"].append({"prefix": ["agent", "prompt", "sample-dev"], "exit": 1,
                                     "stderr": {"error": {"code": "timeout", "message": "synthetic ambiguous prompt"}}})
        self.run_cli("watch", "--once")
        carried = inbox.read_box(self.repo, "sample-dev", inbox.CUR)
        self.assertEqual(len(carried), 1)
        self.run_cli("watch", "--once")
        self.assertEqual(len(self.argvs("agent", "prompt", "sample-dev")), 1)
        self.assertEqual(len([e for e in log_entries(self.repo) if e["kind"] == "send"]), 1)
        self.assertEqual(len(messages.owed(log_entries(self.repo))), 1)

    def test_missing_prompt_after_validation_is_visible_and_retries_same_occurrence(self):
        ctx = self.ctx()
        _ = ctx.fleet
        os.remove(os.path.join(self.repo, "timer.md"))
        with self.assertRaises(OpError):
            with timers.cycle(ctx):
                pass
        self.assertFalse(self.queued())
        self.assertTrue(any(e.get("outcome") == "error" for e in log_entries(self.repo)))
        with open(os.path.join(self.repo, "timer.md"), "w") as handle:
            handle.write(BODY)
        with timers.cycle(ctx):
            pass
        self.assertEqual(len(self.queued()), 1)

    def test_inconsistent_watermark_is_preserved_and_never_overwrites_occurrence(self):
        self.schedule()
        path = timers.state_path(self.repo)
        original = self.journal()
        for through in (None, "2026-01-01T10:00:00+00:00"):
            state = json.loads(json.dumps(original))
            history = state["timers"]["daily-check"]
            history["through"] = through
            next(iter(history["occurrences"].values()))["state"] = "settled"
            with open(path, "w") as handle:
                json.dump(state, handle)
            before = self.tree()
            code, out, err = self.run_cli("watch", "--once")
            self.assertEqual(code, 1, out + err)
            self.assertIn("watermark", err)
            self.assertEqual(self.tree(), before)

    def test_timer_identity_collision_checks_exact_header_id_without_body_reads(self):
        self.schedule()
        item = self.queued()[0]
        with open(item.path, "r+") as handle:
            text = handle.read().replace("id: " + item.id, "id: 20260102T100000Z-abcdef", 1)
            handle.seek(0)
            handle.write(text)
            handle.truncate()
        with mock.patch.object(inbox, "load", side_effect=AssertionError("scheduler read a mailbox body")), \
                mock.patch.object(inbox, "read_box", side_effect=AssertionError("scheduler read a mailbox body")):
            with self.assertRaisesRegex(OpError, "collision"):
                self.schedule()

    def test_scheduler_recovery_reads_headers_only_and_journal_contains_no_payload(self):
        self.schedule()
        with mock.patch.object(inbox, "load", side_effect=AssertionError("scheduler read a mailbox body")), \
                mock.patch.object(inbox, "read_box", side_effect=AssertionError("scheduler read a mailbox body")):
            self.schedule()
        self.assertNotIn("body_sha256", json.dumps(self.journal()))
        self.assertNotIn(BODY, json.dumps(self.journal()))

    def test_send_recovery_after_intervening_delivery_pull_unconfirmed_or_expiry(self):
        for via in ("deliver", "pull", "hook", "unconfirmed", "expired"):
            with self.subTest(via=via):
                self.config(name="recover-" + via)
                real = inbox.write
                def after_file(*args, **kwargs):
                    real(*args, **kwargs)
                    raise OSError("synthetic file/log crash")
                with mock.patch.object(inbox, "write", side_effect=after_file):
                    self.run_cli("watch", "--once")
                self.status_of("w2:p2", "idle")
                if via == "pull":
                    self.run_cli("inbox", "--as", "sample-dev", "--read")
                elif via == "hook":
                    self.run_cli("inbox", "--hook", "stop", env=dict(self.env, HERDR_PANE_ID="w2:p2"),
                                 stdin=json.dumps({"hook_event_name": "stop", "cwd": self.repo, "session_id": "synthetic"}))
                elif via == "expired":
                    with mock.patch.object(inbox, "now_utc", return_value=NOW.replace(hour=16, minute=0)):
                        self.run_cli("deliver", "sample-dev")
                else:
                    if via == "unconfirmed":
                        self.world["canned"].append({"prefix": ["agent", "prompt", "sample-dev"], "exit": 1,
                                                     "stderr": {"error": {"code": "timeout", "message": "synthetic"}}})
                    self.run_cli("deliver", "sample-dev")
                submitted = len(self.argvs("agent", "prompt", "sample-dev"))
                self.run_cli("watch", "--once")
                self.assertEqual(len(self.argvs("agent", "prompt", "sample-dev")), submitted)
                entries = [e for e in log_entries(self.repo) if e.get("timer") == "recover-" + via]
                self.assertEqual(len([e for e in entries if e["kind"] == "send"]), 1)
                self.assertEqual(len(messages.owed(entries)), 0 if via == "expired" else 1)
                listing = json.loads(self.run_cli("timers", "--json")[1])["timers"][0]
                self.assertEqual(listing["last"]["outcome"], "unconfirmed" if via == "unconfirmed" else
                                 "expired" if via == "expired" else "delivered")
                self.assertNotEqual(listing["state"], "error")
                self.status_of("w2:p2", "working")

    def test_atomic_journal_replace_failure_leaves_previous_bytes_and_no_partial_file(self):
        self.schedule()
        path = timers.state_path(self.repo)
        with open(path, "rb") as handle:
            before = handle.read()
        state = self.journal()
        state["timers"]["daily-check"]["last"] = {"outcome": "synthetic"}
        with mock.patch.object(timers.os, "replace", side_effect=OSError("synthetic replace failure")):
            with self.assertRaises(OSError):
                timers.save(self.repo, state)
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), before)
        self.assertFalse([n for n in os.listdir(os.path.dirname(path)) if n.startswith(".timers.")])

    def test_crash_after_decision_log_restores_original_last_time_without_duplicate(self):
        class Crash(BaseException):
            pass
        for outcome in ("skipped", "stopped", "error"):
            with self.subTest(outcome=outcome):
                path = os.path.join(self.repo, "timer.md")
                with open(path, "w") as handle:
                    handle.write(BODY)
                self.config(name="decision-" + outcome, starts_at="2026-01-02T10:00:00Z")
                ctx = self.ctx()
                _ = ctx.fleet
                instant = NOW.replace(hour=16, minute=0) if outcome == "skipped" else (
                    datetime.datetime(2026, 1, 10, tzinfo=UTC) if outcome == "stopped" else NOW)
                if outcome == "error":
                    os.remove(path)
                real = messages.append
                def crash(repo, entry):
                    real(repo, entry)
                    if entry.get("timer") == "decision-" + outcome and entry.get("outcome") == outcome:
                        raise Crash()
                with mock.patch.object(inbox, "now_utc", return_value=instant), \
                        mock.patch.object(messages, "append", side_effect=crash):
                    with self.assertRaises(Crash):
                        with timers.cycle(ctx):
                            pass
                later = instant + datetime.timedelta(minutes=1)
                with mock.patch.object(inbox, "now_utc", return_value=later):
                    if outcome == "error":
                        with self.assertRaises(OpError):
                            with timers.cycle(ctx):
                                pass
                    else:
                        with timers.cycle(ctx):
                            pass
                last = self.journal()["timers"]["decision-" + outcome]["last"]
                self.assertEqual((last["outcome"], last["at"]), (outcome, timers.iso(instant)))
                self.assertEqual(len([e for e in log_entries(self.repo) if e.get("timer") == "decision-" + outcome
                                      and e.get("outcome") == outcome]), 1)

    def test_recovered_send_does_not_answer_intervening_owner_debt_in_same_second(self):
        stamp = "2026-01-02T10:01:00+00:00"
        source = {"kind": "send", "from": "owner", "to": "sample-dev", "time": stamp,
                  "id": "20260102T100000Z-100001", "delivery": "queued", "timer": "daily-check",
                  "timer_enqueue_order": 0, "first_line": "Synthetic timer"}
        delivered = {"kind": "delivered", "from": "herdr-fleet", "to": "sample-dev", "time": stamp,
                     "id": source["id"], "delivery": "sent"}
        request = {"kind": "send", "from": "sample-dev", "to": "owner", "time": stamp,
                   "id": "20260102T100100Z-100002", "delivery": "queued", "first_line": "Synthetic owner review"}
        owner_delivery = {"kind": "delivered", "from": "herdr-fleet", "to": "owner", "time": stamp,
                          "id": request["id"], "delivery": "sent"}
        for response in ([], [{"kind": "ack", "from": "owner", "to": "sample-dev", "time": stamp,
                               "id": request["id"]}]):
            ordered = [source, delivered, request, owner_delivery] + response
            recovered = [delivered, request, owner_delivery, source] + response
            self.assertEqual(messages.owed(recovered), messages.owed(ordered))
            self.assertEqual(len(messages.owed(recovered)), 0 if response else 1)
            if not response:
                self.assertEqual((messages.owed(recovered)[0]["from"], messages.owed(recovered)[0]["to"]),
                                 ("sample-dev", "owner"))

    def test_recovered_send_retains_message_creation_time_and_enqueue_order(self):
        real = inbox.write
        def after_file(*args, **kwargs):
            real(*args, **kwargs)
            raise OSError("synthetic crash")
        with mock.patch.object(inbox, "write", side_effect=after_file):
            self.run_cli("watch", "--once")
        item = self.queued()[0]
        original_order = next(iter(self.journal()["timers"]["daily-check"]["occurrences"].values()))["send_after"]
        self.run_cli("inbox", "--as", "sample-dev", "--read")
        with mock.patch.object(inbox, "now_utc", return_value=NOW + datetime.timedelta(minutes=1)):
            self.run_cli("watch", "--once")
        source = next(e for e in log_entries(self.repo) if e["kind"] == "send")
        self.assertEqual(source["time"], item.time)
        self.assertEqual(source["timer_enqueue_order"], original_order)


class TimerWatchReload(TimerCase):
    def test_log_mode_cycles_reload_disable_remove_and_target_edits(self):
        self.status_of("w2:p2", "working")
        self.status_of("w2:p3", "working")
        self.behave(wait_results={"sample-dev": ["timeout"] * 20, "sample-reviewer": ["timeout"] * 20})
        put(self.repo, "sample-dev", "Unrelated synthetic mail")
        for action in ("disable", "remove", "target"):
            self.config(name="reload-" + action)
            passes = []
            def edit_after_first_cycle():
                passes.append(True)
                if len(passes) == 1:
                    before = os.stat(os.path.join(self.repo, ".herdr", "fleet.toml")).st_mtime_ns
                    if action == "remove":
                        self.write_fleet(self.base)
                    else:
                        self.config(name="reload-" + action, **({"enabled": False} if action == "disable" else
                                                               {"to": "sample-reviewer"}))
                    os.utime(os.path.join(self.repo, ".herdr", "fleet.toml"), ns=(before + 1000000,) * 2)
                    return False
                return True
            self.clock.until = edit_after_first_cycle
            code, out, err = self.watch("--log", "--debounce", "0", cycles=3)
            self.assertEqual(code, 0, out + err)
            self.assertIn("reloaded", out)
            self.assertEqual([m.body for m in self.queued()], ["Unrelated synthetic mail"])
            self.assertFalse(self.queued("sample-reviewer"))
        self.assertFalse(self.prompts_to("sample-dev"))

    def test_log_mode_invalid_edit_keeps_last_good_then_repairs(self):
        self.status_of("w2:p2", "working")
        self.behave(wait_results={"sample-dev": ["timeout"] * 8})
        passes = []
        def edit():
            passes.append(True)
            if len(passes) == 1:
                self.write_fleet("not valid TOML [")
            elif len(passes) == 2:
                self.config(enabled=False)
            return len(passes) == 3
        self.clock.until = edit
        code, out, err = self.watch("--log", cycles=4)
        self.assertEqual(code, 0, out + err)
        self.assertIn("last good", out)
        self.assertIn("reloaded", out)
        self.assertFalse(self.queued())

    def test_log_mode_real_waiter_delivers_one_batch_and_restart_deduplicates(self):
        self.behave(wait_results={"sample-dev": ["idle"]})
        code, out, err = self.watch("--log", "--debounce", "0", cycles=2)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(len(self.prompts_to("sample-dev")), 1)
        self.watch("--log", "--debounce", "0", cycles=2)
        self.assertEqual(len(self.prompts_to("sample-dev")), 1)
        entries = log_entries(self.repo)
        self.assertEqual(len([e for e in entries if e["kind"] == "send"]), 1)
        self.assertEqual(len([e for e in entries if e["kind"] == "delivered"]), 1)

    def test_history_free_fleet_uses_no_timer_lock_state_or_calendar(self):
        self.write_fleet(self.base)
        with mock.patch.object(timers, "locked", side_effect=AssertionError("no timer lock")), \
                mock.patch.object(timers.CALENDAR, "window", side_effect=AssertionError("no calendar")):
            self.run_cli("watch", "--once")
            self.watch("--log", cycles=2)
        self.assertFalse(os.path.exists(timers.state_path(self.repo)))
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".herdr", "timers.lock")))


class TimerDeliveryPaths(TimerCase):
    def test_expired_timer_cannot_be_revived_by_pull_hook_kickoff_or_send_flush(self):
        for path in ("pull", "hook", "kickoff", "send"):
            with self.subTest(path=path):
                self.config(name="expiry-" + path)
                self.status_of("w2:p2", "working")
                self.run_cli("watch", "--once")
                self.assertEqual(len(self.queued()), 1)
                self.status_of("w2:p2", "idle")
                with mock.patch.object(inbox, "now_utc", return_value=NOW.replace(hour=16, minute=0)):
                    if path == "pull":
                        code, out, err = self.run_cli("inbox", "--as", "sample-dev", "--read")
                    elif path == "hook":
                        code, out, err = self.run_cli("inbox", "--hook", "stop", env=dict(self.env, HERDR_PANE_ID="w2:p2"),
                            stdin=json.dumps({"hook_event_name": "stop", "cwd": self.repo, "session_id": "synthetic"}))
                    elif path == "kickoff":
                        self.write_fleet(self.read_fleet().replace('name = "sample-dev"',
                            'name = "sample-dev"\nkickoff_text = "Synthetic kickoff {inbox}"'))
                        code, out, err = self.run_cli("kickoff", "sample-dev")
                    else:
                        code, out, err = self.run_cli("send", "sample-dev", "A new synthetic request")
                self.assertEqual(code, 0, out + err)
                self.assertNotIn(BODY, out)
                self.assertFalse(self.queued())
        self.assertFalse(any(BODY in prompt for prompt in self.prompts_to("sample-dev")))
