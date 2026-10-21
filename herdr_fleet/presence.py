"""The presence guard (design 21.2, invariant 19): before any text goes to a seat,
is the owner typing there?

1. ``owner_pane``: is the target the pane the owner's keystrokes go to? ``pane get``
   says whether the pane is ``focused``; only then ``workspace list`` says whether its
   workspace is the focused one and that workspace's active tab is the pane's tab.
2. ``prompt_line`` (pure): the agent's prompt line among the last lines of the
   screen, the last line whose first non-blank character is one of the kind's
   glyphs (``seats.PROMPT_GLYPHS``), as the text after the glyph.
3. ``typing``: the whole guard. ``None`` when text may go (not the owner's pane, an
   empty prompt line or the kind's placeholder, or a kind with no glyph entry, which
   cannot be checked); otherwise the detail ``owner typing in w1:p2``. No prompt line
   on screen (a dialog, a picker, a menu) counts as typing.

The screen lines are read for the prompt line alone and discarded: nothing is kept,
logged or decided from them besides this one answer.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from herdr_fleet.herdr import Herdr, HerdrError

SCREEN_LINES = 12  # design 21.2: `pane read TARGET --lines 12`
BORDERS = "\u2502\u2503"  # box-drawing verticals some builds draw around the input line


def glyphs_for(kind: str) -> Tuple[str, ...]:
    from herdr_fleet.seats import PROMPT_GLYPHS  # seats imports this module
    return tuple(PROMPT_GLYPHS.get(kind or "", ()))


def placeholders_for(kind: str) -> Tuple[str, ...]:
    from herdr_fleet.seats import PROMPT_PLACEHOLDERS
    return tuple(PROMPT_PLACEHOLDERS.get(kind or "", ()))


def checkable(kind: str) -> bool:
    """True when the guard knows the kind's prompt glyph (else it passes, design 21.2 item 3)."""
    return bool(glyphs_for(kind))


def unchecked_kinds(kinds: Sequence[str]) -> List[str]:
    """The kinds, in order and once each, whose seats the guard cannot check (doctor)."""
    found: List[str] = []
    for kind in kinds:
        if kind and not checkable(kind) and kind not in found:
            found.append(kind)
    return found


def prompt_line(text: str, kind: str) -> Optional[str]:
    """The text on the agent's prompt line, stripped, or None when no line of ``text``
    starts (after blanks) with one of the kind's prompt glyphs. The last such line wins:
    the input box sits below the conversation."""
    glyphs = glyphs_for(kind)
    found: Optional[str] = None
    for line in (text or "").splitlines():
        body = line.strip().strip(BORDERS).strip()  # older Claude Code builds box the line: `│ > │`
        for glyph in glyphs:
            if body.startswith(glyph):
                found = body[len(glyph):].strip()
                break
    return found


def is_typed(line: Optional[str], kind: str) -> bool:
    """A prompt line holds the owner's text: anything but empty or the kind's placeholder.
    No prompt line at all (None) counts too: the agent shows something else to answer."""
    if line is None:
        return True
    return bool(line) and line not in placeholders_for(kind)


def owner_pane(herdr: Herdr, pane_id: str) -> bool:
    """Is ``pane_id`` the pane the owner's keystrokes go to? ``pane get`` first (one call
    when the pane is not focused, the common case), then ``workspace list``."""
    pane = herdr.pane_get(pane_id)
    if not pane.get("focused"):
        return False
    workspace = pane.get("workspace_id")
    for record in herdr.workspace_list():
        if record.get("workspace_id") == workspace:
            return bool(record.get("focused")) and record.get("active_tab_id") == pane.get("tab_id")
    return False


def typing(herdr: Herdr, pane_id: str, kind: str) -> Optional[str]:
    """The guard: None when text may go to ``pane_id`` (whose live agent is of ``kind``),
    else ``owner typing in <pane>``. Raises HerdrError when herdr cannot answer."""
    if not pane_id or not checkable(kind):
        return None
    if not owner_pane(herdr, pane_id):
        return None
    if is_typed(prompt_line(herdr.pane_read(pane_id, SCREEN_LINES), kind), kind):
        return "owner typing in %s" % pane_id
    return None


def typing_at(herdr: Herdr, agent: Dict) -> Optional[str]:
    """``typing`` for an agent record (``pane_id`` and ``agent``, the live kind)."""
    return typing(herdr, str(agent.get("pane_id") or ""), str(agent.get("agent") or ""))


def would_hold(herdr: Herdr, target: Optional[str] = None, agent: Optional[Dict] = None) -> Optional[str]:
    """For a dry run: would the guard hold text for ``target`` (a name or pane id, read
    with ``agent get``) or for ``agent`` (a record already read) right now? The guard only
    reads, so a dry run runs it; None when it would pass or herdr cannot say."""
    try:
        if agent is None:
            if not target:
                return None
            agent = herdr.agent_get(target)
        return typing_at(herdr, agent)
    except HerdrError:
        return None
