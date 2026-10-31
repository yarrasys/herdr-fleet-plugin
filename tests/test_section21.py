"""Design section 21 (version 0.10.0): nothing is typed into a working seat except by
``send --now`` and ``deliver NAME --now`` (21.1), and the presence guard (21.2): no text
goes to the pane the owner is typing in, or whose prompt line is not on screen.

The guard is checked against the fake herdr's focus and scripted screens
(``fake_herdr.focus``, ``type_in``, ``show_dialog``); ``tests/support.py`` fails any
test in which an ``agent prompt`` reached such a pane (``owner_typing``)."""

from __future__ import annotations

import json
import os
import re
import unittest

import fake_herdr
from support import CALLER_PANE, FleetCase, LiveFleet, typed_ahead_allowed, policy_output
from herdr_fleet import inbox, presence, watch
from herdr_fleet.herdr import Herdr
from herdr_fleet.inbox import CUR, NEW
from herdr_fleet.messages import URGENT_TYPED, place
from herdr_fleet.seats import PROMPT_GLYPHS, PROMPT_PLACEHOLDERS, SEND_ALLOWED, SETTLED, safe_prompt
from test_inbox import box, log_entries, put
from test_watch import StopClock, WatchCase, events

SHORT = r"#[0-9a-f]{6}"
WORKING = re.escape(place("working"))
WAIT = ["--until", "idle", "--until", "done", "--timeout", "30000"]
TYPING = "owner typing in w2:p2"
IN_INPUT = "text in its input (w2:p2); press Ctrl-U there to clear it"  # design 23.11: a pane not the owner's

# Screen tails as `herdr pane read PANE --lines 12` printed them live (herdr 0.9.5, 21 Oct 2026).
CLAUDE_EMPTY = ("⏺ Done. The file is saved.\n\n" + "─" * 80 + "\n❯ \n" + "─" * 80 +
                "\n  ? for shortcuts                                   Context left until auto-compact: 61%\n")
CLAUDE_TYPED = CLAUDE_EMPTY.replace("❯ \n", "❯ half typed by the owner\n")
CODEX_EMPTY = ("• Updated README.md\n\n› Ask Codex to do anything\n\n"
               "  100% context left · ? for shortcuts\n")
CODEX_TYPED = CODEX_EMPTY.replace("Ask Codex to do anything", "fix the flaky test")
AGY_EMPTY = "✦ Done.\n\n>\n\n  gemini-3-pro · Press up to edit queued messages\n"
AGY_TYPED = AGY_EMPTY.replace("\n>\n", "\n> look at the logs\n")


# -- the pure part --------------------------------------------------------------------------------


class PromptLine(unittest.TestCase):
    def test_the_three_real_screen_tails(self):
        self.assertEqual(presence.prompt_line(CLAUDE_EMPTY, "claude"), "")
        self.assertEqual(presence.prompt_line(CLAUDE_TYPED, "claude"), "half typed by the owner")
        self.assertEqual(presence.prompt_line(CODEX_EMPTY, "codex"), "Ask Codex to do anything")
        self.assertEqual(presence.prompt_line(CODEX_TYPED, "codex"), "fix the flaky test")
        self.assertEqual(presence.prompt_line(AGY_EMPTY, "agy"), "")
        self.assertEqual(presence.prompt_line(AGY_TYPED, "agy"), "look at the logs")

    def test_typed_means_text_that_is_not_the_placeholder(self):
        for text, kind, typed in ((CLAUDE_EMPTY, "claude", False), (CLAUDE_TYPED, "claude", True),
                                  (CODEX_EMPTY, "codex", False), (CODEX_TYPED, "codex", True),
                                  (AGY_EMPTY, "agy", False), (AGY_TYPED, "agy", True)):
            with self.subTest(kind=kind, typed=typed):
                self.assertIs(presence.is_typed(presence.prompt_line(text, kind), kind), typed)

    def test_the_last_glyph_line_wins_and_older_builds_use_a_plain_angle(self):
        screen = "> an earlier prompt in the history\n\nanswer\n" + "─" * 40 + "\n> \n"
        self.assertEqual(presence.prompt_line(screen, "claude"), "")
        self.assertEqual(presence.prompt_line("   ❯   spaced out  \n  status\n", "claude"), "spaced out")
        boxed_empty = "│ >                                        │\n└──────────┘\n"
        self.assertEqual(presence.prompt_line(boxed_empty, "claude"), "")
        self.assertFalse(presence.is_typed(presence.prompt_line(boxed_empty, "claude"), "claude"))
        self.assertEqual(presence.prompt_line("│ > boxed text │\n", "claude"), "boxed text")

    def test_a_fresh_claude_session_shows_a_suggestion_which_is_not_typed_text(self):
        fresh = "\u2500" * 40 + "\n\u276f Try \"write a test for test_init.py\"\n" + "\u2500" * 40 + "\n  status\n"
        self.assertEqual(presence.prompt_line(fresh, "claude"), 'Try "write a test for test_init.py"')
        self.assertFalse(presence.is_typed(presence.prompt_line(fresh, "claude"), "claude"))
        self.assertTrue(presence.is_typed('Try "x" and more', "claude"))
        self.assertTrue(presence.is_typed("Try harder", "claude"))

    def test_no_prompt_line_counts_as_typing(self):
        dialog = "╭──╮\n│ Do you want to proceed? │\n│   1. Yes │\n╰──╯\n"
        self.assertIsNone(presence.prompt_line(dialog, "claude"))
        self.assertTrue(presence.is_typed(None, "claude"))
        self.assertIsNone(presence.prompt_line("", "codex"))

    def test_glyph_tables(self):
        self.assertEqual(PROMPT_GLYPHS, {"claude": ("❯", ">"), "codex": ("›",), "agy": (">",)})
        self.assertEqual(PROMPT_PLACEHOLDERS, {"codex": ("Ask Codex to do anything",)})
        self.assertFalse(presence.checkable("pi"))
        self.assertIsNone(presence.prompt_line("> typed\n", "pi"))
        self.assertEqual(presence.unchecked_kinds(["claude", "pi", "codex", "pi", "gemini", ""]), ["pi", "gemini"])

    def test_send_allowed_is_the_now_allowance(self):
        self.assertEqual(SEND_ALLOWED, SETTLED + ("working",))


