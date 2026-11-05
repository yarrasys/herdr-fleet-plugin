"""Design section 35 (version 0.19.0): provider tiers and failover on a limit.

35.1: ``[tiers.NAME]`` tables and tier seats (``tier`` with ``prefer``, ``extra_args``, ``quit``) and
the effective kind and args every reader sees. 35.2: a limit message on a seat's screen at the end
of a turn (and on a seat stuck ``working``, the Kimi case) is recorded in the state file, held
through the watcher's hold, notified once per episode and ended by a swap, the reset time or
``deliver NAME``; Codex's allowance is read from a rollout and warned about at
``failover_at_percent``. 35.3: ``swap NAME --fallback`` and the way back. 35.4: the watcher's new
reads, which ``tests/support.py`` checks like every other call.

Every identifier here is synthetic; the rollout fixtures hold numbers only."""

from __future__ import annotations

import datetime
import json
import os
import re
import unittest
from unittest import mock

import fake_herdr
from support import ARGS, FakeClock, FleetCase, LiveFleet
from herdr_fleet import context_size, fleetfile, limits, messages, seats, watch
from herdr_fleet.fleetfile import FleetError, Variant, apply_failover, load_fleet
from herdr_fleet.inbox import CUR, NEW
from test_inbox import box, log_entries, put
from test_section24 import Strip, text
from test_section25 import PageCase
from test_watch import WatchCase, events
from herdr_fleet import board as B

CLAUDE = ["--model", "claude-opus-5-5"]
CODEX = ["-m", "gpt-6-astra"]
KIMI = ["-m", "kimi-code/k3.1-pro", "-y"]


def claude_limit(now=None):
    """Keep a recurring daily reset comfortably ahead of the wall-clock cases too."""
    reset = ((now or limits.inbox.now_utc()) + datetime.timedelta(hours=6)).astimezone()
    return "Claude usage limit reached. Your limit will reset at %d:%02d%s." % (
        reset.hour % 12 or 12, reset.minute, "pm" if reset.hour >= 12 else "am")


def codex_limit(now=None):
    """A provider-format reset two days after the test's clock, never a dated deadline."""
    reset = ((now or limits.inbox.now_utc()).astimezone() + datetime.timedelta(days=2)).replace(
        second=0, microsecond=0)
    months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    suffix = "th" if 11 <= reset.day <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(reset.day % 10, "th")
    return "You've hit your usage limit. Try again at %s %d%s, %d %d:%02d %s." % (
        months[reset.month - 1], reset.day, suffix, reset.year, reset.hour % 12 or 12, reset.minute,
        "PM" if reset.hour >= 12 else "AM")


CLAUDE_LIMIT = claude_limit()
CODEX_LIMIT = codex_limit()
ALLOWANCE_RESET = int((limits.inbox.now_utc() + datetime.timedelta(days=7)).timestamp())
NEXT_ALLOWANCE_RESET = ALLOWANCE_RESET + 7 * 86400
KIMI_LIMIT = ("Error: [provider.api_error] 429 You've reached your 5-hour usage limit. Your quota will reset "
              "when the current 5-hour window ends.")
SID = "00000000-0000-4000-8000-000000000035"

TIERS = '''
workspace = "sample"

[tiers.strong]
claude = ["--model", "claude-opus-5-5"]
codex  = { args = ["-m", "gpt-6-astra"], quit = "/quit" }
kimi   = ["-m", "kimi-code/k3.1-pro", "-y"]

[[agents]]
name = "sample-orchestrator"
tier = "strong"
prefer = ["claude", "codex", "kimi"]

[[agents]]
name = "sample-dev"
tier = "strong"
prefer = ["claude", "kimi"]
extra_args = { kimi = ["--add-dir", "../shared"] }

[[agents]]
name = "sample-reviewer"
kind = "codex"
args = ["-m", "gpt-6-astra"]
'''


def temp_fleet(text):
    import shutil
    import tempfile
    folder = tempfile.mkdtemp(prefix="fleet-tiers-")
    os.makedirs(os.path.join(folder, ".herdr"))
    path = os.path.join(folder, ".herdr", "fleet.toml")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return folder, path, lambda: shutil.rmtree(folder, ignore_errors=True)


class Parsing(unittest.TestCase):
    def load(self, text):
        folder, path, cleanup = temp_fleet(text)
        self.addCleanup(cleanup)
        return load_fleet(path)

    def errors(self, text):
        with self.assertRaises(FleetError) as caught:
            self.load(text)
        return "\n".join(caught.exception.errors)

    def test_a_tier_seat_has_the_primarys_kind_args_and_quit(self):
        fleet = self.load(TIERS)
        lead, dev, plain = fleet.seats
        self.assertEqual((lead.kind, lead.args, lead.quit, lead.primary), ("claude", CLAUDE, None, "claude"))
        self.assertEqual((lead.tier, lead.prefer), ("strong", ["claude", "codex", "kimi"]))
        self.assertEqual(dev.variants["kimi"].args, KIMI + ["--add-dir", "../shared"])
        self.assertEqual(lead.variants["codex"], Variant(CODEX, "/quit"))
        self.assertEqual((plain.kind, plain.prefer, plain.tier), ("codex", [], None))
        self.assertEqual(plain.kinds(), ["codex"])
        self.assertEqual(fleet.kinds(), ["claude", "codex", "kimi"])
        self.assertEqual(fleet.failover_at_percent, 95)

    def test_use_switches_kind_args_and_quit_together(self):
        lead = self.load(TIERS).seats[0]
        self.assertTrue(lead.use("codex"))
        self.assertEqual((lead.kind, lead.args, lead.quit), ("codex", CODEX, "/quit"))
        self.assertTrue(lead.use("kimi"))
        self.assertEqual((lead.kind, lead.args, lead.quit), ("kimi", KIMI, None))
        self.assertFalse(lead.use("gemini"))
        self.assertEqual(lead.kind, "kimi")

    def test_the_seats_quit_overrides_the_tiers_for_every_kind(self):
        fleet = self.load(TIERS.replace('prefer = ["claude", "kimi"]', 'prefer = ["claude", "codex"]\nquit = "/bye"')
                          .replace('extra_args = { kimi = ["--add-dir", "../shared"] }\n', ""))
        dev = fleet.seats[1]
        self.assertEqual({k: v.quit for k, v in dev.variants.items()}, {"claude": "/bye", "codex": "/bye"})

    def test_failover_at_percent(self):
        self.assertEqual(self.load(TIERS + "").failover_at_percent, 95)
        fleet = self.load('failover_at_percent = 90\n' + TIERS)
        self.assertEqual(fleet.failover_at_percent, 90)
        for bad in ('"high"', "0", "101", "true", "9.5"):
            self.assertIn("failover_at_percent", self.errors("failover_at_percent = %s\n" % bad + TIERS))

    def test_a_seat_uses_kind_and_args_or_tier_and_prefer_never_both(self):
        for extra in ('kind = "claude"', 'args = ["-x"]'):
            text = TIERS.replace('prefer = ["claude", "kimi"]', 'prefer = ["claude", "kimi"]\n' + extra)
            self.assertIn("cannot be combined with tier", self.errors(text))

    def test_tier_errors(self):
        self.assertIn("is not a tier in this file", self.errors(TIERS.replace('tier = "strong"\nprefer = ["claude", "kimi"]',
                                                                             'tier = "weak"\nprefer = ["claude"]')))
        self.assertIn("agents[1] (sample-dev).prefer: required", self.errors(TIERS.replace('prefer = ["claude", "kimi"]\n', "")))
        self.assertIn("'gemini' is not a kind of tier strong",
                      self.errors(TIERS.replace('prefer = ["claude", "kimi"]', 'prefer = ["claude", "gemini"]')))
        self.assertIn("a kind is listed twice",
                      self.errors(TIERS.replace('prefer = ["claude", "kimi"]', 'prefer = ["claude", "claude"]')))
        self.assertIn("extra_args: 'codex' is not in prefer",
                      self.errors(TIERS.replace('extra_args = { kimi', 'extra_args = { codex = ["-x"], kimi')))
        self.assertIn("tier: required with prefer or extra_args",
                      self.errors(TIERS.replace('tier = "strong"\nprefer = ["claude", "kimi"]', 'prefer = ["claude"]')))

    def test_tier_table_errors(self):
        self.assertIn("tiers.strong.claude: must be an array of strings",
                      self.errors(TIERS.replace('claude = ["--model", "claude-opus-5-5"]', 'claude = "opus"')))
        self.assertIn("tiers.strong.claude: args must be a list of strings",
                      self.errors(TIERS.replace('claude = ["--model", "claude-opus-5-5"]', 'claude = [1]')))
        self.assertIn("unknown key 'tool'",
                      self.errors(TIERS.replace('claude = ["--model", "claude-opus-5-5"]',
                                                'claude = { args = [], tool = 1 }')))
        self.assertIn("tiers: must be a table", self.errors('tiers = 3\n' + TIERS.split("[tiers.strong]")[0]
                                                           + '[[agents]]\nname = "a"\nkind = "claude"\n'))

    def test_unknown_kinds_are_the_kind_check_of_the_operations_that_start_agents(self):
        fleet = self.load(TIERS)
        self.assertEqual(fleetfile.check_kinds(fleet, ["claude", "codex", "kimi"]), [])
        problems = fleetfile.check_kinds(fleet, ["claude", "codex"])
        self.assertEqual(len(problems), 2)  # kimi in both prefer lists
        self.assertTrue(all("prefer" in p and "'kimi'" in p for p in problems))
        unused = self.load(TIERS.replace('kimi   = ["-m", "kimi-code/k3.1-pro", "-y"]',
                                         'kimi   = ["-m", "kimi-code/k3.1-pro", "-y"]\nzzz = []')
                           .replace('prefer = ["claude", "codex", "kimi"]', 'prefer = ["claude", "codex"]')
                           .replace('prefer = ["claude", "kimi"]', 'prefer = ["claude", "codex"]')
                           .replace('extra_args = { kimi = ["--add-dir", "../shared"] }\n', ""))
        self.assertTrue(any("tiers.strong.zzz" in p for p in fleetfile.check_kinds(unused, ["claude", "codex"])))

    def test_apply_failover_switches_the_seat_and_ignores_a_stale_kind(self):
        fleet = self.load(TIERS)
        lead, dev, plain = fleet.seats
        switched = apply_failover(fleet, {"sample-orchestrator": {"failover": {"from": "claude", "to": "codex"}},
                                          "sample-dev": {"failover": {"from": "claude", "to": "gemini"}},
                                          "sample-reviewer": {"failover": {"from": "codex", "to": "kimi"}}})
        self.assertEqual(switched, ["sample-orchestrator"])
        self.assertEqual((lead.kind, lead.args, lead.quit), ("codex", CODEX, "/quit"))
        self.assertEqual(dev.kind, "claude")
        self.assertEqual(plain.kind, "codex")


