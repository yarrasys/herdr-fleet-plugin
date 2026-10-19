# herdr-fleet: requirements handover

Written 19 Oct 2026 by the Fablyn orchestrator session (Claude) for the session that will design and build this plugin with the owner. Brainstorm first; nothing here is decided unless it says so.

## The problem

The owner runs a team of named coding agents in [herdr](https://herdr.dev) (0.9.4 on this machine) to build Fablyn (repo `~/work/yarrasys/projects/emberline`). There are three roles:

| Name | Role |
| --- | --- |
| `fablyn-orchestrator` | designs, reviews, runs browser and server tests, merges, deploys |
| `fablyn-dev` | builds on a branch in its own worktree |
| `fablyn-reviewer` | reviews read-only |

Each role can be held by a different agent kind and model (Claude Code, Codex, …). The owner swaps them as token budgets run out; today `fablyn-dev` moves from Codex to Claude Opus 5.5.

Today the team is defined as a markdown table: emberline `docs/agent-team.md`, section "Team configuration". An agent turns that table into herdr commands by hand:
- `herdr pane split`;
- `herdr agent start <name> --kind <kind> --pane <id> -- <args>`;
- `herdr agent rename`;
- then a kickoff message per agent with `herdr agent prompt`.

Restarting one agent (quit it, start it again in its pane, re-brief it) is also manual. That is error-prone, and slow when the owner wants to restart everything.

**No existing plugin does this** (surveyed 19 Oct). Layout plugins run a command per pane but never give agents herdr names, and names are what agents use to message each other.

| Plugin | What it does | Why it doesn't fit |
| --- | --- | --- |
| [yuk1ty/herdr-spreader](https://github.com/yuk1ty/herdr-spreader) | YAML workspace, tab and pane layouts with a command per pane (tmuxinator-style) | Rust build; config in the plugin's global config dir, or `--file` for the standalone CLI; no agent names |
| [cloudmanic/herdr-plus](https://github.com/cloudmanic/herdr-plus) | TOML "Projects" templates in a global config dir | No agent names |
| [eliasstravik/herdr-projects](https://github.com/eliasstravik/herdr-projects) | A whole task board with a Coordinator agent | Replaces the owner's workflow rather than configuring it |
| [jbaham2/herdr-plugin](https://github.com/jbaham2/herdr-plugin) | A Claude Code plugin with herdr skills and `/herd` and `/herd-layout` | A different kind of tool; its storage format is undocumented |

## What we want (draft, to brainstorm)

A herdr plugin plus a **per-repository fleet file**, checked into git, that declares a team of named agents. Any agent, Claude or Codex, can read the file and any of them can drive the plugin, so it must be vendor-neutral.

1. **Declare.** Each agent has:
   - a name;
   - a kind (`claude`, `codex`, …);
   - start arguments (model, effort, …);
   - a working directory;
   - a role or kickoff prompt (inline or a file path);
   - where it sits in the layout.
2. **Up.** Create or reuse a workspace, split panes, and start each agent with `herdr agent start <name> --kind … --pane … -- <args>`. Wait until it's ready, then send its kickoff.
3. **Restart one.** Quit the agent in its pane (only when it's idle or done, never while blocked). Start it again with the same name and pane, then send its kickoff plus a "where things stand" note.
4. **Swap.** Change a role's kind or model in the file and apply it. The role keeps its name and pane; only that agent restarts.
5. **Status.** For each fleet agent: name, kind, pane, state (`idle`/`working`/`blocked`/`done`), plus whether herdr's agent-state integrations are current (`herdr integration status`).
6. **Down.** Stop the fleet cleanly.

## Facts and constraints (herdr 0.9.4, checked 19 Oct)

- **Plugin manifest.** `herdr-plugin.toml` at the plugin root: `id`, `name`, `version`, `min_herdr_version`, optional `description` and `platforms`.
  - `[[build]]` runs on GitHub install, not on `plugin link`.
  - `[[startup]]` runs one-shot after the session restores.
  - `[[actions]]` take `id`, `title`, `contexts` and `command` (argv), and are invoked as `plugin.id.action`.
  - `[[events]]` take `on` and `command`. `[[panes]]` take `placement`: overlay, popup, split, tab or zoomed.
  - Use `HERDR_PLUGIN_CONFIG_DIR` and `HERDR_PLUGIN_STATE_DIR` for config and state. Call herdr through `HERDR_BIN_PATH` or `HERDR_SOCKET_PATH`.
  - Plugins run as the user and are not sandboxed. Docs: https://herdr.dev/docs/plugins/
  - Develop with `herdr plugin link <path>`, `herdr plugin action invoke <id>` and `herdr plugin log list`. Distribute with `herdr plugin install owner/repo`, tagging the GitHub repo `herdr-plugin` for the marketplace.
- **`herdr agent start`.**
  - The pane must be at its interactive shell prompt with nothing in the foreground.
  - It returns once the agent is ready, or `agent_not_ready` if the agent is blocked at startup (the name stays reserved).
  - Names match `[a-z][a-z0-9_-]{0,31}` and must be unique among live agents. A name is cleared when its agent exits.
  - Supported kinds: `pi`, `claude`, `codex`, `gemini`, `cursor`, `devin`, `opencode`, `copilot`, `kimi`, `amp`, `grok` and more (see `herdr agent start --help`).
- **Other agent commands:**
  - `herdr agent prompt <name> "<text>"`;
  - `herdr agent wait <name> --until done --until idle --until blocked --timeout <ms>` (milliseconds);
  - `herdr agent read <name> --source visible|recent-unwrapped`;
  - `herdr agent rename <pane> <name>`;
  - `herdr agent list` (JSON).
- **Panes and workspaces.** `herdr workspace create --cwd --label --env --focus` and `herdr pane split [--pane] --direction right|down --ratio --cwd --env`. Both return JSON containing the new ids.
- **Safety rules the owner holds (must not break):**
  - Never send keys or prompts to a `blocked` pane. It is showing a permission prompt, and Enter approves it.
  - Never answer an agent's permission prompt and never edit an agent's permission settings. The owner does that.
  - Don't run bare `herdr` (it attaches the TUI).

## First user: the Fablyn team

Something like this, as an illustration only; the format is open:

```toml
workspace = "fablyn"
cwd = "~/work/yarrasys/projects/emberline"

[[agents]]
name = "fablyn-orchestrator"
kind = "claude"
args = ["--model", "claude-opus-5-5"]
kickoff = "docs/agent-team.md#roles"

[[agents]]
name = "fablyn-dev"
kind = "claude"
args = ["--model", "claude-opus-5-5"]

[[agents]]
name = "fablyn-reviewer"
kind = "codex"
args = ["-m", "gpt-6-astra"]
```

## Non-goals

- Not a task board or coordinator. The owner's loop (brief, build, review, fix, ship) stays as it is.
- No automatic approval of anything, and no model or token management beyond start arguments.

## Open questions for the brainstorm

1. File name, format and discovery: `.herdr/fleet.toml`? TOML or YAML? Walk up from the cwd?
2. Implementation language and build: a shell script needs no build step. Node, Go or Rust needs a toolchain, or prebuilt release binaries like herdr-plus.
3. Layout: one tab with N panes, or reuse herdr-spreader's workspace/tab/pane model? Keep it minimal?
4. Quitting an agent cleanly per kind (Claude `/exit`, Codex `/quit`, …), only when idle. Is there a herdr-native release or stop?
5. Kickoffs: per-role prompt files with variables (repo, branch, "where things stand")? Where does the "where things stand" note come from, a STATE file the orchestrator keeps?
6. Can it also ship as an agent skill, so a Claude or Codex orchestrator drives `up`, `restart` and `swap` itself?
7. Name and repo: `herdr-fleet`? Licence?
