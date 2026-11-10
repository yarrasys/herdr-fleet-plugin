"""Authored synthetic sample run, isolated from every live service/adapter.

Everything here is fixed, invented content: three sample fleets and one scripted run that
plays on the first of them. The run is a list of timed steps; the state at any moment is
derived from the script clock alone, so no request mutates anything but two timestamps
(``replay`` and ``reply``). No Herdr call, no file, no fleet, no send.
"""
from __future__ import annotations
import base64
import datetime
import threading
import time

STAGES = ["Brief", "Review", "Decision", "Fix", "Verify", "Report"]
TEAM, DOCS, ARCHIVE = "demo-team", "demo-docs", "demo-archive"
ORCH, DEV, REV, DES = "sample-orchestrator", "sample-dev", "sample-reviewer", "sample-designer"
HOLD_AT = 13.6          # script seconds: the run waits here until the owner's local sample reply
M_BRIEF, M_ASK, M_REPLY = "20261101T100003Z-b71ef1", "20261101T100012Z-0a41c7", "20261101T100014Z-d0e5a1"
M_FIX, M_FIXED = "20261101T100018Z-c3d2e0", "20261101T100027Z-4b8f1a"
M_VERDICT, M_REPORT = "20261101T100033Z-d90a77", "20261101T100037Z-f2e6b4"
GREEN, RED, CYAN, DIM, OFF = "\x1b[32m", "\x1b[31m", "\x1b[36m", "\x1b[2m", "\x1b[0m"

BODIES = {
    M_BRIEF: (ORCH, REV, False, "Review the parser branch against the spec",
              "Synthetic brief: review the sample parser branch against the spec and report merge or fix."),
    M_ASK: (REV, "owner", False, "Merge the parser branch now, or fix the two flaky tests first?",
            "Synthetic question: two sample tests fail now and then; it looks like test ordering, not the parser. "
            "Merge now and fix after, or fix first?"),
    M_REPLY: ("owner", REV, False, "Synthetic owner reply (your text stays in this browser)",
              "Synthetic owner reply. The text typed in the Demo page is never sent to the server."),
    M_FIX: (REV, DEV, False, "Fix the two order tests before the merge",
            "Synthetic request: fix the two order tests before the merge, then report."),
    M_FIXED: (DEV, REV, False, "Order tests fixed; the sample suite passes",
              "Synthetic report: both order tests are fixed and the sample suite passes."),
    M_VERDICT: (REV, ORCH, False, "Merge: the two order tests are fixed",
                "Synthetic verdict: merge."),
    M_REPORT: (ORCH, "owner", True, "Parser branch merged after the test fix",
               "Synthetic report from the seat. It is the seat's own statement, not a checked result."),
}


def _send(ident, delivery="sent"):
    sender, to = BODIES[ident][0], BODIES[ident][1]
    return {"id": ident, "from": sender, "to": to, "kind": "send", "delivery": delivery}


def _delivered(ident):
    sender, to = BODIES[ident][0], BODIES[ident][1]
    return {"id": ident, "from": sender, "to": to, "kind": "delivered", "delivery": "delivered"}


