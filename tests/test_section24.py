"""Design section 24 (version 0.13.0): the strip as a board.

24.1: ``watch`` draws the board when stdout is a terminal and ``--log`` is absent; ``--log``
or a stdout that is not a terminal prints the lines as before; ``--once`` never draws; in
board mode the lines go to ``.herdr/watch.log`` and the footer. 24.2: ``board.build`` from
the fake herdr. 24.3: ``board.render`` (pure) at the sizes of 24.5. 24.4: the keys,
through ``tui.FakeScreen``, which replays scripted keys and logs each one into the call
log, so ``tests/support.py`` can hold the board to invariant 22: ``agent focus`` from
``watch`` only right after a number or Enter, ``agent read`` from ``watch`` only of a seat
whose state at the last ``agent list`` was ``blocked``, and every prompt rule as before."""

from __future__ import annotations

import datetime
import io
import json
import os
import re
import unittest
from unittest import mock

import fake_herdr
from support import CALLER_PANE, THREE_SEATS, LiveFleet
from herdr_fleet import board, inbox, messages, tui, watch
from herdr_fleet.context import Ctx
from herdr_fleet.herdr import Herdr
from herdr_fleet.inbox import CUR, NEW
from test_context import assistant, write_transcript
from test_inbox import box, log_entries, put
from test_watch import StopClock, events, inline

B = board
SECRET = "SECRET-TOOL-OUTPUT-LINE"
PERMISSION = "\n".join([
    "\u23fa Bash(pnpm test)",
    "  \u23bf  " + SECRET,
    "",
    "\u2500" * 60,
    " Allow Bash(pnpm test)?",
    "",
    " \u276f 1. Yes",
    "   2. Yes, and don't ask again for pnpm test commands",
    "   3. No, and tell Claude what to do differently",
    "",
    " Esc to cancel"]) + "\n"
ORCH_SESSION = "6e3695ad-375b-464e-ba43-1aa9fc13e401"
DEV_SESSION = "0b1c2d3e-4f50-6172-8394-a5b6c7d8e9f0"
DEBT_ID = "20261024T050000Z-62437b"
STAMP = re.compile(r"^\d\d:\d\d:\d\d  ")
HHMM = r"\d\d:\d\d"


def ago(**delta):
    return inbox.now_utc() - datetime.timedelta(**delta)


OWNER_NEWEST = B.Ref(id="20261024T011300Z-bade2a", sender="winner-chronicles", to="owner",
                     time=datetime.datetime.now().astimezone().replace(hour=12, minute=13, second=0,
                                                                        microsecond=0).isoformat(),
                     first_line="1.28.1 is LIVE")


def sample(**extra):
    """The board of design 24.3's example: four seats, one of them blocked."""
    rows = [
        B.Row("fablyn-dev", "fablyn-dev", "working", 0, age=180, doing="vitest run", queued=2, oldest=200,
              next_id="018934", next_from="fablyn-orchestrator", next_line="two 1.30.4 items", tokens=376000),
        B.Row("fablyn-reviewer", "fablyn-reviewer", "idle", 1, age=7300, debt_to="fablyn-orchestrator",
              debt_shown="orchestrator", debt_id="62437b", debt_age=7300, tokens=514000, over=True),
        B.Row("fablyn-designer", "fablyn-designer", "idle", 2, age=1500, tokens=382000),
        B.Row("fablyn-orchestrator", "fablyn-orchestrator", "blocked", 3, age=240, doing="Allow Bash(pnpm test)?",
              queued=1, oldest=60, next_id="aaaaaa", note="holding: owner typing", tokens=730000, over=True),
    ]
    values = dict(fleet="fablyn", rows=rows, watching=11 * 3600 + 60, owner_count=1, owner_id="bade2a",
                  owner_line="1.28.1 is LIVE", waiting=2, holding=1, owner_newest=OWNER_NEWEST,
                  event=B.Event("08:02", "delivered 2 to fablyn-dev"), threshold=450000)
    values.update(extra)
    return B.Board(**values)


def six(**extra):
    board_ = sample(**extra)
    board_.rows += [B.Row("fablyn-qa", "fablyn-qa", "done", 4, age=30),
                    B.Row("fablyn-ops", "fablyn-ops", "absent", 5)]
    return board_


def text(lines):
    return B.plain_text(lines)


def widths(lines):
    return [sum(B.width_of(t) for t, _ in line) for line in lines]


def styles(line, fragment):
    """The style of the span holding ``fragment``."""
    return next(style for t, style in line if fragment in t)


# -- 24.3: render ------------------------------------------------------------------------------------


