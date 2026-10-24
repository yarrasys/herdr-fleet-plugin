"""Codex context integration and bounded, read-only synthetic rollout fixtures."""

from __future__ import annotations

import datetime
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from herdr_fleet import board, context_size
from test_section24 import BoardCase
from test_context import assistant, write_transcript

SID = "00000000-0000-4000-8000-000000000029"
STAMP = "2026-01-01T00:00:01Z"
SECRET = "SYNTHETIC-PRIVATE-CONTENT"


def agent(sid=SID, source="herdr:codex"):
    return {"agent_session": {"source": source, "value": sid}}


def event(tokens=123000, window=200000, when=STAMP):
    return {"type": "event_msg", "timestamp": when, "private": SECRET,
            "payload": {"type": "token_count", "prompt": SECRET,
                        "rate_limits": {"private": SECRET}, "info": {
                            "last_token_usage": {"input_tokens": 120000, "cached_input_tokens": 100000,
                                                 "output_tokens": 3000, "reasoning_output_tokens": 1000,
                                                 "total_tokens": tokens},
                            "total_token_usage": {"total_tokens": 9000000},
                            "model_context_window": window}}}


def rollout(root, records=None, sid=SID, day=True, suffix="00-00-00"):
    folder = os.path.join(root, "2026", "01", "01") if day else root
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "rollout-2026-01-01T%s-%s.jsonl" % (suffix, sid))
    with open(path, "w", encoding="utf-8") as handle:
        for record in records if records is not None else [event()]:
            handle.write(json.dumps(record) + "\n")
    return path


class SourceFixture:
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fleet-codex-context-")
        self.env = {"HOME": self.tmp}
        self.root = os.path.join(self.tmp, ".codex", "sessions")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def reading(self):
        return context_size.seat_context(agent(), self.env)


