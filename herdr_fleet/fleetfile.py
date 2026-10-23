"""The fleet file: discovery, TOML parsing and validation (design sections 2 and 14).

``find_fleet_file`` walks up from the invoking directory to the first
directory holding ``.herdr/fleet.toml`` unless ``--file`` or
``HERDR_FLEET_FILE`` names one. ``load_fleet`` parses it into a ``Fleet`` of
``Seat`` records and reports every validation error at once as a
``FleetError`` (the CLI exits 2). Kind validation needs the installed herdr,
so it lives in ``check_kinds`` and callers decide whether a bad kind warns
(doctor) or refuses (operations that start agents).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover - exercised on Python < 3.11
    from herdr_fleet._vendor import tomli as tomllib

NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
FLEET_DIR = ".herdr"
FLEET_NAME = "fleet.toml"
DIRECTIONS = ("right", "down")

TOP_KEYS = {"workspace", "cwd", "state_note", "kickoff", "layout", "agents", "contacts", "quit_timeout_ms",
            "restart_hint_tokens", "watch", "inbox_print_max", "interrupt"}
CONTACT_KEYS = {"name", "match", "note"}
MATCH_KEYS = {"cwd", "kind", "name"}
LAYOUT_KEYS = {"mode", "columns", "direction", "ratio"}
MODES = ("chain", "grid")
WATCH_KEYS = {"ratio", "position", "notify_blocked_after"}
INBOX_PRINT_MAX = 8192               # design 20.4: bytes of one message `inbox --read` prints in full
DURATION_RE = re.compile(r"^([1-9][0-9]*)([mhd])$")  # the --ttl grammar (30m, 2h, 1d)
DURATION_UNITS = {"m": 60, "h": 3600, "d": 86400}
WATCH_POSITIONS = ("bottom",)        # design 19.1: the only position in 0.8.0
WATCH_RATIO = 0.12                   # the strip's default share of the area it is split from
WATCH_RATIO_RANGE = (0.1, 0.4)       # herdr clamps every split ratio to 0.1-0.9 (19.4)
SEAT_KEYS = {"name", "kind", "args", "cwd", "split", "ratio", "from", "env", "kickoff",
             "kickoff_text", "quit", "notes", "worktrees", "label"}
LABEL_MAX = 40                       # design 22.1: the seat key `label`, shown on the pane's border
INTERRUPT_ALLOWED = ("ctrl+c", "esc", "enter")  # design 23.7: the keys an [interrupt] entry may name


class FleetError(Exception):
    """The fleet file is missing or invalid. ``errors`` lists every problem."""

    def __init__(self, errors: Sequence[str]):
        self.errors = list(errors)
        super().__init__("\n".join(self.errors))


@dataclass
class Seat:
    """One ``[[agents]]`` entry; paths are already resolved to absolute paths."""

    name: str
    kind: str
    args: List[str]
    cwd: str
    split: str
    ratio: Optional[float]
    env: Dict[str, str]
    kickoff: Optional[str]
    kickoff_text: Optional[str]
    quit: Optional[str]
    notes: str
    index: int
    from_seat: Optional[str] = None  # chain mode: split from this earlier seat's pane (``from``)
    worktrees: List[str] = field(default_factory=list)  # path globs, ~ expanded (design 17.2)
    label: Optional[str] = None  # the pane's border name instead of the seat name (design 22.1)


@dataclass
class Contact:
    """A ``[[contacts]]`` entry (design 16.4): a peer the team messages but never manages."""

    name: str
    cwd: Optional[str]          # resolved: absolute, ~ expanded
    kind: Optional[str]
    agent_name: Optional[str]   # herdr name to match
    note: str


@dataclass
class Fleet:
    path: str
    repo: str
    workspace: Optional[str]
    cwd: str
    state_note: Optional[str]
    kickoff: Optional[str]
    direction: str
    ratio: Optional[float]
    quit_timeout_ms: Optional[int]
    seats: List[Seat]
    warnings: List[str] = field(default_factory=list)
    mode: str = "chain"              # chain | grid (design section 14)
    columns: Optional[int] = None    # grid only
    contacts: List[Contact] = field(default_factory=list)
    restart_hint_tokens: Optional[int] = None  # status --context: "consider restart" at or above (17.4)
    watch: bool = True                         # design 19: `up` starts the watcher in a strip
    watch_ratio: float = WATCH_RATIO           # [watch] ratio, clamped to WATCH_RATIO_RANGE
    watch_position: str = "bottom"             # [watch] position
    notify_blocked_after: Optional[int] = None  # [watch] notify_blocked_after in seconds (design 20.5)
    inbox_print_max: int = INBOX_PRINT_MAX     # design 20.4
    interrupt: Dict[str, List[str]] = field(default_factory=dict)  # [interrupt] keys per kind (design 23.7)

    def contact(self, name: str) -> Optional[Contact]:
        for contact in self.contacts:
            if contact.name == name:
                return contact
        return None

    def seat(self, name: str) -> Optional[Seat]:
        for seat in self.seats:
            if seat.name == name:
                return seat
        return None

    def kinds(self) -> List[str]:
        return sorted({seat.kind for seat in self.seats})


def load_toml(path: str) -> Dict:
    with open(path, "rb") as handle:
        return tomllib.load(handle)


def repo_root_for(path: str) -> str:
    """The repo root for a fleet file: the parent of its ``.herdr`` directory,
    or the file's own directory when it lives elsewhere."""
    folder = os.path.dirname(os.path.abspath(path))
    if os.path.basename(folder) == FLEET_DIR:
        return os.path.dirname(folder)
    return folder


