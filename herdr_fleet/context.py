"""Per-invocation context: who called, from where, and with which files.

``Ctx`` bundles the herdr client, environment, clock and output streams with
lazily loaded fleet file, state file and plugin config. ``detect_invocation``
works out the invoking directory and the caller's pane (design section 2):
the current directory for the CLI; the focused pane's cwd for a manifest
action, whose own cwd is the plugin root. ``preflight`` enforces the common
preconditions of section 4. ``Report`` collects per-seat results and maps
them to exit codes 0 / 1 / 3.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, TextIO

from herdr_fleet import MIN_HERDR_VERSION, PLUGIN_ID
from herdr_fleet.fleetfile import Fleet, FleetError, find_fleet_file, load_fleet, load_toml
from herdr_fleet.herdr import Herdr, HerdrError
from herdr_fleet.notify import notify
from herdr_fleet.seats import DEFAULT_QUIT_TIMEOUT_MS, Clock
from herdr_fleet.state import State, load_state, save_state

EXIT_OK, EXIT_FAIL, EXIT_USAGE, EXIT_PARTIAL = 0, 1, 2, 3


class OpError(Exception):
    """A hard failure that stops the command (exit 1)."""


class UsageError(Exception):
    """Bad arguments that argparse cannot catch, e.g. an unknown seat name (exit 2)."""


@dataclass
class Invocation:
    cwd: str                    # directory used for fleet-file discovery
    pane_id: Optional[str]      # the caller's pane: never started into
    workspace_id: Optional[str]
    mode: str                   # cli | action | pane
    context: Dict = field(default_factory=dict)


@dataclass
class PluginConfig:
    quit: Dict[str, str] = field(default_factory=dict)
    quit_timeout_ms: Optional[int] = None
    path: Optional[str] = None
    error: Optional[str] = None


@dataclass
class SeatResult:
    name: str
    status: str
    detail: str
    level: str  # ok | partial | fail


class Report:
    """Results of one operation, in the order they happened."""

    def __init__(self, op: str):
        self.op = op
        self.results: List[SeatResult] = []

    def add(self, name: str, status: str, detail: str, level: str = "ok") -> None:
        self.results.append(SeatResult(name, status, detail, level))

    def extend(self, other: "Report") -> None:
        self.results.extend(other.results)

    def exit_code(self) -> int:
        levels = {r.level for r in self.results}
        if "fail" in levels:
            return EXIT_FAIL
        if "partial" in levels:
            return EXIT_PARTIAL
        return EXIT_OK

    def lines(self) -> List[str]:
        return ["%s: %s%s" % (r.name, r.status, " - " + r.detail if r.detail else "") for r in self.results]


def parse_context(raw: Optional[str]) -> Dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def context_id(context: Mapping, what: str) -> Optional[str]:
    """Pane or workspace id from HERDR_PLUGIN_CONTEXT_JSON. herdr 0.9.4 sends
    ``focused_pane_id`` and ``workspace_id`` (see design section 12); other
    spellings (``pane_id``, or a ``focused_pane``/``pane`` value that is an id
    or an object with one) are accepted in case the shape changes."""
    keys = {"pane": ("focused_pane_id", "pane_id", "focused_pane", "pane"),
            "workspace": ("workspace_id", "workspace")}[what]
    for key in keys:
        value = context.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict):
            for sub in (what + "_id", "id"):
                if isinstance(value.get(sub), str) and value.get(sub):
                    return value[sub]
    return None


def context_cwd(context: Mapping) -> Optional[str]:
    """The focused pane's cwd from HERDR_PLUGIN_CONTEXT_JSON: ``focused_pane_cwd``
    (herdr 0.9.4), else a ``cwd`` inside a ``focused_pane``/``pane`` object."""
    value = context.get("focused_pane_cwd")
    if isinstance(value, str) and value:
        return value
    for key in ("focused_pane", "pane"):
        nested = context.get(key)
        if isinstance(nested, dict) and isinstance(nested.get("cwd"), str) and nested["cwd"]:
            return nested["cwd"]
    return None


def focused_cwd(herdr: Herdr, context: Mapping, pane: Optional[str]) -> Optional[str]:
    """The focused pane's cwd: from the context JSON when present, else ``pane get``."""
    return context_cwd(context) or pane_cwd(herdr, pane)


def detect_invocation(env: Mapping[str, str], herdr: Herdr, cwd: str) -> Invocation:
    context = parse_context(env.get("HERDR_PLUGIN_CONTEXT_JSON"))
    pane = env.get("HERDR_PANE_ID") or context_id(context, "pane")
    workspace = env.get("HERDR_WORKSPACE_ID") or context_id(context, "workspace")
    if env.get("HERDR_PLUGIN_ACTION_ID"):
        mode = "action"
    elif env.get("HERDR_PLUGIN_ENTRYPOINT_ID"):
        mode = "pane"
    else:
        mode = "cli"
    directory = cwd
    if mode == "action":  # actions run in the plugin root; discover from the focused pane
        directory = focused_cwd(herdr, context, pane) or directory
    return Invocation(cwd=directory, pane_id=pane, workspace_id=workspace, mode=mode, context=context)


