# M6-06 — OpenCode live-handoff record (phase 1 against `b70e884`)

| | |
|---|---|
| **Purpose** | Prepare one disposable run of the real OpenCode leg: allocate the scenario, verify it on disk, and hand the operator an exact, already-proven-against gate continuation |
| **Target commit** | `b70e88485503bcf393e0b56df7f5b8b29b969528` ("Add isolated two-phase M6-06 OpenCode E2E runner") — the tip of local `main` at the time of this run |
| **Modes executed** | `--plan`, `--preflight`, phase 1 (`--confirm-live-run --stop-after allocate --keep-root`) |
| **Modes NOT executed** | dispatch, delivery, cleanup, `--resume-handoff`, any prompt, any spawn, any cloud call |
| **Runner** | `scripts/m606_opencode_e2e.py` @ `b70e884` (+ one operator-guidance fix, §8) |
| **Status** | **Phase 1 complete and retained. Phase 2 is available but deliberately not run.** |

## 0. Sanitization applied to this document

Personal and live-host values are replaced by placeholders. Nothing below contains a
home directory path, a native session UUID, or a terminal id.

| Placeholder | Stands for | How to recover the real value |
|---|---|---|
| `<TMP>` | the host temp directory holding the run root | `cd "$TMPDIR"` — the root is `m606-live-handoff-b70e884` beneath it |
| `<REPO>` | the TeamCraft checkout used to run the script | `git rev-parse --show-toplevel` |
| `<HOME>` | operator home directory | `$HOME` |
| `<ses:opencode>`, `<ses:codex>` | native session ids of the two live panes | `herdr agent list` |
| `<term:…>` | Herdr terminal ids | `herdr agent list` |

Disposable-scenario identifiers (run/project/task/attempt/allocation ids, branch,
base sha, digests, protocol pair) are **kept**: they are throwaway values created by
this run and are required to verify and continue it. Pane/tab identifiers
(`w3:p4`, `w3:p2Q`, `w3:t1`) are kept but are point-in-time only — re-read the
topology before acting. A path written as `<HOME>/…` is a placeholder plus its
XDG-relative remainder (e.g. the Herdr socket name), never a literal personal path.

## 1. Inputs and pins (all re-read at run time, none hard-coded)

| Item | Value | Check |
|---|---|---|
| Protocol | `1.0.0` / `c5c358bf5e1654bc902579972acda4b29984ffde171a547559d289b20ef0a742` | `pinned_match: true` against `<REPO>/.agents/skills/teamcraft-ceo/references` |
| oMLX route | `http://127.0.0.1:8000/v1` (literal loopback IP) | `health: healthy`, `reason_code: ok`, source `/v1/models` |
| Pinned model | `Qwen3.8-Flash-Next-oQ4e-mtp` | present in loopback catalog |
| Catalog contents | `Qwen3.8-Flash-Next-4bit-mtp`, `Qwen3.8-Flash-Next-oQ4e-mtp` | loopback only — no cloud route contacted |
| Provider capabilities | `inference_request: supported`; `prompt_ack: unsupported`; `event_stream: unsupported` | recorded, not assumed (§7 non-claims) |
| Herdr | client 0.9.3 / server 0.9.3, private protocol 22, `endpoint_compatible: yes`, `private_protocol_compatible: yes`, `update.restart_needed: no` | preflight gate G3 |
| Schema | `SCHEMA_VERSION = 17`; fresh run-local DB migrated `0 → 17` | never the live runtime DB |
| Harness binaries | `herdr`, `opencode`, `codex` present; `claude` **absent** | PG-1 remains open |
| Socket | `<HOME>/.config/herdr/herdr.sock`, `server.status: running` | read-only |

## 2. Modes executed, with results

| # | Invocation | Exit | Filesystem effect |
|---|---|---|---|
| 1 | `--plan --root <TMP>/m606-live-handoff-b70e884 --json` | `0` | **none** — root absent afterwards; `created_or_mutation_issued: "none"`, `would_execute: false` |
| 2 | `--preflight …` | `0` | **none** — root absent afterwards; `root_would_be_created: true`, `writable: false` |
| 3 | `--confirm-live-run … --stop-after allocate --keep-root --json` | `0` | root created and **retained** (`root_retained: true`) |
| 4 | single-invocation live dispatch against the allocated root | `2` | refused, wrote nothing (§5) |

`--plan` reported `G1_agent_submit: not-released` and
`operator_handoff.single_invocation_live_dispatch: "refused by design"`.

Rows 1–3 were run a second time against a throwaway root, purely to bracket the live
runtime DB before and after each mode (§8); that root was reclaimed immediately and is
not part of the delivered handoff.