def find_fleet_file(start: str, explicit: Optional[str] = None,
                    env: Optional[Mapping[str, str]] = None) -> str:
    """Locate the fleet file; raise FleetError when there is none."""
    env = env if env is not None else os.environ
    chosen = explicit or env.get("HERDR_FLEET_FILE")
    if chosen:
        path = os.path.abspath(os.path.join(start, os.path.expanduser(chosen)))
        if not os.path.isfile(path):
            raise FleetError(["fleet file not found: %s" % path])
        return path
    folder = os.path.abspath(start)
    while True:
        candidate = os.path.join(folder, FLEET_DIR, FLEET_NAME)
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(folder)
        if parent == folder:
            raise FleetError(["no %s/%s found in %s or any parent directory (use --file PATH); "
                              "run `herdr-fleet init` to create one" % (FLEET_DIR, FLEET_NAME, os.path.abspath(start))])
        folder = parent


def resolve_path(raw: str, repo: str) -> str:
    """Resolve ``~/…``, absolute and repo-relative paths."""
    expanded = os.path.expanduser(raw)
    if os.path.isabs(expanded):
        return os.path.normpath(expanded)
    return os.path.normpath(os.path.join(repo, expanded))


def load_fleet(path: str) -> Fleet:
    """Parse and validate a fleet file (everything except kinds)."""
    path = os.path.abspath(path)
    repo = repo_root_for(path)
    try:
        data = load_toml(path)
    except (OSError, ValueError) as exc:  # TOMLDecodeError is a ValueError
        raise FleetError(["%s: %s" % (path, exc)])
    errors: List[str] = []
    warnings: List[str] = []
    _unknown_keys(data, TOP_KEYS, "top level", warnings)

    workspace = _opt_str(data, "workspace", "workspace", errors)
    base_cwd = resolve_path(_opt_str(data, "cwd", "cwd", errors) or ".", repo)
    state_note = _opt_str(data, "state_note", "state_note", errors)
    state_note = resolve_path(state_note, repo) if state_note else None
    default_kickoff = _kickoff_path(data, "kickoff", repo, errors)
    quit_timeout = data.get("quit_timeout_ms")
    if quit_timeout is not None and (not isinstance(quit_timeout, int) or isinstance(quit_timeout, bool)
                                     or quit_timeout <= 0):
        errors.append("quit_timeout_ms: must be a positive integer (milliseconds)")
        quit_timeout = None
    hint_tokens = data.get("restart_hint_tokens")
    if hint_tokens is not None and (not isinstance(hint_tokens, int) or isinstance(hint_tokens, bool)
                                    or hint_tokens <= 0):
        errors.append("restart_hint_tokens: must be a positive integer (tokens)")
        hint_tokens = None
    print_max = data.get("inbox_print_max", INBOX_PRINT_MAX)
    if not isinstance(print_max, int) or isinstance(print_max, bool) or print_max <= 0:
        errors.append("inbox_print_max: must be a positive integer (bytes)")
        print_max = INBOX_PRINT_MAX

    layout = data.get("layout", {})
    if not isinstance(layout, dict):
        errors.append("layout: must be a table")
        layout = {}
    _unknown_keys(layout, LAYOUT_KEYS, "[layout]", warnings)
    direction = _direction(layout.get("direction", "right"), "layout.direction", errors)
    ratio = _ratio(layout.get("ratio"), "layout.ratio", errors)
    mode = layout.get("mode", "chain")
    if mode not in MODES:
        errors.append("layout.mode: %r must be chain or grid" % (mode,))
        mode = "chain"
    columns = layout.get("columns")
    if columns is not None and (not isinstance(columns, int) or isinstance(columns, bool) or columns < 1):
        errors.append("layout.columns: %r must be a positive integer" % (columns,))
        columns = None
    if columns is not None and mode != "grid":
        warnings.append("layout.columns: ignored in chain mode (it applies to mode = \"grid\")")

    raw_seats = data.get("agents", [])
    if not isinstance(raw_seats, list) or not all(isinstance(s, dict) for s in raw_seats):
        errors.append("agents: must be an array of tables ([[agents]])")
        raw_seats = []
    if not raw_seats:
        errors.append("agents: the fleet declares no [[agents]]")

    seats: List[Seat] = []
    seen = set()
    for index, raw in enumerate(raw_seats):
        seat = _parse_seat(raw, index, repo, base_cwd, direction, ratio, errors, warnings)
        if seat is None:
            continue
        if seat.name in seen:
            errors.append("agents[%d].name: duplicate name %r" % (index, seat.name))
        _check_from(seat, [s.name for s in seats], errors)
        seen.add(seat.name)
        seats.append(seat)
    if mode == "grid":
        _grid_overrides(seats, raw_seats, direction, ratio, warnings)
    contacts = _parse_contacts(data.get("contacts", []), repo, [s.name for s in seats], errors, warnings)
    watch, watch_ratio, watch_position, blocked_after = _parse_watch(data.get("watch", True), errors, warnings)
    interrupt = parse_interrupt(data.get("interrupt", {}), errors)

    if errors:
        raise FleetError(["%s: %s" % (path, e) for e in errors])
    return Fleet(path=path, repo=repo, workspace=workspace, cwd=base_cwd, state_note=state_note,
                 kickoff=default_kickoff, direction=direction, ratio=ratio,
                 quit_timeout_ms=quit_timeout, seats=seats, warnings=warnings, mode=mode, columns=columns,
                 contacts=contacts, restart_hint_tokens=hint_tokens, watch=watch, watch_ratio=watch_ratio,
                 watch_position=watch_position, notify_blocked_after=blocked_after, inbox_print_max=print_max,
                 interrupt=interrupt)