def pane_cwd(herdr: Herdr, pane: Optional[str]) -> Optional[str]:
    if not pane:
        return None
    try:
        info = herdr.pane_get(pane)
    except HerdrError:
        return None
    return info.get("cwd") or info.get("foreground_cwd")


class Ctx:
    """Everything an operation needs. Construct once per command."""

    def __init__(self, herdr: Herdr, env: Mapping[str, str], clock: Clock, stdout: TextIO,
                 stderr: TextIO, cwd: str, file: Optional[str] = None, notify: bool = False,
                 dry_run: bool = False):
        self.herdr = herdr
        self.env = env
        self.clock = clock
        self.stdout = stdout
        self.stderr = stderr
        self.cwd = cwd
        self.file = file
        self.notify_enabled = notify
        self.dry_run = dry_run
        self._invocation: Optional[Invocation] = None
        self._fleet: Optional[Fleet] = None
        self._state: Optional[State] = None
        self._config: Optional[PluginConfig] = None

    # -- output ------------------------------------------------------------

    def say(self, text: str = "") -> None:
        self.stdout.write(text + "\n")

    def warn(self, text: str) -> None:
        self.stderr.write("herdr-fleet: %s\n" % text)

    def notify(self, title: str, body: str, sound: str) -> None:
        """Notify through herdr, but only from inside herdr: outside it, ``herdr``
        on PATH could reach some other session."""
        if self.env.get("HERDR_ENV") != "1" and not self.env.get("HERDR_BIN_PATH"):
            return
        notify(self.herdr, title, body, sound, errors=self.stderr)

    # -- lazily loaded pieces -----------------------------------------------

    @property
    def invocation(self) -> Invocation:
        if self._invocation is None:
            self._invocation = detect_invocation(self.env, self.herdr, self.cwd)
        return self._invocation

    @property
    def fleet(self) -> Fleet:
        if self._fleet is None:
            self._fleet = load_fleet(self._find_fleet())
            for warning in self._fleet.warnings:
                self.warn(warning)
        return self._fleet

    def _find_fleet(self) -> str:
        inv = self.invocation
        try:
            return find_fleet_file(inv.cwd, self.file, self.env)
        except FleetError:
            # A plugin pane opened without --cwd starts in the plugin root:
            # fall back to the focused pane's directory.
            fallback = (focused_cwd(self.herdr, inv.context, context_id(inv.context, "pane"))
                        if inv.mode == "pane" else None)
            if not fallback or fallback == inv.cwd:
                raise
            return find_fleet_file(fallback, self.file, self.env)

    @property
    def state(self) -> State:
        if self._state is None:
            self._state = load_state(self.fleet.repo)
        return self._state

    @property
    def config(self) -> PluginConfig:
        if self._config is None:
            self._config = load_config(self.env, self.herdr)
        return self._config

    def quit_timeout_ms(self) -> int:
        return self.fleet.quit_timeout_ms or self.config.quit_timeout_ms or DEFAULT_QUIT_TIMEOUT_MS

    def save(self) -> None:
        """Persist the state file (never during --dry-run)."""
        if not self.dry_run and self._state is not None and self._state.dirty:
            save_state(self._state)


def preflight(ctx: Ctx) -> None:
    """Common preconditions (design section 4): inside herdr, reachable, new enough."""
    if ctx.env.get("HERDR_ENV") != "1" and not ctx.env.get("HERDR_BIN_PATH"):
        raise OpError("not running inside Herdr (HERDR_ENV=1 or HERDR_BIN_PATH is required)")
    ctx.herdr.agent_list()
    version = ctx.herdr.version()
    if version is None or version < MIN_HERDR_VERSION:
        found = ".".join(str(v) for v in version) if version else "unknown"
        raise OpError("herdr %s is too old; herdr-fleet needs %s or newer"
                      % (found, ".".join(str(v) for v in MIN_HERDR_VERSION)))


def load_config(env: Mapping[str, str], herdr: Herdr) -> PluginConfig:
    """Read ``config.toml`` from the plugin config dir: ``[quit]`` per-kind
    commands and ``quit_timeout_ms`` (under ``[quit]``, as design section 12
    says, or at the top level)."""
    folder = env.get("HERDR_PLUGIN_CONFIG_DIR")
    if not folder:
        try:
            folder = herdr.plugin_config_dir(PLUGIN_ID)
        except HerdrError:
            folder = None
    if not folder:
        return PluginConfig()
    path = os.path.join(folder, "config.toml")
    if not os.path.isfile(path):
        return PluginConfig(path=path)
    try:
        data = load_toml(path)
    except (OSError, ValueError) as exc:
        return PluginConfig(path=path, error="%s: %s" % (path, exc))
    quit_table = data.get("quit", {})
    config = PluginConfig(path=path)
    if isinstance(quit_table, dict):
        config.quit = {str(k): str(v) for k, v in quit_table.items() if isinstance(v, str)}
    timeout = (quit_table if isinstance(quit_table, dict) else {}).get("quit_timeout_ms", data.get("quit_timeout_ms"))
    if isinstance(timeout, int) and not isinstance(timeout, bool) and timeout > 0:
        config.quit_timeout_ms = timeout
    return config
