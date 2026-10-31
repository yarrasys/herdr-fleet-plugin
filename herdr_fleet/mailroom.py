"""The ``deliver`` and ``inbox`` commands (design 18.5 items 3 and 4).

- ``deliver`` hands queued messages to seats that can take them now: an idle or
  done seat gets its mailbox as one batch through ``seats.safe_prompt``; a
  working seat gets it typed ahead only with ``deliver NAME --now`` (design
  21.1); otherwise, and for blocked, unknown or absent seats, the messages stay
  queued, as they do while the owner is typing in the seat's pane (the presence
  guard, 21.2). ``owner`` gets a notification.
- ``inbox`` is the pull side: the caller's own mailbox, listed with positions,
  read (printed, long bodies cut at ``inbox_print_max`` unless ``--full``, and
  moved to ``cur/``), waited for (``--wait``: a filesystem poll every second
  until something arrives, then ``--read``; design 23.2; ``waiting.json`` says so
  while it waits, 32.4), counted, or replayed
  (``--history``: ``cur/`` files and log-only sends merged);
  ``--redeliver`` moves a message from ``cur/`` back to ``new/``. It runs no
  herdr command except the caller lookup, and none with ``--as``.
- ``inbox --hook stop|prompt`` (design 18.7) is the same pull from inside a
  Claude Code hook: the seat is found from ``HERDR_PANE_ID`` with ``agent get``
  (its only herdr command), ``stop`` hands the batch back as
  ``additionalContext`` and moves it to ``cur/``, ``prompt`` only counts. Every
  error path prints nothing and exits 0 (invariant 14).

Both expire messages past their ttl before anything else (logged ``expired``)
and log one ``delivered`` entry per message they hand over.
"""

from __future__ import annotations

import io
import json
import os
from typing import Dict, List, Optional, Sequence, TextIO, Tuple

from herdr_fleet import contacts as contact_mod
from herdr_fleet import crossfleet, inbox, seen
from herdr_fleet.context import EXIT_FAIL, EXIT_OK, EXIT_PARTIAL, Ctx, UsageError, resolve_caller
from herdr_fleet.fleetfile import NAME_RE, FleetError, find_fleet_file
from herdr_fleet.inbox import CUR, NEW, Message, size_label
from herdr_fleet.messages import (HOLD, OWNER, Drop, agent_state, caller_record, compose_prompt,
                                  dry_hold, expire_due, first_line, notify_owner, place, prompt_mailbox, queued_in,
                                  read, record, selection_label, settle, since_label)
from herdr_fleet.seats import SEND_ALLOWED, SETTLED, state_words
from herdr_fleet.state import now_iso

# -- deliver ---------------------------------------------------------------------------------


def op_deliver(ctx: Ctx, names: Sequence[str], fyi_too: bool = False, now: bool = False) -> int:
    """Design 18.5 item 3, 20.8 and 21.1. Without names: every seat and contact whose queue
    holds a message that is not FYI (``--fyi``: any message), then ``owner`` if anything
    waits for it. ``--now`` (names only) types a whole mailbox ahead to a working seat,
    the only way besides ``send --now`` that text reaches one. Exit 0 unless a prompt
    failed or may not have arrived (1)."""
    if now and not names:
        raise UsageError("deliver --now needs the seats to type ahead to (deliver NAME... --now)")
    fleet, repo = ctx.fleet, ctx.fleet.repo
    agents = ctx.herdr.agent_list()
    if not ctx.dry_run:  # what is past its ttl goes first, logged, wherever it waits
        expire_all(ctx)
    waiting = inbox.queued(repo)
    if names:
        known = ({s.name for s in fleet.seats} | {c.name for c in fleet.contacts} | {OWNER}
                 | set(inbox.mailboxes(repo)) | {str(a["name"]) for a in agents if a.get("name")})
        unknown = [n for n in names if n not in known]
        if unknown:
            raise UsageError("not a seat, contact, owner or mailbox: %s" % ", ".join(unknown))
        order = list(dict.fromkeys(names))
    else:
        order = candidates(ctx, waiting, fyi_too)
        if waiting.get(OWNER):
            order.append(OWNER)
        if not order:
            seen.observe_agents(ctx, agents)
            ctx.say("nothing to deliver" + ("" if fyi_too or not waiting else
                                            " (only FYIs are queued; deliver --fyi takes them)"))
            return EXIT_OK
    codes = [EXIT_OK]
    observed: Dict[str, str] = {}
    for name in order:
        line, code, _ = deliver_one(ctx, name, agents, waiting.get(name, []), now=now, observed=observed)
        ctx.say(line)
        codes.append(code)
    seen.observe_agents(ctx, agents, observed)
    return max(codes)


