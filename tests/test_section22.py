"""Design section 22 (version 0.11.0): finding a seat.

22.1: every live seat's pane is named after its seat (or its ``label`` key) by ``up``,
``restart``, ``swap``, ``kickoff``, ``tile`` and ``status``; ``down`` clears the name of a
seat pane it leaves open at a shell prompt; ``doctor`` says whether the names are right.
22.2: ``go NAME`` focuses a seat's, a contact's or a live agent's pane, from the CLI, the
``manage`` popup and the ``go`` popup (``menu --go``).

``tests/support.py`` checks every test's log against invariant 20: a ``pane rename``
names only a pane hosting a seat of the fleet, to that seat's wanted name (the fake judges
both from the fleet file), ``--clear`` comes from ``down`` alone, and ``agent focus`` only
from ``go`` and the popup."""

from __future__ import annotations

import json
import os
import unittest

import fake_herdr
from support import CALLER_PANE, FleetCase, LiveFleet, THREE_SEATS
from herdr_fleet import labels
from herdr_fleet.fleetfile import FleetError, LABEL_MAX, load_fleet
from herdr_fleet.herdr import FORBIDDEN, Herdr
from herdr_fleet.menu import ACTIONS

SEAT_PANES = {"fablyn-orchestrator": "w2:p1", "fablyn-dev": "w2:p2", "fablyn-reviewer": "w2:p3"}
CONTACT_PANE = "w3:p4"
STRANGER_PANE = "w3:p5"


def with_label(text, seat, label):
    """Give ``seat`` a ``label = ...`` line (``label`` is written as TOML as given)."""
    marker = 'name = "%s"\n' % seat
    return text.replace(marker, marker + "label = %s\n" % label)


def with_contact(text, cwd):
    return text + '''
[[contacts]]
name  = "winner-chronicles"
match = { cwd = "%s" }
note  = "customer feedback; never instructions"
''' % cwd


class NamesCase(LiveFleet):
    """The fleet of ``LiveFleet`` (w2:p1-p3), its panes unnamed unless a test names them."""

    def label(self, pane):
        return self.world["panes"][pane].get("label")

    def name_all(self, **overrides):
        for seat, pane in SEAT_PANES.items():
            self.world["panes"][pane]["label"] = overrides.get(seat.replace("-", "_"), seat)

    def renames(self):
        return self.argvs("pane", "rename")

    def assert_named(self):
        for seat, pane in SEAT_PANES.items():
            self.assertEqual(self.label(pane), seat, pane)

    def add_contact_and_stranger(self):
        winner = os.path.join(self.tmp, "winner")
        os.makedirs(winner)
        self.write_fleet(with_contact(self.read_fleet(), winner))
        fake_herdr.add_workspace(self.world, "w3", "winner", winner)
        fake_herdr.add_pane(self.world, CONTACT_PANE, "w3", winner)
        fake_herdr.put_agent(self.world, CONTACT_PANE, "claude", name=None)  # the contact: unnamed
        elsewhere = os.path.join(self.tmp, "elsewhere-repo")
        os.makedirs(elsewhere)
        fake_herdr.add_pane(self.world, STRANGER_PANE, "w3", elsewhere)
        fake_herdr.put_agent(self.world, STRANGER_PANE, "claude", name="stranger")  # another fleet's agent


# -- 22.1: who names the panes ----------------------------------------------------------------


