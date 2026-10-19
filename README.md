# herdr-fleet

A [herdr](https://herdr.dev) plugin that starts and manages a team of named coding agents from a fleet file committed with your repository (`.herdr/fleet.toml`).

You declare the seats: a herdr agent name, a kind (`claude`, `codex`, …), start arguments, a working directory and a kickoff prompt for each. Then:

- `up` creates or reuses a workspace, splits panes, starts every seat with `herdr agent start`, and briefs each one;
- `restart` quits an idle seat, starts it again in the same pane and re-briefs it with a "where things stand" note;
- `swap` applies a change of kind or model in the file by restarting only the seats that drifted;
- `status`, `kickoff` and `down` do what they say.

Any agent can read the fleet file and run `herdr-fleet`, so a Claude or Codex orchestrator can manage its own team. The design is in [docs/design.md](docs/design.md).

Requirements: herdr 0.9.4 or newer, `python3` 3.8 or newer, Linux or macOS. No build step and no third-party packages (TOML parsing uses `tomllib`, or a vendored `tomli` on Python < 3.11).

## Install

From GitHub (the repository is tagged `herdr-plugin` for the marketplace):

```bash
herdr plugin install yarrasys/herdr-fleet-plugin
```

Or link a local checkout while developing:

```bash
herdr plugin link /path/to/herdr-fleet
herdr plugin action list --plugin yarrasys.fleet
```

Agents run the CLI by name once it is on `PATH`:

```bash
python3 /path/to/herdr-fleet/bin/herdr-fleet install-cli      # symlinks into ~/.local/bin
herdr-fleet doctor
```

`herdr plugin list --json` shows where herdr installed the plugin.

## Quick start

Installing the plugin does not give a repository a fleet; `init` does. From anywhere inside the repository:

```bash
herdr-fleet init              # writes the starter files at the repository root
$EDITOR .herdr/fleet.toml     # name the seats, pick kinds and models
herdr-fleet doctor            # check the file, herdr and the kinds
herdr-fleet up                # start every seat beside your pane and brief it
```

Or, inside herdr, run the **Fleet: init** action (`herdr plugin action invoke yarrasys.fleet.init`) with a pane in the repository focused, then **Fleet: up**. The action reports what it wrote in a notification.

`init` writes at the root of the Git work tree that contains the directory (else the directory itself), and prints the root it chose:

| File | Contents |
| --- | --- |
| `.herdr/fleet.toml` | one seat, `<repository name>-dev`, kind `claude`, no args, no `workspace` (so seats open beside your pane), with every optional key shown in comments |
| `.herdr/kickoff/default.md` | the built-in kickoff template, as the default for every seat |
| `.herdr/STATE.md` | the "where things stand" note that fills `{state}` |
| `.herdr/.gitignore` | ignores `fleet.state.json` |

It never overwrites a file: each one is reported as `created` or `kept`, and only `--force` overwrites (`overwritten`). It runs no herdr command and works outside herdr. `herdr-fleet init --example fablyn` copies the three-seat example in [examples/fablyn](examples/fablyn) as shipped instead. Commit the `.herdr` files (the state file stays ignored).

## Actions and panes

| Manifest entry | What it does |
| --- | --- |
| action `yarrasys.fleet.init` | `herdr-fleet init --notify` in the focused pane's repository |
| action `yarrasys.fleet.up` | `herdr-fleet up --notify` from the focused pane's directory |
| action `yarrasys.fleet.status` | `herdr-fleet ui status`: opens the `status` pane (an overlay running `status --hold`) |
| action `yarrasys.fleet.manage` | `herdr-fleet ui manage`: opens the `manage` pane (a popup running the menu: pick a seat, then restart, swap, kickoff, down or up) |
| action `yarrasys.fleet.doctor` | `herdr-fleet doctor --notify` |

Actions run with the plugin root as their working directory, so they find the fleet file from the focused pane instead: `focused_pane_cwd` in `HERDR_PLUGIN_CONTEXT_JSON`, else the `cwd` from `herdr pane get $HERDR_PANE_ID`. On herdr 0.9.4 an action's context JSON has these keys (values abridged):

```json
{"workspace_id": "w1F", "workspace_label": "...", "workspace_cwd": "...",
 "tab_id": "w1F:t1", "tab_label": "...",
 "focused_pane_id": "w1F:p1", "focused_pane_cwd": "/path/to/repo",
 "focused_pane_agent": "...", "focused_pane_status": "...",
 "invocation_source": "...", "correlation_id": "..."}
```

herdr-fleet uses `focused_pane_id` as the caller's pane (never started into) when `HERDR_PANE_ID` is unset, `workspace_id` when `HERDR_WORKSPACE_ID` is unset, and `focused_pane_cwd` for discovery. Other spellings (`pane_id`, or a `focused_pane` object with `pane_id` and `cwd`) are accepted too.

`ui status` and `ui manage` open the pane with `herdr plugin pane open --plugin yarrasys.fleet --entrypoint <id> --cwd <repo root>`, and the pane process runs in that directory, not in the plugin root. That is why the two `[[panes]]` commands are `["sh", "-c", "exec python3 \"$HERDR_PLUGIN_ROOT/bin/herdr-fleet\" status --hold"]` and the `menu` equivalent: a relative `bin/herdr-fleet` does not resolve there, and manifest commands are argv with no shell expansion unless they start one.

Plugin output is shown nowhere on screen. herdr keeps each action's stdout, stderr and exit code in `herdr plugin log list --plugin yarrasys.fleet` (pane entrypoints are not logged; they show their output in the pane). So actions also report through `herdr notification show`: `up --notify` and `doctor --notify` send their summary, and a `ui` action that cannot find the fleet file or fails its preconditions sends the error with the `request` sound and exits 2. Notifications are only sent from inside herdr.

Bind an action to a key in herdr's config:

```toml
[[keys.command]]
key = "prefix+F"
type = "plugin_action"
command = "yarrasys.fleet.manage"
description = "fleet menu"
```

## The fleet file

`<repo>/.herdr/fleet.toml`. Discovery walks up from the current directory to the first directory containing `.herdr/fleet.toml`; that directory is the repo root. `--file PATH` or `HERDR_FLEET_FILE` overrides discovery (the repo root is then the parent of the `.herdr` directory holding the file, or the file's own directory). A full example lives in [examples/fablyn](examples/fablyn).

```toml
workspace  = "fablyn"                  # optional: reuse the workspace with this label, else create it.
                                       # Absent: use the workspace the command runs in.
cwd        = "."                       # default agent cwd, relative to the repo root
state_note = ".herdr/STATE.md"           # optional: its contents fill {state} in kickoffs
kickoff    = ".herdr/kickoff/default.md" # optional default kickoff template
quit_timeout_ms = 20000                # optional: how long to wait for an agent to exit (ms)

[layout]
mode      = "chain"                    # chain (default) | grid; see Layout below
# columns = 2                          # grid only: number of columns (default ceil(sqrt(seats placed)))
direction = "right"                    # chain: split direction between seats. grid: first split off your pane
ratio     = 0.5                        # chain: split ratio. grid: share your pane keeps. 0 < ratio < 1

[[agents]]
name    = "fablyn-orchestrator"        # herdr agent name: [a-z][a-z0-9_-]{0,31}, unique
kind    = "claude"                     # a kind from `herdr agent start --help`
args    = ["--model", "claude-opus-5-5"]          # passed after `--` to herdr agent start
cwd     = "."                          # relative to the repo root, absolute, or ~/…
split   = "right"                      # chain only: overrides [layout].direction for this seat
ratio   = 0.5                          # chain only: overrides [layout].ratio
# from  = "<an earlier seat>"          # chain only: split from that seat's pane, not the previous one
env     = { FABLYN_ROLE = "orchestrator" }        # set on the seat's pane
kickoff = ".herdr/kickoff/orchestrator.md"          # or kickoff_text = "inline template"
quit    = "/exit"                      # overrides the per-kind quit command
notes   = "Needs localhost for Playwright."       # shown by status only
```

Seats start in file order. The first seat takes the root pane of a workspace that `up` creates (created with that seat's `cwd` and `env`). When the fleet has no `workspace`, new panes split off your pane: herdr-fleet never starts an agent in the pane you run it from. Where the other seats go depends on the layout mode.

Validation reports every problem at once and exits 2: names match herdr's pattern and are unique; `args` is a list of strings; kickoff files exist; `split` is `right` or `down`; `ratio` is strictly between 0 and 1; only one of `kickoff` and `kickoff_text`; `mode` is `chain` or `grid`; `columns` is a positive integer; `from` names a seat earlier in the file. Kinds must be ones the installed herdr supports: `doctor` warns, and commands that start agents refuse. Unknown keys are warned about and ignored.

### Layout

`[layout] mode` picks how `up` arranges the panes it creates.

- **chain** (the default): each seat splits from the previous seat's pane, or from its `from` seat's pane, in its own `split` direction with its own `ratio` (falling back to `[layout]`). With `ratio = 0.5` every split halves the previous seat, so a long chain gets narrow; use `split = "down"` and `from` to shape it.
- **grid**: the seats placed in one run fill an even grid, column-major in file order. There are `columns` columns of equal width (default ceil(sqrt(seats))), and each column's rows have equal height, with any extra rows in the first columns (3 seats: 2 + 1; 5 seats: 2 + 2 + 1). Per-seat `split`, `ratio` and `from` are ignored with a warning. The grid fills the root pane of a workspace `up` created in that run. Otherwise it fills a new pane split off the anchor (your pane, or the first pane of the labelled workspace) with `[layout] direction` and `ratio`, and the anchor keeps `ratio` of the space. If seats placed by an earlier run are already in the workspace, new seats are appended with the chain rule instead.

Four seats in a new workspace (`mode = "chain"`, `ratio = 0.5` on the left; `mode = "grid"` on the right), and the grid beside you with `up --me orchestrator` and `ratio = 0.4`:

```
chain                        grid                         grid with --me
+-----------+-----+--+--+    +-----------+-----------+    +---------+-------+-------+
|           |     |  |  |    |     1     |     3     |    |         |   2   |       |
|     1     |  2  |3 |4 |    |           |           |    |   you   |       |       |
|           |     |  |  |    +-----------+-----------+    |   (1)   +-------+   4   |
|           |     |  |  |    |     2     |     4     |    |         |   3   |       |
|           |     |  |  |    |           |           |    |         |       |       |
+-----------+-----+--+--+    +-----------+-----------+    +---------+-------+-------+
```

The grid is built with chained splits, since `herdr pane split SRC --ratio r` leaves `SRC` with share `r` and gives the new pane `1 - r`. First the column heads, each split `right` from the previous head with ratio `1/(columns remaining)`; then each column's rows, split `down` from the cell above with ratio `1/(rows remaining)`. `up --dry-run` prints every split with its ratio.

### Kickoff templates

Each seat gets one `herdr agent prompt <name> "<text>"` after every seat has started, in file order, so the roster is complete (put the orchestrator last to brief it last). Placeholders use Python `str.format_map` rules (`{{` and `}}` are literal braces); anything that is not one of these is left exactly as written:

| Placeholder | Value |
| --- | --- |
| `{name}` `{kind}` `{args}` | the seat |
| `{repo}` `{cwd}` `{workspace}` | repo root, the seat's cwd, the workspace label |
| `{roster}` | a Markdown table of every seat: name, kind, pane id, cwd |
| `{state}` | the contents of `state_note`, if it exists |
| `{note}` | `--note TEXT` or `--note-file PATH` |
| `{reason}` | `up`, `restart`, `swap` or `kickoff` |

The text starts with `[from herdr-fleet]` unless the template starts with `[from`. Without a template, a built-in default introduces the seat, points it at `AGENTS.md`, and lists the team. A line that is entirely an HTML comment (`<!-- ... -->`) is a note for humans and is never sent; `init` puts one at the top of its starter template to list the placeholders.

## Commands

All commands except `init` and `install-cli` accept `--file PATH`; all but `install-cli` accept `--notify` (also send the summary as a herdr notification). When no fleet file is found, the error says so and suggests `herdr-fleet init`.

| Command | What it does |
| --- | --- |
| `init [DIR] [--example NAME] [--force]` | Write the starter `.herdr` files at the root of DIR's Git work tree (else DIR; default the current directory), or copy `examples/NAME` with `--example` (`fablyn`). Never overwrites without `--force`. Runs no herdr command and does not need `HERDR_ENV`. Exit 0 when every file was created or kept, 1 when a write failed, 2 for an unknown example or a bad DIR. |
| `up [NAME...] [--me NAME] [--dry-run] [--no-kickoff] [--note TEXT]` | Start every seat that is not live, then brief the ones it started. Skips seats whose name is live; adopts an unnamed agent of the right kind that herdr resumed in a seat's recorded pane (rename, no kickoff); reuses a recorded pane that is at a shell prompt; else splits a new pane. `--me NAME` names the agent in your own pane as seat NAME (rename only, no kickoff). `--dry-run` prints the exact herdr commands and changes nothing. |
| `status [--json] [--hold]` | Every seat: expected kind, live kind, pane, state (`idle`, `working`, `blocked`, `done`, `unknown` or `absent`), drift and notes, then `herdr integration status` for the kinds in use, flagging `outdated` and `not installed`. |
| `restart NAME... [--note TEXT \| --note-file PATH] [--wait MS] [--dry-run]` | Quit each seat, start it again in the same pane with the same name, and brief it with `reason = restart`. A seat that is not running is started (in its recorded pane if that is at a shell prompt, else a fresh split). |
| `swap [NAME...] [--note …] [--wait MS] [--dry-run]` | Restart exactly the seats that drifted from the file, and start absent ones. Drift: the live kind differs, or the running process's argv does not contain the file's `args` in order, or the state file recorded a different kind or args at start. `--dry-run` lists what would change and why. |
| `kickoff NAME... [--note …]` | Resend the kickoff (`reason = kickoff`), e.g. after you answered a seat's startup prompt. |
| `down [NAME...] [--wait MS] [--close-panes] [--close-workspace]` | Quit seats. `--close-panes` then closes the panes herdr-fleet created, once they are back at a shell. `--close-workspace` closes the workspace only if herdr-fleet created it and no agent is left in it. |
| `doctor` | Read-only checks: python, herdr binary and version, `HERDR_ENV`, server reachable, fleet file, kinds, kickoff files, `state_note`, integration status, a quit command per seat, the state file. Exit 1 on any `fail`. |
| `install-cli [--dir DIR]` | Symlink `bin/herdr-fleet` into DIR (default `~/.local/bin`) and print the plugin root. |
| `ui status\|manage`, `menu` | Used by the manifest: open the overlay or popup pane; the popup's numbered menu. |

Exit codes: 0 ok, 1 failure, 2 usage error or invalid fleet file, 3 partial (some seats skipped, refused, blocked or not briefed). Every herdr failure is reported with its `error.code` and message.

## Safety rules

herdr-fleet never answers anything for you. In code and in tests:

1. Text reaches agents only through `herdr agent prompt`, and only when the agent is `idle` or `done` at that moment. herdr-fleet never runs `agent send-keys`, `pane send-keys`, `pane send-text` or `pane run`, and never runs bare `herdr`.
2. A `blocked` or `unknown` agent is never prompted, quit or restarted. A `working` agent is only quit after `--wait MS` lets it settle.
3. A prompt that returns `agent_prompt_stalled` or `timeout` is reported and never resent: herdr says the text may have been delivered.
4. Only panes recorded as created by herdr-fleet are closed, and only with `--close-panes`; only a workspace it created is closed, and only with `--close-workspace`.
5. An agent is started only in a pane whose foreground is a bare shell (`zsh`, `bash`, `fish`, `sh` or `nu`). `agent_pane_busy` is retried every 250 ms for up to 10 s.
6. Your own pane is never started into, restarted or quit; `--me` only renames.

If a seat is blocked at startup (typically a trust prompt), `up` records it, notifies you with the `request` sound and moves on. Answer the prompt yourself, then run `herdr-fleet kickoff NAME`.

### Quitting agents

herdr has no stop command, so herdr-fleet sends the kind's quit command with `herdr agent prompt`, then polls every 250 ms until `herdr agent get <pane>` fails and the pane is back at a shell, for up to `quit_timeout_ms` (default 20000). On timeout it reports and leaves the seat alone.

| kind | command |
| --- | --- |
| claude, opencode, copilot, pi | `/exit` |
| codex, gemini | `/quit` |

Other kinds need a `quit` on the seat or an entry in `$HERDR_PLUGIN_CONFIG_DIR/config.toml` (that is `~/.config/herdr/plugins/config/yarrasys.fleet/config.toml`; `herdr plugin config-dir yarrasys.fleet` prints the directory). The poll timeout can go there too; `quit_timeout_ms` at the top of the fleet file wins over it.

```toml
[quit]
amp = "/exit"
quit_timeout_ms = 30000
```

In the smoke test, `/exit` through `agent prompt` quit Claude Code cleanly and the shell was back within a second.

## The state file

`<repo>/.herdr/fleet.state.json`, created on the first `up`, records the workspace and, per seat, the pane, whether herdr-fleet created it, the kind and args it started with, and timestamps. herdr-fleet also creates `<repo>/.herdr/.gitignore` (or appends to it) so the state file is not committed. It lives in the repo rather than the plugin state directory so the manifest actions and an agent running the CLI share it.

Pane and workspace ids are valid for one herdr server. Every command checks the ids it is about to use and drops the ones herdr no longer knows. Writes are atomic.

Lifecycle:

- `up` adds or updates an entry per seat; `restart` and `swap` update `kind`, `args` and `started_at`; kickoffs set `last_kickoff_at`.
- `down` quits the seats but keeps their entries and pane ids, so the next `up` starts the seats again in the same panes.
- `down --close-panes` drops the entry of every seat whose pane it closed, since nothing is left to reuse; the next `up` splits new panes for them. Seats whose panes were not created by herdr-fleet keep their entries. The workspace fields stay.
- `down --close-workspace` clears the workspace fields once the workspace is closed.
- The file itself is never deleted. Remove it by hand to forget the layout.

Example after `down --close-panes` of a one-seat fleet:

```json
{"version": 1, "repo": "/path/to/repo", "workspace_id": "w1F", "workspace_label": "herdr-fleet-plugin",
 "created_workspace": false, "agents": {}}
```

## Limitations

- Restarts only on request: nothing watches for crashed agents (a plugin must not leave a supervisor running). Run `up` or `restart` to bring a seat back.
- Linux and macOS only; no curses UI; no per-seat worktrees (brief the orchestrator to create them).
- One tab: seats split from each other in one tab of the fleet's workspace.
- Quitting depends on each agent's own quit command; kinds without a known command must be configured.
- The shell check recognises `zsh`, `bash`, `fish`, `sh` and `nu` only.
- Drift from argv needs herdr to report the agent process's `argv` or `cmdline`; the state file catches changes it cannot see.
- If several workspaces share the fleet's label and none is recorded in the state file, `up` refuses; rename all but one.

## Development

See [docs/testing.md](docs/testing.md) for the unit tests (a fake herdr, no live server) and the live smoke test, and [AGENTS.md](AGENTS.md) for the rules agents follow in this repository.

## Licence

MIT. The vendored `tomli` (MIT) keeps its licence in `herdr_fleet/_vendor/tomli/LICENSE`.
