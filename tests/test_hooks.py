"""Claude Code hooks (design 18.7, version 0.7.0): `hooks install [--remove]` on a
settings file with other content, `inbox --hook stop` (the silent cases, the batch, the
files moved, the cap), `inbox --hook prompt` (counts, moves nothing), and the doctor line."""

from __future__ import annotations

import contextlib
import io
import json
import os
import unittest

from support import CALLER_PANE, LiveFleet
from herdr_fleet import hooks, inbox
from herdr_fleet.inbox import CUR, NEW
from test_inbox import box, log_entries, put

OTHER = {
    "model": "claude-opus-5-5",
    "permissions": {"allow": ["Bash(herdr agent:*)", "Bash(herdr-fleet:*)"], "deny": ["Bash(rm:*)"]},
    "hooks": {
        "Stop": [{"hooks": [{"type": "command", "command": "afplay done.aiff"}]}],
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "audit.sh"}]}],
    },
}
STOP = {"type": "command", "command": "herdr-fleet inbox --hook stop", "timeout": 10}
PROMPT = {"type": "command", "command": "herdr-fleet inbox --hook prompt", "timeout": 10}


class Install(LiveFleet):
    def settings(self):
        return os.path.join(self.repo, ".claude", "settings.local.json")

    def read(self):
        with open(self.settings()) as handle:
            return handle.read()

    def test_install_creates_the_file_with_exactly_the_two_entries(self):
        code, out, _ = self.run_cli("hooks", "install")
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[0],
                         "installed the Stop and UserPromptSubmit hooks in %s" % self.settings())
        self.assertEqual(json.loads(self.read()),
                         {"hooks": {"Stop": [{"hooks": [STOP]}], "UserPromptSubmit": [{"hooks": [PROMPT]}]}})
        self.assertEqual(oct(os.stat(self.settings()).st_mode & 0o777), "0o644")
        self.assertEqual(self.calls(), [])  # pure file work

    def test_install_and_remove_keep_everything_else_and_are_idempotent(self):
        os.makedirs(os.path.dirname(self.settings()))
        with open(self.settings(), "w") as handle:
            json.dump(OTHER, handle)
        self.assertEqual(self.run_cli("hooks", "install")[0], 0)
        data = json.loads(self.read())
        self.assertEqual(data["model"], OTHER["model"])
        self.assertEqual(data["permissions"], OTHER["permissions"])
        self.assertEqual(data["hooks"]["PreToolUse"], OTHER["hooks"]["PreToolUse"])
        self.assertEqual(data["hooks"]["Stop"], OTHER["hooks"]["Stop"] + [{"hooks": [STOP]}])
        self.assertEqual(data["hooks"]["UserPromptSubmit"], [{"hooks": [PROMPT]}])
        once = self.read()
        code, out, _ = self.run_cli("hooks", "install")
        self.assertEqual((code, out.splitlines()[0]), (0, "already installed in %s" % self.settings()))
        self.assertEqual(self.read(), once)
        code, out, _ = self.run_cli("hooks", "install", "--remove")
        self.assertEqual((code, out.strip()),
                         (0, "removed the Stop and UserPromptSubmit hooks from %s" % self.settings()))
        self.assertEqual(json.loads(self.read()), OTHER)
        code, out, _ = self.run_cli("hooks", "install", "--remove")
        self.assertEqual(out.strip(), "nothing to remove: no herdr-fleet hooks in %s" % self.settings())
        self.assertEqual(json.loads(self.read()), OTHER)
        self.assertEqual(self.calls(), [])

    def test_remove_takes_only_our_entry_out_of_a_shared_group(self):
        data = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "mine.sh"}, dict(STOP)]}],
                          "UserPromptSubmit": [{"hooks": [dict(PROMPT)]}]}}
        self.assertEqual(hooks.remove(data), ["Stop", "UserPromptSubmit"])
        self.assertEqual(data, {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "mine.sh"}]}]}})
        self.assertEqual(hooks.installed(data), {"Stop": False, "UserPromptSubmit": False})

    def test_a_broken_settings_file_is_left_alone(self):
        os.makedirs(os.path.dirname(self.settings()))
        for text in ("{not json", "[1, 2]", '{"hooks": []}'):
            with open(self.settings(), "w") as handle:
                handle.write(text)
            code, _, err = self.run_cli("hooks", "install")
            self.assertEqual(code, 1, text)
            self.assertIn("nothing was changed", err)
            self.assertEqual(self.read(), text)

    def test_warnings_for_no_claude_seat_and_no_launcher(self):
        self.write_fleet('[[agents]]\nname = "fleet-codex"\nkind = "codex"\n')
        env = dict(self.env, PATH="/nonexistent")
        code, _, err = self.run_cli("hooks", "install", env=env)
        self.assertEqual(code, 0)
        self.assertIn("no seat of kind claude", err)
        self.assertIn("herdr-fleet is not on PATH", err)

    def test_doctor_line(self):
        def line():
            out = self.run_cli("doctor")[1]
            return [l for l in out.splitlines() if l[6:].startswith("hooks:")]
        self.assertEqual(line(), ["info  hooks: not installed (herdr-fleet hooks install)"])
        self.run_cli("hooks", "install")
        self.assertEqual(line(), ["ok    hooks: installed"])
        data = json.loads(self.read())
        del data["hooks"]["UserPromptSubmit"]
        with open(self.settings(), "w") as handle:
            json.dump(data, handle)
        self.assertEqual(line(), ["warn  hooks: partly installed (Stop only); run herdr-fleet hooks install"])
        self.write_fleet('[[agents]]\nname = "fleet-codex"\nkind = "codex"\n')
        self.assertEqual(line(), [])  # no Claude seat: no line


