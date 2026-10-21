"""The ``deliver`` and ``inbox`` commands (design 18.5 items 3 and 4).

- ``deliver`` hands queued messages to seats that can take them now: an idle or
  done seat gets its mailbox as one batch through ``seats.safe_prompt``; a
  working, blocked, unknown or absent seat is left alone with its messages
  queued (queued messages never interrupt). ``owner`` gets a notification.
- ``inbox`` is the pull side: the caller's own mailbox, listed, read (printed
  and moved to ``cur/``), counted, or replayed from ``cur/`` (``--history``);
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
from herdr_fleet import inbox
from herdr_fleet.context import EXIT_FAIL, EXIT_OK, Ctx, UsageError, resolve_caller
from herdr_fleet.fleetfile import NAME_RE, FleetError, find_fleet_file
from herdr_fleet.inbox import CUR, NEW, Message, size_label
from herdr_fleet.messages import (OWNER, Drop, agent_state, compose_prompt, delivered_entry, expire_due,
                                  first_line, notify_owner, prompt_mailbox, queued_in, read, record,
                                  resolve_sender, settle, since_label)
from herdr_fleet.seats import SETTLED

# -- deliver ---------------------------------------------------------------------------------


def op_deliver(ctx: Ctx, names: Sequence[str], fyi_too: bool = False) -> int:
    """Design 18.5 item 3. Without names: every seat and contact whose queue holds a
    message that is not FYI (``--fyi``: any message), then ``owner`` if anything waits
    for it. Exit 0 unless a prompt failed or may not have arrived (1)."""
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
            ctx.say("nothing to deliver" + ("" if fyi_too or not waiting else
                                            " (only FYIs are queued; deliver --fyi takes them)"))
            return EXIT_OK
    codes = [EXIT_OK]
    for name in order:
        line, code, _ = deliver_one(ctx, name, agents, waiting.get(name, []))
        ctx.say(line)
        codes.append(code)
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


def deliver_one(ctx: Ctx, name: str, agents: Sequence[Dict], waiting: List[Message],
                via: str = "deliver") -> Tuple[str, int, Optional[Drop]]:
    """Hand one mailbox over if its owner can take it now: (line, exit code, what the
    attempt did, or None when nothing was attempted). ``watch --once`` uses it with
    ``via="watch"``."""
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
    if problem or state not in SETTLED:
        return "%s: %s, %d left queued" % (name, state, count), EXIT_OK, None
    if ctx.dry_run:
        _, size, pointer = compose_prompt(waiting)
        if pointer:
            return "would prompt %s (%s) with a pointer: %s" % (
                name, state, inbox.pointer_text(waiting, size, prefix=False)), EXIT_OK, None
        return "would deliver %d to %s (%s), %s" % (count, name, state, size_label(size)), EXIT_OK, None
    with inbox.locked(ctx.fleet.repo, name):
        drop = prompt_mailbox(ctx, name, str(target), SETTLED, via)
    line, code = _line(name, drop, state)
    return line, code, drop


def _line(name: str, drop: Drop, state: str) -> Tuple[str, int]:
    shown = drop.state or state
    if drop.status == "sent":
        return "delivered %d to %s (%s), %s" % (len(drop.carried), name, shown, size_label(drop.size)), EXIT_OK
    if drop.status == "unconfirmed":
        return ("unconfirmed %s: %s; %d logged as delivered (unconfirmed), not resent"
                % (name, drop.detail, len(drop.carried)), EXIT_FAIL)
    if drop.status == "pointer":
        return "prompted %s (%s) with a pointer: %s" % (
            name, shown, inbox.pointer_text(drop.waiting, drop.size, prefix=False)), EXIT_OK
    if drop.status == "empty":
        return "nothing queued for %s" % name, EXIT_OK
    if drop.status == "refused":
        return "%s: %s, %d left queued" % (name, drop.state, len(drop.waiting)), EXIT_OK
    return "failed %s: %s; %d left queued" % (name, drop.detail, len(drop.waiting)), EXIT_FAIL



# -- inbox -----------------------------------------------------------------------------------------


def lookup_caller(ctx: Ctx, as_name: Optional[str]) -> str:
    """``--as`` (no herdr command at all), else the agent in the caller's pane as ``send``
    finds it (``pane current --current`` and ``agent get``), else ``owner``."""
    if not as_name and ctx.inside_herdr() and ctx.invocation.pane_id:
        resolve_caller(ctx)
    return resolve_sender(ctx, as_name)


def op_inbox(ctx: Ctx, as_name: Optional[str] = None, read_all: bool = False, count: bool = False,
             history: Optional[str] = None, redeliver: Optional[str] = None, as_json: bool = False) -> int:
    """Design 18.5 item 4: the caller's own mailbox."""
    repo = ctx.fleet.repo
    if history is not None and not NAME_RE.match(history):
        raise UsageError("--history %r is not a name" % history)
    if redeliver:
        return _redeliver(ctx, redeliver, as_json)
    caller = lookup_caller(ctx, as_name)
    if history is not None:
        return _history(ctx, caller, history, as_json)
    if not inbox.has_queued(repo, caller):
        messages: List[Message] = []
    else:
        with inbox.locked(repo, caller):
            expire_due(ctx, caller)
            messages = queued_in(ctx, caller)
            if read_all and messages:
                for message in messages:
                    inbox.move(repo, message, CUR)
                    record(ctx, delivered_entry(message, "inbox"))
    if count:
        ctx.say(json.dumps({"name": caller, "count": len(messages)}) if as_json else str(len(messages)))
    elif as_json:
        ctx.say(json.dumps([_as_dict(m, body=read_all) for m in messages], indent=2, ensure_ascii=False))
    elif not messages:
        ctx.say("nothing queued for %s" % caller)
    elif read_all:
        ctx.say(inbox.render_batch(messages))
    else:
        for message in messages:
            ctx.say("#%s  %s  %s  %s%s" % (message.short, since_label(message.time), message.sender,
                                          "[fyi]  " if message.fyi else "", first_line(message.body)))
    return EXIT_OK


