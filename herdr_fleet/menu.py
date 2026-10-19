"""The popup menu (manifest pane ``manage``; design section 8).

A plain numbered text menu on stdin, no curses: list seats with their state,
pick one seat or all, pick restart / swap / kickoff / down / up, confirm, run
the same operation the CLI runs, show the result, and wait for Enter.
End of input at any prompt quits without doing anything.
"""

from __future__ import annotations

from typing import List, Optional, TextIO

from herdr_fleet.checks import collect_status
from herdr_fleet.context import Ctx, Report
from herdr_fleet.ops import op_down, op_kickoff, op_restart, op_swap, op_up

ACTIONS = ("restart", "swap", "kickoff", "down", "up")


def _ask(ctx: Ctx, stdin: TextIO, prompt: str) -> Optional[str]:
    ctx.stdout.write(prompt)
    ctx.stdout.flush()
    line = stdin.readline()
    if not line:
        return None
    return line.strip()


def wait_for_enter(ctx: Ctx, stdin: TextIO) -> None:
    _ask(ctx, stdin, "\nPress Enter to close. ")


def _pick_seats(ctx: Ctx, stdin: TextIO) -> Optional[List[str]]:
    """Returns [] for all seats, [name] for one, None to quit."""
    data = collect_status(ctx, integrations=False)
    seats = data["seats"]
    ctx.say("Fleet %s" % data["fleet_file"])
    for number, seat in enumerate(seats, 1):
        drift = " (drift)" if seat["drift"] else ""
        ctx.say("  %d) %-24s %-9s %-8s %s%s" % (number, seat["name"], seat["live_kind"] or seat["kind"],
                                                 seat["state"], seat["pane_id"] or "-", drift))
    ctx.say("  a) all seats")
    while True:
        answer = _ask(ctx, stdin, "Seat (number, a, or q to quit): ")
        if answer is None or answer.lower() in ("q", ""):
            return None
        if answer.lower() == "a":
            return []
        if answer.isdigit() and 1 <= int(answer) <= len(seats):
            return [seats[int(answer) - 1]["name"]]
        ctx.say("Not a choice: %s" % answer)


def _pick_action(ctx: Ctx, stdin: TextIO) -> Optional[str]:
    ctx.say("  " + "  ".join("%d) %s" % (i, a) for i, a in enumerate(ACTIONS, 1)))
    while True:
        answer = _ask(ctx, stdin, "Action (number or q): ")
        if answer is None or answer.lower() in ("q", ""):
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(ACTIONS):
            return ACTIONS[int(answer) - 1]
        if answer in ACTIONS:
            return answer
        ctx.say("Not a choice: %s" % answer)


def run_menu(ctx: Ctx, stdin: TextIO) -> int:
    names = _pick_seats(ctx, stdin)
    if names is None:
        return 0
    action = _pick_action(ctx, stdin)
    if action is None:
        return 0
    if not names and action in ("restart", "kickoff"):
        names = [s.name for s in ctx.fleet.seats]
    note = None
    close_panes = False
    if action in ("restart", "swap", "kickoff", "up"):
        note = _ask(ctx, stdin, "Note for {note} in the kickoff (Enter for none): ") or None
    if action == "down":
        close_panes = (_ask(ctx, stdin, "Also close panes herdr-fleet created? [y/N] ") or "").lower() == "y"
    label = " ".join(names) if names else "(all seats)"
    confirm = _ask(ctx, stdin, "Run %s %s? [y/N] " % (action, label))
    if (confirm or "").lower() != "y":
        ctx.say("Nothing done.")
        wait_for_enter(ctx, stdin)
        return 0
    report = _run(ctx, action, names, note, close_panes)
    for line in report.lines():
        ctx.say(line)
    wait_for_enter(ctx, stdin)
    return report.exit_code()


def _run(ctx: Ctx, action: str, names: List[str], note: Optional[str], close_panes: bool) -> Report:
    if action == "restart":
        return op_restart(ctx, names, note)
    if action == "swap":
        return op_swap(ctx, names, note)
    if action == "kickoff":
        return op_kickoff(ctx, names, note)
    if action == "down":
        return op_down(ctx, names, close_panes=close_panes)
    return op_up(ctx, names, note=note)