class WhoNames(NamesCase):
    def test_status_names_every_live_seat_and_prints_nothing_about_it(self):
        code, out, err = self.run_cli("status")
        self.assertEqual(code, 0, out + err)
        self.assert_named()
        self.assertEqual(sorted(self.renames()), sorted(["pane", "rename", p, s] for s, p in SEAT_PANES.items()))
        self.assertNotIn("panes:", out)
        self.assertNotIn("named", out)
        self.assertEqual(err, "")

    def test_status_json_payload_is_unchanged_and_nothing_else_is_printed(self):
        code, first, err = self.run_cli("status", "--json")  # names the panes
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.renames()), 3)
        code, second, _ = self.run_cli("status", "--json")  # already named: reads only
        self.assertEqual(len(self.renames()), 3)
        one, two = json.loads(first), json.loads(second)  # stdout is the JSON object alone
        self.assertEqual(one, two)
        self.assertNotIn("label", json.dumps(one["seats"]))
        self.assert_named()

    def test_already_named_panes_cost_one_read_and_no_write(self):
        self.name_all()
        code, out, _ = self.run_cli("status")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.renames(), [])
        gets = [argv[2] for argv in self.argvs("pane", "get")]
        for pane in SEAT_PANES.values():
            self.assertIn(pane, gets)

    def test_kickoff_names_and_says_so_once(self):
        code, out, err = self.run_cli("kickoff", "fablyn-dev")
        self.assertEqual(code, 0, out + err)
        self.assert_named()
        self.assertEqual(out.count("panes:"), 1)
        self.assertIn("panes: named 3 after their seats", out)
        self.assertTrue(out.rstrip().endswith("panes: named 3 after their seats"), out)

    def test_a_single_rename_names_the_seat(self):
        self.name_all()
        self.world["panes"]["w2:p3"]["label"] = None
        code, out, _ = self.run_cli("kickoff", "fablyn-dev")
        self.assertEqual(code, 0, out)
        self.assertIn("panes: named 1 (fablyn-reviewer)", out)
        self.assertEqual(self.renames(), [["pane", "rename", "w2:p3", "fablyn-reviewer"]])

    def test_nothing_changed_adds_no_line(self):
        self.name_all()
        for argv in (["kickoff", "fablyn-dev"], ["restart", "fablyn-dev"], ["swap"], ["tile"]):
            code, out, err = self.run_cli(*argv)
            self.assertEqual(code, 0, (argv, out, err))
            self.assertNotIn("panes:", out, argv)
        self.assertEqual(self.renames(), [])

    def test_restart_into_a_new_pane_names_it(self):
        self.name_all()
        del self.world["panes"]["w2:p2"]  # the dev's pane is gone; restart starts it in a fresh split
        code, out, err = self.run_cli("restart", "fablyn-dev")
        self.assertEqual(code, 0, out + err)
        pane = self.pane_of("fablyn-dev")
        self.assertNotEqual(pane, "w2:p2")
        self.assertEqual(self.label(pane), "fablyn-dev")
        self.assertIn("panes: named 1 (fablyn-dev)", out)
        self.assertEqual(self.renames(), [["pane", "rename", pane, "fablyn-dev"]])

    def test_restart_in_place_keeps_the_name(self):
        self.name_all()
        code, out, _ = self.run_cli("restart", "fablyn-dev")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.pane_of("fablyn-dev"), "w2:p2")
        self.assertEqual(self.label("w2:p2"), "fablyn-dev")
        self.assertEqual(self.renames(), [])

    def test_swap_names_once_at_its_end(self):
        self.write_fleet(self.read_fleet().replace(
            'name = "fablyn-dev"\nkind = "claude"\nargs = ["--model", "claude-opus-5-5"]',
            'name = "fablyn-dev"\nkind = "claude"\nargs = ["--model", "claude-opus-6"]'))
        code, out, err = self.run_cli("swap")
        self.assertEqual(code, 0, out + err)
        self.assertIn("fablyn-dev: drifted", out)
        self.assertEqual(out.count("panes:"), 1)
        self.assertTrue(out.rstrip().endswith("panes: named 3 after their seats"), out)
        self.assertEqual(len(self.renames()), 3)
        self.assert_named()

    def test_tile_names_the_seats(self):
        code, out, err = self.run_cli("tile")
        self.assertEqual(code, 0, out + err)
        self.assertIn("panes: named 3 after their seats", out)
        for seat in SEAT_PANES:
            self.assertEqual(self.label(self.pane_of(seat)), seat)

    def test_absent_seats_have_no_pane_to_name(self):
        self.world["panes"]["w2:p3"]["agent"] = None
        self.world["panes"]["w2:p3"]["procs"] = [dict(fake_herdr.SHELL)]
        code, out, _ = self.run_cli("status")
        self.assertEqual(code, 0, out)
        self.assertIsNone(self.label("w2:p3"))
        self.assertEqual(sorted(argv[2] for argv in self.renames()), ["w2:p1", "w2:p2"])