# -- the guard against the fake herdr --------------------------------------------------------------


class Guard(LiveFleet):
    """``presence.typing`` with a herdr client on the fake, step by step (design 21.2)."""

    def client(self):
        with open(self.world_path, "w", encoding="utf-8") as handle:
            json.dump(self.world, handle)
        return Herdr("/fake/bin/herdr", runner=fake_herdr.Fake(self.world_path, self.log_path))

    def guard(self, pane="w2:p2", kind="claude"):
        before = len(self.calls())
        answer = presence.typing(self.client(), pane, kind)
        return answer, [c["argv"][:2] for c in self.calls()[before:]]

    def test_not_focused_is_one_pane_read_and_text_there_holds_too(self):  # design 23.11
        self.assertEqual(self.guard(), (None, [["pane", "read"]]))
        fake_herdr.type_in(self.world, "w2:p2", "half typed")  # text left in a pane the owner is not in
        self.assertEqual(self.guard(), (IN_INPUT, [["pane", "read"], ["pane", "get"]]))

    def test_focused_in_an_unfocused_workspace_or_a_background_tab_is_not_the_owners(self):
        fake_herdr.focus(self.world, "w2:p2")
        fake_herdr.type_in(self.world, "w2:p2", "half typed")
        fake_herdr.unfocus(self.world)  # the owner is in another app, or herdr's UI is detached
        self.assertEqual(self.guard(), (IN_INPUT, [["pane", "read"], ["pane", "get"], ["workspace", "list"]]))
        fake_herdr.focus(self.world, "w2:p2")
        self.world["workspaces"]["w2"]["active_tab_id"] = "w2:t9"  # the owner switched to another tab
        self.assertEqual(self.guard()[0], IN_INPUT)

    def test_focused_and_empty_passes_after_reading_twelve_lines(self):
        fake_herdr.focus(self.world, "w2:p2")
        answer, calls = self.guard()
        self.assertIsNone(answer)
        self.assertEqual(calls, [["pane", "read"]])  # design 23.11: the focus chain only words a hold
        (read,) = self.argvs("pane", "read")
        self.assertEqual(read, ["pane", "read", "w2:p2", "--lines", "12", "--format", "ansi"])  # design 32.8

    def test_focused_and_typing_holds(self):
        fake_herdr.focus(self.world, "w2:p2")
        fake_herdr.type_in(self.world, "w2:p2", "half typed by the owner")
        self.assertEqual(self.guard()[0], TYPING)

    def test_focused_with_a_dialog_on_screen_holds(self):
        fake_herdr.focus(self.world, "w2:p2")
        fake_herdr.show_dialog(self.world, "w2:p2")
        self.assertEqual(self.guard()[0], TYPING)

    def test_codex_placeholder_is_empty_and_its_text_is_typing(self):
        fake_herdr.focus(self.world, "w2:p3")
        self.assertIsNone(self.guard("w2:p3", "codex")[0])
        fake_herdr.type_in(self.world, "w2:p3", "fix the flaky test")
        self.assertEqual(self.guard("w2:p3", "codex")[0], "owner typing in w2:p3")

    def test_antigravity(self):
        fake_herdr.put_agent(self.world, "w2:p3", "agy", "sample-reviewer")
        fake_herdr.focus(self.world, "w2:p3")
        self.assertIsNone(self.guard("w2:p3", "agy")[0])
        fake_herdr.set_screen(self.world, "w2:p3", AGY_TYPED)
        self.assertEqual(self.guard("w2:p3", "agy")[0], "owner typing in w2:p3")

    def test_an_unknown_kind_passes_without_a_call(self):
        fake_herdr.put_agent(self.world, "w2:p3", "pi", "sample-reviewer")
        fake_herdr.focus(self.world, "w2:p3")
        fake_herdr.type_in(self.world, "w2:p3", "typed")
        self.assertEqual(self.guard("w2:p3", "pi"), (None, []))

    def test_safe_prompt_runs_the_guard_after_the_state_check(self):
        fake_herdr.focus(self.world, "w2:p2")
        fake_herdr.type_in(self.world, "w2:p2", "half typed")
        outcome = safe_prompt(self.client(), "sample-dev", "hello")
        self.assertEqual((outcome.ok, outcome.status, outcome.detail), (False, "typing", TYPING))
        self.assertEqual(self.prompts_to("sample-dev"), [])
        self.status_of("w2:p2", "blocked")  # a blocked seat is refused before any screen is read
        before = len(self.calls())
        outcome = safe_prompt(self.client(), "sample-dev", "hello")
        self.assertEqual(outcome.status, "blocked")
        self.assertEqual([c["argv"][:2] for c in self.calls()[before:]], [["agent", "get"]])

    def test_a_herdr_error_in_the_guard_sends_nothing(self):
        fake_herdr.focus(self.world, "w2:p2")
        self.world["canned"] = [{"prefix": ["pane", "read"], "exit": 1,
                                 "stderr": {"error": {"code": "pane_not_found", "message": "gone"}}}]
        outcome = safe_prompt(self.client(), "sample-dev", "hello")
        self.assertEqual(outcome.status, "error")
        self.assertIn("presence check", outcome.detail)
        self.assertEqual(self.prompts_to("sample-dev"), [])


