# M6-06 — Smoke Candidate Readiness (bounded local OpenCode)

Status: **prepared, not dispatched.** No prompt was sent, no model inference was
invoked, the live `teamcraft` DB was not touched, no session id is exposed here,
and nothing is committed or pushed.

This page is the operator/CEO handoff for a *bounded* local smoke: phase‑1 is
already provisioned from clean committed code; phase‑2 (dispatch → continuation)
is gated on operator actions and two concrete blockers below.

## 0. Provenance

| Fact | Value |
| --- | --- |
| Commit provisioned from | `a2a76f3` (`Continue M6-06 delivery without replaying dispatch`) |
| Commit identity | `a2a76f38b63beda3b115b00709fa6074a5202441` |
| Source used | pristine `git archive a2a76f3` snapshot (NOT the working tree) |
| Runner vs commit | `scripts/m606_opencode_e2e.py` byte‑identical to `a2a76f3` |
| Isolation reason | working tree carries peer edits (`teamcraft/{delivery,domain,recovery,runtime_adapter,worktrees}.py`) — provisioning ran against the committed snapshot only |
| Protocol pin | `1.0.0` / `c5c358bf5e1654bc902579972acda4b29984ffde171a547559d289b20ef0a742` (`pinned_match: true`) |
| Pinned model | `Qwen3.8-Flash-Next-oQ4e-mtp` |
| Loopback route | `http://127.0.0.1:8000/v1` |

## 1. Rehearsal handoff

```
<rehearsal-root>/evidence/handoff.json
```

Disposable run root:
`<rehearsal-root>`

**Do not resume this rehearsal for live dispatch.** Its verified Git and SQLite
allocation used fake provider and harness probes. Create a separate run using
real read-only host probes before sending a real prompt.

Provisioned by: `--confirm-live-run --root "$ROOT" --stop-after allocate --keep-root`,
**offline transport seams** (fake pane + loopback-catalog seam + harness seam), so
phase‑1 created real disposable Git + run‑local SQLite + `handoff.json` while
touching no pane, no network model, and the live DB not at all.

Phase‑1 durable facts (run‑local, disposable; safe to share — contain no session id):

| Field | Value |
| --- | --- |
| `run_id` | `f9282b2c5ab2e267` |
| `task_id` | `a429e637-01d8-47f0-a1dc-f84ecd4458a3` |
| `attempt_id` | `7d65cd6d-0b7c-481d-ac02-165d80d77164` |
| `allocation_id` | `wta-8e9c755f56ff537be6d31853856d1151` |
| `branch` | `tc/a429e637-01d8-47f0-a1dc-f84ecd4458a3/7d65cd6d-0b7c-481d-ac02-165d80d77164` |
| `base_sha` | `06307ddee6b679da44b0f1a1bcee89c3bcc406b9` |
| `brief_digest` | `7fc55818d7bbc7681cec6ac3197d8e48607b44545b8970153077d93fbbb5a14d` |
| allocated cwd (worker pane target) | `…/m606-smoke-candidate-210949/project/.teamcraft/worktrees/7d65cd6d-0b7c-481d-ac02-165d80d77164` |
| project root (CEO pane target) | `…/m606-smoke-candidate-210949/project` |
| `submit_allowed_in_this_invocation` | `false` (phase‑1 never submits) |

`handoff.json` carries no session ID. This allocation is rehearsal-only;
phase 2 must use a separate allocation made with real probes.

## 2. Readiness matrix (sanitized)

Session ids are redacted, never the raw value.

### 2a. Environment gates (real, read‑only, no prompts, no inference)

| Gate | Requirement | Observed | Verdict |
| --- | --- | --- | --- |
| Local model route | loopback oMLX healthy at `127.0.0.1:8000/v1`, **no cloud fallback** | `health: healthy`, source `http://127.0.0.1:8000/v1/models` (loopback) | ✅ PASS |
| Pinned model served | `Qwen3.8-Flash-Next-oQ4e-mtp` in catalog | `pinned_model_present: true` (catalog size 2) | ✅ PASS |
| Transport protocol | client==server, compatible, no restart pending | `server.status: running`, client==server `0.9.3`, `endpoint_compatible: yes`, `private_protocol_compatible: yes`, `update.restart_needed: no` | ✅ PASS |
| `prompt_ack` receipt | n/a on this host | `unsupported` → receipts stay `assumed_default`; acknowledgement **unasserted** | ⚠️ nuance (not a blocker) |
| `event_stream` receipt | n/a on this host | `unsupported` → no host event stream; never inferred | ⚠️ nuance (not a blocker) |
| Binaries | `opencode`, `herdr` present; `claude` out of scope (PG‑1) | `opencode`✓ `codex`✓ `herdr`✓ `claude` absent | ✅ PASS |
| Protocol references | canonical pair present & pinned in snapshot | `.agents/skills/teamcraft-ceo/references` (`CEO_PROTOCOL.md`, `protocol.manifest.json`) | ✅ PASS |

Note: only the loopback `/v1/models` **catalog** was read. No inference/generation
endpoint was called; no real prompt was submitted.

### 2b. Role gates — distinct pane cwd requirements