class UpNames(FleetCase):
    def test_up_names_the_seats_it_started_and_not_the_strip(self):
        code, out, err = self.run_cli("up")
        self.assertEqual(code, 0, out + err)
        self.assertIn("panes: named 3 after their seats", out)
        for seat in ("fablyn-orchestrator", "fablyn-dev", "fablyn-reviewer"):
            pane = self.pane_of(seat)
            self.assertEqual(self.world["panes"][pane]["label"], seat)
        strip = self.read_state()["watch"]["pane_id"]
        self.assertEqual(self.world["panes"][strip]["label"], "Fleet watch")  # its manifest title, untouched
        self.assertNotIn(strip, [argv[2] for argv in self.argvs("pane", "rename")])
        code, out, _ = self.run_cli("up")  # everything live and named: nothing more
        self.assertEqual(code, 0, out)
        self.assertNotIn("panes:", out)
        self.assertEqual(len(self.argvs("pane", "rename")), 3)

    def test_up_names_an_adopted_me_seat(self):
        code, out, err = self.run_cli("up", "--me", "fablyn-orchestrator")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.world["panes"][CALLER_PANE]["label"], "fablyn-orchestrator")
        self.assertIn(["pane", "rename", CALLER_PANE, "fablyn-orchestrator"], self.argvs("pane", "rename"))

    def test_up_dry_run_renames_nothing(self):
        code, out, _ = self.run_cli("up", "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.argvs("pane", "rename"), [])
        self.assertNotIn("panes:", out)


class DryRuns(NamesCase):
    def test_dry_runs_rename_nothing(self):
        self.write_fleet(self.read_fleet().replace('args = ["-m", "gpt-6-astra"]', 'args = ["-m", "gpt-7"]'))
        for argv in (["restart", "fablyn-dev", "--dry-run"], ["swap", "--dry-run"], ["tile", "--dry-run"]):
            code, out, err = self.run_cli(*argv)
            self.assertEqual(code, 0, (argv, out, err))
            self.assertNotIn("panes:", out, argv)
        self.assertEqual(self.renames(), [])
        self.assertEqual(self.argvs("pane", "rename"), [])


# -- the label key -----------------------------------------------------------------------------


class LabelKey(NamesCase):
    def test_label_names_the_pane_and_nothing_else(self):
        self.write_fleet(with_label(self.read_fleet(), "fablyn-orchestrator", '"ORCH"'))
        code, out, _ = self.run_cli("status")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.label("w2:p1"), "ORCH")
        self.assertIn("fablyn-orchestrator", out)  # listings keep the seat name
        self.assertNotIn("ORCH", out)
        self.assertIn(["pane", "rename", "w2:p1", "ORCH"], self.renames())
        code, out, _ = self.run_cli("kickoff", "fablyn-dev")
        self.assertIn("[from herdr-fleet] You are fablyn-dev", self.prompts_to("fablyn-dev")[0])
        self.assertNotIn("ORCH", self.prompts_to("fablyn-dev")[0])

    def test_a_pane_named_after_the_seat_is_renamed_to_its_label(self):
        self.name_all()
        self.write_fleet(with_label(self.read_fleet(), "fablyn-dev", '"  Dev two  "'))
        code, out, _ = self.run_cli("kickoff", "fablyn-dev")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.label("w2:p2"), "Dev two")  # kept without surrounding blanks
        self.assertIn("panes: named 1 (fablyn-dev)", out)

    def test_wanted(self):
        fleet = load_fleet(self.write_fleet(with_label(THREE_SEATS, "fablyn-dev", '"%s"' % ("x" * LABEL_MAX))))
        self.assertEqual(labels.wanted(fleet.seat("fablyn-dev")), "x" * 40)
        self.assertEqual(labels.wanted(fleet.seat("fablyn-reviewer")), "fablyn-reviewer")
        self.assertIsNone(fleet.seat("fablyn-reviewer").label)
        self.assertEqual(fleet.warnings, [])  # `label` is a known seat key

    def errors(self, value):
        path = self.write_fleet(with_label(THREE_SEATS, "fablyn-dev", value))
        with self.assertRaises(FleetError) as caught:
            load_fleet(path)
        return "\n".join(caught.exception.errors)

    def test_invalid_labels(self):
        where = "agents[1] (fablyn-dev).label: "
        self.assertIn(where + "must not be empty", self.errors('""'))
        self.assertIn(where + "must not be empty", self.errors('"   "'))
        self.assertIn(where + "41 characters; at most 40", self.errors('"%s"' % ("y" * 41)))
        self.assertIn(where + "must be a string", self.errors("42"))
        self.assertIn(where + "must be a string", self.errors('["ORCH"]'))
        self.assertIn(where + "must be a single line", self.errors('"two\\nlines"'))
        self.assertIn(where + "'-dev-' must not start with '-'", self.errors('"-dev-"'))

    def test_errors_are_reported_together(self):
        text = with_label(with_label(THREE_SEATS, "fablyn-dev", '""'), "fablyn-reviewer", "7")
        with self.assertRaises(FleetError) as caught:
            load_fleet(self.write_fleet(text))
        self.assertEqual(len(caught.exception.errors), 2)
        code, _, err = self.run_cli("status")
        self.assertEqual(code, 2)
        self.assertIn("label: must not be empty", err)
        self.assertEqual(self.renames(), [])


