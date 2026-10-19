# Prior art (corrected survey, 19 Oct 2026)

The four-plugin survey in `docs/requirements.md` was far too small. The [awesome-herdr](https://github.com/yigitkonur/awesome-herdr) index lists 107 projects under "multi-agent fleets and supervisors" alone. herdr itself has nothing project-level: no team file, no template, nothing in `config.toml` (checked against `herdr --default-config` 0.9.4), and no plugin installed here. What follows is what comes closest, verified by reading each project's source or docs, and what we borrowed.

## Closest matches

| Project | Per-repo file | Names agents with `agent start` | Restart / swap | Shape | What we took |
| --- | --- | --- | --- | --- | --- |
| [herdup](https://github.com/jellyfishmobile/herdup) | `.herdr/team.toml`, `[[pane]]` with role, cli, briefing | yes | no | Rust desktop app plus `launcher-cli`; works only in its own named herdr session; licence TBD | the `.herdr/` directory, "never brief a pane that is waiting on you", "brief the coordinator last with the roster", delegating readiness to `agent start` and the blocked check to `agent prompt` |
| [herdr-agent-profiles](https://github.com/mikeyobrien/herdr-agent-profiles) | no, global `profiles.toml` in the plugin config dir | yes, one profile at a time | no | real herdr plugin, Python, popup picker pane, MIT | the plugin shape: action plus popup pane, no build step, opaque argv per profile |
| [schmerdr](https://github.com/kylescudder/schmerdr) | one shell template per project in `~/.config/schmerdr` | yes, `start_agent <kind> [name]` | no | shell DSL over the herdr CLI | retrying `agent start` while the fresh pane reports `agent_pane_busy`; passing `--name` to Claude so the session title matches |
| [herdr-workspace-save](https://github.com/chandrasekharan98/herdr-workspace-save) | no | n/a | resumes saved agent sessions with per-kind resume commands | herdr plugin, Python, MIT | the warning that plugin stdout is shown nowhere, so results go to `herdr notification show` or an overlay pane; the per-kind command table pattern |
| [@chankov/agent-fleet](https://www.npmjs.com/package/@chankov/agent-fleet) | `.pi/agents/teams.yaml` and `peers.yaml` (runner `pi` or `claude-code`, persona, model, env file) | spawns peers into herdr panes | new run per start | pi-only dispatcher hub with a coms layer; herdr is its control plane | the peer declaration schema as a reference; not vendor-neutral, so not reusable |
| [agent-fleet-plugins](https://github.com/nakamori-naoya/agent-fleet-plugins) | `fleet.yml` with role refs and view profiles | one agent per pane | new run per start | Claude Code and Codex plugin, Japanese docs, role catalog and dispatch | nothing; far heavier than the owner's loop |
| [herdr-orchestrator](https://github.com/kylezk777/herdr-orchestrator) | project `profiles.toml` | `orch agent start <role> --profile` | no | Rust, file-based task board | `agent prompt --wait --until working` as turn-start evidence |
| [herdr-dev-team](https://github.com/0-CYBERDYNE-SYSTEMS-0/herdr-dev-team) | global `team.toml` roster under `~/.agents/skills` | the lead starts seats | no | a skill, not a plugin; seven fixed roles | nothing |
| [herdr-plus](https://github.com/cloudmanic/herdr-plus), [herdr-spreader](https://github.com/yuk1ty/herdr-spreader), [tendr](https://github.com/tombell/tendr) | global templates | no | no | layout launchers | herdr-plus's build script that prefers a local toolchain and falls back to a prebuilt binary, if we ever move to Go |

Two unrelated repositories are already called `herdr-fleet`: [chetanunadkat-lang/herdr-fleet](https://github.com/chetanunadkat-lang/herdr-fleet) (a Claude Code plugin with an `hm` CLI) and [Northern-Lighthouse/herdr-fleet](https://github.com/Northern-Lighthouse/herdr-fleet) (a fleet of herdr machines over Tailscale).

## What none of them do

Declare a vendor-neutral team of named agents in a file committed with the repository, bring it up idempotently, restart one seat with a "where things stand" note, and swap a seat's kind or model by editing the file. That gap is what `docs/design.md` fills.

## herdr facts added by this survey

All facts in `docs/requirements.md` hold against the 0.9.4 binary and the raw 0.9.4 docs (identical to 0.9.5 for the plugin, agent-automation, CLI and agents pages). Additions that shaped the design:

- `herdr plugin action invoke <id>` passes no arguments; an action gets only the context environment. Anything that names a seat must go through a pane or the CLI.
- No herdr-native way to stop an agent. `pane release-agent` is for state integrations. `agent rename --clear` exists.
- Only `agent start` answers `--help`; the other groups print usage when run without a subcommand.
- `agent list`, `pane get` and `pane list` carry a `name` only for agents started by name or renamed, and no pane label field. `workspace list` does carry `label`.
- `pane process-info` returns the foreground processes with `name`, `argv`, `cmdline` and `cwd`, which gives both "is the shell back" and "which model args is this agent running".
- `herdr notification show <title> [--body TEXT] [--sound none|done|request]` exists.
- herdr resumes agents into their native sessions after a server restart when `[session] resume_agents_on_restore` is on; names do not survive, so `up` adopts an unnamed agent of the right kind in a recorded pane.
- Plugin stdout and stderr are shown nowhere on screen; `herdr plugin log list` keeps them.
- The system `python3` on this Mac is 3.9.6 without `tomllib`; Homebrew 3.12 and 3.14 are installed. The herdr Claude and Codex integration hooks already require python3, so it is a de facto herdr dependency.
- The emberline "Team configuration" table exists only on branch `origin/docs/team-config` (commit 126d66e6), not on `main`.