def expire_all(ctx: Ctx) -> None:
    """Move every queued message past its ttl to ``cur/``, logged ``expired``, in every mailbox."""
    repo = ctx.fleet.repo
    for mailbox in inbox.mailboxes(repo):
        if inbox.has_queued(repo, mailbox):
            with inbox.locked(repo, mailbox):
                expire_due(ctx, mailbox)


def candidates(ctx: Ctx, waiting: Dict[str, List[Message]], fyi_too: bool = False) -> List[str]:
    """The mailboxes ``deliver`` takes without names (design 18.5 item 3): seats in file
    order, then contacts, each only with a message that is not FYI (``fyi_too``: any)."""
    fleet = ctx.fleet
    names = [s.name for s in fleet.seats] + [c.name for c in fleet.contacts]
    return [n for n in names if any(fyi_too or not m.fyi for m in waiting.get(n, []))]


def herdr_target(ctx: Ctx, name: str, agents: Sequence[Dict]) -> Tuple[Optional[str], Optional[str]]:
    """(what ``agent prompt`` gets, or None; why it cannot be reached, or None)."""
    contact = ctx.fleet.contact(name)
    if contact is not None and ctx.fleet.seat(name) is None:
        res = contact_mod.resolve(contact, agents)
        problem = res.problem()
        return res.pane, ("%s: %s" % (res.state, problem) if problem else None)
    live = {str(a["name"]) for a in agents if a.get("name")}
    return (name, None) if name in live else (None, "absent")


def other_fleet_of(ctx: Ctx, name: str, agents: Sequence[Dict]) -> Optional[str]:
    """For a mailbox that is a live agent of another fleet (not a seat, contact or owner),
    that fleet's repository, so its log gets the ``delivered`` entries too (20.8)."""
    fleet = ctx.fleet
    if name == OWNER or fleet.seat(name) is not None or fleet.contact(name) is not None:
        return None
    return crossfleet.find(agents, name, fleet.repo)


def deliver_one(ctx: Ctx, name: str, agents: Sequence[Dict], waiting: List[Message],
                via: str = "deliver", now: bool = False,
                observed: Optional[Dict[str, str]] = None) -> Tuple[str, int, Optional[Drop]]:
    """Hand one mailbox over if its owner can take it now: (line, exit code, what the
    attempt did, or None when nothing was attempted). An idle or done seat gets it; a
    working one only with ``now`` (``deliver NAME --now``): typed ahead (design 21.1).
    While the owner is typing in the seat's pane nothing moves (21.2). ``watch --once``
    uses it with ``via="watch"``."""
    count = len(waiting)
    if not count:
        return "nothing queued for %s" % name, EXIT_OK, None
    plural = "%d message%s" % (count, "" if count == 1 else "s")
    if name == OWNER:
        if ctx.dry_run:
            return "would notify owner of %s" % plural, EXIT_OK, None
        with inbox.locked(ctx.fleet.repo, OWNER):
            drop = notify_owner(ctx)
        if drop.status == "sent":
            return "delivered %d to owner (notification)" % len(drop.carried), EXIT_OK, drop
        return "owner: %s, %s left queued" % (drop.detail or "not shown", plural), EXIT_OK, drop
    target, problem = herdr_target(ctx, name, agents)
    state = problem or agent_state(ctx, target)
    if observed is not None:
        observed[name] = "absent" if problem else state
    ahead = state == "working" and now
    if problem or (state not in SETTLED and not ahead):
        return "%s: %s, %d left queued" % (name, state_words(state), count), EXIT_OK, None
    if ctx.dry_run:
        hold = dry_hold(ctx, target)  # design 21.2: the guard only reads
        if hold:
            return "%s: %s, %d left queued" % (name, HOLD % hold, count), EXIT_OK, None
        _, size, pointer = compose_prompt(waiting)
        if pointer:
            return "would prompt %s (%s) with a pointer: %s" % (
                name, place(state), inbox.pointer_text(waiting, size, prefix=False)), EXIT_OK, None
        return "would deliver %d to %s (%s), %s" % (count, name, place(state), size_label(size)), EXIT_OK, None
    with inbox.locked(ctx.fleet.repo, name):
        drop = prompt_mailbox(ctx, name, str(target), SEND_ALLOWED if ahead else SETTLED, via,
                              mirror=other_fleet_of(ctx, name, agents))
    line, code = _line(name, drop, state)
    return line, code, drop


