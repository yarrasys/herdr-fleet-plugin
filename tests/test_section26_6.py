"""Design 26.6 (version 0.14.1): board follow-ups from the 0.14.0 rollout.

Contact rows are marked (``↔ `` before the name, the whole row in the dim ``contact``
style, on the board and in the packed form; the contact page's header reads ``↔ NAME ·
contact of this fleet · STATE · PANE``), and number keys and Enter still open them. The
watcher's board reads the fleet file again when its mtime changed, before building the
model: an added or removed contact or a new label shows within a refresh, a file that no
longer parses keeps the last good fleet with the error in the footer (red) and the log,
and mailbox waiters only follow the file at their next pass. ``--log`` mode never reloads."""

from __future__ import annotations

import os
import unittest

from support import THREE_SEATS
from herdr_fleet import board
from test_inbox import put
from test_section24 import sample
from test_section25 import PageCase
from test_watch import StopClock

B = board
DOT = " · "
CONTACT = '''
[[contacts]]
name = "docs-channel"
match = { cwd = "%s" }
'''


def contact_row(**extra):
    values = dict(name="docs-channel", shown="docs-channel", state="idle", index=4, age=60, contact=True,
                  queued=1, oldest=120, next_id="aaaaaa", next_line="hello")
    values.update(extra)
    return B.Row(**values)


class Marker(unittest.TestCase):
    def test_a_contact_row_is_marked_and_dim(self):
        board_ = sample(contacts=[contact_row()])
        lines = B.render(board_, 162, 8, True)
        row = lines[5]
        text = "".join(t for t, _ in row)
        self.assertTrue(text.startswith("↔ docs-channel      ○ idle 1m"))
        self.assertEqual({style for _, style in row}, {"contact"})  # the whole row, its state colour included
        self.assertEqual({style for _, style in lines[2]} - {""}, {"dim", "yellow"})  # a seat keeps its styles
        board_.selected = "docs-channel"
        self.assertEqual({style for _, style in B.render(board_, 162, 8, True)[5]}, {"contact reverse"})
        plain = B.render(board_, 162, 8, False)[5]
        self.assertEqual({style for _, style in plain}, {"reverse"})  # no dim without colours; the marker stays
        self.assertTrue("".join(t for t, _ in plain).startswith("↔ docs-channel"))

    def test_packed_rows_mark_the_contact_too(self):
        board_ = sample(contacts=[contact_row()])
        board_.rows += [B.Row("sample-qa", "sample-qa", "done", 4, age=30),
                        B.Row("sample-ops", "sample-ops", "absent", 5)]
        lines = B.render(board_, 162, 6, True)
        line = lines[4]  # six seats on rows 1 to 3, the contact on the left of row 4
        self.assertTrue("".join(t for t, _ in line).startswith("↔ docs-channel      ○ idle 1m     1 queued"))
        left = []
        used = 0
        for text, style in line:
            if used >= 81:
                break
            left.append(style)
            used += B.width_of(text)
        self.assertEqual(set(left), {"contact"})

    def test_the_contact_page_header(self):
        page = B.ContactPage("docs-channel")
        page.row = contact_row(pane="w4:pG")
        self.assertEqual(page.title(), "↔ docs-channel · contact of this fleet · ○ idle 1m "
                                       "· w4:pG")
        page.row = None
        self.assertEqual(page.title(), "↔ docs-channel · contact of this fleet · ? unknown "
                                       "· no pane")


class ContactKeys(PageCase):
    def test_number_keys_and_enter_open_a_contact_page(self):
        self.contact()
        self.page(["4", "esc", "down", "down", "down", "enter"])
        titles = [t for t in self.titles() if t.startswith("↔ ")]
        self.assertTrue(titles)
        self.assertTrue(all(t.startswith("↔ partner-workspace · contact of this fleet") for t in titles))
        self.assertGreaterEqual(len({i for i, t in enumerate(self.titles()) if t.startswith("↔ ")}), 2)
        self.assertFalse(self.argvs("agent", "focus"))


