"""Bounded, read-only dashboard observation. No Ctx, seen writes or pane labels."""
from __future__ import annotations

import copy
import datetime
import hashlib
import json
import os
import threading
import time
from typing import Dict, List, Optional

from herdr_fleet import context_size, inbox, messages
from herdr_fleet.fleetfile import load_fleet, find_fleet_file, NAME_RE, FleetError

MAX_FLEETS = 32
MAX_SEATS = 128
MAX_FILE = 2 * 1024 * 1024
MAX_LOG = 1024 * 1024
MAX_ENTRIES = 2000
MAX_MAIL = 2000


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def binding(agent: Dict) -> str:
    return digest([agent.get(k) for k in ("name", "pane_id", "agent", "agent_session", "terminal_id")])


class DataError(ValueError):
    pass


class FileCache:
    def __init__(self):
        self.cache = {}

    def bytes(self, path, limit=MAX_FILE, tail=False):
        try:
            stat = os.stat(path)
        except FileNotFoundError:
            return b"", False
        signature = (stat.st_ino, stat.st_mtime_ns, stat.st_size, limit, tail)
        previous = self.cache.get(path)
        if previous and previous[0] == signature:
            return previous[1], previous[2]
        if stat.st_size > limit and not tail:
            raise DataError("file exceeds observation size limit")
        with open(path, "rb") as handle:
            if tail and stat.st_size > limit:
                handle.seek(stat.st_size - limit)
            data = handle.read(limit + 1)
        truncated = stat.st_size > limit
        if len(data) > limit:
            raise DataError("file changed beyond observation size limit")
        if tail and truncated:
            data = data.partition(b"\n")[2]
        if len(self.cache) >= 256 and path not in self.cache:
            self.cache.pop(next(iter(self.cache)))
        self.cache[path] = (signature, data, truncated)
        return data, truncated

    def object(self, path):
        data, _ = self.bytes(path)
        if not data:
            return {}
        value = json.loads(data)
        if not isinstance(value, dict):
            raise DataError("expected an object")
        return value

    def log(self, path):
        data, truncated = self.bytes(path, MAX_LOG, tail=True)
        # A writer may be in the middle of its final record. Never expose it.
        lines = data.split(b"\n")[:-1]
        truncated = truncated or len(lines) > MAX_ENTRIES
        entries = []
        for line in lines[-MAX_ENTRIES:]:
            try:
                item = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(item, dict) and all(isinstance(item.get(k), str) for k in ("time", "from", "to")):
                if (any(len(item[k]) > 64 for k in ("time", "from", "to"))
                        or ("id" in item and (not isinstance(item["id"], str) or not inbox.ID_RE.fullmatch(item["id"])))):
                    continue
                if "first_line" in item:
                    item["first_line"] = str(item["first_line"])[:1024]
                entries.append(item)
        return entries, truncated


class Registry:
    def __init__(self, paths, cwd, env, all_fleets=False):
        if len(paths or []) > MAX_FLEETS:
            raise DataError("at most 32 fleets may be registered")
        self.cwd, self.env, self.all_fleets = cwd, dict(env), all_fleets
        selected = paths or [find_fleet_file(cwd, None, env)]
        self.explicit = {os.path.realpath(os.path.expanduser(p)) for p in selected}
        self.paths = {}
        self.lock = threading.RLock()
        self.reconcile([])

    def reconcile(self, agents):
        paths = set(self.explicit)
        if self.all_fleets:
            for agent in agents[:MAX_FLEETS * MAX_SEATS]:
                cwd = agent.get("cwd") or agent.get("foreground_cwd")
                if not isinstance(cwd, str):
                    continue
                try:
                    path = find_fleet_file(cwd, None, {})
                    paths.add(os.path.realpath(path))
                except (ValueError, OSError, FleetError):
                    continue
                if len(paths) >= MAX_FLEETS:
                    break
        with self.lock:
            roots = {}
            for path in sorted(paths):
                try:
                    if os.path.getsize(path) > MAX_FILE:
                        raise DataError("oversized fleet")
                    root = os.path.realpath(load_fleet(path).repo)
                except (ValueError, OSError, FleetError):
                    root = "invalid:" + path
                roots.setdefault(root, path)
            self.paths = {digest(p)[:24]: p for p in list(roots.values())[:MAX_FLEETS]}

    def path(self, fleet_id):
        with self.lock:
            path = self.paths.get(fleet_id)
        if path is None:
            raise DataError("fleet is no longer registered; refresh the dashboard")
        return path

    def fleet(self, fleet_id):
        path = self.path(fleet_id)
        if os.path.getsize(path) > MAX_FILE:
            raise DataError("fleet file is too large")
        try:
            fleet = load_fleet(path)
        except FleetError:
            raise DataError("fleet configuration is unavailable or invalid")
        if len(fleet.seats) > MAX_SEATS:
            raise DataError("fleet exceeds the 128-seat observation limit")
        return fleet

    def target(self, fleet_id, name, view, agents):
        fleet = self.fleet(fleet_id)
        seat = fleet.seat(name) if isinstance(name, str) else None
        found = [a for a in agents if a.get("name") == name]
        if seat is None or len(found) != 1:
            raise DataError("seat is absent, removed or ambiguous; refresh the dashboard")
        agent = found[0]
        actual = target_view(fleet, seat, agent)
        if not isinstance(view, str) or view != actual:
            raise DataError("seat binding/configuration changed; refresh before acting")
        return fleet, seat, agent


