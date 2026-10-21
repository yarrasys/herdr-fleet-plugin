"""The watcher (design 18.6): ``watch`` delivers queued messages as seats become idle.

A foreground process the owner starts (from a shell, or with the ``Fleet: watch``
pane) and stops with Ctrl-C. It never runs in the background and asks no agent
to poll (invariants 11 and 13): every wait is ``herdr agent wait`` in the herdr
server, one thread per mailbox with something to deliver.

- One watcher per fleet: ``.herdr/watch.lock`` names its pid; a lock naming a
  live pid refuses the start, a dead one is replaced, and the lock goes on exit.
- Every 2 s the loop reads the mailboxes (filesystem only). A seat or contact
  with a message that is not FYI, and no waiter yet, gets a thread running
  ``agent wait NAME --until idle --until done --timeout 30000``; a timeout ends
  the thread and the next pass starts another. ``agent list`` is asked only
  when a waiter is due. Absent agents get none (their kickoff carries the
  messages) and are looked for again every 30 s; FYI-only mailboxes get none.
- When the wait sees idle or done: sleep ``--debounce``, read the state again
  with ``agent get``, and if still idle or done deliver the batch through
  ``messages.prompt_mailbox`` exactly as ``deliver`` does (logged via
  ``watch``), then ``herdr notification show``. Otherwise go back to waiting.
- ``owner`` is notified at once (``messages.notify_owner``).
- ``--once`` delivers what can be delivered now, like ``deliver``, and exits:
  no waiters, no lock.

It runs only ``agent list``, ``agent wait``, ``agent get``, ``agent prompt``
(through ``seats.safe_prompt``) and ``notification show`` (invariant 12): it
never prompts a blocked or unknown seat, never answers a dialog, never
redelivers an unconfirmed message, and never starts, restarts or quits anything.
"""

from __future__ import annotations

import datetime
import json
import os
import signal
import tempfile
import threading
import time
from typing import Callable, Dict, List, Optional, Set

from herdr_fleet import inbox
from herdr_fleet.context import EXIT_OK, Ctx, OpError, UsageError
from herdr_fleet.herdr import HerdrError, describe
from herdr_fleet.inbox import NEW, Message
from herdr_fleet.mailroom import candidates, deliver_one, expire_all, herdr_target
from herdr_fleet.messages import OWNER, Drop, agent_state, notify_owner, prompt_mailbox, since_label
from herdr_fleet.seats import SETTLED
from herdr_fleet.state import ensure_gitignore, now_iso

LOCK_NAME = "watch.lock"
WAIT_TIMEOUT_MS = 30000     # one `agent wait`; a timeout only lets the loop look again
CYCLE_S = 2.0               # how often the loop re-reads the mailboxes
DEFAULT_DEBOUNCE_MS = 5000
RETRY_AFTER_S = 60.0        # after a failed delivery or wait, leave that mailbox alone this long
ABSENT_RECHECK_S = WAIT_TIMEOUT_MS / 1000.0  # an absent agent is looked for as often as a wait times out
DELIVERY_GRACE_S = 60.0     # on exit, how long a delivery in flight may take to finish
EXIT_GRACE_S = 1.0          # on exit, how long waiters blocked in `agent wait` are given


# -- the lock -----------------------------------------------------------------------------------


def lock_path(repo: str) -> str:
    return os.path.join(repo, ".herdr", LOCK_NAME)


def pid_alive(pid: Optional[int]) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # alive, another user's
    except OSError:
        return False
    return True