class Render(unittest.TestCase):
    def test_162x6_the_full_row(self):
        lines = B.render(sample(), 162, 6, True)
        self.assertEqual(len(lines), 6)
        self.assertEqual(widths(lines), [162] * 6)
        shown = text(lines)
        self.assertTrue(shown[0].startswith(  # design 25.3: the newest message to the owner
            'fablyn \u00b7 watching 11h \u00b7 owner: #bade2a 12:13 from winner-chronicles "1.28.1 is LIVE"'))
        self.assertTrue(shown[0].endswith("waiting on 2 \u00b7 holding 1"))
        dev, reviewer, designer, orch = shown[1:5]
        # name 20, state 14, then 118 columns shared 2:3:2 (33, 52, 33), context 10
        self.assertEqual(dev[:20], "fablyn-dev".ljust(20))
        self.assertEqual(dev[20:34], "\u25cf working 3m".ljust(14))
        self.assertEqual(dev[34:67], "vitest run".ljust(33))
        self.assertEqual(dev[67:119], '2 queued, oldest 3m  #018934 "two 1.30.4 items"'.ljust(52))
        self.assertEqual(dev[119:152], "\u2013".ljust(33))
        self.assertEqual(dev[152:], "ctx 376k")
        self.assertEqual(reviewer[20:34].rstrip(), "\u25cb idle 2h")
        self.assertEqual(reviewer[119:152].rstrip(), "owes orchestrator 2h #62437b")
        self.assertEqual(reviewer[152:], "ctx 514k \u26a0")
        self.assertEqual(designer[67:119].rstrip(), "\u2013")
        self.assertEqual(orch[20:34].rstrip(), "\u25d0 blocked 4m")
        self.assertEqual(orch[34:67].rstrip(), '"Allow Bash(pnpm test)?"')
        self.assertEqual(orch[67:119].rstrip(), "1 queued, holding: owner typing")
        self.assertEqual(shown[5], "08:02 delivered 2 to fablyn-dev")

    def test_162x8_leaves_room_and_shows_contacts(self):
        board_ = sample(contacts=[B.Row("winner-chronicles", "winner-chronicles", "idle", 4, age=60, contact=True)])
        shown = text(B.render(board_, 162, 8, True))
        self.assertEqual(len(shown), 8)
        self.assertEqual([line[:20].rstrip() for line in shown[1:6]],
                         ["fablyn-dev", "fablyn-reviewer", "fablyn-designer", "fablyn-orchestrator",
                          "winner-chronicles"])
        self.assertEqual(shown[6], "")
        self.assertEqual(shown[7], "08:02 delivered 2 to fablyn-dev")
        at6 = text(B.render(board_, 162, 6, True))  # no room: the contact is left out, and is not "more"
        self.assertNotIn("winner-chronicles", "\n".join(at6[1:]))  # the header names the owner's sender
        self.assertNotIn("more", at6[-1])

    def test_120x6_keeps_the_doing_cell_and_119_drops_it(self):
        at120 = text(B.render(sample(), 120, 6, True))
        self.assertEqual(widths(B.render(sample(), 120, 6, True)), [120] * 6)
        self.assertIn("vitest run", at120[1])
        self.assertEqual(at120[1][34:55].rstrip(), "vitest run")  # 76 columns: 21, 34, 21
        self.assertIn("owes orchestrator", at120[2])
        at119 = text(B.render(sample(), 119, 6, True))
        self.assertNotIn("vitest run", at119[1])
        self.assertNotIn("Allow Bash", at119[4])
        self.assertTrue(at119[1][34:].startswith("2 queued, oldest 3m"))
        self.assertIn("owes orchestrator", at119[2])

    def test_90x6_keeps_the_debt_cell_and_89_drops_it(self):
        at90 = text(B.render(sample(), 90, 6, True))
        self.assertIn("owes orchestrato", at90[2])
        self.assertTrue(at90[2].endswith("ctx 514k \u26a0"))
        at89 = text(B.render(sample(), 89, 6, True))
        self.assertNotIn("owes", at89[2])
        self.assertTrue(at89[2].endswith("ctx 514k \u26a0"))
        self.assertTrue(at89[1][34:].startswith("2 queued, oldest 3m  #018934"))

    def test_60x6_and_59x6(self):
        at60 = text(B.render(sample(), 60, 6, True))
        self.assertEqual(widths(B.render(sample(), 60, 6, True)), [60] * 6)
        self.assertEqual(at60[1][34:50].rstrip(), "2 queued, olde\u2026")
        self.assertTrue(at60[4].endswith("ctx 730k \u26a0"))
        at59 = text(B.render(sample(), 59, 6, True))
        self.assertEqual(at59[1], "fablyn-dev          \u25cf working 3m  2 queued")
        self.assertEqual(at59[4], "fablyn-orchestrator \u25d0 blocked 4m  1 queued")
        self.assertNotIn("ctx", "\n".join(at59))

    def test_40x2_is_one_line_the_most_urgent_seat(self):
        lines = B.render(sample(), 40, 2, True)
        self.assertEqual(len(lines), 1)
        self.assertEqual(widths(lines), [40])
        self.assertEqual(text(lines), ["fablyn-orchest\u2026 \u25d0 blocked 4m  1 queued"])
        board_ = sample()
        board_.rows[3].state = "idle"
        self.assertTrue(text(B.render(board_, 40, 1, True))[0].startswith("fablyn-dev"))  # then a queue

    def test_six_seats_in_six_rows_packed_two_to_a_row(self):
        lines = B.render(six(), 162, 6, True)
        shown = text(lines)
        self.assertEqual(widths(lines), [162] * 6)
        self.assertEqual(shown[1][:81].rstrip(), "fablyn-dev          \u25cf working 3m  2 queued")
        self.assertEqual(shown[1][81:].rstrip(), "fablyn-reviewer     \u25cb idle 2h     \u2013" + " " * 44 + "\u26a0")
        self.assertTrue(shown[2][81:].startswith("fablyn-orchestrator \u25d0 blocked 4m  1 queued"))
        self.assertEqual(shown[3], "fablyn-qa           \u2713 done <1m    \u2013" + " " * 46
                         + "fablyn-ops          \u2013 absent      \u2013")
        self.assertEqual(shown[4], "")
        self.assertNotIn("more", shown[5])
        self.assertEqual([r.name for r in B.visible(six(), 162, 6)],
                         ["fablyn-dev", "fablyn-reviewer", "fablyn-designer", "fablyn-orchestrator", "fablyn-qa",
                          "fablyn-ops"])

    def test_packed_rows_take_contacts_after_the_seats_when_they_fit(self):
        board_ = six(contacts=[B.Row("winner-chronicles", "winner-chronicles", "idle", 6, contact=True)])
        shown = text(B.render(board_, 162, 6, True))
        self.assertTrue(shown[4].startswith("winner-chronicles   \u25cb idle"))
        self.assertEqual(B.visible(board_, 162, 6)[-1].name, "winner-chronicles")
        self.assertNotIn("winner", "\n".join(text(B.render(board_, 162, 5, True))[1:]))  # no room left

    def test_six_seats_too_narrow_to_pack_show_the_most_urgent_and_more(self):
        shown = text(B.render(six(), 100, 6, True))
        self.assertEqual([line[:20].rstrip() for line in shown[1:5]],
                         ["fablyn-orchestrator", "fablyn-dev", "fablyn-reviewer", "fablyn-designer"])
        self.assertTrue(shown[5].startswith("08:02 delivered 2 to fablyn-dev"))
        self.assertTrue(shown[5].endswith("+2 more"))
        self.assertEqual(len(shown[5]), 100)
        board_ = six()
        board_.rows[5].state, board_.rows[2].state = "working", "done"  # working before the rest
        self.assertEqual([r.name for r in B.visible(board_, 100, 6)],
                         ["fablyn-orchestrator", "fablyn-dev", "fablyn-ops", "fablyn-reviewer"])

    def test_packed_rows_that_still_do_not_fit(self):
        board_ = six()
        board_.rows += [B.Row("extra-%d" % n, "extra-%d" % n, "idle", 6 + n) for n in range(4)]
        shown = text(B.render(board_, 162, 5, True))  # 3 rows of 2: the six most urgent
        self.assertTrue(shown[4].endswith("+4 more"))
        self.assertEqual(len(B.visible(board_, 162, 5)), 6)
        self.assertEqual(B.visible(board_, 162, 5)[0].name, "fablyn-orchestrator")

    def test_every_state_cell(self):
        cases = {"working": "\u25cf working 3m", "idle": "\u25cb idle 3m", "blocked": "\u25d0 blocked 3m",
                 "done": "\u2713 done 3m", "unknown": "? unknown 3m", "absent": "\u2013 absent 3m"}
        for state, expected in cases.items():
            with self.subTest(state=state):
                row = B.Row("s", "s", state, 0, age=200, doing="the task" if state in ("working", "blocked") else "")
                self.assertEqual(B.state_text(row), expected)
                doing = B.doing_text(row, 30)
                self.assertEqual(doing, {"working": "the task", "blocked": '"the task"'}.get(state, ""))
        self.assertEqual(B.state_text(B.Row("s", "s", "idle", 0)), "\u25cb idle")  # age unknown
        self.assertEqual([B.short_age(s) for s in (None, 0, 59, 60, 3599, 3600, 86399, 86400 * 3)],
                         ["", "<1m", "<1m", "1m", "59m", "1h", "23h", "3d"])

    def test_the_queue_debt_and_context_cells(self):
        row = B.Row("s", "s", "idle", 0)
        self.assertEqual(B.queue_text(row, 50), "\u2013")
        row.queued, row.oldest, row.next_id, row.next_line = 1, 200, "018934", "first line"
        self.assertEqual(B.queue_text(row, 50), '1 queued, 3m  #018934 "first line"')
        row.queued = 2
        self.assertEqual(B.queue_text(row, 50), '2 queued, oldest 3m  #018934 "first line"')
        self.assertEqual(B.queue_text(row, 36), '2 queued, oldest 3m  #018934 "first\u2026')
        self.assertEqual(B.queue_text(row, 30), "2 queued, oldest 3m  #018934")
        for note in ("holding: owner typing", "retrying in 45 s", "pointed to inbox --read"):
            row.note = note
            self.assertEqual(B.queue_text(row, 50), "2 queued, " + note)
        self.assertEqual(B.debt_text(B.Row("s", "s", "idle", 0), 30), "\u2013")
        debt = B.Row("s", "s", "idle", 0, debt_to="owner", debt_shown="owner", debt_age=7300)
        self.assertEqual(B.debt_text(debt, 30), "owes owner 2h")  # a message sent before ids
        debt.debt_id = "62437b"
        self.assertEqual(B.debt_text(debt, 30), "owes owner 2h #62437b")
        self.assertEqual(B.context_text(B.Row("s", "s", "idle", 0)), "")
        self.assertEqual(B.context_text(B.Row("s", "s", "idle", 0, tokens=1234567)), "ctx 1.2M")
        self.assertEqual([B.hold_reason(d) for d in ("owner typing in w1:p2",
                                                     "text in its input (w1:p2); press Ctrl-U there to clear it",
                                                     "a dialog or a menu is on screen in w1:p2; answer it",
                                                     "presence check: herdr error x; nothing sent")],
                         ["owner typing", "text in its input", "a dialog or a menu", "presence check: herdr error x"])

    def test_colours_on(self):
        board_ = sample(selected="fablyn-designer")
        lines = B.render(board_, 162, 6, True)
        self.assertEqual(styles(lines[4], "blocked"), "red bold")
        self.assertEqual(styles(lines[4], "Allow Bash"), "red bold")
        self.assertEqual(styles(lines[1], "working"), "green")
        self.assertEqual(styles(lines[2], "idle"), "dim")
        self.assertEqual(styles(lines[2], "\u26a0"), "yellow")
        self.assertTrue(all("reverse" in style.split() for _, style in lines[3]))  # the selected row, all of it
        self.assertFalse(any("reverse" in style for _, style in lines[2]))
        board_.error = "cannot read herdr: herdr_not_found"
        self.assertEqual(styles(B.render(board_, 162, 6, True)[5], "cannot read"), "red")
        board_.error, board_.event = "", B.Event("08:03", "failed fablyn-dev: x", True)
        self.assertEqual(styles(B.render(board_, 162, 6, True)[5], "failed"), "red")
        done = sample()
        done.rows[0].state = "done"
        self.assertEqual(styles(B.render(done, 162, 6, True)[1], "done"), "dim")

    def test_colours_off(self):
        board_ = sample(selected="fablyn-designer", error="cannot read herdr: x")
        lines = B.render(board_, 162, 6, False)
        self.assertEqual(styles(lines[4], "blocked"), "bold")      # still marked without colour
        self.assertEqual(styles(lines[1], "working"), "")
        self.assertEqual(styles(lines[2], "idle"), "")
        self.assertEqual(styles(lines[2], "\u26a0"), "")
        self.assertEqual(styles(lines[5], "cannot read"), "")
        self.assertTrue(all(style == "reverse" for _, style in lines[3]))  # the selection stays visible
        words = {w for line in lines for _, style in line for w in style.split()}
        self.assertLessEqual(words, {"bold", "reverse"})
        self.assertEqual(text(lines), text(B.render(board_, 162, 6, True)))  # same text either way

    def test_the_footer_notice_and_the_header_without_queues(self):
        board_ = sample(notice="ack #62437b (fablyn-reviewer owes fablyn-orchestrator 2h)? y/n")
        self.assertEqual(text(B.render(board_, 162, 6, True))[5],
                         "ack #62437b (fablyn-reviewer owes fablyn-orchestrator 2h)? y/n")
        quiet = sample(owner_count=0, owner_newest=None, waiting=0, holding=0)
        self.assertEqual(text(B.render(quiet, 162, 6, True))[0], "fablyn \u00b7 watching 11h \u00b7 owner: nothing")
        self.assertEqual(text(B.render(sample(event=None), 162, 6, True))[5], "")

    def test_text_is_cut_never_wrapped(self):
        long = "x" * 300
        board_ = sample(owner_line=long, event=B.Event("08:02", long))
        board_.rows[0].doing = "\u6f22\u5b57" * 40 + "\ttab\nnewline"
        board_.rows[0].next_line = long
        for width in (162, 120, 90, 60, 40, 7, 1):
            for height in (6, 2):
                lines = B.render(board_, width, height, True)
                self.assertTrue(all(w == width for w in widths(lines)), (width, height, widths(lines)))
                self.assertFalse(any("\n" in t or "\t" in t for line in lines for t, _ in line))
        self.assertEqual(B.cut("\u6f22\u5b57\u6f22", 4), "\u6f22\u2026")
        self.assertEqual(B.render(sample(), 0, 6, True), [])

    def test_render_is_pure(self):
        board_ = sample()
        before = repr(board_)
        self.assertEqual(B.render(board_, 162, 6, True), B.render(board_, 162, 6, True))
        self.assertEqual(repr(board_), before)

    def test_render_log(self):
        lines = B.render_log(["10:00:01  one", "10:00:02  two", "10:00:03  three"], 40, 3, True)
        self.assertEqual(text(lines), ["watch.log \u00b7 any key returns to the board", "10:00:02  two",
                                       "10:00:03  three"])
        self.assertEqual(text(B.render_log([], 30, 3, True)), ["watch.log \u00b7 any key returns t\u2026", "", ""])
        self.assertEqual(text(B.render_log(["a", "b"], 30, 1, True)), ["b"])


