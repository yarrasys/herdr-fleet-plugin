"""Read-only Claude and Codex context readings (design 17.4 and 29).

Provider resolution, discovery and reading are shared by status and the board.
Read backwards in 256 KiB chunks, at most 4 MiB. Claude counts input plus
cache creation/read; Codex uses last_token_usage.total_tokens and an optional
reported window, never cumulative usage or an inferred model limit.

Boundary (invariants 8 and 9): parsers retain only usage numbers, timestamp and
type/source tags. No prompt, tool content, rate limits, path or session id is
returned or logged. Nothing under either provider's source root is written.
"""

from __future__ import annotations

import datetime
import glob
import json
import os
import re
from typing import Callable, Dict, Mapping, Optional, Tuple

CHUNK = 256 * 1024
LIMIT = 4 * 1024 * 1024
SOURCE = "claude-transcript"
CLAUDE_SESSION = "herdr:claude"
CODEX_SESSION = "herdr:codex"
CODEX_SOURCE = "codex-transcript"
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


def session_id(agent: Optional[Mapping], source: str = CLAUDE_SESSION) -> Optional[str]:
    """A validated id for the requested source (Claude by default), else None."""
    session = (agent or {}).get("agent_session")
    if not isinstance(session, dict) or session.get("source") != source:
        return None
    value = session.get("value")
    return value if isinstance(value, str) and SESSION_RE.fullmatch(value) else None


def sessions_dir(env: Mapping[str, str]) -> str:
    def expand(path: str) -> str:
        home = env.get("HOME") or os.path.expanduser("~")
        if path == "~":
            return home
        if path.startswith("~/"):
            return os.path.join(home, path[2:])
        return os.path.expanduser(path)

    override = env.get("HERDR_FLEET_CODEX_SESSIONS")
    if override:
        return expand(override)
    if env.get("CODEX_HOME"):
        return os.path.join(expand(env["CODEX_HOME"]), "sessions")
    return os.path.join(env.get("HOME") or os.path.expanduser("~"), ".codex", "sessions")


def resolve(agent: Optional[Mapping], env: Mapping[str, str]) -> Optional[Tuple[str, str, str]]:
    """Provider, absolute source root and validated id; never included in output."""
    for provider, source in (("claude", CLAUDE_SESSION), ("codex", CODEX_SESSION)):
        sid = session_id(agent, source)
        if sid is not None:
            root = projects_dir(env) if provider == "claude" else sessions_dir(env)
            if "\0" in root:
                return None
            return provider, os.path.abspath(root), sid
    return None


def _newest(patterns) -> Optional[str]:
    found = []
    for pattern in patterns:
        for path in glob.glob(pattern):
            try:
                if os.path.isfile(path):
                    found.append((os.stat(path).st_mtime_ns, path))
            except OSError:
                continue  # a file can disappear during discovery
    return max(found)[1] if found else None


def find_transcript(projects: str, sid: str) -> Optional[str]:
    """``<projects>/*/<sid>.jsonl``; the newest when (unexpectedly) several match."""
    if not SESSION_RE.fullmatch(sid):
        return None
    return _newest([os.path.join(glob.escape(projects), "*", sid + ".jsonl")])


def find_codex_transcript(root: str, sid: str) -> Optional[str]:
    """Only this session's root-level or year/month/day rollout, newest match first."""
    if not SESSION_RE.fullmatch(sid):
        return None
    root = glob.escape(root)
    name = "rollout-*-" + sid + ".jsonl"
    return _newest([os.path.join(root, name), os.path.join(root, "[0-9]" * 4,
                                                        "[0-9]" * 2, "[0-9]" * 2, name)])


def find_source(provider: str, root: str, sid: str) -> Optional[str]:
    if provider == "claude":
        return find_transcript(root, sid)
    if provider == "codex":
        return find_codex_transcript(root, sid)
    return None


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


def codex_usage_record(raw: bytes) -> Optional[Dict]:
    """Whitelist the latest request's total and optional window from a Codex event."""
    if b'"token_count"' not in raw:
        return None
    try:
        line = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(line, dict) or line.get("type") != "event_msg":
        return None
    payload = line.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "token_count":
        return None
    info = payload.get("info")
    usage = info.get("last_token_usage") if isinstance(info, dict) else None
    tokens = usage.get("total_tokens") if isinstance(usage, dict) else None
    if not isinstance(tokens, int) or isinstance(tokens, bool) or tokens < 0:
        return None
    timestamp = line.get("timestamp")
    result = {"tokens": tokens, "at": timestamp if isinstance(timestamp, str) else None, "source": CODEX_SOURCE}
    window = info.get("model_context_window")
    if isinstance(window, int) and not isinstance(window, bool) and window > 0:
        result["window_tokens"] = window
    return result


def _last_record(path: str, parser: Callable, chunk: int, limit: int) -> Optional[Dict]:
    """The last usage record in a transcript, reading backwards from the end in
    ``chunk``-byte steps and at most ``limit`` bytes. A trailing partial line fails to
    parse and is skipped; the cut line at the start of each window is kept and
    completed by the next chunk, or dropped when the limit is reached."""
    if chunk <= 0 or limit <= 0:
        return None
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            pos = handle.tell()
            head = b""   # the start of a line whose beginning is still before ``pos``
            read = 0
            while pos > 0 and read < limit:
                step = min(chunk, pos, limit - read)
                pos -= step
                handle.seek(pos)
                data = handle.read(step)
                read += step
                lines = (data + head).split(b"\n")
                head = lines.pop(0) if pos > 0 else b""
                for raw in reversed(lines):
                    record = parser(raw)
                    if record is not None:
                        return record
    except (OSError, ValueError):
        return None
    return None


def last_usage(path: str, chunk: int = CHUNK, limit: int = LIMIT) -> Optional[Dict]:
    return _last_record(path, usage_record, chunk, limit)


def last_codex_usage(path: str, chunk: int = CHUNK, limit: int = LIMIT) -> Optional[Dict]:
    return _last_record(path, codex_usage_record, chunk, limit)


def read_context(provider: str, path: str) -> Optional[Dict]:
    if provider == "codex":
        return last_codex_usage(path)
    if provider != "claude":
        return None
    record = last_usage(path)
    return None if record is None else {"tokens": context_tokens(record), "at": record["timestamp"], "source": SOURCE}


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
    try:
        tz = datetime.timezone.utc
        if zone and zone != "Z":
            sign = 1 if zone[0] == "+" else -1
            digits = zone[1:].replace(":", "")
            tz = datetime.timezone(sign * datetime.timedelta(hours=int(digits[:2]), minutes=int(digits[2:])))
        return datetime.datetime(int(year), int(month), int(day), int(hour), int(minute), int(second), micro, tz)
    except ValueError:
        return None


def seat_context(agent: Optional[Mapping], env: Mapping[str, str]) -> Optional[Dict]:
    """Usage/timestamp/source and an optional reported window, else None."""
    resolved = resolve(agent, env)
    if resolved is None:
        return None
    provider, root, sid = resolved
    path = find_source(provider, root, sid)
    return read_context(provider, path) if path else None


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
    """``183k (2 min)`` or ``123k/200k (2 min)``; ``-`` without a reading."""
    if not info:
        return "-"
    label = tokens_label(int(info["tokens"]))
    if info.get("window_tokens") is not None:
        label += "/" + tokens_label(int(info["window_tokens"]))
    when = parse_time(info.get("at"))
    if when is None:
        return label
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return "%s (%s)" % (label, age_label((now - when).total_seconds()))
