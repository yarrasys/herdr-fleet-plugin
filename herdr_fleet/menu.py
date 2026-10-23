"""The popup menu (manifest pane ``manage``; design section 8) and the ``go`` popup
(``menu --go``; design 22.2).

A plain numbered text menu on stdin, no curses: list seats with their state,
pick one seat or all, pick restart / swap / kickoff / down / up / tile (tile
always covers every live seat) / go, confirm, run
the same operation the CLI runs, show the result, and wait for Enter.
End of input at any prompt quits without doing anything.

``go`` takes exactly one seat, asks no confirmation and, once ``agent focus``
returned, exits at once, so the focus lands on the seat and not on the popup. An
absent seat prints its line and waits for Enter. ``menu --go`` lists the seats as
above, then the contacts as ``c1``, ``c2``, reads one answer, focuses it and exits;
``q`` or Enter closes with nothing done, and an absent seat says so and asks again.

A popup never presses a key in an agent's pane (design 23.7, invariant 21): its
restart, swap and down report an exit confirmation for the owner to answer.
"""

from __future__ import annotations

from typing import Dict, List, Optional, TextIO

from herdr_fleet.checks import collect_status
from herdr_fleet.context import EXIT_OK, Ctx, Report
from herdr_fleet.ops import op_down, op_go, op_kickoff, op_restart, op_swap, op_up
from herdr_fleet.tile import op_tile

ACTIONS = ("restart", "swap", "kickoff", "down", "up", "tile", "go")


def _ask(ctx: Ctx, stdin: TextIO, prompt: str) -> Optional[str]:
    ctx.stdout.write(prompt)
    ctx.stdout.flush()
    line = stdin.readline()
    if not line:
        return None
    return line.strip()


def wait_for_enter(ctx: Ctx, stdin: TextIO) -> None:
    _ask(ctx, stdin, "\nPress Enter to close. ")


def _list_seats(ctx: Ctx, data: Dict) -> List[Dict]:
    """Print the numbered seat list (state and pane) of a ``collect_status`` answer."""
    seats = data["seats"]
    ctx.say("Fleet %s" % data["fleet_file"])
    for number, seat in enumerate(seats, 1):
        drift = " (drift)" if seat["drift"] else ""
        ctx.say("  %d) %-24s %-9s %-8s %s%s" % (number, seat["name"], seat["live_kind"] or seat["kind"],
                                                 seat["state"], seat["pane_id"] or "-", drift))
    return seats


def _pick_seats(ctx: Ctx, stdin: TextIO) -> Optional[List[str]]:
    """Returns [] for all seats, [name] for one, None to quit."""
    seats = _list_seats(ctx, collect_status(ctx, integrations=False))
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


def run_menu(ctx: Ctx, stdin: TextIO, go: bool = False) -> int:
    ctx.keys_allowed = False  # design 23.7, invariant 21: a popup never presses a key in an agent's pane
    if go:
        return run_go(ctx, stdin)
    names = _pick_seats(ctx, stdin)
    if names is None:
        return 0
    while True:
        action = _pick_action(ctx, stdin)
        if action is None:
            return 0
        if action == "go" and not names:
            ctx.say("go needs one seat")
            continue
        break
    if action == "go":  # no confirmation, and no Enter after a success (design 22.2)
        return _go(ctx, stdin, names[0], wait=True)
    if not names and action in ("restart", "kickoff"):
        names = [s.name for s in ctx.fleet.seats]
    note = None
    close_panes = False
    if action == "tile":
        names = []  # tile always re-tiles every live seat
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


def _go(ctx: Ctx, stdin: TextIO, name: str, wait: bool) -> int:
    """Focus ``name`` and return at once on success, so the popup closes and the focus
    stays on the seat. Otherwise print the line and, with ``wait``, wait for Enter."""
    code, line = op_go(ctx, name)
    ctx.say(line)
    if code != EXIT_OK and wait:
        wait_for_enter(ctx, stdin)
    return code


def run_go(ctx: Ctx, stdin: TextIO) -> int:
    """``menu --go`` (design 22.2): the seats numbered with state and pane, then the
    contacts as ``c1``, ``c2`` with their state from ``agent list``; one answer is
    focused and the popup exits 0. ``q``, Enter or end of input close with nothing done;
    an absent seat or contact says so and asks again."""
    data = collect_status(ctx, integrations=False)
    seats = _list_seats(ctx, data)
    contacts = data.get("contacts") or []
    for number, contact in enumerate(contacts, 1):
        ctx.say("  c%d) %-23s %-9s %-8s %s" % (number, contact["name"], "contact", contact["state"],
                                                contact["pane_id"] or "-"))
    choices = "number, c1-c%d" % len(contacts) if contacts else "number"
    while True:
        answer = _ask(ctx, stdin, "Go to (%s, or q to quit): " % choices)
        if answer is None or answer.lower() in ("q", ""):
            return 0
        name = _choice(answer.lower(), seats, contacts)
        if name is None:
            ctx.say("Not a choice: %s" % answer)
            continue
        if _go(ctx, stdin, name, wait=False) == EXIT_OK:
            return 0


def _choice(answer: str, seats: List[Dict], contacts: List[Dict]) -> Optional[str]:
    """``3`` -> the third seat, ``c1`` -> the first contact, else None."""
    if answer.isdigit() and 1 <= int(answer) <= len(seats):
        return seats[int(answer) - 1]["name"]
    if answer.startswith("c") and answer[1:].isdigit() and 1 <= int(answer[1:]) <= len(contacts):
        return contacts[int(answer[1:]) - 1]["name"]
    return None


def _run(ctx: Ctx, action: str, names: List[str], note: Optional[str], close_panes: bool) -> Report:
    if action == "restart":
        return op_restart(ctx, names, note)
    if action == "swap":
        return op_swap(ctx, names, note)
    if action == "kickoff":
        return op_kickoff(ctx, names, note)
    if action == "down":
        return op_down(ctx, names, close_panes=close_panes)
    if action == "tile":
        return op_tile(ctx)
    return op_up(ctx, names, note=note)
