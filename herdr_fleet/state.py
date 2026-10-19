"""The state file ``<repo>/.herdr/fleet.state.json`` (design section 5).

It records, per seat, the pane the seat lives in, whether herdr-fleet created
that pane, the kind and args the seat was started with, and timestamps; and
for the fleet, the workspace and whether herdr-fleet created it. Pane and
workspace ids are valid for one herdr server, so ``prune_state`` drops ids
herdr no longer knows. Writes are atomic (temp file in ``.herdr`` then
rename) and keep ``.herdr/.gitignore`` ignoring the state file.
"""

from __future__ import annotations

import datetime
import json
import os
import tempfile
from typing import Dict, List, Optional

from herdr_fleet.herdr import Herdr, HerdrError

STATE_NAME = "fleet.state.json"
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


def save_state(state: State) -> None:
    """Write atomically and make sure ``.herdr/.gitignore`` ignores the file."""
    folder = os.path.dirname(state.path)
    os.makedirs(folder, exist_ok=True)
    _ensure_gitignore(folder)
    fd, tmp = tempfile.mkstemp(prefix=".fleet.state.", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state.data, handle, indent=2, sort_keys=False)
            handle.write("\n")
        os.replace(tmp, state.path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    state.existed = True
    state.dirty = False


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
    """Drop recorded panes and workspace that herdr no longer knows. A seat whose
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
                messages.append("state: %s moved from %s to %s" % (name, pane, live[name]))
                continue
            state.drop(name)
            messages.append("state: dropped %s (pane %s no longer exists)" % (name, pane))
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


def _ensure_gitignore(folder: str) -> None:
    path = os.path.join(folder, ".gitignore")
    lines: List[str] = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    if STATE_NAME in (line.strip() for line in lines):
        return
    with open(path, "a", encoding="utf-8") as handle:
        if lines and lines[-1] != "" and not _ends_with_newline(path):
            handle.write("\n")
        handle.write(STATE_NAME + "\n")


def _ends_with_newline(path: str) -> bool:
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            return True
        handle.seek(-1, os.SEEK_END)
        return handle.read(1) == b"\n"
