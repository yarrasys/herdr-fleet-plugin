"""Section 27: usable starter examples and capacity for seats plus contacts."""

from __future__ import annotations

import os
import unittest

import fake_herdr
from herdr_fleet import board as B
from herdr_fleet.fleetfile import load_fleet
from support import THREE_SEATS
from test_init import InitCase
from test_section25 import PageCase


def fleet_board():
    seats = [B.Row("seat-%d" % i, "seat-%d" % i, state, i)
             for i, state in enumerate(("idle", "working", "blocked"))]
    contacts = [B.Row("peer-%d" % i, "peer-%d" % i, "idle", i + 3, contact=True)
                for i in range(5)]
    return B.Board(fleet="scratch", rows=seats, contacts=contacts)


class StarterExamples(InitCase):
    def test_generated_layout_and_disabled_watcher_load_without_warnings(self):
        project = self.mkdir("project")
        code, out, err = self.run_cli("init", cwd=project)
        self.assertEqual(code, 0, out + err)
        path = os.path.join(project, ".herdr", "fleet.toml")
        text = self.read(path).decode("utf-8")
        for line in ('# [layout]', '# mode = "chain"', '# direction = "right"',
                     '# ratio = 0.5', '# watch = false'):
            text = text.replace(line, line[2:])
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        fleet = load_fleet(path)
        self.assertEqual(fleet.warnings, [])
        self.assertEqual(fleet.mode, "chain")
        self.assertFalse(fleet.watch)
        self.assertEqual(self.calls(), [])


class CombinedCapacity(unittest.TestCase):
    def test_normal_rows_show_three_seats_and_five_contacts_in_file_order(self):
        model = fleet_board()
        mode, rows, more = B.plan(model, 162, 10)
        self.assertEqual((mode, more), ("rows", 0))
        self.assertEqual(rows, model.rows + model.contacts)
        lines = B.render(model, 162, 10, True)
        self.assertEqual(len(lines), 10)
        for index, contact in enumerate(model.contacts, 4):
            self.assertTrue(B.plain_text([lines[index]])[0].startswith("↔ " + contact.name))
            self.assertEqual({style for _, style in lines[index]}, {"contact"})

    def test_contacts_trigger_packing_even_when_the_seats_alone_fit(self):
        model = fleet_board()
        mode, rows, more = B.plan(model, 162, 6)
        self.assertEqual((mode, more), ("packed", 0))
        self.assertEqual(rows, model.rows + model.contacts)
        lines = B.render(model, 162, 6, True)
        shown = B.plain_text(lines)
        self.assertTrue(shown[2][81:].startswith("↔ peer-0"))
        self.assertTrue(shown[4][81:].startswith("↔ peer-4"))
        self.assertNotIn("more", shown[-1])
        for line in lines[3:5]:
            self.assertEqual({style for _, style in line}, {"contact"})

    def test_packed_overflow_prefers_urgent_seats_then_contacts_and_counts_all_hidden(self):
        model = fleet_board()
        mode, rows, more = B.plan(model, 162, 5)
        self.assertEqual((mode, more), ("packed", 2))
        self.assertEqual([row.name for row in rows],
                         ["seat-2", "seat-1", "seat-0", "peer-0", "peer-1", "peer-2"])
        self.assertIn("+2 more", B.plain_text(B.render(model, 162, 5, True))[-1])

    def test_narrow_overflow_keeps_contacts_in_remaining_room_and_counts_hidden(self):
        model = fleet_board()
        mode, rows, more = B.plan(model, 80, 6)
        self.assertEqual((mode, more), ("rows", 4))
        self.assertEqual([row.name for row in rows], ["seat-2", "seat-1", "seat-0", "peer-0"])
        self.assertIn("+4 more", B.plain_text(B.render(model, 80, 6, False))[-1])

    def test_when_seats_overflow_contacts_cannot_displace_them(self):
        model = fleet_board()
        model.contacts[0].state = "blocked"
        mode, rows, more = B.plan(model, 80, 4)
        self.assertEqual((mode, more), ("rows", 6))
        self.assertEqual([row.name for row in rows], ["seat-2", "seat-1"])
        self.assertIn("+6 more", B.plain_text(B.render(model, 80, 4, True))[-1])

    def test_very_short_board_keeps_its_most_urgent_seat(self):
        model = fleet_board()
        model.contacts[0].state = "blocked"
        self.assertEqual(B.visible(model, 162, 2), [model.rows[2]])
        self.assertEqual(len(B.render(model, 162, 2, True)), 1)

    def test_packed_click_and_number_map_to_the_contact_drawn_in_that_half(self):
        model = fleet_board()
        page = B.BoardPage()
        page.model = model
        page.render(163, 6, True)
        self.assertEqual(B.hit_test(model, 163, 6, 2, 80), ("row", "seat-2"))
        self.assertEqual(B.hit_test(model, 163, 6, 2, 81), ("row", "peer-0"))
        self.assertEqual(page.click(2, 81, 163, 6), ("open", "contact", "peer-0"))
        self.assertEqual(page.key("enter"), ("open", "contact", "peer-0"))
        self.assertEqual(page.key("8"), ("open", "contact", "peer-4"))
        self.assertEqual(B.hit_test(model, 163, 6, 5, 81), ("footer",))


class ContactPages(PageCase):
    def setUp(self):
        super().setUp()
        text = THREE_SEATS
        for i in range(5):
            name = "peer-%d" % i
            text += '\n[[contacts]]\nname = "%s"\nmatch = { name = "%s" }\n' % (name, name)
            fake_herdr.add_pane(self.world, "w3:p%d" % (i + 1), "w3", self.repo)
            fake_herdr.put_agent(self.world, "w3:p%d" % (i + 1), "claude", name=name, status="idle")
        self.write_fleet(text)

    def test_packed_click_opens_the_contact_page_without_agent_control(self):
        code, _, err = self.page([("click", 2, 81)], size=(162, 6))
        self.assertEqual(code, 0, err)
        self.assertTrue(self.draw_titled("↔ peer-0 · contact of this fleet")[0].endswith("w3:p1"))
        self.assertFalse(self.argvs("agent", "focus"))
        self.assertFalse(self.argvs("agent", "prompt"))
        self.assertFalse(self.argvs("agent", "send-keys"))

    def test_hidden_contacts_remain_reachable_through_page_navigation(self):
        code, _, err = self.page(["1", "left"], size=(80, 5))
        self.assertEqual(code, 0, err)
        self.assertTrue(self.draw_titled("↔ peer-4 · contact of this fleet")[0].endswith("w3:p5"))
        self.assertIn("+5 more", self.screen.text(0)[-1])
