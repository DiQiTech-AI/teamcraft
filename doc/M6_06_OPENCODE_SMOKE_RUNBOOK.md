# M6-06 — Real-host OpenCode smoke runbook (PG-7 CEO + Wake + Worker, distinct panes)

Pinned commit: `23372d5897fa08de561b9686c18c1ff368c51a53` ("Validate M6-06 CEO
bootstrap and Wake in isolated runner") — the commit that carries the CEO/Wake
runner and its offline proof.
Runner: `scripts/m606_opencode_e2e.py` · Offline proof: `tests/test_m606_runner.py`
Owned by this slice: **this file only**. Nothing else was created or modified.

Status: **prepared, not executed.** This pass used `--help`, `--plan` and
`--preflight` (all inert / read-only). **No prompt was submitted to any pane, no
cloud endpoint was contacted, no live database was written.**

Verification marks used below:
* ✅ **observed** — the exact command in this file was run during this prep pass.
* 🧪 **test-verified** — proven offline against a disposable DB with a fake
  transport (`tests/test_m606_runner.py`, named in the row).
* ⏳ **run-time** — only resolvable inside an executing invocation; the runbook
  tells you which artifact to read to confirm it.

---

## 0. Redaction policy (apply before sharing anything)

Never paste into a shared channel or ticket:
* native session ids (`ses_…`, UUIDs) — write `<WORKER_SESSION>` / `<CEO_SESSION>`;
* workspace/pane ids — write `<WORKER_PANE>` / `<CEO_PANE>` (form `<WS>:p<n>`);
* `server.socket` and any `$HOME`-rooted path — the `--preflight` payload contains
  it under `herdr_status["server.socket"]`;
* the per-user temp folder id inside `$TMPDIR` — write `<TMP>`;
* one-use secrets: the CEO bootstrap nonce. The runner only ever stores its SHA-256
  and reports digest prefixes (✅ `ephemeral_leak_scan` in `run-manifest.json`,
  🧪 `CeoBootstrapTests.test_ceo_bootstrap_reaches_initialized_under_assumed_default`).

`handoff.json`, `run-manifest.json` and `ceo-submit.json` legitimately contain
absolute paths and session ids. Treat them as **local evidence**; redact on export.

Placeholders used below:

| Token | Meaning |
| --- | --- |
| `<REPO>` | this repository's clone root (contains `scripts/…`) |
| `<TMP>` | `${TMPDIR%/}` (macOS: `/var/folders/<id>/T`) |
| `ROOT` | `<TMP>/m606-smoke-23372d5` — this smoke's disposable root |
| `<WORKER_PANE>` | existing pane that will receive the worker prompt |
| `<CEO_PANE>` | existing, **different** pane designated as canonical CEO |
| `<WORKER_SESSION>`, `<CEO_SESSION>` | native session ids of those panes |
| `<ALLOC_CWD>`, `<TASK_ID>`, `<ATTEMPT_ID>`, `<BRIEF_DIGEST>`, `<PROJECT_ID>`, `<RUN_ID>` | read back from `ROOT/evidence/handoff.json` |

The two panes **must be different**: the whole point of this smoke is to attribute
one prompt to the worker leg and a different prompt to the Wake delivered to the
CEO. A same-pane configuration is supported by the CLI (`--ceo-pane` equal to
`--herdr-pane` falls back to `--expect-session-id`) but cannot separate those two
attributions, so it is out of scope here.

---

## 1. What this prep pass already established

### 1.1 Modes are mutually exclusive — plan/preflight cannot carry live consent
✅ `--plan --confirm-live-run …` and `--plan --preflight …` are **argparse errors**:
the mode group is `[--plan | --preflight | --confirm-live-run | --cleanup-only]`.
Consequence: `--plan` validates the *inert* shape only; the full phase-2 command is
validated by guards **inside** the invocation that executes it. The equivalent
proof, without touching the host, is the offline suite (§1.4).

### 1.2 `--plan` is genuinely inert ✅
`--plan --json --root <TMP>/m606-smoke-23372d5` → exit `0`, and the root was still
absent afterwards. Payload reported `would_execute: false`,
`created_or_mutation_issued: "none"`, `root_would_be_created: true`, and the pins:

| pin | value |
| --- | --- |
| model | `Qwen3.8-Flash-Next-oQ4e-mtp` |
| oMLX | `http://127.0.0.1:8000/v1` (literal IP) |
| protocol | `1.0.0` / `c5c358bf5e1654bc…` (64-hex) |

Hard rules printed by the plan (verbatim): topology mutation verbs structurally
forbidden · no cloud fallback, loopback only, no credentials read · delivery is
submitted/unknown unless `--human-accept` · cleanup removes only journalled paths of
this invocation · project repo, its worktrees and its live DB are untouchable.

### 1.3 `--preflight` is read-only and currently green on this host ✅
`--preflight --json --root <TMP>/m606-smoke-23372d5` → exit `0`,
`stage: "preflight"`, `writable: false`, root still absent afterwards.

| check | observed |
| --- | --- |
| binaries | `opencode` ✅ present, `herdr` ✅, `codex` ✅, `claude` **absent** (PG-1 out of scope) |
| herdr | `client.version == server.version == 0.9.3`, `protocol 22`, `server.endpoint_compatible=yes`, `server.private_protocol_compatible=yes`, `update.restart_needed=no`, `server.status=running` |
| loopback oMLX | `health: healthy`, `reason_code: ok`, `pinned_model_present: true`, catalog = `[Qwen3.8-Flash-Next-4bit-mtp, Qwen3.8-Flash-Next-oQ4e-mtp]` |
| oMLX capabilities | `inference_request: supported`, **`prompt_ack: unsupported`**, **`event_stream: unsupported`** |
| protocol | `pinned_match: true` (`1.0.0`, `c5c358bf…`) against `teamcraft-ceo/references` |
| topology | structural digest `2dd133a0d309340f…` |
| repo | `repo_dirty_entries: 15` (peer working-tree changes; see blocker B4) |

The two `unsupported` capabilities are the reason PG-7 lands on
`assumed_default` and why no acknowledgement may be claimed (§5).

### 1.4 Offline authority for the live-mode rules 🧪
`tests/test_m606_runner.py`: **84 passed, 61 subtests** in this pass. The rows below
cite it for rules that `--plan` structurally cannot reach.

### 1.5 Guard refusals observed verbatim ✅ (all in inert `--plan` mode)

| invocation fragment | outcome |
| --- | --- |
| `--ceo-pane <CEO_PANE>` | `ABORT [GuardError]: --ceo-pane runs the PG-7 CEO bootstrap, which submits a prompt; it requires --confirm-live-run` |
| `--wake-deliver` | `--wake-deliver requires --ceo-pane: a Wake is a delivery TO the canonical CEO, and no CEO is designated` |
| `--ceo-expect-session-id X` | `--ceo-expect-session-id requires --ceo-pane` |
| `--ceo-claim-file f.json` | `--ceo-claim-file requires --ceo-pane: a claim without a designated CEO has nothing to attach to` |
| `--human-accept` | `--human-accept requires --accept-actor and --accept-reason; acceptance is a separate, attributed acknowledgement` |
| `--submit-wait` | `--submit-wait only applies to a dispatched prompt` |
| `--resume-handoff` | `--resume-handoff continues a run on disk, so it requires --confirm-live-run` |
| `--omlx-url http://localhost:8000/v1` | `malformed oMLX endpoint or nonliteral host` |
| `--protocol-sha256 abc123` | `--protocol-sha256 must be 64 lowercase hex, got 'abc123'` |
| `--root <repo>/x` | `--root targets …, which is the live project repository … or inside it` |
| `--root relative-root` | `--root must be an absolute path, got 'relative-root'` |
| `--stop-after ceo` | argparse error; choices are `provision, allocate, dispatch, deliver, cleanup, close` (`ceo` is **not** a stop point) |

Also ✅: the phase-1/phase-2 flag *tails* (`--stop-after allocate --keep-root`,
`--stop-after deliver --keep-root --worker-report-file <path>`) parse cleanly.

---

## 2. Prerequisites (all checkable read-only)

| # | Prerequisite | How to check | State now |
| --- | --- | --- | --- |
| P1 | Code at `23372d5`, runner + tests identical to HEAD | `git rev-parse HEAD`; `git status --porcelain scripts/m606_opencode_e2e.py tests/test_m606_runner.py` (empty) | ✅ |
| P2 | `.venv/bin/python` ≥ 3.13 | `.venv/bin/python -V` → `3.13.14` | ✅ |
| P3 | Herdr server running, client/server equal, protocol compatible, no restart pending | `herdr status` | ✅ via Step 2's `--preflight` (fields in §1.3) |
| P4 | Loopback oMLX healthy and serving the pinned model | `curl -s http://127.0.0.1:8000/v1/models` | ✅ via Step 2's `--preflight` (same GET: `health=healthy`, `pinned_model_present=true`) |
| P5 | **Two distinct panes hosting `opencode`, both `idle`** | `herdr api snapshot` → `agents[].agent`, `.agent_status`, `.agent_session.value`, `.cwd` (command in §3.3) | ❌ one opencode pane only, and it reported `working` early in this pass and `blocked` later — neither is `idle` (B1) |
| P6 | Worker pane cwd = `<ALLOC_CWD>` (exactly), CEO pane cwd = the disposable project root `ROOT/project` | `herdr pane get <pane>` / snapshot `cwd` | ❌ both currently at `<REPO>` (B2) |
| P7 | Operator can attest each pane's **selected** model | read the pane: the pinned model id must be visible in its UI; write it into the attestation (§3.3) | ⏳ needs eyes (B3) |
| P8 | `ROOT` absent (fresh, disposable, outside `<REPO>`) | `test -e "$ROOT"` | ✅ `<TMP>/m606-smoke-23372d5` absent |
| P9 | Live-DB baseline stamped (content, not mtime — see B11) | snippet below | ✅ `user_version=17`, `journal_mode=wal`, `projects=1` (0 under a disposable root), `ceo_initializations=[(initialized, assumed_default, ceo)]`, `wakes=0` |
| P10 | Schema head | `.venv/bin/python -c "import teamcraft.storage as s;print(s.SCHEMA_VERSION)"` → `17` | ✅ |

**Live-DB baseline stamp** (read-only; run before Step 4 and again after cleanup —
the numbers must match, and nothing may reference `$ROOT`):
```sh
DB=<REPO>/.teamcraft/teamcraft.db
sqlite3 "file:$DB?mode=ro" <<'SQL'
select 'projects_total',        count(*) from projects;
select 'projects_under_root',   count(*) from projects
  where repo_path like '%/m606-smoke-%';
select 'ceo_init',              state || '/' || receipt_source || '/' || canonical_ceo_name
  from ceo_initializations;
select 'wakes',                count(*) from wakes;
SQL
```
Baseline on this host at authoring time: `projects_total=1`,
`projects_under_root=0`, `ceo_init=initialized/assumed_default/ceo`, `wakes=0`.
Note that last row: **the live project already holds an `assumed_default` CEO
receipt from earlier work** — it is not evidence from this smoke, and must not be
re-presented as such (B11).

---

## 3. Prepare the operator artifacts

### 3.1 Step 1 — first runnable command (inert, no side effects) ✅
```sh
cd <REPO> && ROOT="${TMPDIR%/}/m606-smoke-23372d5" \
  && .venv/bin/python scripts/m606_opencode_e2e.py --plan --json --root "$ROOT"
```
Expect exit `0`, `mode: "plan"`, `would_execute: false`,
`created_or_mutation_issued: "none"`, `root_would_be_created: true`, and
`test -e "$ROOT"` still false. Read `gates.G7_ceo_bootstrap`,
`gates.G7_ceo_bootstrap_policy`, `gates.G8_wake`, `gates.pg7_ceo` to confirm the
leg you are about to run is the one you intend.

### 3.2 Step 2 — read-only host preflight ✅
```sh
cd <REPO> && ROOT="${TMPDIR%/}/m606-smoke-23372d5" \
  && .venv/bin/python scripts/m606_opencode_e2e.py --preflight --json --root "$ROOT"
```
Expect `writable: false` + every row of §1.3. **STOP here** if any guard aborts
(exit `2`): `opencode`/`herdr` missing, protocol skew, `restart_needed=yes`,
server not running, incompatible endpoint, or the pinned model absent.

### 3.3 Step 3 — discover panes and write the attestations
```sh
herdr api snapshot | jq -r '.result.snapshot.agents[]
  | [.pane_id, .terminal_id, .agent, .agent_status, (.cwd // "?"),
     (.foreground_cwd // "-"), (.agent_session.value // "-")] | @tsv'
```
Observed on this host at authoring time (redacted per §0): two panes in one tab —
one `codex` (`idle`), one `opencode` (**not** idle). No row satisfies
`agent=opencode` + `agent_status=idle` twice, so pane selection cannot be completed
today (B1/B2):

```text
<PANE_A>   <TERMINAL_A>   codex     idle     <HOME>/…   -   <redacted-session>
<PANE_B>   <TERMINAL_B>   opencode  blocked  <HOME>/…   -   <redacted-session>
```
Neither row is usable as a worker today: `<PANE_A>` hosts `codex` (wrong agent kind
for `--herdr-agent opencode`) and `<PANE_B>` is not idle.

Select two rows where `agent == "opencode"` and `agent_status == "idle"`; the
snapshot's `agents[]` (and `panes[]`) already expose `pane_id`, `terminal_id`,
`cwd`, `foreground_cwd` and `agent_session.value` — that is exactly the set the
runner re-reads, so capture it here rather than guessing.

Worker attestation (required by `--pane-model-evidence`; keys and matching rules are
🧪 `PaneGateTests`), saved as e.g. `"$ROOT"-attest-worker.json`:
```json
{
  "pane_id": "<WORKER_PANE>",
  "terminal_id": "<WORKER_TERMINAL>",
  "session_id": "<WORKER_SESSION>",
  "agent": "opencode",
  "model": "Qwen3.8-Flash-Next-oQ4e-mtp",
  "provenance": "operator <handle> read the model selector in pane <WORKER_PANE> at <ISO-8601>"
}
```
Rules the runner enforces: `model` must equal the pin (no substitution); every
present identity field must equal the live binding; a missing/invalid `provenance`
is refused ("an unattributed claim is not evidence"); the file must exist.
Result is recorded as `operator-attested` with `host_trusted: false` — never more.

A matching attestation for the **CEO** pane has no CLI flag; write it as a run note
(e.g. `"$ROOT"-ceo-pane-attestation.json`) and reference it from the manifest
narrative afterwards. It corroborates the operator's observation only — the runner
consumes nothing from it, and it can never raise a receipt:
```json
{
  "pane_id": "<CEO_PANE>", "terminal_id": "<CEO_TERMINAL>",
  "session_id": "<CEO_SESSION>", "agent": "opencode",
  "model": "Qwen3.8-Flash-Next-oQ4e-mtp",
  "provenance": "operator <handle> read the model selector in pane <CEO_PANE> at <ISO-8601>",
  "status": "run-note, not a runner input"
}
```

Optional CEO claim file (`--ceo-claim-file`): the CEO agent's own echo of the
challenge values. It is retained as **unverified audit material only** — it can
neither initialize the project nor upgrade the receipt
🧪 `CeoBootstrapTests.test_agent_claim_never_initializes_nor_upgrades_the_receipt`.

---

## 4. The run

### 4.1 Step 4 — phase 1: provision + allocate, hand off, keep root (⏳ creates the disposable world)
```sh
cd <REPO> && ROOT="${TMPDIR%/}/m606-smoke-23372d5" \
  && .venv/bin/python scripts/m606_opencode_e2e.py \
       --confirm-live-run --root "$ROOT" --stop-after allocate --keep-root --json
```
Submits nothing (`submit_allowed_in_this_invocation: false` 🧪
`PaneGateTests.test_single_invocation_live_dispatch_is_refused`). Read back
`$ROOT/evidence/handoff.json` for `allocated_cwd`, `task_id`, `attempt_id`,
`brief_digest`, `project_id`, `run_id`, `protocol`, `model`, `omlx_url`,
`capability_digests`.

### 4.2 Step 5 — operator actions between phases (manual, and the reason phase 2 exists)
1. `cd <WORKER_PANE>` into `allocated_cwd` (exact path; a suffix or symlink target
   fails the gate).
2. `cd <CEO_PANE>` into `"$ROOT/project"` — the disposable **project root**. The
   runner deliberately allows the allocated worktree only when the CEO *is* the
   worker pane 🧪 `CeoBootstrapTests.test_ceo_pane_outside_the_disposable_world_is_refused`.
3. Confirm both panes are `idle`; note both native session ids.
4. Confirm the **CEO pane's** selected model visually and record it in the run
   notes — the CLI has no attestation channel for the CEO pane (B3).

### 4.3 Step 6 — consent-free gate rehearsal (recommended; still submits nothing)
```sh
.venv/bin/python scripts/m606_opencode_e2e.py --confirm-live-run --root "$ROOT" \
  --resume-handoff --herdr-pane <WORKER_PANE> --expect-session-id <WORKER_SESSION> \
  --pane-model-evidence "$ROOT-attest-worker.json" --stop-after dispatch --json
```
Without `--consent-dispatch` the resume path evaluates every gate read-only and
writes `dispatch.json` with `submit: "not-consented"` and `would_submit: true|false`
⏳. **This rehearsal cannot prove PG-7**: `--ceo-pane` requires
`--consent-dispatch`, so the CEO leg does not run here.

### 4.4 Step 7 — the PG-7 + worker + Wake command (first fully attributed command) ⏳
```sh
.venv/bin/python scripts/m606_opencode_e2e.py \
  --confirm-live-run --root "$ROOT" --resume-handoff --consent-dispatch \
  --herdr-pane <WORKER_PANE>  --pane-model-evidence "$ROOT-attest-worker.json" \
  --expect-session-id <WORKER_SESSION> \
  --ceo-pane <CEO_PANE> --ceo-expect-session-id <CEO_SESSION> \
  --ceo-claim-file "$ROOT-ceo-claim.json" --wake-deliver \
  --stop-after dispatch --keep-root --json
```
Order is fixed by the code: **CEO leg runs before the worker gate**; a CEO submit
that is unresolved halts the whole leg, so the worker is never prompted and the
challenge stays fenced 🧪 `CeoBootstrapTests.test_ceo_leg_happens_before_the_worker_prompt`,
`.test_a_lost_ceo_submit_leaves_the_challenge_fenced`.
Exactly three prompts, each once: CEO challenge, worker task, Wake
🧪 `WakeIntegrationTests.test_delivered_wake_records_submitted_but_never_acknowledged`.
Drop `--wake-deliver` to exercise the honest admit-only path instead
(`transport: "admitted-not-delivered"`, row left `sending`, no outcome claimed).

### 4.5 Step 8 — delivery leg with an external WorkerReport ⏳
Author the report **after** the run, from `handoff.json` values (schema v1, exact key
set; `milestone_id` must be declared by the Attempt Brief — this scenario uses
`final`, and a `final` report must carry `outcome ∈ completed|blocked|failed`):
```json
{
  "schema_version": 1, "task_id": "<TASK_ID>", "attempt_id": "<ATTEMPT_ID>",
  "task_version": <from handoff/brief>, "brief_digest": "<BRIEF_DIGEST>",
  "report_kind": "final", "outcome": "completed", "milestone_id": "final",
  "summary": "<what the agent actually did, ≤ 4096 bytes>",
  "artifact_digest": null, "usage": {"total_tokens": null}
}
```
Continue without resubmitting the prompt:

```sh
.venv/bin/python scripts/m606_opencode_e2e.py --confirm-live-run \
  --root "$ROOT" --resume-handoff --post-dispatch \
  --worker-report-file "$REPORT" --stop-after deliver --keep-root --json
```

Set `REPORT` to the path of the final WorkerReport JSON. This mode requires
proven prior submission and rejects UNKNOWN or a mismatched
report. It records blocked and failed reports without advancing to verification.
Acceptance remains a separate attributed decision after verification.

### 4.6 Step 9 — cleanup
```sh
.venv/bin/python scripts/m606_opencode_e2e.py --cleanup-only --root "$ROOT" --json
```
Removes **only** this run's journalled paths 🧪
`DeliveryCleanupTests.test_journal_refuses_to_sweep_foreign_siblings`.
`--stop-after cleanup`
does the same inline; `--keep-root` retains the tree for inspection.

---

## 5. Stop conditions

**Halt before any write / prompt (guard, exit 2):** mode conflict ✅ · root inside
`<REPO>` or relative or populated-without-journal ✅ · non-literal oMLX host ✅ ·
short protocol hash ✅ · consent/CEO flag combinations (§1.5) ✅ · `--stop-after ceo`
✅ · preflight failures (missing `opencode`/`herdr`, version skew,
`restart_needed`, incompatible endpoint, server down, pinned model absent) ✅/⏳.

**Halt at the pane gates (⏳, rules 🧪):** pane not in snapshot · wrong agent
kind · no native session id · session unstable across two reads · session ≠
`--expect-session-id` / `--ceo-expect-session-id` · pane not `idle` · worker cwd ≠
`allocated_cwd` (or its foreground cwd differs) · CEO cwd outside
`ROOT/project` · resume drift (`resume: durable facts drifted between phases (…);
this handoff is void`) · allocated worktree gone · phase-1 capability digest no
longer registered.

**Halt inside PG-7 (⏳, 🧪):** an unresolved CEO submit (`transport != submitted`,
occupant changed, or status not observable) leaves the challenge **fenced at
`unknown`** with `receipt_source=unverified`, raises, and never resends; a consumed
or expired nonce can never be re-issued or accepted
(`CeoBootstrapTests.test_a_consumed_challenge_cannot_be_reissued`,
`CeoDomainGuaranteesTests.test_an_expired_challenge_can_never_be_accepted`);
a second canonical CEO is refused and team identity is immutable
(`CeoDomainGuaranteesTests.test_two_canonical_ceos_are_refused`,
`.test_team_identity_is_immutable_once_set`). **Recovery is reconciliation, never
re-send**: do not re-run Step 7 to "retry" a fenced challenge — that would double
prompt a real agent.

**Wake stop conditions (⏳, 🧪):** admission defers with a named reason —
`wake_sending:<id>` (one in-flight wake at a time), `wake_submitted:<id>` (the
previous wake is not yet CEO-acknowledged), or `wake_budget_exhausted:<n>/<max>`;
in every deferral the report says `transport: "not-attempted:deferred"`,
`outcome: null`, `duplicate_sends: 0`
(`WakeIntegrationTests.test_a_second_wake_cannot_be_admitted_while_one_is_unresolved`,
`.test_deferred_admission_claims_nothing`). An unresolved delivery records
`unknown` with detail "…must be reconciled and must not be re-sent"
(`.test_an_unresolved_wake_delivery_is_unknown_not_a_retry`).

**Do not proceed past a stop condition by weakening a flag.** Every refusal above
is the guard doing its job; the fix is host-side (idle the pane, fix the cwd, name
the session, attest the model), never a code or gate change.

---

## 6. Evidence needed for honest Wake / WorkerReport attribution

Read these files under `$ROOT/evidence/`; the expectations are what the offline
suite already proves and what the live run must reproduce.

| artifact | must show | mark |
| --- | --- | --- |
| `handoff.json` | `phase: allocated-awaiting-operator`, `submit_allowed_in_this_invocation: false`, exact `allocated_cwd` | 🧪 |
| `identity-binding.json` / `ceo-pane-identity.json` | two **different** pane ids, each `idle`, session stable across two reads, distinct native session ids | 🧪 |
| `pane-model-proof.json` | `status: "operator-attested"`, `attested: true`, `host_trusted: false`, `identity_fields_matched` non-empty, `evidence_sha256`; the terminal-screen observation is explicitly `conclusive: false` | 🧪/⏳ |
| `ceo-challenge.json` | `nonce_sha256` only, `nonce_chars`, `nonce_expires_at`, **no** plaintext `nonce` key | 🧪 |
| `ceo-submit.json` | exactly one CEO submit, `occupant_rechecked: true`, `occupant_same: true`, `pane_gate.cwd_kind: "project_root"` | 🧪 |
| `ceo-bootstrap.json` | `action: "bootstrapped"`, `state: "initialized"`, `receipt_source: "assumed_default"`, `identity_mode: "native"`, `fenced_before_submit: true`, `host_trusted: false`, `duplicate_sends: 1`, `replay_refused` non-null, `claim.can_initialize: false` / `claim.retained_as: "unverified audit material"` | 🧪 |
| `dispatch.json` | `state: submitted`, `transport_observation: "accepted"`, `prompt_sha256`, `session_stable: true`, `topology_unchanged: true`, `brief_digest`/`worktree_path`/`branch` matching `handoff.json`, `pane_model_proof.host_trusted: false`, `prompt_ack: "unknown"` (unless operator evidence), and the embedded `wake` object. The six `prompt_binding.carries_*` flags (alloc path, branch, base sha, brief digest, pinned model, protocol) must all be `true` — recorded in `prompt-binding.json`, not here | 🧪/⏳ |
| `wake.json` | `wake_id`; **admit-only**: `transport: "admitted-not-delivered"`, `outcome: null`, `state: "sending"`, `duplicate_sends: 0`; **delivered**: `transport: "delivered"`, `outcome: "submitted"`, `state: "submitted"`, `duplicate_sends: 1`, `delivery.target_pane == <CEO_PANE>`, and `delivery.prompt_sha256 ≠ dispatch.json.prompt_sha256` (a wake is its own message) | 🧪 |
| `wake.json` + DB | `ceo_acknowledged: false` **always**, and the durable cursor `acknowledged_seq == 0` in the `wake_cursors` row (read via `wake.get_cursor`); `acknowledge_wake` is never called 🧪 `WakeIntegrationTests.test_acknowledge_wake_is_never_called` | 🧪/⏳ |
| `run-manifest.json` → `evidence_class` | `"live-agent"` for a real pane leg; `"scripted"` only when `--scripted-worker-change` was used, in which case `scripted-worker-leg.json.real_agent_evidence: false` and the run **cannot** be cited as agent evidence | 🧪 |
| WorkerReport (file + DB) | keys exactly as §4.5; `brief_digest`/`task_version` match the immutable Attempt Brief; `outcome` present only on `final`; verifiers (`verification.json`) must pass **from repo facts**, not from the report's claims | 🧪 |
| `run-manifest.json` | `stage_matrix.ceo == "executed"` (never blanket-inferred); `ceo.receipt_source == "assumed_default"`; `non_claims.ceo_receipt` states "NOT host-verified"; `non_claims.ceo_acknowledgement: "not-asserted (never inferred from transport)"`; `non_claims.wake_transport ∈ {admitted-not-delivered, delivered, not-attempted, unreadable}`; `non_claims.pg7 == "bootstrap+wake-executed"` (else `open:<stage>`); `non_claims.host_trusted_receipt: false`; `ephemeral_leak_scan.leaks == {}` with tokens shown only as 12-hex prefixes | 🧪 |

Two claims this smoke can **never** license, whatever else passes: a
host-trusted protocol/skill receipt (`host_verified`) and a CEO acknowledgement.
Both are structurally unavailable on herdr 0.9.3 (`prompt_ack`/`event_stream`
`unsupported`) and the DB refuses provenance erasure ("CEO receipt source is
immutable once recorded").

---

## 7. Concrete blockers (as observed on this host, this pass)

* **B1 — no two idle OpenCode panes.** `herdr api snapshot` shows one workspace,
  one tab, two panes: one `codex` (`idle`) and one `opencode` that reported
  `agent_status: "working"` early in this pass and `blocked` later — neither reading
  is `idle`. The run needs **two** panes hosting `opencode`, both `idle`. The
  runner cannot create one (topology verbs are structurally forbidden) — the operator
  must open/convert a second OpenCode pane and let both reach idle. Note that a
  `blocked` pane is also a live agent that may be mid-task: do not send anything to
  it to "check".
* **B2 — cwd is the live repo.** Both panes currently report `cwd = <REPO>`. The
  worker gate requires exactly `allocated_cwd`; the CEO gate requires `ROOT/project`.
  Both refuse. Fix only after phase 1 (§4.2) creates those paths.
* **B3 — pane-side model is unverifiable by tooling.** `prompt_ack` and
  `event_stream` are `unsupported`, so model selection is only
  operator-attested; and there is **no** attestation flag for the CEO pane. The CEO
  pane's model must be confirmed by eye and recorded in the run notes, and the
  manifest must still say `host_trusted: false`.
* **B4 — PG-7 executes through uncommitted peer edits.** `teamcraft/{delivery,domain,
  recovery,runtime_adapter,worktrees}.py` are modified in the working tree while
  `scripts/m606_opencode_e2e.py` and `tests/test_m606_runner.py` are clean at
  `23372d5`. A run labelled "23372d5" therefore executes peer code. Either have the
  peer changes committed (then re-pin) or record `git status --porcelain` in the run
  evidence and label the run `23372d5+dirty`. Do not stash or revert them here.
* **B5 — `--plan` cannot validate the live combination.** Because the mode flags are
  mutually exclusive, the first time the phase-2 CEO+worker combination is guard-
  checked is inside the executing invocation. Until then, the authority is the
  offline suite (§1.4/§1.5). Plan accordingly: run Step 6 before Step 7.
* **B6 — the WorkerReport cannot be pre-filled.** `task_id`, `attempt_id`,
  `task_version` and `brief_digest` exist only after phase 1; a delivery leg with a
  stale digest is refused. `milestone_id: "final"` is the declared milestone for
  this scenario's brief.
* **B7 — idleness races the gate.** The worker pane must be `idle` *at gate time*;
  if the agent starts working between Step 5 and Step 7, the gate aborts before any
  submit (that refusal is correct — re-check, don't bypass).
* **B8 — do not reuse the older retained root.** `<TMP>/m606-live-handoff-b70e884`
  is a phase-1 handoff from the pre-PG-7 commit. Fresh smoke → fresh
  `m606-smoke-23372d5` root; mixing roots splits attribution across commits.
* **B9 — preflight leaks shareable identifiers.** `herdr_status["server.socket"]`
  contains the operator's `$HOME`; `--json` payloads contain absolute paths and real
  pane/session ids. Apply §0 before publishing evidence.
* **B10 — housekeeping leftovers (not removed in this slice, ownership limited to
  this doc).** Prior scratch directories are outside this run. This procedure
  only reclaims journalled resources belonging to the selected smoke run.
* **B11 — a co-resident TeamCraft API server is attached to this repository, and the
  live DB is in WAL mode.** Observed: a `screen` session `teamcraft-web-8765`
  running `… -m teamcraft.api --project-root <REPO>`, plus two older
  `teamcraft.api` processes pointed at other roots; `.teamcraft/` also holds
  `web-8765.log`. Consequences:
  * the live DB's **mtime is not a valid "untouched" signal** — it moved twice
    during this prep pass while only `--plan`/`--preflight`/read-only `herdr` calls
    were run (WAL commits change mtime without changing the main file size);
  * the invariant to use is the **content stamp** in §2 (row P9): identical counts,
    `projects_under_root = 0`, and no row anywhere referencing this run's
    `<PROJECT_ID>` / `<RUN_ID>` / `$ROOT`;
  * the live DB **already** contains `ceo_initializations =
    initialized/assumed_default/ceo` from earlier work on the real project. PG-7 on
    this host is therefore *already partly claimed* in the live store, and a smoke
    run at a disposable root neither confirms nor extends it. Never present the live
    row as this smoke's result, and never copy it into run evidence;
  * optionally stop the server for the duration of the run for a cleaner bracket —
    that is an operator decision outside this runner's permissions (it is a
    lifecycle action, and the runner forbids lifecycle verbs by design).

---

## 8. Definition of done for the smoke run

1. Step 1 and Step 2 exit `0` with `would_execute: false` / `writable: false`.
2. Phase 1 produces `handoff.json` with an `allocated_cwd` that exists.
3. Step 6 (no consent) shows `submit: "not-consented"` — proving the consent gate.
4. Step 7 produces `ceo-bootstrap.json`, `dispatch.json`, `wake.json` with the
   field values in §6, three distinct prompt digests, `ceo_acknowledged: false`,
   `acknowledged_seq == 0`, `host_trusted: false`, `receipt_source:
   assumed_default`, and `ephemeral_leak_scan.leaks == {}`.
5. The worker's `src/feature.py` change carries `M606-LIVE`, verifiers pass from
   repo facts, and the disposition is `submitted` — `accepted` only with an explicit,
   attributed `--human-accept`.
6. `--cleanup-only` (or `--stop-after cleanup`) removes only this run's journalled
   paths; foreign roots count unchanged (255 + 1); the §2 content stamp for the live
   DB is byte-for-byte the same set of answers as before the run, with
   `projects_under_root = 0` (mtime is not the invariant — B11); and every SQLite
   path the run touched is under `$ROOT` — both `storage.connect` call sites use
   `self.root / "teamcraft.db"`, and `isolation.json` names only `$ROOT` and
   `<REPO>`.
7. Any blocked step is reported with its refusal text and its blocker id (B1–B11),
   not by relaxing a gate.
