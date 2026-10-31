"""Per-seat lifecycle against herdr (design sections 4.1 step 4, 6 and 7).

- ``at_shell_prompt``: the process-info check used before every start and
  after every quit (exactly one foreground process, and it is a shell).
- ``find_agent_process`` / ``process_argv``: which foreground process is the
  agent and what argv it runs, for drift detection.
- ``safe_prompt``: the only path by which text reaches an agent. It re-reads
  the agent immediately before sending and refuses anything not ``idle`` or
  ``done`` (``send --now`` and ``deliver NAME --now`` alone also allow
  ``working``, design 21.1; ``blocked`` and ``unknown`` never); then it runs the
  presence guard (``presence.typing``, design 21.2) and sends nothing while the
  owner is typing in the target pane; ``agent_prompt_stalled`` and ``timeout``
  are reported, never resent.
- ``start_in_pane``: shell check, ``agent start --timeout 15000``, retry on
  ``agent_pane_busy`` every 250 ms for up to 10 s; after ``agent_not_ready``, read
  ``agent get`` for the rest of the 10 s (``idle``, ``done`` or ``working`` count as
  started, design 16.12) and only then report blocked. A start that answers
  ``timeout`` is classified by ``agent get`` and ``process-info`` (design 32.1): a late
  agent, an agent process still coming up (polled until 45 s after the first start),
  a bare shell (the command never ran: issued once more, never a third time), or
  anything else (failed). Nothing is sent, typed, cleared or reset meanwhile.
- ``quit_agent``: the per-kind quit command through ``safe_prompt`` (so the
  presence guard runs first; a hold for a dialog or menu is read again for up to 5 s,
  then named and left to the owner, design 32.2), then poll until the agent is gone
  and the shell is back, or give up without retrying. When the agent is still there
  ``EXIT_DIALOG_AFTER_S`` later, its screen is read once (``agent read``) and the
  kind's exit confirmation (``EXIT_DIALOGS``), with the exit choice highlighted,
  gets one Enter (design 23.7, invariant 21); any other screen gets no key.
- ``interrupt_agent``: the kind's interrupt keys (``INTERRUPT_KEYS``, overridable
  per kind by ``[interrupt]``), 300 ms apart, then ``agent wait`` until idle or
  done (design 23.7). ``interrupt`` is its only caller.
"""

from __future__ import annotations

import os
import re
import shlex
import time
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from herdr_fleet import presence
from herdr_fleet.fleetfile import Seat
from herdr_fleet.herdr import Herdr, HerdrError, describe

SHELLS = ("zsh", "bash", "fish", "sh", "nu")
SETTLED = ("idle", "done")
STARTED = SETTLED + ("working",)  # after agent_not_ready, these mean the agent got past startup
POLL_S = 0.25
START_BUDGET_S = 10.0
START_READY_MS = 15000  # design 32.1: `agent start --timeout`; slower starts are polled for here
START_SLOW_S = 45.0     # design 32.1: how long after the first start a late agent is waited for
LOST = "lost"           # a start that timed out with the pane at its bare shell: the command never ran
# The remedy a blocked or slow start names; once its kickoff is queued (32.6) it says that instead.
KICKOFF_REMEDY = ", then run `herdr-fleet kickoff %s`"   # blocked at startup (16.12)
SLOW_REMEDY = ", then run herdr-fleet kickoff %s"         # a slow start past its deadline (32.1)
KICKOFF_QUEUED = "; its kickoff is queued (#%s) and goes when it is idle"
QUIT_SETTLE_S = 5.0     # design 32.2: how long a quit held by a dialog or menu reads the guard again
QUIT_SETTLE_POLL_S = 0.5
DEFAULT_QUIT_TIMEOUT_MS = 20000
UNSENT_CODES = ("agent_prompt_stalled", "timeout")

DEFAULT_QUIT: Dict[str, str] = {
    "claude": "/exit",
    "codex": "/quit",
    "gemini": "/quit",
    "opencode": "/exit",
    "copilot": "/exit",
    "pi": "/exit",
    "agy": "/exit",
}