class Doctor(FleetCase):
    def test_a_kind_without_a_glyph_is_reported(self):
        self.write_fleet('[[agents]]\nname = "a"\nkind = "claude"\n[[agents]]\nname = "b"\nkind = "pi"\n'
                         '[[agents]]\nname = "c"\nkind = "codex"\n')
        fake_herdr.add_workspace(self.world, "w2", "fleet", self.repo)
        fake_herdr.put_agent(self.world, "w2:p1", "gemini", "c")  # live kind differs from the file
        code, out, _ = self.run_cli("doctor")
        self.assertEqual(code, 0, out)
        lines = [l for l in policy_output(out).splitlines() if "presence" in l]
        self.assertEqual(lines, ["info  presence: no prompt glyph for kind pi; its seats are not held while the "
                                 "owner types",
                                 "info  presence: no prompt glyph for kind gemini; its seats are not held while "
                                 "the owner types"])

    def test_no_line_when_every_kind_is_checked(self):
        code, out, _ = self.run_cli("doctor")
        self.assertNotIn("presence", out)


# -- every caller's `typing` line ------------------------------------------------------------------


class OwnerTyping(LiveFleet):
    """The owner is in sample-dev's pane (w2:p2) with text half typed."""

    def setUp(self):
        super().setUp()
        fake_herdr.focus(self.world, "w2:p2")
        fake_herdr.type_in(self.world, "w2:p2", "half typed by the owner")

    def clear(self):
        fake_herdr.type_in(self.world, "w2:p2", "")

    def header(self, name):
        (message,) = inbox.read_box(self.repo, name, NEW)
        with open(message.path) as handle:
            return handle.read().split("\n\n", 1)[0].splitlines()

    def test_send_queues_on_refusal(self):
        code, out, _ = self.run_cli("send", "--as", "sample-orchestrator", "sample-dev", "ping")
        self.assertEqual(code, 0, out)
        self.assertRegex(policy_output(out).strip(), r"^queued in herdr-fleet for sample-dev \(idle, %s\) %s, position 1 of 1$"
                         % (TYPING, SHORT))
        self.assertEqual(self.prompts_to("sample-dev"), [])
        self.assertIn("queued_by: refusal", self.header("sample-dev"))
        (entry,) = log_entries(self.repo)
        self.assertEqual((entry["kind"], entry["delivery"]), ("send", "queued"))
        self.clear()  # the owner submitted or deleted the line: the next delivery goes
        code, out, _ = self.run_cli("deliver")
        self.assertRegex(policy_output(out).strip(), r"^delivered 1 to sample-dev \(idle\), \d+ B$")
        self.assertIn("ping", self.prompts_to("sample-dev")[0])

    def test_send_now_is_refused(self):
        code, out, _ = self.run_cli("send", "--now", "sample-dev", "ping")
        self.assertEqual(code, 1)
        self.assertEqual(policy_output(out).strip(), "refused sample-dev (idle, %s): nothing sent" % TYPING)
        (entry,) = log_entries(self.repo)
        self.assertEqual(entry["delivery"], "refused")
        self.assertEqual(box(self.repo, "sample-dev", CUR), [entry["id"]])
        self.assertEqual(box(self.repo, "sample-dev", NEW), [])
        self.status_of("w2:p2", "working")
        code, out, _ = self.run_cli("send", "--now", "sample-dev", "ping again")
        self.assertEqual((code, policy_output(out).strip()), (1, "refused sample-dev (working, %s): nothing sent" % TYPING))
        self.assertEqual(self.argvs("agent", "prompt"), [])

    def test_other_targets_of_the_same_send_still_go(self):
        code, out, _ = self.run_cli("send", "sample-dev", "sample-reviewer", "standup")
        self.assertEqual(code, 0)
        lines = policy_output(out).splitlines()
        self.assertTrue(lines[0].startswith("queued in herdr-fleet for sample-dev (idle, %s)" % TYPING), lines)
        self.assertRegex(lines[1], r"^sent to sample-reviewer \(idle\), \d+ B$")

    def test_deliver_and_watch_once_leave_the_queue(self):
        put(self.repo, "sample-dev", "one")
        put(self.repo, "sample-dev", "two")
        for argv in (["deliver"], ["deliver", "sample-dev"], ["watch", "--once"]):
            with self.subTest(argv=argv):
                code, out, _ = self.run_cli(*argv)
                self.assertEqual((code, policy_output(out).strip()), (0, "sample-dev: %s, 2 left queued" % TYPING))
        self.assertEqual(len(box(self.repo, "sample-dev", NEW)), 2)
        self.assertEqual(self.argvs("agent", "prompt"), [])
        self.assertEqual(self.world["notifications"], [])

    def test_deliver_now_is_held_too(self):
        put(self.repo, "sample-dev", "one")
        self.status_of("w2:p2", "working")
        code, out, _ = self.run_cli("deliver", "sample-dev", "--now")
        self.assertEqual((code, policy_output(out).strip()), (0, "sample-dev: %s, 1 left queued" % TYPING))
        self.assertEqual(self.argvs("agent", "prompt"), [])

    def test_the_flush_before_send_is_held(self):
        self.status_of("w2:p2", "blocked")
        self.run_cli("send", "sample-dev", "first")
        self.status_of("w2:p2", "idle")
        code, out, _ = self.run_cli("send", "--queue", "sample-dev", "second")
        self.assertEqual(code, 0)
        lines = policy_output(out).splitlines()
        self.assertRegex(lines[0], r"^queued in herdr-fleet for sample-dev \(idle\) %s, position 2 of 2$" % SHORT)
        self.assertEqual(lines[1], "sample-dev: %s, 1 left queued" % TYPING)
        self.assertEqual(self.argvs("agent", "prompt"), [])

    def test_restart_swap_and_down_refuse_like_a_blocked_seat(self):
        code, out, _ = self.run_cli("restart", "sample-dev")
        self.assertEqual(code, 3, out)
        self.assertIn("sample-dev: refused - %s" % TYPING, out)
        code, out, _ = self.run_cli("down", "sample-dev")
        self.assertEqual(code, 3, out)
        self.assertIn("sample-dev: refused - %s" % TYPING, out)
        self.write_fleet(self.read_fleet().replace(
            'name = "sample-dev"\nkind = "claude"\nargs = ["--model", "claude-opus-5-5"]',
            'name = "sample-dev"\nkind = "claude"\nargs = ["--model", "claude-opus-6"]'))
        code, out, _ = self.run_cli("swap", "sample-dev")
        self.assertEqual(code, 3, out)
        self.assertIn("sample-dev: refused - %s" % TYPING, out)
        self.assertEqual(self.argvs("agent", "prompt"), [])  # no quit command typed into the owner's line
        self.assertEqual(self.argvs("agent", "start"), [])
        self.assertEqual(self.pane_of("sample-dev"), "w2:p2")

    def test_down_quits_the_others(self):
        code, out, _ = self.run_cli("down")
        self.assertEqual(code, 3, out)
        self.assertIn("sample-dev: refused - %s" % TYPING, out)
        self.assertEqual(self.prompts_to("sample-orchestrator"), ["/exit"])
        self.assertEqual(self.prompts_to("sample-reviewer"), ["/quit"])

    def test_kickoff_of_a_live_seat_is_refused(self):
        code, out, _ = self.run_cli("kickoff", "sample-dev", "sample-reviewer")
        self.assertEqual(code, 3, out)
        self.assertIn("sample-dev: not briefed - %s" % TYPING, out)
        self.assertIn("sample-reviewer: briefed", out)
        self.assertEqual(self.prompts_to("sample-dev"), [])

    def test_dry_runs_say_would_hold(self):
        code, out, _ = self.run_cli("send", "--dry-run", "sample-dev", "ping")
        self.assertEqual((code, policy_output(out).strip()), (0, "would queue in herdr-fleet for sample-dev (idle); would hold: %s"
                                               % TYPING))
        code, out, _ = self.run_cli("send", "--dry-run", "--now", "sample-dev", "ping")
        self.assertEqual(policy_output(out).strip(), "would refuse sample-dev (idle): nothing sent; would hold: %s" % TYPING)
        put(self.repo, "sample-dev", "one")
        code, out, _ = self.run_cli("deliver", "--dry-run")
        self.assertEqual(policy_output(out).strip(), "sample-dev: would hold: %s, 1 left queued" % TYPING)
        code, out, _ = self.run_cli("restart", "--dry-run", "sample-dev")
        self.assertIn("sample-dev: plan - would hold: %s (the quit is refused while the owner types there)" % TYPING,
                      out)
        self.assertNotIn("agent start", out)
        self.assertEqual(self.argvs("agent", "prompt"), [])
        self.assertEqual(len(box(self.repo, "sample-dev", NEW)), 1)

    def test_a_dialog_on_screen_holds_like_text(self):
        fake_herdr.show_dialog(self.world, "w2:p2")
        code, out, _ = self.run_cli("send", "sample-dev", "ping")
        self.assertIn("(idle, %s)" % TYPING, out)
        self.assertEqual(self.argvs("agent", "prompt"), [])

    def test_a_seat_the_owner_only_looks_at_is_delivered_to(self):
        self.clear()
        code, out, _ = self.run_cli("send", "sample-dev", "ping")
        self.assertRegex(policy_output(out).strip(), r"^sent to sample-dev \(idle\), \d+ B$")
        (prompt,) = [c for c in self.calls() if c["argv"][:2] == ["agent", "prompt"]]
        self.assertFalse(prompt["info"]["owner_typing"])
        kinds = [c["argv"][:2] for c in self.calls()]
        self.assertLess(kinds.index(["pane", "read"]), kinds.index(["agent", "prompt"]))

    def test_text_left_in_a_pane_the_owner_left_holds_too(self):  # design 23.11 (21.2 let it merge)
        fake_herdr.focus(self.world, "w2:p1")  # the owner clicked into the orchestrator's pane
        code, out, _ = self.run_cli("send", "sample-dev", "ping")
        self.assertEqual(code, 0)
        self.assertRegex(policy_output(out).strip(), r"^queued in herdr-fleet for sample-dev \(idle, %s\) %s, position 1 of 1$"
                         % (re.escape(IN_INPUT), SHORT))
        self.assertEqual(self.prompts_to("sample-dev"), [])