class CodexReader(SourceFixture, unittest.TestCase):
    def test_metadata_rejects_unsafe_ids_and_unsupported_sources_without_io(self):
        with mock.patch.object(context_size, "find_source", side_effect=AssertionError("discovery")):
            for sid in (None, 29, True, "", "../x", "a/b", "a\\b", "*", "a?", "[x]", "x\n", "x\0", "x" * 129):
                with self.subTest(sid=sid):
                    self.assertIsNone(context_size.seat_context(agent(sid), self.env))
            for metadata in (None, [], "session", {}, {"source": "herdr:codex"}):
                self.assertIsNone(context_size.seat_context({"agent_session": metadata}, self.env))
            for source in (None, "codex", "herdr:agy", "herdr:other"):
                self.assertIsNone(context_size.seat_context(agent(source=source), self.env))
            self.env["HERDR_FLEET_CODEX_SESSIONS"] = "bad\0root"
            self.assertIsNone(self.reading())

    def test_default_codex_home_and_override_precedence(self):
        rollout(self.root, [event(100)])
        self.assertEqual(self.reading()["tokens"], 100)
        custom_home = os.path.join(self.tmp, "custom-home")
        rollout(os.path.join(custom_home, "sessions"), [event(200)])
        self.env["CODEX_HOME"] = custom_home
        self.assertEqual(self.reading()["tokens"], 200)
        override = os.path.join(self.tmp, "override")
        rollout(override, [event(300)], day=False)
        self.env["HERDR_FLEET_CODEX_SESSIONS"] = override
        self.assertEqual(self.reading()["tokens"], 300)

    def test_tilde_roots_expand_using_supplied_home(self):
        rollout(os.path.join(self.tmp, "custom", "sessions"), [event(10)])
        self.env["CODEX_HOME"] = "~/custom"
        self.assertEqual(self.reading()["tokens"], 10)
        self.env["HERDR_FLEET_CODEX_SESSIONS"] = "~/custom/sessions"
        self.assertEqual(self.reading()["tokens"], 10)

    def test_invalid_timestamp_keeps_usage_without_breaking_display(self):
        for stamp in (None, {}, "bad date", "2026-99-01T00:00:01Z", "2026-01-01T00:00:01+99:00"):
            with self.subTest(stamp=stamp):
                rollout(self.root, [event(when=stamp)])
                self.assertEqual(context_size.context_label(self.reading()), "123k/200k")

    def test_unknown_provider_helpers_fail_closed_without_source_io(self):
        with mock.patch("builtins.open", side_effect=AssertionError("unsupported provider read")), \
                mock.patch.object(context_size.glob, "glob", side_effect=AssertionError("unsupported discovery")):
            self.assertIsNone(context_size.read_context("unknown", "synthetic.jsonl"))
            self.assertIsNone(context_size.find_source("unknown", self.root, SID))

    def test_root_glob_characters_are_literal_and_other_sessions_are_not_read(self):
        root = os.path.join(self.tmp, "sessions[1]*?")
        self.env["HERDR_FLEET_CODEX_SESSIONS"] = root
        path = rollout(root, [event(55)])
        rollout(root, [event(99)], sid="other-session", day=False)
        with mock.patch.object(context_size, "last_codex_usage", wraps=context_size.last_codex_usage) as read:
            self.assertEqual(self.reading()["tokens"], 55)
        read.assert_called_once_with(path)

    def test_duplicate_root_and_day_rollouts_choose_newest(self):
        old = rollout(self.root, [event(50)])
        new = rollout(self.root, [event(75)], day=False)
        os.utime(old, ns=(100, 100))
        os.utime(new, ns=(200, 200))
        self.assertEqual(self.reading()["tokens"], 75)
        os.utime(old, ns=(300, 300))
        self.assertEqual(self.reading()["tokens"], 50)

    def test_missing_empty_unreadable_and_wrong_provider_data(self):
        self.assertIsNone(self.reading())
        path = rollout(self.root, [])
        self.assertIsNone(self.reading())
        rollout(self.root, [assistant(1, 2, 3)])
        self.assertIsNone(self.reading())
        with mock.patch("builtins.open", side_effect=PermissionError("synthetic unreadable source")):
            self.assertIsNone(context_size.last_codex_usage(path))
        with mock.patch.object(context_size.os, "stat", side_effect=OSError("source disappeared")):
            self.assertIsNone(self.reading())

    def test_corrupt_partial_null_and_non_usage_events_are_skipped(self):
        records = [event(), {"type": "event_msg", "payload": {"type": "token_count", "info": None}},
                   {"type": "event_msg", "payload": {"type": "token_count", "info": {}}},
                   {"type": "response_item", "payload": event()["payload"]},
                   {"type": "event_msg", "payload": {"type": "other", "info": event()["payload"]["info"]}},
                   {"type": "event_msg", "payload": None}, [], None]
        path = rollout(self.root, records)
        with open(path, "ab") as handle:
            handle.write(b'\xff\n{corrupt}\n{"type":"event_msg","payload":{"type":"token_count"')
        self.assertEqual(self.reading()["tokens"], 123000)

    def test_invalid_usage_is_skipped_but_valid_zero_wins(self):
        for value in (None, True, False, -1, 123.5, "123000", {}, []):
            with self.subTest(value=value):
                rollout(self.root, [event(55), event(value)])
                self.assertEqual(self.reading()["tokens"], 55)
        rollout(self.root, [event(55), event(0, 100000)])
        self.assertEqual(self.reading()["tokens"], 0)
        self.assertEqual(context_size.context_label(self.reading(), datetime.datetime(
            2026, 1, 1, 0, 2, 1, tzinfo=datetime.timezone.utc)), "0/100k (2 min)")

    def test_missing_and_invalid_window_keep_usage_without_inference(self):
        for value in (None, True, False, 0, -1, 200000.5, "200000", {}, []):
            with self.subTest(value=value):
                rollout(self.root, [event(window=value)])
                reading = self.reading()
                self.assertEqual(set(reading), {"tokens", "at", "source"})
                self.assertEqual(reading["tokens"], 123000)
        record = event()
        del record["payload"]["info"]["model_context_window"]
        rollout(self.root, [record])
        self.assertNotIn("window_tokens", self.reading())

    def test_compaction_and_model_window_changes_choose_latest_not_largest(self):
        rollout(self.root, [event(), event(1200, 100000, "2026-01-01T00:00:02Z")])
        self.assertEqual(self.reading(), {"tokens": 1200, "window_tokens": 100000,
                                         "at": "2026-01-01T00:00:02Z", "source": "codex-transcript"})

    def test_parser_has_exact_public_whitelist_for_valid_json(self):
        record = event()
        record.update({"session_id": SID, "cwd": "/synthetic/private", "tool": SECRET})
        parsed = context_size.codex_usage_record(json.dumps(record).encode())
        self.assertEqual(parsed, {"tokens": 123000, "at": STAMP,
                                  "source": "codex-transcript", "window_tokens": 200000})
        for private in (SID, SECRET, "private", "cached_input_tokens", "rate_limits", "9000000"):
            self.assertNotIn(private, json.dumps(parsed))
        record["timestamp"] = {"content": SECRET}
        self.assertIsNone(context_size.codex_usage_record(json.dumps(record).encode())["at"])

    def test_backward_reader_completes_split_lines_and_handles_final_no_newline(self):
        path = rollout(self.root, [event(), {"type": "other", "content": "x" * 4000}])
        expected = self.reading()
        for chunk in (7, 64, 512, 4096):
            self.assertEqual(context_size.last_codex_usage(path, chunk=chunk), expected)
        path = rollout(self.root)
        with open(path, "rb") as handle:
            data = handle.read().rstrip(b"\n")
        with open(path, "wb") as handle:
            handle.write(data)
        self.assertEqual(context_size.last_codex_usage(path, chunk=7), expected)

    def test_default_backward_read_has_four_mib_bound_and_256_kib_chunks(self):
        path = rollout(self.root)
        with open(path, "ab") as handle:
            handle.write(b"x" * (context_size.LIMIT + context_size.CHUNK))
        reads = []
        real_open = open

        class Meter:
            def __enter__(self):
                self.handle = real_open(path, "rb")
                return self

            def __exit__(self, *args):
                self.handle.close()

            def seek(self, *args):
                return self.handle.seek(*args)

            def tell(self):
                return self.handle.tell()

            def read(self, size):
                reads.append(size)
                return self.handle.read(size)

        with mock.patch("builtins.open", return_value=Meter()):
            self.assertIsNone(context_size.last_codex_usage(path))
        self.assertEqual(sum(reads), 4 * 1024 * 1024)
        self.assertEqual(max(reads), 256 * 1024)
        self.assertIsNotNone(context_size.last_codex_usage(path, limit=os.path.getsize(path)))

    def test_io_failure_mid_read_returns_none(self):
        handle = mock.MagicMock()
        handle.__enter__.return_value = handle
        handle.tell.return_value = 100
        handle.read.side_effect = OSError("synthetic read failure")
        with mock.patch("builtins.open", return_value=handle):
            self.assertIsNone(context_size.last_codex_usage("synthetic.jsonl"))


