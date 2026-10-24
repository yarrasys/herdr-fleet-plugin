# herdr-fleet plugin: state

## production
herdr-fleet 0.14.1 (767a8fc) is on GitHub main and installed; the owner's five fleets run it with the top band and the board strip.

## in flight
Section 27 implements the owner's setup/README audit and expands this development fleet from two to five external lead contacts. The installed 0.14.1 board has reloaded and visibly shows all five. Local code also fixes starter TOML placement and combined seat/contact packing; that Python code is not yet installed into running watchers. Builder and reviewer each passed all 861 tests under both required interpreters; reviewer verdict MERGE (#6da9d1). Production and version strings remain 0.14.1; no push or release is authorized. Verification and live smoke evidence are recorded in design 27.4.

The designer is now Codex (gpt-6.1-sol). Standing workflow notes:
- The owner's standing rulings: no Claude Code-only changes (vendor-neutral fixes only); the uniform grid is preferred for the middle seats; the orchestrator band on top, strip at the bottom, on every fleet; pushes are approved one by one.
- Each release: design section first (brainstorm with the owner), build by fleet-builder, review by fleet-reviewer, live smoke test on a Haiku seat in a scratch fleet (a fleet file under /private/tmp is fine), amendment subsection, commit, then ask "push".
- After a release that touches the watcher, restart the five strips: close the strip pane, `up <lead seat>`, `tile`, in each fleet's repository.
- Field reports: ask both orchestrators (fablyn-orchestrator, winner-chronicles) with a plain send; their replies arrive in this seat's inbox; rank, verify claims against the message logs, propose, build.
- Open: the awesome-herdr listing PR #43 (yigitkonur/awesome-herdr) and the herdr discussion #2247 on per-state pane colours; the 0.10.1 candidate below; mouse clicks in the strip unverified.

## backlog
- 0.10.1 candidate: `tile` drops a strip's record silently when herdr answers not-found (seen once on 22 Oct, never reproduced).
- Context size for Codex seats; the watcher on herdr's event stream instead of one `agent wait` per mailbox.
- Mouse clicks in the strip: unverified which encoding herdr sends.
- Existing `RealThreads.test_waiters_in_threads_deliver_and_stop_cleanly` failed once during concurrent section-27 full-suite runs, then passed in isolation and in a serial full-suite rerun. No watcher delivery code changed; keep the intermittent observation with the validation evidence.