## 3. Phase-1 evidence — 13 postconditions, 13 passing

| Step | Claim |
|---|---|
| provision | seed push targeted the local bare origin only |
| provision | clone HEAD equals the seeded base |
| provision | installer dry-run wrote nothing |
| provision | provider still loopback oMLX after provisioning |
| provision | pinned model still in loopback catalog |
| provision | catalog presence alone is not pane proof |
| provision | fresh DB at schema head |
| provision | root is outside the project repository |
| capabilities | harness probe persisted durably before bind |
| capabilities | provider probe persisted durably before bind |
| allocate | handoff names the exact allocated cwd |
| allocate | phase 1 submitted no prompt |
| allocate | phase 1 created no worker evidence |

Stage matrix: `provision: executed`, `allocate: executed`,
`dispatch / deliver / cleanup / close: pending`.
`evidence_class: "none"` (no agent worked), `delivery.disposition: "unknown"`.

Artifacts (11 files in `<root>/evidence/`): `handoff.json`, `route-gate.json`,
`isolation.json`, `git-locality.json`, `installer.json`, `protocol-verify.json`,
`capability-binding.json`, `run-manifest.json`, `postconditions.json`,
`evidence-index.json`, `journal.jsonl`.

All 9 digest entries recorded in `evidence-index.json` were re-hashed at the end of
this slice and every one matched the file on disk — including `handoff.json`
(`6932be2b39ba97ad…`) — so the retained handoff has not drifted since phase 1. A
credential-shape sweep across the persisted JSON found nothing, and the recorded
`redactions` map is empty (no screen text was read, because no pane was touched).

Route gate ordering is recorded as
`gated_before: "any filesystem or database write"` — the loopback/model check runs
before `mkdir`, so a dead route aborts a pristine filesystem instead of leaving a
half-built world.

Git locality: `local_origin_only: true`, `delivery_verbs: []`, bootstrap verbs
`{init, clone, remote add, push, -c, add}` — the only `push` targeted this run's own
bare `origin.git` to give the clone a base commit. `origin.git` refs:
`refs/heads/main` only; **no additional ref was created and nothing left the host.**

## 4. Paths verified locally (not asserted from JSON)

`allocated_cwd` = `<TMP>/m606-live-handoff-b70e884/project/.teamcraft/worktrees/c2b63b6c-…`
(full value in `<root>/evidence/handoff.json`, recoverable via
`find "$TMPDIR" -maxdepth 1 -name 'm606-live-handoff-b70e884'`).

| Independent check | Result |
|---|---|
| `test -d <allocated_cwd>` | exists |
| `realpath` equals the recorded path | yes (no symlink indirection) |
| outside `<REPO>` | yes |
| `git rev-parse --is-inside-work-tree` | `true` |
| `git rev-parse --abbrev-ref HEAD` == `handoff.branch` | `tc/0e3c695c-…/c2b63b6c-…` — matches |
| `git rev-parse HEAD` == `handoff.base_sha` | `224970181262168d74dcf153d10e1ff812a90cd6` — matches |
| `git rev-parse --verify <base_sha>` | resolves in the worktree |
| `git rev-list --count <base_sha>..<branch>` | `0` — no work committed yet |
| `git status --porcelain` in the worktree | empty — clean allocation |
| `git worktree list` (from the clone) | 2 worktrees: clone + the allocated one |
| `git remote -v` on `origin.git` | local path only; refs = `refs/heads/main` |

### 4.1 What "against `b70e884`" does and does not mean here

Honest distinction, because it is easy to overstate:

* **The code under test is `b70e884` + working-tree state.** The runner imports
  `teamcraft` from `<REPO>` and installs into the clone via
  `<REPO>/scripts/install.sh`. Preflight recorded `repo_dirty_entries: 15`, which
  includes pre-existing uncommitted edits to `teamcraft/{delivery,domain,recovery,
  runtime_adapter,worktrees}.py`. So this is *not* a clean-tree attestation of
  `b70e884`.
* **The git world of the run is synthetic.** `base_sha 22497018…` is a seeded
  commit authored by the fixture inside `<root>/origin.git`; it is **not**
  `b70e884` and does not contain TeamCraft's history. The disposable scenario exists
  to exercise allocation → gate → delivery mechanics without touching the real repo.
* `b70e884` had already been pushed to GitHub. The local `origin/main` tracking
  ref was stale because the push used a direct HTTPS URL.

## 5. Why phase 2 refuses right now (verified, correct behavior)

The pane gate is evaluated before anything is sent. Observed on this host during this
slice (re-read at run time — never trust this table on a later day):

