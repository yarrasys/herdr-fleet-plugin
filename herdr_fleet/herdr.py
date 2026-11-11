"""Herdr client: the only module that executes the herdr binary.

``Herdr.run(*argv)`` runs ``<herdr> argv...`` and returns the ``result``
object of the JSON that herdr prints on stdout. A failure becomes
``HerdrError(code, message)``, taken from the ``{"error": {...}}`` JSON that
herdr prints on stderr (exit 1), or synthesised for usage errors (exit 2),
unparseable output and a missing binary.

The client refuses, before spawning anything, the commands this plugin must
never send (design section 7): bare ``herdr`` (it attaches the TUI),
``agent send-keys``, ``pane send-keys``, ``pane send-text`` and ``pane run``.
Text reaches agents only through ``agent prompt``. The one exception is
``agent_send_keys`` (design 23.7, invariants 1 and 21): the only way
``agent send-keys`` is ever run, and only with the keys in ``SENDABLE_KEYS``;
``run`` and ``text`` keep refusing it. The two display commands, ``pane
rename`` (a seat pane's border name, design 22.1) and ``agent focus`` (``go``,
22.2), send nothing to the agent (invariant 20).
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import os
import selectors
import time
import copy
import contextlib
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from herdr_fleet import permits
from herdr_fleet.native_commands import classify

Runner = Callable[[List[str]], Tuple[int, str, str]]

FORBIDDEN = (
    ("agent", "attach"),
    ("agent", "send-keys"),
    ("pane", "send-keys"),
    ("pane", "send-text"),
    ("pane", "run"),
)
SEND_KEYS = ("agent", "send-keys")  # accepted only from ``Herdr.agent_send_keys`` (design 23.7)
# Design 23.7 and 23.11: the only keys the plugin may ever press in an agent's pane. `esc` is
# herdr's canonical Escape name; Enter answers the exit confirmation; Ctrl-C interrupts a
# turn; Ctrl-U clears the prompt an interrupt put back into the input (`interrupt` only).
SENDABLE_KEYS = ("enter", "esc", "ctrl+c", "ctrl+u")


class HerdrError(Exception):
    """A herdr command failed. ``code`` is herdr's ``error.code`` when it gave one."""

    def __init__(self, code: str, message: str, argv: Optional[Sequence[str]] = None):
        super().__init__("%s: %s" % (code, message))
        self.code = code
        self.message = message
        self.argv = list(argv or [])


def subprocess_runner(argv: List[str]) -> Tuple[int, str, str]:
    """Run argv without a shell or stdin and return (exit code, stdout, stderr)."""
    context = permits.native_transport()
    proc = subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        env=context[0] if context else None, pass_fds=context[1] if context else (),
    )
    return proc.returncode, proc.stdout, proc.stderr


def dashboard_runner(argv: List[str]) -> Tuple[int, str, str]:
    """Dashboard-owned ordinary reads/actions have a finite native-command lifetime."""
    try:
        context = permits.native_transport()
        proc = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True, timeout=5,
                              env=context[0] if context else None, pass_fds=context[1] if context else ())
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        raise HerdrError("timeout", "Herdr request timed out; action delivery may be unconfirmed")


