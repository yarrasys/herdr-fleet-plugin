"""Destination-owned routes and validated terminal receipts (design section 31).

New sends discover a unique named seat's existing fleet from authoritative cwd
metadata. Their sole pending file lives there; the source keeps nonpending history.
Sorted root locks reserve ids before the destination mailbox lock. Terminal receipts
validate both roots and paired headers without acquiring root locks or prompting.

The legacy mirror helpers remain for already-pending source queues (section 20.8).
They never migrate or replay stored messages into a new destination queue.
"""

from __future__ import annotations

import contextlib
import copy
import fcntl
import os
from typing import Dict, Mapping, Optional, Sequence

from herdr_fleet import inbox
from herdr_fleet.fleetfile import FLEET_DIR, FLEET_NAME
from herdr_fleet.inbox import CUR, Message


def fleet_root(start: object) -> Optional[str]:
    """The repository root of the first fleet at or above ``start``, or None."""
    if not isinstance(start, str) or not start or not os.path.isdir(start):
        return None
    folder = os.path.realpath(start)
    while True:
        if os.path.isfile(os.path.join(folder, FLEET_DIR, FLEET_NAME)):
            return folder
        parent = os.path.dirname(folder)
        if parent == folder:
            return None
        folder = parent


def other_fleet(agent: Optional[Mapping], own_repo: str) -> Optional[str]:
    """The other fleet's repository for a live agent record: from ``cwd``, then
    ``foreground_cwd``; None when neither leads to a fleet, or it is this one."""
    if not agent:
        return None
    own = os.path.realpath(own_repo)
    for key in ("cwd", "foreground_cwd"):
        root = fleet_root(agent.get(key))
        if root is None:
            continue
        return None if os.path.realpath(root) == own else root
    return None


def find(agents: Sequence[Mapping], name: str, own_repo: str) -> Optional[str]:
    """``other_fleet`` for the live agent named ``name``."""
    for agent in agents:
        if agent.get("name") == name:
            return other_fleet(agent, own_repo)
    return None


def _herdr_dir(repo: str) -> Optional[str]:
    folder = os.path.join(repo, FLEET_DIR)
    return folder if os.path.isdir(folder) else None


def mirror_entry(repo: str, entry: Dict) -> None:
    """Append one log entry to the other fleet's message log (only when its ``.herdr/``
    exists). Raises OSError; the caller turns it into a warning."""
    from herdr_fleet.messages import append  # the log's own O_APPEND writer
    if _herdr_dir(repo) is None:
        raise OSError("%s has no %s directory" % (repo, FLEET_DIR))
    append(repo, entry)


def mirror_body(repo: str, message: Message) -> None:
    """Store the message file in the other fleet's ``inbox/<to>/cur/`` (atomically, as
    ``inbox.write``), unless a file with that id is already there."""
    if _herdr_dir(repo) is None:
        raise OSError("%s has no %s directory" % (repo, FLEET_DIR))
    target = os.path.join(inbox.box_dir(repo, message.to, CUR), message.id + ".md")
    if os.path.lexists(target):
        return
    inbox.write(repo, copy.copy(message), CUR)

# -- destination-owned routes (design 31); legacy mirrors above remain for old queues --


def validated_fleet(repo: str):
    """An existing real fleet root; never create/traverse a redirected metadata directory."""
    from herdr_fleet.fleetfile import FleetError, load_fleet
    if (not isinstance(repo, str) or not os.path.isabs(repo) or any(c in repo for c in "\r\n\0")
            or os.path.realpath(repo) != repo):
        raise OSError("fleet root is not an absolute real directory")
    folder = os.path.join(repo, FLEET_DIR)
    config = os.path.join(folder, FLEET_NAME)
    if (not os.path.isdir(folder) or os.path.realpath(folder) != folder
            or not os.path.isfile(config) or os.path.realpath(config) != config):
        raise OSError("fleet metadata directory/config is missing or redirected")
    log = os.path.join(folder, "messages.jsonl")
    if os.path.lexists(log) and (os.path.islink(log) or not os.path.isfile(log)):
        raise OSError("fleet message log is redirected or not a file")
    try:
        return load_fleet(config)
    except FleetError as exc:
        raise OSError("fleet configuration is invalid: %s" % exc)


