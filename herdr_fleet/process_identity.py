"""Bounded, read-only local process-generation evidence for watcher eligibility."""
from __future__ import annotations

import hashlib
import os
import subprocess

SCHEME = "ps-c-utc-v1"


def identity(pid):
    if type(pid) is not int or pid <= 0:
        return None
    env = dict(os.environ)
    env.update(LC_ALL="C", LC_TIME="C", LANG="C", TZ="UTC0")
    env.pop("COLUMNS", None)
    try:
        result = subprocess.run(["/bin/ps", "-ww", "-p", str(pid), "-o", "uid=,lstart=,command="],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=1, env=env)
        if result.returncode or len(result.stdout) > 65536:
            return None
        raw = result.stdout.strip()
        if not raw or raw.split(None, 1)[0] != str(os.geteuid()).encode():
            return None
        return hashlib.sha256(SCHEME.encode("ascii") + b"\0" + raw).hexdigest()
    except (OSError, subprocess.TimeoutExpired):
        return None