class CodexCache(SourceFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.cache = board.Cache()

    def test_provider_root_and_session_are_all_cache_keys(self):
        rollout(self.root, [event(10)])
        first = self.cache.context(agent(), self.env)
        self.assertEqual(first["tokens"], 10)
        claude = os.path.join(self.tmp, ".claude", "projects")
        write_transcript(os.path.join(claude, "project"), SID, [assistant(1, 2, 3)])
        self.assertEqual(self.cache.context(agent(source="herdr:claude"), self.env), {
            "tokens": 6, "at": "2026-10-20T22:40:52.536Z", "source": "claude-transcript"})
        other = os.path.join(self.tmp, "other")
        rollout(other, [event(20)])
        env = dict(self.env, HERDR_FLEET_CODEX_SESSIONS=other)
        self.assertEqual(self.cache.context(agent(), env)["tokens"], 20)
        rollout(other, [event(30)], sid="another-session")
        self.assertEqual(self.cache.context(agent("another-session"), env)["tokens"], 30)
        self.assertEqual(len(self.cache.contexts), 4)
        self.assertEqual(self.cache.context(agent(), self.env), first)

    def test_unchanged_file_cached_then_compaction_and_size_change_reread(self):
        path = rollout(self.root)
        with mock.patch.object(context_size, "last_codex_usage", wraps=context_size.last_codex_usage) as read, \
                mock.patch.object(context_size, "find_codex_transcript", wraps=context_size.find_codex_transcript) as find:
            self.assertEqual(self.cache.context(agent(), self.env)["tokens"], 123000)
            self.assertEqual(self.cache.context(agent(), self.env)["tokens"], 123000)
            self.assertEqual((read.call_count, find.call_count), (1, 1))
            old = os.stat(path)
            rollout(self.root, [event(0, 100000)])
            os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))  # changed size alone must invalidate
            self.assertEqual(self.cache.context(agent(), self.env)["tokens"], 0)
            self.assertEqual(self.cache.context(agent(), self.env)["window_tokens"], 100000)
            self.assertEqual((read.call_count, find.call_count), (2, 1))
            rollout(self.root, [event(5, 100000)])  # same size, new mtime
            os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns + 1000000))
            self.assertEqual(self.cache.context(agent(), self.env)["tokens"], 5)
            self.assertEqual((read.call_count, find.call_count), (3, 1))

    def test_reuses_successful_path_until_missing_then_relooks_up(self):
        old = rollout(self.root, [event(10)])
        self.assertEqual(self.cache.context(agent(), self.env)["tokens"], 10)
        new = rollout(self.root, [event(20)], day=False)
        os.utime(new, ns=(os.stat(old).st_mtime_ns + 1000000,) * 2)
        with mock.patch.object(context_size, "find_codex_transcript", wraps=context_size.find_codex_transcript) as find:
            self.assertEqual(self.cache.context(agent(), self.env)["tokens"], 10)
            find.assert_not_called()
            os.remove(old)
            self.assertEqual(self.cache.context(agent(), self.env)["tokens"], 20)
            find.assert_called_once()

    def test_missing_file_retry_and_unavailable_data_is_not_cached(self):
        self.assertIsNone(self.cache.context(agent(), self.env))
        rollout(self.root, [])
        with mock.patch.object(context_size, "last_codex_usage", wraps=context_size.last_codex_usage) as read:
            self.assertIsNone(self.cache.context(agent(), self.env))
            self.assertIsNone(self.cache.context(agent(), self.env))
            self.assertEqual(read.call_count, 2)
            rollout(self.root, [event()])
            self.assertEqual(self.cache.context(agent(), self.env)["tokens"], 123000)
            self.assertEqual(read.call_count, 3)

    def test_transient_read_failure_retries_unchanged_source_for_both_providers(self):
        rollout(self.root)
        write_transcript(os.path.join(self.tmp, ".claude", "projects", "project"), SID, [assistant(1, 2, 3)])
        real_read = context_size.read_context
        for source in ("herdr:codex", "herdr:claude"):
            with self.subTest(source=source):
                seat = agent(source=source)
                key = context_size.resolve(seat, self.env)
                provider, root, sid = key
                path = context_size.find_source(provider, root, sid)
                before = os.stat(path)
                expected = real_read(provider, path)
                cache = board.Cache()
                with mock.patch.object(context_size, "read_context", side_effect=[None, expected]) as read:
                    self.assertIsNone(cache.context(seat, self.env))
                    self.assertNotIn(key, cache.contexts)
                    self.assertEqual(cache.context(seat, self.env), expected)
                    self.assertEqual(read.call_count, 2)
                after = os.stat(path)
                self.assertEqual((before.st_mtime_ns, before.st_size), (after.st_mtime_ns, after.st_size))
                with open(path, "ab") as handle:
                    handle.write(b"\n")  # invalidate an earlier successful reading
                before = os.stat(path)
                with mock.patch.object(context_size, "read_context", side_effect=[None, expected]):
                    self.assertIsNone(cache.context(seat, self.env))
                    self.assertNotIn(key, cache.contexts)
                    self.assertEqual(cache.context(seat, self.env), expected)
                after = os.stat(path)
                self.assertEqual((before.st_mtime_ns, before.st_size), (after.st_mtime_ns, after.st_size))


