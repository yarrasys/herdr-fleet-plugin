# Testing herdr-fleet

Two layers: unit tests against a fake herdr (no live server, safe to run anywhere), and a live smoke test the designer runs by hand inside herdr.

## Unit tests

Run from the repository root, under both supported interpreters on this Mac (the system Python 3.9 has no `tomllib`, so it exercises the vendored `tomli`; Homebrew 3.12 uses `tomllib`):

```bash
cd /Users/nabsha/work/yarrasys/projects/herdr-fleet-plugin
/usr/bin/python3 -m unittest discover -s tests -v
/opt/homebrew/bin/python3.12 -m unittest discover -s tests -v
/usr/bin/python3 -m compileall -q herdr_fleet bin/herdr-fleet
```

The suite takes a few seconds. Nothing in it talks to a real herdr: `HERDR_BIN_PATH` always points at the fake.

### The fake herdr

`tests/fake_herdr.py` is a stand-in `herdr` binary. It keeps a simulated server in a JSON world file (workspaces, panes with their foreground processes, the agent in each pane) and appends every call to a JSON-lines log. It supports:

- canned responses keyed by an argv prefix (`world["canned"]`), for errors and odd output;
- stateful simulation of `workspace`, `pane` and `agent` commands with the JSON shapes of herdr 0.9.4;
- behaviours for the hard cases: `agent_pane_busy` for a fresh split, `agent_not_ready`, start errors, a quit that takes N polls of `agent get` before the agent is gone, an agent that never quits, processes that linger after exit, stalled prompts, and `agent wait` outcomes.

Most tests call `herdr_fleet.cli.main` in-process with the fake as the runner and an instant clock, so 250 ms polls and 10 s or 20 s timeouts cost nothing. `tests/test_cli_subprocess.py` runs `bin/herdr-fleet` as a real process with the fake installed as an executable on `HERDR_BIN_PATH`, exactly as herdr would. `tests/test_manifest.py` checks `herdr-plugin.toml` and runs its commands the way herdr does: actions from the plugin root with an action's context JSON, pane entrypoints through `sh -c` from the repo directory that `ui` passes as `--cwd`.

Like live herdr 0.9.4, the fake's `pane get` and `pane list` records never carry the agent `name`; only `agent get` and `agent list` do (design section 12). Like live herdr 0.9.5, agent records can carry `agent_session` (`{"agent": "claude", "kind": "id", "source": "herdr:claude", "value": "<uuid>"}`); the fake adds it only when a scenario sets one with `put_agent(..., session=fake_herdr.claude_session(uuid))`, and otherwise leaves the key out.

### Safety invariants

`tests/support.py` checks every test's call log in `tearDown` against design section 7, so every scenario is also a safety test:

- the log never contains bare `herdr`, `agent send-keys`, `pane send-keys`, `pane send-text` or `pane run`;
- every `agent prompt` went to an agent that was `idle` or `done` at that moment (never `blocked`, `unknown` or `working`);
- every `agent start` targeted a pane at a bare shell prompt and never the caller's pane;
- every `pane close` closed a pane herdr-fleet created and that hosted no agent; every `workspace close` closed a workspace herdr-fleet created.

Tests that need git use `GitCase` from `tests/support.py`: a real temporary repository (`git init`, `git commit`, `git worktree add`) with HOME set to the test's temp dir and no system git config. The plugin's git calls go through a logging shim on `PATH`, and `tearDown` asserts invariant 8 on them: only `rev-parse`, `symbolic-ref`, `worktree list`, `log`, `status`, `merge-base`, `ls-files` and `cat-file`, never `fetch` or `prune`. These tests are skipped when git is missing.

Specific tests cover restart refusing blocked, unknown and working agents, stalled prompts never being resent, quit polling and timeout, swap drift from process-info argv, and `down` closing only created panes.

`tests/test_init.py` covers `init` (design section 13):