def screen(kind, line, **kwargs):
    return fake_herdr.limit_screen(kind, line, **kwargs)


class LimitLines(unittest.TestCase):
    def test_each_kinds_pattern_matches_what_was_seen_live(self):
        credit = "Credit balance too low \u00b7 Add funds: https://example.test/billing"
        self.assertEqual(limits.limit_line("claude", screen("claude", credit)), credit)  # the real layout of 2 Nov
        self.assertEqual(limits.limit_line("claude", screen("claude", CLAUDE_LIMIT)), CLAUDE_LIMIT)
        self.assertIn("Credit balance is too low", limits.limit_line("claude", screen("claude", "Credit balance is too low")))
        self.assertEqual(limits.limit_line("codex", screen("codex", CODEX_LIMIT)), CODEX_LIMIT)
        self.assertEqual(limits.limit_line("kimi", screen("kimi", KIMI_LIMIT)), KIMI_LIMIT)  # error, wrap, hint, box
        self.assertTrue(limits.limit_line("kimi", screen("kimi", "Error: [provider.api_error] 429 rate limited")))
        for line in ("usage limit reached. Your limit will reset at 3pm", "5-hour limit reached \u00b7 resets 3pm",
                     "Weekly limit reached \u00b7 resets Mon 9am"):  # documented wording, not seen live (35.5 item 18)
            self.assertEqual(limits.limit_line("claude", screen("claude", line)), line)

    def test_a_screen_without_a_limit_message_matches_nothing(self):
        for kind in ("claude", "codex", "kimi"):
            self.assertIsNone(limits.limit_line(kind, fake_herdr.prompt_screen(kind)))
            self.assertIsNone(limits.limit_line(kind, screen(kind, "Done. 3 files changed")))
        self.assertIsNone(limits.limit_line("agy", screen("claude", CLAUDE_LIMIT)))  # no patterns: never read
        self.assertFalse(limits.has_patterns("agy"))
        self.assertTrue(all(limits.has_patterns(k) for k in ("claude", "codex", "kimi")))

    def test_prose_that_mentions_a_usage_limit_does_not_count(self):
        # an assistant bullet, an echoed prompt, and a plain sentence: even within the six lines, none starts with
        # the provider's wording
        for kind in ("claude", "codex", "kimi"):
            self.assertIsNone(limits.limit_line(kind, screen(kind, "Done", prose="The usage limit is hit at 5 hours")))
        near = ["We talked about the usage limit earlier"] + ["line %d of the build output" % n for n in range(5)]
        self.assertIsNone(limits.limit_line("claude", "\n".join(near + ["", fake_herdr.RULE, "\u276f", fake_herdr.RULE])))
        for line in ("\u276f what is the usage limit?", "\u25cf usage limit docs"):
            self.assertIsNone(limits.limit_line("claude", "\n".join([line, fake_herdr.RULE, "\u276f", fake_herdr.RULE])))

    def test_the_reviewers_false_positive_screens_do_not_match(self):
        rule, prompt = fake_herdr.RULE, ["\u273b Worked for 2m", fake_herdr.RULE, "\u276f ", fake_herdr.RULE]
        screens = {
            "a) a later line of a multi-line answer": ("claude", [
                "\u23fa Review done. The Kimi seats failed with", "  You've reached your 5-hour usage limit, so",
                "  they stayed working."] + prompt),
            "b) a tool result quoting a grep hit": ("claude", [
                "\u23fa Bash(grep -n 'usage limit' herdr_fleet/seats.py)",
                "  \u23bf  85:    \"claude\": [r\"usage limit\", ...],", "\u23fa Found it."] + prompt),
            "c) a list item": ("claude", ["  - limits.py: the usage limit check is fine"] + prompt),
            "c) a table row": ("claude", ["  \u2502 claude \u2502 usage limit \u2502"] + prompt),
            "d) kimi prose above the box": ("kimi", [
                "  The builder says: You've reached your 5-hour usage limit", "",
                "\u256d" + "\u2500" * 30 + "\u256e", "\u2502 > ", "\u2570" + "\u2500" * 30 + "\u256f"]),
            "e) a codex continuation line": ("codex", [
                "\u2022 Done. Note that when Codex says", "  you've hit your usage limit, swap it.", "",
                rule, "\u203a Ask Codex to do anything", rule]),
            "claude credit balance quoted mid-line": ("claude", [
                "  see: Credit balance too low is what Claude prints"] + prompt),
            "codex without its marker": ("codex", ["  You've hit your usage limit.", "", rule, "\u203a", rule]),
        }
        for label, (kind, rows) in screens.items():
            self.assertIsNone(limits.limit_line(kind, "\n".join(rows) + "\n"), label)

    def test_the_real_layouts_form_a_limit_block(self):
        for kind, line in (("claude", CLAUDE_LIMIT), ("kimi", KIMI_LIMIT), ("codex", CODEX_LIMIT)):
            self.assertTrue(limits.limit_block(kind, screen(kind, line)), kind)
            self.assertTrue(limits.limit_block(kind, screen(kind, line, prose="earlier prose")), kind)
        self.assertEqual(limits.ABOVE_INPUT, 6)

    def test_a_pattern_needs_the_start_of_the_line_after_its_marker_and_spaces(self):
        rows = lambda text: "\n".join([text, "", fake_herdr.RULE, "\u276f", fake_herdr.RULE])  # noqa: E731
        for text in ("  \u23bf  Credit balance too low", "     Credit balance is too low \u00b7 Add funds",
                     "\u2502 Usage limit reached \u2502"):
            self.assertTrue(limits.limit_line("claude", rows(text)), text)
        self.assertIsNone(limits.limit_line("claude", rows("Anthropic: Credit balance too low")))

    def test_a_limit_line_below_the_input_or_without_an_input_area_counts_nothing(self):
        below = "\n".join(["Done", fake_herdr.RULE, "\u276f", fake_herdr.RULE, "  " + CLAUDE_LIMIT])
        self.assertIsNone(limits.limit_line("claude", below))
        self.assertIsNone(limits.limit_line("claude", "Allow Bash?\n1. Yes\n2. No\n" + CLAUDE_LIMIT))  # a dialog
        self.assertIsNone(limits.limit_line("claude", CLAUDE_LIMIT))

    def test_the_input_line_empty_or_not(self):
        self.assertTrue(limits.input_empty("claude", screen("claude", CLAUDE_LIMIT)))
        self.assertTrue(limits.input_empty("kimi", screen("kimi", KIMI_LIMIT)))
        self.assertTrue(limits.input_empty("codex", screen("codex", CODEX_LIMIT)))  # the placeholder is no text
        for kind in ("claude", "kimi", "codex"):
            self.assertFalse(limits.input_empty(kind, screen(kind, "x usage limit", typed="half typed")), kind)
        self.assertFalse(limits.input_empty("claude", "no input area here"))

    def test_escapes_and_length_are_cleaned(self):
        line = limits.limit_line("claude", screen("claude", "\x1b[31mClaude usage limit reached " + "x" * 500 + "\x1b[0m"))
        self.assertNotIn("\x1b", line)
        self.assertEqual(len(line), limits.LINE_MAX)

    def test_reset_times(self):
        now = datetime.datetime(2026, 11, 2, 10, 41).astimezone()
        later = limits.parse_reset("limit reached, resets 11:05am", now)
        self.assertEqual((later.hour, later.minute, later.date()), (11, 5, now.date()))
        tomorrow = limits.parse_reset("limit reached, resets 9am", now)
        self.assertEqual(tomorrow.date(), (now + datetime.timedelta(days=1)).date())
        dated = limits.parse_reset(codex_limit(now), now)
        self.assertEqual(dated, now + datetime.timedelta(days=2))
        self.assertIsNone(limits.parse_reset(KIMI_LIMIT, now))
        self.assertIsNone(limits.parse_reset("resets 25pm", now))

    def test_limit_fixtures_follow_the_test_clock_across_years(self):
        for now in (datetime.datetime(2035, 12, 31, 23, 59, tzinfo=datetime.timezone.utc),
                    datetime.datetime(2098, 4, 9, 8, 44, tzinfo=datetime.timezone.utc)):
            with self.subTest(now=now), mock.patch.object(limits.inbox, "now_utc", return_value=now):
                line = codex_limit()
                self.assertEqual(limits.parse_reset(line, now), now.astimezone() + datetime.timedelta(days=2))
                self.assertTrue(limits.counts(limits.new_record("codex", line, now), now))
                claude = limits.new_record("claude", claude_limit(), now)
                self.assertTrue(limits.counts(claude, now + datetime.timedelta(minutes=2)))

    def test_reset_time_with_a_named_zone_keeps_its_clock_hour(self):
        now = datetime.datetime(2026, 11, 2, 0, 0, tzinfo=datetime.timezone.utc)
        reset = limits.parse_reset(
            "Claude usage limit reached. Your limit will reset at 3pm (Australia/Melbourne).", now)
        self.assertEqual((reset.hour, reset.minute), (15, 0))

    def test_kimis_record_carries_its_5_hour_window(self):
        now = datetime.datetime(2026, 11, 2, 10, 41, tzinfo=datetime.timezone.utc)
        record = limits.new_record("kimi", KIMI_LIMIT, now)
        self.assertEqual(record["resets_hint"], "5h window")
        self.assertEqual(limits.reset_time(record) - now, datetime.timedelta(hours=5))
        named = limits.new_record("claude", CLAUDE_LIMIT, now)
        self.assertNotIn("resets_hint", named)
        self.assertIsNotNone(limits.reset_time(named))
        bare = limits.new_record("claude", "Credit balance too low", now)
        self.assertNotIn("resets_at", bare)

    def test_holds_counts_and_candidates(self):
        now = datetime.datetime(2026, 11, 2, 10, 41, tzinfo=datetime.timezone.utc)
        record = limits.new_record("kimi", KIMI_LIMIT, now)
        self.assertTrue(limits.holds(record, "kimi", now))
        self.assertFalse(limits.holds(record, "claude", now))  # the seat was swapped to another kind
        self.assertTrue(limits.counts(record, now))
        self.assertFalse(limits.counts(record, now + datetime.timedelta(hours=5)))  # reset
        self.assertFalse(limits.counts(dict(record, released="now"), now))
        bare = {"kind": "claude", "line": "x", "at": "2026-11-02T10:41:00+00:00"}
        self.assertTrue(limits.holds(bare, "claude", now + datetime.timedelta(days=30)))  # no reset time known
        seat = load_seat()
        self.assertEqual(limits.fallback_candidates(seat, {}), ["codex", "kimi"])
        self.assertEqual(limits.fallback_candidates(seat, {"codex": ["x"]}), ["kimi"])
        seat.use("codex")
        self.assertEqual(limits.fallback_candidates(seat, {}), ["kimi", "claude"])
        seat.use("kimi")
        self.assertEqual(limits.fallback_candidates(seat, {"claude": ["x"]}), ["codex"])
        self.assertEqual(limits.fallback_candidates(seat, {"claude": ["x"], "codex": ["y"]}), [])

    def test_replaced_remembers_the_other_kinds_that_still_count(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        first = limits.new_record("claude", "Credit balance too low", now)
        entry = {"limited": first}
        second = limits.new_record("codex", CODEX_LIMIT, now)
        fields = limits.replaced(entry, second, now)
        self.assertEqual(fields["limited"], second)
        self.assertEqual([r["kind"] for r in fields["limited_also"]], ["claude"])
        self.assertEqual(sorted(limits.kinds_limited({"s": fields}, now)), ["claude", "codex"])
        again = limits.replaced(fields, limits.new_record("claude", "Credit balance too low", now), now)
        self.assertEqual([r["kind"] for r in again["limited_also"]], ["codex"])


def load_seat():
    folder, path, cleanup = temp_fleet(TIERS)
    try:
        return load_fleet(path).seats[0]
    finally:
        cleanup()


class TierLive(LiveFleet):
    """The three-seat fleet of the other tests with the first two seats on a tier."""

    fleet_text = TIERS

    def entry(self, name):
        return self.read_state()["agents"][name]

    def put_state(self, name, **fields):
        state = self.read_state()
        state["agents"][name].update(fields)
        self.write_state(state)


class EffectivePair(TierLive):
    def test_up_starts_a_missing_tier_seat_with_the_primarys_arguments(self):
        del self.world["panes"]["w2:p2"]
        code, out, err = self.run_cli("up", "--no-kickoff", "--no-watch")
        self.assertEqual(code, 0, out + err)
        (start,) = self.argvs("agent", "start")
        self.assertEqual(start[:6], ["agent", "start", "sample-dev", "--kind", "claude", "--pane"])
        self.assertEqual(start[start.index("--") + 1:], CLAUDE)
        self.assertEqual(self.entry("sample-dev")["kind"], "claude")

    def test_a_failed_over_seat_starts_with_the_kind_it_was_swapped_to(self):
        del self.world["panes"]["w2:p2"]
        self.put_state("sample-dev", failover={"from": "claude", "to": "kimi", "at": "t", "reason": "x"})
        code, out, err = self.run_cli("up", "--no-kickoff", "--no-watch")
        self.assertEqual(code, 0, out + err)
        (start,) = self.argvs("agent", "start")
        self.assertEqual(start[3:5], ["--kind", "kimi"])
        self.assertEqual(start[start.index("--") + 1:], KIMI + ["--add-dir", "../shared"])

    def test_restart_uses_the_failed_over_kind_and_quit(self):
        fake_herdr.put_agent(self.world, "w2:p1", "codex", "sample-orchestrator", args=CODEX)
        self.put_state("sample-orchestrator", kind="codex", args=CODEX,
                       failover={"from": "claude", "to": "codex", "at": "t", "reason": "x"})
        code, out, err = self.run_cli("restart", "sample-orchestrator")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.prompts_to("sample-orchestrator")[0], "/quit")  # the tier's codex quit
        (start,) = self.argvs("agent", "start")
        self.assertEqual(start[3:5], ["--kind", "codex"])
        self.assertEqual(start[start.index("--") + 1:], CODEX)

    def test_status_shows_failed_over_instead_of_a_drift(self):
        fake_herdr.put_agent(self.world, "w2:p1", "codex", "sample-orchestrator", args=CODEX)
        self.put_state("sample-orchestrator", kind="codex", args=CODEX,
                       failover={"from": "claude", "to": "codex", "at": "t", "reason": "x"})
        code, out, err = self.run_cli("status", "--json")
        self.assertEqual(code, 0, out + err)
        seats = {s["name"]: s for s in json.loads(out)["seats"]}
        lead = seats["sample-orchestrator"]
        self.assertEqual(lead["drift"], ["failed over from claude"])
        self.assertEqual((lead["kind"], lead["live_kind"], lead["tier"], lead["prefer"]),
                         ("codex", "codex", "strong", ["claude", "codex", "kimi"]))
        self.assertEqual(lead["failover"]["to"], "codex")
        self.assertEqual(seats["sample-dev"]["drift"], [])
        self.assertNotIn("tier", seats["sample-reviewer"])
        code, out, _ = self.run_cli("status")
        self.assertIn("failed over from claude", out)

    def test_doctor_names_the_tiers_and_the_quit_of_every_kind(self):
        code, out, err = self.run_cli("doctor")
        self.assertIn("tier sample-orchestrator", out)
        self.assertIn("claude > codex > kimi (tier strong)", out)
        self.assertRegex(out, r"quit sample-orchestrator/codex: /quit \(codex\)")
        self.assertRegex(out, r"quit sample-orchestrator/kimi: /exit \(kimi\)")
        self.assertRegex(out, r"quit sample-reviewer: /quit \(codex\)")
        self.assertRegex(out, r"presence: no prompt glyph for kind kimi")


