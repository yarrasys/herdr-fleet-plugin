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
- behaviours for the hard cases: `agent_pane_busy` for a fresh split, `agent_not_ready`, start errors, a quit that takes N polls of `agent get` before the agent is gone, an agent that never quits, processes that linger after exit, stalled prompts, and `agent wait` outcomes;
- scripted `agent wait` results for the watcher (`wait_results`: per agent, a list of `idle`, `done`, `working`, `blocked`, `timeout` or `not_found`, consumed one per wait). A state that `--until` does not name times out, as live herdr would; `timeout` without `--timeout` answers `fake_wait_forever`, because live herdr would never return. `status_after_wait` changes the agent's state right after a wait answered, as when a seat starts working during the debounce. The log records each wait's `--until` and `--timeout`. In-process calls are serialised by a lock, and the program form takes a file lock, so the watcher's waiter threads can share one world.
- the watcher strip of 0.8.0 (design 19): `plugin pane open --placement split --target-pane P --direction D` splits P 50/50 (live herdr 0.9.5 has no ratio option) into a pane marked `plugin` and `created`, in P's workspace and tab, with the `--env` values recorded; the `watch` entrypoint's pane runs `python3 …/bin/herdr-fleet watch`; the answer is the live `result.plugin_pane` (`entrypoint`, `pane`, `plugin_id`), and the pane is focused unless `--no-focus`. `pane layout` and `pane resize` answer the 0.9.5 shapes (`result.layout`: `area`, `panes` with `rect`, `splits` with `direction`, `ratio` and `rect`; `result.resize`: `changed`, `pane_id`, `focused_pane_id`, `layout`, `reason`) from `fake_herdr.geometry`, which replays the tab's recorded splits on a 200x50 cell area with herdr's rounding. `pane resize` follows herdr 0.9.5's source (`src/layout.rs`): the nearest split of the matching direction on the pane's named edge, else the opposite edge, gains the amount for `right` and `down` and loses it for `left` and `up`, clamped to 0.1-0.9; the amount is taken as `abs()` and capped at 0.5.

- focus and screens (design 21.2): panes and workspaces answer `focused` (false unless a test calls `fake_herdr.focus(world, pane)`, which makes that pane the owner's: focused in its tab, its tab the workspace's `active_tab_id`, its workspace the only focused one; `fake_herdr.unfocus(world)` unfocuses every workspace). `pane read PANE --lines N` prints the last N lines of a scripted screen as plain text, as live herdr does even in JSON mode: by default a rule, the empty prompt line of the pane's agent kind (`❯`, `› Ask Codex to do anything`, `>`) and a status line; `fake_herdr.type_in(world, pane, text)` puts the owner's text on the prompt line, `fake_herdr.show_dialog(world, pane)` shows a permission dialog with no prompt line, and `fake_herdr.set_screen` scripts any text. At every `agent prompt` the log records `owner_typing`: whether the target pane was then the owner's with text on its prompt line or no prompt line on screen, judged with the fake's own glyph table, independently of `presence.py`;

- pane names and focus (design 22): `pane rename PANE LABEL...` sets the pane's `label` (the words joined), `--clear` removes it, and `pane get`/`pane list` records carry `label` (null when unset); a plugin pane is named after its manifest title (`Fleet watch`), as live; `rename_errors` ({pane: code}) makes a rename fail. At every `pane rename` the log records the pane, the name asked for or `clear`, whether the pane then hosted a seat of the fleet (`seat`) and the name the fleet wants for it (`wanted`: the seat's `label`, else its name), judged by the fake from the fleet file (passed by `tests/support.py`, or `FAKE_HERDR_FLEET` for the program), and whether it hosted an agent or was a plugin pane. `agent focus TARGET` (a name or a pane id) moves the owner's focus there through `fake_herdr.focus` and marks the agent seen (`done` becomes `idle`);

- keys and screens (design 23.7, 23.9): `agent read TARGET --lines N` prints the last N lines of the agent's pane screen, as `pane read` does. `agent send-keys TARGET KEY...` is simulated (it is no longer in the fake's forbidden list; `pane send-keys`, `pane send-text` and `pane run` still are): the log records the keys and, before any canned answer, the agent's name, kind and state and whether its screen then showed Claude Code's exit confirmation with `Exit and stop tasks` highlighted (`exit_dialog`, judged by the fake). An `enter` on that screen ends the agent and puts the pane at a shell; a `ctrl+c` to a `working` agent sets the next state of `interrupt_results` ({name: [state, ...]}, `done` by default, `absent` ending the agent), and with `restore_prompt_on_interrupt` (true, or a list of names; design 23.11) puts the agent's last prompt back into its prompt line, as Claude Code does when interrupted while thinking; a `ctrl+u` empties the scripted prompt line (`fake_herdr.clear_prompt`). `agent read` records `input_text` (the prompt line it returned holds text) and every `agent prompt` records `input_held` (the target's prompt line held text, or none was on screen, whatever the focus; `fake_herdr.input_held`). `fake_herdr.EXIT_DIALOG` is the confirmation as a 16-line read shows it; `show_exit_dialog(world, pane)` puts it (or another screen) on a pane and makes the agent `blocked`, as herdr reports it, and `exit_dialog_on_quit(world, name[, screen])` (behaviour `dialog_on_quit`) makes the quit command show it instead of quitting. `agent get` records the agent's name and pane, so the harness knows each seat's state at its last read;

- the board (design 24): `pane get` and `pane list` records carry `terminal_title` and `terminal_title_stripped` (empty unless a test calls `fake_herdr.set_title(world, pane, title)`, as Claude Code and Codex set the title to their task), and every `agent list` records `states`, the state of each named agent it answered, so the harness knows what the board saw before it read a screen. The board's terminal is `herdr_fleet.tui.FakeScreen` (no curses): it records every draw (`screen.draws`, `screen.text(i)`), replays a script of keys (`"2"`, `"a"`, `"up"`, `"down"`, `"left"`, `"right"`, `"enter"`, `"esc"`, `"backspace"`, `"pageup"`, `"pagedown"`), clicks (`("click", row, col)`), `("resize", w, h)`, `("wait", seconds)` or a callable run between keys, advances the fake clock by each read's timeout while no key is scripted, and raises KeyboardInterrupt, as Ctrl-C would, once its idle reads are spent; `on_key=self.log_key` writes each replayed key into the call log as `{"key": …}` (a click as `click:ROW:COL`). Tests open it by patching `watch.is_terminal` (true) and `tui.open_screen` (the fake), as `tests/test_section24.py` and `tests/test_section25.py` do. `pane zoom [PANE] [--current] --toggle` flips the pane's `zoomed` flag and records the pane, whether `--current` named it and whether it is a plugin pane; a fresh Claude Code session's `❯ Try "…"` suggestion counts as an empty prompt line for `owner_typing` and `input_held`, as for the plugin's guard;

- at every `agent prompt` the log also records the target's name and, read from the fleet's `.herdr/inbox` (passed to the in-process runner by `tests/support.py`, or `FAKE_HERDR_INBOX` for the program), the mailboxes and short ids that held a message queued on refusal (`queued_by: refusal` in `new/`) at that moment (design 20.8). Nothing the fake answers changes: its response shapes stay those of herdr 0.9.5.

Most tests call `herdr_fleet.cli.main` in-process with the fake as the runner and an instant clock, so 250 ms polls and 10 s or 20 s timeouts cost nothing. `tests/test_cli_subprocess.py` runs `bin/herdr-fleet` as a real process with the fake installed as an executable on `HERDR_BIN_PATH`, exactly as herdr would. `tests/test_manifest.py` checks `herdr-plugin.toml` and runs its commands the way herdr does: actions from the plugin root with an action's context JSON, pane entrypoints through `sh -c` from the repo directory that `ui` passes as `--cwd`.

Like live herdr 0.9.4, the fake's `pane get` and `pane list` records never carry the agent `name`; only `agent get` and `agent list` do (design section 12). Like live herdr 0.9.5, agent records can carry `agent_session` (`{"agent": "claude", "kind": "id", "source": "herdr:claude", "value": "<uuid>"}`); the fake adds it only when a scenario sets one with `put_agent(..., session=fake_herdr.claude_session(uuid))`, and otherwise leaves the key out.

### Safety invariants

`tests/support.py` checks every test's call log in `tearDown` against design section 7, so every scenario is also a safety test:

- the log never contains bare `herdr`, `pane send-keys`, `pane send-text` or `pane run`;
- `agent send-keys` (invariant 21, design 23.7) only as one `enter` from `restart`, `swap` or `down`, to an agent whose screen then showed the highlighted exit confirmation, right after an `agent read` of it, at most once per command per agent; or from `interrupt`, to a seat that was `working` at its last `agent get` and whose screen the command read (`agent read`), pressing exactly the kind's keys (the fleet file's `[interrupt]` entry, else the plugin config's, else the harness's own copy of the defaults: one `ctrl+c` for claude, two for agy), never two `ctrl+c` to a Claude seat; one `ctrl+u` alone, from `interrupt` only, right after an `agent read` of that seat that showed text on its prompt line (design 23.11), at most once per command; only `enter`, `esc`, `ctrl+c` and `ctrl+u`; and never from `watch`, `menu`, `deliver`, any other command or a dry run (`support.KeysCheck`);
- `agent read` came only from `peek`, `interrupt`, the quit of `restart`, `swap`, `down` or the popup, and the board (`watch`), whose reads go only to a seat of the fleet whose state at the command's last `agent list` was `blocked`, or come right after a `p` the fake screen replayed (invariant 22), and `peek` ran nothing but `agent list`, `herdr --version`, `pane current` and `agent read`;
- every `agent prompt` went to an agent that was `idle` or `done` at that moment (never `blocked` or `unknown`; `working` only as below);
- every `agent start` targeted a pane at a bare shell prompt and never the caller's pane;
- every `pane close` closed a pane herdr-fleet created and that hosted no agent; every `workspace close` closed a workspace herdr-fleet created;
- an `inbox` command ran nothing but the caller lookup (`pane current`, `agent get`), and nothing at all with `--as` (design 18.5);
- kickoffs prompted only idle or done agents (invariant 11); a working one may be reached only by `send --now` and `deliver NAME --now` (design 21.1, `support.typed_ahead_allowed`): not by a plain `send`, the flush before it, `deliver` or `watch`, whatever the mailbox holds;
- no `agent prompt` reached a pane that was, at that call, the owner's pane with text on its prompt line or no prompt line on screen (`owner_typing` in the fake's log; invariant 19), nor, since 23.11, any pane whose prompt line held text or was not on screen, focused or not (`input_held`);
- `watch` ran nothing but `agent wait`, `agent get`, `agent list`, `agent prompt`, `notification show`, the presence guard's `pane get`, `workspace list` and `pane read`, the preflight's `herdr --version`, and the board's `agent read` (above) and `agent focus` (below), and every `agent wait` carried `--timeout` (invariants 12 and 13);
- `inbox --hook` ran nothing but `agent get` (invariant 14), and `hooks` ran no herdr command at all;
- `plugin pane open` came only from `up` (also through the popup menu) or `ui`, `pane resize` only from `up`, and a pane opened as a plugin pane was never prompted, renamed or started into (design 19.3);
- every `pane rename` came from `up`, `restart`, `swap`, `kickoff`, `tile`, `status`, `down` or the popup, never touched a plugin pane, and named a pane hosting a seat of the fleet with exactly that seat's wanted name; `--clear` came only from `down` (or the popup, whose `down` runs it) and only for a pane hosting no agent; `agent focus` came only from `go`, `menu` or the board (invariant 20, design 22), and from `watch` only right after a `g` or a number key the fake screen replayed, one focus per key (invariant 22, design 25.1); `pane zoom` came only from `watch`, right after a `z`, once per key, and only on the strip's own pane (`--current`, or the plugin pane the state file records).

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

