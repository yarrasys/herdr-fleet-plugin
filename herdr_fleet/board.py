"""The board (design 24): what the watcher strip draws instead of a scrolling log.

- ``build(ctx, watcher_state)`` is the model, rebuilt by the watcher every
  ``BOARD_REFRESH_S`` and after every watcher event, from exactly the sources of
  design 24.2: one ``agent list`` (seats' states, kinds and panes; contacts'
  states); ``pane get`` for a working seat, whose ``terminal_title_stripped`` is
  what it is doing (a title equal to the seat's name or label is nothing); the
  ``state_seen`` records (design 20.5, recorded here for what the list showed, as
  every command that reads states does) for each state's age; the mailboxes
  (count, oldest age, the next message's id, sender and first line; the owner's
  mailbox too); ``messages.owed`` over the log, parsed again only when
  ``.herdr/messages.jsonl`` changed; the Claude seats' context size from their
  transcripts (``context_size``, usage numbers only); for a ``blocked`` seat only,
  ``agent read NAME --lines 16`` and the first line of the dialog it shows
  (``seats.dialog_first_line``), kept in the model being built and nothing else
  of the screen kept at all (invariants 19 and 22); and the watcher's own state
  (``WatcherState``: what it waits on, holds and their reasons, retries, the last
  event, the last error). Nothing here prompts, presses a key or moves the focus.
- ``render(board, width, height, colours)`` is pure: the lines to draw, each a
  list of ``(text, style)`` spans, ``style`` being space-separated words (``bold``,
  ``dim``, ``reverse``, ``red``, ``green``, ``yellow``). The widths, breakpoints,
  packing and colours are design 24.3's.
- ``visible(board, width, height)`` is the seats as ``render`` places them, so the
  number keys and the selection mean what the owner sees.
- ``render_log(lines, width, height, colours)`` is the ``l`` view: the last lines of
  ``.herdr/watch.log``.
"""

from __future__ import annotations

import datetime
import os
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from herdr_fleet import contacts as contact_mod
from herdr_fleet import context_size, inbox, seen
from herdr_fleet.herdr import HerdrError, describe
from herdr_fleet.mailroom import tags
from herdr_fleet.messages import OWNER, first_line, log_path, owed, owed_lines, read, since_label, unsettled
from herdr_fleet.seats import EXIT_DIALOG_LINES, dialog_lines

Span = Tuple[str, str]
Line = List[Span]

DIALOG_LINES = EXIT_DIALOG_LINES      # `agent read NAME --lines 16` (design 24.2)
DIALOG_SHOWN = 4                      # a seat page shows up to four lines of the dialog (design 25.2)
NAME_W, STATE_W, CONTEXT_W = 20, 14, 10
RATIO = (2, 3, 2)                     # doing : queue : debt, of what is left (design 24.3)
DOING_FROM, DEBT_FROM, FULL_FROM = 120, 90, 60   # the doing cell needs 120 columns, the debt 90, a full row 60
PACK_HALF = 60                        # packed rows: two seats per row while each half has 60 columns
COUNT_W = 10                          # the queue count cell of a narrow or packed row
WARN = "⚠"
ELLIPSIS = "…"
DASH = "–"
DOT = " · "
GLYPHS = {"working": "●", "idle": "○", "blocked": "◐", "done": "✓", "unknown": "?",
          "absent": DASH}
STATE_STYLE = {"blocked": "red bold", "working": "green", "idle": "dim", "done": "dim"}
CONTACT = "\u2194 "                 # design 26.6: a contact's name on the board and its page starts so
CONTACT_STYLE = "contact"           # ... and its whole row is drawn dim
PLAIN_WORDS = ("bold", "reverse")     # what is left of a style with colours off (design 24.3)


# -- the model ------------------------------------------------------------------------------------


@dataclass
class Event:
    """The watcher's last line: ``time`` is ``HH:MM``."""

    time: str
    text: str
    error: bool = False


@dataclass
class Row:
    """One seat (or contact) on the board."""

    name: str                       # the seat or contact name: `go` and `ack` use it
    shown: str                      # the seat's label, else its name
    state: str                      # working, idle, blocked, done, unknown, absent (ambiguous for a contact)
    index: int = 0                  # file order
    age: Optional[float] = None     # seconds in that state, as far as herdr-fleet knows
    doing: str = ""                 # the title while working; the dialog's first line while blocked
    queued: int = 0
    oldest: Optional[float] = None  # age of the oldest queued message
    next_id: str = ""               # the next message in delivery order
    next_from: str = ""
    next_line: str = ""
    note: str = ""                  # from the watcher: `holding: owner typing`, `retrying in 45 s`, ...
    debt_to: str = ""               # the oldest debt this seat owes: to whom (full name) ...
    debt_shown: str = ""            # ... as the cell shows it
    debt_id: str = ""               # its short id ("" for a message sent before ids)
    debt_age: Optional[float] = None
    debt_line: str = ""
    tokens: Optional[int] = None    # context size (Claude seats with a transcript) ...
    context_at: str = ""            # ... and the time of the call it was read from
    over: bool = False              # at or above restart_hint_tokens
    contact: bool = False
    kind: str = ""                  # the live agent's kind, else the fleet file's (design 25.2)
    pane: str = ""                  # the live agent's pane
    dialog: List[str] = field(default_factory=list)  # a blocked seat's dialog, up to four lines (25.2)


@dataclass
class WatcherState:
    """What the watcher itself is doing, snapshotted for ``build`` (design 24.2) and the
    fleet page (25.3)."""

    pid: Optional[int] = None
    since: str = ""                                # when its lock was taken
    warnings: List[str] = field(default_factory=list)  # what other code warned of lately (board mode)
    watching: Optional[float] = None               # seconds since it started
    waiting: List[str] = field(default_factory=list)   # mailboxes with a waiter
    holds: Dict[str, str] = field(default_factory=dict)  # mailbox -> the presence guard's detail
    retries: Dict[str, float] = field(default_factory=dict)  # mailbox -> seconds to the next try
    pointed: List[str] = field(default_factory=list)  # mailboxes a pointer prompt already named
    event: Optional[Event] = None
    trouble: str = ""                              # the loop's own error, while it lasts
    cache: Optional["Cache"] = None


@dataclass
class Board:
    fleet: str
    rows: List[Row] = field(default_factory=list)
    contacts: List[Row] = field(default_factory=list)
    watching: Optional[float] = None
    owner_count: int = 0
    owner_oldest: Optional[float] = None
    owner_id: str = ""
    owner_from: str = ""
    owner_line: str = ""
    waiting: int = 0
    holding: int = 0
    retrying: int = 0
    event: Optional[Event] = None
    error: str = ""                  # what the footer shows in red: the watcher's trouble, else problems
    problems: str = ""               # what this build could not read
    threshold: Optional[int] = None
    owner_newest: Optional["Ref"] = None  # design 25.3: the newest message to the owner, delivered or not
    selected: Optional[str] = None   # set by the driver: the selected seat's name, or HEADER
    notice: str = ""                 # set by the driver: a confirmation, the help line, the `q` line
    hints: str = ""                  # set by the driver: the root view's key hints (design 25.1)