def check_kinds(fleet: Fleet, kinds: Optional[Sequence[str]]) -> List[str]:
    """Errors for seats whose kind the installed herdr does not support.
    Returns no errors when the kind list is unknown (``kinds`` is None)."""
    if kinds is None:
        return []
    known = set(kinds)
    return ["%s: agents[%d].kind %r is not a kind this herdr supports (%s)"
            % (fleet.path, seat.index, seat.kind, "|".join(kinds))
            for seat in fleet.seats if seat.kind not in known]


def _parse_seat(raw: Dict, index: int, repo: str, base_cwd: str, direction: str,
                ratio: Optional[float], errors: List[str], warnings: List[str]) -> Optional[Seat]:
    where = "agents[%d]" % index
    _unknown_keys(raw, SEAT_KEYS, where, warnings)
    name = raw.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        errors.append("%s.name: %r must match [a-z][a-z0-9_-]{0,31}" % (where, name))
        name = None
    else:
        where = "agents[%d] (%s)" % (index, name)
    kind = raw.get("kind")
    if not isinstance(kind, str) or not kind:
        errors.append("%s.kind: required string" % where)
        kind = None
    args = raw.get("args", [])
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        errors.append("%s.args: must be a list of strings" % where)
        args = []
    cwd_raw = _opt_str(raw, "cwd", where + ".cwd", errors)
    cwd = resolve_path(cwd_raw, repo) if cwd_raw else base_cwd
    if not os.path.isdir(cwd):
        warnings.append("%s.cwd: directory does not exist: %s" % (where, cwd))
    split = _direction(raw.get("split", direction), where + ".split", errors)
    seat_ratio = _ratio(raw.get("ratio"), where + ".ratio", errors)
    env = raw.get("env", {})
    if not isinstance(env, dict) or not all(isinstance(v, str) for v in env.values()):
        errors.append("%s.env: must be a table of strings" % where)
        env = {}
    kickoff = _kickoff_path(raw, "kickoff", repo, errors, where + ".kickoff")
    kickoff_text = _opt_str(raw, "kickoff_text", where + ".kickoff_text", errors)
    if kickoff and kickoff_text:
        errors.append("%s: set kickoff or kickoff_text, not both" % where)
    quit_cmd = _opt_str(raw, "quit", where + ".quit", errors)
    notes = _opt_str(raw, "notes", where + ".notes", errors) or ""
    label = _label(raw, where + ".label", errors)
    from_seat = _opt_str(raw, "from", where + ".from", errors)
    worktrees = _worktree_globs(raw.get("worktrees", []), where + ".worktrees", errors, warnings)
    if name is None or kind is None:
        return None
    return Seat(name=name, kind=kind, args=list(args), cwd=cwd, split=split,
                ratio=seat_ratio if seat_ratio is not None else ratio, env=dict(env),
                kickoff=kickoff, kickoff_text=kickoff_text, quit=quit_cmd, notes=notes, index=index,
                from_seat=from_seat, worktrees=worktrees, label=label)