# Each step: script time, then any of: seats {name: (state, doing, waiting)}, event, lines {name: [text]},
# done (stage index, evidence id), ask / answered / report (owner mailbox changes).
SCRIPT = [
    (0.8, {"seats": {ORCH: ("working", False)},
           "lines": {ORCH: ["> ask sample-reviewer to review the parser branch", "", "Writing the brief for sample-reviewer."]}}),
    (3.2, {"event": _send(M_BRIEF), "lines": {ORCH: [DIM + "herdr-fleet send sample-reviewer --file brief.md" + OFF]}}),
    (4.4, {"event": _delivered(M_BRIEF), "done": (0, M_BRIEF),
           "seats": {ORCH: ("working", True), REV: ("working", False)},
           "lines": {ORCH: [DIM + "  sent to sample-reviewer (idle) #b71ef1" + OFF],
                     REV: [CYAN + "[from sample-orchestrator #b71ef1] Review the parser branch against the spec" + OFF]}}),
    (5.6, {"lines": {REV: ["Reading parser/tokens.py"]}}),
    (6.8, {"lines": {REV: ["Reading tests/test_order.py"]}}),
    (8.0, {"lines": {REV: ["Running the sample tests on the older Python"]}}),
    (9.0, {"lines": {REV: [RED + "  test_order_a ... FAIL (intermittent)" + OFF]}}),
    (9.8, {"lines": {REV: [RED + "  test_order_b ... FAIL (intermittent)" + OFF]}}),
    (11.0, {"lines": {REV: ["Two tests fail now and then. It looks like test ordering, not the parser."]}}),
    (12.4, {"event": _send(M_ASK, "queued"), "lines": {REV: [DIM + "herdr-fleet send owner --file question.md" + OFF]}}),
    (HOLD_AT, {"ask": True, "done": (1, M_ASK), "seats": {REV: ("idle", False)},
               "lines": {REV: [DIM + "  queued for owner #0a41c7: owner gets a notification" + OFF]}}),
    # ---- everything below needs the owner's local sample reply
    (13.8, {"event": _send(M_REPLY), "answered": True}),
    (15.0, {"event": _delivered(M_REPLY), "done": (2, M_REPLY), "seats": {REV: ("working", False)},
            "lines": {REV: [CYAN + "[from owner #d0e5a1] Synthetic owner reply (your text stays in this browser)" + OFF]}}),
    (16.6, {"lines": {REV: ["The owner has decided. Asking sample-dev to fix the two tests first."]}}),
    (17.8, {"event": _send(M_FIX), "lines": {REV: [DIM + "herdr-fleet send sample-dev --file fix.md" + OFF]}}),
    (19.0, {"event": _delivered(M_FIX), "seats": {REV: ("working", True), DEV: ("working", False)},
            "lines": {DEV: [CYAN + "[from sample-reviewer #c3d2e0] Fix the two order tests before the merge" + OFF]}}),
    (20.2, {"lines": {DEV: ["Editing tests/test_order.py"]}}),
    (21.4, {"lines": {DEV: ["Running the two order tests"]}}),
    (22.4, {"lines": {DEV: [GREEN + "  test_order_a ... ok" + OFF]}}),
    (23.1, {"lines": {DEV: [GREEN + "  test_order_b ... ok" + OFF]}}),
    (24.2, {"lines": {DEV: ["Running the whole sample suite"]}}),
    (25.6, {"lines": {DEV: [GREEN + "  no failures (authored sample output)" + OFF]}}),
    (26.6, {"event": _send(M_FIXED), "lines": {DEV: [DIM + "herdr-fleet send sample-reviewer --file fixed.md" + OFF]}}),
    (27.8, {"event": _delivered(M_FIXED), "done": (3, M_FIXED), "seats": {DEV: ("idle", False), REV: ("working", False)},
            "lines": {REV: [CYAN + "[from sample-dev #4b8f1a] Order tests fixed; the sample suite passes" + OFF]}}),
    (29.0, {"lines": {REV: ["Running the two tests ten times over"]}}),
    (30.6, {"lines": {REV: [GREEN + "  no failures in ten runs (authored sample output)" + OFF]}}),
    (31.6, {"lines": {REV: ["Verdict: merge."]}}),
    (32.6, {"event": _send(M_VERDICT), "lines": {REV: [DIM + "herdr-fleet send sample-orchestrator --file verdict.md" + OFF]}}),
    (33.8, {"event": _delivered(M_VERDICT), "done": (4, M_VERDICT), "seats": {REV: ("idle", False), ORCH: ("working", False)},
            "lines": {ORCH: [CYAN + "[from sample-reviewer #d90a77] Merge: the two order tests are fixed" + OFF]}}),
    (35.0, {"lines": {ORCH: ["git merge parser   (authored sample: nothing is run)"]}}),
    (36.2, {"lines": {ORCH: ["Merged. Reporting to the owner."]}}),
    (37.2, {"event": _send(M_REPORT, "queued"), "lines": {ORCH: [DIM + "herdr-fleet send owner --fyi --file report.md" + OFF]}}),
    (38.4, {"report": True, "done": (5, M_REPORT), "seats": {ORCH: ("idle", False)}}),
]
END_AT = SCRIPT[-1][0]