# -- 24.2: build -------------------------------------------------------------------------------------


class BoardCase(LiveFleet):
    fleet_text = "restart_hint_tokens = 450000\n" + THREE_SEATS

    def setUp(self):
        super().setUp()
        self.projects = os.path.join(self.tmp, ".claude", "projects")  # HOME is self.tmp

    def make_ctx(self):
        """A watch command's context on the fake, its calls logged as `watch`'s."""
        with open(self.world_path, "w", encoding="utf-8") as handle:
            json.dump(self.world, handle)
        with open(self.log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"command": ["watch"]}) + "\n")
        runner = fake_herdr.Fake(self.world_path, self.log_path, CALLER_PANE,
                                 os.path.join(self.repo, ".herdr", "inbox"),
                                 os.path.join(self.repo, ".herdr", "fleet.toml"))
        return Ctx(Herdr("/fake/bin/herdr", runner), self.env, self.clock, io.StringIO(), io.StringIO(), self.repo)

    def build(self, state=None):
        result = B.build(self.make_ctx(), state)
        with open(self.world_path, "r", encoding="utf-8") as handle:
            self.world = json.load(handle)
        return result

    def blocked_orchestrator(self):
        fake_herdr.set_screen(self.world, "w2:p1", PERMISSION)
        self.status_of("w2:p1", "blocked")

    def debt(self, when=None):
        """fablyn-reviewer owes fablyn-orchestrator #62437b, delivered two hours ago."""
        when = when or ago(hours=2, minutes=1)
        put(self.repo, "fablyn-reviewer", "Review round 3, please", ident=DEBT_ID, when=when, which=CUR)
        messages.append(self.repo, {"time": inbox.iso_local(when), "from": "fablyn-orchestrator",
                                    "to": "fablyn-reviewer", "kind": "send", "id": DEBT_ID, "bytes": 40,
                                    "first_line": "Review round 3, please", "delivery": "sent"})

    def sessions(self, orch=730000, dev=376000):
        recent = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        self.world["panes"]["w2:p1"]["agent"]["agent_session"] = fake_herdr.claude_session(ORCH_SESSION)
        self.world["panes"]["w2:p2"]["agent"]["agent_session"] = fake_herdr.claude_session(DEV_SESSION)
        self.world["panes"]["w2:p3"]["agent"]["agent_session"] = fake_herdr.claude_session("c0dex", "codex")
        write_transcript(os.path.join(self.projects, "-repo"), ORCH_SESSION, [assistant(1, 0, orch - 1, when=recent)])
        write_transcript(os.path.join(self.projects, "-repo-dev"), DEV_SESSION, [assistant(1, 0, dev - 1, when=recent)])

    def seen_since(self, name, state, **delta):
        data = self.read_state()
        data["agents"][name]["state_seen"] = {"state": state, "since": inbox.iso_local(ago(**delta))}
        self.write_state(data)


