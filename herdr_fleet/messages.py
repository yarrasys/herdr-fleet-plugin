"""Messages (design 16.1 to 16.3, 17.1, 18.2 to 18.4, 18.8): ``send``, ``ack``, the
message log, owed replies, and the delivery engine that ``send``, kickoffs and
``deliver`` share.

herdr-fleet is a messenger only: it never reads or interprets an agent's
output, never routes replies and never decides who gets a message.

- ``send`` writes every message to the target's mailbox first (``inbox``), then
  follows the table of design 18.3: an idle or done seat gets it now, a working
  one gets it typed ahead (herdr queues it for the next turn; the only prompt
  that may reach a ``working`` agent), and anything else leaves it queued.
  ``--queue`` always queues, ``--now`` restores the 16.1 refusals, and an FYI
  never wakes an idle seat. Any delivery carries the queued messages in front
  (the batch of 18.4), so order is never inverted.
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
  pair, and ``--reply-to`` or ``ack #id`` settles one message exactly.
  ``ack`` writes the log only and sends nothing (invariant 10).
"""

from __future__ import annotations

import datetime
import json
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

from herdr_fleet import contacts as contact_mod
from herdr_fleet import inbox
from herdr_fleet.context import EXIT_FAIL, EXIT_OK, EXIT_PARTIAL, Ctx, UsageError, preflight
from herdr_fleet.fleetfile import NAME_RE
from herdr_fleet.herdr import HerdrError
from herdr_fleet.inbox import CUR, NEW, Message, size_label
from herdr_fleet.kickoff import INBOX_HEADING
from herdr_fleet.notify import notify
from herdr_fleet.seats import SEND_ALLOWED, SETTLED, safe_prompt
from herdr_fleet.state import ensure_gitignore, now_iso

LOG_NAME = "messages.jsonl"
FIRST_LINE_MAX = 120
PLUGIN_SENDER = "herdr-fleet"
OWNER = "owner"
PREFIX_RE = re.compile(r"^\[from [^\]]*\]\s*")  # [from X], [from X #3f2a9c], [from X at 10:09 #3f2a9c re #91be02]
FYI_RE = re.compile(r"^fyi[: ]", re.IGNORECASE)
ANSWERED = ("sent", "unconfirmed", "queued")  # a send in these states answers (refused never does)
since_label = inbox.time_label


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
    return entry


def delivered_entry(message: Message, via: str, delivery: str = "sent") -> Dict:
    return {"time": now_iso(), "from": PLUGIN_SENDER, "to": message.to, "kind": "delivered", "id": message.id,
            "via": via, "bytes": 0, "first_line": first_line(message.body), "delivery": delivery}


def disposal_entry(kind: str, message: Message, via: str) -> Dict:
    """``expired`` (via ``ttl``) or ``dropped`` (via ``drop-inbox``): moved to ``cur/`` undelivered."""
    return {"time": now_iso(), "from": PLUGIN_SENDER, "to": message.to, "kind": kind, "id": message.id,
            "via": via, "bytes": 0, "first_line": first_line(message.body), "delivery": "none"}


def ack_entry(caller: str, seat: str, time: Optional[str] = None, ident: Optional[str] = None) -> Dict:
    """Design 17.1: one hand-made acknowledgement; nothing is sent to anyone. With ``ident``
    it settles that one message (18.8)."""
    entry = {"time": time or now_iso(), "from": caller, "to": seat, "kind": "ack", "bytes": 0,
             "first_line": "ack", "delivery": "logged"}
    if ident:
        entry["id"] = ident
    return entry


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
        append(ctx.fleet.repo, entry)
    except OSError as exc:
        ctx.warn("cannot append to %s: %s" % (log_path(ctx.fleet.repo), exc))


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


