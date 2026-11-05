"""Design section 37 (version 0.19.2): real limit screens, fallback starts and FYI guidance."""

from __future__ import annotations

import datetime
import os
import unittest

import fake_herdr
from support import LiveFleet
from herdr_fleet import kickoff, limits, messages
from herdr_fleet.cli import build_parser
from herdr_fleet.inbox import NEW
from test_inbox import box, log_entries
from test_section31 import FleetPair
from test_section35 import CLAUDE, CLAUDE_LIMIT, CODEX_LIMIT, KIMI, KIMI_LIMIT, TIERS, TierLive


def without_margin(screen):
    return "\n".join(line[1:] if line.startswith(" ") else line for line in screen.splitlines()) + "\n"


def insert_before(screen, marker, *rows):
    lines = screen.splitlines()
    index = next(i for i, line in enumerate(lines) if marker in line)
    return "\n".join(lines[:index] + list(rows) + lines[index:]) + "\n"


class LimitBlocks(TierLive):
    def limited(self, kind="kimi", args=KIMI, line=KIMI_LIMIT, **screen_kwargs):
        fake_herdr.put_agent(self.world, "w2:p2", kind, "sample-dev", status="working", args=args)
        now = limits.inbox.now_utc()
        since = limits.inbox.iso_local(now - datetime.timedelta(minutes=10))
        self.put_state("sample-dev", kind=kind, args=args, limited=limits.new_record(kind, line, now),
                       state_seen={"state": "working", "since": since},
                       failover={"from": "claude", "to": kind, "at": "t", "reason": "x"})
        fake_herdr.show_limit(self.world, "w2:p2", line, **screen_kwargs)

    def test_kimi_wraps_accept_the_live_margin_and_no_margin(self):
        for wrap in (1, 2, 3):
            for margin in (True, False):
                with self.subTest(wrap=wrap, margin=margin):
                    screen = fake_herdr.limit_screen("kimi", KIMI_LIMIT, wrap=wrap)
                    if not margin:
                        screen = without_margin(screen)
                    self.assertTrue(limits.limit_line("kimi", screen))
                    self.assertTrue(limits.limit_block("kimi", screen))

    def test_the_three_line_kimi_screen_is_quit_and_swapped(self):
        self.limited(wrap=3)
        code, out, err = self.run_cli("swap", "sample-dev", "--fallback")
        self.assertEqual(code, 0, out + err)
        self.assertIn("it is quit", out)
        self.assertEqual(self.argvs("agent", "start")[0][3:5], ["--kind", "claude"])

    def test_a_wrap_indented_as_deep_as_the_error_is_refused(self):
        lines = fake_herdr.limit_screen("kimi", KIMI_LIMIT, wrap=2).splitlines()
        error = next(i for i, line in enumerate(lines) if "provider.api_error" in line)
        lines[error + 1] = "   " + lines[error + 1].lstrip()
        self.assertFalse(limits.limit_block("kimi", "\n".join(lines) + "\n"))

    def test_the_reviewers_spinner_blank_line_and_claude_screens_are_refused(self):
        kimi = fake_herdr.limit_screen("kimi", KIMI_LIMIT, wrap=2)
        self.assertFalse(limits.limit_block("kimi", insert_before(kimi, "If this persists", " \u280b Thinking\u2026")))
        without_hint = "\n".join(line for line in kimi.splitlines() if "If this persists" not in line) + "\n"
        self.assertFalse(limits.limit_block("kimi", insert_before(without_hint, "\u256d", " continued")))
        claude = fake_herdr.limit_screen("claude", CLAUDE_LIMIT)
        self.assertFalse(limits.limit_block("claude", insert_before(claude, "Worked for", "\u2722 Thinking\u2026")))

    def test_claudes_worked_trailer_and_codexs_bare_line_are_accepted(self):
        claude = fake_herdr.limit_screen("claude", CLAUDE_LIMIT).replace("Worked for 3s", "Worked for 2m 3s")
        self.assertTrue(limits.limit_block("claude", claude))
        self.assertTrue(limits.limit_block("codex", fake_herdr.limit_screen("codex", CODEX_LIMIT)))

    def test_the_resumed_kimi_screen_is_refused(self):
        screen = fake_herdr.limit_screen("kimi", KIMI_LIMIT, wrap=2)
        screen = insert_before(screen, "\u256d", " continue", " \u280b Thinking\u2026")
        self.assertTrue(limits.limit_line("kimi", screen))
        self.assertFalse(limits.limit_block("kimi", screen))
        self.limited(wrap=2)
        fake_herdr.set_screen(self.world, "w2:p2", screen)
        code, out, err = self.run_cli("restart", "sample-dev")
        self.assertEqual(code, 3, out + err)
        self.assertEqual([p for p in self.prompts_to("sample-dev") if p.startswith("/")], [])

    def test_a_spinner_prompt_or_assistant_text_inside_the_block_is_refused(self):
        for extra in (" \u273b Thinking\u2026", " > continue", " \u23fa Continuing the turn"):
            with self.subTest(extra=extra):
                screen = fake_herdr.limit_screen("kimi", KIMI_LIMIT, wrap=2)
                self.assertFalse(limits.limit_block("kimi", insert_before(screen, "If this persists", extra)))

    def test_a_continuation_after_the_trailer_is_refused(self):
        screen = fake_herdr.limit_screen("kimi", KIMI_LIMIT, wrap=2)
        self.assertFalse(limits.limit_block("kimi", insert_before(screen, "\u256d", " continued after the hint")))

    def test_text_in_the_input_is_refused(self):
        for kind, line, args in (("kimi", KIMI_LIMIT, KIMI), ("claude", CLAUDE_LIMIT, CLAUDE)):
            with self.subTest(kind=kind):
                self.assertFalse(limits.limit_block(kind, fake_herdr.limit_screen(kind, line, typed="half typed")))
                self.limited(kind=kind, args=args, line=line, typed="half typed")
                code, out, err = self.run_cli("restart", "sample-dev")
                self.assertEqual(code, 3, out + err)
                self.assertEqual([p for p in self.prompts_to("sample-dev") if p.startswith("/")], [])


