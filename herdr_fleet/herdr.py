"""Herdr client: the only module that executes the herdr binary.

``Herdr.run(*argv)`` runs ``<herdr> argv...`` and returns the ``result``
object of the JSON that herdr prints on stdout. A failure becomes
``HerdrError(code, message)``, taken from the ``{"error": {...}}`` JSON that
herdr prints on stderr (exit 1), or synthesised for usage errors (exit 2),
unparseable output and a missing binary.

The client refuses, before spawning anything, the commands this plugin must
never send (design section 7): bare ``herdr`` (it attaches the TUI),
``agent send-keys``, ``pane send-keys``, ``pane send-text`` and ``pane run``.
Text reaches agents only through ``agent prompt``. The two display commands,
``pane rename`` (a seat pane's border name, design 22.1) and ``agent focus``
(``go``, 22.2), send nothing to the agent (invariant 20).
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
from typing import Callable, Dict, List, Optional, Sequence, Tuple

Runner = Callable[[List[str]], Tuple[int, str, str]]

FORBIDDEN = (
    ("agent", "send-keys"),
    ("pane", "send-keys"),
    ("pane", "send-text"),
    ("pane", "run"),
)


class HerdrError(Exception):
    """A herdr command failed. ``code`` is herdr's ``error.code`` when it gave one."""

    def __init__(self, code: str, message: str, argv: Optional[Sequence[str]] = None):
        super().__init__("%s: %s" % (code, message))
        self.code = code
        self.message = message
        self.argv = list(argv or [])


def subprocess_runner(argv: List[str]) -> Tuple[int, str, str]:
    """Run argv without a shell or stdin and return (exit code, stdout, stderr)."""
    proc = subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def find_binary(env: Dict[str, str]) -> str:
    """HERDR_BIN_PATH when set, else ``herdr`` on PATH."""
    path = env.get("HERDR_BIN_PATH")
    if path:
        return path
    return shutil.which("herdr") or "herdr"


def parse_version(text: str) -> Optional[Tuple[int, int, int]]:
    """``herdr 0.9.4`` -> (0, 9, 4); None when no version is present."""
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def parse_integration_status(text: str) -> List[Dict[str, str]]:
    """Parse ``herdr integration status`` lines such as
    ``pi: outdated (v8 < v9) (/path)`` into {kind, status, line} dicts.
    ``status`` is ``current``, ``outdated``, ``not installed`` or the raw text."""
    rows = []
    for line in (text or "").splitlines():
        line = line.strip()
        match = re.match(r"^([A-Za-z0-9_.-]+)(?: \([^)]*\))?:\s*(.*)$", line)
        if not match:
            continue
        detail = match.group(2)
        status = detail
        for known in ("current", "outdated", "not installed"):
            if detail.startswith(known):
                status = known
                break
        rows.append({"kind": match.group(1), "status": status, "line": line})
    return rows


def has_agent(pane: Dict) -> bool:
    """True when a pane record (from pane get/list) shows a recognised agent."""
    return bool(pane.get("agent"))