class Hook(LiveFleet):
    """Each test runs the hook as sample-dev's Claude Code would: HERDR_PANE_ID is its pane."""

    def setUp(self):
        super().setUp()
        self.seat_env = dict(self.env, HERDR_PANE_ID="w2:p2")

    def hook(self, which, payload=None, env=None, cwd=None):
        stdin = json.dumps(payload if payload is not None else {"hook_event_name": which, "cwd": self.repo,
                                                                 "session_id": "s"})
        before = len(self.calls())
        code, out, err = self.run_cli("inbox", "--hook", which, stdin=stdin, env=env or self.seat_env, cwd=cwd)
        self.assertEqual((code, err), (0, ""))
        return out, [c["argv"] for c in self.calls()[before:]]

    def test_stop_hands_the_batch_back_and_moves_the_files(self):
        first = put(self.repo, "sample-dev", "Task A\n")
        second = put(self.repo, "sample-dev", "FYI: build green", sender="sample-reviewer", fyi=True)
        out, calls = self.hook("stop", {"hook_event_name": "Stop", "stop_hook_active": False, "cwd": self.repo})
        self.assertEqual(calls, [["agent", "get", "w2:p2"]])
        data = json.loads(out)
        self.assertEqual(list(data), ["hookSpecificOutput"])
        self.assertEqual(data["hookSpecificOutput"]["hookEventName"], "Stop")
        self.assertEqual(data["hookSpecificOutput"]["additionalContext"], inbox.render_batch([first, second]))
        self.assertEqual(box(self.repo, "sample-dev", NEW), [])
        self.assertEqual(box(self.repo, "sample-dev", CUR), sorted([first.id, second.id]))
        self.assertEqual([(e["kind"], e["via"], e["id"]) for e in log_entries(self.repo)],
                         [("delivered", "hook", first.id), ("delivered", "hook", second.id)])
        self.assertEqual(self.argvs("agent", "prompt"), [])  # read by the seat, never prompted
        self.assertEqual(self.hook("stop")[0], "")  # nothing left

    def test_stop_is_silent_in_every_other_case(self):
        put(self.repo, "sample-dev", "Task A")
        outside = {k: v for k, v in self.seat_env.items() if k not in ("HERDR_ENV", "HERDR_BIN_PATH")}
        no_pane = {k: v for k, v in self.seat_env.items() if k != "HERDR_PANE_ID"}
        cases = [
            ("loop protection", {"stop_hook_active": True, "cwd": self.repo}, None, None, []),
            ("no pane", None, no_pane, None, []),
            ("no herdr", None, outside, None, []),
            ("no fleet", {"cwd": self.tmp}, None, self.tmp, []),
            ("not a seat", None, dict(self.env, HERDR_PANE_ID=CALLER_PANE), None, [["agent", "get", CALLER_PANE]]),
            ("no agent in the pane", None, dict(self.env, HERDR_PANE_ID="w9:p9"), None, [["agent", "get", "w9:p9"]]),
        ]
        for label, payload, env, cwd, expected in cases:
            out, calls = self.hook("stop", payload, env=env, cwd=cwd)
            self.assertEqual((out, calls), ("", expected), label)
        self.assertEqual(len(box(self.repo, "sample-dev", NEW)), 1)

    def test_unreadable_hook_input_counts_as_no_input(self):
        put(self.repo, "sample-dev", "Task A")
        code, out, err = self.run_cli("inbox", "--hook", "stop", stdin="not json", env=self.seat_env)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Task A", json.loads(out)["hookSpecificOutput"]["additionalContext"])

    def test_stop_with_only_fyis_or_nothing_prints_nothing(self):
        self.assertEqual(self.hook("stop")[0], "")
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".herdr", "inbox")))  # no mailbox created
        put(self.repo, "sample-dev", "FYI: x", fyi=True)
        self.assertEqual(self.hook("stop")[0], "")
        self.assertEqual(len(box(self.repo, "sample-dev", NEW)), 1)

    def test_a_broken_fleet_file_is_silent(self):
        put(self.repo, "sample-dev", "Task A")
        self.write_fleet("[[agents]]\nname = 'Bad Name'\n")
        self.assertEqual(self.hook("stop")[0], "")
        self.assertEqual(len(box(self.repo, "sample-dev", NEW)), 1)

    def test_the_fleet_is_found_from_the_hook_cwd(self):
        put(self.repo, "sample-dev", "Task A")
        out, _ = self.hook("stop", {"cwd": os.path.join(self.repo, ".herdr")}, cwd=self.tmp)
        self.assertIn("Task A", json.loads(out)["hookSpecificOutput"]["additionalContext"])

    def test_above_the_cap_the_pointer_and_the_files_stay(self):
        put(self.repo, "sample-dev", "y" * 9100)  # under 16 KiB, over the 9,000 characters a hook may carry
        out, _ = self.hook("stop")
        context = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        self.assertRegex(context, r"^\[from herdr-fleet\] 1 message waiting \(oldest \d\d:\d\d, 9\.\d KB\); "
                                  r"run: herdr-fleet inbox --read$")
        self.assertEqual(len(box(self.repo, "sample-dev", NEW)), 1)
        self.assertEqual(log_entries(self.repo), [])

    def test_prompt_counts_and_moves_nothing(self):
        self.assertEqual(self.hook("prompt")[0], "")
        put(self.repo, "sample-dev", "Task A")
        out, calls = self.hook("prompt", {"hook_event_name": "UserPromptSubmit", "prompt": "hi", "cwd": self.repo})
        self.assertEqual(json.loads(out), {"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "1 message waiting; run: herdr-fleet inbox --read"}})
        self.assertEqual(calls, [["agent", "get", "w2:p2"]])
        put(self.repo, "sample-dev", "FYI: y", fyi=True)
        put(self.repo, "sample-dev", "gone", ttl=inbox.now_utc())  # past its ttl: not counted
        self.assertEqual(json.loads(self.hook("prompt")[0])["hookSpecificOutput"]["additionalContext"],
                         "2 messages waiting; run: herdr-fleet inbox --read")
        self.assertEqual(len(box(self.repo, "sample-dev", NEW)), 3)
        self.assertEqual(log_entries(self.repo), [])

    def test_hook_output_is_ascii(self):
        put(self.repo, "sample-dev", "café → done")
        out, _ = self.hook("stop")
        out.encode("ascii")
        self.assertIn("café → done", json.loads(out)["hookSpecificOutput"]["additionalContext"])

    def test_usage(self):
        self.assertEqual(self.run_cli("inbox", "--hook", "stop", "--as", "sample-dev", env=self.seat_env)[0], 2)
        with contextlib.redirect_stderr(io.StringIO()):  # argparse's own usage text
            self.assertEqual(self.run_cli("inbox", "--hook", "stop", "--read", env=self.seat_env)[0], 2)
            self.assertEqual(self.run_cli("inbox", "--hook", "later", env=self.seat_env)[0], 2)
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
