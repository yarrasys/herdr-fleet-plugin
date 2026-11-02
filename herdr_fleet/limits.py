"""Provider limits and failover (design section 35): pure helpers, no herdr, no agent.

A seat whose provider refuses further turns shows it on its screen. ``limit_line`` finds that
line in the last 6 non-blank lines above the input area of a screen (``seats.LIMIT_PATTERNS``, one
list per kind, anchored at the start of the cleaned line) and
``new_record`` builds the ``limited`` record the watcher keeps in the seat's state entry:
``kind``, the matched ``line``, ``at``, and ``resets_at`` when the line names a time (or the
kind's window is known: Kimi's is five hours) with ``resets_hint`` for the second case.

The record is the whole story of one episode. It holds the seat's queued mail for as long as
it ``holds`` (the seat still runs the kind that was limited, ``released`` was not set by
``deliver NAME``, and the reset time, when there is one, has not passed). After a swap to
another kind it no longer holds, but it still ``counts`` for ``swap --fallback``, which skips
any kind with a limit still recorded, until the reset time passes or ``released`` is set.

Nothing here reads or writes a file. The screen text a caller passes is searched and not kept:
only the one matched line (at most ``LINE_MAX`` characters) leaves ``limit_line``.
"""

from __future__ import annotations

import datetime
import re
from typing import Dict, List, Mapping, Optional, Sequence

from herdr_fleet import inbox
from herdr_fleet.seats import (LIMIT_BULLETS, LIMIT_MARKER_REQUIRED, LIMIT_PATTERNS, LIMIT_WINDOW_HINTS, LIMIT_WINDOW_S, PROMPT_GLYPHS,
                               PROMPT_PLACEHOLDER_PATTERNS, PROMPT_PLACEHOLDERS)
from herdr_fleet.state import patch_entry, read_agents