class Build(BoardCase):
    def test_states_titles_ages_and_the_dialog_line(self):
        self.blocked_orchestrator()
        self.status_of("w2:p2", "working")
        fake_herdr.set_title(self.world, "w2:p2", "vitest run")
        fake_herdr.set_title(self.world, "w2:p3", "fablyn-reviewer")  # idle: not read anyway
        self.seen_since("fablyn-dev", "working", minutes=10)
        self.seen_since("fablyn-reviewer", "working", minutes=10)  # another state: since becomes now
        result = self.build()
        orch, dev, reviewer = result.rows
        self.assertEqual((orch.name, orch.state, orch.doing), ("fablyn-orchestrator", "blocked",
                                                                "Allow Bash(pnpm test)?"))
        self.assertEqual((dev.state, dev.doing), ("working", "vitest run"))
        self.assertEqual((reviewer.state, reviewer.doing), ("idle", ""))
        self.assertAlmostEqual(dev.age, 600, delta=5)
        self.assertLess(reviewer.age, 5)
        self.assertEqual(self.read_state()["agents"]["fablyn-reviewer"]["state_seen"]["state"], "idle")  # 20.5
        self.assertEqual(result.fleet, "fablyn")
        # what was read: one list, a pane get for the working seat, the blocked seat's screen
        reads = [c["argv"] for c in self.calls()]
        self.assertEqual([a for a in reads if a[:2] == ["agent", "list"]], [["agent", "list"]])
        self.assertEqual([a for a in reads if a[:2] == ["pane", "get"]], [["pane", "get", "w2:p2"]])
        self.assertEqual([a for a in reads if a[:2] == ["agent", "read"]],
                         [["agent", "read", "fablyn-orchestrator", "--lines", "16"]])
        self.assertFalse([a for a in reads if a[:2] in (["agent", "prompt"], ["agent", "send-keys"],
                                                         ["agent", "focus"])])
        # nothing else of the screen is kept or shown (invariant 22)
        self.assertNotIn(SECRET, repr(result))
        self.assertNotIn(SECRET, "\n".join(text(B.render(result, 162, 6, True))))

    def test_a_title_naming_the_seat_or_its_label_is_nothing(self):
        self.write_fleet(self.read_fleet().replace('name = "fablyn-dev"', 'name = "fablyn-dev"\nlabel = "DEV"'))
        self.status_of("w2:p2", "working")
        self.status_of("w2:p3", "working")
        fake_herdr.set_title(self.world, "w2:p2", "DEV")
        fake_herdr.set_title(self.world, "w2:p3", "fablyn-reviewer")
        result = self.build()
        self.assertEqual([(r.shown, r.doing) for r in result.rows],
                         [("fablyn-orchestrator", ""), ("DEV", ""), ("fablyn-reviewer", "")])

    def test_a_blocked_seat_without_a_numbered_choice_shows_nothing(self):
        fake_herdr.show_dialog(self.world, "w2:p1")  # the fake's dialog has no highlighted choice
        self.status_of("w2:p1", "blocked")
        self.assertEqual(self.build().rows[0].doing, "")

    def test_queues_owner_inbox_and_holds(self):
        put(self.repo, "fablyn-dev", "older task\nwith a second line", when=ago(minutes=7))
        put(self.repo, "fablyn-dev", "[from x] newer task", sender="fablyn-reviewer", when=ago(minutes=2))
        put(self.repo, "fablyn-dev", "an FYI that waits", when=ago(minutes=1), fyi=True)
        put(self.repo, "fablyn-reviewer", "expired", when=ago(minutes=9), ttl=ago(minutes=1))
        owner = put(self.repo, "owner", "1.28.1 is LIVE\nsecond line", sender="fablyn-dev", when=ago(minutes=4))
        state = B.WatcherState(watching=3600 * 11, waiting=["fablyn-dev"],
                               holds={"fablyn-dev": "owner typing in w2:p2"}, retries={"fablyn-orchestrator": 42.4},
                               pointed=["fablyn-reviewer"], event=B.Event("08:02", "delivered 2 to fablyn-dev"))
        put(self.repo, "fablyn-orchestrator", "for the orchestrator", sender="fablyn-dev")
        result = self.build(state)
        orch, dev, reviewer = result.rows
        self.assertEqual((dev.queued, dev.next_from, dev.next_line), (3, "fablyn-orchestrator", "older task"))
        self.assertAlmostEqual(dev.oldest, 420, delta=5)
        self.assertEqual(dev.note, "holding: owner typing")
        self.assertEqual(orch.note, "retrying in 42 s")
        self.assertEqual((reviewer.queued, reviewer.note), (0, "pointed to inbox --read"))  # past its ttl: not counted
        self.assertEqual(len(box(self.repo, "fablyn-reviewer", NEW)), 1)  # and not moved: the board only reads
        self.assertEqual((result.owner_count, result.owner_id, result.owner_from, result.owner_line),
                         (1, owner.short, "fablyn-dev", "1.28.1 is LIVE"))
        self.assertEqual((result.owner_newest.id, result.owner_newest.sender), (owner.id, "fablyn-dev"))
        self.assertEqual((result.waiting, result.holding, result.retrying, result.watching), (1, 1, 1, 3600 * 11))
        self.assertEqual(result.event.text, "delivered 2 to fablyn-dev")
        shown = text(B.render(result, 162, 6, True))
        self.assertRegex(shown[0], r'owner: #%s %s from fablyn-dev "1\.28\.1 is LIVE"' % (owner.short, HHMM))
        self.assertTrue(shown[0].endswith("waiting on 1 \u00b7 holding 1 \u00b7 retrying 1"))
        self.assertIn("3 queued, holding: owner typing", shown[2])
        self.assertTrue(shown[5].startswith("08:02 delivered 2 to fablyn-dev"))

    def test_debts_the_oldest_owed_by_each_seat(self):
        self.debt()
        later = ago(minutes=30)
        put(self.repo, "fablyn-reviewer", "second ask", sender="owner", ident="20261024T060000Z-a1b2c3",
            when=later, which=CUR)
        messages.append(self.repo, {"time": inbox.iso_local(later), "from": "owner", "to": "fablyn-reviewer",
                                    "kind": "send", "id": "20261024T060000Z-a1b2c3", "bytes": 9,
                                    "first_line": "second ask", "delivery": "sent"})
        result = self.build()
        reviewer = result.rows[2]
        self.assertEqual((reviewer.debt_to, reviewer.debt_shown, reviewer.debt_id), ("fablyn-orchestrator",
                                                                                      "orchestrator", "62437b"))
        self.assertAlmostEqual(reviewer.debt_age, 7260, delta=5)
        self.assertEqual(result.rows[0].debt_to, "")
        self.assertIn("owes orchestrator 2h #62437b", text(B.render(result, 162, 6, True))[3])

    def test_the_log_is_parsed_again_only_when_it_changed(self):
        self.debt()
        state = B.WatcherState(cache=B.Cache())
        real = messages.read
        with mock.patch.object(B, "read", side_effect=real) as reading:
            self.build(state)
            self.build(state)
            self.build(state)
            self.assertEqual(reading.call_count, 1)
            messages.append(self.repo, messages.ack_entry("fablyn-reviewer", "fablyn-orchestrator", ident=DEBT_ID))
            result = self.build(state)
            self.assertEqual(reading.call_count, 2)
        self.assertEqual(result.rows[2].debt_to, "")  # the ack settled it

    def test_context_from_the_transcripts_and_the_threshold(self):
        self.sessions(orch=450000, dev=449999)
        result = self.build()
        orch, dev, reviewer = result.rows
        self.assertEqual((orch.tokens, orch.over), (450000, True))      # at the threshold: ⚠
        self.assertRegex(orch.context_at, r"^\d{4}-\d\d-\d\dT")
        self.assertEqual((dev.tokens, dev.over), (449999, False))
        self.assertEqual((reviewer.tokens, reviewer.over), (None, False))  # a Codex seat: blank
        shown = text(B.render(result, 162, 6, True))
        self.assertTrue(shown[1].endswith("ctx 450k \u26a0"))
        self.assertTrue(shown[2].endswith("ctx 450k"))
        self.assertEqual(shown[3][152:], "")
        self.assertNotIn("TOP-SECRET", repr(result))  # usage numbers only (invariant 9)
        self.write_fleet(THREE_SEATS)                    # no threshold: never ⚠
        self.assertFalse(any(r.over for r in self.build().rows))

    def test_a_transcript_is_read_again_only_when_it_changed(self):
        self.sessions()
        state = B.WatcherState(cache=B.Cache())
        from herdr_fleet import context_size
        real = context_size.last_usage
        with mock.patch.object(context_size, "last_usage", side_effect=real) as reading, \
                mock.patch.object(context_size, "find_transcript", side_effect=context_size.find_transcript) as finding:
            self.build(state)
            self.build(state)
            self.assertEqual((reading.call_count, finding.call_count), (2, 2))  # two Claude seats, once each
            write_transcript(os.path.join(self.projects, "-repo-dev"), DEV_SESSION,
                             [assistant(1, 0, 500000)], partial=False)
            result = self.build(state)
            self.assertEqual((reading.call_count, finding.call_count), (3, 2))
        self.assertEqual(result.rows[1].tokens, 500001)

    def test_contacts_and_an_absent_seat(self):
        winner = os.path.join(self.tmp, "winner")
        os.makedirs(winner)
        self.write_fleet(self.read_fleet() + '\n[[contacts]]\nname = "winner-chronicles"\nmatch = { cwd = "%s" }\n'
                         % winner)
        fake_herdr.add_workspace(self.world, "w3", "winner", winner)
        fake_herdr.add_pane(self.world, "w3:p4", "w3", winner)
        fake_herdr.put_agent(self.world, "w3:p4", "claude", name=None, status="working")
        self.world["panes"]["w2:p3"]["agent"] = None  # the reviewer is gone
        put(self.repo, "winner-chronicles", "for the contact", sender="fablyn-dev")
        result = self.build()
        self.assertEqual(result.rows[2].state, "absent")
        (contact,) = result.contacts
        self.assertEqual((contact.name, contact.state, contact.queued, contact.contact),
                         ("winner-chronicles", "working", 1, True))
        self.assertEqual(text(B.render(result, 162, 8, True))[4][:40].rstrip(),
                         "winner-chronicles   \u25cf working <1m")
        self.assertFalse(self.argvs("pane", "get"))  # a contact's title is not read

    def test_herdr_unreachable(self):
        self.world["canned"].append({"prefix": ["agent", "list"], "exit": 1,
                                     "stderr": {"error": {"code": "server_unreachable", "message": "no server"}}})
        before = self.read_state()
        result = self.build()
        self.assertEqual([r.state for r in result.rows], ["unknown"] * 3)
        self.assertIn("cannot read herdr", result.error)
        self.assertEqual(self.read_state(), before)  # nothing recorded from a failed read
        self.assertIn("cannot read herdr", text(B.render(result, 162, 6, True))[5])