| Pane | Agent | Status | cwd |
|---|---|---|---|
| `w3:p4` | codex | `done` | `<REPO>` |
| `w3:p2Q` | opencode | **`working`** | `<REPO>` |

Consequences for `--herdr-pane <opencode-pane>` against this handoff:

1. `agent_status` is `working`, not `idle` → refusal (`not idle`).
2. pane `cwd` is `<REPO>`, not `<allocated_cwd>` → refusal (`cwd … not this run`).
3. `foreground_cwd` likewise mismatches.

A single-invocation live dispatch attempt against the allocated root exits `2` with:

> `--root <TMP>/m606-live-handoff-b70e884 is an allocated phase-1 handoff
> (['evidence', 'origin.git', 'project', 'seed']…), not a fresh root: continue it
> with --resume-handoff (see its evidence/handoff.json for the pane, cwd and
> attestation it is waiting on). Use --cleanup-only only if you intend to discard
> that handoff`

and created/wrote nothing — the root and its 11 evidence files were byte-identical
after the refusal.

## 6. Operator handoff (what must happen before any prompt)

### 6.1 Handoff facts (`evidence/handoff.json`)

| Field | Value |
|---|---|
| `phase` | `allocated-awaiting-operator` |
| `run_id` | `084aaaba7411e90f` |
| `project_id` | `86a276f8-f8d4-4078-8cfb-de957af744cc` |
| `task_id` / `attempt_id` | `0e3c695c-05e2-4c34-a904-532c2bc7a37e` / `c2b63b6c-42ea-410c-84f4-f2a75c6da037` |
| `allocation_id` | `wta-7d61747110ada9f450def5572382fd6f` |
| `branch` | `tc/0e3c695c-…/c2b63b6c-…` |
| `base_sha` | `224970181262168d74dcf153d10e1ff812a90cd6` |
| `claims` | `src/**` |
| `brief_digest` | `9fddf731630cf187f620f250831260b4685a4428bb2af7a30bd2d5db555f7741` |
| `model` / `omlx_url` | `Qwen3.8-Flash-Next-oQ4e-mtp` / `http://127.0.0.1:8000/v1` |
| capability digests (durable, pre-bind) | provider `14e4cde0136af41c…`, harness `cceda651d01e2290…` |
| `submit_allowed_in_this_invocation` | **`false`** |

### 6.2 Operator actions

1. `cd` an **existing** OpenCode pane into `<allocated_cwd>` (§4). The runner never
   creates a pane and never moves one.
2. Confirm that pane is **idle** and note its native session id → `<ses:opencode>`.
3. Write an attestation file naming the pane identity and the model that pane is
   actually running:

   ```json
   {
     "pane_id": "<pane>",
     "terminal_id": "<term:…>",
     "session_id": "<ses:opencode>",
     "agent": "opencode",
     "model": "Qwen3.8-Flash-Next-oQ4e-mtp",
     "provenance": "read from the pane status line at <time>"
   }
   ```

   The runner validates this against the live pane it is about to prompt
   (pane / terminal / session / agent) **and** against the pinned model. It is
   recorded as `operator-attested`, `host_trusted: false`. Missing, mismatched,
   foreign, or unattributed evidence fails closed. Catalog presence alone is never
   sufficient: it proves what the server serves, not what the pane selected.
4. Phase 2 — re-proves every durable fact, then submits **once**:

   ```bash
   .venv/bin/python scripts/m606_opencode_e2e.py \
     --confirm-live-run --root <TMP>/m606-live-handoff-b70e884 \
     --resume-handoff --consent-dispatch \
     --herdr-pane <pane> \
     --pane-model-evidence <attestation.json> \
     --expect-session-id <ses:opencode>
   ```

   Phase 2 re-validates project root, task/attempt/allocation, branch, base sha,
   brief digest, model, worktree existence, and that both phase-1 probe digests are
   *still* registered (`revalidated_on_resume: true`) — it does **not** re-probe.
   Any drift voids the handoff. The prompt itself carries the allocated path, branch,
   base sha, brief digest, pinned model and protocol pair; only its digest, length and
   those six binding booleans are persisted, never the text.

## 7. PG-7 CEO bootstrap — exact requirements, measured against this world

`teamcraft/ceo_initialization.py` is the gate. Probed read-only against this run's
disposable DB:

```
require_initialized  → CEOInitializationGateClosed: CEO initialization is missing;
                       Scheduler dispatch is blocked
discover_and_issue   → CEOInitializationError: project must have exactly one
                       canonical CEO member; found []
```

