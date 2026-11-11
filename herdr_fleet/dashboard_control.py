"""Explicit authenticated human leases. No CLI, timer or agent entry point.

The creator worker alone owns the opaque permit and native writer. HTTP handlers
carry bounded jobs; output/probe threads cannot borrow input authority. Nothing
here logs, journals or persists input, output, authentication or lease secrets.
"""
from __future__ import annotations

import copy
import json
import os
import queue
import re
import secrets
import stat
import threading
import time

from herdr_fleet import build_identity, permits
from herdr_fleet.dashboard_data import DataError, binding
from herdr_fleet.dashboard_terminal import Frames
from herdr_fleet.herdr import HerdrError
from herdr_fleet.process_identity import identity
from herdr_fleet.seats import find_agent_process

DISCLOSURES = [
    "Control targets this terminal. It may become a shell after the agent exits; agent binding is not atomic.",
    "Control changes real terminal sizing. Already accepted work or bytes cannot be recalled.",
    "The hold covers participating plugin versions. Native, older and unrelated writers remain outside it.",
    "Taking control may start/resume the saved agent process if Herdr restores this terminal between validation and attachment, even before any input.",
    "A failed acquisition or Release does not undo a resume already performed.",
]
TAB = re.compile(r"[A-Za-z0-9_-]{16,96}\Z")
SECRET = re.compile(r"[A-Za-z0-9_-]{24,96}\Z")
MAX_LEASES, PREPARE_S, SETUP_S, UNATTACHED_S = 4, 30, 5, 5
HEARTBEAT_S, ABSOLUTE_S, PROBE_S = 15, 600, 2


class ControlError(DataError):
    """Fixed, body-free failure safe for the owner response."""


def watcher_eligible(fleet):
    if not fleet.watch:
        return
    path = os.path.join(fleet.repo, ".herdr", "watch.lock")
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError:
        raise ControlError("Watcher metadata is unreadable; Control unavailable")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            actual = os.fstat(fd)
            if (not stat.S_ISREG(actual.st_mode) or actual.st_ino != before.st_ino
                    or actual.st_uid != os.geteuid() or actual.st_mode & 0o022 or actual.st_size > 16384):
                raise ValueError("invalid watcher metadata")
            value = json.loads(os.read(fd, 16385))
            if not isinstance(value, dict): raise ValueError("invalid watcher metadata")
        finally: os.close(fd)
    except (OSError, ValueError):
        raise ControlError("Watcher metadata is unreadable or ambiguous; Control unavailable")
    pid = value.get("pid")
    if type(pid) is not int or pid <= 0:
        raise ControlError("Watcher identity is unknown; Control unavailable")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    except OSError:
        raise ControlError("Watcher liveness is unknown; Control unavailable")
    launch = build_identity.validated(value.get("launch"))
    recorded = value.get("process_identity")
    if (type(value.get("control_gate_protocol")) is not int or value["control_gate_protocol"] != 1
            or not launch["version"] or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", launch["version"])
            or not launch["commit"] or not isinstance(recorded, str)
            or not re.fullmatch(r"[0-9a-f]{64}", recorded) or identity(pid) != recorded
            or os.path.realpath(str(value.get("fleet", ""))) != os.path.realpath(fleet.path)):
        raise ControlError("Enabled watcher participation is unverified; Control unavailable")


class Probe:
    def __init__(self, manager, selection):
        self.done, self.result = threading.Event(), None
        if not manager.probe_slots.acquire(False):
            raise ControlError("Control identity-check capacity reached")
        def run():
            try:
                self.result = manager.snapshot(selection)
            except Exception:
                self.result = None
            finally:
                manager.probe_slots.release()
                self.done.set()
        try:
            threading.Thread(target=run, name="control-read-only-probe", daemon=True).start()
        except RuntimeError:
            manager.probe_slots.release()
            raise ControlError("Control identity-check capacity unavailable")

    def wait(self, stop):
        end = time.monotonic() + 2.5
        while not self.done.wait(0.02):
            if stop.is_set() or time.monotonic() >= end:
                raise ControlError("Control identity unavailable; no input enabled")
        if stop.is_set() or self.result is None:
            raise ControlError("Control target/runtime/watcher unavailable or changed")
        return self.result