# -- only seat panes --------------------------------------------------------------------------


class OnlySeatPanes(NamesCase):
    def test_contacts_and_other_fleets_agents_are_never_renamed(self):
        self.add_contact_and_stranger()
        for argv in (["status"], ["kickoff", "fablyn-dev"], ["tile"], ["up"]):
            code, out, err = self.run_cli(*argv)
            self.assertIn(code, (0, 3), (argv, out, err))
        renamed = {argv[2] for argv in self.renames()}
        self.assertEqual(renamed, set(SEAT_PANES.values()))
        self.assertIsNone(self.label(CONTACT_PANE))
        self.assertIsNone(self.label(STRANGER_PANE))

    def test_the_watcher_strip_is_never_renamed(self):
        strip = "w2:p9"
        fake_herdr.add_pane(self.world, strip, "w2", self.repo, created=True)
        argv = ["python3", "/plugins/yarrasys.fleet/bin/herdr-fleet", "watch"]
        self.world["panes"][strip].update(plugin="watch", label="Fleet watch", procs=[
            {"name": "python3", "argv0": "python3", "argv": argv, "cmdline": " ".join(argv), "pid": 700}])
        state = self.read_state()
        state["watch"] = {"pane_id": strip, "created_pane": True, "started": "2026-10-22T10:00:00+11:00"}
        self.write_state(state)
        code, out, _ = self.run_cli("status")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.label(strip), "Fleet watch")
        self.assertNotIn(strip, [argv[2] for argv in self.renames()])

    def test_seat_panes_skips_the_strip_even_if_a_record_says_otherwise(self):
        state = self.read_state()
        state["watch"] = {"pane_id": "w2:p3", "created_pane": True, "started": "2026-10-22T10:00:00+11:00"}
        self.write_state(state)
        code, out, _ = self.run_cli("status")
        self.assertEqual(code, 0, out)
        self.assertNotIn("w2:p3", [argv[2] for argv in self.renames()])


# -- failures ---------------------------------------------------------------------------------


class FailedRename(NamesCase):
    def test_a_failed_rename_is_a_warning_and_the_command_goes_on(self):
        self.world["behaviors"]["rename_errors"] = {"w2:p2": "pane_rename_failed"}
        code, out, err = self.run_cli("kickoff", "fablyn-dev")
        self.assertEqual(code, 0, out + err)  # the kickoff itself succeeded
        self.assertEqual(len(self.prompts_to("fablyn-dev")), 1)
        self.assertIn("panes: could not name w2:p2: herdr error pane_rename_failed:", out)
        self.assertIn("panes: named 2 after their seats", out)
        self.assertEqual(self.label("w2:p1"), "fablyn-orchestrator")
        self.assertIsNone(self.label("w2:p2"))

    def test_status_sends_a_failure_to_stderr_only(self):
        self.world["behaviors"]["rename_errors"] = {"w2:p3": "pane_rename_failed"}
        code, out, err = self.run_cli("status", "--json")
        self.assertEqual(code, 0, err)
        json.loads(out)
        self.assertIn("herdr-fleet: panes: could not name w2:p3: herdr error pane_rename_failed", err)

    def test_a_failed_pane_get_is_a_warning_too(self):
        state = self.read_state()
        del state["agents"]["fablyn-orchestrator"]  # so only the naming reads w2:p1
        self.write_state(state)
        self.world["canned"].append({"prefix": ["pane", "get", "w2:p1"], "exit": 1,
                                     "stderr": {"error": {"code": "server_busy", "message": "try again"}}})
        code, out, _ = self.run_cli("kickoff", "fablyn-dev")
        self.assertEqual(code, 0, out)
        self.assertIn("panes: could not name w2:p1: herdr error server_busy: try again", out)


# -- down --------------------------------------------------------------------------------------


