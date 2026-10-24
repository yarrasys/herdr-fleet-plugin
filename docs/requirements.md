# Historical requirements

This document records the original product requirements for herdr-fleet. It is historical context, not the current command reference. Deployment identities and organizational anecdotes have been removed; all names and paths below are synthetic. [The design](design.md) records the decisions and later amendments, and [the README](../README.md) describes current setup and behavior.

## The problem

A repository may use several coding agents with stable roles: an orchestrator coordinates work, a developer implements it, and a reviewer checks it independently. The roles may use different agent executables and native arguments. Maintaining pane layouts, names, readiness checks and briefings by hand makes it easy to start in the wrong pane, lose a role's identity or send a prompt while a dialog is open.

The plugin should turn a per-repository declaration into repeatable lifecycle operations while leaving task allocation and authorization with the owner. A model change is configuration; it must not silently broaden an agent's authority.

## Functional requirements

1. **Declare a team.** A fleet file specifies each seat's stable name, agent kind, opaque native arguments, working directory, kickoff template and layout preference. It can be reviewed with the repository's other configuration.
2. **Bring it up.** Create or reuse a workspace, create only the necessary panes, start named agents through Herdr's agent interface, wait for readiness, and send briefings after the roster is complete. An already-live seat is not restarted.
3. **Restart a seat.** When safe, quit it, start it again in the same pane and brief it with current project state. Refuse blocked/unknown seats and protect the caller's own agent.
4. **Apply configuration drift.** Compare configured kind/arguments with available live evidence; restart only selected drifted seats. Treat externally adopted sessions conservatively.
5. **Inspect state.** Show expected and live kinds, panes, lifecycle state, drift and integration diagnostics. Errors must identify the affected seat and leave other seats usable.
6. **Stop cleanly.** Quit seats through their native quit command. Close only panes/workspaces recorded as created by the plugin and only when explicitly requested.

## Historical Herdr constraints

The first implementation targeted Herdr 0.9.4. These observations explain design choices; use the installed CLI's `--help` for current syntax.

- A plugin is a directory with a `herdr-plugin.toml` manifest. Actions and pane entrypoints execute ordinary argv commands with invocation context and plugin config/state locations supplied by Herdr. See the [public plugin documentation](https://herdr.dev/docs/plugins/).
- A named agent starts in an existing pane at an interactive shell prompt. A busy foreground process must not be replaced. Agent readiness can be delayed by shell startup or a trust dialog.
- Names match `[a-z][a-z0-9_-]{0,31}` and are unique among live agents. Pane records do not provide an agent name; obtain identity from `agent get` or `agent list`.
- Herdr's agent API provides start, prompt, wait, read and rename operations. Process information supplies evidence for a shell prompt and native argv. Agent stop/quit behavior remains specific to the agent kind.
- A wait can be bounded and run in the Herdr server rather than making an agent poll. Resumed unnamed sessions can be adopted only after validating their kind and recorded pane.
- Plugin action output is available through logs; notifications and plugin panes provide visible feedback.

## Safety requirements

- Never submit text to a blocked or unknown agent or answer a permission prompt on the owner's behalf. The later, narrowly scoped exit-confirmation and interrupt paths are specified in design sections 23.7 and 23.11; they are not general dialog automation.
- Keep all agent text on the guarded agent-prompt path. The current presence check protects existing input in focused and unfocused panes.
- Never run bare `herdr`; it launches or attaches the terminal UI.
- Preserve running sessions, caller context and unowned panes. A dry run must not mutate state or move focus.
- Merging, deployment, publishing and pushing require explicit owner authorization; a role name is not authorization.

## Synthetic team example

The complete, runnable example lives in [examples/team](../examples/team). It supplies role templates within its own `.herdr/` tree. Its agent kinds are illustrative; install and authenticate the kinds you choose and provide available model arguments yourself.

```toml
workspace = "sample"
cwd = "."

[[agents]]
name = "sample-orchestrator"
kind = "claude"
args = []
kickoff_text = "Read AGENTS.md if present. Coordinate the requested task; owner approval is required before merging, deploying or pushing."

[[agents]]
name = "sample-dev"
kind = "claude"
args = []

[[agents]]
name = "sample-reviewer"
kind = "codex"
args = []
```

## Non-goals and resolved questions

The plugin is not a task board, autonomous release system, credential manager or model-budget manager. It should work across agent vendors, use a small dependency footprint and keep owner interaction visible. Questions about format/discovery, implementation language, layout, per-kind quitting, briefing templates and state storage were resolved in design sections 1–15. Messaging and the visible watcher were added in later sections rather than assumed in these original requirements.
