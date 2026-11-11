"""Closed shapes for the native commands the plugin actually supports (45.3)."""
from __future__ import annotations

import math
import re


def identifier(value):
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}", value))


def number(value):
    return bool(re.fullmatch(r"[0-9]{1,10}", value)) and int(value) <= 2147483647


def fraction(value):
    try:
        result = float(value)
        return math.isfinite(result) and 0 < result < 1
    except ValueError:
        return False


def path(value):
    return value.startswith("/") and len(value) <= 4096


def environment(value):
    return "=" in value and bool(value.split("=", 1)[0]) and not value.startswith("-")


def options(argv, validators, required=(), flags=(), repeated=()):
    found = {}
    i = 0
    while i < len(argv):
        key = argv[i]
        if key in found and key not in repeated:
            return None
        if key in flags:
            found.setdefault(key, []).append(True)
            i += 1
        elif key in validators and i + 1 < len(argv) and validators[key](argv[i + 1]):
            found.setdefault(key, []).append(argv[i + 1])
            i += 2
        else:
            return None
    return found if all(k in found for k in required) else None


def classify(argv, plugin_id="yarrasys.fleet"):
    """Return read/exempt/mutation, or None. This never executes anything."""
    if any(not isinstance(a, str) or "\0" in a for a in argv):
        return None
    fixed = (("--version",), ("agent",), ("agent", "start", "--help"),
             ("agent", "list"), ("integration", "status"), ("plugin", "list", "--json"),
             ("pane", "current", "--current"), ("workspace", "list"))
    if tuple(argv) in fixed:
        return "read"
    pair = tuple(argv[:2])
    if pair == ("plugin", "config-dir"):
        return "read" if len(argv) == 3 and identifier(argv[2]) else None
    if pair in (("agent", "get"), ("pane", "get"), ("workspace", "get")):
        return "read" if len(argv) == 3 and identifier(argv[2]) else None
    if pair in (("pane", "list"), ("tab", "list")):
        return "read" if len(argv) == 4 and argv[2] == "--workspace" and identifier(argv[3]) else None
    if pair in (("pane", "process-info"), ("pane", "layout")):
        return "read" if len(argv) == 4 and argv[2] == "--pane" and identifier(argv[3]) else None
    if pair in (("agent", "read"), ("pane", "read")):
        if len(argv) < 3 or not identifier(argv[2]): return None
        allowed = {"--lines": number, "--format": lambda a: a == "ansi"}
        if pair[0] == "agent": allowed["--source"] = lambda a: a in ("recent", "visible")
        return "read" if options(argv[3:], allowed, required=("--lines",)) is not None else None
    if pair == ("agent", "wait"):
        if len(argv) < 3 or not identifier(argv[2]): return None
        opts = options(argv[3:], {"--until": lambda a: a in ("idle", "done", "blocked"), "--timeout": number},
                       required=("--until", "--timeout"), repeated=("--until",))
        return "read" if opts is not None else None
    if pair == ("agent", "prompt"):
        return "mutation" if len(argv) == 4 and identifier(argv[2]) else None
    if pair == ("agent", "send-keys"):
        return "mutation" if len(argv) >= 4 and identifier(argv[2]) and all(k in ("enter", "esc", "ctrl+c", "ctrl+u") for k in argv[3:]) else None
    if pair in (("agent", "focus"), ("pane", "close"), ("workspace", "close")):
        return "mutation" if len(argv) == 3 and identifier(argv[2]) else None
    if pair in (("agent", "rename"), ("pane", "rename")):
        if len(argv) != 4 or not identifier(argv[2]): return None
        if pair[0] == "agent": return "mutation" if identifier(argv[3]) else None
        return "mutation" if argv[3] and (not argv[3].startswith("-") or argv[3] == "--clear") else None
    if pair == ("pane", "zoom"):
        return "mutation" if len(argv) == 4 and identifier(argv[2]) and argv[3] == "--toggle" else None
    if pair == ("agent", "start"):
        prefix = argv[:argv.index("--")] if "--" in argv else argv
        if len(prefix) < 3 or not identifier(prefix[2]): return None
        opts = options(prefix[3:], {"--kind": identifier, "--pane": identifier, "--timeout": number},
                       required=("--kind", "--pane"))
        return "mutation" if opts is not None else None
    if pair == ("pane", "split"):
        if len(argv) < 3 or not identifier(argv[2]): return None
        opts = options(argv[3:], {"--direction": lambda a: a in ("right", "down"), "--ratio": fraction,
                                 "--cwd": path, "--env": environment}, required=("--direction", "--no-focus"),
                       flags=("--no-focus",), repeated=("--env",))
        return "mutation" if opts is not None else None
    if pair == ("pane", "move"):
        if len(argv) < 3 or not identifier(argv[2]): return None
        opts = options(argv[3:], {"--tab": identifier, "--split": lambda a: a in ("right", "down"),
                                 "--ratio": fraction, "--target-pane": identifier, "--workspace": identifier,
                                 "--label": lambda a: bool(a) and not a.startswith("-")},
                       required=("--no-focus",), flags=("--no-focus", "--new-tab"))
        if opts is None: return None
        keys = set(opts)
        if keys == {"--new-tab", "--workspace", "--label", "--no-focus"}: return "mutation"
        if {"--tab", "--split", "--no-focus"} <= keys and not keys & {"--new-tab", "--workspace", "--label"}: return "mutation"
        return None
    if pair == ("pane", "resize"):
        opts = options(argv[2:], {"--pane": identifier, "--direction": lambda a: a in ("up", "down", "left", "right"), "--amount": fraction},
                       required=("--pane", "--direction", "--amount"))
        return "mutation" if opts is not None else None
    if pair == ("workspace", "create"):
        opts = options(argv[2:], {"--cwd": path, "--label": lambda a: bool(a) and not a.startswith("-"), "--env": environment},
                       required=("--cwd", "--label", "--no-focus"), flags=("--no-focus",), repeated=("--env",))
        return "mutation" if opts is not None else None
    if tuple(argv[:3]) == ("plugin", "pane", "open"):
        opts = options(argv[3:], {"--plugin": lambda a: a == plugin_id and identifier(a), "--entrypoint": lambda a: a in ("watch", "handover", "status", "manage", "go"),
                                 "--placement": lambda a: a in ("split", "tab", "window"), "--target-pane": identifier,
                                 "--workspace": identifier, "--cwd": path, "--env": environment,
                                 "--direction": lambda a: a in ("right", "down")},
                       required=("--plugin", "--entrypoint", "--cwd"), flags=("--no-focus",), repeated=("--env",))
        if opts is None: return None
        place = (opts.get("--placement") or [None])[0]
        ui_split = place == "split" and set(opts) == {"--plugin", "--entrypoint", "--cwd", "--placement"}
        if place == "split" and not ui_split and not {"--target-pane", "--direction", "--no-focus"} <= set(opts): return None
        if place == "tab" and not {"--workspace", "--no-focus"} <= set(opts): return None
        if place not in ("split", "tab") and set(opts) & {"--target-pane", "--workspace", "--direction", "--env", "--no-focus"}: return None
        return "mutation"
    if pair == ("notification", "show"):
        if len(argv) != 7 or not argv[2] or argv[2].startswith("-") or argv[3] != "--body" or argv[5] != "--sound": return None
        return "exempt" if identifier(argv[6]) else None
    return None