class DownClears(NamesCase):
    def setUp(self):
        super().setUp()
        self.name_all()

    def clears(self):
        return [argv for argv in self.renames() if "--clear" in argv]

    def test_a_pane_left_at_a_shell_loses_its_name(self):
        code, out, err = self.run_cli("down", "fablyn-dev")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.clears(), [["pane", "rename", "w2:p2", "--clear"]])
        self.assertIsNone(self.label("w2:p2"))
        self.assertEqual(self.label("w2:p1"), "fablyn-orchestrator")  # not taken down: kept

    def test_a_closed_pane_needs_nothing_and_a_kept_one_is_cleared(self):
        code, out, err = self.run_cli("down", "--close-panes")
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("w2:p2", self.world["panes"])  # created: closed
        self.assertNotIn("w2:p3", self.world["panes"])
        self.assertEqual(self.clears(), [["pane", "rename", "w2:p1", "--clear"]])  # not created: kept, cleared
        self.assertIsNone(self.label("w2:p1"))

    def test_a_pane_without_a_name_is_left_alone(self):
        self.world["panes"]["w2:p2"]["label"] = None
        code, out, _ = self.run_cli("down", "fablyn-dev")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.clears(), [])

    def test_a_refused_seat_keeps_its_name(self):
        self.status_of("w2:p2", "blocked")
        code, out, _ = self.run_cli("down", "fablyn-dev")
        self.assertEqual(code, 3, out)
        self.assertEqual(self.clears(), [])
        self.assertEqual(self.label("w2:p2"), "fablyn-dev")

    def test_a_failed_clear_is_a_warning(self):
        self.world["behaviors"]["rename_errors"] = {"w2:p2": "pane_rename_failed"}
        code, out, _ = self.run_cli("down", "fablyn-dev")
        self.assertEqual(code, 0, out)
        self.assertIn("panes: could not clear the name of w2:p2: herdr error pane_rename_failed", out)

    def test_down_never_names(self):
        self.world["panes"]["w2:p1"]["label"] = None
        code, out, _ = self.run_cli("down", "fablyn-dev")
        self.assertEqual(code, 0, out)
        self.assertEqual([argv for argv in self.renames() if "--clear" not in argv], [])


# -- doctor ------------------------------------------------------------------------------------


class DoctorLine(NamesCase):
    def doctor_line(self):
        code, out, _ = self.run_cli("doctor")
        lines = [line for line in out.splitlines() if "pane names:" in line]
        self.assertEqual(len(lines), 1, out)
        return code, lines[0]

    def test_unnamed_seats_are_listed_with_the_fix(self):
        self.name_all()
        self.world["panes"]["w2:p2"]["label"] = None
        self.world["panes"]["w2:p1"]["label"] = "claude"
        code, line = self.doctor_line()
        self.assertEqual(code, 0)
        self.assertEqual(line, "info  pane names: 2 seats unnamed (fablyn-dev, fablyn-orchestrator); "
                               "run herdr-fleet status")
        self.world["panes"]["w2:p1"]["label"] = "fablyn-orchestrator"
        self.assertEqual(self.doctor_line()[1], "info  pane names: 1 seat unnamed (fablyn-dev); run herdr-fleet status")

    def test_every_live_seat_named(self):
        self.assertEqual(self.run_cli("status")[0], 0)
        self.assertEqual(self.doctor_line()[1], "ok    pane names: every live seat's pane is named")

    def test_doctor_renames_nothing(self):
        self.doctor_line()
        self.assertEqual(self.renames(), [])
        self.assertEqual(self.argvs("agent", "focus"), [])

    def test_a_label_is_what_doctor_expects(self):
        self.name_all()
        self.write_fleet(with_label(self.read_fleet(), "fablyn-reviewer", '"REV"'))
        self.assertEqual(self.doctor_line()[1],
                         "info  pane names: 1 seat unnamed (fablyn-reviewer); run herdr-fleet status")


# -- 22.2: go ------------------------------------------------------------------------------------


