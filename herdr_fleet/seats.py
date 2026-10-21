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
- ``start_in_pane``: shell check, ``agent start``, retry on ``agent_pane_busy``
  every 250 ms for up to 10 s; after ``agent_not_ready``, read ``agent get``
  for the rest of the 10 s (``idle``, ``done`` or ``working`` count as started,
  design 16.12) and only then report blocked. Nothing is sent meanwhile.
- ``quit_agent``: the per-kind quit command through ``safe_prompt`` (so the
  presence guard runs first), then poll until the agent is gone and the shell is
  back, or give up without retrying.
"""

from __future__ import annotations

import os
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
    """Result of a lifecycle step. ``status`` is step specific."""

    ok: bool
    status: str
    detail: str
    agent: Dict = field(default_factory=dict)


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
        return Outcome(False, status, "agent %s is %s; nothing sent" % (target, status), agent)
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
    """Start ``seat`` in ``pane`` once the pane is at a shell prompt.
    status: started | blocked (agent_not_ready and still not ready at the
    deadline) | failed. The deadline covers busy retries and the ready poll."""
    deadline = clock.monotonic() + budget_s
    while True:
        try:
            info = herdr.process_info(pane)
        except HerdrError as exc:
            return Outcome(False, "failed", "cannot inspect pane %s: %s" % (pane, describe(exc)))
        if at_shell_prompt(info):
            try:
                agent = herdr.agent_start(seat.name, seat.kind, pane, seat.args)
                return Outcome(True, "started", "in %s" % pane, agent)
            except HerdrError as exc:
                if exc.code == "agent_not_ready":
                    return _await_ready(herdr, clock, seat, pane, deadline, exc)
                if exc.code != "agent_pane_busy":
                    return Outcome(False, "failed", describe(exc))
                last = describe(exc)
        else:
            last = "pane %s is not at a shell prompt (foreground: %s)" % (pane, describe_foreground(info))
        if clock.monotonic() >= deadline:
            return Outcome(False, "failed", "%s after %.0f s" % (last, budget_s))
        clock.sleep(POLL_S)


def _await_ready(herdr: Herdr, clock: Clock, seat: Seat, pane: str, deadline: float,
                 exc: HerdrError) -> Outcome:
    """After ``agent_not_ready``: the agent is registered but was blocked when herdr
    gave up waiting. A one-time notice can clear by itself (design 16.12), so read
    ``agent get`` every POLL_S until the start deadline. Read-only: nothing is
    sent to the pane. ``idle``/``done``/``working`` count as started; still
    ``blocked`` (or ``unknown``, or no answer) at the deadline is blocked."""
    while True:
        try:
            agent = herdr.agent_get(pane)
        except HerdrError:
            agent = {}
        if agent.get("agent_status") in STARTED:
            return Outcome(True, "started", "in %s" % pane, agent)
        if clock.monotonic() >= deadline:
            return Outcome(False, "blocked",
                           "blocked at startup in %s (%s); answer the prompt there, then run "
                           "`herdr-fleet kickoff %s`" % (pane, exc.message, seat.name))
        clock.sleep(POLL_S)


def quit_agent(herdr: Herdr, clock: Clock, target: str, pane: str, quit_cmd: str,
               timeout_ms: int) -> Outcome:
    """Quit one agent (design section 6). status: quit | refused | failed. A seat whose
    pane the owner is typing in is refused like a blocked one (design 21.2)."""
    sent = safe_prompt(herdr, target, quit_cmd)
    if not sent.ok:
        status = "refused" if sent.status in ("blocked", "unknown", "working", "typing") else "failed"
        return Outcome(False, status, sent.detail, sent.agent)
    deadline = clock.monotonic() + timeout_ms / 1000.0
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
                    return Outcome(True, "quit", "quit with %s" % quit_cmd)
            except HerdrError:
                pass
        if clock.monotonic() >= deadline:
            return Outcome(False, "failed",
                           "agent %s did not exit within %d ms after %s; not retried, no keys sent, "
                           "seat left alone" % (target, timeout_ms, quit_cmd))