SCAN = int(watch.LIMIT_SCAN_S / watch.CYCLE_S) + 3   # watcher passes until its first look at the quiet seats


class Watcher(WatchCase, TierLive):
    """The watcher looks at the quiet seats' screens for a limit line (35.6), whatever their queues."""

    def reads(self, name=None):
        return [a for a in self.argvs("agent", "read") if name is None or a[2] == name]

    def notes(self):
        return [(n["title"], n["body"]) for n in self.world["notifications"]]

    def quiet(self, *names, state="idle", minutes=10):
        """These seats have been in ``state`` since this long ago, as the state file remembers."""
        since = limits.inbox.iso_local(limits.inbox.now_utc() - datetime.timedelta(minutes=minutes))
        for name in names:
            self.put_state(name, state_seen={"state": state, "since": since})

    def test_a_clean_screen_is_read_once_per_unchanged_state_and_nothing_is_recorded(self):
        self.quiet("sample-orchestrator", "sample-dev", "sample-reviewer")
        code, out, err = self.watch(cycles=3 * SCAN)  # three scans
        self.assertEqual(code, 0, out + err)
        self.assertEqual(sorted(a[2] for a in self.reads()), ["sample-dev", "sample-orchestrator", "sample-reviewer"])
        self.assertEqual(self.reads("sample-dev"), [["agent", "read", "sample-dev", "--lines", "12"]])
        self.assertNotIn("limited", self.entry("sample-dev"))
        self.assertEqual(self.notes(), [])

    def test_a_seat_with_an_empty_queue_is_detected_in_the_claude_layout(self):
        fake_herdr.show_limit(self.world, "w2:p2", "Credit balance too low \u00b7 Add funds: https://example.test/billing")
        self.quiet("sample-dev")
        code, out, err = self.watch(cycles=SCAN)
        self.assertEqual(code, 0, out + err)
        record = self.entry("sample-dev")["limited"]
        self.assertEqual(record["kind"], "claude")
        self.assertTrue(record["line"].startswith("Credit balance too low"))
        ((title, body),) = self.notes()
        self.assertEqual(title, "Fleet: sample-dev hit its claude limit")
        self.assertIn("Run: herdr-fleet swap sample-dev --fallback", body)
        self.assertEqual(self.prompts_to("sample-dev"), [])

    def test_a_seat_with_an_empty_queue_is_detected_in_the_kimi_layout(self):
        self.world["panes"]["w2:p2"]["agent"]["agent"] = "kimi"
        self.put_state("sample-dev", kind="kimi", args=KIMI)
        fake_herdr.show_limit(self.world, "w2:p2", KIMI_LIMIT)
        self.status_of("w2:p2", "working")  # Kimi's limit leaves the seat working (35.2)
        self.quiet("sample-dev", state="working")
        self.behave(agent_not_idle=True)
        code, out, err = self.watch(cycles=SCAN)
        self.assertEqual(code, 0, out + err)
        record = self.entry("sample-dev")["limited"]
        self.assertEqual((record["kind"], record["resets_hint"]), ("kimi", "5h window"))
        self.assertEqual(limits.reset_time(record) - limits.inbox.parse_time(record["at"]), datetime.timedelta(hours=5))
        self.assertEqual(len(self.reads("sample-dev")), 1)  # refused for a working seat ...
        self.assertEqual([a for a in self.argvs("pane", "read") if a[2] == "w2:p2" and a[-1] == "12"][-1:],
                         [["pane", "read", "w2:p2", "--lines", "12"]])  # ... so the pane is read
        self.assertEqual([t for t, _ in self.notes()], ["Fleet: sample-dev hit its kimi limit"])

    def test_prose_that_mentions_a_usage_limit_does_not_hold_or_notify(self):
        put(self.repo, "sample-dev", "Task A")
        self.status_of("w2:p2", "working")
        fake_herdr.show_limit(self.world, "w2:p2", "Done", prose="The usage limit resets every 5 hours")
        fake_herdr.set_screen(self.world, "w2:p1", "\n".join(
            ["We read about the usage limit in the docs"] + ["output line %d" % n for n in range(8)]
            + ["", fake_herdr.RULE, "\u276f", fake_herdr.RULE]))
        self.quiet("sample-dev", "sample-orchestrator", state="working")
        self.status_of("w2:p1", "working")
        self.quiet("sample-orchestrator", state="working")
        self.behave(wait_results={"sample-dev": ["timeout"] * 200})
        code, out, err = self.watch(cycles=SCAN)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(len(self.reads("sample-dev")), 1)
        self.assertEqual(len(self.reads("sample-orchestrator")), 1)
        for name in ("sample-dev", "sample-orchestrator"):
            self.assertNotIn("limited", self.entry(name))
        self.assertEqual(self.notes(), [])

    def test_a_limit_holds_the_mail_of_a_working_seat_and_notifies_once(self):
        put(self.repo, "sample-dev", "Task A")
        self.status_of("w2:p2", "working")
        fake_herdr.show_limit(self.world, "w2:p2", CLAUDE_LIMIT)
        self.quiet("sample-dev", state="working")
        self.behave(wait_results={"sample-dev": ["timeout"] * 400})
        code, out, err = self.watch(cycles=2 * SCAN)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.prompts_to("sample-dev"), [])
        self.assertEqual(len(box(self.repo, "sample-dev", NEW)), 1)
        self.assertEqual(len(self.reads("sample-dev")), 1)  # once per unchanged state
        record = self.entry("sample-dev")["limited"]
        self.assertEqual((record["kind"], record["line"]), ("claude", CLAUDE_LIMIT))
        self.assertTrue(record["resets_at"] and record["at"])
        ((title, body),) = self.notes()
        self.assertEqual(title, "Fleet: sample-dev hit its claude limit")
        self.assertIn(CLAUDE_LIMIT, body)
        self.assertIn("Run: herdr-fleet swap sample-dev --fallback", body)
        lines = events(out)
        self.assertEqual(sum(1 for l in lines if l.startswith("Fleet: sample-dev hit its claude limit")), 1)
        self.assertIn("sample-dev limited (claude): holding 1", " ".join(lines))

    def test_the_first_look_waits_and_a_seat_is_read_again_only_after_its_state_changes(self):
        self.quiet("sample-dev", minutes=2)  # idle for 2 minutes: too fresh
        self.watch(cycles=SCAN)
        self.assertEqual(self.reads("sample-dev"), [])
        self.quiet("sample-dev", minutes=10)
        self.watch(cycles=2 * SCAN)
        self.assertEqual(len(self.reads("sample-dev")), 1)
        self.quiet("sample-dev", state="done", minutes=10)  # a new state, quiet again
        self.status_of("w2:p2", "done")
        self.watch(cycles=SCAN)
        self.assertEqual(len(self.reads("sample-dev")), 2)

    def test_a_seat_without_a_tier_gets_no_fallback_defined(self):
        fake_herdr.show_limit(self.world, "w2:p3", CODEX_LIMIT)
        self.quiet("sample-reviewer")
        code, out, err = self.watch(cycles=SCAN)
        self.assertEqual(code, 0, out + err)
        ((title, body),) = self.notes()
        self.assertEqual(title, "Fleet: sample-reviewer hit its codex limit")
        self.assertTrue(body.endswith("no fallback defined"))
        self.assertEqual(self.entry("sample-reviewer")["limited"]["kind"], "codex")

    def test_when_every_other_kind_is_limited_the_notice_says_no_fallback_is_available(self):
        now = limits.inbox.now_utc()
        self.put_state("sample-orchestrator", limited=limits.new_record("kimi", KIMI_LIMIT, now))
        self.put_state("sample-reviewer", limited=limits.new_record("codex", CODEX_LIMIT, now))
        fake_herdr.show_limit(self.world, "w2:p2", CLAUDE_LIMIT)
        self.quiet("sample-dev")
        self.watch(cycles=SCAN)
        self.assertTrue(self.notes()[0][1].endswith("no fallback available: every other kind in prefer is limited"))

    def test_a_kind_without_limit_patterns_is_never_read(self):
        self.world["panes"]["w2:p2"]["agent"]["agent"] = "gemini"
        self.quiet("sample-dev")
        self.watch(cycles=SCAN)
        self.assertEqual(self.reads("sample-dev"), [])

    def test_blocked_and_unknown_seats_are_never_read_for_a_limit(self):
        self.status_of("w2:p2", "blocked")
        self.quiet("sample-dev", state="blocked")
        self.watch(cycles=SCAN)
        self.assertEqual(self.reads("sample-dev"), [])

    def test_a_screen_that_cannot_be_read_records_nothing_and_stops_nothing(self):
        self.quiet("sample-dev")
        with mock.patch.object(fake_herdr.Sim, "_agent_read",
                               lambda self_, args: fake_herdr._err("agent_not_found", "x")):
            code, out, err = self.watch(cycles=SCAN)
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("limited", self.entry("sample-dev"))

    def hold_ends(self, **record_fields):
        """Record a claude limit on sample-dev (as the watcher would), queue mail, run the watcher."""
        record = limits.new_record("claude", CLAUDE_LIMIT, limits.inbox.now_utc())
        record.update(record_fields)
        self.put_state("sample-dev", limited=record)
        put(self.repo, "sample-dev", "Task A")
        self.behave(wait_results={"sample-dev": ["idle"] * 4})

    def test_a_recorded_limit_holds_the_queue_with_no_waiter_and_no_read(self):
        self.hold_ends(resets_at=limits.inbox.iso_local(limits.inbox.now_utc() + datetime.timedelta(hours=2)))
        code, out, err = self.watch(cycles=3)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.argvs("agent", "wait", "sample-dev"), [])
        self.assertEqual(self.reads("sample-dev"), [])
        self.assertEqual(self.prompts_to("sample-dev"), [])
        self.assertEqual(self.notes(), [])  # nobody is told twice: this episode was notified when recorded
        self.assertEqual(sum(1 for l in events(out) if "limited (claude): holding 1" in l), 1)

    def test_a_swap_ends_the_hold(self):
        self.hold_ends(resets_at=limits.inbox.iso_local(limits.inbox.now_utc() + datetime.timedelta(hours=2)))
        self.put_state("sample-dev", kind="kimi")  # what start_seat records after a swap to another kind
        code, out, err = self.watch(cycles=2)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(len(self.prompts_to("sample-dev")), 1)

    def test_the_reset_time_ends_the_hold_and_is_notified_once(self):
        self.hold_ends(resets_at=limits.inbox.iso_local(limits.inbox.now_utc() - datetime.timedelta(minutes=1)))
        code, out, err = self.watch(cycles=3)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(len(self.prompts_to("sample-dev")), 1)
        titles = [t for t, _ in self.notes() if "limit reset" in t]
        self.assertEqual(titles, ["Fleet: claude limit reset; sample-dev's queue goes out again"])
        self.assertTrue(self.entry("sample-dev")["limited"]["reset_notified"])
        self.watch(cycles=2)  # a restarted watcher does not say it again
        self.assertEqual(len([t for t, _ in self.notes() if "limit reset" in t]), 1)

    def test_the_reset_of_a_failed_over_seats_old_kind_says_which_command_returns_it(self):
        self.put_state("sample-dev", kind="kimi", args=KIMI,
                       failover={"from": "claude", "to": "kimi", "at": "t", "reason": "x"},
                       limited=dict(limits.new_record("claude", CLAUDE_LIMIT, limits.inbox.now_utc()),
                                    resets_at=limits.inbox.iso_local(limits.inbox.now_utc()
                                                                     - datetime.timedelta(minutes=1))))
        self.watch(cycles=2)
        self.assertEqual([t for t, _ in self.notes()], ["Fleet: claude limit reset; swap sample-dev returns it"])

    def test_deliver_name_ends_the_hold_and_delivers(self):
        self.hold_ends()  # no reset time known: held until swapped or released
        code, out, err = self.run_cli("deliver", "sample-dev")
        self.assertEqual(code, 0, out + err)
        self.assertIn("sample-dev: claude limit released; delivering anyway", out)
        self.assertEqual(len(self.prompts_to("sample-dev")), 1)
        self.assertIn("released", self.entry("sample-dev")["limited"])
        put(self.repo, "sample-dev", "Task B")
        code, out, _ = self.watch(cycles=2)  # the watcher waits on it again
        self.assertEqual(len(self.argvs("agent", "wait", "sample-dev")), 1)

    def test_a_limit_after_a_release_is_a_new_episode_and_notifies_again(self):
        self.hold_ends()
        self.run_cli("deliver", "sample-dev")  # released; delivered; the limit line is still on the screen
        fake_herdr.show_limit(self.world, "w2:p2", CLAUDE_LIMIT)
        self.status_of("w2:p2", "working")
        self.quiet("sample-dev", state="working")
        put(self.repo, "sample-dev", "Task B")
        self.behave(wait_results={"sample-dev": ["timeout"] * 400})
        self.watch(cycles=SCAN)
        self.assertEqual(len([t for t, _ in self.notes() if t == "Fleet: sample-dev hit its claude limit"]), 1)
        self.assertNotIn("released", self.entry("sample-dev")["limited"])
        self.assertEqual(len(self.prompts_to("sample-dev")), 1)  # Task B stays queued

    def test_a_second_providers_limit_keeps_the_first_in_mind(self):
        fake_herdr.show_limit(self.world, "w2:p2", CLAUDE_LIMIT)
        self.quiet("sample-dev")
        self.watch(cycles=SCAN)
        self.put_state("sample-dev", kind="kimi")  # swapped; the new session hits its own limit
        self.world["panes"]["w2:p2"]["agent"]["agent"] = "kimi"
        fake_herdr.show_limit(self.world, "w2:p2", KIMI_LIMIT)
        self.quiet("sample-dev")
        self.watch(cycles=SCAN)
        entry = self.entry("sample-dev")
        self.assertEqual((entry["limited"]["kind"], [r["kind"] for r in entry["limited_also"]]), ("kimi", ["claude"]))
        self.assertEqual(sorted(limits.kinds_limited(self.read_state()["agents"])), ["claude", "kimi"])

    def test_deliver_without_names_and_watch_once_leave_a_held_seat_alone(self):
        self.hold_ends()
        code, out, err = self.run_cli("deliver")
        self.assertEqual(code, 0, out + err)
        self.assertIn("sample-dev: limited (claude), 1 left queued; deliver sample-dev releases it", out)
        code, out, err = self.run_cli("watch", "--once")
        self.assertIn("sample-dev: limited (claude), 1 left queued; deliver sample-dev releases it", out)
        self.assertEqual(self.prompts_to("sample-dev"), [])

    def test_the_status_state_cell_and_json_name_the_limit(self):
        self.hold_ends()
        code, out, err = self.run_cli("status")
        self.assertRegex(out, r"sample-dev\s+claude\s+claude\s+w2:p2\s+idle, limited \(claude, since \d\d:\d\d\)")
        code, out, _ = self.run_cli("status", "--json")
        dev = {s["name"]: s for s in json.loads(out)["seats"]}["sample-dev"]
        self.assertEqual(dev["limited"]["kind"], "claude")
        self.assertTrue(dev["limit_holds"])