def _as_dict(message: Message, body: bool = False) -> Dict:
    item = {"id": message.id, "from": message.sender, "to": message.to, "time": message.time, "fyi": message.fyi,
            "reply_to": message.reply_to, "ttl": message.ttl, "file": message.file,
            "first_line": first_line(message.body)}
    if body:
        item["body"] = message.body
    return item


def _redeliver(ctx: Ctx, ref: str, as_json: bool) -> int:
    message = inbox.resolve(ctx.fleet.repo, ref)
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


def _history(ctx: Ctx, caller: str, other: str, as_json: bool) -> int:
    """Every delivered message between the caller and ``other``, from both ``cur/``
    directories, oldest first. The log says which ``cur/`` files were delivered:
    a message whose last log entry is ``expired``, ``dropped`` or a refused ``send``
    is left out; a file the log does not mention is shown."""
    repo = ctx.fleet.repo
    warnings: List[str] = []
    fate: Dict[str, str] = {}
    for entry in read(repo, warnings):
        ident = entry.get("id")
        kind = entry.get("kind")
        if not ident or kind not in ("send", "delivered", "expired", "dropped"):
            continue
        fate[str(ident)] = str(entry.get("delivery")) if kind == "send" else str(kind)
    found: List[Message] = []
    for mailbox, sender in ((caller, other), (other, caller)):
        found.extend(m for m in inbox.read_box(repo, mailbox, CUR, warnings) if m.sender == sender)
    for warning in warnings:
        ctx.warn(warning)
    shown = sorted((m for m in found if fate.get(m.id) not in ("expired", "dropped", "refused")),
                   key=Message.sort_key)
    if as_json:
        ctx.say(json.dumps([_as_dict(m, body=True) for m in shown], indent=2, ensure_ascii=False))
    elif not shown:
        ctx.say("no delivered messages between %s and %s" % (caller, other))
    else:
        ctx.say(inbox.render_batch(shown, direction=True))
    return EXIT_OK


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
        batch = inbox.render_batch(waiting)
        size = inbox.size_of(batch)
        if size > inbox.BATCH_CAP or len(batch) > HOOK_CAP_CHARS:
            return _hook_json(which, inbox.pointer_text(waiting, size))  # the files stay queued
        settle(ctx, waiting, "hook", "sent")
    return _hook_json(which, batch)


def _hook_json(which: str, context: str) -> str:
    # ASCII only: a hook's stdout may have no UTF-8 encoding, and the files have moved by now.
    return json.dumps({"hookSpecificOutput": {"hookEventName": HOOK_EVENTS[which], "additionalContext": context}})