def _line(name: str, drop: Drop, state: str) -> Tuple[str, int]:
    shown = drop.state or state
    if drop.status == "sent":
        return "delivered %d to %s (%s), %s" % (len(drop.carried), name, place(shown),
                                                size_label(drop.size)), EXIT_OK
    if drop.status == "unconfirmed":
        return ("unconfirmed %s: %s; %d logged as delivered (unconfirmed), not resent"
                % (name, drop.detail, len(drop.carried)), EXIT_FAIL)
    if drop.status == "pointer":
        return "prompted %s (%s) with a pointer: %s" % (
            name, place(shown), inbox.pointer_text(drop.waiting, drop.size, prefix=False)), EXIT_OK
    if drop.status == "empty":
        return "nothing queued for %s" % name, EXIT_OK
    if drop.status == "refused":
        return "%s: %s, %d left queued" % (name, state_words(drop.state), len(drop.waiting)), EXIT_OK
    if drop.status == "typing":  # design 21.2: `X: owner typing in w1:p2, 3 left queued`
        return "%s: %s, %d left queued" % (name, drop.detail, len(drop.waiting)), EXIT_OK
    return "failed %s: %s; %d left queued" % (name, drop.detail, len(drop.waiting)), EXIT_FAIL



# -- inbox -----------------------------------------------------------------------------------------


def lookup_caller(ctx: Ctx, as_name: Optional[str]) -> str:
    """``--as`` (no herdr command at all), else the agent in the caller's pane as ``send``
    finds it (verified caller pane and ``agent get``); only a verified shell or outside
    Herdr uses ``owner``. Failed caller lookup requires explicit ``--as``. The state
    that ``agent get`` answered is recorded for a seat (design 20.5)."""
    if not as_name and ctx.inside_herdr() and ctx.env.get("HERDR_PANE_ID"):
        resolve_caller(ctx)
    name, record_ = caller_record(ctx, as_name)
    if record_.get("agent_status"):
        seen.observe(ctx, {name: str(record_["agent_status"])})
    return name


REASON_TAGS = {"refusal": "[on refusal]", "queue": "[queued]", "fyi": "[fyi]",  # design 20.8
               "kickoff": "[kickoff]"}                                            # design 32.6


def tags(message: Message) -> str:
    """``[urgent]`` and how the message waits, each followed by two spaces."""
    parts = (["[urgent]"] if message.urgent else []) + [REASON_TAGS[message.mode]]
    if message.fyi and message.mode != "fyi":
        parts.append("[fyi]")
    return "".join(p + "  " for p in parts)


WAIT_POLL_S = 1.0  # design 23.2: `inbox --wait` looks at the mailbox directory every second