class LimitedQuit(TierLive):
    """Design 35.6, invariant 11's one new exception: a limited seat herdr reports `working` is quit and
    swapped, but only when a fresh read still shows the limit line and its input is empty."""

    def limited(self, kind="kimi", args=KIMI, name="sample-dev", pane="w2:p2", line=None, working_for=10,
                **screen_kwargs):
        """sample-dev runs ``kind`` (failed over to it), has been `working` for ``working_for`` minutes (negative:
        it started working that long after the limit was recorded) with a limit recorded now and on screen."""
        line = line or (KIMI_LIMIT if kind == "kimi" else CLAUDE_LIMIT)
        fake_herdr.put_agent(self.world, pane, kind, name, status="working", args=args)
        now = limits.inbox.now_utc()
        since = limits.inbox.iso_local(now - datetime.timedelta(minutes=working_for))
        self.put_state(name, kind=kind, args=args, limited=limits.new_record(kind, line, now),
                       state_seen={"state": "working", "since": since},
                       failover={"from": "claude", "to": kind, "at": "t", "reason": "x"} if kind != "claude" else None)
        if kind == "claude":
            state = self.read_state()
            del state["agents"][name]["failover"]
            self.write_state(state)
        fake_herdr.show_limit(self.world, pane, line, **screen_kwargs)

    def test_a_limited_working_seat_is_quit_and_swapped_to_the_next_kind(self):
        self.limited()  # kimi, failed over from claude: the way on is claude again (the order wraps)
        code, out, err = self.run_cli("swap", "sample-dev", "--fallback")
        self.assertEqual(code, 0, out + err)
        self.assertIn("working, but its kimi limit is on screen and its input is empty; it is quit", out)
        self.assertEqual(self.prompts_to("sample-dev")[0], "/exit")
        (start,) = self.argvs("agent", "start")
        self.assertEqual((start[3:5], start[start.index("--") + 1:]), (["--kind", "claude"], CLAUDE))
        failover = self.entry("sample-dev")["failover"]
        self.assertEqual((failover["from"], failover["to"]), ("kimi", "claude"))

    def test_the_same_in_the_claude_layout_and_for_a_plain_swap_restart_and_down(self):
        for argv in (["swap", "sample-dev"], ["restart", "sample-dev"], ["down", "sample-dev"]):
            with self.subTest(argv=argv):
                self.limited(kind="claude", args=CLAUDE)
                if argv[0] == "swap":  # a swap needs drift: the file wants claude with other arguments
                    self.write_fleet(TIERS.replace('claude = ["--model", "claude-opus-5-5"]',
                                                   'claude = ["--model", "claude-opus-6"]'))
                code, out, err = self.run_cli(*argv)
                self.assertEqual(code, 0, out + err)
                self.assertIn("it is quit", out)
                self.assertTrue([p for p in self.prompts_to("sample-dev") if p.startswith("/")])
                self.write_fleet(TIERS)

    def test_a_working_seat_with_text_in_its_input_is_still_refused(self):
        for kind, args in (("kimi", KIMI), ("claude", CLAUDE)):
            with self.subTest(kind=kind):
                self.limited(kind=kind, args=args, typed="half typed")
                code, out, err = self.run_cli("restart", "sample-dev")
                self.assertEqual(code, 3, out + err)
                self.assertIn("agent is working", out)
                self.assertEqual([p for p in self.prompts_to("sample-dev") if p.startswith("/")], [])

    def test_a_working_seat_whose_screen_shows_no_limit_line_is_still_refused(self):
        self.limited()
        fake_herdr.set_screen(self.world, "w2:p2", fake_herdr.prompt_screen("kimi"))  # recorded, but gone from the screen
        code, out, err = self.run_cli("swap", "sample-dev", "--fallback")
        self.assertEqual(code, 3, out + err)
        self.assertIn("agent is working", out)
        self.assertEqual(self.argvs("agent", "start"), [])
        self.assertEqual(self.entry("sample-dev")["failover"]["reason"], "x")  # unchanged

    def test_a_working_seat_with_prose_about_limits_is_still_refused(self):
        self.limited()
        fake_herdr.show_limit(self.world, "w2:p2", "Done", prose="the usage limit is 5 hours")
        code, out, err = self.run_cli("restart", "sample-dev")
        self.assertEqual(code, 3, out + err)
        self.assertEqual(self.argvs("agent", "start"), [])

    def test_a_working_seat_with_no_recorded_limit_is_refused_even_with_a_limit_line_on_screen(self):
        self.limited()
        state = self.read_state()
        del state["agents"]["sample-dev"]["limited"]
        self.write_state(state)
        code, out, err = self.run_cli("restart", "sample-dev")
        self.assertEqual(code, 3, out + err)
        self.assertEqual([p for p in self.prompts_to("sample-dev") if p.startswith("/")], [])

    def test_only_the_limited_seat_is_relaxed(self):
        self.limited()
        self.status_of("w2:p1", "working")  # the orchestrator works too, with nothing recorded for it
        code, out, err = self.run_cli("swap", "sample-dev", "sample-orchestrator", "--fallback")
        self.assertEqual(code, 3, out + err)
        self.assertIn("sample-orchestrator: refused - agent is working", out)
        self.assertEqual(self.prompts_to("sample-orchestrator"), [])
        self.assertEqual([s[2] for s in self.argvs("agent", "start")], ["sample-dev"])

    def test_a_seat_resumed_after_the_limit_was_recorded_is_refused(self):
        # the reviewer's case: the owner fixed /login and typed "continue"; the limit line is still four lines up
        for kind, args in (("claude", CLAUDE), ("kimi", KIMI)):
            with self.subTest(kind=kind):
                self.limited(kind=kind, args=args, working_for=-2)  # working since 2 minutes AFTER the record
                code, out, err = self.run_cli("restart", "sample-dev")
                self.assertEqual(code, 3, out + err)
                self.assertIn("agent is working", out)
                self.assertEqual([p for p in self.prompts_to("sample-dev") if p.startswith("/")], [])

    def test_a_kimi_turn_resumed_in_place_is_not_a_limit_block(self):
        # The error, its wrap and the hint, then the owner's "continue" and a spinner: the resumed turn
        # after the trailer refuses even when the seat has been working since before the record.
        self.limited()
        box = "\u2570" + "\u2500" * 58 + "\u256f"
        rows = ["  " + KIMI_LIMIT, "window ends. To continue now, purchase extra usage or upgrade your plan",
                "  If this persists, run `/export-debug-zip` and attach the archive.", "  continue",
                "  \u280b Thinking\u2026", "", "\u256d" + "\u2500" * 58 + "\u256e", "\u2502 > " + " " * 55 + "\u2502", box]
        fake_herdr.set_screen(self.world, "w2:p2", "\n".join(rows) + "\n")
        self.assertTrue(limits.limit_line("kimi", "\n".join(rows)))  # the watcher's six lines still see it
        self.assertFalse(limits.limit_block("kimi", "\n".join(rows)))
        code, out, err = self.run_cli("restart", "sample-dev")
        self.assertEqual(code, 3, out + err)
        self.assertIn("agent is working", out)
        self.assertEqual([p for p in self.prompts_to("sample-dev") if p.startswith("/")], [])

    def test_a_seat_last_seen_in_another_state_is_refused(self):
        self.limited()
        self.put_state("sample-dev", state_seen={"state": "idle", "since": limits.inbox.iso_local(
            limits.inbox.now_utc() - datetime.timedelta(minutes=10))})
        code, out, err = self.run_cli("restart", "sample-dev")
        self.assertEqual(code, 3, out + err)
        self.put_state("sample-dev", state_seen=None)
        state = self.read_state()
        del state["agents"]["sample-dev"]["state_seen"]
        self.write_state(state)
        code, out, err = self.run_cli("restart", "sample-dev")
        self.assertEqual(code, 3, out + err)
        self.assertEqual([p for p in self.prompts_to("sample-dev") if p.startswith("/")], [])

    def test_a_record_of_another_kind_than_the_live_agents_is_refused(self):
        self.limited()  # a kimi seat, recorded as limited on kimi ...
        self.put_state("sample-dev", kind="claude", limited=limits.new_record("claude", CLAUDE_LIMIT, limits.inbox.now_utc()))
        code, out, err = self.run_cli("restart", "sample-dev")  # ... the state says claude, the live agent is kimi
        self.assertEqual(code, 3, out + err)
        self.assertEqual([p for p in self.prompts_to("sample-dev") if p.startswith("/")], [])

    def test_a_blocked_limited_seat_is_never_quit(self):
        self.limited()
        self.status_of("w2:p2", "blocked")
        code, out, err = self.run_cli("restart", "sample-dev")
        self.assertEqual(code, 3, out + err)
        self.assertEqual(self.argvs("agent", "prompt"), [])

    def test_wait_still_applies_to_an_ordinary_working_seat(self):
        self.status_of("w2:p2", "working")
        code, out, err = self.run_cli("restart", "sample-dev")
        self.assertEqual(code, 3, out + err)
        self.assertIn("pass --wait MS", out)