def _label(raw: Dict, where: str, errors: List[str]) -> Optional[str]:
    """The seat key ``label`` (design 22.1): a non-empty single line of at most 40
    characters, kept without surrounding blanks. It must not start with ``-``, which
    ``herdr pane rename`` would read as an option."""
    value = _opt_str(raw, "label", where, errors)
    if value is None:
        return None
    text = value.strip()
    if not text:
        errors.append("%s: must not be empty" % where)
    elif "\n" in text or "\r" in text:
        errors.append("%s: must be a single line" % where)
    elif len(text) > LABEL_MAX:
        errors.append("%s: %d characters; at most %d" % (where, len(text), LABEL_MAX))
    elif text.startswith("-"):
        errors.append("%s: %r must not start with '-'" % (where, text))
    else:
        return text
    return None


def _worktree_globs(value: object, where: str, errors: List[str], warnings: List[str]) -> List[str]:
    """``worktrees = ["<glob>", ...]`` (design 17.2): non-empty strings, ``~`` expanded.
    A glob that is still relative matches no worktree path, so it is warned about."""
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        errors.append("%s: must be a list of non-empty strings (path globs)" % where)
        return []
    globs = [os.path.expanduser(v) for v in value]
    for raw, expanded in zip(value, globs):
        if not os.path.isabs(expanded):
            warnings.append("%s: %r is not an absolute path or ~/...; it matches no worktree" % (where, raw))
    return globs


