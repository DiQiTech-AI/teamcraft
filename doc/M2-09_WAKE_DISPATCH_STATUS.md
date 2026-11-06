# M2-09 Wake Dispatcher status

Updated: 2026-11-06

## Current implementation

- SQLite schema v11 persists event intake, merged Wake rows, source-event links,
  command bindings, and scanned/acknowledged/processed cursors.
- Scheduler Leader ticks persist new Wake candidates before normal task
  admission. Event intake does not contact Herdr.
- Wake admission binds one deterministic CEO Command and records
  `pending -> sending -> submitted|unknown`; CEO acknowledgement and processing
  remain separate transitions. Unknown delivery is not automatically retried.
- `assumed_default` is a temporary receipt policy. After a successful bootstrap
  prompt submission and a second live-session identity check, the database
  records the assumption explicitly. It is not evidence that the host verified
  Protocol or Skill loading.

## Deferred verification and remaining work

- Real Codex/OpenCode host-receipt verification and real Herdr Wake delivery
  tests are deferred. Do not report those as passed based on mocks or the
  `assumed_default` policy.
- Complete the Wake Dispatcher CLI/Herdr delivery and reconciliation entry
  points, then exercise submission, lost-response recovery, explicit CEO
  acknowledgement, restart recovery, and identity drift against the available
  Herdr/Harness versions.
- Replace `assumed_default` with a host-verified receipt source after its
  provider can be tested. The M2-08 trusted-receipt acceptance criteria remain
  open until then.

## Verification recorded for this change

- Python 3.13 targeted suite: 182 tests passed across CEO initialization,
  Wake, Scheduler, domain, verification, protocol, runtime adapter, and CLI
  contracts.
- No real Herdr prompt was sent as part of this implementation.