def target_view(fleet, seat, agent):
    return digest([fleet.path, seat.name, seat.kind, seat.cwd, seat.args, binding(agent)])


def mail_headers(repo, name, box):
    """Bounded metadata only, including a visibly incomplete count when capped."""
    folder = inbox.box_dir(repo, name, box)
    found = []
    try:
        with os.scandir(folder) as entries:
            for index, entry in enumerate(entries):
                if index >= MAX_MAIL:
                    return found, True
                if (entry.is_file(follow_symlinks=False) and inbox.ID_RE.fullmatch(entry.name[:-3])
                        and entry.name.endswith(".md")):
                    try:
                        message = inbox.load_headers(entry.path, box)
                        if message.to == name:
                            found.append(message)
                    except (OSError, ValueError):
                        pass
    except FileNotFoundError:
        pass
    return sorted(found, key=inbox.Message.delivery_key), False


def message_metadata(message):
    return {"id": message.id, "sender": message.sender, "to": message.to, "time": message.time,
            "fyi": message.fyi, "box": message.box, "mode": message.mode}


class Observer:
    def __init__(self, herdr, registry, env, context=False):
        self.herdr, self.registry, self.env = herdr, registry, dict(env)
        self.context = context
        self.cache = FileCache()
        from herdr_fleet.dashboard_conversation import Conversation
        self.conversations = Conversation(registry, self.cache)
        self.lock = threading.RLock()
        self.last = {}
        self.agents = []
        self.revision = 0

    def refresh(self):
        with self.lock:
            now = datetime.datetime.now(datetime.timezone.utc)
            try:
                agents = self.herdr.agent_list()
                if not isinstance(agents, list) or len(agents) > MAX_FLEETS * MAX_SEATS:
                    raise DataError("agent list exceeds limit")
                self.agents = agents
                self.registry.reconcile(agents)
            except Exception:
                for item in self.last.values():
                    item.update(stale=True, warning="Herdr disconnected; showing the last observation")
                return self.snapshot()
            latest = {}
            with self.registry.lock:
                paths = list(self.registry.paths)
            for ident in paths:
                try:
                    fleet = self.registry.fleet(ident)
                    state = self.cache.object(os.path.join(fleet.repo, ".herdr", "fleet.state.json"))
                    if not isinstance(state.get("agents", {}), dict):
                        raise DataError("invalid state")
                    entries, truncated = self.cache.log(messages.log_path(fleet.repo))
                    seats = []
                    for seat in fleet.seats:
                        records = [a for a in agents if a.get("name") == seat.name]
                        agent = records[0] if len(records) == 1 else {}
                        queued, capped = mail_headers(fleet.repo, seat.name, inbox.NEW)
                        queued = [m for m in queued if not inbox.is_due(m, now, fleet.fyi_ttl)]
                        actionable, fyis = inbox.split_fyi(queued)
                        record = state.get("agents", {}).get(seat.name) or {}
                        if not isinstance(record, dict):
                            raise DataError("invalid seat state")
                        status = agent.get("agent_status") or ("unknown" if agent else "absent")
                        if not isinstance(status, str) or status not in ("working", "idle", "done", "blocked", "absent", "unknown"):
                            status = "unknown"
                        item = {"name": seat.name, "kind": agent.get("agent") or seat.kind,
                                "state": status,
                                "view": target_view(fleet, seat, agent) if agent else None,
                                "queued": len(actionable), "fyi": len(fyis), "truncated": capped,
                                "limited": ({k: v for k, v in (record.get("limited") or {}).items()
                                             if k in ("kind", "at", "resets_at", "released")
                                             and isinstance(v, (str, int, float, bool, type(None)))
                                             and (not isinstance(v, str) or len(v) <= 128)}
                                            if isinstance(record.get("limited"), dict) else None),
                                "state_seen": ({k: v for k, v in (record.get("state_seen") or {}).items()
                                                if k in ("state", "since") and isinstance(v, str) and len(v) <= 128}
                                               if isinstance(record.get("state_seen"), dict) else None)}
                        if self.context:
                            info = context_size.seat_context(agent, self.env)
                            item["context"] = ({k: v for k, v in (info or {}).items()
                                                if k in ("tokens", "window", "at", "source")} or None)
                        seats.append(item)
                    owner, capped = mail_headers(fleet.repo, "owner", inbox.NEW)
                    owner = [m for m in owner if not m.fyi and not inbox.is_due(m, now, fleet.fyi_ttl)]
                    watcher = self.cache.object(os.path.join(fleet.repo, ".herdr", "watch.lock"))
                    pid = watcher.get("pid")
                    running = None
                    if type(pid) is int and pid > 0:
                        try:
                            os.kill(pid, 0)
                            running = True
                        except ProcessLookupError:
                            running = False
                        except PermissionError:
                            running = None
                    debts = messages.owed(entries)
                    latest[ident] = {"id": ident, "label": fleet.workspace or os.path.basename(fleet.repo),
                                     "observed_at": now.isoformat(), "stale": False, "warning": None,
                                     "seats": seats, "owner": [message_metadata(m) for m in owner[:100]],
                                     "owed": [{k: d.get(k) for k in ("id", "from", "to", "since")} for d in debts[:200]],
                                     "watcher": {"running": running, "since": watcher.get("since")},
                                     "contacts": [{"name": c.name} for c in fleet.contacts[:128]],
                                     "events": [{k: e[k] for k in ("id", "time", "from", "to", "kind", "delivery")
                                                 if k in e} for e in entries[-100:] if e.get("id")],
                                     "truncated": truncated or capped}
                except Exception:
                    previous = copy.deepcopy(self.last.get(ident, {"id": ident, "label": "Unavailable fleet",
                                                                  "seats": [], "owner": [], "owed": []}))
                    previous.update(stale=True, warning="Fleet files unavailable or invalid; last observation retained")
                    latest[ident] = previous
            self.last = latest
            self.revision += 1
            return self.snapshot()

    def snapshot(self):
        with self.lock:
            return {"demo": False, "revision": self.revision, "fleets": copy.deepcopy(list(self.last.values()))}

    def conversation(self, source, fleet, seat, limit=50):
        with self.lock:
            return self.conversations.read(source, fleet, seat, limit)

    def listing(self, fleet_id, mode, name=None, limit=50, ref=None):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise DataError("limit must be between 1 and 100")
        with self.lock:
            fleet = self.registry.fleet(fleet_id)
            if mode == "log":
                entries, truncated = self.cache.log(messages.log_path(fleet.repo))
                allowed = ("id", "time", "from", "to", "kind", "delivery", "via", "fyi", "first_line", "reply_to")
                return {"items": [{k: e[k] for k in allowed if k in e} for e in entries[-limit:]],
                        "truncated": truncated or len(entries) > limit}
            if mode == "watch-log":
                data, truncated = self.cache.bytes(os.path.join(fleet.repo, ".herdr", "watch.log"), 65536, tail=True)
                lines = data.decode("utf-8", "replace").splitlines()
                return {"items": lines[-limit:], "truncated": truncated or len(lines) > limit}
            approved = {"owner"} | {s.name for s in fleet.seats}
            if name not in approved:
                raise DataError("mailbox is not registered")
            headers = []
            capped = False
            for box in (inbox.NEW, inbox.CUR):
                items, part = mail_headers(fleet.repo, name, box)
                headers += items
                capped = capped or part
            if ref is not None:
                if not isinstance(ref, str) or not inbox.ID_RE.fullmatch(ref):
                    raise DataError("inspection needs a full message id")
                headers = [m for m in headers if m.id == ref]
                if len(headers) != 1:
                    raise DataError("message missing or ambiguous in this mailbox")
            headers.sort(key=inbox.Message.sort_key, reverse=True)
            result = []
            for message in headers[:limit]:
                # Paths are server-derived; bodies are read only for this explicit listing.
                with open(message.path, "rb") as handle:
                    raw = handle.read(65537)
                if len(raw) > 65536:
                    raw = raw[:65536]
                    capped = True
                body = raw.partition(b"\n\n")[2].decode("utf-8", "replace")
                result.append(dict(message_metadata(message), body=body, body_truncated=len(raw) >= 65536))
            return {"items": result, "truncated": capped or len(headers) > limit}