def log_kickoff(repo: str, to: str, text: str) -> Optional[str]:
    """Record a kickoff; returns a warning instead of failing the kickoff."""
    try:
        append(repo, make_entry(PLUGIN_SENDER, to, "kickoff", text))
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
    that one. Kickoffs and plugin entries never count. Times have one-second
    resolution, so equal times are ordered by position in the append-only log."""
    pending: Dict[str, Dict] = {}
    asks: List[Tuple[Key, Dict]] = []
    answers: Dict[Tuple[str, str], Key] = {}
    named: Set[str] = set()

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
        key: Key = (when, index)
        kind = entry.get("kind", "send")
        pair = (str(entry["from"]), str(entry["to"]))
        if kind == "send":
            delivery = entry.get("delivery") or "sent"
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
            source = pending.pop(str(entry.get("id")), None)
            if source is not None:
                ask(source, key, str(entry["time"]))
        elif kind == "ack":
            if entry.get("id"):
                named.add(str(entry["id"]))
            elif pair not in answers or key > answers[pair]:
                answers[pair] = key
    still = []
    for key, item in asks:
        reply = answers.get((item["to"], item["from"]))
        if item.get("id") in named or (reply is not None and reply > key):
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


def owed_lines(items: Sequence[Dict], today: Optional[datetime.date] = None) -> List[str]:
    return ["awaiting: %s owes %s since %s (%s)" % (o["to"], o["from"], since_label(o["since"], today),
                                                    o["first_line"]) for o in items]


def format_entry(entry: Dict) -> str:
    """``time  from → to  bytes  first_line``. After the bytes: ``?`` for an unconfirmed
    delivery; then ``#id``, ``[queued]`` or ``[refused]`` and ``[fyi]`` before the first
    line. Plugin entries read ``time  herdr-fleet → to  delivered #id via send  first_line``
    (``expired``, ``dropped`` alike) and an acknowledgement ``time  from → to  ack [#id]``."""
    kind = entry.get("kind", "send")
    ident = "#%s" % inbox.short_of(str(entry["id"])) if entry.get("id") else ""
    head = "%s  %s → %s" % (entry["time"], entry["from"], entry["to"])
    if kind == "ack":
        return "%s  ack%s" % (head, " " + ident if ident else "")
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

    status: str                  # sent | unconfirmed | pointer | refused | failed | empty
    detail: str = ""
    state: str = ""              # the state safe_prompt saw
    carried: List[Message] = field(default_factory=list)  # in the prompt, now in cur/
    waiting: List[Message] = field(default_factory=list)  # still in new/
    size: int = 0                # bytes of the prompt (of the batch, for a pointer)


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
    waiting = queued_in(ctx, name)
    if not waiting:
        return "", [], []
    block = INBOX_HEADING + "\n" + inbox.render_batch(waiting)
    size = inbox.size_of(block)
    if size > inbox.BATCH_CAP:
        return INBOX_HEADING + "\n" + inbox.pointer_text(waiting, size, prefix=False), [], waiting
    return block, waiting, []


def compose_prompt(queued: Sequence[Message], own: Optional[Message] = None) -> Tuple[str, int, bool]:
    """(prompt, size, pointer). A message alone is ``[from X #id] body``; with anything
    queued, the batch of 18.4 with ``own`` last. Above BATCH_CAP the prompt is the pointer."""
    items = list(queued) + ([own] if own is not None else [])
    text = inbox.direct_text(own) if own is not None and not queued else inbox.render_batch(items)
    size = inbox.size_of(text)
    if size > inbox.BATCH_CAP:
        return inbox.pointer_text(items, size), size, True
    return text, size, False


def settle(ctx: Ctx, messages: Sequence[Message], via: str, delivery: str,
           own: Optional[Message] = None) -> None:
    """After a sent or unconfirmed prompt: move every carried file to ``cur/`` and log one
    ``delivered`` entry each (``own``, a message ``send`` just wrote, logs its own entry)."""
    for message in messages:
        try:
            inbox.move(ctx.fleet.repo, message, CUR)
        except OSError as exc:
            ctx.warn("delivered, but cannot move %s to cur/: %s" % (message.path, exc))
        if own is None or message.id != own.id:
            record(ctx, delivered_entry(message, via, delivery))


def prompt_mailbox(ctx: Ctx, name: str, target: str, allow: Sequence[str], via: str,
                   own: Optional[Message] = None) -> Drop:
    """Deliver a mailbox in one prompt (design 18.4). The caller holds ``inbox.locked``.
    ``target`` is what ``agent prompt`` gets (a name or a pane id); ``allow`` the states
    ``safe_prompt`` accepts; ``own`` the message ``send`` just queued, which goes last."""
    expire_due(ctx, name)
    queued = [m for m in queued_in(ctx, name) if own is None or m.id != own.id]
    items = queued + ([own] if own is not None else [])
    if not items:
        return Drop("empty")
    text, size, pointer = compose_prompt(queued, own)
    outcome = safe_prompt(ctx.herdr, target, text, allow)
    state = str(outcome.agent.get("agent_status") or "")
    if pointer and (outcome.ok or outcome.status == "unconfirmed"):
        return Drop("pointer", outcome.detail, state, [], items, size)
    if outcome.ok or outcome.status == "unconfirmed":
        delivery = "sent" if outcome.ok else "unconfirmed"
        settle(ctx, items, via, delivery, own)
        return Drop(delivery, outcome.detail, state, items, [], size)
    if outcome.status == "error":
        return Drop("failed", outcome.detail, state, [], items, size)
    return Drop("refused", outcome.detail, outcome.status, [], items, size)  # herdr sent nothing


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
        title = "%d messages for owner (oldest %s)" % (len(items), since_label(items[0].time))
        body = "\n".join("#%s %s: %s" % (m.short, m.sender, first_line(m.body)) for m in items)
    senders = sorted({m.sender for m in items})
    body += "\nread: herdr-fleet inbox --as owner --history %s" % (senders[0] if len(senders) == 1 else "<name>")
    if not notify(ctx.herdr, title, body, "request", errors=ctx.stderr):
        return Drop("failed", "notification failed", "", [], items)
    settle(ctx, items, "notify", "sent")
    return Drop("sent", "", "notification", items, [], sum(inbox.size_of(inbox.direct_text(m)) for m in items))


def queued_in(ctx: Ctx, name: str) -> List[Message]:
    """The ``new/`` messages of a mailbox, oldest first; unreadable files are warned about."""
    warnings: List[str] = []
    found = inbox.read_box(ctx.fleet.repo, name, NEW, warnings)
    for warning in warnings:
        ctx.warn(warning)
    return found


# -- send -----------------------------------------------------------------------------------


@dataclass
class Target:
    name: str                    # as given: seat, contact, owner or live agent name; also the mailbox
    herdr: Optional[str]         # what `agent prompt` gets: a name or a pane id
    problem: Optional[str] = None
    kind: str = "seat"           # seat | contact | agent | owner


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


def resolve_sender(ctx: Ctx, as_name: Optional[str]) -> str:
    """``--as``, else the named agent in the caller's pane, else ``owner``. Outside herdr
    nothing is asked: the caller is ``owner``."""
    if as_name:
        if not NAME_RE.match(as_name):
            raise UsageError("--as %r must match [a-z][a-z0-9_-]{0,31}" % as_name)
        return as_name
    pane = ctx.invocation.pane_id if ctx.inside_herdr() else None
    name = ctx.herdr.pane_agent(pane).get("name") if pane else None
    return str(name) if name else OWNER


def resolve_targets(ctx: Ctx, names: Sequence[str], agents: Sequence[Dict]) -> List[Target]:
    """Seats, contacts, ``owner`` and live agent names; unknown names are a usage error."""
    fleet = ctx.fleet
    live = {str(a["name"]): a for a in agents if a.get("name")}
    targets, unknown = [], []
    for name in names:
        contact = fleet.contact(name)
        if fleet.seat(name) is not None:
            targets.append(Target(name, name, None if name in live else "absent"))
        elif contact is not None:
            res = contact_mod.resolve(contact, agents)
            targets.append(Target(name, res.pane, res.problem() and "%s: %s" % (res.state, res.problem()),
                                  "contact"))
        elif name == OWNER:
            targets.append(Target(name, None, None, "owner"))
        elif name in live:
            targets.append(Target(name, name, None, "agent"))
        else:
            unknown.append(name)
    if unknown:
        raise UsageError("not a seat, contact or live agent: %s" % ", ".join(unknown))
    return targets


def op_send(ctx: Ctx, words: Sequence[str], text_file: Optional[str] = None, as_name: Optional[str] = None,
            when_ready_ms: Optional[int] = None, all_seats: bool = False, fyi: bool = False,
            queue: bool = False, now: bool = False, reply_to: Optional[str] = None,
            ttl: Optional[str] = None) -> int:
    """Design 18.3. One line per target. Exit 0 when every message was sent or queued,
    1 on a failed write, a herdr failure, an unconfirmed prompt or a ``--now`` refusal."""
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
            ctx.say("nothing sent: no live seat besides %s" % sender)
            return EXIT_PARTIAL
        raise UsageError("send needs a target and a text, or --file")
    targets = resolve_targets(ctx, names, agents)
    repo = ctx.fleet.repo
    answers = inbox.resolve(repo, reply_to).id if reply_to else None
    fyi = fyi or is_fyi(text)
    mode = "queue" if queue else "now" if now else "default"
    taken = inbox.taken_shorts(repo)
    codes = []
    for target in targets:
        stamp = inbox.now_utc()
        message = Message(id=inbox.new_id(stamp, taken), sender=sender, to=target.name, time=inbox.iso_local(stamp),
                          body=text, fyi=fyi, reply_to=answers,
                          ttl=inbox.iso_local(stamp + lifetime) if lifetime else None, file=text_file)
        line, code = _send_one(ctx, target, message, mode, when_ready_ms,
                               os.path.abspath(text_file) if text_file else None)
        ctx.say(line)
        codes.append(code)
    if EXIT_FAIL in codes:
        return EXIT_FAIL
    return EXIT_PARTIAL if EXIT_PARTIAL in codes else EXIT_OK


def _send_one(ctx: Ctx, target: Target, message: Message, mode: str, when_ready_ms: Optional[int],
              log_file: Optional[str]) -> Tuple[str, int]:
    """Write the file, then apply the row of design 18.3 for the target's state."""
    if not ctx.dry_run:
        try:
            inbox.write(ctx.fleet.repo, message)
        except OSError as exc:
            return "failed %s: cannot write the message file (%s); nothing sent" % (target.name, exc), EXIT_FAIL
    try:
        if target.kind == "owner":
            return _send_owner(ctx, message, mode, log_file)
        return _route(ctx, target, message, mode, when_ready_ms, log_file)
    except (HerdrError, OSError) as exc:  # before any prompt: the message stays queued
        if ctx.dry_run:
            raise
        record(ctx, send_entry(message, "queued", log_file))
        return "failed %s: %s; #%s left queued" % (target.name, exc, message.short), EXIT_FAIL


def _route(ctx: Ctx, target: Target, message: Message, mode: str, when_ready_ms: Optional[int],
           log_file: Optional[str]) -> Tuple[str, int]:
    state = target.problem or agent_state(ctx, target.herdr)
    reachable = not target.problem and state != "absent"
    if mode == "queue":
        return _queued(ctx, target, message, state, log_file)
    if when_ready_ms and reachable and state not in SETTLED:
        if ctx.dry_run:
            return ("would wait up to %d ms for %s (%s), then deliver or queue" % (when_ready_ms, target.name, state),
                    EXIT_OK)
        try:
            ctx.herdr.agent_wait(str(target.herdr), ("idle", "done"), when_ready_ms)
        except HerdrError:
            pass
        state = agent_state(ctx, str(target.herdr))
        if state not in SETTLED:  # not ready: --now refuses, anything else queues
            label = "%s after --when-ready %d ms" % (state, when_ready_ms)
            if mode == "now":
                return _refused(ctx, target, message, label, log_file)
            return _queued(ctx, target, message, label, log_file)
    if state in SETTLED:
        deliver = mode == "now" or not message.fyi  # an FYI never wakes an idle seat
    else:
        deliver = state == "working"
    if not deliver:
        if mode == "now":
            return _refused(ctx, target, message, state, log_file)
        return _queued(ctx, target, message, state, log_file)
    allow = ("working",) if message.fyi and mode != "now" else SEND_ALLOWED
    if ctx.dry_run:
        return _would_send(ctx, target, message, state), EXIT_OK
    with inbox.locked(ctx.fleet.repo, target.name):
        drop = prompt_mailbox(ctx, target.name, str(target.herdr), allow, "send", own=message)
    extra = len(drop.carried or drop.waiting) - 1
    queued_note = " with %d queued" % extra if extra > 0 else ""
    shown = drop.state or state
    if drop.status == "sent":
        record(ctx, send_entry(message, "sent", log_file))
        return "sent to %s (%s)%s, %s" % (target.name, shown, queued_note, size_label(drop.size)), EXIT_OK
    if drop.status == "unconfirmed":  # stalled or timed out: may have arrived; logged, never resent
        record(ctx, send_entry(message, "unconfirmed", log_file))
        return ("unconfirmed %s%s, %s: %s; logged as unconfirmed" % (target.name, queued_note,
                                                                    size_label(drop.size), drop.detail), EXIT_FAIL)
    if drop.status == "pointer":
        record(ctx, send_entry(message, "queued", log_file))
        return ("queued for %s (%s) #%s; prompted a pointer instead: %s" % (
            target.name, shown, message.short, inbox.pointer_text(drop.waiting, drop.size, prefix=False)), EXIT_OK)
    if drop.status == "refused":
        if mode == "now":
            return _refused(ctx, target, message, drop.state, log_file)
        return _queued(ctx, target, message, drop.state, log_file)
    record(ctx, send_entry(message, "queued", log_file))
    return "failed %s: %s; #%s left queued" % (target.name, drop.detail, message.short), EXIT_FAIL


def _label(state: Optional[str], message: Message) -> str:
    parts = ([state] if state else []) + (["fyi"] if message.fyi else [])
    return " (%s)" % ", ".join(parts) if parts else ""


def _queued(ctx: Ctx, target: Target, message: Message, state: Optional[str],
            log_file: Optional[str]) -> Tuple[str, int]:
    if ctx.dry_run:
        return "would queue for %s%s" % (target.name, _label(state, message)), EXIT_OK
    record(ctx, send_entry(message, "queued", log_file))
    return "queued for %s%s #%s" % (target.name, _label(state, message), message.short), EXIT_OK


def _refused(ctx: Ctx, target: Target, message: Message, state: str, log_file: Optional[str]) -> Tuple[str, int]:
    """``--now`` to a seat that cannot take it: nothing sent, the file goes to ``cur/``."""
    if ctx.dry_run:
        return "would refuse %s (%s): nothing sent" % (target.name, state), EXIT_OK
    try:
        inbox.move(ctx.fleet.repo, message, CUR)
    except OSError as exc:
        ctx.warn("cannot move %s to cur/: %s" % (message.path, exc))
    record(ctx, send_entry(message, "refused", log_file))
    return "refused %s (%s): nothing sent" % (target.name, state), EXIT_FAIL


def _would_send(ctx: Ctx, target: Target, message: Message, state: str) -> str:
    now = inbox.now_utc()
    queued = [m for m in inbox.read_box(ctx.fleet.repo, target.name, NEW) if not inbox.is_due(m, now)]
    _, size, pointer = compose_prompt(queued, message)
    if pointer:
        return "would queue for %s (%s) and prompt a pointer: %s" % (
            target.name, state, inbox.pointer_text(queued + [message], size, prefix=False))
    return "would send to %s (%s)%s, %s" % (target.name, state, " with %d queued" % len(queued) if queued else "",
                                           size_label(size))


def _send_owner(ctx: Ctx, message: Message, mode: str, log_file: Optional[str]) -> Tuple[str, int]:
    """``owner``: queued, then the mailbox is shown as a notification (unless --queue, or
    outside herdr, where it stays queued for ``inbox --as owner``)."""
    target = Target(OWNER, None, None, "owner")
    if mode == "queue":
        return _queued(ctx, target, message, None, log_file)
    if not ctx.inside_herdr():
        return _queued(ctx, target, message, "not in herdr", log_file)
    if ctx.dry_run:
        return "would notify owner, %s" % size_label(inbox.size_of(inbox.direct_text(message))), EXIT_OK
    record(ctx, send_entry(message, "queued", log_file))
    with inbox.locked(ctx.fleet.repo, OWNER):
        drop = notify_owner(ctx)
    if drop.status == "sent":
        extra = len(drop.carried) - 1
        return "sent to owner (notification)%s, %s" % (" with %d queued" % extra if extra > 0 else "",
                                                       size_label(drop.size)), EXIT_OK
    return "queued for owner (%s) #%s" % (drop.detail or "not shown", message.short), EXIT_OK


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


def op_ack(ctx: Ctx, names: Sequence[str], all_owed: bool = False, as_name: Optional[str] = None) -> int:
    """Design 17.1 and 18.8: settle what the caller owes by hand. A name settles the
    caller's debt to that sender; an id (``#3f2a9c``) settles that one message. Appends
    one ``ack`` entry per debt; sends nothing to anyone. The caller is found as for
    ``send``; herdr is asked only when ``--as`` does not say."""
    if not names and not all_owed:
        raise UsageError("ack needs a name, a message id or --all")
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
    debts = [o for o in owed(entries) if o["to"] == caller]
    by_sender = {o["from"]: o for o in debts}
    open_ids = {item["id"]: item for _, item in unsettled(entries) if item["to"] == caller and item.get("id")}
    todo: List[Tuple[str, str]] = []
    for item in items + ([("name", o["from"]) for o in debts] if all_owed else []):
        if item not in todo:
            todo.append(item)
    if not todo:
        ctx.say("nothing owed by %s" % caller)
        return EXIT_OK
    for kind, value in todo:
        if kind == "id":
            debt = open_ids.get(value)
            if debt is None:
                ctx.say("nothing owed for #%s" % inbox.short_of(value))
                continue
            append(repo, ack_entry(caller, debt["from"], ident=value))
            ctx.say("acked #%s from %s since %s (%s)" % (inbox.short_of(value), debt["from"],
                                                         since_label(debt["since"]), debt["first_line"]))
            continue
        debt = by_sender.get(value)
        if debt is None:
            ctx.say("nothing owed to %s" % value)
            continue
        append(repo, ack_entry(caller, value))
        ctx.say("acked %s since %s (%s)" % (value, since_label(debt["since"]), debt["first_line"]))
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