class FallbackStarts(TierLive):
    def test_a_failed_codex_start_moves_to_claude_and_explains_it(self):
        self.write_fleet(TIERS.replace('prefer = ["claude", "codex", "kimi"]',
                                       'prefer = ["kimi", "codex", "claude"]'))
        fake_herdr.put_agent(self.world, "w2:p1", "kimi", "sample-orchestrator", args=KIMI)
        self.put_state("sample-orchestrator", kind="kimi", args=KIMI,
                       failover={"from": "claude", "to": "kimi", "at": "t", "reason": "x"})
        self.world["behaviors"]["start_lost"] = {"sample-orchestrator": 2}
        code, out, err = self.run_cli("swap", "sample-orchestrator", "--fallback")
        self.assertEqual(code, 0, out + err)
        step = next(line for line in out.splitlines() if "codex did not start" in line)
        self.assertEqual(step, "sample-orchestrator: fallback - codex did not start (the start command did not run "
                               "in w2:p1 twice, or the agent exited at once); trying claude")
        self.assertEqual([a[4] for a in self.argvs("agent", "start")], ["codex", "codex", "claude"])
        kickoff = self.prompts_to("sample-orchestrator")[-1]
        self.assertIn("this session runs claude", kickoff)
        self.assertIn("codex did not start: the start command did not run in w2:p1 twice, or the agent exited at once",
                      kickoff)
        self.assertNotIn("mouse tracking", kickoff)
        self.assertNotIn("run herdr-fleet up", kickoff)
        self.assertEqual(self.entry("sample-orchestrator")["failover"]["to"], "claude")
        self.assertEqual(self.world["notifications"], [])

    def test_every_candidate_failing_reports_each_and_notifies_once(self):
        self.world["behaviors"]["start_lost"] = {"sample-orchestrator": 6}
        code, out, err = self.run_cli("swap", "sample-orchestrator", "--fallback")
        self.assertEqual(code, 1, out + err)
        steps = [line for line in out.splitlines() if ": fallback - " in line and "did not start" in line]
        self.assertEqual(len(steps), 2)
        self.assertTrue(all("mouse tracking" not in line and "run herdr-fleet up" not in line for line in steps))
        self.assertIn("a quit agent may have left mouse tracking", out)
        self.assertIn("run herdr-fleet up sample-orchestrator", out)
        self.assertEqual([a[4] for a in self.argvs("agent", "start")], ["codex", "codex", "kimi", "kimi"])
        self.assertIsNone(self.pane_of("sample-orchestrator"))
        self.assertNotIn("failover", self.entry("sample-orchestrator"))
        self.assertNotIn("limited", self.entry("sample-orchestrator"))
        ((title, body),) = [(n["title"], n["body"]) for n in self.world["notifications"]]
        self.assertEqual(title, "Fleet: sample-orchestrator did not start")
        self.assertIn("codex did not start:", body)
        self.assertIn("kimi did not start:", body)

    def test_a_blocked_first_candidate_stops_without_trying_the_next(self):
        self.world["behaviors"]["start_not_ready"] = ["sample-orchestrator"]
        code, out, err = self.run_cli("swap", "sample-orchestrator", "--fallback")
        self.assertEqual(code, 3, out + err)
        self.assertEqual([a[4] for a in self.argvs("agent", "start")], ["codex"])
        self.assertEqual(self.entry("sample-orchestrator")["failover"]["to"], "codex")
        self.assertEqual([n["title"] for n in self.world["notifications"]],
                         ["Fleet: sample-orchestrator is waiting for you"])

    def test_a_slow_first_candidate_is_waited_for_and_kept(self):
        self.world["behaviors"]["start_slow"] = {"sample-orchestrator": 2}
        code, out, err = self.run_cli("swap", "sample-orchestrator", "--fallback")
        self.assertEqual(code, 0, out + err)
        self.assertEqual([a[4] for a in self.argvs("agent", "start")], ["codex"])
        self.assertEqual(self.entry("sample-orchestrator")["failover"]["to"], "codex")
        self.assertIn("briefed", out)

    def test_a_slow_start_past_the_deadline_keeps_its_notice_and_stops(self):
        self.world["behaviors"]["start_slow"] = {"sample-orchestrator": 100000}
        code, out, err = self.run_cli("swap", "sample-orchestrator", "--fallback")
        self.assertEqual(code, 1, out + err)
        self.assertIn("has not seen it ready after 45 s", out)
        self.assertIn("kickoff queued", out)
        self.assertEqual([a[4] for a in self.argvs("agent", "start")], ["codex"])
        self.assertEqual(self.entry("sample-orchestrator")["failover"]["to"], "codex")
        self.assertEqual([n["title"] for n in self.world["notifications"]],
                         ["Fleet: sample-orchestrator did not start"])

    def test_a_failed_kind_is_not_remembered_for_the_next_seat(self):
        self.write_fleet(TIERS.replace('prefer = ["claude", "kimi"]', 'prefer = ["claude", "codex", "kimi"]'))
        self.world["behaviors"]["start_lost"] = {"sample-orchestrator": 2, "sample-dev": 2}
        code, out, err = self.run_cli("swap", "sample-orchestrator", "sample-dev", "--fallback")
        self.assertEqual(code, 0, out + err)
        self.assertEqual([(a[2], a[4]) for a in self.argvs("agent", "start")], [
            ("sample-orchestrator", "codex"), ("sample-orchestrator", "codex"),
            ("sample-orchestrator", "kimi"), ("sample-dev", "codex"),
            ("sample-dev", "codex"), ("sample-dev", "kimi")])
        self.assertEqual(self.entry("sample-orchestrator")["failover"]["to"], "kimi")
        self.assertEqual(self.entry("sample-dev")["failover"]["to"], "kimi")
        self.assertNotIn("limited", self.entry("sample-orchestrator"))
        self.assertNotIn("limited", self.entry("sample-dev"))