class CodexIntegration(BoardCase):
    def setUp(self):
        super().setUp()
        self.root = os.path.join(self.tmp, ".codex", "sessions")
        self.path = rollout(self.root)
        self.world["panes"]["w2:p3"]["agent"].update(agent())

    def test_codex_status_json_text_threshold_and_privacy(self):
        self.write_fleet(self.read_fleet().replace("450000", "123000"))
        code, out, err = self.run_cli("status", "--json", "--context")
        self.assertEqual(code, 0, out + err)
        seat = json.loads(out)["seats"][2]
        self.assertEqual(seat["context"], {"tokens": 123000, "at": STAMP,
                                          "source": "codex-transcript", "window_tokens": 200000})
        self.assertTrue(seat["restart_hint"])
        text = self.run_cli("status", "--context")[1]
        self.assertIn("123k/200k (", text)
        self.assertIn("consider restart", text)
        for private in (SECRET, SID, self.path, "9000000", "rate_limits"):
            self.assertNotIn(private, out + text + err)

    def test_codex_board_real_cache_model_render_and_seat_header(self):
        state = board.WatcherState(cache=board.Cache())
        model = self.build(state)
        row = model.rows[2]
        self.assertEqual(row.tokens, 123000)
        self.assertEqual(row.window_tokens, 200000)
        self.assertEqual(row.context_at, STAMP)
        self.assertFalse(row.over)
        self.assertIn("ctx 123k/200k", "\n".join(board.plain_text(board.render(model, 162, 6, True))))
        page = board.SeatPage(row.name)
        page.load(self.make_ctx(), model, state.cache, state)
        self.assertIn("ctx 123k/200k", page.title())
        for private in (SECRET, SID, self.path, "9000000", "rate_limits"):
            self.assertNotIn(private, repr(model) + page.title())

    def test_codex_reader_uses_last_not_lifetime_or_double_counted_tokens(self):
        self.assertEqual(context_size.seat_context(agent(), self.env), {
            "tokens": 123000, "at": STAMP, "source": "codex-transcript", "window_tokens": 200000})

    def test_status_without_flag_does_not_resolve_or_read_either_provider(self):
        with mock.patch.object(context_size, "resolve", side_effect=AssertionError("transcript access")):
            self.assertEqual(self.run_cli("status")[0], 0)
            data = json.loads(self.run_cli("status", "--json")[1])
        self.assertNotIn("context", data["seats"][2])

    def test_context_reading_adds_no_herdr_calls_and_does_not_write_sources(self):
        claude = write_transcript(os.path.join(self.projects, "project"), SID, [assistant(1, 2, 3)])
        self.world["panes"]["w2:p1"]["agent"].update(agent(source="herdr:claude"))

        def snapshot():
            result = {}
            for path in (self.path, claude):
                with open(path, "rb") as handle:
                    result[path] = (handle.read(), os.stat(path).st_mtime_ns)
            return result

        before = snapshot()
        self.run_cli("status")  # establish pane labels before comparing calls
        os.remove(self.log_path)
        self.run_cli("status")
        plain = [item["argv"] for item in self.calls()]
        os.remove(self.log_path)
        self.run_cli("status", "--context")
        self.assertEqual([item["argv"] for item in self.calls()], plain)
        self.build(board.WatcherState(cache=board.Cache()))
        self.assertEqual(snapshot(), before)

    def test_board_cache_refreshes_compacted_usage_window_zero_and_missing_data(self):
        state = board.WatcherState(cache=board.Cache())
        with mock.patch.object(context_size, "last_codex_usage", wraps=context_size.last_codex_usage) as read:
            self.build(state)
            self.build(state)
            self.assertEqual(read.call_count, 1)
            rollout(self.root, [event(0, 100000)])
            row = self.build(state).rows[2]
            self.assertEqual((row.tokens, row.window_tokens, row.over), (0, 100000, False))
            self.assertEqual(board.context_text(row), "ctx 0/100k")
            self.assertEqual(read.call_count, 2)
            os.remove(self.path)
            row = self.build(state).rows[2]
            self.assertEqual((row.tokens, row.window_tokens, row.over), (None, None, False))

    def test_threshold_depends_only_on_used_tokens_not_window_or_lifetime(self):
        rollout(self.root, [event(123000, 100000)])  # above window, below configured restart threshold
        data = json.loads(self.run_cli("status", "--json", "--context")[1])
        self.assertFalse(data["seats"][2]["restart_hint"])
        self.assertFalse(self.build().rows[2].over)
        self.write_fleet(self.read_fleet().replace("450000", "123000"))
        self.assertTrue(self.build().rows[2].over)
        rollout(self.root, [event(0, None)])
        data = json.loads(self.run_cli("status", "--json", "--context")[1])
        self.assertEqual(data["seats"][2]["context"]["tokens"], 0)
        self.assertFalse(data["seats"][2]["restart_hint"])


