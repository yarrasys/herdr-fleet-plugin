"""Bounded metadata-only attention derived from existing records; never effects."""
from __future__ import annotations

import os

from herdr_fleet import inbox, messages, crossfleet, limits
from herdr_fleet.dashboard_data import digest

TYPES = ("owner_message", "native_dialog", "send_issue", "provider_hold", "observation_issue")
PER_FLEET = 512
RESPONSE_CAP = 200


def item(kind, fleet, identity, status, reason, when=None, **fields):
    result = {"id": digest([kind, fleet, identity])[:32], "type": kind, "fleet": fleet,
              "status": status, "reason": reason, "time": when if inbox.parse_time(when) else None,
              "actions": []}
    result.update(fields)
    return result


class Attention:
    def __init__(self, registry, reader):
        self.registry, self.reader = registry, reader
        self.rows, self.incomplete_fleets, self.capped_fleets = [], set(), set()

    def safe(self, message, root, roots):
        if message.origin_fleet or message.destination_fleet:
            if (not isinstance(message.origin_fleet, str) or not isinstance(message.destination_fleet, str)
                    or message.origin_fleet not in roots or message.destination_fleet not in roots
                    or root not in (message.origin_fleet, message.destination_fleet)):
                return False
            crossfleet.counterpart(root, message)
        elif message.history_only:
            return False
        return True

    def exact(self, root, name, ident):
        found = None
        for box in (inbox.NEW, inbox.CUR):
            try:
                value = self.reader.header(os.path.join(inbox.box_dir(root, name, box), ident + ".md"), box)
            except FileNotFoundError:
                continue
            if value.to != name or (found and not crossfleet.receipt_matches(found, value)):
                raise ValueError("conflicting attention identity")
            found = value
        return found

    def owners(self, ident, source, roots, now):
        fleet, entries = source["fleet"], source["entries"]
        root = os.path.realpath(fleet.repo)
        headers, incomplete, conflicts = self.reader.mailbox(root, "owner")
        by_id = {m.id: m for m in headers}
        requests = {r.get("id") or digest([r["from"], r["since"]]): r
                    for _, r in messages.unsettled(entries) if r["to"] == "owner"}
        pending = {m.id for m in headers if m.box == inbox.NEW and not m.fyi
                   and not inbox.is_due(m, now, fleet.fyi_ttl)}
        rows = []
        for ident_ in sorted(set(requests) | pending)[:PER_FLEET]:
            request, header = requests.get(ident_), by_id.get(ident_)
            valid = header is not None and ident_ not in conflicts
            try:
                valid = valid and self.safe(header, root, roots) and inbox.NAME_RE.fullmatch(header.sender) is not None
            except (OSError, ValueError):
                valid = False
            if valid and header.history_only and header.origin_fleet == root:
                continue  # The validated authoritative destination owns this owner request.
            if request and header and (request["from"], request["to"]) != (header.sender, header.to):
                valid = False
            status = "unread" if ident_ in pending else "awaiting_reply"
            sender = header.sender if valid else (request or {}).get("from")
            when = header.time if valid else (request or {}).get("since")
            row = item("owner_message", ident, ident_, status if valid else "unavailable",
                       "owner_pending" if valid and status == "unread" else
                       "owner_awaiting_reply" if valid else "owner_header_unavailable", when,
                       sender=sender if isinstance(sender, str) and inbox.NAME_RE.fullmatch(sender) else None,
                       incomplete=not valid or bool(request and source.get("incomplete")))
            if inbox.ID_RE.fullmatch(ident_):
                row["message"] = ident_
            if valid:
                row["actions"] = [{"kind": "inspect_owner", "source": ident, "message": ident_}]
            rows.append(row)
        return rows, incomplete or bool(conflicts), len(set(requests) | pending) > PER_FLEET

    def sends(self, ident, source, sources, roots):
        from herdr_fleet.dashboard_actions import Journal
        root, groups, incomplete = os.path.realpath(source["fleet"].repo), {}, False
        root_ids = {os.path.realpath(s["fleet"].repo): i for i, s in sources.items()}
        for entry in source["entries"]:
            if (entry.get("kind") == "send" and entry.get("from") == "owner"
                    and isinstance(entry.get("id"), str) and inbox.ID_RE.fullmatch(entry["id"])
                    and isinstance(entry.get("to"), str) and inbox.NAME_RE.fullmatch(entry["to"])):
                # A correctly mirrored routed record belongs only to its origin.
                if isinstance(entry.get("origin_fleet"), str) and entry["origin_fleet"] in roots and entry["origin_fleet"] != root:
                    try:
                        mirror = self.exact(root, entry["to"], entry["id"])
                        if (mirror and self.safe(mirror, root, roots) and mirror.sender == "owner"
                                and all(entry.get(k) == getattr(mirror, k) for k in
                                        ("origin_fleet", "destination_fleet", "requested_to"))):
                            continue
                    except (OSError, ValueError):
                        pass
                    incomplete = True
                groups.setdefault((entry["id"], entry["to"]), {"entry": entry, "journal": None})
        try:
            raw, _ = self.reader.cache.bytes(Journal(root).path, 1048576)
            if not raw and os.path.exists(Journal(root).path):
                raise ValueError("empty journal")
            journal = Journal.records(raw)["intents"] if raw else {}
            for intent, record in journal.items():
                metadata, result = record["metadata"], record["result"] or {}
                if metadata.get("action") != "send":
                    continue
                target = metadata.get("seat")
                if (metadata.get("source") != ident or not isinstance(target, str)
                        or not inbox.NAME_RE.fullmatch(target)):
                    incomplete = True
                    continue
                full = metadata.get("outgoing_id") or result.get("id")
                if not isinstance(full, str) or not inbox.ID_RE.fullmatch(full):
                    full = None
                group = groups.setdefault((full or "intent:" + intent, target), {"entry": None, "journal": None})
                if group["journal"] is not None:
                    group["conflict"] = True
                group["journal"] = (intent, metadata, result)
        except (OSError, ValueError):
            incomplete = True
        rows = []
        for (full, target), group in sorted(groups.items())[:PER_FLEET]:
            entry, retained = group["entry"], group["journal"]
            result = retained[2] if retained else {}
            status, part, header, destination = "unconfirmed", False, None, None
            intent = retained[0] if retained else None
            when = (entry or {}).get("time")
            try:
                if not inbox.ID_RE.fullmatch(full):
                    raise ValueError("no message id yet")
                header = self.exact(root, target, full)
                if header is None or header.sender != "owner" or not self.safe(header, root, roots):
                    raise ValueError("missing approved owner header")
                if header.origin_fleet and header.origin_fleet != root:
                    raise ValueError("unvalidated mirror is not an origin route")
                destination = root_ids.get(header.destination_fleet or root)
                if destination is None or sources[destination]["fleet"].seat(target) is None:
                    raise ValueError("target not in approved destination")
                if retained:
                    metadata = retained[1]
                    if (metadata.get("fleet") != destination or result.get("id", full) != full
                            or group.get("conflict")):
                        raise ValueError("journal association differs")
                    if header.dashboard_chat is not None:
                        from herdr_fleet.dashboard_provenance import valid_shape
                        if (not valid_shape(header.dashboard_chat) or header.dashboard_chat["intent"] != intent
                                or header.dashboard_chat["source_file"] != os.path.realpath(source["fleet"].path)):
                            raise ValueError("journal provenance differs")
                logs = {r: sources[root_ids[r]]["entries"] for r in dict.fromkeys((root, header.destination_fleet or root))}
                status, receipts, _, part = self.reader.evidence(header, logs)
                when = header.time
                if not receipts and retained:
                    status = result.get("status", "unconfirmed")
                    part = result.get("certainty") != "confirmed"
                if result.get("status") in ("expired", "dropped", "refused") and status in ("delivered", "queued"):
                    part = True
            except (OSError, ValueError):
                part, destination = True, None
                if result.get("status") in ("refused", "expired", "dropped"):
                    status = result["status"]
                elif entry and entry.get("delivery") in ("refused", "expired", "dropped"):
                    status = entry["delivery"]
                elif (entry and entry.get("delivery") in ("sent", "queued")
                      and not retained and header is None):
                    continue  # Missing unrelated positive/queued history is not a recorded send issue.
            recorded_issue = (status in ("unconfirmed", "refused", "expired", "dropped")
                              or (part and (retained or (entry or {}).get("delivery") == "unconfirmed")))
            if not recorded_issue:
                continue
            recorded = status if status in ("refused", "expired", "dropped") else "unconfirmed"
            row = item("send_issue", ident, [full, target], recorded, "send_" + recorded, when,
                       seat=target, incomplete=part, actions=[{"kind": "inspect_log", "fleet": ident}])
            if inbox.ID_RE.fullmatch(full):
                row["message"] = full
            if intent:
                row["intent"] = intent
            if destination is not None:
                row["actions"] = [{"kind": "inspect_send", "source": ident, "fleet": destination,
                                   "seat": target, "message": full}]
            rows.append(row)
        return rows, incomplete, len(groups) > PER_FLEET

    @staticmethod
    def observed(fleet, source, now):
        rows, ident = [], fleet["id"]
        for seat in fleet.get("seats", []):
            seen = seat.get("state_seen") or {}
            since = seen.get("since") if seen.get("state") == seat["state"] else None
            if seat["state"] == "blocked" and seat.get("view") and not fleet.get("stale"):
                rows.append(item("native_dialog", ident, seat["name"], "blocked", "native_dialog_in_herdr", since,
                                 seat=seat["name"], actions=[{"kind": "watch", "fleet": ident,
                                                           "seat": seat["name"], "view": seat["view"]}]))
            entry = ((source or {}).get("state", {}).get("agents", {}).get(seat["name"]) or {})
            try:
                records = limits.records(entry)
            except (TypeError, ValueError):
                records = []
                rows.append(item("observation_issue", ident, [seat["name"], "limit_metadata"], "incomplete",
                                 "source_metadata_incomplete", fleet.get("observed_at"), incomplete=True,
                                 actions=[{"kind": "refresh"}]))
            for record in records:
                reset = record.get("resets_at")
                valid = (inbox.parse_time(record.get("at")) is not None
                         and (reset is None or inbox.parse_time(reset) is not None)
                         and ("released" not in record or type(record["released"]) is bool))
                if (not fleet.get("stale") and seat.get("view") and valid
                        and limits.holds(record, seat["kind"], now)):
                    rows.append(item("provider_hold", ident, [seat["name"], record["kind"], record["at"]],
                                     "held", "active_provider_hold", record["at"], seat=seat["name"],
                                     resets_at=reset, actions=[{"kind": "inspect_seat", "fleet": ident,
                                                             "seat": seat["name"]}]))
                    break
        if fleet.get("stale"):
            rows.append(item("observation_issue", ident, "fleet", "stale", "fleet_observation_stale",
                             fleet.get("observed_at"), actions=[{"kind": "refresh"}]))
        running = fleet.get("watcher", {}).get("running")
        if running is not True and fleet.get("watcher", {}).get("enabled") is not False:
            rows.append(item("observation_issue", ident, "watcher", "stopped" if running is False else "unknown",
                             "watcher_stopped" if running is False else "watcher_liveness_unknown",
                             fleet.get("observed_at"), actions=[{"kind": "refresh"}]))
        if source and source.get("incomplete"):
            rows.append(item("observation_issue", ident, "metadata", "incomplete", "source_metadata_incomplete",
                             fleet.get("observed_at"), incomplete=True, actions=[{"kind": "refresh"}]))
        return rows

    def project(self, fleets, sources, now):
        roots = {os.path.realpath(s["fleet"].repo) for s in sources.values()}
        rows, incomplete_fleets, capped_fleets = [], set(), set()
        for fleet in fleets:
            source = sources.get(fleet["id"])
            rows += self.observed(fleet, source, now)
            if fleet.get("stale"):
                incomplete_fleets.add(fleet["id"])
            if source is None:
                incomplete_fleets.add(fleet["id"])
                capped_fleets.add(fleet["id"])
                continue
            try:
                found, part, capped = self.owners(fleet["id"], source, roots, now)
                if fleet.get("stale"):
                    for row in found:
                        row["incomplete"] = True
                rows += found
                issues, bad_journal, send_capped = self.sends(fleet["id"], source, sources, roots)
                rows += issues
                part, capped = part or bad_journal, capped or send_capped
                if bad_journal:
                    rows.append(item("observation_issue", fleet["id"], "journal", "incomplete", "source_metadata_incomplete",
                                     fleet.get("observed_at"), incomplete=True, actions=[{"kind": "refresh"}]))
                if part or source.get("incomplete", False):
                    incomplete_fleets.add(fleet["id"])
                if capped or source.get("incomplete"):
                    capped_fleets.add(fleet["id"])
            except (OSError, ValueError):
                incomplete_fleets.add(fleet["id"])
        def order(row):
            when = inbox.parse_time(row["time"])
            return (TYPES.index(row["type"]), when.timestamp() if when else float("inf"), row["id"])
        rows.sort(key=order)
        self.rows, self.incomplete_fleets, self.capped_fleets = rows, incomplete_fleets, capped_fleets
        return self.page()

    def page(self, scope="all", fleet=None, category="all"):
        with self.registry.lock:
            registered = list(self.registry.paths)
        return filtered(self.rows, registered, scope, fleet, category, self.incomplete_fleets, self.capped_fleets)


