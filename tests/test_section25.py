"""Design section 25 (version 0.13.0): pages in the board.

25.1: the view stack (board, seat page, fleet, owner and contact pages, message bodies),
Esc, Backspace and ``q`` going back, Left and Right stepping through the seats and
contacts, number keys and Enter opening pages, ``g`` focusing a pane, ``z`` zooming the
strip, clicks, and every view's key hints at the end of its footer. 25.2: the seat
page's sections, ``p``, ``a`` and ``d``. 25.3: the fleet, owner and contact pages. 25.4:
bodies. The harness (``tests/support.py``) holds the board to invariant 22 as amended:
``agent read`` from ``watch`` for a blocked seat's refresh or right after a ``p``,
``pane zoom`` only right after a ``z`` and only on the strip's own pane, ``agent focus``
only right after a ``g`` or a number key."""

from __future__ import annotations

import datetime
import json
import os
import re
import unittest

import fake_herdr
from support import THREE_SEATS
from herdr_fleet import board, inbox, messages, watch
from herdr_fleet.fleetfile import WATCH_RATIO, load_fleet
from herdr_fleet.inbox import CUR, NEW, Message
from test_inbox import box, log_entries
from test_section24 import PERMISSION, SECRET, FakeCurses, Strip, ago
from herdr_fleet import tui

B = board
DOT = " \u00b7 "
HHMM = r"\d\d:\d\d"
WINNER = "winner-chronicles"
CONTACT_FLEET = THREE_SEATS + '''
[[contacts]]
name = "winner-chronicles"
match = { cwd = "%s" }
'''


class PageCase(Strip):
    """A live fleet whose seats never turn idle under the watcher's own waits, so only the
    keys deliver anything."""

    def setUp(self):
        super().setUp()
        self.world["behaviors"]["wait_results"] = {n: ["timeout"] * 40 for n in
                                                   ("fablyn-orchestrator", "fablyn-dev", "fablyn-reviewer")}

    def page(self, keys, size=(162, 20), idle=6, **kwargs):
        return self.run_board(keys, size=size, idle=idle, **kwargs)

    def titles(self):
        return [self.screen.text(i)[0] if self.screen.text(i) else "" for i in range(len(self.screen.draws))]

    def draw_titled(self, prefix, last=True):
        found = [i for i, title in enumerate(self.titles()) if title.startswith(prefix)]
        self.assertTrue(found, "no draw titled %r in %s" % (prefix, self.titles()))
        return self.screen.text(found[-1] if last else found[0])

    def message(self, sender, to, body, minutes=5, box_=CUR, delivery="sent", queued_by=None, log=True,
                store=True, fyi=False):
        """A message as an earlier send left it: its file (in ``box_``) and its log entry."""
        when = ago(minutes=minutes)
        ident = inbox.new_id(when, inbox.taken_shorts(self.repo))
        if store:
            inbox.write(self.repo, Message(id=ident, sender=sender, to=to, time=inbox.iso_local(when), body=body,
                                           fyi=fyi, queued_by=queued_by), box_)
        if log:
            entry = {"time": inbox.iso_local(when), "from": sender, "to": to, "kind": "send", "id": ident,
                     "bytes": len(body), "first_line": body.splitlines()[0], "delivery": delivery}
            if fyi:
                entry["fyi"] = True
            messages.append(self.repo, entry)
        return ident

    def contact(self):
        winner = os.path.join(self.tmp, "winner")
        os.makedirs(winner, exist_ok=True)
        self.write_fleet(CONTACT_FLEET % winner)
        fake_herdr.add_workspace(self.world, "w3", "winner", winner)
        fake_herdr.add_pane(self.world, "w3:p4", "w3", winner)
        fake_herdr.put_agent(self.world, "w3:p4", "claude", name=None, status="idle")


# -- 25.1: navigation ---------------------------------------------------------------------------------


