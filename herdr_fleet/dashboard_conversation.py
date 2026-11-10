"""Bounded owner/seat conversation joins. Read-only; never interprets message prose."""
from __future__ import annotations

import os
from typing import Dict

from herdr_fleet import inbox, messages, crossfleet
from herdr_fleet.dashboard_data import DataError, digest, MAX_MAIL

BODY_LIMIT = 32768
TOTAL_BODY = 262144


class Conversation:
    def __init__(self, registry, cache):
        self.registry, self.cache = registry, cache
        self.headers = {}

    def header(self, path, box):
        if os.path.realpath(path) != os.path.abspath(path):
            raise DataError("redirected conversation file")
        stat = os.stat(path)
        signature = (stat.st_ino, stat.st_mtime_ns, stat.st_size, box)
        previous = self.headers.get(path)
        if previous and previous[0] == signature:
            return previous[1]
        item = inbox.load_headers(path, box)
        if (not inbox.ID_RE.fullmatch(item.id) or os.path.basename(path) != item.id + ".md"
                or not isinstance(item.time, str) or inbox.parse_time(item.time) is None):
            raise DataError("invalid conversation identity")
        if len(self.headers) >= 512 and path not in self.headers:
            self.headers.pop(next(iter(self.headers)))
        self.headers[path] = (signature, item)
        return item

    def mailbox(self, root, name):
        found, incomplete, conflicts = {}, False, set()
        for box in (inbox.NEW, inbox.CUR):
            folder = inbox.box_dir(root, name, box)
            try:
                with os.scandir(folder) as entries:
                    for index, entry in enumerate(entries):
                        if index >= MAX_MAIL:
                            incomplete = True
                            break
                        if not entry.is_file(follow_symlinks=False) or not entry.name.endswith(".md"):
                            continue
                        try:
                            item = self.header(entry.path, box)
                            # A concurrent NEW->CUR rename is one immutable message.
                            # A differing duplicate belongs to this exact id's
                            # evidence, even when its recipient header changed.
                            if item.id in found and inbox.render_file(found[item.id]) != inbox.render_file(item):
                                incomplete = True
                                conflicts.add(item.id)
                                continue
                            found[item.id] = item
                        except FileNotFoundError:
                            continue  # CUR is read next; an unresolved race is shown by missing evidence.
                        except (OSError, ValueError):
                            incomplete = True
            except FileNotFoundError:
                pass
        return [item for item in found.values() if item.to == name], incomplete, conflicts

    @staticmethod
    def route(item, source, target):
        if item.origin_fleet or item.destination_fleet:
            if (item.origin_fleet != source or item.destination_fleet != target or source == target):
                raise DataError("conversation route differs from approved pair")
            crossfleet.counterpart(source, item)
        elif item.history_only:
            raise DataError("unpaired history copy")

    def body(self, item, budget):
        path, box = item.path, item.box
        if os.path.realpath(path) != os.path.abspath(path):
            return None, False, True
        try:
            data, truncated = self.cache.bytes(path, 16384 + BODY_LIMIT, tail=False)
        except FileNotFoundError:
            data, truncated = b"", False
        except DataError:
            # Header was filtered already. Read only a bounded prefix of this exact file.
            with open(path, "rb") as handle:
                data = handle.read(16384 + BODY_LIMIT)
            truncated = True
        if not data:
            other = inbox.CUR if box == inbox.NEW else inbox.NEW
            replacement = os.path.join(os.path.dirname(os.path.dirname(path)), other, item.id + ".md")
            try:
                check = self.header(replacement, other)
                if inbox.render_file(check) != inbox.render_file(item):
                    return None, False, True
                with open(replacement, "rb") as handle:
                    data = handle.read(16384 + BODY_LIMIT)
                truncated = len(data) >= 16384 + BODY_LIMIT
                path, box = replacement, other
            except (OSError, ValueError):
                return None, False, True
        # Validate the header and body from the SAME bounded read. A filtered
        # path/name alone cannot authenticate bytes replaced after that filter.
        actual = inbox.parse_file(data.decode("utf-8", "replace"), path, box)
        if (os.path.realpath(path) != os.path.abspath(path)
                or not crossfleet.receipt_matches(actual, item) or actual.history_only != item.history_only
                or (actual.timer, actual.scheduled_for) != (item.timer, item.scheduled_for)):
            return None, False, True
        raw = data.partition(b"\n\n")[2]
        size = min(BODY_LIMIT, budget[0])
        decoded = raw[:size].decode("utf-8", "replace").encode("utf-8")
        text = decoded[:size].decode("utf-8", "ignore")
        budget[0] -= len(text.encode("utf-8"))
        return text, truncated or len(raw) > size or len(decoded) > size, False

    @staticmethod
    def evidence(item, logs):
        receipts, outcomes, deliveries, incomplete = [], [], {}, False
        holding = item.path
        if holding:
            for _ in range(5):
                holding = os.path.dirname(holding)
            holding = os.path.realpath(holding)
        else:
            holding = next(iter(logs))
        for root, entries in logs.items():
            for index, entry in enumerate(entries):
                if entry.get("id") != item.id:
                    continue
                kind, when = entry.get("kind"), inbox.parse_time(entry.get("time"))
                if when is None:
                    continue
                key = (when, index)
                if kind in ("send", "delivered", "expired", "dropped"):
                    if item.origin_fleet:
                        valid = all(entry.get(k) == getattr(item, k)
                                    for k in ("origin_fleet", "destination_fleet", "requested_to"))
                    else:
                        valid = root == holding and not entry.get("origin_fleet") and not entry.get("destination_fleet")
                    if not valid:
                        incomplete = True
                        continue
                if kind == "send":
                    if (entry.get("from"), entry.get("to")) != (item.sender, item.to):
                        incomplete = True
                        continue
                    delivery = entry.get("delivery")
                    status = "delivered" if delivery == "sent" else delivery
                elif (kind in ("delivered", "expired", "dropped") and entry.get("to") == item.to
                      and entry.get("from") == messages.PLUGIN_SENDER):
                    if item.origin_fleet and any(entry.get(k) != getattr(item, k)
                                                 for k in ("origin_fleet", "destination_fleet", "requested_to")):
                        incomplete = True
                        continue
                    status = ("unconfirmed" if entry.get("delivery") == "unconfirmed" else "delivered") if kind == "delivered" else kind
                else:
                    continue
                if status not in ("queued", "delivered", "unconfirmed", "refused", "expired", "dropped"):
                    continue
                outcomes.append((when, status))
                if status in ("delivered", "unconfirmed"):
                    deliveries.setdefault(root, []).append(key)
                receipts.append({"kind": status, "time": entry["time"], "by": item.to,
                                 "via": str(entry.get("via") or "")[:32], "exact": True})
        terminal = [entry for entry in outcomes if entry[1] != "queued"]
        priority = {"queued": 0, "delivered": 1, "refused": 2, "expired": 3, "dropped": 3, "unconfirmed": 4}
        status = max(terminal or outcomes, key=lambda e: (e[0], priority[e[1]]))[1] if outcomes else "unconfirmed"
        if not outcomes:
            incomplete = True
        return status, receipts, deliveries, incomplete

    def read(self, source_id, target_id, name, limit=50):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise DataError("conversation limit must be between 1 and 100")
        source, target = self.registry.fleet(source_id), self.registry.fleet(target_id)
        if not isinstance(name, str) or target.seat(name) is None:
            raise DataError("conversation needs a declared canonical seat")
        own_root, peer_root = os.path.realpath(source.repo), os.path.realpath(target.repo)
        logs, incomplete = {}, False
        for root in dict.fromkeys((own_root, peer_root)):
            logs[root], capped = self.cache.log(messages.log_path(root))
            incomplete = incomplete or capped
        outgoing, part, conflicts = self.mailbox(own_root, name)
        incomplete = incomplete or part
        incoming, part, incoming_conflicts = self.mailbox(own_root, "owner")
        incomplete = incomplete or part
        conflicts.update(incoming_conflicts)
        selected, originals = {}, {}
        for item in outgoing:
            if item.sender != "owner" or item.to != name:
                continue
            try:
                if own_root != peer_root and not item.origin_fleet:
                    # A local historical file cannot identify a different root by name alone.
                    raise DataError("outgoing route is missing")
                self.route(item, own_root, peer_root)
                selected[item.id] = item
                if item.id not in conflicts:
                    originals[item.id] = item
            except (OSError, ValueError):
                incomplete = True
        with self.registry.lock:
            registered = list(self.registry.paths)
        membership = 0
        for ident in registered:
            try:
                membership += self.registry.fleet(ident).seat(name) is not None
            except (OSError, ValueError):
                continue
        for item in incoming:
            if item.sender != name or item.to != "owner":
                continue
            if item.reply_to and item.reply_to not in originals:
                continue
            if not item.reply_to and membership > 1:
                incomplete = True
                continue
            if own_root != peer_root and not item.origin_fleet and item.reply_to not in originals:
                incomplete = True
                continue
            try:
                self.route(item, peer_root, own_root)
                selected[item.id] = item
            except (OSError, ValueError):
                incomplete = True
        missing_ids = set()
        for entry in logs[own_root]:
            ident = entry.get("id")
            pair = (entry.get("from"), entry.get("to"))
            if (entry.get("kind") != "send" or not isinstance(ident, str) or not inbox.ID_RE.fullmatch(ident)
                    or ident in selected or pair not in (("owner", name), (name, "owner"))):
                continue
            if pair == (name, "owner") and (entry.get("reply_to") and entry["reply_to"] not in originals or membership > 1):
                continue
            if pair == (name, "owner") and own_root != peer_root and entry.get("reply_to") not in originals:
                incomplete = True
                continue
            if own_root != peer_root and pair == ("owner", name) and (
                    entry.get("origin_fleet"), entry.get("destination_fleet")) != (own_root, peer_root):
                continue
            item = inbox.Message(ident, pair[0], pair[1], entry["time"], "", fyi=bool(entry.get("fyi")),
                                 reply_to=entry.get("reply_to"))
            selected[ident] = item
            missing_ids.add(ident)
        valid_reply_refs = set()
        # Compatibility: old agent replies stayed in target's owner mailbox.
        # Only a validated exact mirrored reference selects a body there.
        for entry in logs[own_root]:
            if (entry.get("kind") != "ack" or entry.get("via") != "reply" or entry.get("id") not in originals
                    or (entry.get("from"), entry.get("to")) != (name, "owner")):
                continue
            ident = entry.get("reply_id")
            if (entry.get("reply_fleet") != peer_root or not isinstance(ident, str)
                    or not inbox.ID_RE.fullmatch(ident)):
                incomplete = True
                continue
            candidates = []
            for box in (inbox.NEW, inbox.CUR):
                try:
                    item = self.header(os.path.join(inbox.box_dir(peer_root, "owner", box), ident + ".md"), box)
                    if (item.sender == name and item.to == "owner" and item.reply_to == entry["id"]
                            and not item.origin_fleet and not item.history_only):
                        candidates.append(item)
                except (OSError, ValueError):
                    pass
            if len(candidates) == 1:
                selected[ident] = candidates[0]
                valid_reply_refs.add((entry["id"], ident))
            else:
                incomplete = True
        all_items = sorted(selected.values(), key=inbox.Message.sort_key)
        truncated = len(all_items) > limit
        visible = all_items[-limit:]
        rows, budget = [], [TOTAL_BODY]
        owed = {d.get("id") for d in messages.owed(logs[own_root])
                if d.get("from") == name and d.get("to") == "owner"}
        for item in visible:
            status, receipts, deliveries, part = self.evidence(item, logs)
            if item.sender == "owner":
                for root, entries in logs.items():
                    for index, entry in enumerate(entries):
                        if (entry.get("kind") != "ack" or (entry.get("from"), entry.get("to")) != (name, "owner")
                                or (entry.get("id") is not None and entry["id"] != item.id)):
                            continue
                        if entry.get("via") == "reply" and (item.id, entry.get("reply_id")) not in valid_reply_refs:
                            continue
                        when = inbox.parse_time(entry.get("time"))
                        if when is None or (not entry.get("id") and
                                            not any((when, index) > key for key in deliveries.get(root, []))):
                            continue
                        if own_root != peer_root and item.id not in originals:
                            continue  # Missing/invalid paired route is not exact acknowledgement authority.
                        if not entry.get("id") and item.fyi:
                            continue
                        receipts.append({"kind": "ack", "time": entry["time"], "by": str(entry.get("by") or name)[:64],
                                         "via": str(entry.get("via") or "")[:32], "exact": bool(entry.get("id"))})
            try:
                if item.id in missing_ids or item.id in conflicts:
                    raise DataError("body missing or identity conflicts in stored history")
                body, body_truncated, missing = self.body(item, budget)
            except (OSError, ValueError):
                body, body_truncated, missing = None, False, True
            unique = {digest(r): r for r in receipts}
            row = {"id": item.id, "from": item.sender, "to": item.to, "time": item.time,
                   "body": body, "body_available": body is not None, "body_truncated": body_truncated,
                   "fyi": item.fyi, "reply_to": None if item.id in conflicts else item.reply_to,
                   "reply_ids": [m.id for m in all_items if m.reply_to == item.id and m.id not in conflicts],
                   "owed": item.sender == name and item.id in owed,
                   "status": status if item.sender == "owner" else "received",
                   "certainty": "unconfirmed" if status == "unconfirmed" or part or missing else "confirmed",
                   "incomplete": bool(part or missing),
                   "receipts": sorted(unique.values(), key=lambda r: r["time"])}
            rows.append(row)
        associations, journal_incomplete = self.intents(source_id, target_id, name, source.repo, selected)
        result = {"source": source_id, "fleet": target_id, "seat": name, "messages": rows,
                  "intents": associations, "truncated": truncated or any(r["body_truncated"] for r in rows),
                  "incomplete": journal_incomplete or incomplete or any(r["incomplete"] for r in rows)}
        result["revision"] = digest(result)
        return result

    def intents(self, source, target, seat, repo, selected):
        from herdr_fleet.dashboard_actions import Journal
        try:
            raw, _ = self.cache.bytes(Journal(repo).path, 1048576)
            if not raw and os.path.exists(Journal(repo).path):
                return [], True
            entries = Journal.records(raw)["intents"] if raw else {}
        except (OSError, ValueError):
            return [], True
        found = []
        for intent, entry in entries.items():
            metadata, result = entry["metadata"], entry["result"] or {}
            if (metadata.get("action"), metadata.get("source"), metadata.get("fleet"), metadata.get("seat")) != ("send", source, target, seat):
                continue
            ident = metadata.get("outgoing_id") or result.get("id")
            item = selected.get(ident)
            if item is None or item.sender != "owner" or item.to != seat:
                continue
            if item.dashboard_chat is not None:
                from herdr_fleet.dashboard_provenance import validated
                provenance = validated(item)
                if provenance is None or provenance["intent"] != intent:
                    continue
            found.append({"intent": intent, "id": ident, "status": result.get("status", "unconfirmed"),
                          "certainty": result.get("certainty", "unconfirmed"), "completed": entry["result"] is not None})
        return found[-100:], False
