"""``worktrees`` (design 17.2): every Git worktree of the fleet's repository,
read-only.

``git -C <repo> worktree list --porcelain`` gives the worktrees in git's
order; for each one the last commit's age (``git log -1 --format=%ct``),
the count of changed paths (``git status --porcelain``), whether its HEAD
is merged into the default branch (``git merge-base --is-ancestor``) and
the seats whose ``worktrees`` globs match its path. All git calls go
through ``gitinfo`` (2 s timeouts, no locks, nothing that writes); a
failed call renders as ``?``. No herdr command is run at all.
"""

from __future__ import annotations

import fnmatch
import json
import os
import time
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence

from herdr_fleet.checks import shorten
from herdr_fleet.context import EXIT_OK, Ctx, OpError, UsageError
from herdr_fleet.context_size import age_label
from herdr_fleet.fleetfile import Seat
from herdr_fleet.gitinfo import default_branch, run_git

MAIN_MARK = "main"


@dataclass
class Worktree:
    """One block of ``git worktree list --porcelain``."""

    path: str
    head: str = ""
    branch: Optional[str] = None   # short name; None when detached or bare
    detached: bool = False
    bare: bool = False
    locked: bool = False
    prunable: bool = False


def parse_porcelain(text: str) -> List[Worktree]:
    """Blocks separated by blank lines: ``worktree <path>``, ``HEAD <sha>``, then
    ``branch refs/heads/<name>`` or ``detached`` (or ``bare``), optionally
    ``locked [reason]`` and ``prunable [reason]``."""
    items: List[Worktree] = []
    current: Optional[Worktree] = None
    for line in text.splitlines():
        if not line.strip():
            current = None
            continue
        word, _, value = line.partition(" ")
        if word == "worktree":
            current = Worktree(path=value)
            items.append(current)
        elif current is None:
            continue
        elif word == "HEAD":
            current.head = value.strip()
        elif word == "branch":
            ref = value.strip()
            current.branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
        elif word == "detached":
            current.detached = True
        elif word == "bare":
            current.bare = True
        elif word == "locked":
            current.locked = True
        elif word == "prunable":
            current.prunable = True
    return items


def _realpath_glob(pattern: str) -> str:
    """The glob with its wildcard-free leading directories resolved through symlinks,
    so ``/tmp/sample-*`` also matches the ``/private/tmp/sample-x`` git prints on macOS."""
    parts = pattern.split(os.sep)
    fixed: List[str] = []
    for part in parts[:-1]:
        if any(char in part for char in "*?["):
            break
        fixed.append(part)
    if len(fixed) < 2:  # nothing but the root before the first wildcard
        return pattern
    head = os.sep.join(fixed)
    return os.path.realpath(head) + pattern[len(head):]


def matches(globs: Sequence[str], path: str) -> bool:
    """``fnmatch`` (case-sensitive) of the worktree path against each glob."""
    for pattern in globs:
        if fnmatch.fnmatchcase(path, pattern) or fnmatch.fnmatchcase(path, _realpath_glob(pattern)):
            return True
    return False


def _commit_time(path: str, env: Mapping[str, str]) -> Optional[int]:
    result = run_git(path, ["log", "-1", "--format=%ct"], env)
    if result is None or result.code != 0:
        return None
    try:
        return int(result.out.strip())
    except ValueError:
        return None


def _changed(path: str, env: Mapping[str, str]) -> Optional[int]:
    result = run_git(path, ["status", "--porcelain"], env)
    if result is None or result.code != 0:
        return None
    return len([line for line in result.out.splitlines() if line.strip()])


def _merged(repo: str, head: str, default: Optional[str], env: Mapping[str, str]) -> Optional[bool]:
    if not default or not head:
        return None
    result = run_git(repo, ["merge-base", "--is-ancestor", head, default], env)
    if result is None or result.code not in (0, 1):
        return None
    return result.code == 0


