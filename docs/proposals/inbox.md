# Proposal: a durable message inbox (towards design section 18)

Status: proposal, 21 Oct 2026, not yet decided. Facts verified live on herdr 0.9.5 and Claude Code hooks reference (code.claude.com/docs/en/hooks.md).

## 1. The processes, and who pays tokens

| Process | What it is | Tokens |
|---|---|---|
| herdr server | one persistent Rust process per session (`herdr status server`: protocol 22, socket `~/.config/herdr/herdr.sock`). Owns workspaces, panes and the agent table. Learns each agent's state (`idle`, `working`, `blocked`, `done`, `unknown`) from the integration hook installed in that agent's harness (`herdr-agent-state.sh`), not by reading the screen. | none |
| herdr CLI | a thin client: every `herdr …` call is one request over the socket. `herdr agent wait TARGET --until idle --until done` blocks inside the server until the state is observed, with no polling on the client. The socket API also offers `events.subscribe` with `pane_agent_status_changed` events. | none |
| herdr-fleet commands | short-lived Python processes started by a person, an agent's tool call, or a plugin action. | none by themselves; a tool call inside an agent's turn costs a few hundred tokens |
| agents | Claude Code, Codex, agy. Run only during a turn. Every turn re-reads the whole context from cache (400k to 730k tokens per Fablyn seat today). An idle agent does nothing until prompted. | the whole cost |
| harness hooks | Claude Code `Stop` and `UserPromptSubmit` hooks run shell commands inside the agent's own process. Output goes into the context only when non-empty. | none when silent |
| watcher | a visible foreground `herdr-fleet watch` in a pane, started by the owner. Not a daemon the plugin spawns. | none |

Rule that follows: an agent never polls. Every wait sits in the herdr server or in a hook; every delivery is batched so one wake-up carries everything.

## 2. Storage

```
.herdr/inbox/<name>/new/<id>.md       queued, not yet delivered
.herdr/inbox/<name>/cur/<id>.md       delivered (kept for replay)
.herdr/messages.jsonl                 the index: queued / delivered / expired / unconfirmed entries carry the id
.herdr/watch.lock                     pid of the running watcher, if any
```

`<name>` is a seat, a contact, or `owner`. `<id>` is `YYYYMMDDTHHMMSS-<4 hex>`; unique names make concurrent senders safe without locks, and delivery is an atomic rename `new/` to `cur/`. A file is a short header then the body:

```
from: fablyn-orchestrator
to: fablyn-dev
time: 2026-10-21T10:09:00+11:00
fyi: false
reply_to: 20261021T095300-3f2a      (optional)
ttl: 2026-10-21T12:00:00+11:00      (optional)

Range for 1.25.1: 90ba47c..a659ceb. Build in /private/tmp/fablyn-tm1251.
```

Everything under `.herdr/inbox/` is gitignored. Direct sends store their body too, so `inbox --history fablyn-dev` can replay a conversation into a fresh context.

## 3. The life of a message

