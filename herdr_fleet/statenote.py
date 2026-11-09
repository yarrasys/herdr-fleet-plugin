"""State note sections (design 16.5 and 40.3): show, keys, get, set and append.

The state note (``state_note`` in the fleet file) is Markdown. A section is a
``## KEY`` heading and everything up to the next level-1 or level-2 heading.
``set`` replaces one section's text and stamps its heading
``## KEY (updated 2026-10-20 00:31 UTC)``, appending the section at the end
when it is absent. ``get`` requires a unique heading; ``append`` retains the old
body. Both writers read and replace under a strict parent-directory flock.
Text outside sections is preserved; writes are atomic.
"""

from __future__ import annotations

import datetime
import fcntl
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
    return _replace_section(text, key, body.strip("\n"), now)


def _replace_section(text: str, key: str, body: str, now: Optional[datetime.datetime] = None) -> str:
    now = now or datetime.datetime.now(datetime.timezone.utc)
    stamp = now.astimezone(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    block = ["## %s (updated %s)\n" % (key, stamp)]
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
    if action in ("show", "keys"):
        current = _read(path)
        if action == "show":
            ctx.say(current.rstrip("\n") if current.strip() else "(%s is empty)" % path)
        else:
            for name in keys(current):
                ctx.say(name)
        return EXIT_OK
    if not key or not key.strip() or len(key.splitlines()) != 1 or "\r" in key or "\n" in key:
        raise UsageError("state %s needs a nonempty one-line KEY" % action)
    if action == "get":
        body = section_body(_read(path), key)
        if body is None:
            raise UsageError("no section ## %s in %s; use state keys to list sections" % (key, path))
        if body.rstrip("\n"):
            ctx.say(body.rstrip("\n"))
        return EXIT_OK
    if text_file is not None:
        if text is not None:
            raise UsageError("give TEXT or --file, not both")
        try:
            with open(text_file, "r", encoding="utf-8") as handle:
                text = handle.read()
        except (OSError, UnicodeDecodeError) as exc:
            raise UsageError("cannot read --file %s: %s" % (text_file, exc))
    if text is None:
        raise UsageError("state %s KEY needs TEXT or --file PATH" % action)
    if action == "append" and not text.strip():
        raise UsageError("state append needs non-whitespace TEXT or --file contents")
    # A directory descriptor survives replacement of the note; every process locks
    # the same inode. Lock failures propagate visibly, without the state helper's fallback.
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    fd = os.open(folder, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            current = _read(path)
            if action == "append":
                old = section_body(current, key) or ""
                body = "\n".join(part for part in (old.rstrip("\n"), text.strip("\n")) if part)
                updated = _replace_section(current, key, body)
            else:
                updated = set_section(current, key, text)
            _write(path, updated)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
    ctx.say("updated ## %s in %s" % (key, path))
    return EXIT_OK


def section_body(text: str, key: str) -> Optional[str]:
    """A unique section body, None if absent; never silently choose a duplicate."""
    found = [(start, end) for name, start, end in sections(text) if name == key]
    if len(found) > 1:
        raise UsageError("duplicate sections ## %s; give each section a unique heading" % key)
    if not found:
        return None
    start, end = found[0]
    return "".join(text.splitlines(True)[start + 1:end])