START_LINES = {
    ORCH: ["sample-dev reports that the parser tests are pushed.", ""],
    DEV: ["Parser tests written and pushed to the sample branch.", ""],
    REV: ["Ready for the next review.", ""],
    DES: ["Settings page layout written to docs/design/settings.md", ""],
    "peer-lead": ["> outline the next sample chapter", "", "Writing notes/outline.md"],
    "peer-writer": ["Chapter draft is final. Moving it into chapters/.", "",
                    "  Bash command", "  git mv drafts/sample.md chapters/sample.md", "",
                    "  Do you want to proceed?", "\x1b[7m  > 1. Yes\x1b[0m", "    2. No", "",
                    DIM + "(authored native dialog: it can only be answered in Herdr, never here)" + OFF],
    "peer-checker": ["Dates in the sample chapter checked against the notes file.", ""],
    "spare-reader": ["Section summary written to notes/section.md", ""],
}
KINDS = {ORCH: "claude", DEV: "codex", REV: "claude", DES: "kimi", "peer-lead": "claude", "peer-writer": "claude",
         "peer-checker": "codex", "spare-reader": "claude", "spare-indexer": "codex"}


def _iso(stamp):
    return datetime.datetime.fromtimestamp(stamp, datetime.timezone.utc).isoformat()


