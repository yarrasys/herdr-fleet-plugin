"""Self-handover (design 32.5): a seat restarted or swapped when its own turn ends.

A seat may not restart, swap or quit itself (its own pane is refused, invariant 5), and
that rule stays. ``restart NAME --handover`` and ``swap NAME --handover`` give the seat a
visible helper instead:

- ``request`` runs in the seat's own command. Only the caller's own seat is allowed
  (``--handover`` naming another seat, or run outside a seat's pane, is a usage error);
  a ``swap`` without drift prints ``unchanged`` and opens nothing; one handover per fleet.
  It opens the plugin pane entrypoint ``handover`` in a new tab of the seat's workspace
  (``plugin pane open --placement tab --no-focus``) with ``HERDR_FLEET_FILE`` and
  ``HERDR_FLEET_HANDOVER`` (one JSON object: ``op``, ``seat``, ``wait_ms`` and, when given,
  ``note_file`` and ``drop_inbox``), records the pane in the state file as ``"handover"``
  (``created_pane``: herdr-fleet opened it) and returns at once, telling the seat to end
  its turn.
- ``op_handover`` is ``herdr-fleet handover``, which only that pane runs: it checks that it
  runs in the recorded pane for the recorded seat and operation, waits with ``agent wait
  NAME --until idle --until done`` for at most what is left of ``wait_ms``, then runs
  ``ops.op_restart`` or ``ops.op_swap`` for that one seat from its own pane, so every guard
  of 4.3, 6, 21.2, 32.1 and 32.2 applies unchanged. A seat refused because it was working
  again is waited for again; a blocked seat, a held quit or a failed start ends it. On
  success it notifies, drops its record and closes its own pane (``pane close``, a pane
  it created, invariant 4); otherwise it notifies, prints the report and waits for Enter
  before doing the same.

The helper is a foreground process in a pane the owner sees and can close, started only
by the seat's explicit command (invariant 11). The watcher never starts one or acts for
one (invariant 12). It sends nothing to any agent except through restart and swap.
"""

from __future__ import annotations

import json
import os
from typing import Dict, Optional, Sequence, TextIO

from herdr_fleet import PLUGIN_ID, inbox
from herdr_fleet.checks import drift_reasons
from herdr_fleet.context import EXIT_OK, EXIT_PARTIAL, Ctx, OpError, Report, UsageError
from herdr_fleet.fleetfile import NAME_RE, FleetError
from herdr_fleet.herdr import HerdrError, describe, plugin_tab_argv, render_cmd
from herdr_fleet.menu import wait_for_enter
from herdr_fleet.ops import op_restart, op_swap, prune, select_seats
from herdr_fleet.seats import SETTLED, live_by_name
from herdr_fleet.state import ensure_gitignore, now_iso

ENV = "HERDR_FLEET_HANDOVER"
ENTRYPOINT = "handover"
OPS = ("restart", "swap")
DEFAULT_WAIT_MS = 3600000
DONE = {"restart": "restarted", "swap": "swapped"}
NOT_OWN = "--handover is for your own seat; run %s NAME from another pane for any other"
STOPS = ("blocked", "refused", "failed")  # a seat result that ends the helper (with "refused": unless working)


def note_path(repo: str, seat: str) -> str:
    """``.herdr/handover/<seat>-note.md``: the copy of ``--note`` the helper reads."""
    return os.path.join(repo, ".herdr", "handover", "%s-note.md" % seat)


# -- the seat's own command ------------------------------------------------------------------


