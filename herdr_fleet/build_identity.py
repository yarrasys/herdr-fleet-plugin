"""Immutable process/launch identity (design 31.4); read-only, best-effort Git."""
from __future__ import annotations

import os
import re
from functools import lru_cache
from typing import Dict, Mapping

from herdr_fleet import PLUGIN_ROOT, __version__
from herdr_fleet import gitinfo

COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


@lru_cache(maxsize=1)
def current() -> Dict:
    commit = None
    root = gitinfo.run_git(PLUGIN_ROOT, ["rev-parse", "--show-toplevel"])
    if root and root.code == 0 and os.path.realpath(root.out.strip()) == os.path.realpath(PLUGIN_ROOT):
        result = gitinfo.run_git(PLUGIN_ROOT, ["rev-parse", "--verify", "HEAD"])
        if result and result.code == 0 and COMMIT.fullmatch(result.out.strip()):
            commit = result.out.strip()
    return {"version": __version__, "commit": commit}


def validated(value: object) -> Dict:
    value = value if isinstance(value, dict) else {}
    version, commit = value.get("version"), value.get("commit")
    return {"version": version if isinstance(version, str) and version else None,
            "commit": commit if isinstance(commit, str) and COMMIT.fullmatch(commit) else None}


def compare(cli: Mapping, launch: Mapping) -> str:
    for key in ("version", "commit"):
        if cli.get(key) and launch.get(key) and cli[key] != launch[key]:
            return "stale"
    return "current" if all(cli.get(k) and launch.get(k) for k in ("version", "commit")) else "unknown"


def label(identity: Mapping) -> str:
    return "version %s, commit %s" % (identity.get("version") or "unknown", identity.get("commit") or "unknown")