# Design 21.2: the first non-blank character of each kind's prompt line, as `pane read`
# prints it (Claude Code `❯`, older builds `>`; Codex `›`; Antigravity `>`), and the
# placeholder text an empty prompt line shows. A kind with no entry is not held while
# the owner types (doctor says so).
PROMPT_GLYPHS: Dict[str, Tuple[str, ...]] = {
    "claude": ("\u276f", ">"),
    "codex": ("\u203a",),
    "agy": (">",),
}
PROMPT_PLACEHOLDERS: Dict[str, Tuple[str, ...]] = {
    "codex": ("Ask Codex to do anything",),
}
# A fresh Claude Code session shows a suggestion in its empty input line (`❯ Try "fix typecheck
# errors"`), which `pane read` prints like typed text; it is a placeholder (design 23.11).
PROMPT_PLACEHOLDER_PATTERNS: Dict[str, Tuple[str, ...]] = {
    "claude": (r'Try "[^"]*"',),
}
# Design 32.8: after a turn Claude Code suggests a next prompt in its empty input line, drawn faint
# (`❯\xa0\e[2m<suggested prompt>\e[0m`); the owner's own typing is drawn at normal intensity. For these
# kinds the guard drops faint characters from the prompt line. Not for others: a faint run there may
# be the owner's pasted text.
PROMPT_FAINT_PLACEHOLDER: Tuple[str, ...] = ("claude",)

# Design 23.7: the exit confirmation a kind may show after its quit command, which the
# plugin answers with one Enter (invariant 21). ``signature``: lines that must all be on
# screen; ``choice``: the exit option, which must be the highlighted one (a line whose
# first non-blank character is the highlight mark and which contains it); ``answer``:
# how the report names the choice. A kind without an entry is never answered.
HIGHLIGHT = "\u276f"   # `❯`, the mark on Claude Code's highlighted option


@dataclass(frozen=True)
class ExitDialog:
    signature: Tuple[str, ...]
    choice: str
    answer: str


EXIT_DIALOGS: Dict[str, ExitDialog] = {
    "claude": ExitDialog(("Background work is running", "Exit and stop tasks"), "Exit and stop tasks",
                         "exit and stop tasks"),
}
EXIT_DIALOG_AFTER_S = 3.0   # design 23.7 `exit_dialog_after`: how long after the quit command to look
EXIT_DIALOG_LINES = 16      # `agent read NAME --lines 16`

# Design 23.7: the keys `interrupt NAME` presses in a working seat, per kind, 300 ms apart.
# Claude Code quits on a second Ctrl-C, so a Claude seat gets exactly one. `[interrupt]` in
# the fleet file, then in the plugin config, overrides a kind, with the keys of
# `fleetfile.INTERRUPT_ALLOWED` only (`fleetfile.interrupt_problems` checks both tables).
INTERRUPT_KEYS: Dict[str, Tuple[str, ...]] = {"claude": ("ctrl+c",), "agy": ("ctrl+c", "ctrl+c")}
INTERRUPT_GAP_S = 0.3
DEFAULT_INTERRUPT_WAIT_MS = 10000
BLOCKED_WORDS = "blocked: a dialog is open"  # design 23.5: refusal and queue lines that name `blocked`

QUIT_CONFIG_HINT = ('set `quit = "..."` on the seat in .herdr/fleet.toml, or add the kind under '
                    '[quit] in $HERDR_PLUGIN_CONFIG_DIR/config.toml')


def _base(value: object) -> str:
    text = str(value or "").lstrip("-")
    return os.path.basename(text)


def live_by_name(herdr: Herdr) -> Dict[str, Dict]:
    """Live named agents keyed by name (from ``herdr agent list``)."""
    return {str(a["name"]): a for a in herdr.agent_list() if a.get("name")}


def foreground(info: Mapping) -> List[Dict]:
    return [p for p in (info.get("foreground_processes") or []) if isinstance(p, dict)]


def at_shell_prompt(info: Mapping) -> bool:
    """Exactly one foreground process and it is a shell (zsh, bash, fish, sh, nu)."""
    procs = foreground(info)
    if len(procs) != 1:
        return False
    proc = procs[0]
    return _base(proc.get("name")) in SHELLS or _base(proc.get("argv0")) in SHELLS


def describe_foreground(info: Mapping) -> str:
    names = [str(p.get("argv0") or p.get("name") or "?") for p in foreground(info)]
    return ", ".join(names) if names else "nothing"


