---
name: teamcraft
description: Build and coordinate a stateful software delivery team in the current Herdr tab, with a CEO orchestrator and on-demand built-in or custom specialist agents. Use when the user asks to assemble, recover, inspect, or manage a development team through Herdr; do not use for ordinary single-agent delegation.
---

# TeamCraft

Create a task-specific team in sibling panes while preserving the current tab, project directory, and user focus.

## Preconditions

Before Herdr control, require `HERDR_ENV=1`, then run `herdr --skill` and follow the installed binary's semantics. Do not inspect or control a focused Herdr session from outside Herdr. Do not create a workspace, tab, or worktree unless the user requests that topology.

Use `scripts/teamctl.py` for managed team members. Runtime state lives ONLY in the SQLite database `<project>/.teamcraft/teamcraft.db` (schema v8): ownership and intent are reserved before layout mutation, every persistent Herdr side effect (pane split, agent start, prompt) is recorded as a durable Command claimed **before** it executes, and each later phase is written transactionally together with domain events, scoped to the current Herdr socket session and fenced by a project leader lease. Version-bound prompts additionally carry an immutable Attempt Brief and a distinct delivery record: submitted is not an acknowledgement, and ambiguous delivery must be reconciled rather than resent. CEO Attempts also pin their protocol version and exact content hash. Verification records distinguish the dispatch-time Task version pinned by Brief/WorkerReport from the completion-time Task version; they bind an immutable report artifact to the Attempt, Brief digest, and final WorkerReport identity/digest. Approval rechecks that complete evidence under the project Leader fence. Do not edit this state by hand while a team operation is running.

The `.teamcraft/` runtime directory contains the SQLite database, locks, and journals and is ignored by version control. JSON is not runtime state: the CLI fails closed — without calling Herdr and without creating, reading, modifying, or deleting any file — when it finds legacy `.herdr-team/state.json` or a non-empty `.teamcraft/state.json`. Resolve such legacy JSON manually before continuing. The importable Python Core (`teamcraft`) ships bundled offline under `scripts/core/`; no network access is ever required at runtime.

## Team model

Bootstrap only the CEO unless the user asks for a fixed roster. If the user appoints the current agent as CEO, adopt the responsibilities in the current pane instead of spawning a duplicate CEO. Otherwise launch one CEO. The CEO decomposes the objective and creates workers only when their work is separable.

Roles describe responsibility; `--kind` independently selects any harness supported by the installed Herdr CLI.

- CEO owns requirements, staffing, assignments, review, integration, and delivery.
- CTO owns architecture, interfaces, tradeoffs, and technical risk.
- Developer owns bounded implementation and verification.
- Designer owns assigned flows, interaction, visual specifications, and assets.
- Tester owns acceptance coverage, reproduction, and evidence.
- Custom supplies another specialty through a concrete `--responsibility`.

Every editing worker needs concrete deliverables, acceptance criteria, and one or more non-overlapping ownership paths. Ownership is coordination metadata, not filesystem access enforcement. Use normalized project-relative exact paths or directory prefixes ending in `/**`; complex globs and prose are rejected. Use `--read-only` for review, investigation, or testing that must not edit files.

The CEO must collect changed-file lists and verification evidence, review results, release or complete finished ownership, and resolve integration gaps. Workers may inspect shared context but must not edit outside recorded ownership.

## Launch members

Launch a CEO from the project root:

```bash
python3 .agents/skills/teamcraft/scripts/teamctl.py launch \
  --role ceo --kind codex --name ceo \
  --task "Deliver the requested feature and create only needed workers."
```

Launch an OpenCode developer with two separate claims:

```bash
python3 .agents/skills/teamcraft/scripts/teamctl.py launch \
  --role developer --kind opencode --name api_dev \
  --ownership 'src/api/**' \
  --ownership 'tests/api/**' \
  --task-file /absolute/path/to/assignment.md \
  --agent-arg=--standalone
```

Launch a read-only tester:

```bash
python3 .agents/skills/teamcraft/scripts/teamctl.py launch \
  --role tester --kind codex --name acceptance \
  --read-only --task "Run the acceptance checks and report evidence; do not edit."
```

For another specialty:

```bash
python3 .agents/skills/teamcraft/scripts/teamctl.py launch \
  --role custom --responsibility "Own deployment automation and release evidence." \
  --kind codex --name release_eng --ownership 'deploy/**' \
  --task "Prepare staging; acceptance: the dry run succeeds."
```

The helper resolves the caller or `--from-pane` through Herdr before matching layout geometry, uses an explicit canonical pane ID, preserves native arguments, and keeps focus with `--no-focus`. It validates the installed Herdr kind list and harness executable before splitting.

