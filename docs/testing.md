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
| Cross-fleet ownership, caller/build identity and launcher provenance | `test_section31.py` |
| Daily timers, calendar/fallback and durable queue recovery | `test_section30.py` |
| Starter examples and combined seat/contact capacity | `test_section27.py` |
| Claude/Codex transcript usage, windows and cache | `test_context.py`, `test_section29.py` |
| Git/permissions and command entrypoints | `test_worktrees.py`, `test_permissions.py`, `test_status_doctor.py`, `test_manifest.py`, `test_cli_subprocess.py` |

Section 27's regression enables the generated layout and top-level disabled watcher together, then loads the fleet without warnings. Its board cases use three seats plus five contacts at full, packed and narrow sizes, count hidden rows, preserve contact styling, map clicks to packed halves, open the contact page through the fake screen, and reach hidden contacts through page navigation.

Section 29's synthetic rollout fixture exercises both `status --context` and the real board cache/model/rendering and seat-page header. Its reported last-request total is 123000, effective window 200000 and lifetime total 9000000: the result must be `123k/200k`, with neither lifetime tokens nor double-counted cache/reasoning tokens. Reader cases cover malformed/partial data, null info, valid zero, invalid windows, source/id validation, root precedence and glob escaping, duplicate files, split lines, and the 4 MiB/256 KiB read bound. Cache cases cover provider/root/session isolation, unchanged files, compaction/window changes, missing-file retry and recovery from a failed read even with unchanged file metadata. Layout cases preserve narrow/packed selection and reserve 16 columns only when a visible row has a window. Existing Claude cases remain required.

Use temporary source roots (`HERDR_FLEET_CLAUDE_PROJECTS` and `HERDR_FLEET_CODEX_SESSIONS`) for these tests. The suite checks the exact public usage dictionary, absence of transcript content/id/path in output, unchanged source bytes/modification times, no transcript access from status without `--context`, and no additional Herdr calls for context. Do not copy a live rollout into a fixture. A fake fixture cannot verify compatibility with every agent version; any authorized live check should compare only numeric usage/window fields and report a generic outcome without publishing live identities, paths, prompts or readings. A missing window is unavailable data, not a reason to assume a model limit.

Section 30 exercises the public timers CLI/parser, offline zero-write dry-run, the real watcher delivery seam, TTL on ordinary pull/hook/kickoff/send paths, metadata-only ownership checks, body snapshotting, crash recovery before file/after file/after logging, reordered send/delivery audit, corruption, concurrent schedulers and lock order. Both calendar backends receive the same DST/fold/gap/non-hour-zone checks; forcing the native backend on Python 3.9/3.12 simulates the fallback and is not an actual Python 3.8 run. Active schedules beyond the former small-cache capacity must reuse calendar batches on the next cycle. Test timer/history-bearing log-mode reload, removal of the last timer preserving unrelated mail, and pristine no-timer behavior separately.

Section 31 exercises the real send/destination inbox/watcher delivery seams with two synthetic fleets. Check alias canonicalization, one destination pending file, source history not counting as delivery, immediate pull of deferred FYIs, ordinary state/input holds, TTL and terminal receipt propagation, exact replies/acknowledgements (including local owner replies settling origin debt without routing owner mail), partial-write recovery, short-id collisions across both trees, concurrent readers/watchers/reverse routes, and legacy source queues without migration. Dry-run must leave both trees byte-identical. Move the source configuration to a valid custom filename with no conventional source fleet file: explicit `--fleet` sends through both a contact alias and a dynamic agent must queue locally with an origin-eligibility reason and leave the destination unchanged. Normal conventional-source routing remains covered separately. Caller tests distinguish a verified shell from missing/failed identity and retain clean JSON/count stdout plus silent hooks. Build tests cover old locks, missing Git, cached checkout identity and current/stale/unknown comparisons. Launcher subprocess tests isolate both PATH and `HERDR_BIN_PATH` with fake registry executables, use marker-writing development files to detect any implicit fallback, and check exact plugin selection, registry exit codes, malformed/missing registrations/executables, explicit override/dev behavior, untouched argument arrays and hook silence. Output policy fixtures separate additive identity headings only at their assertions; the raw CLI streams and safety call logs remain intact.

Historical release inventories, live observations and their limitations are in [docs/design.md](design.md), particularly sections 12 and 17–31. They record what was run at the time; they are not current procedures or proof that a new build was smoke-tested.

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

For fleet-file reload, edit a scratch seat's `label` and observe it on the board within a refresh (about 5 s). A malformed edit should keep the last good board and show a red error until repaired. An edit does not reload Python code or rearrange panes. `--log` mode reloads when the watcher has timers or timer history; a history-free watcher that has never had timers retains its no-reload behavior and needs a restart for the first timer addition. Restarts, top-band geometry, interrupt cleanup and exit-confirmation edge cases have focused fake coverage; exercise them live only when the change calls for it and in this scratch fleet, with the owner handling dialogs.

