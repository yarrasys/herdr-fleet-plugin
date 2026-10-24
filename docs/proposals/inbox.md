# Archived proposal: a durable message inbox

**Superseded.** This is historical rationale for [design section 18](../design.md#18-the-inbox-durable-batched-token-free-delivery-versions-060-061-070), with later delivery and safety changes in sections 20, 21, 23 and 24–27. It is not current operating instructions. Names, paths and message bodies are synthetic; deployment anecdotes and private usage estimates have been removed. Use [the README](../../README.md#inbox) for current commands.

## 1. Processes and model requests

A local CLI or watcher can inspect files and wait for Herdr lifecycle events without invoking a model. Prompt delivery can start a model turn, so batching reduces repeated submissions. Agents should wait in a tool call rather than repeatedly asking the model to poll. Herdr may learn state through agent integrations or screen detection, depending on the agent and installed integration.

The proposal compared server-side `agent wait` calls with a future event subscription. The implementation chose bounded waits per mailbox; local mailbox/board polling remains ordinary CPU and filesystem work. No measured organization-specific token or price assumptions are required for this design.

## 2. Storage

The durable mailbox separates queued messages from messages that were delivered, expired or dropped:

```
.herdr/inbox/<name>/new/<id>.md
.herdr/inbox/<name>/cur/<id>.md
.herdr/messages.jsonl
.herdr/watch.lock
```

The adopted id format is specified in design section 18.1. Header metadata includes sender, recipient, time, optional reply target and expiry; the body is stored verbatim. For example, a synthetic body can be `Please review the parser change.` Local mailbox contents and runtime state are excluded from publication.

## 3. Message lifecycle

Write the durable message before attempting delivery. Batch queued messages in delivery order and use the guarded prompt path. Move confirmed and unconfirmed deliveries to retained storage, log their outcomes, and never automatically repeat a possibly delivered prompt. Keep refused delivery attempts queued unless the sender explicitly requested refusal through `--now`.

The early proposal considered typing ahead to working agents by default. That was withdrawn: current behavior leaves working seats queued unless `send --now` or named `deliver --now` explicitly allows it. Every prompt still passes the presence check. Large batches use an inbox pointer; expiry and explicit inbox dropping are logged separately from delivery. Reply debts start at delivery rather than enqueue time.

## 4. Visible watcher

A foreground watcher holds a per-fleet lock and waits for eligible seats/contacts to become idle or done. A short debounce combines message bursts; state and input are checked again before delivery. The watcher never starts/restarts/quits an agent or answers a dialog. It can be started manually or by `up` in a recorded visible strip. An event-stream implementation remains a separate future design choice.

## 5. Optional hooks

The Claude Code hooks are an optional extra. They let a seat read mailbox context inside its own process without typing into its input. Empty/error paths are silent, FYI-only queues do not continue a turn, and loop protection limits repeated continuation. Other agent kinds use the watcher, direct delivery, kickoff or an explicit inbox read. Hook details are defined in design section 18.7 and its amendments.

## 6. Owner inbox

`owner` is a mailbox target. Inside Herdr a notification can signal delivery; local files retain the body for inspection. Outside Herdr or with explicit queuing, messages stay queued. The channel conveys information and requests; it does not grant authorization.

## 7. Safety principles retained

- Queue delivery defaults to idle/done and uses the shared guarded path.
- The watcher never answers a dialog, repeats an unconfirmed delivery or manages agent lifecycle.
- An agent is not asked to poll repeatedly; waits occur in a tool or local process.
- Hook delivery reads files within the seat's process rather than injecting terminal input.
- The later presence guard and narrowly scoped key paths remain authoritative.

## 8. Cost interpretation

Queueing, local reads and server waits do not themselves make model requests. Actual delivery may cause a model turn; no fixed price or context-size estimate is promised. FYIs normally ride with another delivery, but explicit delivery and pending-request flushes can deliver them immediately. The original absolute zero-turn claim was not retained.

## 9. Implementation phases

Storage, message ids, batching, inbox commands, reply ids and expiry came first; the foreground watcher followed, then optional hooks. Subsequent releases added the strip, presence guard and board. Consult the numbered design sections for accepted behavior and the test suite for current coverage.

## 10. Decisions resolved

The accepted design keeps durable bodies, queues by default when a target is unavailable, distinguishes explicit delivery from queuing, and derives reply debt from delivery evidence. Historical alternatives in this proposal do not override those decisions. No background agent supervisor, automatic approval or authority to merge/deploy/push follows from the mailbox design.
