"""status --context (design 17.4): the transcript reader, its boundary
(invariant 9), the CONTEXT column and restart_hint_tokens.

Transcripts are small fake ``.jsonl`` files under the test's temporary HOME
(or HERDR_FLEET_CLAUDE_PROJECTS); the real ``~/.claude`` is never read."""

from __future__ import annotations

import datetime
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import fake_herdr
from support import LiveFleet
from herdr_fleet import context_size

SECRET = "TOP-SECRET-PROMPT-TEXT"
TOOL = "rm -rf /secret/tool/call"
ORCH_SESSION = "00000000-0000-4000-8000-000000000001"
DEV_SESSION = "00000000-0000-4000-8000-000000000002"


def assistant(inp, create, read, out=342, when="2026-10-20T22:40:52.536Z", text=SECRET):
    return {"type": "assistant", "timestamp": when, "sessionId": "x", "cwd": "/secret/cwd",
            "message": {"role": "assistant", "model": "claude-opus-5-5",
                        "content": [{"type": "text", "text": text},
                                    {"type": "tool_use", "name": "Bash", "input": {"command": TOOL}}],
                        "usage": {"input_tokens": inp, "cache_creation_input_tokens": create,
                                  "cache_read_input_tokens": read, "output_tokens": out,
                                  "service_tier": "standard"}}}


def user(text=SECRET):
    return {"type": "user", "timestamp": "2026-10-20T22:41:00.000Z", "message": {"role": "user", "content": text}}


def write_transcript(folder, session, lines, partial=True):
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, session + ".jsonl")
    with open(path, "w", encoding="utf-8") as handle:
        for line in lines:
            handle.write(json.dumps(line) + "\n")
        if partial:  # a turn being written as we read
            handle.write('{"type": "assistant", "timestamp": "2026-10-20T22:59:00Z", "message": {"usage": {"inp')
    return path


