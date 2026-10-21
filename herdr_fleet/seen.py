"""How long a seat has been in its state (design 20.5).

Every command that reads seat states (``status``, ``send``, ``deliver``,
``inbox``, ``watch``) passes what it saw to ``observe``, which keeps
``state_seen = {"state", "since"}`` in the seat's state-file entry: ``since``
becomes now whenever the observed state differs from the recorded one. The age
is therefore a lower bound (since herdr-fleet last noticed). ``status`` prints
``blocked (4m)`` and ``unknown (12m)``, and the watcher's ``notify_blocked_after``
reads the same record. Nothing here touches herdr or an agent.
"""

from __future__ import annotations

import datetime
from typing import Dict, Mapping, Optional, Sequence

from herdr_fleet import inbox
from herdr_fleet.state import record_seen

AGED = ("blocked", "unknown")  # the states status prints with their age


def observe(ctx, observed: Mapping[str, str]) -> Dict[str, Dict]:
    """Record the states just observed for the fleet's seats (other names are ignored).
    Returns ``{seat: {"state", "since"}}``; {} when nothing was recorded (no state file
    yet, a dry run, or an unwritable file, which is a warning)."""
    seats = {s.name for s in ctx.fleet.seats}
    picked = {str(n): str(s) for n, s in observed.items() if n in seats and s}
    if not picked or ctx.dry_run:
        return {}
    try:
        result = record_seen(ctx.fleet.repo, picked, inbox.now_utc())
    except OSError as exc:
        ctx.warn("cannot record seat states in the state file: %s" % exc)
        return {}
    state = getattr(ctx, "_state", None)  # keep a state already loaded in step, so a later save agrees
    if state is not None:
        for name, seen in result.items():
            entry = state.agents.get(name)
            if isinstance(entry, dict):
                entry["state_seen"] = seen
    return result


def observe_agents(ctx, agents: Sequence[Dict], extra: Optional[Mapping[str, str]] = None) -> Dict[str, Dict]:
    """``observe`` every seat from an ``agent list`` answer: a seat that is not listed is
    ``absent``. ``extra`` overrides single seats with a fresher reading."""
    live = {str(a["name"]): str(a.get("agent_status") or "unknown") for a in agents if a.get("name")}
    observed = {s.name: live.get(s.name, "absent") for s in ctx.fleet.seats}
    observed.update(extra or {})
    return observe(ctx, observed)


def age_of(seen: Optional[Mapping], status: str, now: Optional[datetime.datetime] = None) -> Optional[float]:
    """Seconds the seat has been in ``status`` by its record, or None when the record is
    for another state or has no readable ``since``."""
    if not seen or seen.get("state") != status:
        return None
    when = inbox.parse_time(seen.get("since"))
    if when is None:
        return None
    return ((now or inbox.now_utc()) - when).total_seconds()


def label(status: str, seen: Optional[Mapping], now: Optional[datetime.datetime] = None) -> str:
    """``blocked (4m)`` or ``unknown (12m)``; any other state, or no record, as it is."""
    if status not in AGED:
        return status
    age = age_of(seen, status, now)
    return status if age is None else "%s (%s)" % (status, inbox.age_label(age))