- the starter in a plain directory (root is the directory) and in a Git work tree invoked from a subdirectory (root is the toplevel, found with a real `git init` in the test; skipped when git is missing);
- the written `.herdr/fleet.toml` loads through `load_fleet` with no errors or warnings, shows every optional key in comments, and its seat name is a valid herdr name for odd directory names (spaces, uppercase, leading digits, accents, non-Latin, very long);
- existing files are `kept`, `--force` reports `overwritten`, a failed write exits 1;
- `--example fablyn` copies every file of `examples/fablyn` byte for byte; an unknown example exits 2 and lists the available ones;
- the manifest action writes into the root of `focused_pane_cwd` from the context JSON, not the plugin root, and exits 2 without it;
- `init` sends no herdr command at all, except the one notification with `--notify` inside herdr;
- `--version` prints the package version, and `up`, `status`, `restart`, `swap`, `kickoff`, `down`, `ui status` and `doctor` suggest `herdr-fleet init` when no fleet file is found;
- `init`, then `doctor` and `up` against the fake herdr: the seat starts beside the caller and its kickoff carries no template comment.

`tests/test_manifest.py` also checks the `init` action entry verbatim and runs it from the plugin root.

Grid layout (design section 14):

- `tests/test_layout.py` tests the pure plan in `herdr_fleet/layout.py`: rows per column for 1 to 10 seats and with `columns` set, column-major order, the exact split sequence and ratios, and, for 1 to 6 seats (and larger grids), equal cells, proved by simulating herdr's split rule (the split pane keeps `r`, the new pane gets `1 - r`) independently of the code under test.
- `tests/test_grid.py` runs `up` against the fake herdr and rebuilds every pane's rectangle from the splits herdr received. It covers:
  - a grid in a created workspace (the root pane is the first cell; 4 seats → 2×2, 5 → 2+2+1);
  - a grid beside the caller with `--me` (the caller keeps `ratio`, the other seats share the rest evenly) and without it;
  - a grid off the first pane of an existing labelled workspace;
  - a partial run appending with the chain rule, and a restart of an absent seat in grid mode;
  - a failed column head skipping the cells below it;
  - `--dry-run` printing every split with its ratio;
  - warnings for per-seat `split`/`ratio`/`from` in grid mode and `columns` in chain mode, and errors for a bad `mode` or `columns`;
  - `from` validation (unknown, later or own seat) and behaviour (splits from the named seat; falls back to the previous seat when the named seat has no pane);
  - the `--me` hint when the caller's pane has no agent (exit 3, other seats still start).
- `tests/test_cli_subprocess.py` checks that the Fablyn example's dry run shows its 2×2 grid. The chain tests in `test_up.py` are unchanged.

Adopted seats, `--join` and `tile` (design section 15). The fake herdr supports both `pane move` forms with the live 0.9.4 behaviour: `result.move_result.pane` and `previous_pane_id`, ids kept within a workspace and changed across workspaces, and a move to the pane's own tab is a no-op. It also supports `tab list`.

- `tests/test_adopted.py`:
  - the `adopted` flag is set by `--me` and by adopt-on-resume, and cleared when the plugin starts the seat;
  - `status` shows `adopted` and no args drift for such a seat;
  - a bare `swap` skips adopted seats, while `swap NAME` swaps them and named `restart`/`swap` still refuse a blocked one;
  - `--me` outside the fleet's workspace warns with the exact `pane move` and moves nothing;
  - `--join` makes exactly one move, records the new pane id, and the next seat splits off it, both into an existing workspace and into one created in the same run;
  - `--join` needs `--me`, `--dry-run` only plans the move, a failed move leaves the pane and the other seats still start, and nothing moves when the caller is already in the workspace.
- `tests/test_adopted.py` also covers the amendment to section 15:
  - a seat with no recorded `args` counts as adopted (bare `swap` skips it; `status` shows `adopted`, no args drift), and seats herdr-fleet started do not;
  - refreshing a live seat's pane keeps `adopted`, `args` and timestamps (and drops `created_pane` when the old pane still exists);
  - a session moved by hand to another workspace is followed in the state file with every field kept, and is skipped by a bare `swap` when adopted;
  - preflight resolves the caller with `pane current --current` (a stale `HERDR_PANE_ID` after a move still identifies the caller, also after `--join`) and falls back to `HERDR_PANE_ID` when the query fails.
- `tests/test_tile.py`:
  - a 4-seat grid makes exactly three out-and-back pairs: all parks first, then the returns with the plan's sources, directions and ratios. The resulting rectangles are equal, and names and pane ids survive;
  - chain mode follows each seat's `split`, `ratio` and `from`;
  - `--dry-run` prints the moves and makes none;
  - seats elsewhere or absent are left alone, and seats spanning two tabs are refused;
  - the first failed move stops the run and names the seats not re-tiled;
  - the menu offers `tile`;
  - no test sees a prompt, start, split, close or rename from `tile`.