# -- 24.1 and 24.4: the watcher with the board -------------------------------------------------------


class Strip(BoardCase):
    def run_board(self, keys=(), size=(162, 6), idle=16, args=(), colours=True):
        self.screen = tui.FakeScreen(keys, size, clock=self.clock, on_key=self.log_key, idle=idle, colours=colours)
        opened = []

        def open_screen(env, stream=None):
            opened.append(stream)
            return self.screen

        with mock.patch.object(watch, "is_terminal", lambda stream: True), \
                mock.patch.object(tui, "open_screen", open_screen), mock.patch.object(watch, "spawn", inline):
            code, out, err = self.run_cli("watch", *args)
        self.assertEqual(len(opened), 1)
        return code, out, err

    def watch_log(self):
        path = os.path.join(self.repo, ".herdr", "watch.log")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        for line in lines:
            self.assertRegex(line, STAMP)
        return [line[10:] for line in lines]

    def footers(self):
        return [self.screen.text(i)[-1] if self.screen.text(i) else "" for i in range(len(self.screen.draws))]

    def focused_pane(self):
        return next((p for p, r in self.world["panes"].items() if r.get("focused") and
                     self.world["workspaces"][r["workspace_id"]].get("focused")), None)


class Modes(Strip):
    def test_board_mode_logs_the_lines_and_draws(self):
        code, out, err = self.run_board()
        self.assertEqual(code, 0, err)
        self.assertRegex(out, r"^\d\d:\d\d:\d\d  stopped\n$")  # printed once the board is gone
        lines = self.watch_log()
        self.assertRegex(lines[0], r"^watching .*fleet\.toml \(pid %d, debounce 5000 ms\); Ctrl-C stops$" % os.getpid())
        self.assertEqual(lines[-1], "stopped")
        self.assertTrue(self.screen.closed)
        first = self.screen.text(0)
        self.assertEqual(len(first), 6)
        self.assertTrue(first[0].startswith("fablyn \u00b7 watching <1m \u00b7 owner: nothing"))
        self.assertEqual([line[:20].rstrip() for line in first[1:4]],
                         ["fablyn-orchestrator", "fablyn-dev", "fablyn-reviewer"])
        self.assertRegex(first[5], r"^%s watching repo \(pid %d\)  " % (HHMM, os.getpid()))  # 25.8: short
        self.assertTrue(self.watch_log()[0].startswith("watching %s (pid" % os.path.join(self.repo, ".herdr",
                                                                                         "fleet.toml")))
        self.assertIn("Enter open \u00b7 g go", first[5])  # design 25.1: the board's key hints
        with open(os.path.join(self.repo, ".herdr", ".gitignore")) as handle:
            self.assertIn("watch.log", handle.read().split())
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".herdr", "watch.lock")))
        self.assertFalse(self.argvs("agent", "prompt"))
        self.assertFalse(self.argvs("agent", "focus"))

    def test_what_other_code_prints_goes_to_the_log_once(self):
        with mock.patch("herdr_fleet.seen.record_seen", side_effect=OSError("disk full")):
            code, out, err = self.run_board(idle=40)  # 10 s: three builds, three failed records
        self.assertEqual(code, 0)
        self.assertEqual(err, "")  # nothing written past the board
        self.assertRegex(out, r"^\d\d:\d\d:\d\d  stopped\n$")
        warnings = [line for line in self.watch_log() if "cannot record seat states" in line]
        self.assertEqual(warnings, ["herdr-fleet: cannot record seat states in the state file: disk full"])
        self.assertTrue(any("disk full" in f for f in self.footers()))

    def test_a_terminal_without_curses_prints_the_lines(self):
        self.clock = StopClock(cycles=2)
        with mock.patch.object(watch, "is_terminal", lambda stream: True), \
                mock.patch.object(tui, "open_screen", lambda env, stream=None: None):
            code, out, _ = self.run_cli("watch")
        self.assertEqual(code, 0)
        self.assertEqual(events(out)[1:], ["stopped"])
        self.assertEqual(self.watch_log(), [])

    def test_log_prints_todays_lines_on_a_terminal(self):
        put(self.repo, "fablyn-dev", "Task A")
        self.world["behaviors"]["wait_results"] = {"fablyn-dev": ["idle"]}
        self.clock = StopClock(cycles=2)

        def never(env, stream=None):
            raise AssertionError("the board was opened")

        with mock.patch.object(watch, "is_terminal", lambda stream: True), \
                mock.patch.object(tui, "open_screen", never), mock.patch.object(watch, "spawn", inline):
            code, out, _ = self.run_cli("watch", "--log")
        self.assertEqual(code, 0)
        self.assertEqual(events(out)[1:], ["waiting on fablyn-dev (1 queued)", "fablyn-dev idle; delivering 1",
                                           "delivered 1 to fablyn-dev", "stopped"])
        self.assertEqual(self.watch_log(), [])

    def test_a_stdout_that_is_not_a_terminal_prints_todays_lines(self):
        self.clock = StopClock(cycles=1)

        def never(env, stream=None):
            raise AssertionError("the board was opened")

        with mock.patch.object(tui, "open_screen", never):
            code, out, _ = self.run_cli("watch")  # the harness's stdout is a StringIO
        self.assertEqual(code, 0)
        self.assertRegex(events(out)[0], r"^watching ")
        self.assertFalse(watch.is_terminal(io.StringIO()))

    def test_once_never_draws(self):
        def never(env, stream=None):
            raise AssertionError("the board was opened")

        with mock.patch.object(watch, "is_terminal", lambda stream: True), mock.patch.object(tui, "open_screen", never):
            code, out, _ = self.run_cli("watch", "--once")
        self.assertEqual((code, out), (0, "nothing to deliver\n"))

    def test_the_board_follows_deliveries_and_rebuilds_on_events(self):
        put(self.repo, "fablyn-dev", "Task A")
        self.world["behaviors"]["wait_results"] = {"fablyn-dev": ["timeout", "idle"]}
        built = []
        real = B.build

        def counting(ctx, state=None):
            built.append(self.clock.now)
            return real(ctx, state)

        with mock.patch.object(B, "build", counting):
            code, _, err = self.run_board(args=("--debounce", "0"), idle=60)
        self.assertEqual(code, 0, err)
        start = built[0]
        # at the start; at the next pass, right after the delivery (an event); then 5 s later
        self.assertEqual([t - start for t in built[:3]], [0.0, 2.0, 7.0])
        self.assertIn("delivered 1 to fablyn-dev", self.watch_log())
        self.assertTrue(any(f.startswith("") and "delivered 1 to fablyn-dev" in f for f in self.footers()))
        self.assertEqual(len(self.prompts_to("fablyn-dev")), 1)