def rollout(root, percent, resets_at=ALLOWANCE_RESET, window=10080, sid=SID, with_info=False):
    now = limits.inbox.now_utc()
    folder = os.path.join(root, now.strftime("%Y"), now.strftime("%m"), now.strftime("%d"))
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "rollout-%s-%s.jsonl" % (now.strftime("%Y-%m-%dT%H-%M-%S"), sid))
    payload = {"type": "token_count", "info": None,
               "rate_limits": {"limit_id": "synthetic", "primary": {"used_percent": percent, "window_minutes": window,
                                                                    "resets_at": resets_at},
                               "secondary": None, "plan_type": "synthetic"}}
    if with_info:
        payload["info"] = {"last_token_usage": {"total_tokens": 1000}}
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"timestamp": limits.inbox.iso_local(now), "type": "event_msg", "payload": payload}) + "\n")
    return path


class Allowance(WatchCase, TierLive):
    """Codex's allowance (35.2): read from the rollout every 10 minutes, warned at the threshold."""

    def setUp(self):
        super().setUp()
        self.root = os.path.join(self.tmp, "codex-sessions")
        self.env["HERDR_FLEET_CODEX_SESSIONS"] = self.root
        self.world["panes"]["w2:p3"]["agent"]["agent_session"] = {"source": "herdr:codex", "value": SID}
        # a second Codex seat: the warning names every one
        self.world["panes"]["w2:p1"]["agent"]["agent"] = "codex"
        self.world["panes"]["w2:p1"]["agent"]["agent_session"] = {"source": "herdr:codex", "value": SID}

    def pass_time(self, seconds):
        return self.watch(cycles=int(seconds / watch.CYCLE_S) + 2)

    def warnings(self):
        return [(n["title"], n["body"]) for n in self.world["notifications"] if n["title"].startswith("Fleet: Codex")]

    def test_the_first_read_waits_so_a_quiet_watcher_stays_quiet(self):
        rollout(self.root, 99)
        self.watch(cycles=3)
        self.assertEqual(self.warnings(), [])
        self.assertEqual(self.argvs("pane", "read"), [])

    def test_at_the_threshold_one_warning_names_every_codex_seat_and_its_command(self):
        rollout(self.root, 96.5)
        self.pass_time(watch.ALLOWANCE_FIRST_S)
        ((title, body),) = self.warnings()
        self.assertRegex(title, r"^Fleet: Codex at 96% \(resets \w{3} \d\d:\d\d\)$")
        self.assertIn("sample-orchestrator: Run: herdr-fleet swap sample-orchestrator --fallback", body)
        self.assertIn("sample-reviewer: no fallback defined", body)
        recorded = self.read_state()["codex_allowance"]
        self.assertEqual((recorded["used_percent"], recorded["resets_at"], recorded["window_minutes"]),
                         (96.5, ALLOWANCE_RESET, 10080))
        self.assertEqual(recorded["warned_for"], ALLOWANCE_RESET)

    def test_below_the_threshold_nothing_is_said_but_the_reading_is_kept(self):
        rollout(self.root, 94.9)
        self.pass_time(watch.ALLOWANCE_FIRST_S)
        self.assertEqual(self.warnings(), [])
        self.assertEqual(self.read_state()["codex_allowance"]["used_percent"], 94.9)
        self.assertNotIn("warned_for", self.read_state()["codex_allowance"])

    def test_the_threshold_comes_from_the_fleet_file(self):
        rollout(self.root, 91)
        self.write_fleet("failover_at_percent = 90\n" + self.read_fleet())
        self.pass_time(watch.ALLOWANCE_FIRST_S)
        self.assertEqual(len(self.warnings()), 1)

    def readings(self, *percents_and_resets):
        """One watcher run long enough for len(...) reads; each read answers the next reading."""
        script = [{"used_percent": p, "resets_at": r, "window_minutes": 10080, "at": None, "source": "codex-transcript"}
                  for p, r in percents_and_resets]
        calls = []

        def next_reading(agent, env):
            calls.append(self.clock.now)
            return script[min(len(calls), len(script)) - 1]

        with mock.patch.object(context_size, "seat_allowance", next_reading):
            self.watch(cycles=int((watch.ALLOWANCE_FIRST_S + watch.ALLOWANCE_READ_S * len(script)) / watch.CYCLE_S))
        return calls

    def test_one_warning_per_reset_window_and_a_new_window_warns_again(self):
        calls = self.readings((97, ALLOWANCE_RESET), (97, ALLOWANCE_RESET),
                             (12, NEXT_ALLOWANCE_RESET), (98, NEXT_ALLOWANCE_RESET))
        self.assertEqual(len(calls), 4)
        self.assertEqual(len(self.warnings()), 2)  # the first window once, the second window once
        self.assertRegex(self.warnings()[0][0], r"^Fleet: Codex at 97%")
        self.assertRegex(self.warnings()[1][0], r"^Fleet: Codex at 98%")
        recorded = self.read_state()["codex_allowance"]
        self.assertEqual((recorded["used_percent"], recorded["warned_for"]), (98, NEXT_ALLOWANCE_RESET))

    def test_a_drop_below_the_threshold_in_the_same_window_does_not_warn_twice(self):
        self.readings((97, ALLOWANCE_RESET), (60, ALLOWANCE_RESET), (97, ALLOWANCE_RESET))
        self.assertEqual(len(self.warnings()), 1)

    def test_it_is_read_at_most_every_10_minutes_and_one_rollout_per_pass(self):
        calls = self.readings((50, ALLOWANCE_RESET), (51, ALLOWANCE_RESET), (52, ALLOWANCE_RESET))
        self.assertEqual(len(calls), 3)  # one rollout read per pass: the first Codex seat answers for the account
        gaps = [b - a for a, b in zip(calls, calls[1:])]
        self.assertTrue(all(watch.ALLOWANCE_READ_S <= g < watch.ALLOWANCE_READ_S + 2 * watch.CYCLE_S for g in gaps), gaps)
        self.assertGreaterEqual(calls[0] - 1000.0, watch.ALLOWANCE_FIRST_S)

    def test_status_shows_the_recorded_reading_and_context_reads_it_now(self):
        code, out, _ = self.run_cli("status")
        self.assertIn("codex: allowance not read yet", out)
        rollout(self.root, 83)
        code, out, err = self.run_cli("status", "--context")
        self.assertRegex(out, r"codex: 83% of the weekly allowance \(resets \w{3} \d\d:\d\d\)")
        self.pass_time(watch.ALLOWANCE_FIRST_S)
        code, out, _ = self.run_cli("status")
        self.assertRegex(out, r"codex: 83% of the weekly allowance")
        data = json.loads(self.run_cli("status", "--json")[1])
        self.assertEqual(data["codex"]["reading"]["used_percent"], 83)
        self.assertEqual(data["codex"]["seats"], ["sample-orchestrator", "sample-reviewer"])

    def test_a_fleet_without_codex_has_no_codex_line(self):
        self.world["panes"]["w2:p1"]["agent"]["agent"] = "claude"
        self.write_fleet(TIERS.replace('kind = "codex"\nargs = ["-m", "gpt-6-astra"]', 'kind = "claude"'))
        self.world["panes"]["w2:p3"]["agent"]["agent"] = "claude"
        code, out, _ = self.run_cli("status")
        self.assertIsNone(re.search(r"^codex:", out, re.M))

    def test_the_reader_keeps_numbers_only(self):
        path = rollout(self.root, 77)
        record = context_size.last_codex_allowance(path)
        self.assertEqual(sorted(record), ["at", "resets_at", "source", "used_percent", "window_minutes"])
        self.assertIsNone(context_size.seat_allowance({"agent_session": {"source": "herdr:claude", "value": SID}}, self.env))
        self.assertIsNone(context_size.seat_allowance(None, self.env))
        for bad in (True, "77", -1):
            self.assertIsNone(context_size.codex_allowance_record(json.dumps(
                {"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {"primary": {
                    "used_percent": bad}}}}).encode()))
        # the latest valid record wins even when a later event carries none
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": None}}) + "\n")
            handle.write("not json\n")
        self.assertEqual(context_size.last_codex_allowance(path)["used_percent"], 77.0)