class Wording(unittest.TestCase):
    def test_the_roster_sentence_has_the_new_opening_and_keeps_the_rest(self):
        opening = ('Message a seat with `herdr-fleet send <name> "..."`. Finished work, a verdict or an answer someone '
                   'is waiting for goes as a plain message, even when you want no reply; `--fyi` is only for what '
                   'nobody has to act on, because an FYI never wakes an idle seat. When you have acted on a request, '
                   'report it, or close it with `ack #id` when there is nothing to report, since the plugin cannot see '
                   'what you did.')
        self.assertTrue(kickoff.ROSTER_SENTENCE.startswith(opening + " To control order, queue "))
        self.assertNotIn("reply with `--fyi`", kickoff.ROSTER_SENTENCE)
        self.assertIn("To wait for a reply, run `herdr-fleet inbox --wait --timeout 600000`", kickoff.ROSTER_SENTENCE)

    def test_the_fyi_help_defines_what_the_recipient_has_to_do(self):
        parser = build_parser()
        sub = next(a for a in parser._actions if a.dest == "command")
        action = next(a for a in sub.choices["send"]._actions if a.dest == "fyi")
        self.assertEqual(action.help,
                         'nothing for the recipient to act on: it never wakes an idle seat and goes with its next '
                         'delivery, or expires after fyi_ttl; finished work or a verdict someone waits for is not an '
                         'FYI (text starting with "FYI:" or "FYI " counts too)')