class Cache:
    """What ``build`` keeps between refreshes: the owed list until the log changes, each
    transcript's reading until the file changes, and when a state was first seen for
    seats the state file cannot record (no state file yet) and for contacts."""

    def __init__(self) -> None:
        self.log_sig: Optional[Tuple[int, int]] = None
        self.owed_items: List[Dict] = []
        self.entries: List[Dict] = []          # the log, as last parsed (design 25: pages read it)
        self.unsettled_items: List[Dict] = []  # every request still awaiting an answer, oldest first
        self.contexts: Dict[str, Tuple[Tuple, Optional[Dict]]] = {}
        self.first: Dict[str, Tuple[str, datetime.datetime]] = {}

    def owed(self, repo: str) -> List[Dict]:
        """``messages.owed`` over the log, parsed again only when the file changed."""
        try:
            info = os.stat(log_path(repo))
            sig: Optional[Tuple[int, int]] = (info.st_mtime_ns, info.st_size)
        except OSError:
            sig = None
        if sig != self.log_sig or sig is None:
            self.entries = read(repo) if sig is not None else []
            self.owed_items = owed(self.entries)
            self.unsettled_items = [item for _, item in unsettled(self.entries)]
            self.log_sig = sig
        return self.owed_items

    def context(self, agent: Dict, env) -> Optional[Dict]:
        """``context_size.seat_context`` for a live Claude seat, read again only when its
        transcript changed, and looked for again only when it is gone (usage numbers and
        the timestamp only, invariant 9)."""
        sid = context_size.session_id(agent)
        if sid is None:
            return None
        known = self.contexts.get(sid)
        path = known[0][0] if known is not None else None
        try:  # the transcript found last time, while it is there; else look for it again
            info = os.stat(path) if path else None
        except OSError:
            info = None
        if info is None:
            path = context_size.find_transcript(context_size.projects_dir(env), sid)
            if path is None:
                return None
            try:
                info = os.stat(path)
            except OSError:
                return None
        sig = (path, info.st_mtime_ns, info.st_size)
        if known is not None and known[0] == sig:
            return known[1]
        record = context_size.last_usage(path)
        value = None if record is None else {"tokens": context_size.context_tokens(record), "at": record["timestamp"]}
        self.contexts[sid] = (sig, value)
        return value

    def since(self, name: str, state: str, now: datetime.datetime) -> datetime.datetime:
        known = self.first.get(name)
        if known is None or known[0] != state:
            self.first[name] = (state, now)
            return now
        return known[1]


def hold_reason(detail: str) -> str:
    """The presence guard's detail, short: ``owner typing``, ``text in its input``, ``a
    dialog or a menu``; anything else up to its first ``;``."""
    for prefix in ("owner typing", "text in its input", "a dialog or a menu"):
        if detail.startswith(prefix):
            return prefix
    return detail.split(";", 1)[0].strip()


def short_name(name: str, fleet) -> str:
    """A name as the debt cell shows it: a seat's label, else without the fleet's
    ``<workspace>-`` prefix (``sample-orchestrator`` -> ``orchestrator``)."""
    seat = fleet.seat(name)
    if seat is not None and seat.label:
        return seat.label
    prefix = (fleet.workspace or "") + "-"
    if fleet.workspace and name.startswith(prefix) and len(name) > len(prefix):
        return name[len(prefix):]
    return name


def _age(when: object, now: datetime.datetime) -> Optional[float]:
    parsed = inbox.parse_time(when)
    return None if parsed is None else max(0.0, (now - parsed).total_seconds())


def _queue(row: Row, messages: Sequence[inbox.Message], ws: WatcherState, now: datetime.datetime) -> None:
    row.queued = len(messages)
    if messages:
        oldest = min(messages, key=inbox.Message.sort_key)
        row.oldest = _age(oldest.time, now)
        head = messages[0]  # delivery order: urgent first, then oldest first
        row.next_id, row.next_from, row.next_line = head.short, head.sender, first_line(head.body)
    if row.name in ws.holds:
        row.note = "holding: %s" % hold_reason(ws.holds[row.name])
    elif ws.retries.get(row.name, 0) > 0:
        row.note = "retrying in %d s" % int(round(ws.retries[row.name]))
    elif row.name in ws.pointed:
        row.note = "pointed to inbox --read"


def _debt(row: Row, debts: Sequence[Dict], fleet, now: datetime.datetime) -> None:
    mine = [o for o in debts if o.get("to") == row.name]
    if not mine:
        return
    oldest = min(mine, key=lambda o: inbox.parse_time(o.get("since")) or now)
    row.debt_to = str(oldest["from"])
    row.debt_shown = short_name(row.debt_to, fleet)
    row.debt_id = inbox.short_of(str(oldest["id"])) if oldest.get("id") else ""
    row.debt_age = _age(oldest.get("since"), now)
    row.debt_line = str(oldest.get("first_line") or "")


def _title(ctx, agent: Dict, seat) -> str:
    """What a working seat is doing: its pane's ``terminal_title_stripped`` (Claude Code and
    Codex set it to the task); its own name or label is nothing."""
    pane = str(agent.get("pane_id") or "")
    if not pane:
        return ""
    try:
        title = clean(str(ctx.herdr.pane_get(pane).get("terminal_title_stripped") or "")).strip()
    except HerdrError:
        return ""
    return "" if title in (seat.name, seat.label or seat.name) else title


def _dialog(ctx, name: str) -> List[str]:
    """For a blocked seat: up to four lines of the dialog on its screen, the first of them
    being 23.7's first dialog line, and nothing else of the screen, which is dropped here
    (invariant 22)."""
    try:
        screen = ctx.herdr.agent_read(name, DIALOG_LINES)
    except HerdrError:
        return []
    lines = [clean(line) for line in dialog_lines(screen, DIALOG_SHOWN)]
    del screen
    return lines


def build(ctx, watcher_state: Optional[WatcherState] = None) -> Board:
    """The model of design 24.2. Total: a herdr or file error becomes the board's ``error``
    and the cells it would have filled stay empty."""
    ws = watcher_state or WatcherState()
    cache = ws.cache if ws.cache is not None else Cache()
    fleet, repo = ctx.fleet, ctx.fleet.repo
    now = inbox.now_utc()
    problems: List[str] = []
    try:
        agents: Optional[List[Dict]] = ctx.herdr.agent_list()
    except HerdrError as exc:
        agents = None
        problems.append("cannot read herdr: %s" % describe(exc))
    live = {str(a["name"]): a for a in agents or [] if a.get("name")}
    states: Dict[str, str] = {}
    for seat in fleet.seats:
        agent = live.get(seat.name)
        if agents is None:
            states[seat.name] = "unknown"
        else:
            states[seat.name] = str(agent.get("agent_status") or "unknown") if agent else "absent"
    records = seen.observe(ctx, states) if agents is not None else {}
    try:
        waiting = inbox.queued(repo, now)
    except OSError as exc:
        waiting = {}
        problems.append("cannot read the mailboxes: %s" % exc)
    try:
        debts = cache.owed(repo)
    except OSError as exc:
        debts = []
        problems.append("cannot read the message log: %s" % exc)
    threshold = fleet.restart_hint_tokens
    rows: List[Row] = []
    for seat in fleet.seats:
        agent = live.get(seat.name)
        state = states[seat.name]
        row = Row(name=seat.name, shown=seat.label or seat.name, state=state, index=seat.index,
                  kind=str((agent or {}).get("agent") or seat.kind), pane=str((agent or {}).get("pane_id") or ""))
        record = records.get(seat.name)
        if record and record.get("state") == state:
            row.age = seen.age_of(record, state, now)
        elif agents is not None:
            row.age = max(0.0, (now - cache.since(seat.name, state, now)).total_seconds())
        if agent is not None and state == "working":
            row.doing = _title(ctx, agent, seat)
        elif agent is not None and state == "blocked":
            row.dialog = _dialog(ctx, seat.name)
            row.doing = row.dialog[0] if row.dialog else ""
        _queue(row, waiting.get(seat.name, []), ws, now)
        _debt(row, debts, fleet, now)
        if agent is not None:
            reading = cache.context(agent, ctx.env)
            if reading is not None:
                row.tokens, row.context_at = int(reading["tokens"]), str(reading.get("at") or "")
                row.over = bool(threshold) and row.tokens >= int(threshold)
        rows.append(row)
    contact_rows: List[Row] = []
    for index, contact in enumerate(fleet.contacts):
        resolution = contact_mod.resolve(contact, agents) if agents is not None else None
        state = resolution.state if resolution is not None else "unknown"
        row = Row(name=contact.name, shown=contact.name, state=state, index=len(rows) + index, contact=True,
                  pane=str((resolution.pane if resolution is not None else "") or ""),
                  kind=str(((resolution.agent if resolution is not None else None) or {}).get("agent") or ""))
        if agents is not None:
            row.age = max(0.0, (now - cache.since("contact:" + contact.name, state, now)).total_seconds())
        _queue(row, waiting.get(contact.name, []), ws, now)
        _debt(row, debts, fleet, now)
        contact_rows.append(row)
    board = Board(fleet=fleet.workspace or os.path.basename(repo), rows=rows, contacts=contact_rows,
                  watching=ws.watching, waiting=len(ws.waiting), holding=len(ws.holds),
                  retrying=len([n for n, left in ws.retries.items() if left > 0]), event=ws.event,
                  threshold=threshold)
    owner = waiting.get(OWNER, [])
    if owner:
        board.owner_count = len(owner)
        board.owner_oldest = _age(min(owner, key=inbox.Message.sort_key).time, now)
        board.owner_id, board.owner_from, board.owner_line = owner[0].short, owner[0].sender, first_line(owner[0].body)
    try:
        board.owner_newest = newest_to(repo, OWNER, cache.entries)
    except OSError as exc:
        problems.append("cannot read the owner's mailbox: %s" % exc)
    board.problems = "; ".join(problems)
    board.error = ws.trouble or board.problems
    return board