def _check_from(seat: Seat, earlier: List[str], errors: List[str]) -> None:
    """``from`` must name a seat earlier in the file (design 14.3)."""
    if seat.from_seat is not None and seat.from_seat not in earlier:
        errors.append("agents[%d] (%s).from: %r must name a seat earlier in the file (%s)"
                      % (seat.index, seat.name, seat.from_seat, ", ".join(earlier) or "none"))


def _grid_overrides(seats: List[Seat], raw_seats: List[Dict], direction: str, ratio: Optional[float],
                    warnings: List[str]) -> None:
    """Grid mode places seats itself: per-seat ``split``, ``ratio`` and ``from`` are
    ignored (with a warning); seats appended to an existing layout use [layout]."""
    for seat in seats:
        ignored = [key for key in ("split", "ratio", "from") if key in raw_seats[seat.index]]
        if ignored:
            warnings.append("agents[%d] (%s): %s ignored in grid mode"
                            % (seat.index, seat.name, ", ".join(ignored)))
        seat.split, seat.ratio, seat.from_seat = direction, ratio, None


def parse_duration(text: object) -> Optional[int]:
    """``30m``, ``2h`` or ``1d`` (the ``--ttl`` grammar) in seconds; None when malformed."""
    match = DURATION_RE.match(text.strip()) if isinstance(text, str) else None
    return int(match.group(1)) * DURATION_UNITS[match.group(2)] if match else None


def _parse_watch(value: object, errors: List[str],
                 warnings: List[str]) -> Tuple[bool, float, str, Optional[int]]:
    """``watch = false`` (or ``true``) at the top level, or a ``[watch]`` table with ``ratio``,
    ``position`` (design 19.1) and ``notify_blocked_after`` (20.5). TOML cannot hold both,
    so the table means "on"."""
    if isinstance(value, bool):
        return value, WATCH_RATIO, "bottom", None
    if not isinstance(value, dict):
        errors.append("watch: must be true, false or a [watch] table")
        return True, WATCH_RATIO, "bottom", None
    _unknown_keys(value, WATCH_KEYS, "[watch]", warnings)
    ratio = _ratio(value.get("ratio"), "watch.ratio", errors)
    if ratio is None:
        ratio = WATCH_RATIO
    low, high = WATCH_RATIO_RANGE
    if not low <= ratio <= high:
        clamped = min(max(ratio, low), high)
        warnings.append("watch.ratio: %s is outside %s-%s; using %s" % (ratio, low, high, clamped))
        ratio = clamped
    position = value.get("position", "bottom")
    if position not in WATCH_POSITIONS:
        errors.append("watch.position: %r is not supported (only %s)"
                      % (position, ", ".join('"%s"' % p for p in WATCH_POSITIONS)))
        position = "bottom"
    blocked_after = None
    if "notify_blocked_after" in value:
        blocked_after = parse_duration(value["notify_blocked_after"])
        if blocked_after is None:
            errors.append("watch.notify_blocked_after: %r must be a duration such as \"3m\", \"2h\" or \"1d\""
                          % (value["notify_blocked_after"],))
    return True, float(ratio), str(position), blocked_after


def interrupt_problems(kind: str, keys: object) -> List[str]:
    """What is wrong with one ``[interrupt]`` entry (design 23.7): it must be a non-empty
    list of keys from INTERRUPT_ALLOWED, and a Claude seat gets one ``ctrl+c`` at most,
    since Claude Code quits on the second."""
    if not isinstance(keys, list) or not keys or not all(isinstance(k, str) for k in keys):
        return ["%s: must be a non-empty list of keys (%s)" % (kind, ", ".join(INTERRUPT_ALLOWED))]
    bad = [k for k in keys if k not in INTERRUPT_ALLOWED]
    if bad:
        return ["%s: %s not allowed; the keys are limited to %s" % (kind, ", ".join(repr(k) for k in bad),
                                                                  ", ".join(INTERRUPT_ALLOWED))]
    if kind == "claude" and keys.count("ctrl+c") > 1:
        return ["%s: Claude Code quits on a second Ctrl-C, so a Claude seat gets one ctrl+c at most" % kind]
    return []


