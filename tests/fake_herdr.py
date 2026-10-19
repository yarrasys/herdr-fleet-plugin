#!/usr/bin/env python3
"""A fake ``herdr`` binary for tests; no live herdr is ever needed.

The simulated server lives in a JSON *world* file (env ``FAKE_HERDR_WORLD``):
workspaces, panes with their foreground processes and the agent in each,
counters, ``behaviors`` and ``canned`` responses. Every call reads the world,
acts, writes it back and appends one JSON line to the call log (env
``FAKE_HERDR_LOG``): ``{"argv": [...], "exit": n, "info": {...}}``.

- Canned responses: ``world["canned"]`` entries ``{"prefix": [...], "exit": n,
  "stdout": str|obj, "stderr": str|obj, "times": n}`` answer any call whose
  argv starts with ``prefix`` (``times`` limits how often), before simulation.
- Stateful simulation: workspace/pane/agent commands behave like herdr 0.9.4
  (JSON shapes copied from the live binary; as live, ``pane get``/``pane list``
  records never carry the agent ``name``, only ``agent get``/``agent list`` do). Behaviours: ``start_not_ready``
  (names), ``start_errors`` ({name: {code, message}}), ``split_busy`` (n
  ``agent_pane_busy`` answers for a fresh split), ``quit_polls`` (``agent
  get`` keeps succeeding n times after a quit command, then the agent is
  gone), ``ignore_quit`` (names that never exit), ``linger_polls`` (n
  process-info answers that still show a non-shell process after exit),
  ``prompt_errors`` ({name: code}), ``wait_status`` ({name: status}),
  ``argv_absent`` (agent processes carry ``cmdline`` but no ``argv``).
- The log records facts the safety tests assert on: the target's state at
  every ``agent prompt``, whether a pane was at a shell at every ``agent
  start``, and whether a closed pane or workspace was created by a command
  in this world (``split``/``workspace create``) rather than set up by a test.

Use it in-process (``Fake(world, log)(argv)``) or as a program.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Dict, List, Optional, Tuple

KINDS = ["pi", "claude", "codex", "gemini", "cursor", "devin", "agy", "cline", "omp", "mastracode",
         "opencode", "copilot", "kimi", "kiro", "droid", "amp", "grok", "hermes", "kilo", "qodercli",
         "qwen", "letta", "maki", "muse"]
FORBIDDEN = (("agent", "send-keys"), ("pane", "send-keys"), ("pane", "send-text"), ("pane", "run"))
SHELL = {"name": "zsh", "argv0": "-zsh", "argv": ["-zsh"], "cmdline": "-zsh", "pid": 500}

Result = Tuple[int, str, str]


def new_world() -> Dict:
    return {
        "version": "herdr 0.9.4",
        "kinds": list(KINDS),
        "integration": [
            "pi: outdated (v8 < v9) (/home/u/.pi/agent/extensions/herdr-agent-state.ts)",
            "claude: current (v10) (/home/u/.claude/hooks/herdr-agent-state.sh)",
            "codex: current (v9) (/home/u/.codex/herdr-agent-state.sh)",
            "copilot: not installed (/home/u/.copilot/hooks/herdr-agent-state.sh)",
            "letta (experimental): not installed (/home/u/.letta/hooks/herdr-agent-session.sh)",
        ],
        "counter": 100,
        "workspaces": {},
        "panes": {},
        "canned": [],
        "behaviors": {},
        "config_dir": None,
        "notifications": [],
    }


def add_workspace(world: Dict, ws: str, label: str, cwd: str = "/tmp", created: bool = False) -> str:
    """Add a workspace with one tab and a root shell pane ``<ws>:p1``."""
    number = len(world["workspaces"]) + 1
    world["workspaces"][ws] = {"label": label, "number": number, "active_tab_id": ws + ":t1",
                               "created": created}
    return add_pane(world, ws + ":p1", ws, cwd)


def add_pane(world: Dict, pane: str, ws: str, cwd: str = "/tmp", procs: Optional[List[Dict]] = None,
             created: bool = False) -> str:
    world["panes"][pane] = {"workspace_id": ws, "tab_id": ws + ":t1", "cwd": cwd,
                            "procs": procs if procs is not None else [dict(SHELL, cwd=cwd)],
                            "agent": None, "busy": 0, "quitting": None, "linger": 0,
                            "created": created, "env": {}, "prompts": []}
    return pane


def agent_proc(kind: str, args: List[str], cwd: str, argv_absent: bool = False) -> Dict:
    argv = [kind] + list(args)
    proc = {"argv0": kind, "cmdline": " ".join(argv), "cwd": cwd,
            "name": "2.1.299" if kind == "claude" else kind, "pid": 900}
    if not argv_absent:
        proc["argv"] = argv
    return proc


def put_agent(world: Dict, pane: str, kind: str, name: Optional[str] = None, status: str = "idle",
              args: Optional[List[str]] = None) -> None:
    """Place a running agent in a pane (as if started earlier or resumed by herdr)."""
    record = world["panes"][pane]
    record["agent"] = {"agent": kind, "agent_status": status, "name": name}
    record["procs"] = [agent_proc(kind, list(args or []), record["cwd"],
                                  world["behaviors"].get("argv_absent", False))]


class Fake:
    """Callable runner: ``Fake(world_path, log_path)([binary, *argv])``."""

    def __init__(self, world_path: str, log_path: str):
        self.world_path = world_path
        self.log_path = log_path

    def __call__(self, argv: List[str]) -> Result:
        args = list(argv[1:])
        with open(self.world_path, "r", encoding="utf-8") as handle:
            world = json.load(handle)
        info: Dict = {}
        code, out, err = Sim(world, info).handle(args)
        with open(self.world_path, "w", encoding="utf-8") as handle:
            json.dump(world, handle)
        with open(self.log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"argv": args, "exit": code, "info": info}) + "\n")
        return code, out, err


def _ok(result: Dict, name: str = "cli") -> Result:
    return 0, json.dumps({"id": name, "result": result}) + "\n", ""


def _err(code: str, message: str) -> Result:
    return 1, "", json.dumps({"error": {"code": code, "message": message}, "id": "cli"}) + "\n"


def _opts(args: List[str], valued: Tuple[str, ...]) -> Tuple[List[str], Dict[str, List[str]], List[str]]:
    """Split args into positionals, valued options and anything after ``--``."""
    positional: List[str] = []
    options: Dict[str, List[str]] = {}
    rest: List[str] = []
    i = 0
    while i < len(args):
        item = args[i]
        if item == "--":
            rest = args[i + 1:]
            break
        if item in valued:
            options.setdefault(item, []).append(args[i + 1])
            i += 2
            continue
        if item.startswith("--"):
            options.setdefault(item, []).append("")
        else:
            positional.append(item)
        i += 1
    return positional, options, rest


class Sim:
    def __init__(self, world: Dict, info: Dict):
        self.w = world
        self.info = info
        self.b = world.get("behaviors", {})

    # -- helpers -----------------------------------------------------------

    def _next(self) -> int:
        self.w["counter"] += 1
        return self.w["counter"]

    def _pane_record(self, pid: str) -> Dict:
        p = self.w["panes"][pid]
        rec = {"cwd": p["cwd"], "foreground_cwd": p["cwd"], "focused": False, "pane_id": pid,
               "revision": 1, "scroll": {"max_offset_from_bottom": 0, "offset_from_bottom": 0,
                                         "viewport_rows": 40},
               "tab_id": p["tab_id"], "terminal_id": "term_" + pid, "terminal_title": "",
               "workspace_id": p["workspace_id"]}
        if p["agent"]:  # live herdr: pane records carry the kind and state, never the name
            rec["agent"] = p["agent"]["agent"]
            rec["agent_status"] = p["agent"]["agent_status"]
        return rec

    def _agent_record(self, pid: str) -> Dict:
        """``agent get``/``agent list`` records: like a pane record plus ``name`` when named."""
        rec = self._pane_record(pid)
        rec.pop("scroll", None)
        name = self.w["panes"][pid]["agent"].get("name")
        if name:
            rec["name"] = name
        return rec

    def _resolve(self, target: str) -> Optional[str]:
        """Pane id hosting the agent named or located by ``target``."""
        for pid, p in self.w["panes"].items():
            if p["agent"] and (pid == target or p["agent"].get("name") == target):
                return pid
        return None

    def _exit_agent(self, pid: str) -> None:
        p = self.w["panes"][pid]
        p["agent"] = None
        p["quitting"] = None
        p["procs"] = [dict(SHELL, cwd=p["cwd"])]
        p["linger"] = int(self.b.get("linger_polls", 0))

    def _at_shell(self, pid: str) -> bool:
        procs = self.w["panes"][pid]["procs"]
        return len(procs) == 1 and procs[0].get("name") in ("zsh", "bash", "fish", "sh", "nu")

    # -- dispatch ------------------------------------------------------------

    def handle(self, args: List[str]) -> Result:
        if not args:
            self.info["bare"] = True
            return 99, "", "fake herdr: bare herdr would attach the TUI\n"
        canned = self._canned(args)
        if canned is not None:
            return canned
        if tuple(args[:2]) in FORBIDDEN:
            self.info["forbidden"] = True
            return _ok({"type": "ok"})
        if args == ["--version"]:
            return 0, self.w["version"] + "\n", ""
        if args == ["agent"]:
            usage = "herdr agent commands:\n  herdr agent list\n  kinds: %s\n" % "|".join(self.w["kinds"])
            return 2, "", usage
        if args[:3] == ["agent", "start", "--help"]:
            return 0, "--kind <KIND>\n  [possible values: %s]\n" % ", ".join(self.w["kinds"]), ""
        if args[:2] == ["integration", "status"]:
            return 0, "\n".join(self.w["integration"]) + "\n", ""
        method = getattr(self, "_" + "_".join(a.replace("-", "_") for a in args[:2]), None)
        if method is None:
            return 2, "", "error: unrecognized command %s\n" % " ".join(args)
        return method(args[2:])

    def _canned(self, args: List[str]) -> Optional[Result]:
        for entry in self.w.get("canned", []):
            prefix = entry["prefix"]
            if args[:len(prefix)] != prefix:
                continue
            if "times" in entry:
                if entry["times"] <= 0:
                    continue
                entry["times"] -= 1
            self.info["canned"] = True
            out, err = entry.get("stdout", ""), entry.get("stderr", "")
            out = out if isinstance(out, str) else json.dumps(out) + "\n"
            err = err if isinstance(err, str) else json.dumps(err) + "\n"
            return int(entry.get("exit", 0)), out, err
        return None

    # -- agents -----------------------------------------------------------------

    def _agent_list(self, args: List[str]) -> Result:
        agents = [self._agent_record(pid) for pid, p in self.w["panes"].items() if p["agent"]]
        return _ok({"agents": agents, "type": "agent_list"}, "cli:agent:list")

    def _agent_get(self, args: List[str]) -> Result:
        pid = self._resolve(args[0])
        if pid is None:
            return _err("agent_not_found", "no agent for %s" % args[0])
        p = self.w["panes"][pid]
        if p["quitting"] is not None:
            if p["quitting"] <= 0:
                self._exit_agent(pid)
                return _err("agent_not_found", "no agent for %s" % args[0])
            p["quitting"] -= 1
        return _ok({"agent": self._agent_record(pid), "type": "agent_info"}, "cli:agent:get")

    def _agent_start(self, args: List[str]) -> Result:
        pos, opts, rest = _opts(args, ("--kind", "--pane", "--timeout"))
        name, kind, pid = pos[0], opts["--kind"][0], opts["--pane"][0]
        if kind not in self.w["kinds"]:
            return 2, "", "error: invalid value '%s' for '--kind <KIND>'\n" % kind
        if pid not in self.w["panes"]:
            return _err("pane_not_found", "pane %s not found" % pid)
        p = self.w["panes"][pid]
        self.info["at_shell"] = self._at_shell(pid) and not p["agent"]
        if p["agent"] or not self._at_shell(pid):
            return _err("agent_pane_busy", "pane %s is not at an interactive shell prompt" % pid)
        if p["busy"] > 0:
            p["busy"] -= 1
            return _err("agent_pane_busy", "pane %s is not at an interactive shell prompt" % pid)
        if self._resolve(name) is not None:
            return _err("agent_name_taken", "agent name %s is already in use" % name)
        error = self.b.get("start_errors", {}).get(name)
        if error:
            return _err(error["code"], error["message"])
        blocked = name in self.b.get("start_not_ready", [])
        p["agent"] = {"agent": kind, "agent_status": "blocked" if blocked else "idle", "name": name}
        p["procs"] = [agent_proc(kind, rest, p["cwd"], self.b.get("argv_absent", False))]
        if blocked:
            return _err("agent_not_ready", "agent %s is blocked during startup" % name)
        return _ok({"agent": self._agent_record(pid), "type": "agent_info"}, "cli:agent:start")

    def _agent_prompt(self, args: List[str]) -> Result:
        pos, opts, _ = _opts(args, ("--until", "--timeout"))
        target, text = pos[0], pos[1]
        pid = self._resolve(target)
        if pid is None:
            return _err("agent_not_found", "no agent for %s" % target)
        p = self.w["panes"][pid]
        status = p["agent"]["agent_status"]
        self.info["target_status"] = status
        if status == "blocked":
            return _err("agent_blocked", "agent %s is blocked" % target)
        name = p["agent"].get("name") or pid
        p["prompts"].append(text)
        error = self.b.get("prompt_errors", {}).get(name)
        if error:
            return _err(error, "prompt %s for %s" % (error, name))
        if text in self.b.get("quit_texts", ["/exit", "/quit"]) and name not in self.b.get("ignore_quit", []):
            p["quitting"] = int(self.b.get("quit_polls", 1))
        return _ok({"agent": self._agent_record(pid), "type": "agent_info"}, "cli:agent:prompt")

    def _agent_wait(self, args: List[str]) -> Result:
        pos, opts, _ = _opts(args, ("--until", "--timeout"))
        pid = self._resolve(pos[0])
        if pid is None:
            return _err("agent_not_found", "no agent for %s" % pos[0])
        agent = self.w["panes"][pid]["agent"]
        new = self.b.get("wait_status", {}).get(agent.get("name") or pid)
        if new:
            agent["agent_status"] = new
        until = opts.get("--until") or ["idle", "done", "blocked"]
        if agent["agent_status"] not in until:
            return _err("timeout", "timed out waiting for %s" % pos[0])
        return _ok({"agent": self._agent_record(pid), "type": "agent_info"}, "cli:agent:wait")

    def _agent_rename(self, args: List[str]) -> Result:
        pid = self._resolve(args[0])
        if pid is None:
            return _err("agent_not_found", "no agent for %s" % args[0])
        name = None if args[1] == "--clear" else args[1]
        other = self._resolve(name) if name else None
        if other is not None and other != pid:
            return _err("agent_name_taken", "agent name %s is already in use" % name)
        self.w["panes"][pid]["agent"]["name"] = name
        return _ok({"agent": self._agent_record(pid), "type": "agent_info"}, "cli:agent:rename")

    # -- panes --------------------------------------------------------------------

    def _pane_get(self, args: List[str]) -> Result:
        pid = args[0]
        if pid not in self.w["panes"]:
            return _err("pane_not_found", "pane %s not found" % pid)
        return _ok({"pane": self._pane_record(pid), "type": "pane_info"}, "cli:pane:get")

    def _pane_list(self, args: List[str]) -> Result:
        _, opts, _ = _opts(args, ("--workspace",))
        ws = (opts.get("--workspace") or [None])[0]
        panes = [self._pane_record(pid) for pid, p in self.w["panes"].items()
                 if ws is None or p["workspace_id"] == ws]
        return _ok({"panes": panes, "type": "pane_list"}, "cli:pane:list")

    def _pane_process_info(self, args: List[str]) -> Result:
        _, opts, _ = _opts(args, ("--pane",))
        pid = opts["--pane"][0]
        if pid not in self.w["panes"]:
            return _err("pane_not_found", "pane %s not found" % pid)
        p = self.w["panes"][pid]
        procs = list(p["procs"])
        if p["linger"] > 0:
            p["linger"] -= 1
            procs = procs + [{"name": "node", "argv0": "node", "cwd": p["cwd"], "pid": 901}]
        info = {"foreground_process_group_id": procs[0]["pid"] if procs else 0,
                "foreground_processes": procs, "pane_id": pid, "shell_pid": 500}
        return _ok({"process_info": info, "type": "pane_process_info"}, "cli:pane:process_info")

    def _pane_split(self, args: List[str]) -> Result:
        pos, opts, _ = _opts(args, ("--pane", "--direction", "--ratio", "--cwd", "--env"))
        source = pos[0] if pos else opts["--pane"][0]
        if source not in self.w["panes"]:
            return _err("pane_not_found", "pane %s not found" % source)
        src = self.w["panes"][source]
        pid = "%s:p%d" % (src["workspace_id"], self._next())
        cwd = (opts.get("--cwd") or [src["cwd"]])[0]
        add_pane(self.w, pid, src["workspace_id"], cwd, created=True)
        p = self.w["panes"][pid]
        p["busy"] = int(self.b.get("split_busy", 0))
        p["env"] = dict(item.split("=", 1) for item in opts.get("--env", []))
        p["split"] = {"from": source, "direction": opts["--direction"][0],
                      "ratio": (opts.get("--ratio") or [None])[0]}
        return _ok({"pane": self._pane_record(pid), "type": "pane_info"}, "cli:pane:split")

    def _pane_close(self, args: List[str]) -> Result:
        pid = args[0]
        if pid not in self.w["panes"]:
            return _err("pane_not_found", "pane %s not found" % pid)
        self.info["created"] = bool(self.w["panes"][pid].get("created"))
        self.info["had_agent"] = bool(self.w["panes"][pid]["agent"])
        del self.w["panes"][pid]
        return _ok({"type": "ok"}, "cli:pane:close")

    # -- workspaces ------------------------------------------------------------------

    def _ws_record(self, ws: str) -> Dict:
        record = self.w["workspaces"][ws]
        panes = [p for p in self.w["panes"].values() if p["workspace_id"] == ws]
        return {"active_tab_id": record["active_tab_id"], "agent_status": "idle", "focused": False,
                "label": record["label"], "number": record["number"], "pane_count": len(panes),
                "tab_count": 1, "workspace_id": ws}

    def _workspace_list(self, args: List[str]) -> Result:
        return _ok({"type": "workspace_list", "workspaces": [self._ws_record(ws) for ws in self.w["workspaces"]]},
                   "cli:workspace:list")

    def _workspace_get(self, args: List[str]) -> Result:
        if args[0] not in self.w["workspaces"]:
            return _err("workspace_not_found", "workspace %s not found" % args[0])
        return _ok({"type": "workspace_info", "workspace": self._ws_record(args[0])}, "cli:workspace:get")

    def _workspace_create(self, args: List[str]) -> Result:
        _, opts, _ = _opts(args, ("--cwd", "--label", "--env"))
        ws = "w%d" % self._next()
        cwd = (opts.get("--cwd") or ["/tmp"])[0]
        root = add_workspace(self.w, ws, (opts.get("--label") or [ws])[0], cwd, created=True)
        self.w["panes"][root]["created"] = True
        self.w["panes"][root]["env"] = dict(item.split("=", 1) for item in opts.get("--env", []))
        return _ok({"workspace": self._ws_record(ws), "tab": {"tab_id": ws + ":t1", "workspace_id": ws},
                    "root_pane": self._pane_record(root), "type": "workspace_created"}, "cli:workspace:create")

    def _workspace_close(self, args: List[str]) -> Result:
        ws = args[0]
        if ws not in self.w["workspaces"]:
            return _err("workspace_not_found", "workspace %s not found" % ws)
        self.info["created"] = bool(self.w["workspaces"][ws].get("created"))
        del self.w["workspaces"][ws]
        for pid in [pid for pid, p in self.w["panes"].items() if p["workspace_id"] == ws]:
            del self.w["panes"][pid]
        return _ok({"type": "ok"}, "cli:workspace:close")

    # -- misc ---------------------------------------------------------------------------

    def _notification_show(self, args: List[str]) -> Result:
        pos, opts, _ = _opts(args, ("--body", "--sound", "--position"))
        self.w["notifications"].append({"title": pos[0], "body": (opts.get("--body") or [""])[0],
                                        "sound": (opts.get("--sound") or ["none"])[0]})
        return _ok({"type": "ok"}, "cli:notification:show")

    def _plugin_pane(self, args: List[str]) -> Result:
        """``plugin pane open`` answers with the new pane at result.plugin_pane.pane (live 0.9.4)."""
        if args[:1] != ["open"]:
            return _ok({"type": "ok"}, "cli:plugin:pane")
        _, opts, _ = _opts(args[1:], ("--plugin", "--entrypoint", "--placement", "--cwd", "--env",
                                      "--width", "--height", "--workspace", "--target-pane", "--direction"))
        ws = next(iter(self.w["workspaces"]), "w1")
        pid = "%s:p%d" % (ws, self._next())
        add_pane(self.w, pid, ws, (opts.get("--cwd") or ["/tmp"])[0])
        pane = dict(self._pane_record(pid), label=(opts.get("--entrypoint") or [""])[0])
        return _ok({"plugin_pane": {"pane": pane}, "type": "plugin_pane_opened"}, "cli:plugin:pane:open")

    def _plugin_config_dir(self, args: List[str]) -> Result:
        if not self.w.get("config_dir"):
            return _err("plugin_not_found", "plugin %s is not installed" % args[0])
        return 0, self.w["config_dir"] + "\n", ""

    def _plugin_list(self, args: List[str]) -> Result:
        return _ok({"plugins": [], "type": "plugin_list"}, "cli:plugin")


def main() -> int:
    world = os.environ.get("FAKE_HERDR_WORLD")
    log = os.environ.get("FAKE_HERDR_LOG")
    if not world or not log:
        sys.stderr.write("fake herdr: FAKE_HERDR_WORLD and FAKE_HERDR_LOG must be set\n")
        return 98
    code, out, err = Fake(world, log)([sys.argv[0]] + sys.argv[1:])
    sys.stdout.write(out)
    sys.stderr.write(err)
    return code


if __name__ == "__main__":
    sys.exit(main())