# -- text helpers ----------------------------------------------------------------------------------


def clean(text: str) -> str:
    """One line of plain text: tabs and newlines become spaces, other control characters go."""
    out = []
    for char in str(text):
        if char in "\t\n\r":
            out.append(" ")
        elif unicodedata.category(char) in ("Cc", "Cf"):
            continue
        else:
            out.append(char)
    return "".join(out)


def char_width(char: str) -> int:
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def width_of(text: str) -> int:
    return sum(char_width(c) for c in text)


def cut(text: str, width: int) -> str:
    """``text`` in at most ``width`` cells, cut with ``…`` (never wrapped)."""
    if width <= 0:
        return ""
    if width_of(text) <= width:
        return text
    out, used = [], 0
    for char in text:
        size = char_width(char)
        if used + size > width - 1:
            break
        out.append(char)
        used += size
    return "".join(out) + ELLIPSIS


def fit(text: str, width: int) -> str:
    """``text`` cut to ``width`` cells and padded with spaces to exactly ``width``."""
    text = cut(text, width)
    return text + " " * max(0, width - width_of(text))


def quoted(text: str, width: int) -> str:
    """``"text"`` in at most ``width`` cells; a cut text loses its closing quote to ``…``."""
    if width_of(text) + 2 <= width:
        return '"%s"' % text
    return '"' + cut(text, width - 1) if width >= 3 else cut('"' + text, width)