| Requirement for `require_initialized` to open | State in this disposable world |
|---|---|
| exactly one `members` row with `role='ceo'`, `kind ∈ {codex, opencode}`, canonical name | **missing** (0 member rows) |
| `runtime_meta` id=1: `team_id`, `project_root` == project repo path, valid Herdr team session identity json | **missing** (0 rows) |
| CEO member bound to a TeamCraft Session + `terminal_id` + `attempt_id`, and member `agent_session_json.value` == session `native_session_id` | **missing** (0 sessions carry a native id) |
| session liveness `idle`/`working` | **missing** (phase 1 recorded `unknown`, no pane — deliberately, since no pane was designated) |
| CEO attempt carries the immutable protocol pin (`protocol_version` + `protocol_hash`) | **present** (`1.0.0` / `c5c358bf…`) |
| `<project>/.agents/skills/teamcraft/SKILL.md`, not a symlink | **present** (10 435 B, installed by `install.sh`) |
| `<project>/.agents/skills/teamcraft-ceo/references` with `CEO_PROTOCOL.md` + `protocol.manifest.json` | **present** |
| a live OpenCode pane whose native session id can be observed | **not yet** — no idle pane in `<allocated_cwd>` (§5) |
| leader lease for the fence/record calls | available to the runner (`acquire_or_renew`) |

### 7.1 The structural finding

PG-7 CEO bootstrap **is itself a dispatch action**; it cannot be pre-staged:

1. `discover_and_issue` returns the nonce plaintext **once** and never persists it
   (hash only), TTL default 300 s.
2. `admit_ceo_bootstrap_effect` → `mark_bootstrap_unknown` fences the challenge
   *immediately before* the Herdr prompt and requires a lease.
3. The prompt must actually reach a live CEO pane and return success.
4. `record_bootstrap_submitted_assumed` then pins the **post-submit** native session
   id + session state, after the caller rechecked the same live occupant, writing
   `receipt_source = "assumed_default"`.

So the bootstrap must complete inside one invocation, at the moment of a real prompt,
against a pane registered as the project's canonical CEO. **Phase 1 cannot and must
not perform it**, and this slice did not attempt it. Nothing was self-initialized:
`ceo_initializations` remains empty here, and where the wake path meets this gate the
runner records `blocked:ceo-initialization` / `pending:admission-blocked` with
`ceo_acknowledged: false` rather than bypassing it (`record_submission_result` is
explicitly not a CEO acknowledgement).

**Net for the operator:** after a successful phase-2 prompt, the dispatch leg can
complete, but the **wake** leg will still be recorded as CEO-blocked until the
bootstrap above is performed by whoever holds the CEO pane. A run that dispatches a
worker prompt is therefore honest evidence of *prompt transport*, not of *wake*.

## 8. Footprint this slice created, changed, and did not change

**Created (and kept):**
* one run root — `<TMP>/m606-live-handoff-b70e884` — **retained** so the handoff
  stays usable; it is the only `m606-live-*` root on this host, and its provenance
  journal lists exactly the 6 paths it owns (root, `origin.git`, `seed`, `project`,
  `teamcraft.db`, worktree). Reclaim later with
  `--cleanup-only --root <TMP>/m606-live-handoff-b70e884`.
* this document, `doc/M6_06_OPENCODE_HANDOFF.md`.

**Created then reclaimed (transient, for measurement only):**
* one extra root — `<TMP>/m606-live-dbproof-*` — created solely to bracket the live
  DB across `plan → preflight → phase 1` (§8), then removed with the runner's own
  `--cleanup-only` (exit `0`). After that, exactly **one** `m606-live-*` root exists
  on this host: the retained handoff above.

**Changed in the runner (1 focused fix, with test):** the populated-root refusal
previously told an operator holding an allocated handoff to use `--cleanup-only` —
i.e. it suggested deleting the very scenario they were mid-way through. It now names
the root as a phase-1 handoff and points at `--resume-handoff`, offering
`--cleanup-only` only as an explicit discard. Covered by
`test_a_root_awaiting_phase_two_points_at_resume_not_deletion`.

**Two-file suite:** `tests/test_m606_runner.py` + `tests/test_local_e2e_m606.py` →
**88 passed, 0 failed, 54 subtests passed** (~62 s).

**Not changed:**
* **Live runtime DB** `<REPO>/.teamcraft/teamcraft.db` — bracketed `size:mtime`
  around a fresh `plan → preflight → phase 1` cycle:
  `917504:1795352289` before, and identical after each of the three modes and at the
  end. Every run uses its own run-local DB (`<root>/teamcraft.db`, 405 504 B,
  migrated `0 → 17`); the live DB was never opened by the runner.
