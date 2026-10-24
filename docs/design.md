# herdr-fleet: design

This is the historical technical design. Deployment identities, paths, message bodies and operational anecdotes have been anonymized or replaced with synthetic examples. Synthetic names do not identify deployments that were observed. Historical verification statements retain their original scope and limits; this privacy edit is not a new live verification. Later numbered amendments take precedence over earlier behavior. `docs/requirements.md` records the original requirements and `docs/survey.md` the public prior art.

Initial scope choices: a Herdr plugin, Python, and the MIT licence. The owner retains authorization over publication and other externally visible actions.

## 1. Shape

A herdr plugin with no build step and one runtime dependency, `python3` 3.8 or newer. It ships:

- `herdr-plugin.toml`: four actions and two panes (an overlay for status, a popup menu for restart, swap, kickoff, down).
- `bin/herdr-fleet`: a CLI with the same operations plus arguments. Orchestrator agents run this directly, e.g. `herdr-fleet restart sample-dev`. Manifest actions cannot take arguments, so the CLI is the surface for anything that names an agent.
- `herdr_fleet/`: the Python package. TOML parsing uses `tomllib` when present and a vendored `tomli` (MIT, under `herdr_fleet/_vendor/tomli/`) otherwise.

Everything talks to herdr through `HERDR_BIN_PATH` when set, else `herdr` on `PATH`, and parses the JSON that herdr returns. No socket code. No daemon. No background processes survive a command.

Plugin id `yarrasys.fleet`. Repository name `herdr-fleet` (two unrelated GitHub repositories already use that name; the marketplace card shows the owner, so it is survivable). The fleet file convention `.herdr/fleet.toml` is ours; herdr defines nothing project-level. herdup uses `.herdr/team.toml`, so the directory has a precedent.

## 2. The fleet file