def short_age(seconds: Optional[float]) -> str:
    """``<1m``, ``25m``, ``2h``, ``3d``: the board's ages (the cells are narrow)."""
    if seconds is None:
        return ""
    minutes = int(max(0.0, seconds) // 60)
    if minutes < 1:
        return "<1m"
    if minutes < 60:
        return "%dm" % minutes
    if minutes < 24 * 60:
        return "%dh" % (minutes // 60)
    return "%dd" % (minutes // (24 * 60))


def style_of(words: str, colours: bool, selected: bool = False) -> str:
    parts = words.split()
    if not colours:
        parts = [p for p in parts if p in PLAIN_WORDS]
    if selected and "reverse" not in parts:
        parts.append("reverse")
    return " ".join(parts)


# -- the cells --------------------------------------------------------------------------------------


def state_text(row: Row) -> str:
    glyph = GLYPHS.get(row.state, "?")
    age = short_age(row.age)
    return "%s %s%s" % (glyph, row.state, " " + age if age else "")


def doing_text(row: Row, width: int) -> str:
    if not row.doing:
        return ""
    return quoted(row.doing, width) if row.state == "blocked" else cut(row.doing, width)


def queue_text(row: Row, width: int) -> str:
    """``2 queued, oldest 3m  #103001 "first line…"``, ``1 queued, holding: owner typing`` or ``–``."""
    if not row.queued:
        return DASH
    head = "%d queued" % row.queued
    if row.note:
        return cut("%s, %s" % (head, row.note), width)
    age = short_age(row.oldest)
    if age:
        head += (", oldest %s" if row.queued > 1 else ", %s") % age
    if not row.next_id:
        return cut(head, width)
    ident = "%s  #%s" % (head, row.next_id)
    room = width - width_of(ident) - 1
    if room >= 4 and row.next_line:
        return ident + " " + quoted(clean(row.next_line), room)
    return cut(ident, width)


def count_text(row: Row) -> str:
    return "%d queued" % row.queued if row.queued else DASH


def debt_text(row: Row, width: int) -> str:
    """``owes orchestrator 2h #103002`` or ``–``."""
    if not row.debt_to:
        return DASH
    text = "owes %s %s" % (row.debt_shown or row.debt_to, short_age(row.debt_age))
    if row.debt_id:
        text += " #%s" % row.debt_id
    return cut(text, width)


def context_text(row: Row) -> str:
    if row.tokens is None:
        return ""
    return "ctx %s" % context_size.tokens_label(row.tokens)


def columns(width: int) -> List[Tuple[str, int]]:
    """The cells of a seat row and their widths at ``width`` columns (design 24.3)."""
    if width < FULL_FROM:
        name = max(4, min(NAME_W, width - STATE_W - COUNT_W))
        return [("name", name), ("state", STATE_W), ("count", max(1, width - name - STATE_W))]
    rest = max(0, width - NAME_W - STATE_W - CONTEXT_W)
    if width >= DOING_FROM:
        cells, weights = ["doing", "queue", "debt"], RATIO
    elif width >= DEBT_FROM:
        cells, weights = ["queue", "debt"], RATIO[1:]
    else:
        cells, weights = ["queue"], RATIO[1:2]
    total = sum(weights)
    sizes = [rest * w // total for w in weights]
    sizes[cells.index("queue")] += rest - sum(sizes)
    return [("name", NAME_W), ("state", STATE_W)] + list(zip(cells, sizes)) + [("context", CONTEXT_W)]


def compact_columns(width: int) -> List[Tuple[str, int]]:
    """Half a packed row: name, state, queue count, ``⚠``."""
    name = max(4, min(NAME_W, width - STATE_W - COUNT_W - 2))
    return [("name", name), ("state", STATE_W), ("count", max(1, width - name - STATE_W - 2)), ("warn", 2)]


def cell_spans(cell: str, row: Row, width: int, colours: bool, selected: bool, last: bool) -> Line:
    """One cell as spans, exactly ``width`` cells wide; every cell but the last of a row
    keeps one blank column after its text."""
    room = width if last else width - 1
    state_style = STATE_STYLE.get(row.state, "")
    spans: Line = []
    if cell == "name":
        spans.append((cut((CONTACT if row.contact else "") + row.shown, room), ""))
    elif cell == "state":
        spans.append((cut(state_text(row), room), state_style))
    elif cell == "doing":
        spans.append((doing_text(row, room), "red bold" if row.state == "blocked" else
                      "green" if row.state == "working" else ""))
    elif cell == "queue":
        spans.append((queue_text(row, room), ""))
    elif cell == "count":
        spans.append((cut(count_text(row), room), ""))
    elif cell == "debt":
        spans.append((debt_text(row, room), ""))
    elif cell == "context":
        text = context_text(row)
        if row.over and text:
            spans.append((cut(text + " ", max(0, room - 1)), ""))
            spans.append((WARN, "yellow"))
        else:
            spans.append((cut(text, room), ""))
    elif cell == "warn":
        spans.append((WARN if row.over else "", "yellow" if row.over else ""))
    used = sum(width_of(text) for text, _ in spans)
    spans.append((" " * max(0, width - used), ""))
    if row.contact:  # design 26.6: the whole row of a contact is dim, its state colour included
        spans = [(text, CONTACT_STYLE) for text, _ in spans]
    return [(text, style_of(style, colours, selected)) for text, style in spans if text]


def row_line(row: Row, width: int, colours: bool, selected: bool,
             layout: Optional[List[Tuple[str, int]]] = None) -> Line:
    cols = layout or columns(width)
    line: Line = []
    for position, (cell, size) in enumerate(cols):
        line.extend(cell_spans(cell, row, size, colours, selected, position == len(cols) - 1))
    return clip(line, width)


def clip(line: Line, width: int) -> Line:
    """A line cut (with ``…``) or padded to exactly ``width`` cells: on a very narrow
    terminal even the fixed cells do not fit."""
    out: Line = []
    used = 0
    for text, style in line:
        size = width_of(text)
        if used + size > width:
            if width - used > 0:
                out.append((cut(text, width - used), style))
                used += width_of(out[-1][0])
            break
        out.append((text, style))
        used += size
    if used < width:
        out.append((" " * (width - used), out[-1][1] if out and "reverse" in out[-1][1] else ""))
    return out


# -- the layout -------------------------------------------------------------------------------------


def urgency(row: Row) -> Tuple[int, int]:
    """Blocked first, then seats with a queue, then working ones, then the rest in file order."""
    if row.state == "blocked":
        rank = 0
    elif row.queued:
        rank = 1
    elif row.state == "working":
        rank = 2
    else:
        rank = 3
    return rank, row.index


def plan(board: Board, width: int, height: int) -> Tuple[str, List[Row], int]:
    """``(mode, rows in display order, hidden count)``. Seats and contacts share capacity;
    overflow favours urgent seats, then contacts in file order (design 27.2). Below
    three rows the existing most-urgent-seat view stays a single line."""
    seats = list(board.rows)
    if height < 3:
        chosen = sorted(seats, key=urgency)[:1]
        return "single", chosen, max(0, len(seats) - len(chosen))
    room = height - 2
    combined = seats + list(board.contacts)
    if len(combined) <= room:
        return "rows", combined, 0
    mode = "packed" if width >= 2 * PACK_HALF else "rows"
    capacity = 2 * room if mode == "packed" else room
    if len(combined) <= capacity:
        return mode, combined, 0
    chosen = sorted(seats, key=urgency)[:capacity]
    chosen += list(board.contacts)[:capacity - len(chosen)]
    return mode, chosen, len(combined) - len(chosen)


def visible(board: Board, width: int, height: int) -> List[Row]:
    """The rows on screen in the order the owner reads them (packed: left, then right)."""
    return plan(board, width, height)[1]


# -- the lines --------------------------------------------------------------------------------------


def owner_cell(board: Board) -> str:
    """Design 25.3: ``owner: #103003 12:13 from partner-workspace "Example ready…"`` (the
    newest message to the owner, delivered or not), or ``owner: nothing``."""
    ref = board.owner_newest
    if ref is None:
        return "owner: nothing"
    text = "owner: %s%s from %s" % ("#%s " % ref.short if ref.id else "", inbox.time_label(ref.time), ref.sender)
    if ref.first_line:
        text += " " + quoted(clean(ref.first_line), 40)
    return text


def header_layout(board: Board, width: int) -> Tuple[str, str, Tuple[int, int]]:
    """``(left, right, owner columns)``: the header's text cut to ``width`` and where the
    owner cell sits in it, for a click (design 25.1)."""
    prefix = board.fleet
    if board.watching is not None:
        prefix += DOT + "watching %s" % short_age(board.watching)
    prefix += DOT
    left = prefix + owner_cell(board)
    parts = []
    if board.waiting:
        parts.append("waiting on %d" % board.waiting)
    if board.holding:
        parts.append("holding %d" % board.holding)
    if board.retrying:
        parts.append("retrying %d" % board.retrying)
    right = DOT.join(parts)
    if right and width_of(left) + 2 + width_of(right) <= width:
        left = left + " " * (width - width_of(left) - width_of(right))
    elif right and width >= 2 * width_of(right) + 2:
        left = fit(left, width - width_of(right) - 2) + "  "
    else:
        left, right = fit(left, width), ""
    start = width_of(prefix)
    return left, right, (start, min(start + width_of(owner_cell(board)), width_of(left.rstrip())))


def header_line(board: Board, width: int, colours: bool) -> Line:
    left, right, _ = header_layout(board, width)
    selected = board.selected == HEADER
    line: Line = [(left, style_of("bold", colours, selected))]
    if right:
        line.append((right, style_of("", colours, selected)))
    return line


def fit_hints(hints: str, room: int) -> str:
    """The key hints cut from the right: as many whole hints, from the first, as fit."""
    out = ""
    for item in hints.split(DOT) if hints else []:
        candidate = item if not out else out + DOT + item
        if width_of(candidate) > room:
            break
        out = candidate
    return out


@dataclass
class Footer:
    """What the driver tells a view to show at the bottom (design 24.3, 25.1)."""

    event: Optional[Event] = None
    notice: str = ""
    error: str = ""


def footer_spans(width: int, footer: Footer, hints: str, more: int, colours: bool) -> Line:
    """The last event (or the watcher's error, or a notice, which takes the whole line),
    ``+N more`` on the board, and the view's key hints at the right end, cut from the right
    when the width is short (design 25.1). Exactly ``width`` cells."""
    if footer.notice:
        return [(fit(clean(footer.notice), width), style_of("bold", colours))]
    if footer.error:
        text, style = footer.error, "red"
    elif footer.event is not None:
        text, style = "%s %s" % (footer.event.time, footer.event.text), "red" if footer.event.error else ""
    else:
        text, style = "", ""
    text = clean(text)
    tail = "  +%d more" % more if more else ""
    shown = ""
    if hints:  # what the event leaves, but never less than a third of the line while they need it
        room = max(width - width_of(text) - width_of(tail) - 2, min(width_of(hints), width // 3))
        shown = fit_hints(hints, room)
    right = ("  " + shown) if shown else ""
    room = max(0, width - width_of(tail) - width_of(right))
    line: Line = [(fit(text, room), style_of(style, colours))]
    if tail:
        line.append((tail, ""))
    if right:
        line.append((right, style_of("dim", colours)))
    return clip(line, width)


def footer_line(board: Board, width: int, more: int, colours: bool) -> Line:
    return footer_spans(width, Footer(board.event, board.notice, board.error), board.hints, more, colours)


def blank(width: int) -> Line:
    return [(" " * width, "")]


def render(board: Board, width: int, height: int, colours: bool) -> List[Line]:
    """Design 24.3 and 27.2, pure: the header, seats then contacts (packed two to a row,
    or urgent seats then contacts with ``+N more`` when they do not fit), the
    footer; below three rows, the most urgent seat's row alone."""
    if width < 1 or height < 1:
        return []
    mode, rows, more = plan(board, width, height)
    if mode == "single":
        if not rows:
            return [[(fit("%s: no seats" % board.fleet, width), "")]]
        return [row_line(rows[0], width, colours, rows[0].name == board.selected)]  # no room for hints
    lines: List[Line] = [header_line(board, width, colours)]
    if mode == "packed":
        left_w = width // 2
        right_w = width - left_w
        for start in range(0, len(rows), 2):
            pair = rows[start:start + 2]
            line = row_line(pair[0], left_w, colours, pair[0].name == board.selected, compact_columns(left_w))
            if len(pair) > 1:
                line += row_line(pair[1], right_w, colours, pair[1].name == board.selected,
                                 compact_columns(right_w))
            else:
                line += blank(right_w)
            lines.append(line)
    else:
        for row in rows:
            lines.append(row_line(row, width, colours, row.name == board.selected))
    while len(lines) < height - 1:
        lines.append(blank(width))
    lines.append(footer_line(board, width, more, colours))
    return lines


def render_log(lines: Sequence[str], width: int, height: int, colours: bool) -> List[Line]:
    """The ``l`` view: a title line, then the last lines of ``.herdr/watch.log``."""
    if width < 1 or height < 1:
        return []
    title = [(fit("watch.log" + DOT + "any key returns to the board", width), style_of("reverse", colours))]
    if height == 1:
        last = lines[-1] if lines else ""
        return [[(fit(clean(last), width), "")]]
    shown = list(lines)[-(height - 1):]
    out: List[Line] = [title]
    out.extend([(fit(clean(line), width), "")] for line in shown)
    while len(out) < height:
        out.append(blank(width))
    return out


def plain_text(lines: Sequence[Line]) -> List[str]:
    """The text of rendered lines, trailing blanks dropped (tests and the log view)."""
    return ["".join(text for text, _ in line).rstrip() for line in lines]


# -- pages (design 25) ------------------------------------------------------------------------------

HEADER = "header"                     # the board's selection when it is on the header line
HINTS_BOARD = DOT.join(["Enter open", "g go", "a ack", "d deliver", "z zoom", "l log", "? help"])
HINTS_SEAT = DOT.join(["Esc back", "\u2191\u2193 scroll", "\u2190\u2192 seat", "g go", "a ack", "d deliver",
                       "p screen", "z zoom"])
HINTS_BODY = DOT.join(["Esc back", "\u2191\u2193 scroll"])
HINTS_FLEET = DOT.join(["Esc back", "\u2191\u2193 scroll", "z zoom"])
HINTS_OWNER = DOT.join(["Esc back", "\u2191\u2193 select", "Enter open", "a ack", "z zoom"])
HINTS_CONTACT = DOT.join(["Esc back", "\u2191\u2193 select", "Enter open", "\u2190\u2192 seat", "z zoom"])
BACK_KEYS = ("esc", "backspace", "q")
LABEL_W = 10                          # the section label column of a seat page
RECENT = 20                           # design 25.2: the seat's last twenty log entries
SCREEN_LINES = 12                     # design 25.2: `p` reads the seat's last twelve screen lines
FLEET_LOG_LINES = 200                 # the fleet page's log tail
STATES = {"refusal": "queued (on refusal)", "queue": "queued", "fyi": "queued (fyi)"}

Action = Tuple  # ("open", kind, name), ("replace", name), ("back",), ("go", name), ("ack", Ref),
#                 ("deliver_all",), ("deliver", name), ("screen", name), ("body", Ref), ("notice", text)


@dataclass
class Ref:
    """A message a page lists, or a debt on one (design 25.2 to 25.4)."""

    id: str = ""            # the full id ("" for a message sent before ids), or a short id
    sender: str = ""
    to: str = ""
    time: str = ""
    first_line: str = ""
    state: str = ""         # queued (on refusal), sent, delivered, expired, ...
    message: bool = True    # False for a log entry that is no message (a kickoff, an ack, an interrupt)
    debt: bool = False      # ``to`` owes ``sender`` a reply to it
    age: Optional[float] = None  # a debt's age

    @property
    def short(self) -> str:
        return inbox.short_of(self.id) if self.id else ""

    @property
    def key(self) -> Tuple[str, str, str, bool]:
        return (self.id, self.sender, self.time, self.debt)


@dataclass
class PageRow:
    label: str
    text: str
    style: str = ""
    ref: Optional[Ref] = None   # a row the selection can stop on


def tail_lines(path: str, count: int, limit: int = 64 * 1024) -> List[str]:
    """The last ``count`` lines of a text file (at most ``limit`` bytes read); [] without it."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            data = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = data.splitlines()
    if size > limit and lines:
        lines = lines[1:]  # the first one is probably cut
    return lines[-count:] if count > 0 else []


def fates(entries: Sequence[Dict]) -> Dict[str, str]:
    """Each message id's last known fate in the log: a ``send`` entry's delivery, then
    ``delivered`` (``delivered?`` when unconfirmed), ``expired`` or ``dropped``."""
    found: Dict[str, str] = {}
    for entry in entries:
        ident = entry.get("id")
        kind = entry.get("kind", "send")
        if not ident:
            continue
        if kind == "send":
            found[str(ident)] = str(entry.get("delivery") or "sent")
        elif kind == "delivered":
            found[str(ident)] = "delivered?" if entry.get("delivery") == "unconfirmed" else "delivered"
        elif kind in ("expired", "dropped"):
            found[str(ident)] = str(kind)
    return found


def _file_ids(repo: str, name: str) -> Dict[str, str]:
    """{id: box} of a mailbox's message files, without reading them."""
    found: Dict[str, str] = {}
    for box in (inbox.NEW, inbox.CUR):
        try:
            folder = inbox.box_dir(repo, name, box)
            names = os.listdir(folder) if os.path.isdir(folder) else []
        except Exception:  # noqa: BLE001 - not a mailbox name: nothing there
            names = []
        for filename in names:
            if filename.endswith(".md") and inbox.ID_RE.match(filename[:-3]):
                found[filename[:-3]] = box
    return found


def _log_only(entries: Sequence[Dict], files: Dict[str, str], match) -> List[Ref]:
    return [Ref(id=str(e.get("id") or ""), sender=str(e["from"]), to=str(e["to"]), time=str(e["time"]),
                first_line=str(e.get("first_line") or ""), state=str(e.get("delivery") or "sent"))
            for e in entries if e.get("kind", "send") == "send" and match(e) and str(e.get("id") or "") not in files]


def _newest_first(refs: List[Ref]) -> List[Ref]:
    def when(ref: Ref) -> float:
        parsed = inbox.parse_time(ref.time)
        return parsed.timestamp() if parsed is not None else 0.0
    return sorted(refs, key=when, reverse=True)


def newest_to(repo: str, name: str, entries: Sequence[Dict]) -> Optional[Ref]:
    """The newest message to ``name``, delivered or not (design 25.3): the newest file of
    its mailbox (ids sort by time, so only that one is read) or a log-only ``send``."""
    files = _file_ids(repo, name)
    candidates: List[Ref] = []
    if files:
        ident = max(files)
        try:
            message = inbox.load(os.path.join(inbox.box_dir(repo, name, files[ident]), ident + ".md"), files[ident])
            candidates.append(Ref(id=message.id, sender=message.sender, to=message.to, time=message.time,
                                  first_line=first_line(message.body)))
        except (OSError, ValueError):
            pass
    candidates += _log_only(entries, files, lambda e: str(e["to"]) == name)
    ordered = _newest_first(candidates)
    return ordered[0] if ordered else None


def messages_to(repo: str, name: str, entries: Sequence[Dict]) -> List[Ref]:
    """Every message to ``name``, newest first, delivered or not: its mailbox's files and
    the log's ``send`` entries without one (design 25.3, the owner page)."""
    known = fates(entries)
    refs: List[Ref] = []
    for box in (inbox.NEW, inbox.CUR):
        for message in inbox.read_box(repo, name, box):
            state = STATES[message.mode] if box == inbox.NEW else known.get(message.id, "delivered")
            refs.append(Ref(id=message.id, sender=message.sender, to=message.to, time=message.time,
                            first_line=first_line(message.body), state=state))
    refs += _log_only(entries, _file_ids(repo, name), lambda e: str(e["to"]) == name)
    return _newest_first(refs)


def exchanged(entries: Sequence[Dict], name: str) -> List[Ref]:
    """The messages ``name`` sent or received in this fleet's log, newest first (the contact
    page), each with its last known fate."""
    known = fates(entries)
    refs = [Ref(id=str(e.get("id") or ""), sender=str(e["from"]), to=str(e["to"]), time=str(e["time"]),
                first_line=str(e.get("first_line") or ""),
                state=known.get(str(e.get("id") or ""), str(e.get("delivery") or "sent")))
            for e in entries if e.get("kind", "send") == "send" and name in (e.get("from"), e.get("to"))]
    return _newest_first(refs)


def find_message(repo: str, ref: Ref) -> Optional[inbox.Message]:
    """The stored message a ref names: its recipient's mailbox first, then any mailbox."""
    if not ref.id:
        return None
    if inbox.ID_RE.match(ref.id) and ref.to:
        for box in (inbox.NEW, inbox.CUR):
            try:
                path = os.path.join(inbox.box_dir(repo, ref.to, box), ref.id + ".md")
            except Exception:  # noqa: BLE001 - not a mailbox name
                break
            if os.path.exists(path):
                try:
                    return inbox.load(path, box)
                except (OSError, ValueError):
                    return None
    try:
        return inbox.resolve(repo, ref.id)
    except Exception:  # noqa: BLE001 - unknown, ambiguous or unreadable: a log-only message
        return None


def _hhmm(text: str) -> str:
    return inbox.time_label(text) if text else "--:--"


def _quote(text: str) -> str:
    return '"%s"' % clean(text) if text else ""


def _debt_text(item: Dict, name: str) -> str:
    ident = "#%s" % inbox.short_of(str(item["id"])) if item.get("id") else "no id"
    if item["to"] == name:
        head = "owes %s" % item["from"]
    else:
        head = "owed by %s" % item["to"]
    return "%s since %s (%s)  %s" % (head, since_label(str(item["since"])), ident, _quote(item.get("first_line", "")))


def _recent_text(entry: Dict, name: str, senders: Dict[str, str]) -> Tuple[str, Optional[Ref]]:
    """One line of a seat page's ``recent`` section (design 25.2) and the ref it opens."""
    kind = entry.get("kind", "send")
    when = _hhmm(str(entry.get("time") or ""))
    ident = str(entry.get("id") or "")
    short = " #%s" % inbox.short_of(ident) if ident else ""
    line = clean(str(entry.get("first_line") or ""))
    if kind == "send":
        outgoing = entry.get("from") == name
        other = entry["to"] if outgoing else entry["from"]
        text = "%s %s %s%s %s" % (when, "\u2192" if outgoing else "\u2190", other, short,
                                  entry.get("delivery") or "sent")
        ref = Ref(id=ident, sender=str(entry["from"]), to=str(entry["to"]), time=str(entry["time"]),
                  first_line=line, state=str(entry.get("delivery") or "sent"))
        return text + ("  " + _quote(line) if line else ""), ref
    if kind in ("delivered", "expired", "dropped"):
        sender = senders.get(ident, str(entry.get("from") or ""))
        via = " via %s" % entry["via"] if kind == "delivered" and entry.get("via") else ""
        text = "%s \u2190 %s%s %s%s" % (when, sender, short, kind, via)
        ref = Ref(id=ident, sender=sender, to=str(entry["to"]), time=str(entry["time"]), first_line=line, state=kind)
        return text + ("  " + _quote(line) if line else ""), ref
    entry_ref = Ref(time=str(entry.get("time") or ""), message=False)
    if kind == "kickoff":
        return "%s kickoff" % when, entry_ref
    if kind == "ack":
        outgoing = entry.get("from") == name
        other = entry["to"] if outgoing else entry["from"]
        by = " (by %s%s)" % (entry["by"], ": %s" % entry["reason"] if entry.get("reason") else "") \
            if entry.get("by") else ""
        return "%s ack %s %s%s%s" % (when, "\u2192" if outgoing else "\u2190", other, short, by), entry_ref
    if kind == "interrupt":
        return "%s interrupt (%s)" % (when, entry.get("result", "?")), entry_ref
    return "%s %s" % (when, kind), entry_ref


class View:
    """One view of the stack (design 25.1): a title line, rows, a footer with the view's
    key hints. Up and Down move the selection over the rows that carry a ``ref`` (or scroll
    when none does), PageUp and PageDown a page at a time; Esc, Backspace and ``q`` go back.
    ``load`` reads what the page needs (on open and on each refresh while it is open);
    ``render`` and ``key`` read and change only the view's own state."""

    kind = "view"
    hints = ""
    label_w = LABEL_W
    preselect = False   # pages of messages open with their first entry selected (design 25.8)

    def __init__(self, name: str = ""):
        self.name = name
        self.rows: List[PageRow] = []
        self.top = 0
        self.cursor: Optional[int] = None
        self.height = 1
        self.footer = Footer()

    def title(self) -> str:
        return self.name

    def load(self, ctx, model: Board, cache: Cache, ws: WatcherState) -> None:
        pass

    # -- selection and scrolling --

    def selectable(self) -> List[int]:
        return [i for i, row in enumerate(self.rows) if row.ref is not None]

    def selected(self) -> Optional[Ref]:
        if self.cursor is None or not 0 <= self.cursor < len(self.rows):
            return None
        return self.rows[self.cursor].ref

    def restore(self, previous: Optional[Ref]) -> None:
        """After a reload, keep the selection on the same item when it is still listed; else,
        on a page that preselects, its first entry, so Enter opens it at once."""
        stops = self.selectable()
        self.cursor = stops[0] if stops and self.preselect else None
        if previous is None:
            return
        for index in stops:
            if self.rows[index].ref.key == previous.key:
                self.cursor = index
                return

    def scroll(self, step: int) -> None:
        self.top = min(max(self.top + step, 0), max(0, len(self.rows) - self.height))

    def move(self, step: int) -> None:
        stops = self.selectable()
        if not stops:
            self.scroll(step)
            return
        if self.cursor not in stops:
            self.cursor = stops[0] if step > 0 else stops[-1]
            return
        position = stops.index(self.cursor) + step
        if 0 <= position < len(stops):
            self.cursor = stops[position]
        else:
            self.scroll(step)  # past the last item: show what follows it (the screen lines)

    def page(self, step: int) -> None:
        for _ in range(max(1, self.height)):
            self.move(step)

    def key(self, key) -> Optional[Action]:
        if key in BACK_KEYS:
            return ("back",)
        if key == "up":
            self.move(-1)
        elif key == "down":
            self.move(1)
        elif key == "pageup":
            self.page(-1)
        elif key == "pagedown":
            self.page(1)
        else:
            return self.act(key)
        return None

    def act(self, key) -> Optional[Action]:
        return None

    def click(self, row: int, col: int, width: int, height: int) -> Optional[Action]:
        return ("back",)  # design 25.1: a click anywhere in a page acts as Esc

    # -- drawing --

    def _into_view(self) -> None:
        if self.cursor is not None:
            if self.cursor < self.top:
                self.top = self.cursor
            elif self.cursor >= self.top + self.height:
                self.top = self.cursor - self.height + 1
        self.top = min(max(self.top, 0), max(0, len(self.rows) - self.height))

    def row_line(self, index: int, width: int, colours: bool) -> Line:
        row = self.rows[index]
        selected = index == self.cursor
        line: Line = []
        if self.label_w:
            line.append((fit(row.label, self.label_w), style_of("dim", colours, selected)))
        line.append((fit(clean(row.text), max(0, width - self.label_w)), style_of(row.style, colours, selected)))
        return clip(line, width)

    def render(self, width: int, height: int, colours: bool) -> List[Line]:
        if width < 1 or height < 1:
            return []
        lines: List[Line] = [[(fit(clean(self.title()), width), style_of("bold", colours))]]
        if height == 1:
            return lines
        self.height = max(1, height - 2)
        self._into_view()
        for index in range(self.top, min(len(self.rows), self.top + height - 2)):
            lines.append(self.row_line(index, width, colours))
        while len(lines) < height - 1:
            lines.append(blank(width))
        lines.append(footer_spans(width, self.footer, self.hints, 0, colours))
        return lines


def ring(model: Board) -> List[str]:
    """The seats in file order, then the contacts: what Left and Right step through."""
    return [r.name for r in model.rows] + [c.name for c in model.contacts]


def row_of(model: Board, name: str) -> Optional[Row]:
    for row in model.rows + model.contacts:
        if row.name == name:
            return row
    return None


class _Ringed(View):
    """A page of one seat or contact: Left and Right step to the previous or next one."""

    def __init__(self, name: str):
        super().__init__(name)
        self.names: List[str] = [name]
        self.row: Optional[Row] = None

    def step(self, delta: int) -> Action:
        names = self.names or [self.name]
        index = names.index(self.name) if self.name in names else 0
        return ("replace", names[(index + delta) % len(names)])

    def state_part(self) -> str:
        return state_text(self.row) if self.row is not None else "? unknown"


class SeatPage(_Ringed):
    """Design 25.2: one seat. Sections, each left out when empty: doing (or dialog), queue,
    debts, recent, screen (only after ``p``)."""

    kind = "seat"
    hints = HINTS_SEAT
    preselect = True

    def __init__(self, name: str):
        super().__init__(name)
        self.screen: List[str] = []  # the last `p` read; dropped with the page

    def title(self) -> str:
        row = self.row
        if row is None:
            return "%s%s? not a seat" % (self.name, DOT)
        parts = [row.shown, self.state_part(), row.kind or "?", row.pane or "no pane"]
        if row.tokens is not None:
            parts.append(context_text(row) + (" " + WARN if row.over else ""))
        return DOT.join(parts)

    def load(self, ctx, model: Board, cache: Cache, ws: WatcherState) -> None:
        previous = self.selected()
        self.names = ring(model)
        self.row = row_of(model, self.name)
        repo = ctx.fleet.repo
        rows: List[PageRow] = []

        def section(label: str, items: List[PageRow]) -> None:
            for index, item in enumerate(items):
                item.label = label if index == 0 else ""
                rows.append(item)

        row = self.row
        if row is not None and row.state == "working" and row.doing:
            section("doing", [PageRow("", row.doing, "green")])
        elif row is not None and row.state == "blocked" and row.dialog:
            section("dialog", [PageRow("", line, "red bold") for line in row.dialog])
        now = inbox.now_utc()
        try:
            waiting = [m for m in inbox.read_box(repo, self.name, inbox.NEW) if not inbox.is_due(m, now)]
        except OSError:  # an unreadable mailbox shows nothing
            waiting = []
        section("queue", [PageRow("", "#%s %s from %s %s%s" % (m.short, _hhmm(m.time), m.sender, tags(m),
                                                               _quote(first_line(m.body))),
                                  ref=Ref(id=m.id, sender=m.sender, to=m.to, time=m.time,
                                          first_line=first_line(m.body), state=STATES[m.mode]))
                          for m in waiting])
        debts = [item for item in cache.unsettled_items if self.name in (item.get("to"), item.get("from"))]
        section("debts", [PageRow("", _debt_text(item, self.name),
                                  ref=Ref(id=str(item.get("id") or ""), sender=str(item["from"]), to=str(item["to"]),
                                          time=str(item["since"]), first_line=str(item.get("first_line") or ""),
                                          state="delivered", debt=True, age=_age(item["since"], now)))
                          for item in debts])
        senders = {str(e["id"]): str(e["from"]) for e in cache.entries
                   if e.get("id") and e.get("kind", "send") == "send"}
        mine = [e for e in cache.entries if self.name in (e.get("from"), e.get("to"))
                and e.get("kind", "send") in ("send", "delivered", "expired", "dropped", "ack", "kickoff", "interrupt")]
        recent = []
        for entry in reversed(mine[-RECENT:]):
            text, ref = _recent_text(entry, self.name, senders)
            recent.append(PageRow("", text, ref=ref))
        section("recent", recent)
        section("screen", [PageRow("", line) for line in self.screen])
        self.rows = rows
        self.restore(previous)

    def act(self, key) -> Optional[Action]:
        ref = self.selected()
        if key in ("left", "right"):
            return self.step(-1 if key == "left" else 1)
        if key == "g":
            return ("go", self.name)
        if key == "d":
            return ("deliver", self.name)
        if key == "p":
            return ("screen", self.name)
        if key == "a":
            if ref is not None and ref.debt:
                return ("ack", ref)
            return ("notice", "select a debt with \u2191 and \u2193 first")
        if key == "enter":
            if ref is not None and ref.message:
                return ("body", ref)
            if ref is not None:
                return ("notice", "not a message")
            return ("notice", "select a message with \u2191 and \u2193 first")
        return None


class ContactPage(_Ringed):
    """Design 25.3: a contact's state, age and pane, and the messages it exchanged with this
    fleet, newest first; Enter opens a body. No actions."""

    kind = "contact"
    hints = HINTS_CONTACT
    preselect = True

    def title(self) -> str:
        """``↔ docs-channel · contact of this fleet · ○ idle 1m · w4:pG`` (design 26.6)."""
        pane = self.row.pane if self.row is not None and self.row.pane else "no pane"
        return DOT.join([CONTACT + self.name, "contact of this fleet", self.state_part(), pane])

    def load(self, ctx, model: Board, cache: Cache, ws: WatcherState) -> None:
        previous = self.selected()
        self.names = ring(model)
        self.row = row_of(model, self.name)
        rows = []
        for ref in exchanged(cache.entries, self.name):
            outgoing = ref.sender == self.name
            text = "%s %s %s%s %s  %s" % (_hhmm(ref.time), "\u2192" if outgoing else "\u2190",
                                          ref.to if outgoing else ref.sender, " #%s" % ref.short if ref.id else "",
                                          ref.state, _quote(ref.first_line))
            rows.append(PageRow("" if rows else "messages", text, ref=ref))
        self.rows = rows or [PageRow("messages", "none in this fleet's log")]
        self.restore(previous)

    def act(self, key) -> Optional[Action]:
        if key in ("left", "right"):
            return self.step(-1 if key == "left" else 1)
        if key == "enter":
            ref = self.selected()
            return ("body", ref) if ref is not None else ("notice", "select a message with \u2191 and \u2193 first")
        return None


class OwnerPage(View):
    """Design 25.3: every message to the owner, newest first, delivered or not; Enter opens
    a body, ``a`` acks the selected one."""

    kind = "owner"
    hints = HINTS_OWNER
    label_w = 0
    preselect = True

    def __init__(self) -> None:
        super().__init__(OWNER)
        self.count = 0

    def title(self) -> str:
        return "owner%s%d message%s" % (DOT, self.count, "" if self.count == 1 else "s")

    def load(self, ctx, model: Board, cache: Cache, ws: WatcherState) -> None:
        previous = self.selected()
        try:
            refs = messages_to(ctx.fleet.repo, OWNER, cache.entries)
        except OSError:
            refs = []
        open_ids = {str(item["id"]) for item in cache.unsettled_items if item.get("id") and item["to"] == OWNER}
        now = inbox.now_utc()
        rows = []
        for ref in refs:
            ref.debt = ref.id in open_ids
            ref.age = _age(ref.time, now)
            text = "%s %s from %s %s%s  %s" % (_hhmm(ref.time), "#%s" % ref.short if ref.id else "(no id)", ref.sender,
                                               ref.state, ", owed" if ref.debt else "", _quote(ref.first_line))
            rows.append(PageRow("", text, ref=ref))
        self.count = len(refs)
        self.rows = rows or [PageRow("", "nothing for the owner")]
        self.restore(previous)

    def act(self, key) -> Optional[Action]:
        ref = self.selected()
        if key == "enter":
            return ("body", ref) if ref is not None else ("notice", "select a message with \u2191 and \u2193 first")
        if key == "a":
            if ref is None:
                return ("notice", "select a message with \u2191 and \u2193 first")
            if not ref.debt:
                return ("notice", "#%s: nothing owed (an FYI, answered, or not delivered)" % ref.short
                        if ref.id else "a message without an id: ack it by name (herdr-fleet ack NAME)")
            return ("ack", Ref(id=ref.id, sender=ref.sender, to=OWNER, time=ref.time, first_line=ref.first_line,
                               debt=True, age=ref.age))
        return None


class FleetPage(View):
    """Design 25.3: the fleet file and workspace, the watcher (pid, uptime, lock), its waits,
    holds and retries, the lines of ``status`` the board can say from what it reads, the
    warnings it saw, then the log tail (newest last)."""

    kind = "fleet"
    hints = HINTS_FLEET
    label_w = 0

    def title(self) -> str:
        return "%s%sfleet" % (self.name, DOT)

    def load(self, ctx, model: Board, cache: Cache, ws: WatcherState) -> None:
        fleet = ctx.fleet
        self.name = model.fleet
        try:
            from herdr_fleet.state import load_state
            workspace = load_state(fleet.repo).workspace_id or "not recorded"
        except Exception:  # noqa: BLE001 - an unreadable state file: say so
            workspace = "state file unreadable"
        lines = ["fleet file  %s" % fleet.path, "workspace   %s (%s)" % (fleet.workspace or "(caller's)", workspace),
                 "layout      %s%s" % (fleet.mode, DOT + "top %s" % fleet.top if getattr(fleet, "top", None) else "")]
        watcher = "watcher     pid %s%s%s" % (ws.pid or "?", DOT + "up %s" % short_age(ws.watching)
                                              if ws.watching is not None else "",
                                              DOT + "lock .herdr/watch.lock since %s" % since_label(ws.since)
                                              if ws.since else "")
        lines.append(watcher)
        lines.append("waiting on  %s" % (", ".join(ws.waiting) or "nothing"))
        for name, detail in sorted(ws.holds.items()):
            lines.append("holding     %s: %s" % (name, detail))
        for name, left in sorted(ws.retries.items()):
            if left > 0:
                lines.append("retrying    %s in %d s" % (name, int(round(left))))
        lines.extend(owed_lines(cache.owed_items, hint=True))
        queued = [(r.name, r.queued) for r in model.rows + model.contacts if r.queued]
        if model.owner_count:
            queued.append((OWNER, model.owner_count))
        if queued:
            lines.append("queued: " + ", ".join("%s %d" % item for item in queued))
        for warning in list(fleet.warnings) + list(ws.warnings) + ([model.problems] if model.problems else []):
            lines.append("warning: %s" % warning)
        lines.append("drift, worktrees, hooks and doctor's checks: herdr-fleet status, herdr-fleet doctor")
        lines.append("\u2500\u2500 .herdr/watch.log " + "\u2500" * 20)
        lines.extend(tail_lines(os.path.join(fleet.repo, ".herdr", "watch.log"), FLEET_LOG_LINES))
        at_end = self.top >= max(0, len(self.rows) - self.height) and self.rows
        self.rows = [PageRow("", line) for line in lines]
        if at_end:  # following the log: stay at its end
            self.top = max(0, len(self.rows) - self.height)


class Body(View):
    """Design 25.4: one message's stored body, scrollable, cut to the width line by line; a
    log-only message shows its first line and ``(no stored body)``."""

    kind = "body"
    hints = HINTS_BODY
    label_w = 0

    def __init__(self, ref: Ref):
        super().__init__(ref.id)
        self.ref = ref
        self.head = ""

    def title(self) -> str:
        return self.head

    def load(self, ctx, model: Board, cache: Cache, ws: WatcherState) -> None:
        ref = self.ref
        message = find_message(ctx.fleet.repo, ref)
        known = fates(cache.entries)
        if message is None:
            parts = ["#%s" % ref.short if ref.id else "(no id)", _hhmm(ref.time), "%s \u2192 %s" % (ref.sender, ref.to),
                     known.get(ref.id, ref.state) or "sent"]
            self.head = DOT.join(parts)
            self.rows = [PageRow("", ref.first_line), PageRow("", "(no stored body)", "dim")]
            return
        state = STATES[message.mode] if message.box == inbox.NEW else known.get(message.id, "delivered")
        self.head = DOT.join(["#%s" % message.short, _hhmm(message.time), "%s \u2192 %s" % (message.sender, message.to),
                              state, inbox.size_label(inbox.size_of(message.body))])
        self.rows = [PageRow("", line) for line in message.body.rstrip("\n").split("\n")]


class BoardPage(View):
    """The root view (design 25.1): the board of 24.3. Up and Down select the header or a
    row; Enter and the number keys open pages, ``o`` the owner page; ``g`` focuses the
    selected seat's pane, ``a`` acks its oldest debt, ``d`` delivers to every idle seat."""

    kind = "board"
    hints = HINTS_BOARD

    def __init__(self) -> None:
        super().__init__("board")
        self.model: Board = Board(fleet="")
        self.selection: Optional[str] = None
        self.size = (1, 1)

    def ensure(self) -> None:
        """The board starts with its first seat row selected (design 25.8), so Down then
        Enter opens the second seat; Up from there selects the header."""
        rows = visible(self.model, *self.size)
        if self.selection is None or (self.selection != HEADER and self.selection not in [r.name for r in rows]):
            self.selection = rows[0].name if rows else None  # also when the selected seat left (26.6)

    def render(self, width: int, height: int, colours: bool) -> List[Line]:
        self.size = (width, height)
        self.ensure()
        model = self.model
        model.selected = self.selection
        model.notice, model.event, model.error = self.footer.notice, self.footer.event, self.footer.error
        model.hints = self.hints
        return render(model, width, height, colours)

    def stops(self) -> List[str]:
        return [HEADER] + [r.name for r in visible(self.model, *self.size)]

    def open(self, name: str) -> Action:
        return ("open", "contact" if any(c.name == name for c in self.model.contacts) else "seat", name)

    def key(self, key) -> Optional[Action]:
        self.ensure()
        if key in ("up", "down"):
            stops = self.stops()
            if self.selection in stops:
                index = min(max(stops.index(self.selection) + (1 if key == "down" else -1), 0), len(stops) - 1)
            else:
                index = 1 if key == "down" and len(stops) > 1 else len(stops) - 1
            self.selection = stops[index]
            return None
        return self.act(key)

    def act(self, key) -> Optional[Action]:
        if isinstance(key, str) and len(key) == 1 and key in "123456789":
            rows = visible(self.model, *self.size)
            number = int(key)
            return self.open(rows[number - 1].name) if number <= len(rows) else \
                ("notice", "no seat %d on the board" % number)
        row = row_of(self.model, self.selection) if self.selection and self.selection != HEADER else None
        if key == "enter":
            if self.selection == HEADER:
                return ("open", "fleet", "")
            if row is not None and self.selection in self.stops():
                return self.open(row.name)
            return ("notice", "select a seat with \u2191 and \u2193 first")
        if key == "o":
            return ("open", "owner", "")
        if key == "g":
            if row is None or row.contact:
                return ("notice", "select a seat with \u2191 and \u2193 first")
            return ("go", row.name)
        if key == "a":
            if row is None:
                return ("notice", "select a seat with \u2191 and \u2193 first")
            if not row.debt_to:
                return ("notice", "%s owes nothing" % row.name)
            if not row.debt_id:
                return ("notice", "%s's oldest debt (to %s) has no id; %s can ack it: herdr-fleet ack %s"
                        % (row.name, row.debt_to, row.name, row.debt_to))
            return ("ack", Ref(id=row.debt_id, sender=row.debt_to, to=row.name, debt=True, age=row.debt_age))
        if key == "d":
            return ("deliver_all",)
        if key == "q":
            return ("notice", "Ctrl-C stops the watcher")
        return None

    def click(self, row: int, col: int, width: int, height: int) -> Optional[Action]:
        hit = hit_test(self.model, width, height, row, col)
        if hit is None or hit[0] == "footer":
            return None
        if hit[0] == "owner":
            return ("open", "owner", "")
        if hit[0] == "fleet":
            return ("open", "fleet", "")
        self.selection = hit[1]
        return self.open(hit[1])


def hit_test(model: Board, width: int, height: int, row: int, col: int) -> Optional[Tuple[str, ...]]:
    """What a click at ``(row, col)`` of the board lands on: ``("owner",)`` or ``("fleet",)``
    on the header, ``("row", name)``, ``("footer",)``, or None (design 25.1)."""
    mode, rows, _ = plan(model, width, height)
    if mode == "single":
        return ("row", rows[0].name) if rows and row == 0 else None
    if row == 0:
        _, _, (start, end) = header_layout(model, width)
        return ("owner",) if start <= col < end else ("fleet",)
    if row == height - 1:
        return ("footer",)
    index = row - 1
    if mode == "packed":
        index = index * 2 + (1 if col >= width // 2 else 0)
    if 0 <= index < len(rows):
        return ("row", rows[index].name)
    return None