def safe_mailbox(repo: str, name: str) -> None:
    from herdr_fleet.fleetfile import NAME_RE
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise OSError("unsafe mailbox name")
    for parts in (("inbox",), ("inbox", name), ("inbox", name, "new"), ("inbox", name, "cur")):
        path = os.path.join(repo, FLEET_DIR, *parts)
        if os.path.lexists(path) and (os.path.islink(path) or not os.path.isdir(path)):
            raise OSError("mailbox directory is redirected or not a directory")


def destination(agent: Optional[Mapping], agents: Sequence[Mapping], own_repo: str):
    """(validated fleet, canonical name, fallback reason), from live metadata only."""
    from herdr_fleet.fleetfile import FleetError, NAME_RE
    if not agent:
        return None, None, "missing or ambiguous live target"
    name = agent.get("name")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        return None, None, "live target has no usable canonical name"
    if len([a for a in agents if a.get("name") == name]) != 1:
        return None, None, "canonical live agent name is ambiguous"
    # Later receipts authenticate the origin through its conventional fleet.
    # A custom selected source file remains usable when that proof is absent;
    # keep its ordinary source-owned queue instead of choosing an invalid route.
    try:
        validated_fleet(os.path.realpath(own_repo))
    except (OSError, FleetError, ValueError) as exc:
        return None, None, "source fleet root is ineligible for automatic routing: %s" % exc
    root = other_fleet(agent, own_repo)
    if root is None:
        return None, None, "target has no different existing fleet (same-root or missing)"
    root = os.path.realpath(root)
    try:
        fleet = validated_fleet(root)
        if fleet.seat(name) is None:
            return None, None, "destination does not declare canonical agent %s as a seat" % name
        safe_mailbox(root, name)
        return fleet, name, None
    except (OSError, FleetError, ValueError) as exc:
        return None, None, "destination fleet is unavailable or invalid: %s" % exc


@contextlib.contextmanager
def reserved(roots):
    """ID reservation across sorted existing roots, before any mailbox lock.

    Receipt mirroring never takes these directory locks while holding a mailbox
    lock, so concurrent reverse routes and timer delivery cannot invert them.
    """
    handles = []
    try:
        for root in sorted(set(roots)):
            validated_fleet(root)
            fd = os.open(os.path.join(root, FLEET_DIR), os.O_RDONLY)
            handles.append(fd)
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        for fd in reversed(handles):
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def receipt_matches(actual: Message, expected: Message) -> bool:
    return all(getattr(actual, key) == getattr(expected, key) for key in (
        "id", "sender", "to", "time", "fyi", "reply_to", "ttl", "queued_by", "priority",
        "origin_fleet", "destination_fleet", "requested_to", "route_bytes", "origin_send_order", "destination_send_order"))


def counterpart(repo: str, message: Message):
    """Validate both existing fleets and the paired immutable headers before mirroring.

    A path in a header alone is never authority to create a directory or append a
    log. The existing source receipt and destination message must agree exactly.
    """
    from herdr_fleet.fleetfile import NAME_RE
    if (not inbox.ID_RE.fullmatch(message.id) or not NAME_RE.fullmatch(message.sender)
            or not message.requested_to or not NAME_RE.fullmatch(message.requested_to)
            or any(type(getattr(message, field)) is not int or getattr(message, field) < 0
                   for field in ("route_bytes", "origin_send_order", "destination_send_order"))):
        raise OSError("invalid route identity metadata")
    origin, destination_root = message.origin_fleet, message.destination_fleet
    if not origin or not destination_root or origin == destination_root:
        raise OSError("incomplete route origin/destination")
    validated_fleet(origin)
    validated_fleet(destination_root)
    if os.path.realpath(repo) not in (origin, destination_root):
        raise OSError("route does not belong to this fleet")
    for root in (origin, destination_root):
        safe_mailbox(root, message.to)
    source_path = os.path.join(inbox.box_dir(origin, message.to, CUR), message.id + ".md")
    if os.path.islink(source_path):
        raise OSError("redirected source receipt")
    receipt = inbox.load_headers(source_path, CUR)
    if not receipt.history_only or not receipt_matches(receipt, message):
        raise OSError("source receipt metadata does not match route")
    found = []
    for box in (inbox.NEW, CUR):
        path = os.path.join(inbox.box_dir(destination_root, message.to, box), message.id + ".md")
        if os.path.lexists(path):
            if os.path.islink(path):
                raise OSError("redirected destination message")
            actual = inbox.load_headers(path, box)
            if actual.history_only or not receipt_matches(actual, message):
                raise OSError("destination message metadata does not match route")
            found.append(actual)
    if len(found) != 1:
        raise OSError("route lacks one authoritative destination message")
    return destination_root if os.path.realpath(repo) == origin else origin