class Keys(Strip):
    def test_g_goes_to_the_selected_seat(self):  # design 25.1: what Enter and the numbers did in 24.4
        code, _, err = self.run_board(["down", "g"])  # 25.8: the first row starts selected
        self.assertEqual(code, 0, err)
        self.assertEqual(self.argvs("agent", "focus"), [["agent", "focus", "fablyn-dev"]])
        self.assertEqual(self.focused_pane(), "w2:p2")
        self.assertIn("focused fablyn-dev (w2:p2, idle)", self.watch_log())

    def test_a_number_without_a_seat_and_an_absent_seat(self):
        self.world["panes"]["w2:p3"]["agent"] = None
        self.run_board(["7", "down", "down", "down", "g"])
        self.assertFalse(self.argvs("agent", "focus"))
        self.assertIn("no seat 7 on the board", self.footers())
        self.assertIn("fablyn-reviewer is absent; nothing to focus", self.watch_log())

    def test_up_down_and_enter(self):
        self.run_board(["down", "down", "up", "g"])
        self.assertEqual(self.argvs("agent", "focus"), [["agent", "focus", "fablyn-dev"]])
        selected = [i for i, draw in enumerate(self.screen.draws)
                    if any("reverse" in style for line in draw[1:2] for _, style in line)]
        self.assertTrue(selected)  # the first row was drawn selected after the first Down

    def test_the_first_row_starts_selected(self):  # design 25.8
        self.run_board(["enter"])
        self.assertTrue(all("reverse" in style for _, style in self.screen.draws[0][1]))
        self.assertTrue(self.screen.text()[0].startswith("fablyn-orchestrator \u00b7"))  # Enter opened its page
        self.assertFalse(self.argvs("agent", "focus"))

    def test_a_then_y_acks_the_oldest_debt_as_owner(self):
        self.debt()
        code, _, err = self.run_board(["down", "down", "down", "a", "y"])
        self.assertEqual(code, 0, err)
        self.assertTrue(any(re.match(r"^ack #62437b \(fablyn-reviewer owes fablyn-orchestrator 2h\)\? y/n$", f)
                            for f in self.footers()), self.footers())
        acks = [e for e in log_entries(self.repo) if e["kind"] == "ack"]
        self.assertEqual(len(acks), 1)
        self.assertEqual({k: acks[0][k] for k in ("from", "to", "id", "by", "reason")},
                         {"from": "fablyn-reviewer", "to": "fablyn-orchestrator", "id": DEBT_ID, "by": "owner",
                          "reason": "from the board"})
        self.assertTrue(any(line.startswith("acked #62437b from fablyn-orchestrator since ")
                            and line.endswith("owed by fablyn-reviewer; reason: from the board")
                            for line in self.watch_log()), self.watch_log())
        self.assertFalse(self.argvs("pane", "current"))  # the caller is owner: no lookup
        self.assertFalse(self.argvs("agent", "prompt"))  # ack sends nothing (invariant 10)
        self.assertNotIn("owes orchestrator", self.screen.text()[3])  # settled on the board

    def test_a_then_n_acks_nothing(self):
        self.debt()
        self.run_board(["down", "down", "down", "a", "n"])
        self.assertFalse([e for e in log_entries(self.repo) if e["kind"] == "ack"])
        self.assertIn("not acked", self.footers())

    def test_a_with_nothing_owed_or_nothing_selected(self):
        self.run_board(["a", "down", "a", "up", "up", "a"])
        self.assertIn("fablyn-orchestrator owes nothing", self.footers())
        self.assertIn("fablyn-dev owes nothing", self.footers())
        self.assertIn("select a seat with \u2191 and \u2193 first", self.footers())  # the header selected
        self.assertFalse([e for e in log_entries(self.repo) if e["kind"] == "ack"])

    def test_d_delivers_now_to_idle_seats_only(self):
        put(self.repo, "fablyn-dev", "Task A")
        put(self.repo, "fablyn-reviewer", "Review B")
        put(self.repo, "fablyn-orchestrator", "for the blocked one", sender="fablyn-dev")
        self.status_of("w2:p3", "working")
        self.blocked_orchestrator()
        self.world["behaviors"]["wait_results"] = {"fablyn-dev": ["timeout"] * 9,
                                                   "fablyn-reviewer": ["timeout"] * 9}
        code, _, err = self.run_board(["d"])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.prompts_to("fablyn-dev")), 1)
        self.assertEqual(self.prompts_to("fablyn-reviewer"), [])
        self.assertEqual(self.prompts_to("fablyn-orchestrator"), [])
        lines = self.watch_log()
        self.assertIn("delivered 1 to fablyn-dev", lines)
        self.assertIn("fablyn-reviewer working; 1 left queued", lines)
        self.assertIn("fablyn-orchestrator blocked: a dialog is open; 1 left queued", lines)
        self.assertNotIn("fablyn-dev idle; delivering 1", lines)  # no debounce
        self.assertEqual(len(box(self.repo, "fablyn-reviewer", NEW)), 1)
        self.assertEqual([(e["kind"], e["via"]) for e in log_entries(self.repo)], [("delivered", "watch")])

    def test_d_holds_for_text_in_the_input_and_says_when_nothing_waits(self):
        put(self.repo, "fablyn-dev", "Task A")
        fake_herdr.type_in(self.world, "w2:p2", "half typed")
        self.world["behaviors"]["wait_results"] = {"fablyn-dev": ["timeout"] * 9}
        self.run_board(["d"])
        self.assertEqual(self.prompts_to("fablyn-dev"), [])
        self.assertIn("fablyn-dev idle; text in its input (w2:p2); press Ctrl-U there to clear it, 1 left queued",
                      self.watch_log())
        os.remove(box_path(self.repo, "fablyn-dev"))
        self.run_board(["d"])
        self.assertIn("nothing to deliver: no seat has a queue", self.footers())

    def test_l_shows_the_log_and_any_key_returns(self):
        self.run_board(["l", "x"])
        titles = [draw[0] for draw in (self.screen.text(i) for i in range(len(self.screen.draws)))]
        at = titles.index("watch.log \u00b7 any key returns to the board")
        log_view = self.screen.text(at)
        self.assertRegex(log_view[1], r"^\d\d:\d\d:\d\d  watching ")
        self.assertTrue(titles[at + 1].startswith("fablyn \u00b7 watching"))

    def test_question_mark_shows_the_keys_for_five_seconds(self):
        self.run_board(["?"], idle=40)
        footers = self.footers()
        at = footers.index(watch.HELP)
        self.assertRegex(footers[at + 1], r"^%s watching " % HHMM)  # back to the last event
        self.assertIn("Enter or 1-9 open seat", watch.HELP); self.assertIn("p screen", watch.HELP); self.assertIn("q back", watch.HELP)

    def test_q_does_not_stop_the_watcher(self):
        code, out, _ = self.run_board(["q", "Q", "z"], idle=8)
        self.assertEqual(code, 0)
        self.assertIn("Ctrl-C stops the watcher", self.footers())
        self.assertGreater(len(self.screen.timeouts), 8)  # it kept reading keys after q
        self.assertEqual(self.watch_log()[-1], "stopped")

    def test_other_keys_do_nothing(self):
        self.run_board(["z", "x", "code:27"])
        self.assertEqual(len(self.screen.draws), 1)  # nothing to redraw
        self.assertFalse(self.argvs("agent", "focus"))

    def test_resize(self):
        self.run_board([("resize", 90, 6), ("wait", 1.0), ("resize", 40, 2)])
        sizes = [(widths(draw)[0], len(draw)) for draw in self.screen.draws]
        self.assertEqual(sizes[0], (162, 6))
        self.assertIn((90, 6), [(widths(d)[0], len(d)) for d in self.screen.draws])
        last = self.screen.draws[-1]
        self.assertEqual((widths(last), len(last)), ([40], 1))
        self.assertTrue(text(last)[0].startswith("fablyn-orche"))

    def test_colours_follow_the_screen_and_no_color(self):
        self.blocked_orchestrator()
        self.run_board()  # the first row is the selected one (25.8)
        self.assertEqual(styles(self.screen.draws[0][1], "blocked"), "red bold reverse")
        self.env["NO_COLOR"] = "1"
        self.run_board()
        self.assertEqual(styles(self.screen.draws[0][1], "blocked"), "bold reverse")
        del self.env["NO_COLOR"]
        self.run_board(colours=False)
        self.assertEqual(styles(self.screen.draws[0][1], "blocked"), "bold reverse")

    def test_the_blocked_seat_is_read_and_nothing_is_typed(self):
        self.blocked_orchestrator()
        put(self.repo, "fablyn-orchestrator", "for the blocked one", sender="fablyn-dev")
        self.world["behaviors"]["wait_results"] = {"fablyn-orchestrator": ["timeout"] * 9}
        self.run_board(["d", "g"])
        self.assertIn('"Allow Bash(pnpm test)?"', self.screen.text(0)[1])
        self.assertTrue(self.argvs("agent", "read", "fablyn-orchestrator"))
        self.assertFalse(self.argvs("agent", "read", "fablyn-dev"))
        self.assertFalse(self.argvs("agent", "prompt"))
        self.assertFalse(self.argvs("agent", "send-keys"))
        self.assertEqual(self.argvs("agent", "focus"), [["agent", "focus", "fablyn-orchestrator"]])
        self.assertNotIn(SECRET, "\n".join(self.watch_log()))


