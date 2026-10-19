# herdr-fleet

A herdr plugin that starts and manages a team of named coding agents from a per-repository fleet file. Read `docs/design.md` first: it is the spec, and behaviour changes start there. `docs/requirements.md` holds the problem and the facts about herdr 0.9.4; `docs/survey.md` the prior art.

- Check herdr syntax against the installed binary (`herdr <group> --help`, `herdr --skill`) and https://herdr.dev/docs/plugins/.
- Never send keys or prompts to a herdr pane in state `blocked`, never answer another agent's permission prompt, and never run bare `herdr`.
- Don't push to a remote without the owner's explicit approval.

## Architecture

`bin/herdr-fleet` puts the plugin root on `sys.path` and calls `herdr_fleet.cli.main`, which parses arguments, runs the common preconditions (`context.preflight`) and dispatches to an operation in `ops.py` (up, restart, swap, kickoff, down, ui, install-cli), `checks.py` (status, drift, doctor) or `menu.py` (the popup). Operations work on a `context.Ctx` holding the herdr client, the parsed fleet file (`fleetfile.py`), the state file (`state.py`) and the plugin config, and return a `Report` that the CLI prints and maps to exit codes 0/1/2/3. `bringup.py` finds or creates the workspace and brings one seat up; `seats.py` holds every interaction with a live agent: the shell-prompt check, `start_in_pane` (with the `agent_pane_busy` retry), `safe_prompt` (the only path for text to an agent; it refuses anything not `idle` or `done`) and `quit_agent`. `herdr.py` is the only module that executes herdr, and it refuses `send-keys`, `send-text`, `pane run` and bare `herdr` before spawning anything. Live herdr facts the code relies on are in `docs/design.md` section 12; in particular, pane records carry no agent `name`, so read names through `agent get` (`Herdr.pane_agent`), never from `pane get`/`pane list`. `kickoff.py` renders templates; `notify.py` wraps `herdr notification show`. No third-party dependencies; `tomli` is vendored under `herdr_fleet/_vendor/` for Python < 3.11. Code must run on Python 3.8+: `from __future__ import annotations`, `typing.List`/`Optional`, no `match`, no `X | Y`.

## Build and test

There is no build step. Run the tests under both interpreters before handing work back:

```bash
/usr/bin/python3 -m unittest discover -s tests -v            # 3.9, vendored tomli
/opt/homebrew/bin/python3.12 -m unittest discover -s tests -v
/usr/bin/python3 -m compileall -q herdr_fleet bin/herdr-fleet
```

Tests use the fake herdr in `tests/fake_herdr.py`; never point them at a live herdr. `tests/support.py` checks every test's call log against the safety invariants of design section 7, so a new scenario is automatically a safety test. The live smoke test in `docs/testing.md` is run by hand, by the designer or the owner, never by the test suite.