- `tests/test_manifest.py` checks the `tile` action entry verbatim.

Messages (design section 16). The safety hook in `tests/support.py` stays strict for every command except one. `run_cli` writes a `{"command": argv}` marker into the call log, and a prompt to a `working` agent is accepted only when the marker says `send`. `blocked` and `unknown` are refused for every command, `send` included. A test proves the hook still fails for a prompt to a working agent from any other command.

- `tests/test_messages.py`:
  - `send` to one seat: the exact prompt text with the `[from <sender>]` prefix, the sender found through `pane current` and `agent get`, the one-line output and the log entry;
  - a working target gets the text queued; blocked, unknown and absent targets are refused, nothing is sent and nothing logged; a mix of sent and refused exits 3;
  - `--as`, the `owner` fallback (unnamed agent or no pane), the existing `[from` prefix kept, and `--file` (the log's `file` field, empty and missing files);
  - `--all` skips the sender; live agent names work and unknown names are a usage error before anything is sent;
  - `--when-ready` waits then sends, or refuses a target still busy; a stalled or timed-out prompt is reported, never resent, and logged with `"delivery": "unconfirmed"` (other herdr errors are not logged); `--dry-run`;
  - `log` marks unconfirmed entries with `?` after the bytes, owed replies count them, and entries written before 0.4.1 (no `delivery`) read as sent;
  - argument placement (0.4.2): options before, between and after the items (`send A --dry-run TEXT`), `send --all TEXT`, `--file` with several targets around options, the two-item minimum, `--` before a text starting with `-`, unknown options still rejected, and the same for `restart A --dry-run B` and `state set KEY --fleet X TEXT`;
  - `--fleet PATH` on every fleet command from a directory with no fleet file, `--file` still an alias where it meant the fleet file, `send --fleet --file` and `state set --fleet --file`, `HERDR_FLEET_FILE`, and `init` refusing `--fleet`;
  - the `log` command (filters, `--json`, corrupt lines skipped, no herdr calls) and kickoffs logged as `kind = "kickoff"`;
  - owed replies as a pure function (latest message per pair, replies, kickoffs, equal times ordered by the log) and in `status`/`status --json`/`log --owed`;
  - the `pane A → B` lines for a seat found in another pane, a restart that follows a moved seat, and `--join`.
- `tests/test_contacts.py`:
  - `[[contacts]]` validation (name pattern, seat collisions, duplicates, missing `match`, a `match.name` that is a seat);
  - resolution by `cwd`/`foreground_cwd`, `kind` and `name` (zero, one, several);
  - `send` to a contact's pane id, with refusals listing the candidates, and `--all` never reaching contacts;
  - the status block and `{contacts}` in kickoffs (empty without contacts);
  - a lifecycle sweep (`up`, `status`, `kickoff`, `restart`, `swap`, `tile`, `down --close-panes --close-workspace`, `up`) with contact agents in the fleet's own tab, asserting that no prompt, rename, start, wait, split, close or move ever names a contact's pane.
- `tests/test_statenote.py`: `state set` replaces or appends a section with a stamped heading, keeps text outside sections and `###` subsections, `--file`, `keys`, `show`, atomic writes, usage errors, and no herdr calls.
- `tests/test_permissions.py`:
  - the coverage rule (`:*`, ` *`, word prefixes, bare `Bash`, exact entries not counting);
  - the doctor lines with no settings (exact suggested entries), with entries spread over the three files (HOME is the test's temp dir, never the real `~/.claude`), and with only the missing ones named;
  - unreadable settings;
  - the status `CWD` (with `~`) and `BRANCH` columns, using a real `git init` (skipped without git).

Version 0.5.0 (design section 17):

- `tests/test_messages.py`:
  - FYI by `--fyi` and by prefix (`FYI:`, `fyi `, after an existing `[from …]`), the text sent as written, `"fyi": true` in the log and no key otherwise, `[fyi]` in `log`;
  - the owed rule as a pure function: an FYI never owes, an FYI reply settles, a later FYI from the asker keeps the request open, `ack` settles and never owes, same-second order;
  - `ack` with names and `--as` (the exact entry, no herdr call at all, nothing sent), `owner` and options anywhere, `--all` resolving the caller from its pane (only `agent list`, `--version`, `pane current`, `agent get`), `--all` with names, `nothing owed` (no entry written), unknown names and usage errors (nothing written), `status` after an ack;
  - the roster sentence about `--fyi` in every kickoff `up` sends.
- `tests/test_worktrees.py` (real repositories): the table and `--json` with a merged branch, an unmerged dirty one with an old commit, a detached worktree, a deleted (missing, prunable) one and a locked one; rows in git's order; the main worktree marked `main`; seat globs (also through the unresolved temp path); `--seat`; no herdr call; the index unchanged and nothing pruned; no `origin` at all; the `origin/main` and `origin/master` fallbacks and an `origin/HEAD` target; not a repository; the porcelain parser; the status hint line.
- `tests/test_status_doctor.py`:
  - `fleet file vs git`: identical to `HEAD` and `origin/main`, no origin, untracked with a differing `origin/main` copy (emberline's case), modified since `HEAD`, no copy on `origin/main`, not a repository, git missing, and no new herdr command;
  - CWD and BRANCH folding: a shared cwd and branch fold into the header (real repository), a blank branch folds away, a different cwd keeps the column, absent seats do not count, one live seat keeps both columns.
- `tests/test_context.py` (fake transcripts under the test's HOME): the last `assistant` line with usage wins over later user and other lines and a trailing partial line; the parsed record has exactly `type`, `timestamp` and the four usage integers, with no text, tool call or other field; synthetic all-zero lines are skipped; the chunked backward read gives the same answer for any chunk size and honours the 4 MiB limit (scaled down); missing, empty and session-less cases; `status --context` and `--json --context` with no transcript text, path or session id in the output and nothing written under `~/.claude`; no extra herdr command; nothing read without `--context`; `restart_hint_tokens` (at or above, joined to notes, JSON) and its validation; `HERDR_FLEET_CLAUDE_PROJECTS`.
- `tests/test_fleetfile.py`: `worktrees` (list of non-empty strings, `~` expanded, a relative glob warned about) and `restart_hint_tokens` (positive integer).

### Running the CLI against the fake by hand

```bash
cd /Users/nabsha/work/yarrasys/projects/herdr-fleet-plugin
D=$(mktemp -d)
printf '#!/bin/sh\nexec /usr/bin/python3 "%s/tests/fake_herdr.py" "$@"\n' "$PWD" > "$D/herdr"; chmod +x "$D/herdr"
/usr/bin/python3 - "$D" <<'EOF'
import json, sys
sys.path.insert(0, "tests")
import fake_herdr
w = fake_herdr.new_world()
fake_herdr.add_workspace(w, "w1", "caller", "/tmp")
fake_herdr.put_agent(w, "w1:p1", "claude", name="caller", status="working")
json.dump(w, open(sys.argv[1] + "/world.json", "w"))
EOF
export HERDR_BIN_PATH="$D/herdr" FAKE_HERDR_WORLD="$D/world.json" FAKE_HERDR_LOG="$D/calls.jsonl"
export HERDR_ENV=1 HERDR_PANE_ID=w1:p1 HERDR_WORKSPACE_ID=w1
(cd examples/fablyn && /usr/bin/python3 ../../bin/herdr-fleet doctor && /usr/bin/python3 ../../bin/herdr-fleet up --dry-run)
cat "$D/calls.jsonl"
```

Do this in a shell where you will not run real herdr commands afterwards, or `unset HERDR_BIN_PATH FAKE_HERDR_WORLD FAKE_HERDR_LOG` when done.

## Live smoke test

Run by the designer on 19 Oct 2026 against herdr 0.9.4, from the designer's own pane, and passed. This is the procedure as run. It uses one Claude seat on the cheap Haiku model, a fleet file in a scratch directory passed with `--file` (since 0.4.1 an alias of `--fleet`, which is the documented spelling), and the seat's `cwd` set to a directory Claude Code already trusts, so the seat starts without a trust prompt. The fleet has no `workspace` key, so the seat splits from the pane the commands run in; herdr-fleet never starts into, restarts or quits that pane.

If the seat does stop at a trust or permission prompt, `up` reports it as blocked, notifies with the `request` sound and sends it nothing. Only the owner answers that prompt (AGENTS.md); then run `kickoff fleet-smoke`.

### Commands

```bash
cd /Users/nabsha/work/yarrasys/projects/herdr-fleet-plugin
test "$HERDR_ENV" = 1 && echo "inside herdr"
S=$(mktemp -d /tmp/fleet-smoke.XXXXXX)
mkdir -p "$S/.herdr"
F="$S/.herdr/fleet.toml"
cat > "$F" <<EOF
cwd = "$PWD"   # trusted by Claude Code already, so no trust prompt

[[agents]]
name = "fleet-smoke"
kind = "claude"
args = ["--model", "claude-haiku-5-5"]
kickoff_text = "[from herdr-fleet] Smoke test of herdr-fleet ({reason}). You are {name}. Reply with the single word READY, then wait. Do not run any tools. {note}"
EOF

python3 bin/herdr-fleet doctor --file "$F"
python3 bin/herdr-fleet up --file "$F"                       # 1
python3 bin/herdr-fleet restart fleet-smoke --file "$F" --note "restart check"   # 2
sed -i '' 's/claude-haiku-5-5/claude-sonnet-5-5/' "$F"        # 3: edit args
python3 bin/herdr-fleet swap --dry-run --file "$F"           # 4
python3 bin/herdr-fleet swap --file "$F"                     # 5
python3 bin/herdr-fleet status --json --file "$F"            # 6

herdr plugin link "$PWD"                                      # 7: needed for plugin pane open
herdr plugin pane open --plugin yarrasys.fleet --entrypoint status --placement split --cwd "$S" --no-focus
# note result.plugin_pane.pane.pane_id from the JSON it prints, look at the pane, then:
herdr plugin pane close <pane_id>

python3 bin/herdr-fleet down --close-panes --file "$F"       # 8
cat "$S/.herdr/fleet.state.json"
herdr plugin unlink yarrasys.fleet                            # if it was linked only for this test
rm -rf "$S"
```

Between steps, wait until `status` shows the seat `idle` or `done`: `restart` and `swap` refuse a `working` seat (exit 3) unless given `--wait MS`.

### Results

| Step | Expected and observed |
| --- | --- |
| doctor | every check `ok`, exit 0 |
| 1. `up` | `fleet-smoke: started - in wN:pM`, `fleet-smoke: briefed - kickoff sent (up)`, then the status table; exit 0; 4.4 s. The split appears next to the caller's pane; the seat replies READY. `$S/.herdr/fleet.state.json` and `$S/.herdr/.gitignore` are created. |
| 2. `restart --note` | `quit - quit with /exit`, `started - in wN:pM` (same pane), `briefed - kickoff sent (restart)`; exit 0; 5.8 s. `/exit` through `agent prompt` quits Claude Code cleanly and the shell is back within a second. The kickoff ends with "restart check". |
| 3-4. `swap --dry-run` | `fleet-smoke: would restart - args: running --model claude-haiku-5-5, file says --model claude-sonnet-5-5`; nothing changes |
| 5. `swap` | quit, start in the same pane with `--model claude-sonnet-5-5`, kickoff `(swap)`; exit 0; 5.7 s |
| 6. `status --json` | the seat `idle` (or `done`), `live_kind` `claude`, `drift` empty |
| 7. status pane | `plugin pane open` returns the new pane at `result.plugin_pane.pane.pane_id`; `--cwd` sets the pane process's cwd, so the pane finds `$S/.herdr/fleet.toml`, shows the table and waits for Enter. `plugin pane close <pane_id>` closes it. |
| 8. `down --close-panes` | `quit - quit with /exit`, `pane closed - wN:pM`; exit 0. The split disappears; the caller's pane is untouched. The state file remains with `"agents": {}` (the closed seat's entry is dropped) and the workspace fields. |

Both model ids used here, `claude-haiku-5-5` and `claude-sonnet-5-5`, started fine. `claude-opus-5-5` in the Fablyn example is the owner's choice for that team.

### What the smoke test found (fixed since)

- The status and manage panes died instantly: a pane opened with `--cwd` runs in that directory, so the manifest's relative `bin/herdr-fleet` did not resolve. The `[[panes]]` commands now run `sh -c 'exec python3 "$HERDR_PLUGIN_ROOT/bin/herdr-fleet" ...'`; `tests/test_manifest.py` runs them from the repo directory as herdr does.
- `pane get` and `pane list` records carry no agent `name`; only `agent get` and `agent list` do. The first build read names from pane records, so `up` would have treated a named agent of the right kind in a recorded pane as a resumed unnamed one and renamed it. Names now come from `agent get` (`Herdr.pane_agent`), and the fake herdr mirrors the live shapes.

### Not covered by the smoke test

The manifest actions through `herdr plugin action invoke` (`up`, `doctor`, `status`, `manage`) and the popup menu, a fleet with a `workspace` label (creating a workspace), several seats, adopt-on-resume after a herdr server restart, and a seat blocked at startup. The unit tests cover these against the fake herdr.

## Live smoke test for 0.5.0 (to run by hand)

Not run yet. For the designer or the owner, inside herdr, from this repository. Steps 1 to 5 only read: they run against emberline's real fleet file but prompt nobody, fetch nothing and write nothing (`status` may prune stale ids from emberline's state file, as every command does). Steps 6 to 10 use the one-seat Haiku fleet of the smoke test above (`$S`, `$F`, after its `up`).

```bash
cd /Users/nabsha/work/yarrasys/projects/herdr-fleet-plugin
E=/Users/nabsha/work/yarrasys/projects/emberline/.herdr/fleet.toml

time python3 bin/herdr-fleet worktrees --fleet "$E"                        # 1
python3 bin/herdr-fleet worktrees --fleet "$E" --json | head -30           # 2
python3 bin/herdr-fleet doctor --fleet "$E" | grep 'fleet file vs git'     # 3
time python3 bin/herdr-fleet status --context --fleet "$E"                 # 4
python3 bin/herdr-fleet status --json --context --fleet "$E" | grep -A4 '"context"'   # 5

python3 bin/herdr-fleet send --fyi --as owner fleet-smoke "smoke check, no reply needed" --fleet "$F"   # 6
python3 bin/herdr-fleet send --as owner fleet-smoke "Reply with the single word PONG" --fleet "$F"      # 7
python3 bin/herdr-fleet log --owed --fleet "$F"                            # 8
python3 bin/herdr-fleet ack --as fleet-smoke owner --fleet "$F"            # 9
python3 bin/herdr-fleet log --fleet "$F"; python3 bin/herdr-fleet log --owed --fleet "$F"; tail -3 "$S/.herdr/messages.jsonl"   # 10
```

| Step | Expected |
| --- | --- |
| 1. `worktrees` | a `repo … default origin/main` line and one row per worktree (14 in emberline), in `git worktree list` order; the shared checkout marked `main`; `/private/tmp/fablyn-*` rows with branch, age, `clean` or `N changed`, MERGED `yes`/`no`; no herdr command (nothing in `herdr plugin log list`); about a second |
| 2. `--json` | `repo`, `default_branch` and the per-worktree keys of design 17.2 |
| 3. drift | `warn  fleet file vs git: untracked in this checkout (branch …); origin/main copy differs: +7/-0 lines, first at line N` (the `[[contacts]]` entry) |
| 4. `status --context` | a CONTEXT column with `183k (2 min)`-style cells for the Claude seats and `-` for others; `consider restart` only if emberline sets `restart_hint_tokens`; no transcript text, path or session id anywhere; not noticeably slower than `status` |
| 5. JSON | `"context": {"tokens": …, "at": "…Z", "source": "claude-transcript"}` or `null`, and `"restart_hint"` per seat |
| 6. `send --fyi` | `sent to fleet-smoke (idle), …`; the seat receives `[from owner] smoke check, no reply needed` exactly as written |
| 7. request | sent; the seat answers PONG in its own pane (not through the log) |
| 8. `log --owed` | one line, `awaiting: fleet-smoke owes owner since … (Reply with the single word PONG)`; nothing for the FYI |
| 9. `ack` | `acked owner since … (Reply with the single word PONG)`; no prompt reaches the seat |
| 10. log | the FYI line shows `[fyi]`, the ack line ends in `ack`; `log --owed` says `nothing awaiting a reply`; the last entries carry `"fyi": true` and `"kind": "ack", "delivery": "logged"` |

Then run `down --close-panes --fleet "$F"` and remove `$S` as in the first smoke test.