class KickoffAfterStart(FleetCase):
    def test_a_seat_started_in_the_pane_the_owner_looks_at_is_briefed(self):
        self.write_fleet('workspace = "sample"\n[[agents]]\nname = "sample-dev"\nkind = "claude"\n')
        self.fleet_ws()
        fake_herdr.focus(self.world, "w2:p1")
        self.write_state({"version": 1, "workspace_id": "w2", "workspace_label": "sample", "created_workspace": False,
                          "agents": {"sample-dev": {"pane_id": "w2:p1", "created_pane": False}}})
        code, out, err = self.run_cli("up", "--no-watch")
        self.assertEqual(code, 0, out + err)
        self.assertIn("sample-dev: briefed", out)
        kinds = [c["argv"][:2] for c in self.calls()]
        start = kinds.index(["agent", "start"])
        self.assertEqual(kinds[start:].count(["pane", "read"]), 1)  # the guard ran, and passed
        self.assertLess(kinds.index(["pane", "read"], start), kinds.index(["agent", "prompt"], start))


# -- the watcher's hold ----------------------------------------------------------------------------


class HoldClock(StopClock):
    """StopClock that runs ``on_hold(n)`` at the n-th 3 s hold sleep, and stops a hold that
    would never end."""

    def __init__(self, on_hold, cycles=2):
        super().__init__(cycles=cycles)
        self.on_hold, self.holds = on_hold, 0

    def sleep(self, seconds):
        if seconds == watch.HOLD_RECHECK_S:
            self.holds += 1
            if self.holds > 50:
                raise KeyboardInterrupt
            self.on_hold(self.holds)
        super().sleep(seconds)