class Herdr:
    """Thin typed wrapper over the herdr CLI. See the module docstring."""

    def __init__(self, binary: str, runner: Optional[Runner] = None):
        self.binary = binary
        self.runner = runner or subprocess_runner

    # -- transport -------------------------------------------------------

    def _exec(self, argv: Sequence[str]) -> Tuple[int, str, str]:
        argv = [str(a) for a in argv]
        if not argv:
            raise HerdrError("forbidden_command", "refusing to run bare herdr (it attaches the TUI)")
        if tuple(argv[:2]) in FORBIDDEN:
            raise HerdrError(
                "forbidden_command",
                "herdr-fleet never runs `herdr %s`; text reaches agents only through `agent prompt`"
                % " ".join(argv[:2]),
                argv,
            )
        try:
            return self.runner([self.binary] + argv)
        except OSError as exc:
            raise HerdrError("herdr_not_found", "cannot run %s: %s" % (self.binary, exc), argv)

    def run(self, *argv: str) -> Dict:
        """Run a JSON command; return its ``result`` dict or raise HerdrError."""
        code, out, err = self._exec(argv)
        if code != 0:
            raise _error_from(code, out, err, argv)
        try:
            doc = json.loads(out) if out.strip() else {}
        except ValueError:
            raise HerdrError("bad_output", "herdr printed non-JSON output: %s" % _first_line(out), argv)
        if isinstance(doc, dict) and isinstance(doc.get("error"), dict):
            raise _error_from(1, "", out, argv)
        result = doc.get("result") if isinstance(doc, dict) else None
        return result if isinstance(result, dict) else {}

    def text(self, *argv: str) -> str:
        """Run a plain-text command (``--version``, ``integration status``)."""
        code, out, err = self._exec(argv)
        if code != 0:
            raise _error_from(code, out, err, argv)
        return out

    # -- discovery -------------------------------------------------------

    def version(self) -> Optional[Tuple[int, int, int]]:
        return parse_version(self.text("--version"))

    def kinds(self) -> Optional[List[str]]:
        """Agent kinds the installed herdr supports, from the ``herdr agent``
        usage text (printed on stderr with exit 2), else from
        ``herdr agent start --help``. None when neither can be parsed."""
        code, out, err = self._exec(["agent"])
        match = re.search(r"kinds:\s*([A-Za-z0-9_|-]+)", out + "\n" + err)
        if match:
            return [k for k in match.group(1).split("|") if k]
        code, out, err = self._exec(["agent", "start", "--help"])
        match = re.search(r"\[possible values: ([^\]]+)\]", out + "\n" + err)
        if match:
            return [k.strip() for k in match.group(1).split(",") if k.strip()]
        return None

    def integration_status(self) -> List[Dict[str, str]]:
        return parse_integration_status(self.text("integration", "status"))

    def plugin_config_dir(self, plugin_id: str) -> Optional[str]:
        """The plugin's config directory, or None when herdr cannot say."""
        code, out, err = self._exec(["plugin", "config-dir", plugin_id])
        if code != 0:
            return None
        text = out.strip()
        try:
            doc = json.loads(text)
        except ValueError:
            return text.splitlines()[0] if text else None
        result = doc.get("result", doc) if isinstance(doc, dict) else {}
        for key in ("config_dir", "path", "dir"):
            if isinstance(result, dict) and result.get(key):
                return str(result[key])
        return None

    # -- agents ----------------------------------------------------------

    def agent_list(self) -> List[Dict]:
        return list(self.run("agent", "list").get("agents") or [])

    def agent_get(self, target: str) -> Dict:
        return dict(self.run("agent", "get", target).get("agent") or {})

    def pane_agent(self, pane_id: str) -> Dict:
        """The agent record hosted by a pane (it carries ``name``; pane records
        do not), or {} when the pane hosts no agent or does not exist."""
        try:
            return self.agent_get(pane_id)
        except HerdrError:
            return {}

    def agent_start(self, name: str, kind: str, pane: str, args: Sequence[str],
                    timeout_ms: Optional[int] = None) -> Dict:
        argv = start_argv(name, kind, pane, args, timeout_ms)
        return dict(self.run(*argv).get("agent") or {})

    def agent_prompt(self, target: str, text: str) -> Dict:
        return self.run("agent", "prompt", target, text)

    def agent_wait(self, target: str, until: Sequence[str], timeout_ms: int) -> Dict:
        argv = ["agent", "wait", target]
        for status in until:
            argv += ["--until", status]
        argv += ["--timeout", str(int(timeout_ms))]
        return dict(self.run(*argv).get("agent") or {})

    def agent_rename(self, target: str, name: str) -> Dict:
        return self.run("agent", "rename", target, name)

    def agent_focus(self, target: str) -> Dict:
        """Move the owner's UI focus to the pane of ``target`` (a live agent name or a pane
        id). Only ``go`` and the popup's ``go`` run it (design 22.2, invariant 20); the
        answer is opaque to the plugin."""
        return self.run("agent", "focus", target)

    # -- panes and workspaces --------------------------------------------

    def pane_get(self, pane_id: str) -> Dict:
        result = self.run("pane", "get", pane_id)
        return dict(result.get("pane") or result)

    def pane_current(self) -> Dict:
        """The caller's pane as herdr sees it now (``pane current --current``). After a
        pane move the inherited HERDR_PANE_ID is stale but still resolves here."""
        result = self.run("pane", "current", "--current")
        return dict(result.get("pane") or result)

    def pane_rename(self, pane_id: str, name: str) -> Dict:
        """Set a pane's display name, shown in its border (``pane rename PANE NAME``; design
        22.1). Only a seat's pane, only to that seat's name or ``label`` (invariant 20)."""
        result = self.run("pane", "rename", pane_id, name)
        return dict(result.get("pane") or result)

    def pane_clear_name(self, pane_id: str) -> Dict:
        """Remove a pane's display name (``pane rename PANE --clear``): ``down`` does it for
        a seat pane it leaves open at a shell prompt (design 22.1)."""
        result = self.run("pane", "rename", pane_id, "--clear")
        return dict(result.get("pane") or result)

    def pane_list(self, workspace_id: str) -> List[Dict]:
        return list(self.run("pane", "list", "--workspace", workspace_id).get("panes") or [])

    def pane_read(self, pane_id: str, lines: int) -> str:
        """The last ``lines`` lines of a pane's screen (``pane read PANE --lines N``), which
        herdr prints as plain text even in JSON mode. Only the presence guard reads them
        (design 21.2, invariant 19): for the prompt line alone, never kept."""
        return self.text("pane", "read", pane_id, "--lines", str(int(lines)))

    def process_info(self, pane_id: str) -> Dict:
        return dict(self.run("pane", "process-info", "--pane", pane_id).get("process_info") or {})

    def pane_split(self, from_pane: str, direction: str, ratio: Optional[float] = None,
                   cwd: Optional[str] = None, env: Optional[Dict[str, str]] = None) -> str:
        argv = split_argv(from_pane, direction, ratio, cwd, env)
        pane = self.run(*argv).get("pane") or {}
        if not pane.get("pane_id"):
            raise HerdrError("bad_output", "pane split returned no pane id", argv)
        return str(pane["pane_id"])

    def pane_move_to_tab(self, pane_id: str, tab_id: str, direction: str = "right", ratio: Optional[float] = 0.5,
                         target_pane: Optional[str] = None) -> str:
        """Move a pane (and its running process) into a tab, split beside
        ``target_pane`` (else herdr's choice); return its id afterwards. Ids are
        workspace-qualified: a move to another workspace changes the id. Moving a
        pane to the tab it is already in is a no-op in herdr 0.9.4."""
        return self._moved(pane_move_argv(pane_id, tab_id, direction, ratio, target_pane))

    def pane_move_new_tab(self, pane_id: str, workspace_id: str, label: str) -> str:
        """Move a pane into a new tab of a workspace (``tile`` parks panes this
        way); the emptied tab disappears when its pane moves on."""
        return self._moved(pane_park_argv(pane_id, workspace_id, label))

    def _moved(self, argv: List[str]) -> str:
        moved = (self.run(*argv).get("move_result") or {}).get("pane") or {}
        if not moved.get("pane_id"):
            raise HerdrError("bad_output", "pane move returned no pane id", argv)
        return str(moved["pane_id"])

    def tab_list(self, workspace_id: str) -> List[Dict]:
        return list(self.run("tab", "list", "--workspace", workspace_id).get("tabs") or [])

    def pane_close(self, pane_id: str) -> Dict:
        return self.run("pane", "close", pane_id)

    def workspace_list(self) -> List[Dict]:
        return list(self.run("workspace", "list").get("workspaces") or [])

    def workspace_get(self, workspace_id: str) -> Dict:
        result = self.run("workspace", "get", workspace_id)
        return dict(result.get("workspace") or result)

    def workspace_create(self, cwd: str, label: str, env: Optional[Dict[str, str]] = None) -> Tuple[str, str]:
        """Create a workspace without focusing it; return (workspace id, root pane id)."""
        argv = workspace_create_argv(cwd, label, env)
        result = self.run(*argv)
        ws = (result.get("workspace") or {}).get("workspace_id")
        root = (result.get("root_pane") or {}).get("pane_id")
        if not ws or not root:
            raise HerdrError("bad_output", "workspace create returned no workspace or root pane id", argv)
        return str(ws), str(root)

    def workspace_close(self, workspace_id: str) -> Dict:
        return self.run("workspace", "close", workspace_id)

    # -- UI ----------------------------------------------------------------

    def notification_show(self, title: str, body: str, sound: str) -> Dict:
        return self.run("notification", "show", title, "--body", body, "--sound", sound)

    def plugin_pane_open(self, plugin_id: str, entrypoint: str, cwd: str, placement: Optional[str] = None) -> Dict:
        argv = ["plugin", "pane", "open", "--plugin", plugin_id, "--entrypoint", entrypoint, "--cwd", cwd]
        if placement:
            argv += ["--placement", placement]
        return self.run(*argv)

    def plugin_split_open(self, plugin_id: str, entrypoint: str, target_pane: str, direction: str, cwd: str,
                          env: Optional[Dict[str, str]] = None) -> Dict:
        """Open a plugin pane as a split of ``target_pane`` without moving focus (design 19);
        return the new pane's record (``result.plugin_pane.pane``)."""
        argv = plugin_split_argv(plugin_id, entrypoint, target_pane, direction, cwd, env)
        pane = (self.run(*argv).get("plugin_pane") or {}).get("pane") or {}
        if not pane.get("pane_id"):
            raise HerdrError("bad_output", "plugin pane open returned no pane id", argv)
        return dict(pane)

    def pane_layout(self, pane_id: str) -> Dict:
        """The layout snapshot of the tab holding ``pane_id`` (``result.layout``: ``area``,
        ``panes`` with ``rect``, ``splits`` with ``direction``, ``ratio`` and ``rect``)."""
        return dict(self.run("pane", "layout", "--pane", pane_id).get("layout") or {})

    def pane_resize(self, pane_id: str, direction: str, amount: float) -> Dict:
        """Move the nearest split on ``pane_id``'s edge (``result.resize``, whose ``layout``
        is the snapshot afterwards). herdr adds ``amount`` to that split's first-child
        ratio for right and down, subtracts it for left and up (design 19.4)."""
        return dict(self.run(*pane_resize_argv(pane_id, direction, amount)).get("resize") or {})