class Controls:
    def __init__(self, herdr, registry, authority):
        self.herdr, self.registry, self.authority = herdr, registry, authority
        self.lock = threading.RLock()
        self.pending, self.used, self.leases, self.retired = {}, {}, {}, {}
        self.probe_slots = threading.BoundedSemaphore(8)
        self.closed = False
        self.now = time.monotonic

    def authorized(self, session):
        with self.authority.lock:
            entry = self.authority.sessions.get(session)
            return entry is not None and entry[1] > time.monotonic() and not self.closed

    def snapshot(self, selection):
        native = self.herdr.control_client()
        fleet, seat, agent = self.registry.target(selection["fleet"], selection["seat"], selection["view"], native.agent_list())
        watcher_eligible(fleet)
        if agent.get("launch_pending", False) is not False:
            raise ControlError("Pending or unresolved native runtime; Control unavailable")
        values = native.agent_resources(agent)
        pane = native.pane_get(agent["pane_id"])
        if any(pane.get(k) != agent.get(k) for k in ("pane_id", "terminal_id", "workspace_id")):
            raise ControlError("Native target changed")
        info = native.process_info(agent["pane_id"])
        processes = info.get("foreground_processes")
        matches = ([p for p in processes if isinstance(p, dict) and find_agent_process(
            dict(info, foreground_processes=[p]), str(agent.get("agent") or "")) is not None]
            if isinstance(processes, list) and len(processes) <= 128 else [])
        proc = matches[0] if len(matches) == 1 else None
        if (info.get("pane_id") != agent["pane_id"] or type(info.get("shell_pid")) is not int
                or info["shell_pid"] <= 0 or type(info.get("foreground_process_group_id")) is not int
                or info["foreground_process_group_id"] <= 0 or proc is None
                or type(proc.get("pid")) is not int or proc["pid"] <= 0):
            raise ControlError("Missing or ambiguous native process runtime")
        signature = (native.domain().key, binding(agent), agent["workspace_id"], info["shell_pid"], proc["pid"])
        target = {"fleet_label": fleet.workspace or "Registered fleet", "seat": seat.name,
                  "pane": agent["pane_id"], "terminal": agent["terminal_id"]}
        return native, values, signature, target

    def _prune(self):
        now = time.monotonic()
        for nonce, item in list(self.pending.items()):
            if now >= item["until"]:
                del self.pending[nonce]
        while len(self.used) > 1024:
            for key, item in self.used.items():
                if item[2] is None or item[2].done.is_set():
                    del self.used[key]
                    break
            else:
                raise ControlError("Control attempt capacity reached")
        while len(self.retired) > 1024:
            del self.retired[next(iter(self.retired))]

    @staticmethod
    def schema(action, body):
        expected = {"prepare": {"fleet", "seat", "view", "tab"},
                    "acquire": {"nonce", "tab", "confirmed", "cols", "rows"},
                    "stream": {"lease", "tab"}, "input": {"lease", "tab", "seq", "text"},
                    "resize": {"lease", "tab", "seq", "cols", "rows"},
                    "heartbeat": {"lease", "tab"}, "release": {"lease", "tab"}}
        if action == "release" and set(body) == {"nonce", "tab"}:
            fields = {"nonce", "tab"}
        else:
            fields = expected.get(action)
        if fields is None or set(body) != fields or not isinstance(body.get("tab"), str) or not TAB.fullmatch(body["tab"]):
            raise ControlError("Invalid Control request schema")
        for key in fields & {"fleet", "seat", "view"}:
            if not isinstance(body[key], str) or not 0 < len(body[key]) <= 256:
                raise ControlError("Invalid registered Control selection")
        for key in fields & {"lease", "nonce"}:
            if not isinstance(body[key], str) or not SECRET.fullmatch(body[key]):
                raise ControlError("Invalid Control capability")
        if action == "acquire" and body["confirmed"] is not True:
            raise ControlError("Explicit confirmation is required")
        if action in ("acquire", "resize") and (type(body["cols"]) is not int or type(body["rows"]) is not int
                or not 20 <= body["cols"] <= 300 or not 5 <= body["rows"] <= 120):
            raise ControlError("Invalid Control viewport")
        if action in ("input", "resize") and (type(body["seq"]) is not int or not 1 <= body["seq"] <= 10000000):
            raise ControlError("Invalid Control submission sequence")
        if action == "input":
            try:
                valid = isinstance(body["text"], str) and 0 < len(body["text"].encode("utf-8")) <= 8192
            except UnicodeError:
                valid = False
            if not valid:
                raise ControlError("Input exceeds the 8 KiB limit or is empty")

    def prepare(self, body, session):
        self.schema("prepare", body)
        if not self.authorized(session):
            raise ControlError("Owner authority expired")
        snapshot = Probe(self, body).wait(threading.Event())
        with self.lock:
            self._prune()
            if self.closed or len(self.pending) >= 64:
                raise ControlError("Control prepare capacity reached")
            nonce = secrets.token_urlsafe(32)
            self.pending[nonce] = dict(selection=dict(body), session=session, until=time.monotonic() + PREPARE_S,
                                       signature=snapshot[2])
        return dict(nonce=nonce, expires_in=30, target=snapshot[3], disclosures=list(DISCLOSURES))

    def acquire(self, body, session):
        self.schema("acquire", body)
        with self.lock:
            self._prune()
            item = self.pending.get(body["nonce"])
            if (not self.authorized(session) or item is None or item["session"] != session
                    or item["selection"]["tab"] != body["tab"]):
                raise ControlError("Control confirmation expired, cancelled or already used; never retry acquisition")
            del self.pending[body["nonce"]]
            self.used[body["nonce"]] = (session, body["tab"], None)
            self._prune()
            if len(self.leases) >= MAX_LEASES or any(w.tab == body["tab"] for w in self.leases.values()):
                raise ControlError("Control capacity or tab already occupied")
            worker = Lease(self, item, body)
            self.leases[worker.ident] = worker
            self.used[body["nonce"]] = (session, body["tab"], worker)
            try:
                worker.thread.start()
            except RuntimeError:
                self.leases.pop(worker.ident, None)
                worker.state = "released"; worker.done.set(); worker.ready.set()
                raise ControlError("Control worker capacity unavailable")
        if not worker.ready.wait(SETUP_S + 3) or worker.state != "ready":
            worker.revoke("released")
            worker.done.wait(1.5)
            raise ControlError("Control acquisition unavailable; input was not enabled; native resume is not undone")
        return dict(lease=worker.ident, target=worker.target, remaining_seconds=600, state="ready")

    def get(self, body, session):
        with self.lock:
            worker = self.leases.get(body["lease"])
        if worker is None or worker.session != session or worker.tab != body["tab"] or not self.authorized(session):
            raise ControlError("Control lease unavailable or owner authority expired")
        return worker

    def perform(self, action, body, session):
        try:
            self.schema(action, body)
        except ControlError:
            if action in ("input", "resize"):
                # A malformed write with a valid bound lease is fail-closed too;
                # seq=0 must not be rejected yet leave seq=1 usable afterward.
                try:
                    addressed = {"lease": body.get("lease"), "tab": body.get("tab")}
                    self.schema("stream", addressed)
                    self.uncertain(addressed, session)
                except ControlError:
                    pass
            raise
        if action == "prepare": return self.prepare(body, session)
        if action == "acquire": return self.acquire(body, session)
        if action == "release": return self.release(body, session)
        worker = self.get(body, session)
        if action in ("input", "resize"):
            return worker.submit(action, body)
        if action == "heartbeat":
            return worker.check_heartbeat()
        raise ControlError("Unsupported Control action")

    def release(self, body, session):
        with self.lock:
            if "nonce" in body:
                item = self.pending.get(body["nonce"])
                used = self.used.get(body["nonce"])
                if item and item["session"] == session and item["selection"]["tab"] == body["tab"]:
                    del self.pending[body["nonce"]]
                    self.used[body["nonce"]] = (session, body["tab"], None)
                    self._prune()
                    return {"state": "released"}
                if not used or used[:2] != (session, body["tab"]):
                    raise ControlError("Control attempt unavailable")
                worker = used[2]
            else:
                worker = self.leases.get(body["lease"])
                if worker is None:
                    old = self.retired.get(body["lease"])
                    if old and old[:2] == (session, body["tab"]): return {"state": "released"}
                    raise ControlError("Control lease unavailable")
                if (worker.session, worker.tab) != (session, body["tab"]):
                    raise ControlError("Control lease unavailable")
        if worker is None: return {"state": "released"}
        worker.revoke("released")
        return {"state": "released" if worker.done.wait(1.5) else "release_incomplete"}

    def uncertain(self, body, session):
        try: self.get(body, session).revoke("uncertain")
        except ControlError: pass

    def frames(self, body, session, disconnected):
        self.schema("stream", body)
        worker = self.get(body, session)
        with worker.lock:
            if worker.attached or worker.state != "ready":
                worker.revoke("released")
                raise ControlError("Control stream is one-shot; never reconnect")
            worker.attached = True
            worker.state = "active"
        try:
            while not worker.stop.is_set():
                if disconnected() or not self.authorized(session):
                    return
                try:
                    frame, size = worker.output.get(timeout=0.05)
                except queue.Empty:
                    continue
                with worker.lock: worker.output_bytes -= size
                yield frame
            complete = worker.done.wait(1.5)
            yield {"type": "control", "state": worker.state if complete else "release_incomplete",
                   "message": "Control ended; accepted input or native resume cannot be undone"}
        finally:
            worker.revoke("released")

    def close(self):
        self.closed = True
        with self.lock: workers = list(self.leases.values())
        for worker in workers: worker.revoke("released")
        end = time.monotonic() + 2
        for worker in workers: worker.done.wait(max(0, end - time.monotonic()))