class WatcherHold(WatchCase):
    def setUp(self):
        super().setUp()
        put(self.repo, "sample-dev", "Task A")
        fake_herdr.focus(self.world, "w2:p2")
        fake_herdr.type_in(self.world, "w2:p2", "half typed by the owner")

    def edit_world(self, change):
        with open(self.world_path, encoding="utf-8") as handle:
            world = json.load(handle)
        change(world)
        with open(self.world_path, "w", encoding="utf-8") as handle:
            json.dump(world, handle)

    def at_hold(self, number, change):
        def hook(n):
            if n == number:
                self.edit_world(change)
        self.clock = HoldClock(hook)

    def test_holds_until_the_owner_clears_the_line_then_delivers(self):
        self.at_hold(3, lambda w: fake_herdr.type_in(w, "w2:p2", ""))
        self.behave(wait_results={"sample-dev": ["idle"]})
        code, out, err = self.watch(cycles=1)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(events(out)[1:], ["waiting on sample-dev (1 queued)", "sample-dev idle; delivering 1",
                                           "sample-dev idle; %s, holding 1" % TYPING,
                                           "delivered 1 to sample-dev", "stopped"])
        (prompt,) = self.prompts_to("sample-dev")
        self.assertIn("Task A", prompt)
        self.assertEqual(self.clock.holds, 3)
        self.assertEqual(self.clock.slept, 5.0 + 3 * watch.HOLD_RECHECK_S + watch.CYCLE_S)
        # the guard ran at the delivery, at each of the three looks, and at the delivery after them
        self.assertEqual(len(self.argvs("pane", "read", "w2:p2")), 1 + 3 + 1)
        self.assertEqual([e["via"] for e in log_entries(self.repo)], ["watch"])

    def test_a_pane_the_owner_left_still_holds_until_its_text_is_cleared(self):  # design 23.11
        def change(n):
            if n == 2:
                self.edit_world(lambda w: fake_herdr.focus(w, "w2:p1"))  # the text stays behind
            if n == 4:
                self.edit_world(lambda w: fake_herdr.type_in(w, "w2:p2", ""))
        self.clock = HoldClock(change)
        self.behave(wait_results={"sample-dev": ["idle"]})
        code, out, _ = self.watch(cycles=1)
        self.assertEqual(events(out)[1:], ["waiting on sample-dev (1 queued)", "sample-dev idle; delivering 1",
                                           "sample-dev idle; %s, holding 1" % TYPING,
                                           "delivered 1 to sample-dev", "stopped"])
        self.assertEqual(self.clock.holds, 4)
        looks = [c["argv"][:2] for c in self.calls() if c["argv"][:2] in (["pane", "get"], ["pane", "read"])]
        # the delivery and looks 1, 2 (focused: read, get), looks 3 (not focused: read, get), look 4 and the
        # delivery after it (read only: the line is empty)
        self.assertEqual(looks, [["pane", "read"], ["pane", "get"]] * 4 + [["pane", "read"]] * 2)

    def test_the_seat_going_working_during_a_hold_goes_back_to_waiting(self):
        def change(world):
            fake_herdr.type_in(world, "w2:p2", "")  # the owner submitted the line: the seat works
            world["panes"]["w2:p2"]["agent"]["agent_status"] = "working"
        self.at_hold(1, change)
        self.behave(wait_results={"sample-dev": ["idle", "idle"]})
        code, out, _ = self.watch(cycles=1)
        self.assertEqual(code, 0)
        self.assertEqual(events(out)[1:], ["waiting on sample-dev (1 queued)", "sample-dev idle; delivering 1",
                                           "sample-dev idle; %s, holding 1" % TYPING, "sample-dev working; waiting",
                                           "sample-dev idle; delivering 1", "delivered 1 to sample-dev", "stopped"])
        self.assertEqual(self.argvs("agent", "wait", "sample-dev"), [["agent", "wait", "sample-dev"] + WAIT] * 2)
        self.assertEqual(len(self.prompts_to("sample-dev")), 1)

    def test_messages_read_meanwhile_end_the_hold(self):
        def change(world):  # the seat read its own inbox during the hold
            for name in os.listdir(os.path.join(self.repo, ".herdr", "inbox", "sample-dev", NEW)):
                os.rename(os.path.join(self.repo, ".herdr", "inbox", "sample-dev", NEW, name),
                          os.path.join(self.repo, ".herdr", "inbox", "sample-dev", CUR, name))
        self.at_hold(2, change)
        self.behave(wait_results={"sample-dev": ["idle"]})
        code, out, _ = self.watch(cycles=1)
        self.assertEqual(events(out)[-1], "stopped")
        self.assertNotIn("delivered 1 to sample-dev", events(out))
        self.assertEqual(self.argvs("agent", "prompt"), [])

    def test_the_hold_has_no_cap(self):
        self.at_hold(40, lambda w: fake_herdr.type_in(w, "w2:p2", ""))
        self.behave(wait_results={"sample-dev": ["idle"]})
        code, out, _ = self.watch(cycles=1)
        self.assertIn("delivered 1 to sample-dev", events(out))
        self.assertEqual(self.clock.holds, 40)
        self.assertEqual(events(out).count("sample-dev idle; %s, holding 1" % TYPING), 1)  # said once


