"""Messages (design 16.1 to 16.3, 17.1): ``send``, ``ack``, the message log and
owed replies.

herdr-fleet is a messenger only: it never reads or interprets an agent's
output, never routes replies and never decides who gets a message.

- ``send`` prompts each target once with ``[from <sender>] <text>`` through
  ``seats.safe_prompt``. Unlike every other command it may reach a ``working``
  agent (herdr queues the text for its next turn); ``blocked``, ``unknown``
  and absent targets are refused, and a stalled or timed-out prompt is never
  resent.
- The log is ``<repo>/.herdr/messages.jsonl``: one JSON object per ``send``
  or kickoff that herdr accepted (``"delivery": "sent"``) or that may have
  arrived (``"delivery": "unconfirmed"``: the prompt stalled or timed out),
  appended with ``O_APPEND``. Refused sends are never logged.
- Owed replies are derived from the log alone (``owed``). A message sent with
  ``--fyi``, or whose text starts with ``FYI:`` or ``FYI `` after the prefix,
  is logged with ``"fyi": true`` and never creates an obligation, though it
  still answers one. ``ack`` appends ``kind = "ack"`` entries that settle what
  the caller owes and sends nothing to anyone (invariant 10).
"""

from __future__ import annotations

import datetime
import json
import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from herdr_fleet import contacts as contact_mod
from herdr_fleet.context import EXIT_FAIL, EXIT_OK, EXIT_PARTIAL, Ctx, UsageError, preflight
from herdr_fleet.fleetfile import NAME_RE
from herdr_fleet.herdr import HerdrError
from herdr_fleet.seats import SEND_ALLOWED, SETTLED, safe_prompt
from herdr_fleet.state import ensure_gitignore, now_iso

LOG_NAME = "messages.jsonl"
FIRST_LINE_MAX = 120
PLUGIN_SENDER = "herdr-fleet"
OWNER = "owner"
PREFIX_RE = re.compile(r"^\[from [^\]]*\]\s*")
FYI_RE = re.compile(r"^fyi[: ]", re.IGNORECASE)


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


def ack_entry(caller: str, seat: str, time: Optional[str] = None) -> Dict:
    """Design 17.1: one hand-made acknowledgement; nothing is sent to anyone."""
    return {"time": time or now_iso(), "from": caller, "to": seat, "kind": "ack", "bytes": 0,
            "first_line": "ack", "delivery": "logged"}


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


