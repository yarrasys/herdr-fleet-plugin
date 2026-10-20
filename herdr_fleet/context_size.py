"""Context size per seat for ``status --context`` (design 17.4), read-only.

For a live seat whose herdr record carries ``agent_session.source ==
"herdr:claude"``, the Claude Code transcript is
``<projects>/*/<session id>.jsonl``, where ``<projects>`` is
``HERDR_FLEET_CLAUDE_PROJECTS`` (tests) or ``$HOME/.claude/projects``. The
file is read backwards in 256 KiB chunks, up to 4 MiB, until the last
``assistant`` line with ``message.usage`` is found. Context is
``input_tokens + cache_creation_input_tokens + cache_read_input_tokens``.

Boundary (invariant 9): ``usage_record`` keeps only ``type``,
``timestamp`` and the four usage integers of a line. Message text, tool
calls and every other field are dropped as the line is parsed and are never
logged, printed, stored or returned; neither is the transcript path or the
session id. Nothing under ``~/.claude`` is ever written.
"""

from __future__ import annotations

import datetime
import glob
import json
import os
import re
from typing import Dict, Mapping, Optional

CHUNK = 256 * 1024
LIMIT = 4 * 1024 * 1024
SOURCE = "claude-transcript"
CLAUDE_SESSION = "herdr:claude"
USAGE_FIELDS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
CONTEXT_FIELDS = USAGE_FIELDS[:3]
RECORD_KEYS = ("type", "timestamp") + USAGE_FIELDS
SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")  # a uuid; never a path or a glob
TIME_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:?\d{2})?$")


def projects_dir(env: Mapping[str, str]) -> str:
    override = env.get("HERDR_FLEET_CLAUDE_PROJECTS")
    if override:
        return os.path.expanduser(override)
    home = env.get("HOME") or os.path.expanduser("~")
    return os.path.join(home, ".claude", "projects")


def session_id(agent: Optional[Mapping]) -> Optional[str]:
    """The Claude Code session id from a herdr agent record, else None (other kinds,
    no ``agent_session``, or a value that is not a plain id)."""
    session = (agent or {}).get("agent_session")
    if not isinstance(session, dict) or session.get("source") != CLAUDE_SESSION:
        return None
    value = session.get("value")
    return value if isinstance(value, str) and SESSION_RE.match(value) else None


def find_transcript(projects: str, sid: str) -> Optional[str]:
    """``<projects>/*/<sid>.jsonl``; the newest when (unexpectedly) several match."""
    if not SESSION_RE.match(sid):
        return None
    found = [p for p in glob.glob(os.path.join(glob.escape(projects), "*", sid + ".jsonl")) if os.path.isfile(p)]
    if not found:
        return None
    return max(found, key=lambda p: os.path.getmtime(p))


def usage_record(raw: bytes) -> Optional[Dict]:
    """Keep only ``type``, ``timestamp`` and the four usage integers of an ``assistant``
    line with ``message.usage``; None for anything else. A line whose input counts
    are all zero (a synthetic message, not an API call) is skipped too."""
    if b'"assistant"' not in raw or b'"usage"' not in raw:
        return None
    try:
        line = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None  # a partial or corrupt line
    if not isinstance(line, dict) or line.get("type") != "assistant":
        return None
    message = line.get("message")
    usage = message.get("usage") if isinstance(message, dict) else None
    timestamp = line.get("timestamp")
    del line, message  # nothing else of the line is kept
    if not isinstance(usage, dict):
        return None
    record: Dict = {"type": "assistant", "timestamp": timestamp if isinstance(timestamp, str) else None}
    for key in USAGE_FIELDS:
        value = usage.get(key)
        record[key] = value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
    if not any(record[key] for key in CONTEXT_FIELDS):
        return None
    return record


def last_usage(path: str, chunk: int = CHUNK, limit: int = LIMIT) -> Optional[Dict]:
    """The last usage record in a transcript, reading backwards from the end in
    ``chunk``-byte steps and at most ``limit`` bytes. A trailing partial line fails to
    parse and is skipped; the cut line at the start of each window is kept and
    completed by the next chunk, or dropped when the limit is reached."""
    try:
        handle = open(path, "rb")
    except OSError:
        return None
    with handle:
        handle.seek(0, os.SEEK_END)
        pos = handle.tell()
        head = b""   # the start of a line whose beginning is still before ``pos``
        read = 0
        while pos > 0 and read < limit:
            step = min(chunk, pos, limit - read)
            pos -= step
            handle.seek(pos)
            data = handle.read(step)
            read += len(data)
            lines = (data + head).split(b"\n")
            head = lines.pop(0) if pos > 0 else b""  # at the start of the file every line is whole
            for raw in reversed(lines):
                record = usage_record(raw)
                if record is not None:
                    return record
    return None


def context_tokens(record: Mapping) -> int:
    return sum(int(record.get(key) or 0) for key in CONTEXT_FIELDS)


def parse_time(text: Optional[str]) -> Optional[datetime.datetime]:
    """``2026-10-20T22:40:52.536Z`` and other ISO forms (Python 3.8's ``fromisoformat``
    takes neither ``Z`` nor arbitrary fractions)."""
    match = TIME_RE.match(text or "")
    if not match:
        return None
    year, month, day, hour, minute, second, fraction, zone = match.groups()
    micro = int((fraction or "0")[:6].ljust(6, "0"))
    tz = datetime.timezone.utc
    if zone and zone != "Z":
        sign = 1 if zone[0] == "+" else -1
        digits = zone[1:].replace(":", "")
        tz = datetime.timezone(sign * datetime.timedelta(hours=int(digits[:2]), minutes=int(digits[2:])))
    try:
        return datetime.datetime(int(year), int(month), int(day), int(hour), int(minute), int(second), micro, tz)
    except ValueError:
        return None


def seat_context(agent: Optional[Mapping], env: Mapping[str, str]) -> Optional[Dict]:
    """``{"tokens", "at", "source"}`` for a live Claude Code seat, else None."""
    sid = session_id(agent)
    if sid is None:
        return None
    path = find_transcript(projects_dir(env), sid)
    record = last_usage(path) if path else None
    if record is None:
        return None
    return {"tokens": context_tokens(record), "at": record["timestamp"], "source": SOURCE}


def tokens_label(tokens: int) -> str:
    """``850``, ``183k``, ``1.2M`` (above a million)."""
    if tokens >= 1000000:
        return "%.1fM" % (tokens / 1000000.0)
    if tokens >= 1000:
        return "%dk" % int(round(tokens / 1000.0))
    return str(tokens)


def age_label(seconds: Optional[float]) -> str:
    """``3 min``, ``2 h``, ``5 d``, ``9 w`` (also the AGE column of ``worktrees``); ``?`` when unknown."""
    if seconds is None:
        return "?"
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return "%d min" % (seconds // 60)
    if seconds < 86400:
        return "%d h" % (seconds // 3600)
    if seconds < 7 * 86400:
        return "%d d" % (seconds // 86400)
    return "%d w" % (seconds // (7 * 86400))


def context_label(info: Optional[Mapping], now: Optional[datetime.datetime] = None) -> str:
    """``183k (2 min)``; ``-`` without a reading."""
    if not info:
        return "-"
    label = tokens_label(int(info["tokens"]))
    when = parse_time(info.get("at"))
    if when is None:
        return label
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return "%s (%s)" % (label, age_label((now - when).total_seconds()))
