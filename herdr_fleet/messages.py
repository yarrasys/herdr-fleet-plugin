"""Messages (design 16.1 to 16.3, 17.1, 18.2 to 18.4, 18.8): ``send``, ``ack``, the
message log, owed replies, and the delivery engine that ``send``, kickoffs and
``deliver`` share.

herdr-fleet is a messenger only: it never reads or interprets an agent's
output, never routes replies and never decides who gets a message.

- ``send`` writes every message to the target's mailbox first (``inbox``), then
  follows the table of design 18.3 as 21.1 amends it: an idle or done seat gets
  it now, and anything else, a working seat included, leaves it queued for the
  seat's next idle or done. ``--queue`` always queues; ``--now`` types it ahead
  to a working seat (the only prompt besides ``deliver NAME --now`` that may
  reach a ``working`` agent) and restores the 16.1 refusals; an FYI never wakes
  an idle seat. Any delivery carries the queued messages in front (the batch of
  18.4), so order is never inverted. When the owner is typing in the target
  pane (the presence guard, design 21.2) nothing is sent: a plain send queues,
  ``--now`` refuses.
- ``prompt_mailbox`` is the engine: under the mailbox lock it expires messages
  past their ttl, renders the batch (or a pointer above 16 KiB), prompts it
  through ``seats.safe_prompt``, and on a sent or unconfirmed prompt moves the
  files to ``cur/`` and logs one ``delivered`` entry per message. A refused
  prompt leaves everything queued. ``notify_owner`` is the same for ``owner``,
  through ``herdr notification show``.
- The log is ``<repo>/.herdr/messages.jsonl``, appended with ``O_APPEND``:
  ``send`` (``delivery`` sent, unconfirmed, queued or refused), ``delivered``,
  ``expired``, ``dropped``, ``ack`` and ``kickoff`` entries.
- Owed replies are derived from the log alone (``owed``): a message counts from
  its delivery, FYIs never create an obligation, a plain answer settles the
  pair, ``--reply-to`` or ``ack #id`` settles one message exactly, and a kickoff
  of a seat settles everything owed by it and to it before (design 20.8).
  ``ack`` writes the log only and sends nothing (invariant 10); acting on a
  message is never inferred from anything an agent prints (invariant 16).
- How a queued message waits is decided by how it was sent (``queued_by``,
  invariant 18): a plain send a seat could not take is ``refusal``, ``--queue``
  and ``--when-ready`` leftovers are ``queue``, FYIs ride along. Since 0.10.0
  ``refusal`` and ``queue`` messages are delivered alike, at the seat's next idle
  or done, by every path (design 21.1); a ``refusal`` message still goes along
  with a ``--queue`` or FYI send to an idle or done seat (the flush before send,
  20.9). ``--urgent`` reorders only the mailbox herdr-fleet holds (invariant 17).
- A send to a live agent of another fleet on this machine is also logged there
  (``crossfleet``).
- Design 23: ``send`` prints the note about the sender's own unread messages
  first and its per-target lines last; the caller is ``caller_record`` (the
  pane's herdr name, else the one contact matching that agent, else a usage
  error; ``owner`` only without an agent or outside herdr); ``ack #id --reason``
  closes a debt whoever owes it; ``interrupt`` entries are logged here too.
"""

from __future__ import annotations

import datetime
import json
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

from herdr_fleet import contacts as contact_mod
from herdr_fleet import crossfleet, inbox, presence, seen
from herdr_fleet.context import EXIT_FAIL, EXIT_OK, EXIT_PARTIAL, Ctx, UsageError, preflight
from herdr_fleet.fleetfile import NAME_RE
from herdr_fleet.herdr import HerdrError
from herdr_fleet.inbox import CUR, NEW, Message, size_label
from herdr_fleet.kickoff import INBOX_HEADING
from herdr_fleet.notify import notify
from herdr_fleet.seats import SEND_ALLOWED, SETTLED, at_shell_prompt, safe_prompt, state_words
from herdr_fleet.state import ensure_gitignore, mark_kicked_off, now_iso

LOG_NAME = "messages.jsonl"
FIRST_LINE_MAX = 120
PLUGIN_SENDER = "herdr-fleet"
OWNER = "owner"
PREFIX_RE = re.compile(r"^\[from [^\]]*\]\s*")  # [from X], [from X #3f2a9c], [from X at 10:09 #3f2a9c re #91be02]
FYI_RE = re.compile(r"^fyi[: ]", re.IGNORECASE)
ANSWERED = ("sent", "unconfirmed", "queued")  # a send in these states answers (refused never does)
since_label = inbox.time_label
WORKING_PLACE = "working: typed into its own input, read after its current turn"  # design 20.3
FYI_PLACE = "fyi: delivered with the next plain send, kickoff or deliver"         # design 20.8
URGENT_TYPED = "--urgent cannot reorder the seat's own input"                      # design 20.3
HOLD = "would hold: %s"                     # design 21.2: a dry run when the presence guard would hold
LANDS = "; lands at its next idle, or now with --now"  # design 23.1: a plain send queued to a working seat
BLOCKED_TAIL = ": a dialog is open"  # design 23.5, after `blocked after --when-ready 5000 ms`


def place(state: str) -> str:
    """The state as a send or deliver line shows it: a working seat says where the text
    sits (in the seat's own input, which herdr-fleet cannot see or reorder)."""
    return WORKING_PLACE if state == "working" else state


# -- the log -----------------------------------------------------------------------------


def log_path(repo: str) -> str:
    return os.path.join(repo, ".herdr", LOG_NAME)


def first_line(text: str) -> str:
    """The first non-empty line after the ``[from ...]`` prefix, at most 120 characters."""
    for line in PREFIX_RE.sub("", text.strip(), count=1).splitlines():
        if line.strip():
            return line.strip()[:FIRST_LINE_MAX]
    return ""


def is_fyi(text: str) -> bool:
    """Design 17.1: after the ``[from X]`` prefix and leading whitespace, the text
    starts with ``FYI:`` or ``FYI `` (any case)."""
    return bool(FYI_RE.match(PREFIX_RE.sub("", text.lstrip(), count=1).lstrip()))


def make_entry(sender: str, to: str, kind: str, text: str, file: Optional[str] = None,
               time: Optional[str] = None, delivery: str = "sent", fyi: bool = False) -> Dict:
    entry = {"time": time or now_iso(), "from": sender, "to": to, "kind": kind,
             "bytes": len(text.encode("utf-8")), "first_line": first_line(text), "delivery": delivery}
    if file:
        entry["file"] = file
    if fyi:
        entry["fyi"] = True
    return entry


def send_entry(message: Message, delivery: str, file: Optional[str] = None) -> Dict:
    """Design 18.2: a ``send`` entry. ``bytes`` is the message on its own (``[from X #id] body``)."""
    entry = {"time": now_iso(), "from": message.sender, "to": message.to, "kind": "send", "id": message.id,
             "bytes": inbox.size_of(inbox.direct_text(message)), "first_line": first_line(message.body),
             "delivery": delivery}
    if file:
        entry["file"] = file
    if message.fyi:
        entry["fyi"] = True
    if message.reply_to:
        entry["reply_to"] = message.reply_to
    entry.update(timer_metadata(message))
    entry.update(route_metadata(message))
    return entry


def timer_metadata(message: Message) -> Dict:
    return {"timer": message.timer, "scheduled_for": message.scheduled_for} if message.timer else {}


def route_metadata(message: Message) -> Dict:
    return {"origin_fleet": message.origin_fleet, "destination_fleet": message.destination_fleet,
            "requested_to": message.requested_to} if message.origin_fleet else {}


def delivered_entry(message: Message, via: str, delivery: str = "sent") -> Dict:
    return {"time": now_iso(), "from": PLUGIN_SENDER, "to": message.to, "kind": "delivered", "id": message.id,
            "via": via, "bytes": 0, "first_line": first_line(message.body), "delivery": delivery,
            **timer_metadata(message), **route_metadata(message)}


def disposal_entry(kind: str, message: Message, via: str) -> Dict:
    """``expired`` (via ``ttl``) or ``dropped`` (via ``drop-inbox``): moved to ``cur/`` undelivered."""
    return {"time": now_iso(), "from": PLUGIN_SENDER, "to": message.to, "kind": kind, "id": message.id,
            "via": via, "bytes": 0, "first_line": first_line(message.body), "delivery": "none",
            **timer_metadata(message), **route_metadata(message)}


def ack_entry(caller: str, seat: str, time: Optional[str] = None, ident: Optional[str] = None,
              by: Optional[str] = None, reason: Optional[str] = None) -> Dict:
    """Design 17.1: one hand-made acknowledgement; nothing is sent to anyone. With ``ident``
    it settles that one message (18.8). Design 23.4: ``ack #id --reason TEXT`` closes a debt
    whoever owes it; ``caller`` is then the debtor, ``by`` who closed it and ``reason`` why."""
    entry = {"time": time or now_iso(), "from": caller, "to": seat, "kind": "ack", "bytes": 0,
             "first_line": "ack", "delivery": "logged"}
    if ident:
        entry["id"] = ident
    if by:
        entry["by"] = by
    if reason is not None:
        entry["reason"] = reason
    return entry