class Fallback(TierLive):
    def swap(self, *args):
        return self.run_cli("swap", *args)

    def test_the_next_kind_in_prefer_is_started_with_its_arguments_and_the_state_records_it(self):
        code, out, err = self.swap("sample-dev", "--fallback", "--note", "Pick up the review.")
        self.assertEqual(code, 0, out + err)
        self.assertIn("sample-dev: fallback - claude -> kimi", out)
        self.assertIn("sample-dev: failed over - from claude to kimi; `swap sample-dev` returns it to claude", out)
        self.assertEqual(self.prompts_to("sample-dev")[0], "/exit")
        (start,) = self.argvs("agent", "start")
        self.assertEqual(start[3:5], ["--kind", "kimi"])
        self.assertEqual(start[start.index("--") + 1:], KIMI + ["--add-dir", "../shared"])
        entry = self.entry("sample-dev")
        self.assertEqual((entry["kind"], entry["args"]), ("kimi", KIMI + ["--add-dir", "../shared"]))
        failover = entry["failover"]
        self.assertEqual((failover["from"], failover["to"]), ("claude", "kimi"))
        self.assertTrue(failover["at"] and failover["reason"] == "swap --fallback")
        self.assertEqual(self.prompts_to("sample-orchestrator"), [])  # only that one seat

    def test_the_kickoff_carries_the_failover_note_the_last_request_and_the_git_reminder(self):
        code, out, err = self.run_cli("send", "sample-dev", "Review the parser change\nsecond line",
                                      "--as", "sample-orchestrator")
        self.assertEqual(code, 0, out + err)
        self.put_state("sample-dev", limited=limits.new_record("claude", CLAUDE_LIMIT, limits.inbox.now_utc()))
        code, out, err = self.swap("sample-dev", "--fallback", "--note", "Pick up the review.")
        self.assertEqual(code, 0, out + err)
        kickoff = self.prompts_to("sample-dev")[-1]
        self.assertIn('Failover: sample-dev was running claude; this session runs kimi because claude hit its '
                      'usage limit ("%s").' % CLAUDE_LIMIT, kickoff)
        self.assertRegex(kickoff, r"The last request delivered to sample-dev was #[0-9a-f]{6} from "
                                  r"sample-orchestrator: Review the parser change\n")
        self.assertIn("Run git status: work in progress from the previous session may be in the working tree.",
                      kickoff)
        self.assertLess(kickoff.index("Failover:"), kickoff.index("Pick up the review."))
        self.assertEqual(self.entry("sample-dev")["failover"]["reason"], CLAUDE_LIMIT)

    def test_an_fyi_is_not_the_last_request(self):
        self.run_cli("send", "sample-dev", "Real request", "--as", "sample-orchestrator")
        self.run_cli("send", "sample-dev", "FYI: build green", "--as", "sample-reviewer")
        entries = messages.read(self.repo)
        first = [e for e in entries if e["kind"] == "send" and e["first_line"] == "Real request"][0]
        last = messages.last_request(self.repo, "sample-dev")
        self.assertEqual((last["id"], last["from"], last["first_line"]), (first["id"], "sample-orchestrator", "Real request"))
        self.assertIsNone(messages.last_request(self.repo, "sample-orchestrator"))

    def test_codex_as_the_reason_names_the_percentage(self):
        fake_herdr.put_agent(self.world, "w2:p2", "codex", "sample-dev", args=CODEX)
        self.put_state("sample-dev", kind="codex", args=CODEX)
        state = self.read_state()
        state["codex_allowance"] = {"used_percent": 96.2, "resets_at": ALLOWANCE_RESET, "window_minutes": 10080}
        self.write_state(state)
        self.write_fleet(TIERS.replace('prefer = ["claude", "kimi"]', 'prefer = ["codex", "claude"]')
                         .replace('extra_args = { kimi = ["--add-dir", "../shared"] }\n', ""))
        code, out, err = self.swap("sample-dev", "--fallback")
        self.assertEqual(code, 0, out + err)
        self.assertRegex(self.prompts_to("sample-dev")[-1], r"because codex is at 96% of its allowance \(resets \w{3} \d\d:\d\d\)")

    def test_a_kind_with_a_limit_still_recorded_is_skipped(self):
        now = limits.inbox.now_utc()
        self.put_state("sample-reviewer", limited=limits.new_record("codex", CODEX_LIMIT, now))
        code, out, err = self.swap("sample-orchestrator", "--fallback")  # claude > codex > kimi: codex is limited
        self.assertEqual(code, 0, out + err)
        self.assertIn("claude -> kimi", out)
        self.assertEqual(self.argvs("agent", "start")[0][3:5], ["--kind", "kimi"])

    def test_a_limit_that_was_released_or_has_reset_no_longer_counts(self):
        now = limits.inbox.now_utc()
        past = limits.inbox.iso_local(now - datetime.timedelta(hours=1))
        self.put_state("sample-reviewer", limited=dict(limits.new_record("codex", CODEX_LIMIT, now), resets_at=past))
        code, out, err = self.swap("sample-orchestrator", "--fallback", "--dry-run")
        self.assertIn("claude -> codex", out)
        self.put_state("sample-reviewer", limited=dict(limits.new_record("codex", CODEX_LIMIT, now), released="x"))
        code, out, err = self.swap("sample-orchestrator", "--fallback", "--dry-run")
        self.assertIn("claude -> codex", out)

    def test_the_order_wraps_round_to_the_kinds_before_the_running_one(self):
        fake_herdr.put_agent(self.world, "w2:p1", "kimi", "sample-orchestrator", args=KIMI)
        self.put_state("sample-orchestrator", kind="kimi", args=KIMI,
                       failover={"from": "codex", "to": "kimi", "at": "t", "reason": "x"})
        code, out, err = self.swap("sample-orchestrator", "--fallback")
        self.assertEqual(code, 0, out + err)
        self.assertIn("kimi -> claude", out)  # nothing after kimi: claude first, then codex
        failover = self.entry("sample-orchestrator")["failover"]
        self.assertEqual((failover["from"], failover["to"]), ("kimi", "claude"))

    def test_an_absent_seat_is_started_on_the_next_kind(self):
        del self.world["panes"]["w2:p2"]
        code, out, err = self.swap("sample-dev", "--fallback")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.argvs("agent", "start")[0][3:5], ["--kind", "kimi"])
        self.assertEqual(self.entry("sample-dev")["failover"]["to"], "kimi")

    def test_the_kind_it_runs_is_the_live_agents_when_the_file_disagrees(self):
        fake_herdr.put_agent(self.world, "w2:p2", "kimi", "sample-dev", args=KIMI)  # started by hand
        code, out, err = self.swap("sample-dev", "--fallback")
        self.assertEqual(code, 0, out + err)
        self.assertIn("kimi -> claude", out)

    def test_several_names_are_processed_one_at_a_time(self):
        code, out, err = self.swap("sample-orchestrator", "sample-dev", "--fallback")
        self.assertEqual(code, 0, out + err)
        starts = self.argvs("agent", "start")
        self.assertEqual([(a[2], a[4]) for a in starts], [("sample-orchestrator", "codex"), ("sample-dev", "kimi")])
        for name, to in (("sample-orchestrator", "codex"), ("sample-dev", "kimi")):
            self.assertEqual(self.entry(name)["failover"]["to"], to)
        quits = [(a[2], a[3]) for a in self.argvs("agent", "prompt") if a[3].startswith("/")]
        self.assertEqual(quits, [("sample-orchestrator", "/exit"), ("sample-dev", "/exit")])  # each quit, then its start

    def test_no_names_covers_every_tier_seat_marked_limited(self):
        now = limits.inbox.now_utc()
        self.put_state("sample-dev", limited=limits.new_record("claude", CLAUDE_LIMIT, now))
        self.put_state("sample-reviewer", limited=limits.new_record("codex", CODEX_LIMIT, now))  # not a tier seat
        code, out, err = self.swap("--fallback")
        self.assertEqual(code, 0, out + err)
        self.assertEqual([(a[2], a[4]) for a in self.argvs("agent", "start")], [("sample-dev", "kimi")])  # codex is limited on the reviewer
        self.assertEqual(self.prompts_to("sample-orchestrator"), [])
        self.assertEqual(self.entry("sample-dev")["failover"]["to"], "kimi")

    def test_no_names_and_nothing_limited_does_nothing(self):
        code, out, err = self.swap("--fallback")
        self.assertEqual(code, 0, out + err)
        self.assertIn("no tier seat is marked limited", out)
        self.assertEqual(self.argvs("agent", "prompt"), [])

    def test_no_names_reports_a_limited_seat_with_no_free_kind_and_goes_on(self):
        now = limits.inbox.now_utc()
        self.put_state("sample-dev", limited=limits.new_record("claude", CLAUDE_LIMIT, now))
        self.put_state("sample-orchestrator", limited=limits.new_record("claude", CLAUDE_LIMIT, now))
        self.put_state("sample-reviewer", limited=dict(limits.new_record("kimi", KIMI_LIMIT, now)))
        self.put_state("sample-orchestrator", limited_also=[limits.new_record("codex", CODEX_LIMIT, now)])
        code, out, err = self.swap("--fallback")  # dev: claude > kimi, kimi limited; orchestrator: codex and kimi both
        self.assertEqual(code, 3, out + err)
        self.assertIn("sample-dev: refused - sample-dev: no fallback available: every other kind in prefer is limited", out)
        self.assertIn("sample-orchestrator: refused", out)
        self.assertEqual(self.argvs("agent", "start"), [])

    def test_a_dry_run_changes_nothing(self):
        code, out, err = self.swap("sample-dev", "--fallback", "--dry-run")
        self.assertEqual(code, 0, out + err)
        self.assertIn("claude -> kimi", out)
        self.assertEqual(self.argvs("agent", "start"), [])
        self.assertEqual(self.argvs("agent", "prompt"), [])
        self.assertNotIn("failover", self.entry("sample-dev"))

    def test_usage_errors(self):
        for argv, text in (
                (["sample-reviewer", "--fallback"], "no fallback defined"),
                (["sample-dev", "--fallback", "--handover"], "cannot be combined with --handover"),
                (["nobody", "--fallback"], "not a seat")):
            code, out, err = self.swap(*argv)
            self.assertEqual(code, 2, (argv, out, err))
            self.assertIn(text, err, argv)
        self.assertEqual(self.argvs("agent", "start"), [])
        self.assertEqual(self.argvs("agent", "prompt"), [])

    def test_an_adopted_seat_is_refused(self):
        state = self.read_state()
        del state["agents"]["sample-dev"]["args"]  # no recorded args: herdr-fleet did not start it
        self.write_state(state)
        code, out, err = self.swap("sample-dev", "--fallback")
        self.assertEqual(code, 2)
        self.assertIn("adopted session", err)
        self.assertEqual(self.argvs("agent", "prompt"), [])

    def test_every_other_kind_limited_is_a_usage_error(self):
        now = limits.inbox.now_utc()
        self.put_state("sample-reviewer", limited=limits.new_record("kimi", KIMI_LIMIT, now))
        code, out, err = self.swap("sample-dev", "--fallback")  # claude > kimi, and kimi is limited
        self.assertEqual(code, 2)
        self.assertIn("every other kind in prefer is limited (kimi (resets", err)
        self.put_state("sample-reviewer", limited=limits.new_record("kimi", KIMI_LIMIT, now))
        code, out, err = self.swap("sample-dev", "--fallback")
        self.assertEqual(self.argvs("agent", "prompt"), [])

    def test_a_seat_that_lists_one_kind_has_no_other_to_move_to(self):
        self.write_fleet(TIERS.replace('prefer = ["claude", "kimi"]', 'prefer = ["claude"]')
                         .replace('extra_args = { kimi = ["--add-dir", "../shared"] }\n', ""))
        code, out, err = self.swap("sample-dev", "--fallback")
        self.assertEqual(code, 2)
        self.assertIn("lists no other kind in prefer", err)

    def test_a_blocked_seat_is_refused_and_nothing_is_recorded(self):
        self.status_of("w2:p2", "blocked")
        code, out, err = self.swap("sample-dev", "--fallback")
        self.assertEqual(code, 3, out + err)
        self.assertEqual(self.argvs("agent", "start"), [])
        self.assertNotIn("failover", self.entry("sample-dev"))
        self.assertEqual(self.entry("sample-dev")["kind"], "claude")

    def test_a_start_that_fails_records_no_failover(self):
        self.behave_start_lost()
        code, out, err = self.swap("sample-dev", "--fallback")
        self.assertEqual(code, 1, out + err)
        self.assertNotIn("failover", self.entry("sample-dev"))

    def behave_start_lost(self):
        self.world["behaviors"]["start_lost"] = {"sample-dev": 5}

    def test_a_plain_swap_skips_failed_over_seats_and_says_so(self):
        fake_herdr.put_agent(self.world, "w2:p2", "kimi", "sample-dev", args=KIMI + ["--add-dir", "../shared"])
        self.put_state("sample-dev", kind="kimi", args=KIMI + ["--add-dir", "../shared"],
                       failover={"from": "claude", "to": "kimi", "at": "t", "reason": "x"})
        code, out, err = self.swap()
        self.assertEqual(code, 0, out + err)
        self.assertIn("sample-dev: skipped - failed over from claude to kimi; swap sample-dev returns it to claude", out)
        self.assertEqual(self.argvs("agent", "start"), [])

    def test_swap_name_returns_a_failed_over_seat_to_its_primary(self):
        fake_herdr.put_agent(self.world, "w2:p2", "kimi", "sample-dev", args=KIMI + ["--add-dir", "../shared"])
        self.put_state("sample-dev", kind="kimi", args=KIMI + ["--add-dir", "../shared"],
                       failover={"from": "claude", "to": "kimi", "at": "t", "reason": "x"})
        code, out, err = self.swap("sample-dev")
        self.assertEqual(code, 0, out + err)
        self.assertIn("kind: running kimi, file says claude", out)
        self.assertIn("sample-dev: failover cleared - back on claude", out)
        self.assertEqual(self.prompts_to("sample-dev")[0], "/exit")
        (start,) = self.argvs("agent", "start")
        self.assertEqual((start[3:5], start[start.index("--") + 1:]), (["--kind", "claude"], CLAUDE))
        self.assertNotIn("failover", self.entry("sample-dev"))
        self.assertEqual(self.entry("sample-dev")["kind"], "claude")

    def test_returning_to_a_kind_whose_limit_is_recorded_says_the_hold_returns(self):
        fake_herdr.put_agent(self.world, "w2:p2", "kimi", "sample-dev", args=KIMI)
        record = limits.new_record("claude", CLAUDE_LIMIT, limits.inbox.now_utc())
        self.put_state("sample-dev", kind="kimi", args=KIMI, limited=record,
                       failover={"from": "claude", "to": "kimi", "at": "t", "reason": "x"})
        code, out, err = self.swap("sample-dev")
        self.assertEqual(code, 0, out + err)
        self.assertRegex(out, r"sample-dev: note - its claude limit is still recorded \(resets .*\); the watcher "
                              r"holds its queue until it resets, or `deliver sample-dev` releases it")

    def test_a_returning_seat_that_already_runs_its_primary_just_clears_the_record(self):
        self.put_state("sample-dev", failover={"from": "claude", "to": "kimi", "at": "t", "reason": "x"})
        code, out, err = self.swap("sample-dev")
        self.assertEqual(code, 0, out + err)
        self.assertIn("failover cleared - already runs claude", out)
        self.assertEqual(self.argvs("agent", "start"), [])
        self.assertNotIn("failover", self.entry("sample-dev"))

    def test_a_failed_over_seat_that_could_not_be_returned_stays_failed_over(self):
        fake_herdr.put_agent(self.world, "w2:p2", "kimi", "sample-dev", args=KIMI)
        self.status_of("w2:p2", "blocked")
        self.put_state("sample-dev", kind="kimi", args=KIMI,
                       failover={"from": "claude", "to": "kimi", "at": "t", "reason": "x"})
        code, out, err = self.swap("sample-dev")
        self.assertEqual(code, 3, out + err)
        self.assertEqual(self.entry("sample-dev")["failover"]["to"], "kimi")