def op_inbox(ctx: Ctx, as_name: Optional[str] = None, read_all: bool = False, count: bool = False,
             history: Optional[str] = None, redeliver: Optional[str] = None, as_json: bool = False,
             full: bool = False, wait: bool = False, timeout_ms: Optional[int] = None) -> int:
    """Design 18.5 item 4 and 20.1, 20.3, 20.4, 23.2: the caller's own mailbox. ``wait``
    blocks until the mailbox holds a message not past its ttl (FYIs count), then reads it
    as ``--read`` does; with ``timeout_ms`` it gives up after that long (exit 3)."""
    repo = ctx.fleet.repo
    if timeout_ms is not None and not wait:
        raise UsageError("--timeout goes with --wait")
    if wait and (count or history is not None or redeliver):
        raise UsageError("--wait reads what arrives; it takes neither --count, --history nor --redeliver")
    if timeout_ms is not None and timeout_ms < 0:
        raise UsageError("--timeout takes milliseconds, 0 or more")
    if history is not None and not NAME_RE.match(history):
        raise UsageError("--history %r is not a name" % history)
    read_all = read_all or wait
    if full and not (read_all or history is not None):
        raise UsageError("--full goes with --read or --history")
    limit = None if full else ctx.fleet.inbox_print_max
    caller = lookup_caller(ctx, as_name)
    identity = "inbox: %s; selected fleet: %s; %s" % (
        caller, selection_label(ctx), getattr(ctx, "identity_reason", "explicit --as"))
    if as_json or count:
        ctx.warn(identity)
    else:
        ctx.say(identity)
    if redeliver:
        return _redeliver(ctx, redeliver, as_json)
    if history is not None:
        return _history(ctx, caller, history, as_json, limit)
    deadline = None if timeout_ms is None else ctx.clock.monotonic() + timeout_ms / 1000.0
    if wait:  # design 32.4: say that this process waits, for status and the board; gone whatever ends it
        try:
            inbox.write_waiting(repo, caller, os.getpid(), now_iso(), timeout_ms)
        except OSError as exc:
            ctx.warn("cannot write %s: %s" % (inbox.waiting_path(repo, caller), exc))
    try:
        while True:
            if wait and not _await_mail(ctx, caller, deadline):
                if as_json:
                    ctx.say("[]")
                    ctx.warn("nothing arrived for %s in %s" % (caller, wait_label(timeout_ms or 0)))
                else:
                    ctx.say("nothing arrived in %s" % wait_label(timeout_ms or 0))
                return EXIT_PARTIAL
            messages = _take(ctx, caller, read_all)
            if messages or not wait:  # empty after the wait: the watcher took it first; wait on
                break
    finally:
        if wait:
            inbox.clear_waiting(repo, caller, os.getpid())
    if count:
        ctx.say(json.dumps({"name": caller, "count": len(messages)}) if as_json else str(len(messages)))
    elif as_json:
        ctx.say(json.dumps([dict(_as_dict(m, body=read_all), position=i)
                            for i, m in enumerate(messages, 1)], indent=2, ensure_ascii=False))
    elif not messages:
        ctx.say("nothing queued for %s" % caller)
    elif read_all:
        ctx.say(inbox.render_for_print(messages, limit))
    else:
        for index, message in enumerate(messages, 1):
            ctx.say("%d. #%s  %s  %s  %s%s" % (index, message.short, since_label(message.time), message.sender,
                                              tags(message), first_line(message.body)))
    return EXIT_OK


def _take(ctx: Ctx, name: str, read_all: bool) -> List[Message]:
    """The mailbox's queued messages after expiring what is past its ttl, under the mailbox
    lock; with ``read_all`` they move to ``cur/``, each logged ``delivered`` via ``inbox``."""
    repo = ctx.fleet.repo
    if not inbox.has_queued(repo, name):
        return []
    with inbox.locked(repo, name):
        expire_due(ctx, name)
        messages = queued_in(ctx, name)
        if read_all:  # a queued kickoff read this way counts as sent (32.6), logged ahead of the rest
            settle(ctx, messages, "inbox", "sent")
    return messages


def _await_mail(ctx: Ctx, name: str, deadline: Optional[float]) -> bool:
    """Design 23.2: look at the mailbox every WAIT_POLL_S (files only; no herdr command)
    until it holds a message not past its ttl; False when the ``deadline`` (of the fake
    or real clock) passed first. Without one it waits until the agent's own tool timeout
    ends the command. The read that follows takes the mailbox lock, as the watcher does,
    so a message the watcher delivered meanwhile is not read twice: the wait goes on."""
    repo = ctx.fleet.repo
    while True:
        if inbox.has_queued(repo, name):
            now = inbox.now_utc()
            if any(not inbox.is_due(m, now) for m in inbox.read_box(repo, name, NEW)):
                return True
        if deadline is not None and ctx.clock.monotonic() >= deadline:
            return False
        ctx.clock.sleep(WAIT_POLL_S if deadline is None else
                        max(0.0, min(WAIT_POLL_S, deadline - ctx.clock.monotonic())))


def wait_label(ms: int) -> str:
    """``10m`` for 600000 (the age format of design 20.2), ``45 s`` under a minute."""
    seconds = ms / 1000.0
    return inbox.age_label(seconds) if seconds >= 60 else "%s s" % format(seconds, "g")


def _as_dict(message: Message, body: bool = False) -> Dict:
    item = {"id": message.id, "from": message.sender, "to": message.to, "time": message.time, "fyi": message.fyi,
            "reply_to": message.reply_to, "ttl": message.ttl, "file": message.file,
            "queued_by": message.mode, "urgent": message.urgent, "first_line": first_line(message.body)}
    if body:
        item["body"] = message.body
    if message.timer:
        item.update(timer=message.timer, scheduled_for=message.scheduled_for)
    if message.origin_fleet:
        item.update(origin_fleet=message.origin_fleet, destination_fleet=message.destination_fleet,
                    requested_to=message.requested_to, history_only=message.history_only)
    return item


