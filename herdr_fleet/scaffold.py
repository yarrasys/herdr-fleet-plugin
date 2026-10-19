"""``herdr-fleet init`` (design section 13): bootstrap a repository's fleet file.

Pure file writing; no herdr command runs. The target is ``DIR``, else the
invoking directory (the current directory for the CLI, ``focused_pane_cwd``
from the context JSON for the manifest action). The root is the Git toplevel
of that directory when it is inside a work tree, else the directory itself.
``init`` writes a commented starter (``starter_files``) or copies a shipped
example (``example_files``), and never overwrites a file unless ``--force``.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass
from typing import List, Mapping, Optional, Tuple

from herdr_fleet import PLUGIN_ROOT
from herdr_fleet.context import UsageError, context_cwd, parse_context
from herdr_fleet.kickoff import DEFAULT_TEMPLATE

EXAMPLES_DIR = os.path.join(PLUGIN_ROOT, "examples")
SKIP_NAMES = (".DS_Store", "fleet.state.json")  # never copied from an example
NAME_SUFFIX = "-dev"

Files = List[Tuple[str, bytes]]  # (path relative to the root, content)

STARTER_FLEET = '''# .herdr/fleet.toml: the agent team for this repository, read by herdr-fleet.
# Written by `herdr-fleet init`; see "The fleet file" in the herdr-fleet README.
# Check it with `herdr-fleet doctor`, then start the team with `herdr-fleet up`.
# Every commented line is optional.

# workspace = "__BASE__"         # reuse the workspace with this label, else create it.
#                                # Without it, seats open beside the pane you run herdr-fleet from.
# cwd = "."                      # default agent cwd, relative to the repository root
state_note = ".herdr/STATE.md"   # its contents fill {state} in every kickoff; keep it current
kickoff = ".herdr/kickoff/default.md"   # default kickoff template for every seat
# quit_timeout_ms = 20000        # how long to wait for an agent to exit after its quit command

# [layout]
# direction = "right"            # split direction between consecutive seats: right | down
# ratio = 0.5                    # split ratio, strictly between 0 and 1

[[agents]]
__NAME_LINE__ # herdr agent name: [a-z][a-z0-9_-]{0,31}, unique in this file
kind = "claude"                  # a kind from `herdr agent start --help`
# args = ["--model", "..."]      # passed after `--` to herdr agent start
# cwd = "."                      # relative to the repository root, absolute, or ~/...
# split = "right"                # overrides [layout].direction for this seat
# ratio = 0.5                    # overrides [layout].ratio for this seat
# env = { ROLE = "dev" }         # environment for the seat's pane
# kickoff = ".herdr/kickoff/dev.md"   # this seat's own template, instead of the default
# kickoff_text = "..."           # or an inline template (not both)
# quit = "/exit"                 # overrides the per-kind quit command
# notes = "..."                  # shown by `herdr-fleet status` only

# Add seats with more [[agents]] tables. Seats start in file order and are
# briefed in file order once all are up: put an orchestrator last to brief it last.
'''

KICKOFF_COMMENT = ("<!-- herdr-fleet kickoff template. Lines like this one are not sent. Placeholders: "
                   "{name} {kind} {args} {repo} {cwd} {workspace} {roster} {state} {note} {reason}; "
                   "{{ and }} are literal braces. See \"Kickoff templates\" in the herdr-fleet README. -->\n")

STATE_NOTE = "Where things stand: (nothing yet).\n"


@dataclass
class FileResult:
    path: str     # relative to the root
    status: str   # created | kept | overwritten | failed
    detail: str = ""


def target_dir(directory: Optional[str], env: Mapping[str, str], cwd: str) -> str:
    """The directory to initialise (design 13.1, "Target")."""
    if directory:
        path = os.path.abspath(os.path.join(cwd, os.path.expanduser(directory)))
    elif env.get("HERDR_PLUGIN_ACTION_ID"):
        path = context_cwd(parse_context(env.get("HERDR_PLUGIN_CONTEXT_JSON")))
        if not path:
            raise UsageError("cannot tell the focused pane's directory (no focused_pane_cwd in "
                             "HERDR_PLUGIN_CONTEXT_JSON); run `herdr-fleet init DIR` instead")
    else:
        path = cwd
    if not os.path.isdir(path):
        raise UsageError("not a directory: %s" % path)
    return os.path.abspath(path)


def find_root(directory: str) -> Tuple[str, bool]:
    """(root, inside a Git work tree): ``git rev-parse --show-toplevel`` or the directory."""
    try:
        proc = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=directory, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    except OSError:
        return directory, False
    top = proc.stdout.strip()
    if proc.returncode == 0 and top and os.path.isdir(top):
        return os.path.normpath(top), True
    return directory, False


def name_base(root: str) -> str:
    """The root's basename reduced to the herdr name pattern, leaving room for ``-dev``."""
    raw = unicodedata.normalize("NFKD", os.path.basename(os.path.normpath(root)))
    base = raw.encode("ascii", "ignore").decode("ascii").lower()
    base = re.sub(r"[^a-z0-9_-]+", "-", base)
    base = re.sub(r"-{2,}", "-", base)
    base = re.sub(r"^[^a-z]+", "", base)  # names start with a letter
    base = base[:32 - len(NAME_SUFFIX)].rstrip("-_")
    return base or "fleet"