def filtered(rows, registered, scope="all", fleet=None, category="all", incomplete_fleets=(), capped_fleets=()):
    from herdr_fleet.dashboard_data import DataError
    if scope not in ("all", "selected") or category not in ("all",) + TYPES:
        raise DataError("Unknown attention scope or category")
    if fleet is not None and fleet not in registered:
        raise DataError("Attention fleet is not registered")
    if scope == "selected" and fleet is None:
        raise DataError("Selected attention scope needs a registered fleet")
    relevant = set(registered) if scope == "all" else {fleet}
    scoped = [r for r in rows if r["fleet"] in relevant]
    rows = [r for r in scoped if category == "all" or r["type"] == category]
    truncated = bool(relevant & set(capped_fleets))
    incomplete = bool(relevant & set(incomplete_fleets)) or any(r.get("incomplete") for r in scoped)
    counts = {kind: 0 for kind in TYPES}
    fleet_counts = {ident: {kind: 0 for kind in TYPES} for ident in relevant}
    for row in scoped:
        counts[row["type"]] += 1
        fleet_counts[row["fleet"]][row["type"]] += 1
    return {"items": rows[:RESPONSE_CAP], "counts": counts, "counts_capped": truncated,
            "fleet_counts": fleet_counts, "matched_count": len(rows),
            "truncated": truncated or len(rows) > RESPONSE_CAP, "incomplete": incomplete or truncated}
