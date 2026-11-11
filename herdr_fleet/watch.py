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
  Every mailbox is waited on for idle or done only, whatever its ``queued_by``:
  nothing is typed into a working seat (design 21.1).
- When the owner is typing in the seat's pane (the presence guard, design 21.2)
  the delivery is held: one line, then the guard again every 3 s for as long as
  it holds; once it passes, the state is read again and the batch delivered if
  the seat is still idle or done, else the waiter goes back to waiting.
- Every state it sees is recorded for ``status`` (``seen``, design 20.5). With
  ``[watch] notify_blocked_after`` it also reads ``agent list`` every 30 s and
  sends one notification per blocked episode once a seat has been blocked that
  long; it never touches the seat.
- ``owner`` is notified at once (``messages.notify_owner``).
- ``--once`` delivers what can be delivered now, like ``deliver``, and exits:
  no waiters, no lock.

The board (design 24): when stdout is a terminal and ``--log`` is not given, the
strip draws ``board.render`` through ``tui`` instead of printing; ``--log``, a
stdout that is not a terminal, or a terminal curses cannot start prints the lines
as before, and ``--once`` never draws. Every delivery path above is unchanged.
In board mode each line goes to ``.herdr/watch.log`` (with its time) and to the
board's footer instead of stdout; the waiter threads post an event to a queue
with every line and every wait that returns; between passes the main loop drains
it, rebuilds the model (``board.build``) after an event or every
``BOARD_REFRESH_S``, draws, and reads keys (``BoardView``): a number or Enter is
``go`` (``ops.op_go``, the only ``agent focus`` the watcher runs, and only on a
key), ``a`` acks the selected seat's oldest debt as ``owner`` with ``--reason "from
the board"`` after a ``y``, ``d`` delivers every idle or done seat's queue now
through ``Watcher.deliver`` (no debounce), ``l`` shows the log, ``?`` the keys.
No key stops the watcher; Ctrl-C, SIGTERM and SIGHUP do, as before.

It runs only ``agent list``, ``agent wait``, ``agent get``, ``agent prompt``
(through ``seats.safe_prompt``), ``notification show`` and the presence guard's
read-only ``pane get``, ``workspace list`` and ``pane read`` (invariants 12 and
19), and for the board ``pane get``, ``agent read`` of a blocked seat and, on a
key, ``agent focus`` (invariant 22): it never prompts a blocked, unknown or
working seat, never answers a dialog, never sends a key, never redelivers an
unconfirmed message, and never starts, restarts or quits anything.
"""

from __future__ import annotations

import datetime
import json
import os
import queue
import signal
import tempfile
import threading
import time
from typing import Callable, Dict, List, Optional, Set, Tuple

from herdr_fleet import board, build_identity, context_size, inbox, limits, presence, seen, timers, tui
from herdr_fleet.context import EXIT_OK, Ctx, OpError, UsageError
from herdr_fleet.fleetfile import FleetError
from herdr_fleet.herdr import HerdrError, describe
from herdr_fleet.inbox import NEW, Message
from herdr_fleet.mailroom import (candidates, deliver_one, expire_all, herdr_target, limited_line, release_limit,
                                  sweep_expired)
from herdr_fleet.messages import (OWNER, Drop, agent_state, fleet_flag, notify_owner, op_ack, prompt_mailbox,
                                  since_label)
from herdr_fleet.seats import LIMIT_SCREEN_LINES, SETTLED, state_words
from herdr_fleet.state import (ensure_gitignore, mark_notified, now_iso, patch_entry, patch_top,
                               read_agents, read_top)

LOCK_NAME = "watch.lock"
WAIT_TIMEOUT_MS = 30000     # one `agent wait`; a timeout only lets the loop look again
CYCLE_S = 2.0               # how often the loop re-reads the mailboxes
DEFAULT_DEBOUNCE_MS = 5000
RETRY_AFTER_S = 60.0        # after a failed delivery or wait, leave that mailbox alone this long
ABSENT_RECHECK_S = WAIT_TIMEOUT_MS / 1000.0  # an absent agent is looked for as often as a wait times out
DELIVERY_GRACE_S = 60.0     # on exit, how long a delivery in flight may take to finish
EXIT_GRACE_S = 1.0          # on exit, how long waiters blocked in `agent wait` are given
BLOCKED_SCAN_S = 30.0       # with notify_blocked_after: how often `agent list` is read for blocked seats
HOLD_RECHECK_S = 3.0        # design 21.2: while the owner types in a seat's pane, look again this often
DIALOG_NOTIFY_S = 300.0     # design 39.2: one notification per dialog/menu hold this old
EXPIRE_SWEEP_S = 300.0      # design 33.1: how often the watcher expires what is due in every mailbox
LIMIT_SCAN_S = 300.0        # design 35.6: the seats' screens are looked at for a limit this often ...
LIMIT_QUIET_S = 300.0       # ... state and last logged prompt both this old, once per pair (39.3)
ALLOWANCE_READ_S = 600.0    # design 35.2: Codex's allowance is read from a rollout this often ...
ALLOWANCE_FIRST_S = 60.0    # ... the first time this long after the watcher starts, so a quiet watcher stays quiet
LOG_NAME = "watch.log"      # design 24.1: in board mode the lines go here (and to the footer)
BOARD_REFRESH_S = 5.0       # design 24.2: the board's model is rebuilt this often, and after every event
KEY_POLL_S = 0.25           # how long one read of the keyboard waits, so events show at once
NOTICE_S = 5.0              # design 24.4: the help line and the `q` line stay this long
BOARD_REASON = "from the board"  # design 24.4: the reason of an ack made with `a`
LOG_TAIL_BYTES = 64 * 1024  # the `l` view reads at most this much of the end of watch.log
WARNINGS_KEPT = 10          # the fleet page lists this many of the latest warnings
HELP = ("Enter or 1-9 open seat \u00b7 \u2191\u2193 select \u00b7 o owner \u00b7 g go \u00b7 p screen \u00b7 a ack \u00b7 "
        "d deliver \u00b7 z zoom \u00b7 l log \u00b7 Esc or q back \u00b7 Ctrl-C stops the watcher")
SCREEN_READ_LINES = board.SCREEN_LINES  # design 25.2: `p` reads the seat's last twelve lines


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
    launch = build_identity.validated((data or {}).get("launch"))
    if pid and pid_alive(pid):
        return {"running": True, "pid": pid, "since": (data or {}).get("started"), "launch": launch}
    return {"running": False, "pid": None, "since": None, "launch": launch}


def watcher_line(status: Dict) -> str:
    """``watcher: running (pid 123 since 10:00)`` or ``watcher: not running``."""
    if not status.get("running"):
        return "watcher: not running"
    since = " since %s" % since_label(status["since"]) if status.get("since") else ""
    return "watcher: running (pid %d%s)" % (status["pid"], since)


def acquire(repo: str, fleet_path: str, pid: Optional[int] = None) -> Dict:
    """Write ``.herdr/watch.lock``, or raise OpError when a live watcher holds it. The file
    is created with a hard link, so of two watchers starting at once only one gets it."""
    from herdr_fleet.process_identity import identity
    data = {"pid": pid or os.getpid(), "started": now_iso(), "fleet": fleet_path,
            "launch": dict(build_identity.current()), "control_gate_protocol": 1,
            "process_identity": identity(pid or os.getpid())}
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


def fleet_mtime(path: str) -> Optional[int]:
    """The fleet file's modification time in ns, or None when it cannot be read."""
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def is_terminal(stream) -> bool:
    """True when ``stream`` is a terminal (design 24.1: the board is drawn only there)."""
    try:
        return bool(stream.isatty())
    except (AttributeError, OSError, ValueError):
        return False