def read_lock(repo: str) -> Optional[Dict]:
    """The lock's JSON; None when there is no lock, {} when it cannot be read."""
    try:
        with open(lock_path(repo), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _pid(data: Optional[Dict]) -> Optional[int]:
    pid = (data or {}).get("pid")
    return pid if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 else None


def watcher_status(repo: str) -> Dict:
    """``{"running", "pid", "since"}`` for ``status`` (a lock naming a dead pid is not running)."""
    data = read_lock(repo)
    pid = _pid(data)
    if pid and pid_alive(pid):
        return {"running": True, "pid": pid, "since": (data or {}).get("started")}
    return {"running": False, "pid": None, "since": None}


def watcher_line(status: Dict) -> str:
    """``watcher: running (pid 123 since 10:00)`` or ``watcher: not running``."""
    if not status.get("running"):
        return "watcher: not running"
    since = " since %s" % since_label(status["since"]) if status.get("since") else ""
    return "watcher: running (pid %d%s)" % (status["pid"], since)


def acquire(repo: str, fleet_path: str, pid: Optional[int] = None) -> Dict:
    """Write ``.herdr/watch.lock``, or raise OpError when a live watcher holds it. The file
    is created with a hard link, so of two watchers starting at once only one gets it."""
    data = {"pid": pid or os.getpid(), "started": now_iso(), "fleet": fleet_path}
    folder = os.path.join(repo, ".herdr")
    os.makedirs(folder, exist_ok=True)
    ensure_gitignore(folder)
    fd, tmp = tempfile.mkstemp(prefix=".watch.", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
            handle.write("\n")
        os.chmod(tmp, 0o644)
        for _ in range(3):
            try:
                os.link(tmp, lock_path(repo))
                return data
            except FileExistsError:
                holder = _pid(read_lock(repo))
                if holder and pid_alive(holder):
                    raise OpError("watcher already running (pid %d)" % holder)
                try:  # a dead watcher's lock, or an unreadable one: replace it
                    os.unlink(lock_path(repo))
                except FileNotFoundError:
                    pass
        raise OpError("cannot take %s" % lock_path(repo))
    finally:
        os.unlink(tmp)


def release(repo: str, pid: int) -> None:
    """Remove the lock if it is still ours."""
    if _pid(read_lock(repo)) == pid:
        try:
            os.unlink(lock_path(repo))
        except FileNotFoundError:
            pass


# -- the command ---------------------------------------------------------------------------------


def spawn(work: Callable[[], None]) -> Optional[threading.Thread]:
    """Run one waiter. A daemon thread: Ctrl-C never waits for an `agent wait` to time out.
    Tests substitute a function that runs ``work`` inline."""
    thread = threading.Thread(target=work, name="herdr-fleet-waiter", daemon=True)
    thread.start()
    return thread


def op_watch(ctx: Ctx, debounce_ms: Optional[int] = None, once: bool = False) -> int:
    """Design 18.6. Runs until Ctrl-C (exit 0), or one pass with ``--once``."""
    debounce = DEFAULT_DEBOUNCE_MS if debounce_ms is None else debounce_ms
    if debounce < 0:
        raise UsageError("--debounce takes milliseconds, 0 or more")
    fleet = ctx.fleet
    if once:
        return watch_once(ctx)
    lock = acquire(fleet.repo, fleet.path)
    previous: Dict = {}
    try:
        previous = _trap_signals()
        return Watcher(ctx, debounce).run()
    finally:
        _restore_signals(previous)
        release(fleet.repo, lock["pid"])


def watch_once(ctx: Ctx) -> int:
    """``--once``: what ``deliver`` without names would deliver, logged via ``watch``,
    with the watcher's notification for each mailbox delivered. Exit 1 when a prompt
    failed or may not have arrived, as ``deliver``; else 0."""
    agents = ctx.herdr.agent_list()
    expire_all(ctx)
    waiting = inbox.queued(ctx.fleet.repo)
    order = candidates(ctx, waiting) + ([OWNER] if waiting.get(OWNER) else [])
    if not order:
        ctx.say("nothing to deliver")
        return EXIT_OK
    codes = [EXIT_OK]
    for name in order:
        line, code, drop = deliver_one(ctx, name, agents, waiting.get(name, []), via="watch")
        ctx.say(line)
        codes.append(code)
        if drop is not None and name != OWNER:
            notify_delivery(ctx, name, drop)
    return max(codes)


def notify_delivery(ctx: Ctx, name: str, drop: Drop) -> None:
    """The owner's one-liner after a delivery to a seat: ``delivered 3 to fablyn-dev``."""
    count = len(drop.carried)
    senders: Dict[str, int] = {}
    for message in drop.carried or drop.waiting:
        senders[message.sender] = senders.get(message.sender, 0) + 1
    body = "from " + ", ".join("%s (%d)" % item for item in senders.items()) if senders else ""
    if drop.status == "sent":
        ctx.notify("delivered %d to %s" % (count, name), body, "none")
    elif drop.status == "unconfirmed":
        ctx.notify("delivery to %s unconfirmed" % name, "%s; %d not resent (herdr-fleet inbox --redeliver ID)"
                   % (drop.detail, count), "request")
    elif drop.status == "pointer":
        ctx.notify("pointed %s to %d queued messages" % (name, len(drop.waiting)), body, "none")
    elif drop.status == "failed":
        ctx.notify("delivery to %s failed" % name, drop.detail, "request")


def _on_signal(signum, frame) -> None:  # noqa: ARG001
    raise KeyboardInterrupt


def _trap_signals() -> Dict:
    """SIGTERM and SIGHUP (the pane closed) stop the watcher like Ctrl-C, so the lock goes."""
    previous: Dict = {}
    if threading.current_thread() is not threading.main_thread():
        return previous
    for name in ("SIGTERM", "SIGHUP"):
        number = getattr(signal, name, None)
        if number is None:
            continue
        try:
            previous[number] = signal.signal(number, _on_signal)
        except (OSError, ValueError):
            continue
    return previous


def _restore_signals(previous: Dict) -> None:
    for number, handler in previous.items():
        try:
            signal.signal(number, handler)
        except (OSError, ValueError, TypeError):
            pass


# -- the loop ------------------------------------------------------------------------------------


class Watcher:
    """The loop of design 18.6. ``cycle`` is one pass over the mailboxes; waiters run in
    threads (``spawn``) and deliver from there. ``cond`` guards the shared fields."""

    def __init__(self, ctx: Ctx, debounce_ms: int):
        self.ctx = ctx
        self.repo = ctx.fleet.repo
        self.debounce_s = debounce_ms / 1000.0
        self.cond = threading.Condition()
        self.out = threading.Lock()
        self.waiters: Set[str] = set()       # mailboxes with a waiter
        self.threads: List[threading.Thread] = []
        self.delivering = 0
        self.stopping = False
        self.announced: Dict[str, int] = {}  # mailbox -> the count last announced as "waiting on"
        self.noted: Dict[str, str] = {}      # mailbox -> the last line about why nobody waits on it
        self.pointed: Dict[str, Set[str]] = {}  # mailbox -> ids a pointer prompt already named
        self.retry_at: Dict[str, float] = {}
        self.owner_failed: Set[str] = set()
        self.trouble = ""                    # the last error of the loop itself, said once

    # -- output ------------------------------------------------------------------------------

    def say(self, text: str) -> None:
        if self.stopping:
            return
        with self.out:
            self.ctx.say("%s  %s" % (datetime.datetime.now().strftime("%H:%M:%S"), text))
            try:
                self.ctx.stdout.flush()
            except (AttributeError, OSError, ValueError):
                pass

    def note(self, name: str, text: str) -> None:
        """Say why a mailbox has no waiter, once until the reason changes."""
        if self.noted.get(name) != text:
            self.noted[name] = text
            self.say(text)

    # -- the loop ----------------------------------------------------------------------------

    def run(self) -> int:
        fleet = self.ctx.fleet
        self.say("watching %s (pid %d, debounce %d ms); Ctrl-C stops"
                 % (fleet.path, os.getpid(), int(self.debounce_s * 1000)))
        try:
            while True:
                try:
                    self.cycle()
                    self.trouble = ""
                except (HerdrError, OSError) as exc:  # herdr away, a file unreadable: say it once, keep going
                    text = describe(exc) if isinstance(exc, HerdrError) else str(exc)
                    if text != self.trouble:
                        self.trouble = text
                        self.say("cannot look: %s; trying again every %d s" % (text, int(CYCLE_S)))
                self.ctx.clock.sleep(CYCLE_S)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()
        with self.out:
            self.ctx.say("%s  stopped" % datetime.datetime.now().strftime("%H:%M:%S"))
        return EXIT_OK

    def stop(self) -> None:
        """No new delivery starts; one in flight finishes (its files must reach ``cur/``);
        waiters blocked in ``agent wait`` get a moment, then are left to their timeout."""
        with self.cond:
            self.stopping = True
            deadline = time.monotonic() + DELIVERY_GRACE_S
            while self.delivering and time.monotonic() < deadline:
                self.cond.wait(max(0.0, deadline - time.monotonic()))
        deadline = time.monotonic() + EXIT_GRACE_S
        for thread in list(self.threads):
            thread.join(max(0.0, deadline - time.monotonic()))

    def cycle(self) -> None:
        """One pass: notify the owner, then start a waiter for every seat or contact with a
        message that is not FYI and no waiter yet. Reads files; asks herdr only for the
        agent list, and only when a waiter is due."""
        waiting = inbox.queued(self.repo)
        if waiting.get(OWNER):
            self.owner(waiting[OWNER])
        wanted = candidates(self.ctx, waiting)
        for table in (self.announced, self.noted, self.pointed):
            for name in [n for n in table if n not in wanted]:
                del table[name]
        self.threads = [t for t in self.threads if t.is_alive()]
        with self.cond:
            due = [n for n in wanted if n not in self.waiters and not self.held(n, waiting[n])]
        if not due or self.stopping:
            return
        agents = self.ctx.herdr.agent_list()
        for name in due:
            count = len(waiting[name])
            target, problem = herdr_target(self.ctx, name, agents)
            if problem or not target:  # its kickoff (or `deliver`) carries the messages
                self.note(name, "%s: %s, %d left queued" % (name, problem or "absent", count))
                self.retry_at[name] = self.ctx.clock.monotonic() + ABSENT_RECHECK_S
                continue
            self.noted.pop(name, None)
            if self.announced.get(name) != count:
                self.announced[name] = count
                self.say("waiting on %s (%d queued)" % (name, count))
            self.start(name, target)

    def held(self, name: str, messages: List[Message]) -> bool:
        """A mailbox left alone for now: a pointer already named all of it (a new message
        lifts that), or a failure or an absent agent asked for a pause."""
        if name in self.pointed and {m.id for m in messages} <= self.pointed[name]:
            return True
        return self.retry_at.get(name, 0.0) > self.ctx.clock.monotonic()

    def start(self, name: str, target: str) -> None:
        with self.cond:
            self.waiters.add(name)
        thread = spawn(lambda: self.waiter(name, target))
        if thread is not None:
            self.threads.append(thread)

    # -- one waiter ----------------------------------------------------------------------------

    def waiter(self, name: str, target: str) -> None:
        try:
            self._wait_and_deliver(name, target)
        except Exception as exc:  # noqa: BLE001 - a waiter never takes the watcher down
            self.say("%s: %s; trying again in %d s" % (name, exc, int(RETRY_AFTER_S)))
            self.retry_at[name] = self.ctx.clock.monotonic() + RETRY_AFTER_S
        finally:
            with self.cond:
                self.waiters.discard(name)

    def _wait_and_deliver(self, name: str, target: str) -> None:
        while not self.stopping:
            try:
                agent = self.ctx.herdr.agent_wait(target, SETTLED, WAIT_TIMEOUT_MS)
            except HerdrError as exc:
                if exc.code == "timeout" or "not_found" in exc.code:
                    return  # the next pass looks again (and notes an absent agent)
                self.say("%s: %s; trying again in %d s" % (name, describe(exc), int(RETRY_AFTER_S)))
                self.retry_at[name] = self.ctx.clock.monotonic() + RETRY_AFTER_S
                return
            if self.stopping:
                return
            count = len(self.queued(name))
            if not count:
                return  # read or delivered by someone else meanwhile
            self.say("%s %s; delivering %d" % (name, agent.get("agent_status") or "idle", count))
            self.ctx.clock.sleep(self.debounce_s)
            if self.stopping:
                return
            state = agent_state(self.ctx, target)
            if state in SETTLED:
                self.deliver(name, target)
                return
            self.say("%s %s; still waiting" % (name, state))
            if state == "absent":
                return

    def queued(self, name: str) -> List[Message]:
        now = inbox.now_utc()
        return [m for m in inbox.read_box(self.repo, name, NEW) if not inbox.is_due(m, now)]

    def deliver(self, name: str, target: str) -> None:
        with self.cond:
            if self.stopping:
                return
            self.delivering += 1
        try:
            with inbox.locked(self.repo, name):
                drop = prompt_mailbox(self.ctx, name, target, SETTLED, "watch")
            self.report(name, drop)
        finally:
            with self.cond:
                self.delivering -= 1
                self.cond.notify_all()

    def report(self, name: str, drop: Drop) -> None:
        count = len(drop.carried)
        if drop.status == "sent":
            self.announced.pop(name, None)
            self.say("delivered %d to %s" % (count, name))
        elif drop.status == "unconfirmed":
            self.announced.pop(name, None)
            self.say("unconfirmed %s: %s; %d logged as delivered (unconfirmed), not resent"
                     % (name, drop.detail, count))
        elif drop.status == "pointer":
            self.pointed[name] = {m.id for m in drop.waiting}
            self.say("prompted %s with a pointer: %s" % (
                name, inbox.pointer_text(drop.waiting, drop.size, prefix=False)))
        elif drop.status == "refused":
            self.say("%s %s; still waiting" % (name, drop.state))
        elif drop.status == "failed":
            self.retry_at[name] = self.ctx.clock.monotonic() + RETRY_AFTER_S
            self.say("failed %s: %s; %d left queued, trying again in %d s"
                     % (name, drop.detail, len(drop.waiting), int(RETRY_AFTER_S)))
        else:  # empty
            return
        notify_delivery(self.ctx, name, drop)

    # -- owner -----------------------------------------------------------------------------------

    def owner(self, messages: List[Message]) -> None:
        """``owner`` gets a notification at once, as ``send`` does. After a failed one the
        same messages are not tried again; a new message is."""
        ids = {m.id for m in messages}
        if ids <= self.owner_failed:
            return
        with inbox.locked(self.repo, OWNER):
            drop = notify_owner(self.ctx)
        if drop.status == "sent":
            self.owner_failed = set()
            self.say("delivered %d to owner (notification)" % len(drop.carried))
        elif drop.status != "empty":
            self.owner_failed = ids
            self.say("owner: %s, %d left queued" % (drop.detail or "not shown", len(drop.waiting)))