def render_cmd(argv: Sequence[str]) -> str:
    """A herdr argv as a copy-pasteable shell command (for plans and messages)."""
    return "herdr " + " ".join(shlex.quote(str(a)) for a in argv)


def start_argv(name: str, kind: str, pane: str, args: Sequence[str],
               timeout_ms: Optional[int] = None) -> List[str]:
    """The exact ``agent start`` argv (without the binary); also used by --dry-run."""
    argv = ["agent", "start", name, "--kind", kind, "--pane", pane]
    if timeout_ms:
        argv += ["--timeout", str(int(timeout_ms))]
    if args:
        argv += ["--"] + [str(a) for a in args]
    return argv


def pane_move_argv(pane_id: str, tab_id: str, direction: str = "right", ratio: Optional[float] = 0.5,
                   target_pane: Optional[str] = None) -> List[str]:
    """The exact ``pane move ... --tab`` argv (``up --join``, ``tile``; design section 15)."""
    argv = ["pane", "move", pane_id, "--tab", tab_id, "--split", direction]
    if target_pane:
        argv += ["--target-pane", target_pane]
    if ratio is not None:
        argv += ["--ratio", str(ratio)]
    return argv + ["--no-focus"]


def plugin_split_argv(plugin_id: str, entrypoint: str, target_pane: str, direction: str, cwd: str,
                      env: Optional[Dict[str, str]] = None) -> List[str]:
    """The exact ``plugin pane open --placement split`` argv ``up`` uses for the watcher
    strip (design 19.1). ``--no-focus``: herdr's CLI focuses the new pane by default."""
    argv = ["plugin", "pane", "open", "--plugin", plugin_id, "--entrypoint", entrypoint, "--placement", "split",
            "--target-pane", target_pane, "--direction", direction, "--cwd", cwd]
    for key in sorted(env or {}):
        argv += ["--env", "%s=%s" % (key, env[key])]
    return argv + ["--no-focus"]