class FyiHint(LiveFleet):
    def ask(self):
        code, out, err = self.run_cli("send", "--as", "sample-orchestrator", "sample-dev", "Please do the work")
        self.assertEqual(code, 0, out + err)
        return log_entries(self.repo)[-1]["id"]

    def report(self, *extra):
        return self.run_cli("send", "--as", "sample-dev", "--fyi", *extra, "sample-orchestrator", "Work finished")

    def assert_hint(self, out, request, state="idle"):
        expected = ("note: sample-orchestrator is %s and an FYI does not wake it; this one answers its #%s. "
                    "If it is waiting for this, run: herdr-fleet deliver sample-orchestrator; otherwise leave it."
                    % (state, request[-6:]))
        self.assertEqual(out.splitlines()[-1], expected)

    def test_an_fyi_answering_an_idle_seats_open_request_gets_the_hint(self):
        request = self.ask()
        code, out, err = self.report()
        self.assertEqual(code, 0, out + err)
        self.assert_hint(out, request)

    def test_the_hint_says_done_for_a_done_seat(self):
        request = self.ask()
        self.status_of("w2:p1", "done")
        code, out, err = self.report()
        self.assertEqual(code, 0, out + err)
        self.assert_hint(out, request, "done")

    def test_no_hint_for_a_working_target(self):
        self.ask()
        self.status_of("w2:p1", "working")
        code, out, err = self.report()
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("an FYI does not wake it", out)

    def test_no_hint_when_a_limit_record_holds_the_queue(self):
        self.ask()
        state = self.read_state()
        state["agents"]["sample-orchestrator"]["limited"] = limits.new_record("claude", CLAUDE_LIMIT)
        self.write_state(state)
        code, out, err = self.report()
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("an FYI does not wake it", out)

    def test_no_hint_when_an_ack_already_settled_the_request(self):
        self.ask()
        code, out, err = self.run_cli("ack", "--as", "sample-dev", "sample-orchestrator")
        self.assertEqual(code, 0, out + err)
        code, out, err = self.report()
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("an FYI does not wake it", out)

    def test_no_hint_when_an_earlier_reply_settled_the_request(self):
        self.ask()
        self.run_cli("send", "--as", "sample-dev", "sample-orchestrator", "Already answered")
        code, out, err = self.report()
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("an FYI does not wake it", out)

    def test_no_hint_when_the_target_never_asked(self):
        code, out, err = self.report()
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("an FYI does not wake it", out)

    def test_no_hint_for_a_plain_send_or_fyi_now(self):
        self.ask()
        code, out, err = self.run_cli("send", "--as", "sample-dev", "sample-orchestrator", "Work finished")
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("an FYI does not wake it", out)
        self.ask()
        code, out, err = self.report("--now")
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("an FYI does not wake it", out)

    def test_an_open_request_without_an_id_uses_the_amendment_text(self):
        messages.append(self.repo, {"time": messages.now_iso(), "from": "sample-orchestrator", "to": "sample-dev",
                                    "kind": "send", "delivery": "sent", "first_line": "Please do the work"})
        code, out, err = self.report()
        self.assertEqual(code, 0, out + err)
        self.assertIn("this one answers its open request. If it is waiting for this", out)

    def test_the_dry_run_prints_the_hint_and_writes_nothing(self):
        request = self.ask()
        before = log_entries(self.repo)
        code, out, err = self.report("--dry-run")
        self.assertEqual(code, 0, out + err)
        self.assert_hint(out, request)
        self.assertEqual(log_entries(self.repo), before)
        self.assertEqual(box(self.repo, "sample-orchestrator", NEW), [])

    def test_the_fyi_stays_queued_and_no_waiter_starts(self):
        self.ask()
        code, out, err = self.report()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(len(box(self.repo, "sample-orchestrator", NEW)), 1)
        self.assertEqual(self.prompts_to("sample-orchestrator"), [])
        code, out, err = self.run_cli("watch", "--once")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.argvs("agent", "wait", "sample-orchestrator"), [])
        self.assertEqual(self.prompts_to("sample-orchestrator"), [])
        self.assertEqual(len(box(self.repo, "sample-orchestrator", NEW)), 1)

    def test_the_printed_deliver_command_keeps_an_explicit_fleet(self):
        self.ask()
        path = os.path.join(self.repo, ".herdr", "fleet.toml")
        code, out, err = self.run_cli("send", "--as", "sample-dev", "--fyi", "--fleet", path,
                                      "sample-orchestrator", "Work finished", cwd=self.tmp)
        self.assertEqual(code, 0, out + err)
        self.assertIn("run: herdr-fleet deliver --fleet %s sample-orchestrator;" % path, out)


class RoutedFyiHint(FleetPair):
    def test_no_hint_for_a_routed_target(self):
        code, out, err = self.run_cli("send", "--as", "peer-lead", "--fleet", self.destination,
                                      "sample-orchestrator", "Please do the work")
        self.assertEqual(code, 0, out + err)
        code, out, err = self.run_cli("send", "--as", "sample-orchestrator", "--fyi", "partner", "Work finished")
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("an FYI does not wake it", out)


if __name__ == "__main__":
    unittest.main()