class Reader(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="herdr-fleet-ctx-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_last_assistant_line_with_usage_wins(self):
        path = write_transcript(self.tmp, "s1", [
            assistant(2, 100, 1000, when="2026-10-20T22:00:00.000Z"),
            assistant(2, 5375, 177630),
            user(),
            {"type": "attachment", "timestamp": "2026-10-20T22:42:00Z"},
            {"type": "assistant", "timestamp": "2026-10-20T22:43:00Z", "message": {"content": "no usage"}},
            {"type": "custom-title", "title": SECRET}])
        record = context_size.last_usage(path)
        self.assertEqual(record["timestamp"], "2026-10-20T22:40:52.536Z")
        self.assertEqual(context_size.context_tokens(record), 2 + 5375 + 177630)
        self.assertEqual(record["output_tokens"], 342)

    def test_the_parsed_record_carries_nothing_else(self):
        raw = json.dumps(assistant(2, 5375, 177630)).encode()
        record = context_size.usage_record(raw)
        self.assertEqual(set(record), set(context_size.RECORD_KEYS))
        self.assertEqual(set(record), {"type", "timestamp", "input_tokens", "cache_creation_input_tokens",
                                       "cache_read_input_tokens", "output_tokens"})
        for leaked in (SECRET, TOOL, "/secret/cwd", "claude-opus-5-5", "standard"):
            self.assertNotIn(leaked, repr(record))
        self.assertIsNone(context_size.usage_record(json.dumps(user()).encode()))
        self.assertIsNone(context_size.usage_record(b'{"type": "assistant", "message": {"usage": {"inp'))

    def test_a_synthetic_line_with_no_input_counts_is_skipped(self):
        path = write_transcript(self.tmp, "s1", [assistant(2, 10, 1000), assistant(0, 0, 0, out=0)])
        self.assertEqual(context_size.context_tokens(context_size.last_usage(path)), 1012)

    def test_chunked_backward_read(self):
        filler = [user("y" * 700) for _ in range(40)]  # lines longer than the chunk
        path = write_transcript(self.tmp, "s1", [assistant(1, 2, 3, when="2026-10-20T21:00:00Z"),
                                                 assistant(5, 6, 7)] + filler)
        whole = context_size.last_usage(path)
        for chunk in (16, 64, 100, 1000, 4096):
            self.assertEqual(context_size.last_usage(path, chunk=chunk), whole, chunk)
        self.assertEqual(context_size.context_tokens(whole), 18)
        # The limit is honoured: the usage line sits more than 2 KB from the end.
        self.assertIsNone(context_size.last_usage(path, chunk=512, limit=2048))
        self.assertIsNotNone(context_size.last_usage(path, chunk=512, limit=os.path.getsize(path)))

    def test_first_line_of_the_file_is_read_too(self):
        path = write_transcript(self.tmp, "s1", [assistant(1, 1, 1)], partial=False)
        self.assertEqual(context_size.context_tokens(context_size.last_usage(path, chunk=7)), 3)
        with open(path, "rb") as handle:
            data = handle.read().rstrip(b"\n")
        with open(path, "wb") as handle:  # a complete last line without its newline
            handle.write(data)
        self.assertEqual(context_size.context_tokens(context_size.last_usage(path, chunk=7)), 3)

    def test_missing_or_empty_file(self):
        self.assertIsNone(context_size.last_usage(os.path.join(self.tmp, "nope.jsonl")))
        path = write_transcript(self.tmp, "s1", [user()], partial=False)
        self.assertIsNone(context_size.last_usage(path))
        open(path, "w").close()
        self.assertIsNone(context_size.last_usage(path))

    def test_session_ids_and_transcript_lookup(self):
        agent = {"agent_session": fake_herdr.claude_session(ORCH_SESSION)}
        self.assertEqual(context_size.session_id(agent), ORCH_SESSION)
        for other in (fake_herdr.claude_session("x", "codex"), fake_herdr.claude_session("x", "agy"), None, "x"):
            self.assertIsNone(context_size.session_id({"agent_session": other}))
        self.assertIsNone(context_size.session_id({}))
        for bad in ("../../etc/passwd", "*", "a/b", ""):
            self.assertIsNone(context_size.session_id({"agent_session": fake_herdr.claude_session(bad)}))
        write_transcript(os.path.join(self.tmp, "-Users-me-work-x"), ORCH_SESSION, [assistant(1, 1, 1)])
        self.assertTrue(context_size.find_transcript(self.tmp, ORCH_SESSION).endswith(ORCH_SESSION + ".jsonl"))
        self.assertIsNone(context_size.find_transcript(self.tmp, DEV_SESSION))

    def test_labels(self):
        self.assertEqual([context_size.tokens_label(n) for n in (850, 183005, 999499, 1234567)],
                         ["850", "183k", "999k", "1.2M"])
        now = datetime.datetime(2026, 10, 20, 22, 42, 52, tzinfo=datetime.timezone.utc)
        info = {"tokens": 183007, "at": "2026-10-20T22:40:52.536Z"}
        self.assertEqual(context_size.context_label(info, now), "183k (1 min)")
        self.assertEqual(context_size.context_label({"tokens": 5, "at": None}, now), "5")
        self.assertEqual(context_size.context_label(None, now), "-")
        self.assertEqual([context_size.age_label(s) for s in (59, 180, 7200, 5 * 86400, 63 * 86400, -5, None)],
                         ["0 min", "3 min", "2 h", "5 d", "9 w", "0 min", "?"])
        self.assertEqual(context_size.parse_time("2026-10-20T22:40:52+11:00"),
                         datetime.datetime(2026, 10, 20, 11, 40, 52, tzinfo=datetime.timezone.utc))
        self.assertIsNone(context_size.parse_time("yesterday"))


class StatusContext(LiveFleet):
    """Seats live in w2; orchestrator and dev carry Claude sessions, the reviewer is codex."""

    def setUp(self):
        super().setUp()
        self.world["panes"]["w2:p1"]["agent"]["agent_session"] = fake_herdr.claude_session(ORCH_SESSION)
        self.world["panes"]["w2:p2"]["agent"]["agent_session"] = fake_herdr.claude_session(DEV_SESSION)
        self.world["panes"]["w2:p3"]["agent"]["agent_session"] = fake_herdr.claude_session("c0dex", "codex")
        self.projects = os.path.join(self.tmp, ".claude", "projects")  # HOME is self.tmp
        self.recent = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        self.orch = write_transcript(os.path.join(self.projects, "-tmp-repo"), ORCH_SESSION,
                                     [assistant(2, 5375, 177630, when=self.recent), user()])
        self.dev = write_transcript(os.path.join(self.projects, "-tmp-repo-dev"), DEV_SESSION,
                                    [assistant(3, 1000, 455000, when=self.recent)])

    def snapshot(self):
        found = {}
        for folder, _, files in os.walk(os.path.join(self.tmp, ".claude")):
            for name in files:
                path = os.path.join(folder, name)
                with open(path, "rb") as handle:
                    found[path] = handle.read()
        return found

    def test_column_and_json(self):
        before = self.snapshot()
        code, out, err = self.run_cli("status", "--context")
        self.assertEqual(code, 0, out + err)
        lines = out.splitlines()
        self.assertIn("CONTEXT", lines[1])
        rows = {l.split()[0]: l for l in lines[2:5]}
        self.assertIn("183k (0 min)", rows["sample-orchestrator"])
        self.assertIn("456k (0 min)", rows["sample-dev"])
        self.assertRegex(rows["sample-reviewer"], r"\bidle\s+-\s")  # Codex session has no transcript fixture
        data = json.loads(self.run_cli("status", "--json", "--context")[1])
        contexts = [s["context"] for s in data["seats"]]
        self.assertEqual(contexts[0], {"tokens": 183007, "at": self.recent, "source": "claude-transcript"})
        self.assertEqual(contexts[1]["tokens"], 456003)
        self.assertIsNone(contexts[2])
        self.assertEqual([s["restart_hint"] for s in data["seats"]], [False, False, False])
        for text in (out, json.dumps(data), err):  # invariant 9: no content, no path, no session id
            for leaked in (SECRET, TOOL, "/secret/cwd", ORCH_SESSION, DEV_SESSION, ".jsonl", self.projects):
                self.assertNotIn(leaked, text)
        self.assertEqual(self.snapshot(), before)  # nothing under ~/.claude was written

    def test_context_adds_no_herdr_command(self):
        self.run_cli("status")  # names the panes (design 22.1); the next status only reads them
        os.remove(self.log_path)
        self.run_cli("status")
        plain = [c["argv"] for c in self.calls()]
        os.remove(self.log_path)
        self.run_cli("status", "--context")
        self.assertEqual([c["argv"] for c in self.calls()], plain)

    def test_without_context_nothing_is_read(self):
        refuse = mock.patch.object(context_size, "find_transcript", side_effect=AssertionError("read a transcript"))
        with refuse, mock.patch.object(context_size, "last_usage", side_effect=AssertionError("read a transcript")):
            out = self.run_cli("status")[1]
            data = json.loads(self.run_cli("status", "--json")[1])
        self.assertNotIn("CONTEXT", out)
        self.assertNotIn("context", data["seats"][0])
        self.assertNotIn("restart_hint", data["seats"][0])

    def test_missing_transcript_absent_seat_and_no_session(self):
        os.remove(self.dev)
        self.world["panes"]["w2:p1"]["agent"]["agent_session"] = None   # like an agy started by hand
        self.write_fleet(self.read_fleet() + '\n[[agents]]\nname = "helper"\nkind = "claude"\n')
        data = json.loads(self.run_cli("status", "--json", "--context")[1])
        self.assertEqual([s["context"] for s in data["seats"]], [None, None, None, None])
        out = self.run_cli("status", "--context")[1]
        self.assertNotIn("(0 min)", out)

    def test_restart_hint_threshold(self):
        self.write_fleet("restart_hint_tokens = 450000\n" + self.read_fleet().replace(
            'name = "sample-dev"', 'name = "sample-dev"\nnotes = "slice 4"'))
        out = self.run_cli("status", "--context")[1]
        rows = {l.split()[0]: l for l in out.splitlines()[2:5]}
        self.assertTrue(rows["sample-dev"].endswith("slice 4; consider restart"))
        self.assertNotIn("consider restart", rows["sample-orchestrator"])
        data = json.loads(self.run_cli("status", "--json", "--context")[1])
        self.assertEqual([s["restart_hint"] for s in data["seats"]], [False, True, False])
        self.assertEqual(data["seats"][1]["notes"], "slice 4")
        self.write_fleet("restart_hint_tokens = 183007\n" + self.read_fleet().split("\n", 1)[1])
        data = json.loads(self.run_cli("status", "--json", "--context")[1])
        self.assertEqual([s["restart_hint"] for s in data["seats"]], [True, True, False])  # "at or above"
        self.assertNotIn("consider restart", self.run_cli("status")[1])  # only with --context
        self.assertEqual(self.argvs("agent", "prompt"), [])  # nothing restarts on its own

    def test_projects_override(self):
        other = os.path.join(self.tmp, "elsewhere")
        write_transcript(os.path.join(other, "p"), ORCH_SESSION, [assistant(1, 1, 998)])
        self.env["HERDR_FLEET_CLAUDE_PROJECTS"] = other
        data = json.loads(self.run_cli("status", "--json", "--context")[1])
        self.assertEqual(data["seats"][0]["context"]["tokens"], 1000)
        self.assertIsNone(data["seats"][1]["context"])

    def test_bad_restart_hint_tokens(self):
        base = self.read_fleet()
        for value in ("0", "-5", '"450k"', "true", "1.5"):
            self.write_fleet("restart_hint_tokens = %s\n" % value + base)
            code, _, err = self.run_cli("status", "--context")
            self.assertEqual(code, 2, value)
            self.assertIn("restart_hint_tokens: must be a positive integer", err)


if __name__ == "__main__":
    unittest.main()