def inspect(repo: str, seats: Sequence[Seat], env: Mapping[str, str], now: Optional[float] = None) -> Dict:
    """The ``worktrees --json`` payload. Raises OpError when ``worktree list`` fails."""
    listed = run_git(repo, ["worktree", "list", "--porcelain"], env)
    if listed is None:
        raise OpError("git did not answer in %s (is git installed?)" % repo)
    if listed.code != 0:
        raise OpError("not a git repository: %s" % repo)
    now = time.time() if now is None else now
    default = default_branch(repo, env)
    rows = []
    for index, tree in enumerate(parse_porcelain(listed.out)):
        missing = not os.path.isdir(tree.path)
        skip = missing or tree.prunable or tree.bare  # no git calls in that path
        committed = None if skip else _commit_time(tree.path, env)
        claimed = [seat.name for seat in seats if seat.worktrees and matches(seat.worktrees, tree.path)]
        rows.append({
            "path": tree.path,
            "branch": tree.branch,
            "head": tree.head,
            "detached": tree.detached,
            "age_s": None if committed is None else max(0, int(now - committed)),
            "changed": None if skip else _changed(tree.path, env),
            "missing": missing,
            "prunable": tree.prunable,
            "locked": tree.locked,
            "merged": None if tree.bare else _merged(repo, tree.head, default, env),
            "seats": claimed,
            "bare": tree.bare,
            "main": index == 0,
        })
    return {"repo": repo, "default_branch": default, "worktrees": rows}


def public(data: Dict) -> Dict:
    """The JSON shape of design 17.2 (drops the rendering helpers ``bare`` and ``main``)."""
    keys = ("path", "branch", "head", "detached", "age_s", "changed", "missing", "prunable", "locked", "merged",
            "seats")
    return {"repo": data["repo"], "default_branch": data["default_branch"],
            "worktrees": [{k: row[k] for k in keys} for row in data["worktrees"]]}


def _short(path: str, home: Optional[str]) -> str:
    """``~/…`` also when git printed the real path of a symlinked home."""
    short = shorten(path, home)
    if short == path and home:
        short = shorten(path, os.path.realpath(home))
    return short


def _cells(row: Dict, default: Optional[str], home: Optional[str]) -> List[str]:
    skipped = row["missing"] or row["prunable"] or row["bare"]
    if row["bare"]:
        branch = "(bare)"
    elif row["detached"] or not row["branch"]:
        branch = "detached @%s" % (row["head"][:7] or "?")
    else:
        branch = row["branch"]
    state: List[str] = []
    if row["missing"]:
        state.append("missing")
    elif not skipped:
        changed = row["changed"]
        state.append("?" if changed is None else "clean" if changed == 0 else "%d changed" % changed)
    if row["prunable"]:
        state.append("prunable")
    if row["locked"]:
        state.append("locked")
    if not default or row["bare"]:
        merged = "-"
    else:
        merged = "?" if row["merged"] is None else "yes" if row["merged"] else "no"
    seats = ", ".join(row["seats"]) or (MAIN_MARK if row["main"] else "")
    return [_short(row["path"], home), branch, "-" if skipped else age_label(row["age_s"]),
            ", ".join(state) or "-", merged, seats]


def render(data: Dict, home: Optional[str] = None) -> str:
    default = data["default_branch"]
    header = ["PATH", "BRANCH", "AGE", "STATE", "MERGED", "SEAT"]
    rows = [_cells(row, default, home) for row in data["worktrees"]]
    widths = [max(len(r[i]) for r in [header] + rows) for i in range(len(header) - 1)]
    lines = ["repo %s  default %s" % (_short(data["repo"], home), default or "-")]
    for row in [header] + rows:
        lines.append("  ".join([cell.ljust(widths[i]) for i, cell in enumerate(row[:-1])] + [row[-1]]).rstrip())
    if not default:
        lines.append("note: no default branch found (no origin/HEAD, origin/main or origin/master); "
                     "MERGED is -")
    return "\n".join(lines)


def op_worktrees(ctx: Ctx, seat_name: Optional[str] = None, as_json: bool = False) -> int:
    """Design 17.2. Reads git only; runs no herdr command."""
    fleet = ctx.fleet
    if seat_name and fleet.seat(seat_name) is None:
        raise UsageError("not a seat: %s" % seat_name)
    data = inspect(fleet.repo, fleet.seats, ctx.env)
    if seat_name:
        data["worktrees"] = [row for row in data["worktrees"] if seat_name in row["seats"]]
    if as_json:
        ctx.say(json.dumps(public(data), indent=2, ensure_ascii=False))
        return EXIT_OK
    seat = fleet.seat(seat_name) if seat_name else None
    if seat is not None and not data["worktrees"]:
        if seat.worktrees:
            ctx.say("no worktree matches %s's globs: %s" % (seat.name, ", ".join(seat.worktrees)))
        else:
            ctx.say("%s declares no worktrees globs (worktrees = [...] in the fleet file)" % seat.name)
        return EXIT_OK
    ctx.say(render(data, ctx.env.get("HOME")))
    return EXIT_OK