class Demo:
    def __init__(self):
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._t0 = time.monotonic()
        self._wall0 = time.time()
        self._released = None       # monotonic time of the owner's local reply, once given
        self._runs = 0

    # ---- the only two things a request can change -------------------------------------------
    @property
    def started(self):
        return self._t0

    @started.setter
    def started(self, value):       # the current server's replay branch assigns this
        self.replay()

    def replay(self):
        with self._lock:
            self._t0, self._wall0, self._released, self._runs = time.monotonic(), time.time(), None, self._runs + 1

    def reply(self):
        with self._lock:
            now = time.monotonic()
            if self._released is None and now - self._t0 >= HOLD_AT:
                self._released = now

    # ---- derived state ----------------------------------------------------------------------
    def _clock(self):
        """(script seconds, holding, wall time of a script moment, run number)."""
        with self._lock:
            now, t0, wall0, released, run = time.monotonic(), self._t0, self._wall0, self._released, self._runs
        elapsed = now - t0
        if elapsed < HOLD_AT:
            script, holding = elapsed, False
        elif released is None:
            script, holding = HOLD_AT, True
        else:
            script, holding = min(END_AT + 1, HOLD_AT + (now - released)), False
        gap = 0.0 if released is None else (released - t0) - HOLD_AT
        def wall(moment):
            return wall0 + moment + (gap if moment > HOLD_AT else 0.0)
        return script, holding, wall, run

    def _state(self):
        script, holding, wall, run = self._clock()
        seats = {name: ["idle", False] for name in (ORCH, DEV, REV, DES)}
        lines = {name: list(START_LINES[name]) for name in (ORCH, DEV, REV, DES)}
        events, evidence, asked, reported, steps = [], [None] * len(STAGES), False, False, 0
        for moment, step in SCRIPT:
            if moment > script:
                break
            steps += 1
            for name, (state, waiting) in step.get("seats", {}).items():
                seats[name] = [state, waiting]
            for name, more in step.get("lines", {}).items():
                lines[name].extend(more)
            if "event" in step:
                events.append(dict(step["event"], time=_iso(wall(moment))))
            if "done" in step:
                evidence[step["done"][0]] = step["done"][1]
            asked = (asked or bool(step.get("ask"))) and not step.get("answered")
            reported = reported or bool(step.get("report"))
        return {"script": script, "holding": holding, "wall": wall, "run": run, "seats": seats, "lines": lines,
                "events": events, "evidence": evidence, "asked": asked, "reported": reported, "steps": steps}

    def _owner_item(self, ident, now_wall, box="new"):
        sender, to, fyi = BODIES[ident][0], BODIES[ident][1], BODIES[ident][2]
        return {"id": ident, "sender": sender, "to": to, "time": _iso(now_wall), "fyi": fyi, "box": box,
                "mode": "fyi" if fyi else "refusal"}

    def _seat(self, name, state, waiting=False, queued=0, fyi=0, limited=None, absent=False):
        item = {"name": name, "kind": KINDS[name], "state": "absent" if absent else state,
                "view": None if absent else "demo-view-" + name, "queued": queued, "fyi": fyi,
                "limited": limited, "context": None}
        if waiting:
            item["waiting"] = True
        return item

    def snapshot(self):
        s = self._state()
        now = time.time()
        owner = []
        if s["asked"]:
            owner.append(self._owner_item(M_ASK, s["wall"](HOLD_AT)))
        if s["reported"]:
            owner.append(self._owner_item(M_REPORT, s["wall"](END_AT)))
        done = sum(1 for e in s["evidence"] if e)
        team = {"id": TEAM, "label": "Sample team", "observed_at": _iso(now), "stale": False, "warning": None,
                "watcher": {"running": True}, "contacts": [], "owed": [], "truncated": False, "owner": owner,
                "events": s["events"][-100:],
                "seats": [self._seat(ORCH, *s["seats"][ORCH]), self._seat(DEV, *s["seats"][DEV]),
                          self._seat(REV, *s["seats"][REV]), self._seat(DES, "idle", fyi=1)]}
        docs = {"id": DOCS, "label": "Sample docs", "observed_at": _iso(now), "stale": False, "warning": None,
                "watcher": {"running": True}, "contacts": [], "owed": [], "truncated": False, "owner": [], "events": [],
                "seats": [self._seat("peer-lead", "working"), self._seat("peer-writer", "blocked", queued=1),
                          self._seat("peer-checker", "idle",
                                     limited={"kind": "claude", "at": _iso(now - 1200), "resets_at": None, "released": False})]}
        archive = {"id": ARCHIVE, "label": "Sample archive", "observed_at": _iso(now - 41 * 60), "stale": True,
                   "warning": "Sample archive: the last observation is 41 minutes old (authored stale example).",
                   "watcher": {"running": False}, "contacts": [], "owed": [], "truncated": False, "owner": [],
                   "events": [], "seats": [self._seat("spare-reader", "idle"), self._seat("spare-indexer", "absent", absent=True)]}
        return {"demo": True, "revision": s["run"] * 1000 + s["steps"], "stage": done, "stages": list(STAGES),
                "stage_evidence": s["evidence"], "hold": s["holding"], "fleets": [team, docs, archive]}

    def listing(self, fleet, mode, name=None, limit=50, ref=None):
        if fleet not in (TEAM, DOCS, ARCHIVE):
            raise ValueError("unknown Demo fleet")
        limit = max(1, min(200, int(limit)))
        if mode == "watch-log":
            return {"items": ["Synthetic demo only. No live watcher is connected."], "truncated": False}
        s = self._state()
        if mode == "log":
            if fleet != TEAM:
                return {"items": [{"id": "20261101T090000Z-5a11e0", "kind": "send", "from": "peer-lead",
                                   "to": "peer-writer", "time": "2026-11-01T09:00:00+00:00", "delivery": "queued",
                                   "first_line": "Synthetic entry for a static sample fleet"}] if fleet == DOCS else [],
                        "truncated": False}
            items = [dict(e, first_line=BODIES[e["id"]][3], fyi=BODIES[e["id"]][2]) for e in s["events"]]
            return {"items": items[-limit:], "truncated": len(items) > limit}
        if fleet != TEAM:
            return {"items": [], "truncated": False}
        name = name or "owner"
        items = []
        for event in s["events"]:
            ident = event["id"]
            if event["kind"] != "send" or BODIES[ident][1] != name or (ref and ident != ref):
                continue
            landed = any(e["kind"] == "delivered" and e["id"] == ident for e in s["events"])
            answered = ident == M_ASK and not s["asked"] and s["script"] > HOLD_AT
            items.append({"id": ident, "sender": BODIES[ident][0], "to": name, "time": event["time"],
                          "box": "cur" if landed or answered else "new", "fyi": BODIES[ident][2],
                          "mode": "fyi" if BODIES[ident][2] else "refusal", "body": BODIES[ident][4],
                          "body_truncated": False})
        return {"items": items[-limit:], "truncated": len(items) > limit}

    def conversation(self, source, fleet, seat, limit=50):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid Demo conversation limit")
        fleets = {f["id"]: f for f in self.snapshot()["fleets"]}
        if (source not in fleets or fleet not in fleets
                or not any(s["name"] == seat for s in fleets[fleet]["seats"])):
            raise ValueError("unknown Demo conversation")
        state = self._state()
        rows = []
        for event in state["events"] if source == TEAM and fleet == TEAM else []:
            if event["kind"] != "send" or (event["from"], event["to"]) not in (("owner", seat), (seat, "owner")):
                continue
            ident = event["id"]
            landed = next((e for e in state["events"] if e["id"] == ident and e["kind"] == "delivered"), None)
            receipts = [{"kind": "queued", "time": event["time"], "by": event["to"], "exact": True}]
            if landed:
                receipts.append({"kind": "delivered", "time": landed["time"], "by": event["to"], "exact": True})
            rows.append({"id": ident, "from": event["from"], "to": event["to"], "time": event["time"],
                         "body": BODIES[ident][4], "body_available": True, "body_truncated": False,
                         "reply_to": M_ASK if ident == M_REPLY else None, "fyi": BODIES[ident][2],
                         "status": "local" if ident == M_REPLY else "received", "certainty": "confirmed",
                         "receipts": receipts, "incomplete": False})
        return {"source": source, "fleet": fleet, "seat": seat, "messages": rows[-limit:], "intents": [],
                "revision": state["run"] * 1000 + state["steps"], "incomplete": False, "truncated": len(rows) > limit}

    def _screen(self, fleet_id, name):
        if fleet_id == TEAM:
            s = self._state()
            return (s["run"], len(s["lines"][name])), s["lines"][name]
        return (0, 0), START_LINES[name]

    def frames(self, fleet_id, name, view, cols, rows, disconnected):
        fleets = {f["id"]: f for f in self.snapshot()["fleets"]}
        fleet = fleets.get(fleet_id)
        if fleet is None or not any(s["name"] == name and s["view"] and s["view"] == view for s in fleet["seats"]):
            raise ValueError("unknown Demo seat")
        seq, last = 0, None
        while not self._closed.is_set():
            key, lines = self._screen(fleet_id, name)
            if key != last:
                seq, last = seq + 1, key
                body = "\x1b[2J\x1b[H" + CYAN + "SYNTHETIC DEMO · WATCH ONLY · " + name + OFF + "\r\n\r\n"
                body += "\r\n".join(lines[-max(1, rows - 3):])
                yield {"type": "frame", "full": True, "seq": seq,
                       "bytes": base64.b64encode(body.encode("utf-8")).decode("ascii"), "cols": cols, "rows": rows}
            if disconnected():
                return
            self._closed.wait(0.25)

    def close(self):
        self._closed.set()
