# herdr-fleet: design

Decided 19 Oct 2026 by the owner and herdr-fleet-designer after the brainstorm in this session. `docs/requirements.md` holds the problem; `docs/survey.md` holds the corrected survey of prior art. This file is the spec the implementation follows. Change it before changing behaviour.

Decisions the owner made: a herdr plugin, not a desktop app; Python; open source (MIT); everything else was delegated to the designer and is recorded here.

## 1. Shape

A herdr plugin with no build step and one runtime dependency, `python3` 3.8 or newer. It ships:

- `herdr-plugin.toml`: four actions and two panes (an overlay for status, a popup menu for restart, swap, kickoff, down).
- `bin/herdr-fleet`: a CLI with the same operations plus arguments. Orchestrator agents run this directly, e.g. `herdr-fleet restart fablyn-dev`. Manifest actions cannot take arguments, so the CLI is the surface for anything that names an agent.
- `herdr_fleet/`: the Python package. TOML parsing uses `tomllib` when present and a vendored `tomli` (MIT, under `herdr_fleet/_vendor/tomli/`) otherwise.

Everything talks to herdr through `HERDR_BIN_PATH` when set, else `herdr` on `PATH`, and parses the JSON that herdr returns. No socket code. No daemon. No background processes survive a command.

Plugin id `yarrasys.fleet`. Repository name `herdr-fleet` (two unrelated GitHub repositories already use that name; the marketplace card shows the owner, so it is survivable). The fleet file convention `.herdr/fleet.toml` is ours; herdr defines nothing project-level. herdup uses `.herdr/team.toml`, so the directory has a precedent.

## 2. The fleet file