def _redeliver(ctx: Ctx, ref: str, as_json: bool) -> int:
    message = inbox.resolve(ctx.fleet.repo, ref)
    if message.history_only:
        raise UsageError("#%s is a nonpending source receipt, not a delivered source queue; inspect its "
                         "destination with --fleet %s rather than redelivering the history copy"
                         % (message.short, os.path.join(message.destination_fleet or "PATH", ".herdr", "fleet.toml")))
    if message.box != CUR:
        raise UsageError("#%s is still queued for %s (in new/), so there is nothing to redeliver"
                         % (message.short, message.to))
    with inbox.locked(ctx.fleet.repo, message.to):
        inbox.move(ctx.fleet.repo, message, NEW)
    if as_json:
        ctx.say(json.dumps({"id": message.id, "to": message.to, "box": NEW}))
    else:
        ctx.say("#%s is queued again for %s; the next delivery carries it" % (message.short, message.to))
    return EXIT_OK


def _history(ctx: Ctx, caller: str, other: str, as_json: bool, limit: Optional[int]) -> int:
    """Design 18.5 and 20.1: every delivered message between the caller and ``other``,
    oldest first, from two sources: the ``cur/`` files of both mailboxes (full bodies)
    and the log's ``send`` entries between the two that have no file (sent or
    unconfirmed; messages from before 0.6.0, or whose file is gone), shown with their
    first line only. The log says which ``cur/`` files were delivered: a message whose
    last log entry is ``expired``, ``dropped`` or a refused ``send`` is left out; a file
    the log does not mention is shown."""
    repo = ctx.fleet.repo
    warnings: List[str] = []
    fate: Dict[str, str] = {}
    entries = read(repo, warnings)
    for entry in entries:
        ident = entry.get("id")
        kind = entry.get("kind")
        if not ident or kind not in ("send", "delivered", "expired", "dropped"):
            continue
        fate[str(ident)] = str(entry.get("delivery")) if kind == "send" else str(kind)
    found: List[Message] = []
    for mailbox, sender in ((caller, other), (other, caller)):
        found.extend(m for m in inbox.read_box(repo, mailbox, CUR, warnings) if m.sender == sender)
    queued_ids = {m.id for mailbox in (caller, other) for m in inbox.read_box(repo, mailbox, NEW, warnings)}
    for warning in warnings:
        ctx.warn(warning)
    files = [m for m in found if fate.get(m.id) not in ("expired", "dropped", "refused")
             and (not m.history_only or fate.get(m.id) in ("sent", "unconfirmed", "delivered"))]
    filed = {m.id for m in found} | queued_ids
    pair = {(caller, other), (other, caller)}
    log_only = [e for e in entries if e.get("kind", "send") == "send" and (str(e["from"]), str(e["to"])) in pair
                and (e.get("delivery") or "sent") in ("sent", "unconfirmed") and str(e.get("id") or "") not in filed]
    timeline: List[Tuple[Tuple, object]] = []
    for index, message in enumerate(sorted(files, key=Message.sort_key)):
        timeline.append((_when_key(message.time, 0, index), message))
    for index, entry in enumerate(log_only):
        timeline.append((_when_key(entry["time"], 1, index), entry))
    timeline.sort(key=lambda pair_: pair_[0])
    total, partial = len(timeline), len(log_only)
    if as_json:
        items = []
        for _, item in timeline:
            if isinstance(item, Message):
                items.append(dict(_as_dict(item, body=True), body_available=True))
            else:
                items.append({"id": item.get("id"), "from": item["from"], "to": item["to"], "time": item["time"],
                              "fyi": bool(item.get("fyi")), "reply_to": item.get("reply_to"), "ttl": None,
                              "file": item.get("file"), "first_line": item.get("first_line", ""), "body": None,
                              "body_available": False})
        ctx.say(json.dumps(items, indent=2, ensure_ascii=False))
        return EXIT_OK
    if not timeline:
        ctx.say("no messages between %s and %s" % (caller, other))
        return EXIT_OK
    blocks = []
    for _, item in timeline:
        if isinstance(item, Message):
            blocks.append(inbox.render_for_print([item], limit, direction=True))
        else:
            blocks.append("[from %s to %s at %s, first line only]\n%s" % (
                item["from"], item["to"], since_label(str(item["time"])), item.get("first_line", "")))
    ctx.say("\n\n".join(blocks))
    if partial:
        ctx.say("\n%d of %d messages have only their first line (sent before the inbox kept bodies)"
                % (partial, total))
    return EXIT_OK


