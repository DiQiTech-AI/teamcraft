# M2-09 Wake Dispatcher closure and deferred verification

Updated: 2026-11-07

## Current implementation

- SQLite schema v12 persists event intake, merged Wake rows, source-event links,
  command bindings, and scanned/acknowledged/processed cursors.
- Scheduler Leader ticks persist new Wake candidates before normal task
  admission. Event intake does not contact Herdr.
- Wake admission binds one deterministic CEO Command and records
  `pending -> sending -> submitted|unknown`; CEO acknowledgement and processing
  remain separate transitions. Unknown delivery is not automatically retried.
- `wake-dispatch` ingests and admits one pending Wake, prompts only the exact
  initialized CEO session, and records ambiguous outcomes as `unknown`.
  Interrupted submissions are recovered to `unknown` on the next dispatch.
- `wake-status`, `wake-ack`, `wake-process`, and `wake-reconcile` expose the
  manual queue lifecycle. Reconciliation requires explicit inspected evidence;
  “not submitted” blocks the Wake and never generates a resend.
- `assumed_default` is a temporary receipt policy. After a successful bootstrap
  prompt submission and a second live-session identity check, the database
  records the assumption explicitly. It is not evidence that the host verified
  Protocol or Skill loading.
- Confirmed capability boundary (2026-11-07): the Herdr session API exposes no
  server generation field, so TeamCraft detects Herdr server/session replacement
  via socket path plus stat device/inode. Wake admission and delivery always
  require a native Session ID; a CEO bound in non-portable live-only
  (`live_process`, schema v14) mode is rejected by the default
  `require_initialized` gate at admission and again adjacent to Herdr I/O. A
  process exit, PID start-time fingerprint change, or Herdr socket change
  therefore blocks Wake continuation instead of silently retargeting.

## Closure decision and deferred verification

- M2-09 implementation work remains closed by owner decision. M2-08 has
  re-entered focused acceptance for its first-start and live-only identity
  corrections (see `TEAMCRAFT_2_0_M0_REVIEW.md`, section 12.17); this does not
  reopen M2-09 or claim that deferred real-world checks passed.
- Later, test a trusted Codex/OpenCode host receipt source and real Herdr Wake
  submission, lost-response/restart recovery, explicit CEO acknowledgement,
  and identity drift. Do not treat mocks or `assumed_default` as those results.
- `assumed_default` remains explicitly marked as a temporary assumption. Replace
  it with host-verified receipt only after the provider can be tested.
- Dormant CEO auto-start remains off and was not delivered here. No token usage
  budget decision is inferred from unknown usage.

## Verification recorded for this change

- Python 3.13 targeted suite: 37 tests passed across Wake, CEO initialization,
  and verification migration behavior; `compileall` passed.
- `wake-dispatch --help` and `git diff --check` passed.
- 2026-11-07 live-only boundary round: full local suites passed — 387 core tests
  (`tests/`) and 80 skill tests (`.agents/skills/teamcraft/tests/`), including a
  new explicit case that Wake admission of a live-only CEO fails closed with no
  `ceo.wake` command and the Wake left `pending`, and genuine v11→v14 ladder
  migration coverage. No real Herdr prompt was sent for these.
- No real Herdr prompt was sent as part of this implementation; actual delivery
  and host-receipt verification remain deferred.