# -- 21.1: a working seat gets text only from --now ------------------------------------------------


class WorkingSeats(LiveFleet):
    MODES = {"": "queued", "--queue": "queued", "--now": "sent", "--fyi": "queued"}

    def test_the_working_row_for_all_four_modes(self):
        for flag, outcome in self.MODES.items():
            with self.subTest(flag=flag):
                self.status_of("w2:p2", "working")
                before = len(self.prompts_to("sample-dev"))
                code, out, _ = self.run_cli("send", *([flag] if flag else []), "sample-dev", "go %s" % flag)
                self.assertEqual(code, 0, out)
                prompted = len(self.prompts_to("sample-dev")) - before
                if outcome == "sent":
                    self.assertEqual(prompted, 1)
                    self.assertTrue(policy_output(out).startswith("sent to sample-dev (%s)" % place("working")), out)
                else:
                    self.assertEqual(prompted, 0)
                    self.assertTrue(policy_output(out).startswith("queued in herdr-fleet for sample-dev (working"), out)

    def test_now_carries_the_queue_in_front_and_urgent_says_it_cannot_reorder(self):
        self.status_of("w2:p2", "working")
        self.run_cli("send", "sample-dev", "first")
        code, out, _ = self.run_cli("send", "--now", "--urgent", "sample-dev", "second")
        self.assertEqual(code, 0)
        self.assertRegex(policy_output(out).strip(), r"^sent to sample-dev \(%s\) with 1 queued, \d+ B; %s$"
                         % (WORKING, re.escape(URGENT_TYPED)))
        (prompt,) = self.prompts_to("sample-dev")
        self.assertLess(prompt.index("second"), prompt.index("first"))  # urgent first, in the mailbox only

    def test_the_flush_goes_only_with_a_delivery_to_an_idle_or_done_seat(self):
        self.status_of("w2:p2", "blocked")
        self.run_cli("send", "sample-dev", "on refusal")
        self.status_of("w2:p2", "working")
        for argv in (["--queue", "sample-dev", "queued"], ["--fyi", "sample-dev", "fyi"],
                     ["--when-ready", "100", "sample-dev", "ready?"]):
            with self.subTest(argv=argv):
                code, out, _ = self.run_cli("send", *argv)
                self.assertEqual(code, 0)
                self.assertEqual(len(policy_output(out).strip().splitlines()), 1, out)
        self.assertEqual(self.argvs("agent", "prompt"), [])
        self.status_of("w2:p2", "done")
        code, out, _ = self.run_cli("send", "sample-dev", "now it is done")
        self.assertRegex(policy_output(out).strip(), r"^sent to sample-dev \(done\) with 4 queued, \d+ B$")

    def test_deliver_leaves_a_working_seats_refusal_mailbox_queued(self):
        self.status_of("w2:p2", "blocked")
        self.run_cli("send", "sample-dev", "on refusal")
        self.status_of("w2:p2", "working")
        for argv in (["deliver"], ["deliver", "sample-dev"], ["watch", "--once"]):
            with self.subTest(argv=argv):
                self.assertEqual(self.run_cli(*argv)[1].strip(), "sample-dev: working, 1 left queued")
        self.assertEqual(self.argvs("agent", "prompt"), [])
        code, out, _ = self.run_cli("deliver", "sample-dev", "--now")
        self.assertRegex(policy_output(out).strip(), r"^delivered 1 to sample-dev \(%s\), \d+ B$" % WORKING)

    def test_a_seat_that_turned_working_before_the_prompt_keeps_the_message(self):
        working = {"id": "cli:agent:get", "result": {"type": "agent_info", "agent": {
            "agent": "claude", "agent_status": "working", "name": "sample-dev", "pane_id": "w2:p2"}}}
        # idle when send looks, working when safe_prompt looks again
        idle = json.loads(json.dumps(working).replace('"working"', '"idle"'))
        self.world["canned"] = [{"prefix": ["agent", "get", "sample-dev"], "stdout": idle, "times": 1},
                                {"prefix": ["agent", "get", "sample-dev"], "stdout": working, "times": 1}]
        code, out, _ = self.run_cli("send", "sample-dev", "ping")
        self.assertEqual(code, 0)
        self.assertRegex(policy_output(out).strip(), r"^queued in herdr-fleet for sample-dev \(working\) %s, position 1 of 1; lands "
                                      r"at its next idle, or now with --now$" % SHORT)
        self.assertEqual(self.argvs("agent", "prompt"), [])


