"""The presence guard (design 21.2 as 23.11 amends it, invariant 19): before any text
goes to a seat, does its input already hold text?

1. ``prompt_line`` (pure): the agent's prompt line among the last lines of the
   screen, the last line whose first non-blank character is one of the kind's
   glyphs (``seats.PROMPT_GLYPHS``), as the text after the glyph.
2. ``typing``: the whole guard. It reads ``pane read TARGET --lines 12`` for every
   target pane, focused or not (23.11: text left in an unfocused seat's input merges
   with the next paste too). ``None`` when text may go (an empty prompt line or the
   kind's placeholder, or a kind with no glyph entry, which cannot be checked);
   otherwise the detail. No prompt line on screen (a dialog, a picker, a menu) holds
   the text as well.
3. ``owner_pane`` only words the detail, and only once the line holds text: is the
   target the pane the owner's keystrokes go to? ``pane get`` says whether the pane is
   ``focused``; only then ``workspace list`` says whether its workspace is the focused
   one and that workspace's active tab is the pane's tab. The owner's pane reads
   ``owner typing in w1:p2``; any other ``text in its input (w1:p2); press Ctrl-U
   there to clear it``, or, with no prompt line or a highlighted numbered option
   (``❯ 1. Yes``) where the line would be, ``a dialog or a menu is on screen in w1:p2;
   answer or close it there``.

The screen lines are read for the prompt line alone and discarded: nothing is kept,
logged or decided from them besides this one answer.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence, Tuple

from herdr_fleet.herdr import Herdr, HerdrError

SCREEN_LINES = 12  # design 21.2: `pane read TARGET --lines 12`
OWNER_TYPING = "owner typing in %s"                                   # the owner's pane (21.2)
IN_INPUT = "text in its input (%s); press Ctrl-U there to clear it"   # any other pane (23.11)
DIALOG = "a dialog or a menu is on screen in %s; answer or close it there"  # no prompt line, or `❯ 1. Yes`
CHOICE_RE = re.compile(r"^\d+[.)]\s")  # a highlighted numbered option read as a prompt line: `❯ 1. Yes`
BORDERS = "\u2502\u2503"  # box-drawing verticals some builds draw around the input line


def glyphs_for(kind: str) -> Tuple[str, ...]:
    from herdr_fleet.seats import PROMPT_GLYPHS  # seats imports this module
    return tuple(PROMPT_GLYPHS.get(kind or "", ()))


def placeholders_for(kind: str) -> Tuple[str, ...]:
    from herdr_fleet.seats import PROMPT_PLACEHOLDERS
    return tuple(PROMPT_PLACEHOLDERS.get(kind or "", ()))


def placeholder_patterns_for(kind: str) -> Tuple[str, ...]:
    from herdr_fleet.seats import PROMPT_PLACEHOLDER_PATTERNS
    return tuple(PROMPT_PLACEHOLDER_PATTERNS.get(kind or "", ()))


def is_placeholder(line: str, kind: str) -> bool:
    """The kind's empty-input text: an exact placeholder or one matching a pattern in full."""
    if line in placeholders_for(kind):
        return True
    return any(re.fullmatch(pattern, line) for pattern in placeholder_patterns_for(kind))


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


def holds_text(line: Optional[str], kind: str) -> bool:
    """A prompt line is on screen and holds text that is not the kind's placeholder: what
    ``interrupt`` clears with one Ctrl-U (design 23.11). Unlike ``is_typed``, no prompt
    line at all is not text (it may be a dialog, which a key could answer)."""
    return line is not None and is_typed(line, kind)


def is_typed(line: Optional[str], kind: str) -> bool:
    """A prompt line holds the owner's text: anything but empty or the kind's placeholder.
    No prompt line at all (None) counts too: the agent shows something else to answer."""
    if line is None:
        return True
    return bool(line) and not is_placeholder(line, kind)


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
    else the detail (``owner typing in <pane>`` in the owner's pane, ``text in its input
    (<pane>); press Ctrl-U there to clear it`` elsewhere). The prompt line is read first,
    focused or not (design 23.11); the focus chain is asked only when it holds text.
    Raises HerdrError when herdr cannot answer."""
    if not pane_id or not checkable(kind):
        return None
    line = prompt_line(herdr.pane_read(pane_id, SCREEN_LINES), kind)
    if not is_typed(line, kind):
        return None
    if owner_pane(herdr, pane_id):
        return OWNER_TYPING % pane_id
    return (DIALOG if line is None or CHOICE_RE.match(line) else IN_INPUT) % pane_id


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
