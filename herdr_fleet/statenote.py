"""State note sections (design 16.5): ``state show``, ``state set KEY``, ``state keys``.

The state note (``state_note`` in the fleet file) is Markdown. A section is a
``## KEY`` heading and everything up to the next level-1 or level-2 heading.
``set`` replaces one section's text and stamps its heading
``## KEY (updated 2026-10-20 00:31 UTC)``, appending the section at the end
when it is absent. Text outside sections is preserved; writes are atomic.
"""

from __future__ import annotations

import datetime
import os
import re
import tempfile
from typing import List, Optional, Tuple

from herdr_fleet.context import EXIT_OK, Ctx, UsageError

HEADING = re.compile(r"^##\s+(.*?)(?:\s+\(updated [^)]*\))?\s*$")
BOUNDARY = re.compile(r"^#{1,2}\s")
STARTER_SECTIONS = ("production", "in flight", "next", "backlog")


def sections(text: str) -> List[Tuple[str, int, int]]:
    """(key, first line index, end line index) for every ``## KEY`` section."""
    lines = text.splitlines(True)
    found: List[Tuple[str, int, int]] = []
    for index, line in enumerate(lines):
        if BOUNDARY.match(line):
            if found and found[-1][2] == -1:
                key, start, _ = found[-1]
                found[-1] = (key, start, index)
            match = HEADING.match(line.rstrip("\n"))
            if match and line.startswith("## "):
                found.append((match.group(1).strip(), index, -1))
    if found and found[-1][2] == -1:
        key, start, _ = found[-1]
        found[-1] = (key, start, len(lines))
    return found


def keys(text: str) -> List[str]:
    return [key for key, _, _ in sections(text)]


def set_section(text: str, key: str, body: str, now: Optional[datetime.datetime] = None) -> str:
    """Replace (or append) section ``key`` with ``body`` under a stamped heading."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    stamp = now.astimezone(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    block = ["## %s (updated %s)\n" % (key, stamp)]
    body = body.strip("\n")
    if body:
        block.append(body + "\n")
    block.append("\n")
    lines = text.splitlines(True)
    for name, start, end in sections(text):
        if name == key:
            return "".join(lines[:start] + block + lines[end:])
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    if lines and lines[-1].strip():
        lines.append("\n")
    return "".join(lines + block)


def starter_text(intro: str = "") -> str:
    """The four empty sections ``init`` and the Sample example ship."""
    head = intro.rstrip("\n") + "\n\n" if intro else ""
    return head + "".join("## %s\n\n" % key for key in STARTER_SECTIONS)


def _note_path(ctx: Ctx) -> str:
    if not ctx.fleet.state_note:
        raise UsageError("the fleet file sets no state_note (add `state_note = \".herdr/STATE.md\"`)")
    return ctx.fleet.state_note


def _read(path: str) -> str:
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _write(path: str, text: str) -> None:
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".state-note.", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def op_state(ctx: Ctx, action: str, key: Optional[str] = None, text: Optional[str] = None,
             text_file: Optional[str] = None) -> int:
    path = _note_path(ctx)
    current = _read(path)
    if action == "show":
        ctx.say(current.rstrip("\n") if current.strip() else "(%s is empty)" % path)
        return EXIT_OK
    if action == "keys":
        for name in keys(current):
            ctx.say(name)
        return EXIT_OK
    if not key or "\n" in key:
        raise UsageError("state set needs a one-line KEY")
    if text_file:
        if text is not None:
            raise UsageError("give TEXT or --file, not both")
        try:
            with open(text_file, "r", encoding="utf-8") as handle:
                text = handle.read()
        except (OSError, UnicodeDecodeError) as exc:
            raise UsageError("cannot read --file %s: %s" % (text_file, exc))
    if text is None:
        raise UsageError("state set KEY needs TEXT or --file PATH")
    _write(path, set_section(current, key, text))
    ctx.say("updated ## %s in %s" % (key, path))
    return EXIT_OK