def _when_key(text: str, source: int, index: int) -> Tuple:
    when = inbox.parse_time(text)
    stamp = when.timestamp() if when is not None else 0.0
    return (stamp, source, index)


# -- inbox --hook (design 18.7) ---------------------------------------------------------------------

HOOK_CAP_CHARS = 9000  # Claude Code caps additionalContext at 10,000 characters
HOOK_EVENTS = {"stop": "Stop", "prompt": "UserPromptSubmit"}


def op_inbox_hook(ctx: Ctx, which: str, stdin: TextIO) -> int:
    """``inbox --hook stop|prompt``: the seat reads its own inbox inside its own Claude
    Code process. Prints one ``hookSpecificOutput`` object or nothing; always exit 0,
    and nothing on stderr either, so a broken hook never disturbs the agent."""
    ctx.stderr = io.StringIO()  # warnings (a fleet file key, a corrupt message) stay out of the hook
    try:
        output = _hook_output(ctx, which, stdin)
    except Exception:  # noqa: BLE001 - invariant 14: a hook prints nothing on any error
        return EXIT_OK
    if output:
        try:
            ctx.say(output)
            ctx.stdout.flush()
        except Exception:  # noqa: BLE001
            pass
    return EXIT_OK


def _hook_input(stdin: TextIO) -> Dict:
    try:
        data = json.loads(stdin.read() or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _hook_seat(ctx: Ctx, data: Dict) -> Optional[str]:
    """The seat whose Claude Code runs this hook: the fleet is found from the hook's
    ``cwd`` (else the process cwd), the agent from ``HERDR_PANE_ID`` through ``agent get``.
    None when any of it is missing or the pane's agent is not a seat."""
    pane = ctx.env.get("HERDR_PANE_ID")
    if not pane or not ctx.inside_herdr():
        return None
    hook_cwd = data.get("cwd")
    path = None
    for directory in ([hook_cwd] if isinstance(hook_cwd, str) and os.path.isdir(hook_cwd) else []) + [ctx.cwd]:
        try:
            path = find_fleet_file(directory, ctx.file, ctx.env)
            break
        except FleetError:
            continue
    if path is None:
        return None
    ctx.file = path  # found; loading it may still fail, which the caller turns into silence
    fleet = ctx.fleet
    name = ctx.herdr.pane_agent(pane).get("name")
    return str(name) if name and fleet.seat(str(name)) is not None else None


def _hook_output(ctx: Ctx, which: str, stdin: TextIO) -> Optional[str]:
    data = _hook_input(stdin)
    if which == "stop" and data.get("stop_hook_active") is True:
        return None  # a Stop hook already continued this turn: let it end
    name = _hook_seat(ctx, data)
    if not name:
        return None
    repo = ctx.fleet.repo
    if not inbox.has_queued(repo, name):
        return None
    if which == "prompt":
        now = inbox.now_utc()
        count = len([m for m in queued_in(ctx, name) if not inbox.is_due(m, now)])
        if not count:
            return None
        return _hook_json(which, "%d message%s waiting; run: %s" % (count, "" if count == 1 else "s",
                                                                    inbox.POINTER_COMMAND))
    with inbox.locked(repo, name):
        expire_due(ctx, name)
        waiting = queued_in(ctx, name)
        if all(m.fyi for m in waiting):
            return None  # nothing, or only FYIs: an FYI never costs a turn (18.12)
        batch, size, pointer = compose_prompt(waiting)  # a queued kickoff first and verbatim (32.6)
        if pointer or len(batch) > HOOK_CAP_CHARS:
            return _hook_json(which, inbox.pointer_text(waiting, size))  # the files stay queued
        settle(ctx, waiting, "hook", "sent")
    return _hook_json(which, batch)


def _hook_json(which: str, context: str) -> str:
    # ASCII only: a hook's stdout may have no UTF-8 encoding, and the files have moved by now.
    return json.dumps({"hookSpecificOutput": {"hookEventName": HOOK_EVENTS[which], "additionalContext": context}})