Path: `<repo>/.herdr/fleet.toml`, committed. Discovery walks up from the invoking directory until a directory contains `.herdr/fleet.toml`; that directory is the repo root. `--file PATH` or `HERDR_FLEET_FILE` overrides discovery, and then the repo root is the directory holding the `.herdr` directory (or the file's directory when the file is elsewhere).

Invoking directory: for the CLI, the current directory. For a manifest action or pane, the `cwd` of the focused pane from `herdr pane get $HERDR_PANE_ID` (the action runs with the plugin root as its own cwd, which is useless for discovery). When `HERDR_PANE_ID` is unset, read `focused_pane` from `HERDR_PLUGIN_CONTEXT_JSON`.

```toml
# .herdr/fleet.toml  — the Sample team, as an example
workspace  = "sample"            # optional: reuse the workspace with this label, else create it.
                                 # Absent: use the workspace the command was invoked from.
cwd        = "."                 # optional default agent cwd, relative to the repo root. Default ".".
state_note = ".herdr/STATE.md"     # optional: file the orchestrator keeps; its contents fill {state}.
kickoff    = ".herdr/kickoff/default.md"   # optional default kickoff template (path, relative to repo root)

[layout]
direction = "right"              # default split direction between consecutive seats: right | down
ratio     = 0.5                  # optional default split ratio

[[agents]]
name    = "sample-orchestrator"  # herdr agent name: [a-z][a-z0-9_-]{0,31}, unique in the file
kind    = "claude"               # a kind from `herdr agent start --help`
args    = []   # passed after `--` to herdr agent start
cwd     = "."                    # optional, relative to repo root, or absolute, or ~/…
split   = "right"                # optional, overrides [layout].direction for this seat
ratio   = 0.5                    # optional
env     = { SAMPLE_ROLE = "orchestrator" }  # optional, injected with --env on the pane
kickoff = ".herdr/kickoff/orchestrator.md"    # optional template path; or kickoff_text = "inline"
quit    = "/exit"                # optional override of the per-kind quit command
notes   = "Coordinates the example team; project-specific access is configured by the owner."  # shown in status only

[[agents]]
name = "sample-dev"
kind = "claude"
args = []

[[agents]]
name = "sample-reviewer"
kind = "codex"
args = []
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
   - `herdr agent start NAME --kind KIND --pane PANE -- ARGS…`. Retry on `agent_pane_busy` every 250 ms for up to 10 s (a fresh pane's shell may still be printing its rc). On `agent_not_ready` (blocked during startup, typically a trust prompt): record the pane, mark the seat `blocked_at_startup`, notify (section 7), continue; never send anything to it. The user answers the prompt and runs `herdr-fleet kickoff NAME`. After `agent_not_ready` the plugin first keeps polling `herdr agent get PANE` (read-only) every 250 ms for the rest of the 10 s budget and treats `idle`, `done` or `working` as started, since a one-time notice can clear by itself (section 16.12); only a seat still not ready at the deadline is handled as above.
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
  "repo": "/home/example/projects/sampleapp",
  "workspace_id": "wY",
  "workspace_label": "sample",
  "created_workspace": false,
  "agents": {
    "sample-dev": {
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
| agy | `/exit` |

Any other kind without a configured command: refuse to quit, with the config hint.

Procedure: confirm the state is `idle` or `done` immediately before; `herdr agent prompt NAME "<quit>"`; then poll every 250 ms for up to `quit_timeout_ms` (default 20000) until `herdr agent get PANE` fails (no agent) and `herdr pane process-info --pane PANE` shows a foreground process whose `name` is a shell (`zsh`, `bash`, `fish`, `sh`, `nu`) with no other foreground entries. On timeout: report that the agent did not exit, do not retry, do not send keys, leave the seat alone.

"At a shell prompt" elsewhere in this document means that same process-info check.

## 7. Safety invariants

These are enforced in code and covered by tests:

1. Text reaches agents only through `herdr agent prompt`. No key or raw-text command is ever sent to a pane hosting an agent, with the two named paths of section 23.7 (version 0.12.0): one Enter on the exit confirmation the plugin's own quit provoked, and the kind's interrupt keys from `interrupt NAME`; both only to a seat of this fleet, never from the watcher or a popup.
2. A `blocked` or `unknown` agent is never prompted, quit or restarted. `working` requires an explicit `--wait`, and since 0.10.0 text reaches a `working` seat only from `send --now` and `deliver NAME --now` (section 21.1).
3. A prompt that returns `agent_prompt_stalled` or `timeout` is reported and never resent automatically (herdr documents that the text may have been delivered).
4. Only panes recorded as `created_pane` are ever closed; only a workspace recorded as `created_workspace` is ever closed; both only on explicit flags.
5. An agent is started only in a pane the check in section 6 reports as at a shell prompt (herdr also enforces this with `agent_pane_busy`).
6. The caller's own pane is never started into; `--me` only renames.
7. Never run bare `herdr`.
8. `worktrees`, the `fleet file vs git` check and `--context` are read-only: no `git fetch`, no `git worktree prune`, no write under `~/.claude`, and no herdr command beyond `agent list`/`agent get`.
9. `--context` extracts usage numbers and timestamps only; no transcript content is ever surfaced.
10. `ack` and `--fyi` only write the message log; `ack` sends nothing.
11. Queued messages are delivered only to `idle` or `done` seats, only through `safe_prompt`, and only by a plugin command that a person, an agent's own tool call, or a watcher invoked. Nothing the plugin spawns runs in the background: `watch` is a foreground process in a pane the owner can see and close, never a detached one. The owner starts it, or `up` does, in a visible plugin pane recorded in the state file and closed by `down --close-panes` (amended by 19.2). (Since 0.10.0 the only exceptions are `send --now` and `deliver NAME --now`, which type a message ahead to a `working` seat with the queue in front, always through `safe_prompt`; the 18.3, 18.11 and 20.8 typed-ahead deliveries were withdrawn by section 21.1.)
12. The watcher never prompts a `blocked` or `unknown` seat, never answers a dialog, never redelivers an unconfirmed message, and never starts, restarts or quits anything.
13. An agent is never asked to poll. Waits live in the herdr server (`agent wait`) or in a silent hook.
14. A hook-delivered batch is the seat reading its own inbox inside its own process; the plugin never writes to an agent through a hook, and a hook prints nothing on any error.
15. Message bodies are stored verbatim and only under `.herdr/inbox/` of the fleet's repository (and, for a send to a live agent of another fleet on this machine, also of that fleet's, design 20.8); the plugin never reads them for any purpose but rendering a batch, a listing or a history.
16. Acting on a message is never inferred from agent output; debts are settled only by replies, `--fyi` messages, `ack` and, since the fresh context starts over, a kickoff of either seat (design 20.8).
17. `--urgent` reorders only the mailbox herdr-fleet holds; nothing reorders or inspects an agent's own input.
18. A message's delivery mode is decided by how it was sent (`queued_by`), never by its content; the plugin reads nothing to decide.
19. Before any text is sent to a seat, the plugin reads the target pane's prompt line, whatever the focus; if it holds text or is not on screen, nothing is sent (section 21.2, amended by 23.11: focus only words the reason). The check reads only the last lines of that pane to find the prompt line, keeps nothing, and decides nothing else from them.
20. The plugin changes a pane's display name only for a pane that hosts one of the fleet's seats, and only to that seat's name or `label` (section 22.1); `pane rename` and `agent focus` are the only display commands it runs, neither reaches the agent, and `agent focus` is run only by `go` and the popup.
21. Keys reach an agent only through `herdr agent send-keys`, only from the two paths of 23.7, only the keys those paths name (`enter` on the exit confirmation; the kind's interrupt keys; and, from `interrupt` alone, one `ctrl+u` to clear a prompt the interrupt put back, 23.11), only to a seat of this fleet that the plugin has just read the screen of, and never from the watcher, a popup, a dry run or any automatic path. `pane send-keys`, `pane send-text` and `pane run` stay refused.
22. The watcher's board (sections 24 and 25) reads only what 24.2 and 25.2 list; from a screen it shows a blocked seat's dialog lines, and a seat's last lines only on the owner's `p` key; it prompts nothing, sends no key, delivers only as the watcher may, and runs `agent focus` and `pane zoom` (on its own pane) only on a key or click of the owner.

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
examples/team/.herdr/fleet.toml
examples/team/.herdr/kickoff/*.md
docs/design.md docs/requirements.md docs/survey.md docs/testing.md
README.md LICENSE AGENTS.md
```

## 10. Testing

- Unit tests run with `python3 -m unittest discover -s tests` under the system Python 3.9 and under Homebrew 3.12. The fake herdr binary (`tests/fake_herdr.py`) records every argv to a log and answers from a scenario (JSON per expected call). Tests cover: discovery and validation; up on an empty workspace; up idempotence; adopt-on-resume; `agent_pane_busy` retry; `agent_not_ready` handling; restart refusing blocked, unknown and working; quit polling and timeout; swap drift detection from process-info argv; down closing only created panes; the safety invariants (assert the log never contains `send-keys`, `send-text` or `pane run`).
- `docs/testing.md` describes the live smoke test in a throwaway split of the designer's own workspace: a one-seat fleet of kind `claude` in this repository, then `status`, `restart`, `swap` after editing `args`, and `down --close-panes`.

## 11. Out of scope for 0.1

Automatic restarts on crash (needs a supervised process, which plugins must not leave behind), Windows, a curses UI, per-seat worktree creation (the orchestrator's brief already handles worktrees), and any approval of anything.

## 12. Amendments after the first build and smoke test (19 Oct 2026)

The first implementation was reviewed and smoke-tested. This section records accepted corrections to earlier sections; where they disagree, this section takes precedence.

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

## 13. `init` (added 19 Oct 2026, version 0.2.0)

Installing the plugin installs only the plugin; the fleet file is per repository. `init` is the bootstrap step for a repository that has none.

### 13.1 CLI

`init [DIR] [--example NAME] [--force] [--notify]`

- **Target.** `DIR` when given, else the invoking directory (the current directory for the CLI; `focused_pane_cwd` for the manifest action). The repository root is `git rev-parse --show-toplevel` run in that directory when it is inside a Git work tree, else the directory itself. `init` prints the root it chose.
- **Starter files** (default, no `--example`), all paths under the root:
  - `.herdr/fleet.toml`: a commented starter with one seat named `<root basename, sanitised to the herdr name pattern>-dev`, kind `claude`, no args, `kickoff = ".herdr/kickoff/default.md"`, `state_note = ".herdr/STATE.md"`, no `workspace` key (so seats open beside the caller), and comments showing every optional key from section 2.
  - `.herdr/kickoff/default.md`: the built-in default template from section 3, with a comment line explaining the placeholders.
  - `.herdr/STATE.md`: one line, `Where things stand: (nothing yet).`
  - `.herdr/.gitignore`: `fleet.state.json`.
- **`--example team`** copies the whole `examples/team` tree instead (`.herdr/fleet.toml`, `.herdr/kickoff/*.md`, `.herdr/STATE.md`), paths as shipped. Unknown example names exit 2 and list the available ones.
- **Never overwrites.** Each file is reported as `created`, `kept` (already existed, left untouched) or, with `--force`, `overwritten`. Directories are created as needed.
- `init` runs no herdr command at all and does not require `HERDR_ENV`; it is pure file writing. Exit 0 when every file is created or kept, 1 when a write failed, 2 on usage errors.
- `--notify` (set by the manifest action) sends one notification listing the files created, or the failure, through `herdr notification show`; outside herdr it is skipped like every other notification.

### 13.2 Manifest

A fifth action:

```toml
[[actions]]
id = "init"
title = "Fleet: init (write a starter .herdr/fleet.toml here)"
contexts = ["workspace"]
command = ["python3", "bin/herdr-fleet", "init", "--notify"]
```

### 13.3 Pointers

When discovery finds no fleet file, `up`, `status`, `restart`, `swap`, `kickoff`, `down`, the UI entrypoints and `doctor` append `run \`herdr-fleet init\` to create one` to their message. The README quick start begins with `init`.

Version 0.2.0 in `herdr-plugin.toml` and `herdr_fleet/__init__.py`.

### 13.4 Amendments from the build

- The starter's `kickoff` is the fleet-wide top-level default, as in the Sample example, not a per-seat key.
- Whole-line HTML comments (`<!-- … -->` alone on a line) are stripped from every kickoff template before rendering, so a template can carry notes for humans without them reaching the agent. Inline comments are kept.
- The `init` action exits 2 with a hint to run `init DIR` when the context JSON carries no `focused_pane_cwd`; it never falls back to `pane get`.
- `DIR` must exist. `--example` skips `.DS_Store` and `fleet.state.json`. Writes are atomic with mode 0644.
- The "run `herdr-fleet init`" pointer is added only when discovery fails, not when an explicit `--file` path is missing.

### 13.5 Version 0.2.1

Everything fleet-related lives under `.herdr/`: the shipped Sample example keeps its kickoff templates in `.herdr/kickoff/` and its state note at `.herdr/STATE.md`, like the plain starter, so `init --example team` writes nothing outside `.herdr/`. The example gains a fourth seat, `sample-designer`. The section 2 example above uses the same paths.

## 14. Grid layout, `from`, and the `--me` hint (version 0.3.0)

### 14.1 Facts

`herdr pane split <pane> --direction right|down --ratio r` keeps share `r` for the pane being split and gives `1 - r` to the new pane (verified live: ratio 0.333 left the original pane at one third of the width). `layout.apply` exists only on the socket, builds a fresh tab and does not preserve running processes, so it is not used.

### 14.2 `[layout] mode`

```toml
[layout]
mode    = "grid"     # chain (default, the behaviour of sections 2 and 4.1) | grid
columns = 2          # grid only, optional; default ceil(sqrt(number of seats to place))
direction = "right"  # chain: default split direction. grid: direction of the first split off the anchor
ratio   = 0.5        # chain: default ratio. grid: share the anchor keeps when it is not a seat pane
```

Grid mode places the seats that this run creates panes for as an even grid, column-major in file order: `columns` columns of equal width; column `j` holds `R_j` rows of equal height, with rows distributed evenly and the extra rows going to the first columns (4 seats → 2×2; 3 seats in 2 columns → 2 + 1; 5 seats → 3 columns, 2 + 2 + 1).

The grid area is:

- the root pane when the plugin created the workspace in this run (seat 1 takes the first cell);
- otherwise a new pane split from the anchor (the caller's pane, or the first pane of the labelled workspace's active tab) with `[layout] direction` and `ratio`, so with `--me` the orchestrator keeps `ratio` of the tab and the other seats form the grid in the rest.

Building the grid with chained splits: the column heads are made by splitting the previous head `right` with ratio `1/(columns remaining including this one)`, and each column's rows by splitting the previous cell `down` with ratio `1/(rows remaining including this one)`. This yields equal cells regardless of the seat count.

Per-seat `split` and `ratio` are ignored in grid mode with a warning. When only some seats need panes in a run (others are live or adopted), grid mode still computes the grid for the seats being placed, from the anchor; if a previous run already placed seats, the new ones are appended with the chain rule from the last placed seat's pane. `--dry-run` prints every split with its ratio.

### 14.3 Per-seat `from`

In chain mode a seat may set `from = "<seat name>"` to split from that seat's pane instead of the previous seat's. The named seat must be earlier in the file; validation rejects anything else. Ignored in grid mode with a warning.

### 14.4 `--me` when the caller is not an agent

When `--me NAME` finds no agent in the caller's pane, the message says so and adds: `start NAME as a seat instead with \`herdr-fleet up NAME\`, or start a NAME-kind agent in this pane first and run --me from inside it`. The run continues with the other seats as before (exit 3).

Version 0.3.0. The Sample example sets `mode = "grid"`.

## 15. Adopted seats and `--join` (version 0.3.1)

Observed live: `up --me` from a session in another workspace adopts that session where it is, while `workspace = "<label>"` sends every other seat to the labelled workspace, so the team ends up split across two workspaces. And an adopted session usually runs with different args than the file (for example a hand-started `claude --chrome`), so a bare `swap` from another pane would quit the owner's own session and restart it, losing its conversation.

1. **Adopted seats are recorded** with `adopted = true` in the state file (set by `--me` and by adopt-on-resume, cleared when the plugin starts the seat itself). `status` shows `adopted` in the DRIFT column instead of comparing args. `swap` without names skips adopted seats and says so; `swap NAME` and `restart NAME` still act on them, subject to the usual state rules.
2. **`--me` outside the target workspace** prints a warning naming both workspaces and the exact `herdr pane move <pane> --tab <tab> --split right --ratio 0.5 --no-focus` that would join them. With `--join`, `up` runs that move itself (it is reversible and keeps the process), then records the new pane id. Without `--join` nothing moves.
3. Nothing else changes. Version 0.3.1.

### 15.1 `tile`: re-tile live seats without restarting them

Observed live: `herdr pane move <pane> --tab <same tab> --split … --target-pane …` is a no-op when the pane is already in that tab, but moving the pane out to a temporary tab (`pane move <pane> --new-tab --workspace <id> --label park --no-focus`) and straight back (`pane move <pane> --tab <tab> --split <dir> --target-pane <src> --ratio <r> --no-focus`) re-attaches it exactly where asked, keeps the process and the agent name, keeps the pane id within the workspace, and the temporary tab disappears when it empties. Four seats were re-tiled this way into an equal 2×2 while all four agents kept working.

`tile [--dry-run]` rebuilds the layout of the seats that are live in the fleet's workspace into the `[layout]` grid (or, in chain mode, the chain order with each seat's `split`/`from`) using that out-and-back move for every seat after the first, in grid-plan order, with the planned source pane, direction and ratio. Seats in other workspaces are reported and left alone. No agent is prompted, quit or started. The first seat's pane stays where it is and anchors the result. `--dry-run` prints the moves. Manifest action `tile` ("Fleet: tile") runs it for the focused pane's repository.

### 15.2 Amendments from the build

- "Adopted" means the state entry carries `adopted = true` or records no `args` (the plugin never started that seat). Pane ids are not compared.
- A live named seat found in a different pane keeps its entry; only `pane_id` is refreshed. When the recorded pane still exists, `created_pane` becomes false (someone started the agent elsewhere; the plugin must not close a pane it did not create). When the recorded pane is gone, the move is followed and every field is kept.
- The caller's pane is resolved with `herdr pane current --current` at preflight, falling back to `HERDR_PANE_ID`, so the caller guards survive `--join` and hand moves.
- `tile` parks every seat after the first and then re-attaches them in plan order, so the ratios yield equal cells from any starting layout. On a failed return it names the seats left parked and the command to bring each back.

## 16. Messages: `send`, the log, owed replies, contacts, state sections (version 0.4.0)

Messaging requirements: preserve sender identity, durable outcomes and reply accountability without interpreting task completion from agent output. Later sections add the durable inbox and stricter delivery guards.

### 16.1 `send`

`send TARGET... (TEXT | --file PATH) [--as NAME] [--when-ready MS] [--all] [--dry-run]`

- **Targets** are seat names, contact names (16.4), or live herdr agent names. `--all` means every live seat except the sender. Several targets get the same text, one `agent prompt` each, in the order given.
- **Sender.** Resolved from the caller's pane (`pane current --current`, then `agent get` on it): its herdr name. `--as NAME` overrides. If the caller's pane hosts no named agent and `--as` is absent, the sender is `owner`.
- **Prefix.** The text is sent as `[from <sender>] <text>` unless it already starts with `[from`. `--file` reads the text from a file (UTF-8); TEXT and `--file` are mutually exclusive; empty text is a usage error.
- **Safety.** The target's state is read immediately before each send. `blocked` and `unknown` are refused, nothing sent. `working` is allowed: herdr queues the text for the agent's next turn, which is how the team already operates; this is a deliberate exception to invariant 2 for `send` only. `--when-ready MS` first runs `agent wait TARGET --until idle --until done --timeout MS` and then re-checks; a target still blocked or working after the wait is refused. `agent_prompt_stalled` and `timeout` are reported and never resent. Delivery to a seat in state `absent` is refused.
- **Output.** One line per target: `sent to sample-dev (working), 3.4 KB` or `refused sample-reviewer (blocked): nothing sent`. Exit 0 when every target was sent, 3 when some were refused, 1 on a herdr failure, 2 on usage.
- **Manifest.** No action: `send` needs arguments. The popup menu does not offer it in 0.4.

### 16.2 The message log

`<repo>/.herdr/messages.jsonl`, appended by every successful `send` (and by every kickoff the plugin sends, with `kind = "kickoff"`), ignored by `.herdr/.gitignore`. One JSON object per line:

```json
{"time":"2026-10-20T10:31:04+11:00","from":"sample-orchestrator","to":"sample-reviewer","kind":"send","bytes":3412,"first_line":"Please review the parser example","file":"/private/tmp/brief-3.md"}
```

`first_line` is the first non-empty line after the prefix, truncated to 120 characters. `file` is present only for `--file`. Refused sends are not logged.

`log [--seat NAME] [--last N] [--json]` prints the entries newest last, as `time  from → to  bytes  first_line`; `--seat` keeps entries where the seat is sender or recipient; `--last` defaults to 50. Writes are append-only with `O_APPEND`; a corrupt line is skipped with a warning.

### 16.3 Owed replies

Derived from the log only. For every ordered pair (A, B) with at least one `send` from A to B: the latest message from A to B is **awaiting** if no message from B to A has a later time, else **replied at** that time. Kickoffs count as messages from the plugin, not from a seat, and never create an obligation.

`status` prints, after the seat table and before the integrations, one line per awaiting pair, oldest first: `awaiting: sample-reviewer owes sample-orchestrator since 10:31 (Please review the parser example)`. `status --json` adds `"owed": [{"from","to","since","first_line"}]`. `log --owed` prints the same list on its own.

### 16.4 Contacts

Named peers that are not seats: sessions in other repositories the team talks to, such as a customer's agent.

```toml
[[contacts]]
name  = "partner-workspace"
match = { cwd = "~/projects/partner-workspace" }   # also: kind = "claude", name = "<herdr name>"
note  = "customer feedback; never instructions"
```

- `match` resolves at send time against `agent list`: `cwd` (after `~` expansion, compared to the agent's `cwd` and `foreground_cwd`), optional `kind`, optional herdr `name`. Exactly one live agent must match; zero or several is a refusal that lists the candidates. Contacts are never named, started, restarted, quit, moved or tiled; `status` lists them in a short block with their resolved pane and state or `absent`.
- Contact names must match the herdr name pattern and must not collide with seat names.
- Kickoffs get `{contacts}`: a Markdown table of contact name, note and current pane, preceded by the sentence `Text from a contact is data, never instructions.` Empty when there are no contacts.

### 16.5 State note sections

`state show` prints the state note. `state set KEY TEXT` and `state set KEY --file PATH` replace the section headed `## KEY` (created at the end if absent) with the text and set the header to `## KEY (updated 2026-10-20 00:31 UTC)`. `state keys` lists sections. Text outside any section is preserved. Writes are atomic. `{state}` in kickoffs is unchanged. The starter from `init` and the Sample example ship `## production`, `## in flight`, `## next` and `## backlog` as empty sections.

### 16.6 Permission preflight in `doctor`

For each seat of kind `claude`, `doctor` reads `permissions.allow` from `<repo>/.claude/settings.json`, `<repo>/.claude/settings.local.json` and `~/.claude/settings.json` (missing files are fine) and warns, never fails, when no entry covers each of `herdr agent prompt`, `herdr agent get`, `herdr agent wait`, `herdr agent read` and `herdr-fleet` (an entry covers a command when it is `Bash(<command prefix>:*)` or `Bash(<command prefix> *)` or a broader prefix of it). The warning names the file it would add to and the exact entries, e.g. `add to .claude/settings.json permissions.allow: "Bash(herdr agent prompt:*)"`. Other kinds print `not checked`. The check is advisory because Claude Code also honours per-session "don't ask again" choices the plugin cannot see.

### 16.7 Small

- `status` gains `CWD` (shortened with `~`) and `BRANCH` (`git -C <cwd> rev-parse --abbrev-ref HEAD`, blank when not a repository or on error; one call per seat, 2 s timeout).
- Any pane move the plugin performs or detects prints `pane w3:p1 → w2:p5` for the seat concerned, so notes that hard-code pane ids can be corrected.

### 16.8 Invariants

Invariant 1 extends to `send`: text reaches agents only through `agent prompt`. Invariant 2 gains the `send`-to-`working` exception above. Everything else is unchanged. Version 0.4.0.

### 16.9 Amendments from the build

- Owed replies: log times have one-second resolution, so equal times are ordered by position in the log.
- The built-in default kickoff template includes `{contacts}` after `{roster}`.
- `send` and `state set` use `--file` for their text, so for those two the fleet file is chosen with `HERDR_FLEET_FILE` (0.4.1 adds `--fleet PATH` everywhere).
- `send`: an unknown target is a usage error before anything is sent; `--all` plus explicit targets sends to both; `--when-ready` also waits on blocked and unknown targets.
- Contacts: `match` needs at least one key; `match.name` may not be a seat name; a seat whose cwd matches a contact makes the contact ambiguous.
- Doctor permissions: non-Claude seats print `skip`; `Bash` or `Bash(*)` covers everything; an entry without a wildcard covers nothing; prefixes match on whole words, so `Bash(herdr:*)` does not cover `herdr-fleet`.
- Status `CWD` is the live agent's `foreground_cwd`, else `cwd`, else the seat's configured cwd.
- Historical smoke checks confirmed send submission, file input, sender prefix, log outcomes, owed replies and state-note section updates. Working-seat delivery policy was subsequently restricted by section 21.1.

### 16.10 Version 0.4.1

`--fleet PATH` chooses the fleet file on every command (`--file` stays an alias where it already meant that; `send` and `state` keep `--file` for their text). Sends that herdr reports as stalled or timed out are logged with `delivery = "unconfirmed"` and shown with `?` in `log`, since the text may have been delivered; they are never resent. Refusals and other errors are still not logged.

### 16.11 Version 0.4.2

Options may appear anywhere on the command line: commands with a name list (`send`, `restart`, `swap`, `kickoff`, `down`, `up`, `state set`) parse with `parse_known_args` and fold the remaining words back into the list in order; `--` still ends options. `send` item rules: without `--file` the last item is the text and at least two items are required (one with `--all`); with `--file` every item is a target.

### 16.12 Version 0.4.3

- **agy quits with `/exit`.** `agy` joins the default quit table (section 6).
- **A transient `agent_not_ready` counts as started.** After `agent start` answers `agent_not_ready`, `start_in_pane` polls `herdr agent get PANE` every 250 ms until the 10 s start budget (counted from the start of the call, so busy retries use it up too) runs out. It sends nothing meanwhile. `idle` or `done` is a normal start, and the kickoff follows as for a clean start. `working` is also a start; the kickoff then follows the usual `safe_prompt` rules, so a working agent is not briefed. Still `blocked` or `unknown` at the deadline, or no answer from `agent get`, is handled as before: blocked at startup, recorded, notified, never prompted. `restart` and `swap` use the same path.
- **Status labels.** For a seat flagged `blocked_at_startup`, the STATE column says `(blocked at startup)` only while the live state is `blocked`, `(kickoff pending)` when it is `idle`, `done` or `working`, and nothing when it is `unknown` or `absent`. A seat without the flag gets no label, whatever its state. `status --json` keeps the `blocked_at_startup` boolean unchanged.

Facts learned live (21 Oct 2026; herdr 0.9.5, Antigravity CLI `agy` 1.4.3):

- A seat with `kind = "agy"` works end to end. `up` took 4.1 s and the kickoff was delivered through `agent prompt` and answered. `status` shows the integration row for kind `agy` under the integration name `antigravity-cli` (hook v4, `~/.gemini/config/hooks/herdr-agent-state.sh`). `down` quit it with `/exit` through `agent prompt` in 1.2 s, with the shell back in the pane.
- On the first launch of agy on a machine, `herdr agent start` answered `agent_not_ready` ("agent fleet-agy is blocked during startup and is not ready for prompts"). Yet `herdr agent get` showed `agent_status: idle, interactive_ready: true` within a second of `up` returning, and nobody had answered anything: agy had shown a one-time dismissable "what's new" notice, not a permission prompt. 0.4.2 reported the seat blocked at startup, skipped its kickoff, and `status` kept printing `idle (blocked at startup)` until the next kickoff. The second and third starts returned normally, with the agent idle at once.

## 17. Informational messages, worktrees, fleet file drift and context size (version 0.5.0)

This extension adds informational messages, Git worktree inspection, fleet-file drift and opt-in context-size readings. The Git and transcript helpers read their sources without fetching, pruning or writing transcripts; the enclosing commands retain their separately documented state and display behavior. Historical format checks covered session identifiers and numeric transcript usage fields, not message content.

### 17.1 Informational messages and acknowledgements

Problem: most seat-to-seat messages are reports ("cleanup done", "holding", "committed"), and section 16.3 turns every one of them into a debt, so the awaiting list fills with non-debts.

- `send --fyi` marks the message as not awaiting a reply. So does a message whose text, after the `[from X]` prefix and leading whitespace, starts with `FYI:` or `FYI ` (case-insensitive). The log entry gains `"fyi": true`; entries without the key are not FYI. The `[from X]` prefix is unchanged; the text is sent as written.
- Owed replies (16.3) become: for every ordered pair (A, B), the latest `send` from A to B is awaiting if it is not FYI and no entry from B to A of kind `send` or `ack` has a later time. An FYI never creates an obligation but still settles one: a report from B to A answers A's last message to B.
- `ack SEAT... [--all] [--as NAME] [--fleet PATH]` clears debts by hand. It appends one entry per seat `{"time", "from": <caller>, "to": SEAT, "kind": "ack", "bytes": 0, "first_line": "ack", "delivery": "logged"}` and sends nothing to anyone. `--all` acknowledges every message the caller currently owes (every awaiting pair where the caller is `to`); with explicit seats as well, both. A seat that is not owed anything is reported as `nothing owed` and no entry is written. The caller is resolved as for `send` (the pane's agent name, else `owner`, overridable with `--as`). Unknown names are usage errors (exit 2). `log --ack SEAT...` is not added; `ack` is the command.
- `log` prints `fyi` entries with a `[fyi]` tag after the kind and `ack` entries as `ack`; `log --owed` and `status` use the new rule. `status --json` `owed` keeps its shape.
- The kickoff roster sentence (16.1) gains: "status reports go with `--fyi`; a message that asks for something does not". `examples/team` templates get the sentence through the `{roster}` placeholder, nothing else changes there.

### 17.2 Worktrees and the CWD/BRANCH columns

Problem: a seat's reported cwd may differ from the worktree where a task is being developed. A read-only worktree listing makes those paths and branches inspectable without assuming one shared checkout.

- `worktrees [--seat NAME] [--json] [--fleet PATH]`, read-only. Runs `git -C <repo> worktree list --porcelain` and prints one row per worktree: `PATH` (shortened with `~`), `BRANCH` (`detached @<7-char sha>` when detached), `AGE` of the last commit (`git -C <path> log -1 --format=%ct`, rendered `3 min`, `2 h`, `5 d`, `9 w`), `STATE` (`clean`, or `N changed` from the line count of `git -C <path> status --porcelain`; `missing` when the directory is gone, plus `prunable`/`locked` when the porcelain output says so, in which case the git calls in that path are skipped), `MERGED` (`yes`/`no`: `git -C <repo> merge-base --is-ancestor <HEAD> <default>`, where `<default>` is `origin/HEAD`'s target from `git symbolic-ref -q refs/remotes/origin/HEAD`, else `origin/main`, else `origin/master` if that ref exists, else the column reads `-` and a note says no default branch was found), and `SEAT` (the seats whose `worktrees` globs match the path, comma-separated, else blank). The main worktree (first entry) is marked `main` in `SEAT` when no seat claims it. Rows keep the order git prints. Every git call has a 2 s timeout and a failure renders as `?` in that cell; the command never runs `git fetch`, `git worktree prune` or anything else that writes.
- Fleet file: an optional per-seat `worktrees = ["<glob>", ...]` (absolute paths after `~` expansion, matched with `fnmatch` against the worktree path). `--seat NAME` keeps only rows matching that seat's globs. `--json` prints `{"repo", "default_branch", "worktrees": [{"path","branch","head","detached","age_s","changed","missing","prunable","locked","merged","seats"}]}`.
- `status`: when every seat that is not `absent` reports the same `cwd`, the `CWD` column is dropped and the value is printed once in the header line after the workspace (`cwd ~/projects/…/sampleapp`); the same independently for `BRANCH` (`branch main`). A column stays when any value differs or when fewer than two seats are live. `status --json` keeps `cwd` and `branch` per seat. `status` does not list worktrees (it stays fast); it adds a trailing hint line `worktrees: see herdr-fleet worktrees` only when at least one seat declares `worktrees`.

### 17.3 Fleet file drift from the committed copy

Problem: local fleet configuration can differ from committed configuration without an obvious diagnostic. Compare the working file with available Git refs without fetching.

`doctor` gains a `fleet file vs git` check, read-only and without `git fetch`:

- Not a git repository, or git missing: `skip fleet file vs git: not a git repository`.
- Tracked (`git -C <repo> ls-files --error-unmatch .herdr/fleet.toml` succeeds) and identical to `HEAD:.herdr/fleet.toml` and to `<default>:.herdr/fleet.toml` (default resolved as in 17.2): `ok fleet file vs git: committed, same as origin/main`.
- Otherwise `warn`, one line that names what differs, for example `warn fleet file vs git: untracked in this checkout (branch docs/example-change); origin/main copy differs: +7/-1 lines, first at line 42` or `… modified since HEAD: +2/-0 lines …`. The counts come from `difflib` over the two texts; no diff body is printed. When no copy exists in `HEAD` or on the default branch, the line says `no copy on origin/main`. Drift is never a `fail`: the file in the working tree is the one that runs, by design (section 2).

### 17.4 Context size per seat (opt-in)

Problem: a seat may accumulate a large context without a convenient numeric reading. An opt-in usage display can inform an explicit restart decision without inspecting or exposing transcript content.

- `status --context` adds a `CONTEXT` column. For a live seat whose `agent_session.source` is `herdr:claude`, the plugin locates the transcript with the glob `~/.claude/projects/*/<session id>.jsonl` (the id is unique, so the project directory's name encoding does not matter) and reads the last `assistant` line that has `message.usage`. Context = `input_tokens + cache_creation_input_tokens + cache_read_input_tokens`, rendered `183k` (`1.2M` above a million) with the age of that call in brackets, `183k (2 min)`. The file is read from the end in 256 KiB chunks until such a line is found, up to 4 MiB; if none, or the file is missing, the cell is `-`. Codex, agy and other kinds show `-` in this version (their transcript formats are not verified). Without `--context` nothing is read and the column is absent. `status --json --context` adds `"context": {"tokens", "at", "source": "claude-transcript"}` or `null` per seat.
- **Boundary.** The reader keeps only `type`, `timestamp` and the four usage integers of a line; message text, tool calls and every other field are discarded as each line is parsed and never logged, printed, stored or returned. The transcript path and session id are not printed either. This is a stated invariant (17.5) and a test asserts the parsed record carries nothing else.
- `restart_hint_tokens = N` (optional, top level of the fleet file, positive integer): with `--context`, a seat at or above N gets `consider restart` appended to its `NOTES`, and `status --json --context` sets `"restart_hint": true`. Nothing restarts on its own; the orchestrator decides.
- Only the owner's own transcripts are involved: the plugin runs as the owner's user and reads files under the owner's home.

### 17.5 Invariants (added to section 7)

8. `worktrees`, the `fleet file vs git` check and `--context` are read-only: no `git fetch`, no `git worktree prune`, no write under `~/.claude`, and no herdr command beyond `agent list`/`agent get`.
9. `--context` extracts usage numbers and timestamps only; no transcript content is ever surfaced.
10. `ack` and `--fyi` only write the message log; `ack` sends nothing.

### 17.6 Tests

Fake herdr: `agent_session` on agent records (shape as above). Fake transcripts: tests write small `.jsonl` files under a temporary `HOME` (the reader takes the projects directory from `HOME`, overridable by `HERDR_FLEET_CLAUDE_PROJECTS` for tests) with `assistant` lines carrying `message.usage` plus text fields that must not appear in the output, a `user` line, and a trailing partial line; cover the last-line selection, the chunked backward read, a missing file and the threshold note. Worktrees and the git drift check are tested against real temporary git repositories built with `git init`, `git worktree add`, `git commit` (git is available in CI and on the owner's machines); cover detached, missing, dirty, merged and unmerged, no `origin` at all, and the untracked/modified/identical fleet file cases. Messages: FYI by flag and by prefix, the settle-but-never-owe rule, `ack` with seats, `--all`, `nothing owed`, unknown name.

### 17.7 Amendments from the build

- **Owed rule.** "The latest `send` from A to B" is read as the latest one that is not FYI. A later FYI from A to B neither creates an obligation nor cancels an earlier unanswered request from A, which stays awaiting with its own time and first line. Any `send` (FYI or not) or `ack` from B to A that is later settles it, as 17.1 says.
- **Roster sentence.** Section 16.1 has no roster sentence, so `{roster}` gains one: the table, a blank line, then ``Message a seat with `herdr-fleet send <name> "..."`: status reports go with `--fyi`; a message that asks for something does not.`` The default template's and the Sample templates' own line about `herdr agent prompt` is unchanged; no file in `examples/team` changed.
- **FYI detection.** `FYI:` or `FYI ` (one space) after the prefix and leading whitespace, any case; `FYIs`, `FYI-ish`, a bare `FYI` or `FYI` followed by a tab are not FYI. `send` prints the same line for an FYI as for any message; the log carries `"fyi": true`.
- **`log` lines.** `log` prints no kind column, so the `[fyi]` tag goes between the bytes and the first line (`time  a → b  412  [fyi]  cleanup done`), and an acknowledgement reads `time  a → b  ack`.
- **`ack` names.** Debts are often owed to `owner` or to live agents that are not seats, so `ack` accepts seats, contacts, `owner` and any name that appears in the message log; anything else is a usage error before anything is written. Explicit names are handled first, then those `--all` adds (oldest debt first), each once. Output: `acked NAME since 10:31 (first line)`, `nothing owed to NAME`, and with `--all` and no debts `nothing owed by CALLER`. Every case exits 0; nothing owed is not a refusal.
- **`ack` and herdr.** With `--as`, `ack` runs no herdr command at all and works outside herdr. Without it, it runs the preflight and caller lookup of `send` (`agent list`, `--version`, `pane current --current`, `agent get` on the caller's pane), all read-only.
- **`worktrees` and herdr.** `worktrees` runs no herdr command and no preflight, like `log`, so it works outside herdr. It takes `--fleet` only (new commands get no `--file` alias). Exit 1 when `git worktree list` fails (not a repository, or git missing).
- **`worktrees` table.** The first line is `repo <path>  default <ref>` (`-` without one, plus the `note:` line after the table). Git calls in a worktree's own path are skipped when its directory is missing, when git marks it `prunable`, and for the `bare` entry of a bare repository; a `locked` worktree whose directory exists is inspected normally. Skipped cells show `-`, failed calls `?`. STATE parts are joined with a comma (`missing, prunable`, `clean, locked`). MERGED runs in the repository with the HEAD from the porcelain output, so a missing worktree still gets it. Each default candidate (`origin/main`, `origin/master`) must exist (`rev-parse --verify`); the `origin/HEAD` target is taken as git reports it. AGE floors to whole units: minutes under an hour, hours under a day, days under a week, then weeks; `status --context` uses the same format. Paths are shortened against `HOME` and its real path, since git prints real paths. `--seat` with no matching row prints one line saying so; an unknown seat is a usage error.
- **`worktrees --json`.** `branch` is null when detached; `age_s`, `changed` and `merged` are null when skipped or unknown; `seats` lists only seats whose globs match (the `main` mark is a table convenience).
- **Seat globs.** Matched with case-sensitive `fnmatch`, as written and also with the glob's wildcard-free leading directories resolved through symlinks, so `/tmp/sample-*` matches the `/private/tmp/sample-x` that git prints on macOS. A glob that is not absolute after `~` expansion can match nothing, so it is a fleet-file warning, not an error. Validation errors: not a list, or an item that is not a non-empty string.
- **Git calls.** All git calls (worktrees, the drift check, the BRANCH column) run with `GIT_OPTIONAL_LOCKS=0`, so `git status --porcelain` never refreshes the index or takes a lock. The drift check and `worktrees` pass the command's environment to git.
- **Status folding.** When every live seat has the same blank branch (no seat in a git repository), the BRANCH column goes and the header says nothing about it. CONTEXT sits right after STATE. `consider restart` is joined to a seat's notes with `; `. With `--context`, `status --json` carries `context` and `restart_hint` (true or false) on every seat and leaves `notes` as written. The `worktrees:` hint is the last line of `status` only (not of the table `up` prints) and is not in the JSON.
- **Drift check.** It comes right after the `fleet file` lines. With no default branch at all, a tracked file identical to `HEAD` is `ok fleet file vs git: committed, same as HEAD; no default branch found to compare with`: there is no committed copy elsewhere to drift from. A `warn` line joins its parts with `; `: first `untracked in this checkout (branch B)`, `modified since HEAD: …` or `no copy in HEAD` (tracked but absent from `HEAD`); then `<default> copy differs: …`, `no copy on <default>`, `no default branch found …`, or `same as <default>` when another part already warns. `+N` counts lines only in the working tree, `-N` lines only in the git copy, and `first at line N` is a line of the working-tree file. Texts that differ only in line endings or the final newline say so instead of counts. The file is compared through `git cat-file -p <rev>:./<path relative to the repo root>`, which also works when the repo root is a subdirectory of the git work tree or the file was chosen with `--fleet`.
- **Context reader.** An `assistant` line whose three input counts are all zero is skipped: Claude Code writes such lines for errors and interrupted turns, and a context of 0 would be wrong. The session id must look like an id (letters, digits, `.`, `_`, `-`) before it goes into the glob; when several transcripts match, the newest is read. Labels: plain below 1000, rounded thousands (`183k`), one decimal from a million (`1.2M`). A timestamp that cannot be parsed drops the age bracket. A missing or unreadable transcript, a seat with no `agent_session` (or `null`) and an absent seat all show `-`.
- **`init`.** The starter fleet file is unchanged: it shows the keys of section 2, and `worktrees` and `restart_hint_tokens` are documented in the README instead.
- **Tests.** The fake herdr's agent records carry `agent_session` only when a scenario sets one (`put_agent(..., session=fake_herdr.claude_session(uuid))`). `tests/support.py` gains `GitCase`, a real temporary git repository whose git calls from the plugin go through a logging shim on `PATH`; its `tearDown` asserts invariant 8 (read-only subcommands only, no `fetch`, no `prune`).

## 18. The inbox: durable, batched, token-free delivery (versions 0.6.0, 0.6.1, 0.7.0)

This section adopts the durable-inbox proposal in `docs/proposals/inbox.md`. Local file operations and server waits do not invoke a model. Batched prompt delivery reduces separate submissions; it does not promise a fixed token or monetary cost. The optional hooks are an additional pull path, not a prerequisite for vendor-neutral delivery.

### 18.1 Storage

```
.herdr/inbox/<name>/new/<id>.md     queued
.herdr/inbox/<name>/cur/<id>.md     delivered, expired or dropped (kept for replay)
.herdr/watch.lock                   the running watcher (18.6)
```

`<name>` is a seat name, a contact name or `owner`. `<id>` is `<UTC time as YYYYMMDDTHHMMSSZ>-<6 lowercase hex>`, for example `20261020T230900Z-3f2a9c`; the hex part is random and re-drawn if the file exists. The short form `#3f2a9c` (the hex part, with or without `#`) names a message on the command line; a short form that matches several ids in the fleet's inbox tree is a usage error that lists them. Files are written atomically (write to a temp file in the same directory, rename) and never modified afterwards; the location (`new/` or `cur/`) is the state, and the log holds the timeline. `ensure_gitignore` adds `inbox/` and `watch.lock` to `.herdr/.gitignore`.

A message file is header lines `key: value`, a blank line, then the body exactly as the sender wrote it (no `[from]` prefix):

```
id: 20261020T230900Z-3f2a9c
from: sample-orchestrator
to: sample-dev
time: 2026-10-21T10:09:00+11:00
fyi: false
reply_to: 20261020T225300Z-91be02      (only when given)
ttl: 2026-10-21T12:00:00+11:00         (only when given; absolute, from --ttl)
file: .herdr/briefs/range.md           (only when sent with --file; the path as given)

Please review the parser change in the example worktree.
```

Every message, including one delivered at once, gets a file (direct sends land in `cur/` right away). Writing the file is the first thing `send` does; a message that could not be written is a failure (exit 1) and nothing is prompted.

### 18.2 Log entries

Every entry keeps the 16.2 fields and gains `id`. New `kind` values and `delivery` values:

- `send` with `delivery: "queued"`: written when the message waits in `new/`. `send` with `delivery: "sent"` or `"unconfirmed"`: as today, when it was prompted at once.
- `delivered`: `{time, from: "herdr-fleet", to: <name>, kind: "delivered", id, via, bytes: 0, first_line: "<original first line>", delivery: "sent"|"unconfirmed"}`. `via` is one of `send` (flushed before a direct send), `kickoff`, `deliver`, `watch`, `inbox` (the seat read it), `hook`, `notify` (owner). One entry per message delivered.
- `expired` and `dropped`: same shape with `via: "ttl"` or `via: "drop-inbox"`, `delivery: "none"`.
- `ack` gains `id` when it acknowledges one message, and `reply_to` on a `send` records the id it answers.

### 18.3 `send`

`send TARGET... [TEXT | --file PATH] [--fyi] [--queue] [--now] [--reply-to ID] [--ttl DURATION] [--as NAME] [--when-ready MS] [--all] [--dry-run] [--fleet PATH]`. `--ttl` takes `30m`, `2h`, `1d` (integers with `m`, `h` or `d`). For each target, after writing the file:

| target state | default | `--queue` | `--now` | `--fyi` |
|---|---|---|---|---|
| idle or done | deliver now: the batch (18.4) of everything in `new/` plus this message, one prompt | stays queued | deliver now | stays queued (an FYI never wakes an idle seat) |
| working | typed ahead now, with the batch in front, as 16.1 allows (withdrawn by 21.1: stays queued) | stays queued | typed ahead now | typed ahead now, batch in front (withdrawn by 21.1: stays queued) |
| blocked, unknown, absent | stays queued; `queued for sample-dev (absent) #3f2a9c`, exit 0 | stays queued | refused as 16.1, the file moves to `cur/` and the entry says `delivery: "refused"`, exit 1 | stays queued |

`--queue` and `--now` together is a usage error. `--when-ready` keeps its meaning and then applies the default row. `--dry-run` writes nothing. Direct delivery prefixes the text `[from X #3f2a9c]` (the short id joins the 16.1 prefix so a seat can `--reply-to` it); the kickoff templates' sentence "the [from] prefix is added for you" stays true. `--reply-to` accepts an id or short form that exists in the fleet's inbox tree, else usage error. Output lines: `sent to X (idle), 2.4 KB` as today; `sent to X (idle) with 3 queued, 5.1 KB`; `queued for X (absent) #3f2a9c`; `queued for X (idle, fyi) #3f2a9c`.

`owner` is a valid target in every mode. Delivery to the owner is `herdr notification show` with the first line (`message from sample-dev #3f2a9c: ...`) plus the file in `cur/`, logged `delivered` with `via: "notify"`; without `HERDR_ENV` or `HERDR_BIN_PATH` (notify.py's rule) the message stays in `new/` and is reported queued.

### 18.4 The batch

A batch is the `new/` files of one mailbox, oldest id first, each rendered as

```
[from sample-orchestrator at 10:09 #3f2a9c]
<body>
```

joined by blank lines. `at` is the message time as in `since_label` (local `HH:MM`, or `YYYY-MM-DD HH:MM` for another day). A `reply_to` adds ` re #91be02` after the short id. FYIs in the mailbox ride along in every batch. When the rendered batch (plus the new message, for `send`) exceeds 16 KiB, the prompt is instead `[from herdr-fleet] 4 messages waiting (oldest 10:09, 41 KB); run: herdr-fleet inbox --read` and the files stay in `new/` until the seat reads them. Before rendering, files past their `ttl` are moved to `cur/` and logged `expired`; they are not delivered. After a successful prompt (`sent`) or an unconfirmed one, the files move to `cur/` and one `delivered` entry per message is logged with the prompt's delivery value; a refused prompt (blocked, unknown) leaves the files in `new/` and logs nothing. An unconfirmed message is never redelivered by the plugin; `inbox --redeliver ID` moves its file back to `new/` by hand.

### 18.5 Delivery paths

1. **Kickoff.** `up`, `restart` and `kickoff` render the seat's batch through a new placeholder `{inbox}`: the block `Messages that arrived while you were away:` followed by the batch, or the empty string when `new/` is empty. A template without `{inbox}` gets the block appended after the template text when the mailbox is non-empty, so sampleapp's existing templates deliver without edits. The files move to `cur/` only after the kickoff prompt was accepted (`delivered` via `kickoff`); a kickoff that was skipped (blocked at startup) leaves them queued. The size cap of 18.4 applies to the block alone. `examples/team` templates and the `init` starter gain the `{inbox}` block before "Where things stand:". `restart --drop-inbox` and `kickoff --drop-inbox` first move every queued file of that seat to `cur/`, logged `dropped`.
2. **Flush before send.** 18.3: any direct delivery carries the mailbox in front. Order is never inverted.
3. **`deliver [NAME...] [--fyi] [--dry-run] [--fleet PATH]`.** Without names: every seat and contact whose `new/` holds at least one non-FYI message; `--fyi` also takes mailboxes that hold only FYIs; `owner` is included (notification). For each: idle or done → the batch through `safe_prompt`, logged via `deliver`; working, blocked, unknown or absent → `sample-dev: working, 3 left queued`, nothing sent (queued messages never interrupt). Exit 0 when nothing was refused by error; a stalled or timed-out prompt exits 1 as `send` does.
4. **Pull: `inbox`.** `inbox [--as NAME] [--read] [--count] [--history NAME] [--redeliver ID] [--json] [--fleet PATH]`. The mailbox is the caller's (resolved as `send` resolves the sender; `--as` overrides, and `--as owner` is how the owner reads theirs). Default: list `new/` as `#3f2a9c  10:09  sample-orchestrator  Review parser change...` (`[fyi]` tag when set), oldest first, nothing moved. `--read`: print the batch format in full, move the files to `cur/`, log `delivered` via `inbox`. `--count`: print the number of files in `new/` and nothing else (exit 0 even for 0). `--history NAME`: every delivered message between the caller and NAME from both `cur/` directories, oldest first, in batch format with direction, for replay into a fresh context. `--redeliver ID`: move that `cur/` file back to `new/` (usage error if it is not in `cur/`). `inbox` runs no herdr command except the caller lookup, and none with `--as`.
5. **Watcher** (18.6) and **hooks** (18.7).

### 18.6 Watcher (0.6.1)

`watch [--debounce MS] [--once] [--fleet PATH]` runs in the foreground until Ctrl-C and delivers queued messages as seats become idle. Only the owner starts it: from a shell, or with the manifest pane `watch` (`Fleet: watch`, `--placement split`, cwd from the focused pane) which runs `herdr-fleet watch` for that repository.

- One watcher per fleet: it writes `.herdr/watch.lock` (`{"pid", "started", "fleet"}`) and refuses to start when the lock names a live pid (exit 1, `watcher already running (pid 123)`); a lock whose pid is dead is replaced. The lock is removed on exit, including Ctrl-C.
- Loop: read every mailbox; for each seat or contact with at least one non-FYI message in `new/` and no waiter yet, start a thread that runs `herdr agent wait NAME --until idle --until done --timeout 30000`. A `timeout` result just ends the thread; the loop re-reads the mailboxes every 2 s (filesystem only) and starts waiters again, so new messages for new targets are picked up without touching any agent. An agent that is absent gets no waiter (`agent wait` would fail); it is delivered at its next kickoff.
- On an observed `idle` or `done`: sleep `--debounce` (default 5000 ms), re-read the state with `agent get`, and if still idle or done deliver the batch exactly as `deliver` does (18.4, via `watch`). If the seat went working or blocked meanwhile, go back to waiting. Then `herdr notification show "delivered 3 to sample-dev"`.
- FYI-only mailboxes get no waiter. Mailbox `owner` is delivered at once (notification), as `send` does.
- `--once`: deliver whatever can be delivered right now (seats already idle or done) and exit 0; no waiters, no lock.
- Output: one line per event (`waiting on sample-dev (3 queued)`, `sample-dev idle; delivering 3`, `delivered 3 to sample-dev`, `sample-dev blocked; still waiting`). `status` prints `watcher: running (pid 123 since 10:00)` or `watcher: not running` after the owed lines whenever any mailbox is non-empty, and the per-seat queued counts: `queued: sample-dev 3 (oldest 10:09), owner 1`. `status --json` adds `"queued": {name: count}` and `"watcher": {"running", "pid", "since"}`.
- The watcher never prompts a `blocked` or `unknown` seat, never answers a dialog, never redelivers an unconfirmed message, and never starts, restarts or quits anything. It runs only `agent wait`, `agent get`, `agent prompt` (through `safe_prompt`) and `notification show`.

### 18.7 Hooks (0.7.0, Claude Code only, opt-in)

`hooks install [--remove] [--fleet PATH]` edits `<repo>/.claude/settings.local.json` (creating it if absent, preserving everything else, atomic write): under `hooks`, a `Stop` entry running `herdr-fleet inbox --hook stop` and a `UserPromptSubmit` entry running `herdr-fleet inbox --hook prompt`, both `type: "command"`, `timeout: 10`. Entries are recognised by their command text, so install is idempotent and `--remove` deletes exactly those. `doctor` reports `ok hooks: installed` / `info hooks: not installed (herdr-fleet hooks install)` for fleets with a Claude seat.

`inbox --hook stop|prompt` reads the hook JSON from stdin, resolves the seat from `HERDR_PANE_ID` through `agent get` (no pane, no herdr, no fleet, or the pane's agent is not a seat: print nothing, exit 0; every error path is silent and exit 0, so a broken hook never disturbs the agent). `stop`: if the input has `stop_hook_active: true`, print nothing (loop protection); else if `new/` is empty, print nothing; else print `{"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": "<batch>"}}`, move the files to `cur/` and log `delivered` via `hook`. The size cap of 18.4 applies (pointer text instead of the batch). `prompt`: if `new/` is non-empty, print `{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "N messages waiting; run: herdr-fleet inbox --read"}}`, moving nothing. Codex, agy and other kinds have no equivalent; they rely on 18.5 and 18.6.

### 18.8 Owed replies with ids

16.3 and 17.1 stay, with two changes: a message counts from the time of its `delivered` entry (a message still queued or expired creates no debt; a `send` entry with `delivery: "sent"` or `"unconfirmed"` is its own delivery), and a `send` with `reply_to`, or an `ack` with an `id`, settles that message exactly even if a newer message from the same sender to the same recipient is awaiting. `ack` therefore accepts ids and short forms as well as names: `ack #3f2a9c`, `ack sample-dev`, `ack --all`.

### 18.9 Invariants (added to section 7)

11. Queued messages are delivered only to `idle` or `done` seats, only through `safe_prompt`, and only by a plugin command that a person, an agent's own tool call, or the owner-started watcher invoked. Nothing the plugin spawns runs in the background; `watch` is a foreground process the owner starts and stops.
12. The watcher never prompts a `blocked` or `unknown` seat, never answers a dialog, never redelivers an unconfirmed message, and never starts, restarts or quits anything.
13. An agent is never asked to poll. Waits live in the herdr server (`agent wait`) or in a silent hook.
14. A hook-delivered batch is the seat reading its own inbox inside its own process; the plugin never writes to an agent through a hook, and a hook prints nothing on any error.
15. Message bodies are stored verbatim and only under `.herdr/inbox/` of the fleet's repository (and, for a send to a live agent of another fleet on this machine, also of that fleet's, design 20.8); the plugin never reads them for any purpose but rendering a batch, a listing or a history.
16. Acting on a message is never inferred from agent output; debts are settled only by replies, `--fyi` messages, `ack` and, since the fresh context starts over, a kickoff of either seat (design 20.8).
17. `--urgent` reorders only the mailbox herdr-fleet holds; nothing reorders or inspects an agent's own input.
18. A message's delivery mode is decided by how it was sent (`queued_by`), never by its content; the plugin reads nothing to decide.
19. Before any text is sent to a seat, the plugin reads the target pane's prompt line, whatever the focus; if it holds text or is not on screen, nothing is sent (section 21.2, amended by 23.11: focus only words the reason). The check reads only the last lines of that pane to find the prompt line, keeps nothing, and decides nothing else from them.
20. The plugin changes a pane's display name only for a pane that hosts one of the fleet's seats, and only to that seat's name or `label` (section 22.1); `pane rename` and `agent focus` are the only display commands it runs, neither reaches the agent, and `agent focus` is run only by `go` and the popup.
21. Keys reach an agent only through `herdr agent send-keys`, only from the two paths of 23.7, only the keys those paths name (`enter` on the exit confirmation; the kind's interrupt keys; and, from `interrupt` alone, one `ctrl+u` to clear a prompt the interrupt put back, 23.11), only to a seat of this fleet that the plugin has just read the screen of, and never from the watcher, a popup, a dry run or any automatic path. `pane send-keys`, `pane send-text` and `pane run` stay refused.
22. The watcher's board (sections 24 and 25) reads only what 24.2 and 25.2 list; from a screen it shows a blocked seat's dialog lines, and a seat's last lines only on the owner's `p` key; it prompts nothing, sends no key, delivers only as the watcher may, and runs `agent focus` and `pane zoom` (on its own pane) only on a key or click of the owner.

### 18.10 Tests

Fake herdr unchanged in shape; `agent wait` already exists there (check) or gains `--until`/`--timeout` with scripted outcomes (`wait_results: {name: [idle|timeout|...]}`). Cover: id generation and short-form resolution (unique, ambiguous, unknown); file write then prompt order (a failed write prompts nothing); every row of the 18.3 table; FYI never wakes idle; flush before send keeps order and logs one `delivered` per message; batch rendering, the 16 KiB pointer, `reply_to` rendering; ttl expiry at delivery and in `inbox`; kickoff `{inbox}` with and without the placeholder, skipped kickoff leaves files queued, `--drop-inbox`; `deliver` with and without names, `--fyi`, working/blocked left queued; `inbox` list/read/count/history/redeliver and `--as owner`; owner notification path with and without `HERDR_ENV`; owed from delivery, `reply_to` and `ack #id`; watcher (0.6.1) with the fake clock and scripted waits: lock creation and stale lock, waiter per target, debounce re-check that backs off when the seat went working, FYI-only skip, `--once`; hooks (0.7.0): install/remove idempotence on a settings file with other content, `--hook stop` silent cases (no pane, not a seat, `stop_hook_active`, empty), batch output and files moved, `--hook prompt` counts. `tests/support.py` learns the new commands so its invariant checks apply (`agent wait` allowed; prompts from `watch` and `deliver` only to idle/done).

### 18.11 Amendments from the 0.6.0 build

0.6.0 builds 18.1 to 18.5 (items 1 to 4), 18.8 and invariants 11 to 15; `watch` (18.6) and hooks (18.7) are not built. New modules: `inbox.py` (mailboxes: ids, files, batches, moves, ttl; no herdr, no log) and `mailroom.py` (`deliver`, `inbox`); the delivery engine every path shares is `messages.prompt_mailbox`.

- **One file per recipient.** `send A B TEXT` writes one message per target, each with its own id. The hex part is drawn again while any file in the fleet's inbox tree uses it, so short forms are unique when written; an ambiguous short form can only come from files made by hand.
- **Order within a second.** Messages sort by the id's second, then the file's mtime (a rename keeps it), then the id, so two messages written in the same second keep their write order.
- **Mailbox lock.** A command holds `flock` on `.herdr/inbox/<name>/.lock` while it expires, renders, prompts and moves one mailbox, so two commands flushing the same seat never deliver a message twice. Kickoffs take it only when `new/` holds a file, so no mailbox directory is created for a seat with nothing queued.
- **Names and headers.** A mailbox name must match the herdr name pattern; a live agent that is neither seat nor contact gets a mailbox under its herdr name. Header values are one line (a newline in a `--file` path becomes a space). The `file:` header is the path as given; the log keeps the absolute path (16.2).
- **Log entries.** Kickoff entries carry no `id` (a kickoff is not a stored message and nothing can reply to it); `ack NAME` carries none either, `ack #id` does. A `send` entry's `bytes` is the message on its own (`[from X #id] body`), whatever the prompt carried. The `delivered` entries of a flushed queue come before the `send` entry of the new message, in prompt order. `--redeliver` writes no entry (18.2 defines none); the later `delivered` entry records the second delivery. A herdr error after the file was written (other than a refusal) logs the `send` as `queued` and reports `failed X: …; #id left queued`, exit 1.
- **`log` lines.** `#id` follows the bytes, then `[queued]` or `[refused]`, then `[fyi]`: `time  a → b  412  #3f2a9c  [queued]  [fyi]  cleanup done`. Plugin entries read `time  herdr-fleet → b  delivered #3f2a9c via send  first line` (`delivered?` when unconfirmed; `expired #id`, `dropped #id` alike); an acknowledgement of one message reads `ack #3f2a9c`.
- **The table's columns.** `--queue` and `--now` win over the FYI column: `--now --fyi` to an idle seat delivers, `--queue --fyi` queues. A message that is FYI by its prefix (17.1) follows the FYI column too. An FYI typed ahead is prompted with only `working` allowed, so a seat that turned idle between the check and the prompt is not woken: the FYI stays queued.
- **`--when-ready`.** 16.1 refused a target still busy after the wait; 18.3 says the default row then applies, whose `working` cell types ahead and so defeats the option. Built: the wait is unchanged (working, blocked and unknown targets); then idle or done follows the mode's column; anything else stays queued (`queued for X (working after --when-ready 5000 ms) #id`), never typed ahead; with `--now` it is refused (exit 1). `--queue` skips the wait.
- **Prompt text.** A message alone is `[from X #id] body`; with anything queued, the whole prompt is the batch of 18.4 with the new message last. A text that already starts with `[from` is sent as written (16.1), so it carries no short id.
- **The cap includes a lone message.** 18.4 measures "the batch plus the new message", so a single message over 16 KiB to an idle seat is queued and the seat gets the pointer: `queued for X (idle) #id; prompted a pointer instead: 1 message waiting (oldest 10:09, 17.0 KB); run: herdr-fleet inbox --read`. A pointer prompt is not logged; the files stay in `new/` and the `send` entry says `queued`. Sizes use the 1000-based `KB` of `send`'s output.
- **Refusals and exit codes.** A prompt refused after the state check (the seat turned blocked) leaves the message queued (exit 0), or with `--now` moves it to `cur/` logged `refused` (exit 1). Exit 1 also for a failed write and an unconfirmed prompt; 3 only for `--all` with no live seat; queued is 0.
- **Owner.** `owner` is notified in every mode but `--queue`, FYIs included (a notification costs no tokens). One notification carries the whole owner mailbox: titled `message from sample-dev #3f2a9c` for one message, `N messages for owner (oldest 10:09)` for several, with the first lines and `read: herdr-fleet inbox --as owner --history NAME` in the body, sound `request`. The new message is logged `send` `queued`, then each message `delivered` via `notify`. 18.3's "without HERDR_ENV or HERDR_BIN_PATH it stays queued" could not happen under `send`'s preflight, so a `send` whose only target is `owner` skips the preflight outside herdr and reports `queued for owner (not in herdr) #id` without running any herdr command; any other target still needs herdr. A failed notification leaves the messages queued.
- **Contacts.** A contact that resolves to no agent or to several is treated as absent: queued with its reason in the label (`queued for partner-workspace (absent: no live agent matches cwd …) #id`); `deliver` resolves it again.
- **Batch rendering.** Trailing newlines of a body are trimmed in the batch only; the file is never changed. The batch shows no `[fyi]` tag (18.4); listings do.
- **Kickoffs.** With nothing queued, a line that is only `{inbox}` is removed together with one blank line after it, so a kickoff reads byte for byte as in 0.5.0. Above the cap the block is the heading and the pointer line without a `[from herdr-fleet]` prefix, and the files stay queued. An unconfirmed kickoff moves the files it carried with `delivered` entries marked `unconfirmed` (18.4), while the kickoff itself is reported `not briefed` and not logged, as before. The built-in default template, which is also the `init` starter, has no "Where things stand:" line, so `{inbox}` sits between `{contacts}` and `{state}` (the starter state note begins with "Where things stand:"); the starter's comment line now lists `{contacts}` and `{inbox}`. `up --dry-run` and other dry runs say which seats' kickoffs would carry queued messages.
- **`--drop-inbox`** runs first, before any quit, for each named seat, and reports `sample-dev: dropped - 2 queued messages moved to cur/ (inbox --redeliver brings one back)`. It is on `restart` and `kickoff` only, not `swap` or `up`.
- **`deliver`.** Without names, the order is seats in file order, then contacts, then `owner`, which is included whenever its mailbox holds anything (FYI or not). Named mailboxes are delivered even when they hold only FYIs. A name may be a seat, a contact, `owner`, an existing mailbox or a live agent. Before choosing, `deliver` expires past-ttl messages in every mailbox. Lines: `delivered 3 to sample-dev (idle), 5.1 KB`, `sample-dev: working, 3 left queued`, `prompted sample-dev (idle) with a pointer: …`, `nothing queued for X`, `nothing to deliver (only FYIs are queued; deliver --fyi takes them)`.
- **`inbox`.** The caller lookup is `pane current --current` then `agent get`, only inside herdr; outside herdr, or without a pane, the caller is `owner`. Every mode first expires past-ttl messages. `--read` prints everything with no cap (it is what the pointer asks for). The modes are mutually exclusive. `--history` shows `cur/` files between the two names as `[from A to B at 10:09 #id]`, leaving out those whose last log entry is `expired`, `dropped` or a refused `send` (a `cur/` file the log does not mention is shown). `--redeliver` accepts a message in any mailbox, so the owner can requeue a seat's unconfirmed delivery. `--json`: a list of `{id, from, to, time, fyi, reply_to, ttl, file, first_line}` (plus `body` with `--read` and `--history`); `--count --json` prints `{"name", "count"}`.
- **Owed replies.** A plain answer from B to A (a `send` without `reply_to`, FYI or not, in any delivery state but `refused`, or an `ack` without `id`) settles every request from A to B delivered before it. An exact answer (`reply_to`, `ack #id`) settles only its target, and does not count as a plain answer, so when the newest request is settled exactly an older unanswered one shows again. An answer counts from its own time even while it waits in `new/`: B has answered. `status` still shows one line per pair (the latest unsettled request); `owed` items gain `id` in JSON when the message has one.
- **`ack` with ids.** `#3f2a9c` or a full id is always an id; a bare hex part is an id unless it is also a known name. `ack #id` for a message the caller does not owe (not to the caller, FYI, not delivered, already settled) prints `nothing owed for #3f2a9c` and exits 0.
- **`status`.** The `queued:` line of 18.6 is built now (the `watcher:` line comes with 0.6.1): mailboxes in the order seats, contacts, other names, `owner`; `(oldest HH:MM)` only when a mailbox holds two or more. `status` counts only messages not past their ttl and moves nothing. `--json` adds `"queued": {name: count}` and `"queued_oldest": {name: time}`.
- **`.gitignore` and `init`.** The starter `.gitignore` lists all four entries; `init --example` never copies an `inbox/` directory or `watch.lock`.
- **`install-cli` writes a launcher** (so CLI invocations resolve the enabled plugin rather than an incidental checkout). `~/.local/bin/herdr-fleet` (or `--dir`) becomes a `/bin/sh` script, written atomically with mode 0755, that runs `$HERDR_FLEET_BIN` when set, else the `bin/herdr-fleet` of the enabled `yarrasys.fleet` plugin found through `herdr plugin list --json` (the directory of its `manifest_path`), else the `bin/herdr-fleet` that ran `install-cli` (`FALLBACK`). It replaces a symlink or a file containing the marker `herdr-fleet launcher`, and refuses any other file (exit 1). `install-cli --dev` writes the 0.5.0 symlink to this checkout instead. The text is `ops.LAUNCHER_TEMPLATE`; tests check it but never run it, since it calls herdr. Live smoke tests of unreleased code set `HERDR_FLEET_BIN` to the clone's `bin/herdr-fleet`.
- **Tests.** The fake herdr is unchanged (`agent wait` with `wait_status` and canned responses cover 0.6.0). `tests/support.py` adds: an `inbox` command may run only `pane current` and `agent get`, and none with `--as`; `deliver` and kickoffs keep the strict rule (idle or done only), and only `send` may reach a working agent. `tests/test_inbox.py` covers 18.10's 0.6.0 list.

### 18.12 Amendments from the 0.6.1/0.7.0 build

One build makes both versions: 0.6.1 is 18.6 (`watch`, the lock, the `status` watcher line and JSON, the manifest entries) and 0.7.0 is 18.7 (`hooks install`, `inbox --hook`, the doctor line); the version string is 0.7.0. New modules: `watch.py` (the lock, the loop, the waiters, `--once`) and `hooks.py` (the settings file and the doctor check); `inbox --hook` lives in `mailroom.py`. Both deliver through `messages.prompt_mailbox`, `settle` and `notify_owner` only, and `--once` through `mailroom.deliver_one`, which `deliver` uses too.

Watcher (18.6):

- **Threads.** One daemon thread per mailbox does the wait, the debounce, the re-check and the delivery (under the mailbox lock, as `deliver`); a condition guards the shared fields. Ctrl-C, SIGTERM and SIGHUP (the pane closed) all stop it: no new delivery starts, one in flight gets up to 60 s to finish so its files reach `cur/`, and waiters still blocked in `agent wait` get 1 s and are then left to their 30 s timeout (read-only; Ctrl-C and a closed pane signal them too).
- **Commands.** 18.6 lists `agent wait`, `agent get`, `agent prompt` and `notification show`, yet also says an absent agent gets no waiter and contacts must be resolved: the watcher also runs `agent list`, only when a waiter is due. Its preconditions are the common ones without the caller lookup (`agent list` and the local `herdr --version`, no `pane current`). `tests/support.py` allows exactly these for `watch` and requires `--timeout` on every wait.
- **Which mailboxes.** Seats in file order, then contacts (contacts are waited on through their pane id), as `deliver` without names; other live agents' mailboxes get no waiter. A mailbox whose messages are all past their ttl gets none either: `prompt_mailbox` expires at delivery, so the loop moves nothing itself.
- **Absent agents.** One line until the reason changes (`sample-reviewer: absent, 1 left queued`), and looked for again every 30 s, the cadence of a wait timeout, not on every 2 s pass.
- **Lines.** Every line starts with the local time (`10:09:31  waiting on sample-dev (3 queued)`), which a long-running pane needs; the run opens with `watching <fleet file> (pid N, debounce 5000 ms); Ctrl-C stops` and ends with `stopped`. `waiting on` is printed when a waiter starts with a count not yet announced, so a 30 s restart prints nothing. `idle; delivering N` gives the count when the wait returned; `delivered N to X` the count delivered, which includes messages that arrived during the debounce. Other outcomes: `X working; still waiting` (also `blocked`, `unknown`), `unconfirmed X: …; N logged as delivered (unconfirmed), not resent`, `prompted X with a pointer: …`, `failed X: …; N left queued, trying again in 60 s`, `delivered N to owner (notification)`, `owner: <why>, N left queued`.
- **Re-check.** After the debounce `agent get` decides: idle or done delivers; working, blocked or unknown waits again; absent ends the waiter. A prompt refused at the last moment (the seat turned blocked) prints `X blocked; still waiting` and the next pass starts a new waiter.
- **No repeated turns.** An unconfirmed prompt moves its files (18.4), so nothing repeats. A pointer prompt (queue above 16 KiB) is not prompted again for the same messages; a new message lifts the hold, otherwise every idle would cost the seat a turn for the same pointer. A failed delivery, or a wait that fails with anything but `timeout` or `agent_not_found`, pauses that mailbox for 60 s. A failed owner notification is retried only when a new owner message arrives. An error of the loop itself (herdr unreachable) is printed once and retried every 2 s.
- **Notifications.** `delivered 3 to sample-dev` with the senders and counts as the body (`from sample-orchestrator (2), sample-reviewer (1)`, no message text) and sound `none`, since one arrives per delivery; `delivery to X unconfirmed` and `delivery to X failed` with `request`; `pointed X to N queued messages` with `none`. `owner` gets `notify_owner`'s notification only.
- **`--once`.** `deliver`'s lines and rules (expire first, the same mailboxes, owner included), logged via `watch`, with the watcher's notifications; no wait, no debounce, no lock. 18.6 says "exit 0"; built as `deliver`: 1 when a prompt failed or may not have arrived, else 0.
- **`--debounce`.** 0 or more; a negative value is a usage error.
- **The lock.** Written to a temp file and hard-linked into place, so of two watchers starting together only one gets it and nobody reads half a lock. A lock that cannot be parsed is treated like a dead one and replaced. On exit the lock is removed only if it still names this pid. A pid is alive when `kill(pid, 0)` succeeds or fails with EPERM. `status` reports a lock naming a dead pid as `not running`.
- **The fleet file** is read once when the watcher starts; restart it after editing the file.
- **`status`.** The watcher line comes after the owed lines and before the `queued:` line, and both appear only while some mailbox holds a message not past its ttl. `status --json` always has `"watcher"`: `{"running": false, "pid": null, "since": null}` when none runs, `since` being the lock's `started`.
- **Manifest.** Action `watch` ("Fleet: watch") runs `ui watch`, which opens pane `watch` with `--cwd <repo root>` and `--placement split`. The pane entry declares no `placement`: herdr's plugin docs show only `overlay` and `popup` for manifest panes, while `plugin pane open --placement` documents `split`. A watch pane that cannot start (another watcher runs) also sends the error as a notification, since its pane may close before anyone reads it.
- **Fake herdr.** `wait_results` and `status_after_wait` script `agent wait` (18.10); calls are serialised (a lock in-process, a file lock as a program) so waiter threads share one world. The watcher tests run waiters inline on the fake clock, with one test on real threads.

Hooks (18.7):

- **FYIs never continue a turn.** The earlier hook description continued on any non-empty mailbox. FYI-only mailboxes should instead remain available for a later delivery without creating a continuation by themselves. Built: `--hook stop` prints nothing when only FYIs are queued, and carries them along with any other message. `--hook prompt` counts FYIs, since the owner's prompt is a turn anyway.
- **Cap.** The stop batch also falls back to the pointer above 9,000 characters (Claude Code caps `additionalContext` at 10,000), besides 18.4's 16 KiB. The pointer keeps its `[from herdr-fleet]` prefix; the files stay queued and nothing is logged. `stop_hook_active` limits it to one continuation per turn.
- **Order and encoding.** The files move to `cur/` and are logged (`settle`, via `hook`) before the JSON is printed, and the JSON is ASCII-escaped, so a stdout without UTF-8 cannot fail after the move.
- **Finding the seat.** `stop_hook_active` is checked first, before any herdr command; unreadable stdin counts as `{}`. The fleet is found from the hook input's `cwd` when that is a directory, else the process cwd (`--fleet` and `HERDR_FLEET_FILE` apply as usual). The seat is `agent get $HERDR_PANE_ID` and nothing else (no `pane current`), so a seat whose pane moved to another workspace, which makes `HERDR_PANE_ID` stale, has silent hooks until it restarts.
- **Silence.** In hook mode stderr is discarded too (fleet-file warnings, unreadable message files). `--hook` is a mode of `inbox`, exclusive with `--read`, `--count`, `--history` and `--redeliver`; with `--as` or `--json` it is a usage error (exit 2), a mistake in a hand-written command line rather than a runtime error.
- **`prompt` text.** `1 message waiting; run: herdr-fleet inbox --read` or `N messages waiting; …`, counting messages not past their ttl; no lock, nothing moved, nothing logged.
- **`hooks install`.** Runs no herdr command and no preconditions, so it works outside herdr. A missing entry is appended as its own group `{"hooks": [entry]}` (these events take no matcher); an entry counts as present wherever its command text appears under that event. `--remove` takes the entry out of whatever group holds it and drops a group, an event list or the `hooks` object only when the removal emptied it. The file is rewritten atomically with two-space indentation (its old formatting is not kept) and its mode; a file that is not a JSON object, or whose `hooks` is not an object or an event not a list, is refused with exit 1 and left alone. Output: `installed the Stop and UserPromptSubmit hooks in <path>`, `already installed in <path>`, `removed the … from <path>`, `nothing to remove: …`, plus warnings when no seat is of kind `claude` or `herdr-fleet` is not on `PATH`, and a reminder that a running session may start running new entries at once while removal is not live until the session restarts (observed on 21 Oct 2026, below).
- **`doctor`.** `ok hooks: installed`, `info hooks: not installed (herdr-fleet hooks install)`, and `warn hooks: partly installed (Stop only); run herdr-fleet hooks install` when one entry is missing, or `warn` with the reason when the settings file cannot be read. No line for a fleet without a Claude seat. `info` is a new doctor level; like `skip` it is not a problem for `--notify`.

Historical verification of 0.6.1 and 0.7.0 used a disposable Claude seat on Herdr 0.9.5 and Claude Code 2.1.x. Local identities are omitted; the technical outcomes and untested paths follow:

- **Watcher.** With one message queued (`send --queue` to an idle seat), `watch` printed `waiting on fleet-smoke (1 queued)`, observed idle through `agent wait`, delivered after the 5 s debounce (`delivered 1 to fleet-smoke`, logged via `watch`), and a second `watch` was refused with `watcher already running (pid …)`. SIGTERM removed `.herdr/watch.lock` within 0.5 s and printed `stopped`. `status` prints the `watcher:` line only while a mailbox is non-empty, so a running watcher with empty queues is invisible there; a later version may print it whenever the lock is held.
- **Stop hook.** Controlled checks confirmed batched hook delivery and continuation. Existing sessions could retain hook configuration until restart, so removal was not immediately effective. A typed-ahead prompt could become the next turn before the hook feedback was acted on; do not infer message handling from output. The `UserPromptSubmit` hook was not exercised live in this check (unit-tested only).
- **Older installed code.** A retained hook can outlive the installed version that understands `--hook`. A nonzero unknown-option exit can disturb the harness. The launcher therefore makes hook calls silent when the selected installation predates hook support; hooks can be installed before updating the plugin.

## 19. The watcher as part of bring-up (version 0.8.0)

The watcher should run as a visible foreground plugin pane, opened by `up` and tracked for explicit cleanup. Historical API checks confirmed split plugin panes, explicit cwd/environment, pane layout and resize information, and process termination on close. Focus behavior and resize semantics are amended in 19.4.

### 19.1 Behaviour

- **Default on.** `up` starts a watcher for the fleet unless `watch = false` at the top level of the fleet file or `up --no-watch` is given. If `.herdr/watch.lock` names a live process, `up` prints `watcher: already running (pid N)` and does nothing more. It never starts a watcher for a fleet with no seats to bring up and none live.
- **A visible strip.** The watcher runs in a plugin pane (`watch` entrypoint, so the installed plugin's own copy runs) opened with `plugin pane open --placement split --direction down --cwd <repo>` and `--env HERDR_FLEET_FILE=<fleet path>` (so `--fleet` is honoured when `up` was given one). Target and size come from `[watch]`: `ratio` (default `0.12`, the strip's share of the area it is split from, clamped to 0.05–0.4) and `position = "bottom"` (the only value in 0.8.0; the table is there so `top`/`right` can come later). The strip is split from the **grid area** so it spans the seats: on a workspace `up` creates, from the root pane before any seat is placed, so seat 1 and the grid take the remaining 1 - ratio; on an existing layout, after placement, by the `tile` rule below. herdr splits 50/50, so `up` resizes the new pane with `pane resize --direction up --amount <so that the strip is ratio of the pair>`; the exact amount is computed from the pair's heights in `pane layout`, and a failure to resize is a warning, not an error. After opening, `up` restores focus to the caller's pane when a herdr command allows it (`pane focus`-family; the builder confirms which); otherwise it reports that focus moved.
- **Never a seat.** The watch pane is recorded in the state file as `"watch": {"pane_id", "created_pane": true, "started": <iso>}`. It is excluded from seat classification, from `status`'s seat table (which keeps its `watcher:` line, now always printed when `watch` is on: `watcher: running (pid N, pane w2:p7, since 10:00)` or `watcher: not running`), from drift, from `tile`'s seat set and from `down`'s seat loop.
- **`tile`.** Parks every seat but the anchor as today; if a watch pane exists in the tab, moves it under the anchor first (`pane move --tab T --split down --target-pane <anchor> --ratio 1-ratio`, which keeps the anchor at 1 - ratio and gives the strip ratio), then returns the seats into the anchor's area. The strip therefore spans the whole grid after any `tile`. A watch pane in another tab or workspace is left alone and reported.
- **`down`.** With `--close-panes`, after the seats, closes the watch pane when `created_pane` is true and its lock names a live pid or no lock exists (closing it stops the watcher). Without `--close-panes` the watcher keeps running, with nothing to deliver. `down NAME...` never touches it. `restart` and `swap` ignore it.
- **State pruning.** A recorded watch pane that no longer exists is dropped from the state like a seat's pane; a watch pane whose process is no longer the watcher (shell returned) is reported as `watcher pane idle` by `status` and closed by `down --close-panes` like a created seat pane at a shell.
- **Manual start stays.** `herdr-fleet watch` in any pane and "Fleet: watch" keep working; `up` simply skips when the lock is alive. `up --dry-run` prints the watch pane plan.

### 19.2 Invariant 11, amended

The watcher may also be started by `up`, in a visible plugin pane recorded in the state file and closed by `down --close-panes`; it is still a foreground process in a pane the owner can see and close, never a detached one.

### 19.3 Tests

Fake herdr: `plugin pane open` with `--placement split` returning the live shape, `pane resize`, `pane layout` (minimal: the pair's heights). Cover: `up` on a fresh workspace splits the strip from the root before seats and resizes it; `up` on live seats skips when the lock is alive and otherwise opens under the last placed seat; `--no-watch` and `watch = false`; the state entry; `status` lines in both states; `tile` moving the strip under the anchor first and keeping seat order; `down --close-panes` closing it after the seats and never a strip it did not create; pruning; `--dry-run`. `tests/support.py`: `plugin pane open` is allowed for `up` and `ui` only; the watch pane id must never be prompted.

### 19.4 Amendments from the build

0.8.0 builds 19.1 to 19.3. New module: `watchpane.py` (the strip: whether `up` opens one, where, its size, the status line, `down`'s close and `tile`'s move). `watch.py` is unchanged: `HERDR_FLEET_FILE` already reaches `find_fleet_file` through the pane's environment, so `--env HERDR_FLEET_FILE=<fleet path>` needs no hook. `placement.place_chain` and `place_grid` gain an `on_area` hook. Facts read from herdr's source at tag v0.9.5 (and v0.9.4 for the CLI options), since none of them could be tried live: `plugin pane open` focuses the new pane unless `--no-focus` (the CLI's default is `focus = true`, which is why the live open returned `focused: true`); `pane resize` moves the nearest split on the named edge of the pane (else on the opposite edge), adding the amount to that split's first-child ratio for `right` and `down` and subtracting it for `left` and `up`, with the amount taken as `abs()`, capped at 0.5, default 0.05; every split ratio is clamped to 0.1-0.9; `pane layout` answers `result.layout` with `area`, `panes` (`pane_id`, `focused`, `rect`) and `splits` (`id`, `direction` right or down, `ratio` of the first child, `rect`), where pane rects are drawn inside borders and split rects are not; `pane resize` answers `result.resize` with the layout afterwards; `pane focus` only moves to a neighbour in a direction.

- **Focus.** The strip is opened with `--no-focus`, which the 0.9.4 and 0.9.5 CLIs accept, so no focus needs restoring. `pane focus` could not do it anyway: it cannot name the caller's pane. If herdr still returns the pane focused, `up` says `herdr moved the focus to it`.
- **Resize direction.** 19.1 resizes the new pane with `--direction up`; by herdr's rule that grows the strip. Built: `pane resize --pane <the pane above> --direction down --amount <(1 - ratio) - current>`. The pane above is a leaf whose bottom edge is exactly the strip's split, so the right split moves whatever the layout around it. `current` is the `ratio` of the smallest `down` split in `pane layout` whose rect holds both panes, else the panes' heights; when neither can be read, 0.5 is assumed and the output says `about 12% of the area (pane layout unreadable; assumed herdr's 50/50 split)`. A strip that would need to grow is resized itself with `up`. Within 0.005 of the target nothing is resized.
- **Ratio range.** herdr clamps split ratios to 0.1-0.9, so 19.1's lower bound of 0.05 cannot happen: `[watch] ratio` outside 0.1-0.4 is clamped with a warning, and outside (0, 1) is an error like every other ratio.
- **`watch` in TOML.** A file cannot hold both `watch = false` and a `[watch]` table, so `watch` is either a boolean or the table, and the table means on. `position` other than `bottom` is an error; unknown keys in `[watch]` warn.
- **Fresh and existing layouts.** A layout is fresh when this run places at least one seat and no other seat of the fleet has a pane in the target workspace (the caller's own pane does not count). The strip is then split from the first pane of the layout before anything else splits it. For a workspace `up` creates that is the root pane (19.1). Generalised: in a workspace `up` did not create, it is the grid area split off the anchor (grid mode) or the first chained seat's pane, so the strip spans the seats there too and never the caller's share. Otherwise 19.1 says "by the `tile` rule" and 19.3 says "under the last placed seat". Built as 19.3: under the pane of the last seat in file order that has a pane in the fleet's workspace (never the caller's), opened after the kickoffs. Applying the tile rule from `up` would mean moving live seats nobody asked to move; `tile` afterwards stretches the strip across the layout. If a fresh run's first placement fails, this rule applies instead.
- **Order with kickoffs.** On a fresh layout the strip opens during placement, before the seats start; the watcher finds those seats absent and looks again after 30 s, and kickoffs carry the inbox under the mailbox lock, so each message is delivered once either way. On an existing layout it opens after the kickoffs, so a seat just started (idle) does not get its queue ahead of its kickoff.
- **When `up` opens none.** `watch = false` and `--no-watch` print nothing. A live lock prints `watcher: already running (pid N)`. A recorded strip that still exists and runs the watcher, but holds no lock yet (the first seconds), prints `watcher: starting - in pane P`. A recorded strip that is idle or runs something else prints `watcher: not running - its pane P is idle; run herdr-fleet watch there, or close the pane and run up again` (exit 3). `up` must not close panes (invariant 4), and a second strip would leave the first unrecorded. An error while looking at the recorded strip (other than not-found, which prune handles) is also exit 3 with no second strip. With no seat live, adopted or started: `watcher: not started - no seat is up`. A failed `plugin pane open` is exit 3 and the seats are unaffected.
- **Is the watcher in the pane?** A pane hosts the watcher when a foreground process has the lock's pid or an argv naming `herdr-fleet` and `watch` (the manifest runs `sh -c 'exec python3 …/bin/herdr-fleet watch'`). It is idle at a shell prompt or with no foreground process, and busy otherwise, including when it hosts an agent. Finding out costs one `pane get` and one `pane process-info`, both read-only.
- **`status`.** "Now always printed when `watch` is on" is built as: printed whenever a lock is held or a strip is recorded, and otherwise only while something is queued (as in 0.7.0). A fleet that never started a watcher shows no new line, and 0.7.0's status tests pass unchanged. Lines: `watcher: running (pid N, pane P, since HH:MM)`; `watcher: running (pid N since HH:MM)` (0.7.0's text) for a watcher outside the strip, with `; watcher pane P idle` added when the strip is idle; `watcher: starting (pane P)`; `watcher: not running (watcher pane P idle)`; `watcher: not running (watcher pane P runs something else)`; `watcher: not running`. `status --json` adds `"pane"` and `"pane_state"` (`watcher`, `idle`, `busy`) to `"watcher"` only when a strip is recorded, so the 0.7.0 object is unchanged otherwise.
- **`down`.** 19.1 closes the strip when `created_pane` is true and its lock names a live pid or no lock exists. Built on what runs in the pane rather than the lock alone. The strip is closed when it hosts the watcher (`watcher: pane closed - P (the watcher stopped)`) or is idle. It is kept (exit 3) when anything else runs there. It is never closed without `created_pane` or with names. It is closed only with `--close-panes`, after the seats' panes, and its entry is dropped once it is closed. `ui watch` panes are not recorded, so `down` leaves them alone.
- **`tile`.** The strip is parked after the seats, so a failed seat park leaves it untouched. It comes back under the anchor with `--ratio <1 - ratio>` before any seat returns: `watcher: moved - below sample-orchestrator (P), ratio 0.88`. A failed strip move is reported (exit 3, with the command to bring it back) and the seats are tiled anyway. A strip in another tab or workspace is reported `elsewhere` and left alone. `tile` reads the strip with one `pane get`; `--dry-run` prints its two moves.
- **Never a seat.** The anchor of a labelled workspace that the caller is not in (`_first_pane`) skips the recorded strip. Prune drops a strip that herdr no longer knows (`state: dropped the watcher pane P (no longer exists)`; `status` prunes silently as before).
- **Report lines.** `watcher: started - herdr-fleet watch in pane P below Q, 12% of the area`; `watcher: plan - <plugin pane open>, then <pane resize> (the strip gets 12% of Q)` (the dry-run amount assumes the 50/50 split); and `watcher: warning - pane resize: …; the strip keeps herdr's 50%`, which leaves the exit code alone.
- **`--env`.** `HERDR_FLEET_FILE` always names the absolute path of the fleet file in use, not only when `--fleet` was given.
- **Tests.** The fake's `plugin pane open` honours `--target-pane`, `--placement split`, `--direction`, `--env` and `--no-focus`, marks the pane `plugin` and `created`, and runs the watcher process in it for the `watch` entrypoint. `pane layout` and `pane resize` replay the tab's recorded splits (`fake_herdr.geometry`, 200x50 cells, herdr's rounding and resize rule). `tests/support.py` allows `plugin pane open` from `up`, `ui` and `menu` (the popup's `up`) and `pane resize` from `up` and `menu`, and fails any `agent prompt`, `agent rename` or `agent start` aimed at a pane opened as a plugin pane. `GridCase.rects` leaves plugin panes out, so its cells stay shares of the grid area. `tests/test_watchpane.py` covers 19.3 (42 tests); the 437 tests of 0.7.0 pass unchanged with the strip on by default.

## 20. History, debt age, queue locations, large messages and blocked seats (version 0.9.0)

This extension addresses incomplete history, reply-debt age, ambiguous queue locations, large message display and blocked-state duration. The examples below are synthetic; no private work messages or deployment roster are retained.

### 20.1 History includes messages without stored bodies

`inbox --history NAME` showed "No delivered messages" although the log held four, because 0.6.0 built history from `cur/` files only and pre-0.6 entries have no file and no id. Now history merges two sources, oldest first: `cur/` files (full body) and log entries of kind `send` between the two names that have no file (`delivery` of `sent` or `unconfirmed`, pre-0.6 or otherwise). A log-only message renders as `[from X at HH:MM, first line only]` followed by its `first_line`. The listing ends with `N of M messages have only their first line (sent before the inbox kept bodies)` when N > 0, and with `no messages between A and B` when both sources are empty. `--json` carries `body_available: false` for those.

### 20.2 Debt age and bulk acknowledgement

- `status` and `log --owed` print the age: `awaiting: partner-workspace owes peer-analyst for 5h12m (#a1b2c3, since 08:49): <first line>`. Ages render as `3m`, `1h05m`, `2d03h`. Ids are shown so `ack #id` is one copy away; a message without an id (pre-0.6) shows `(no id)` and is acknowledged by name.
- `ack --older-than DURATION [--dry-run] [--as NAME]` acknowledges every message the caller owes that was delivered more than DURATION ago (`30m`, `2h`, `1d`, same grammar as `--ttl`); `--dry-run` lists what would be acknowledged. Combinable with names and `--all` (the intersection applies: names restrict, `--older-than` filters).
- The roster sentence (16.1, 17.1) gains: "when you have acted on a request, reply with --fyi or `ack #id`, since the plugin cannot see what you did". Acting on a message is never inferred.

### 20.3 Where a message sits

The send line names the place: `sent to peer-builder (working: typed into its own input, read after its current turn)` for the typed-ahead path; `sent to X (idle)` as today; `queued in herdr-fleet for X (absent) #id, position 2 of 2` for the mailbox. The `(working)` wording makes clear that herdr-fleet cannot see or reorder what sits inside an agent's own input queue. `inbox --as NAME` lists positions (`1.`, `2.`, …).

`send --urgent` puts a message ahead of everything already queued in the mailbox: the file gets a `priority: 1` header, batches order by priority (1 before none) and then by id, and the listing marks it `[urgent]`. For a working seat `--urgent` has nothing to reorder and is reported so (`typed ahead; --urgent cannot reorder the seat's own input`); senders who need ordering use `--queue` so the mailbox holds the order, as the roster sentence now says: "to control order, queue (`--queue`) and let the watcher or the next kickoff deliver in one batch". `--before ID` is not added; priority covers the deadline case without a second ordering scheme.

### 20.4 Large messages on read

`inbox --read` prints a message in full only up to `inbox_print_max` bytes (top-level fleet key, default 8192). Above that it prints the header line, the first 10 lines of the body, a line `… <size> in full: <path to the cur/ file>` and the delivery is still recorded (the file is the stored copy, nothing is written twice). `--full` prints everything. The same cap applies to `--history`. The pointer prompt of 18.4 is unchanged.

### 20.5 Blocked seats with duration

- The state file records, per seat, `state_seen: {"state": <s>, "since": <iso>}`, updated by every command that reads the seat's state (`status`, `send`, `deliver`, `inbox`, `watch` on each observation): when the observed state differs from the recorded one, `since` becomes now. `status` prints `blocked (4m)` and, for `unknown`, `unknown (12m)`; other states print as before. The age is a lower bound (since the plugin last noticed), which the column header note in README says.
- `[watch] notify_blocked_after = "3m"` (optional; unset means never): the watcher, which already observes states through `agent wait`, sends one `herdr notification show` (sound `request`) when a seat has been blocked for that long, once per blocked episode, naming the seat and pane: `peer-research blocked 3m (w3:pA): answer its prompt`. It never touches the seat. `status --json` gains `state_since`.

### 20.6 Invariants

16. Acting on a message is never inferred from agent output; debts are settled only by replies, `--fyi` messages and `ack`.
17. `--urgent` reorders only the mailbox herdr-fleet holds; nothing reorders or inspects an agent's own input.

### 20.7 Tests

History from mixed sources and the first-line-only note; owed ages and ids; `ack --older-than` with and without names, `--dry-run`; the send lines for all three places and the position; `--urgent` ordering in batches and listings, the working-seat report; `inbox_print_max` cut with the path line, `--full`, the same in `--history`; `state_seen` updates across commands with the fake clock and the `blocked (4m)` label; the watcher's blocked notification once per episode with `notify_blocked_after`; roster sentence changes in the template tests.

### 20.8 Delivery intent and cross-fleet logging, same release

This amendment covers delivery intent, explicit FYIs, kickoff debt boundaries and cross-fleet logging. The typed-ahead paths proposed here were withdrawn by section 21.1; they are retained as historical design alternatives, not current operating guidance.

- **A queue to a busy seat drains at the first opportunity (typed-ahead policy later withdrawn by 21.1).** A message queued because the target was `blocked`, `unknown` or `absent` at send time came from a plain send: the sender wanted it read as soon as possible, and 0.6.0 then held it until the seat was idle, which an orchestrator rarely is. The file header gains `queued_by: refusal | queue | fyi`. `refusal` messages are delivered at the first opportunity: the watcher, `deliver` and the flush-before-send type them ahead when the seat is `working` (the `send` exception of invariant 2, through `safe_prompt` with `working` allowed), and deliver them when it is idle or done. `queue` messages keep waiting for idle or done, and `fyi` messages keep riding along. The watcher therefore also waits on seats whose mailbox holds a `refusal` message with `agent wait NAME --until working --until idle --until done`, and the status line says which: `sample-orchestrator working; typing ahead 1 (queued on refusal)`. `deliver NAME --now` types ahead a whole mailbox to a working seat by hand. `inbox --as NAME` shows the reason per message (`[on refusal]`, `[queued]`, `[fyi]`).
- **An FYI queued to an idle seat says so plainly.** The send line becomes `queued for sample-dev (idle, fyi: delivered with the next plain send, kickoff or deliver) #id`. Text is never refused on its wording.
- **Restarts settle debts.** Every kickoff entry (reason `up`, `restart`, `swap` or `kickoff`) for seat S settles, at its time, every message owed by S and every message owed to S: the fresh context neither remembers what it asked nor what it was asked, and the kickoff's `{inbox}` and `{state}` are its record. The log already holds kickoffs with the seat as `to`; `owed()` treats them as a boundary for that seat.
- **Cross-fleet sends are logged on both sides.** When `send`'s target is a live agent that is not a seat or contact of this fleet but whose `cwd` (or `foreground_cwd`) holds a `.herdr/fleet.toml` of another fleet on this machine, the plugin also appends the `send` entry (same id, same fields) to that fleet's `.herdr/messages.jsonl` and stores the body in that fleet's `.herdr/inbox/<target>/cur/<id>.md`, so the recipient's `status`, `log --owed` and `inbox --history <sender>` see the message and the debt the sender's reply settles. The reverse case is the same code: a reply from partner-workspace to sample-orchestrator lands in sampleapp's log. Nothing is prompted twice; the other fleet's files are written with the same atomic rules and only when they exist (`.herdr/` present). A target with no fleet file is logged only locally, as today.
- **Queue age in status.** `queued: sample-dev 2 (oldest 25m), owner 1` uses ages, not clock times, like 20.2.
- **Inbox hint on send.** When the caller's own mailbox is non-empty, every `send` ends with `you have N waiting; herdr-fleet inbox --read` (one line, after the per-target lines).
- **Hooks per seat.** `hooks = false` on a seat (default true) makes `inbox --hook` print nothing for it, so the owner can install the hooks for a repository and enable them for one seat, such as the orchestrator alone. `doctor`'s hooks line names seats with hooks off.

Invariant 18: a message's delivery mode is decided by how it was sent (`queued_by`), never by its content; the plugin reads nothing to decide.

Tests (added to 20.7): `queued_by` in files and listings; refusal messages typed ahead by watcher, `deliver` and flush when the seat is working, `queue` ones not; the watcher waiting with `--until working` only for mailboxes holding refusal messages; `deliver --now`; the FYI send line; kickoffs as owed boundaries in both directions; cross-fleet logging into a second temporary fleet (entry, body, and the owed view from the other side), and the no-fleet case; queue ages; the inbox hint; per-seat `hooks = false` in `inbox --hook` and doctor.

### 20.9 Amendments from the build

One build makes 20.1 to 20.8; the version is 0.9.0. New modules: `seen.py` (`state_seen`: what every command observed, the `blocked (4m)` label) and `crossfleet.py` (finding another fleet from an agent record, mirroring entries and bodies there). Two considered alternatives were not included:

- **Hooks per seat (20.8, last item) dropped.** Per-seat `hooks = false` was not included: it would add an agent-specific fleet-file option to a vendor-neutral change. The chosen delivery policy is vendor-neutral: the watcher, `deliver` and the flush before send decide by `queued_by` and the herdr state alone, deliver through `agent prompt`, and never depend on the agent kind or on the hooks, which stay an opt-in extra for Claude seats.
- **`tile` keeping an adopted first seat's share dropped.** Re-tiling uses the declared uniform layout rather than preserving a special side share for an adopted seat.

Decisions section 20 left open:

- **`queued_by` comes from the mode.** 18.1 writes the file before the target's state is known, and files are never rewritten, so the header is decided by how the message was sent: `--queue` and a `--when-ready` send are `queue` (the sender asked for the seat to be ready, so a leftover is never typed ahead, as 18.11 says); an FYI (flag or prefix) is `fyi` unless `--now`; everything else, `--now` included, is `refusal`. A message delivered at once carries the header too; it matters only while the message waits. Files written before 0.9.0 count as `queue`, or `fyi` when they are FYIs.
- **A refusal message takes its whole mailbox.** "Type them ahead" is built as typing the whole mailbox ahead (queue and FYI messages ride along, urgent first, then oldest first), as 18.3's `working` row already does for a send, so order is never inverted. A working seat whose mailbox holds only `queue` or FYI messages is left alone, as before.
- **The flush before send.** A direct delivery already carries the whole queue (18.3). New: a send whose own message stays queued (`--queue`, a `--when-ready` leftover) to a seat that is idle, done or working first delivers the messages queued on refusal before it, its own message staying queued: `delivered 1 queued on refusal to X (…)` after the queued line. An FYI to an idle seat whose queue holds a refusal message is delivered with it rather than waiting, since the seat is woken anyway.
- **`deliver`.** A working seat with a refusal message gets its queue typed ahead (`delivered 2 to X (working: typed into its own input, read after its current turn), 1.2 KB`). `deliver NAME --now` types any mailbox ahead to a working seat; `--now` without names is a usage error (a hand tool, aimed at seats), and it never reaches a blocked, unknown or absent seat. `watch --once` follows `deliver`.
- **The watcher.** Each wait asks for `--until working` too when the mailbox holds a refusal message at that moment. On `working` it types ahead at once, without the debounce (`X working; typing ahead 1 (queued on refusal)`, or `typing ahead 3 (1 queued on refusal)` for a mixed mailbox). After the debounce re-check a seat found working with a refusal message is typed ahead too. If the refusal message was delivered meanwhile, the waiter goes back to waiting for idle or done.
- **`notify_blocked_after`.** 20.5 says the watcher already observes states through `agent wait`; it does not for blocked seats (no wait asks for `blocked`, and a seat with an empty mailbox has no waiter). With the setting, the watcher reads `agent list` every 30 s and records every seat; a seat blocked for the duration gets one notification, `X blocked 3m (P): answer its prompt` with the body `blocked since HH:MM; herdr-fleet sends it nothing while it is blocked`, sound `request`. An episode is the `since` of `state_seen`; once notified it is marked `"notified": true` there, so a restarted watcher does not notify it again. Without a state file the episode is kept in memory. The value uses the `--ttl` grammar; anything else is a fleet-file error.
- **`state_seen`.** Kept in the seat's state-file entry, written only when the state file exists (the fleet was brought up) and never on a dry run, for seats only; `absent` is recorded too, so a seat that comes back blocked starts a new episode. Recorded by `status` (every seat), `send` and `deliver` (every seat from their `agent list`, the targets from their fresh `agent get`), `inbox` (the caller's own seat, from its caller lookup; nothing with `--as`, and nothing in hook mode) and `watch` (wait answers, re-checks, `agent list`). Several commands write it at once, so `state.record_seen` rewrites only that field under an `flock` on the `.herdr` directory, and `save_state` takes the same lock and keeps the `state_seen` already on disk. An entry may hold only `state_seen`; it counts as no entry everywhere else.
- **Labels and ages.** Ages floor to `3m`, `1h05m`, `2d03h`, and `<1m` under a minute. A seat blocked at startup reads `blocked (4m) (blocked at startup)`. `status --json` adds `state_since` (null without a record). The `queued:` line keeps 0.6.0's rule of an `(oldest …)` part only for two or more messages; `queued_oldest` in JSON keeps times.
- **Owed lines.** `awaiting: B owes A for 5h12m (#a1b2c3, since 08:49): first line`, or `(no id, since …)`; `status --json` and `log --owed --json` keep their shape.
- **`ack --older-than`.** Each old message is acknowledged by id. A message without an id is acknowledged by its sender's name only when no newer message from that sender is still owed within the limit, since a plain `ack` settles everything before it; otherwise `skipped a message from X since HH:MM, … (…): it has no id, and ack X would also settle newer ones`. Names and ids restrict it; `--all` adds nothing. `--dry-run` works for every form of `ack` (`would ack …`). Lines: `acked #3f2a9c from X since 08:49, 5h12m ago (first line)`; with nothing old enough, `nothing owed by X older than 2h`.
- **Kickoffs as boundaries.** Only a logged kickoff counts (one herdr accepted). Messages the kickoff itself carried in `{inbox}` count from the kickoff and stay owed: the fresh context has them. An unconfirmed kickoff is not logged, so it settles nothing, and what it carried counts from its `delivered` entries. Invariant 16 names kickoffs, which 20.6's wording left out.
- **Send lines.** Every queued message reads `queued in herdr-fleet for X (state) #id, position N of M`, FYIs and `owner` included. 20.8's FYI line lacks 20.3's "in herdr-fleet" and position, so both are added; its explanation `fyi: delivered with the next plain send, kickoff or deliver` replaces `fyi` for every queued FYI, whatever the state. Positions count the messages not past their ttl, in delivery order. `deliver`, the flush and dry runs use the same `working: typed into its own input, read after its current turn` wording.
- **`--urgent`.** The header is `priority: 1`; `new/` is read in delivery order (urgent first, then oldest first) by batches, kickoff blocks, listings and positions; the pointer's "oldest" stays the oldest. To a working seat the line ends `; --urgent cannot reorder the seat's own input` (it already says the text was typed into the seat's own input, so "typed ahead" is not repeated). It combines with `--queue`.
- **Listings.** `1. #3f2a9c  10:09  sample-orchestrator  [urgent]  [on refusal]  first line`: every message has one of `[on refusal]`, `[queued]`, `[fyi]` (plus `[fyi]` for an FYI sent with `--now`). `--json` adds `queued_by`, `urgent` and `position`.
- **`inbox_print_max`.** Measured on each message's body. The cut prints the header line, ten lines and `… 17.0 KB in full: <path>` (the file in `cur/`); `--json` is never cut. `--full` goes with `--read` or `--history` only (usage error otherwise, and with `--hook`). A value that is not a positive integer is a fleet-file error.
- **History.** Log-only messages are `send` entries between the two names, `sent` or `unconfirmed`, whose id has no file in either mailbox (`new/` or `cur/`). They read `[from A to B at 10:09, first line only]`, with `to B` like the other history items. The note follows a blank line. The empty case reads `no messages between A and B` (0.6.0 said "no delivered messages between"). `--json` items carry `body_available` (true for files) and `body: null` for log-only ones. Items sort by time, files first within a second.
- **Cross-fleet.** As 20.8 says, only for a live agent that is neither seat nor contact: a contact's entries carry the contact's name, which the other fleet does not know, so a contact is logged here only (send to the agent's herdr name to log both sides). Every `send` entry the send logs (sent, unconfirmed, queued, refused) is mirrored with the body into that fleet's `cur/`. So that the other side sees the debt of a message first queued here, the `delivered` entries of its later delivery by `send` or `deliver NAME` are mirrored too. The walk stops at the first fleet and ignores `HERDR_FLEET_FILE`; this fleet's own repository is recognised by real path. The other fleet's `.herdr/.gitignore` gets the usual entries, as `inbox.write` does everywhere. A file with the same id there is never overwritten; short ids are unique per tree only. A failure there is a warning and never fails the send. Invariant 15 now names the other fleet's inbox.
- **Inbox hint.** Counts the sender's messages not past their ttl; with `--as NAME` the hint reads `herdr-fleet inbox --as NAME --read`. Printed on dry runs too (it only reads).
- **Roster.** The two 20.2 and 20.3 sentences follow 17.7's sentence in `{roster}`; no `examples/team` file changed.
- **Invariants.** Invariant 11's exception names the typed-ahead deliveries of 20.8. Invariant 18 holds after sending: the FYI prefix rule of 17.1 reads the text once, at send time, and is recorded as `queued_by: fyi`; nothing reads a body to decide a delivery.
- **Tests.** The fake records, at every `agent prompt`, the target's name and the mailboxes and short ids holding a refusal message at that moment (from the fleet's inbox, passed by `tests/support.py`); its answers are unchanged. `support.typed_ahead_allowed` lets a prompt reach a working agent only from `send`, `deliver --now`, or `watch` and `deliver` for such a mailbox. `tests/test_section20.py` covers 20.7 and 20.8 (38 tests); the 479 tests of 0.8.0 pass with their expected lines updated to the new wording.

## 21. The owner at the keyboard: no typing into working seats, and the presence guard (version 0.10.0)

Safety problem: a prompt paste can merge with draft input, or its Enter can reach a dialog that appears before state detection catches up. The presence guard and default queue-to-working behavior address these races. Historical controlled smoke checks established the input-merge behavior and the detection delay described below.

- `agent prompt` pastes the text and an encoded Enter into the pane's input as one submission (herdr documents this). Whatever already sits in the box goes with it: with `half typed by the owner` in the smoke seat's box, a delivered message was submitted as the single prompt `half typed by the ownerping from test: ...`. Every paste can do this, to an idle seat as much as to a working one.
- herdr classifies Claude Code's question dialog (`AskUserQuestion`) as `blocked`, and refuses to prompt it, but it recognises the dialog from the screen: the Claude integration hook (v11) reports only `SessionStart`. Measured once: the dialog was on screen 4.02 s after the prompt and `agent get` said `blocked` at 4.25 s. An `agent prompt` sent in that window was accepted (`agent_status: working`), its Enter selected the first option and the seat carried on. Only a paste into a `working` seat can land in that window, and 0.9.0 pasted into working seats from a plain `send`, from the flush before send, from `deliver` and from the watcher the moment a seat left `blocked`, which is exactly when dialogs come one after another.
- Focus is readable without any new command: `workspace list` carries `focused` and `active_tab_id` per workspace, `pane get` carries `focused` and `tab_id`. `pane read PANE --lines N` prints plain text even under `HERDR_OUTPUT=json` (the client's `text()` path). Empty prompt lines read `❯` for Claude Code, `› Ask Codex to do anything` for Codex (a placeholder) and `>` for Antigravity; typed text follows the glyph on the same line (`❯ half typed by the owner`).

Two changes, both vendor-neutral, in one release. The 0.7.0 hooks stay the opt-in Claude extra they are: they avoid the input box altogether but are not the fix.

### 21.1 Nothing is typed into a working seat unless the owner says `--now`

A `working` seat receives text only from `send --now` and `deliver NAME --now`. Every path that typed ahead in 0.9.0 now leaves the message queued and lets the seat's next idle or done carry it: a plain `send` to a working seat, the flush before send, `deliver` for a mailbox holding a message queued on refusal, and the watcher's `--until working` wait, which goes. This costs the team nothing in practice: Claude Code, Codex and Antigravity all hold typed-ahead text until the current turn ends (Antigravity even shows `Press up to edit queued messages`), so a typed-ahead message was read at the next turn anyway, which is when the watcher delivers, seconds later. What is lost: in a fleet without a watcher (`watch = false`, or the strip closed) a message to a busy seat waits for the next `send`, `deliver` or kickoff instead of sitting in the seat's own input; `status` shows the queue and `watcher: not running`, as it does today.

- The 18.3 table's `working` row becomes: default `stays queued` (`queued in herdr-fleet for X (working) #id, position N of M`); `--queue` `stays queued`; `--now` `typed ahead now, with the batch in front`; `--fyi` `stays queued`. The `--when-ready` leftover keeps 18.11's rule (never typed ahead).
- `queued_by` keeps its three values: the header still records why a message waits and listings keep `[on refusal]`, `[queued]` and `[fyi]`; `refusal` and `queue` messages are now delivered alike, at the seat's next idle or done, by every path. Invariant 18 is unchanged. 20.9's "an FYI to an idle seat whose queue holds a refusal message is delivered with it" stays: that seat is idle.
- The flush before send happens only on a delivery to an idle or done seat; a `--queue` or FYI send to a working seat flushes nothing.
- `deliver`: a working seat reads `X: working, 3 left queued`, as 18.5 says; `deliver NAME --now` keeps 20.9's line (`working: typed into its own input, read after its current turn`). README says plainly that `--now` is the one way text enters a seat mid-turn and that a dialog appearing in the next quarter second can take its Enter.
- The watcher waits `--until idle --until done` for every mailbox holding a non-FYI message, whatever its `queued_by`; the `typing ahead` lines go; a seat found working at the debounce re-check goes back to waiting. `watch --once` follows `deliver`.
- `--urgent` reaches a working seat only with `--now` (the `--urgent cannot reorder the seat's own input` ending stays for that case).

### 21.2 The presence guard

Before any text goes to a seat, through `safe_prompt` or `quit_agent`, the plugin asks whether the owner is typing there:

1. **Is this the owner's pane?** `pane get TARGET`: when `focused` is false, it is not, and the guard is done (one read-only call, the common case). When true, `workspace list`: the pane is the owner's only if its workspace is `focused` and that workspace's `active_tab_id` is the pane's `tab_id`. herdr's focus is the UI's, so this is the pane the owner's keystrokes go to.
2. **Is the owner typing?** `pane read TARGET --lines 12`. The prompt line is the last of those lines whose first non-blank character is the kind's prompt glyph: `❯` or `>` for `claude`, `›` for `codex`, `>` for `agy` (`seats.PROMPT_GLYPHS`, beside `DEFAULT_QUIT`; the kind is the live agent's `agent` field). The text after the glyph, stripped, is "typing" when it is non-empty and not the kind's placeholder (`seats.PROMPT_PLACEHOLDERS`: Codex's `Ask Codex to do anything`). No prompt line among the twelve means the agent's UI is in some other mode (a dialog, a picker, a menu), which is just as unsafe to paste into, so it counts as typing too.
3. A live kind with no glyph entry cannot be checked: the guard passes, as in 0.9.0, and `doctor` says `info presence: no prompt glyph for kind X; its seats are not held while the owner types`.

The outcome is a new `safe_prompt`/`quit_agent` status, `typing`, detail `owner typing in w3:p1`, and nothing is sent. The guard runs after the state check (a blocked or unknown seat is refused before any screen is read) and before every prompt, kickoffs after `agent start` included (their line is empty, so they pass; the cost is the one `pane get`). What each caller does with `typing`:

- `send`: the message stays queued, `queued in herdr-fleet for X (idle, owner typing in w3:p1) #id, position N of M`, exit 0, `queued_by: refusal` as for any plain send the seat could not take. `send --now`: refused like `--now` to a blocked seat, `refused X (idle, owner typing in w3:p1): nothing sent`, file to `cur/`, `delivery: "refused"`, exit 1.
- `deliver`, `watch --once`, the flush before send: `X: owner typing in w3:p1, 3 left queued`; nothing moves.
- `restart`, `swap`, `down` (the quit) and `kickoff` of a live seat: refused exactly as for a blocked seat, `refused X (owner typing in w3:p1)`, exit 3.
- The watcher: after the debounce re-check, on `typing` it prints once `X idle; owner typing in w3:p1, holding 3` and re-runs the guard every 3 s (`pane get` first, so a pane the owner left costs one call) with no cap: the hold lasts exactly as long as the owner's focus and text do. When the guard passes it re-reads the state and delivers if the seat is still idle or done, else goes back to waiting (`X working; waiting`). A seat the owner is merely looking at, with an empty line, is delivered to: the owner sees the paste land.
- Hooks (18.7) are untouched: a hook reads the inbox inside the seat's own process and types nothing.
- `--dry-run` runs the guard too (it only reads) and reports `would hold: owner typing in w3:p1`.

Cost: one read-only herdr call per delivery attempt when the pane is not focused, three when it is. No fleet-file key: the guard is always on, since the merge is never wanted. The twelve lines are read for the prompt line alone and discarded (invariant 19).

### 21.3 Invariants (section 7)

Invariant 2 and invariant 11 are amended as section 7 now reads; invariant 19 is new. Invariant 12's "never answers a dialog" is what 21.1 restores in full: the quarter-second window belongs to herdr, and the plugin stays out of it by never pasting into a seat that could be about to show one.

### 21.4 Code and tests

New module `presence.py`: `owner_pane(herdr, pane)` (the focus chain), `prompt_line(text, kind)` (pure), `typing(herdr, pane, kind)` (the whole guard, returning `None` or the detail). `seats.safe_prompt` and `quit_agent` call it and return status `typing`; `SEND_ALLOWED` becomes the `--now` allowance only, so no caller passes `working` without `--now`. `watch.py` loses `AHEAD` and the refusal waits and gains the hold loop. The fake herdr gains `workspace list` focus and `active_tab_id` per workspace, `pane get` `focused`, and `pane read --lines N` answering a scripted screen per pane (default: the empty prompt of the pane's agent kind), with helpers `fake_herdr.focus(world, pane)` and `fake_herdr.type_in(world, pane, text)`. `tests/support.py`: `typed_ahead_allowed` is true only for `send --now` and `deliver --now`; every test fails if an `agent prompt` reached a pane that was, at that call, the focused chain's pane with typed text or no prompt line (the fake records `owner_typing` on each prompt call). Cases: the guard table (not focused; focused and empty; focused and typing; focused with no prompt line; the Codex placeholder; Antigravity; an unknown kind passes and doctor says so); every caller's `typing` line; the watcher's hold and release with the fake clock, and the seat going working during a hold; no `--until working` wait anywhere; the flush only on delivery; the 18.3 `working` row for all four modes; `restart`/`down` refusing a typing pane. `docs/testing.md` gains the live reproduction: `herdr pane send-text <smoke pane> "half typed"` (never on a pane in state `blocked`), then a `send`, then `pane read` to see that the line is still the owner's. README's inbox table, the "on refusal" paragraph and the `deliver` row change to match, and the field-report note to the orchestrators explains why the drain they asked for was reversed.

### 21.5 Amendments from the build

One build makes 21.1 to 21.4; the version is 0.10.0. New module `presence.py`; `herdr.Herdr.pane_read` runs `pane read PANE --lines N` through the plain-text path. Deviations and decisions the section left open:

- **Report lines.** `restart`, `swap` and `down` keep the lifecycle report format, so a typing seat reads `sample-dev: refused - owner typing in w3:p1` (exit 3) rather than `refused X (owner typing in …)`. `kickoff` of a live seat reads `sample-dev: not briefed - owner typing in w3:p1` (exit 3), which is exactly what it prints for a blocked seat.
- **Dry runs.** `send --dry-run`: `would queue in herdr-fleet for X (idle); would hold: owner typing in w3:p1`, or with `--now` `would refuse X (idle): nothing sent; would hold: …`; a `--queue` dry run whose flush would be held ends `; would hold: …` instead of `; would deliver what was queued on refusal`. `deliver --dry-run`: `X: would hold: owner typing in w3:p1, 3 left queued`. `restart --dry-run` prints `X: plan - would hold: owner typing in w3:p1 (the quit is refused while the owner types there)` and plans nothing further for that seat. `swap --dry-run` lists drift only, as before; `kickoff` and `down` have no `--dry-run`; `up --dry-run` runs no guard, since its kickoffs go to seats not started yet.
- **A herdr error inside the guard sends nothing.** If `pane get`, `workspace list` or `pane read` fails, `safe_prompt` returns status `error` (`presence check: herdr error …; nothing sent`), which every caller already reports as a failure: `send` leaves the message queued (`failed X: …; #id left queued`, exit 1), `deliver` prints `failed X: …` (exit 1), a quit is `failed`, a kickoff `not briefed`, and the watcher pauses that mailbox for 60 s (an error during a hold does the same).
- **Order of the checks.** The kind is looked at first: a kind with no glyph entry passes without any herdr call, so its cost is zero rather than one `pane get`.
- **Doctor.** The kind checked per seat is the live agent's, or the fleet file's for a seat that is not live; one `info presence` line per kind, in seat order.
- **The flush.** 20.9's flush (refusal messages delivered, with their whole mailbox, before a send whose own message stays queued) now runs only when the seat is idle or done: a `--queue` send to such a seat still takes them along, and an FYI to an idle seat still rides with them. A `--when-ready` leftover is never idle or done, so it never flushes. A seat that turns working between `send`'s state check and the prompt now keeps the message queued (`queued in herdr-fleet for X (working) #id, …`), since only `--now` allows `working`.
- **The watcher's hold.** When the guard passes, the delivery goes through `prompt_mailbox` as usual, so the guard runs once more inside `safe_prompt`; if the owner typed again in that instant the hold goes on without a second line. The hold also ends, delivering nothing, when the mailbox was emptied meanwhile (read by the seat, delivered by another command) or the agent is gone (the next pass notes it as absent). After the debounce re-check the old line stays `X working; still waiting`; the line after a hold is the section's `X working; waiting`.
- **Fake herdr.** Beside `focus` and `type_in` it has `unfocus` (no workspace focused), `show_dialog` (a permission dialog, no prompt line) and `set_screen`. It judges `owner_typing` with its own copy of the glyph table, so a kind it has no entry for is never recorded as typing, as the guard passes such a kind. `tests/support.py` fails a test on any `agent prompt` call recorded as `owner_typing`, whatever herdr answered, and lets `watch` run the guard's three read-only commands.
- **Delivery-policy rationale.** README explains why default typing-ahead was withdrawn: a working seat may show a dialog before the state detector notices it.
- **Bordered prompt lines.** Added by the designer after the build: `prompt_line` strips box-drawing verticals (`│`, `┃`) from both ends of a line before looking for the glyph, so a build that boxes its input line (`│ > │`) reads as an empty prompt line rather than as no prompt line, and `│ > text │` as typed text.
- **Historical live verification.** A disposable seat confirmed the focused/unfocused input hold, explicit-now refusal, queued delivery after the input was cleared, watcher hold/release and normal cleanup. The checks used controlled scratch input; no operational message content is retained.

## 22. Finding a seat: pane names and `go` (version 0.11.0)

Several seats of the same agent kind need distinct visible labels and explicit navigation. Historical API checks established the pane-label and agent-focus behavior below; local fleet sizes and identities are omitted.

- herdr draws a label in each split pane's border. With `ui.show_agent_labels_on_pane_borders = true` (when enabled) it is the detected agent kind, so every Claude pane in a fleet reads the same; the docs say the setting applies "when no manual pane name is set". `herdr pane rename PANE LABEL...` sets that manual name (`pane get` returns it as `label`, null when unset; `--clear` removes it), and it then shows in the border in place of the kind. Plugin panes already carry their manifest title as the name (`Fleet watch`). A seat restarted into a new pane needs its label applied again, which is why the plugin maintains seat labels.
- `herdr agent focus TARGET` (a live agent name or a pane id) moves the UI to that agent's pane and marks it seen (its done badge clears). It takes no options.
- `pane rename` and `agent focus` send nothing to the agent and are not among the commands `herdr.py` refuses.

### 22.1 Pane names

Every seat's pane is named after the seat. The optional seat key `label` (a non-empty single line, at most 40 characters) replaces the seat name on the border for owners who want `ORCH` instead of `sample-orchestrator`; it changes nothing else (names in messages, logs and listings stay the seat name).

- **Who applies it.** `labels.apply(ctx, live)` runs at the end of `up` (every live seat, adopted and `--me` seats included), `restart`, `swap`, `kickoff`, `tile` and `status` (`--json` too): for each live seat, `pane get` on its pane; when `label` differs from the wanted name, `pane rename PANE NAME`. A pane already named as wanted is left alone, so the steady state costs one read per seat and no writes. Never on a dry run.
- **Only seat panes.** A contact's pane, a live agent of another fleet, the watcher strip and any plugin pane are never renamed (invariant 20). A seat that is absent has no pane to name.
- **`down`.** A seat pane that `down` leaves open at a shell prompt (no `--close-panes`, or a pane the plugin did not create) gets its name cleared (`pane rename PANE --clear`), so an empty shell is not labelled as a seat. A pane that is closed needs nothing.
- **Output.** `up`, `restart`, `swap`, `kickoff` and `tile` add one line only when something changed: `panes: named 4 after their seats` (or `named 1 (sample-designer)` for a single rename). `status` prints nothing about it. A rename that fails is a warning (`panes: could not name w2:p3: <reason>`), never an error, and never stops the command.
- **Doctor.** `info pane names: 2 seats unnamed (sample-designer, sample-dev); run herdr-fleet status` when a live seat's pane is not named as wanted; `ok pane names: every live seat's pane is named` otherwise.

### 22.2 `go`

`go NAME [--fleet PATH]` focuses a seat's pane from anywhere: NAME resolves as a `send` target does (a seat, a contact, or a live herdr agent name), the plugin runs `herdr agent focus <herdr name>` and prints `focused sample-orchestrator (w2:p5, working)`. An absent seat exits 3 with `sample-dev is absent; nothing to focus`; an unknown name is a usage error (exit 2). Nothing is written: `go` records no `state_seen` and touches no file. It is the one command that moves the owner's focus, and it does so because the owner asked by name.

- **Popup.** The `manage` menu's action list gains `go`. It takes exactly one seat (`a` answers `go needs one seat`), asks no confirmation and waits for no Enter: the popup exits as soon as `agent focus` returns, so the focus lands on the seat and not on the popup. An absent seat prints its line and waits for Enter as the other actions do.
- **Manifest.** Action `go` ("Fleet: go to a seat", context `workspace`) runs `ui go`, which opens the popup pane `go` (`placement = "popup"`, 50% by 50%, command `herdr-fleet menu --go`) through `plugin pane open` with the repository's cwd, like `ui manage`. `menu --go` prints the seats numbered with state and pane, contacts after them (`c1`, `c2`, as the agent list knows them), reads one answer, focuses it and exits; `q` or Enter closes with nothing done; an absent seat says so and asks again. The seat list is the same `collect_status` listing the `manage` menu prints.
- **Sidebar.** Nothing to build: clicking an agent's row in herdr's sidebar focuses its pane, and `ui.agent_panel_sort = "priority"` keeps the seats that need attention on top.

### 22.3 Not built

Seat order remains the order declared in the fleet file; the plugin does not infer an orchestrator role or reorder seats automatically. The later top-band option is specified in section 26. Per-state pane colours remain Herdr's responsibility.

### 22.4 Code and tests

New module `labels.py`: `wanted(seat)` (the `label` key or the seat name), `apply(ctx, live, report)` and `clear(ctx, pane, report)`; `herdr.py` gains `pane_rename(pane, name)`, `pane_clear_name(pane)` and `agent_focus(target)`; `fleetfile.py` parses `label` (type and length checked, errors as for `notes`); `ops.py` gains `op_go` and `ui go`; `menu.py` the `go` action and `--go` mode; `cli.py` the `go` command and `menu --go`; `checks.py` the doctor line; `herdr-plugin.toml` the action and the popup pane; version 0.11.0. The fake herdr: `pane rename` sets the pane's `label` (and `--clear` removes it), `pane get` returns it, `agent focus` records the call and sets the world's focused pane (reusing the 21.4 focus chain), and the call log carries, for every `pane rename`, whether the pane hosted a seat and what name the fleet wanted. `tests/support.py` fails any `pane rename` on a pane that hosts no seat of the fleet, or to a name that is not that seat's wanted name, and any `agent focus` not issued by `go` or the popup. Cases: names applied by each of the six commands, no rename when already named, the `label` key (valid, empty, too long, not a string), contacts and the strip never renamed, `down` clearing a kept pane and not a closed one, dry runs renaming nothing, a failed rename as a warning, the doctor line both ways, `go` for a seat, a contact, a live agent, an absent seat and an unknown name, the popup `go` flow (one seat, `a`, absent, quit) and `menu --go`, and the manifest entries. README gains a "Finding a seat" section (border names, `label`, `go`, the popup action, the sidebar) and the command table rows; AGENTS.md's architecture paragraph names `labels.py` and `go`.

### 22.5 Amendments from the build

One build makes 22.1, 22.2 and 22.4; the version is 0.11.0. New module `labels.py` (`wanted`, `apply`, `clear`, and `unnamed` for the doctor); `herdr.Herdr` gains `pane_rename`, `pane_clear_name` and `agent_focus`. Deviations and decisions the section left open:

- **`label` validation.** Besides type, emptiness, one line and 40 characters, a label must not start with `-`, which `herdr pane rename` would read as an option (`agents[1] (sample-dev).label: '-dev-' must not start with '-'`); blanks around it are dropped, since a name that differed from the pane's `label` only by blanks herdr trims would be renamed on every run. A blank-only label is empty.
- **Warnings and the exit code.** A failed `pane get` or `pane rename` while naming is a `panes: could not name P: herdr error …` line that leaves the exit code alone. `status` prints nothing about naming on stdout, so its table and the `--json` payload are byte for byte what they would be without it; a failure there goes to stderr as `herdr-fleet: panes: could not name P: …`.
- **Where `apply` runs.** At the very end of each command, after the strip (`up`), the kickoffs and the state save, with a fresh `agent list` (one call), so seats started or adopted in the run are included. `swap` names once, at its end: the `restart` it runs for drifted seats does not name. `tile` names also when it found nothing to tile in the fleet's workspace, and names live seats in other workspaces like every other command; a `tile` that refuses (seats across tabs, no workspace) names nothing. `status` names after `collect_status`, from the panes it read, without a second `agent list`. The `manage` popup's seat list and the status table `up` prints after its report do not name (they are not `status`).
- **`down`.** Only panes whose agent `down` quit in this run are cleared, after `--close-panes` (a pane it closed is skipped) and before `--close-workspace`; a seat refused or already absent keeps its pane's name. The clear reads the pane first and runs `pane rename P --clear` only when the pane has a name and hosts no agent. A successful clear prints nothing; a failure is `panes: could not clear the name of P: …` with the exit code unchanged.
- **Doctor.** The unnamed seats are listed in alphabetical order (as the section's example), `1 seat unnamed (…)` in the singular; the line comes after the `presence` lines. A pane `pane get` cannot read is not counted; when `agent list` fails no line is printed (the `herdr server` line says why). With no seat live the `ok` line is printed.
- **`go`.** The pane and state in the line come from `agent get`, read just before `agent focus`, so a done seat reads `done` (herdr then marks it seen). `go owner` is a usage error (`owner is not a pane; nothing to focus`). A contact that cannot be reached exits 3 like an absent seat, with the reason: `partner-workspace is absent (no live agent matches cwd …); nothing to focus` or `… is ambiguous (2 live agents match …)`. A herdr failure of `agent focus` exits 1 like any herdr error. `go` takes `--fleet` only (no `--notify`).
- **Popups.** `go` is the seventh and last action of the `manage` menu, so the existing numbers stay; after `a` and `go` the menu prints `go needs one seat` and the action list again. `menu --go` lists contacts as `  c1) partner-workspace  contact  idle  w3:p4` (the kind column says `contact`), prompts `Go to (number, c1-cN, or q to quit): ` (`number` alone without contacts), takes `C1` as `c1`, answers anything else `Not a choice: X` and asks again, and asks again after an absent seat or contact as well. Like the `manage` menu, its listing is `collect_status`, which records `state_seen`; the focusing itself writes nothing.
- **Strip.** `labels.seat_panes` skips a pane the state file records as the watcher strip, even if a seat's agent were reported there; nothing else in naming reads the state file.
- **Fake herdr and harness.** The fake records the `pane rename` facts before any canned answer, so a canned failure is checked like a real one, and gains `rename_errors` ({pane: code}). Plugin panes now carry their manifest title as `label` (`Fleet watch`), where the open answer used to carry the entrypoint id. `tests/support.py` passes the fleet file the command uses (`--fleet`, `--file` where it is the alias, `HERDR_FLEET_FILE`, else the repository's) and `install_fake_herdr` sets `FAKE_HERDR_FLEET`. It allows `pane rename` from `up`, `restart`, `swap`, `kickoff`, `tile`, `status`, `down` and `menu`, the `--clear` form from `down` and `menu` (the popup's `down` runs it) and only on a pane hosting no agent, and never on a plugin pane. `tests/test_section22.py` has 63 tests and `tests/test_manifest.py` two more (the `go` action and pane run as herdr runs them); the 563 tests of 0.10.0 pass with two expectations changed (the action list gains `go`; `status --context` is compared with a second plain `status`, since the first names the panes).
- **Not verified live.** The build ran no `pane rename` or `agent focus` against a live herdr (only their `--help`, which matches 22's facts); `docs/testing.md` provides the current scratch-fleet workflow. `agent focus`'s answer is not read.
- **Historical live verification.** A disposable seat confirmed label application/clearing, `status` idempotence, doctor diagnostics, explicit focus, invalid-target errors and cleanup. The popup flow and live border rendering were not exercised in that check. Private fleet identities and navigation logs are omitted.

## 23. Mail before reports, senders, debts and two key paths (version 0.12.0)

This extension makes delivery outcomes prominent, resolves unnamed senders through contacts, improves acknowledgement discoverability, and defines two narrowly scoped key paths. Historical smoke checks on a disposable Claude seat established the public UI behavior described below; copied reports and operational message ids are omitted.

- A delivery outcome can be missed when an unread-inbox hint is printed last. Section 23.1 puts the hint first so the final line describes the send outcome.
- A pane whose agent has no herdr name resolves the sender to `owner` (`messages.caller_record`); that is how a contact's message arrived "[from owner]".
- `/exit` to a Claude Code seat with background work open shows, and herdr reports the seat `blocked`:

  ```
     Background work is running
     The following will stop when you exit:

     shell · sleep 900

     ❯ 1. Exit and stop tasks
       2. Move to background and exit
       3. Stay

     Enter to confirm · Esc to cancel
  ```

  `herdr agent send-keys NAME enter` selected the highlighted line and the agent was gone within 4 s; `down --close-panes` then closed the pane as usual.
- `herdr agent send-keys NAME ctrl+c`, once, to a Claude seat mid-tool-call: herdr reported `done` within seconds, the agent stayed alive, and its screen read `Interrupted · What should Claude do instead?`. A second Ctrl-C inside Claude Code's exit window quits it, so the plugin never sends two to a Claude seat. `esc` had no visible effect (a lone escape byte is not a reliable key). The Antigravity interrupt sequence uses two Ctrl-C keys; key sequences remain kind-specific and subject to the state and dialog guards in 23.7.
- `agent send-keys <TARGET> <KEY>...` and `agent read <TARGET> --lines N` are the forms; `esc` is the canonical escape name.
- `ack NAME` already settles everything NAME asked the caller, and `ack --older-than` already does the bulk case; both orchestrators grep ids or ack by hand because the roster text and the `status` lines never say so.

### 23.1 The hint first, the outcome last

`send` prints the inbox note **before** the per-target lines, as `note: 1 unread from sample-orchestrator (#103001, 20:20); read it before you go on: herdr-fleet inbox --read` (several senders: `note: 3 unread from sample-orchestrator and peer-reviewer (oldest #103001, 20:20); …`; with `--as NAME` the command reads `inbox --as NAME --read`). The per-target lines come last and are unchanged, except that a plain send queued to a working seat ends `; lands at its next idle, or now with --now`. Dry runs print the note too (it only reads).

### 23.2 `inbox --wait [--timeout MS]`

A seat that waits for an answer waits inside one tool call, at no token cost: `inbox --wait` blocks until the caller's mailbox holds at least one message not past its ttl (FYIs count: the seat is awake anyway), then behaves exactly as `inbox --read` (the batch printed in full, files to `cur/`, `delivered` via `inbox`). Something already waiting returns at once. The wait polls the mailbox directory every second and runs no herdr command after the caller lookup. `--timeout MS` ends it with `nothing arrived in 10m` and exit 3; without it the agent's tool timeout bounds the wait, which is the intended use. `--wait` with `--count`, `--history` or `--redeliver` is a usage error; `--as NAME` works as for `--read`. The roster sentence gains: "To wait for a reply, run `herdr-fleet inbox --wait --timeout 600000` instead of sleeping or polling: it returns with the reply inside your turn and costs nothing while it waits." The watcher and `inbox --wait` share the per-mailbox lock, so a message is delivered once whichever gets it first.

### 23.3 Who the sender is

`caller_record` resolves the caller's pane in this order: a named agent in the pane, its herdr name (as today); an agent without a herdr name, the fleet's `[[contacts]]` resolved against that one pane (the same matching `contacts.resolve` uses, restricted to the caller's pane), its contact name; otherwise a usage error, `the agent in w1X:pY has no herdr name; pass --as NAME or run: herdr agent rename w1X:pY NAME`. `owner` is the sender only when the pane hosts no agent or the command runs outside herdr. This applies wherever the caller is resolved: `send`, `ack`, `inbox`, `log`, `go`.

### 23.4 Debts anyone can close, and the two commands nobody knew

- `ack #id --reason TEXT` closes the debt of that message whoever owes it, including one between two other seats: logged as an `ack` entry naming the id, the caller as `by` and the `reason`; `unsettled` treats it as settling that message; `log` shows `acked by partner-workspace: superseded`. Without `--reason`, `ack #id` keeps today's rule and a debt that is not the caller's answers `#103008 is owed by peer-builder, not you; add --reason to close it anyway` (exit 2). `--reason` combines with ids only.
- Each `awaiting:` line of `status` ends with `(ack NAME, or ack #id)`; the roster sentence says "`ack NAME` settles everything NAME asked you; `ack --older-than 2h` the stale ones; `ack #id --reason TEXT` any debt at all".

### 23.5 Wording

Every refusal or queue line that names the state `blocked` reads `blocked: a dialog is open` (`send`, `deliver`, `restart`, `swap`, `down`, `kickoff`, the watcher's lines); the `status` STATE column keeps `blocked (4m)`. The presence guard's `owner typing in PANE` is already distinct.

### 23.6 `peek NAME [--lines N]`

Prints the last N (default 30) screen lines of a seat, contact or live agent through `herdr agent read NAME --lines N`, nothing else: read-only, nothing logged, nothing moved; an absent seat exits 3, an unknown name is a usage error. It is the orchestrators' raw `herdr agent read`, kept inside the plugin so `doctor`'s permission rules cover it. Invariant 19 is about the presence guard, which keeps nothing; `peek` prints what it reads because that is its job.

### 23.7 Two keys

Decided by the owner on 24 Oct 2026, both recommended and both approved. These are the first keys the plugin ever sends to an agent, so each is as narrow as the case that asked for it.

- **The exit confirmation.** In `quit_agent`, after the quit command, when the agent is still present `exit_dialog_after` (3 s) later, the plugin reads `agent read NAME --lines 16` and looks for the kind's dialog signature in `seats.EXIT_DIALOGS` (`claude`: the lines `Background work is running` and `Exit and stop tasks`, both present) with the exit choice highlighted (a line whose first non-blank character is `❯` and which contains `Exit and stop tasks`). Only then it sends `agent send-keys NAME enter`, once per quit, logs nothing in the message log, reports `sample-dev: quit - answered its exit confirmation (exit and stop tasks)` and goes on polling for the exit as today. Any other screen gets no key: the failure line becomes `sample-dev: failed - did not exit within 20000 ms after /exit; the pane shows a dialog: "<first non-blank line of it>"; answer it by hand` when a `❯` choice is on screen, else today's `not retried, no keys sent, seat left alone`. Kinds without an entry are never answered. Never on a dry run, never from the watcher (which quits nothing anyway).
- **`interrupt NAME [--wait MS]`.** For a seat of this fleet in state `working`: sends the kind's interrupt keys (`seats.INTERRUPT_KEYS = {"claude": ("ctrl+c",), "agy": ("ctrl+c", "ctrl+c")}`, 300 ms between keys; a `[interrupt]` table in the fleet file and in the plugin config overrides per kind exactly as `[quit]` does, with keys limited to `ctrl+c`, `esc` and `enter`; `codex` has no default and refuses with `no interrupt keys known for codex; set [interrupt] codex = ["ctrl+c"]`), then `agent wait NAME --until idle --until done --timeout MS` (default 10000) and prints `interrupted sample-dev (done)` or `sample-dev still working after 10 s; nothing more sent` (exit 3). `blocked`, `unknown`, `absent`, `idle` and `done` are refused with `nothing to interrupt: sample-dev is blocked (a dialog is open)` and the like; contacts and other fleets' agents are refused (`interrupt is for this fleet's seats`). Logged in `.herdr/messages.jsonl` as `{"kind": "interrupt", "from": <caller>, "to": NAME, "keys": [...], "result": <state>}`; `log` prints it as `interrupt → sample-dev (done)`. CLI only: no manifest action, no popup entry, never from the watcher. Claude seats get exactly one Ctrl-C.
- `herdr.py` gains `agent_send_keys(target, keys)` and `agent_read(target, lines)`; `_exec` keeps refusing `pane send-keys`, `pane send-text` and `pane run`, and `agent send-keys` is accepted only from `agent_send_keys`, which refuses any key outside `enter`, `esc`, `ctrl+c`. Invariants 1 and 21.

### 23.8 Not built

- "Deliver a seat's queue at the moment it sends": a send happens mid-turn, which is exactly what 0.10.0 stopped typing into; 23.1's note on the seat's own send is the mechanism, and 23.2 is the way to wait.
- A Claude Code hook that surfaces the queue on the next tool call: Claude-only, so not a fix; the 0.7.0 hooks remain the opt-in extra.

### 23.9 Tests

Fake herdr: `agent read` serves the scripted screen of the agent's pane (the 21.4 screens); `agent send-keys` records the keys and, when the pane's screen is the scripted exit dialog and the key is `enter`, removes the agent and puts the pane at a shell; when the agent is `working` and the key is `ctrl+c`, sets the state the scenario says (`done` by default for claude; a scripted `interrupt_results` list); helpers `fake_herdr.show_exit_dialog(world, pane)` and `set_screen`. `tests/support.py` fails any `agent send-keys` except (a) `enter` from `restart`, `swap` or `down` to an agent whose pane's screen holds the exit signature with the highlighted exit line at that moment, once per command per agent, immediately after an `agent read` of it, and (b) the kind's interrupt keys from `interrupt` to a seat that was `working` at its last `agent get`; and any `agent send-keys` from `watch`, `menu`, `deliver` or a dry run. Cases: 23.1 order and wording (one sender, several, `--as`, dry run, queued-to-working ending); 23.2 (returns at once, waits with the fake clock, timeout, FYIs count, ttl, usage errors, lock shared with the watcher); 23.3 (named agent, contact match, unnamed with no match, shell pane, outside herdr, each command); 23.4 (`--reason` by a third party, without `--reason` refused, log line, status hint, roster text); 23.5; 23.6 (seat, contact, absent, unknown, `--lines`); 23.7 (dialog answered, other dialog reported and no key, no dialog, kinds without entry, dry run, `interrupt` for each state and kind, config override, key whitelist, the log entry). README (send output, `inbox --wait`, `ack --reason`, `peek`, `interrupt`, the two keys under safety rules), AGENTS.md (the rules paragraph and the architecture paragraph), docs/testing.md (the two live reproductions above), `examples/team` and the `init` starter where the roster sentence lives.

### 23.10 Amendments from the build

One build makes 23.1 to 23.7 and the tests of 23.9; the version is 0.12.0. No new module: `seats.py` gains `EXIT_DIALOGS`, `INTERRUPT_KEYS`, `shows_exit_dialog`, `dialog_first_line`, `interrupt_agent` and `state_words`; `ops.py` gains `op_peek` and `op_interrupt`; `herdr.Herdr` gains `agent_read` and `agent_send_keys` (`SENDABLE_KEYS`), and `agent send-keys` stays in `FORBIDDEN`, so `run` and `text` keep refusing it and only `agent_send_keys` (which also refuses an empty key list and a target starting with `-`) can issue it. Deviations and decisions the section left open:

- **23.3, which commands.** `log` runs no herdr command and `go` resolves a target, not a caller, so neither resolves the caller and nothing changes for them (a pane with an unnamed agent can still run both). The rule applies to `send`, `ack`, `inbox` and `interrupt`, which logs the caller as `from`. Since the usage error says "pass --as NAME", `interrupt` takes `--as NAME` too. An unnamed agent that two or more contacts match is the same usage error, naming them (`… has no herdr name (it matches the contacts a and b); pass --as NAME …`). "Hosts no agent" is an `agent get` on the pane that fails or answers a record without `agent`.
- **23.1.** Several messages read `read them before you go on`; senders are listed in the order of their oldest unread message (`a and b`, `a, b and c`). The note counts messages not past their ttl. The working ending is added to a plain send only: not to an FYI, a `--queue` send or a `--when-ready` leftover (`working after --when-ready 5000 ms`), and also to the dry run's `would queue …` line and to a plain send that found the seat working only at the prompt.
- **23.2.** `--wait` takes the mailbox lock only to read; when the read finds nothing (the watcher delivered it first) the wait goes on until the deadline. `--timeout 0` looks once; a negative value, `--timeout` without `--wait`, and `--wait` with `--hook` are usage errors as well. Under a minute the timeout line reads `nothing arrived in 30 s`; from a minute on it uses the age format of 20.2 (`10m`, `1h05m`). A message past its ttl does not end the wait; the read that follows expires it as `--read` does.
- **23.4.** The `ack` entry of `--reason` names the debtor as `from` and the creditor as `to` (as if the debtor had acknowledged it), plus `by` (the caller) and `reason`, so `unsettled` needs no change (an `ack` with an `id` settles that message). The line is `acked #103008 from partner-workspace since 08:49 (first line), owed by peer-builder; reason: superseded` (`, owed by …` only for someone else's debt); `--dry-run` works with it. Without `--reason`, every id is checked before anything is written, so one id owed by someone else refuses the whole command (exit 2). `ack #id` for a message nobody owes still prints `nothing owed for #id` (exit 0). `--reason` with a name, `--all`, `--older-than` or a blank text is a usage error. The `status` hint is on `status` only (`log --owed` is unchanged); a debt without an id ends `(ack NAME)`.
- **23.5.** The `--when-ready` label reads `blocked after --when-ready 5000 ms: a dialog is open`. herdr's own error text for an `agent_blocked` answer at the moment of a prompt is quoted as herdr gives it. `up`'s `blocked at startup` lines and the watcher's `notify_blocked_after` notification are unchanged (neither is a refusal or queue line).
- **23.6.** `peek owner` and `--lines` below 1 are usage errors; a contact that cannot be reached exits 3 with its reason, as `go` does. The text is printed as `agent read` gave it, with its trailing newlines cut to one.
- **23.7, the exit confirmation.** The screen is read once, at the first poll 3 s or more after the quit command, and only for a kind with an `EXIT_DIALOGS` entry and when keys are allowed. "A `❯` choice on screen" is read as a line whose first non-blank character is `❯` followed by a numbered option (`❯ 1. …`), since Claude Code's empty prompt line is a bare `❯`; for the failure line the screen is read again at the deadline (for every kind), and "the first non-blank line of it" is the first non-blank line after the last horizontal rule above that choice, else the first of the 16 lines, with box-drawing borders stripped and cut at 80 characters. A failed Enter is `failed - its exit confirmation is on screen, but the Enter failed: <herdr error>; answer it by hand`; an Enter after which the agent still has not exited at the deadline is `failed - did not exit within 20000 ms after /exit, though its exit confirmation was answered once; not retried, no more keys sent, seat left alone`. The popup is held to invariant 21 by a `Ctx.keys_allowed` flag the `manage` menu clears: its `restart`, `swap` and `down` never look at 3 s and never press, and report the dialog for the owner.
- **23.7, `interrupt`.** Invariant 21 asks for a screen the plugin "has just read": `interrupt` reads `agent read NAME --lines 16` right before the keys and refuses a screen showing a highlighted numbered choice (`refused sample-dev: a dialog is on screen ("…"); no keys sent`, exit 3), since a dialog can be up a moment before herdr reports `blocked` (21) and a Ctrl-C or Esc would answer it. It also runs the presence guard (`refused sample-dev: owner typing in w3:p2; no keys sent`) and refuses the caller's own seat (`refused sample-dev: it is the caller's own agent; interrupt it yourself`), both exit 3; none of these three is in 23.7's text. Each key is one `agent send-keys` call, 300 ms apart. A Claude entry with two `ctrl+c` in `[interrupt]` is an error (23.7: "Claude seats get exactly one Ctrl-C"); in the fleet file every bad entry is a fleet-file error (exit 2), and in the plugin config a bad entry is left out and `doctor` prints `warn plugin config: <path>: [interrupt].codex: …`. After the wait: `interrupted sample-dev (done)` (or `(idle)`); `sample-dev still working after 10 s; nothing more sent`; `sample-dev is absent after the keys; nothing more sent`; any other state `sample-dev is blocked: a dialog is open after 10 s; nothing more sent`, all exit 3; a failed key `failed sample-dev: <herdr error>; 0 of 1 keys sent` (exit 1). The entry is written whenever at least one key went, with `result` the state reached (or `failed`). Usage errors read `interrupt is for this fleet's seats (partner-workspace is a contact)`, `(owner is not a seat)` or `(X is not a seat of <fleet file>)`; `--wait` must be more than 0. `log` prints `time  caller  interrupt → sample-dev (done)`.
- **Roster, examples and the starter.** The two sentences are appended to `kickoff.ROSTER_SENTENCE`, which every template gets through `{roster}`, so no file in `examples/team` and nothing in the `init` starter changed (as in 17.7 and 20.9).
- **Fake herdr and harness.** Besides `show_exit_dialog` and `set_screen`, the fake has `exit_dialog_on_quit(world, name[, screen])` (behaviour `dialog_on_quit`: the quit command shows that screen and makes the agent `blocked` instead of quitting) and `EXIT_DIALOG`, the confirmation with a line of conversation and a rule above it. `esc` changes nothing in the fake. It records the `agent send-keys` facts before any canned answer, as for `pane rename`, and `agent get` records the agent's name. `tests/support.py` (`KeysCheck`) also requires, for `interrupt`, an `agent read` of the seat earlier in the same command, takes the expected keys from the fleet file's and the config's `[interrupt]` tables read with the fake's TOML reader, and allows `agent read` only from `peek`, `interrupt` and the quit of `restart`, `swap`, `down` and `menu`; `peek` may run only `agent list`, `--version`, `pane current` and `agent read`. `tests/test_section23.py` has 65 tests; the 628 tests of 0.11.0 pass with their expected lines updated (the blocked wording, the working ending, the note first, and a shell pane for the `owner` sender).
- **Not verified live.** The build ran no `agent read`, `agent send-keys` or any other herdr command that changes state; only `herdr agent read --help` and `herdr agent send-keys --help` (herdr 0.9.5), which match the forms of 23. The exit confirmation's screen and the Ctrl-C behaviour are the designer's facts of 24 Oct; the rule above the dialog that `dialog_first_line` looks for is an assumption about Claude Code's layout (without one, the first non-blank line of the 16 lines read is quoted). `docs/testing.md` provides the current scratch-fleet workflow.

#### From 23.11

The same build makes 23.11's four points. Decisions and deviations:

- **Guard order.** `presence.typing` reads `pane read TARGET --lines 12` first, for every target pane; an empty line or the placeholder passes with that one call. Only when the line holds something does it ask `pane get` (the pane's `focused` and `tab_id`, which `workspace list` alone cannot give) and, when the pane is focused, `workspace list`. 23.11 names only the `workspace list` as conditional; the `pane get` moved behind the read as well, so the common case costs one call.
- **Wording of a dialog.** In a pane that is not the owner's, no prompt line on screen, or a "prompt line" that is a highlighted numbered option (`❯ 1. Yes`, which is how Claude Code's dialogs read to `prompt_line`), is worded `a dialog or a menu is on screen in w2:p5; answer or close it there` instead of `text in its input (w2:p5); press Ctrl-U there to clear it`, which would be wrong advice in a dialog. It holds the text exactly as 23.11 says; the owner's own pane keeps `owner typing in …` for every case, as in 21.2. `interrupt` therefore refuses a Claude seat showing such a dialog through the guard (`refused sample-dev: a dialog or a menu is on screen in w2:p2; answer or close it there; no keys sent`); 23.10's screen read before the keys still refuses one for a kind the guard cannot read.
- **The cleanup.** It runs only after a successful interrupt (`idle` or `done`), not while the seat is still working, and not for a kind without a prompt glyph. It reads `agent read NAME --lines 12` (so the harness can see the read); `ctrl+u` is pressed only when a prompt line is on screen and holds text (no prompt line could be a dialog, which a key could answer), and the second read counts as cleared only when it shows an empty prompt line. A `ctrl+u` that fails, or text that is still there, gives the still-held line (exit 3). The log entry's `keys` include `ctrl+u` when it was pressed.
- **Section 7.** Invariants 19 and 21 in section 7 (and their copies in 18.9) still read as before 23.11, since the build may only append to this file: 19 should say every target pane is read whatever the focus, and 21 should name `ctrl+u` and its one path. README's safety rules 17 and 19 already say both.
- **Fake and harness.** The fake's `input_held` is true for a prompt line with text and for no prompt line (for the kinds it has glyphs for), whatever the focus, matching the guard; `tests/support.py` fails any `agent prompt` it records as held, and allows `ctrl+u` alone, once per command, from `interrupt`, when that command's last `agent read` of the seat showed text (`input_text`). Four tests of section 21 now assert 23.11's guard (an unfocused pane is one `pane read`; text in a pane the owner left holds; a focused pane in an unfocused workspace or background tab holds with the new wording; the watcher's hold outlasts the owner leaving the pane). `tests/test_section23.py` gains 13 tests (78 in all); the suite has 706.
- **Not verified live.** Every delivery now reads its target pane even when nobody is in it. An idle seat whose last 12 lines show no prompt line (a kind's layout the guard has not seen, a long status block under the input) would have every delivery held; the screens of 21 (Claude Code, Codex, Antigravity) show one, but the build ran nothing against a live herdr.

### 23.11 Amendments from the live test (designer, 24 Oct 2026)

Two behaviors were established by a controlled disposable-seat check of the 0.12.0 build:

- **An interrupt can leave the prompt in the input box.** `interrupt fleet-smoke` sent while the seat was still thinking (before its tool call) settled it to `done` in under a second, and Claude Code put the interrupted prompt back into the input line: `agent read` showed `❯ Using the Bash tool, run: …` between the two rules. `esc` did not clear it; `ctrl+u` did, leaving `❯`. (When the Ctrl-C lands during a tool call, the screen reads `Interrupted · What should Claude do instead?` and the line stays empty.)
- **Text in an unfocused seat's input merges with the next paste.** `down --close-panes` on that seat pasted `/exit` after the restored text; the seat ran the task again instead of exiting, and `down` reported the quit as failed. 21.2's limit ("text left in a pane the owner clicked away from is not protected") is no longer acceptable now that the plugin itself can leave text there.

Changes, in the same release:

1. **The presence guard reads the prompt line of every target pane, focused or not.** `presence.typing` runs `pane read TARGET --lines 12` before every prompt and every quit; a prompt line with text, or no prompt line, holds the text whatever the focus. The focus chain only words the detail: `owner typing in w2:p5` when the pane is the owner's, else `text in its input (w2:p5); press Ctrl-U there to clear it`. Every caller's handling (21.2) is unchanged: `send` queues, `--now` refuses, `deliver` and the watcher hold and re-check every 3 s, `restart`, `swap`, `down` and `kickoff` refuse. Cost: one read-only call per paste, which replaces the `pane get` the guard made first; the `workspace list` call is made only when the line holds text, to choose the wording. A kind without a glyph entry still passes.
2. **`interrupt` cleans up after itself.** After the keys and the wait, it reads the prompt line once more; when the line holds text it presses `ctrl+u` once (`SENDABLE_KEYS` gains `ctrl+u`; it is sent only from this step) and reads again. The line becomes `interrupted fleet-smoke (done); cleared the interrupted prompt from its input`, or, when text remains, `interrupted fleet-smoke (done); its input still holds text; press Ctrl-U there before sending it anything` (exit 3). Invariant 21 names the third key and its one path.
3. **The quit line names the cure.** When `quit_agent` is refused by the guard, `restart` and `down` read `sample-dev: refused - text in its input (w2:p1); press Ctrl-U there to clear it`.
4. Tests: the fake's `agent send-keys ctrl+u` empties the scripted prompt line; a scenario flag `restore_prompt_on_interrupt` makes a `ctrl+c` put the last prompt back into the line; `tests/support.py` allows `ctrl+u` only from `interrupt`, after an `agent read` that showed text on the prompt line, and fails any `agent prompt` to a pane whose scripted prompt line holds text, focused or not. Cases: the guard on an unfocused pane with text (send, deliver, watcher hold and release, restart and down refusals with the new wording); `interrupt` with and without the restored prompt, the cleanup line, the still-held line; the focus-dependent wording. README's "the owner at the keyboard" paragraph and the 21.2 limit sentence are rewritten to match; docs/testing.md gains the reproduction (interrupt within 3 s of a prompt, then `agent read`).
- **Historical live verification.** Disposable-seat checks covered output ordering, peek, inbox wait/timeout, third-party acknowledgement rules, the recognized exit confirmation and interrupt-restored input cleanup. Input retained in an unfocused pane held later sends and quits until manually cleared. No production task, message id or live roster is retained.

## 24. The strip as a board (version 0.13.0)

A compact watcher pane needs a structured view of state, queues, debts and context readings. A curses board can render the existing model and local files without prompting an agent merely to refresh the display. Terminal dimensions vary; log mode remains available when a board is unsuitable.

### 24.1 Modes

`watch` draws the board when stdout is a terminal and `--log` is not given; `--log` (or a non-terminal stdout) prints today's lines unchanged; `--once` never draws. In board mode every line the watcher would have printed goes to `.herdr/watch.log` (appended with the same timestamps; `state.IGNORED` gains it) and to the footer. Ctrl-C, SIGTERM and SIGHUP stop the watcher exactly as today; no key in the board stops it.

### 24.2 The model

Rebuilt every `BOARD_REFRESH_S` (5 s) and after every watcher event (a wait returning, a delivery, a hold, an error), from:

- `agent list` (one call): each seat's state, kind, pane; contacts' states.
- `pane get` per live seat: `terminal_title_stripped` as "what it is doing" while `working` (Claude Code and Codex set it to the task; a title equal to the seat's name or label is shown as nothing).
- `state_seen` (design 20.5) for the state's age.
- The mailboxes: per seat, the queued count, the oldest age, and the next message in delivery order (id, sender, first line); the owner's mailbox the same way.
- `messages.owed` over the log (parsed again only when `.herdr/messages.jsonl` changed): per seat, the oldest debt it owes (to whom, id, age).
- `context_size.seat_context` for Claude seats (tokens and when), compared with `restart_hint_tokens` when set.
- For a `blocked` seat only: `agent read NAME --lines 16` and 23.7's first-dialog-line extractor, so the board can say what is being asked; one read per refresh per blocked seat, and the line is shown, never kept (invariant 19's rule applies: nothing else from a screen is shown or stored).
- The watcher's own state: what it is waiting on, holds and their reasons, retries, the last event with its time, the last error.

### 24.3 The board

Rows: a header, one row per seat in fleet-file order, contacts after them when height allows, and a footer.

```
sample · watching 11h · owner inbox 1: #103003 "Example ready…"                 08:02 delivered 2 to sample-dev
sample-dev           ● working 3m   vitest run             2 queued, oldest 3m  #103001 "Review parser tests…"   ctx 376k
sample-reviewer      ○ idle 2h                             owes orchestrator 2h #103002                      ctx 514k ⚠
sample-designer      ○ idle 25m                            –                                                 ctx 382k
sample-orchestrator  ◐ blocked 4m  "Allow Bash(pnpm test)?"  1 queued, holding: owner typing                ctx 730k ⚠
```

- Cells, left to right: name (the seat's `label` or name); state glyph, state and age (`●` working, `○` idle, `◐` blocked, `✓` done, `?` unknown, `–` absent); doing (the title while working, or the dialog's first line while blocked, quoted); queue (`2 queued, oldest 3m  #103001 "first line…"`, or `holding: <reason>` from the watcher, or `–`); debt (`owes orchestrator 2h #103002`, or `–`); context (`ctx 376k`, `⚠` at or above `restart_hint_tokens`; blank for kinds without a transcript).
- Width: the name column is 20, the state column 14, the context column 10; the rest share what is left in the ratio 2:3:2. Below 120 columns the doing cell goes; below 90 the debt cell goes; below 60 the row is name, state and queue count only. Text is cut with `…`, never wrapped.
- Height: header and footer always; when seats need more rows than remain, the rows are packed two seats per row (each half a compact cell: name, state, queue count, `⚠`) while the width allows two of at least 60 columns, else the rows show the most urgent seats (blocked first, then those with a queue, then working, then the rest in file order) and the footer ends `+N more`. Below three rows, one line: the most urgent seat's row.
- Colours (off under `NO_COLOR` or when the terminal has none): blocked red and bold, working green, idle and done dim, `⚠` yellow, errors in the footer red, the selected row reverse video.
- Footer: the last event with its time; a pending confirmation or the help line replaces it while shown.

### 24.4 Keys

Keys reach the strip only while it has focus (herdr's rule), so the board is complete without them.

- `1` to `9`: `go` to that seat (the row number), through `agent focus`; the focus leaves the strip as it lands.
- Up and Down select a row; Enter is `go` to it.
- `a`: ack the selected seat's oldest debt, after a footer confirmation `ack #103002 (sample-reviewer owes sample-orchestrator 2h)? y/n`; the ack is written as `ack #id --reason "from the board"` by `owner` (the strip's pane hosts no agent, so 23.3 resolves the caller to `owner`).
- `d`: deliver now every queue whose seat is idle or done, skipping the debounce, through the watcher's own delivery path (never to a working seat: invariant 11 holds, and no key send of any kind, invariant 21).
- `l`: toggle between the board and the scrolling log (the last lines of `.herdr/watch.log`); any key returns to the board.
- `?`: the help line in the footer for 5 s. Any other key does nothing; `q` shows `Ctrl-C stops the watcher`.

### 24.5 Code and tests

New modules: `board.py` (the model, `build(ctx, watcher_state) -> Board`, and the pure `render(board, width, height, colours) -> List[Line]`, where a `Line` is a list of `(text, style)` spans) and `tui.py` (the curses driver: a `Screen` protocol with `size()`, `draw(lines)`, `getch(timeout_ms)`, `KEY_RESIZE` handling; the real implementation wraps curses, the fake records draws and replays scripted keys). `watch.py` keeps every delivery path; it gains an event queue the threads post to, the refresh timer, the log file, and the key handlers, which call existing functions (`op_go`, `ack` with `--reason`, the watcher's deliver). Invariant 22: the board reads only what 24.2 lists, shows a dialog's first line and nothing else from any screen, prompts nothing, sends no key, and runs `agent focus` only on a key the owner pressed.

Tests (`tests/test_section24.py`): `render` at 162×6, 162×8, 120×6, 90×6, 60×6, 40×2 and with six seats in six rows (packing and the `+N more` form), each cell's text for every state, the `⚠` threshold, colours on and off; `build` from the fake (states, titles, ages, queues, owner inbox, debts, context from a fake transcript, the blocked seat's dialog line, holds); keys through the fake screen (`go` by number and by Enter, `a` with `y` and `n`, `d` delivering only idle seats, `l`, `?`, `q`, resize); `--log` and non-terminal stdout printing today's lines; the log file written in board mode; `tests/support.py` allows `agent focus` from `watch` only after a scripted key, `agent read` from `watch` only for a seat whose last `agent list` state was `blocked`, and keeps every prompt rule. README gains "The board" under the watcher section; docs/testing.md the live check (open the strip, press `?`, press a number). `[watch] ratio` is left as it is; the owner can raise it for more rows.

### 24.6 Amendments from the build

One build makes 24.1 to 24.5; the version is 0.13.0. New modules `board.py` (`build`, `render`, `visible`, `render_log`, the `Row`, `Board`, `WatcherState` and `Cache` types) and `tui.py` (`Screen`, `CursesScreen`, `open_screen`, `FakeScreen`); `watch.py` gains `is_terminal`, `LineSink`, the event queue, `Watcher.snapshot` and `BoardView` (the refresh timer, the drawing and the key handlers); `watch --log` is the new option. Deviations and decisions the section left open:

- **The header's right end.** 24.3's example ends the header with the last event, while its footer rule puts the last event in the footer. Built as the rule says: the event is in the footer only, and the header's right end shows the watcher's own state, `waiting on 2 · holding 1 · retrying 1` (each part only when non-zero). The owner's mailbox reads `owner inbox 0` when empty; it is usually empty, since the watcher notifies the owner at once (18.6).
- **What is read.** `pane get` runs for a `working` seat only, the one state whose title the board shows (24.2 says "per live seat"; this reads less). The board records the states its `agent list` showed through `seen.observe`, as 20.5 asks of every command that reads states, so the ages stay true; for a seat the state file cannot record (no state file yet) and for contacts, which have no `state_seen`, the age counts from when this watcher first saw that state, also a lower bound. Each wait that returns, with or without a line, is an event; a line said while the board builds (a warning from recording a state) asks for no second build.
- **Cells.** Ages are compact to fit the 14-column state cell: `<1m`, `25m`, `2h`, `3d` (not 20.2's `1h05m`). One queued message reads `1 queued, 3m  #103001 "…"` (`oldest` only from two, as `status` does). The queue cell's watcher notes are `holding: <reason>` with the guard's detail shortened (`owner typing`, `text in its input`, `a dialog or a menu`), `retrying in 45 s` after a failed delivery or wait, and `pointed to inbox --read` after a pointer prompt. The debt cell names the creditor by its `label`, else without the `<workspace>-` prefix (`owes orchestrator 2h #103002`, as the example); the confirmation and the log keep full names. A debt without an id (sent before 0.6.0) shows without one. The context cell is blank whenever there is no reading, a Claude seat whose transcript cannot be read included; the board keeps the reading's time in the model (`context_at`) but does not print it. A blocked seat whose screen shows no highlighted numbered choice shows nothing in the doing cell.
- **Narrow and short.** Below 60 columns the name cell shrinks below 20 so state and count stay; on a strip too narrow even for those the row is cut with `…`. When packing is not enough either (more than twice the rows), the most urgent seats are packed and the footer ends `+N more`; contacts follow the seats, packed too, while rows are left. The number keys count the rows as drawn, packed ones left then right.
- **Colours off.** `NO_COLOR` or a terminal without colours drops the colours and `dim`, but keeps `bold` on the blocked state (and a blocked seat's dialog line) and `reverse` on the selected row: they are attributes, not colours, and without them the selection could not be seen.
- **Keys.** `a` writes the ack with the caller given as `owner` (`op_ack(..., as_name="owner", reason="from the board")`), which is what 23.3 resolves for the strip's pane, so the watcher runs no caller lookup; `y` (or `Y`) confirms and any other key answers no (`not acked` for 5 s). Without a selection `a` and Enter say `select a seat with ↑ and ↓ first`; a seat that owes nothing says so; a debt without an id says that only its debtor can ack it (`herdr-fleet ack NAME`). `d` takes the mailboxes `deliver` takes without names (seats, then contacts, with a message that is not FYI), reads each seat's state with `agent get`, delivers to the idle or done ones through `Watcher.deliver` with no debounce, prints a line for the others (`sample-reviewer working; 1 left queued`), does not prompt again a mailbox a pointer already named (the watcher's own rule), and reports the presence guard's hold as one line rather than starting the waiter's hold loop on the board's thread. The `?` and `q` lines stay 5 s; a number with no row says `no seat 7 on the board`. A resize is not a key: it redraws and keeps the log view or a pending question.
- **Footer colours.** The lines that report a failure are drawn red (`cannot look`, `failed …`, `unconfirmed …`, a wait error, `cannot record …`, `owner: …`, a failed `go` or `ack`, warnings caught from other code); the loop's own trouble stays in the footer, in red, until a pass succeeds.
- **Output in board mode.** Everything other code prints while the board is up (`op_ack`'s line, warnings) is caught (`LineSink`) and goes to the log and the footer like the watcher's own lines, so nothing scribbles on the board; a warning is said once until its text changes, since the refresh would repeat it every 5 s. When the watcher stops, the board is torn down and the `stopped` line is printed on the terminal as well as logged. When curses cannot start (no `curses` module, a terminal type `setupterm` does not know), the watcher prints its lines as with `--log`.
- **Section 7.** Invariant 20 still says `agent focus` is run only by `go` and the popup; invariant 22 adds the board's number and Enter keys, and the build may only append to this file. README's safety rule 18 names the board, and its new rule 20 is invariant 22.
- **Tests.** The fake's `agent list` records `states` and its pane records carry `terminal_title` and `terminal_title_stripped` (`fake_herdr.set_title`). `FakeScreen(on_key=self.log_key)` writes each replayed key into the call log, and `tests/support.py` allows `agent focus` from `watch` only right after a number or Enter (one focus per key) and `agent read` from `watch` only of a seat of the fleet whose state at the command's last `agent list` was `blocked`. `tests/test_section24.py` has 57 tests; the 706 tests of 0.12.0 pass with four expectations changed (the `.herdr/.gitignore` written by the state file, the message log and `init` gains `watch.log`).
- **Verified, not live.** Besides the fake screen, the curses driver was run by hand in a pseudo-terminal of 162 by 6 against the fake herdr (never a live one) under both interpreters: the board drew, `?`, a number (`agent focus` on the fake), `l`, `q` and a resize to 90 columns worked, and SIGTERM, Ctrl-C and closing the terminal stopped the watcher and removed the lock. Nothing was run in the owner's strip or against a live herdr; docs/testing.md has the live check.
- **A fresh Claude Code session shows a suggestion on its empty input line** (found by the designer on 24 Oct while bringing up the four-seat board demo on this build): `❯ Try "write a test for test_init.py"`, drawn dim but printed by `pane read` like typed text, so `up` refused every kickoff with `text in its input`. `seats.PROMPT_PLACEHOLDER_PATTERNS` (`claude`: `Try "…"`) joins the exact placeholders: a prompt line matching one in full is empty. Anything beyond the suggestion (`Try "x" and more`) is still typed text.

## 25. Pages in the board (same release, 0.13.0)

The board gains pages for seats, contacts, owner messages and stored bodies. State ages remain compact. Enter opens a page; `g` explicitly focuses a seat; `z` names the watcher's own pane for zoom (25.7 corrects an earlier assumption about `--current`). The default strip share becomes 0.2; usable rows depend on the terminal height and borders.

### 25.1 Views and navigation

The board (24.3) is the root view. Views stack: board → seat page → message body; board → fleet page; board → owner page; board → contact page. `Esc`, Backspace and `q` go back one view; on the board `q` still shows `Ctrl-C stops the watcher`. Up and Down move the selection on the board and scroll a page; Left and Right on a seat page step to the previous or next seat's page in file order, wrapping; `g` on the board or a seat page focuses the selected seat's pane (what Enter did in 24.4); `z` anywhere zooms the strip (`pane zoom --current --toggle`) and back; `l` and `?` as in 24.4. Number keys keep opening seats, now as pages. The footer of every view ends with its key hints (`Enter open · g go · a ack · d deliver · z zoom · l log · ? help` on the board; `Esc back · ↑↓ scroll · ←→ seat · g go · a ack · d deliver · p screen · z zoom` on a seat page; `Esc back · ↑↓ scroll` on a body), cut from the right when the width is short. Mouse: a click on a board row opens that page, a click on the header opens the fleet page, a click on the footer does nothing; a click anywhere in a page acts as Esc.

### 25.2 The seat page

```
demo-dev · ● working 3m · claude · w7:p3 · ctx 57k ⚠                                        Esc back · ←→ other seat · g go to pane
doing     Herdr-fleet demo-dev seat setup
queue     #103004 09:55 from demo-orchestrator [on refusal]  "Review parser and CLI behavior"
          #103005 09:55 from demo-orchestrator [on refusal]  "Then report the test results"
debts     owes demo-orchestrator since 09:56 (#103006)  "Please review the example patch…"
recent    09:57 ← demo-orchestrator #103004 queued · 09:50 → demo-orchestrator #103007 sent · 09:44 kickoff
10:03 waiting on demo-dev (2 queued)                                            ↑↓ scroll · Enter open message · a ack · d deliver · p screen
```

Sections, in this order, each omitted when empty: `doing` (the title while working; while blocked, `dialog` with up to four lines of the open dialog from the 24.2 read); `queue` (every queued message in delivery order: id, time, sender, the 20.9 reason tag, first line); `debts` (what the seat owes and what it is owed, oldest first, each with the other party, `since`, id and first line); `recent` (the seat's last twenty log entries, newest first: time, `←` received or `→` sent, the other party, id, delivery; kickoffs, acks and interrupts as their kind); `screen` (only after `p`: the seat's last twelve screen lines through `agent read`, re-read on each `p`, dropped when the page closes). The selection moves over queue, debts and recent entries. Enter on a queued or recent message opens its body (25.4); `a` on a debt acks it as 24.4 does (confirmation in the footer, `--reason "from the board"`, by `owner`); `d` delivers the seat's queue when the seat is idle or done, through the watcher's path, else the footer says why not (`working: lands at its next idle`, `blocked: a dialog is open`, `text in its input`). Nothing on a page restarts, interrupts or messages a seat.

### 25.3 The fleet, owner and contact pages

- **Fleet page** (Enter on the board's header, or a click on it): the fleet file path and workspace; watcher pid, uptime, lock; every wait, hold (with reason) and retry in progress; the `status` block's lines that are not seat rows (drift, worktrees, hooks, the `doctor` warnings the last refresh saw); then the log tail (`.herdr/watch.log`, newest last, scrollable).
- **Owner page** (`o` on the board, or Enter on the header's owner cell): every message to the owner, newest first, delivered or not, with time, sender, id and first line; Enter opens the body; `a` acks the selected one. The header's cell becomes `owner: #103003 12:13 from partner-workspace "Example ready…"` (the newest message, delivered or not) or `owner: nothing`.
- **Contact page** (Enter on a contact row, shown after the seats when rows allow, else reachable with Left/Right from the last seat): state and age, pane, and the messages exchanged with this fleet from the log, newest first, with bodies on Enter; no actions.

### 25.4 Message bodies

Enter on any message opens its stored body (from `cur/` or `new/`, through the inbox module), with the header line `#103004 · 09:55 · demo-orchestrator → demo-dev · queued (on refusal) · 2.1 KB`, scrollable with Up and Down and PageUp/PageDown, cut to the strip's width with `…` per line, never wrapped. A message whose body is log-only (20.9) shows its first line and `(no stored body)`. Reading a body here is a listing in the sense of invariant 15.

### 25.5 Code and tests

`board.py` gains the view stack (`View` objects: `Board`, `SeatPage`, `FleetPage`, `OwnerPage`, `ContactPage`, `Body`, each with `render(width, height, colours)` and `key(k)`), the page models (built from the 24.2 model plus the log entries and bodies the page needs, read on open and on each refresh while open), and the hints; `tui.py` gains mouse events (`Screen.getch` returns clicks as `("click", row, col)`), `zoom()` through `herdr.pane_zoom_toggle(pane)` on `HERDR_PANE_ID` (else the state's watch pane; `z` is ignored when neither is known) and the fake's scripted clicks. `fleetfile.WATCH_RATIO` becomes 0.2. Invariant 22 as amended. Tests (`tests/test_section25.py`): navigation (Enter, Esc, Backspace, `q`, Left/Right wrapping, number keys, `g`, `z` with and without a pane id, clicks on row, header, footer and inside a page); the seat page's sections present and absent, scrolling, `p` reading the screen only then, `a` and `d` with every refusal reason; the fleet, owner and contact pages; bodies (stored, log-only, long, PageUp/PageDown); hints cut at every breakpoint; the harness: `agent read` from `watch` allowed for a blocked seat's refresh and after a scripted `p`, `pane zoom` only after a scripted `z`, `agent focus` only after a scripted `g` or number key, everything else as before. README's board section and docs/testing.md's live check (open a page, read a body, zoom, Esc back) updated; `init`'s starter comment for `[watch] ratio` says 0.2.

### 25.6 Amendments from the build

The same build makes 25.1 to 25.5 in the 0.13.0 working tree, with the two live-demo fixes (the guard's `❯ Try "…"` placeholder, the 0.2 ratio). `board.py` gains `Ref`, `PageRow`, `Footer`, the views and `hit_test`; `tui.py` gains the new key names, clicks and `zoom`; `seats.py` gains `dialog_lines`; `herdr.Herdr` gains `pane_zoom_toggle`. Deviations and decisions the section left open:

- **Names and shape.** The root view is `BoardPage`, since `Board` stays the name of 24.5's model. A view's `key` (and `click`) changes only the view and gives back an action tuple (`open`, `replace`, `back`, `body`, `go`, `ack`, `deliver`, `deliver_all`, `screen`, `notice`) that `watch.BoardView.act` carries out with the existing functions; a page reads what it shows in `load`, on open and at each refresh while it is the top view (a page revealed by going back is read again at once). `z`, `l` and `?` are taken by the driver in every view; in the log view any key or click returns, and while a question is in the footer any key or click but `y` answers no. `zoom` is a function of `tui` (`tui.zoom(herdr, env, watch_pane)`), not a `Screen` method.
- **Hints.** 25.2's example splits the seat page's hints between its header and footer; built as 25.1 says, in the footer only, at its right end. They take what the event leaves, but never less than a third of the line while they need it, and are cut to whole hints from the first; a notice (a question, the help line) takes the whole footer. 25.1 gives no hints for the fleet, owner and contact pages; built: `Esc back · ↑↓ scroll · z zoom`, `Esc back · ↑↓ select · Enter open · a ack · z zoom` and `Esc back · ↑↓ select · Enter open · ←→ seat · z zoom`. The `?` line now lists the 25.1 keys.
- **The board.** Up from the first row selects the header, so Enter can open the fleet page; a click on the header's `owner:` cell opens the owner page, anywhere else on the header the fleet page. `g` and `a` on a contact row or the header say `select a seat …`. The owner cell of a message sent before ids reads `owner: 12:13 from X "…"`.
- **The seat page.** `recent` has one entry per line (the example joins them), each with its first line: a `send` with the delivery its entry recorded, a `delivered`, `expired` or `dropped` entry with the original sender and `via`, kickoffs, acks (with `by` and the reason) and interrupts; every entry can be selected, and Enter on one that is not a message says so. `debts` lists every request still unsettled (`messages.unsettled`), not only the latest per pair as `owed` does for the board's cell, so each can be acked. The selection skips `doing`, `dialog` and `screen` lines; Down past the last item scrolls on. `p` runs `ops.op_peek` (`agent list`, `agent read NAME --lines 12`); its lines stay on the page until it closes or Left/Right moves to another seat, and are not read again on a refresh. `d`'s reasons are 25.2's plus `owner typing` and `a dialog or a menu` (the guard's other wordings, shortened), `absent: its next kickoff carries the queue`, `unknown: left queued`, `nothing queued for X` and the pointer hold of 18.12.
- **The fleet page.** It cannot show drift, worktrees, hooks or `doctor`'s checks: they need `pane process-info`, git, `integration status` and doctor's other herdr calls, which invariant 22 as amended (24.2 and 25.2's reads) does not allow the board. It shows the `status` lines it can say from its own reads (the `awaiting:` lines with their hints and a `queued:` line), the fleet file's warnings and the latest warnings the watcher caught (the "doctor warnings the last refresh saw"), and the line `drift, worktrees, hooks and doctor's checks: herdr-fleet status, herdr-fleet doctor`. The lock is shown as `lock .herdr/watch.lock since HH:MM`. Scrolled to its end, the page follows the log.
- **Owner and contact pages.** The owner page lists the owner's mailbox files (`new/` and `cur/`) and the log's `send` entries to `owner` that have no file, newest first, each with its fate from the log and `owed` when the owner owes a reply; `a` acks only a message the owner owes, and says why not otherwise (an FYI, answered, not delivered, or no id). The contact page lists the log's `send` entries from and to the contact, newest first, with their last fate; Left and Right step through the seats and then the contacts, wrapping.
- **Bodies.** The state in the header line is `queued (on refusal)`, `queued` or `queued (fyi)` for a message still in `new/`, else its last fate in the log (`delivered`, `delivered?`, `expired`, `dropped`, `sent`); the size is the body's. A log-only message's header has no size.
- **Mouse and Esc.** `CursesScreen` asks for `BUTTON1_PRESSED | BUTTON1_CLICKED` with `mouseinterval(0)`, so a press is the click, and sets the escape delay to 25 ms (`set_escdelay`, or `ESCDELAY` before `initscr` on Python 3.8), so Esc goes back at once rather than after ncurses' default second. Run by hand in a pseudo-terminal against the fake herdr under both interpreters: an X10-encoded click (what ncurses asks for with `xterm-256color`) opened the clicked seat's page; SGR-encoded clicks are not parsed by the system ncurses, so a terminal that sends them anyway would type their digits. Whether herdr passes clicks to the strip was not verified here.
- **Zoom.** `HERDR_PANE_ID` set: `pane zoom --current --toggle` (the form verified live); else the state file's watch pane, `pane zoom PANE --toggle`; else nothing. A failure is a red line.
- **Keys moved.** As 25.1 says, Enter and the number keys open pages and `g` focuses; `tests/test_section24.py` follows (and the harness's `agent focus` key is now `g` or a number, as 25.5 says, no longer Enter).
- **The ratio and the starter.** `WATCH_RATIO = 0.2`; `tests/test_watchpane.py`'s geometry follows (a resize of 0.3, `20% of the area`, the strip 10 rows of 50, tile's `--ratio 0.8`). The `init` starter had no `[watch]` lines at all; it gains a commented `watch = false` and a commented `[watch]` table with `ratio = 0.2` and `notify_blocked_after`.
- **The guard's placeholder.** The designer's `seats.PROMPT_PLACEHOLDER_PATTERNS` and `presence.is_placeholder` are kept as they were; the fake herdr's own glyph judgement (`owner_typing`, `input_held`) gains the same `Try "…"` pattern for `claude`.
- **Tests.** `tests/test_section25.py` has 30 tests; with the 706 of 0.12.0, section 24's 57 and the designer's one in `tests/test_section21.py`, the suite has 794.

### 25.7 Amendments from the live demo (designer, 24 Oct 2026)

- **`pane zoom --current` is the UI-focused pane, not necessarily the caller.** Historical smoke checks showed it could zoom another pane despite the caller having `HERDR_PANE_ID`. `tui.zoom` therefore names its own pane explicitly, with a recorded-watch-pane fallback, and does nothing if neither is known. The harness refuses `--current`. Explicit watcher-pane zoom and restoration were verified in a scratch terminal.
- **Historical key-routing check.** Keys pressed while the watcher had focus reached its handlers and explicit navigation worked. Raw local log lines and pane identifiers are omitted.
- **The first capture after a move was stale.** Right after `tile` moved the strip from 81 to 162 columns the board drew once at the old width without its header; the next refresh drew it right. A `pane resize` nudge was not needed afterwards; the resize event arrives, only later than the first refresh.

### 25.8 Amendments from the polish

Four items from driving the 25 build in the demo strip, made in the same working tree; the zoom code of 25.7 is the designer's and was not touched.

- **The first seat starts selected.** `BoardPage.ensure` puts the selection on the first row the board shows (the most urgent seat when rows overflow), at the first draw and before any key, so Down then Enter opens the second seat and Up from the first row selects the header. With no seat on the board there is no selection. `tests/test_section24.py` and `tests/test_section25.py` count their Downs from there.
- **Pages open with their first message selected.** The seat, owner and contact pages (`View.preselect`) select their first entry on opening, and again when a reload no longer lists the selected one; the fleet page and bodies have no entries to select. On a seat page the first entry is the first queued message, else the first debt, else the newest recent entry.
- **The opening line.** In board mode the footer shows `watching <basename of the fleet's repository directory> (pid N)` (`Watcher.say`'s new `shown`); `.herdr/watch.log` keeps the whole line with the fleet file's path, the debounce and `Ctrl-C stops`. "The fleet directory" is read as the repository the fleet belongs to (`fleet.repo`, the directory that holds `.herdr/`), whose name is the one the owner knows; the fleet file's own directory would always be `.herdr`. Log mode prints the whole line as before.
- **Escape sequences.** `CursesScreen.getch` no longer maps a raw Esc byte to Esc at once: it reads what follows (`ESC_FOLLOW_MS`, 50 ms a byte, at most `SEQUENCE_MAX` bytes). Nothing more is a lone Esc; `ESC [ < b ; x ; y M` is a click when `b` names the left button without the motion or wheel bits (modifier bits are allowed), and the same with `m` (a release) or any other button is nothing; a raw X10 report `ESC [ M b x y` is read the same way, in case curses passes it on; every other sequence (a CSI up to its final byte, SS3 and its byte, Esc with any other byte, i.e. Alt with a key) is dropped whole. Clicks curses decodes itself still come through `KEY_MOUSE`. Run by hand in a pseudo-terminal against the fake herdr under both interpreters: an SGR click now opens the clicked seat's page (before, its digits were read as number keys), as an X10 click does.
- **Tests.** `tests/test_section25.py` gains seven (37 in all); the suite has 801.

## 26. The orchestrator across the top (version 0.14.0)

Grid layout gains an optional full-width band for a designated seat above the remaining grid and watcher strip. The band makes an orchestrator prominent without changing its lifecycle, briefing order or authority.

### 26.1 `[layout] top`

```toml
[layout]
mode      = "grid"
top       = "sample-orchestrator"   # this seat gets a band across the whole width, above the grid
top_ratio = 0.3                     # the band's share of the tab's height, default 0.3, clamped 0.1-0.6
columns   = 3                       # optional, as before: the grid of the other seats
```

- `top` names a seat of the fleet (unknown name: fleet-file error) and needs `mode = "grid"` (chain mode: error `layout.top needs mode = "grid"`). `top_ratio` follows the `ratio` grammar. Both keys are optional; without `top`, nothing changes.
- The shape, top to bottom: the `top` seat's pane across the full width with `top_ratio` of the tab's height; the grid of every other seat (14.2 unchanged: columns, column-major, equal cells) in the middle; the watcher strip (19.1) across the bottom with `[watch] ratio`. Heights are shares of the whole tab: with `top_ratio = 0.3` and `[watch] ratio = 0.2` the grid gets 0.5.
- The `top` seat is never counted in the grid's columns or rows (`describe_shape` says `top: sample-orchestrator, then 3 seats in 2 columns (2+1)`).

### 26.2 How it is built

herdr splits only panes, so the band is made the way the strip is (19.4): the `top` seat's pane is the anchor of the whole tab, the strip is split from it `down` with ratio `1 - watch_ratio`, and then the grid's first seat is split from it `down` with ratio `top_ratio / (1 - watch_ratio)` so the band keeps `top_ratio` of the tab; the other grid seats then tile against that first grid seat exactly as 14.2 does, with that pane as the grid area.

- **`up` on a workspace it creates:** the root pane goes to the `top` seat (whatever its position in the file; it is started first), the strip is opened from the root before any other seat (19.1), then the grid's first seat is split from the root `down` with the ratio above, and the rest of the grid from it. With `--me NAME` where NAME is the `top` seat, the caller's pane is the band: the strip is split from it, then the grid's first seat, as above; 14.4's side share is not used when `top` is set. With `--me NAME` for another seat, that seat keeps 14.4's behaviour and `top` is reported as not applied (`layout: top needs the fleet's own workspace or --me <top seat>`).
- **`up` on an existing layout:** nothing moves (19.3 stands); the report ends `layout: run herdr-fleet tile for the top band` when the `top` seat is live but its pane is not a full-width band above the others (its rect's width is less than the tab's, or any seat's rect is above it).
- **`tile`:** the `top` seat anchors the layout instead of the first seat. After parking every other seat (15.1): the strip comes back under the anchor with `--ratio 1 - watch_ratio` (as today), then the grid's first seat under the anchor with `--ratio top_ratio / (1 - watch_ratio)`, then the rest of the grid from that seat in grid-plan order. An adopted or `--me` `top` seat is moved like any other live seat (it is already the one kept in place). When the `top` seat is not live, `tile` tiles the others as today and reports `layout: top seat sample-orchestrator is not live; tiled without the band`.
- The band's pane is a seat's pane like any other: named by 22.1, read by the guard, counted in `status`; `restart` of the `top` seat reuses its pane as today.

### 26.3 The board

Nothing changes in the strip. The seat page's Left/Right order and the board's row order stay file order.

### 26.4 Tests

`layout.py`: `band_plan(top, names, columns, top_ratio, watch_ratio)` (pure) returning the splits with the two ratios above, covered for 2 to 6 seats, every `columns`, both ratios at their bounds and the default; fleet-file parsing of `top` and `top_ratio` (unknown seat, chain mode, out-of-range ratio clamped with a warning, `top` with one seat in the fleet gives a band and an empty grid). `up` fresh with and without `--me` on the top seat (the strip from the root first, the band's ratio, the grid in the remainder, geometry checked with `fake_herdr.geometry` at 200x50), `up` existing (nothing moves, the `run tile` line present and absent), `tile` (moves in order: strip, then the grid's first seat, then the rest; the not-live case; `--dry-run`), `status`/`doctor` unchanged. README's layout section and the `init` starter's `[layout]` comment gain `top` and `top_ratio`; `examples/team/fleet.toml` sets `top = "sample-orchestrator"`.

### 26.5 Amendments from the build

One build makes 26.1 to 26.4; the version is 0.14.0. `layout.py` gains `band_plan`, `band_ratio`, `clamp_ratio`, `strip_keep`, `band_warning`, `is_top_band` and `describe_shape(..., top=)`; `placement.py` gains `band_anchor` (whether a run builds the band, and the line when it does not), `place_band` and `band_check` (the `run herdr-fleet tile` line); `tile.py` anchors on the top seat; `fleetfile.Fleet` gains `top` and `top_ratio`. Deviations and decisions the section left open:

- **Cases 26.2 does not name, and their lines.** All are `layout:` report lines that leave the exit code alone. A fresh layout in a workspace `up` did not create, without `--me` (beside the caller, or split off the first pane of a labelled workspace the caller is not in), is treated like `--me` for another seat: placed as 14.2 says, with `layout: top needs the fleet's own workspace or --me sample-orchestrator` (the seat's name stands for `<top seat>`). `--me` for another seat in a workspace that `up` creates in the same run does get the band: that workspace is the fleet's own, its root pane goes to the top seat, and the caller stays where 15.2 leaves it. A workspace created in the run whose top seat is not placed in it (not named on the command line, live elsewhere, a conflict), and `--me <top seat>` that could not adopt the caller's pane (14.4), say `layout: top seat sample-orchestrator is not placed in this run; placed without the band`. `--me <top seat>` from outside the fleet's workspace without `--join` says `layout: top seat sample-orchestrator is not in the fleet's workspace; placed without the band`. `--me <top seat> --join` into a workspace created in the same run cannot build the band: the caller's pane joins beside the root pane, which is reserved for another seat and is never closed, and herdr splits and moves panes only `right` or `down`, so nothing can go above it. The grid fills the root pane as before, and the report ends with the tile line; `tile` then makes the band.
- **"Full width" is the seats' width, not the tab's.** The tile line is printed when the top seat's rect does not span the left and right edges of every other seat's rect in its tab (within one cell, since pane rects are drawn inside borders), or when a seat's rect starts above the top seat's bottom edge. 26.2 compares with the tab's width, but a pane that is not a seat (the caller's shell beside a fleet without `workspace`, the first pane of a labelled workspace) shares the tab and is never moved by `tile`, so that rule would print the line after every `up` with nothing `tile` could do about it; and pane rects inside borders are never as wide as the tab's area. The check reads `agent list` and `pane layout` (both read-only), only when `top` is set, and also after an `up` that built the band (silent then) or had nothing to place; it is skipped after a line saying the band was not applied, and on a layout herdr cannot describe.
- **No strip, no division.** Without a strip in the run (`--no-watch`, `watch = false`, a watcher already running, a strip that failed to open; for `tile`, no strip in the tab), the grid's first seat keeps `top_ratio` itself, so the band is still `top_ratio` of the area. `Strip.open_at` now answers whether it opened (or, on a dry run, planned) the strip. A strip whose resize failed keeps herdr's 50% and the band is then off by that; `tile` corrects both.
- **The clamp.** `top_ratio / (1 - [watch] ratio)` above 0.9 (0.6 with a 0.4 strip asks for 1.0) is clamped to 0.9 with `herdr-fleet: layout.top_ratio: the grid's first seat under the band would keep 1.0 of the band's pane, outside herdr's 0.1-0.9; using 0.9` on stderr, when `up` or `tile` plans the split rather than when the file is read, since the strip may be off for the run. The lower bound cannot be reached (0.1 / 0.9 is 0.1111).
- **Fleet file wording.** An unknown seat: `layout.top: 'sample-boss' is not a seat in this file (seats: …)`. Chain mode, including the default: `layout.top needs mode = "grid"`. `top_ratio` outside 0.1-0.6: `layout.top_ratio: 0.7 is outside 0.1-0.6; using 0.6`, as for `[watch] ratio`. `top_ratio` without `top` warns `layout.top_ratio: ignored without layout.top`.
- **`tile` with the top seat in another workspace.** The seat is reported `elsewhere` as before, and the band line reads `layout: top seat sample-orchestrator is in workspace w3; tiled without the band`; 26.2 names only the not-live case. Both lines come right after the `layout: grid - …` line. The top seat alone tiles to the band and the strip: `top: sample-orchestrator, then no other seats`.
- **The strip on an existing layout.** The designated top seat is excluded when choosing the last grid seat under which to open a later strip, so the strip does not split the band from the grid.
- **Order.** On a band `up` from a root pane the top seat's `agent start` comes first; kickoffs stay in file order (section 3). The tile line is the report's last line; `up` prints its status table after it, as always.
- **`band_plan`.** `names` may include the top seat (it is left out of the grid); `watch_ratio` None means no strip; the strip's step is named `layout.STRIP` (`(watcher strip)`, never a seat name), and `tile` and `up` carry it out with 19.4's own moves (`tile_move`, `plugin pane open` and `pane resize`) rather than a split.
- **Tests.** `tests/test_section26.py` (42 tests). The example's dry run in `tests/test_cli_subprocess.py` now expects the band; the other 800 tests of 0.13.0 pass unchanged. The suite has 843.
- **Historical live verification.** A disposable four-seat workspace confirmed the top band, grid and watcher proportions; a second `up` left the layout unchanged, `tile` preserved the geometry, and explicit cleanup removed the scratch resources. Example identifiers are anonymized, not a new observed deployment.

### 26.6 Contact markers and configuration reload (0.14.1)

Contact rows need a distinct marker and style so they are not confused with managed seats. The board also reloads valid fleet-file edits; a parse failure keeps the previous valid configuration.

- **Contact rows are marked.** A contact row's name cell starts with `↔ ` and the whole row is drawn dim (`contact` style), on the board and in the packed form; its page header reads `↔ docs-channel · contact of this fleet · ○ idle 1m · w4:pG`. Number keys and Enter keep working on them.
- **The watcher re-reads the fleet file.** The board's refresh compares the fleet file's mtime with the one it loaded; when it changed, the watcher reloads the fleet (`load_fleet`) before building the model, so added or removed contacts, `label`s, `top` and `restart_hint_tokens` show within a refresh instead of after a strip restart. A file that no longer parses keeps the last good fleet and puts the error in the footer (red) and the log until it parses again. Mailbox waiters are unaffected: a seat removed from the file keeps its waiter until it ends, and gets no new one.
- Tests: the marker and style on the board, packed rows and the page header; a fleet-file edit between refreshes adding a contact, removing one, changing a label; a broken edit keeping the last good fleet with the footer line; `--log` mode untouched.

### 26.7 Amendments from the build

One build makes 26.6; the version is 0.14.1. `board.py` gains `CONTACT` and the `contact` style; `context.Ctx` gains `reload_fleet` (`load_fleet` on the path in use, its warnings said, the old fleet kept on a `FleetError`); `watch.BoardView` gains `reread_fleet`, run first in each refresh, and `fleet_error`. Decisions the section left open:

- **The style.** `contact` is a style word of its own that `tui.CursesScreen` draws as `A_DIM`; on a contact row it replaces every span's style, the state colour included (the selection still adds `reverse`). With colours off (`NO_COLOR`, or a terminal without them) it is dropped like `dim` (24.6), and the `↔ ` marker alone tells a contact row. The marker takes two of the name cell's twenty columns.
- **The page header.** `↔ NAME · contact of this fleet · STATE AGE · PANE`, with `no pane` for a contact that resolves to no agent (0.13.0's header ended with `· contact`).
- **When the file is read again.** The mtime (ns) is compared at the start of every board refresh (every 5 s and after each event); a file whose mtime cannot be read is left alone. A broken file's mtime is remembered too, so it is parsed again only after the next edit. Lines: `fleet file PATH changed; reloaded`, `fleet file PATH parses again; reloaded`, and once per distinct error `fleet file PATH does not parse; still using the last good one: <errors>` (red), whose footer form leaves out the path so the error itself fits; the footer keeps it, in place of the last event and after the loop's own trouble, until an edit parses. Warnings of a reloaded file are said like other warnings (once until they change). `[watch] notify_blocked_after` follows the reload too; `[watch] ratio`, `top` and `top_ratio` matter only to `up` and `tile`.
- **`top` on the board.** 26.3 keeps the board's rows in file order whatever `top` says, so nothing in the strip showed it; the fleet page gains a line `layout      grid · top sample-orchestrator` (the mode alone without `top`), which is where a reloaded `top` shows.
- **The selection.** A selected seat that is no longer on the board (removed from the file, or no longer among the rows shown) gives the selection back to the first row.
- **Waiters.** Nothing in the waiters changed: each pass already reads `ctx.fleet` for its candidates, so a seat removed from the file gets no new waiter, and one already waiting runs on (its delivery uses the mailbox name it started with; its state is no longer recorded, the seat being gone from `seen`'s list). Covered with the waits run inline (a removed seat's waits stop at the reload while a remaining seat's go on); a thread still blocked in `agent wait` across a reload was not tested on real threads.
- **`--log` mode** never re-reads the file, as before; README says to restart that watcher after an edit.
- **Tests.** `tests/test_section26_6.py` (8 tests); four expectations of 0.13.0's tests changed for the marker and the contact page header (`test_section24.py`: the 162x8 contact row, the packed contact, the built contact row; `test_section25.py`: the contact page title twice), and the fleet page test reads the new `layout` line. The suite has 851.

## 27. Setup instructions and external contact capacity (24 Oct 2026)

This section corrects public setup instructions and explicit contact visibility. A board must support a combined seat/contact roster without silently hiding contacts; the capacity fixtures use synthetic agents. No automatic discovery, external-fleet editing or agent control is added.

### 27.1 Setup and documentation

- README installation must work from a GitHub install without assuming a source checkout. Resolve the enabled `yarrasys.fleet` plugin's `manifest_path` from `herdr plugin list --json`, derive its root, run its `bin/herdr-fleet install-cli`, and explain `~/.local/bin` on PATH. Keep a separate local-development path. Check syntax against the installed binary and the official plugin documentation.
- The quick start explicitly enters the target repository root before `init` and editing `.herdr/fleet.toml`. Explain installed agent executables, authentication, integrations, inside-Herdr requirements, dry runs, startup dialogs and adopting an existing orchestrator. Do not require optional Claude hooks for a working fleet.
- Audit the entire README against the CLI, implementation and latest design amendments, including commands, options, examples, contacts, board keys, reloads, delivery modes, costs, safety and limitations. Correct stale claims such as three example seats, no curses UI, and `pane zoom --current`; the board names its own pane explicitly (25.7). Distinguish local polling from agent turns, explicit delivery overrides from normal FYI delivery, and successful kickoffs from unconfirmed prompts.
- The generated starter places the top-level `watch = false` example before any table so enabling the layout example cannot silently turn it into `layout.watch`. Comments explain mutually exclusive alternatives. Correct stale setup comments in the development fleet, agent guidance and shipped examples without changing the first three AGENTS.md rules.
- `docs/testing.md` must distinguish current instructions from historical release coverage. Provide portable unit-test instructions alongside checks for both interpreter paths. Replace stale or unsafe copyable live procedures with a current scratch-fleet procedure, explicit targets, state checks and manual owner interaction for typing and dialogs. Retain historical evidence in this design document; do not claim an unrun live check passed.

### 27.2 Contact visibility

- Contacts are explicit `[[contacts]]` declarations with exact-name and optional cwd/kind matching. Local development configuration is outside the public tree. Editing that roster never grants control over external agents.
- Board capacity and packing count seats and contacts together. When all rows fit, show seats followed by contacts in their existing order. When they fit packed, pack the combined ordered list; every contact retains its marker, style, click target and page. When neither fits, prefer seats using the existing seat urgency order, use remaining capacity for contacts in file order, and count every hidden row in `+N more`. A contact remains reachable through Left/Right from a seat page even when its row is hidden. Very short boards keep their existing most-urgent-seat behavior.
- Board-mode fleet-file reload applies contact edits within a refresh. Verify matching with read-only data; adding a contact requires no focus, zoom, keys or prompt. Running Python code changes still require a new watcher process.

### 27.3 Verification

Add a regression test that enables the generated layout example together with top-level `watch = false` and loads the resulting fleet without warnings, with the watcher disabled. Add meaningful board tests for three seats plus five contacts at normal, packed and narrow sizes, including overflow counts and opening a packed contact page. Run both interpreter suites and compileall. Review the resulting diff against this section. Smoke-test generated setup in a temporary directory and the board on a cheap Claude scratch seat before committing; record the actual results in an amendment. No push is authorized.

### 27.4 Amendments and verification

- **Implementation.** The shared `board.plan` now counts seats and contacts together; rendering, selection and hit testing continue to use that one plan. Combined file order is preserved when every row fits; overflow keeps the existing seat urgency order before contacts. Below three rows the most-urgent-seat view is unchanged. `scaffold.STARTER_FLEET` places the top-level watcher boolean before all tables. The only other runtime-source edit is documentation: `send --when-ready` help now says an unready target stays queued, with `--now` refusing, and the send description and `--fyi` help explain explicit delivery overrides instead of promising FYIs never wake a seat. Delivery behavior, dependencies, key paths and version strings are unchanged.
- **Documentation.** README installation resolves the exact enabled plugin id and guards launcher installation when lookup fails. It separates agent installation/login, Herdr integrations and optional inbox hooks; explains PATH, repository root, adoption, dry runs, watcher updates, contacts and troubleshooting; and corrects stale safety, board, cost and delivery claims. The testing guide replaces historical copyable procedures with current fake-test and disposable-fleet instructions, and points here for historical evidence. AGENTS.md's first three rules are byte-for-byte unchanged; its existing Ctrl-U and own-pane zoom descriptions now match 23.11 and 25.7.
- **Regression evidence (builder).** Ten new tests in `tests/test_section27.py`; eight failed against the old implementation and two controls passed. Failures covered the starter's ignored `layout.watch`, contacts not triggering packing, missing hidden-row counts, packed click mapping and contact-page navigation. All ten pass with the change; two section-24 expectations now describe packing instead of silently hidden contacts. The full suite grows from 851 to 861 tests.
- **Historical automated checks.** Both Python 3.9 and Python 3.12 full suites passed 861 tests, and compilation passed. An initial concurrent run failed the existing `RealThreads.test_waiters_in_threads_deliver_and_stop_cleanly` on a missing second-delivery output line. All 23 watcher tests then passed in isolation and a serial full rerun passed; the watcher implementation and thread test were not changed. This intermittent result remains a verification limit.
- **Historical setup verification.** The enabled-plugin root lookup and temporary launcher were exercised under bash and zsh against an existing installation. Create/keep initializer behavior and the layout/disabled-watcher example were checked under both test interpreters. Shell, TOML/JSON and local-link checks passed. Plugin installation/update/link and integration installation were not repeated.
- **Historical live verification.** A disposable Claude seat was started, briefed and cleaned up. A read-only board model resolved configured external contacts; rendering covered full, packed and narrow layouts with hidden-row counts. The checks made no external-agent prompt, key, focus or zoom request. Deployment names, paths, pane ids, model choice and roster are omitted; the names in examples are synthetic.
- **Historical running-watcher check.** Contact-file edits were observed through an existing watcher reload. That did not install the new packing code into the running process. Live mouse clicks, hooks, input holds, dialogs and watcher process replacement were not retested in this change.
- **Historical independent review.** Review found no actionable issue, and independently passed both 861-test suites, compilation, Python 3.8 grammar parsing and documentation checks. Substituting the previous plan/starter reproduced eight failures and two passing controls. The earlier threaded-test output race did not reproduce in the review suites. This records completed checks, not authorization to release, install or push.

## 28. Public documentation and repository privacy

The owner requested validation that the documentation is suitable for a public audience and exposes no internal organization information. The publication boundary is the entire tracked source tree, including historical design notes, examples, fixtures and development configuration. A clean current tree does not remove information from older Git commits.

### 28.1 Publication boundary

- Keep public package identity: the repository owner/source, `yarrasys.fleet`, manifest action ids, licence copyright and third-party attribution. These identify the published software; they are not deployment details.
- Replace actual product/fleet names, developer usernames and absolute home/work paths, branch/deployment references, copied message bodies, internal commit/message ids and operational rollout notes with explicitly synthetic examples or product-level explanations. Preserve documented behavior, safety invariants, numbered sections and technical rationale. Historical records must identify anonymized examples as such rather than claiming synthetic details were observed live.
- Rename the shipped example to `examples/team` and document `init --example team`. It is a generic orchestrator/developer/reviewer/designer team with no organization-specific project assumptions, deployment authority, unavailable role document or hard-coded model requirement. Adjust CLI help and fixtures consistently; no legacy internal-name alias is published.
- Keep the actual development fleet, contact roster, kickoff prompts and state note on disk for the existing local workflow, but remove root `.herdr/` files from Git tracking and ignore `/.herdr/` at the repository root. Shipped example `.herdr/` trees remain tracked. Do not edit, restart or prompt any live fleet for this cleanup.
- Source comments and tests are public too: use synthetic fixture names and paths there. Preserve real public upstream links and public plugin identifiers; do not blindly delete copyright, attribution or third-party credits.

### 28.2 Verification and history

Inventory all tracked files and scan their contents and names for known internal identifiers, user home/work paths, copied operational text and credential patterns. Inspect findings in context and maintain a narrow allowlist for public package identity and licence attribution. Check the generic example initializes and loads, its kickoff references exist, all documentation snippets and local links remain valid, and both required interpreter suites plus compileall pass. An independent reviewer checks the full publication boundary.

Report historical exposure separately using read-only Git inspection. The owner chose removal from the current files with existing history unchanged. Do not rewrite, remove or force-push history, and do not prepare a replacement history. Record actual validation and limitations here before committing. No push is authorized.

### 28.3 Verification and limits

- **Publication boundary.** The candidate contains 94 files. Seven checkout-local fleet/configuration files were removed from Git tracking and protected by the root-only `/.herdr/` ignore rule. Their local contents were verified unchanged; the generic example's `.herdr/` files remain publishable. Runtime contact configuration and running fleets were not changed by this cleanup.
- **Public content.** All candidate filenames and contents were scanned for known deployment identities, personal/organization work paths, copied operational phrases, internal release references and credential signatures. No matches remained after cleanup. Additional literal-authentication and private-network-reference checks also returned no findings. These are bounded scans plus content review, not a guarantee about unknown identifiers or older commits. Private scan patterns and local backup locations are not part of the public repository.
- **Examples and historical notes.** `init --example team` supplies four generic roles with empty native arguments, local kickoff templates and explicit owner authorization for publishing actions. The example initialized seven files and kept all seven on a repeat under both test interpreters; the fleet loaded without warnings. Historical design/requirements/proposal notes now distinguish technical history from synthetic example data and omit deployment anecdotes, rosters and copied operational plans.
- **Documentation checks.** Across the complete publication candidate, 18 shell blocks passed both bash and zsh syntax checks, 11 TOML and six JSON blocks parsed, and all 13 local Markdown link/anchor references resolved. The package/repository identity, copyright and third-party attribution were preserved intentionally.
- **History and live limits.** Existing commits still retain previously recorded material. History was not rewritten, and no publication, release or push was performed. No new live-agent smoke test was needed for this content cleanup; initialization and fake-agent tests cover the renamed example, while existing live fleets remain untouched.

- **Automated regression checks.** Both required interpreter suites pass 862 tests and compilation passes. The added team-example test initializes and renders all four roles, requires no fixed model arguments or external role document, and fails against the previous example directory in an isolated baseline copy. All 40 runtime modules retain the same executable syntax trees after excluding docstrings; only the shipped example selection/data changes. The first three AGENTS.md rules and version files are unchanged.
- **Independent review.** The reviewer returned MERGE with no actionable findings after checking the complete publication boundary and final wording corrections. Independent checks covered the privacy scans, Python 3.8 grammar, all example-role rendering and publishing constraints, documentation syntax/links, the preserved safety rules and runtime logic, and retained local configuration. Full-suite and compilation results above are the builder's evidence; the reviewer performed no live-agent operations and did not repeat those mutating checks. Historical exposure remains a separate, accepted limit.