class FakeCurses:
    """Just enough of the ``curses`` module to drive ``tui.CursesScreen`` without a terminal."""

    class error(Exception):
        pass

    KEY_RESIZE, KEY_UP, KEY_DOWN, KEY_ENTER = 410, 259, 258, 343
    KEY_LEFT, KEY_RIGHT, KEY_PPAGE, KEY_NPAGE, KEY_BACKSPACE, KEY_MOUSE = 260, 261, 339, 338, 263, 409
    BUTTON1_PRESSED, BUTTON1_CLICKED = 2, 4
    A_BOLD, A_DIM, A_REVERSE = 1 << 21, 1 << 20, 1 << 18
    COLOR_RED, COLOR_GREEN, COLOR_YELLOW, COLOR_BLACK = 1, 2, 3, 0

    def __init__(self, keys=(), size=(6, 40), colours=True, fail=None):
        self.keys, self.colours, self.fail, self.calls = list(keys), colours, fail, []
        self.screen = FakeWindow(self, size)

    def color_pair(self, number):
        return number << 8

    def setupterm(self, term=None, fd=-1):
        self.calls.append(("setupterm", term, fd))
        if self.fail == "setupterm":
            raise self.error("unknown terminal type")

    def initscr(self):
        return self.screen

    def has_colors(self):
        return self.colours

    def __getattr__(self, name):  # noecho, cbreak, curs_set, start_color, init_pair, endwin, ...
        def call(*args):
            self.calls.append((name,) + args)
        return call


