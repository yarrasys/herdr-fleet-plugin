"""The inbox (design 18.1 and 18.4): durable mailboxes under ``<repo>/.herdr/inbox``.

``<name>/new/<id>.md`` is a queued message and ``<name>/cur/<id>.md`` one that
was delivered, expired, dropped or refused; the location is the state and the
message log holds the timeline. ``<name>`` is a seat, a contact, ``owner`` or
another live agent's herdr name. An id is ``<UTC YYYYMMDDTHHMMSSZ>-<6 hex>``;
the hex part (``#3f2a9c``) names a message on the command line and is drawn
again while any file in the fleet's inbox tree already uses it.

A message file is ``key: value`` header lines, a blank line, then the body
exactly as the sender wrote it. Files are written atomically (temp file in the
same directory, then rename) and never modified; delivery is a rename from
``new/`` to ``cur/``. ``locked`` serialises deliveries to one mailbox, so two
commands flushing it at once never prompt the same message twice.

Pure file work: no herdr command and no message log here. ``messages`` and
``mailroom`` log what these functions move. Bodies are read only to render a
batch, a listing or a history (invariant 15).
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import os
import re
import secrets
import tempfile
from dataclasses import dataclass
from typing import Dict, Iterator, List, Optional, Sequence, Set, Tuple

from herdr_fleet.context import UsageError
from herdr_fleet.fleetfile import NAME_RE
from herdr_fleet.state import ensure_gitignore

INBOX = "inbox"
NEW, CUR = "new", "cur"
ID_RE = re.compile(r"^(\d{8}T\d{6}Z)-([0-9a-f]{6})$")
SHORT_RE = re.compile(r"^#?([0-9a-f]{6})$")
TTL_RE = re.compile(r"^([1-9][0-9]*)([mhd])$")
TTL_UNITS = {"m": 60, "h": 3600, "d": 86400}
BATCH_CAP = 16 * 1024  # bytes of rendered messages one prompt may carry (18.4)
LOCK_NAME = ".lock"
POINTER_COMMAND = "herdr-fleet inbox --read"


@dataclass
class Message:
    """One message file. ``path``, ``box`` and ``mtime_ns`` describe where it lies now."""

    id: str
    sender: str
    to: str
    time: str
    body: str
    fyi: bool = False
    reply_to: Optional[str] = None
    ttl: Optional[str] = None
    file: Optional[str] = None
    path: str = ""
    box: str = NEW
    mtime_ns: int = 0

    @property
    def short(self) -> str:
        return short_of(self.id)

    def sort_key(self) -> Tuple[str, int, str]:
        """Oldest first: the id's second, then the write time (two messages in the same
        second keep the order they were written in), then the id."""
        return (self.id.split("-", 1)[0], self.mtime_ns, self.id)


def short_of(message_id: str) -> str:
    return message_id.rsplit("-", 1)[-1]


# -- time --------------------------------------------------------------------------------


def now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def iso_local(when: datetime.datetime) -> str:
    """Local time with offset, as every timestamp herdr-fleet writes."""
    return when.astimezone().isoformat(timespec="seconds")


def parse_time(text: object) -> Optional[datetime.datetime]:
    """An ISO timestamp as an aware datetime (naive ones are UTC); None when unparseable."""
    try:
        when = datetime.datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return when


def time_label(text: str, today: Optional[datetime.date] = None) -> str:
    """``10:31`` for today (local time), else ``2026-10-19 10:31``."""
    when = parse_time(text)
    if when is None:
        return str(text)
    local = when.astimezone()
    today = today or datetime.datetime.now().astimezone().date()
    return local.strftime("%H:%M") if local.date() == today else local.strftime("%Y-%m-%d %H:%M")


def parse_ttl(text: str) -> datetime.timedelta:
    """``30m``, ``2h`` or ``1d``: a positive integer and a unit. ValueError otherwise."""
    match = TTL_RE.match(text.strip())
    if not match:
        raise ValueError("--ttl takes a positive integer with m, h or d (30m, 2h, 1d), not %r" % text)
    return datetime.timedelta(seconds=int(match.group(1)) * TTL_UNITS[match.group(2)])


def is_due(message: Message, now: datetime.datetime) -> bool:
    """True when the message has a ttl and it has passed."""
    if not message.ttl:
        return False
    limit = parse_time(message.ttl)
    return limit is not None and limit <= now


# -- paths and ids ------------------------------------------------------------------------


def inbox_dir(repo: str) -> str:
    return os.path.join(repo, ".herdr", INBOX)


def box_dir(repo: str, name: str, box: str) -> str:
    if not NAME_RE.match(name):  # names become directories: never a path
        raise UsageError("not a mailbox name: %r" % name)
    return os.path.join(inbox_dir(repo), name, box)


def mailboxes(repo: str) -> List[str]:
    """Every mailbox directory under ``.herdr/inbox``, sorted."""
    folder = inbox_dir(repo)
    if not os.path.isdir(folder):
        return []
    return sorted(n for n in os.listdir(folder) if NAME_RE.match(n) and os.path.isdir(os.path.join(folder, n)))


def _files(folder: str) -> List[str]:
    if not os.path.isdir(folder):
        return []
    return [n for n in os.listdir(folder) if n.endswith(".md") and ID_RE.match(n[:-3])]


def has_queued(repo: str, name: str) -> bool:
    """True when the mailbox's ``new/`` holds at least one message file (no parsing)."""
    return bool(NAME_RE.match(name)) and bool(_files(os.path.join(inbox_dir(repo), name, NEW)))