def process_argv(proc: Mapping) -> Optional[List[str]]:
    """The process argv, from ``argv`` or else ``cmdline``; None when herdr gave neither."""
    argv = proc.get("argv")
    if isinstance(argv, list) and argv:
        return [str(a) for a in argv]
    cmdline = proc.get("cmdline")
    if isinstance(cmdline, str) and cmdline.strip():
        try:
            return shlex.split(cmdline)
        except ValueError:
            return cmdline.split()
    return None


def find_agent_process(info: Mapping, kind: str) -> Optional[Dict]:
    """The foreground process whose name, argv0, or first two argv entries
    (``node /path/to/codex``) name the kind's executable."""
    for proc in foreground(info):
        candidates = [proc.get("name"), proc.get("argv0")] + list((process_argv(proc) or [])[:2])
        if any(_base(c) == kind for c in candidates):
            return proc
    return None


def agent_args(argv: Sequence[str], kind: str) -> List[str]:
    """The arguments after the executable in an agent's argv."""
    for index, item in enumerate(argv[:2]):
        if _base(item) == kind:
            return list(argv[index + 1:])
    return list(argv[1:])


def contains_in_order(haystack: Sequence[str], needle: Sequence[str]) -> bool:
    """True when ``needle`` appears in ``haystack`` as a contiguous run."""
    needle = list(needle)
    if not needle:
        return True
    width = len(needle)
    return any(list(haystack[i:i + width]) == needle for i in range(len(haystack) - width + 1))


def state_words(state: str) -> str:
    """A state as a refusal or queue line names it (design 23.5): ``blocked`` reads
    ``blocked: a dialog is open``; ``status`` keeps the bare word in its STATE column."""
    return BLOCKED_WORDS if state == "blocked" else state


def interrupt_keys(live_kind: str, fleet_keys: Mapping[str, Sequence[str]],
                   config_keys: Mapping[str, Sequence[str]]) -> Optional[Tuple[str, ...]]:
    """The fleet file's ``[interrupt]`` entry for the kind, then the plugin config's, then
    INTERRUPT_KEYS; None when the kind has none (design 23.7)."""
    for table in (fleet_keys, config_keys, INTERRUPT_KEYS):
        if live_kind in table:
            return tuple(table[live_kind])
    return None


def quit_command(seat: Seat, live_kind: str, config_quit: Mapping[str, str]) -> Optional[str]:
    """Seat override (when the live kind is the seat's kind), then config, then defaults."""
    if seat.quit and live_kind == seat.kind:
        return seat.quit
    if live_kind in config_quit:
        return config_quit[live_kind]
    return DEFAULT_QUIT.get(live_kind)


class Clock:
    """Real time. Tests substitute a fake with the same two methods."""

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


@dataclass
class Outcome:
    """Result of a lifecycle step. ``status`` is step specific. ``shell``: a failed start
    left the pane at its shell prompt (design 32.1); ``slow``: a failed start whose agent
    process runs, which usually comes up later, so its kickoff is queued (32.1 as the review
    amended it); ``needs_owner``: a refused quit only a person can clear (a dialog or menu,
    design 32.2)."""

    ok: bool
    status: str
    detail: str
    agent: Dict = field(default_factory=dict)
    shell: bool = False
    needs_owner: bool = False
    slow: bool = False
    late: bool = False  # the second attempt found the first start's agent coming up: no second start


# Design 21.1: the `--now` allowance. Only `send --now` and `deliver NAME --now` pass it,
# so text reaches a working seat from nothing else (invariant 2).
SEND_ALLOWED = SETTLED + ("working",)


def safe_prompt(herdr: Herdr, target: str, text: str, allow: Sequence[str] = SETTLED) -> Outcome:
    """Send text to an agent only if its state right now is in ``allow``
    (invariants 1-3): idle or done for every command; ``send --now`` and
    ``deliver NAME --now`` alone also allow working (SEND_ALLOWED). Blocked and
    unknown are never allowed. Then the presence guard (invariant 19): while the
    owner is typing in the target pane, or its prompt line is not on screen,
    nothing is sent (status ``typing``, detail ``owner typing in <pane>``)."""
    allow = tuple(s for s in allow if s not in ("blocked", "unknown"))
    try:
        agent = herdr.agent_get(target)
    except HerdrError as exc:
        return Outcome(False, "absent", "no live agent %s (%s)" % (target, describe(exc)))
    status = str(agent.get("agent_status") or "unknown")
    if status not in allow:
        return Outcome(False, status, "agent %s is %s; nothing sent" % (target, state_words(status)), agent)
    try:
        held = presence.typing_at(herdr, agent)
    except HerdrError as exc:  # cannot tell whether the owner is typing: send nothing
        return Outcome(False, "error", "presence check: %s; nothing sent" % describe(exc), agent)
    if held:
        return Outcome(False, "typing", held, agent)
    try:
        herdr.agent_prompt(target, text)
    except HerdrError as exc:
        if exc.code in UNSENT_CODES:
            return Outcome(False, "unconfirmed", "%s; the text may have been delivered, so it is not resent"
                           % describe(exc), agent)
        if exc.code == "agent_blocked":  # turned blocked since the check; herdr sent nothing
            return Outcome(False, "blocked", "%s; nothing sent" % describe(exc), agent)
        return Outcome(False, "error", describe(exc), agent)
    return Outcome(True, "sent", "sent", agent)


