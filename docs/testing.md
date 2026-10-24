# Testing herdr-fleet

Run automated checks against the fake Herdr before handing back a change. Live checks are separate, manual work for the designer or owner in a disposable fleet. A passing fake test is not evidence that a terminal, agent version or live Herdr integration behaved the same way.

## Automated checks

From the checkout root, portable commands are:

```bash
python3 --version
python3 -m unittest discover -s tests -v
python3 -m compileall -q herdr_fleet bin/herdr-fleet
```

Python 3.8 or newer is supported. Exercise both a Python below 3.11 (vendored `tomli`) and one at or above 3.11 (`tomllib`) when available. For a handover, exercise both parsing paths when these interpreters are available (or use their equivalent absolute paths):

```bash
python3.9 -m unittest discover -s tests -v
python3.12 -m unittest discover -s tests -v
python3 -m compileall -q herdr_fleet bin/herdr-fleet
```

Quote the actual test count, failures/skips, interpreter and command from each run. Compilation is a separate exit-status check, not another test count. Test duration depends on the machine and subprocess workload; use the measured result rather than a fixed estimate.

For a focused change, run the relevant file first, then the required full suites:

```bash
python3 -m unittest discover -s tests -p test_section27.py -v
```

The tests inject `tests/fake_herdr.py` as their runner or executable. Never replace it with a live `HERDR_BIN_PATH` to turn the suite into a smoke test. Tests that need Git create isolated temporary repositories; they do not fetch or prune the owner's repositories. They are skipped when Git is unavailable. Transcript fixtures live under a temporary HOME rather than the owner's agent history.

### Fixtures and safety checks

