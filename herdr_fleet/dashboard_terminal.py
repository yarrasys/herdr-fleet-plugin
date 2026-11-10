"""Read-only terminal frames and bounded observer ownership (design 41.6)."""
from __future__ import annotations

import base64
import binascii
import json
import threading
import time
from typing import Dict

from herdr_fleet.herdr import HerdrError
from herdr_fleet.dashboard_data import DataError


class Frames:
    def __init__(self):
        self.sequence = None
        self.full = False

    def decode(self, raw):
        if len(raw) > 262144:
            raise DataError("Watch frame exceeds the limit")
        try:
            frame = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise DataError("Watch received malformed JSON")
        if not isinstance(frame, dict):
            raise DataError("Watch received a non-object frame")
        if frame.get("type") == "terminal.closed":
            return {"type": "closed", "message": "Observer closed; this does not establish agent exit"}
        if (frame.get("type") != "terminal.frame" or frame.get("encoding") != "ansi"
                or type(frame.get("full")) is not bool or type(frame.get("seq")) is not int
                or not isinstance(frame.get("bytes"), str)
                or type(frame.get("width")) is not int or type(frame.get("height")) is not int
                or not 1 <= frame["width"] <= 300 or not 1 <= frame["height"] <= 120):
            raise DataError("Watch received an unsupported frame")
        sequence = frame["seq"]
        if sequence < 0 or (self.sequence is not None and sequence <= self.sequence):
            raise DataError("Watch frame sequence is stale")
        if not self.full and not frame["full"]:
            raise DataError("Watch needs a full frame before deltas")
        if self.sequence is not None and sequence != self.sequence + 1 and not frame["full"]:
            raise DataError("Watch has a frame gap; reconnect for a full frame")
        try:
            data = base64.b64decode(frame["bytes"], validate=True)
        except (ValueError, binascii.Error):
            raise DataError("Watch received invalid ANSI bytes")
        if len(data) > 196608:
            raise DataError("Watch ANSI payload exceeds the limit")
        self.full, self.sequence = True, sequence
        # Forward validated base64 to xterm as bytes; never inject ANSI into DOM/HTML.
        return {"type": "frame", "full": frame["full"], "seq": sequence, "bytes": frame["bytes"],
                "cols": frame["width"], "rows": frame["height"]}


class Watches:
    def __init__(self, herdr, registry, max_observers=8):
        self.herdr, self.registry = herdr, registry
        self.slots = threading.BoundedSemaphore(max_observers)
        self.lock = threading.Lock()
        self.active = {}
        self.closed = False

    def frames(self, fleet_id, name, view, cols, rows, disconnected):
        if not self.slots.acquire(False):
            raise DataError("Watch capacity reached; close an existing view")
        stream = None
        stop = threading.Event()
        try:
            if self.closed:
                raise DataError("Dashboard is stopping")
            _, _, agent = self.registry.target(fleet_id, name, view, self.herdr.agent_list())
            target = agent.get("terminal_id")
            if not isinstance(target, str) or not target:
                raise DataError("Watch unavailable: this Herdr has no terminal identity")
            stream = self.herdr.terminal_observe(target, cols, rows)
            with self.lock:
                if self.closed:
                    stream.close()
                    raise DataError("Dashboard is stopping")
                self.active[id(stop)] = (stop, stream)
            decoder = Frames()
            last_check = [0.0]
            def check():
                if disconnected() or self.closed:
                    return False
                if time.monotonic() - last_check[0] >= 2:
                    self.registry.target(fleet_id, name, view, self.herdr.agent_list())
                    last_check[0] = time.monotonic()
                return True
            for raw in stream.lines(stop, on_tick=check):
                if disconnected():
                    return
                # A current identity check belongs to every frame, not a cached roster.
                if not check():
                    return
                frame = decoder.decode(raw)
                yield frame
                if frame["type"] == "closed":
                    return
            if stream.process.returncode not in (None, 0) and not disconnected() and not self.closed:
                yield {"type": "error", "message": "Watch unavailable: this Herdr may not support terminal observation"}
            else:
                yield {"type": "ended", "message": "Watch stream ended; agent state is not inferred"}
        except HerdrError:
            yield {"type": "error", "message": "Watch unavailable or disconnected; refresh and reconnect"}
        finally:
            stop.set()
            if stream is not None:
                stream.close()
            with self.lock:
                self.active.pop(id(stop), None)
            self.slots.release()

    def close(self):
        with self.lock:
            self.closed = True
            active = list(self.active.values())
        for stop, stream in active:
            stop.set()
            stream.close()
