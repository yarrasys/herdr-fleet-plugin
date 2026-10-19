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

Like live herdr 0.9.4, the fake's `pane get` and `pane list` records never carry the agent `name`; only `agent get` and `agent list` do (design section 12).

### Safety invariants

`tests/support.py` checks every test's call log in `tearDown` against design section 7, so every scenario is also a safety test:

- the log never contains bare `herdr`, `agent send-keys`, `pane send-keys`, `pane send-text` or `pane run`;
- every `agent prompt` went to an agent that was `idle` or `done` at that moment (never `blocked`, `unknown` or `working`);
- every `agent start` targeted a pane at a bare shell prompt and never the caller's pane;
- every `pane close` closed a pane herdr-fleet created and that hosted no agent; every `workspace close` closed a workspace herdr-fleet created.

Specific tests cover restart refusing blocked, unknown and working agents, stalled prompts never being resent, quit polling and timeout, swap drift from process-info argv, and `down` closing only created panes.

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

Run by the designer on 19 Oct 2026 against herdr 0.9.4, from the designer's own pane, and passed. This is the procedure as run. It uses one Claude seat on the cheap Haiku model, a fleet file in a scratch directory passed with `--file`, and the seat's `cwd` set to a directory Claude Code already trusts, so the seat starts without a trust prompt. The fleet has no `workspace` key, so the seat splits from the pane the commands run in; herdr-fleet never starts into, restarts or quits that pane.

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
