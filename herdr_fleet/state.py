"""The state file ``<repo>/.herdr/fleet.state.json`` (design section 5).

It records, per seat, the pane the seat lives in, whether herdr-fleet created
that pane, the kind and args the seat was started with, and timestamps; for
the fleet, the workspace and whether herdr-fleet created it; and the watcher
strip ``up`` opened (``"watch"``, design 19), which is never a seat. Pane and
workspace ids are valid for one herdr server, so ``prune_state`` drops ids
herdr no longer knows. Writes are atomic (temp file in ``.herdr`` then
rename) and keep ``.herdr/.gitignore`` ignoring the state file.

``state_seen`` (design 20.5) is the one field several commands write at once (the
watcher, ``status``, ``send``, ``deliver``): ``record_seen`` reads the file fresh,
changes only those entries and writes it back, all under a lock on the ``.herdr``
directory that ``save_state`` takes too, and ``save_state`` keeps the ``state_seen``
already on disk, so neither side loses the other's changes.
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
import tempfile
from typing import Dict, Iterator, List, Mapping, Optional

from herdr_fleet.herdr import Herdr, HerdrError

STATE_NAME = "fleet.state.json"
IGNORED = (STATE_NAME, "messages.jsonl", "inbox/", "watch.lock")  # .herdr/.gitignore entries (5, 16.2, 18.1)
VERSION = 1


class StateError(Exception):
    """The state file exists but cannot be read."""


def now_iso() -> str:
    """Local time with offset, e.g. 2026-10-19T20:05:11+11:00."""
    return datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(timespec="seconds")


def state_path(repo: str) -> str:
    return os.path.join(repo, ".herdr", STATE_NAME)


class State:
    """In-memory state. Mutators set ``dirty``; ``save_state`` writes it."""

    def __init__(self, path: str, data: Dict, existed: bool):
        self.path = path
        self.data = data
        self.existed = existed
        self.dirty = False

    @property
    def agents(self) -> Dict[str, Dict]:
        return self.data.setdefault("agents", {})

    def entry(self, name: str) -> Optional[Dict]:
        return self.agents.get(name)

    def pane_of(self, name: str) -> Optional[str]:
        entry = self.entry(name)
        return entry.get("pane_id") if entry else None

    def update(self, name: str, **fields: object) -> Dict:
        entry = self.agents.setdefault(name, {})
        entry.update(fields)
        self.dirty = True
        return entry

    def drop(self, name: str) -> None:
        if self.agents.pop(name, None) is not None:
            self.dirty = True

    def set_workspace(self, workspace_id: Optional[str], label: Optional[str], created: bool) -> None:
        changed = (self.data.get("workspace_id"), self.data.get("workspace_label"),
                   self.data.get("created_workspace")) != (workspace_id, label, created)
        self.data["workspace_id"] = workspace_id
        self.data["workspace_label"] = label
        self.data["created_workspace"] = created
        self.dirty = self.dirty or changed

    @property
    def workspace_id(self) -> Optional[str]:
        return self.data.get("workspace_id")

    @property
    def watch(self) -> Optional[Dict]:
        """The watcher strip ``up`` opened: ``{"pane_id", "created_pane", "started"}`` or None."""
        entry = self.data.get("watch")
        return entry if isinstance(entry, dict) and entry.get("pane_id") else None

    @property
    def watch_pane(self) -> Optional[str]:
        entry = self.watch
        return str(entry["pane_id"]) if entry else None

    def set_watch(self, pane_id: str, created: bool, started: str) -> None:
        self.data["watch"] = {"pane_id": pane_id, "created_pane": created, "started": started}
        self.dirty = True

    def update_watch(self, **fields: object) -> None:
        if self.watch is not None:
            self.data["watch"].update(fields)
            self.dirty = True

    def drop_watch(self) -> None:
        if self.data.pop("watch", None) is not None:
            self.dirty = True

    @property
    def created_workspace(self) -> bool:
        return bool(self.data.get("created_workspace"))


def load_state(repo: str) -> State:
    path = state_path(repo)
    if not os.path.exists(path):
        return State(path, _empty(repo), existed=False)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise StateError("cannot read state file %s: %s (fix or delete it)" % (path, exc))
    if not isinstance(data, dict) or not isinstance(data.get("agents", {}), dict):
        raise StateError("state file %s is not a herdr-fleet state object (fix or delete it)" % path)
    data.setdefault("version", VERSION)
    data["repo"] = repo
    data.setdefault("agents", {})
    return State(path, data, existed=True)


@contextlib.contextmanager
def locked(folder: str) -> Iterator[None]:
    """An exclusive ``flock`` on the ``.herdr`` directory itself (no lock file to ignore),
    held while the state file is read and rewritten. A directory that cannot be locked
    is written without it, as before 0.9.0."""
    try:
        fd = os.open(folder, os.O_RDONLY)
    except OSError:
        yield
        return
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError:
            pass
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            os.close(fd)


def _read(path: str) -> Optional[Dict]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("agents", {}), dict) else None


def save_state(state: State) -> None:
    """Write atomically and make sure ``.herdr/.gitignore`` ignores the file. The
    ``state_seen`` values on disk win over those in memory: ``record_seen`` owns them."""
    folder = os.path.dirname(state.path)
    os.makedirs(folder, exist_ok=True)
    ensure_gitignore(folder)
    with locked(folder):
        disk = _read(state.path) or {}
        for name, entry in (disk.get("agents") or {}).items():
            seen = entry.get("state_seen") if isinstance(entry, dict) else None
            if seen and isinstance(state.agents.get(name), dict):
                state.agents[name]["state_seen"] = seen
        _write(state.path, state.data)
    state.existed = True
    state.dirty = False


def record_seen(repo: str, observed: Mapping[str, str], now: datetime.datetime) -> Dict[str, Dict]:
    """Design 20.5: for each seat name and the state just observed, keep
    ``state_seen = {"state", "since"}`` in its entry, with ``since`` set to now when the
    state differs from the recorded one. Written only when the state file exists (the
    fleet was brought up) and something changed. Returns the entries' ``state_seen``."""
    path = state_path(repo)
    if not observed or not os.path.exists(path):
        return {}
    stamp = now.astimezone().isoformat(timespec="seconds")
    result: Dict[str, Dict] = {}
    with locked(os.path.dirname(path)):
        data = _read(path)
        if data is None:
            return {}
        agents = data.setdefault("agents", {})
        changed = False
        for name, status in observed.items():
            entry = agents.setdefault(name, {})
            if not isinstance(entry, dict):
                continue
            seen = entry.get("state_seen")
            if not (isinstance(seen, dict) and seen.get("state") == status and seen.get("since")):
                seen = {"state": status, "since": stamp}
                entry["state_seen"] = seen
                changed = True
            result[name] = dict(seen)
        if changed:
            _write(path, data)
    return result


