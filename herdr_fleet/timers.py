"""Daily request scheduling, recovery and inspection (design 30).

The journal contains metadata only. The timer lock precedes every mailbox lock.
Scheduling never interacts with Herdr: the existing watcher alone submits mail.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, replace
from typing import Dict, Iterator, List, Optional, Sequence

from herdr_fleet.timer_calendar import CALENDAR, UTC, iso, valid_zone

STATE = "timers.state.json"
LOCK = "timers.lock"
VERSION = 1
KEYS = {"name", "to", "file", "at", "tz", "starts_at", "until", "grace", "enabled"}
AT = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")


@dataclass
class Timer:
    name: str
    to: str
    file: str
    at: str
    tz: str
    starts_at: dt.datetime
    until: dt.datetime
    grace: int = 21600
    enabled: bool = True


def aware(value: object) -> Optional[dt.datetime]:
    if not isinstance(value, str):
        return None
    try:
        result = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(UTC) if result.utcoffset() is not None else None
    except (ValueError, OverflowError):
        return None


def prompt(path: str) -> str:
    with open(path, "r", encoding="utf-8", newline="") as handle:
        text = handle.read()
    if not text.strip():
        raise ValueError("empty prompt file")
    return text


def parse_timers(raw: object, repo: str, seats: Sequence[str], errors: List[str], warnings: List[str]) -> List[Timer]:
    from herdr_fleet.fleetfile import NAME_RE, parse_duration, resolve_path, _unknown_keys
    if not isinstance(raw, list) or not all(isinstance(r, dict) for r in raw):
        errors.append("timers: must be an array of tables ([[timers]])")
        return []
    result = []
    names = set()
    for index, table in enumerate(raw):
        where = "timers[%d]" % index
        _unknown_keys(table, KEYS, where, warnings)
        before = len(errors)
        name = table.get("name")
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            errors.append(where + ".name: must match [a-z][a-z0-9_-]{0,31}")
        elif name in names:
            errors.append(where + ".name: duplicate timer name " + name)
        else:
            names.add(name)
        to = table.get("to")
        if to not in seats:
            errors.append(where + ".to: must name a declared local seat")
        raw_file = table.get("file")
        path = ""
        if not isinstance(raw_file, str) or not raw_file.strip():
            errors.append(where + ".file: required nonempty UTF-8 prompt file")
        else:
            path = resolve_path(raw_file, repo)
            try:
                prompt(path)
            except (OSError, ValueError, UnicodeError):
                errors.append(where + ".file: must be a readable nonempty UTF-8 prompt file")
        at = table.get("at")
        if not isinstance(at, str) or not AT.fullmatch(at):
            errors.append(where + ".at: must be HH:MM (00:00 through 23:59)")
        zone = table.get("tz")
        if not valid_zone(zone):
            errors.append(where + ".tz: must name an available IANA timezone")
        start, end = aware(table.get("starts_at")), aware(table.get("until"))
        if start is None:
            errors.append(where + ".starts_at: required offset-aware ISO timestamp")
        if end is None:
            errors.append(where + ".until: required offset-aware ISO timestamp")
        if start is not None and end is not None and end <= start:
            errors.append(where + ".until: must be after starts_at")
        grace = parse_duration(table.get("grace", "6h"))
        if grace is None or grace > 86400:
            errors.append(where + ".grace: positive m/h/d duration, at most 24h")
        enabled = table.get("enabled", True)
        if not isinstance(enabled, bool):
            errors.append(where + ".enabled: must be boolean")
        if len(errors) == before:
            result.append(Timer(name, to, path, at, zone, start, end, grace, enabled))
    return result


def state_path(repo: str) -> str:
    return os.path.join(repo, ".herdr", STATE)


def fail(text: str):
    from herdr_fleet.context import OpError
    raise OpError("timer journal: " + text + "; preserved, scheduling stopped")


def load(repo: str) -> Dict:
    path = state_path(repo)
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError:
        return {"version": VERSION, "timers": {}}
    except (ValueError, OSError, RecursionError) as exc:
        fail("unreadable or corrupt %s (%s)" % (STATE, exc))
    if (not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != VERSION
            or not isinstance(value.get("timers"), dict)):
        fail("incompatible " + STATE)
    if set(value) - {"version", "timers"}:
        fail("unknown journal fields")
    from herdr_fleet.fleetfile import NAME_RE
    from herdr_fleet.inbox import ID_RE
    ids = set()
    for name, history in value["timers"].items():
        if not NAME_RE.fullmatch(name) or not isinstance(history, dict) or not isinstance(history.get("occurrences"), dict):
            fail("invalid timer history")
        if set(history) - {"occurrences", "through", "last", "stopped"}:
            fail("unknown timer history fields")
        through = history.get("through")
        if through is not None and aware(through) is None:
            fail("invalid occurrence watermark")
        if history.get("last") is not None and not isinstance(history["last"], dict):
            fail("invalid last decision")
        if history.get("last") is not None:
            last = history["last"]
            if (set(last) - {"outcome", "at", "scheduled_for", "reason", "event_id"} or not isinstance(last.get("outcome"), str)
                    or aware(last.get("at")) is None
                    or (last.get("scheduled_for") is not None and aware(last["scheduled_for"]) is None)):
                fail("invalid last decision metadata")
        if history.get("stopped") is not None and aware(history["stopped"]) is None:
            fail("invalid stopped transition")
        for scheduled, record in history["occurrences"].items():
            if (aware(scheduled) is None or not isinstance(record, dict) or record.get("scheduled_for") != scheduled
                    or not isinstance(record.get("id"), str) or not ID_RE.fullmatch(record["id"])
                    or not isinstance(record.get("to"), str) or not NAME_RE.fullmatch(record["to"])
                    or aware(record.get("deadline")) is None
                    or record.get("state") not in ("prepared", "enqueued", "settled", "expired", "skipped", "superseded")):
                fail("invalid prepared occurrence")
            if set(record) - {"id", "to", "scheduled_for", "deadline", "state", "prepared_at", "body_bytes", "retire", "send_after"}:
                fail("unknown occurrence fields")
            if aware(record.get("prepared_at")) is None or aware(record["deadline"]) <= aware(scheduled):
                fail("invalid occurrence times")
            if type(record.get("body_bytes", 0)) is not int or record.get("body_bytes", 0) < 0:
                fail("invalid occurrence size")
            if type(record.get("send_after", 0)) is not int or record.get("send_after", 0) < 0:
                fail("invalid enqueue ordering")
            if through is None or aware(through) < aware(scheduled):
                fail("occurrence exceeds journal watermark")
            if record["id"] in ids:
                fail("message id reused across occurrences")
            ids.add(record["id"])
            retire = record.get("retire")
            if retire is not None and (not isinstance(retire, dict) or not isinstance(retire.get("reason"), str)
                                       or retire.get("outcome") not in ("expired", "superseded")):
                fail("invalid retirement intent")
    return value


def save(repo: str, state: Dict) -> None:
    folder = os.path.dirname(state_path(repo))
    fd, tmp = tempfile.mkstemp(prefix=".timers.", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, state_path(repo))
        directory = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@contextlib.contextmanager
def locked(repo: str) -> Iterator[None]:
    from herdr_fleet.state import ensure_gitignore
    folder = os.path.join(repo, ".herdr")
    os.makedirs(folder, exist_ok=True)
    ensure_gitignore(folder)
    fd = os.open(os.path.join(folder, LOCK), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def upcoming(timer: Timer, now: dt.datetime, count: int) -> List[Dict]:
    return [item for item in CALENDAR.window(timer.tz, timer.at, max(now, timer.starts_at))
            if item["utc"] and timer.starts_at <= aware(item["utc"]) < timer.until
            and aware(item["utc"]) > now][:count]


class Scheduler:
    """One lock scope plans, durably prepares, reconciles mail and records decisions."""
    def __init__(self, ctx, now: dt.datetime):
        from herdr_fleet import messages
        self.ctx, self.repo, self.now = ctx, ctx.fleet.repo, now
        self.state = load(self.repo)
        entries = messages.read(self.repo)
        self.events = {str(e["timer_event"]): {"time": e["time"], "order": index}
                       for index, e in enumerate(entries) if e.get("timer_event")}
        self.order = len(entries)
        self.lines: List[str] = []

    def persist(self) -> None:
        save(self.repo, self.state)

    def remember(self, token: str, entry: Dict) -> None:
        self.order += 1
        self.events[token] = {"time": entry["time"], "order": self.order}

    def last(self, name: str, token: str, outcome: str, scheduled: Optional[str], reason: str = "") -> None:
        """Rebuild a logged decision with its original timestamp and relative order."""
        history = self.state["timers"][name]
        previous = history.get("last") or {}
        old_order = self.events.get(previous.get("event_id"), {}).get("order", -1)
        event = self.events[token]
        if event["order"] >= old_order:
            if aware(event["time"]) is None:
                fail("invalid timer event timestamp")
            history["last"] = {"outcome": outcome, "at": event["time"], "scheduled_for": scheduled,
                               "reason": reason, "event_id": token}

    def event(self, name: str, outcome: str, occurrence: Optional[Dict] = None, reason: str = "", **extra) -> None:
        from herdr_fleet import messages
        scheduled = (occurrence or {}).get("scheduled_for") or extra.get("scheduled_for")
        token = hashlib.sha256(json.dumps([name, scheduled, outcome, reason, extra], sort_keys=True).encode()).hexdigest()
        if token not in self.events:
            entry = {"kind": "timer", "from": "herdr-fleet", "to": (occurrence or {}).get("to", "owner"),
                     "time": iso(self.now), "timer": name, "scheduled_for": scheduled,
                     "outcome": outcome, "reason": reason, "timer_event": token}
            if occurrence and occurrence.get("id"):
                entry["id"] = occurrence["id"]
            entry.update(extra)
            messages.append(self.repo, entry)
            self.remember(token, entry)
        self.last(name, token, outcome, scheduled, reason)

    def matching(self, name: str, record: Dict):
        from herdr_fleet import inbox
        found = []
        # Include other mailboxes for collision detection, not just the current target.
        for mailbox in inbox.mailboxes(self.repo):
            for box in (inbox.NEW, inbox.CUR):
                path = os.path.join(inbox.box_dir(self.repo, mailbox, box), record["id"] + ".md")
                if not os.path.lexists(path):
                    continue
                try:
                    message = inbox.load_headers(path, box)
                except (OSError, ValueError):
                    fail("unreadable message metadata for timer " + name)
                if (message.id != record["id"] or message.to != record["to"] or message.timer != name
                        or message.scheduled_for != record["scheduled_for"] or message.sender != "owner"
                        or message.fyi or message.mode != "queue" or message.ttl != record["deadline"]):
                    fail("message id collision for timer " + name)
                found.append(message)
        if len(found) > 1:
            fail("duplicate message id for timer " + name)
        return found[0] if found else None

    def send_log(self, message, record: Dict) -> None:
        from herdr_fleet import messages
        token = "timer-send:" + message.id
        if token not in self.events:
            # Recovery reads headers only, never a mailbox payload for identity or
            # audit. Timer send summaries identify the occurrence, not body text.
            entry = messages.send_entry(replace(message, body=""), "queued", message.file)
            entry["time"] = message.time  # recovery is not a new owner request/answer
            if "send_after" in record:
                entry["timer_enqueue_order"] = record["send_after"]
            entry["bytes"] += record.get("body_bytes", 0)
            entry["first_line"] = "timer %s scheduled %s" % (message.timer, message.scheduled_for)
            entry["timer_event"] = token
            messages.append(self.repo, entry)
            self.remember(token, entry)

    def retire(self, name: str, record: Dict, reason: str, outcome: str = "expired") -> None:
        from herdr_fleet import inbox, messages
        with inbox.locked(self.repo, record["to"]):
            message = self.matching(name, record)
            if message is not None:
                self.send_log(message, record)
                if message.box == inbox.NEW or record.get("retire"):
                    record["retire"] = {"reason": reason, "outcome": outcome}
                    self.persist()
                if message.box == inbox.NEW:
                    inbox.move(self.repo, message, inbox.CUR)
                if record.get("retire"):
                    entry = messages.disposal_entry("expired", message, reason)
                    token = "timer-expired:" + message.id
                    if token not in self.events:
                        entry["timer_event"] = token
                        messages.append(self.repo, entry)
                        self.remember(token, entry)
            record["state"] = outcome
            self.event(name, outcome, record, reason)
            self.persist()

    def enqueue(self, timer: Timer, record: Dict) -> None:
        from herdr_fleet import inbox
        with inbox.locked(self.repo, record["to"]):
            message = self.matching(timer.name, record)
            if message is None:
                if record["state"] != "prepared":
                    return  # terminal history never recreates deleted/moved/delivered mail
                if "timer-send:" + record["id"] in self.events:
                    record["state"] = "settled"  # a logged file was already created; never recreate it
                    self.persist()
                    return
                self.now = inbox.now_utc()  # mailbox lock acquisition may have waited past the deadline
                if self.now >= aware(record["deadline"]):
                    record["state"] = "skipped"
                    self.event(timer.name, "skipped", record, "prepared occurrence expired")
                    self.persist()
                    return
                body = prompt(timer.file)
                self.now = inbox.now_utc()
                if self.now >= aware(record["deadline"]):
                    record["state"] = "skipped"
                    self.event(timer.name, "skipped", record, "prepared occurrence expired")
                    self.persist()
                    return
                record["body_bytes"] = inbox.size_of(body)
                from herdr_fleet import messages
                record["send_after"] = len(messages.read(self.repo))
                self.persist()
                message = inbox.Message(id=record["id"], sender="owner", to=record["to"], time=iso(self.now),
                                        body=body, fyi=False, ttl=record["deadline"], file=timer.file,
                                        queued_by="queue", timer=timer.name, scheduled_for=record["scheduled_for"])
                inbox.write(self.repo, message)
            self.send_log(message, record)
            record["state"] = "enqueued"
            self.last(timer.name, "timer-send:" + record["id"], "enqueued", record["scheduled_for"])
            self.persist()
            self.lines.append("timer %s enqueued #%s" % (timer.name, message.short))

    def run(self) -> List[str]:
        from herdr_fleet import inbox
        timers = {t.name: t for t in self.ctx.fleet.timers}
        if timers:
            CALENDAR.retain((t.tz, t.at) for t in timers.values())
        # History is retained on removal. Reconciliation needs no calendar for removed timers.
        for name, history in self.state["timers"].items():
            timer = timers.get(name)
            for record in history["occurrences"].values():
                if record["state"] not in ("prepared", "enqueued"):
                    continue
                if record.get("retire"):
                    self.retire(name, record, record["retire"]["reason"], record["retire"]["outcome"])
                    continue
                with inbox.locked(self.repo, record["to"]):
                    message = self.matching(name, record)
                    if message is not None and message.box == inbox.CUR:
                        self.send_log(message, record)
                        record["state"] = "settled"
                        self.persist()
                        continue
                reason = ("removed" if timer is None else "disabled" if not timer.enabled else
                          "target changed" if timer.to != record["to"] else
                          "cutoff" if self.now >= timer.until else
                          "ttl" if self.now >= aware(record["deadline"]) else None)
                if reason:
                    self.retire(name, record, reason)
        errors = []
        for timer in timers.values():
            history = self.state["timers"].setdefault(timer.name, {"occurrences": {}, "through": None})
            try:
                self.plan(timer, history)
            except (OSError, ValueError, UnicodeError) as exc:
                failed = history["occurrences"].get(history.get("through"))
                self.event(timer.name, "error", failed if failed and failed["state"] in ("prepared", "enqueued") else None,
                           reason="cannot read or enqueue occurrence")
                self.persist()
                errors.append("timer %s error: %s" % (timer.name, exc))
        if errors:
            from herdr_fleet.context import OpError
            raise OpError("; ".join(errors))
        return self.lines

    def plan(self, timer: Timer, history: Dict) -> None:
        from herdr_fleet import inbox
        if not timer.enabled:
            return
        if self.now >= timer.until:
            end = iso(timer.until)
            if history.get("stopped") == end:
                return
            self.event(timer.name, "stopped", reason="cutoff", until=end)
            history["stopped"] = end
            self.persist()
            return
        if self.now < timer.starts_at:
            return
        # Recover existing preparations before considering any new date. A later due
        # occurrence may supersede an old preparation, but never generate a backlog.
        rows = CALENDAR.window(timer.tz, timer.at, self.now)
        through = aware(history.get("through"))
        decided = {aware(key) for key in history["occurrences"]}
        due = [r for r in rows if r["utc"] and timer.starts_at <= aware(r["utc"]) < timer.until
               and aware(r["utc"]) <= self.now and aware(r["utc"]) not in decided
               and (through is None or aware(r["utc"]) > through)]
        latest = due[-1] if due else None
        # Gap records are dated decisions and are deduplicated in the decision log.
        for row in rows:
            if row["utc"] is None:
                gap_at = aware(row.get("gap_at"))
                if gap_at and timer.starts_at <= gap_at < timer.until and gap_at <= self.now:
                    self.event(timer.name, "skipped", reason="daylight-saving gap", local_date=row["date"],
                               timezone=timer.tz, wall_time=timer.at)
        if latest is not None:
            scheduled = aware(latest["utc"])
            lower = history.get("through") or iso(timer.starts_at)
            if scheduled > timer.starts_at and (through is None or scheduled - through > dt.timedelta(days=1)):
                self.event(timer.name, "skipped", reason="older missed interval", after=lower, before=latest["utc"])
            deadline = min(scheduled + dt.timedelta(seconds=timer.grace), timer.until)
            if self.now >= deadline:
                history["through"] = latest["utc"]
                self.event(timer.name, "skipped", reason="outside grace", scheduled_for=latest["utc"])
                self.persist()
                return
            for record in history["occurrences"].values():
                if record["state"] in ("prepared", "enqueued"):
                    self.retire(timer.name, record, "newer occurrence", "superseded")
            taken = inbox.taken_shorts(self.repo)
            for item in self.state["timers"].values():
                taken.update(inbox.short_of(r["id"]) for r in item["occurrences"].values())
            record = {"id": inbox.new_id(scheduled, taken), "to": timer.to, "scheduled_for": latest["utc"],
                      "deadline": iso(deadline), "state": "prepared", "prepared_at": iso(self.now)}
            history["occurrences"][latest["utc"]] = record
            history["through"] = latest["utc"]
            self.persist()  # chosen id is durable before either file or send log exists
            self.enqueue(timer, record)
        else:
            for record in history["occurrences"].values():
                if record["state"] == "prepared":
                    self.enqueue(timer, record)


@contextlib.contextmanager
def cycle(ctx) -> Iterator[List[str]]:
    """Optional timer lock scope; watch --once retains it through its delivery pass."""
    from herdr_fleet import inbox
    if not ctx.fleet.timers and not os.path.exists(state_path(ctx.fleet.repo)):
        yield []
        return
    with locked(ctx.fleet.repo):
        scheduler = Scheduler(ctx, inbox.now_utc())
        yield scheduler.run()


def waiting_lines(items, states: Dict[str, str], typing: Sequence[str] = ()) -> List[str]:
    return ["timer %s waiting: %s" % (message.timer, "typing" if message.to in typing else
                                     states.get(message.to, "absent"))
            for message in items if message.timer]


def inspect(ctx, names: Sequence[str], count: int, dry_run: bool, as_json: bool) -> int:
    from herdr_fleet import inbox, messages
    from herdr_fleet.context import UsageError
    from herdr_fleet.herdr import HerdrError
    if not 1 <= count <= 20:
        raise UsageError("--next must be between 1 and 20")
    configured = {t.name: t for t in ctx.fleet.timers}
    if configured:
        CALENDAR.retain((t.tz, t.at) for t in configured.values())
    unknown = set(names) - set(configured)
    if unknown:
        raise UsageError("unknown timers: " + ", ".join(sorted(unknown)))
    state = load(ctx.fleet.repo)
    now = inbox.now_utc()
    agents = None
    if not dry_run and ctx.inside_herdr():
        try:
            agents = ctx.herdr.agent_list()
        except HerdrError:
            pass
    logs = messages.read(ctx.fleet.repo)
    result = []
    for timer in ctx.fleet.timers:
        if names and timer.name not in names:
            continue
        history = state["timers"].get(timer.name, {})
        pending = None
        status = "disabled" if not timer.enabled else "stopped" if now >= timer.until else "enabled"
        occurrences = history.get("occurrences", {})
        latest = history.get("last")
        for record in occurrences.values():
            for message in inbox.read_box(ctx.fleet.repo, record["to"], inbox.NEW):
                if message.id == record["id"] and message.timer == timer.name:
                    pending = {"id": message.id, "deadline": message.ttl}
                    live = next((a for a in (agents or []) if a.get("name") == timer.to), None)
                    status = "waiting"
                    pending["state"] = (str(live.get("agent_status") or "unknown") if live else
                                        "absent" if agents is not None else "unknown")
        if occurrences:
            last = max(occurrences.values(), key=lambda r: r["scheduled_for"])
            progress = [(i, e) for i, e in enumerate(logs) if e.get("id") == last["id"] and
                        e.get("kind") in ("send", "delivered", "expired", "dropped")]
            if progress:
                dispositions = [(i, e) for i, e in progress if e["kind"] != "send"]
                index, entry = dispositions[-1] if dispositions else progress[-1]
                decision = next(((i, e) for i, e in enumerate(logs)
                                 if latest and e.get("timer_event") == latest.get("event_id")), None)
                order = entry.get("timer_enqueue_order") if entry["kind"] == "send" else None
                progress_order = order * 2 - 1 if type(order) is int else index * 2
                replace_last = latest is None or (latest.get("outcome") in ("enqueued", "settled")
                    and latest.get("scheduled_for") == last["scheduled_for"])
                if not replace_last:
                    replace_last = (progress_order > decision[0] * 2 if decision else
                                    (aware(entry["time"]) is not None and aware(latest.get("at")) is not None
                                     and aware(entry["time"]) > aware(latest["at"])))
                if replace_last:
                    latest = {"scheduled_for": last["scheduled_for"], "at": entry["time"],
                              "outcome": ("unconfirmed" if entry.get("delivery") == "unconfirmed" else
                                          "enqueued" if entry["kind"] == "send" else entry["kind"])}
        if latest and latest.get("outcome") == "error" and timer.enabled and now < timer.until:
            status = "error"
        try:
            future = upcoming(timer, now, count) if timer.enabled and now < timer.until else []
        except (ValueError, OSError, KeyError) as exc:
            from herdr_fleet.context import OpError
            raise OpError("timer %s calendar error: %s" % (timer.name, exc))
        result.append({"name": timer.name, "to": timer.to, "tz": timer.tz, "enabled": timer.enabled,
                       "state": status, "next": future[0] if future else None, "upcoming": future,
                       "last": latest, "pending": pending})
    if as_json:
        ctx.say(json.dumps({"timers": result}, ensure_ascii=False))
    elif not result:
        ctx.say("no timers configured")
    else:
        for row in result:
            next_time = row["next"]["utc"] if row["next"] else "-"
            ctx.say("%s -> %s [%s] %s; next %s" % (row["name"], row["to"], row["tz"], row["state"], next_time))
            for item in row["upcoming"]:
                ctx.say("  %s (%s)" % (item["utc"], item["local"]))
            if row["pending"]:
                ctx.say("  pending %(id)s until %(deadline)s (%(state)s)" % row["pending"])
            if row["last"]:
                ctx.say("  last " + json.dumps(row["last"], sort_keys=True))
    return 0
