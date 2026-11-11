"""Bounded, read-only local process-generation evidence for watcher eligibility."""
from __future__ import annotations

import hashlib
import os
import subprocess


def identity(pid):
    if type(pid) is not int or pid <= 0:
        return None
    try:
        result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "uid=,lstart=,command="],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=1)
        raw = result.stdout.strip()
        if (result.returncode or not raw or len(raw) > 65536
                or raw.split(None, 1)[0] != str(os.geteuid()).encode()):
            return None
        return hashlib.sha256(raw).hexdigest()
    except (OSError, subprocess.TimeoutExpired):
        return None