def op_watch(ctx: Ctx, debounce_ms: Optional[int] = None, once: bool = False, log: bool = False) -> int:
    """Design 18.6. Runs until Ctrl-C (exit 0), or one pass with ``--once``. Design 24.1:
    on a terminal, without ``--log``, it draws the board instead of printing its lines."""
    debounce = DEFAULT_DEBOUNCE_MS if debounce_ms is None else debounce_ms
    if debounce < 0:
        raise UsageError("--debounce takes milliseconds, 0 or more")
    fleet = ctx.fleet
    if once:
        return watch_once(ctx)
    lock = acquire(fleet.repo, fleet.path)
    previous: Dict = {}
    screen = None
    watcher: Optional[Watcher] = None
    try:
        previous = _trap_signals()
        if not log and is_terminal(ctx.stdout):
            screen = tui.open_screen(ctx.env, ctx.stdout)  # None: curses cannot start; print as before
        watcher = Watcher(ctx, debounce, screen)
        code = watcher.run()
    finally:
        if watcher is not None:
            watcher.unhook()
        if screen is not None:
            screen.close()
        _restore_signals(previous)
        release(fleet.repo, lock["pid"])
    if screen is not None and watcher is not None and watcher.final:
        try:
            ctx.say(watcher.final)  # the board is gone: the terminal shows how the watcher ended
        except (OSError, ValueError):
            pass  # the pane closed (SIGHUP): nobody is there to read it
    return code


def watch_once(ctx: Ctx) -> int:
    with timers.cycle(ctx) as lines:
        for line in lines:
            ctx.say(line)
        return _watch_once(ctx)


def _watch_once(ctx: Ctx) -> int:
    """``--once``: what ``deliver`` without names would deliver, logged via ``watch``,
    with the watcher's notification for each mailbox delivered. Exit 1 when a prompt
    failed or may not have arrived, as ``deliver``; else 0."""
    agents = ctx.herdr.agent_list()
    expire_all(ctx)
    waiting = inbox.queued(ctx.fleet.repo, ctx.fleet.fyi_ttl)
    order = candidates(ctx, waiting) + ([OWNER] if waiting.get(OWNER) else [])
    if not order:
        ctx.say("nothing to deliver")
        return EXIT_OK
    codes = [EXIT_OK]
    timer_states = {str(a.get("name")): str(a.get("agent_status") or "unknown") for a in agents}
    typing = []
    for name in order:
        held = limited_line(ctx, name, len(waiting.get(name, [])))  # design 35.2: the hold stands
        if held:
            ctx.say(held)
            continue
        line, code, drop = deliver_one(ctx, name, agents, waiting.get(name, []), via="watch")
        ctx.say(line)
        codes.append(code)
        if drop is not None and name != OWNER:
            notify_delivery(ctx, name, drop)
            if drop.status in ("typing", "coordination"):
                typing.append(name)
    if ctx.fleet.timers:
        pending = [m for queue in inbox.queued(ctx.fleet.repo, ctx.fleet.fyi_ttl).values() for m in queue]
        for line in timers.waiting_lines(pending, timer_states, typing):
            ctx.say(line)
    return max(codes)


def read_state_allowance(repo: str) -> Dict:
    """The watcher's last Codex allowance reading, as the state file keeps it (design 35.2)."""
    value = read_top(repo, "codex_allowance")
    return value if isinstance(value, dict) else {}


def notify_delivery(ctx: Ctx, name: str, drop: Drop) -> None:
    """The owner's one-liner after a delivery to a seat: ``delivered 3 to sample-dev``."""
    count = len(drop.carried)
    senders: Dict[str, int] = {}
    for message in drop.carried or drop.waiting:
        senders[message.sender] = senders.get(message.sender, 0) + 1
    body = "from " + ", ".join("%s (%d)" % item for item in senders.items()) if senders else ""
    if drop.status == "sent":
        ctx.notify("delivered %d to %s" % (count, name), body, "none")
    elif drop.status == "unconfirmed":
        ctx.notify("delivery to %s unconfirmed" % name, "%s; %d not resent (herdr-fleet inbox%s --redeliver ID)"
                   % (drop.detail, count, fleet_flag(ctx)), "request")
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


class LineSink:
    """A text stream that hands each complete line to ``emit``: in board mode, what other
    code prints (``op_ack``'s lines, warnings) goes to the log and the footer, never to
    the terminal curses is drawing on."""

    def __init__(self, emit: Callable[[str], None]):
        self.emit = emit
        self.lock = threading.Lock()
        self.buffer = ""

    def write(self, text: str) -> int:
        with self.lock:
            self.buffer += str(text)
            lines = self.buffer.split("\n")
            self.buffer = lines.pop()
        for line in lines:
            if line.strip():
                self.emit(line)
        return len(text)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False