For OpenCode, project profiles live in `.opencode/agents/herdr-*.md`. The helper selects the role through a pane-local `OPENCODE_CONFIG_CONTENT` default-agent override and retains the full OpenCode TUI for Herdr detection. This does not modify an existing default profile or widen its permissions. A live run has verified the CEO, developer, and tester profiles with `omlx/Qwen3.8-Flash-Next-oQ4e-mtp`; confirm the displayed role and model when first using an unverified profile or another project.

## Inspect and recover

Inspect recorded phase and actual live state:

```bash
python3 .agents/skills/teamcraft/scripts/teamctl.py status
```

`agent_state` reports `blocked`, `working`, `idle`, `done`, `unknown`, or `missing`. When blocked, inspect `herdr agent get` and `herdr agent read`; do not blindly approve, resend, or retry.

Treat both `idle` and `done` as settled completion states. Do not wait only for `done`: Herdr may report a successfully completed worker as `idle`, and a done-only polling loop will not converge.

Recover a partial launch without creating another pane:

```bash
python3 .agents/skills/teamcraft/scripts/teamctl.py resume api_dev
```

Resume verifies the recorded terminal ID and native agent session before adopting a live occupant. It reuses an available recorded shell pane after a failed start. Its default `--prompt-action inspect` never resends a possibly delivered task. After inspection, choose explicitly:

```bash
python3 .agents/skills/teamcraft/scripts/teamctl.py resume api_dev --prompt-action send
python3 .agents/skills/teamcraft/scripts/teamctl.py resume api_dev --prompt-action skip
```

If a split timed out, inspect the layout first; use `--split-action retry` only when no pane was created. A timeout does not prove failure.

Release ownership without closing the pane, or mark the assignment complete:

```bash
python3 .agents/skills/teamcraft/scripts/teamctl.py release api_dev
python3 .agents/skills/teamcraft/scripts/teamctl.py complete api_dev
```

Neither operation closes a pane or terminates an agent. Do not pre-create idle specialists, overlap active edit claims, or infer completion from `unknown`.

Reset all TeamCraft runtime records to an empty schema only when explicitly requested:

```bash
python3 .agents/skills/teamcraft/scripts/teamctl.py reset \
  --cwd /absolute/project/root \
  --confirm-project /absolute/project/root
```

Reset requires the confirmation path to exactly match the resolved project root, refuses while a recorded TeamCraft agent is live, creates a verified online database backup and retains the previous database/WAL artifacts in a unique `.teamcraft/archive-*` directory, then atomically installs a freshly initialized schema. It does not close Herdr panes or modify project files. The operation is destructive to the active TeamCraft records; the archive is the recovery copy.

## Verify and approve report-only work

For a finished scout Attempt, configure the project's human-approved verifier argv, run it, inspect the immutable artifact digest, and record an explicit accept/reject decision:

```bash
python3 .agents/skills/teamcraft/scripts/teamctl.py verifier-configure \
  --executable python3 --arg=-m --arg pytest --arg=-q \
  --rules-version tests-v1 --actor human --idempotency-key verifier-v1
python3 .agents/skills/teamcraft/scripts/teamctl.py verify \
  --attempt-id ATTEMPT_ID --idempotency-key verify-ATTEMPT_ID-v1
python3 .agents/skills/teamcraft/scripts/teamctl.py verification-status --task-id TASK_ID
python3 .agents/skills/teamcraft/scripts/teamctl.py verification-artifact --verification-id VERIFICATION_ID
python3 .agents/skills/teamcraft/scripts/teamctl.py verification-approval \
  --verification-id VERIFICATION_ID --decision accept \
  --actor human --reason "Report accepted" --idempotency-key approve-VERIFICATION_ID
```

Verifier argv is executed directly with `shell=False`; an absent verifier never means PASS. The artifact, digest, result, rules/config versions, and approval evidence are durable and version-bound. Only a current accepted scout report can reach DONE. Ordinary ship remains VERIFYING/NEEDS_HUMAN until M4 provides Worktree/Commit/Diff evidence; M2 does not implement local snapshots or direct-PR delivery.

Release and completion refuse an agent in `working`, `blocked`, or `unknown` state. Use `--force` only after explicitly deciding that its ownership may be reassigned despite that state. Identity verification still applies.

Member lifecycle operations use an exclusive lock plus a durable leader lease (monotonic epoch). An ambiguous outcome — crashed process, lost lease, or a response that arrives after the lease was lost — is recorded as UNKNOWN and must be settled by `resume` inspection against live facts; the CLI never blindly resends, and a stale leader can never commit success. State, database, and locks in `.teamcraft/` are local runtime files excluded from version control. OpenCode launches require the selected role profile in the target project and preserve existing JSON `OPENCODE_CONFIG_CONTENT` settings while overriding only `default_agent`.