class Navigation(PageCase):
    def test_enter_opens_a_seat_page_and_esc_backspace_and_q_go_back(self):
        code, _, err = self.page(["down", "enter", "esc", "enter", "backspace", "enter", "q", "q"])
        self.assertEqual(code, 0, err)
        kinds = []
        for title in self.titles():
            kind = "seat" if title.startswith("fablyn-dev" + DOT) else "board" if title.startswith("fablyn" + DOT) \
                else "?"
            if not kinds or kinds[-1] != kind:
                kinds.append(kind)
        self.assertEqual(kinds, ["board", "seat", "board", "seat", "board", "seat", "board"])
        self.assertIn("Ctrl-C stops the watcher", self.footers())  # q on the board itself
        self.assertFalse(self.argvs("agent", "focus"))  # Enter opens a page; it focuses nothing
        page = self.draw_titled("fablyn-dev" + DOT)
        self.assertEqual(page[0], "fablyn-dev \u00b7 \u25cb idle <1m \u00b7 claude \u00b7 w2:p2")
        self.assertIn("Esc back \u00b7 \u2191\u2193 scroll \u00b7 \u2190\u2192 seat", page[-1])  # cut from the right

    def test_number_keys_open_pages(self):
        self.page(["3", "esc", "9"])
        self.assertTrue(self.draw_titled("fablyn-reviewer" + DOT)[0].startswith(
            "fablyn-reviewer \u00b7 \u25cb idle <1m \u00b7 codex \u00b7 w2:p3"))
        self.assertIn("no seat 9 on the board", self.footers())
        self.assertFalse(self.argvs("agent", "focus"))

    def test_left_and_right_step_through_the_seats_and_contacts_and_wrap(self):
        self.contact()
        self.page(["1", "left", "left", "right", "right", "right"])
        order = []
        for title in self.titles():
            name = title.split(DOT, 1)[0].replace("\u2194 ", "")
            if name != "fablyn" and (not order or order[-1] != name):
                order.append(name)
        self.assertEqual(order, ["fablyn-orchestrator", WINNER, "fablyn-reviewer", WINNER, "fablyn-orchestrator",
                                 "fablyn-dev"])
        self.assertEqual(self.draw_titled("\u2194 " + WINNER + DOT)[0], "\u2194 winner-chronicles \u00b7 contact of "
                         "this fleet \u00b7 \u25cb idle <1m \u00b7 w3:p4")  # design 26.6

    def test_g_focuses_from_the_board_and_from_a_seat_page(self):
        self.page(["g", "2", "g", "esc", "g"])
        self.assertEqual(self.argvs("agent", "focus"), [["agent", "focus", "fablyn-orchestrator"],
                                                        ["agent", "focus", "fablyn-dev"],
                                                        ["agent", "focus", "fablyn-orchestrator"]])

    def test_z_zooms_the_strip_through_its_own_pane(self):
        code, _, err = self.page(["z", "2", "z"])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.argvs("pane", "zoom"), [["pane", "zoom", "w1:p1", "--toggle"]] * 2)
        self.assertFalse(self.world["panes"]["w1:p1"].get("zoomed"))  # zoomed, then back
        self.assertTrue(self.draw_titled("fablyn-dev" + DOT))  # z works on a page too

    def test_z_without_a_pane_id_uses_the_strip_the_state_file_records(self):
        fake_herdr.add_pane(self.world, "w2:p9", "w2", self.repo, created=True)
        self.world["panes"]["w2:p9"]["plugin"] = "watch"
        data = self.read_state()
        data["watch"] = {"pane_id": "w2:p9", "created_pane": True, "started": "2026-10-24T09:00:00+11:00"}
        self.write_state(data)
        del self.env["HERDR_PANE_ID"]
        self.page(["z"])
        self.assertEqual(self.argvs("pane", "zoom"), [["pane", "zoom", "w2:p9", "--toggle"]])
        self.assertTrue(self.world["panes"]["w2:p9"]["zoomed"])

    def test_z_is_ignored_when_no_pane_is_known(self):
        del self.env["HERDR_PANE_ID"]
        code, _, err = self.page(["z"])
        self.assertEqual(code, 0, err)
        self.assertFalse(self.argvs("pane", "zoom"))

    def test_clicks(self):
        # rows: 0 header, 1 orchestrator, 2 dev, 3 reviewer, ... the footer is the last row
        self.page([("click", 2, 5), ("click", 7, 7), ("click", 0, 0), ("click", 3, 3), ("click", 0, 30),
                   ("click", 1, 1), ("click", 19, 10), ("click", 12, 3)])
        titles = []
        for title in self.titles():
            if not titles or titles[-1] != title:
                titles.append(title)
        heads = [t.split(DOT)[0] if not t.startswith("fablyn" + DOT) else
                 ("fleet" if t.endswith(DOT + "fleet") else "board") for t in titles]
        self.assertEqual(heads, ["board", "fablyn-dev", "board", "fleet", "board", "owner", "board"])
        self.assertFalse(self.argvs("agent", "focus"))  # a click opens; it never focuses

    def test_the_owner_cell_is_where_its_text_is(self):
        model = B.Board(fleet="fablyn", watching=60)
        left, _, (start, end) = B.header_layout(model, 162)
        self.assertEqual(left[start:end], "owner: nothing")
        self.assertEqual(B.hit_test(model, 162, 6, 0, start), ("owner",))
        self.assertEqual(B.hit_test(model, 162, 6, 0, start - 1), ("fleet",))
        self.assertEqual(B.hit_test(model, 162, 6, 5, 3), ("footer",))
        self.assertIsNone(B.hit_test(model, 162, 6, 2, 3))  # no seat on that row