def parse_interrupt(value: object, errors: List[str], where: str = "interrupt") -> Dict[str, List[str]]:
    """``[interrupt]`` (design 23.7): per kind, the keys ``interrupt NAME`` presses in a
    working seat of that kind, overriding ``seats.INTERRUPT_KEYS``. Every problem is an
    error; a valid entry is kept."""
    if not isinstance(value, dict):
        errors.append("%s: must be a table of kind = [keys]" % where)
        return {}
    keys: Dict[str, List[str]] = {}
    for kind, entry in value.items():
        problems = interrupt_problems(str(kind), entry)
        if problems:
            errors.extend("%s.%s" % (where, problem) for problem in problems)
        else:
            keys[str(kind)] = [str(k) for k in entry]
    return keys


def _parse_contacts(raw: object, repo: str, seat_names: List[str], errors: List[str],
                    warnings: List[str]) -> List[Contact]:
    """``[[contacts]]`` (design 16.4): names follow the herdr pattern and never collide
    with seats; ``match`` needs at least one of cwd, kind, name."""
    if not isinstance(raw, list) or not all(isinstance(c, dict) for c in raw):
        errors.append("contacts: must be an array of tables ([[contacts]])")
        return []
    contacts: List[Contact] = []
    for index, table in enumerate(raw):
        where = "contacts[%d]" % index
        _unknown_keys(table, CONTACT_KEYS, where, warnings)
        name = table.get("name")
        if not isinstance(name, str) or not NAME_RE.match(name):
            errors.append("%s.name: %r must match [a-z][a-z0-9_-]{0,31}" % (where, name))
            continue
        where = "contacts[%d] (%s)" % (index, name)
        if name in seat_names:
            errors.append("%s.name: %r is already a seat name" % (where, name))
        if any(c.name == name for c in contacts):
            errors.append("%s.name: duplicate contact %r" % (where, name))
        match = table.get("match")
        if not isinstance(match, dict) or not match:
            errors.append("%s.match: required table with cwd, kind and/or name" % where)
            continue
        _unknown_keys(match, MATCH_KEYS, where + ".match", warnings)
        cwd = _opt_str(match, "cwd", where + ".match.cwd", errors)
        kind = _opt_str(match, "kind", where + ".match.kind", errors)
        agent_name = _opt_str(match, "name", where + ".match.name", errors)
        if agent_name and agent_name in seat_names:
            errors.append("%s.match.name: %r is a seat; contacts are never seats" % (where, agent_name))
        if not (cwd or kind or agent_name):
            errors.append("%s.match: needs at least one of cwd, kind, name" % where)
        note = _opt_str(table, "note", where + ".note", errors) or ""
        contacts.append(Contact(name, resolve_path(cwd, repo) if cwd else None, kind, agent_name, note))
    return contacts


def _unknown_keys(table: Dict, allowed: set, where: str, warnings: List[str]) -> None:
    for key in sorted(set(table) - allowed):
        warnings.append("%s: unknown key %r ignored" % (where, key))


def _opt_str(table: Dict, key: str, where: str, errors: List[str]) -> Optional[str]:
    value = table.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        errors.append("%s: must be a string" % where)
        return None
    return value


def _kickoff_path(table: Dict, key: str, repo: str, errors: List[str],
                  where: Optional[str] = None) -> Optional[str]:
    raw = _opt_str(table, key, where or key, errors)
    if not raw:
        return None
    path = resolve_path(raw, repo)
    if not os.path.isfile(path):
        errors.append("%s: kickoff file does not exist: %s" % (where or key, path))
    return path


def _direction(value: object, where: str, errors: List[str]) -> str:
    if value not in DIRECTIONS:
        errors.append("%s: %r must be right or down" % (where, value))
        return "right"
    return str(value)


def _ratio(value: object, where: str, errors: List[str]) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value < 1:
        errors.append("%s: %r must be a number between 0 and 1 (exclusive)" % (where, value))
        return None
    return float(value)