Most tests run `herdr_fleet.cli.main` with the fake runner and an instant clock. CLI and manifest tests also exercise real subprocess entrypoints against the fake. `tests/fake_herdr.py` stores a JSON world, records calls, and provides state transitions, errors, screens, focus, geometry, dialogs and interrupt outcomes. `tests/support.py` validates call logs against the invariants in [design section 7](design.md#7-safety-invariants), including later amendments. Extend that harness when introducing a new command or permitted path.

Use the existing helpers rather than live panes:

- `fake_herdr.focus`, `type_in`, `show_dialog` and `set_screen` exercise input holds. An unfocused pane with text, or no prompt line, holds delivery too.
- `exit_dialog_on_quit`, `show_exit_dialog`, `interrupt_results` and `restore_prompt_on_interrupt` exercise the restricted agent-key paths. Ctrl-U is allowed once only after an interrupt and a read showing restored input text. The popup, watcher and dry runs send no agent keys.
- `wait_results` and `status_after_wait` script server waits and races around debounce. `StopClock` and inline waiter fixtures avoid real timeout delays; thread tests cover the paths that need real threads.
- `tui.FakeScreen(keys, on_key=self.log_key)` records draws and owner keys/clicks. Patch `watch.is_terminal` and `tui.open_screen` as `tests/test_section24.py` and `tests/test_section25.py` do. The harness checks that focus follows an authorized board key, screen reads follow the blocked-state rule or `p`, and zoom follows `z` and names the watcher's own pane explicitly. A UI-focused pane selected through `--current` is not a safe zoom target.

Tests are the executable coverage inventory. For the principal areas, start with:

| Area | Tests |
| --- | --- |
| Parsing, templates and initialization | `test_fleetfile.py`, `test_init.py`, `test_units.py` |
| Start, restart, swap, down and safety | `test_up.py`, `test_restart.py`, `test_swap_down.py`, `test_units.py` |
| Layout, adopted seats and watcher strip | `test_layout.py`, `test_grid.py`, `test_adopted.py`, `test_tile.py`, `test_watchpane.py`, `test_section26.py` |
| Messages, inbox, delivery and state notes | `test_messages.py`, `test_inbox.py`, `test_contacts.py`, `test_statenote.py`, `test_section20.py`, `test_section23.py` |
| Watcher, hooks and presence | `test_watch.py`, `test_hooks.py`, `test_section21.py` |
| Board, pages and file reload | `test_section24.py`, `test_section25.py`, `test_section26_6.py` |
| Starter examples and combined seat/contact capacity | `test_section27.py` |
| Claude/Codex transcript usage, windows and cache | `test_context.py`, `test_section29.py` |
| Git/permissions and command entrypoints | `test_worktrees.py`, `test_permissions.py`, `test_status_doctor.py`, `test_manifest.py`, `test_cli_subprocess.py` |

Section 27's regression enables the generated layout and top-level disabled watcher together, then loads the fleet without warnings. Its board cases use three seats plus five contacts at full, packed and narrow sizes, count hidden rows, preserve contact styling, map clicks to packed halves, open the contact page through the fake screen, and reach hidden contacts through page navigation.

Section 29's synthetic rollout fixture exercises both `status --context` and the real board cache/model/rendering and seat-page header. Its reported last-request total is 123000, effective window 200000 and lifetime total 9000000: the result must be `123k/200k`, with neither lifetime tokens nor double-counted cache/reasoning tokens. Reader cases cover malformed/partial data, null info, valid zero, invalid windows, source/id validation, root precedence and glob escaping, duplicate files, split lines, and the 4 MiB/256 KiB read bound. Cache cases cover provider/root/session isolation, unchanged files, compaction/window changes, missing-file retry and recovery from a failed read even with unchanged file metadata. Layout cases preserve narrow/packed selection and reserve 16 columns only when a visible row has a window. Existing Claude cases remain required.

Use temporary source roots (`HERDR_FLEET_CLAUDE_PROJECTS` and `HERDR_FLEET_CODEX_SESSIONS`) for these tests. The suite checks the exact public usage dictionary, absence of transcript content/id/path in output, unchanged source bytes/modification times, no transcript access from status without `--context`, and no additional Herdr calls for context. Do not copy a live rollout into a fixture. A fake fixture cannot verify compatibility with every agent version; any authorized live check should compare only numeric usage/window fields and report a generic outcome without publishing live identities, paths, prompts or readings. A missing window is unavailable data, not a reason to assume a model limit.

Historical release inventories, live observations and their limitations are in [docs/design.md](design.md), particularly sections 12 and 17–29. They record what was run at the time; they are not current procedures or proof that a new build was smoke-tested.

## Manual scratch-fleet check

The designer or owner runs this workflow inside Herdr. A builder prohibited from live mutations stops after the automated checks and temporary-file setup validation, and reports the live steps as unrun. Use a plain shell pane as the controller, not an agent that might mistake scratch messages for a production brief. No step below requires a real working fleet, automatic dialog answers, raw terminal text injection or raw agent-key commands.

### Select the code under test

In the checkout root, record the absolute path and use a shell function for multiword commands. This works in bash and zsh; putting `python3 bin/herdr-fleet` in one string and executing that string does not work in both shells.

```bash
export FLEET_CHECKOUT="$PWD"
test "${HERDR_ENV:-}" = 1
python3 "$FLEET_CHECKOUT/bin/herdr-fleet" --version
herdr --version
herdr agent start --help
herdr integration status
herdr plugin list --json
```

Stop if the inside-Herdr check fails. Install/authenticate Claude Code and its Herdr integration before starting the scratch seat, as described in the README. Select a currently available inexpensive Claude model with access on your account; there is no fixed model id required by this project.

The CLI commands below execute the checkout explicitly. A strip opened by `up` executes the enabled plugin registration, not `HERDR_FLEET_BIN`. For a board test of the checkout, first inspect the registry and deliberately link this checkout with `herdr plugin link "$FLEET_CHECKOUT"`, recording the previous registration so it can be restored. Linking affects future plugin invocations for this user. If changing registration is unsuitable, keep it as it is and use the manual checkout watcher below instead. An existing watcher process must be stopped and restarted to pick up changed Python code; see Herdr's [plugin lifecycle](https://herdr.dev/docs/plugins/#install-and-link).

### Generate an isolated setup

Create a temporary directory and run the real initializer there. This step alone needs no live agent and can be used to validate setup without a Herdr server.

```bash
export FLEET_SMOKE_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/fleet-smoke.XXXXXX")
export FLEET_SMOKE_FILE="$FLEET_SMOKE_ROOT/.herdr/fleet.toml"
python3 "$FLEET_CHECKOUT/bin/herdr-fleet" init "$FLEET_SMOKE_ROOT"
python3 "$FLEET_CHECKOUT/bin/herdr-fleet" init "$FLEET_SMOKE_ROOT"
```

The first run creates the starter files; the second keeps them. To test the commented alternatives, enable only the `[layout]`, chain `mode`, `direction`, `ratio`, and top-level `watch = false` examples in this generated file. The disabled watcher must stay before the first table; leave `[watch]` commented. Load it directly to verify `mode == "chain"`, `watch == False`, and an empty warnings list:

```bash
python3 - "$FLEET_CHECKOUT" "$FLEET_SMOKE_FILE" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from herdr_fleet.fleetfile import load_fleet
fleet = load_fleet(sys.argv[2])
assert fleet.mode == "chain"
assert fleet.watch is False
assert fleet.warnings == [], fleet.warnings
print("starter layout loads with watcher disabled and no warnings")
PY
```

For the live check, replace only this scratch fleet file with a unique workspace and seat. The script uses JSON escaping for valid TOML basic strings and passes the currently selected model as data. Do not select any name already in `herdr agent list` or any workspace label already in use.

```bash
printf 'Current inexpensive Claude model id: '
read -r FLEET_SMOKE_MODEL
export FLEET_SMOKE_NAME=$(python3 -c 'import secrets; print("fleet-smoke-" + secrets.token_hex(3))')
python3 - "$FLEET_SMOKE_FILE" "$FLEET_SMOKE_NAME" "$FLEET_SMOKE_MODEL" <<'PY'
import json
import sys
path, name, model = sys.argv[1:]
if not model.strip():
    sys.exit("Choose a current inexpensive model before starting a live seat")
with open(path, "w", encoding="utf-8") as handle:
    handle.write('workspace = %s\nwatch = false\n\n[layout]\nmode = "grid"\n\n' % json.dumps(name))
    handle.write('[[agents]]\nname = %s\nkind = "claude"\nargs = %s\n' %
                 (json.dumps(name), json.dumps(["--model", model])))
    handle.write('kickoff_text = %s\n' % json.dumps(
        "[from herdr-fleet] Scratch validation ({reason}). Reply READY, then wait. "
        "Run no tools and send no messages. {note}"))
PY
fleet_smoke() {
    python3 "$FLEET_CHECKOUT/bin/herdr-fleet" "$@" --fleet "$FLEET_SMOKE_FILE"
}
herdr agent list
herdr workspace list
fleet_smoke doctor
fleet_smoke up --dry-run
```

Review the dry-run targets and root. The new workspace must be the unique scratch label and the only seat the unique scratch name. The initial watcher is disabled to make message and lifecycle checks deterministic. Fix diagnostics before proceeding. The seat's working directory is the scratch root, so a new-directory trust prompt is possible.

### Start, delivery and lifecycle

```bash
fleet_smoke up
herdr agent get "$FLEET_SMOKE_NAME"
fleet_smoke peek "$FLEET_SMOKE_NAME" --lines 12
```

If startup is blocked, the owner answers the trust/login/permission dialog manually in the scratch pane. A builder does not answer it. Once it is `idle` or `done` and the input is empty, `fleet_smoke kickoff "$FLEET_SMOKE_NAME"` sends the held briefing. A working, blocked or unknown seat must not be assumed ready merely because a previous command succeeded.

At each following step, check `herdr agent get "$FLEET_SMOKE_NAME"` and the empty input again before delivery or lifecycle operations. Expected outcomes are assertions to verify, not results already observed:

```bash
fleet_smoke send --as owner --fyi "$FLEET_SMOKE_NAME" "FYI: scratch delivery check"
fleet_smoke inbox --as "$FLEET_SMOKE_NAME"
fleet_smoke deliver "$FLEET_SMOKE_NAME"
```

The FYI-only message should remain queued until the named `deliver` sends it. That explicit delivery can start a new turn even though it is FYI. Once the seat is idle/done again:

```bash
fleet_smoke send --as owner --queue "$FLEET_SMOKE_NAME" "Reply PONG, then wait; run no tools."
fleet_smoke inbox --as "$FLEET_SMOKE_NAME" --count
fleet_smoke deliver "$FLEET_SMOKE_NAME" --dry-run
fleet_smoke deliver "$FLEET_SMOKE_NAME"
fleet_smoke log --owed
fleet_smoke inbox --as owner --history "$FLEET_SMOKE_NAME"
fleet_smoke restart "$FLEET_SMOKE_NAME" --wait 60000 --note "Restart check: reply READY, then wait."
fleet_smoke status --json
```

The queued request should arrive once in one batch, and the restart should reuse the seat's pane. A successful kickoff settles older debts; an unconfirmed kickoff is not logged as successful. A stalled or timed-out delivery must not be resent automatically. Retain the output and inspect the mailbox/history before any intentional redelivery.

### Board and input hold

For a checkout board without changing the plugin registration, open another ordinary Herdr shell manually and run the following with the actual absolute paths printed above:

```bash
python3 /absolute/path/to/checkout/bin/herdr-fleet watch --fleet /absolute/path/to/scratch/.herdr/fleet.toml
```

This starts the checkout watcher in that shell. Alternatively, with the checkout deliberately linked, remove the scratch file's top-level `watch = false` and run `fleet_smoke up` to open its recorded plugin strip. Run only one watcher for this scratch fleet. A non-terminal stdout or missing curses produces the log instead of the board.

In the focused watcher pane, verify the header, scratch seat row, footer and refresh. `?` shows help; Enter opens the selected seat page; Esc returns; `l` shows the log and any key returns. `z` should zoom this watcher pane and restore it: the code explicitly names its own `HERDR_PANE_ID` or recorded watch pane, never the UI-focused pane through `--current`. `g` deliberately focuses the scratch seat; click back into the watcher manually. Click a row to open its page, and inside the page to return. Record mouse failures with terminal type and size; passing fake mouse tests does not prove the live terminal passes the same encoding.

Queue a scratch message, then use its seat page's `d` to deliver and `p` to view the screen. A page/body read should not move mailbox files; `d` may do so. To verify `a`, select an actual unsettled scratch debt, confirm the exact id in the footer, and check the resulting log acknowledgement. The combined three-seat/five-contact layouts and packed click targets are covered by automated section 27 tests; use disposable contacts if expanding a live fixture, rather than modifying working fleets.

To test the presence guard, the owner types `draft text` into the idle scratch seat's input by hand, without submitting it, then returns to the controller shell. Run a plain scratch `send`. It should queue and the watcher should hold it, including after the owner leaves that pane. Clear the draft manually with Ctrl-U in the scratch pane; do not submit it. Delivery can then proceed when the seat is idle/done. If a dialog appears, answer or close it manually; no raw key or prompt command should be used to dismiss it.

For fleet-file reload, edit a scratch seat's `label` and observe it on the board within a refresh (about 5 s). A malformed edit should keep the last good board and show a red error until repaired. An edit does not reload Python code or rearrange panes. `--log` mode does not reload the fleet file. Restarts, top-band geometry, interrupt cleanup and exit-confirmation edge cases have focused fake coverage; exercise them live only when the change calls for it and in this scratch fleet, with the owner handling dialogs.

### Cleanup and evidence

Stop a manual watcher with Ctrl-C in its own pane. Before cleanup, confirm the scratch seat is idle/done with empty input; the owner clears input and answers any dialog manually. From the controller:

```bash
fleet_smoke down --wait 60000 --close-panes --close-workspace
herdr agent list
herdr workspace list
```

Verify only the scratch seat and scratch workspace disappeared, the controller stayed alive, and a recorded scratch watcher stopped and released its lock. A refused or failed shutdown needs inspection before another attempt. Keep the temporary directory and logs until review; remove only that verified scratch directory afterwards. If you changed plugin registration, restore the previously recorded installation/link deliberately; running old plugin panes remain separate processes.

Report the code revision or working-tree state, Herdr/Python/agent versions, model chosen, terminal size, exact commands, observed outcomes, files retained, and everything not run. Separate automated evidence from live evidence. Never label a proposed procedure as a passed check.