def request(ctx: Ctx, op: str, names: Sequence[str], note: Optional[str] = None, wait_ms: Optional[int] = None,
            drop_inbox: bool = False) -> Report:
    """``restart NAME --handover`` / ``swap NAME --handover`` (design 32.5): open the helper
    pane for the caller's own seat and return at once."""
    report = Report(op)
    fleet = ctx.fleet
    seats = select_seats(fleet, names, required=True)
    if wait_ms is not None and wait_ms <= 0:
        raise UsageError("--wait takes milliseconds, more than 0")
    pane = ctx.invocation.pane_id
    agent = live_by_name(ctx.herdr).get(seats[0].name) if len(seats) == 1 else None
    if not pane or agent is None or str(agent.get("pane_id") or "") != pane:
        raise UsageError(NOT_OWN % op)
    seat = seats[0]
    prune(ctx)  # a recorded handover pane that is gone is dropped first
    if op == "swap" and not drift_reasons(ctx, seat, agent):
        report.add(seat.name, "unchanged", "matches the fleet file")
        return report
    current = ctx.state.handover
    if current:
        report.add(seat.name, "refused", "a handover is already waiting in %s (%s %s); one at a time"
                   % (current["pane_id"], current.get("op"), current.get("seat")), "partial")
        return report
    wait = wait_ms or DEFAULT_WAIT_MS
    spec: Dict[str, object] = {"op": op, "seat": seat.name, "wait_ms": wait}
    if note is not None:
        spec["note_file"] = note_path(fleet.repo, seat.name)
    if drop_inbox:
        spec["drop_inbox"] = True
    env = {"HERDR_FLEET_FILE": fleet.path, ENV: json.dumps(spec)}
    workspace = str(agent.get("workspace_id") or ctx.invocation.workspace_id or "")
    plugin = ctx.env.get("HERDR_PLUGIN_ID") or PLUGIN_ID
    then = "then herdr-fleet %s %s once it is idle or done (up to %s)" % (op, seat.name,
                                                                          inbox.age_label(wait / 1000.0))
    if ctx.dry_run:
        report.add(seat.name, "plan", "%s (the handover helper, in a new tab)"
                   % render_cmd(plugin_tab_argv(plugin, ENTRYPOINT, workspace, fleet.repo, env)))
        report.add(seat.name, "plan", then)
        return report
    if note is not None:
        path = note_path(fleet.repo, seat.name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        ensure_gitignore(os.path.join(fleet.repo, ".herdr"))
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(note)
    try:
        opened = ctx.herdr.plugin_tab_open(plugin, ENTRYPOINT, workspace, fleet.repo, env)
    except HerdrError as exc:
        report.add(seat.name, "failed", "cannot open the handover pane: %s" % describe(exc), "fail")
        return report
    helper = str(opened["pane_id"])
    ctx.state.set_handover(helper, seat.name, op, now_iso())
    ctx.save()
    report.add(seat.name, "handover", "waiting in %s (new tab); you are %s when this turn ends, so end your turn "
               "now and do not start inbox --wait" % (helper, DONE[op]))
    return report


# -- the helper pane ---------------------------------------------------------------------------


def parse_spec(raw: Optional[str], repo: str) -> Dict:
    """``HERDR_FLEET_HANDOVER`` as the request wrote it; a usage error when it is missing or
    not one (the command is the handover pane's alone)."""
    try:
        data = json.loads(raw or "")
    except ValueError:
        data = None
    if (not isinstance(data, dict) or data.get("op") not in OPS or not isinstance(data.get("seat"), str)
            or not NAME_RE.match(data["seat"])):
        raise UsageError("herdr-fleet handover runs only in the pane that `restart NAME --handover` or `swap NAME "
                         "--handover` opens (%s is missing or not valid)" % ENV)
    wait = data.get("wait_ms")
    spec = {"op": data["op"], "seat": data["seat"], "drop_inbox": data.get("drop_inbox") is True,
            "wait_ms": wait if isinstance(wait, int) and not isinstance(wait, bool) and wait > 0 else DEFAULT_WAIT_MS,
            "note_file": None}
    note = data.get("note_file")
    if isinstance(note, str) and note:
        if os.path.abspath(note) != os.path.abspath(note_path(repo, spec["seat"])):
            raise UsageError("%s names a note outside .herdr/handover: %s" % (ENV, note))
        spec["note_file"] = note
    return spec


def op_handover(ctx: Ctx, stdin: TextIO) -> int:
    """``herdr-fleet handover`` (design 32.5): wait for the recorded seat's turn to end, run
    its recorded restart or swap, then end as 32.5 says."""
    spec = parse_spec(ctx.env.get(ENV), ctx.fleet.repo)
    pane = ctx.invocation.pane_id
    record = _recorded(ctx, spec, pane)
    if record is None:
        raise OpError("this pane (%s) is not the recorded handover pane (%s); herdr-fleet handover runs only in the "
                      "pane that restart or swap --handover opened"
                      % (pane or "none", (ctx.state.handover or {}).get("pane_id") or "none recorded"))
    name, op = spec["seat"], spec["op"]
    if ctx.fleet.seat(name) is None:
        raise OpError("%s is no longer a seat of %s" % (name, ctx.fleet.path))
    note = _read_note(spec["note_file"])
    deadline = ctx.clock.monotonic() + spec["wait_ms"] / 1000.0
    ctx.say("handover: waiting for %s to end its turn (idle or done, up to %s), then herdr-fleet %s %s"
            % (name, inbox.age_label(spec["wait_ms"] / 1000.0), op, name))
    while True:
        report = Report(op)
        left = int((deadline - ctx.clock.monotonic()) * 1000)
        problem = "%s did not end its turn within %s" % (name, inbox.age_label(spec["wait_ms"] / 1000.0))
        if left > 0:
            try:
                ctx.herdr.agent_wait(name, SETTLED, left)
                problem = ""
            except HerdrError as exc:
                if exc.code != "timeout":
                    problem = "waiting for %s: %s" % (name, describe(exc))
        if problem:
            report.add(name, "stopped", problem, "partial")
            for line in report.lines():
                ctx.say(line)
            return _stopped(ctx, name, report, stdin, spec)
        ctx._state = None  # the wait may have been long: read the state and the fleet file afresh
        ctx._fleet = None
        try:
            if op == "swap":
                report = op_swap(ctx, [name], note)
            else:
                report = op_restart(ctx, [name], note, None, drop_inbox=spec["drop_inbox"])
        except (UsageError, OpError, FleetError, HerdrError) as exc:
            detail = describe(exc) if isinstance(exc, HerdrError) else (
                "; ".join(exc.errors) if isinstance(exc, FleetError) else str(exc))
            report.add(name, "failed", detail, "fail")
        for line in report.lines():
            ctx.say(line)
        verdict = _verdict(ctx, report, name)
        if verdict == "again":
            ctx.say("handover: %s is working again; waiting for it once more" % name)
            continue
        if verdict == "stop":
            return _stopped(ctx, name, report, stdin, spec)
        title = "Fleet: %s %s" % (name, DONE[op]) if verdict == "done" else "Fleet: %s unchanged" % name
        ctx.notify(title, "\n".join(report.lines()), "done")
        _close(ctx, spec)
        return EXIT_OK


RECORD_WAIT_S = 2.0  # the request saves the record only after herdr opened this pane (review N7)


def _recorded(ctx: Ctx, spec: Dict, pane: Optional[str]) -> Optional[Dict]:
    """The state's handover record when it names this pane, seat and operation, read again
    every 250 ms for up to RECORD_WAIT_S; None when it never does."""
    deadline = ctx.clock.monotonic() + RECORD_WAIT_S
    while True:
        record = ctx.state.handover
        if (record and pane and str(record["pane_id"]) == pane and record.get("seat") == spec["seat"]
                and record.get("op") == spec["op"]):
            return record
        if not pane or ctx.clock.monotonic() >= deadline:
            return None
        ctx.clock.sleep(0.25)
        ctx._state = None  # read the state file again


def _read_note(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError as exc:
        raise OpError("cannot read the handover note %s: %s" % (path, exc))


def _verdict(ctx: Ctx, report: Report, name: str) -> str:
    """``done`` (the seat started, and nothing for it was blocked, refused or failed),
    ``unchanged`` (a swap found no drift any more), ``again`` (refused while it was working
    again: wait once more) or ``stop``."""
    statuses = [r.status for r in report.results if r.name == name]
    if "started" in statuses and not any(s in STOPS for s in statuses):
        return "done"
    if statuses and all(s == "unchanged" for s in statuses):
        return "unchanged"
    if "refused" in statuses and "started" not in statuses and "failed" not in statuses:
        try:
            state = str(ctx.herdr.agent_get(name).get("agent_status") or "")
        except HerdrError:
            return "stop"
        return "again" if state == "working" else "stop"
    return "stop"


def _stopped(ctx: Ctx, name: str, report: Report, stdin: TextIO, spec: Dict) -> int:
    """Notify, keep the pane (and its record) until the owner presses Enter, then close it."""
    ctx.notify("Fleet: handover of %s stopped" % name, "\n".join(report.lines()), "request")
    ctx.say("handover of %s stopped; nothing more will be done here" % name)
    wait_for_enter(ctx, stdin)
    _close(ctx, spec)
    return report.exit_code() or EXIT_PARTIAL


def _close(ctx: Ctx, spec: Optional[Dict]) -> None:
    """Drop the record (saved first: closing the pane ends this process), remove the note
    copy, then close the helper's own pane, which herdr-fleet created (invariant 4)."""
    record = ctx.state.handover
    ctx.state.drop_handover()
    ctx.save()
    if spec and spec.get("note_file"):
        try:
            os.remove(spec["note_file"])
        except OSError:
            pass
    pane = ctx.invocation.pane_id
    if record and record.get("created_pane") and str(record["pane_id"]) == pane:
        try:
            ctx.herdr.pane_close(pane)
        except HerdrError as exc:
            ctx.warn("cannot close the handover pane %s: %s" % (pane, describe(exc)))