def taken_shorts(repo: str) -> Set[str]:
    """The hex parts already used anywhere in the fleet's inbox tree."""
    taken: Set[str] = set()
    for name in mailboxes(repo):
        for box in (NEW, CUR):
            taken.update(short_of(f[:-3]) for f in _files(os.path.join(inbox_dir(repo), name, box)))
    return taken


def new_id(now: datetime.datetime, taken: Set[str]) -> str:
    """``20261020T230900Z-3f2a9c``; the hex part is drawn again while ``taken`` has it.
    The new hex part is added to ``taken``."""
    stamp = now.astimezone(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    while True:
        hexpart = secrets.token_hex(3)
        if hexpart not in taken:
            taken.add(hexpart)
            return "%s-%s" % (stamp, hexpart)


# -- files ----------------------------------------------------------------------------------


def _one_line(value: str) -> str:
    return " ".join(str(value).splitlines())


def render_file(message: Message) -> str:
    """Header lines, a blank line, the body verbatim (design 18.1)."""
    lines = ["id: %s" % message.id, "from: %s" % message.sender, "to: %s" % message.to,
             "time: %s" % message.time, "fyi: %s" % ("true" if message.fyi else "false")]
    for key, value in (("reply_to", message.reply_to), ("ttl", message.ttl), ("file", message.file)):
        if value:
            lines.append("%s: %s" % (key, _one_line(value)))
    return "\n".join(lines) + "\n\n" + message.body


def parse_file(text: str, path: str = "", box: str = NEW) -> Message:
    """The inverse of ``render_file``; ValueError when the header is incomplete."""
    head, sep, body = text.partition("\n\n")
    if not sep:
        raise ValueError("no blank line after the header")
    fields: Dict[str, str] = {}
    for line in head.splitlines():
        key, colon, value = line.partition(":")
        if colon:
            fields[key.strip()] = value.strip()
    missing = [k for k in ("id", "from", "to", "time") if not fields.get(k)]
    if missing:
        raise ValueError("header lacks %s" % ", ".join(missing))
    return Message(id=fields["id"], sender=fields["from"], to=fields["to"], time=fields["time"], body=body,
                   fyi=fields.get("fyi", "").lower() == "true", reply_to=fields.get("reply_to") or None,
                   ttl=fields.get("ttl") or None, file=fields.get("file") or None, path=path, box=box)


def write(repo: str, message: Message, box: str = NEW) -> Message:
    """Write a new message file atomically into ``box`` of its mailbox; returns it with
    its path. Raises OSError (the caller reports it and prompts nothing)."""
    folder = box_dir(repo, message.to, box)
    os.makedirs(folder, exist_ok=True)
    ensure_gitignore(os.path.join(repo, ".herdr"))
    path = os.path.join(folder, message.id + ".md")
    if os.path.lexists(path):
        raise OSError("message file %s already exists" % path)
    fd, tmp = tempfile.mkstemp(prefix=".msg.", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(render_file(message))
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    message.path, message.box, message.mtime_ns = path, box, os.stat(path).st_mtime_ns
    return message


def load(path: str, box: str) -> Message:
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        message = parse_file(handle.read(), path, box)
    message.mtime_ns = os.stat(path).st_mtime_ns
    return message


def read_box(repo: str, name: str, box: str = NEW, warnings: Optional[List[str]] = None) -> List[Message]:
    """The messages of one box, oldest first. Unreadable files are skipped with a warning."""
    folder = box_dir(repo, name, box)
    found = []
    for filename in _files(folder):
        path = os.path.join(folder, filename)
        try:
            found.append(load(path, box))
        except (OSError, ValueError) as exc:
            if warnings is not None:
                warnings.append("%s: unreadable message skipped (%s)" % (path, exc))
    return sorted(found, key=Message.sort_key)


def all_messages(repo: str, warnings: Optional[List[str]] = None) -> List[Message]:
    """Every message in the tree, both boxes of every mailbox, oldest first."""
    found: List[Message] = []
    for name in mailboxes(repo):
        for box in (NEW, CUR):
            found.extend(read_box(repo, name, box, warnings))
    return sorted(found, key=Message.sort_key)


def move(repo: str, message: Message, box: str) -> Message:
    """Move a message file to ``box`` of its mailbox (a rename; the file is never rewritten)."""
    folder = box_dir(repo, message.to, box)
    os.makedirs(folder, exist_ok=True)
    target = os.path.join(folder, os.path.basename(message.path))
    os.rename(message.path, target)
    message.path, message.box = target, box
    return message


@contextlib.contextmanager
def locked(repo: str, name: str) -> Iterator[None]:
    """Hold the mailbox's lock while its queued files are read, prompted and moved."""
    folder = os.path.dirname(box_dir(repo, name, NEW))
    os.makedirs(folder, exist_ok=True)
    ensure_gitignore(os.path.join(repo, ".herdr"))
    fd = os.open(os.path.join(folder, LOCK_NAME), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def resolve(repo: str, ref: str) -> Message:
    """The message a command-line id names: a full id, or its hex part with or without
    ``#``. Unknown and ambiguous references are usage errors (the latter lists the ids)."""
    ref = ref.strip()
    short = SHORT_RE.match(ref)
    if ID_RE.match(ref):
        matches = [m for m in all_messages(repo) if m.id == ref]
    elif short:
        matches = [m for m in all_messages(repo) if m.short == short.group(1)]
    else:
        raise UsageError("not a message id: %r (use the full id or #<6 hex>)" % ref)
    if not matches:
        raise UsageError("no message %s in %s" % ("#" + short.group(1) if short else ref, inbox_dir(repo)))
    if len(matches) > 1:
        raise UsageError("%s matches several messages: %s" % (ref, ", ".join(
            "%s (%s/%s)" % (m.id, m.to, m.box) for m in matches)))
    return matches[0]


def looks_like_id(ref: str) -> bool:
    return bool(ID_RE.match(ref.strip()) or SHORT_RE.match(ref.strip()))


# -- rendering --------------------------------------------------------------------------------


def size_of(text: str) -> int:
    return len(text.encode("utf-8"))


def direct_text(message: Message) -> str:
    """A message delivered on its own: ``[from X #3f2a9c] body``, or the body as written
    when it already starts with ``[from`` (design 16.1)."""
    if message.body.lstrip().startswith("[from"):
        return message.body
    return "[from %s #%s] %s" % (message.sender, message.short, message.body)


def item_header(message: Message, direction: bool = False, today: Optional[datetime.date] = None) -> str:
    """``[from fablyn-orchestrator at 10:09 #3f2a9c re #91be02]``; with ``direction`` the
    recipient too (``--history``)."""
    who = "%s to %s" % (message.sender, message.to) if direction else message.sender
    reply = " re #%s" % short_of(message.reply_to) if message.reply_to else ""
    return "[from %s at %s #%s%s]" % (who, time_label(message.time, today), message.short, reply)


def render_batch(messages: Sequence[Message], direction: bool = False,
                 today: Optional[datetime.date] = None) -> str:
    """Each message as its header line and its body, joined by blank lines (design 18.4)."""
    return "\n\n".join("%s\n%s" % (item_header(m, direction, today), m.body.rstrip("\n")) for m in messages)


def pointer_text(messages: Sequence[Message], size: int, prefix: bool = True,
                 today: Optional[datetime.date] = None) -> str:
    """What a prompt says instead of a batch above BATCH_CAP; the files stay queued."""
    count = len(messages)
    oldest = min(messages, key=Message.sort_key)
    text = "%d message%s waiting (oldest %s, %s); run: %s" % (
        count, "" if count == 1 else "s", time_label(oldest.time, today), size_label(size), POINTER_COMMAND)
    return "[from herdr-fleet] " + text if prefix else text


def size_label(size: int) -> str:
    return "%d B" % size if size < 1000 else "%.1f KB" % (size / 1000.0)


# -- summaries ----------------------------------------------------------------------------------


def queued(repo: str, now: Optional[datetime.datetime] = None) -> Dict[str, List[Message]]:
    """Queued messages per mailbox, without those past their ttl (read-only: nothing moves)."""
    now = now or now_utc()
    found: Dict[str, List[Message]] = {}
    for name in mailboxes(repo):
        waiting = [m for m in read_box(repo, name, NEW) if not is_due(m, now)]
        if waiting:
            found[name] = waiting
    return found