# -- 25.2: the seat page --------------------------------------------------------------------------------


class SeatPages(PageCase):
    def busy_dev(self):
        """fablyn-dev: working on a task, two messages queued (one on refusal), owing the
        orchestrator a reply and owed one by the reviewer, a kickoff in its past."""
        self.status_of("w2:p2", "working")
        fake_herdr.set_title(self.world, "w2:p2", "vitest run")
        messages.append(self.repo, messages.make_entry("herdr-fleet", "fablyn-dev", "kickoff", "you are fablyn-dev",
                                                       time=inbox.iso_local(ago(minutes=30))))
        self.asked = self.message("fablyn-orchestrator", "fablyn-dev", "ROUND 1: please review", minutes=20)
        self.owed = self.message("fablyn-dev", "fablyn-reviewer", "check the ocean tick", minutes=15)
        self.first = self.message("fablyn-orchestrator", "fablyn-dev", "two 1.30.4 items\nthe rect floor", minutes=4,
                                  box_=NEW, delivery="queued", queued_by="refusal")
        self.second = self.message("fablyn-orchestrator", "fablyn-dev", "and bump the registry", minutes=3,
                                   box_=NEW, delivery="queued", queued_by="queue")

    def test_the_sections(self):
        self.busy_dev()
        self.page(["2"])
        page = self.draw_titled("fablyn-dev" + DOT)
        self.assertEqual(page[0], "fablyn-dev \u00b7 \u25cf working <1m \u00b7 claude \u00b7 w2:p2")
        body = page[1:-1]
        labels = [line[:10].strip() for line in body if line[:10].strip()]
        self.assertEqual(labels, ["doing", "queue", "debts", "recent"])
        self.assertEqual(body[0], "doing     vitest run")
        self.assertRegex(body[1], r'^queue     #%s %s from fablyn-orchestrator \[on refusal\]  "two 1\.30\.4 items"$'
                         % (inbox.short_of(self.first), HHMM))
        self.assertRegex(body[2], r'^          #%s %s from fablyn-orchestrator \[queued\]  "and bump the registry"$'
                         % (inbox.short_of(self.second), HHMM))
        self.assertRegex(body[3], r'^debts     owes fablyn-orchestrator since %s \(#%s\)  "ROUND 1: please review"$'
                         % (HHMM, inbox.short_of(self.asked)))
        self.assertRegex(body[4], r'^          owed by fablyn-reviewer since %s \(#%s\)  "check the ocean tick"$'
                         % (HHMM, inbox.short_of(self.owed)))
        recent = [line[10:] for line in body[5:] if line]
        self.assertRegex(recent[0], r'^%s \u2190 fablyn-orchestrator #%s queued  "and bump the registry"$'
                         % (HHMM, inbox.short_of(self.second)))
        self.assertRegex(recent[2], r'^%s \u2192 fablyn-reviewer #%s sent  "check the ocean tick"$'
                         % (HHMM, inbox.short_of(self.owed)))
        self.assertRegex(recent[-1], r"^%s kickoff$" % HHMM)
        self.assertFalse(self.argvs("agent", "read"))  # no screen without p

    def test_empty_sections_are_left_out_and_a_blocked_seat_shows_its_dialog(self):
        fake_herdr.set_screen(self.world, "w2:p1", PERMISSION)
        self.status_of("w2:p1", "blocked")
        self.page(["3", "esc", "1"])
        reviewer = self.draw_titled("fablyn-reviewer" + DOT)
        self.assertEqual([line for line in reviewer[1:-1] if line], [])  # nothing to show
        orch = self.draw_titled("fablyn-orchestrator" + DOT)
        self.assertEqual(orch[1:5], ["dialog    Allow Bash(pnpm test)?", "          \u276f 1. Yes",
                                     "          2. Yes, and don't ask again for pnpm test commands",
                                     "          3. No, and tell Claude what to do differently"])
        self.assertNotIn(SECRET, "\n".join("\n".join(self.screen.text(i)) for i in range(len(self.screen.draws))))

    def test_scrolling(self):
        for minute in range(30, 5, -1):
            self.message("fablyn-orchestrator", "fablyn-reviewer", "note %d" % minute, minutes=minute, fyi=True)
        self.page(["3"] + ["down"] * 7 + ["pagedown", "pageup"], size=(162, 8))
        draws = [self.screen.text(i) for i in range(len(self.screen.draws)) if self.titles()[i].startswith(
            "fablyn-reviewer" + DOT)]
        self.assertEqual(draws[0][1][:10], "recent    ")  # the top of the page
        self.assertRegex(draws[0][1], r'"note 6"$')    # newest first
        tops = [re.search(r'"(note \d+)"', d[1]).group(1) for d in draws]
        self.assertEqual(sorted(set(tops), key=tops.index), ["note 6", "note 7", "note 8", "note 14", "note 13"])
        # the first entry selected on opening, seven Downs: the selection on the eighth entry,
        # the page scrolled to keep it in view; a page down (six rows) and back up
        selected = [(i, line) for i, draw in enumerate(self.screen.draws) for line in draw
                    if any("reverse" in style for _, style in line)]
        self.assertTrue(selected)

    def test_p_reads_the_screen_only_then_and_the_page_drops_it(self):
        fake_herdr.set_screen(self.world, "w2:p2", "\u23fa ran the tests\n  12 passed\n" + fake_herdr.prompt_screen(
            "claude"))
        self.page(["2", "p", "esc", "2"])
        self.assertEqual(self.argvs("agent", "read"), [["agent", "read", "fablyn-dev", "--lines", "12"]])
        with_screen = self.draw_titled("fablyn-dev" + DOT, last=False)
        shown = [i for i in range(len(self.screen.draws)) if any(
            line.startswith("screen    ") for line in self.screen.text(i))]
        self.assertTrue(shown)
        lines = self.screen.text(shown[0])
        at = next(i for i, line in enumerate(lines) if line.startswith("screen"))
        self.assertEqual(lines[at], "screen    \u23fa ran the tests")
        self.assertEqual(lines[at + 1], "            12 passed")
        self.assertTrue(with_screen)
        reopened = self.draw_titled("fablyn-dev" + DOT)  # the last draw: the page opened again
        self.assertFalse(any(line.startswith("screen") for line in reopened))

    def test_a_acks_the_selected_debt(self):
        self.busy_dev()
        self.page(["2", "a", "down", "down", "a", "y"])  # the first queued message starts selected
        self.assertIn("select a debt with \u2191 and \u2193 first", self.footers())
        self.assertTrue(any(re.match(r"^ack #%s \(fablyn-dev owes fablyn-orchestrator \d+m\)\? y/n$"
                                     % inbox.short_of(self.asked), f) for f in self.footers()), self.footers())
        (ack,) = [e for e in log_entries(self.repo) if e["kind"] == "ack"]
        self.assertEqual((ack["id"], ack["by"], ack["reason"], ack["from"]),
                         (self.asked, "owner", "from the board", "fablyn-dev"))
        page = self.draw_titled("fablyn-dev" + DOT)
        self.assertFalse(any("owes fablyn-orchestrator" in line for line in page))  # settled, on the reload
        self.assertFalse(self.argvs("agent", "prompt"))