def seat_name(root: str) -> str:
    """``<sanitised root basename>-dev``; always matches [a-z][a-z0-9_-]{0,31}."""
    return name_base(root) + NAME_SUFFIX


def starter_files(root: str) -> Files:
    name_line = ('name = "%s"' % seat_name(root)).ljust(32)
    fleet = STARTER_FLEET.replace("__NAME_LINE__", name_line).replace("__BASE__", name_base(root))
    return [
        (".herdr/fleet.toml", fleet.encode("utf-8")),
        (".herdr/kickoff/default.md", (KICKOFF_COMMENT + DEFAULT_TEMPLATE).encode("utf-8")),
        (".herdr/STATE.md", STATE_NOTE.encode("utf-8")),
        (".herdr/.gitignore", b"fleet.state.json\n"),
    ]


def available_examples() -> List[str]:
    if not os.path.isdir(EXAMPLES_DIR):
        return []
    return sorted(d for d in os.listdir(EXAMPLES_DIR) if os.path.isdir(os.path.join(EXAMPLES_DIR, d)))


def example_files(name: str) -> Files:
    """Every file of ``examples/<name>``, paths as shipped (minus SKIP_NAMES)."""
    if name not in available_examples():
        raise UsageError("unknown example %r; available: %s" % (name, ", ".join(available_examples()) or "none"))
    base = os.path.join(EXAMPLES_DIR, name)
    files: Files = []
    for folder, dirs, names in os.walk(base):
        dirs.sort()
        for filename in sorted(names):
            if filename in SKIP_NAMES:
                continue
            path = os.path.join(folder, filename)
            with open(path, "rb") as handle:
                files.append((os.path.relpath(path, base).replace(os.sep, "/"), handle.read()))
    return files


def write_files(root: str, files: Files, force: bool) -> List[FileResult]:
    """Write each file unless it exists (``--force`` overwrites). Never raises for I/O."""
    results = []
    for rel, content in files:
        path = os.path.join(root, *rel.split("/"))
        existed = os.path.lexists(path)
        if existed and not force:
            results.append(FileResult(rel, "kept"))
            continue
        try:
            _write(path, content)
        except OSError as exc:
            results.append(FileResult(rel, "failed", str(exc)))
            continue
        results.append(FileResult(rel, "overwritten" if existed else "created"))
    return results


def _write(path: str, content: bytes) -> None:
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".init.", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def run_init(directory: Optional[str], example: Optional[str], force: bool, env: Mapping[str, str],
             cwd: str) -> Tuple[str, bool, List[FileResult]]:
    """Resolve the root and write the files. Raises UsageError for bad arguments."""
    files = example_files(example) if example else None
    root, in_git = find_root(target_dir(directory, env, cwd))
    return root, in_git, write_files(root, files if files is not None else starter_files(root), force)
