"""Bounded daily calendar batches; native TZ is confined to a child (design 30.2)."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
import threading
from collections import OrderedDict
from typing import Dict, List, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python 3.8 uses the isolated native calendar below
    ZoneInfo = None

UTC = dt.timezone.utc
ZONE_RE = re.compile(r"[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)*")
ZONE_ROOTS = ("/usr/share/zoneinfo", "/usr/share/lib/zoneinfo", "/usr/lib/zoneinfo")


def zone_file(name: str) -> Optional[str]:
    if not isinstance(name, str) or not ZONE_RE.fullmatch(name):
        return None
    for root in ZONE_ROOTS:
        path = os.path.join(root, name)
        if os.path.isfile(path):
            with open(path, "rb") as handle:
                if handle.read(4) == b"TZif":
                    return path
    return None


def valid_zone(name: object) -> bool:
    if not isinstance(name, str) or not ZONE_RE.fullmatch(name):
        return False
    if ZoneInfo is not None:
        try:
            ZoneInfo(name)
            return True
        except (ValueError, KeyError, OSError):
            return False
    try:
        return zone_file(name) is not None
    except OSError:
        return False


def iso(when: dt.datetime) -> str:
    return when.astimezone(UTC).isoformat(timespec="seconds")


class Calendar:
    """One interface for both backends, one 64-date batch per active schedule.

    A batch contains dates, UTC instants and local displays only. Its first date is
    three days before the anchor's UTC date: enough for the maximum 24-hour grace
    across supported offset changes, with at least 20 upcoming dates remaining.
    """
    def __init__(self, backend: Optional[str] = None):
        self.backend = backend or ("zoneinfo" if ZoneInfo is not None else "native")
        self.cache: Dict = OrderedDict()
        self.lock = threading.Lock()

    def window(self, zone: str, at: str, anchor: dt.datetime) -> List[Dict]:
        day = anchor.astimezone(UTC).date()
        key = (zone, at, day)
        with self.lock:
            if key in self.cache:
                self.cache.move_to_end(key)
                return self.cache[key]
            start = dt.date.fromordinal(max(1, day.toordinal() - 3))
            count = min(64, dt.date.max.toordinal() - start.toordinal() + 1)
            dates = [(start + dt.timedelta(days=i)).isoformat() for i in range(count)]
            if self.backend == "native":
                path = zone_file(zone)
                if path is None:
                    raise ValueError("timezone unavailable: %s" % zone)
                # No shell and no global TZ mutation. The validated absolute file is
                # selected only in the child's environment, not interpolated as code.
                env = dict(os.environ, TZ=":" + path)
                try:
                    result = subprocess.run([sys.executable, os.path.abspath(__file__)],
                                            input=json.dumps({"dates": dates, "at": at}), text=True,
                                            capture_output=True, env=env, timeout=15)
                except subprocess.SubprocessError:
                    raise ValueError("native timezone calendar failed for %s" % zone)
                if result.returncode:
                    raise ValueError("native timezone calendar failed for %s" % zone)
                values = json.loads(result.stdout)
            else:
                if ZoneInfo is None:
                    raise ValueError("zoneinfo unavailable")
                tz = ZoneInfo(zone)
                values = []
                hour, minute = map(int, at.split(":"))
                for date in dates:
                    wall = dt.datetime.fromisoformat(date).replace(hour=hour, minute=minute)
                    candidates = []
                    forward = []
                    for fold in (0, 1):
                        instant = wall.replace(tzinfo=tz, fold=fold).astimezone(UTC)
                        if instant.astimezone(tz).replace(tzinfo=None) == wall:
                            candidates.append(instant)
                        elif instant.astimezone(tz).replace(tzinfo=None) > wall:
                            forward.append(instant)
                    chosen = min(candidates) if candidates else None
                    gap_at = None
                    if chosen is None and forward:
                        gap_at = min(forward)
                        for _ in range(26 * 60):
                            previous = gap_at - dt.timedelta(minutes=1)
                            if previous.astimezone(tz).replace(tzinfo=None) < wall:
                                break
                            gap_at = previous
                    values.append({"date": date, "utc": iso(chosen) if chosen else None,
                                   "local": chosen.astimezone(tz).isoformat() if chosen else date + "T" + at,
                                   "gap_at": iso(gap_at) if gap_at else None})
            self.cache[key] = values
            for old in list(self.cache):
                if old[:2] == key[:2] and old != key:
                    del self.cache[old]
            return values

    def retain(self, schedules) -> None:
        """Prune removed schedule keys without evicting another active timer's batch."""
        active = set(schedules)
        with self.lock:
            for key in list(self.cache):
                if key[:2] not in active:
                    del self.cache[key]


CALENDAR = Calendar()


def native_batch() -> None:
    """Worker entry point. Only this child calls tzset; round-trips detect gaps."""
    import time
    time.tzset()
    data = json.load(sys.stdin)
    hour, minute = map(int, data["at"].split(":"))
    values = []
    for date in data["dates"]:
        day = dt.date.fromisoformat(date)
        parts = (day.year, day.month, day.day, hour, minute, 0)
        candidates = []
        forward = []
        for isdst in (-1, 0, 1):
            try:
                stamp = time.mktime(parts + (0, 0, isdst))
            except (ValueError, OverflowError, OSError):
                continue  # some native libraries reject forced DST in a zone without it
            if time.localtime(stamp)[:6] == parts:
                candidates.append(stamp)
            elif time.localtime(stamp)[:6] > parts:
                forward.append(stamp)
        chosen = min(candidates) if candidates else None
        gap_at = None
        if chosen is None and forward:
            gap = min(forward)
            for _ in range(26 * 60):
                if time.localtime(gap - 60)[:6] < parts:
                    break
                gap -= 60
            gap_at = iso(dt.datetime.fromtimestamp(gap, UTC))
        instant = dt.datetime.fromtimestamp(chosen, UTC) if chosen is not None else None
        if chosen is None:
            local = date + "T" + data["at"]
        else:
            wall = dt.datetime.fromtimestamp(chosen)
            offset = wall - dt.datetime.utcfromtimestamp(chosen)
            local = wall.replace(tzinfo=dt.timezone(offset)).isoformat()
        values.append({"date": date, "utc": iso(instant) if instant else None, "local": local, "gap_at": gap_at})
    json.dump(values, sys.stdout)


if __name__ == "__main__":
    native_batch()