class Deliver(PageCase):
    def queued(self, to):
        return self.message("fablyn-orchestrator" if to != "fablyn-orchestrator" else "fablyn-dev", to, "a task",
                            box_=NEW, delivery="queued", queued_by="refusal")

    def test_d_delivers_an_idle_seats_queue_at_once(self):
        ident = self.queued("fablyn-reviewer")
        self.page(["3", "d"])
        self.assertEqual(len(self.prompts_to("fablyn-reviewer")), 1)
        self.assertEqual(box(self.repo, "fablyn-reviewer", NEW), [])
        self.assertIn("delivered 1 to fablyn-reviewer", self.watch_log())
        self.assertNotIn("fablyn-reviewer idle; delivering 1", self.watch_log())  # no debounce
        page = self.draw_titled("fablyn-reviewer" + DOT)
        self.assertFalse(any(line.startswith("queue") for line in page))
        self.assertTrue(any("delivered via watch" in line and inbox.short_of(ident) in line for line in page))

    def test_every_reason_d_gives_for_not_delivering(self):
        for name, pane in (("fablyn-dev", "w2:p2"), ("fablyn-orchestrator", "w2:p1")):
            self.queued(name)
        self.status_of("w2:p2", "working")
        self.status_of("w2:p1", "blocked")
        self.page(["2", "d", "left", "d"])
        self.assertIn("working: lands at its next idle", self.footers())
        self.assertIn("blocked: a dialog is open", self.footers())
        self.status_of("w2:p2", "idle")
        fake_herdr.type_in(self.world, "w2:p2", "half typed")
        self.page(["2", "d"])
        self.assertIn("text in its input", self.footers())
        fake_herdr.focus(self.world, "w2:p2")
        self.page(["2", "d"])
        self.assertIn("owner typing", self.footers())
        self.world["panes"]["w2:p3"]["agent"] = None
        self.queued("fablyn-reviewer")
        self.page(["3", "d"])
        self.assertIn("absent: its next kickoff carries the queue", self.footers())
        self.page(["1", "esc", "o", "esc", "2", "right", "right", "right", "d"])
        self.assertFalse(self.prompts_to("fablyn-dev") + self.prompts_to("fablyn-orchestrator")
                         + self.prompts_to("fablyn-reviewer"))

    def test_nothing_queued(self):
        self.page(["2", "d"])
        self.assertIn("nothing queued for fablyn-dev", self.footers())