class ContextLayout(unittest.TestCase):
    def model(self):
        return board.Board("sample", rows=[
            board.Row("sample-dev", "sample-dev", "working", tokens=183000),
            board.Row("sample-reviewer", "sample-reviewer", "idle", tokens=123000,
                      window_tokens=200000, over=True)])

    def test_visible_window_reserves_sixteen_columns_for_every_normal_row(self):
        model = self.model()
        for width in (60, 89, 90, 119, 120, 162):
            with self.subTest(width=width):
                lines = board.render(model, width, 4, True)
                text = board.plain_text(lines)
                self.assertEqual(text[1].index("ctx 183k"), width - 16)
                self.assertEqual(text[2].index("ctx 123k/200k"), width - 16)
                self.assertTrue(text[2].endswith("ctx 123k/200k " + board.WARN))
                self.assertTrue(all(sum(board.width_of(t) for t, _ in line) == width for line in lines))

    def test_no_visible_window_keeps_ten_columns_and_hidden_window_does_not_widen(self):
        model = self.model()
        # At 119 columns and 3 lines, urgency chooses only the working Claude row.
        mode, rows, hidden = board.plan(model, 119, 3)
        self.assertEqual((mode, [r.name for r in rows], hidden), ("rows", ["sample-dev"], 1))
        self.assertEqual(board.plain_text(board.render(model, 119, 3, False))[1].index("ctx 183k"), 109)
        model.rows[1].window_tokens = None
        lines = board.plain_text(board.render(model, 162, 4, False))
        self.assertEqual(lines[1].index("ctx 183k"), 152)
        self.assertEqual(lines[2].index("ctx 123k"), 152)

    def test_packed_narrow_and_single_keep_selection_and_navigation_positions(self):
        model = self.model()
        for width, height in ((162, 3), (59, 4), (59, 2), (162, 2)):
            with self.subTest(width=width, height=height):
                mode, rows, hidden = board.plan(model, width, height)
                self.assertEqual([r.name for r in rows], [r.name for r in board.visible(model, width, height)])
                self.assertEqual(len(board.render(model, width, height, False)), 1 if mode == "single" else height)
                if mode == "packed" or width < 60:
                    self.assertNotIn("ctx ", "\n".join(board.plain_text(board.render(model, width, height, False))))
                else:
                    self.assertIn("ctx 183k", board.plain_text(board.render(model, width, height, False))[0])


if __name__ == "__main__":
    import unittest
    unittest.main()