Messages (design section 16). The safety hook in `tests/support.py` stays strict for every command except one. `run_cli` writes a `{"command": argv}` marker into the call log, and a prompt to a `working` agent is accepted only when the marker says `send` (since 0.10.0 only `send --now` or `deliver NAME --now`; see Safety invariants). `blocked` and `unknown` are refused for every command, `send` included. A test proves the hook still fails for a prompt to a working agent from any other command.

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

Version 0.6.0 (design section 18, the inbox), all in `tests/test_inbox.py` unless noted. The fake herdr is unchanged: `agent wait` with `wait_status`, `prompt_errors` and canned answers cover every case.

- Storage: the id format, the hex part drawn again when taken, the message file round trip (body verbatim, optional headers, no temp file left, `inbox/` in `.herdr/.gitignore`), short forms (unique, with and without `#`, full id, ambiguous with both ids listed, unknown, malformed), the write order of two messages in the same second, and mailbox names that cannot become paths.
- Rendering: the batch header (`at HH:MM`, another day's date, `re #id`), `--history` direction, the direct form and the existing `[from` prefix, the pointer text, `compose_prompt` (alone, batched, over 16 KiB), and the new `log` lines.
- Kickoff templates: with nothing queued the text is byte for byte the 0.5.0 text (the built-in default and a template with an `{inbox}` paragraph); with something queued the block sits at the placeholder, or is appended when the template has none; `{{inbox}}` stays literal.
- `send`: every cell of the 18.3 table (idle, done, working, blocked, unknown, absent × default, `--queue`, `--now`, `--fyi`): the output line, exit code, prompt count, log `delivery` and whether the file is in `new/` or `cur/`; `--queue --now`, bad `--ttl` and unknown `--reply-to` are usage errors that write nothing; a failed write prompts nothing (the other target is still sent); an FYI never wakes a seat that turned idle between the check and the prompt; `--dry-run` writes nothing.
- Flush before send: two queued messages and a third in one prompt in order, one `delivered` entry each before the `send` entry, all three in `cur/`; the same typed ahead to a working seat; a prompt refused at the last moment leaves everything queued; an unconfirmed batch is moved and logged `unconfirmed`.
- The 16 KiB pointer from `send` (one large message) and from `deliver` (a large queue): the files stay, nothing is logged `delivered`, and `inbox --read` prints the whole body.
- `--reply-to` stored, logged and rendered; `--ttl` absolute in the file; a message past its ttl is not counted by `status`, is expired by `deliver` (logged `expired` via `ttl`, never prompted) and by `inbox`.
- Kickoffs: the queue delivered by `kickoff` (`with 2 queued`, `delivered` via `kickoff`), appended without the placeholder, left queued when the kickoff is refused (working) or never sent (blocked at startup in `up`); `up` delivering a message sent to a seat that was absent; `up --dry-run` saying which seat carries queued messages; `kickoff --drop-inbox` and `restart --drop-inbox` (logged `dropped`, nothing carried).
- `deliver`: without names (FYI-only mailboxes skipped, working seats left queued, `--fyi`), with names (blocked left queued, FYI-only taken), unknown names, `nothing to deliver`, `--dry-run`, and an unconfirmed prompt (exit 1, never redelivered).
- `inbox`: list, `--count`, `--json`, `--read` (printed in batch format, moved, logged `delivered` via `inbox`), no herdr command with `--as`, `--history` (both directions, unlogged `cur/` files shown, expired ones and other senders left out), `--redeliver` (and its usage errors), the caller from its pane (only `pane current` and `agent get`), `owner` outside herdr, `--as owner`.
- `owner`: the notification inside herdr (title, first line, `--history` hint, `delivered` via `notify`), queued outside herdr with no herdr command at all and delivered later by `deliver`, `--queue`.
- Owed replies (pure and through the CLI): nothing owed while queued, expired or refused; counted from the `delivered` entry; an answer written before the delivery does not settle; a queued answer settles, a refused one does not; `--reply-to` and `ack #id` settle exactly one message, and an older unanswered request shows again; `ack` by id, by the bare hex part, unknown ids.
- `status`: the `queued:` line (order, `oldest` only from two messages) and `"queued"` in JSON.
- The safety hook itself fails for a `deliver` prompt to a working agent and for an `inbox` that runs anything beyond the caller lookup.
- `tests/test_status_doctor.py`: `install-cli` writes the launcher (marker, `FALLBACK`, the `HERDR_FLEET_BIN` line, mode 0755, idempotent, no temp file), replaces a symlink and an older launcher, refuses a foreign file, and `--dev` symlinks the checkout. The launcher is never executed in tests: it calls herdr.
- Existing tests updated for 0.6.0: the `[from X #id]` prefix, absent and blocked targets queued instead of refused, an FYI to an idle seat queued, `log` lines with `#id`, and the four `.gitignore` entries.

Versions 0.6.1 (design 18.6, the watcher) and 0.7.0 (18.7, hooks):

- `tests/test_watch.py` runs the watcher in-process with its waiters inline (`watch.spawn` patched) on a fake clock whose loop sleep raises `KeyboardInterrupt` after a number of passes, as Ctrl-C would:
  - one waiter per mailbox with `agent wait NAME --until idle --until done --timeout 30000`, a timeout ending the thread and the next pass starting another, no waiter for an empty mailbox, the batch after the 5 s debounce, `delivered` via `watch`, the notification, and the lines `waiting on`, `idle; delivering`, `delivered`, `stopped`;
  - the debounce re-check backing off when the seat went working (one prompt, after the second wait), and a seat that went blocked never being prompted;
  - FYI-only mailboxes getting no waiter while FYIs ride along with a real message; a fleet with only FYIs queued asking herdr nothing after the preflight;
  - an absent agent getting no waiter, one line, and one `agent list` per 30 s; a contact waited on through its pane id;
  - `owner` notified once; an unconfirmed prompt moved, logged and never repeated; a failed one tried again only after a minute; a pointer prompted once, and again only when a new message arrives; a message read meanwhile not delivered;
  - the lock: a live pid refuses (exit 1, `watcher already running (pid N)`, the lock untouched), a dead pid's lock and an unreadable one are replaced, the lock records `pid`, `started` and `fleet` and goes on exit, `release` keeps a lock that is not ours;
  - `--once`: deliver's lines, no wait, no lock, no sleep, exit 1 for an unconfirmed prompt;
  - real waiter threads delivering to two seats and leaving no thread behind;
  - `status`: no watcher line while nothing is queued, `watcher: not running` or `watcher: running (pid N since HH:MM)` before the `queued:` line, a dead pid counted as not running, and `"watcher"` in JSON;
  - the fake's scripted waits (shapes, `--until`, `--timeout`, `not_found`), and the safety hook failing for a `watch` that runs anything else, waits without a timeout or prompts a working or blocked agent.
- `tests/test_hooks.py`:
  - `hooks install` creating the settings file with exactly the two entries (mode 0644, no herdr command), keeping every other key, hook and `permissions` entry, idempotent byte for byte, and `--remove` restoring the original object; removing only our entry from a shared group; refusing invalid JSON, a non-object and a non-object `hooks` without touching the file; the warnings for a fleet without a Claude seat and for `herdr-fleet` missing from `PATH`;
  - the doctor line: `info` when not installed, `ok` when installed, `warn` when only one is there, none without a Claude seat;
  - `inbox --hook stop`: the batch as `additionalContext`, files moved and logged `delivered` via `hook`, only `agent get` on the seat's pane; silent with exit 0 and nothing on stderr for `stop_hook_active`, no pane, outside herdr, no fleet, a pane whose agent is not a seat or has no agent, a broken fleet file, an empty mailbox and an FYI-only one; unreadable input counted as no input; the fleet found from the hook's `cwd`; the pointer above 9,000 characters with the files left queued; ASCII-only output;
  - `inbox --hook prompt`: counts what waits (not what is past its ttl), moves and logs nothing;
  - usage errors for `--hook` with `--as`, another mode or an unknown event.
- `tests/test_manifest.py`: the `watch` action and pane entries, the action opening the pane with `--placement split` at the focused pane's repository, and the pane command run as a real process that writes the lock with its own pid and stops cleanly on SIGTERM and on SIGINT, removing it.

Version 0.8.0 (design 19, the watcher strip), `tests/test_watchpane.py`:

- a fresh workspace: the strip is opened from the root pane (exact `plugin pane open` argv with `--placement split`, `--direction down`, `--cwd`, `--env HERDR_FLEET_FILE=…`, `--no-focus`) before any `pane split`, then `pane layout` and `pane resize --pane <root> --direction down --amount 0.38`, and the fake's geometry shows the strip across the full width with 6 of 50 rows and every seat above it, in chain and in grid mode; `[watch] ratio = 0.25` gives `--amount 0.25`;
- beside the caller: chain mode splits the strip from the first seat's pane before the second split; grid mode from the grid area, so the strip spans the grid and not the caller's share;
- an existing layout: the strip opens under the last seat's pane after the kickoffs, sized from the layout;
- skips: `--no-watch`, `watch = false`, a live lock (`watcher: already running (pid N)`), a recorded strip still running the watcher (`starting`), an idle recorded strip (exit 3, no second strip), an error while inspecting it (exit 3, no second strip), and a fleet with no seat up;
- the state entry `"watch"` (`pane_id`, `created_pane: true`, `started`), never among the seats; a strip that is gone is dropped (`up` says so) and the next `up` opens a new one;
- warnings and fallbacks: a failed resize (exit 0), an unreadable layout (`about 12%`), a failed open (exit 3, seats still briefed), and a focus change reported when herdr returns the pane focused;
- `status`: `watcher: starting (pane P)`, `watcher: running (pid N, pane P, since HH:MM)`, `watcher: not running (watcher pane P idle)`, the line shown whenever a lock is held even with nothing queued, `"pane"` and `"pane_state"` in JSON only when a strip is recorded, and the seat table unchanged;
- `tile`: the strip parked after the seats and moved back under the anchor with `--ratio 0.88` before any seat returns, seat order kept, the geometry afterwards (strip full width at the bottom, grid above), `--dry-run`, a strip in another tab left alone, and a failed strip move that still tiles the seats (exit 3);
- `down --close-panes`: the strip closed after the seats (and its entry dropped), an idle strip closed, a strip running something else kept (exit 3), one not created by `up` never closed, nothing without `--close-panes` or with names; `restart` and `swap` ignore it;
- the fleet file: defaults, `watch = false`, the `[watch]` table, the ratio clamped to 0.1-0.4 with a warning, errors for an unsupported `position`, a ratio outside (0, 1) and a `watch` that is neither boolean nor table;
- the safety hook failing for a `plugin pane open` or `pane resize` from another command and for a prompt or start aimed at a watcher pane.

All 437 tests of 0.7.0 pass unchanged with the strip on by default. `GridCase.rects` leaves plugin panes out, so its cells stay shares of the grid area above the strip.

Version 0.9.0 (design 20), `tests/test_section20.py` (38 tests). Wall time is moved by hand where ages matter: tests patch `herdr_fleet.inbox.now_utc`, which `state_seen`, debt ages, queue ages and `ack --older-than` all read; the watcher test derives it from the fake clock.

- history from mixed sources: `cur/` files in full, log-only sends (pre-0.6, no id) as `[from A to B at HH:MM, first line only]`, queued sends and other pairs left out, the closing `N of M messages have only their first line …` note, `body_available` in JSON, and `no messages between A and B`;
- debt ages and ids in `log --owed` and `status` (`for 5h12m (#a1b2c3, since …)`, `(no id, since …)`), and the age format (`<1m`, `3m`, `1h05m`, `2d03h`);
- `ack --older-than` with `--dry-run` (lists, writes nothing, no herdr with `--as`), per-message acks by id, a no-id message acked by name, names restricting, a no-id message skipped when a newer one from that sender is owed, the duration grammar and the usage error without anything to ack;
- the send lines for the three places (`sent … (idle)`, `sent … (working: typed into its own input, read after its current turn)`, `queued in herdr-fleet for … #id, position 2 of 2`); `--urgent` first in the listing (`[urgent]`), in the batch and with `position 1 of 3`, its `priority: 1` header, and the working-seat report;
- `inbox_print_max`: the cut after ten lines with `… 359 B in full: <cur/ path>`, the delivery still recorded, the same cut in `--history`, `--full`, `--full` without `--read` or `--history` as a usage error, and validation;
- `state_seen`: recorded by `status`, `send`, `deliver` and `inbox` (the caller's own seat), `blocked (4m)`, `unknown (12m)`, `state_since` in JSON, other states unlabelled, `save_state` keeping the record already on disk, and nothing written without a state file;
- the watcher's `notify_blocked_after`: one `request` notification per blocked episode (`fablyn-dev blocked 3m (w2:p2): answer its prompt`), a second after the seat was idle and blocked again, none from a restarted watcher for the same episode (`notified` in `state_seen`), and no prompt, wait or get on the seat; the setting's parsing;
- `queued_by`: `refusal`, `queue` (also a `--when-ready` leftover) and `fyi` (flag or prefix) in the header; files without it listed `[queued]` or `[fyi]` and never typed ahead; the FYI line;
- the drain at the first opportunity: `deliver` typing a refusal mailbox ahead to a working seat and leaving a `queue` one; `deliver NAME --now` (its usage error without names, and a blocked seat left alone); the flush before send typing refusal messages ahead while a `--queue` message stays, and not flushing `queue` messages; an FYI riding along to an idle seat; the watcher waiting with `--until working` only for refusal mailboxes, typing ahead at once (no debounce) on `working`, delivering after the debounce on `done`, a mixed mailbox typed ahead whole and in order, and `watch --once`;
- kickoffs as owed boundaries in both directions, messages carried in `{inbox}` staying owed, and the unit rule;
- cross-fleet logging with a second temporary fleet: the same entry in both logs, the body in the other fleet's `cur/`, the debt in the other fleet's `log --owed` and `--history`, a `--reply-to` from there settling both sides, a queued message mirrored with its later `delivered` entry, and nothing written for an agent outside any fleet, inside this fleet, or reached as a contact;
- queue ages in `status` (`queued: fablyn-dev 2 (oldest 25m), owner 1`), the inbox hint with and without `--as`, the roster sentences, and `support.typed_ahead_allowed` (a kickoff to a working seat fails even with a refusal mailbox).

The 479 tests of 0.8.0 pass, with their expected send, status and listing lines updated to the 0.9.0 wording (design 20.9).

Version 0.10.0 (design 21), `tests/test_section21.py` (46 tests):

- `presence.prompt_line` on the three real screen tails (Claude Code `❯` alone and with text, Codex's `› Ask Codex to do anything` placeholder, Antigravity `>`), the last glyph line winning, a dialog with no prompt line, and a kind with no glyph entry;
- the guard table against the fake: a pane not focused (one `pane get`, nothing read), focused in an unfocused workspace or a background tab, focused and empty (delivered), focused and typing, focused with a dialog on screen, the Codex placeholder, Antigravity, an unknown kind passing and `doctor`'s `info presence` line;
- every caller's `typing` line: `send` (queued, `idle, owner typing in …`, exit 0, `queued_by: refusal`), `send --now` (refused, file in `cur/`, `delivery: refused`, exit 1), `deliver`, `watch --once` and the flush before send (`X: owner typing in …, N left queued`), `restart`, `swap`, `down` and `kickoff` of a live seat (refused, exit 3), kickoffs after `agent start` passing, and the `--dry-run` lines (`would hold: owner typing in …`);
- the watcher's hold with the fake clock: one `holding` line, the guard again every 3 s with `pane get` first, delivery once the owner clears the line or leaves the pane, and the seat going working during a hold (`X working; waiting`, back to waiting);
- 21.1: the 18.3 `working` row for all four modes, no `--until working` wait anywhere, the flush before send only to an idle or done seat, `deliver` and `watch --once` leaving a working seat's refusal mailbox queued, `deliver NAME --now` and `send --now --urgent` typing ahead;
- the safety hook failing for a prompt the fake recorded as `owner_typing`, and `typed_ahead_allowed` true only for `send --now` and `deliver --now`.

The 517 tests of 0.9.0 pass, with the expectations of the withdrawn typed-ahead paths (a plain `send`, `deliver`, the flush and the watcher to a working seat) turned into their 0.10.0 lines.

Version 0.11.0 (design 22), `tests/test_section22.py` (63 tests), plus two in `tests/test_manifest.py` that run the `go` action and the `go` pane as herdr does:

- names applied by `up` (adopted `--me` seats included, the strip left as `Fleet watch`), `restart` (a seat restarted into a new pane named again, one restarted in place keeping its name), `swap` (once, at its end), `kickoff`, `tile` and `status` (nothing printed; the `--json` payload unchanged); the one `panes:` line (`named 3 after their seats`, `named 1 (fablyn-reviewer)`) and no line when nothing changed; no rename for a pane already named, nor for an absent seat;
- the `label` key: the pane named `ORCH` while listings and kickoffs keep the seat name, surrounding blanks dropped, a pane named after the seat renamed to its label, and the errors (empty, blank, 41 characters, not a string, two lines, a leading `-`) reported together;
- contacts, another fleet's live agent and the watcher strip never renamed, even with a state record pointing the strip at a seat's pane;
- `down` clearing a pane it left at a shell and not one it closed, nor one without a name, nor a refused seat's; a failed clear as a warning;
- dry runs (`up`, `restart`, `swap`, `tile`) renaming nothing; a failed `pane rename` or `pane get` as a `panes: could not name …` warning with the exit code unchanged, and on stderr only for `status`;
- the doctor line both ways (`info pane names: 2 seats unnamed (…); run herdr-fleet status`, `1 seat`, `ok pane names: every live seat's pane is named`), a `label` counted as the wanted name, and doctor renaming nothing;
- `go` for a seat, a done seat (reported `done`, then marked seen), a contact (by its pane), a live agent of another fleet, an absent seat and an absent contact (exit 3, nothing focused), an unknown name and `owner` (exit 2), `--fleet`, outside herdr, a failed focus (exit 1), and writing nothing under `.herdr/`;
- the `manage` popup's `go` (one seat, no confirmation, no Enter; `a` answered `go needs one seat` and the action asked again; an absent seat waiting for Enter; quitting), `menu --go` (seats, then contacts as `c1`; a seat; a contact; `q`, Enter and end of input; an absent seat and a bad answer asked again; no contacts), and `ui go`;
- the client's argv for `pane rename`, `pane rename --clear` and `agent focus` (none of them forbidden), and the safety hook failing a rename by another command, of a pane hosting no seat, to another name, of a plugin pane, a `--clear` outside `down` or on a pane hosting an agent, and an `agent focus` from `status`.

The 563 tests of 0.10.0 pass; two expectations changed: the manifest's action list gains `go`, and `status --context` is compared with a second plain `status`, since the first one names the panes.

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

## Live smoke test for 0.6.0 (to run by hand)

Not run yet. For the designer or the owner, inside herdr, from this repository, with the one-seat Haiku fleet of the first smoke test (`$S`, `$F`), before its `up`.

Unreleased code must never reach a seat through the installed launcher: `~/.local/bin/herdr-fleet` (written by `install-cli`) runs the plugin copy herdr installed, not this clone. Run the commands below as `python3 bin/herdr-fleet …` from the clone, export `HERDR_FLEET_BIN` for anything that calls `herdr-fleet` by name, and give the seat the same variable so a seat that runs `herdr-fleet inbox --read` runs this clone too:

```bash
cd /Users/nabsha/work/yarrasys/projects/herdr-fleet-plugin
export HERDR_FLEET_BIN="$PWD/bin/herdr-fleet"
echo "env = { HERDR_FLEET_BIN = \"$HERDR_FLEET_BIN\" }" >> "$F"   # the seat's table is the file's last

python3 bin/herdr-fleet send --as owner fleet-smoke "Reply with the single word INBOX" --fleet "$F"   # 1
python3 bin/herdr-fleet status --fleet "$F"                                     # 2
python3 bin/herdr-fleet up --fleet "$F"                                         # 3
python3 bin/herdr-fleet send --fyi --as owner fleet-smoke "FYI: no reply needed" --fleet "$F"    # 4
python3 bin/herdr-fleet inbox --as fleet-smoke --fleet "$F"                     # 5
python3 bin/herdr-fleet deliver --fleet "$F"; python3 bin/herdr-fleet deliver --fyi --fleet "$F"  # 6
python3 bin/herdr-fleet send --queue --as owner fleet-smoke "queued only" --fleet "$F"           # 7
python3 bin/herdr-fleet inbox --as fleet-smoke --read --fleet "$F"              # 8
python3 bin/herdr-fleet send --as fleet-smoke owner "Smoke test: owner notification" --fleet "$F" # 9
python3 bin/herdr-fleet inbox --as owner --history fleet-smoke --fleet "$F"     # 10
python3 bin/herdr-fleet log --fleet "$F"; ls "$S/.herdr/inbox"/*/*              # 11
python3 bin/herdr-fleet install-cli --dir "$S/bin"                              # 12
env -u HERDR_FLEET_BIN "$S/bin/herdr-fleet" --version; "$S/bin/herdr-fleet" --version   # 13
```

| Step | Expected |
| --- | --- |
| 1. send to an absent seat | `queued for fleet-smoke (absent) #xxxxxx`, exit 0; a file in `$S/.herdr/inbox/fleet-smoke/new/`; `inbox/` and `watch.lock` in `$S/.herdr/.gitignore` |
| 2. status | `queued: fleet-smoke 1` after the owed lines |
| 3. up delivers at kickoff | `fleet-smoke: briefed - kickoff sent (up) with 1 queued`; the kickoff (its `kickoff_text` has no `{inbox}`) ends with `Messages that arrived while you were away:` and `[from owner at HH:MM #xxxxxx]` / `Reply with the single word INBOX`; the seat answers READY and INBOX in its pane; the file is in `cur/` |
| 4. FYI to an idle seat | `queued for fleet-smoke (idle, fyi) #yyyyyy`; nothing reaches the seat (it stays idle; check with `status`) |
| 5. inbox list | one line `#yyyyyy  HH:MM  owner  [fyi]  FYI: no reply needed`; runs no herdr command; nothing moves |
| 6. deliver | first `nothing to deliver (only FYIs are queued; deliver --fyi takes them)`, then `delivered 1 to fleet-smoke (idle), …`; the seat receives `[from owner at HH:MM #yyyyyy]` and the FYI as one prompt |
| 7. --queue | `queued for fleet-smoke (idle) #zzzzzz`; nothing reaches the seat |
| 8. inbox --read | prints `[from owner at HH:MM #zzzzzz]` / `queued only`; the file moves to `cur/`; `inbox --count` would now print 0 |
| 9. owner | `sent to owner (notification), …`; a herdr notification titled `message from fleet-smoke #…` with the first line in its body |
| 10. history | the owner's messages to the seat and the seat's message to the owner, oldest first, as `[from A to B at HH:MM #id]` |
| 11. log | `send` entries with `#id` and `[queued]` tags, `delivered #id via kickoff`, `via deliver`, `via inbox`, `via notify` |
| 12. install-cli | `wrote launcher $S/bin/herdr-fleet: runs the herdr-fleet that herdr installed (falls back to …; HERDR_FLEET_BIN overrides)`; the file is executable and starts with the `herdr-fleet launcher` comment |
| 13. launcher | without `HERDR_FLEET_BIN` it prints the installed plugin's version (from `herdr plugin list --json`); with it, `herdr-fleet 0.6.0` from this clone |

Then run `down --close-panes --fleet "$F"` and remove `$S` as in the first smoke test.

## Live smoke test for 0.6.1 and 0.7.0 (to run by hand)

Not run yet. For the designer or the owner, inside herdr, from this repository, with the one-seat Haiku fleet of the first smoke test (`$S`, `$F`), and with `HERDR_FLEET_BIN` exported and in the seat's `env` as in the 0.6.0 test. Wait for the seat to be idle between steps unless a step says otherwise.

### The watcher

```bash
python3 bin/herdr-fleet up --fleet "$F"                                         # 1
herdr plugin link "$PWD"                                                        # 2: the pane runs this clone
herdr plugin pane open --plugin yarrasys.fleet --entrypoint watch --placement split --cwd "$S" --no-focus   # 3
python3 bin/herdr-fleet watch --fleet "$F"; echo "exit $?"                      # 4
python3 bin/herdr-fleet send --queue --as owner fleet-smoke "Reply with the single word WATCH" --fleet "$F"     # 5
python3 bin/herdr-fleet send --as owner fleet-smoke "Count from 1 to 30, one number per line" --fleet "$F"     # 6
python3 bin/herdr-fleet send --queue --as owner fleet-smoke "Then reply with the single word LATER" --fleet "$F"  # 7 (at once)
python3 bin/herdr-fleet send --queue --fyi --as owner fleet-smoke "FYI: no reply needed" --fleet "$F"          # 8
python3 bin/herdr-fleet status --fleet "$F"                                     # 9
herdr plugin pane close <pane id from step 3>                                   # 10
python3 bin/herdr-fleet status --fleet "$F"; ls -a "$S/.herdr"                  # 11
python3 bin/herdr-fleet watch --once --fleet "$F"                               # 12
python3 bin/herdr-fleet log --fleet "$F"                                        # 13
```

| Step | Expected |
| --- | --- |
| 3. watch pane | a split beside the focused pane printing `HH:MM:SS  watching $S/.herdr/fleet.toml (pid N, debounce 5000 ms); Ctrl-C stops`; `$S/.herdr/watch.lock` holds that pid |
| 4. a second watcher | `herdr-fleet: error: watcher already running (pid N)`, exit 1 |
| 5. queued to an idle seat | within 2 s the pane prints `waiting on fleet-smoke (1 queued)` and `fleet-smoke idle; delivering 1`, about 5 s later `delivered 1 to fleet-smoke`; a notification `delivered 1 to fleet-smoke`; the seat answers WATCH |
| 6, 7. queued while working | step 6 is delivered at once and the seat starts counting; step 7 stays queued while it counts, and only after the count ends does the pane print `fleet-smoke idle; delivering 1` and `delivered 1 to fleet-smoke`; the seat answers LATER. If the seat starts another turn within the 5 s, the pane prints `fleet-smoke working; still waiting` and delivers later |
| 8, 9. FYI only | no waiter, nothing printed; `status` shows `watcher: running (pid N since HH:MM)`, then `queued: fleet-smoke 1` |
| 10, 11. pane closed | the watcher stops: `status` shows `watcher: not running`, and `watch.lock` is gone (if it is still there, closing a plugin pane sent the watcher no SIGHUP; note it) |
| 12. --once | `nothing to deliver` (only an FYI is queued); no lock is written |
| 13. log | `delivered #… via watch` entries for steps 5 and 7, none for the FYI |

### Hooks

The seat's Claude Code reads `.claude/settings.local.json` from its own working directory, so this part uses a second scratch fleet whose seat runs in that directory. Claude Code asks once whether to trust the new directory: `up` reports the seat blocked, the owner answers the prompt in the seat's pane, then `kickoff fleet-hooks` briefs it.

```bash
H=$(mktemp -d /tmp/fleet-hooks.XXXXXX); mkdir -p "$H/.herdr"; G="$H/.herdr/fleet.toml"
cat > "$G" <<EOF
cwd = "$H"

[[agents]]
name = "fleet-hooks"
kind = "claude"
args = ["--model", "claude-haiku-5-5"]
env = { HERDR_FLEET_BIN = "$HERDR_FLEET_BIN" }
kickoff_text = "[from herdr-fleet] Hooks smoke test ({reason}). You are {name}. Reply with the single word READY, then wait. Run no tools unless a message asks you to."
EOF
python3 bin/herdr-fleet doctor --fleet "$G" | grep hooks                       # 14
python3 bin/herdr-fleet hooks install --fleet "$G"; cat "$H/.claude/settings.local.json"   # 15
python3 bin/herdr-fleet hooks install --fleet "$G"                             # 16
python3 bin/herdr-fleet doctor --fleet "$G" | grep hooks                       # 17
python3 bin/herdr-fleet up --fleet "$G"                                        # 18, then the owner answers the trust prompt
python3 bin/herdr-fleet kickoff fleet-hooks --fleet "$G"                       # 19
python3 bin/herdr-fleet send --as owner fleet-hooks "Count from 1 to 30, one number per line" --fleet "$G"   # 20
python3 bin/herdr-fleet send --queue --as owner fleet-hooks "Then reply with the single word HOOKED" --fleet "$G"   # 21 (at once)
python3 bin/herdr-fleet send --queue --as owner fleet-hooks "Reply with the single word PULLED" --fleet "$G"   # 22
herdr agent prompt fleet-hooks "Say hi"                                         # 23
python3 bin/herdr-fleet log --fleet "$G"                                       # 24
python3 bin/herdr-fleet hooks install --remove --fleet "$G"; cat "$H/.claude/settings.local.json"   # 25
```

| Step | Expected |
| --- | --- |
| 14 | `info  hooks: not installed (herdr-fleet hooks install)` |
| 15 | `installed the Stop and UserPromptSubmit hooks in $H/.claude/settings.local.json`; the file holds the two entries with `"timeout": 10` |
| 16 | `already installed in …`; the file is unchanged |
| 17 | `ok    hooks: installed` |
| 20, 21 | the seat counts; step 21 stays queued; when the count ends, the Stop hook hands the message back and the seat answers HOOKED in the same turn, then stops (the second Stop sees `stop_hook_active` and the hook prints nothing); the file is in `cur/` |
| 22, 23 | step 22 stays queued (idle seat, `--queue`); the prompt of step 23 arrives with the note `1 message waiting; run: herdr-fleet inbox --read`, and the seat may run it and answer PULLED |
| 24 | `delivered #… via hook` for step 21, and `via inbox` if the seat read step 22 |
| 25 | `removed the Stop and UserPromptSubmit hooks from …`; the file holds no `hooks` key |

Then `down --close-panes` both fleets, `herdr plugin unlink yarrasys.fleet` if it was linked only for this test, and remove `$S` and `$H`.

## Live smoke test for 0.8.0 (to run by hand)

Not run yet. For the designer or the owner, inside herdr, from this repository. The plugin must be linked so the strip runs this clone's `watch` entrypoint, and `HERDR_FLEET_BIN` exported so seats and the launcher run it too. Two Haiku seats in a scratch fleet, running in a directory Claude Code already trusts. Wait for the seats to be idle between steps unless a step says otherwise. Never answer a seat's prompt on its behalf; if a seat is blocked, the owner answers it and runs `kickoff`.

### A fresh workspace

```bash
cd /Users/nabsha/work/yarrasys/projects/herdr-fleet-plugin
export HERDR_FLEET_BIN="$PWD/bin/herdr-fleet"
herdr plugin link "$PWD"
W=$(mktemp -d /tmp/fleet-strip.XXXXXX); mkdir -p "$W/.herdr"; F="$W/.herdr/fleet.toml"
cat > "$F" <<EOF
workspace = "fleet-strip"
cwd = "$PWD"

[layout]
mode = "grid"

[[agents]]
name = "strip-a"
kind = "claude"
args = ["--model", "claude-haiku-5-5"]
env = { HERDR_FLEET_BIN = "$HERDR_FLEET_BIN" }
kickoff_text = "[from herdr-fleet] Strip smoke test ({reason}). You are {name}. Reply with the single word READY, then wait. Run no tools unless a message asks you to."

[[agents]]
name = "strip-b"
kind = "claude"
args = ["--model", "claude-haiku-5-5"]
env = { HERDR_FLEET_BIN = "$HERDR_FLEET_BIN" }
kickoff_text = "[from herdr-fleet] Strip smoke test ({reason}). You are {name}. Reply with the single word READY, then wait. Run no tools unless a message asks you to."
EOF
python3 bin/herdr-fleet up --dry-run --fleet "$F"                               # 1
python3 bin/herdr-fleet up --fleet "$F"                                         # 2
P=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["watch"]["pane_id"])' "$W/.herdr/fleet.state.json")
herdr pane layout --pane "$P"                                                   # 3
python3 bin/herdr-fleet status --fleet "$F"; cat "$W/.herdr/watch.lock"         # 4 (a few seconds after 2)
python3 bin/herdr-fleet up --fleet "$F"                                         # 5
python3 bin/herdr-fleet send --queue --as owner strip-a "Reply with the single word STRIP" --fleet "$F"   # 6
python3 bin/herdr-fleet tile --dry-run --fleet "$F"; python3 bin/herdr-fleet tile --fleet "$F"           # 7
herdr pane layout --pane "$P"                                                   # 8
python3 bin/herdr-fleet down --close-panes --close-workspace --fleet "$F"      # 9
ls -a "$W/.herdr"; cat "$W/.herdr/fleet.state.json"                             # 10
```

| Step | Expected |
| --- | --- |
| 1 | a `watcher: plan - herdr plugin pane open … --placement split --target-pane '<root pane of fleet-strip>' --direction down … --no-focus, then herdr pane resize … --direction down --amount 0.38` line; nothing opens |
| 2 | `watcher: started - herdr-fleet watch in pane P below <root>, 12% of the area`; the strip spans the bottom of the new workspace with both seats above it; your own pane keeps the focus (if the line says `herdr moved the focus to it`, note it: `--no-focus` was not honoured) |
| 3 | one `down` split whose rect is the whole tab, `ratio` about 0.88; the strip's rect is the full width and about an eighth of the height |
| 4 | `watcher: running (pid N, pane P, since HH:MM)` with nothing queued; the lock names that pid and the fleet file |
| 5 | `watcher: already running (pid N)`; no second strip |
| 6 | the strip prints `waiting on strip-a (1 queued)` and about 5 s later `delivered 1 to strip-a`; the seat answers STRIP |
| 7 | the dry run shows the strip parked and moved back under `strip-a` with `--ratio 0.88`, before the seats; the real run prints `watcher: moved - below strip-a (…), ratio 0.88`, and the watcher keeps running (its pane shows no `stopped`) |
| 8 | the strip still spans the full width at the bottom, about 12% high |
| 9 | the seats quit, their created panes close, then `watcher: pane closed - P (the watcher stopped)`; the workspace closes |
| 10 | no `watch.lock`; the state file has no `"watch"` entry |

### Live seats beside your pane

```bash
sed -i '' '/^workspace = /d' "$F"
python3 bin/herdr-fleet up --no-watch --fleet "$F"                              # 11
python3 bin/herdr-fleet status --fleet "$F"                                     # 12
python3 bin/herdr-fleet up --fleet "$F"                                         # 13
python3 bin/herdr-fleet tile --fleet "$F"                                       # 14
herdr pane close <the strip's pane id>                                          # 15
python3 bin/herdr-fleet status --fleet "$F"; ls -a "$W/.herdr"                  # 16
python3 bin/herdr-fleet up --fleet "$F"                                         # 17
python3 bin/herdr-fleet down --close-panes --fleet "$F"                        # 18
```

| Step | Expected |
| --- | --- |
| 11 | the seats split off your pane; no strip and no `watcher` line |
| 12 | no `watcher:` line (no lock, no strip, nothing queued) |
| 13 | `watcher: started - herdr-fleet watch in pane P below <strip-b's pane>, 12% of the area`, printed after the kickoff lines, under the last seat only |
| 14 | the strip moves under `strip-a` and spans both seats, not your pane |
| 15, 16 | the watcher stops through SIGHUP: `status` shows `watcher: not running` (the strip is pruned from the state file) and `watch.lock` is gone. If herdr keeps the pane open with nothing running, `status` shows `watcher: not running (watcher pane P idle)` instead; note which |
| 17 | a new strip opens (or, if step 16 showed an idle pane, `watcher: not running - its pane P is idle`, exit 3, and no second strip) |
| 18 | the seats quit and their panes close, then the strip closes |

Then `herdr plugin unlink yarrasys.fleet` if it was linked only for this test, and remove `$W`.

## Live smoke test for 0.9.0 (to run by hand)

Not run yet. For the designer or the owner, inside herdr, from this repository, with `HERDR_FLEET_BIN` exported and the plugin linked as for 0.8.0. Two Haiku seats in a scratch fleet in a directory Claude Code already trusts, plus a second scratch fleet for the cross-fleet steps. Never answer a seat's prompt on its behalf: where a step needs a seat `blocked`, the owner makes it ask for a permission and answers it at the step that says so.

```bash
cd /Users/nabsha/work/yarrasys/projects/herdr-fleet-plugin
export HERDR_FLEET_BIN="$PWD/bin/herdr-fleet"
W=$(mktemp -d /tmp/fleet-090.XXXXXX); mkdir -p "$W/.herdr"; F="$W/.herdr/fleet.toml"
cat > "$F" <<FLEET
workspace = "fleet-090"
cwd = "$PWD"
inbox_print_max = 200

[watch]
notify_blocked_after = "1m"

[[agents]]
name = "n9-a"
kind = "claude"
args = ["--model", "claude-haiku-5-5"]
env = { HERDR_FLEET_BIN = "$HERDR_FLEET_BIN" }
kickoff_text = "[from herdr-fleet] 0.9.0 smoke test ({reason}). You are {name}. Reply READY, then wait. Run no tools unless a message asks you to."

[[agents]]
name = "n9-b"
kind = "claude"
args = ["--model", "claude-haiku-5-5"]
env = { HERDR_FLEET_BIN = "$HERDR_FLEET_BIN" }
kickoff_text = "[from herdr-fleet] 0.9.0 smoke test ({reason}). You are {name}. Reply READY, then wait. Run no tools unless a message asks you to."
FLEET
H="python3 bin/herdr-fleet"
$H up --fleet "$F"                                                                     # 1
$H send --as owner n9-a "Run: sleep 40, then reply DONE" --fleet "$F"                 # 2 (n9-a now works for 40 s)
$H send --as owner n9-a "Second: reply SECOND" --fleet "$F"                           # 3 (while it works)
$H send --as owner --queue n9-b "one" --fleet "$F"; $H send --as owner --queue n9-b "two" --fleet "$F"
$H send --as owner --urgent --queue n9-b "zero" --fleet "$F"; $H inbox --as n9-b --fleet "$F"    # 4
$H deliver n9-b --fleet "$F"                                                           # 5
$H send --as owner --queue n9-b "$(seq 1 60 | sed 's/^/line /')" --fleet "$F"; $H inbox --as n9-b --read --fleet "$F"   # 6
$H inbox --as owner --history n9-b --fleet "$F"; $H inbox --as owner --history n9-b --full --fleet "$F"                  # 7
$H status --fleet "$F"; $H log --owed --fleet "$F"                                    # 8
$H ack --as n9-a --older-than 1m --dry-run --fleet "$F"                               # 9
$H kickoff n9-a --fleet "$F"; $H log --owed --fleet "$F"                              # 10 (once n9-a is idle)
```

Blocked seats and the drain (the owner makes n9-a ask for a permission, for example by asking it by hand to run a command that needs approval, and leaves the prompt open):

```bash
$H send --as owner n9-a "When you can: reply BLOCKED-OK" --fleet "$F"                # 11 (n9-a blocked)
$H status --fleet "$F"                                                                 # 12 (wait 1-2 min)
# the owner answers n9-a's prompt now
$H log --seat n9-a --last 5 --fleet "$F"                                               # 13
```

Cross-fleet:

```bash
X=$(mktemp -d /tmp/fleet-090x.XXXXXX); mkdir -p "$X/.herdr"
printf '[[agents]]\nname = "n9-x"\nkind = "claude"\nargs = ["--model", "claude-haiku-5-5"]\n' > "$X/.herdr/fleet.toml"
(cd "$X" && $H up --no-watch)                                                          # 14
$H send --as n9-a n9-x "Cross-fleet: reply to n9-a with --fyi --reply-to the id you see" --fleet "$F"   # 15
(cd "$X" && $H log --owed; ls .herdr/inbox/n9-x/cur)                                   # 16
(cd "$X" && $H down --close-panes); $H down --close-panes --close-workspace --fleet "$F"        # 17
```

| Step | Expected |
| --- | --- |
| 1 | both seats briefed; the kickoff's roster now says to reply with `--fyi` or `ack #id` and to queue to control order |
| 2, 3 | 3 prints `sent to n9-a (working: typed into its own input, read after its current turn), …`; n9-a answers SECOND after DONE |
| 4 | three `queued in herdr-fleet for n9-b (idle) #…, position N of M` lines, the urgent one `position 1 of 3`; the listing shows `1. … [urgent]  [queued]  zero`, then `one`, `two` |
| 5 | one prompt to n9-b with zero, one, two in that order |
| 6 | `--read` prints the long message's header, ten lines and `… <size> in full: <path>`; the file at that path holds all 60 lines |
| 7 | `--history` shows the exchange with the long message cut the same way; with `--full`, all 60 lines |
| 8 | `awaiting: … owes owner for …m (#id, since HH:MM): …` lines with ages; `queued:` with ages if anything waits |
| 9 | `would ack #… since …, …m ago (…)` lines for anything n9-a owes; nothing written (`log --owed` unchanged) |
| 10 | n9-a's debts, both ways, gone after its kickoff |
| 11 | `queued in herdr-fleet for n9-a (blocked) #…, position 1 of 1` |
| 12 | `blocked (1m)` or more in STATE; after a minute the watcher strip prints `n9-a blocked 1m (P): answer its prompt` and one notification arrives (only one) |
| 13 | once the owner answered, the strip printed `n9-a working; typing ahead 1 (queued on refusal)` (or, if n9-a went idle first, `n9-a idle; delivering 1`); n9-a replied BLOCKED-OK; the log shows `delivered #… via watch` |
| 14 | the second fleet's seat starts in its own workspace |
| 15 | `sent to n9-x (idle), …`; `$X/.herdr/messages.jsonl` holds the same entry |
| 16 | `awaiting: n9-x owes n9-a …` in the second fleet until n9-x replies, then nothing in either fleet; the body is in `$X/.herdr/inbox/n9-x/cur/` |
| 17 | everything closes; remove `$W` and `$X` |

## Live smoke test for 0.10.0 (to run by hand)

Not run yet. For the designer or the owner, inside herdr, with `HERDR_FLEET_BIN` exported and a one-seat Haiku scratch fleet as for 0.9.0 (seat `n10-a`, watcher strip on). `herdr pane send-text` stands in for the owner's own typing; never use it on a pane in state `blocked`, and check `herdr agent get n10-a` says `idle` first.

```bash
cd /Users/nabsha/work/yarrasys/projects/herdr-fleet-plugin
export HERDR_FLEET_BIN="$PWD/bin/herdr-fleet"
W=$(mktemp -d /tmp/fleet-0100.XXXXXX); mkdir -p "$W/.herdr"; F="$W/.herdr/fleet.toml"
cat > "$F" <<FLEET
workspace = "fleet-0100"
cwd = "$PWD"

[[agents]]
name = "n10-a"
kind = "claude"
args = ["--model", "claude-haiku-5-5"]
env = { HERDR_FLEET_BIN = "$HERDR_FLEET_BIN" }
kickoff_text = "[from herdr-fleet] 0.10.0 smoke test ({reason}). You are {name}. Reply READY, then wait. Run no tools unless a message asks you to."
FLEET
H="python3 bin/herdr-fleet"
$H up --fleet "$F"; P=$(herdr agent get n10-a | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["agent"]["pane_id"])')
$H doctor --fleet "$F" | grep presence                                                # 1 (no line: claude has a glyph)
herdr agent get n10-a                                                                  # 2 must say idle
herdr pane send-text "$P" "half typed"                                                 # 3 the owner's stand-in, no Enter
# 4: run this, then within 15 s click into n10-a's pane and stay there (do not type) for 30 s
(sleep 15; $H send --as owner n10-a "ping from test: reply PONG" --fleet "$F" > "$W/send.out"; \
 herdr pane read "$P" --lines 12 > "$W/read.out") &
# 5: still in n10-a's pane after those 30 s, delete "half typed" by hand; then come back here
cat "$W/send.out" "$W/read.out"                                                        # 6
$H send --as owner n10-a "Run: sleep 30, then reply DONE" --fleet "$F"               # 7
$H send --as owner n10-a "while working: reply SECOND" --fleet "$F"                  # 8 (n10-a is working)
$H deliver --fleet "$F"; $H deliver n10-a --now --dry-run --fleet "$F"                # 9
$H down --close-panes --close-workspace --fleet "$F"                                   # 10
```

| Step | Expected |
| --- | --- |
| 1 | no `presence` line |
| 3 | the seat's prompt line reads `❯ half typed` |
| 4 | the watcher strip prints `n10-a idle; delivering 1`, then once `n10-a idle; owner typing in P, holding 1`, and nothing more while the text stays |
| 5 | within about 3 s of the line being cleared the strip prints `delivered 1 to n10-a` and the seat replies PONG; the prompt it got is the message alone, not `half typedping from test …` |
| 6 | `send.out`: `queued in herdr-fleet for n10-a (idle, owner typing in P) #…, position 1 of 1`; `read.out` still shows `❯ half typed` |
| 7, 8 | 8 prints `queued in herdr-fleet for n10-a (working) #…, position 1 of 1`; nothing reaches the seat until it is idle again, when the strip delivers SECOND |
| 9 | while n10-a works and something is queued: `n10-a: working, 1 left queued`, and the dry run of `--now` says `would deliver … (working: typed into its own input, read after its current turn)` |
| 10 | everything closes; remove `$W` |

The hold lasts as long as the owner's focus and text do (design 21.2): leaving the pane with text still in its box ends it, and the next delivery would merge with that text. Step 5 therefore clears the line before leaving.

Version 0.12.0 (design 23), `tests/test_section23.py` (78 tests: 65 for 23.1 to 23.7, 13 for 23.11):

- 23.1: the note first and the outcome last for one sender, several (in the order of their oldest message, `and`, then commas), `--as` (`inbox --as NAME --read`), a dry run (nothing moves), no note without mail or with only expired mail; `; lands at its next idle, or now with --now` on a plain send queued to a working seat (and its dry run), and not on an FYI, `--queue` or `--when-ready` send;
- 23.2: `inbox --wait` returning at once, waiting on the fake clock (one look per second, no herdr command after the caller lookup), `--timeout` (`nothing arrived in 10m`, `30 s`, exit 3), FYIs counting, a message past its ttl not ending the wait (and expired on the read), `--as`, `--json`, `--full`, the usage errors (`--count`, `--history`, `--redeliver`, `--hook`, `--timeout` alone, a negative timeout), and the lock shared with the watcher (a message the watcher took first is not read again, and the wait goes on), the roster sentence;
- 23.3: a named agent, an unnamed one that a contact matches (sends, reads its inbox, acks and interrupts as the contact), an unnamed one with no match and one that two contacts match (usage errors, nothing sent or written), a shell pane and outside herdr (`owner`), each command that resolves the caller (`send`, `ack`, `inbox`, `interrupt`), `--as` still working, and `log` and `go` unaffected;
- 23.4: `ack #id --reason` by a third party (the entry, the line, `log --owed` empty, the `acked by` log line), the refusal without `--reason` (exit 2, nothing written), the debtor with a reason and a dry run, `--reason` with names, `--all`, `--older-than` or a blank text, the `status` hints `(ack NAME, or ack #id)` and `(ack NAME)`, `log --owed` unchanged, the roster sentence;
- 23.5: `blocked: a dialog is open` in `send`, `send --now`, `deliver`, `restart`, `down`, `swap`, `kickoff` and `--when-ready` lines, and the bare state in `status`;
- 23.6: `peek` of a seat (exactly what `agent read --lines 30` printed, nothing written), `--lines`, a contact (by its pane), a live agent, an absent seat and contact (exit 3), an unknown name, `owner` and `--lines 0` (exit 2);
- 23.7: the exit confirmation answered by `restart`, `down` and `swap` (one `enter`, right after `agent read --lines 16`, after 3 s, nothing logged), another dialog and the exit dialog with `Stay` highlighted (no key, `the pane shows a dialog: "…"; answer it by hand`), no dialog (today's line), a kind without an entry (no look at 3 s, one read for the report), a dry run, the popup (no key), a failed Enter, an Enter that did not end the agent, and the pure `shows_exit_dialog` and `dialog_first_line`; `interrupt` for a working Claude seat (one `ctrl+c`, the wait, the log entry and line, no debt), agy (two, 300 ms apart), codex (refused, then configured), the plugin config and the fleet file's precedence, the whitelist (errors for a bad key, two `ctrl+c` for claude, an empty or non-list entry; a bad config entry left out with a doctor warning), idle, done, unknown, blocked and absent refused, a dialog on screen at the read before the keys refused, still working and gone after the keys, `--wait`, contacts, other agents, `owner` and unknown names (exit 2), the caller's own seat, the owner typing, and no manifest action or popup entry;
- 23.11: the guard on a pane nobody is in with text in its input (one `pane read` when the line is empty; `pane get` only when it holds text), the wording by focus (`owner typing in …`, `text in its input (…); press Ctrl-U there to clear it`, `a dialog or a menu is on screen in …`), `send` queueing and `--now` refusing, `deliver` and `kickoff` holding, `restart` and `down` refused with `refused - text in its input (…); press Ctrl-U there to clear it` and quitting once the line is clear, the watcher holding and delivering after the line was cleared; `interrupt` clearing a restored prompt with one `ctrl+u` between two reads (the log's `keys` gaining `ctrl+u`, the next send landing alone), nothing more without a restored prompt, the still-held line when the key changes nothing or fails, no cleanup while still working or for a kind without a glyph; the harness failing a `ctrl+u` without a read showing text, before any read, twice, or from another command, and a prompt to an unfocused pane holding text;
- the client (`agent_send_keys` accepts `enter`, `esc`, `ctrl+c` and `ctrl+u` only, refuses an option-like target, and `run`/`text` still refuse `agent send-keys` and the pane forms; `agent_read` is plain text) and the harness failing each forbidden shape above.

The 628 tests of 0.11.0 pass, with the lines 0.12.0 rewords: `blocked: a dialog is open` in queue and refusal lines, the `; lands at …` ending, the inbox note first instead of the `you have N waiting` last line, and a shell pane instead of an unnamed agent for the `owner` sender. Four tests of section 21 now assert 23.11's guard instead of 21.2's: an unfocused pane is one `pane read` (not one `pane get`), text left in a pane the owner is not in holds (`text in its input …`) instead of passing, a focused pane in an unfocused workspace or background tab holds with that wording, and the watcher's hold outlasts the owner leaving the pane until the line is cleared.

Version 0.13.0 (design 24), `tests/test_section24.py` (57 tests):

- `board.render` at 162x6 (each cell at its column: name 20, state 14, doing 33, queue 52, debt 33, context 10), 162x8 (blank rows, a contact after the seats, and none when there is no room), 120x6 and 119x6 (the doing cell), 90x6 and 89x6 (the debt cell), 60x6 and 59x6 (name, state, queue count), 40x2 (one line, the most urgent seat); six seats in six rows packed two to a row at 162 columns, contacts packed after them, the six most urgent with `+4 more` when packing is not enough, and at 100 columns the most urgent four (blocked, queued, working, the rest) with `+2 more`; each state's glyph and age, the doing cell for working and blocked, the queue cell (none, one, several, cut with its quote, holding, retrying, pointed), the debt cell (with and without an id), the context cell and `⚠`; colours on (blocked red and bold, working green, idle and done dim, `⚠` yellow, footer errors red, the selected row reversed) and off (`bold` and `reverse` only, the same text); the footer's notice, the header without queues, text cut and never wrapped down to one column (wide characters, tabs, newlines), purity, and the log view;
- `board.build` from the fake: states, a working seat's title (one `pane get`, for it alone), a title naming the seat or its label shown as nothing, ages from `state_seen` (recorded for what the list showed), the blocked seat's dialog line from one `agent read --lines 16` and nothing else of the screen (a line of tool output on it never reaches the model or the drawing), no line for a dialog without a highlighted choice; queues in delivery order with FYIs counted and expired messages left out and left in place, the owner's mailbox, the watcher's holds, retries and pointers; the oldest debt per seat with the creditor shortened (`orchestrator`), the log parsed again only after it changed; context from fake transcripts with the threshold exactly at `restart_hint_tokens`, a Codex seat blank, no threshold no `⚠`, and a transcript looked for once and read again only after it changed; a contact's state and queue, an absent seat, and herdr unreachable (every seat `unknown`, nothing recorded, the error in the footer);
- the watcher with the fake screen: board mode (the lines in `.herdr/watch.log` with their times, `stopped` printed once the board is gone, the first draw, `.gitignore`, the lock removed, nothing prompted or focused), a warning from other code caught into the log and the footer once and never written past the board, a terminal where curses cannot start and `--log` on a terminal (today's lines, no log file), a stdout that is not a terminal, `--once` (never draws), and the model rebuilt at once after a delivery and 5 s later;
- keys: a number (`agent focus`, the focus moved, the line), a number without a seat and an absent seat, Up, Down and Enter, Enter without a selection; `a` then `y` (the confirmation text, the `ack` entry with `by: owner` and `reason: from the board`, no caller lookup, nothing prompted, the debt gone from the board), `a` then `n`, `a` with nothing selected or nothing owed; `d` delivering to the idle seat only (a working and a blocked seat keep their queues, no debounce line), holding for text in the input, and nothing to deliver; `l` and back; `?` for 5 s; `q` (the watcher keeps running); other keys; resizing to 90x6 and 40x2; colours with and without `NO_COLOR` and a screen without colours; a blocked seat read and focused but never prompted or sent a key;
- `tui.CursesScreen` on a stand-in `curses` module (no terminal is opened): the key names (digits, arrows, Enter as 10, 13 or `KEY_ENTER`, a resize that clears the screen, other codes), the styles as attributes and colour pairs, the bottom-right cell's error swallowed, and `open_screen` returning None for a stream without a descriptor, a terminal type `setupterm` refuses and a missing `curses` module, with colours off under `NO_COLOR` and one `endwin` on close;
- the harness failing an `agent focus` from `watch` with no key, after a key that is not a number or Enter, twice for one key, or after a key of an earlier run, and an `agent read` from `watch` before any `agent list`, of a seat listed idle (the last listing counts) and of an agent that is not a seat.

Design 25 (pages in the board, the same release), `tests/test_section25.py` (37 tests):

- navigation: Enter opening a seat page and Esc, Backspace and `q` going back (and `q` on the board saying `Ctrl-C stops the watcher`), number keys opening pages, Left and Right stepping through the seats and then the contacts and wrapping, `g` from the board and from a page, `z` through `pane zoom --current --toggle` (twice: zoomed and back), through the state file's strip without `HERDR_PANE_ID`, and ignored when neither names a pane; clicks on a row (its page), the header (the fleet page, or the owner page on the `owner:` cell), the footer (nothing) and inside a page (back), and where the owner cell is;
- the seat page: `doing`, `queue` (delivery order, `[on refusal]` and `[queued]`), `debts` (owed and owing), `recent` (newest first: `←`, `→`, kickoff), empty sections left out, a blocked seat's four dialog lines (and none of the rest of its screen), scrolling with the selection and a page at a time, `p` reading `agent read --lines 12` only then and the page dropping it when it closes, `a` on a debt (the confirmation, the `ack` entry by `owner` with the reason, the debt gone on the reload) and on anything else (`select a debt …`), `d` delivering an idle seat's queue at once and every reason it gives otherwise (`working: lands at its next idle`, `blocked: a dialog is open`, `text in its input`, `owner typing`, `absent: its next kickoff carries the queue`, `nothing queued for …`);
- the fleet page (file, workspace, pid, uptime, lock, waits, debts, queues, the pointer to `status` and `doctor`, the log, and no `pane process-info`), the owner page (newest first, delivered or not, FYI and log-only messages, `a` on a debt, on an FYI and on a message without an id, a body), the header's `owner:` cell (the newest, a log-only one included), and a contact page (the messages it exchanged, a body);
- bodies: stored, long and scrolled with ↑ ↓, PageUp and PageDown, a line cut and never wrapped, a log-only message (`(no stored body)`), and reading one moving nothing;
- every view's hints cut from the right, whole hints from the first, at 162, 120, 90, 60, 40 and 20 columns, and a notice taking the whole footer;
- the polish of 25.8: the board starting on its first seat row (Down then Enter opens the second), the seat, owner and contact pages opening with their first message selected (Enter opens its body at once), the footer's short `watching repo (pid N)` beside the log's whole line, and the curses driver on a stand-in `curses`: SGR clicks (press of the left button only; a release, the right button, a drag and the wheel give nothing; modifiers do not matter), X10 clicks raw and as `KEY_MOUSE`, an unknown CSI, bracketed paste, SS3, Alt with a digit, a sequence too long or cut short (all dropped whole, the key after them intact), a lone Esc, and no digit of a click ever coming through as a key;
- the harness failing an `agent read` of an idle seat without `p` or twice for one `p`, a `pane zoom` without a `z`, twice for one `z`, of a pane that is not the strip, or from another command, and an `agent focus` after a click or Enter; the ratio's new default and the starter's `[watch]` comment; the fake's guard taking Claude Code's `Try "…"` suggestion for an empty line.

The 706 tests of 0.12.0 pass; their changed expectations: the `.herdr/.gitignore` written by the state file, the message log and `init` gains `watch.log` (four tests), and `tests/test_watchpane.py`'s strip geometry follows the 0.2 default ratio (a resize of 0.3 instead of 0.38, `20% of the area`, the strip 10 rows of 50, tile's `--ratio 0.8`; eight tests). `tests/test_section24.py`'s keys follow 25.1 (Enter and the numbers open pages; `g` focuses) and its header and footer the owner cell and the hints.

## Live smoke test for 0.12.0 (to run by hand)

Not run yet. For the designer or the owner, inside herdr, with `HERDR_FLEET_BIN` exported and a one-seat Haiku scratch fleet as for 0.10.0 (seat `n12-a`, no watcher strip). Two live reproductions of design 23: the exit confirmation that `/exit` shows while a background task runs, and one Ctrl-C to a working seat. Check `herdr agent get n12-a` before each step that sends anything: never send a prompt or a key to it while it is `blocked`, except the one Enter herdr-fleet presses itself in step 3. Run the steps from a plain shell pane or a pane whose agent has a herdr name: since 0.12.0 a pane with an unnamed agent that no contact matches is a usage error for `interrupt`, `send`, `ack` and `inbox` (design 23.3; add `--as owner` there).

```bash
cd /Users/nabsha/work/yarrasys/projects/herdr-fleet-plugin
export HERDR_FLEET_BIN="$PWD/bin/herdr-fleet"
W=$(mktemp -d /tmp/fleet-0120.XXXXXX); mkdir -p "$W/.herdr"; F="$W/.herdr/fleet.toml"
cat > "$F" <<FLEET
workspace = "fleet-0120"
cwd = "$PWD"
watch = false

[[agents]]
name = "n12-a"
kind = "claude"
args = ["--model", "claude-haiku-5-5"]
env = { HERDR_FLEET_BIN = "$HERDR_FLEET_BIN" }
kickoff_text = "[from herdr-fleet] 0.12.0 smoke test ({reason}). You are {name}. Reply READY, then wait. Run no tools unless a message asks you to."
FLEET
H="python3 bin/herdr-fleet"
$H up --fleet "$F"
# A: the exit confirmation
$H send --as owner n12-a "Start the shell command 'sleep 900' as a background task (run_in_background), then reply STARTED and stop." --fleet "$F"   # 1
$H peek n12-a --lines 8 --fleet "$F"                                                   # 2 once it replied STARTED
$H down n12-a --fleet "$F"                                                             # 3
pgrep -fl "sleep 900"; herdr agent get n12-a                                           # 4
$H up --fleet "$F"                                                                     # 5 a fresh n12-a
# B: one Ctrl-C to a working seat
$H send --as owner n12-a "Run the shell command 'sleep 60' in the foreground (not in the background), then reply DONE." --fleet "$F"   # 6
herdr agent get n12-a                                                                  # 7 wait until it says working
$H interrupt n12-a --fleet "$F"; echo "exit $?"                                        # 8
$H peek n12-a --lines 12 --fleet "$F"; herdr agent get n12-a                           # 9
$H log --last 2 --fleet "$F"                                                           # 10
$H interrupt n12-a --fleet "$F"; echo "exit $?"                                        # 11 now idle or done
$H down --close-panes --close-workspace --fleet "$F"                                   # 12
```

| Step | Expected |
| --- | --- |
| 2 | the seat's last lines, as the screen shows them; nothing is written under `$W/.herdr` by `peek` |
| 3 | after about 3 s herdr reported the seat `blocked` with the dialog of design 23 on screen; then `n12-a: quit - answered its exit confirmation (exit and stop tasks)`, within a few seconds |
| 4 | no `sleep 900` left (the dialog's first choice stops the tasks); `agent get` answers `agent_not_found` |
| 8 | `interrupted n12-a (done)` (or `(idle)`), exit 0, within seconds |
| 9 | the screen shows `Interrupted · What should Claude do instead?` and the prompt line; `agent get` still finds n12-a: one Ctrl-C does not quit Claude Code |
| 10 | the last line reads `… <you>  interrupt → n12-a (done)`: your pane's agent name, or `owner` from a plain shell pane |
| 11 | `nothing to interrupt: n12-a is idle` (or `done`), exit 3, and no key reaches the seat |
| 12 | everything closes; remove `$W` |

C, the interrupt that leaves its prompt behind (design 23.11), on the same scratch fleet before step 12: send a prompt and interrupt it within 3 s, while the seat is still thinking, then look at its input and try to quit it.

```bash
$H up --fleet "$F"
$H send --as owner n12-a "Using the Bash tool, run: sleep 60, then reply DONE." --fleet "$F"; sleep 2   # 13
$H interrupt n12-a --fleet "$F"; echo "exit $?"                                        # 14
herdr agent read n12-a --lines 12                                                      # 15
$H send --as owner n12-a "reply PONG" --fleet "$F"                                    # 16
```

| Step | Expected |
| --- | --- |
| 14 | `interrupted n12-a (done); cleared the interrupted prompt from its input` (exit 0) when Claude Code put the prompt back; plain `interrupted n12-a (done)` when the Ctrl-C landed during the tool call |
| 15 | the prompt line between the two rules is a bare `❯` |
| 16 | `sent to n12-a (idle), …`: the seat answers PONG alone, with nothing of the earlier prompt in front |

To see the guard itself: type some text into n12-a's input by hand, click back into your own pane, and run `$H send --as owner n12-a "reply PONG"` and `$H down n12-a`: the first prints `queued in herdr-fleet for n12-a (idle, text in its input (P); press Ctrl-U there to clear it) …`, the second `n12-a: refused - text in its input (P); press Ctrl-U there to clear it` (exit 3), and nothing reaches the seat. Press Ctrl-U in its pane, and both go through.

Also worth one look, on the same scratch fleet: repeat 1 and 2, then run `down` from the **Fleet: manage** popup instead of step 3. The popup presses nothing: after 20 s it reports `n12-a: failed - did not exit within 20000 ms after /exit; the pane shows a dialog: "…"; answer it by hand`, and the owner answers the dialog in the pane.

## Live smoke test for 0.11.0 (to run by hand)

Not run yet. For the owner, inside herdr, on a fleet that is up (Fablyn or Winner Chronicles), with the working tree's copy through `HERDR_FLEET_BIN` or after installing 0.11.0. `pane rename` and `agent focus` send nothing to any agent, so this is safe while seats work.

```bash
cd <the fleet's repository>
herdr-fleet doctor | grep "pane names"                 # 1
herdr-fleet status                                     # 2
herdr-fleet doctor | grep "pane names"                 # 3
herdr-fleet go <the orchestrator's seat name>          # 4 from a plain shell pane, not a seat
herdr-fleet go nobody; echo "exit $?"                  # 5
```

Then 6: open the plugin menu and run **Fleet: go to a seat**; pick a seat by its number. 7: open it again and press Enter. 8: in **Fleet: manage**, pick one seat, then `go`; then pick `a` and `go`.

| Step | Expected |
| --- | --- |
| 1 | `info pane names: N seats unnamed (…); run herdr-fleet status` while the borders still say the agent kind (or `ok …` if they were named by hand as the seats) |
| 2 | the status table as before, with no `panes:` line; every seat pane's border now shows its seat name (or its `label`), the watcher strip still says `Fleet watch`, and a contact's pane keeps its own border |
| 3 | `ok    pane names: every live seat's pane is named` |
| 4 | `focused <seat> (<pane>, <state>)` and the UI moves to that seat's pane; a `done` badge on it clears |
| 5 | `herdr-fleet: usage: not a seat, contact or live agent: nobody`, exit 2, the focus stays |
| 6 | a small popup lists the seats with state and pane, then the contacts as `c1`, …; after the number it closes and the focus is on that seat's pane, not on the popup |
| 7 | the popup closes and nothing moves |
| 8 | one seat: the popup closes on that seat with no question; `a`: `go needs one seat`, and the action is asked again (`q` closes) |

Also worth one look, on a scratch seat only (for example the one-seat Haiku fleet of the 0.10.0 test): close its pane by hand, run `herdr-fleet restart <seat>` and see the new pane named by the restart itself (`panes: named 1 (<seat>)`); then `herdr-fleet down <seat>` leaves the pane at a shell with its border back to unnamed.

## Live check for 0.13.0 (to run by hand)

Not run yet. For the owner, on a fleet that is up with its watcher strip (Fablyn or Winner Chronicles), after installing 0.13.0: the strip runs the installed plugin's own copy, so `HERDR_FLEET_BIN` does not reach it, and a strip opened before the install keeps running the old code (and the old 12% height) until it is closed and `herdr-fleet up` opens a new one. To try the working tree first, close the strip and run `python3 <clone>/bin/herdr-fleet watch` in a plain shell pane in the fleet's repository instead. The board only reads, focuses and zooms its own pane, so this is safe while seats work; still, press nothing in a seat's pane.

1. Look at the strip without touching it. It shows the board, not a log: a header (`<fleet> · watching … · owner: …`), one row per seat with its state and age, a working seat's task, a blocked seat's dialog line in quotes, queues, debts and `ctx` sizes, and the last event and the keys in the footer. Within 5 s of a seat changing state its row follows.
2. Click into the strip and press `?`: the footer shows the keys for 5 s, then the last event again.
3. Press `2`: the second seat's page opens inside the strip (its task or dialog, queue, debts, recent entries). Press ↓ until a message is selected and Enter: its body, as stored; ↓ and PageDown scroll it. Press Esc twice: the board again.
4. Press `z`: the strip fills the tab; `z` again: it is back at its size. Click a seat's row: its page; click inside the page: back.
5. Press ↓ to select a seat and `g`: the focus moves to that seat's pane, as `herdr-fleet go <seat>` would, and `tail -2 .herdr/watch.log` in a shell ends with `focused <seat> (<pane>, <state>)`.
6. Click into the strip again and press `l`: the last lines of `.herdr/watch.log`; any key returns to the board. Press `q`: the footer says `Ctrl-C stops the watcher`, and the watcher keeps running.
7. With more seats than rows (Winner Chronicles' six fit the 0.2 strip's eight rows; lower `[watch] ratio` to 0.12 to see it), the seats are packed two to a row; make the herdr window narrower than 120 columns and the board shows the most urgent seats and `+N more`.

| Step | Expected |
| --- | --- |
| 1 | no scrolling lines; `ls .herdr/watch.log` exists and `git status` does not show it; the strip is about a fifth of the tab |
| 2 | `Enter or 1-9 open a seat · ↑↓ select · o owner · g go to its pane · a ack · d deliver now · z zoom · l log · Esc back · Ctrl-C stops the watcher` |
| 3 | the page and then the body; the body's first line reads `#id · HH:MM · sender → seat · state · size`; nothing reaches the seat (`herdr-fleet log --last 3` shows nothing new) |
| 4 | the strip zooms and comes back; the click opens the page and the next click closes it (if clicks do nothing, herdr is not passing mouse events to the pane: note it) |
| 5 | the seat's pane has the focus; its `done` badge clears; no prompt and no key reached it |
| 6 | the log view, then the board; `herdr-fleet status` still says `watcher: running (pid N, pane P, since …)` |
| 7 | as described; nothing wraps |

`a` (ack), `d` (deliver now) and `p` (screen) change the message log, deliver messages or read a screen; try them on a scratch fleet rather than a working one (the 0.12.0 one-seat Haiku fleet without `watch = false`, so `up` opens its strip): queue a message to the idle seat with `send --as owner --queue n12-a "reply PONG"`, click into the strip, press `1` for its page and `d`, and the seat gets it at once (`delivered 1 to n12-a` in the footer, no `idle; delivering` line before it); press `p`: its last twelve screen lines appear under `screen`. Then `send --as owner n12-a "please answer"` (n12-a now owes owner), select the debt on its page with ↓, press `a` (the footer asks `ack #… (n12-a owes owner …)? y/n`) and `y`: `herdr-fleet log --last 1` reads `<time>  n12-a → owner  ack #…  acked by owner: from the board`, and the debt leaves the page.