def interrupt_entry(caller: str, seat: str, keys: Sequence[str], result: str) -> Dict:
    """Design 23.7: ``interrupt NAME`` pressed ``keys`` in a working seat; ``result`` is the
    state it reached (or ``failed``). Not a message: it neither asks nor answers."""
    return {"time": now_iso(), "from": caller, "to": seat, "kind": "interrupt", "keys": list(keys),
            "result": result}


def append(repo: str, entry: Dict) -> None:
    """Append one line with O_APPEND, so concurrent writers never interleave lines."""
    folder = os.path.join(repo, ".herdr")
    os.makedirs(folder, exist_ok=True)
    ensure_gitignore(folder)
    line = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(log_path(repo), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def record(ctx: Ctx, entry: Dict) -> None:
    """Append, turning a failed write into a warning: the message itself already moved."""
    try:
        crossfleet.local_source(ctx, entry)
    except (OSError, ValueError, UsageError) as exc:
        ctx.warn("cannot repair route send receipt: %s" % exc)
    try:
        append(ctx.fleet.repo, entry)
    except OSError as exc:
        ctx.warn("cannot append to %s: %s" % (log_path(ctx.fleet.repo), exc))
    try:
        crossfleet.terminal(ctx, entry)
    except (OSError, ValueError, UsageError) as exc:
        ctx.warn("cannot mirror route receipt: %s; no message will be re-prompted" % exc)


def read(repo: str, warnings: Optional[List[str]] = None) -> List[Dict]:
    """Every entry, oldest first. Corrupt lines are skipped with a warning."""
    path = log_path(repo)
    if not os.path.exists(path):
        return []
    entries = []
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                entry = None
            if not isinstance(entry, dict) or not all(k in entry for k in ("time", "from", "to")):
                if warnings is not None:
                    warnings.append("%s:%d: corrupt line skipped" % (path, number))
                continue
            entries.append(entry)
    return entries


def log_kickoff(repo: str, to: str, text: str, reason: Optional[str] = None) -> Optional[str]:
    """Record a kickoff (with its ``reason``: up, restart, swap, kickoff); returns a warning
    instead of failing the kickoff."""
    entry = make_entry(PLUGIN_SENDER, to, "kickoff", text)
    if reason:
        entry["reason"] = reason
    try:
        append(repo, entry)
    except OSError as exc:
        return "cannot append to %s: %s" % (log_path(repo), exc)
    return None


# -- owed replies -------------------------------------------------------------------------

Key = Tuple[datetime.datetime, int]


def _when(entry: Dict) -> Optional[datetime.datetime]:
    return inbox.parse_time(entry.get("time"))


def unsettled(entries: Sequence[Dict]) -> List[Tuple[Key, Dict]]:
    """Every delivered request still awaiting an answer, oldest delivery first, as
    (key, {from, to, since, first_line[, id]}).

    A ``send`` that is not FYI is a request from the time of its delivery: its own
    entry when ``delivery`` is sent or unconfirmed, else its ``delivered`` entry
    (still queued, expired, dropped or refused: no request). A request is settled
    by a later answer from its recipient to its sender (a ``send`` that was not
    refused, FYI or not, or an ``ack``), or by a ``send`` whose ``reply_to`` or an
    ``ack`` whose ``id`` names it. An answer that names one message settles only
    that one. A ``kickoff`` entry for seat S settles every request owed by S and to
    S delivered before it (design 20.8): the fresh context neither remembers what it
    asked nor what it was asked. Messages the kickoff itself carried in ``{inbox}``
    count from the kickoff and stay owed. Plugin entries never ask or answer. Times
    have one-second resolution, so equal times are ordered by position in the log."""
    pending: Dict[str, Dict] = {}
    asks: List[Tuple[Key, Dict]] = []
    answers: Dict[Tuple[str, str], Key] = {}
    named: Set[str] = set()
    kickoffs: Dict[str, Key] = {}
    carried: Dict[str, List[Tuple[Dict, str, Key]]] = {}  # seat -> requests its next kickoff entry carries
    # A timer's atomic file can be consumed after a crash before its send is
    # recovered. Join these sources by id even when the send was appended later;
    # unrelated message ordering keeps its existing semantics.
    recoverable_sources = {str(e["id"]): e for e in entries if e.get("kind") == "send" and (e.get("timer") or e.get("origin_fleet"))
                     and e.get("id") and e.get("delivery") == "queued"}
    recoverable_delivered: Set[str] = set()
    delivery_order = {}
    for index, entry in enumerate(entries):
        if entry.get("kind") == "delivered" and str(entry.get("id")) in recoverable_sources:
            delivery_order.setdefault(str(entry["id"]), index)

    def ask(source: Dict, key: Key, since: str) -> None:
        if source.get("fyi"):
            return
        item = {"from": str(source["from"]), "to": str(source["to"]), "since": since,
                "first_line": source.get("first_line", "")}
        if source.get("id"):
            item["id"] = str(source["id"])
        asks.append((key, item))

    for index, entry in enumerate(entries):
        when = _when(entry)
        if when is None:
            continue
        key: Key = (when, index * 2)
        kind = entry.get("kind", "send")
        pair = (str(entry["from"]), str(entry["to"]))
        if kind == "send":
            delivery = entry.get("delivery") or "sent"
            if (entry.get("timer") or entry.get("origin_fleet")) and delivery == "queued":
                # Recovered sends retain their original creation time and logical
                # log position. Equal-second recovery must not answer a newer ask
                # to owner merely because the source was appended after delivery.
                order = entry.get("timer_enqueue_order") if entry.get("timer") else entry.get("route_enqueue_order")
                order = order if type(order) is int and order >= 0 else index
                order = min(order, index, delivery_order.get(str(entry.get("id")), index))
                key = (when, order * 2 - 1)
            if delivery in ANSWERED:
                if entry.get("reply_to"):
                    named.add(str(entry["reply_to"]))
                elif pair not in answers or key > answers[pair]:
                    answers[pair] = key
            if delivery in ("sent", "unconfirmed"):
                ask(entry, key, str(entry["time"]))
            elif delivery == "queued" and entry.get("id"):
                pending[str(entry["id"])] = entry
        elif kind == "delivered":
            ident = str(entry.get("id"))
            source = pending.pop(ident, None) or recoverable_sources.get(ident)
            if source is not None and (source.get("timer") or source.get("origin_fleet")):
                if ident in recoverable_delivered:
                    continue
                recoverable_delivered.add(ident)
            if source is not None:
                if entry.get("via") == "kickoff":
                    carried.setdefault(str(entry["to"]), []).append((source, str(entry["time"]), key))
                else:
                    ask(source, key, str(entry["time"]))
        elif kind == "ack":
            if entry.get("id"):
                named.add(str(entry["id"]))
            elif pair not in answers or key > answers[pair]:
                answers[pair] = key
        elif kind == "kickoff":
            seat = str(entry["to"])
            kickoffs[seat] = key
            for source, since, _ in carried.pop(seat, []):  # carried in this kickoff: count from it
                ask(source, key, since)
    for items in carried.values():  # a kickoff that was not logged (unconfirmed): from their delivery
        for source, since, own_key in items:
            ask(source, own_key, since)
    still = []
    for key, item in asks:
        reply = answers.get((item["to"], item["from"]))
        if item.get("id") in named or (reply is not None and reply > key):
            continue
        if any(kickoffs.get(seat) is not None and kickoffs[seat] > key for seat in (item["to"], item["from"])):
            continue
        still.append((key, item))
    return sorted(still, key=lambda pair: pair[0])


def owed(entries: Sequence[Dict]) -> List[Dict]:
    """Design 16.3 as amended by 17.1 and 18.8: for each ordered pair (A, B), the latest
    unsettled request from A to B is awaiting. Returns them oldest first, as
    {from, to, since, first_line} plus ``id`` when the message has one."""
    latest: Dict[Tuple[str, str], Tuple[Key, Dict]] = {}
    for key, item in unsettled(entries):
        latest[(item["from"], item["to"])] = (key, item)
    return [item for _, item in sorted(latest.values(), key=lambda pair: pair[0])]


def owed_lines(items: Sequence[Dict], today: Optional[datetime.date] = None,
               now: Optional[datetime.datetime] = None, hint: bool = False) -> List[str]:
    """``awaiting: B owes A for 5h12m (#a1b2c3, since 08:49): first line`` (design 20.2);
    ``(no id, since …)`` for a message sent before 0.6.0, acknowledged by name. With
    ``hint`` (``status``, design 23.4) each line ends ``(ack A, or ack #a1b2c3)``."""
    now = now or inbox.now_utc()
    lines = []
    for o in items:
        ident = "#%s" % inbox.short_of(o["id"]) if o.get("id") else "no id"
        age = inbox.age_since(o["since"], now) or "?"
        line = "awaiting: %s owes %s for %s (%s, since %s): %s" % (
            o["to"], o["from"], age, ident, since_label(o["since"], today), o["first_line"])
        if hint:
            line += " (ack %s%s)" % (o["from"], ", or ack %s" % ident if o.get("id") else "")
        lines.append(line)
    return lines


def format_entry(entry: Dict) -> str:
    """``time  from → to  bytes  first_line``. After the bytes: ``?`` for an unconfirmed
    delivery; then ``#id``, ``[queued]`` or ``[refused]`` and ``[fyi]`` before the first
    line. Plugin entries read ``time  herdr-fleet → to  delivered #id via send  first_line``
    (``expired``, ``dropped`` alike), an acknowledgement ``time  from → to  ack [#id]`` (a
    debt closed with ``--reason`` adds ``acked by NAME: reason``, design 23.4) and an
    interrupt ``time  from  interrupt → to (done)`` (23.7)."""
    kind = entry.get("kind", "send")
    ident = "#%s" % inbox.short_of(str(entry["id"])) if entry.get("id") else ""
    if kind == "interrupt":  # design 23.7: `time  caller  interrupt → sample-dev (done)`
        return "%s  %s  interrupt → %s (%s)" % (entry["time"], entry["from"], entry["to"], entry.get("result", "?"))
    head = "%s  %s → %s" % (entry["time"], entry["from"], entry["to"])
    if kind == "ack":
        closed = ""
        if entry.get("by"):  # design 23.4: `acked by partner-workspace: superseded`
            closed = "  acked by %s%s" % (entry["by"], ": %s" % entry["reason"] if entry.get("reason") else "")
        return "%s  ack%s%s" % (head, " " + ident if ident else "", closed)
    if kind in ("delivered", "expired", "dropped"):
        mark = "?" if entry.get("delivery") == "unconfirmed" else ""
        via = " via %s" % entry["via"] if kind == "delivered" and entry.get("via") else ""
        return "%s  %s%s%s%s  %s" % (head, kind, mark, " " + ident if ident else "", via,
                                     entry.get("first_line", ""))
    unconfirmed = "?" if entry.get("delivery") == "unconfirmed" else ""
    tags = [ident] if ident else []
    if entry.get("delivery") in ("queued", "refused"):
        tags.append("[%s]" % entry["delivery"])
    if entry.get("fyi"):
        tags.append("[fyi]")
    return "%s  %s%s  %s%s" % (head, entry.get("bytes", "?"), unconfirmed, "".join(t + "  " for t in tags),
                               entry.get("first_line", ""))


# -- the delivery engine --------------------------------------------------------------------


@dataclass
class Drop:
    """What one delivery attempt to a mailbox did."""

    status: str                  # sent | unconfirmed | pointer | refused | typing | failed | empty
    detail: str = ""             # for typing: ``owner typing in <pane>`` (design 21.2)
    state: str = ""              # the state safe_prompt saw
    carried: List[Message] = field(default_factory=list)  # in the prompt, now in cur/
    waiting: List[Message] = field(default_factory=list)  # still in new/
    size: int = 0                # bytes of the prompt (of the batch, for a pointer)
    agent: Dict = field(default_factory=dict)  # the agent record safe_prompt read (pane_id, kind)


def expire_due(ctx: Ctx, name: str, now: Optional[datetime.datetime] = None) -> List[Message]:
    """Move queued messages past their ttl to ``cur/``, each logged ``expired`` (18.4)."""
    now = now or inbox.now_utc()
    gone = []
    for message in queued_in(ctx, name):
        if inbox.is_due(message, now):
            try:
                inbox.move(ctx.fleet.repo, message, CUR)
            except OSError as exc:
                ctx.warn("cannot expire %s: %s" % (message.path, exc))
                continue
            record(ctx, disposal_entry("expired", message, "ttl"))
            gone.append(message)
    return gone


def drop_queued(ctx: Ctx, name: str) -> int:
    """``--drop-inbox``: move every queued message of ``name`` to ``cur/``, logged ``dropped``."""
    count = 0
    if not inbox.has_queued(ctx.fleet.repo, name):
        return count
    with inbox.locked(ctx.fleet.repo, name):
        for message in queued_in(ctx, name):
            inbox.move(ctx.fleet.repo, message, CUR)
            record(ctx, disposal_entry("dropped", message, "drop-inbox"))
            count += 1
    return count


def inbox_block(ctx: Ctx, name: str) -> Tuple[str, List[Message], List[Message]]:
    """``{inbox}`` for a kickoff (design 18.5): after expiring what is past its ttl, the
    heading and the batch of what is queued, or "" when nothing is. Above BATCH_CAP the
    heading and the pointer instead: nothing is carried and everything stays queued.
    Returns (block, carried, left queued). The caller holds ``inbox.locked``."""
    expire_due(ctx, name)
    waiting = [m for m in queued_in(ctx, name) if not m.is_kickoff]  # 32.6: a queued kickoff is replaced
    if not waiting:
        return "", [], []
    block = INBOX_HEADING + "\n" + inbox.render_batch(waiting)
    size = inbox.size_of(block)
    if size > inbox.BATCH_CAP:
        return INBOX_HEADING + "\n" + inbox.pointer_text(waiting, size, prefix=False), [], waiting
    return block, waiting, []


def compose_prompt(queued: Sequence[Message], own: Optional[Message] = None) -> Tuple[str, int, bool]:
    """(prompt, size, pointer). A message alone is ``[from X #id] body``; with anything
    queued, the batch of 18.4 in delivery order (urgent first, then oldest first, so
    ``own`` is last unless it is urgent). Above BATCH_CAP the prompt is the pointer.
    Design 32.6: a queued kickoff goes first and verbatim, the rest after it as usual;
    above BATCH_CAP the kickoff still goes, and the pointer stands for the rest."""
    items = inbox.in_delivery_order(list(queued) + ([own] if own is not None else []))
    if items and items[0].is_kickoff:
        head, rest = items[0], items[1:]
        if not rest:
            return head.body, inbox.size_of(head.body), False
        others = [m for m in queued if not m.is_kickoff]
        tail = inbox.direct_text(own) if own is not None and not others else inbox.render_batch(rest)
        text = head.body.rstrip("\n") + "\n\n" + tail
        size = inbox.size_of(text)
        if size > inbox.BATCH_CAP:
            return head.body.rstrip("\n") + "\n\n" + inbox.pointer_text(rest, inbox.size_of(tail)), size, True
        return text, size, False
    text = inbox.direct_text(own) if own is not None and not queued else inbox.render_batch(items)
    size = inbox.size_of(text)
    if size > inbox.BATCH_CAP:
        return inbox.pointer_text(items, size), size, True
    return text, size, False


def settle(ctx: Ctx, messages: Sequence[Message], via: str, delivery: str,
           own: Optional[Message] = None, mirror: Optional[str] = None) -> None:
    """After a sent or unconfirmed prompt: move every carried file to ``cur/`` and log one
    ``delivered`` entry each (``own``, a message ``send`` just wrote, logs its own entry).
    ``mirror`` is another fleet's repository that logged these messages' sends (20.8)."""
    for message in messages:
        try:
            inbox.move(ctx.fleet.repo, message, CUR)
        except OSError as exc:
            ctx.warn("delivered, but cannot move %s to cur/: %s" % (message.path, exc))
        if message.is_kickoff and delivery == "sent":  # first in the batch, so its entry precedes the rest's
            kickoff_delivered(ctx, message)
        if own is None or message.id != own.id:
            entry = delivered_entry(message, via, delivery)
            record(ctx, entry)
            if mirror:
                mirror_entry(ctx, mirror, entry)


def prompt_mailbox(ctx: Ctx, name: str, target: str, allow: Sequence[str], via: str,
                   own: Optional[Message] = None, exclude: Sequence[str] = (),
                   mirror: Optional[str] = None) -> Drop:
    """Deliver a mailbox in one prompt (design 18.4). The caller holds ``inbox.locked``.
    ``target`` is what ``agent prompt`` gets (a name or a pane id); ``allow`` the states
    ``safe_prompt`` accepts; ``own`` the message ``send`` just queued; ``exclude`` ids
    that stay queued (a ``--queue`` message while older refusal messages go). A queued
    kickoff (32.6) goes first, and only where ``allow`` is idle or done: ``send --now`` and
    ``deliver NAME --now`` leave it queued."""
    expire_due(ctx, name)
    typed_ahead = "working" in allow
    queued = [m for m in queued_in(ctx, name) if (own is None or m.id != own.id) and m.id not in exclude
              and not (typed_ahead and m.is_kickoff)]
    items = inbox.in_delivery_order(queued + ([own] if own is not None else []))
    if not items:
        return Drop("empty")
    text, size, pointer = compose_prompt(queued, own)
    outcome = safe_prompt(ctx.herdr, target, text, allow)
    state = str(outcome.agent.get("agent_status") or "")
    if outcome.status == "typing":  # design 21.2: the owner is typing there; nothing sent
        return Drop("typing", outcome.detail, state, [], items, size, outcome.agent)
    if pointer and (outcome.ok or outcome.status == "unconfirmed"):
        head = items[:1] if items[0].is_kickoff else []  # 32.6: the kickoff went; the pointer is for the rest
        if head:
            settle(ctx, head, via, "sent" if outcome.ok else "unconfirmed", None, mirror)
        return Drop("pointer", outcome.detail, state, head, items[len(head):], size)
    if outcome.ok or outcome.status == "unconfirmed":
        delivery = "sent" if outcome.ok else "unconfirmed"
        settle(ctx, items, via, delivery, own, mirror)
        return Drop(delivery, outcome.detail, state, items, [], size)
    if outcome.status == "error":
        return Drop("failed", outcome.detail, state, [], items, size)
    return Drop("refused", outcome.detail, outcome.status, [], items, size)  # herdr sent nothing


def kickoff_delivered(ctx: Ctx, message: Message) -> None:
    """Design 32.6: a queued kickoff reached its seat, so do what a sent kickoff does: its
    seat's ``last_kickoff_at`` and ``blocked_at_startup = false`` (only those fields, read
    fresh under the state lock, so a long-running watcher never writes back a stale state
    file) and a ``kickoff`` log entry with its original reason, which settles the debts of
    20.8. The caller logs the batch's ``delivered`` entries after it."""
    when = now_iso()
    try:
        mark_kicked_off(ctx.fleet.repo, message.to, when)
    except OSError as exc:
        ctx.warn("cannot record the kickoff of %s in the state file: %s" % (message.to, exc))
    loaded = getattr(ctx, "_state", None)
    entry = loaded.entry(message.to) if loaded is not None else None
    if entry is not None:  # keep a state this command may still save consistent with the file
        entry.update(last_kickoff_at=when, blocked_at_startup=False)
    problem = log_kickoff(ctx.fleet.repo, message.to, message.body, message.kickoff_reason)
    if problem:
        ctx.warn(problem)


def drop_kickoffs(ctx: Ctx, name: str) -> List[Message]:
    """Move the seat's queued kickoffs to ``cur/``, each logged ``dropped`` via ``kickoff``: a
    newer kickoff replaces them (32.6). The caller holds ``inbox.locked``."""
    dropped = []
    for message in queued_in(ctx, name):
        if message.is_kickoff:
            inbox.move(ctx.fleet.repo, message, CUR)
            record(ctx, disposal_entry("dropped", message, "kickoff"))
            dropped.append(message)
    return dropped


def queue_kickoff(ctx: Ctx, name: str, text: str, reason: str) -> Message:
    """Design 32.6: write a kickoff that could not be sent into the seat's mailbox, from
    ``herdr-fleet`` with ``queued_by: kickoff`` (delivered ahead of everything queued),
    replacing any older one. One per seat; no ttl. The caller holds ``inbox.locked``."""
    drop_kickoffs(ctx, name)
    now = inbox.now_utc()
    message = Message(id=inbox.new_id(now, inbox.taken_shorts(ctx.fleet.repo)), sender=PLUGIN_SENDER, to=name,
                      time=inbox.iso_local(now), body=text, queued_by="kickoff", kickoff_reason=reason)
    return inbox.write(ctx.fleet.repo, message)


def queued_kickoff(repo: str, name: str) -> Optional[Message]:
    """The seat's queued kickoff, or None (read-only; for ``status``)."""
    if not inbox.has_queued(repo, name):
        return None
    return next((m for m in inbox.read_box(repo, name, NEW) if m.is_kickoff), None)


def has_refusal(messages: Sequence[Message], exclude: Sequence[str] = ()) -> bool:
    """True when a queued message was queued on refusal (design 20.8): a ``--queue`` or
    FYI send to an idle or done seat takes it along (20.9; never to a working seat since
    21.1). Decided by ``queued_by`` only."""
    return any(m.mode == "refusal" and m.id not in exclude for m in messages)


def refusal_waiting(ctx: Ctx, name: str, exclude: Sequence[str] = ()) -> bool:
    """``has_refusal`` for the live (not past ttl) queued messages of a mailbox."""
    if not inbox.has_queued(ctx.fleet.repo, name):
        return False
    now = inbox.now_utc()
    return has_refusal([m for m in queued_in(ctx, name) if not inbox.is_due(m, now)], exclude)


def notify_owner(ctx: Ctx) -> Drop:
    """Deliver the ``owner`` mailbox as one ``herdr notification show`` (design 18.3),
    logged ``delivered`` via ``notify``. Outside herdr nothing is shown and the messages
    stay queued. The caller holds ``inbox.locked``."""
    expire_due(ctx, OWNER)
    items = queued_in(ctx, OWNER)
    if not items:
        return Drop("empty")
    if not ctx.inside_herdr():
        return Drop("refused", "not in herdr", "", [], items)
    if len(items) == 1:
        title = "message from %s #%s" % (items[0].sender, items[0].short)
        body = first_line(items[0].body)
    else:
        oldest = min(items, key=Message.sort_key)
        title = "%d messages for owner (oldest %s)" % (len(items), since_label(oldest.time))
        body = "\n".join("#%s %s: %s" % (m.short, m.sender, first_line(m.body)) for m in items)
    senders = sorted({m.sender for m in items})
    body += "\nread: herdr-fleet inbox --as owner --history %s" % (senders[0] if len(senders) == 1 else "<name>")
    if not notify(ctx.herdr, title, body, "request", errors=ctx.stderr):
        return Drop("failed", "notification failed", "", [], items)
    settle(ctx, items, "notify", "sent")
    return Drop("sent", "", "notification", items, [], sum(inbox.size_of(inbox.direct_text(m)) for m in items))


def queued_in(ctx: Ctx, name: str) -> List[Message]:
    """The ``new/`` messages of a mailbox in delivery order; unreadable files are warned about."""
    warnings: List[str] = []
    found = inbox.read_box(ctx.fleet.repo, name, NEW, warnings)
    for warning in warnings:
        ctx.warn(warning)
    return found


def position(ctx: Ctx, name: str, ident: str) -> Tuple[int, int]:
    """Where a queued message sits in its mailbox's delivery order: (1-based position,
    count), counting messages not past their ttl; (0, count) when it is not there."""
    now = inbox.now_utc()
    waiting = [m for m in inbox.read_box(ctx.fleet.repo, name, NEW) if not inbox.is_due(m, now)]
    ids = [m.id for m in waiting]
    return (ids.index(ident) + 1 if ident in ids else 0), len(ids)


def held_label(drop: Drop, state: str) -> str:
    """``idle, owner typing in w1:p2``: the state a send line shows when the presence guard
    held the message (design 21.2)."""
    return "%s, %s" % (drop.state or state, drop.detail)


def dry_hold(ctx: Ctx, target: Optional[str]) -> Optional[str]:
    """For a dry run (design 21.2): would the presence guard hold text for ``target`` now?
    The guard only reads; None when it would pass or herdr cannot say."""
    return presence.would_hold(ctx.herdr, target)


def mirror_entry(ctx: Ctx, repo: str, entry: Dict) -> None:
    """Append ``entry`` to another fleet's log too (design 20.8); a failure is a warning."""
    try:
        crossfleet.mirror_entry(repo, entry)
    except OSError as exc:
        ctx.warn("cannot log in the other fleet at %s: %s" % (repo, exc))


# -- send -----------------------------------------------------------------------------------


@dataclass
class Target:
    name: str                    # as given: seat, contact, owner or live agent name; also the mailbox
    herdr: Optional[str]         # what `agent prompt` gets: a name or a pane id
    problem: Optional[str] = None
    kind: str = "seat"           # seat | contact | agent | owner
    other: Optional[str] = None  # a live agent of another fleet: that fleet's repository (design 20.8)
    destination: Optional[object] = None  # validated destination fleet for a new route
    requested: Optional[str] = None
    route_reason: Optional[str] = None


def message_text(words: Sequence[str], text_file: Optional[str], all_seats: bool = False) -> Tuple[List[str], str]:
    """(targets, text) from the positional items and --file (design 16.1): with --file
    every item is a target; otherwise the last item is the text, so at least two items
    are needed, or one with --all (`send --all TEXT`)."""
    if text_file:
        try:
            with open(text_file, "r", encoding="utf-8") as handle:
                text = handle.read()
        except (OSError, UnicodeDecodeError) as exc:
            raise UsageError("cannot read --file %s: %s" % (text_file, exc))
        targets = list(words)
    else:
        if len(words) < (1 if all_seats else 2):
            raise UsageError("send needs a target and a text, or --file")
        targets, text = list(words[:-1]), words[-1]
    if not text.strip():
        raise UsageError("the message is empty")
    return targets, text


def caller_record(ctx: Ctx, as_name: Optional[str]) -> Tuple[str, Dict]:
    """Who runs this command, and the agent record ``agent get`` answered for the caller's
    pane ({} when none was read). Design 23.3, in order: ``--as NAME``; the herdr name of
    the agent in the caller's pane; for an agent without a herdr name, the one contact
    of the fleet that matches that agent (``contacts.matches``, the matching
    ``contacts.resolve`` uses, restricted to the caller's pane); ``owner`` only when the
    pane hosts no agent or the command runs outside herdr. An unnamed agent that no
    contact matches is a usage error: its messages would otherwise read "[from owner]"."""
    if as_name:
        if not NAME_RE.fullmatch(as_name):
            raise UsageError("--as %r must match [a-z][a-z0-9_-]{0,31}" % as_name)
        ctx.identity_reason = "explicit --as"
        return as_name, {}
    if not ctx.inside_herdr():
        ctx.identity_reason = "owner outside Herdr"
        return OWNER, {}
    guidance = "cannot establish caller identity; pass --as NAME (or --as owner explicitly)"
    if not ctx.env.get("HERDR_PANE_ID"):
        raise UsageError(guidance + ": caller pane is missing (focused-pane context is not caller identity)")
    pane = ctx.invocation.pane_id
    if not pane:
        raise UsageError(guidance + ": caller pane is missing")
    try:
        record = ctx.herdr.agent_get(pane)
    except HerdrError as exc:
        if exc.code != "agent_not_found":
            raise UsageError(guidance + ": agent lookup failed (%s)" % exc.code)
        record = {}
    name = record.get("name")
    if name:
        ctx.identity_reason = "named caller in %s" % pane
        return str(name), record
    if not record.get("agent"):
        try:
            info = ctx.herdr.pane_get(pane)
            shell = not info.get("agent") and at_shell_prompt(ctx.herdr.process_info(pane))
        except HerdrError:
            shell = False
        if not shell:
            raise UsageError(guidance + ": %s is not a verified shell pane" % pane)
        ctx.identity_reason = "owner at verified shell %s" % pane
        return OWNER, record
    found = [c.name for c in ctx.fleet.contacts if contact_mod.matches(c, record)]
    if len(found) == 1:
        ctx.identity_reason = "unique caller contact in %s" % pane
        return found[0], record
    where = str(record.get("pane_id") or pane)
    several = " (it matches the contacts %s)" % " and ".join(found) if found else ""
    raise UsageError("the agent in %s has no herdr name%s; pass --as NAME or run: herdr agent rename %s NAME"
                     % (where, several, where))


def resolve_sender(ctx: Ctx, as_name: Optional[str]) -> str:
    """The caller's name as ``caller_record`` resolves it (design 23.3)."""
    return caller_record(ctx, as_name)[0]


def selection_label(ctx: Ctx) -> str:
    return "%s (%s)" % (ctx.fleet.path, "explicit" if ctx.file or ctx.env.get("HERDR_FLEET_FILE") else "discovered")


def reply_reference(ctx: Ctx, reference: str) -> str:
    try:
        return inbox.resolve(ctx.fleet.repo, reference).id
    except UsageError as exc:
        if not str(exc).startswith("no message "):
            raise
        raise UsageError("%s; selected fleet/mailbox namespace: %s. A forwarded request may belong elsewhere; "
                         "use --fleet PATH for its originating fleet, or use a plain send for a forwarded-only "
                         "message. A plain send does not retain exact reply linkage." % (exc, ctx.fleet.path))


def resolve_targets(ctx: Ctx, names: Sequence[str], agents: Sequence[Dict]) -> List[Target]:
    """Seats, contacts, ``owner`` and live agent names; unknown names are a usage error.
    A live agent that is neither seat nor contact may belong to another fleet (20.8)."""
    fleet = ctx.fleet
    live = {str(a["name"]): a for a in agents if a.get("name")}
    targets, unknown = [], []
    for name in names:
        contact = fleet.contact(name)
        if fleet.seat(name) is not None:
            targets.append(Target(name, name, None if name in live else "absent"))
        elif name == OWNER:
            targets.append(Target(name, None, None, "owner"))
        elif contact is not None:
            res = contact_mod.resolve(contact, agents)
            destination, canonical, reason = crossfleet.destination(res.agent, agents, fleet.repo)
            targets.append(Target(name, res.pane, res.problem() and "%s: %s" % (res.state, res.problem()),
                                  "contact", destination=destination, requested=name, route_reason=reason))
            if canonical:
                targets[-1].name = canonical
                targets[-1].herdr = canonical
        elif name in live:
            destination, canonical, reason = crossfleet.destination(live[name], agents, fleet.repo)
            ambiguous = sum(a.get("name") == name for a in agents) != 1
            targets.append(Target(canonical or name, None if ambiguous else name,
                                  "ambiguous live agent" if ambiguous else None, "agent",
                                  destination=destination, requested=name, route_reason=reason))
        else:
            unknown.append(name)
    if unknown:
        raise UsageError("not a seat, contact or live agent: %s" % ", ".join(unknown))
    return targets


def queued_by_for(mode: str, fyi: bool, when_ready: bool) -> str:
    """Design 20.8: how a message waits if it waits, from how it was sent (invariant 18).
    ``--queue`` and ``--when-ready`` are ``queue``; an FYI rides along (unless ``--now``);
    any other send the seat could not take is ``refusal``. Since 21.1 both wait for the
    seat's next idle or done; listings still say which."""
    if mode == "queue":
        return "queue"
    if mode == "now":
        return "refusal"
    if fyi:
        return "fyi"
    return "queue" if when_ready else "refusal"


def op_send(ctx: Ctx, words: Sequence[str], text_file: Optional[str] = None, as_name: Optional[str] = None,
            when_ready_ms: Optional[int] = None, all_seats: bool = False, fyi: bool = False,
            queue: bool = False, now: bool = False, reply_to: Optional[str] = None,
            ttl: Optional[str] = None, urgent: bool = False) -> int:
    """Design 18.3, 20.3 and 23.1. The inbox note first, then one line per target. Exit 0 when every
    message was sent or queued, 1 on a failed write, a herdr failure, an unconfirmed
    prompt or a ``--now`` refusal."""
    if queue and now:
        raise UsageError("--queue and --now cannot be combined")
    names, text = message_text(words, text_file, all_seats)
    try:
        lifetime = inbox.parse_ttl(ttl) if ttl else None
    except ValueError as exc:
        raise UsageError(str(exc))
    owner_only = bool(names) and not all_seats and all(n == OWNER for n in names)
    agents: List[Dict] = []
    if not (owner_only and not ctx.inside_herdr()):  # a message to the owner may queue outside herdr
        preflight(ctx)
        agents = ctx.herdr.agent_list()
    sender = resolve_sender(ctx, as_name)
    if all_seats:
        live = {a.get("name") for a in agents}
        names += [s.name for s in ctx.fleet.seats if s.name in live and s.name != sender and s.name not in names]
    if not names:
        if all_seats:
            ctx.say("sender: %s; selected fleet: %s" % (sender, selection_label(ctx)))
            ctx.say("nothing sent: no live seat besides %s" % sender)
            return EXIT_PARTIAL
        raise UsageError("send needs a target and a text, or --file")
    targets = resolve_targets(ctx, names, agents)
    repo = ctx.fleet.repo
    answers = reply_reference(ctx, reply_to) if reply_to else None
    fyi = fyi or is_fyi(text)
    mode = "queue" if queue else "now" if now else "default"
    queued_by = queued_by_for(mode, fyi, bool(when_ready_ms))
    taken = inbox.taken_shorts(repo)
    observed: Dict[str, str] = {}
    codes = []
    note = inbox_note(ctx, sender, bool(as_name))  # design 23.1: mail before reports; it only reads
    if note:
        ctx.say(note)
    ctx.say("sender: %s; selected fleet: %s" % (sender, selection_label(ctx)))
    for target in targets:
        stamp = inbox.now_utc()
        message = Message(id="" if target.destination and not ctx.dry_run else inbox.new_id(stamp, taken), sender=sender, to=target.name, time=inbox.iso_local(stamp),
                          body=text, fyi=fyi, reply_to=answers,
                          ttl=inbox.iso_local(stamp + lifetime) if lifetime else None, file=text_file,
                          queued_by=queued_by, priority=1 if urgent else None)
        log_file = os.path.abspath(text_file) if text_file else None
        if target.destination:
            lines, code = _send_routed(ctx, target, message, mode, when_ready_ms, log_file, observed, taken)
        else:
            lines, code = _send_one(ctx, target, message, mode, when_ready_ms, log_file, observed)
        for line in lines:
            owner = target.destination.path if target.destination else ctx.fleet.path
            route = ("; %s -> %s; origin %s" % (target.requested, target.name, ctx.fleet.path)) if target.destination else ""
            reason = "; source-owned: %s" % target.route_reason if target.route_reason else ""
            fyi_pull = "; available to pull inbox immediately; native delivery remains deferred" if message.fyi and code == EXIT_OK and (ctx.dry_run or message.box == NEW) else ""
            ctx.say(line + "; mailbox: %s :: %s%s%s%s" % (owner, target.name, route, reason, fyi_pull))
        codes.append(code)
    if agents:
        seen.observe_agents(ctx, agents, observed)
    if EXIT_FAIL in codes:
        return EXIT_FAIL
    return EXIT_PARTIAL if EXIT_PARTIAL in codes else EXIT_OK


def inbox_note(ctx: Ctx, name: str, explicit: bool) -> Optional[str]:
    """Design 23.1: when the sender's own mailbox holds messages not past their ttl,
    ``note: 1 unread from sample-orchestrator (#103001, 20:20); read it before you go on:
    herdr-fleet inbox --read``; for several, the senders in the order of their oldest
    message and ``(oldest #id, HH:MM)``. ``--as NAME`` names the mailbox in the command.
    ``send`` prints it before its per-target lines; it only reads."""
    if not NAME_RE.match(name) or not inbox.has_queued(ctx.fleet.repo, name):
        return None
    now = inbox.now_utc()
    waiting = sorted((m for m in inbox.read_box(ctx.fleet.repo, name, NEW) if not inbox.is_due(m, now)),
                     key=Message.sort_key)
    if not waiting:
        return None
    senders: List[str] = []
    for message in waiting:
        if message.sender not in senders:
            senders.append(message.sender)
    who = senders[0] if len(senders) == 1 else "%s and %s" % (", ".join(senders[:-1]), senders[-1])
    count = len(waiting)
    oldest = "%s#%s, %s" % ("" if count == 1 else "oldest ", waiting[0].short, since_label(waiting[0].time))
    command = "herdr-fleet inbox%s --read" % (" --as %s" % name if explicit else "")
    return "note: %d unread from %s (%s); read %s before you go on: %s" % (
        count, who, oldest, "it" if count == 1 else "them", command)


Lines = Tuple[List[str], int]


def _send_routed(ctx: Ctx, target: Target, message: Message, mode: str, when_ready_ms: Optional[int],
                 log_file: Optional[str], observed: Dict[str, str], taken: Set[str]) -> Lines:
    """Create one authoritative destination queue, then reuse ordinary send policy."""
    import copy
    from herdr_fleet.context import Ctx as Context
    source = os.path.realpath(ctx.fleet.repo)
    destination = os.path.realpath(target.destination.repo)
    message.origin_fleet, message.destination_fleet, message.requested_to = source, destination, target.requested
    owner = Context(ctx.herdr, ctx.env, ctx.clock, ctx.stdout, ctx.stderr, destination,
                    file=target.destination.path, dry_run=ctx.dry_run)
    owner._fleet = target.destination
    if ctx.dry_run:
        return _route(owner, target, message, mode, when_ready_ms, log_file, {})
    created = False
    try:
        with crossfleet.reserved((source, destination)):
            crossfleet.safe_mailbox(source, message.to)
            crossfleet.safe_mailbox(destination, message.to)
            with inbox.locked(destination, message.to):
                # Revalidate after locks, before writing either inbox tree.
                crossfleet.validated_fleet(source)
                crossfleet.validated_fleet(destination)
                taken.update(inbox.taken_shorts(source) | inbox.taken_shorts(destination))
                message.id = inbox.new_id(inbox.now_utc(), taken)
                message.route_bytes = inbox.size_of(inbox.direct_text(message))
                message.origin_send_order = len(read(source))
                message.destination_send_order = len(read(destination))
                inbox.write(destination, message)
                created = True  # authoritative success; no fallback/replacement after here
                receipt = copy.copy(message)
                receipt.history_only = True
                inbox.write(source, receipt, CUR)
                append(destination, crossfleet.queued_entry(message, destination))
                append(source, crossfleet.queued_entry(message, source))
    except (OSError, ValueError, UsageError) as exc:
        if not created and message.id:
            for box in (NEW, CUR):
                path = os.path.join(inbox.box_dir(destination, message.to, box), message.id + ".md")
                try:
                    if not os.path.islink(path) and os.path.isfile(path):
                        existing = inbox.load_headers(path, box)
                        if not existing.history_only and crossfleet.receipt_matches(existing, message):
                            created = True  # atomic replace succeeded even if its final stat/write result failed
                            message.path, message.box = path, box
                except (OSError, ValueError):
                    pass
        if created:
            return ["queued at destination for %s #%s; source receipt/log error: %s; "
                    "original destination message retained, no replacement" % (message.to, message.short, exc)], EXIT_FAIL
        return ["failed %s: destination queue creation failed (%s); no source pending copy created"
                % (target.requested, exc)], EXIT_FAIL
    try:
        result = _route(owner, target, message, mode, when_ready_ms, log_file, {})
        if not os.path.exists(message.path):
            message.box = CUR
        return result
    except (HerdrError, OSError) as exc:
        return ["failed native delivery for %s: %s; original destination queue retained #%s"
                % (message.to, exc, message.short)], EXIT_FAIL


def _send_one(ctx: Ctx, target: Target, message: Message, mode: str, when_ready_ms: Optional[int],
              log_file: Optional[str], observed: Dict[str, str]) -> Lines:
    """Write the file, then apply the row of design 18.3 for the target's state."""
    if not ctx.dry_run:
        try:
            inbox.write(ctx.fleet.repo, message)
        except OSError as exc:
            return ["failed %s: cannot write the message file (%s); nothing sent" % (target.name, exc)], EXIT_FAIL
    try:
        if target.kind == "owner":
            return _send_owner(ctx, message, mode, log_file)
        return _route(ctx, target, message, mode, when_ready_ms, log_file, observed)
    except (HerdrError, OSError) as exc:  # before any prompt: the message stays queued
        if ctx.dry_run:
            raise
        _log_send(ctx, target, message, "queued", log_file)
        return ["failed %s: %s; #%s left queued" % (target.name, exc, message.short)], EXIT_FAIL


def _log_send(ctx: Ctx, target: Target, message: Message, delivery: str, log_file: Optional[str]) -> None:
    """Log the ``send`` entry here and, for a live agent of another fleet, there too with
    the body in that fleet's ``inbox/<target>/cur/`` (design 20.8)."""
    if message.origin_fleet:
        return  # the sole queued send was recorded under its owning mailbox lock
    entry = send_entry(message, delivery, log_file)
    record(ctx, entry)
    if target.other:
        mirror_entry(ctx, target.other, entry)
        try:
            crossfleet.mirror_body(target.other, message)
        except (OSError, UsageError) as exc:
            ctx.warn("cannot store the message in the other fleet at %s: %s" % (target.other, exc))


def _route(ctx: Ctx, target: Target, message: Message, mode: str, when_ready_ms: Optional[int],
           log_file: Optional[str], observed: Dict[str, str]) -> Lines:
    state = target.problem or agent_state(ctx, target.herdr)
    reachable = not target.problem and state != "absent"
    observed[target.name] = state if reachable else "absent"
    if mode == "queue":  # the flush before send goes only to an idle or done seat (21.1)
        return _queued(ctx, target, message, state, log_file, flush=reachable and state in SETTLED)
    if when_ready_ms and reachable and state not in SETTLED:
        if ctx.dry_run:
            return ["would wait up to %d ms for %s (%s), then deliver or queue"
                    % (when_ready_ms, target.name, state_words(state))], EXIT_OK
        try:
            ctx.herdr.agent_wait(str(target.herdr), ("idle", "done"), when_ready_ms)
        except HerdrError:
            pass
        state = agent_state(ctx, str(target.herdr))
        observed[target.name] = state
        if state not in SETTLED:  # not ready: --now refuses, anything else queues
            label = "%s after --when-ready %d ms%s" % (state, when_ready_ms,
                                                     BLOCKED_TAIL if state == "blocked" else "")
            if mode == "now":
                return _refused(ctx, target, message, label, log_file)
            return _queued(ctx, target, message, label, log_file)
    # Older messages queued on refusal go along to an idle or done seat (20.8), so an FYI
    # to an idle seat then rides along instead of waiting. A working seat takes text only
    # from --now (21.1): everything else waits for its next idle or done.
    refusals = reachable and state in SETTLED and refusal_waiting(ctx, target.name, exclude=(message.id,))
    if state in SETTLED:
        deliver = mode == "now" or not message.fyi or refusals
    else:
        deliver = mode == "now" and state == "working"
    if not deliver:
        if mode == "now":
            return _refused(ctx, target, message, state, log_file)
        return _queued(ctx, target, message, state, log_file, mode=mode)
    allow = SEND_ALLOWED if mode == "now" else SETTLED
    if ctx.dry_run:
        return [_would_send(ctx, target, message, state, mode)], EXIT_OK
    with inbox.locked(ctx.fleet.repo, target.name):
        drop = prompt_mailbox(ctx, target.name, str(target.herdr), allow, "send",
                              own=None if message.origin_fleet else message, mirror=target.other)
    extra = len(drop.carried or drop.waiting) - 1
    queued_note = " with %d queued" % extra if extra > 0 else ""
    shown = drop.state or state
    if drop.status == "empty" and message.origin_fleet:
        return ["already consumed from destination mailbox for %s #%s" % (target.name, message.short)], EXIT_OK
    if drop.status == "sent":
        _log_send(ctx, target, message, "sent", log_file)
        urgent = "; " + URGENT_TYPED if message.urgent and shown == "working" else ""
        return ["sent to %s (%s)%s, %s%s" % (target.name, place(shown), queued_note, size_label(drop.size), urgent)
                ], EXIT_OK
    if drop.status == "unconfirmed":  # stalled or timed out: may have arrived; logged, never resent
        _log_send(ctx, target, message, "unconfirmed", log_file)
        return ["unconfirmed %s%s, %s: %s; logged as unconfirmed" % (target.name, queued_note,
                                                                    size_label(drop.size), drop.detail)], EXIT_FAIL
    if drop.status == "pointer":
        line, code = _queued_line(ctx, target, message, shown, log_file)
        return [line + "; prompted a pointer instead: %s"
                % inbox.pointer_text(drop.waiting, drop.size, prefix=False)], code
    if drop.status in ("refused", "typing"):  # turned busy since the check, or the owner is typing
        label = drop.state if drop.status == "refused" else held_label(drop, state)
        if mode == "now":
            return _refused(ctx, target, message, label, log_file)
        return _queued(ctx, target, message, label, log_file, mode=mode)
    _log_send(ctx, target, message, "queued", log_file)
    return ["failed %s: %s; #%s left queued" % (target.name, drop.detail, message.short)], EXIT_FAIL


def _label(state: Optional[str], message: Message) -> str:
    parts = ([state_words(state)] if state else []) + ([FYI_PLACE] if message.fyi else [])
    return " (%s)" % ", ".join(parts) if parts else ""


def _lands(state: Optional[str], message: Message, mode: str) -> str:
    """Design 23.1: a plain send queued to a working seat says when it lands."""
    return LANDS if mode == "default" and state == "working" and not message.fyi else ""


def _queued_line(ctx: Ctx, target: Target, message: Message, state: Optional[str],
                 log_file: Optional[str], mode: str = "") -> Tuple[str, int]:
    """Log the message as queued: ``queued in herdr-fleet for X (absent) #id, position 2 of 2``
    (a plain send to a working seat adds ``; lands at its next idle, or now with --now``)."""
    _log_send(ctx, target, message, "queued", log_file)
    where, count = position(ctx, target.name, message.id)
    spot = ", position %d of %d" % (where, count) if where else ""
    return "queued in herdr-fleet for %s%s #%s%s%s" % (target.name, _label(state, message), message.short, spot,
                                                     _lands(state, message, mode)), EXIT_OK


def _queued(ctx: Ctx, target: Target, message: Message, state: Optional[str],
            log_file: Optional[str], flush: bool = False, mode: str = "") -> Lines:
    """The message waits in its mailbox. With ``flush`` (the seat is idle or done; never
    working since design 21.1) older messages queued on refusal go now (20.8, 20.9), the
    new one staying queued."""
    if ctx.dry_run:
        line = "would queue in herdr-fleet for %s%s%s" % (target.name, _label(state, message),
                                                         _lands(state, message, mode))
        if flush and refusal_waiting(ctx, target.name, exclude=(message.id,)):
            hold = dry_hold(ctx, target.herdr)
            line += "; " + (HOLD % hold if hold else "would deliver what was queued on refusal")
        return [line], EXIT_OK
    lines: List[str] = []
    code = EXIT_OK
    if flush and refusal_waiting(ctx, target.name, exclude=(message.id,)):
        with inbox.locked(ctx.fleet.repo, target.name):
            drop = prompt_mailbox(ctx, target.name, str(target.herdr), SETTLED, "send",
                                  exclude=(message.id,), mirror=target.other)
        flushed, code = refusal_line(target.name, drop, state or "")
        lines.append(flushed)
    line, _ = _queued_line(ctx, target, message, state, log_file, mode)
    return [line] + lines, code


def refusal_line(name: str, drop: Drop, state: str) -> Tuple[str, int]:
    """What delivering a mailbox for its refusal messages did, as ``deliver`` says it."""
    shown = drop.state or state
    count = len(drop.carried)
    if drop.status == "sent":
        return "delivered %d queued on refusal to %s (%s), %s" % (count, name, place(shown),
                                                                 size_label(drop.size)), EXIT_OK
    if drop.status == "unconfirmed":
        return ("unconfirmed %s: %s; %d logged as delivered (unconfirmed), not resent"
                % (name, drop.detail, count), EXIT_FAIL)
    if drop.status == "pointer":
        return "prompted %s (%s) with a pointer: %s" % (
            name, place(shown), inbox.pointer_text(drop.waiting, drop.size, prefix=False)), EXIT_OK
    if drop.status == "refused":
        return "%s: %s, %d left queued" % (name, state_words(drop.state), len(drop.waiting)), EXIT_OK
    if drop.status == "typing":  # design 21.2: nothing moves while the owner types there
        return "%s: %s, %d left queued" % (name, drop.detail, len(drop.waiting)), EXIT_OK
    if drop.status == "empty":
        return "nothing queued for %s" % name, EXIT_OK
    return "failed %s: %s; %d left queued" % (name, drop.detail, len(drop.waiting)), EXIT_FAIL


def _refused(ctx: Ctx, target: Target, message: Message, state: str, log_file: Optional[str]) -> Lines:
    """``--now`` to a seat that cannot take it: nothing sent, the file goes to ``cur/``."""
    if ctx.dry_run:
        return ["would refuse %s (%s): nothing sent" % (target.name, state_words(state))], EXIT_OK
    if message.origin_fleet:
        with inbox.locked(ctx.fleet.repo, message.to):
            if not os.path.exists(message.path):
                return ["already consumed from destination mailbox for %s #%s" % (message.to, message.short)], EXIT_OK
            inbox.move(ctx.fleet.repo, message, CUR)
            record(ctx, disposal_entry("dropped", message, "send --now refused"))
        return ["refused %s (%s): nothing sent" % (target.name, state_words(state))], EXIT_FAIL
    try:
        inbox.move(ctx.fleet.repo, message, CUR)
    except OSError as exc:
        ctx.warn("cannot move %s to cur/: %s" % (message.path, exc))
    _log_send(ctx, target, message, "refused", log_file)
    return ["refused %s (%s): nothing sent" % (target.name, state_words(state))], EXIT_FAIL


def _would_send(ctx: Ctx, target: Target, message: Message, state: str, mode: str) -> str:
    hold = dry_hold(ctx, target.herdr)  # design 21.2: the guard only reads, so a dry run runs it
    if hold:
        if mode == "now":
            return "would refuse %s (%s): nothing sent; %s" % (target.name, state, HOLD % hold)
        return "would queue in herdr-fleet for %s%s; %s" % (target.name, _label(state, message), HOLD % hold)
    now = inbox.now_utc()
    queued = [m for m in inbox.read_box(ctx.fleet.repo, target.name, NEW) if not inbox.is_due(m, now)]
    _, size, pointer = compose_prompt(queued, message)
    if pointer:
        return "would queue in herdr-fleet for %s (%s) and prompt a pointer: %s" % (
            target.name, state, inbox.pointer_text(queued + [message], size, prefix=False))
    return "would send to %s (%s)%s, %s" % (target.name, place(state),
                                           " with %d queued" % len(queued) if queued else "", size_label(size))


def _send_owner(ctx: Ctx, message: Message, mode: str, log_file: Optional[str]) -> Lines:
    """``owner``: queued, then the mailbox is shown as a notification (unless --queue, or
    outside herdr, where it stays queued for ``inbox --as owner``)."""
    target = Target(OWNER, None, None, "owner")
    if mode == "queue":
        return _queued(ctx, target, message, None, log_file)
    if not ctx.inside_herdr():
        return _queued(ctx, target, message, "not in herdr", log_file)
    if ctx.dry_run:
        return ["would notify owner, %s" % size_label(inbox.size_of(inbox.direct_text(message)))], EXIT_OK
    record(ctx, send_entry(message, "queued", log_file))
    with inbox.locked(ctx.fleet.repo, OWNER):
        drop = notify_owner(ctx)
    if drop.status == "sent":
        extra = len(drop.carried) - 1
        return ["sent to owner (notification)%s, %s" % (" with %d queued" % extra if extra > 0 else "",
                                                        size_label(drop.size))], EXIT_OK
    where, count = position(ctx, OWNER, message.id)
    spot = ", position %d of %d" % (where, count) if where else ""
    return ["queued in herdr-fleet for owner (%s) #%s%s" % (drop.detail or "not shown", message.short, spot)], EXIT_OK


def agent_state(ctx: Ctx, target: Optional[str]) -> str:
    """The agent's state, or ``absent`` when herdr knows no such agent."""
    if not target:
        return "absent"
    try:
        return str(ctx.herdr.agent_get(target).get("agent_status") or "unknown")
    except HerdrError as exc:
        if "not_found" in exc.code:
            return "absent"
        raise


# -- ack ------------------------------------------------------------------------------------------


def log_ack(ctx: Ctx, entry: Dict) -> None:
    append(ctx.fleet.repo, entry)
    try:
        crossfleet.terminal(ctx, entry)
    except (OSError, ValueError, UsageError) as exc:
        ctx.warn("cannot mirror acknowledgement receipt: %s" % exc)


def op_ack(ctx: Ctx, names: Sequence[str], all_owed: bool = False, as_name: Optional[str] = None,
           older_than: Optional[str] = None, reason: Optional[str] = None) -> int:
    """Design 17.1, 18.8, 20.2 and 23.4: settle what the caller owes by hand. A name settles the
    caller's debt to that sender; an id (``#3f2a9c``) settles that one message;
    ``--older-than`` settles every message owed that was delivered longer ago, one by one
    (names and ids restrict it). ``--reason TEXT`` goes with ids only and closes the debt of
    each message whoever owes it, logged with the caller as ``by``; without it, an id owed
    by someone else is a usage error and nothing is written. Appends one ``ack`` entry per
    debt and sends nothing to anyone; ``--dry-run`` writes nothing. The caller is found as
    for ``send``; herdr is asked only when ``--as`` does not say."""
    try:
        limit = inbox.parse_ttl(older_than, "--older-than") if older_than else None
    except ValueError as exc:
        raise UsageError(str(exc))
    if not names and not all_owed and limit is None:
        raise UsageError("ack needs a name, a message id, --all or --older-than")
    if reason is not None and not reason.strip():
        raise UsageError("--reason needs a text (why the debt is closed)")
    if reason is not None and (all_owed or limit is not None):
        raise UsageError("--reason goes with message ids only (ack #id --reason TEXT), not with --all or "
                         "--older-than")
    if not as_name:
        preflight(ctx)
    caller = resolve_sender(ctx, as_name)
    repo = ctx.fleet.repo
    warnings: List[str] = []
    entries = read(repo, warnings)
    for warning in warnings:
        ctx.warn(warning)
    known = {s.name for s in ctx.fleet.seats} | {c.name for c in ctx.fleet.contacts} | {OWNER}
    known |= {str(e.get(k)) for e in entries for k in ("from", "to")}
    items: List[Tuple[str, str]] = []  # ("name", NAME) or ("id", full id)
    unknown = []
    for name in names:
        if name.startswith("#") or (name not in known and inbox.looks_like_id(name)):
            items.append(("id", inbox.resolve(repo, name).id))
        elif name in known:
            items.append(("name", name))
        else:
            unknown.append(name)
    if unknown:
        raise UsageError("not a seat, contact, message id or name in the message log: %s" % ", ".join(unknown))
    if reason is not None and any(kind != "id" for kind, _ in items):
        raise UsageError("--reason goes with message ids only (ack #id --reason TEXT); %s is a name"
                         % ", ".join(value for kind, value in items if kind != "id"))
    if limit is not None:
        return _ack_older(ctx, entries, caller, items, limit, str(older_than))
    debts = [o for o in owed(entries) if o["to"] == caller]
    by_sender = {o["from"]: o for o in debts}
    open_ids = {item["id"]: item for _, item in unsettled(entries) if item.get("id")}
    others = ["#%s is owed by %s, not you; add --reason to close it anyway" % (inbox.short_of(value),
                                                                              open_ids[value]["to"])
              for kind, value in items if kind == "id" and reason is None and value in open_ids
              and open_ids[value]["to"] != caller]
    if others:
        raise UsageError("; ".join(others))
    todo: List[Tuple[str, str]] = []
    for item in items + ([("name", o["from"]) for o in debts] if all_owed else []):
        if item not in todo:
            todo.append(item)
    if not todo:
        ctx.say("nothing owed by %s" % caller)
        return EXIT_OK
    verb = "would ack" if ctx.dry_run else "acked"
    for kind, value in todo:
        if kind == "id":
            debt = open_ids.get(value)
            if debt is None:
                ctx.say("nothing owed for #%s" % inbox.short_of(value))
                continue
            if not ctx.dry_run:
                log_ack(ctx, ack_entry(debt["to"], debt["from"], ident=value,
                                      by=caller if reason is not None else None, reason=reason))
            whose = ", owed by %s" % debt["to"] if debt["to"] != caller else ""
            why = "; reason: %s" % reason if reason is not None else ""
            ctx.say("%s #%s from %s since %s (%s)%s%s" % (verb, inbox.short_of(value), debt["from"],
                                                          since_label(debt["since"]), debt["first_line"], whose, why))
            continue
        debt = by_sender.get(value)
        if debt is None:
            ctx.say("nothing owed to %s" % value)
            continue
        if not ctx.dry_run:
            append(repo, ack_entry(caller, value))
            for _, item in unsettled(entries):
                if item.get("id") and item["to"] == caller and item["from"] == value:
                    try:
                        crossfleet.terminal(ctx, ack_entry(caller, value, ident=item["id"]))
                    except (OSError, ValueError, UsageError) as exc:
                        ctx.warn("cannot mirror acknowledgement receipt: %s" % exc)
        ctx.say("%s %s since %s (%s)" % (verb, value, since_label(debt["since"]), debt["first_line"]))
    return EXIT_OK


def _ack_older(ctx: Ctx, entries: Sequence[Dict], caller: str, items: Sequence[Tuple[str, str]],
               limit: datetime.timedelta, spelled: str) -> int:
    """``ack --older-than``: every unsettled request to the caller delivered more than
    ``limit`` ago, restricted to the named senders and ids when any are given. Each is
    acknowledged by id; a message without one (sent before 0.6.0) by its sender's name,
    and only when no newer message from that sender is still owed, since a plain ``ack``
    settles everything before it."""
    now = inbox.now_utc()
    senders = {value for kind, value in items if kind == "name"}
    ids = {value for kind, value in items if kind == "id"}
    mine = [item for _, item in unsettled(entries) if item["to"] == caller]
    if items:
        mine = [i for i in mine if i["from"] in senders or i.get("id") in ids]

    def age(item: Dict) -> float:
        when = inbox.parse_time(item["since"])
        return (now - when).total_seconds() if when is not None else -1.0

    old = [i for i in mine if age(i) > limit.total_seconds()]
    if not old:
        ctx.say("nothing owed by %s older than %s" % (caller, spelled))
        return EXIT_OK
    verb = "would ack" if ctx.dry_run else "acked"
    named_done: Set[str] = set()
    for item in old:
        label = "since %s, %s ago (%s)" % (since_label(item["since"]), inbox.age_label(age(item)), item["first_line"])
        if item.get("id"):
            if not ctx.dry_run:
                log_ack(ctx, ack_entry(caller, item["from"], ident=item["id"]))
            ctx.say("%s #%s from %s %s" % (verb, inbox.short_of(item["id"]), item["from"], label))
            continue
        sender = item["from"]
        if sender in named_done:
            continue
        if any(i["from"] == sender and age(i) <= limit.total_seconds() for i in mine):
            ctx.say("skipped a message from %s %s: it has no id, and ack %s would also settle newer ones"
                    % (sender, label, sender))
            continue
        named_done.add(sender)
        if not ctx.dry_run:
            append(ctx.fleet.repo, ack_entry(caller, sender))
        ctx.say("%s %s %s" % (verb, sender, label))
    return EXIT_OK


# -- log command --------------------------------------------------------------------------------


def op_log(ctx: Ctx, seat: Optional[str] = None, last: int = 50, as_json: bool = False,
           owed_only: bool = False) -> int:
    warnings: List[str] = []
    entries = read(ctx.fleet.repo, warnings)
    for warning in warnings:
        ctx.warn(warning)
    if owed_only:
        items = owed(entries)
        if as_json:
            ctx.say(json.dumps(items, indent=2, ensure_ascii=False))
        else:
            for line in owed_lines(items) or ["nothing awaiting a reply"]:
                ctx.say(line)
        return EXIT_OK
    if seat:
        entries = [e for e in entries if seat in (e.get("from"), e.get("to"))]
    entries = entries[-last:] if last > 0 else entries
    if as_json:
        ctx.say(json.dumps(entries, indent=2, ensure_ascii=False))
    else:
        for entry in entries:
            ctx.say(format_entry(entry))
    return EXIT_OK