class BoardLimits(Strip):
    fleet_text = TIERS

    def record(self, name="sample-dev", kind="claude", line=CLAUDE_LIMIT):
        data = self.read_state()
        data["agents"][name]["limited"] = limits.new_record(kind, line, limits.inbox.now_utc())
        self.write_state(data)

    def test_a_limited_seats_row_and_page_name_the_limit(self):
        self.record()
        put(self.repo, "sample-dev", "Task A")
        result = self.build()
        dev = [r for r in result.rows if r.name == "sample-dev"][0]
        self.assertRegex(dev.limited, r"^limited \(claude, since \d\d:\d\d\)$")
        self.assertEqual(dev.doing, dev.limited)
        self.assertIn(dev.limited, "\n".join(text(B.render(result, 162, 8, True))))
        other = [r for r in result.rows if r.name == "sample-orchestrator"][0]
        self.assertEqual(other.limited, "")

    def test_a_limit_recorded_for_the_kind_it_no_longer_runs_shows_nothing(self):
        self.record(kind="kimi", line=KIMI_LIMIT)  # the seat runs claude: swapped back
        result = self.build()
        self.assertEqual([r.limited for r in result.rows], ["", "", ""])

    def test_the_watcher_holds_a_limited_seats_mail_and_the_board_says_so(self):
        self.record()
        put(self.repo, "sample-dev", "Task A")
        self.world["behaviors"]["wait_results"] = {"sample-dev": ["idle"] * 9}
        code, _, err = self.run_board([], idle=3)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.prompts_to("sample-dev"), [])
        self.assertTrue(any("holding: limited (claude)" in "\n".join(self.screen.text(i))
                            for i in range(len(self.screen.draws))))
        self.assertIn("sample-dev limited (claude): holding 1", self.watch_log())

    def test_d_on_the_board_leaves_a_limited_seat_alone_and_delivers_to_the_rest(self):
        self.record()
        put(self.repo, "sample-dev", "Task A")
        put(self.repo, "sample-reviewer", "Review B")
        self.world["behaviors"]["wait_results"] = {"sample-dev": ["timeout"] * 9, "sample-reviewer": ["timeout"] * 9}
        code, _, err = self.run_board(["d"])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.prompts_to("sample-dev"), [])
        self.assertEqual(len(self.prompts_to("sample-reviewer")), 1)
        self.assertIn("sample-dev: limited (claude), 1 left queued; deliver sample-dev releases it", self.watch_log())