class Lease:
    def __init__(self, manager, item, body):
        self.manager, self.session, self.tab = manager, item["session"], body["tab"]
        self.selection, self.expected = item["selection"], item["signature"]
        self.cols, self.rows = body["cols"], body["rows"]
        self.ident, self.target = secrets.token_urlsafe(32), None
        self.lock = threading.RLock()
        self.stop, self.done, self.ready, self.full = (threading.Event() for _ in range(4))
        self.output, self.jobs = queue.Queue(32), queue.Queue(1)
        self.output_bytes, self.next_seq, self.inflight = 0, 1, False
        self.state, self.attached = "acquiring", False
        self.born = self.heartbeat = manager.now()
        self.thread = threading.Thread(target=self.run, name="human-control-lease", daemon=True)

    def revoke(self, reason):
        with self.lock:
            if not self.stop.is_set(): self.state = reason
            self.stop.set()

    def validate(self):
        if not self.manager.authorized(self.session):
            raise ControlError("Owner authority expired")
        snapshot = Probe(self.manager, self.selection).wait(self.stop)
        if snapshot[2] != self.expected:
            raise ControlError("Control identity changed; accepted work is not undone")
        return snapshot

    def eligible(self):
        now = self.manager.now()
        if (not self.manager.authorized(self.session) or now - self.born >= ABSOLUTE_S
                or now - self.heartbeat >= HEARTBEAT_S
                or (not self.attached and self.state == "ready" and now - self.born >= UNATTACHED_S)):
            self.revoke("expired")
        return not self.stop.is_set()

    def deadline_watch(self):
        while not self.stop.wait(0.02):
            self.eligible()  # Signal independently of native probes and blocked writes.

    def read_output(self, stream):
        decoder = Frames()
        try:
            for raw in stream.lines(self.stop):
                frame = decoder.decode(raw)
                if frame["type"] != "frame":
                    raise ControlError("Controller output ended")
                size = len(raw)
                with self.lock:
                    if self.output_bytes + size > 1048576: raise ControlError("Control output overflow")
                    self.output.put_nowait((frame, size)); self.output_bytes += size
                    self.full.set()
            self.revoke("released")
        except Exception:
            self.revoke("uncertain")

    def submit(self, action, body):
        event, answer = threading.Event(), []
        with self.lock:
            if self.state != "active" or self.stop.is_set() or self.inflight:
                self.revoke("uncertain")
                raise ControlError("Control not ready or overlapping submission; revoked")
            if action in ("input", "resize"):
                if body["seq"] != self.next_seq:
                    self.revoke("uncertain")
                    raise ControlError("Control sequence mismatch; revoked, never replay")
                self.next_seq += 1  # Reserve before any native write.
            self.inflight = True
            self.jobs.put_nowait((action, dict(body), event, answer))
        if not event.wait(3.5) or not answer or answer[0] is None:
            self.revoke("uncertain")
            raise ControlError("Submission uncertain; revoked, never replay")
        return answer[0]

    def check_heartbeat(self):
        with self.lock:
            if self.state != "active" or self.stop.is_set():
                raise ControlError("Control is not active")
            recent = self.manager.now() - self.heartbeat < PROBE_S
        if not recent:
            try: self.validate()
            except Exception:
                self.revoke("expired")
                raise ControlError("Control authority/identity expired")
            with self.lock:
                if self.stop.is_set(): raise ControlError("Control ended")
                if not self.eligible(): raise ControlError("Control expired")
                self.heartbeat = self.manager.now()
        return {"state": "active", "remaining_seconds": max(0, int(ABSOLUTE_S - (self.manager.now() - self.born)))}

    def run(self):
        permit = stream = reader = None
        job = None
        try:
            snapshot = self.validate()
            permit = permits.Store().control(snapshot[0].domain(), snapshot[1])
            snapshot = self.validate()  # Fresh after exclusion, before the single spawn.
            self.target = snapshot[3]
            if self.stop.is_set(): raise ControlError("Acquisition cancelled")
            stream = snapshot[0].terminal_control(self.target["terminal"], self.cols, self.rows, permit)
            if stream.setup_failed: raise ControlError("Controller pipe setup unavailable")
            reader = threading.Thread(target=self.read_output, args=(stream,), name="control-output-only", daemon=True)
            reader.start()
            end = time.monotonic() + SETUP_S
            while not self.full.wait(0.02):
                if self.stop.is_set() or time.monotonic() >= end: raise ControlError("No full frame")
            self.validate()  # May detect the expressly approved native restore race.
            with self.lock:
                if self.stop.is_set(): raise ControlError("Acquisition ended")
                self.state, self.born, self.heartbeat = "ready", self.manager.now(), self.manager.now()
                self.ready.set()
            threading.Thread(target=self.deadline_watch, name="control-expiry-only", daemon=True).start()
            next_probe = self.manager.now() + PROBE_S
            while not self.stop.is_set():
                now = self.manager.now()
                if not self.eligible(): break
                if now >= next_probe:
                    self.validate(); next_probe = self.manager.now() + PROBE_S
                    if not self.eligible(): break
                try: job = self.jobs.get(timeout=0.02)
                except queue.Empty: continue
                action, body, event, answer = job
                if not self.eligible(): raise ControlError("Revoked")
                if action == "input":
                    stream.input(body["text"], self.stop, self.eligible)
                    result = {"status": "submitted", "accepted": body["seq"]}
                else:
                    stream.resize(body["cols"], body["rows"], self.stop, self.eligible)
                    result = {"status": "submitted", "accepted": body["seq"]}
                with self.lock:
                    if self.stop.is_set(): raise ControlError("Submission uncertain")
                    answer.append(result); self.inflight = False
                body.clear(); event.set(); job = None
        except Exception:
            self.revoke("uncertain")
        finally:
            self.stop.set(); self.ready.set()
            if job is not None:
                job[1].clear(); job[3].append(None); job[2].set()
            while not self.jobs.empty():
                _, payload, event, answer = self.jobs.get_nowait()
                payload.clear(); answer.append(None); event.set()
            # A stuck exact child retains its permit and capacity. Recheck without
            # ever advertising released; no broad kills or explicit LOCK_UN.
            while stream is not None and not stream.teardown():
                self.state = "release_incomplete"
                time.sleep(0.2)
            if reader is not None:
                reader.join(timeout=0.5)
                while reader.is_alive():
                    self.state = "release_incomplete"; reader.join(timeout=0.2)
            if stream is not None and stream.process.stdout: stream.process.stdout.close()
            if permit is not None: permit.close()
            with self.lock:
                while not self.output.empty(): self.output.get_nowait()
                self.output_bytes = 0
                if self.state not in ("expired", "uncertain"): self.state = "released"
            with self.manager.lock:
                self.manager.leases.pop(self.ident, None)
                self.manager.retired[self.ident] = (self.session, self.tab, self.state)
                self.manager._prune()
            self.done.set()