class NoWorkingWaits(WatchCase):
    def test_no_until_working_wait_anywhere(self):
        self.status_of("w2:p2", "blocked")
        self.run_cli("send", "sample-dev", "on refusal")
        self.status_of("w2:p3", "blocked")
        self.run_cli("send", "--queue", "sample-reviewer", "queued")
        self.behave(wait_results={"sample-dev": ["working", "working", "timeout"],
                                  "sample-reviewer": ["working", "timeout", "timeout"]})
        code, out, _ = self.watch(cycles=3)
        self.assertEqual(code, 0)
        waits = self.argvs("agent", "wait")
        self.assertTrue(waits)
        for argv in waits:
            self.assertEqual(argv[3:], WAIT)
        self.assertEqual(self.argvs("agent", "prompt"), [])
        self.assertFalse(any("typing ahead" in line for line in events(out)))


# -- the safety hook -------------------------------------------------------------------------------


class Harness(LiveFleet):
    def test_a_prompt_to_the_pane_the_owner_types_in_fails_every_test(self):
        prompt = ["agent", "prompt", "sample-dev", "hi"]
        for command in (["send", "--now", "sample-dev", "hi"], ["kickoff", "sample-dev"], ["watch"]):
            with self.subTest(command=command):
                with open(self.log_path, "w") as handle:
                    handle.write(json.dumps({"command": command}) + "\n")
                    handle.write(json.dumps({"argv": prompt, "exit": 0,
                                             "info": {"target_status": "idle", "owner_typing": True}}) + "\n")
                with self.assertRaises(AssertionError):
                    self.assert_safety()
        os.remove(self.log_path)

    def test_typed_ahead_allowed_only_for_now(self):
        prompt = ["agent", "prompt", "sample-dev", "hi"]
        self.assertTrue(typed_ahead_allowed(["send", "--now", "sample-dev", "hi"], prompt, {}))
        self.assertTrue(typed_ahead_allowed(["deliver", "sample-dev", "--now"], prompt, {}))
        for command in (["send", "sample-dev", "hi"], ["deliver"], ["watch"], ["kickoff", "sample-dev"],
                        ["restart", "sample-dev"]):
            self.assertFalse(typed_ahead_allowed(command, prompt, {"refusal_boxes": ["sample-dev"]}), command)

    def test_the_fake_records_owner_typing(self):
        self.status_of("w2:p2", "idle")
        world = self.world
        self.assertFalse(fake_herdr.owner_typing(world, "w2:p2"))
        fake_herdr.type_in(world, "w2:p2", "x")
        self.assertFalse(fake_herdr.owner_typing(world, "w2:p2"))  # not focused
        fake_herdr.focus(world, "w2:p2")
        self.assertTrue(fake_herdr.owner_typing(world, "w2:p2"))
        fake_herdr.type_in(world, "w2:p2", "")
        self.assertFalse(fake_herdr.owner_typing(world, "w2:p2"))
        fake_herdr.show_dialog(world, "w2:p2")
        self.assertTrue(fake_herdr.owner_typing(world, "w2:p2"))
        self.assertFalse(fake_herdr.owner_typing(world, CALLER_PANE))


if __name__ == "__main__":
    unittest.main()