class Go(NamesCase):
    def focused(self):
        return [pid for pid, pane in self.world["panes"].items() if pane.get("focused")
                and self.world["workspaces"][pane["workspace_id"]].get("focused")]

    def snapshot(self):
        found = {}
        for folder, _, files in os.walk(os.path.join(self.repo, ".herdr")):
            for name in files:
                with open(os.path.join(folder, name), "rb") as handle:
                    found[os.path.join(folder, name)] = handle.read()
        return found

    def test_a_seat(self):
        self.status_of("w2:p1", "working")
        code, out, err = self.run_cli("go", "fablyn-orchestrator")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(out, "focused fablyn-orchestrator (w2:p1, working)\n")
        self.assertEqual(self.argvs("agent", "focus"), [["agent", "focus", "fablyn-orchestrator"]])
        self.assertEqual(self.focused(), ["w2:p1"])
        self.assertEqual(self.world["workspaces"]["w2"]["active_tab_id"], "w2:t1")

    def test_a_done_seat_is_reported_done_and_herdr_marks_it_seen(self):
        self.status_of("w2:p2", "done")
        code, out, _ = self.run_cli("go", "fablyn-dev")
        self.assertEqual(code, 0, out)
        self.assertEqual(out, "focused fablyn-dev (w2:p2, done)\n")
        self.assertEqual(self.world["panes"]["w2:p2"]["agent"]["agent_status"], "idle")

    def test_a_contact_by_its_pane(self):
        self.add_contact_and_stranger()
        code, out, err = self.run_cli("go", "winner-chronicles")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(out, "focused winner-chronicles (%s, idle)\n" % CONTACT_PANE)
        self.assertEqual(self.argvs("agent", "focus"), [["agent", "focus", CONTACT_PANE]])
        self.assertEqual(self.focused(), [CONTACT_PANE])

    def test_a_live_agent_by_name(self):
        self.add_contact_and_stranger()
        code, out, _ = self.run_cli("go", "stranger")
        self.assertEqual(code, 0, out)
        self.assertEqual(out, "focused stranger (%s, idle)\n" % STRANGER_PANE)
        self.assertEqual(self.argvs("agent", "focus"), [["agent", "focus", "stranger"]])

    def test_an_absent_seat(self):
        self.world["panes"]["w2:p2"]["agent"] = None
        code, out, _ = self.run_cli("go", "fablyn-dev")
        self.assertEqual(code, 3)
        self.assertEqual(out, "fablyn-dev is absent; nothing to focus\n")
        self.assertEqual(self.argvs("agent", "focus"), [])

    def test_an_absent_contact(self):
        self.add_contact_and_stranger()
        self.world["panes"][CONTACT_PANE]["agent"] = None
        code, out, _ = self.run_cli("go", "winner-chronicles")
        self.assertEqual(code, 3)
        self.assertRegex(out, r"^winner-chronicles is absent \(no live agent matches cwd .*\); nothing to focus\n$")
        self.assertEqual(self.argvs("agent", "focus"), [])

    def test_an_unknown_name_and_owner_are_usage_errors(self):
        code, out, err = self.run_cli("go", "nobody")
        self.assertEqual(code, 2)
        self.assertIn("not a seat, contact or live agent: nobody", err)
        code, out, err = self.run_cli("go", "owner")
        self.assertEqual(code, 2)
        self.assertIn("owner is not a pane; nothing to focus", err)
        self.assertEqual(self.argvs("agent", "focus"), [])

    def test_go_writes_nothing(self):
        before = self.snapshot()
        self.status_of("w2:p2", "blocked")
        self.assertEqual(self.run_cli("go", "fablyn-dev")[0], 0)
        self.assertEqual(self.snapshot(), before)  # no state_seen, no log, no new file
        self.assertNotIn("state_seen", json.dumps(self.read_state()))
        self.assertEqual(self.renames(), [])
        self.assertEqual(self.argvs("agent", "prompt"), [])

    def test_go_takes_fleet_and_needs_herdr(self):
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.makedirs(elsewhere)
        code, out, _ = self.run_cli("go", "fablyn-dev", "--fleet", os.path.join(self.repo, ".herdr", "fleet.toml"),
                                    cwd=elsewhere)
        self.assertEqual(code, 0, out)
        env = dict(self.env)
        del env["HERDR_ENV"], env["HERDR_BIN_PATH"]
        code, _, err = self.run_cli("go", "fablyn-dev", env=env)
        self.assertEqual(code, 1)
        self.assertIn("not running inside Herdr", err)

    def test_a_failed_focus_is_an_error(self):
        self.world["canned"].append({"prefix": ["agent", "focus"], "exit": 1,
                                     "stderr": {"error": {"code": "focus_failed", "message": "no ui"}}})
        code, _, err = self.run_cli("go", "fablyn-dev")
        self.assertEqual(code, 1)
        self.assertIn("herdr error focus_failed: no ui", err)


# -- 22.2: the popups ------------------------------------------------------------------------------


