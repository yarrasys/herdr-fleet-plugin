"""State note sections (design 16.5): state show / set / keys."""

from __future__ import annotations

import datetime
import os
import unittest

from support import FleetCase, THREE_SEATS
from herdr_fleet.statenote import keys, sections, set_section, starter_text

NOW = datetime.datetime(2026, 10, 20, 0, 31, tzinfo=datetime.timezone.utc)
STAMP = r"\(updated \d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC\)"


class Sections(unittest.TestCase):
    def test_replace_append_and_preserve(self):
        text = "# Fablyn state\nIntro.\n\n## production\nv12 live\n### detail\nkept inside\n\n# Archive\nold\n"
        out = set_section(text, "production", "v13 live", NOW)
        self.assertEqual(out, "# Fablyn state\nIntro.\n\n## production (updated 2026-10-20 00:31 UTC)\nv13 live\n\n"
                              "# Archive\nold\n")
        out = set_section(out, "next", "ship the export dialog", NOW)
        self.assertTrue(out.endswith("old\n\n## next (updated 2026-10-20 00:31 UTC)\nship the export dialog\n\n"))
        self.assertEqual(keys(out), ["production", "next"])
        again = set_section(out, "production", "v14", NOW)
        self.assertEqual(keys(again), ["production", "next"])
        self.assertIn("## production (updated 2026-10-20 00:31 UTC)\nv14\n", again)
        self.assertNotIn("v13", again)

    def test_starter(self):
        self.assertEqual(starter_text(), "## production\n\n## in flight\n\n## next\n\n## backlog\n\n")
        self.assertEqual(keys(starter_text("Intro.")), ["production", "in flight", "next", "backlog"])
        self.assertEqual(sections("no sections\n"), [])


class Commands(FleetCase):
    fleet_text = 'state_note = ".herdr/STATE.md"\n' + THREE_SEATS

    def note(self):
        with open(os.path.join(self.repo, ".herdr", "STATE.md")) as handle:
            return handle.read()

    def test_set_keys_show(self):
        self.write_file(".herdr/STATE.md", "Where things stand.\n\n" + starter_text())
        code, out, _ = self.run_cli("state", "set", "in flight", "dev: export dialog\nreviewer: round 3")
        self.assertEqual(code, 0, out)
        text = self.note()
        self.assertTrue(text.startswith("Where things stand.\n\n## production\n\n## in flight "))
        self.assertRegex(text, r"## in flight %s\ndev: export dialog\nreviewer: round 3\n\n## next\n" % STAMP)
        brief = self.write_file("risks.md", "none known\n")
        self.assertEqual(self.run_cli("state", "set", "risks", "--file", brief)[0], 0)
        self.assertRegex(self.note(), r"## backlog\n\n## risks %s\nnone known\n\n$" % STAMP)
        self.assertEqual(self.run_cli("state", "keys")[1].splitlines(),
                         ["production", "in flight", "next", "backlog", "risks"])
        self.assertIn("reviewer: round 3", self.run_cli("state", "show")[1])
        self.assertEqual([f for f in os.listdir(os.path.join(self.repo, ".herdr")) if f.endswith(".tmp")], [])
        self.assertEqual(self.calls(), [])  # no herdr command

    def test_usage_errors(self):
        self.assertEqual(self.run_cli("state", "set", "next")[0], 2)
        self.assertEqual(self.run_cli("state", "set", "next", "x", "--file", "y")[0], 2)
        self.write_fleet(THREE_SEATS)
        code, _, err = self.run_cli("state", "show")
        self.assertEqual(code, 2)
        self.assertIn("sets no state_note", err)

    def test_show_missing_note(self):
        code, out, _ = self.run_cli("state", "show")
        self.assertEqual(code, 0)
        self.assertIn("is empty", out)


if __name__ == "__main__":
    unittest.main()