class Watcher:
    """The loop of design 18.6. ``cycle`` is one pass over the mailboxes; waiters run in
    threads (``spawn``) and deliver from there. ``cond`` guards the shared fields. With a
    ``screen`` (design 24) the lines go to ``.herdr/watch.log`` and the board, and the
    pause between passes is the board's (``BoardView.wait``)."""

    def __init__(self, ctx: Ctx, debounce_ms: int, screen=None):
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
        self.blocked_after = ctx.fleet.notify_blocked_after  # seconds, or None (design 20.5)
        self.next_scan = 0.0
        self.next_sweep = 0.0                    # design 33.1: the first pass sweeps
        self.blocked_since: Dict[str, str] = {}  # seat -> since, when no state file records it
        self.notified: Set[str] = set()          # "seat since" of blocked episodes already notified
        # design 24: the board
        self.screen = screen
        self.events: "queue.Queue[str]" = queue.Queue()  # the threads post here; the main loop drains it
        self.holds: Dict[str, str] = {}          # mailbox -> the presence guard's detail, while held
        self.limit_holds: Dict[str, str] = {}    # design 35.2: mailbox -> why a limit holds it (shown as a hold)
        self.limit_read: Dict[str, Tuple[str, str]] = {}  # seat -> (state since, last prompt) read for a limit
        self.last_prompt: Dict[str, str] = {}     # 39.3: seat -> newest logged prompt's timestamp
        self.prompt_log_offset = 0
        self.prompt_log_signature: Optional[Tuple[int, int, int]] = None  # size, mtime, inode
        self.limit_said: Set[str] = set()        # "name kind at" of limit episodes and resets already notified
        self.next_limit_scan = ctx.clock.monotonic() + LIMIT_SCAN_S  # when a stuck-working seat is next looked at
        self.next_allowance = ctx.clock.monotonic() + ALLOWANCE_FIRST_S  # when Codex's allowance is next read
        self.failed_until: Dict[str, float] = {}  # mailbox -> when a failed delivery or wait is tried again
        self.last_event: Optional[board.Event] = None
        self.started = ctx.clock.monotonic()
        self.final = ""                          # board mode: the `stopped` line, printed once the board is gone
        self.log_path = os.path.join(self.repo, ".herdr", LOG_NAME)
        self.view: Optional[BoardView] = None
        self.hooked = None
        if screen is not None:
            self.hooked = (ctx.stdout, ctx.stderr)
            ctx.stdout = LineSink(self.say)
            ctx.stderr = LineSink(self.warned)
            self.view = BoardView(self, screen)
        self.last_warning = ""
        self.warnings: List[str] = []           # the last warnings other code wrote (the fleet page shows them)
        self.lock_since = str((read_lock(self.repo) or {}).get("started") or "")
        self.timer_aware = bool(ctx.fleet.timers) or os.path.exists(timers.state_path(self.repo))
        self.timer_mtime = fleet_mtime(ctx.fleet.path)
        self.timer_error = ""

    def unhook(self) -> None:
        """Give the context its own streams back (board mode)."""
        if self.hooked is not None:
            self.ctx.stdout, self.ctx.stderr = self.hooked
            self.hooked = None

    # -- output ------------------------------------------------------------------------------

    def say(self, text: str, error: bool = False, shown: Optional[str] = None) -> None:
        """One line of the watcher's output, with the time: printed, or in board mode
        appended to ``.herdr/watch.log`` and shown in the footer (design 24.1); ``shown`` is
        a shorter text for the footer, the log keeping ``text`` (25.8)."""
        if self.stopping:
            return
        now = datetime.datetime.now()
        line = "%s  %s" % (now.strftime("%H:%M:%S"), text)
        if self.screen is None:
            with self.out:
                self.ctx.say(line)
                try:
                    self.ctx.stdout.flush()
                except (AttributeError, OSError, ValueError):
                    pass
            return
        with self.out:
            self.write_log(line)
            self.last_event = board.Event(now.strftime("%H:%M"), shown or text, error)
        if not (self.view is not None and self.view.building
                and threading.current_thread() is threading.main_thread()):
            self.events.put("line")  # a line said while the board builds asks for no second build

    def warned(self, text: str) -> None:
        """A warning other code wrote to stderr (board mode): a line, in red, once until it
        changes, since the board's refresh would repeat it every few seconds."""
        if text != self.last_warning:
            self.last_warning = text
            self.warnings = (self.warnings + [text])[-WARNINGS_KEPT:]
            self.say(text, error=True)

    def write_log(self, line: str) -> None:
        try:
            with open(self.log_path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass  # the footer still shows it

    def poke(self) -> None:
        """An event without a line (a wait returned): the board is rebuilt."""
        if self.screen is not None:
            self.events.put("event")

    def log_tail(self, count: int) -> List[str]:
        """The last ``count`` lines of ``.herdr/watch.log`` (the ``l`` view)."""
        return board.tail_lines(self.log_path, count, LOG_TAIL_BYTES)

    def snapshot(self) -> board.WatcherState:
        """The watcher's own state for the board (design 24.2)."""
        now = self.ctx.clock.monotonic()
        with self.cond:
            state = board.WatcherState(
                pid=os.getpid(), since=self.lock_since, warnings=list(self.warnings),
                watching=max(0.0, now - self.started), waiting=sorted(self.waiters),
                holds=dict(self.limit_holds, **self.holds),
                retries={n: t - now for n, t in self.failed_until.items() if t > now},
                pointed=sorted(self.pointed), event=self.last_event, trouble=self.trouble,
                cache=self.view.cache if self.view is not None else None)
        return state

    def note(self, name: str, text: str) -> None:
        """Say why a mailbox has no waiter, once until the reason changes."""
        if self.noted.get(name) != text:
            self.noted[name] = text
            self.say(text)

    # -- the loop ----------------------------------------------------------------------------

    def run(self) -> int:
        fleet = self.ctx.fleet
        self.say("watching %s (pid %d, debounce %d ms); Ctrl-C stops"
                 % (fleet.path, os.getpid(), int(self.debounce_s * 1000)),
                 shown="watching %s (pid %d)" % (os.path.basename(os.path.normpath(fleet.repo)), os.getpid()))
        try:
            while True:
                try:
                    self.cycle()
                    self.trouble = ""
                except (HerdrError, OSError, OpError) as exc:  # say it once, keep going
                    text = describe(exc) if isinstance(exc, HerdrError) else str(exc)
                    if text != self.trouble:
                        self.trouble = text
                        self.say("cannot look: %s; trying again every %d s" % (text, int(CYCLE_S)), error=True)
                self.pause(CYCLE_S)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()
        line = "%s  stopped" % datetime.datetime.now().strftime("%H:%M:%S")
        with self.out:
            if self.screen is None:
                self.ctx.say(line)
            else:
                self.write_log(line)
                self.final = line
        return EXIT_OK

    def pause(self, seconds: float) -> None:
        """Between passes: sleep, or in board mode draw and read keys meanwhile."""
        if self.view is None:
            self.ctx.clock.sleep(seconds)
        else:
            self.view.wait(seconds)

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
        if self.timer_aware:
            self.reload_timers()
        with timers.cycle(self.ctx) as lines:
            for line in lines:
                self.say(line)
        self.sweep()  # after the timer cycle has released its locks, before the mailboxes are read
        waiting = inbox.queued(self.repo, self.ctx.fleet.fyi_ttl)
        self.limit_cycle(waiting)
        if waiting.get(OWNER):
            self.owner(waiting[OWNER])
        if self.blocked_after and self.ctx.clock.monotonic() >= self.next_scan and not self.stopping:
            self.next_scan = self.ctx.clock.monotonic() + BLOCKED_SCAN_S
            self.observe_list(self.ctx.herdr.agent_list())
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
        self.observe_list(agents)
        states = {str(a.get("name")): str(a.get("agent_status") or "unknown") for a in agents}
        for line in timers.waiting_lines([m for queue in waiting.values() for m in queue], states, self.holds):
            if not line.endswith(("idle", "done")):
                self.note("timer:" + line.split()[1], line)
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

    def sweep(self) -> None:
        """Design 33.1: at most once every EXPIRE_SWEEP_S, expire what is due in every
        mailbox whose lock is free (``mailroom.sweep_expired``: no herdr command, no timer
        lock, no waiting for a lock). Never called with a mailbox lock held."""
        if self.ctx.clock.monotonic() < self.next_sweep:
            return
        self.next_sweep = self.ctx.clock.monotonic() + EXPIRE_SWEEP_S
        gone = sweep_expired(self.ctx)
        if gone:
            used = {reason for reasons in gone.values() for reason in reasons}
            self.say("expired %d queued: %s (%s)"
                     % (sum(len(reasons) for reasons in gone.values()),
                        ", ".join("%s %d" % (name, len(reasons)) for name, reasons in gone.items()),
                        ", ".join(r for r in ("ttl", "fyi_ttl") if r in used)))


    # -- provider limits (design 35.2) ---------------------------------------------------------

    def mark_limit(self, name: str, kind: str, line: str) -> None:
        """Record a limit the screen showed, unless an episode for this kind is on record already;
        then notify (one notification per episode)."""
        entry = read_agents(self.repo).get(name) or {}
        current = entry.get("limited")
        now = inbox.now_utc()
        if limits.counts(current, now) and current.get("kind") == kind:
            return  # the same episode: recorded and notified already
        record = limits.new_record(kind, line, now)
        record["notified"] = True
        recorded = patch_entry(self.repo, name, limits.replaced(entry, record, now))
        episode = "%s %s %s" % (name, kind, line)
        if recorded or episode not in self.limit_said:  # with no state entry to remember it in, say it once
            self.limit_said.add(episode)
            self.limit_notice(name, record)

    def limit_notice(self, name: str, record: Dict) -> None:
        """``Fleet: NAME hit its <kind> limit``: the body gives the line and the command that moves
        the seat, or says no fallback is defined."""
        seat = self.ctx.fleet.seat(name)
        when = limits.reset_time(record)
        reset = " (resets %s%s)" % (limits.reset_label(when), ", " + record["resets_hint"] if record.get("resets_hint")
                                    else "") if when else ""
        title = "Fleet: %s hit its %s limit" % (name, record["kind"])
        body = "%s%s. %s" % (record.get("line", ""), reset, self.fallback_words(seat, record["kind"], name))
        self.say("%s%s; holding its queue" % (title, reset), error=True)
        self.ctx.notify(title, body, "request")

    def fallback_words(self, seat, kind: str, name: str) -> str:
        """The command for a tier seat (35.2): ``herdr-fleet swap NAME --fallback``, or why there is none."""
        if seat is None or not seat.prefer:
            return "no fallback defined"
        limited = limits.kinds_limited(read_agents(self.repo))
        if not limits.fallback_candidates(seat, limited, kind):
            return "no fallback available: every other kind in prefer is limited"
        return "Run: herdr-fleet swap %s --fallback%s" % (name, fleet_flag(self.ctx))

    def hold_limit(self, name: str, record: Dict, count: int) -> None:
        """Hold the mailbox of a limited seat through the hold mechanism (21.2): the board shows
        ``holding: limited (claude)``; it ends with a swap, the reset time, or ``deliver NAME``."""
        when = limits.reset_time(record)
        detail = "limited (%s); %s" % (record.get("kind"), "resets %s" % limits.reset_label(when) if when
                                       else "no reset time known; deliver %s releases it" % name)
        with self.cond:
            self.limit_holds[name] = detail
        self.note(name, "%s limited (%s): holding %d%s" % (name, record.get("kind"), count,
                                                          "" if when else "; deliver %s releases it" % name))

    def limit_cycle(self, waiting: Dict[str, List[Message]]) -> None:
        """Each pass (design 35.2 and 35.6): the holds follow the state file (a swap, a reset time
        that passed or ``deliver NAME`` ends one), resets are notified once, every LIMIT_SCAN_S the
        quiet seats' screens are looked at for a limit line (``scan_limits``, whatever their queues),
        and every ALLOWANCE_READ_S Codex's allowance is read. Reads files; asks herdr only when one
        of those is due."""
        fleet = self.ctx.fleet
        try:
            recorded = read_agents(self.repo)
        except OSError:
            return
        now = inbox.now_utc()
        holds: Dict[str, str] = {}
        for seat in fleet.seats:
            entry = recorded.get(seat.name) or {}
            for record in limits.records(entry):
                if limits.passed(record, now) and not record.get("reset_notified"):
                    self.reset_notice(seat, entry, record, now)
            record = entry.get("limited")
            queued = [m for m in waiting.get(seat.name, []) if not m.fyi]
            if queued and limits.holds(record, entry.get("kind") or seat.kind, now):
                self.hold_limit(seat.name, record, len(queued))
                holds[seat.name] = self.limit_holds[seat.name]
        with self.cond:
            for name in [n for n in self.limit_holds if n not in holds]:
                del self.limit_holds[name]
        if self.stopping:
            return
        clock = self.ctx.clock.monotonic()
        scan = clock >= self.next_limit_scan and any(limits.has_patterns(k) for k in fleet.kinds())
        read = "codex" in fleet.kinds() and clock >= self.next_allowance
        if not (scan or read):
            return
        agents = self.ctx.herdr.agent_list()
        live = {str(a["name"]): a for a in agents if a.get("name")}
        if scan:
            self.next_limit_scan = clock + LIMIT_SCAN_S
            self.scan_limits(live)
        if read:
            self.next_allowance = clock + ALLOWANCE_READ_S
            self.read_allowance(live)

    def reset_notice(self, seat, entry: Dict, record: Dict, now: datetime.datetime) -> None:
        """A recorded reset time passed: one notification, then the record is marked so it is not
        said again (also across a restarted watcher)."""
        kind = str(record.get("kind"))
        said = "reset %s %s %s" % (seat.name, kind, record.get("at"))
        if said in self.limit_said:  # the state file could not remember it: once per watcher
            return
        self.limit_said.add(said)
        failed = isinstance(entry.get("failover"), dict) and entry["failover"].get("to") != kind
        title = ("Fleet: %s limit reset; swap %s returns it" % (kind, seat.name) if failed
                 else "Fleet: %s limit reset; %s's queue goes out again" % (kind, seat.name))
        marked = [dict(r, reset_notified=True) if r is record or r.get("kind") == kind else r
                  for r in limits.records(entry)]
        fields = {"limited": marked[0], "limited_also": marked[1:]} if entry.get("limited") else {
            "limited_also": marked}
        patch_entry(self.repo, seat.name, fields)
        self.say(title)
        self.ctx.notify(title, "%s, %s limit hit at %s" % (seat.name, kind, limits.since_label(record)), "request")

    def refresh_last_prompts(self) -> None:
        """Design 39.3: advance through the append-only message log only when it changed.
        Keep just the newest timestamp of text sent to each seat, never message content."""
        path = os.path.join(self.repo, ".herdr", "messages.jsonl")
        try:
            stat = os.stat(path)
        except OSError:
            return
        signature = (stat.st_size, stat.st_mtime_ns, stat.st_ino)
        if signature == self.prompt_log_signature:
            return
        if (stat.st_size < self.prompt_log_offset
                or (self.prompt_log_signature is not None and self.prompt_log_signature[2] != stat.st_ino)):
            self.prompt_log_offset = 0
        try:
            with open(path, "rb") as handle:
                handle.seek(self.prompt_log_offset)
                data = handle.read()
        except OSError:
            return
        self.prompt_log_signature = signature
        complete = data.rfind(b"\n") + 1
        self.prompt_log_offset += complete
        for raw in data[:complete].splitlines():
            try:
                entry = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(entry, dict):
                continue
            kind = entry.get("kind")
            prompted = (kind in ("delivered", "kickoff")
                        or (kind == "send" and entry.get("delivery") in ("sent", "unconfirmed")))
            name, when = entry.get("to"), entry.get("time")
            if prompted and isinstance(name, str) and isinstance(when, str):
                self.last_prompt[name] = when

    def scan_limits(self, live: Dict[str, Dict]) -> None:
        """Design 35.6 and 39.3: read a quiet seat once per (state since, last prompt) pair.
        Both must be LIMIT_QUIET_S old. The last twelve lines are searched for the provider's
        limit line and kept nowhere. A seat already held is not read; blocked and unknown seats
        never are."""
        self.refresh_last_prompts()
        states = {n: str(a.get("agent_status") or "unknown") for n, a in live.items()}
        names = [s.name for s in self.ctx.fleet.seats if s.name in live]
        try:
            records = seen.observe(self.ctx, {n: states[n] for n in names})
        except Exception:  # noqa: BLE001 - a state file problem never stops the watcher
            return
        now = inbox.now_utc()
        for name in names:
            agent = live[name]
            kind = str(agent.get("agent") or "")
            record = records.get(name) or {}
            age = seen.age_of(record, states[name])
            prompted = inbox.parse_time(self.last_prompt.get(name))
            prompt_age = (now - prompted).total_seconds() if prompted is not None else None
            key = (str(record.get("since") or ""), self.last_prompt.get(name, ""))
            if (states[name] not in ("idle", "done", "working") or not limits.has_patterns(kind) or age is None
                    or age < LIMIT_QUIET_S or (prompt_age is not None and prompt_age < LIMIT_QUIET_S)
                    or self.limit_read.get(name) == key or limits.holding(self.repo, name, kind) is not None):
                continue
            self.limit_read[name] = key
            try:
                try:
                    screen = self.ctx.herdr.agent_read(name, LIMIT_SCREEN_LINES)
                except HerdrError as exc:
                    if exc.code != "agent_not_idle" or not agent.get("pane_id"):
                        raise
                    screen = self.ctx.herdr.pane_read(str(agent["pane_id"]), LIMIT_SCREEN_LINES)
            except HerdrError:
                continue
            line = limits.limit_line(kind, screen)
            del screen
            if line:
                self.mark_limit(name, kind, line)
                found = limits.holding(self.repo, name, kind)
                if found is not None:
                    self.hold_limit(name, found, len(self.queued(name)))

    def read_allowance(self, live: Dict[str, Dict]) -> None:
        """Design 35.2: Codex's allowance belongs to the account, so one rollout answers for all
        its seats: the first live Codex seat whose rollout holds a ``rate_limits`` record. The
        reading is kept in the state file for ``status``; at or above ``failover_at_percent`` one
        warning per reset window names every Codex seat."""
        codex = [n for n, a in live.items() if str(a.get("agent")) == "codex" and self.ctx.fleet.seat(n)]
        reading = None
        for name in sorted(codex, key=lambda n: self.ctx.fleet.seat(n).index):
            reading = context_size.seat_allowance(live[name], self.ctx.env)
            if reading is not None:
                break
        if reading is None:
            return
        recorded = read_state_allowance(self.repo)
        keep = {k: reading[k] for k in ("used_percent", "resets_at", "window_minutes", "at") if k in reading}
        keep["read_at"] = inbox.iso_local(inbox.now_utc())
        if recorded.get("warned_for") is not None and recorded.get("warned_for") == reading.get("resets_at"):
            keep["warned_for"] = recorded["warned_for"]
        threshold = self.ctx.fleet.failover_at_percent
        if reading["used_percent"] >= threshold and keep.get("warned_for") != reading.get("resets_at", 0):
            keep["warned_for"] = reading.get("resets_at", 0)
            self.allowance_notice(reading, sorted(codex, key=lambda n: self.ctx.fleet.seat(n).index))
        patch_top(self.repo, "codex_allowance", keep)

    def allowance_notice(self, reading: Dict, names: List[str]) -> None:
        when = limits.epoch_time(reading.get("resets_at"))
        percent = int(reading["used_percent"])
        title = "Fleet: Codex at %d%%%s" % (percent, " (resets %s)" % limits.reset_label(when) if when else "")
        parts = []
        for name in names:
            parts.append("%s: %s" % (name, self.fallback_words(self.ctx.fleet.seat(name), "codex", name)))
        self.say("%s; %s" % (title, "; ".join(parts)), error=True)
        self.ctx.notify(title, "; ".join(parts), "request")

    def reload_timers(self) -> None:
        """Timer-aware watchers reload in log mode too; pristine fleets keep their old path."""
        if self.view is not None:
            self.view.reread_fleet()
            return
        mtime = fleet_mtime(self.ctx.fleet.path)
        if mtime is None or mtime == self.timer_mtime:
            return
        self.timer_mtime = mtime
        try:
            fleet = self.ctx.reload_fleet()
        except FleetError as exc:
            self.timer_error = "; ".join(exc.errors)
            self.say("fleet file does not parse; still using the last good one: " + self.timer_error, error=True)
            return
        self.timer_error = ""
        self.blocked_after = fleet.notify_blocked_after
        self.say("fleet file %s changed; reloaded" % fleet.path)

    def held(self, name: str, messages: List[Message]) -> bool:
        """A mailbox left alone for now: a pointer already named all of it (a new message
        lifts that), or a failure or an absent agent asked for a pause."""
        if name in self.pointed and {m.id for m in messages} <= self.pointed[name]:
            return True
        if name in self.limit_holds:  # design 35.2: the provider refuses turns; nothing is delivered
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
            self.retry_later(name)
            self.say("%s: %s; trying again in %d s" % (name, exc, int(RETRY_AFTER_S)), error=True)
        finally:
            with self.cond:
                self.waiters.discard(name)
            self.poke()

    def retry_later(self, name: str) -> None:
        """Leave a mailbox alone for RETRY_AFTER_S after a failure (the board says so)."""
        self.retry_at[name] = self.ctx.clock.monotonic() + RETRY_AFTER_S
        self.failed_until[name] = self.retry_at[name]

    def _wait_and_deliver(self, name: str, target: str) -> None:
        while not self.stopping:
            try:  # idle or done only: nothing is typed into a working seat (design 21.1)
                agent = self.ctx.herdr.agent_wait(target, SETTLED, WAIT_TIMEOUT_MS)
            except HerdrError as exc:
                if exc.code == "timeout" or "not_found" in exc.code:
                    return  # the next pass looks again (and notes an absent agent)
                self.retry_later(name)
                self.say("%s: %s; trying again in %d s" % (name, describe(exc), int(RETRY_AFTER_S)), error=True)
                return
            self.poke()
            if self.stopping:
                return
            status = str(agent.get("agent_status") or "idle")
            self.observe(name, status, agent)
            waiting = self.queued(name)
            if not waiting:
                return  # read or delivered by someone else meanwhile
            record = limits.holding(self.repo, name, str(agent.get("agent") or ""))
            if record is not None:  # design 35.2: a limit holds it (recorded since this wait began)
                self.hold_limit(name, record, len(waiting))
                return
            self.say("%s %s; delivering %d" % (name, status, len(waiting)))
            self.ctx.clock.sleep(self.debounce_s)
            if self.stopping:
                return
            state = agent_state(self.ctx, target)
            self.observe(name, state)
            if state not in SETTLED:  # working, blocked or unknown: wait again
                self.say("%s %s; still waiting" % (name, state_words(state)))
                if state == "absent":
                    return
                continue
            drop = self.deliver(name, target)
            if drop is None or drop.status not in ("typing", "coordination") or not self.hold(name, target, drop):
                return

    def hold(self, name: str, target: str, drop: Drop) -> bool:
        """Design 21.2: the owner is typing in the seat's pane, so nothing was sent. Say so
        once, then run the guard every HOLD_RECHECK_S (``pane get`` first) for as long as it
        holds, with no cap. When it passes, read the state again: still idle or done
        delivers; anything else goes back to waiting. True when the waiter should wait
        for idle or done again."""
        with self.cond:
            self.holds[name] = drop.detail
        notified = False
        dialog_since = self.ctx.clock.monotonic() if presence.is_dialog(drop.detail) else None
        delay = self.blocked_after if self.blocked_after is not None else DIALOG_NOTIFY_S
        try:
            for line in timers.waiting_lines(drop.waiting, {}, [name]):
                self.say(line)
            self.say("%s %s; %s, holding %d" % (name, drop.state or "idle", drop.detail, len(drop.waiting)))
            while not self.stopping:
                self.ctx.clock.sleep(HOLD_RECHECK_S)
                waiting = self.queued(name)
                if self.stopping or not waiting:  # stopped, or read or delivered meanwhile
                    return False
                now = self.ctx.clock.monotonic()
                if presence.is_dialog(drop.detail):
                    if dialog_since is None:
                        dialog_since = now
                else:
                    dialog_since = None
                if not notified and dialog_since is not None and now - dialog_since >= delay:
                    notified = True
                    pane = str(drop.agent.get("pane_id") or "")
                    title = "%s has a dialog or menu open (%s): %d queued wait" % (name, pane, len(waiting))
                    self.say(title, error=True)
                    self.ctx.notify(title, "answer or close it there; herdr-fleet sends it nothing until its input is back",
                                    "request")
                detail = presence.typing_at(self.ctx.herdr, drop.agent) if drop.status == "typing" else ""
                if detail:
                    drop.detail = detail
                    with self.cond:
                        self.holds[name] = detail
                    continue
                state = agent_state(self.ctx, target)
                self.observe(name, state)
                if state == "absent":  # gone: the next pass notes it, its kickoff carries the queue
                    return False
                if state not in SETTLED:
                    self.say("%s %s; waiting" % (name, state_words(state)))
                    return True
                again = self.deliver(name, target)
                if again is None or again.status not in ("typing", "coordination"):
                    return False
                drop = again  # typing again since the look: keep holding, without a second line
                with self.cond:
                    self.holds[name] = drop.detail
            return False
        finally:
            with self.cond:
                self.holds.pop(name, None)
            self.poke()

    def queued(self, name: str) -> List[Message]:
        now = inbox.now_utc()
        return [m for m in inbox.read_box(self.repo, name, NEW) if not inbox.is_due(m, now, self.ctx.fleet.fyi_ttl)]

    def deliver(self, name: str, target: str) -> Optional[Drop]:
        """The batch to an idle or done seat, as ``deliver`` does; None once stopping."""
        with self.cond:
            if self.stopping:
                return None
            self.delivering += 1
        try:
            with inbox.locked(self.repo, name):
                drop = prompt_mailbox(self.ctx, name, target, SETTLED, "watch")
            self.report(name, drop)
            return drop
        finally:
            with self.cond:
                self.delivering -= 1
                self.cond.notify_all()

    # -- states seen (design 20.5) ------------------------------------------------------------

    def observe_list(self, agents: List[Dict]) -> None:
        """Every seat's state from an ``agent list`` answer (a seat not listed is absent)."""
        live = {str(a["name"]): a for a in agents if a.get("name")}
        for seat in self.ctx.fleet.seats:
            agent = live.get(seat.name)
            self.observe(seat.name, str(agent.get("agent_status") or "unknown") if agent else "absent", agent)

    def observe(self, name: str, status: str, agent: Optional[Dict] = None) -> None:
        """Record a seat's state, and with ``notify_blocked_after`` tell the owner once per
        blocked episode that has lasted that long. Read-only towards the seat."""
        if self.ctx.fleet.seat(name) is None:
            return
        try:
            record = seen.observe(self.ctx, {name: status}).get(name)
        except Exception as exc:  # noqa: BLE001 - a state file problem never stops a delivery
            self.say("cannot record %s's state: %s" % (name, exc), error=True)
            record = None
        if not self.blocked_after:
            return
        with self.cond:
            if status != "blocked":
                self.blocked_since.pop(name, None)
                return
            recorded = record if (record or {}).get("state") == "blocked" else None
            since = (recorded or {}).get("since") or self.blocked_since.setdefault(
                name, inbox.iso_local(inbox.now_utc()))
            age = seen.age_of({"state": "blocked", "since": since}, "blocked")
            episode = "%s %s" % (name, since)
            if (age is None or age < self.blocked_after or episode in self.notified
                    or (recorded or {}).get("notified")):
                return
            self.notified.add(episode)
        if recorded:
            try:
                mark_notified(self.repo, name, since)
            except OSError:
                pass
        pane = str((agent or {}).get("pane_id") or "") or self.pane_of(name)
        title = "%s blocked %s (%s): answer its prompt" % (name, inbox.age_label(age), pane or "pane unknown")
        self.say(title)
        self.ctx.notify(title, "blocked since %s; herdr-fleet sends it nothing while it is blocked"
                        % since_label(since), "request")

    def pane_of(self, name: str) -> str:
        try:
            return str(self.ctx.herdr.agent_get(name).get("pane_id") or "")
        except HerdrError:
            return ""

    def report(self, name: str, drop: Drop) -> None:
        count = len(drop.carried)
        if drop.status == "sent":
            self.announced.pop(name, None)
            self.say("delivered %d to %s" % (count, name))
        elif drop.status == "unconfirmed":
            self.announced.pop(name, None)
            self.say("unconfirmed %s: %s; %d logged as delivered (unconfirmed), not resent"
                     % (name, drop.detail, count), error=True)
        elif drop.status == "pointer":
            self.pointed[name] = {m.id for m in drop.waiting}
            self.say("prompted %s with a pointer: %s" % (
                name, inbox.pointer_text(drop.waiting, drop.size, prefix=False)))
        elif drop.status == "refused":
            self.say("%s %s; still waiting" % (name, state_words(drop.state)))
        elif drop.status in ("typing", "coordination"):
            return
        elif drop.status == "failed":
            self.retry_later(name)
            self.say("failed %s: %s; %d left queued, trying again in %d s"
                     % (name, drop.detail, len(drop.waiting), int(RETRY_AFTER_S)), error=True)
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
            self.say("owner: %s, %d left queued" % (drop.detail or "not shown", len(drop.waiting)), error=True)


# -- the board (design 24) ---------------------------------------------------------------------------


class BoardView:
    """The board in the strip: between the watcher's passes it drains the event queue,
    rebuilds the model after an event or every BOARD_REFRESH_S (and reloads the open page,
    design 25), draws the top of the view stack when the picture changed, and reads keys
    and clicks (design 24.4 and 25.1). The views decide what a key means; what touches
    herdr or the log goes through an existing function: ``ops.op_go``, ``ops.op_peek``,
    ``messages.op_ack`` with a reason, ``Watcher.deliver``, and ``tui.zoom`` on the strip's
    own pane. Keys reach the strip only while it has focus, so nothing depends on them."""

    def __init__(self, watcher: Watcher, screen):
        self.w = watcher
        self.screen = screen
        self.colours = bool(getattr(screen, "colours", False)) and tui.colours_wanted(watcher.ctx.env)
        self.cache = board.Cache()
        self.model: Optional[board.Board] = None
        self.state = board.WatcherState()
        self.root = board.BoardPage()
        self.stack: List[board.View] = [self.root]
        self.fleet_mtime = fleet_mtime(watcher.ctx.fleet.path)  # design 26.6: the file as loaded
        self.fleet_error = ""                                     # the last edit that did not parse
        self.problems = ""
        self.stale = True
        self.building = False
        self.next_refresh = 0.0
        self.notice = ""
        self.notice_until: Optional[float] = None
        self.confirm: Optional[board.Ref] = None
        self.log_view = False
        self.log_lines: Optional[List[str]] = None  # the log view's lines, read again after an event
        self.log_for = 0                             # ... or when the height changed
        self.drawn: Optional[List] = None

    @property
    def clock(self):
        return self.w.ctx.clock

    @property
    def top(self) -> board.View:
        return self.stack[-1]

    @property
    def selected(self) -> Optional[str]:
        return self.root.selection

    def wait(self, seconds: float) -> None:
        """Run the board for ``seconds`` (one pause of the watcher's loop)."""
        deadline = self.clock.monotonic() + seconds
        while True:
            self.drain()
            if self.stale or self.clock.monotonic() >= self.next_refresh:
                self.refresh()
            if self.notice_until is not None and self.clock.monotonic() >= self.notice_until:
                self.notice, self.notice_until = "", None
            self.draw()
            left = deadline - self.clock.monotonic()
            if left <= 0:
                return
            key = self.screen.getch(max(1, int(round(min(left, KEY_POLL_S) * 1000))))
            if key is not None:
                self.key(key)

    def drain(self) -> None:
        while True:
            try:
                self.w.events.get_nowait()
            except queue.Empty:
                return
            self.stale = True
            self.log_lines = None  # the log view reads the file again

    def reread_fleet(self) -> None:
        """Design 26.6: when the fleet file changed since it was loaded, read it again, so
        contacts, labels, ``top`` and ``restart_hint_tokens`` follow an edit within a refresh.
        A file that does not parse keeps the last good fleet; the error stays in the footer
        (red) and is logged once, until an edit parses again. Waiters are left alone: a seat
        no longer in the file keeps its waiter until it ends, and gets no new one (the pass
        reads ``ctx.fleet`` for its candidates)."""
        ctx = self.w.ctx
        mtime = fleet_mtime(ctx.fleet.path)
        if mtime is None or mtime == self.fleet_mtime:
            return
        self.fleet_mtime = mtime
        try:
            fleet = ctx.reload_fleet()
        except FleetError as exc:
            error = "fleet file does not parse; still using the last good one: %s" % "; ".join(exc.errors)
            if error != self.fleet_error:
                self.fleet_error = error  # the footer's line, in red, until an edit parses
                self.w.say("fleet file %s does not parse; still using the last good one: %s"
                           % (ctx.fleet.path, "; ".join(exc.errors)), error=True, shown=error)
            return
        if self.fleet_error:
            self.fleet_error = ""
            self.w.say("fleet file %s parses again; reloaded" % fleet.path)
        else:
            self.w.say("fleet file %s changed; reloaded" % fleet.path)
        self.w.blocked_after = fleet.notify_blocked_after

    def refresh(self) -> None:
        """Rebuild the model (design 24.2) and reload the open page (25.5), after reading
        the fleet file again if it changed (26.6). Lines said while building (a warning) do
        not ask for another build."""
        self.stale = False
        self.building = True
        try:
            self.reread_fleet()
            self.state = self.w.snapshot()
            self.model = board.build(self.w.ctx, self.state)
            self.root.model = self.model
            if self.top is not self.root:
                self.load(self.top)
        finally:
            self.building = False
        self.problems = self.model.problems
        self.next_refresh = self.clock.monotonic() + BOARD_REFRESH_S

    def load(self, view: board.View) -> None:
        try:
            view.load(self.w.ctx, self.model or board.Board(fleet=""), self.cache, self.state)
        except (OSError, HerdrError) as exc:
            self.w.say("cannot read for the %s page: %s" % (view.kind, exc), error=True)

    def draw(self) -> None:
        width, height = self.screen.size()
        if self.log_view:
            if self.log_lines is None or self.log_for != height:
                self.log_lines, self.log_for = self.w.log_tail(max(1, height - 1)), height
            lines = board.render_log(self.log_lines, width, height, self.colours)
        else:
            view = self.top
            view.footer = board.Footer(self.w.last_event, self.notice,
                                       self.w.trouble or self.fleet_error or self.problems)
            lines = view.render(width, height, self.colours)
        if lines != self.drawn:
            self.screen.draw(lines)
            self.drawn = lines

    def show(self, text: str) -> None:
        """A line in the footer for NOTICE_S."""
        self.notice, self.notice_until = text, self.clock.monotonic() + NOTICE_S

    def rows(self) -> List[board.Row]:
        if self.model is None:
            return []
        width, height = self.screen.size()
        return board.visible(self.model, width, height)

    # -- keys and clicks -------------------------------------------------------------------------

    def key(self, key) -> None:
        if key == tui.RESIZE:  # not a key press: redraw at the new size
            self.drawn = None
            return
        click = isinstance(key, tuple) and key[:1] == (tui.CLICK,)
        if self.log_view:  # any key or click returns to the board
            self.log_view = False
            return
        if self.confirm is not None:
            ref, self.confirm = self.confirm, None
            self.notice, self.notice_until = "", None
            if key in ("y", "Y"):
                self.ack(ref)
            else:
                self.show("not acked")
            return
        if click:
            width, height = self.screen.size()
            self.act(self.top.click(int(key[1]), int(key[2]), width, height))
            return
        if key == "z":
            self.zoom()
        elif key == "l":
            self.log_view, self.log_lines = True, None
        elif key == "?":
            self.show(HELP)
        else:
            self.act(self.top.key(key))

    def act(self, action) -> None:
        if not action:
            return
        what = action[0]
        if what == "notice":
            self.show(action[1])
        elif what == "back":
            if len(self.stack) > 1:
                self.stack.pop()
                self.notice, self.notice_until = "", None
                self.stale = True  # the page below is read again
        elif what == "open":
            self.push(self.page(action[1], action[2]))
        elif what == "replace":
            name = action[1]
            kind = "contact" if self.model is not None and any(c.name == name for c in self.model.contacts) else "seat"
            self.stack.pop()
            self.push(self.page(kind, name))
        elif what == "body":
            self.push(board.Body(action[1]))
        elif what == "go":
            self.go(action[1])
        elif what == "ack":
            self.ask_ack(action[1])
        elif what == "deliver_all":
            self.deliver_now()
        elif what == "deliver":
            self.deliver_seat(action[1])
        elif what == "screen":
            self.read_screen(action[1])

    def page(self, kind: str, name: str) -> board.View:
        if kind == "fleet":
            return board.FleetPage(self.model.fleet if self.model is not None else "")
        if kind == "owner":
            return board.OwnerPage()
        if kind == "contact":
            return board.ContactPage(name)
        return board.SeatPage(name)

    def push(self, view: board.View) -> None:
        """Open a page: read what it needs now (design 25.5)."""
        self.load(view)
        self.stack.append(view)
        self.notice, self.notice_until = "", None

    def zoom(self) -> None:
        """``z``: zoom the strip or restore it, on its own pane only (invariant 22); nothing
        when neither ``HERDR_PANE_ID`` nor the state file names that pane."""
        try:
            from herdr_fleet.state import load_state
            strip = (load_state(self.w.repo).data.get("watch") or {}).get("pane_id")
        except Exception:  # noqa: BLE001 - an unreadable state file names no pane
            strip = None
        try:
            tui.zoom(self.w.ctx.herdr, self.w.ctx.env, str(strip) if strip else None)
        except HerdrError as exc:
            self.w.say("zoom: %s" % describe(exc), error=True)

    def go(self, name: str) -> None:
        """``go NAME`` (design 22.2), because the owner pressed ``g`` for it: the one ``agent
        focus`` the watcher ever runs (invariant 22)."""
        from herdr_fleet.ops import op_go  # ops imports watchpane, which imports this module
        try:
            code, line = op_go(self.w.ctx, name)
        except (UsageError, OpError) as exc:
            code, line = 2, str(exc)
        except HerdrError as exc:
            code, line = 1, "go %s: %s" % (name, describe(exc))
        self.w.say(line, error=code in (1, 2))
        self.stale = True

    def read_screen(self, name: str) -> None:
        """``p`` on a seat page: the seat's last twelve screen lines through ``agent read``
        (``peek``), shown on the page until it closes; read again on each ``p``. A working
        seat's visible screen, as ``peek`` falls back to it (32.3), says so in the footer."""
        from herdr_fleet.ops import op_peek
        note = ""
        try:
            code, text, note = op_peek(self.w.ctx, name, SCREEN_READ_LINES)
        except (UsageError, OpError) as exc:
            code, text = 2, str(exc)
        except HerdrError as exc:
            code, text = 1, describe(exc)
        page = self.top
        if code != 0 or not isinstance(page, board.SeatPage):
            self.show(text.splitlines()[0] if text else "nothing to read")
            return
        page.screen = [board.clean(line) for line in text.split("\n")][-SCREEN_READ_LINES:]
        self.load(page)
        if note:
            self.show(note)

    def ask_ack(self, ref: board.Ref) -> None:
        self.confirm = ref
        self.notice, self.notice_until = ("ack #%s (%s owes %s %s)? y/n" % (
            ref.short, ref.to, ref.sender, board.short_age(ref.age))), None

    def ack(self, ref: board.Ref) -> None:
        """``ack #id --reason "from the board"`` by ``owner`` (design 24.4, 25.2); its line is
        logged like the watcher's own."""
        try:
            op_ack(self.w.ctx, ["#" + ref.short], as_name=OWNER, reason=BOARD_REASON)
        except (UsageError, OpError, OSError) as exc:
            self.w.say("ack #%s: %s" % (ref.short, exc), error=True)
        self.stale = True

    def deliver_now(self) -> None:
        """``d`` on the board: every seat's or contact's queue (one with a message that is not
        FYI) whose agent is idle or done right now, delivered at once through
        ``Watcher.deliver``, with no debounce. Nothing reaches a working, blocked or unknown
        seat, and a held pointer is not prompted again."""
        w, ctx = self.w, self.w.ctx
        self.stale = True
        try:
            agents = ctx.herdr.agent_list()
            waiting = inbox.queued(w.repo, w.ctx.fleet.fyi_ttl)
        except (HerdrError, OSError) as exc:
            w.say("cannot deliver: %s" % (describe(exc) if isinstance(exc, HerdrError) else exc), error=True)
            return
        names = candidates(ctx, waiting)
        if not names:
            self.show("nothing to deliver: no seat has a queue")
            return
        for name in names:
            count = len(waiting[name])
            held = limited_line(ctx, name, count)  # design 35.2: the hold stands for `d`; a seat page's `d` ends it
            if held:
                w.say(held)
                continue
            why, detail = self.deliver_one(name, agents, waiting[name])
            if why == "absent":
                w.say("%s: %s, %d left queued" % (name, detail, count))
            elif why == "state":
                w.say("%s %s; %d left queued" % (name, state_words(detail), count))
            elif why == "typing":
                w.say("%s %s, %d left queued" % (name, detail, count))
            elif why == "pointed":
                w.say("%s: already pointed to its %d queued messages; it runs herdr-fleet inbox --read" % (name, count))
            elif why == "failed":
                w.say("failed %s: %s; %d left queued" % (name, detail, count), error=True)

    def deliver_seat(self, name: str) -> None:
        """``d`` on a seat page (design 25.2): that seat's queue, when it is idle or done,
        through the watcher's path; otherwise the footer says why not."""
        w, ctx = self.w, self.w.ctx
        self.stale = True
        try:
            agents = ctx.herdr.agent_list()
            waiting = list(inbox.queued(w.repo, w.ctx.fleet.fyi_ttl).get(name, []))
        except (HerdrError, OSError) as exc:
            w.say("cannot deliver: %s" % (describe(exc) if isinstance(exc, HerdrError) else exc), error=True)
            return
        if not waiting:
            self.show("nothing queued for %s" % name)
            return
        released = release_limit(ctx, name)  # design 35.2: `d` on the seat's own page is `deliver NAME`
        if released:
            w.say(released)
        why, detail = self.deliver_one(name, agents, waiting)
        if why == "absent":
            self.show("%s: its next kickoff carries the queue" % detail if ctx.fleet.seat(name) else detail)
        elif why == "state":
            self.show({"working": "working: lands at its next idle", "blocked": state_words("blocked")}
                      .get(detail, "%s: left queued" % detail))
        elif why == "typing":
            self.show(board.hold_reason(detail.split("; ", 1)[1] if "; " in detail else detail))
        elif why == "pointed":
            self.show("already pointed to its %d queued messages; it runs herdr-fleet inbox --read" % len(waiting))
        elif why == "failed":
            self.show("failed: %s" % detail)

    def deliver_one(self, name: str, agents: List[Dict], waiting: List[Message]) -> Tuple[str, str]:
        """Deliver one mailbox now if its agent is idle or done, through ``Watcher.deliver``
        (no debounce). ``("", "")`` when a delivery was attempted that the watcher reports
        itself; else why not: ``("absent", why)``, ``("state", state)``, ``("typing", "idle;
        <the guard's detail>")``, ``("pointed", "")`` or ``("failed", why)``."""
        w, ctx = self.w, self.w.ctx
        target, problem = herdr_target(ctx, name, agents)
        if problem or not target:
            return "absent", problem or "absent"
        try:
            state = agent_state(ctx, target)
        except HerdrError as exc:
            return "failed", describe(exc)
        w.observe(name, state)
        if state not in SETTLED:
            return "state", state
        if name in w.pointed and {m.id for m in waiting} <= w.pointed[name]:
            return "pointed", ""
        drop = w.deliver(name, target)
        if drop is not None and drop.status in ("typing", "coordination"):
            return "typing", "%s; %s" % (drop.state or state, drop.detail)
        return "", ""
