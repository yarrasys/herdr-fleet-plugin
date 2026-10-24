"""Pane names (design 22.1): every seat's pane carries the seat's name on its border.

herdr draws a pane's manual name (``pane rename``) in its border in place of the
detected agent kind, so a fleet of Claude seats no longer reads "claude" six times.
``apply`` runs at the end of ``up``, ``restart``, ``swap``, ``kickoff``, ``tile`` and
``status``: one ``pane get`` per live seat, and ``pane rename`` only for a pane whose
``label`` is not the wanted name (the seat key ``label``, else the seat name). ``down``
calls ``clear`` for a seat pane it leaves open at a shell prompt.

Only panes hosting one of the fleet's live seats are ever renamed, and only to that
seat's wanted name (invariant 20): contacts, other fleets' agents, the watcher strip
and plugin panes never are. Neither command reaches the agent. A failed rename is a
warning and never stops the command; nothing is renamed on a dry run.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from herdr_fleet.context import Ctx, Report
from herdr_fleet.fleetfile import Seat
from herdr_fleet.herdr import HerdrError, describe
from herdr_fleet.seats import live_by_name
from herdr_fleet.state import StateError

TOPIC = "panes"  # the report line's name: ``panes: named 4 after their seats``


def wanted(seat: Seat) -> str:
    """The name a seat's pane should carry: its ``label`` key, else the seat name."""
    return seat.label or seat.name


def seat_panes(ctx: Ctx, live: Optional[Dict[str, Dict]] = None) -> List[Tuple[Seat, str]]:
    """(seat, pane) for every seat with a live agent, in file order. ``live`` maps agent
    names to ``agent list`` records (read when not given). The watcher strip is never a
    seat's pane; it is skipped should a record ever say otherwise."""
    if live is None:
        live = live_by_name(ctx.herdr)
    try:
        strip = ctx.state.watch_pane
    except StateError:
        strip = None
    found = []
    for seat in ctx.fleet.seats:
        pane = (live.get(seat.name) or {}).get("pane_id")
        if pane and str(pane) != strip:
            found.append((seat, str(pane)))
    return found


def summary(named: List[str]) -> str:
    """``named 1 (sample-designer)`` or ``named 4 after their seats``."""
    if len(named) == 1:
        return "named 1 (%s)" % named[0]
    return "named %d after their seats" % len(named)


def apply(ctx: Ctx, live: Optional[Dict[str, Dict]] = None, report: Optional[Report] = None) -> List[str]:
    """Name every live seat's pane as ``wanted`` says; return the seats renamed. With a
    report, add one ``panes:`` line when something changed and a warning line per
    failure (the exit code is left alone); without one (``status``), print nothing on
    stdout and send failures to stderr. Never on a dry run."""
    if ctx.dry_run:
        return []
    try:
        panes = seat_panes(ctx, live)
    except HerdrError as exc:
        _warn(ctx, report, "could not list the seats: %s" % describe(exc))
        return []
    named: List[str] = []
    for seat, pane in panes:
        name = wanted(seat)
        try:
            if ctx.herdr.pane_get(pane).get("label") == name:
                continue
            ctx.herdr.pane_rename(pane, name)
        except HerdrError as exc:
            _warn(ctx, report, "could not name %s: %s" % (pane, describe(exc)))
            continue
        named.append(seat.name)
    if named and report is not None:
        report.add(TOPIC, summary(named), "")
    return named


def clear(ctx: Ctx, pane: str, report: Report) -> bool:
    """``down``: remove the name of a seat pane left open at a shell prompt, so an empty
    shell is not labelled as a seat. A pane without a name, or hosting an agent again,
    is left alone. True when the name was cleared."""
    if ctx.dry_run:
        return False
    try:
        info = ctx.herdr.pane_get(pane)
        if not info.get("label") or info.get("agent"):
            return False
        ctx.herdr.pane_clear_name(pane)
    except HerdrError as exc:
        if "not_found" in exc.code:  # closed meanwhile: nothing left to name
            return False
        _warn(ctx, report, "could not clear the name of %s: %s" % (pane, describe(exc)))
        return False
    return True


def unnamed(ctx: Ctx) -> List[str]:
    """Doctor (design 22.1): the live seats whose pane is not named as wanted, sorted.
    Read-only: ``agent list`` and one ``pane get`` per live seat; a pane that cannot be
    read is not counted."""
    missing = []
    for seat, pane in seat_panes(ctx):
        try:
            label = ctx.herdr.pane_get(pane).get("label")
        except HerdrError:
            continue
        if label != wanted(seat):
            missing.append(seat.name)
    return sorted(missing)


def _warn(ctx: Ctx, report: Optional[Report], text: str) -> None:
    if report is not None:
        report.add(TOPIC, text, "")
    else:
        ctx.warn("%s: %s" % (TOPIC, text))