def queued_entry(message: Message, repo: str, first_line: str = "") -> Dict:
    from herdr_fleet.messages import send_entry
    entry = send_entry(message, "queued", message.file)
    entry["time"] = message.time
    if type(message.route_bytes) is int and message.route_bytes >= 0:
        entry["bytes"] = message.route_bytes
    order = message.origin_send_order if os.path.realpath(repo) == message.origin_fleet else message.destination_send_order
    if type(order) is int and order >= 0:
        entry["route_enqueue_order"] = order
    if first_line:
        entry["first_line"] = first_line
    return entry


def append_once(repo: str, entry: Dict) -> None:
    from herdr_fleet.messages import append, read
    if not any(e.get("kind") == entry.get("kind") and e.get("id") == entry.get("id") for e in read(repo)):
        append(repo, entry)


def terminal(ctx, entry: Dict) -> None:
    """Mirror validated terminal/ack receipts, never queue or prompt from this path."""
    if (entry.get("kind") == "send" and entry.get("reply_to") and not entry.get("origin_fleet")
            and entry.get("delivery") in ("sent", "unconfirmed", "queued")):
        # Owner and declared local seats remain local. An exact reply still
        # settles its original routed request in the other fleet, without
        # routing/copying this reply body or inventing a second pending message.
        message = metadata(ctx.fleet.repo, str(entry["reply_to"]))
        if message is not None:
            from herdr_fleet.messages import ack_entry
            other = counterpart(ctx.fleet.repo, message)
            receipt = ack_entry(message.to, message.sender, ident=message.id, by=str(entry["from"]))
            receipt.update(via="reply", reply_id=entry.get("id"), reply_fleet=ctx.fleet.repo)
            append_once(other, receipt)
        return
    if entry.get("kind") not in ("delivered", "expired", "dropped", "ack") or not entry.get("id"):
        return
    if not entry.get("origin_fleet") and entry.get("kind") != "ack":
        return
    repo = ctx.fleet.repo
    message = metadata(repo, str(entry["id"]))
    if message is None:
        return
    other = counterpart(repo, message)
    # A failed initial source log may be repaired before its terminal receipt.
    # The immutable file timestamp remains the send time, never the repair time.
    append_once(other, queued_entry(message, other, str(entry.get("first_line") or "")))
    append_once(other, entry)


def local_source(ctx, entry: Dict) -> None:
    """Repair a missing queued send before recording consumption of a routed file."""
    if entry.get("kind") not in ("delivered", "expired", "dropped") or not entry.get("origin_fleet"):
        return
    message = metadata(ctx.fleet.repo, str(entry.get("id") or ""))
    if message is not None:
        append_once(ctx.fleet.repo, queued_entry(message, ctx.fleet.repo, str(entry.get("first_line") or "")))


def metadata(repo: str, ident: str) -> Optional[Message]:
    if not inbox.ID_RE.fullmatch(ident):
        return None
    found = []
    for name in inbox.mailboxes(repo):
        for box in (inbox.NEW, CUR):
            path = os.path.join(inbox.box_dir(repo, name, box), ident + ".md")
            if os.path.islink(path):
                raise OSError("redirected message receipt")
            if os.path.isfile(path):
                message = inbox.load_headers(path, box)
                if message.id != ident:
                    raise OSError("message header id does not match receipt filename")
                found.append(message)
    routed = [message for message in found if message.origin_fleet]
    if len(routed) > 1:
        raise OSError("ambiguous route receipt")
    return routed[0] if routed else None