# -- 25.3: the fleet, owner and contact pages -------------------------------------------------------------


class OtherPages(PageCase):
    def test_the_fleet_page(self):
        self.message("fablyn-orchestrator", "fablyn-dev", "please review", minutes=50)
        self.message("fablyn-orchestrator", "fablyn-dev", "queued work", box_=NEW, delivery="queued",
                     queued_by="queue")
        code, _, err = self.page(["up", "enter"], size=(162, 24))
        self.assertEqual(code, 0, err)
        page = self.draw_titled("fablyn" + DOT + "fleet")
        self.assertEqual(page[1], "fleet file  %s" % os.path.join(self.repo, ".herdr", "fleet.toml"))
        self.assertEqual(page[2], "workspace   fablyn (w2)")
        self.assertEqual(page[3], "layout      chain")  # design 26.7: mode, and `top` when set
        self.assertRegex(page[4], r"^watcher     pid %d \u00b7 up <1m \u00b7 lock \.herdr/watch\.lock since %s$"
                         % (os.getpid(), HHMM))
        self.assertEqual(page[5], "waiting on  nothing")  # waits time out at once on the fake
        self.assertIn("awaiting: fablyn-dev owes fablyn-orchestrator for 50m", "\n".join(page))
        self.assertIn("queued: fablyn-dev 1", page)
        self.assertIn("drift, worktrees, hooks and doctor's checks: herdr-fleet status, herdr-fleet doctor", page)
        at = next(i for i, line in enumerate(page) if line.startswith("\u2500\u2500 .herdr/watch.log"))
        self.assertRegex(page[at + 1], r"^\d\d:\d\d:\d\d  watching ")
        self.assertTrue(page[-1].endswith(B.HINTS_FLEET))
        self.assertFalse(self.argvs("pane", "process-info"))  # drift is not read (invariant 22)

    def test_the_owner_page_and_its_cell(self):
        old = self.message("fablyn-dev", "owner", "1.28.0 shipped", minutes=40)
        fyi = self.message("fablyn-reviewer", "owner", "FYI: review done", minutes=20, fyi=True)
        messages.append(self.repo, {"time": inbox.iso_local(ago(minutes=10)), "from": WINNER, "to": "owner",
                                    "kind": "send", "bytes": 9, "first_line": "an old one", "delivery": "sent"})
        code, _, err = self.page(["o", "a", "y", "down", "a", "down", "enter"], size=(162, 12))
        self.assertEqual(code, 0, err)
        board_draw = self.screen.text(0)
        self.assertRegex(board_draw[0], r'owner: %s from winner-chronicles "an old one"' % HHMM)  # the newest
        page = self.draw_titled("owner" + DOT, last=False)
        self.assertEqual(page[0], "owner \u00b7 3 messages")
        self.assertRegex(page[1], r'^%s \(no id\) from winner-chronicles sent  "an old one"$' % HHMM)
        self.assertRegex(page[2], r'^%s #%s from fablyn-reviewer sent  "FYI: review done"$' % (HHMM,
                                                                                               inbox.short_of(fyi)))
        self.assertRegex(page[3], r'^%s #%s from fablyn-dev sent, owed  "1\.28\.0 shipped"$' % (HHMM,
                                                                                                inbox.short_of(old)))
        self.assertIn("a message without an id: ack it by name (herdr-fleet ack NAME)", self.footers())
        self.assertIn("#%s: nothing owed (an FYI, answered, or not delivered)" % inbox.short_of(fyi), self.footers())
        self.assertFalse([e for e in log_entries(self.repo) if e["kind"] == "ack"])
        body = self.draw_titled("#%s" % inbox.short_of(old))
        self.assertRegex(body[0], r"^#%s \u00b7 %s \u00b7 fablyn-dev \u2192 owner \u00b7 sent \u00b7 14 B$"
                         % (inbox.short_of(old), HHMM))
        self.assertEqual(body[1], "1.28.0 shipped")

    def test_acking_an_owner_debt(self):
        old = self.message("fablyn-dev", "owner", "1.28.0 shipped", minutes=40)
        self.page(["o", "down", "a", "y"])
        (ack,) = [e for e in log_entries(self.repo) if e["kind"] == "ack"]
        self.assertEqual((ack["id"], ack["from"], ack["to"], ack["by"]), (old, "owner", "fablyn-dev", "owner"))

    def test_a_contact_page(self):
        self.contact()
        sent = self.message("fablyn-dev", WINNER, "your 1.28 build is live", minutes=9)
        got = self.message(WINNER, "fablyn-dev", "thanks, verified", minutes=4)
        self.page(["down", "down", "down", "enter", "enter"], size=(162, 12))  # the newest message preselected
        page = self.draw_titled("\u2194 " + WINNER + DOT, last=False)
        self.assertEqual(page[0], "\u2194 winner-chronicles \u00b7 contact of this fleet \u00b7 \u25cb idle <1m "
                                  "\u00b7 w3:p4")
        self.assertRegex(page[1], r'^messages  %s \u2192 fablyn-dev #%s sent  "thanks, verified"$'
                         % (HHMM, inbox.short_of(got)))
        self.assertRegex(page[2], r'^          %s \u2190 fablyn-dev #%s sent  "your 1\.28 build is live"$'
                         % (HHMM, inbox.short_of(sent)))
        self.assertTrue(page[-1].endswith(B.HINTS_CONTACT))
        body = self.draw_titled("#%s" % inbox.short_of(got))
        self.assertEqual(body[1], "thanks, verified")


