"""Independent installed/running identity; bounded read-only probes, never updates."""
from __future__ import annotations

import re
import time

from herdr_fleet import build_identity, inbox


def identity(value):
    result = build_identity.validated(value)
    version = result["version"]
    if version is not None and (len(version) > 64 or not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.-]+)?", version)):
        result["version"] = None
    return result


class Builds:
    def __init__(self, herdr, all_fleets=False):
        self.herdr = herdr
        self.running = identity(build_identity.current())
        self.installed = identity(None)
        self.checked_at, self.last = None, None
        self.restart = "all_fleets" if all_fleets else "original_command"

    def refresh(self, now):
        current = time.monotonic()
        if self.last is not None and current - self.last < 60.0:
            return
        self.last, self.checked_at = current, inbox.iso_local(now)
        self.installed = identity(None)
        try:
            matches = [p for p in self.herdr.plugin_list()
                       if p.get("plugin_id") == "yarrasys.fleet" and p.get("enabled") is True]
            if len(matches) != 1:
                return
            registration = matches[0]
            source = registration.get("source")
            commit = source.get("resolved_commit") if isinstance(source, dict) else None
            self.installed = identity({"version": registration.get("version"), "commit": commit})
        except Exception:
            self.installed = identity(None)

    def snapshot(self):
        comparison = build_identity.compare(self.running, self.installed)
        return {"running": dict(self.running), "installed": dict(self.installed),
                "comparison": "different" if comparison == "stale" else comparison,
                "checked_at": self.checked_at, "restart": self.restart}