class FakeWindow:
    def __init__(self, curses, size):
        self.curses, self.size, self.drawn = curses, size, []

    def getmaxyx(self):
        return self.size

    def timeout(self, ms):
        self.curses.calls.append(("timeout", ms))

    def getch(self):
        return self.curses.keys.pop(0) if self.curses.keys else -1

    def addstr(self, y, x, text, attr=0):
        self.drawn.append((y, x, text, attr))
        if y == self.size[0] - 1 and x + len(text) >= self.size[1]:
            raise self.curses.error("the bottom-right cell")

    def __getattr__(self, name):  # erase, refresh, clear, keypad
        def call(*args):
            self.curses.calls.append((name,) + args)
        return call


class Curses(unittest.TestCase):
    """The real driver's own logic, on a stand-in ``curses`` (no terminal is ever opened)."""

    def test_keys_are_named(self):
        fake = FakeCurses(keys=[ord("2"), 259, 258, 10, 13, 343, 410, ord("?"), 27, -1])
        screen = tui.CursesScreen(fake, fake.screen, True)
        self.assertEqual([screen.getch(250) for _ in range(10)],
                         ["2", "up", "down", "enter", "enter", "enter", "resize", "?", "esc", None])
        self.assertIn(("timeout", 250), fake.calls)
        self.assertIn(("clear",), fake.calls)  # a resize clears, so the next draw is whole

    def test_draw_maps_styles_and_survives_the_last_cell(self):
        fake = FakeCurses(size=(2, 12))
        screen = tui.CursesScreen(fake, fake.screen, True)
        screen.draw([[("abc", "red bold"), ("def", "reverse")], [("x" * 12, "yellow dim")], [("ignored", "")]])
        self.assertEqual(fake.screen.drawn, [(0, 0, "abc", (1 << 8) | (1 << 21)), (0, 3, "def", 1 << 18),
                                             (1, 0, "x" * 12, (3 << 8) | (1 << 20))])
        plain = tui.CursesScreen(fake, fake.screen, False)
        self.assertEqual(plain.attr("red bold"), 1 << 21)  # no colour pairs without colours
        self.assertEqual(screen.size(), (12, 2))

    def test_open_screen(self):
        fake = FakeCurses()
        setlocale = mock.patch("locale.setlocale")  # the process's locale stays as the suite has it
        setlocale.start()
        self.addCleanup(setlocale.stop)
        with mock.patch.dict("sys.modules", {"curses": fake}):
            screen = tui.open_screen({"TERM": "xterm-256color"}, io.StringIO())
            self.assertIsNone(screen)  # a StringIO has no file descriptor
            stream = mock.Mock()
            stream.fileno.return_value = 1
            screen = tui.open_screen({"TERM": "xterm-256color"}, stream)
            self.assertTrue(screen.colours)
            self.assertIn(("setupterm", "xterm-256color", 1), fake.calls)
            self.assertFalse(tui.open_screen({"TERM": "xterm-256color", "NO_COLOR": "1"}, stream).colours)
            screen.close()
            screen.close()  # once only
            self.assertEqual([c[0] for c in fake.calls].count("endwin"), 1)
        failing = FakeCurses(fail="setupterm")
        with mock.patch.dict("sys.modules", {"curses": failing}):
            self.assertIsNone(tui.open_screen({"TERM": "dumb-unknown"}, stream))
        with mock.patch.dict("sys.modules", {"curses": None}):
            self.assertIsNone(tui.open_screen({}, stream))  # no curses module: the watcher prints its lines


def box_path(repo, name):
    folder = os.path.join(repo, ".herdr", "inbox", name, NEW)
    (only,) = os.listdir(folder)
    return os.path.join(folder, only)


class Harness(BoardCase):
    """The checks ``tests/support.py`` adds for invariant 22 catch what they must."""

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

    def test_focus_and_read_rules(self):
        watch_ = {"command": ["watch"]}
        listing = {"argv": ["agent", "list"], "exit": 0,
                   "info": {"states": {"fablyn-orchestrator": "blocked", "fablyn-dev": "idle", "caller": "blocked"}}}
        read_orch = {"argv": ["agent", "read", "fablyn-orchestrator", "--lines", "16"], "exit": 0, "info": {}}
        focus = {"argv": ["agent", "focus", "fablyn-dev"], "exit": 0, "info": {}}
        self.write_log(watch_, listing, read_orch, {"key": "2"}, focus, {"key": "g"}, focus)
        self.assert_safety()  # the allowed shapes pass
        os.remove(self.log_path)
        self.assert_caught(watch_, listing, focus)                                   # no key
        self.assert_caught(watch_, {"key": "a"}, focus)                              # not a go key
        self.assert_caught(watch_, {"key": "enter"}, focus)                          # Enter opens a page (25.1)
        self.assert_caught(watch_, {"key": "2"}, focus, focus)                       # one focus per key
        self.assert_caught(watch_, {"key": "2"}, {"command": ["watch"]}, focus)       # a key of another run
        self.assert_caught(watch_, read_orch)                                        # no agent list yet
        self.assert_caught(watch_, listing, dict(read_orch, argv=["agent", "read", "fablyn-dev", "--lines", "16"]))
        self.assert_caught(watch_, listing, dict(read_orch, argv=["agent", "read", "caller", "--lines", "16"]))
        self.assert_caught(watch_, listing, {"argv": ["agent", "send-keys", "fablyn-orchestrator", "enter"],
                                             "exit": 0, "info": {"keys": ["enter"]}})
        self.assert_caught({"command": ["status"]}, {"key": "2"}, focus)            # only go, the popup, the board

    def test_a_listed_state_is_the_one_that_counts(self):
        listing = {"argv": ["agent", "list"], "exit": 0, "info": {"states": {"fablyn-orchestrator": "blocked"}}}
        relisted = {"argv": ["agent", "list"], "exit": 0, "info": {"states": {"fablyn-orchestrator": "idle"}}}
        read = {"argv": ["agent", "read", "fablyn-orchestrator", "--lines", "16"], "exit": 0, "info": {}}
        self.assert_caught({"command": ["watch"]}, listing, relisted, read)
        self.write_log({"command": ["watch"]}, relisted, listing, read)
        self.assert_safety()
        os.remove(self.log_path)

    def test_the_fake_records_the_listed_states(self):
        self.blocked_orchestrator()
        self.run_cli("status")
        call = next(c for c in self.calls() if c["argv"] == ["agent", "list"])
        self.assertEqual(call["info"]["states"]["fablyn-orchestrator"], "blocked")
        self.assertEqual(call["info"]["states"]["caller"], "working")


if __name__ == "__main__":
    unittest.main()