def start_in_pane(herdr: Herdr, clock: Clock, seat: Seat, pane: str,
                  budget_s: float = START_BUDGET_S) -> Outcome:
    """Start ``seat`` in ``pane`` once the pane is at a shell prompt, with ``agent start
    --timeout START_READY_MS``. status: started | blocked (not ready at its deadline) |
    failed. ``budget_s`` covers busy retries and the 16.12 poll after ``agent_not_ready``.

    Design 32.1: a start that answers ``timeout`` is classified from ``agent get`` and
    ``pane process-info`` alone (``_after_timeout``). One whose pane is back at its bare
    shell with no agent and no process of the kind never ran (a command garbled by mouse
    reports a quit agent left switched on): it is issued once more, through the same
    process-info check, and never a third time. Nothing is typed, cleared or reset."""
    first = clock.monotonic()
    outcome = _attempt(herdr, clock, seat, pane, budget_s, first, again=False)
    if outcome.status != LOST:
        return outcome
    outcome = _attempt(herdr, clock, seat, pane, budget_s, first, again=True)
    if outcome.status == "started" and not outcome.late:
        return Outcome(True, "started", "in %s (second attempt; the first start left a bare shell)" % pane,
                       outcome.agent)
    if outcome.status == LOST:
        return Outcome(False, "failed", "the start command did not run in %s twice, or the agent exited at once; "
                       "a quit agent may have left mouse tracking on there (README: troubleshooting); run "
                       "herdr-fleet up %s" % (pane, seat.name), shell=True)
    return outcome


def _attempt(herdr: Herdr, clock: Clock, seat: Seat, pane: str, budget_s: float, first: float,
             again: bool) -> Outcome:
    """One start: the shell check, ``agent start``, the ``agent_pane_busy`` retry every
    POLL_S within ``budget_s``. status: started | blocked | failed | LOST. ``again`` (the
    second attempt of 32.1): a pane that is no longer at its bare shell is not waited for;
    a process of the kind there means the first command ran after all (the slow poll),
    anything else fails."""
    deadline = clock.monotonic() + budget_s
    while True:
        try:
            info = herdr.process_info(pane)
        except HerdrError as exc:
            return Outcome(False, "failed", "cannot inspect pane %s: %s" % (pane, describe(exc)))
        if at_shell_prompt(info):
            try:
                agent = herdr.agent_start(seat.name, seat.kind, pane, seat.args, START_READY_MS)
                return Outcome(True, "started", "in %s" % pane, agent)
            except HerdrError as exc:
                if exc.code == "agent_not_ready":
                    return _await_ready(herdr, clock, seat, pane, deadline, exc.message)
                if exc.code == "timeout":
                    return _after_timeout(herdr, clock, seat, pane, first, exc)
                if exc.code != "agent_pane_busy":
                    return Outcome(False, "failed", describe(exc), shell=True)
                last = describe(exc)
        elif again and find_agent_process(info, seat.kind) is not None:
            outcome = _await_slow(herdr, clock, seat, pane, first + START_SLOW_S)
            outcome.late = True
            return outcome
        elif again:
            return Outcome(False, "failed", "pane %s is not at a shell prompt for the second start (foreground: %s); "
                           "not retried" % (pane, describe_foreground(info)))
        else:
            last = "pane %s is not at a shell prompt (foreground: %s)" % (pane, describe_foreground(info))
        if clock.monotonic() >= deadline:
            return Outcome(False, "failed", "%s after %.0f s" % (last, budget_s))
        clock.sleep(POLL_S)