**Worker role** (dispatch target, `gate_dispatch_target`):
- existing OpenCode pane, `agent == opencode`, `agent_status == idle`;
- native session id present and stable across two identity reads;
- session equals operator‑named `--expect-session-id`;
- pane `cwd` **exactly** the allocated worktree (foreground cwd `null` or equal);
- operator attestation naming pane/terminal/session/agent/**local** model, non‑host‑trusted;
- provider loopback route serving the pinned model (2a).

**CEO role** (PG‑7 bootstrap, `gate_ceo_target`; optional):
- distinct idle OpenCode pane with its own native session id;
- session equals `--ceo-expect-session-id`;
- pane `cwd` must be the **project root** (or the allocated worktree only when the
  CEO reuses the worker pane);
- same loopback route + protocol as worker; CEO acceptance is recorded
  `assumed_default`, never host‑verified, and never inferred.

### 2c. Live pane survey (read via `herdr api snapshot`, session ids redacted)

| Pane | Agent | Status | Native session present | Pane cwd placement |
| --- | --- | --- | --- | --- |
| `pane A` | opencode | **idle** | ✅ `redacted` | not yet in allocated worktree (worker) / project root (CEO) |
| `pane B` | opencode | **working** | ✅ `redacted` | busy — cannot host a new prompt |

Topology digest (structural, for drift detection):
`f62c4ca8c694c487681fe11b70ef96458a098d99b61b719d5959364ac2d3dd0d`

## 3. Non‑submitting report continuation (verify, never re‑dispatch)

The committed runner provides `--post-dispatch`: continue an already‑dispatched
attempt using an external **final** WorkerReport, submitting **zero** prompts.

- Plan gate (inert, confirmed against `a2a76f3`):
  `G9_post_dispatch_continuation = "available (--post-dispatch); a dispatched
  attempt is continued, never re-dispatched …"`
- Requirements: `--confirm-live-run --resume-handoff --post-dispatch
  --worker-report-file "$REPORT"`; refuses `--consent-dispatch`,
  `--scripted-worker-change`, `--ceo-pane`, `--wake-deliver`, any pane
  designation, and any `--stop-after` at/before `dispatch`.
- Guarantees: transport locked so it physically cannot prompt; re‑proves the prior
  dispatch from artifacts + run‑local DB; refuses UNKNOWN/missing dispatch; binds
  the report by exact `task/attempt/task_version/brief_digest`; `blocked`/`failed`
  final reports are retained as audit evidence but do **not** advance the Task to
  `verifying` and do **not** proceed to delivery.
- Offline proof: dedicated `PostDispatchContinuationTests` (11 cases) — exactly one
  original submit, second invocation with zero Herdr activity, UNKNOWN/missing
  refusal, mismatched report refusal, blocked/failed retention — pass green.

The **operator path after a report arrives** (the replacement for the old
replay‑dispatch Step 8):

```
.venv/bin/python scripts/m606_opencode_e2e.py --confirm-live-run --root "$ROOT" \
    --resume-handoff --post-dispatch --worker-report-file "$REPORT" \
    --stop-after deliver
```

## 4. Concrete blockers

1. **Single idle OpenCode pane.** Only `pane A` is idle; `pane B` is working. A
   full both‑roles leg (worker dispatch **and** PG‑7 CEO bootstrap in the same
   phase‑2) needs **two distinct idle OpenCode panes**. Right now:
   - pane A is a candidate for a worker-only smoke after a fresh real-probe allocation and cwd/model checks;
   - worker **+** CEO (PG‑7) is blocked until a second OpenCode pane is idle.
2. **Pane cwd placement + attestations not yet done.** Neither candidate pane is
   currently inside the disposable world. Before phase‑2 the operator must:
   - `cd` the worker pane into the allocated worktree (§1),
   - `cd` the CEO pane into the project root (if running PG‑7),
   - capture each pane's native session id and write a **local** model attestation
     per pane (pane_id, terminal_id, session_id, agent, model), and note the
     loopback model the pane is actually running.
3. **Acknowledgement is unassertable here.** `prompt_ack`/`event_stream` are
   unsupported on herdr `0.9.3`, so receipts remain `assumed_default` and the
   human `accepted` vs transport `submitted` distinction cannot be upgraded by this
   host. Downstream claims must keep acknowledgement unasserted.
4. **Continuation cannot be exercised live yet.** A live `--post-dispatch` run in a fresh real-probe allocation requires a prior **successful** `dispatch.json` (`state:
   submitted`). Producing one needs a real prompt, which is out of scope for this
   readiness prep (no prompts allowed). Continuation is proven offline only until a
   real dispatch occurs.

## 5. Constraints honored

- No real prompts sent; **0** submits during provisioning.
- No model inference invoked (loopback `/v1/models` catalog read only).
- Live `teamcraft` DB untouched — provisioning used a **run‑local** SQLite under the
  disposable root.
- No session id exposed (redacted).
- No topology/lifecycle Herdr verbs; transport locked for the client used.
- Provisioned from clean committed `a2a76f3` snapshot; peer working‑tree edits avoided.
- The rehearsal itself did not commit or push. Its source revision was `a2a76f3`.

## 6. Retained disposable artifacts (for CEO; disposable)

- Run root: `<rehearsal-root>` (retain until consumed or
  reclaimed via `--cleanup-only --root "$ROOT"`).
- Committed snapshot used: `…/opencode/m606-smoke-src` (`git archive a2a76f3`).
- Read‑only readiness driver: `…/opencode/m606_readiness.py`.