def mark_notified(repo: str, name: str, since: str) -> None:
    """Mark the seat's current ``state_seen`` episode as notified (``notify_blocked_after``,
    design 20.5), so a restarted watcher does not notify it again; a later state change
    replaces the record and so starts a new episode."""
    path = state_path(repo)
    if not os.path.exists(path):
        return
    with locked(os.path.dirname(path)):
        data = _read(path)
        entry = ((data or {}).get("agents") or {}).get(name)
        seen = entry.get("state_seen") if isinstance(entry, dict) else None
        if isinstance(seen, dict) and seen.get("since") == since and not seen.get("notified"):
            seen["notified"] = True
            _write(path, data)


def _write(path: str, data: Dict) -> None:
    folder = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix=".fleet.state.", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=False)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def pane_moved(name: str, old: str, new: str) -> str:
    """The line printed whenever a seat's pane id changes (design 16.7), so notes that
    hard-code pane ids can be corrected."""
    return "%s: pane %s → %s" % (name, old, new)


def refresh_pane(state: State, name: str, pane: str, kind: Optional[str] = None) -> Dict:
    """Point a live named seat's entry at the pane it is in now, keeping every
    other field (``adopted``, ``args``, timestamps). A new entry is marked
    ``adopted``: herdr-fleet did not start that session. When an existing entry
    pointed at another pane that still exists, the live pane is a different
    terminal, so it is not recorded as created by herdr-fleet (only created
    panes are ever closed)."""
    entry = state.entry(name)
    if entry is None:
        return state.update(name, pane_id=pane, created_pane=False, kind=kind, adopted=True)
    if entry.get("pane_id") != pane:
        state.update(name, pane_id=pane, created_pane=False)
    return entry


def prune_state(state: State, herdr: Herdr) -> List[str]:
    """Drop recorded panes (the seats' and the watcher strip's) and the workspace that
    herdr no longer knows. A seat whose
    recorded pane is gone but which is live by name elsewhere (its pane was moved,
    e.g. to another workspace, which changes the id) is re-pointed instead, with
    every field kept. Only "not found" errors prune; any other herdr error propagates."""
    messages = []
    live: Optional[Dict[str, str]] = None
    for name in list(state.agents):
        pane = state.agents[name].get("pane_id")
        if not pane:
            continue
        try:
            herdr.pane_get(pane)
        except HerdrError as exc:
            if "not_found" not in exc.code:
                raise
            if live is None:
                live = {str(a["name"]): str(a.get("pane_id")) for a in herdr.agent_list() if a.get("name")}
            if name in live:
                state.agents[name]["pane_id"] = live[name]
                state.dirty = True
                messages.append(pane_moved(name, pane, live[name]))
                continue
            state.drop(name)
            messages.append("state: dropped %s (pane %s no longer exists)" % (name, pane))
    strip = state.watch_pane
    if strip:  # design 19.1: a recorded watcher pane that is gone is dropped like a seat's pane
        try:
            herdr.pane_get(strip)
        except HerdrError as exc:
            if "not_found" not in exc.code:
                raise
            state.drop_watch()
            messages.append("state: dropped the watcher pane %s (no longer exists)" % strip)
    elif "watch" in state.data:
        state.drop_watch()
    workspace = state.workspace_id
    if workspace:
        try:
            herdr.workspace_get(workspace)
        except HerdrError as exc:
            if "not_found" not in exc.code:
                raise
            state.set_workspace(None, state.data.get("workspace_label"), False)
            messages.append("state: dropped workspace %s (no longer exists)" % workspace)
    return messages


def _empty(repo: str) -> Dict:
    return {"version": VERSION, "repo": repo, "workspace_id": None, "workspace_label": None,
            "created_workspace": False, "agents": {}}


def ensure_gitignore(folder: str) -> None:
    """Make ``.herdr/.gitignore`` ignore the files herdr-fleet writes there (the
    state file, the message log, the inbox and the watcher's lock), appending
    only missing lines."""
    path = os.path.join(folder, ".gitignore")
    lines: List[str] = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    present = {line.strip() for line in lines}
    missing = [name for name in IGNORED if name not in present]
    if not missing:
        return
    with open(path, "a", encoding="utf-8") as handle:
        if lines and not _ends_with_newline(path):
            handle.write("\n")
        handle.write("".join(name + "\n" for name in missing))


def _ends_with_newline(path: str) -> bool:
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            return True
        handle.seek(-1, os.SEEK_END)
        return handle.read(1) == b"\n"