class Reload(PageCase):
    def edit(self, text):
        """Rewrite the fleet file with a later mtime, as an editor would."""
        def apply():
            path = os.path.join(self.repo, ".herdr", "fleet.toml")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
            self.stamp += 10 ** 9
            os.utime(path, ns=(self.stamp, self.stamp))
        return apply

    def setUp(self):
        super().setUp()
        self.stamp = os.stat(os.path.join(self.repo, ".herdr", "fleet.toml")).st_mtime_ns
        self.channel = os.path.join(self.tmp, "explain")
        os.makedirs(self.channel)

    def board_rows(self):
        """Each board draw's row names (the first column), in draw order."""
        found = []
        for index in range(len(self.screen.draws)):
            lines = self.screen.text(index)
            if lines and lines[0].startswith("sample" + DOT):
                found.append([line[:20].rstrip() for line in lines[1:-1] if line.strip()])
        return found

    def test_an_added_contact_a_new_label_and_a_removed_contact_show_within_a_refresh(self):
        added = THREE_SEATS + CONTACT % self.channel
        labelled = added.replace('name = "sample-dev"', 'name = "sample-dev"\nlabel = "DEV"')
        code, _, err = self.page([self.edit(added), ("wait", 5.0), self.edit(labelled), ("wait", 5.0),
                                  self.edit(THREE_SEATS), ("wait", 5.0)], size=(162, 8))
        self.assertEqual(code, 0, err)
        shapes = []
        for rows in self.board_rows():
            if not shapes or shapes[-1] != rows:
                shapes.append(rows)
        self.assertEqual(shapes, [
            ["sample-orchestrator", "sample-dev", "sample-reviewer"],
            ["sample-orchestrator", "sample-dev", "sample-reviewer", "↔ docs-channel"],
            ["sample-orchestrator", "DEV", "sample-reviewer", "↔ docs-channel"],
            ["sample-orchestrator", "sample-dev", "sample-reviewer"]])
        reloaded = [line for line in self.watch_log() if "changed; reloaded" in line]
        self.assertEqual(len(reloaded), 3)
        self.assertTrue(reloaded[0].startswith("fleet file %s" % os.path.join(self.repo, ".herdr", "fleet.toml")))

    def test_a_broken_edit_keeps_the_last_good_fleet_until_it_parses(self):
        added = THREE_SEATS + CONTACT % self.channel
        broken = added + "\n[[agents]\nname = \n"
        self.page([self.edit(added), ("wait", 5.0), self.edit(broken), ("wait", 5.0), ("wait", 5.0),
                   self.edit(THREE_SEATS), ("wait", 5.0)], size=(162, 8))
        errors = [line for line in self.watch_log() if "does not parse; still using the last good one" in line]
        self.assertEqual(len(errors), 1)  # said once, though the board refreshed again meanwhile
        self.assertIn("parses again; reloaded", "\n".join(self.watch_log()))
        red = [i for i, draw in enumerate(self.screen.draws)
               if any(t.startswith("fleet file does not parse; still using the last good one: ")
                      and "red" in style for t, style in draw[-1])]
        self.assertTrue(red)
        during = self.screen.text(red[-1])
        self.assertIn("↔ docs-channel", "\n".join(during))  # the last good fleet: the contact stays
        last = self.screen.text()
        self.assertNotIn("still using", last[-1])
        self.assertNotIn("docs-channel", "\n".join(last[1:]))  # the repaired file has no contact

    def test_a_removed_seat_gets_no_new_waiter(self):
        put(self.repo, "sample-reviewer", "review this")
        put(self.repo, "sample-dev", "build this")
        without = THREE_SEATS.split('[[agents]]\nname = "sample-reviewer"')[0]
        self.page([("wait", 4.0), self.edit(without), ("wait", 5.0)], idle=40)
        before = [i for i, c in enumerate(self.calls()) if c["argv"][:3] == ["agent", "wait", "sample-reviewer"]]
        dev = [i for i, c in enumerate(self.calls()) if c["argv"][:3] == ["agent", "wait", "sample-dev"]]
        self.assertLessEqual(len(before), 4)   # the passes before the reload (waits time out at once here)
        self.assertGreaterEqual(len(dev), len(before) + 4)  # the seat still in the file is waited on every pass
        self.assertEqual(self.prompts_to("sample-reviewer"), [])

    def test_log_mode_never_reloads(self):
        clock = StopClock(cycles=3)
        broken = THREE_SEATS + "\n[[agents]\nname = \n"
        original = clock.sleep

        def sleep(seconds):
            if clock.passes == 1 and seconds == 2.0:
                self.edit(broken)()
            original(seconds)

        clock.sleep = sleep
        self.clock = clock
        code, out, err = self.run_cli("watch", "--log")
        self.assertEqual(code, 0, err)
        self.assertNotIn("fleet file", out)
        self.assertNotIn("reloaded", out)


if __name__ == "__main__":
    unittest.main()