class PopupGo(NamesCase):
    GO = str(ACTIONS.index("go") + 1)

    def test_go_is_the_last_action(self):
        self.assertEqual(ACTIONS, ("restart", "swap", "kickoff", "down", "up", "tile", "go"))

    def test_one_seat_no_confirmation_no_enter(self):
        code, out, err = self.run_cli("menu", stdin="2\n%s\n" % self.GO)
        self.assertEqual(code, 0, out + err)
        self.assertTrue(out.endswith("focused fablyn-dev (w2:p2, idle)\n"), out)
        self.assertNotIn("Run go", out)
        self.assertNotIn("Press Enter to close.", out)
        self.assertEqual(self.argvs("agent", "focus"), [["agent", "focus", "fablyn-dev"]])

    def test_all_seats_is_refused_and_the_action_asked_again(self):
        code, out, _ = self.run_cli("menu", stdin="a\n%s\nq\n" % self.GO)
        self.assertEqual(code, 0, out)
        self.assertIn("go needs one seat", out)
        self.assertEqual(out.count("Action (number or q): "), 2)
        self.assertEqual(self.argvs("agent", "focus"), [])
        code, out, _ = self.run_cli("menu", stdin="a\ngo\n3\n\ny\n\n")  # then another action still works
        self.assertEqual(code, 0, out)
        self.assertIn("go needs one seat", out)
        for name in SEAT_PANES:
            self.assertEqual(len(self.prompts_to(name)), 1)
        self.assertEqual(self.argvs("agent", "focus"), [])

    def test_an_absent_seat_prints_its_line_and_waits_for_enter(self):
        self.world["panes"]["w2:p2"]["agent"] = None
        code, out, _ = self.run_cli("menu", stdin="2\n%s\n\n" % self.GO)
        self.assertEqual(code, 3)
        self.assertIn("fablyn-dev is absent; nothing to focus", out)
        self.assertTrue(out.endswith("Press Enter to close. "), out)
        self.assertEqual(self.argvs("agent", "focus"), [])

    def test_quit(self):
        for stdin in ("2\nq\n", "q\n", ""):
            self.assertEqual(self.run_cli("menu", stdin=stdin)[0], 0)
        self.assertEqual(self.argvs("agent", "focus"), [])


class MenuGo(NamesCase):
    def setUp(self):
        super().setUp()
        self.add_contact_and_stranger()

    def test_lists_seats_then_contacts(self):
        code, out, _ = self.run_cli("menu", "--go", stdin="q\n")
        self.assertEqual(code, 0, out)
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("Fleet "), out)
        self.assertRegex(lines[1], r"^  1\) fablyn-orchestrator\s+claude\s+idle\s+w2:p1$")
        self.assertRegex(lines[3], r"^  3\) fablyn-reviewer\s+codex\s+idle\s+w2:p3$")
        self.assertRegex(lines[4], r"^  c1\) winner-chronicles\s+contact\s+idle\s+w3:p4$")
        self.assertNotIn("a) all seats", out)
        self.assertNotIn("stranger", out)
        self.assertIn("Go to (number, c1-c1, or q to quit): ", out)
        self.assertEqual(self.argvs("agent", "focus"), [])

    def test_a_seat(self):
        code, out, _ = self.run_cli("menu", "--go", stdin="3\n")
        self.assertEqual(code, 0, out)
        self.assertTrue(out.endswith("focused fablyn-reviewer (w2:p3, idle)\n"), out)
        self.assertEqual(self.argvs("agent", "focus"), [["agent", "focus", "fablyn-reviewer"]])

    def test_a_contact(self):
        code, out, _ = self.run_cli("menu", "--go", stdin="c1\n")
        self.assertEqual(code, 0, out)
        self.assertTrue(out.endswith("focused winner-chronicles (w3:p4, idle)\n"), out)
        self.assertEqual(self.argvs("agent", "focus"), [["agent", "focus", CONTACT_PANE]])

    def test_q_enter_and_end_of_input_close(self):
        for stdin in ("q\n", "\n", ""):
            self.assertEqual(self.run_cli("menu", "--go", stdin=stdin)[0], 0)
        self.assertEqual(self.argvs("agent", "focus"), [])

    def test_an_absent_seat_says_so_and_asks_again(self):
        self.world["panes"]["w2:p2"]["agent"] = None
        code, out, _ = self.run_cli("menu", "--go", stdin="2\nc9\nx\n1\n")
        self.assertEqual(code, 0, out)
        self.assertIn("fablyn-dev is absent; nothing to focus", out)
        self.assertIn("Not a choice: c9", out)
        self.assertIn("Not a choice: x", out)
        self.assertTrue(out.endswith("focused fablyn-orchestrator (w2:p1, idle)\n"), out)
        self.assertEqual(self.argvs("agent", "focus"), [["agent", "focus", "fablyn-orchestrator"]])

    def test_without_contacts(self):
        self.write_fleet(THREE_SEATS)
        code, out, _ = self.run_cli("menu", "--go", stdin="q\n")
        self.assertEqual(code, 0, out)
        self.assertIn("Go to (number, or q to quit): ", out)
        self.assertNotIn("c1)", out)