def _after_timeout(herdr: Herdr, clock: Clock, seat: Seat, pane: str, first: float, exc: HerdrError) -> Outcome:
    """Design 32.1: herdr gave up waiting for the agent. Read only, in this order: ``agent
    get PANE`` answers (a late agent: started, or the 16.12 poll for blocked or unknown);
    else ``process-info`` shows a process of the seat's kind (still coming up: poll ``agent
    get``); else a bare shell (LOST: the command never ran); else something else (failed,
    naming it). Every wait ends START_SLOW_S after the first start."""
    deadline = first + START_SLOW_S
    try:
        agent = herdr.agent_get(pane)
    except HerdrError:
        agent = None
    if agent is not None:
        if agent.get("agent_status") in STARTED:
            return Outcome(True, "started", "in %s" % pane, agent)
        return _await_ready(herdr, clock, seat, pane, deadline, "not ready when the start timed out")
    try:
        info = herdr.process_info(pane)
    except HerdrError as again:
        return Outcome(False, "failed", "%s; cannot inspect pane %s: %s" % (describe(exc), pane, describe(again)))
    if find_agent_process(info, seat.kind) is not None:
        return _await_slow(herdr, clock, seat, pane, deadline)
    if at_shell_prompt(info):
        return Outcome(False, LOST, describe(exc), shell=True)
    return Outcome(False, "failed", "%s; foreground in %s: %s; not retried"
                   % (describe(exc), pane, describe_foreground(info)))


def _await_slow(herdr: Herdr, clock: Clock, seat: Seat, pane: str, deadline: float) -> Outcome:
    """Design 32.1 case 2: the agent's process runs but herdr has not seen the agent. Read
    ``agent get`` every POLL_S until ``deadline``; once it answers, as after a late start."""
    while True:
        if clock.monotonic() >= deadline:
            return Outcome(False, "failed", "its process runs in %s but herdr has not seen it ready after %.0f s; "
                           "look at the pane%s" % (pane, START_SLOW_S, SLOW_REMEDY % seat.name), slow=True)
        clock.sleep(POLL_S)
        try:
            agent = herdr.agent_get(pane)
        except HerdrError:
            continue
        if agent.get("agent_status") in STARTED:
            return Outcome(True, "started", "in %s" % pane, agent)
        return _await_ready(herdr, clock, seat, pane, deadline, "not ready when the start timed out")


def _await_ready(herdr: Herdr, clock: Clock, seat: Seat, pane: str, deadline: float, why: str) -> Outcome:
    """After ``agent_not_ready`` (or a late agent that is blocked or unknown, 32.1): the
    agent is registered but was not ready when herdr gave up waiting. A one-time notice can
    clear by itself (design 16.12), so read ``agent get`` every POLL_S until ``deadline``.
    Read-only: nothing is sent to the pane. ``idle``/``done``/``working`` count as started;
    still ``blocked`` (or ``unknown``, or no answer) at the deadline is blocked."""
    while True:
        try:
            agent = herdr.agent_get(pane)
        except HerdrError:
            agent = {}
        if agent.get("agent_status") in STARTED:
            return Outcome(True, "started", "in %s" % pane, agent)
        if clock.monotonic() >= deadline:
            return Outcome(False, "blocked", "blocked at startup in %s (%s); answer the prompt there%s"
                           % (pane, why, KICKOFF_REMEDY % seat.name))
        clock.sleep(POLL_S)