def _when(entry: Dict) -> Optional[datetime.datetime]:
    try:
        when = datetime.datetime.fromisoformat(str(entry["time"]))
    except (ValueError, KeyError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return when


def owed(entries: Sequence[Dict]) -> List[Dict]:
    """Design 16.3 as amended by 17.1: for each ordered pair (A, B), the latest
    ``send`` from A to B that is not FYI is awaiting unless an entry from B to A of
    kind ``send`` (FYI or not) or ``ack`` has a later time. An FYI never creates an
    obligation but still settles one. Kickoffs come from the plugin and never
    create or settle anything. Times have one-second resolution, so equal times
    are ordered by position in the append-only log. Returns the awaiting
    messages, oldest first, as {from, to, since, first_line}."""
    Key = Tuple[datetime.datetime, int]
    asks: Dict[Tuple[str, str], Tuple[Key, Dict]] = {}
    answers: Dict[Tuple[str, str], Key] = {}
    for index, entry in enumerate(entries):
        when = _when(entry)
        kind = entry.get("kind", "send")
        if kind not in ("send", "ack") or when is None:
            continue
        pair, key = (str(entry["from"]), str(entry["to"])), (when, index)
        if pair not in answers or key > answers[pair]:
            answers[pair] = key
        if kind == "send" and not entry.get("fyi") and (pair not in asks or key > asks[pair][0]):
            asks[pair] = (key, entry)
    awaiting = []
    for (sender, recipient), (key, entry) in asks.items():
        reply = answers.get((recipient, sender))
        if reply is None or reply < key:
            awaiting.append((key, {"from": sender, "to": recipient, "since": entry["time"],
                                   "first_line": entry.get("first_line", "")}))
    return [item for _, item in sorted(awaiting, key=lambda pair: pair[0])]


def since_label(time: str, today: Optional[datetime.date] = None) -> str:
    """``10:31`` for today (local time), else ``2026-10-19 10:31``."""
    when = _when({"time": time})
    if when is None:
        return time
    local = when.astimezone()
    today = today or datetime.datetime.now().astimezone().date()
    return local.strftime("%H:%M") if local.date() == today else local.strftime("%Y-%m-%d %H:%M")


def owed_lines(items: Sequence[Dict], today: Optional[datetime.date] = None) -> List[str]:
    return ["awaiting: %s owes %s since %s (%s)" % (o["to"], o["from"], since_label(o["since"], today),
                                                    o["first_line"]) for o in items]


def format_entry(entry: Dict) -> str:
    """``time  from → to  bytes  first_line``; ``?`` after the bytes marks an unconfirmed
    delivery, ``[fyi]`` before the first line an FYI, and an acknowledgement reads
    ``time  from → to  ack``."""
    if entry.get("kind") == "ack":
        return "%s  %s → %s  ack" % (entry["time"], entry["from"], entry["to"])
    unconfirmed = "?" if entry.get("delivery") == "unconfirmed" else ""
    tag = "[fyi]  " if entry.get("fyi") else ""
    return "%s  %s → %s  %s%s  %s%s" % (entry["time"], entry["from"], entry["to"], entry.get("bytes", "?"),
                                       unconfirmed, tag, entry.get("first_line", ""))


# -- send -----------------------------------------------------------------------------------


@dataclass
class Target:
    name: str                    # as given: seat, contact or live agent name
    herdr: Optional[str]         # what `agent prompt` gets: a name or a pane id
    problem: Optional[str] = None


def size_label(size: int) -> str:
    return "%d B" % size if size < 1000 else "%.1f KB" % (size / 1000.0)


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
    """``--as``, else the named agent in the caller's pane, else ``owner``."""
    if as_name:
        if not NAME_RE.match(as_name):
            raise UsageError("--as %r must match [a-z][a-z0-9_-]{0,31}" % as_name)
        return as_name
    pane = ctx.invocation.pane_id
    name = ctx.herdr.pane_agent(pane).get("name") if pane else None
    return str(name) if name else OWNER


def resolve_targets(ctx: Ctx, names: Sequence[str], agents: Sequence[Dict]) -> List[Target]:
    """Seats, contacts and live agent names; unknown names are a usage error."""
    fleet = ctx.fleet
    live = {str(a["name"]): a for a in agents if a.get("name")}
    targets, unknown = [], []
    for name in names:
        contact = fleet.contact(name)
        if fleet.seat(name) is not None:
            targets.append(Target(name, name, None if name in live else "absent"))
        elif contact is not None:
            res = contact_mod.resolve(contact, agents)
            targets.append(Target(name, res.pane, res.problem() and "%s: %s" % (res.state, res.problem())))
        elif name in live:
            targets.append(Target(name, name))
        else:
            unknown.append(name)
    if unknown:
        raise UsageError("not a seat, contact or live agent: %s" % ", ".join(unknown))
    return targets


def op_send(ctx: Ctx, words: Sequence[str], text_file: Optional[str] = None, as_name: Optional[str] = None,
            when_ready_ms: Optional[int] = None, all_seats: bool = False, fyi: bool = False) -> int:
    """Design 16.1 and 17.1. One line per target; exit 0 all sent, 3 some refused, 1 herdr failure."""
    names, text = message_text(words, text_file, all_seats)
    sender = resolve_sender(ctx, as_name)
    agents = ctx.herdr.agent_list()
    if all_seats:
        live = {a.get("name") for a in agents}
        names += [s.name for s in ctx.fleet.seats if s.name in live and s.name != sender and s.name not in names]
    if not names:
        if all_seats:
            ctx.say("nothing sent: no live seat besides %s" % sender)
            return EXIT_PARTIAL
        raise UsageError("send needs a target and a text, or --file")
    targets = resolve_targets(ctx, names, agents)
    message = text if text.lstrip().startswith("[from") else "[from %s] %s" % (sender, text)
    fyi = fyi or is_fyi(message)
    size = size_label(len(message.encode("utf-8")))
    codes = []
    for target in targets:
        line, code, delivery = _deliver(ctx, target, message, size, when_ready_ms)
        ctx.say(line)
        codes.append(code)
        if delivery and not ctx.dry_run:  # sent, or possibly delivered: log it (never refusals)
            entry = make_entry(sender, target.name, "send", message,
                               os.path.abspath(text_file) if text_file else None, delivery=delivery, fyi=fyi)
            try:
                append(ctx.fleet.repo, entry)
            except OSError as exc:
                ctx.warn("sent, but cannot append to %s: %s" % (log_path(ctx.fleet.repo), exc))
    if EXIT_FAIL in codes:
        return EXIT_FAIL
    return EXIT_PARTIAL if EXIT_PARTIAL in codes else EXIT_OK


def _deliver(ctx: Ctx, target: Target, message: str, size: str,
             when_ready_ms: Optional[int]) -> Tuple[str, int, Optional[str]]:
    """(output line, exit code, delivery to log: "sent", "unconfirmed" or None)."""
    if target.problem or not target.herdr:
        return "refused %s (%s): nothing sent" % (target.name, target.problem or "absent"), EXIT_PARTIAL, None
    status = _status(ctx, target.herdr)
    if when_ready_ms and status not in SETTLED and status != "absent" and ctx.dry_run:
        return ("would wait up to %d ms for %s (%s), then send %s" % (when_ready_ms, target.name, status, size),
                EXIT_OK, None)
    if when_ready_ms and status not in SETTLED and status != "absent":
        try:
            ctx.herdr.agent_wait(target.herdr, ("idle", "done"), when_ready_ms)
        except HerdrError:
            pass
        status = _status(ctx, target.herdr)
        if status not in SETTLED:
            return ("refused %s (%s after --when-ready %d ms): nothing sent" % (target.name, status, when_ready_ms),
                    EXIT_PARTIAL, None)
    if status not in SEND_ALLOWED:
        return "refused %s (%s): nothing sent" % (target.name, status), EXIT_PARTIAL, None
    if ctx.dry_run:
        return "would send to %s (%s), %s" % (target.name, status, size), EXIT_OK, None
    outcome = safe_prompt(ctx.herdr, target.herdr, message, SEND_ALLOWED)
    if outcome.ok:
        return ("sent to %s (%s), %s" % (target.name, outcome.agent.get("agent_status") or status, size),
                EXIT_OK, "sent")
    if outcome.status in ("blocked", "unknown", "absent"):  # herdr sent nothing
        return "refused %s (%s): nothing sent" % (target.name, outcome.status), EXIT_PARTIAL, None
    if outcome.status == "unconfirmed":  # stalled or timed out: may have arrived; logged, never resent
        return ("unconfirmed %s, %s: %s; logged as unconfirmed" % (target.name, size, outcome.detail),
                EXIT_FAIL, "unconfirmed")
    return "failed %s: %s" % (target.name, outcome.detail), EXIT_FAIL, None


def _status(ctx: Ctx, target: str) -> str:
    try:
        return str(ctx.herdr.agent_get(target).get("agent_status") or "unknown")
    except HerdrError as exc:
        if "not_found" in exc.code:
            return "absent"
        raise


# -- ack ------------------------------------------------------------------------------------------


def op_ack(ctx: Ctx, names: Sequence[str], all_owed: bool = False, as_name: Optional[str] = None) -> int:
    """Design 17.1: settle what the caller owes by hand. Appends one ``ack`` entry per
    seat that the caller owes a reply; sends nothing to anyone. The caller is found
    as for ``send``; herdr is asked only when ``--as`` does not say."""
    if not names and not all_owed:
        raise UsageError("ack needs a name or --all")
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
    unknown = [n for n in names if n not in known]
    if unknown:
        raise UsageError("not a seat, contact or name in the message log: %s" % ", ".join(unknown))
    debts = [o for o in owed(entries) if o["to"] == caller]
    by_sender = {o["from"]: o for o in debts}
    targets: List[str] = []
    for name in list(names) + ([o["from"] for o in debts] if all_owed else []):
        if name not in targets:
            targets.append(name)
    if not targets:
        ctx.say("nothing owed by %s" % caller)
        return EXIT_OK
    for name in targets:
        item = by_sender.get(name)
        if item is None:
            ctx.say("nothing owed to %s" % name)
            continue
        append(repo, ack_entry(caller, name))
        ctx.say("acked %s since %s (%s)" % (name, since_label(item["since"]), item["first_line"]))
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