Path: `<repo>/.herdr/fleet.toml`, committed. Discovery walks up from the invoking directory until a directory contains `.herdr/fleet.toml`; that directory is the repo root. `--file PATH` or `HERDR_FLEET_FILE` overrides discovery, and then the repo root is the directory holding the `.herdr` directory (or the file's directory when the file is elsewhere).

Invoking directory: for the CLI, the current directory. For a manifest action or pane, the `cwd` of the focused pane from `herdr pane get $HERDR_PANE_ID` (the action runs with the plugin root as its own cwd, which is useless for discovery). When `HERDR_PANE_ID` is unset, read `focused_pane` from `HERDR_PLUGIN_CONTEXT_JSON`.

```toml
# .herdr/fleet.toml  — the Fablyn team, as an example
workspace  = "fablyn"            # optional: reuse the workspace with this label, else create it.
                                 # Absent: use the workspace the command was invoked from.
cwd        = "."                 # optional default agent cwd, relative to the repo root. Default ".".
state_note = "docs/STATE.md"     # optional: file the orchestrator keeps; its contents fill {state}.
kickoff    = "docs/kickoff/default.md"   # optional default kickoff template (path, relative to repo root)

[layout]
direction = "right"              # default split direction between consecutive seats: right | down
ratio     = 0.5                  # optional default split ratio

[[agents]]
name    = "fablyn-orchestrator"  # herdr agent name: [a-z][a-z0-9_-]{0,31}, unique in the file
kind    = "claude"               # a kind from `herdr agent start --help`
args    = ["--model", "claude-opus-5-5"]   # passed after `--` to herdr agent start
cwd     = "."                    # optional, relative to repo root, or absolute, or ~/…
split   = "right"                # optional, overrides [layout].direction for this seat
ratio   = 0.5                    # optional
env     = { FABLYN_ROLE = "orchestrator" }  # optional, injected with --env on the pane
kickoff = "docs/kickoff/orchestrator.md"    # optional template path; or kickoff_text = "inline"
quit    = "/exit"                # optional override of the per-kind quit command
notes   = "Needs localhost for Playwright and the Miniflare vitest files."  # shown in status only

[[agents]]
name = "fablyn-dev"
kind = "claude"
args = ["--model", "claude-opus-5-5"]

[[agents]]
name = "fablyn-reviewer"
kind = "codex"
args = ["-m", "gpt-6-astra"]
```

Validation (all errors reported together, exit 2): names match the herdr pattern and are unique; `kind` is one of the kinds the installed `herdr agent` prints (the doctor warns, `up` refuses); `args` is a list of strings; kickoff paths exist; `split` is `right` or `down`; `ratio` is between 0 and 1 exclusive.

Seat order is file order. Seat 1 occupies the anchor pane (see 4.1); each later seat splits from the previous seat's pane in its `split` direction.

## 3. Kickoff templates

A template is a text file (or `kickoff_text`) with `{placeholders}` expanded with Python `str.format_map` semantics; unknown placeholders are left as written. Available:

| Placeholder | Value |
| --- | --- |
| `{name}` `{kind}` `{args}` | the seat |
| `{repo}` `{cwd}` `{workspace}` | repo root, the seat's cwd, workspace label |
| `{roster}` | a Markdown table of every seat: name, kind, pane id, cwd |
| `{state}` | contents of `state_note` if it exists, else empty |
| `{note}` | the `--note` text given on the command line, else empty |
| `{reason}` | `up`, `restart`, `swap` or `kickoff` |

Every kickoff is sent as one `herdr agent prompt <name> "<text>"`. The text starts with `[from herdr-fleet]` unless the template already starts with `[from`. When no template is configured, the built-in default is used:

```
[from herdr-fleet] You are {name}, a {kind} agent in the "{workspace}" fleet for {repo}.
Read AGENTS.md there. This message was sent by the fleet tool on {reason}.
Team (herdr names; message them with `herdr agent prompt <name> "..."`):
{roster}
{state}
{note}
```

Kickoffs are sent only after every seat has been started, in file order, so the roster is complete. Put the orchestrator last in the file to brief it last (herdup's rule).

## 4. Operations

Common preconditions: `HERDR_ENV=1` or `HERDR_BIN_PATH` set, else refuse ("not running inside Herdr"); herdr reachable (`herdr status` is not JSON, so use `herdr agent list`); client version at least 0.9.4 (`herdr --version`).

### 4.1 up [NAME...] [--me NAME] [--dry-run] [--no-kickoff] [--note TEXT]

1. Load and validate the fleet file; load the state file (section 5) and drop entries whose pane or workspace no longer exists (`herdr pane get`, `herdr workspace get` fail).
2. Resolve the workspace. With `workspace = "label"`: find it in `herdr workspace list` by `label`; if absent, `herdr workspace create --cwd <repo cwd> --label <label> --no-focus` and record `created_workspace`. The returned `root_pane` becomes the anchor and is used for seat 1. Without `workspace`: use `HERDR_WORKSPACE_ID`; the anchor is `HERDR_PANE_ID`, and seat 1 gets a split from it like every other seat (never start an agent in the caller's pane).
3. `--me NAME` adopts the caller: the agent in `HERDR_PANE_ID` must be live and of NAME's kind; `herdr agent rename $HERDR_PANE_ID NAME`, record the pane, send no kickoff. Without `--me`, if the caller's pane hosts an unnamed agent, print a hint and leave it alone.
4. For each seat in order, skip if an agent with that name is already live (`herdr agent list`, field `name`). Otherwise:
   - If the state file records a pane and that pane hosts an unnamed live agent of the right kind (a herdr restart resumed it), rename it, record it, and skip the kickoff. If it hosts an agent of another kind, report a conflict and skip the seat.
   - Else ensure a pane: the recorded pane if it exists and is at a shell prompt (section 6), else `herdr pane split <previous seat pane> --direction <split> [--ratio r] --cwd <seat cwd> [--env K=V…] --no-focus` and record `created_pane = true`.
   - `herdr agent start NAME --kind KIND --pane PANE -- ARGS…`. Retry on `agent_pane_busy` every 250 ms for up to 10 s (a fresh pane's shell may still be printing its rc). On `agent_not_ready` (blocked during startup, typically a trust prompt): record the pane, mark the seat `blocked_at_startup`, notify (section 7), continue; never send anything to it. The user answers the prompt and runs `herdr-fleet kickoff NAME`.
5. Send kickoffs (section 3) to every seat started in this run, with `--note` text in `{note}` and `reason = up`, unless `--no-kickoff`.
6. Print a status table. Exit 0 if every seat is live and briefed, 3 if some were skipped or blocked, 1 on a hard failure.

`--dry-run` prints the plan (workspace, splits, exact `herdr agent start` argv, kickoff targets) and changes nothing.

### 4.2 status [--json]

For every seat: name, expected kind, live kind, pane id, herdr state (`idle`, `working`, `blocked`, `done`, `unknown`, or `absent`), drift (section 4.4), and `notes`. Then `herdr integration status` lines for the kinds the fleet uses, with `outdated` or `not installed` flagged. `--json` emits one object for agents to parse. In the overlay pane the same table is shown and the pane waits for Enter before closing.

### 4.3 restart NAME... [--note TEXT] [--note-file PATH] [--wait MS] [--dry-run]

For each name: resolve the live agent. Then:

- `blocked` or `unknown`: refuse this seat, say why, continue with the others.
- `working`: refuse unless `--wait MS`, which runs `herdr agent wait NAME --until idle --until done --until blocked --timeout MS` and then re-checks; `blocked` still refuses.
- `idle` or `done`: quit (section 6), then start again in the same pane with the same name, exactly as in `up` step 4, then kickoff with `reason = restart`, `{state}` from `state_note`, `{note}` from `--note` or `--note-file`.
- `absent` (no live agent): start it in the recorded pane if that pane is at a shell prompt, else in a fresh split; this is how a crashed agent comes back.

### 4.4 swap [NAME...] [--dry-run] [--note …] [--wait MS]

Drift for a live seat means: live kind differs from the file, or the live process argv (from `herdr pane process-info --pane PANE`, the foreground process whose `name` or `argv0` matches the kind's executable) does not contain the file's `args` in order, or the state file recorded different `kind`/`args` at start time. `swap` restarts exactly the drifted seats (and starts absent ones) via 4.3, and leaves the rest untouched. With no names, all seats are considered. `--dry-run` lists what would change and why.

### 4.5 kickoff NAME... [--note …]

Resend the kickoff with `reason = kickoff`. Refuses blocked, unknown and working agents (no `--wait` here: a kickoff to a working agent is a mistake).

### 4.6 down [NAME...] [--wait MS] [--close-panes] [--close-workspace]

Quit each live seat (section 6) following the same state rules as restart. `--close-panes` then closes only panes recorded with `created_pane = true`, with `herdr pane close`. `--close-workspace` closes the workspace only if `created_workspace` is recorded and every seat is gone. The state file is updated, never deleted, so `up` can reuse the layout.

### 4.7 doctor

Read-only. Checks: python version; herdr binary and version; `HERDR_ENV`; fleet file found and valid; every kind present in `herdr agent` output; every kickoff file readable; `state_note` readable if set; integration status for used kinds; a quit command known for every kind (section 6); the state file parseable. Prints one line per check with `ok`, `warn` or `fail`. Exit 1 on any `fail`.

### 4.8 install-cli [--dir DIR]

Symlinks `bin/herdr-fleet` into `DIR` (default `~/.local/bin`) so agents can run it by name. Prints the plugin root otherwise (`herdr plugin list --json` exposes it too).

## 5. State file

`<repo>/.herdr/fleet.state.json`, created on first `up`, plus `<repo>/.herdr/.gitignore` containing `fleet.state.json` (created if absent, line appended if missing). Reason for keeping it in the repo rather than `HERDR_PLUGIN_STATE_DIR`: both entry points (manifest action and CLI run by an agent) must find the same file, and herdr sets the state-dir variable only for the former. The file is per-repository like the fleet file, and agents can inspect it.

```json
{
  "version": 1,
  "repo": "/Users/nabsha/work/yarrasys/projects/emberline",
  "workspace_id": "wY",
  "workspace_label": "fablyn",
  "created_workspace": false,
  "agents": {
    "fablyn-dev": {
      "pane_id": "wY:p7",
      "created_pane": true,
      "kind": "claude",
      "args": ["--model", "claude-opus-5-5"],
      "started_at": "2026-10-19T20:05:11+11:00",
      "last_kickoff_at": "2026-10-19T20:05:40+11:00",
      "blocked_at_startup": false
    }
  }
}
```

Pane and workspace ids are valid for one herdr server. Every command verifies the ids it is about to use and drops stale ones. Writes are atomic (write to a temp file in `.herdr`, then rename).

## 6. Quitting an agent

herdr has no stop command for agents; `pane release-agent` is for state integrations. So quitting is per kind and goes through `herdr agent prompt`, which refuses a blocked pane before sending anything. That is the only way text reaches an agent in this plugin. `agent send-keys`, `pane send-keys`, `pane send-text` and `pane run` are never used on a pane that hosts an agent.

Default quit commands, overridable per seat (`quit = "..."`) or per kind in `$HERDR_PLUGIN_CONFIG_DIR/config.toml` under `[quit]`:

| kind | command |
| --- | --- |
| claude | `/exit` |
| codex | `/quit` |
| gemini | `/quit` |
| opencode | `/exit` |
| copilot | `/exit` |
| pi | `/exit` |

Any other kind without a configured command: refuse to quit, with the config hint.

Procedure: confirm the state is `idle` or `done` immediately before; `herdr agent prompt NAME "<quit>"`; then poll every 250 ms for up to `quit_timeout_ms` (default 20000) until `herdr agent get PANE` fails (no agent) and `herdr pane process-info --pane PANE` shows a foreground process whose `name` is a shell (`zsh`, `bash`, `fish`, `sh`, `nu`) with no other foreground entries. On timeout: report that the agent did not exit, do not retry, do not send keys, leave the seat alone.

"At a shell prompt" elsewhere in this document means that same process-info check.

## 7. Safety invariants

These are enforced in code and covered by tests:

1. Text reaches agents only through `herdr agent prompt`. No key or raw-text command is ever sent to a pane hosting an agent.
2. A `blocked` or `unknown` agent is never prompted, quit or restarted. `working` requires an explicit `--wait`.
3. A prompt that returns `agent_prompt_stalled` or `timeout` is reported and never resent automatically (herdr documents that the text may have been delivered).
4. Only panes recorded as `created_pane` are ever closed; only a workspace recorded as `created_workspace` is ever closed; both only on explicit flags.
5. An agent is started only in a pane the check in section 6 reports as at a shell prompt (herdr also enforces this with `agent_pane_busy`).
6. The caller's own pane is never started into; `--me` only renames.
7. Never run bare `herdr`.

Output: plugin commands print to stdout, which herdr shows nowhere, so when invoked as an action (`--notify` flag set by the manifest) the result summary also goes to `herdr notification show "<title>" --body "<text>" --sound done|request`. Blocked-at-startup seats always notify with `--sound request`.

## 8. Plugin manifest

```toml
id = "yarrasys.fleet"
name = "Fleet"
version = "0.1.0"
min_herdr_version = "0.9.4"
description = "Start, restart and swap a team of named coding agents from a per-repository fleet file (.herdr/fleet.toml)."
platforms = ["linux", "macos"]

[[actions]]
id = "up"
title = "Fleet: up"
contexts = ["workspace"]
command = ["python3", "bin/herdr-fleet", "up", "--notify"]

[[actions]]
id = "status"
title = "Fleet: status"
contexts = ["workspace"]
command = ["python3", "bin/herdr-fleet", "ui", "status"]

[[actions]]
id = "manage"
title = "Fleet: manage (restart, swap, kickoff, down)"
contexts = ["workspace"]
command = ["python3", "bin/herdr-fleet", "ui", "manage"]

[[actions]]
id = "doctor"
title = "Fleet: doctor"
contexts = ["global"]
command = ["python3", "bin/herdr-fleet", "doctor", "--notify"]

[[panes]]
id = "status"
title = "Fleet status"
placement = "overlay"
command = ["python3", "bin/herdr-fleet", "status", "--hold"]

[[panes]]
id = "manage"
title = "Fleet"
placement = "popup"
width = "70%"
height = "60%"
command = ["python3", "bin/herdr-fleet", "menu"]
```

`ui status` and `ui manage` run `herdr plugin pane open --plugin yarrasys.fleet --entrypoint <id>` through `HERDR_BIN_PATH`, passing `--cwd <repo root>` so the pane process can discover the fleet file from its own cwd. The popup `menu` is a plain numbered text menu on stdin (no curses): list seats with state, pick a seat or all, pick restart / swap / kickoff / down / up, confirm, run, show the result, wait for Enter.

## 9. Code layout

```
herdr-plugin.toml
bin/herdr-fleet                 # python entry; puts the plugin root on sys.path and calls herdr_fleet.cli.main
herdr_fleet/
  __init__.py
  cli.py          # argparse, subcommands, exit codes 0 ok / 1 failure / 2 usage or invalid fleet file / 3 partial
  herdr.py        # Herdr client: run(argv) -> parsed JSON or HerdrError(code, message); typed helpers
  fleetfile.py    # discovery, parsing (tomllib or vendored tomli), validation, dataclasses
  state.py        # state file load/save/prune
  kickoff.py      # templates, placeholders, roster table
  ops.py          # up, restart, swap, kickoff, down, status, doctor
  menu.py         # popup menu
  notify.py       # herdr notification show wrapper
  _vendor/tomli/  # vendored, with its LICENSE
tests/            # unittest; a fake `herdr` script driven by scenario files, so no live herdr is needed
examples/fablyn/.herdr/fleet.toml
examples/fablyn/docs/kickoff/*.md
docs/design.md docs/requirements.md docs/survey.md docs/testing.md
README.md LICENSE AGENTS.md
```

## 10. Testing

- Unit tests run with `python3 -m unittest discover -s tests` under the system Python 3.9 and under Homebrew 3.12. The fake herdr binary (`tests/fake_herdr.py`) records every argv to a log and answers from a scenario (JSON per expected call). Tests cover: discovery and validation; up on an empty workspace; up idempotence; adopt-on-resume; `agent_pane_busy` retry; `agent_not_ready` handling; restart refusing blocked, unknown and working; quit polling and timeout; swap drift detection from process-info argv; down closing only created panes; the safety invariants (assert the log never contains `send-keys`, `send-text` or `pane run`).
- `docs/testing.md` describes the live smoke test in a throwaway split of the designer's own workspace: a one-seat fleet of kind `claude` in this repository, then `status`, `restart`, `swap` after editing `args`, and `down --close-panes`.

## 11. Out of scope for 0.1

Automatic restarts on crash (needs a supervised process, which plugins must not leave behind), Windows, a curses UI, per-seat worktree creation (the orchestrator's brief already handles worktrees), and any approval of anything.

## 12. Amendments after the first build and smoke test (19 Oct 2026)

The first implementation (Opus subagent, reviewed and smoke-tested by the designer in the designer's own workspace) is accepted with these changes to the sections above. Where this section disagrees with an earlier section, this section wins.

Accepted deviations:

1. **Code layout (section 9).** `ops.py` is split into `ops.py` (operations), `bringup.py` (workspace resolution, bringing one seat up, kickoffs), `checks.py` (status, drift, doctor) and `seats.py` (shell check, start with retry, `safe_prompt`, quit). `context.py` holds invocation detection, preflight, config and the report.
2. **New workspace root pane (4.1 step 2).** `workspace create` uses the cwd and env of the first seat that needs a pane, and its root pane is reserved for that seat; otherwise that seat's `cwd` and `env` could never apply. The root pane is recorded with `created_pane = false`, so `down --close-panes` never closes it (closing it would close the workspace without `--close-workspace`).
3. **Workspace label reuse (4.1 step 2).** When several workspaces share the label, `up` prefers the one in the state file, then the caller's, and otherwise refuses with exit 1. When the labelled workspace exists but the caller is outside it, new seats split from the first pane of its active tab, which is never started into.
4. **Caller's own seat (invariant 6).** `restart` and `down` refuse a seat whose agent is in the caller's own pane; the caller quits itself. `--close-panes` closes a created pane only once it is back at a shell with no agent; `--close-workspace` also requires that no agent of any name is live there.
5. **Quit timeout.** `quit_timeout_ms` may be set at the top level of the fleet file (wins) or under `[quit]` in `config.toml`.
6. **Validation.** `kickoff` and `kickoff_text` together are an error; unknown keys warn; `restart` and `swap` refuse unknown kinds like `up` does.
7. **Drift (4.4).** "Args in order" means a contiguous run of the file's args inside the running argv; `cmdline` is the fallback when `argv` is absent.
8. **Kickoff delivery (section 3).** `agent prompt` is sent without `--wait`. herdr reports submission; the plugin does not wait for the turn to start.
9. **State after `down --close-panes`.** A seat whose pane was closed is dropped from the state file, since there is nothing left to reuse; the workspace entry stays.

Facts learned live (herdr 0.9.4):

- `HERDR_PLUGIN_CONTEXT_JSON` for an action: `{"workspace_id","workspace_label","workspace_cwd","tab_id","tab_label","focused_pane_id","focused_pane_cwd","focused_pane_agent","focused_pane_status","invocation_source","correlation_id"}`. `focused_pane_cwd` is there directly.
- `HERDR_PLUGIN_CONFIG_DIR` is `~/.config/herdr/plugins/config/<id>`; `HERDR_PLUGIN_STATE_DIR` is `~/.local/state/herdr/plugins/<id>` on macOS too.
- `herdr plugin log list` records each action's stdout, stderr, exit code and status, so action output is recoverable even though nothing is shown on screen.
- `pane get` and `pane list` records carry no agent `name`; `agent get <pane>` does. Every name check therefore goes through `agent get` (fixed after the smoke test; the first build read the name from pane records, which would have mis-adopted a named agent). Pane records do carry `label` once a pane has been renamed.
- `plugin pane open` returns the new pane at `result.plugin_pane.pane.pane_id`, and `--cwd` does set the pane process's cwd.
- Smoke test timings with a Claude seat: `up` 4.4 s, `restart` 5.8 s, `swap` 5.7 s, each including the quit, the start and the kickoff. `/exit` through `agent prompt` quits Claude Code cleanly and the shell is back within a second.
