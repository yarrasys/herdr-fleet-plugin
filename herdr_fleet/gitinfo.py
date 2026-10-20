"""Read-only git helpers for ``worktrees`` (design 17.2), the doctor's
``fleet file vs git`` check (17.3) and the status BRANCH column (16.7).

Every call is ``git -C <dir> ...`` without a shell, with a 2 s timeout, and
with ``GIT_OPTIONAL_LOCKS=0`` so that even ``git status`` never refreshes
the index or takes a lock. Nothing here fetches, prunes or writes
(invariant 8): the only subcommands used are ``rev-parse``, ``symbolic-ref``,
``worktree list``, ``log``, ``status``, ``merge-base --is-ancestor``,
``ls-files`` and ``cat-file``. A missing git, a timeout or an unexpected
exit becomes ``None`` for the caller to render as ``?`` or ``skip``.
"""

from __future__ import annotations

import difflib
import os
import subprocess
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence, Tuple

GIT_TIMEOUT = 2.0
DEFAULT_CANDIDATES = ("origin/main", "origin/master")


@dataclass
class GitResult:
    code: int
    out: str


def git_env(env: Optional[Mapping[str, str]] = None) -> dict:
    merged = dict(os.environ if env is None else env)
    merged["GIT_OPTIONAL_LOCKS"] = "0"
    return merged


def run_git(cwd: str, args: Sequence[str], env: Optional[Mapping[str, str]] = None,
            timeout: float = GIT_TIMEOUT) -> Optional[GitResult]:
    """``git -C cwd args``; None when git is missing, cannot run or times out."""
    try:
        proc = subprocess.run(["git", "-C", cwd] + list(args), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, encoding="utf-8", errors="replace", timeout=timeout,
                              env=git_env(env))
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return GitResult(proc.returncode, proc.stdout)


def current_branch(cwd: str, env: Optional[Mapping[str, str]] = None) -> str:
    """``git -C <cwd> rev-parse --abbrev-ref HEAD``; blank when not a repository or on error."""
    if not cwd or not os.path.isdir(cwd):
        return ""
    result = run_git(cwd, ["rev-parse", "--abbrev-ref", "HEAD"], env)
    return result.out.strip() if result and result.code == 0 else ""


def is_work_tree(cwd: str, env: Optional[Mapping[str, str]] = None) -> bool:
    if not cwd or not os.path.isdir(cwd):
        return False
    result = run_git(cwd, ["rev-parse", "--is-inside-work-tree"], env)
    return bool(result and result.code == 0 and result.out.strip() == "true")


def ref_exists(cwd: str, ref: str, env: Optional[Mapping[str, str]] = None) -> bool:
    result = run_git(cwd, ["rev-parse", "--verify", "-q", "refs/remotes/" + ref], env)
    return bool(result and result.code == 0)


def default_branch(cwd: str, env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """``origin/HEAD``'s target (``git symbolic-ref -q refs/remotes/origin/HEAD``), else
    ``origin/main``, else ``origin/master``, each only if that ref exists; else None.
    Never fetches, so this is what the last fetch left behind."""
    result = run_git(cwd, ["symbolic-ref", "-q", "refs/remotes/origin/HEAD"], env)
    if result and result.code == 0 and result.out.strip().startswith("refs/remotes/"):
        return result.out.strip()[len("refs/remotes/"):]
    for candidate in DEFAULT_CANDIDATES:
        if ref_exists(cwd, candidate, env):
            return candidate
    return None


def show_file(cwd: str, rev: str, rel: str, env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """The text of ``<rev>:./<rel>`` (relative to cwd), or None when there is no such file."""
    result = run_git(cwd, ["cat-file", "-p", "%s:./%s" % (rev, rel)], env)
    return result.out if result and result.code == 0 else None


def is_tracked(cwd: str, rel: str, env: Optional[Mapping[str, str]] = None) -> Optional[bool]:
    """``git ls-files --error-unmatch``: True, False, or None when git did not answer."""
    result = run_git(cwd, ["ls-files", "--error-unmatch", "--", rel], env)
    if result is None:
        return None
    return result.code == 0


def line_diff(old: str, new: str) -> Tuple[int, int, Optional[int]]:
    """(added, removed, first differing line in ``new``) from ``difflib``; no diff body."""
    a, b = old.splitlines(), new.splitlines()
    added = removed = 0
    first: Optional[int] = None
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        added += j2 - j1
        removed += i2 - i1
        if first is None:
            first = j1 + 1
    return added, removed, first