def registry_runner(argv: List[str]) -> Tuple[int, str, str]:
    """One bounded metadata read: no stdin, at most 1 MiB and five seconds."""
    context = permits.native_transport()
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               env=context[0] if context else None, pass_fds=context[1] if context else ())
    selector, chunks, size = selectors.DefaultSelector(), [], 0
    end = time.monotonic() + 5.0
    try:
        selector.register(process.stdout, selectors.EVENT_READ)
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise HerdrError("registry_unavailable", "Plugin registry timed out")
            chunk = os.read(process.stdout.fileno(), 8192)
            if not chunk:
                break
            size += len(chunk)
            if size > 1048576:
                raise HerdrError("registry_unavailable", "Plugin registry exceeds observation limit")
            chunks.append(chunk)
        code = process.wait(timeout=max(0.01, end - time.monotonic()))
        return code, b"".join(chunks).decode("utf-8"), ""
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=1)
        process.stdout.close()


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

    def __init__(self, binary: str, runner: Optional[Runner] = None, stream_runner=None):
        self.binary = binary
        self.runner = runner or subprocess_runner
        self.stream_runner = stream_runner or subprocess.Popen
        self.start_timeout = True  # False once herdr rejected `agent start --timeout` (design 32.1, review 6)
        self.env = dict(os.environ)
        self._domain = None
        self.permit = None
        self._agents, self._panes = {}, {}
        self._workspaces = set()

    def configure(self, env):
        snapshot = dict(env)
        if snapshot != self.env:
            self.env, self._domain = snapshot, None

    def domain(self):
        if self._domain is None:
            try:
                candidates = permits.endpoints(self.env)
                with permits.transport(self.env):
                    runner = registry_runner if self.runner in (subprocess_runner, dashboard_runner) else self.runner
                    code, out, err = runner([self.binary, "status", "server", "--json"])
                if code or len(out.encode("utf-8")) > 1048576:
                    raise permits.Unavailable("native endpoint status unavailable")
                status = json.loads(out)
                self._domain = permits.selected(candidates, status)
                self._server_running = status["running"]
            except (ValueError, OSError, permits.Unavailable) as exc:
                raise HerdrError("permit_unavailable", "coordination endpoint unavailable: %s" % exc)
        return self._domain

    def control_client(self):
        """Fresh positively running endpoint/client evidence; no mutations."""
        client = copy.copy(self)
        if client.runner in (subprocess_runner, dashboard_runner):
            client.runner = registry_runner  # Fixed classified reads, bounded bytes/time.
        client._domain, client.permit = None, None
        domain = client.domain()
        if not client._server_running:
            raise HerdrError("control_unavailable", "Native server is not positively running")
        domain.frozen(client.env, client_protocol=True)
        return client

    @contextlib.contextmanager
    def operation(self, values, topology=False):
        """Explicit bound native client for one complete semantic interval."""
        requested = permits.resources(values)
        try:
            if self.permit is not None:
                if not self.permit.covers(requested, topology):
                    raise permits.Unavailable("nested operation exceeds its covering permit")
                yield self
                return
            domain = self.domain()
            growth = 2 * sum(k == "N" for k, _ in requested) + 3 if topology else 0
            with permits.Store().acquire([domain], requested, topology=topology, growth=growth) as permit:
                bound = copy.copy(self)
                bound.permit = permit
                yield bound
        except permits.Held as exc:
            raise HerdrError("owner_control", str(exc))
        except permits.Unavailable as exc:
            raise HerdrError("permit_unavailable", str(exc))

    def agent_resources(self, agent):
        if not all(isinstance(agent.get(k), str) and agent[k] for k in ("pane_id", "terminal_id", "workspace_id")):
            raise HerdrError("permit_unavailable", "native target lacks exact pane/terminal/workspace identity")
        values = [("W", agent["workspace_id"]), ("P", agent["pane_id"]), ("T", agent["terminal_id"])]
        if agent.get("name"):
            values.append(("N", agent["name"]))
        return values

    @contextlib.contextmanager
    def target_operation(self, target):
        if self.permit is not None:
            agent = self._agents.get(target)
            if agent is None:
                raise HerdrError("agent_not_found", "no validated agent for this nested target")
            with self.operation(self.agent_resources(agent)) as bound:
                yield bound
            return
        records = self.agent_list()
        matches = [a for a in records if isinstance(a, dict) and target in (a.get("name"), a.get("pane_id"))]
        if len(matches) > 1:
            raise HerdrError("target_changed", "native target identity is ambiguous")
        agent = matches[0] if matches else None
        if agent is None:
            raise HerdrError("agent_not_found", "no live agent for this permit target")
        values = self.agent_resources(agent)
        with self.operation(values) as bound:
            records = bound.agent_list()
            matches = [a for a in records if isinstance(a, dict) and target in (a.get("name"), a.get("pane_id"))]
            current = matches[0] if len(matches) == 1 else {}
            if any(current.get(k) != agent.get(k) for k in ("name", "pane_id", "terminal_id", "workspace_id")):
                raise HerdrError("target_changed", "target changed while acquiring its permit")
            yield bound

    def _mutation_resources(self, argv):
        """Finite native mutation inventory. No unchecked run/text mutation fallback."""
        if argv == ["agent", "start", "--help"]:
            return None, False
        pair = tuple(argv[:2])
        if pair in (("agent", "prompt"), ("agent", "send-keys"), ("agent", "focus")):
            return self.agent_resources(self._known_agent(argv[2])), False
        if pair == ("agent", "start"):
            pane = argv[argv.index("--pane") + 1]
            info = self._known_pane(pane)
            return [("N", argv[2])] + self.agent_resources(dict(info, name=None)), False
        if pair == ("agent", "rename"):
            record = self._known_agent(argv[2])
            return self.agent_resources(record) + [("N", argv[3])], False
        if pair == ("workspace", "create"):
            if self.permit is None or not any(k[0] == "N" for k in self.permit.requests):
                raise HerdrError("permit_unavailable", "workspace creation needs its reserved seat names")
            return [], False
        if pair == ("workspace", "close"):
            return [("W", argv[2])], True
        if pair[0] == "pane" and pair[1] in ("rename", "close", "split", "move", "resize", "zoom"):
            pane = argv[argv.index("--pane") + 1] if "--pane" in argv else argv[2]
            info = self._known_pane(pane)
            values = [("P", pane)]
            if info.get("workspace_id"):
                values.append(("W", info["workspace_id"]))
            if info.get("terminal_id"):
                values.append(("T", info["terminal_id"]))
            agent = self._agents.get(pane)
            if agent and agent.get("name") and info.get("agent"):
                values.append(("N", agent["name"]))
            topology = pair[1] in ("close", "split", "move", "resize", "zoom")
            if topology:
                if not info.get("workspace_id"):
                    raise HerdrError("permit_unavailable", "topology target has no workspace")
                if "--workspace" in argv:
                    values.append(("W", argv[argv.index("--workspace") + 1]))
                if "--tab" in argv:
                    values.append(("W", argv[argv.index("--tab") + 1].split(":", 1)[0]))
                if "--target-pane" in argv:
                    neighbor = self._known_pane(argv[argv.index("--target-pane") + 1])
                    values.extend(self.agent_resources(dict(neighbor, name=None)))
            return values, topology
        if tuple(argv[:3]) == ("plugin", "pane", "open"):
            if "--target-pane" in argv:
                info = self._known_pane(argv[argv.index("--target-pane") + 1])
                return [("W", info["workspace_id"])], True
            if "--workspace" in argv:
                return [("W", argv[argv.index("--workspace") + 1])], True
            current = self.pane_current()
            if not current.get("workspace_id"):
                raise HerdrError("permit_unavailable", "plugin placement needs an exact workspace")
            return [("W", current["workspace_id"])], True
        return None, False

    def _known_agent(self, target):
        record = self._agents.get(target)
        if not record:
            raise HerdrError("permit_unavailable", "mutation lacks a validated native agent mapping")
        return record

    def _known_pane(self, target):
        record = self._panes.get(target)
        if not record:
            raise HerdrError("permit_unavailable", "mutation lacks a validated native pane mapping")
        return record

    # -- transport -------------------------------------------------------

    def _exec(self, argv: Sequence[str], keys: bool = False) -> Tuple[int, str, str]:
        argv = [str(a) for a in argv]
        if not argv:
            raise HerdrError("forbidden_command", "refusing to run bare herdr (it attaches the TUI)")
        prefix = argv[:argv.index("--")] if "--" in argv else argv
        if argv[0] == "session" or any(a == "--session" or a.startswith("--session=") for a in prefix):
            raise HerdrError("forbidden_command", "session selection cannot override a frozen coordination endpoint", argv)
        if argv[0] == "terminal":
            raise HerdrError("forbidden_command", "terminal access is only through validated read-only observe", argv)
        if tuple(argv[:2]) in FORBIDDEN and not (keys and tuple(argv[:2]) == SEND_KEYS):
            raise HerdrError(
                "forbidden_command",
                "herdr-fleet runs `herdr agent send-keys` only through Herdr.agent_send_keys (design 23.7)"
                if tuple(argv[:2]) == SEND_KEYS else
                "herdr-fleet never runs `herdr %s`; text reaches agents only through `agent prompt`"
                % " ".join(argv[:2]),
                argv,
            )
        category = classify(argv, self.env.get("HERDR_PLUGIN_ID") or "yarrasys.fleet")
        if category is None:
            raise HerdrError("forbidden_command", "unsupported or malformed native command", argv)
        try:
            required, topology = self._mutation_resources(argv)
            if category == "mutation" and required is None:
                raise HerdrError("forbidden_command", "mutation has no resource classifier", argv)
            descriptors, evidence = (), None
            env = self.env
            if required is not None:
                if self.permit is None or not self.permit.covers(required, topology):
                    raise HerdrError("permit_unavailable", "native mutation has no covering semantic permit", argv)
                # A creation child has no W until its response; only its reserved
                # name descriptors exist at submission. Other children inherit
                # exactly their own target/topology resources, not the whole plan.
                child_resources = required or [key for key in self.permit.requests if key[0] == "N"]
                descriptors = self.permit.descriptors(child_resources)
                evidence = {"resources": [list(r) for r in required], "topology": topology,
                            "pid": os.getpid(), "thread": self.permit._owner[1].ident, "fds": list(descriptors),
                            "claims": [[k, v, "exclusive" if self.permit.requests[(k, v)] == permits.fcntl.LOCK_EX else "shared"]
                                       for k, v in child_resources]}
                env = self.domain().frozen(self.env)
            elif self._domain is not None:
                env = self._domain.frozen(self.env)
                if self.permit is not None:
                    descriptors = self.permit.descriptors()
            with permits.transport(env, descriptors, evidence):
                return self.runner([self.binary] + argv)
        except permits.Unavailable as exc:
            raise HerdrError("permit_unavailable", str(exc), argv)
        except OSError as exc:
            raise HerdrError("herdr_not_found", "cannot run %s: %s" % (self.binary, exc), argv)

    def run(self, *argv: str) -> Dict:
        """Run a JSON command; return its ``result`` dict or raise HerdrError."""
        return self._json(argv, self._exec(argv))

    def _json(self, argv: Sequence[str], answer: Tuple[int, str, str]) -> Dict:
        code, out, err = answer
        if code != 0:
            raise _error_from(code, out, err, argv)
        try:
            doc = json.loads(out) if out.strip() else {}
        except ValueError:
            raise HerdrError("bad_output", "herdr printed non-JSON output: %s" % _first_line(out), argv)
        if isinstance(doc, dict) and isinstance(doc.get("error"), dict):
            raise _error_from(1, "", out, argv)
        result = doc.get("result") if isinstance(doc, dict) else None
        previous_panes = set(self._panes)
        previous_workspaces = set(self._workspaces)
        if isinstance(result, dict):
            for workspace in result.get("workspaces", []) if isinstance(result.get("workspaces"), list) else []:
                if isinstance(workspace, dict) and workspace.get("workspace_id"):
                    self._workspaces.add(workspace["workspace_id"])
            records = list(result.get("agents") or []) if isinstance(result.get("agents"), list) else []
            if isinstance(result.get("agents"), list):
                self._agents = {}
            if isinstance(result.get("agent"), dict):
                records.append(result["agent"])
            for agent in records:
                if not isinstance(agent, dict):
                    continue
                if agent.get("pane_id"):
                    self._agents[agent["pane_id"]] = agent
                    self._panes[agent["pane_id"]] = agent
                if agent.get("name"):
                    self._agents[agent["name"]] = agent
            for pane in list(result.get("panes") or []) if isinstance(result.get("panes"), list) else []:
                if isinstance(pane, dict) and pane.get("pane_id"):
                    self._panes[pane["pane_id"]] = pane
            if isinstance(result.get("pane"), dict) and result["pane"].get("pane_id"):
                self._panes[result["pane"]["pane_id"]] = result["pane"]
        if self.permit is not None and isinstance(result, dict):
            pair = tuple(argv[:2])
            panes = []
            if pair == ("workspace", "create"):
                workspace = (result.get("workspace") or {}).get("workspace_id")
                if not workspace:
                    raise HerdrError("bad_output", "created workspace has no native identity")
                if workspace in previous_workspaces:
                    raise HerdrError("bad_output", "workspace creation returned an existing identity")
                self.permit.created_workspace(workspace)
                self._workspaces.add(workspace)
                panes.append(result.get("root_pane") or {})
            elif pair == ("pane", "split"):
                panes.append(result.get("pane") or {})
            elif pair == ("pane", "move"):
                panes.append((result.get("move_result") or {}).get("pane") or {})
            elif tuple(argv[:3]) == ("plugin", "pane", "open"):
                panes.append((result.get("plugin_pane") or {}).get("pane") or {})
            for pane in panes:
                if not pane.get("pane_id") or not pane.get("workspace_id") or not pane.get("terminal_id"):
                    raise HerdrError("bad_output", "created/moved pane lacks native identity")
                if pane["pane_id"] in previous_panes and not (pair == ("pane", "move") and pane["pane_id"] == argv[2]):
                    raise HerdrError("bad_output", "creation/move returned an unrelated existing pane")
                expected = None
                if pair == ("workspace", "create"):
                    expected = workspace
                elif pair == ("pane", "split"):
                    expected = self._known_pane(argv[2]).get("workspace_id")
                elif "--workspace" in argv:
                    expected = argv[argv.index("--workspace") + 1]
                elif "--tab" in argv:
                    expected = argv[argv.index("--tab") + 1].split(":", 1)[0]
                elif "--target-pane" in argv:
                    expected = self._known_pane(argv[argv.index("--target-pane") + 1]).get("workspace_id")
                if expected is not None and pane["workspace_id"] != expected:
                    raise HerdrError("bad_output", "creation/move returned a different workspace")
                self.permit.derive(pane["workspace_id"], [("P", pane["pane_id"]), ("T", pane["terminal_id"])])
                if pair == ("pane", "move"):
                    carried = self._agents.pop(argv[2], None)
                    if carried is not None:
                        current_agent = dict(carried, **{k: v for k, v in pane.items() if k != "name"})
                        self._agents[pane["pane_id"]] = current_agent
                        if current_agent.get("name"):
                            self._agents[current_agent["name"]] = current_agent
                self._panes[pane["pane_id"]] = pane
        return result if isinstance(result, dict) else {}

    def text(self, *argv: str) -> str:
        """Run a plain-text command (``--version``, ``integration status``)."""
        code, out, err = self._exec(argv)
        if code != 0:
            raise _error_from(code, out, err, argv)
        return out

    # -- discovery -------------------------------------------------------

    def bounded_dashboard_client(self):
        client = copy.copy(self)
        client.runner = dashboard_runner if self.runner is subprocess_runner else self.runner
        return client

    def terminal_observe(self, target: str, cols: int, rows: int):
        """The sole streaming path; fixed observe argv and closed stdin (41.6)."""
        if (not isinstance(target, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", target)
                or type(cols) is not int or type(rows) is not int
                or not 20 <= cols <= 300 or not 5 <= rows <= 120):
            raise HerdrError("forbidden_command", "invalid observer target or viewport")
        argv = [self.binary, "terminal", "session", "observe", target, "--cols", str(cols), "--rows", str(rows)]
        try:
            env = self.domain().frozen(self.env, client_protocol=True)
            process = self.stream_runner(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, bufsize=0, env=env)
        except permits.Unavailable as exc:
            raise HerdrError("observe_unavailable", "Watch unavailable: %s" % exc) from exc
        except OSError:
            raise HerdrError("observe_unavailable", "Watch unavailable: could not start the observer")
        return ObserveStream(process)

    def terminal_control(self, target, cols, rows, permit):
        """Fixed section-45 adapter; no generic runner or automated key path."""
        try:
            if (not isinstance(permit, permits.ControlPermit) or not isinstance(target, str)
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", target)
                    or type(cols) is not int or type(rows) is not int
                    or not 20 <= cols <= 300 or not 5 <= rows <= 120):
                raise permits.Unavailable("invalid control permit/target/viewport")
            domain = self.domain()
            descriptors = permit.control_descriptors(domain, target)
            env = domain.frozen(self.env, client_protocol=True)
            evidence = {"human_control": True, "fds": list(descriptors),
                        "claims": [[k, v, "shared" if k == "W" else "exclusive"] for k, v in permit.requests]}
            stream = ControlStream(None, permit, domain, target)  # Allocate owner before spawn.
            with permits.transport(env, descriptors, evidence):
                stream.process = self.stream_runner([self.binary, "terminal", "session", "control", target,
                                                     "--cols", str(cols), "--rows", str(rows)],
                                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                                    bufsize=0, env=env, pass_fds=descriptors, close_fds=True)
            stream.setup()
            return stream
        except (permits.Unavailable, OSError) as exc:
            raise HerdrError("control_unavailable", "Controller unavailable; no input enabled") from exc

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

    def plugin_list(self) -> List[Dict]:
        argv = ["plugin", "list", "--json"]
        answer = (registry_runner([self.binary] + argv) if self.runner in (subprocess_runner, dashboard_runner)
                  else self._exec(argv))
        if len(answer[1].encode("utf-8")) > 1048576:
            raise HerdrError("registry_unavailable", "Plugin registry exceeds observation limit")
        records = self._json(argv, answer).get("plugins")
        if not isinstance(records, list) or len(records) > 512 or any(not isinstance(p, dict) for p in records):
            raise HerdrError("registry_unavailable", "Invalid plugin registry")
        return records

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
        """``agent start``, with ``--timeout`` when given. A herdr that rejects that command
        line (exit 2, ``usage``: it parsed nothing and typed nothing) gets the same start once
        more without ``--timeout``, and no ``--timeout`` for the rest of this command, so its
        own readiness wait applies (design 32.1 as the review amended it)."""
        if timeout_ms and self.start_timeout:
            try:
                return dict(self.run(*start_argv(name, kind, pane, args, timeout_ms)).get("agent") or {})
            except HerdrError as exc:
                if exc.code != "usage":
                    raise
                self.start_timeout = False
        return dict(self.run(*start_argv(name, kind, pane, args)).get("agent") or {})

    def agent_prompt(self, target: str, text: str) -> Dict:
        return self.run("agent", "prompt", target, text)

    def agent_wait(self, target: str, until: Sequence[str], timeout_ms: int) -> Dict:
        argv = ["agent", "wait", target]
        for status in until:
            argv += ["--until", status]
        argv += ["--timeout", str(int(timeout_ms))]
        return dict(self.run(*argv).get("agent") or {})

    def agent_read(self, target: str, lines: int, source: Optional[str] = None, ansi: bool = False) -> str:
        """The last ``lines`` screen lines of an agent (``agent read TARGET --lines N``), plain
        text like ``pane read``. ``quit_agent`` reads the exit confirmation with it (design
        23.7) and ``peek`` prints it (23.6). ``source`` (``visible`` or ``recent``, design
        32.3) adds ``--source``; None leaves herdr's default (``recent``), which herdr refuses
        with ``agent_not_idle`` for a working seat whose history is in the alternate screen.
        ``ansi`` keeps the escape sequences (``--format ansi``): ``interrupt`` reads the prompt
        line so, to tell a faint suggestion from typed text (32.8)."""
        argv = ["agent", "read", target, "--lines", str(int(lines))]
        if source:
            argv += ["--source", source]
        if ansi:
            argv += ["--format", "ansi"]
        return self.text(*argv)

    def agent_send_keys(self, target: str, keys: Sequence[str]) -> Dict:
        """Press ``keys`` in an agent's pane (``agent send-keys TARGET KEY...``): the only way
        the plugin ever runs ``agent send-keys`` (design 23.7, invariants 1 and 21). Refuses,
        before spawning anything, any key outside ``SENDABLE_KEYS`` and a target that would
        read as an option. Its two callers decide when: one ``enter`` on the exit
        confirmation the plugin's own quit provoked, and ``interrupt``'s keys (with one
        ``ctrl+u`` after them when the interrupted prompt came back into the input, 23.11)."""
        keys = [str(k) for k in keys]
        target = str(target or "")
        if not keys or any(k not in SENDABLE_KEYS for k in keys) or not target or target.startswith("-"):
            raise HerdrError("forbidden_command", "herdr-fleet presses only %s, one agent at a time (asked: %s %s)"
                             % (", ".join(SENDABLE_KEYS), target or "(no target)", " ".join(keys) or "(no keys)"))
        argv = list(SEND_KEYS) + [target] + keys
        return self._json(argv, self._exec(argv, keys=True))

    def agent_rename(self, target: str, name: str) -> Dict:
        return self.run("agent", "rename", target, name)

    def agent_focus(self, target: str) -> Dict:
        """Move the owner's UI focus to the pane of ``target`` (a live agent name or a pane
        id). Only ``go`` and the popup's ``go`` run it (design 22.2, invariant 20); the
        answer is opaque to the plugin."""
        return self.run("agent", "focus", target)

    # -- panes and workspaces --------------------------------------------

    def pane_zoom_toggle(self, pane_id: str) -> Dict:
        """Zoom a pane, or restore it (``pane zoom PANE --toggle``). Only the watcher's board
        runs it, on its own strip, on the owner's ``z`` (design 25.1, invariant 22). The pane
        is always named: ``--current`` resolves to the owner's focused pane, not to the
        calling process (25.7)."""
        if not str(pane_id or "") or str(pane_id).startswith("-"):
            raise HerdrError("forbidden_command", "pane zoom needs the strip's own pane id (got %r)" % (pane_id,))
        return self.run("pane", "zoom", str(pane_id), "--toggle")

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

    def pane_read(self, pane_id: str, lines: int, ansi: bool = False) -> str:
        """The last ``lines`` lines of a pane's screen (``pane read PANE --lines N``), which
        herdr prints as plain text even in JSON mode; ``ansi`` keeps the escape sequences
        (``--format ansi``, design 32.8). Only the presence guard reads them (design 21.2,
        invariant 19), for the prompt line alone; and, only when ``agent read`` is refused for a working
        seat (``agent_not_idle``), the watcher's quiet-seat limit check (design 35.6) and
        ``ops.read_screen``, which confirms a limited seat before it is quit (35.6.4); never kept."""
        argv = ["pane", "read", pane_id, "--lines", str(int(lines))]
        if ansi:
            argv += ["--format", "ansi"]
        return self.text(*argv)

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

    def plugin_tab_open(self, plugin_id: str, entrypoint: str, workspace_id: str, cwd: str,
                        env: Optional[Dict[str, str]] = None) -> Dict:
        """Open a plugin pane in a new tab of ``workspace_id`` without moving focus (the
        handover helper, design 32.5); return the new pane's record."""
        argv = plugin_tab_argv(plugin_id, entrypoint, workspace_id, cwd, env)
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


class ControlStream:
    """Bounded nonblocking owner writer; output reader has no permit authority."""
    def __init__(self, process, permit, domain, terminal):
        self.process, self.permit, self.domain, self.terminal = process, permit, domain, terminal
        self.closing = False
        self.setup_failed = False
        if process is not None: self.setup()

    def setup(self):
        try:
            os.set_blocking(self.process.stdin.fileno(), False)
            os.set_blocking(self.process.stdout.fileno(), False)
        except Exception:
            # Ownership was established before either fallible setup operation.
            # The worker still receives this object and must reap the exact child.
            self.setup_failed = True

    def input(self, text, stop, eligible=None):
        if not isinstance(text, str) or not 0 < len(text.encode("utf-8")) <= 8192:
            raise HerdrError("control_invalid", "Input exceeds the 8 KiB limit or is empty")
        self._write({"type": "terminal.input", "text": text}, stop, eligible)

    def resize(self, cols, rows, stop, eligible=None):
        if type(cols) is not int or type(rows) is not int or not 20 <= cols <= 300 or not 5 <= rows <= 120:
            raise HerdrError("control_invalid", "Invalid viewport")
        self._write({"type": "terminal.resize", "cols": cols, "rows": rows}, stop, eligible)

    def _write(self, command, stop, eligible):
        self.permit.control_descriptors(self.domain, self.terminal)
        data = (json.dumps(command, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")
        if len(data) > 65536:
            raise HerdrError("control_invalid", "Controller payload exceeds limit")
        end, offset = time.monotonic() + 0.5, 0
        selector = selectors.DefaultSelector()
        try:
            selector.register(self.process.stdin, selectors.EVENT_WRITE)
            while offset < len(data):
                if self.setup_failed or self.closing or stop.is_set() or time.monotonic() >= end or self.process.poll() is not None:
                    raise HerdrError("control_uncertain", "Submission uncertain; control revoked, never replay")
                if selector.select(0.02):
                    if stop.is_set() or (eligible is not None and not eligible()) or time.monotonic() >= end:
                        raise HerdrError("control_uncertain", "Submission deadline passed; revoked, never replay")
                    try:
                        written = os.write(self.process.stdin.fileno(), data[offset:])
                    except BlockingIOError:
                        continue
                    if written <= 0:
                        raise OSError("closed controller input")
                    offset += written
        except OSError as exc:
            raise HerdrError("control_uncertain", "Submission uncertain; control revoked, never replay") from exc
        finally:
            selector.close()

    def lines(self, stop):
        selector, pending = selectors.DefaultSelector(), b""
        try:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            while not stop.is_set():
                if not selector.select(0.05):
                    continue
                try:
                    data = os.read(self.process.stdout.fileno(), 8192)
                except BlockingIOError:
                    continue
                if not data:
                    return
                pending += data
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    if len(line) > 262144:
                        raise HerdrError("control_unavailable", "Controller output exceeds frame limit")
                    yield line
                if len(pending) > 262144:
                    raise HerdrError("control_unavailable", "Controller output exceeds frame limit")
        finally:
            selector.close()

    def teardown(self):
        """Creator only. EOF/detach, then bounded exact-child terminate/kill/reap.

        False retains the parent's permit; a timeout is never proof of unlock.
        The caller stops/joins the independent reader before closing stdout.
        """
        self.permit.check()
        self.closing = True
        try:
            if self.process.stdin and not self.process.stdin.closed:
                self.process.stdin.close()
            if self.process.poll() is None:
                try:
                    self.process.wait(timeout=0.15)
                except subprocess.TimeoutExpired:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=0.25)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait(timeout=0.5)
            return self.process.poll() is not None
        except (OSError, subprocess.TimeoutExpired):
            return False


class ObserveStream:
    """Bounded byte transport. Closing always stops and reaps the owned child."""

    def __init__(self, process):
        self.process = process
        self.closed = False

    def lines(self, stop, max_record: int = 262144, on_tick=None):
        selector = selectors.DefaultSelector()
        pending = b""
        try:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            while not stop.is_set() and not self.closed:
                if on_tick is not None and not on_tick():
                    return
                if not selector.select(0.25):
                    continue
                chunk = os.read(self.process.stdout.fileno(), 16384)
                if not chunk:
                    if pending:
                        raise HerdrError("bad_frame", "Watch ended with an incomplete frame")
                    return
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    if len(line) > max_record:
                        raise HerdrError("bad_frame", "Watch record exceeds the size limit")
                    if line:
                        yield line
                if len(pending) > max_record:
                    raise HerdrError("bad_frame", "Watch record exceeds the size limit")
        finally:
            selector.close()
            self.close()

    def close(self):
        if self.closed:
            return
        self.closed = True
        process = self.process
        try:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1.0)
        finally:
            if process.stdout:
                process.stdout.close()


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


def plugin_tab_argv(plugin_id: str, entrypoint: str, workspace_id: str, cwd: str,
                    env: Optional[Dict[str, str]] = None) -> List[str]:
    """The exact ``plugin pane open --placement tab`` argv of the handover helper (design
    32.5): a new tab of the seat's workspace, never focused."""
    argv = ["plugin", "pane", "open", "--plugin", plugin_id, "--entrypoint", entrypoint, "--placement", "tab",
            "--workspace", workspace_id, "--cwd", cwd]
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