def quit_agent(herdr: Herdr, clock: Clock, target: str, pane: str, quit_cmd: str,
               timeout_ms: int, answer_exit: bool = True) -> Outcome:
    """Quit one agent (design section 6). status: quit | refused | failed. A seat whose
    pane the owner is typing in is refused like a blocked one (design 21.2).

    Design 23.7: when the agent is still there EXIT_DIALOG_AFTER_S after the quit command,
    its screen is read once (``agent read``); if it shows the kind's exit confirmation with
    the exit choice highlighted, and ``answer_exit`` (never from a popup), one ``enter``
    answers it and the poll goes on. Any other screen gets no key. On timeout the screen
    is read again for the report: a numbered choice on screen means a dialog the owner
    answers by hand.

    Design 32.2: a quit the guard holds for a dialog or menu (no prompt line, or a
    highlighted numbered choice where it would be) reads the guard again every
    QUIT_SETTLE_POLL_S for up to QUIT_SETTLE_S, since a seat that has just turned done may
    still be repainting; then it is refused, naming the dialog by its first line
    (``needs_owner``). Text in the input is refused at once."""
    sent = safe_prompt(herdr, target, quit_cmd)
    if not sent.ok and sent.status == "typing" and presence.is_dialog(sent.detail):
        sent = _settle_held(herdr, clock, target, quit_cmd, sent)
    if not sent.ok:
        status = "refused" if sent.status in ("blocked", "unknown", "working", "typing") else "failed"
        return Outcome(False, status, sent.detail, sent.agent, needs_owner=sent.needs_owner)
    kind = str(sent.agent.get("agent") or "")
    dialog = EXIT_DIALOGS.get(kind)
    started = clock.monotonic()
    deadline = started + timeout_ms / 1000.0
    looked = dialog is None or not answer_exit  # nothing to answer, so nothing to look for at 3 s
    answered = False
    while True:
        clock.sleep(POLL_S)
        try:
            herdr.agent_get(pane)
            gone = False
        except HerdrError:
            gone = True
        if gone:
            try:
                if at_shell_prompt(herdr.process_info(pane)):
                    if answered:
                        return Outcome(True, "quit", "answered its exit confirmation (%s)" % dialog.answer)
                    return Outcome(True, "quit", "quit with %s" % quit_cmd)
            except HerdrError:
                pass
        elif not looked and clock.monotonic() - started >= EXIT_DIALOG_AFTER_S:
            looked = True
            try:
                screen = herdr.agent_read(target, EXIT_DIALOG_LINES)
            except HerdrError:
                screen = ""
            if shows_exit_dialog(kind, screen):
                try:
                    herdr.agent_send_keys(target, ["enter"])
                    answered = True
                except HerdrError as exc:
                    return Outcome(False, "failed", "its exit confirmation is on screen, but the Enter failed: "
                                   "%s; answer it by hand" % describe(exc))
        if clock.monotonic() >= deadline:
            return Outcome(False, "failed", _no_exit(herdr, target, quit_cmd, timeout_ms, gone, answered))


def _settle_held(herdr: Herdr, clock: Clock, target: str, quit_cmd: str, held: Outcome) -> Outcome:
    """Design 32.2: the guard held the quit for a dialog or menu. Read the guard again
    (read-only) every QUIT_SETTLE_POLL_S for up to QUIT_SETTLE_S; once it passes, the quit
    goes through ``safe_prompt`` as usual, and text in the input refuses it at once. Still
    held at the end: one ``agent read`` names the dialog by its first line (no numbered
    choice on screen keeps the guard's words), and the refusal needs the owner."""
    until = clock.monotonic() + QUIT_SETTLE_S
    while clock.monotonic() < until:
        clock.sleep(QUIT_SETTLE_POLL_S)
        try:
            hold = presence.typing_at(herdr, held.agent)
        except HerdrError as exc:  # cannot tell: send nothing
            return Outcome(False, "error", "presence check: %s; nothing sent" % describe(exc), held.agent)
        if hold is None:
            sent = safe_prompt(herdr, target, quit_cmd)
            if sent.ok or sent.status != "typing" or not presence.is_dialog(sent.detail):
                return sent
            held = sent
        elif not presence.is_dialog(hold):
            return Outcome(False, "typing", hold, held.agent)
    try:
        first = dialog_first_line(herdr.agent_read(target, EXIT_DIALOG_LINES))
    except HerdrError:
        first = None
    pane = str(held.agent.get("pane_id") or target)
    detail = held.detail if first is None else presence.DIALOG_NAMED % (pane, first)
    return Outcome(False, "typing", detail, held.agent, needs_owner=True)


def shows_exit_dialog(kind: str, screen: str) -> bool:
    """Design 23.7: the kind's exit confirmation is on screen (every signature line) with
    its exit choice highlighted (a line whose first non-blank character is the highlight
    mark and which contains the choice). Pure."""
    dialog = EXIT_DIALOGS.get(kind)
    if dialog is None or not all(line in screen for line in dialog.signature):
        return False
    return any(_bare(line).startswith(HIGHLIGHT) and dialog.choice in line for line in screen.splitlines())


BOX = "\u2502\u2503"                               # box-drawing verticals around a dialog's lines
RULE_CHARS = set("\u2500\u2501\u2550\u256d\u256e\u2570\u256f\u250c\u2510\u2514\u2518 ")
# `❯ 1. Exit and stop tasks` (Claude Code); Codex marks its highlighted choice with `›` (design 32.2)
CHOICE_RE = re.compile("^[" + HIGHLIGHT + "›]" + r"\s*\d+[.)]\s")