def pane_resize_argv(pane_id: str, direction: str, amount: float) -> List[str]:
    return ["pane", "resize", "--pane", pane_id, "--direction", direction, "--amount", str(amount)]


def pane_park_argv(pane_id: str, workspace_id: str, label: str) -> List[str]:
    """The exact ``pane move ... --new-tab`` argv ``tile`` uses to park a pane."""
    return ["pane", "move", pane_id, "--new-tab", "--workspace", workspace_id, "--label", label, "--no-focus"]


def workspace_create_argv(cwd: str, label: str, env: Optional[Dict[str, str]] = None) -> List[str]:
    """The exact ``workspace create`` argv; ``env`` applies to the root pane's shell."""
    argv = ["workspace", "create", "--cwd", cwd, "--label", label]
    for key in sorted(env or {}):
        argv += ["--env", "%s=%s" % (key, env[key])]
    argv.append("--no-focus")
    return argv


def split_argv(from_pane: str, direction: str, ratio: Optional[float] = None,
               cwd: Optional[str] = None, env: Optional[Dict[str, str]] = None) -> List[str]:
    """The exact ``pane split`` argv (without the binary); also used by --dry-run."""
    argv = ["pane", "split", from_pane, "--direction", direction]
    if ratio is not None:
        argv += ["--ratio", str(ratio)]
    if cwd:
        argv += ["--cwd", cwd]
    for key in sorted(env or {}):
        argv += ["--env", "%s=%s" % (key, env[key])]
    argv.append("--no-focus")
    return argv


def _first_line(text: str) -> str:
    text = (text or "").strip()
    return text.splitlines()[0] if text else "(empty)"


def _error_from(code: int, out: str, err: str, argv: Sequence[str]) -> HerdrError:
    for stream in (err, out):
        try:
            doc = json.loads(stream)
        except (ValueError, TypeError):
            continue
        if isinstance(doc, dict) and isinstance(doc.get("error"), dict):
            error = doc["error"]
            return HerdrError(str(error.get("code") or "error"), str(error.get("message") or ""), argv)
    if code == 2:
        return HerdrError("usage", "herdr rejected the command line: %s" % _first_line(err or out), argv)
    return HerdrError("exit_%d" % code, _first_line(err or out), argv)


def describe(exc: HerdrError) -> str:
    """One-line rendering of a herdr failure, always naming its code."""
    return "herdr error %s: %s" % (exc.code, exc.message)