# -- 25.4: bodies ---------------------------------------------------------------------------------------


class Bodies(PageCase):
    def test_a_stored_body_long_and_scrolled(self):
        text = "\n".join("line %02d of the review" % n for n in range(1, 41)) + "\n"
        ident = self.message("fablyn-orchestrator", "fablyn-dev", text, box_=NEW, delivery="queued",
                             queued_by="refusal")
        self.page(["2", "enter", "down", "down", "pagedown", "pagedown", "pageup", "esc", "esc"],
                  size=(100, 8))
        title = "#%s" % inbox.short_of(ident)
        draws = [self.screen.text(i) for i in range(len(self.screen.draws)) if self.titles()[i].startswith(title)]
        self.assertRegex(draws[0][0], r"^#%s \u00b7 %s \u00b7 fablyn-orchestrator \u2192 fablyn-dev \u00b7 queued "
                         r"\(on refusal\) \u00b7 \d+ B$" % (inbox.short_of(ident), HHMM))
        firsts = [d[1] for d in draws]
        self.assertEqual(firsts[0], "line 01 of the review")
        self.assertIn("line 03 of the review", firsts)   # two lines down
        self.assertIn("line 15 of the review", firsts)   # two pages further (six lines a page)
        self.assertIn("line 09 of the review", firsts)   # and one back
        self.assertTrue(draws[0][-1].endswith(B.HINTS_BODY))
        self.assertEqual(box(self.repo, "fablyn-dev", NEW), [ident])  # reading it moved nothing
        self.assertTrue(self.titles()[-1].startswith("fablyn" + DOT))   # two Esc: back on the board

    def test_a_body_line_is_cut_never_wrapped(self):
        self.message("fablyn-orchestrator", "fablyn-dev", "x" * 300, box_=NEW, delivery="queued", queued_by="queue")
        self.page(["2", "enter"], size=(60, 8))
        body = self.screen.text()
        self.assertEqual(body[1], "x" * 59 + "\u2026")

    def test_a_log_only_message(self):
        messages.append(self.repo, {"time": inbox.iso_local(ago(minutes=8)), "from": "fablyn-orchestrator",
                                    "to": "fablyn-dev", "kind": "send", "bytes": 20, "first_line": "from before 0.6",
                                    "delivery": "sent"})
        self.page(["2", "down", "enter"])  # its debt is preselected; the recent entry below it
        body = self.screen.text()
        self.assertRegex(body[0], r"^\(no id\) \u00b7 %s \u00b7 fablyn-orchestrator \u2192 fablyn-dev \u00b7 sent$"
                         % HHMM)
        self.assertEqual(body[1:3], ["from before 0.6", "(no stored body)"])


# -- hints ------------------------------------------------------------------------------------------------


class Hints(unittest.TestCase):
    def test_hints_are_cut_from_the_right_at_every_breakpoint(self):
        footer = B.Footer(event=B.Event("08:02", "delivered 2 to fablyn-dev"))
        for hints in (B.HINTS_BOARD, B.HINTS_SEAT, B.HINTS_BODY, B.HINTS_FLEET, B.HINTS_OWNER, B.HINTS_CONTACT):
            items = hints.split(DOT)
            for width in (162, 120, 90, 60, 40, 20):
                with self.subTest(hints=hints, width=width):
                    line = B.footer_spans(width, footer, hints, 0, True)
                    self.assertEqual(sum(B.width_of(t) for t, _ in line), width)
                    shown = "".join(t for t, _ in line).rstrip()
                    kept = next(k for k in range(len(items), -1, -1)
                                if k == 0 or shown.endswith("  " + DOT.join(items[:k])))  # whole hints, the first ones
                    if width >= 120:
                        self.assertEqual(kept, len(items))
                    if width >= 60:
                        self.assertGreaterEqual(kept, 2)
                    self.assertTrue(shown.startswith("08:02 deliv"))
        self.assertEqual(B.fit_hints(B.HINTS_SEAT, 25), "Esc back \u00b7 \u2191\u2193 scroll")
        self.assertEqual(B.fit_hints(B.HINTS_SEAT, 3), "")
        notice = B.footer_spans(40, B.Footer(notice="ack #62437b (a owes b 2h)? y/n"), B.HINTS_BOARD, 0, True)
        self.assertEqual("".join(t for t, _ in notice).rstrip(), "ack #62437b (a owes b 2h)? y/n")  # no hints

    def test_the_view_hints_are_25_1s(self):
        self.assertEqual(B.HINTS_BOARD, "Enter open \u00b7 g go \u00b7 a ack \u00b7 d deliver \u00b7 z zoom \u00b7 "
                                        "l log \u00b7 ? help")
        self.assertEqual(B.HINTS_SEAT, "Esc back \u00b7 \u2191\u2193 scroll \u00b7 \u2190\u2192 seat \u00b7 g go "
                                       "\u00b7 a ack \u00b7 d deliver \u00b7 p screen \u00b7 z zoom")
        self.assertEqual(B.HINTS_BODY, "Esc back \u00b7 \u2191\u2193 scroll")