class UiGo(NamesCase):
    def test_ui_go_opens_the_popup_at_the_repo_root(self):
        code, _, err = self.run_cli("ui", "go")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.argvs("plugin", "pane", "open"),
                         [["plugin", "pane", "open", "--plugin", "yarrasys.fleet", "--entrypoint", "go",
                           "--cwd", self.repo]])
        self.assertEqual(self.argvs("agent", "focus"), [])


# -- the client and the harness ---------------------------------------------------------------------


class Client(unittest.TestCase):
    def test_argv_and_not_forbidden(self):
        seen = []

        def runner(argv):
            seen.append(argv[1:])
            return 0, json.dumps({"result": {"pane": {"pane_id": "w1:p2", "label": "x"}}}), ""

        herdr = Herdr("herdr", runner)
        self.assertEqual(herdr.pane_rename("w1:p2", "fablyn-dev")["pane_id"], "w1:p2")
        herdr.pane_clear_name("w1:p2")
        herdr.agent_focus("fablyn-dev")
        self.assertEqual(seen, [["pane", "rename", "w1:p2", "fablyn-dev"], ["pane", "rename", "w1:p2", "--clear"],
                                ["agent", "focus", "fablyn-dev"]])
        self.assertNotIn(("pane", "rename"), FORBIDDEN)
        self.assertNotIn(("agent", "focus"), FORBIDDEN)

    def test_summary(self):
        self.assertEqual(labels.summary(["fablyn-designer"]), "named 1 (fablyn-designer)")
        self.assertEqual(labels.summary(["a", "b", "c", "d"]), "named 4 after their seats")


class Harness(NamesCase):
    """The checks ``tests/support.py`` adds for invariant 20 catch what they must."""

    def check(self, command, argv, info):
        self.assert_rename(command, argv, info)

    def test_rename_rules(self):
        good = {"seat": "fablyn-dev", "wanted": "fablyn-dev", "label": "fablyn-dev", "fleet_known": True}
        self.check(["status"], ["pane", "rename", "w2:p2", "fablyn-dev"], good)
        for command, info in ((["send"], good),                                  # not a naming command
                              (["status"], dict(good, seat=None)),               # no seat in that pane
                              (["status"], dict(good, label="dev")),             # not the wanted name
                              (["status"], dict(good, plugin=True)),             # a plugin pane
                              (["restart"], {"clear": True}),                    # --clear outside down
                              (["down"], {"clear": True, "has_agent": True})):   # --clear on a live agent
            with self.subTest(command=command, info=info):
                with self.assertRaises(AssertionError):
                    self.check(command, ["pane", "rename", "w2:p2", "x"], info)
        self.check(["down"], ["pane", "rename", "w2:p2", "--clear"], {"clear": True})

    def test_focus_only_from_go_and_the_popup(self):
        log = os.path.join(self.tmp, "calls.jsonl")
        with open(log, "a") as handle:
            handle.write(json.dumps({"command": ["status"]}) + "\n")
            handle.write(json.dumps({"argv": ["agent", "focus", "fablyn-dev"], "exit": 0, "info": {}}) + "\n")
        with self.assertRaises(AssertionError):
            self.assert_safety()
        os.remove(log)

    def test_the_fake_judges_seats_from_the_fleet_file(self):
        self.write_fleet(with_label(self.read_fleet(), "fablyn-dev", '"DEV"'))
        self.assertEqual(fake_herdr.fleet_names(os.path.join(self.repo, ".herdr", "fleet.toml")),
                         {"fablyn-orchestrator": "fablyn-orchestrator", "fablyn-dev": "DEV",
                          "fablyn-reviewer": "fablyn-reviewer"})
        self.assertEqual(fake_herdr.fleet_names(os.path.join(self.tmp, "missing.toml")), {})
        self.run_cli("status")
        call = next(c for c in self.calls() if c["argv"][:4] == ["pane", "rename", "w2:p2", "DEV"])
        self.assertEqual((call["info"]["seat"], call["info"]["wanted"]), ("fablyn-dev", "DEV"))


if __name__ == "__main__":
    unittest.main()
