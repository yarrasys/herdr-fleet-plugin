"""Authenticated owner operations and metadata-only intent reservation (41.5).

Only the HTTP handler after session/Origin/CSRF validation calls this adapter.
It is not exposed as a CLI caller override or generic command interface.
"""
from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
import re
import tempfile
from typing import Dict

from herdr_fleet import inbox, mailroom, messages, crossfleet
from herdr_fleet.context import Ctx
from herdr_fleet.dashboard_data import DataError, digest, FileCache, binding
from herdr_fleet.herdr import HerdrError

INTENT_RE = re.compile(r"[A-Za-z0-9_-]{16,128}")
JOURNAL_NAME = "dashboard-intents.json"
MAX_INTENTS = 1000


class Conflict(DataError):
    pass


class RequestState(DataError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class Journal:
    def __init__(self, repo):
        self.folder = os.path.join(repo, ".herdr")
        self.path = os.path.join(self.folder, JOURNAL_NAME)

    @staticmethod
    def retained(existing):
        if existing.get("result") is not None:
            return existing["result"]
        metadata = existing["metadata"]
        result = {"status": "unconfirmed", "certainty": "unconfirmed", "code": 3,
                  "message": "Intent is in progress or interrupted; it will not be resent"}
        if metadata.get("outgoing_id"):
            result["id"] = metadata["outgoing_id"]
        if metadata.get("read_id"):
            result["read_id"] = metadata["read_id"]
            result["read_status"] = metadata.get("read_status")
        return result

    @staticmethod
    def records(raw):
        try:
            doc = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise DataError("intent journal is corrupt; inspect it locally")
        if (not isinstance(doc, dict) or set(doc) != {"version", "intents"} or doc["version"] != 1
                or not isinstance(doc["intents"], dict) or len(doc["intents"]) > MAX_INTENTS):
            raise DataError("intent journal is invalid; inspect it locally")
        for ident, item in doc["intents"].items():
            if (not isinstance(ident, str) or not INTENT_RE.fullmatch(ident) or not isinstance(item, dict)
                    or set(item) != {"fingerprint", "metadata", "result"}
                    or not isinstance(item["fingerprint"], str)
                    or not re.fullmatch(r"[a-f0-9]{64}", item["fingerprint"])
                    or not isinstance(item["metadata"], dict)):
                raise DataError("intent journal contains an invalid retained entry")
            if any(not isinstance(k, str) or not isinstance(v, (str, int, bool, type(None)))
                   or (isinstance(v, str) and len(v) > 256) for k, v in item["metadata"].items()):
                raise DataError("intent journal metadata is invalid")
            result = item["result"]
            if result is not None:
                if (not isinstance(result, dict) or result.get("certainty") not in ("confirmed", "unconfirmed")
                        or result.get("status") not in ("read", "queued", "delivered", "unconfirmed", "expired", "dropped", "refused")
                        or type(result.get("code")) is not int or result["code"] not in (0, 1, 2, 3)
                        or set(result) - {"id", "recipient", "status", "certainty", "code", "source", "fleet", "message",
                                          "read_id", "read_status"}
                        or any(not isinstance(v, (str, int, type(None))) or (isinstance(v, str) and len(v) > 256)
                               for v in result.values())):
                    raise DataError("intent journal retained result is invalid")
        return doc

    def lookup(self, intent, fingerprint):
        try:
            with open(self.path, "rb") as handle:
                raw = handle.read(1048577)
        except FileNotFoundError:
            return None
        if len(raw) > 1048576:
            raise DataError("intent journal exceeds its limit")
        doc = self.records(raw)
        if intent not in doc["intents"]:
            return None
        existing = doc["intents"][intent]
        if not isinstance(existing, dict) or existing.get("fingerprint") != fingerprint:
            raise Conflict("intent id was already used for different content")
        return self.retained(existing)

    def update(self, intent, fingerprint, result=None, metadata=None, progress=None):
        # Called only after all action authentication and input/binding validation.
        os.makedirs(self.folder, exist_ok=True)
        fd = os.open(self.folder, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                try:
                    with open(self.path, "rb") as handle:
                        data = handle.read(1048577)
                    if len(data) > 1048576:
                        raise DataError("intent journal is full; inspect it locally")
                    records = self.records(data)
                except FileNotFoundError:
                    records = {"version": 1, "intents": {}}
                existing = records["intents"].get(intent)
                if intent in records["intents"]:
                    if existing.get("fingerprint") != fingerprint:
                        raise Conflict("intent id was already used for different content")
                    if result is None and progress is None:
                        return self.retained(existing)
                elif result is not None:
                    raise DataError("intent reservation disappeared; delivery may have occurred")
                elif len(records["intents"]) >= MAX_INTENTS:
                    raise DataError("intent journal has 1000 entries; inspect it locally before a new attempt")
                records["intents"][intent] = {"fingerprint": fingerprint, "result": result,
                                             "metadata": (existing or {}).get("metadata", metadata or {})}
                if progress is not None:
                    records["intents"][intent]["metadata"].update(progress)
                payload = json.dumps(records, separators=(",", ":")).encode()
                if len(payload) > 1048576:
                    raise DataError("intent journal exceeds the size limit")
                out, temporary = tempfile.mkstemp(prefix=".dashboard-intent-", dir=self.folder)
                try:
                    with os.fdopen(out, "wb") as handle:
                        handle.write(payload)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.chmod(temporary, 0o600)
                    os.replace(temporary, self.path)
                    os.fsync(fd)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
                return None
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


class OwnerActions:
    def __init__(self, herdr, registry, env, clock):
        self.herdr, self.registry, self.env, self.clock = herdr, registry, dict(env), clock

    def context(self, fleet):
        # The browser's per-launch authority was verified by the handler; attribution
        # is explicit owner. Leaked CLI/plugin caller identities have no role here.
        env = {k: v for k, v in self.env.items() if not k.startswith("HERDR_PLUGIN_")
               and k not in ("HERDR_PANE_ID", "HERDR_WORKSPACE_ID", "CODEX_THREAD_ID", "CODEX_SESSION_ID")}
        ctx = Ctx(self.herdr, env, self.clock, io.StringIO(), io.StringIO(), fleet.repo, file=fleet.path)
        ctx._fleet = fleet
        return ctx

    def roots(self):
        with self.registry.lock:
            ids = list(self.registry.paths)
        roots = set()
        for ident in ids:
            try:
                roots.add(os.path.realpath(self.registry.fleet(ident).repo))
            except (DataError, OSError):
                pass
        return roots

    def validate_route(self, source, destination, target, agents):
        resolved = messages.resolve_targets(self.context(source), [target], agents)
        actual = resolved[0]
        actual_root = (os.path.realpath(actual.destination.repo) if actual.destination
                       else os.path.realpath(source.repo))
        roots = self.roots()
        if (actual.name != target or actual_root != os.path.realpath(destination.repo)
                or actual_root not in roots or (actual.other and os.path.realpath(actual.other) not in roots)):
            raise DataError("recipient routing changed or is outside the approved roster")

    def original(self, source, ident, target=None, allow_due=False, completed=False):
        original = inbox.resolve(source.repo, ident, mailbox="owner")
        if (original.to != "owner" or not inbox.NAME_RE.fullmatch(original.sender)
                or os.path.basename(original.path) != original.id + ".md"):
            raise DataError("only matching files in the selected owner mailbox can be read/replied")
        if target is not None and original.sender != target:
            raise DataError("reply recipient differs from the original sender")
        if completed and original.box != inbox.CUR:
            raise RequestState("unconfirmed", "selected read did not complete; original remains queued")
        if original.origin_fleet or original.destination_fleet:
            roots = self.roots()
            if any(not root or os.path.realpath(root) not in roots
                   for root in (original.origin_fleet, original.destination_fleet)):
                raise DataError("original receipt has a root outside the approved roster")
            crossfleet.counterpart(source.repo, original)
        if original.box == inbox.CUR:
            entries, _ = FileCache().log(messages.log_path(source.repo))
            receipts = [e for e in entries if e.get("id") == ident and e.get("to") == "owner"
                        and e.get("kind") in ("delivered", "expired", "dropped", "send")
                        and inbox.parse_time(e["time"]) is not None]
            if receipts and receipts[-1].get("kind") in ("expired", "dropped"):
                raise RequestState(receipts[-1]["kind"], "owner request is expired/dropped; inspect history")
            if (not receipts or receipts[-1].get("delivery") != "sent"
                    or (receipts[-1].get("kind") == "send" and receipts[-1].get("from") != original.sender)):
                raise RequestState("unconfirmed", "owner history lacks matching confirmed delivery evidence")
        elif not allow_due and inbox.is_due(original, inbox.now_utc(), source.fyi_ttl):
            raise RequestState("expired", "owner request expired; inspect it or explicitly read it")
        return original

    def perform(self, payload):
        if not isinstance(payload, dict):
            raise DataError("action must be a JSON object")
        operation = payload.get("action")
        allowed = ({"action", "intent", "source", "message"} if operation == "read" else
                   {"action", "intent", "source", "fleet", "seat", "view", "text", "reply_to", "read"})
        if operation not in ("read", "send") or set(payload) - allowed:
            raise DataError("only explicit owner read/send/reply actions are available")
        intent = payload.get("intent")
        if not isinstance(intent, str) or not INTENT_RE.fullmatch(intent):
            raise DataError("intent must be a unique 16–128 character id")
        if not isinstance(payload.get("source"), str):
            raise DataError("choose a registered source fleet")
        source = self.registry.fleet(payload["source"])
        fingerprint = digest(payload)
        journal = Journal(source.repo)
        previous = journal.lookup(intent, fingerprint)
        if previous is not None:
            return dict(previous, replayed=True)
        target = None
        message = payload.get("message") if operation == "read" else payload.get("reply_to")
        if message is not None and (not isinstance(message, str) or not inbox.ID_RE.fullmatch(message)):
            raise DataError("owner reading/reply needs an exact full message id")
        if operation == "read" and message is None:
            raise DataError("choose one owner message")
        if operation == "send":
            text = payload.get("text")
            if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > 32768:
                raise DataError("message must contain 1–32768 UTF-8 bytes")
            if "read" in payload and type(payload["read"]) is not bool:
                raise DataError("read must be an explicit boolean")
            if payload.get("read") and message is None:
                raise DataError("read-before-reply needs an original owner message")
            if not isinstance(payload.get("fleet"), str):
                raise DataError("choose a registered destination fleet")
            agents = self.herdr.agent_list()
            destination, seat, agent = self.registry.target(payload["fleet"], payload.get("seat"), payload.get("view"), agents)
            target = seat.name
            self.validate_route(source, destination, target, agents)
        if message:
            self.original(source, message, target, allow_due=operation == "read" or bool(payload.get("read")))
        # Persist only a hash of the canonical payload, never text, body or engine output.
        metadata = {"action": operation, "message": message, "source": payload["source"],
                    "fleet": payload.get("fleet"), "seat": target, "view": payload.get("view"),
                    "body_hash": hashlib.sha256(payload.get("text", "").encode("utf-8")).hexdigest()}
        previous = journal.update(intent, fingerprint, metadata=metadata)
        if previous is not None:
            return dict(previous, replayed=True)
        ctx = self.context(source)
        if operation == "send":
            original_binding = binding(agent)
            source_signature = digest(FileCache().bytes(source.path)[0].decode("utf-8"))
            adapter = self
            class GuardedOwner:
                def __getattr__(self, name):
                    return getattr(adapter.herdr, name)

                def agent_list(self):
                    current = adapter.herdr.agent_list()
                    fresh = adapter.registry.fleet(payload["source"])
                    if digest(FileCache().bytes(fresh.path)[0].decode("utf-8")) != source_signature:
                        raise HerdrError("target_changed", "source configuration changed; refresh")
                    dest, _, _ = adapter.registry.target(payload["fleet"], target, payload["view"], current)
                    adapter.validate_route(fresh, dest, target, current)
                    return current

                def agent_get(self, name):
                    current = adapter.herdr.agent_get(name)
                    if name in (target, agent.get("pane_id")) and binding(current) != original_binding:
                        raise HerdrError("target_changed", "target identity changed; refresh")
                    return current

                def agent_prompt(self, name, text):
                    self.agent_get(name)
                    return adapter.herdr.agent_prompt(name, text)
            ctx.herdr = GuardedOwner()
        result = {"status": "unconfirmed", "certainty": "unconfirmed", "code": 3}
        read_progress = {}
        outgoing = {}
        try:
            if operation == "read" or payload.get("read"):
                current = self.original(source, message, target, allow_due=True)
                was_new = current.box == inbox.NEW
                if current.box == inbox.NEW:
                    code = mailroom.op_inbox(ctx, as_name="owner", read_all=True, read_id=message, as_json=True)
                    if code not in (0, 3):
                        raise RequestState("unconfirmed", "selected reading failed; reply prerequisite is unconfirmed")
                self.original(source, message, target, completed=True)
                if operation == "read":
                    result = {"status": "read", "certainty": "confirmed", "code": 0, "id": message}
                read_progress = {"read_id": message, "read_status": "read" if was_new else "already_read"}
                journal.update(intent, fingerprint, progress=read_progress)
            if operation == "send":
                if message:
                    self.original(source, message, target, completed=bool(payload.get("read")))
                outcomes = []
                def capture(outcome):
                    outcomes.append(outcome)
                    outgoing.update(id=outcome["id"])
                    journal.update(intent, fingerprint, progress={"outgoing_id": outcome["id"],
                                   "outgoing_status": outcome["status"], "outgoing_certainty": outcome["certainty"]})
                code = messages.op_send(ctx, [target, payload["text"]], as_name="owner",
                                        reply_to=message, on_result=capture)
                if outcomes:
                    outcome = outcomes[0]
                    result = {k: outcome[k] for k in ("id", "recipient", "status", "certainty", "code")}
                    result.update(source=payload["source"], fleet=payload["fleet"])
                    result["code"] = code
                else:
                    result["code"] = code
        except RequestState as exc:
            result = {"status": exc.status, "certainty": "unconfirmed" if exc.status == "unconfirmed" else "confirmed",
                      "code": 3, "id": message}
        except Exception:
            # A reservation is never automatically reissued, even if its operation
            # or final journal replacement failed after submitting a prompt.
            result = {"status": "unconfirmed", "certainty": "unconfirmed", "code": 3,
                      "message": "Result uncertain; inspect receipts before a deliberate new attempt"}
            result.update(outgoing)
        result.update(read_progress)
        try:
            journal.update(intent, fingerprint, result)
        except Exception:
            return dict({k: result[k] for k in ("id", "read_id", "read_status") if k in result},
                        status="unconfirmed", certainty="unconfirmed", code=3,
                        message="Could not retain the receipt; this intent will not be automatically reissued")
        return result