# -- the rest of 25: the harness, the ratio, the guard's placeholder ----------------------------------------


class Harness(PageCase):
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

    def test_read_zoom_and_focus_rules(self):
        watch_ = {"command": ["watch"]}
        idle = {"argv": ["agent", "list"], "exit": 0, "info": {"states": {"fablyn-dev": "idle"}}}
        read = {"argv": ["agent", "read", "fablyn-dev", "--lines", "12"], "exit": 0, "info": {}}
        zoom = {"argv": ["pane", "zoom", "w1:p1", "--toggle"], "exit": 0, "info": {"own": True}}
        strip = {"argv": ["pane", "zoom", "w2:p9", "--toggle"], "exit": 0, "info": {"plugin": "watch"}}
        focus = {"argv": ["agent", "focus", "fablyn-dev"], "exit": 0, "info": {}}
        self.write_log(watch_, idle, {"key": "p"}, read, {"key": "z"}, zoom, {"key": "z"}, strip,
                       {"key": "g"}, focus, {"key": "3"}, focus)
        self.assert_safety()  # the allowed shapes pass
        os.remove(self.log_path)
        self.assert_caught(watch_, idle, read)                                   # an idle seat, no p
        self.assert_caught(watch_, idle, {"key": "p"}, read, read)               # one read per p
        self.assert_caught(watch_, zoom)                                         # no z
        self.assert_caught(watch_, {"key": "x"}, zoom)                           # not z
        self.assert_caught(watch_, {"key": "z"}, zoom, zoom)                     # one zoom per z
        self.assert_caught(watch_, {"key": "z"}, dict(strip, info={"plugin": None}))   # not its own pane
        self.assert_caught({"command": ["status"]}, {"key": "z"}, zoom)          # only the board zooms
        self.assert_caught(watch_, {"key": "click:2:5"}, focus)                  # a click opens, never focuses
        self.assert_caught(watch_, {"key": "enter"}, focus)

    def test_the_fake_records_a_zoom(self):
        self.page(["z"])
        (call,) = [c for c in self.calls() if c["argv"][:2] == ["pane", "zoom"]]
        self.assertEqual((call["info"]["pane"], call["info"]["own"], call["info"]["current"]), ("w1:p1", True, False))


# -- 25.8: the polish after the live demo ---------------------------------------------------------------


class Polish(PageCase):
    def test_the_board_starts_on_the_first_seat_so_down_enter_opens_the_second(self):
        self.page(["down", "enter"])
        first = self.screen.draws[0]
        self.assertTrue(all("reverse" in style for _, style in first[1]))     # the first seat row
        self.assertFalse(any("reverse" in style for _, style in first[0]))    # not the header
        self.assertTrue(self.titles()[-1].startswith("fablyn-dev" + DOT))

    def test_pages_open_with_their_first_entry_selected(self):
        self.contact()
        queued = self.message("fablyn-orchestrator", "fablyn-dev", "a queued task", box_=NEW, delivery="queued",
                              queued_by="refusal")
        to_owner = self.message("fablyn-dev", "owner", "for you", minutes=2)
        said = self.message(WINNER, "fablyn-dev", "from the contact", minutes=1)
        self.page(["2", "enter", "esc", "esc", "o", "enter", "esc", "esc", "down", "down", "down", "enter", "enter"])
        opened = [t.split(DOT)[0] for t in self.titles() if t.startswith("#")]
        self.assertEqual(sorted(set(opened), key=opened.index),
                         ["#" + inbox.short_of(queued), "#" + inbox.short_of(to_owner), "#" + inbox.short_of(said)])
        self.assertNotIn("select a message with \u2191 and \u2193 first", self.footers())

    def test_the_opening_line_is_short_in_the_footer_and_whole_in_the_log(self):
        self.page([])
        self.assertRegex(self.footers()[0], r"^%s watching repo \(pid %d\)  " % (HHMM, os.getpid()))
        self.assertEqual(self.watch_log()[0], "watching %s (pid %d, debounce 5000 ms); Ctrl-C stops"
                         % (os.path.join(self.repo, ".herdr", "fleet.toml"), os.getpid()))