LINE_MAX = 200
ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
MONTHS = {name: number for number, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
RESET_RE = re.compile(
    r"(?:resets?|try again)\s*(?:at|on|after)?\s*"
    r"(?:(?P<month>[A-Za-z]{3})[a-z]*\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s*(?:(?P<year>\d{4}),?\s*)?"
    r"(?:at\s+)?)?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>[ap]m)\b"
    r"(?:\s*\((?P<zone>[A-Za-z_]+(?:/[A-Za-z_+-]+)*)\))?", re.IGNORECASE)


def patterns_for(kind: str) -> List["re.Pattern[str]"]:
    return [re.compile(p, re.IGNORECASE) for p in LIMIT_PATTERNS.get(kind, [])]


def has_patterns(kind: Optional[str]) -> bool:
    return bool(kind) and bool(LIMIT_PATTERNS.get(str(kind)))


INPUT_GLYPHS = ("\u276f", "\u203a", ">")   # the first character of an input line: Claude Code, Codex, Kimi, Antigravity
BORDER_CHARS = set("\u2500\u2501\u256d\u256e\u2570\u256f \u2502\u2503")  # a rule or the top of an input box
MARKERS = "\u23bf\u25a0 \xa0"                # a result or error marker that starts the line
ABOVE_INPUT = 6                               # design 35.6: a limit line counts within this many non-blank lines
QUIT_ABOVE = 3                                # ... and, to quit a working seat, within this many (35.5 item 21)


def _bare(raw: str) -> str:
    return ESCAPE_RE.sub("", raw).strip().strip("\u2502\u2503").strip()


def input_top(lines: List[str]) -> Optional[int]:
    """Where the seat's input area starts in ``lines`` (design 35.6): the rule or box top directly
    above the last line that is an input line (a prompt glyph, bare or inside an input box), else the
    input line itself; None when no input line is on screen (a dialog, a menu)."""
    for index in range(len(lines) - 1, -1, -1):
        bare = _bare(lines[index])
        if bare and bare[0] in INPUT_GLYPHS and (len(bare) == 1 or bare[1] in " \xa0"):
            above = _bare_raw(lines[index - 1]) if index else ""
            if above and above[0] in "\u2500\u2501\u256d" and set(above) <= BORDER_CHARS:
                return index - 1
            return index
    return None


def _bare_raw(raw: str) -> str:
    return ESCAPE_RE.sub("", raw).strip()


def limit_line(kind: str, screen: str, above: int = ABOVE_INPUT) -> Optional[str]:
    """The last line of ``screen`` that matches one of the kind's limit patterns, cleaned (no escapes,
    at most LINE_MAX characters), or None. Only the last ABOVE_INPUT non-blank lines above the input
    area count (35.6; ``above`` narrows that: the quit of a limited working seat asks for QUIT_ABOVE,
    35.5 item 21); no input area on screen counts nothing. A pattern is anchored at the start of
    the line once its marker (`⎿`, `■`) and leading spaces are removed (35.5 item 18), so a sentence,
    list item, table row or tool output that merely quotes the message does not match; a kind that
    names a required marker (Codex's `■`) matches only a line that carried it. Lines that start with
    the kind's prompt glyph or an assistant bullet are skipped."""
    compiled = patterns_for(kind)
    if not compiled:
        return None
    lines = screen.splitlines()
    top = input_top(lines)
    if top is None:
        return None
    region = [raw for raw in lines[:top] if raw.strip()][-above:]
    skip = tuple(PROMPT_GLYPHS.get(kind, ())) + tuple(LIMIT_BULLETS)
    marker = LIMIT_MARKER_REQUIRED.get(kind)
    for raw in reversed(region):
        bare = _bare(raw)
        if not bare or bare.startswith(skip):
            continue
        if marker and not bare.startswith(marker):
            continue
        text = bare.lstrip(MARKERS).strip()
        if any(p.match(text) for p in compiled):
            return text[:LINE_MAX]
    return None


def input_empty(kind: str, screen: str) -> bool:
    """The input line on ``screen`` holds nothing (35.6): its glyph alone, or the kind's placeholder.
    False when there is text or no input line on screen. Plain text cannot tell Claude Code's faint
    suggestion from typed text, so a suggestion counts as text: the answer errs towards refusing."""
    lines = screen.splitlines()
    top = input_top(lines)
    if top is None:
        return False
    for raw in lines[top:]:
        bare = _bare(raw)
        if bare and bare[0] in INPUT_GLYPHS and (len(bare) == 1 or bare[1] in " \xa0"):
            text = bare[1:].strip()
            if not text or text in PROMPT_PLACEHOLDERS.get(kind, ()):
                return True
            return any(re.fullmatch(p, text) for p in PROMPT_PLACEHOLDER_PATTERNS.get(kind, ()))
    return False


def parse_reset(line: str, now: datetime.datetime) -> Optional[datetime.datetime]:
    """The reset time a limit line names (``resets 3pm``, ``reset at 3:05pm (Australia/Sydney)``,
    ``try again at Nov 5th, 2026 8:44 PM``) as an aware datetime, else None. A clock time with no
    date is the next such time after ``now``; a zone in brackets is honoured when the platform
    knows it, else the machine's local zone is used."""
    match = RESET_RE.search(line)
    if not match:
        return None
    hour, minute = int(match.group("hour")), int(match.group("minute") or 0)
    if not (1 <= hour <= 12 and 0 <= minute < 60):
        return None
    hour = hour % 12 + (12 if match.group("ampm").lower() == "pm" else 0)
    zone = _zone(match.group("zone"))
    here = now.astimezone(zone) if zone is not None else now.astimezone()
    try:
        if match.group("month"):
            month = MONTHS.get(match.group("month").lower())
            if month is None:
                return None
            year = int(match.group("year") or here.year)
            when = here.replace(year=year, month=month, day=int(match.group("day")), hour=hour, minute=minute,
                                second=0, microsecond=0)
            if not match.group("year") and when < here:
                when = when.replace(year=year + 1)
        else:
            when = here.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if when <= here:
                when += datetime.timedelta(days=1)
    except ValueError:
        return None
    return when


def _zone(name: Optional[str]):
    if not name:
        return None
    try:
        import zoneinfo  # Python 3.9+
        return zoneinfo.ZoneInfo(name)
    except Exception:  # noqa: BLE001 - no zoneinfo, or an unknown zone: the machine's own zone
        return None


def new_record(kind: str, line: str, now: Optional[datetime.datetime] = None) -> Dict:
    """The ``limited`` record for a seat running ``kind`` whose screen showed ``line``."""
    now = now or inbox.now_utc()
    record: Dict = {"kind": kind, "line": line, "at": inbox.iso_local(now)}
    reset = parse_reset(line, now)
    if reset is not None:
        record["resets_at"] = inbox.iso_local(reset)
    elif kind in LIMIT_WINDOW_S:
        record["resets_at"] = inbox.iso_local(now + datetime.timedelta(seconds=LIMIT_WINDOW_S[kind]))
        record["resets_hint"] = LIMIT_WINDOW_HINTS.get(kind, "window")
    return record


def reset_time(record: Optional[Mapping]) -> Optional[datetime.datetime]:
    return inbox.parse_time(record.get("resets_at")) if record else None


def passed(record: Optional[Mapping], now: Optional[datetime.datetime] = None) -> bool:
    """The record's reset time is known and no longer ahead."""
    when = reset_time(record)
    return when is not None and (now or inbox.now_utc()) >= when


def counts(record: Optional[Mapping], now: Optional[datetime.datetime] = None) -> bool:
    """The limit is still recorded for ``swap --fallback`` (35.3): there is a record, the owner has not
    released it with ``deliver NAME`` and its reset time, if any, has not passed."""
    return bool(record) and bool(record.get("kind")) and not record.get("released") and not passed(record, now)


def holds(record: Optional[Mapping], running_kind: Optional[str], now: Optional[datetime.datetime] = None) -> bool:
    """The record holds the seat's queued mail (35.2): it counts, and the seat still runs the
    kind that was limited (a swap to another kind ends the hold)."""
    return counts(record, now) and bool(running_kind) and record.get("kind") == running_kind


def records(entry: Optional[Mapping]) -> List[Dict]:
    """Every limit record of a seat's state entry: the current ``limited`` first, then
    ``limited_also``, the older records of other kinds that a newer one replaced (so a seat that
    hit a second provider's limit still remembers the first's)."""
    if not isinstance(entry, Mapping):
        return []
    found = [entry["limited"]] if isinstance(entry.get("limited"), dict) else []
    found += [r for r in (entry.get("limited_also") or []) if isinstance(r, dict)]
    return found


def kinds_limited(agents: Mapping[str, Mapping], now: Optional[datetime.datetime] = None) -> Dict[str, List[str]]:
    """Every kind with a limit still recorded in any seat's entry, with the seats that recorded it."""
    found: Dict[str, List[str]] = {}
    for name, entry in agents.items():
        for record in records(entry):
            if counts(record, now):
                found.setdefault(str(record["kind"]), []).append(str(name))
    return found


def replaced(entry: Optional[Mapping], record: Dict, now: Optional[datetime.datetime] = None) -> Dict:
    """The fields to write when ``record`` becomes a seat's current ``limited``: itself, and
    ``limited_also`` holding the records of other kinds that still count."""
    others: Dict[str, Dict] = {}
    for old in records(entry):
        if old.get("kind") != record.get("kind") and counts(old, now):
            others.setdefault(str(old["kind"]), old)
    return {"limited": record, "limited_also": list(others.values())}


def holding(repo: str, name: str, running: Optional[str] = None,
            now: Optional[datetime.datetime] = None) -> Optional[Dict]:
    """The record that holds ``name``'s queued mail right now, read fresh from the state file
    (design 35.2): None when there is none. The seat's running kind is the entry's ``kind``, else
    ``running`` (the caller's best knowledge: the live agent's, or the fleet file's)."""
    entry = read_agents(repo).get(name) or {}
    record = entry.get("limited")
    return record if holds(record, entry.get("kind") or running, now) else None


def release(repo: str, name: str, running: Optional[str] = None) -> bool:
    """``deliver NAME`` (35.2): end the hold. The record stays, marked ``released``; a released
    record no longer counts (for the hold or for ``swap --fallback``), and the next limit line
    the watcher sees starts a new episode. True when a record was released."""
    record = holding(repo, name, running)  # only the record of the kind the seat runs now
    if record is None:
        return False
    return patch_entry(repo, name, {"limited": dict(record, released=inbox.iso_local(inbox.now_utc()))})


def since_label(record: Mapping) -> str:
    """``10:41``, the local time the limit was first seen (blank when unreadable)."""
    when = inbox.parse_time(record.get("at"))
    return when.astimezone().strftime("%H:%M") if when else ""


def status_label(record: Mapping) -> str:
    """``limited (claude, since 10:41)`` (35.2) for ``status`` and the board."""
    since = since_label(record)
    return "limited (%s%s)" % (record.get("kind") or "?", ", since " + since if since else "")


def epoch_time(seconds: object) -> Optional[datetime.datetime]:
    """An epoch-seconds number (Codex's ``resets_at``) as an aware datetime; None when it is not one."""
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds <= 0:
        return None
    try:
        return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def reset_label(when: Optional[datetime.datetime]) -> str:
    """``Thu 20:44``: the local day and time of a reset."""
    return when.astimezone().strftime("%a %H:%M") if when else "?"


def fallback_candidates(seat, limited: Mapping[str, Sequence[str]], running: Optional[str] = None) -> List[str]:
    """The kinds a tier seat could move to, in order (35.3): those of ``prefer`` after the kind it
    runs (``running``, the live agent's kind when the caller knows it, else the seat's), then those
    before it, never the running kind and never one with a limit still recorded."""
    kinds = list(seat.prefer)
    running = running if running in kinds else seat.kind
    if running not in kinds:
        return [k for k in kinds if k not in limited]
    at = kinds.index(running)
    ordered = kinds[at + 1:] + kinds[:at]
    return [k for k in ordered if k not in limited]