### Daily timer acceptance in the scratch fleet

The designer or owner runs this only after approving a cheap synthetic scratch seat. Stop any manual scratch watcher with Ctrl-C in its own pane before writing/enabling the timer and keep it stopped through the dry-run review. Dry-run does not pause another running watcher; a timer-aware watcher could reload and enqueue first. Disabled timers project no upcoming fires, so review the enabled candidate only while the watcher is stopped before intentionally activating it. Use the `fleet_smoke` function above: it selects the checkout explicitly, so no global installation is needed and a previously registered plugin cannot silently supply different code. Record `python3 "$FLEET_CHECKOUT/bin/herdr-fleet" --version` and `fleet_smoke timers --help`; this candidate is not an installed release. For an approved deployment later, use the README's install/link instructions and restart the actual watcher process. Never use a production job's request file or change its coordinator for this test.

Generate one timer only in the existing disposable fleet. The script refuses to append a second timer set. Its UTC schedule is due in the current minute, starts a minute earlier, has two minutes of grace and a three-minute cutoff. Run inspection promptly; regenerate a fresh scratch fleet if the window has already passed rather than deleting occurrence history.

```bash
python3 - "$FLEET_SMOKE_FILE" "$FLEET_SMOKE_NAME" <<'PY'
import datetime
import json
from pathlib import Path
import sys
fleet, seat = Path(sys.argv[1]), sys.argv[2]
text = fleet.read_text()
if "[[timers]]" in text:
    sys.exit("Use a fresh scratch fleet; do not duplicate or erase timer history")
now = datetime.datetime.now(datetime.timezone.utc).replace(second=0, microsecond=0)
body = fleet.parent / "timer-check.md"
body.write_text("Synthetic timer acceptance: reply TIMER READY once. Run no tools or jobs.\n")
values = dict(name="scratch-check", to=seat, file=str(body), at=now.strftime("%H:%M"), tz="UTC",
              starts_at=(now-datetime.timedelta(minutes=1)).isoformat(),
              until=(now+datetime.timedelta(minutes=3)).isoformat(), grace="2m")
with fleet.open("a") as handle:
    handle.write("\n[[timers]]\n")
    for key, value in values.items():
        handle.write("%s = %s\n" % (key, json.dumps(value)))
PY
fleet_smoke timers --dry-run --json
fleet_smoke timers scratch-check --next 3
fleet_smoke watch --once
fleet_smoke timers scratch-check --json
fleet_smoke log --json
fleet_smoke log --owed
fleet_smoke watch --once
```

Record the chosen message id, scheduled UTC/local time and deadline. An idle/done seat should receive one owner request with timer metadata, then owe its reply to owner; the repeated pass must not create or submit it again. Inspect only synthetic request/log data. Timer decisions must not copy the body into the journal, and decision entries must not create reply debt. The body file should stay unchanged. A submission marked unconfirmed must not be resubmitted automatically; the fake tests cover this outcome deterministically, so do not manufacture a live failure by interrupting a seat.

For hold/expiry acceptance, use a new scratch timer name and a fresh short window. The owner types a draft into the idle scratch seat without submitting it, then runs `watch --once` from the controller. Expect `timer NAME waiting: typing`, one pending id and its absolute deadline. Keep the draft until the displayed deadline passes, run another pass, and verify that mail moved to `cur/` with an expired audit entry and no timer submission. The owner clears the input manually. Do not inject keys, answer a permission dialog automatically or use `--now` to bypass the hold.

For downtime/catch-up, stop only the scratch watcher, allow a scheduled minute to pass within grace, and restart the selected checkout watcher. It should enqueue only the latest eligible occurrence. Restart once more and verify the same occurrence is not submitted again. Test beyond-grace skip and cutoff using new scratch identities/windows; record skipped/stopped outcomes and no backlog. These short synthetic windows test the same deadline calculation as the default six-hour grace without waiting six hours.

For reload, start a timer-bearing scratch watcher with `--log`, then disable/remove its timer or change its local target in that scratch file. Valid edits must reload; pending owned mail must expire, and unrelated queued mail must remain. Removing the last timer must still reconcile its journal, with no calendar work for removed timers. A malformed edit retains the last valid configuration and reports an error until repaired. Repeat relevant checks in board mode when UI behavior changed. A history-free log watcher requires a restart for its first timer addition, and every Python code update requires a process restart. Stop the manual watcher before returning to cleanup.