class BoardSeatPage(PageCase):
    fleet_text = TIERS

    def test_d_on_the_seats_own_page_is_deliver_name(self):
        data = self.read_state()
        data["agents"]["sample-dev"]["limited"] = limits.new_record("claude", CLAUDE_LIMIT, limits.inbox.now_utc())
        self.write_state(data)
        self.message("sample-orchestrator", "sample-dev", "a task", box_=NEW, delivery="queued", queued_by="refusal")
        self.page(["2", "d"])
        self.assertEqual(len(self.prompts_to("sample-dev")), 1)
        self.assertIn("sample-dev: claude limit released; delivering anyway", self.watch_log())
        self.assertIn("released", self.read_state()["agents"]["sample-dev"]["limited"])


class Plain(unittest.TestCase):
    def test_the_plain_seats_of_older_fleets_are_unchanged(self):
        folder, path, cleanup = temp_fleet(TIERS.split("[tiers.strong]")[0] + '[[agents]]\nname = "a"\nkind = "claude"\n')
        self.addCleanup(cleanup)
        fleet = load_fleet(path)
        seat = fleet.seats[0]
        self.assertEqual((seat.kind, seat.args, seat.tier, seat.prefer, seat.variants), ("claude", [], None, [], {}))
        self.assertFalse(seat.use("codex"))
        self.assertIsNone(seat.quit_for("claude"))
        self.assertEqual(seats.quit_command(seat, "claude", {}), "/exit")
        self.assertEqual(fleet.tiers, {})


if __name__ == "__main__":
    unittest.main()
