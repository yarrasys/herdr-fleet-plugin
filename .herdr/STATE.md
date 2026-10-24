# herdr-fleet plugin: state

## production
herdr-fleet 0.14.1 (767a8fc) is on GitHub main and installed; the owner's five fleets run it with the top band and the board strip.

## in flight
Handover: the designer seat moves from a Claude session to Codex (gpt-6.1-sol). The previous designer's notes that are not in the repo:
- The owner's standing rulings: no Claude Code-only changes (vendor-neutral fixes only); the uniform grid is preferred for the middle seats; the orchestrator band on top, strip at the bottom, on every fleet; pushes are approved one by one.
- Each release: design section first (brainstorm with the owner), build by fleet-builder, review by fleet-reviewer, live smoke test on a Haiku seat in a scratch fleet (a fleet file under /private/tmp is fine), amendment subsection, commit, then ask "push".
- After a release that touches the watcher, restart the five strips: close the strip pane, `up <lead seat>`, `tile`, in each fleet's repository.
- Field reports: ask both orchestrators (fablyn-orchestrator, winner-chronicles) with a plain send; their replies arrive in this seat's inbox; rank, verify claims against the message logs, propose, build.
- Open: the awesome-herdr listing PR #43 (yigitkonur/awesome-herdr) and the herdr discussion #2247 on per-state pane colours; the 0.10.1 candidate below; mouse clicks in the strip unverified.

## backlog
- 0.10.1 candidate: `tile` drops a strip's record silently when herdr answers not-found (seen once on 22 Oct, never reproduced).
- Context size for Codex seats; the watcher on herdr's event stream instead of one `agent wait` per mailbox.
- Mouse clicks in the strip: unverified which encoding herdr sends.