Record exactly which live cases ran and which were only covered by fake tests. Passing synthetic acceptance does not install code, approve production configuration or prove exactly-once external execution. Migration must disable the previous trigger and choose a non-overlapping explicit `starts_at`; the requesting coordinator owns that configuration and production acceptance.

### Cleanup and evidence

Stop a manual watcher with Ctrl-C in its own pane. Before cleanup, confirm the scratch seat is idle/done with empty input; the owner clears input and answers any dialog manually. From the controller:

```bash
fleet_smoke down --wait 60000 --close-panes --close-workspace
herdr agent list
herdr workspace list
```

Verify only the scratch seat and scratch workspace disappeared, the controller stayed alive, and a recorded scratch watcher stopped and released its lock. A refused or failed shutdown needs inspection before another attempt. Keep the temporary directory and logs until review; remove only that verified scratch directory afterwards. If you changed plugin registration, restore the previously recorded installation/link deliberately; running old plugin panes remain separate processes.

Report the code revision or working-tree state, Herdr/Python/agent versions, model chosen, terminal size, exact commands, observed outcomes, files retained, and everything not run. Separate automated evidence from live evidence. Never label a proposed procedure as a passed check.

### Cross-fleet ownership and launch provenance acceptance

The designer or owner can extend the disposable setup above to two temporary repositories. Use unique live names in each, declare each exact name as a seat in its own fleet, and add a contact alias only to the source scratch fleet with `match = { name = "<destination live name>" }`. Keep watchers stopped while setting up; inspect both fleet paths and the live records' cwd before sending. A contact alias is not a destination seat. The commands below use shell variables assigned to these verified scratch paths/names: `FLEET_SOURCE_FILE`, `FLEET_DESTINATION_FILE`, `FLEET_DESTINATION_NAME`, and `FLEET_CONTACT_ALIAS`. They are manual acceptance steps, not automation against owner fleets.

```bash
python3 "$FLEET_CHECKOUT/bin/herdr-fleet" send --as owner --queue --fyi \
    --fleet "$FLEET_SOURCE_FILE" "$FLEET_CONTACT_ALIAS" "FYI: synthetic mailbox acceptance"
python3 "$FLEET_CHECKOUT/bin/herdr-fleet" inbox --as "$FLEET_DESTINATION_NAME" \
    --fleet "$FLEET_DESTINATION_FILE" --count --json
python3 "$FLEET_CHECKOUT/bin/herdr-fleet" inbox --as "$FLEET_DESTINATION_NAME" \
    --fleet "$FLEET_DESTINATION_FILE" --wait --timeout 3000 --full
python3 "$FLEET_CHECKOUT/bin/herdr-fleet" log --json --fleet "$FLEET_SOURCE_FILE"
python3 "$FLEET_CHECKOUT/bin/herdr-fleet" log --json --fleet "$FLEET_DESTINATION_FILE"
```

Expect one destination `new/` file and no source pending alias/canonical copy before the read. Human output must show explicit sender, selected source and holding destination, including alias to canonical name. The source `cur/` receipt is nonpending and produces no debt until delivery. The queued FYI must be visible to an already-running destination wait without a native wake; verify with the wait started in a second scratch controller shell before sending another unique report. After the pull, expect one delivered receipt per log and no native prompt. For native acceptance, check idle/done and empty input, then send a plain synthetic request through the alias; inspect both logs for one delivery. Enter harmless draft text by hand in the destination input and verify a plain send stays queued; clear it manually before intentional delivery. A blocked dialog is answered only by the owner. Never blindly repeat an unconfirmed prompt.

From the selected scratch namespace, try `send --reply-to` with an absent synthetic full/short id; expect exit 2, that namespace's path, `--fleet PATH`/plain-send guidance, and unchanged queues/logs. Exercise explicit sender identity separately from a missing caller environment; inspect JSON/count stdout independently from stderr. Preserve any pre-existing source queues and history; this test does not migrate them.

Start the explicit checkout watcher in its own scratch shell, inspect `status --json` for `cli`, `watcher.launch` and `watcher.comparison`, and compare with the actual launch command. A known different build must be stale; an older lock or unavailable Git must remain unknown. Stop only that watcher with Ctrl-C before relaunching selected code; keep coding-agent sessions running. A fleet-file reload cannot replace Python code.

Generate a launcher only into a separate temporary directory with `install-cli --dir DIR`. Test it with an isolated fake registry command before any real installation. Simulate denied access, invalid JSON and a missing installation, and verify nonzero exit plus stderr provenance for normal calls, empty successful hook results, and no execution of a development marker file. Check a valid exact enabled registration and an explicit override. Refreshing the actual user launcher after release is an explicit deployment step; merely updating plugin files does not replace an older generated launcher. Remove only scratch resources after recording results, using the guarded cleanup above. Report actual observations and any untested paths; these instructions alone provide no live evidence.