def _bare(line: str) -> str:
    return line.strip().strip(BOX).strip()


def dialog_first_line(screen: str) -> Optional[str]:
    """When a numbered choice is highlighted on screen (``❯ 1. …``; the empty prompt line
    ``❯`` is not one), the dialog's first non-blank line: the first after the last rule
    above the choice, else the first of the screen. None when no choice is on screen."""
    lines = [_bare(line) for line in screen.splitlines()]
    choice = next((i for i, line in enumerate(lines) if CHOICE_RE.match(line)), None)
    if choice is None:
        return None
    start = 0
    for index in range(choice - 1, -1, -1):
        if len(lines[index]) >= 3 and set(lines[index]) <= RULE_CHARS:
            start = index + 1
            break
    first = next((line for line in lines[start:choice + 1] if line), lines[choice])
    return first if len(first) <= 80 else first[:79] + "\u2026"


def dialog_lines(screen: str, count: int = 4) -> List[str]:
    """Design 25.2: up to ``count`` non-blank lines of the dialog on screen, from the line
    ``dialog_first_line`` names (the first after the last rule above the highlighted
    choice), each cut at 80 characters; [] when no numbered choice is highlighted."""
    first = dialog_first_line(screen)
    if first is None:
        return []
    lines = [_bare(line) for line in screen.splitlines()]
    choice = next(i for i, line in enumerate(lines) if CHOICE_RE.match(line))
    start = 0
    for index in range(choice - 1, -1, -1):
        if len(lines[index]) >= 3 and set(lines[index]) <= RULE_CHARS:
            start = index + 1
            break
    shown = [line if len(line) <= 80 else line[:79] + "\u2026" for line in lines[start:] if line]
    return shown[:count]


def _no_exit(herdr: Herdr, target: str, quit_cmd: str, timeout_ms: int, gone: bool, answered: bool) -> str:
    """The failure detail of a quit that timed out (design 23.7)."""
    if answered:
        return ("did not exit within %d ms after %s, though its exit confirmation was answered once; "
                "not retried, no more keys sent, seat left alone" % (timeout_ms, quit_cmd))
    first = None
    if not gone:
        try:
            first = dialog_first_line(herdr.agent_read(target, EXIT_DIALOG_LINES))
        except HerdrError:
            first = None
    if first is not None:
        return ("did not exit within %d ms after %s; the pane shows a dialog: \"%s\"; answer it by hand"
                % (timeout_ms, quit_cmd, first))
    return ("agent %s did not exit within %d ms after %s; not retried, no keys sent, seat left alone"
            % (target, timeout_ms, quit_cmd))


def interrupt_agent(herdr: Herdr, clock: Clock, target: str, keys: Sequence[str], wait_ms: int) -> Outcome:
    """Design 23.7: press ``keys`` in a working agent's pane, INTERRUPT_GAP_S apart, then
    ``agent wait --until idle --until done`` for up to ``wait_ms``. The caller has just
    read the agent ``working``. status: idle | done (interrupted), working | blocked |
    unknown (still so at the deadline), absent (the agent is gone) or failed (a key did not
    go: ``detail`` says how many did). ``agent`` carries ``sent``, the keys that went."""
    sent: List[str] = []
    for index, key in enumerate(keys):
        if index:
            clock.sleep(INTERRUPT_GAP_S)
        try:
            herdr.agent_send_keys(target, [key])
        except HerdrError as exc:
            return Outcome(False, "failed", describe(exc), {"sent": sent})
        sent.append(key)
    try:
        agent = herdr.agent_wait(target, SETTLED, wait_ms)
        status = str(agent.get("agent_status") or "unknown")
    except HerdrError as exc:
        if "not_found" in exc.code:
            return Outcome(False, "absent", "the agent is gone", {"sent": sent})
        try:
            status = str(herdr.agent_get(target).get("agent_status") or "unknown")
        except HerdrError as again:  # the keys went: report what can be said, never raise past the log
            if "not_found" in again.code:
                return Outcome(False, "absent", "the agent is gone", {"sent": sent})
            status = "unknown"
    return Outcome(status in SETTLED, status, status, {"sent": sent})
