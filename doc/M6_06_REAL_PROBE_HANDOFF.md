# M6-06 — Real‑Probe Phase‑1 Handoff (sanitized)

Status: **phase‑1 provisioned by a genuine real probe; not dispatched.** This
supersedes the earlier mock‑seeded candidate (`m606-smoke-candidate-210949`,
**retired — do not reuse**). No prompt was sent, no model inference was invoked,
the live `teamcraft` DB was not touched, no session id appears here, and nothing
is committed or pushed.

Unlike the mock candidate, this allocation bound **real** transport — the real
loopback oMLX client and the real local harness probe — with **no injected fake
seams**. Capability binding succeeded genuinely (see §2); nothing was substituted.

> The exact local retained path is reported separately to the CEO (not embedded
> here). Sanitized locator: disposable run `m606-real-probe-handoff-212219`
> under the approved temp working area.

## 0. Provenance

| Fact | Value |
| --- | --- |
| Commit provisioned from | `a2a76f3` (`Continue M6-06 delivery without replaying dispatch`) — latest; no later runner commit |
| Commit identity | `a2a76f38b63beda3b115b00709fa6074a5202441` |
| Source | pristine `git archive a2a76f3` snapshot (NOT the working tree) |
| Runner vs commit | `scripts/m606_opencode_e2e.py` byte‑identical to `a2a76f3` |
| Transport | **real** — default `Herdr`, default loopback oMLX getter, default harness runner; **no fakes injected** |
| Protocol pin | `1.0.0` / `c5c358bf5e1654bc902579972acda4b29984ffde171a547559d289b20ef0a742` (`pinned_match: true`) |
| Pinned model | `Qwen3.8-Flash-Next-oQ4e-mtp` |

Method: `--confirm-live-run --root <fresh> --stop-after allocate --keep-root`.
Read‑only real checks only: `herdr status`, `herdr api snapshot` (topology
digest), oMLX **catalog** `GET /v1/models`, and `opencode --version`. No
`/chat/completions`, no agent prompt, no topology/lifecycle verb, run‑local SQLite
only.

## 1. Phase‑1 durable facts (run‑local, disposable; no session id)

| Field | Value |
| --- | --- |
| `run_id` | `c93283fd68dd19de` |
| `task_id` | `6c2fc051-244e-4a07-94b2-cb6a48adf9ef` |
| `attempt_id` | `72fcbce4-8b38-4aa5-88a4-9f79b39de77c` |
| `allocation_id` | `wta-b156c80a2ac5a85a61c95dd4aa471e3a` |
| `branch` | `tc/6c2fc051-…/72fcbce4-…` |
| `base_sha` | `6c1f86d18d01f79cf02303ca7ba8f311a866e124` |
| `brief_digest` | `55d1252a94a2d5a0cd756d6e18a0d910f6a7f3a20f98ba04616f327dd1ce21b9` |
| allocated cwd (worker pane target) | `…/m606-real-probe-handoff-212219/project/.teamcraft/worktrees/72fcbce4-…77c` |
| project root (CEO pane target) | `…/m606-real-probe-handoff-212219/project` |
| `submit_allowed_in_this_invocation` | `false` (phase‑1 never submits) |

Phase‑1 produced real disposable Git + run‑local SQLite + `handoff.json` +
`capability-binding.json`; exit code `0`; `provisioned: true`.

## 2. Real preflight + real capability binding (sanitized)

### 2a. Loopback model route — REAL (`GET /v1/models`, catalog only)
| Check | Observed | Verdict |
| --- | --- | --- |
| Health | `healthy` | ✅ |
| Source | `http://127.0.0.1:8000/v1/models` (loopback) | ✅ |
| Pinned model served | `Qwen3.8-Flash-Next-oQ4e-mtp` present (catalog size 2) | ✅ |
| Cloud fallback | `not-permitted` (no substitution) | ✅ |

Provider snapshot bound durably before bind: `provider_digest`
`306f85c1260e4a45134bc0b1c1e72e8acaec3625fafabe35127835a0ab0306af`.

### 2b. Local harness — REAL (`opencode --version`)
- Harness bound: `opencode`, `durable_before_bind: true`, `cloud_fallback:
  not-permitted`.
- Harness snapshot digest `77ac4d12ecfdb9db199217843f23b9da5d7e19c90f0225cec159dd2039f9be56`.

### 2c. Transport / protocol — REAL (`herdr status`)
| Check | Observed | Verdict |
| --- | --- | --- |
| server.status | `running` | ✅ |
| client == server version | `0.9.3` == `0.9.3` | ✅ |
| endpoint / private‑protocol compatible | `yes` / `yes` | ✅ |
| update.restart_needed | `no` | ✅ |
| `prompt_ack` / `event_stream` | `unsupported` on 0.9.3 → receipts `assumed_default`; acknowledgement stays **unasserted** | ⚠️ nuance (not a blocker) |

Topology digest: `f62c4ca8c694c487681fe11b70ef96458a098d99b61b719d5959364ac2d3dd0d`.

**Preflight and actual capability binding both PASSED with real checks. No fake
was used, so no substitution occurred.**

## 3. Phase‑2 prerequisites (carryover, sanitized)

Panes observed via `herdr api snapshot` (identifiers redacted):

| Pane | Agent | Status | Native session present |
| --- | --- | --- | --- |
| `pane A` | opencode | idle | ✅ `redacted` |
| `pane B` | opencode | working | ✅ `redacted` |

Before a phase‑2 dispatch on this allocation the operator must:
1. `cd` the **worker** pane into the allocated cwd (§1); `cd` the **CEO** pane
   into the project root (only if running PG‑7);
2. record each pane's native session id and write a **local** model attestation
   per pane (pane_id, terminal_id, session_id, agent, model actually running);
3. note the distinct‑pane requirement: worker and CEO must be distinct idle
   OpenCode panes for this scenario.

**Blocker carried forward:** only **one** idle OpenCode pane (`pane A`) exists;
`pane B` is working. Worker-only dispatch still requires the cwd and model checks above; worker **+** CEO (PG‑7)
requires a second idle OpenCode pane.

## 4. Non‑submitting continuation (no re‑dispatch)

`--post-dispatch` continues this attempt from an external **final** WorkerReport
with **zero** prompts; it re‑proves prior dispatch, refuses UNKNOWN/missing, binds
the report exactly, and (per the completion guard) retains `blocked`/`failed`
reports as audit evidence without advancing the Task or delivering. Offline‑proven
by `PostDispatchContinuationTests`. Operator path once a report arrives:

```
.venv/bin/python scripts/m606_opencode_e2e.py --confirm-live-run --root <root> \
    --resume-handoff --post-dispatch --worker-report-file <final-report.json> \
    --stop-after deliver
```

Live continuation is still gated on a prior successful `dispatch.json`
(`state: submitted`), which needs a real prompt — out of scope for this prep.

## 5. Constraints honored

- Real loopback‑only oMLX + real local harness; **no fakes substituted**.
- No model inference (`/v1/models` catalog + `opencode --version` only), no prompt.
- Live `teamcraft` DB untouched (run‑local SQLite under the disposable root).
- No session id exposed (redacted to `sha256:` refs).
- No topology/lifecycle verbs; dispatch disallowed in this invocation.
- Provisioned from clean committed `a2a76f3` snapshot; old mock candidate retired.
- No commit, no push. HEAD remains `a2a76f3`.