* **Live Herdr topology** — structural digest `2dd133a0d309340f…` identical before and
  after this slice (same workspace, tab, panes, agents, agent session values). No
  pane created, closed, focused, or renamed; zero prompts sent. The digest is
  structural by design: it excludes volatile fields such as `agent_status` and `cwd`,
  so it would *not* by itself catch a cwd change — which is exactly why the dispatch
  path re-reads full pane identity rather than trusting this digest.
* **Foreign temp roots** left untouched: 255 `m606-e2e-*` and 1 `m606-probe-*` (not
  created by me; my ledger structurally refuses to sweep them — reclaim is operator
  action HG-A). Note for the record: that count stayed at exactly **255 across both
  two-file suite runs in this slice**, so the per-test root leak that
  `doc/M6_06_HOST_GATE_RESULTS.md` §8 item 5 reported ("+23 roots per suite run") no
  longer reproduces — it was closed by `c401fc9` ("Clean up only owned M6-06 fixture
  directories"). §8 item 5 and the H-1 row are therefore stale on that point; I did
  not edit that document.
* The phase-1 run itself did not commit or push. Its source HEAD was `b70e884`.
* Pre-existing working-tree modifications preserved verbatim.

## 9. What this record does **not** claim

* **No prompt was submitted** to any pane, and no agent was spawned; `evidence_class`
  is `none` and `dispatch` is `pending`.
* `prompt_ack: "unknown"`, `event_stream: "unknown"` — Herdr 0.9.3 reports both
  `unsupported`; there is no host-trusted receipt (`host_trusted_receipt: false`).
* No delivery, no commit inside the scenario, no push anywhere;
  `delivery.disposition: "unknown"`, `refs` above base = `0`.
* No cloud model, no fallback route (`cloud: "not-permitted"`).
* Claude not exercised (`claude: "pending:PG-1"` — binary absent).
* PG-7 remains **open**: no init handshake, no real dispatch, no real wake.
* The pane-model proof for a real run rests on **operator attestation** validated
  against live pane identity — not on a trusted host receipt, and not on catalog
  presence.
* Phase 1 exercises allocation and gating, not agent behavior; the only end-to-end
  transport proof so far is offline, against a fake pane.

## 10. Reproduce

```bash
cd <REPO>
R="$(mktemp -d "${TMPDIR%/}/m606-live-XXXXXX")"      # must be outside the repo

# inert
.venv/bin/python scripts/m606_opencode_e2e.py --plan      --root "$R" --json
.venv/bin/python scripts/m606_opencode_e2e.py --preflight --root "$R" --json

# phase 1 — allocate + hand off (no prompt possible here)
.venv/bin/python scripts/m606_opencode_e2e.py \
  --confirm-live-run --root "$R" --stop-after allocate --keep-root --json

# then read the allocated cwd to act on:
.venv/bin/python -c "import json,sys;print(json.load(open(sys.argv[1]))['allocated_cwd'])" \
  "$R/evidence/handoff.json"

# … operator: place an idle OpenCode pane there, attest its model (§6.2) …

# phase 2 — the only path that may submit, exactly once
.venv/bin/python scripts/m606_opencode_e2e.py \
  --confirm-live-run --root "$R" --resume-handoff --consent-dispatch \
  --herdr-pane <pane> --pane-model-evidence <attestation.json> \
  --expect-session-id <ses:opencode>

# reclaim only what this run created
.venv/bin/python scripts/m606_opencode_e2e.py --cleanup-only --root "$R"

# offline proof of every gate and refusal (no pane, no model, no prompt)
.venv/bin/python -m pytest tests/test_m606_runner.py tests/test_local_e2e_m606.py -q
```

---
*Evidence sources, all executed this slice unless marked otherwise: `--plan`,
`--preflight`, phase-1 CLI reports and `<root>/evidence/*` digests; independent `git`
queries against `allocated_cwd`, the clone and `origin.git`; read-only `herdr tab
list` / `herdr agent list` / `herdr` status fields; loopback `/v1/models`;
`require_initialized` and `discover_and_issue` probed read-only against the run-local
DB; `stat` on `<REPO>/.teamcraft/teamcraft.db`; `git status`/`rev-parse`/
`rev-list`/`remote`. Requirements §6/§7 read from `teamcraft/ceo_initialization.py`
(`_ceo_bindings`, `discover_and_issue`, `bootstrap_prompt_fields`,
`mark_bootstrap_unknown`, `record_bootstrap_submitted_assumed`,
`require_initialized`) and `teamcraft/runtime_adapter.py`
(`prepare_ceo_bootstrap`, `admit_ceo_bootstrap_effect`,
`record_ceo_bootstrap_submission_assumed`).*
