"""Kickoff templates (design section 3).

A template is a seat's ``kickoff`` file, its ``kickoff_text``, the fleet's
default ``kickoff`` file, or the built-in ``DEFAULT_TEMPLATE``. Placeholders
expand with ``str.format_map`` semantics (``{{`` and ``}}`` are literal
braces); a placeholder that is not one of ours, or that cannot be parsed, is
left exactly as written, so templates may quote code containing braces.
The composed text starts with ``[from herdr-fleet]`` unless the template
already starts with ``[from``. Whole lines that are HTML comments
(``<!-- ... -->``) are notes for humans and are removed before rendering, so
they are never sent (``init`` writes one into its starter template).
"""

from __future__ import annotations

import shlex
import string
from typing import Dict, List, Mapping, Optional, Sequence

from herdr_fleet.fleetfile import Fleet, Seat

PREFIX = "[from herdr-fleet]"
PLACEHOLDERS = ("name", "kind", "args", "repo", "cwd", "workspace", "roster", "contacts", "state", "note",
                "reason")

DEFAULT_TEMPLATE = """[from herdr-fleet] You are {name}, a {kind} agent in the "{workspace}" fleet for {repo}.
Read AGENTS.md there. This message was sent by the fleet tool on {reason}.
Team (herdr names; message them with `herdr agent prompt <name> "..."`):
{roster}
{contacts}
{state}
{note}
"""


def render(template: str, values: Mapping[str, str]) -> str:
    """Expand known placeholders; leave everything else as written."""
    try:
        parts = list(string.Formatter().parse(template))
    except ValueError:
        return _render_plain(template, values)
    out: List[str] = []
    for literal, field, spec, conversion in parts:
        out.append(literal)
        if field is None:
            continue
        if field in values:
            value = values[field]
            if conversion == "r":
                value = repr(value)
            elif conversion == "a":
                value = ascii(value)
            try:
                out.append(format(value, spec or ""))
            except ValueError:
                out.append(str(value))
        else:
            out.append("{" + field + ("!" + conversion if conversion else "")
                       + (":" + spec if spec else "") + "}")
    return "".join(out)


def _render_plain(template: str, values: Mapping[str, str]) -> str:
    """Fallback for templates with unbalanced braces: replace ``{key}`` only."""
    for key, value in values.items():
        template = template.replace("{" + key + "}", value)
    return template


def strip_comments(template: str) -> str:
    """Drop whole-line HTML comments (``<!-- ... -->`` on one line)."""
    kept = [line for line in template.splitlines(True)
            if not (line.strip().startswith("<!--") and line.strip().endswith("-->"))]
    return "".join(kept)


ROSTER_SENTENCE = ("Message a seat with `herdr-fleet send <name> \"...\"`: status reports go with `--fyi`; "
                   "a message that asks for something does not.")


def roster_table(rows: Sequence[Sequence[str]]) -> str:
    """``{roster}``: a Markdown table of seats, rows of (name, kind, pane id, cwd), then
    the roster sentence on how to message them (design 17.1)."""
    lines = ["| name | kind | pane | cwd |", "| --- | --- | --- | --- |"]
    for row in rows:
        lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    lines += ["", ROSTER_SENTENCE]
    return "\n".join(lines)


def template_for(seat: Seat, fleet: Fleet) -> str:
    """The seat's template text (raises OSError if a template file vanished)."""
    if seat.kickoff_text:
        return seat.kickoff_text
    path = seat.kickoff or fleet.kickoff
    if path:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    return DEFAULT_TEMPLATE


def read_state_note(fleet: Fleet) -> str:
    if not fleet.state_note:
        return ""
    try:
        with open(fleet.state_note, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def compose(seat: Seat, fleet: Fleet, workspace: str, roster: str, reason: str,
            note: Optional[str] = None, state_text: Optional[str] = None, contacts: str = "") -> str:
    """The full kickoff message for one seat."""
    values: Dict[str, str] = {
        "name": seat.name,
        "kind": seat.kind,
        "args": " ".join(shlex.quote(a) for a in seat.args),
        "repo": fleet.repo,
        "cwd": seat.cwd,
        "workspace": workspace or "",
        "roster": roster,
        "contacts": contacts,
        "state": read_state_note(fleet) if state_text is None else state_text,
        "note": note or "",
        "reason": reason,
    }
    template = strip_comments(template_for(seat, fleet))
    text = render(template, values).strip()
    if not text.startswith("[from"):
        text = PREFIX + " " + text
    return text