class MouseCurses(FakeCurses):
    def __init__(self, keys=(), mouse=None):
        super().__init__(keys=keys)
        self.mouse = mouse

    def getmouse(self):
        return self.mouse


def raw(text):
    return [ord(c) for c in text]


class Sequences(unittest.TestCase):
    """25.8: the curses driver turns SGR and X10 clicks into clicks and drops every other
    escape sequence whole, so no byte of one reaches the board as a key (a digit would
    open a seat's page, and `g` would move the owner's focus)."""

    def keys(self, data, mouse=None):
        fake = MouseCurses(keys=data, mouse=mouse)
        screen = tui.CursesScreen(fake, fake.screen, True)
        got = []
        while fake.keys:
            got.append(screen.getch(250))
        return got

    def test_sgr_clicks(self):
        self.assertEqual(self.keys(raw("\x1b[<0;6;3M")), [("click", 2, 5)])
        self.assertEqual(self.keys(raw("\x1b[<0;6;3M\x1b[<0;6;3m")), [("click", 2, 5), None])  # the release: nothing
        self.assertEqual(self.keys(raw("\x1b[<2;6;3M")), [None])    # the right button
        self.assertEqual(self.keys(raw("\x1b[<32;6;3M")), [None])   # a drag
        self.assertEqual(self.keys(raw("\x1b[<64;6;3M")), [None])   # the wheel
        self.assertEqual(self.keys(raw("\x1b[<4;120;40M")), [("click", 39, 119)])  # shift held: still a click

    def test_x10_clicks(self):
        self.assertEqual(self.keys(raw("\x1b[M") + [32, 32 + 6, 32 + 3]), [("click", 2, 5)])
        self.assertEqual(self.keys(raw("\x1b[M") + [35, 32 + 6, 32 + 3]), [None])   # the release
        fake_key = FakeCurses.KEY_MOUSE
        self.assertEqual(self.keys([fake_key], mouse=(0, 5, 2, 0, FakeCurses.BUTTON1_PRESSED)), [("click", 2, 5)])
        self.assertEqual(self.keys([fake_key], mouse=(0, 5, 2, 0, 0)), [None])        # decoded by curses, not a press

    def test_other_sequences_are_dropped_whole(self):
        self.assertEqual(self.keys(raw("\x1b[1;5A2")), [None, "2"])   # an unknown CSI, then a real key
        self.assertEqual(self.keys(raw("\x1b[200~g")), [None, "g"])   # bracketed paste start
        self.assertEqual(self.keys(raw("\x1bOP")), [None])            # SS3 (F1)
        self.assertEqual(self.keys(raw("\x1b2")), [None])             # Alt-2: no number key
        self.assertEqual(self.keys(raw("\x1b[<" + "1;" * 20)), [None])  # too long or cut short: nothing
        self.assertEqual(self.keys(raw("\x1b")), ["esc"])             # a lone Esc is Esc

    def test_the_digits_of_a_click_never_open_a_page(self):
        got = self.keys(raw("\x1b[<0;2;3M\x1b[<0;2;3m\x1b[<0;9;9M"))
        self.assertFalse([k for k in got if isinstance(k, str)])


class Defaults(unittest.TestCase):
    def test_the_strip_ratio_is_now_0_2_and_the_starter_says_so(self):
        from herdr_fleet import scaffold
        self.assertEqual(WATCH_RATIO, 0.2)
        self.assertIn("# ratio = 0.2", scaffold.STARTER_FLEET)
        self.assertIn("# [watch]", scaffold.STARTER_FLEET)

    def test_the_fakes_guard_takes_claudes_suggestion_for_an_empty_line(self):
        world = fake_herdr.new_world()
        fake_herdr.add_workspace(world, "w1", "x")
        fake_herdr.put_agent(world, "w1:p1", "claude", "a")
        fake_herdr.set_screen(world, "w1:p1", fake_herdr.prompt_screen("claude", 'Try "fix lint errors"'))
        self.assertFalse(fake_herdr.input_held(world, "w1:p1"))
        fake_herdr.focus(world, "w1:p1")
        self.assertFalse(fake_herdr.owner_typing(world, "w1:p1"))
        fake_herdr.type_in(world, "w1:p1", 'Try "x" and more')
        self.assertTrue(fake_herdr.input_held(world, "w1:p1"))


if __name__ == "__main__":
    unittest.main()