1. `send` writes the file into `new/` and logs `queued` with the id. Then, in the same run:
   - target `idle` or `done`: deliver now, as one prompt that first drains everything in `new/` (oldest first) and ends with this message; move the files to `cur/`, log `delivered`. A plain send to an idle seat therefore still wakes it, as today.
   - target `working`: typed ahead now through `agent prompt`, as today (herdr queues it as the agent's next turn), unless `--queue`.
   - target `blocked`, `unknown` or `absent`: stays in `new/`; the output says `queued: fablyn-dev is absent`. `--now` turns that into today's refusal instead.
   - `--fyi`: never wakes an idle seat on its own. It is typed ahead if the seat is working, else it waits in `new/` for the next delivery to carry it.
2. Later deliveries, all through `seats.safe_prompt`, idle or done only:
   - **kickoff**: `up`, `restart` and `kickoff` render `new/` into the briefing through `{inbox}`; the fresh seat sees what arrived while it was away, with times.
   - **flush before send**: any direct send drains `new/` first (step 1), so order is preserved.
   - **`deliver [SEAT...]`**: run by hand or by the orchestrator when `status` shows queued messages beside an idle seat.
   - **watcher** (section 4).
   - **pull**: the seat runs `herdr-fleet inbox --read` inside a turn it is already in; the command prints the messages and moves them to `cur/`.
3. Above 16 KiB the delivery prompt is a pointer (`4 messages waiting; run herdr-fleet inbox --read`) instead of the bodies.
4. Owed replies count from delivery, not from queuing. With ids, `--reply-to ID` and `ack ID` settle one message; the pair rule of 16.3 remains the fallback.
5. Failure: a stalled or timed-out prompt logs `unconfirmed`, moves the files to `cur/` and never redelivers by itself; `inbox --redeliver ID` does it by hand. A message past its `ttl` is logged `expired` and moved to `cur/` without delivery. `restart --drop-inbox` expires everything queued for that seat before the kickoff.

## 4. The watcher

`herdr-fleet watch [--fleet PATH]` runs in the foreground of a pane (`Fleet: watch` opens one as a split). One per fleet, enforced by `.herdr/watch.lock`.

- Reads `new/` for every seat. For each seat with at least one non-FYI message queued, it runs `herdr agent wait SEAT --until idle --until done --timeout 30000` in a thread: the wait sleeps in the herdr server. A timeout only makes the loop re-read `new/` for new targets; no agent is touched.
- When a seat is observed idle: wait 5 s (debounce, so a burst from three seats becomes one turn), re-read the state, then deliver the batch through `safe_prompt` exactly as `deliver` would. Log `delivered`, and `herdr notification show` a one-liner for the owner.
- FYIs ride along with the batch; a queue holding only FYIs waits for the next real message, the next kickoff, or `deliver --fyi`.
- Blocked seats are never prompted; the watcher simply keeps waiting. Ctrl-C stops it; nothing else can start it.
- Upgrade path: a single `events.subscribe` stream over the socket (`pane_agent_status_changed`) instead of one wait per seat, if fleets grow beyond a handful of seats. That would be the first code path outside the CLI, so it is a separate decision.

## 5. Hooks (Claude Code only, opt-in, later)

`herdr-fleet hooks install` adds two entries to `.claude/settings.local.json`, next to the allow rules `doctor` already checks:

- `Stop`: runs `herdr-fleet inbox --hook stop`. With nothing queued it prints nothing and costs nothing. With messages queued it returns `hookSpecificOutput.additionalContext` holding the batch, which Claude Code feeds to the agent so the turn continues; the files move to `cur/`. This is the seat reading its own inbox at the end of a turn, inside its own process: pull, not push, and no wake-up.
- `UserPromptSubmit`: adds `additionalContext` "N messages waiting, run herdr-fleet inbox --read" when the inbox is non-empty, so a seat the owner prompts by hand learns about them.

Codex and agy have no equivalent injection today; they rely on the watcher and kickoff.

## 6. Owner inbox

`owner` is a valid target. Delivery to the owner is a `herdr notification show` plus the file; `status` prints `for you: 2 messages (herdr-fleet inbox --as owner --read)`. Seats get the only channel to the human they lack today.

## 7. Invariants added

11. Queued messages are delivered only to `idle` or `done` seats, only through `safe_prompt`, only by a plugin command that a person, an agent's tool call, or the owner-started watcher invoked. Nothing the plugin spawns runs in the background.
12. The watcher never prompts a `blocked` or `unknown` seat, never answers a dialog, never redelivers an unconfirmed message, and never starts, restarts or quits anything.
13. An agent is never asked to poll. Waits live in the herdr server (`agent wait`) or in a silent hook.
14. A hook-delivered batch is the seat reading its own inbox; the plugin writes nothing to an agent through a hook.

## 8. Cost summary

| Event | Cost |
|---|---|
| message queued, seat busy or away | 0 tokens |
| watcher waiting | 0 tokens, no polling |
| batch delivered to an idle seat | 1 turn (one context re-read), however many messages |
| FYI | 0 extra turns, ever |
| seat reads inbox inside a turn | one tool call |
| Stop hook, inbox empty | 0 tokens |

## 9. Phases

- 0.6.0: storage, ids, `send` modes (`--queue`, `--now`, FYI never wakes), flush before send, `{inbox}` at kickoff, `deliver`, `inbox` (`--read`, `--count`, `--history`), `--reply-to`, `ack ID`, `--ttl`, `restart --drop-inbox`, owed-from-delivery.
- 0.6.1: `watch` with `.herdr/watch.lock`, `Fleet: watch` action, `status` showing queued counts and the watcher.
- 0.7.0: `hooks install`, owner inbox.

## 10. Decisions for the owner

1. Queue by default on refusal, `--now` to refuse (recommended), or refuse by default.
2. FYI never wakes an idle seat (recommended), or FYI delivered immediately as in 0.5.0.
3. Store the body of every message, including direct sends, for replay (recommended).
4. Ids with `--reply-to` and `ack ID` in 0.6.0 (recommended).
5. Watcher in 0.6.1 via `agent wait` per seat (recommended), or straight to `events.subscribe`.
6. Owner inbox and hooks in 0.7.0 (recommended), or earlier.
