# M6-06 — Codex/OpenCode Local E2E Preflight (record + scenario design)

| Field | Value |
|---|---|
| Status | **Preflight executed (read-only) + scenario design. No agents spawned, no model invocation, no runtime-DB modification, no Claude install, no cloud, no commit/push.** |
| Roadmap item | M6-06 (execution-readiness slice between P1 probe groundwork @ `f0d8867` and the consented E2E run) |
| Drafted from | `main` @ `f0d8867`, 2026-11-22 |
| Owned surface | This file only. |

Scope honesty: this document proves the *environment* is ready for the
Codex/OpenCode legs and enumerates exactly what remains gated. It claims **no**
init/dispatch/Wake real-host results — PG-7 (real Herdr prompt runs) stays open
until the consented run described in §3 actually executes.

---

## 1. Read-only inspection results (executed 2026-11-22 on this host)

### 1.1 Local executables

| Tool | Version observed | Note |
|---|---|---|
| `codex` | **codex-cli 0.170.0** | §12.11 recorded live skill-load on 0.166.0 — version drift since; re-verification required at run time (R4 pins version *observed*, not assumed) |
| `opencode` | **v2.2.6** | §12.11 smoke ran on v2.0.29 and failed on `provider.no-route`; drift since — routing re-check mandatory (G2) |
| `herdr` | **0.9.3** | present; session-contract behavior under this version unverified on host (PG-7/G3) |
| `claude` | **absent** | unchanged from P1 slice; Claude legs remain pending (PG-1) |
| Python | 3.13.x (uv-managed) on host | project floor is ≥3.10; §12.11 accepted runs on 3.10.20 — record interpreter actually used in the run manifest |

### 1.2 Protocol integrity (executed via `teamcraft.ceo_protocol.verify_protocol`)

```
protocol_version : 1.0.0
sha256           : c5c358bf5e1654bc902579972acda4b29984ffde171a547559d289b20ef0a742
manifest match   : references/protocol.manifest.json pins the identical hash ✓
continuity       : byte-identical hash to the value Codex CLI 0.166.0 read live in §12.11
```

**Drift finding (good):** the canonical protocol artifact has not drifted across
the harness upgrades 0.166→0.170 and 2.0.29→2.2.6. The run must still re-verify
each discovered copy (PRE-3) because *copies* (skill dir, worktree clones) can
diverge even when the canonical file does not.

### 1.3 Current managed-runtime blockers (read-only census)

| # | Observation | E2E consequence |
|---|---|---|
| B1 | **Uncommitted managed files in the working tree**: `.agents/skills/teamcraft/scripts/teamctl.py` (+59/−2) and `.opencode/agents/herdr-tester.md` (+1) — third-party in-flight changes | An installer run against the live repo would hit managed-conflict discipline; the disposable scenario therefore installs only into a **fresh target dir** (PRE-4/S1), never here |
| B2 | **20 registered git worktrees** — 19 under `.teamcraft/worktrees/*` (incl. branch-tagged `tc/...` refs) plus an external OpenCode worktree (`~/.local/share/opencode/worktree/…/m306-race-repro`) | Not a hard blocker (scenario uses its own tmp root), but pre-runs the existing-cleanup audit (PRE-8) and forbids touching any of them (M4-06 conservatism) |
| B3 | **Live runtime DB present**: `.teamcraft/teamcraft.db` exists; `.teamcraft/` holds 117 entries incl. `api/` artifacts and ≥10 `archive-*` snapshots | Hard rule: the scenario's DB is created in the disposable root via `storage.migrate_to_head`; a path-guard aborts the run if its project root ever resolves inside this repo (§3 S0) |
| B4 | `opencode.json` (user config) exists in repo root | Installer never replaces it; scenario target gets a template copy only |
| B5 | OpenCode local model route: last recorded host state was `provider.no-route` (§12.11); current oMLX state **not probed in this slice** | Gate G2: run-start `providers.probe_omlx()` loopback check must be healthy before any OpenCode model-driven leg |

## 2. Preflight checklist — executable now vs gated

Executable **now**, without consent beyond routine local testing (✅ = already
executed during this preflight):

| ID | Step | Mode | Status |
|---|---|---|---|
| PRE-1 | Executable + version inventory (§1.1) | read-only | ✅ done 2026-11-22 |
| PRE-2 | `verify_protocol` canonical dir vs manifest (§1.2) | read-only | ✅ done |
| PRE-3 | Re-hash every discovered protocol copy (`repo`, each `.teamcraft/worktrees/*/…/CEO_PROTOCOL.md`) and diff against the canonical hash | read-only | design done; execute at run start |
| PRE-4 | `scripts/install.sh --dry-run --target <disposable>` — flags confirmed present (`--target` L34, `--dry-run` L39); zero writes expected, target must be outside this repo | disposable | design done; execute at run start |
| PRE-5 | Offline contract suites (`test_providers_m501`, `test_router_m502`, `test_ceo_protocol_m108`, `test_ceo_initialization_m208`, `test_wake_m209`) as green-baseline gate | offline tests | design done |
| PRE-6 | Create disposable project root under `TMPDIR` + fresh DB via `storage.migrate_to_head` | disposable | design done |
| PRE-7 | Git fixture: bare origin + clone in tmp, seeded `tc/...` branch namespace, protocol artifact present in target | disposable | design done |
| PRE-8 | Blocker census snapshot (`git status` hash of this repo + worktree list) to prove scenario independence | read-only | design done |

**Gated** (not executable in this slice):

| Gate | What it blocks | Release condition |
|---|---|---|
| G1 | Any step that **spawns a harness agent** (Codex/OpenCode session, herdr pane tab) — S2/S3 of §3 | Explicit operator consent to spawn agents for a bounded run; herdr 0.9.3 session-contract smoke passes first (G3) |
| G2 | OpenCode model-driven legs | `probe_omlx` loopback healthy + model id present in catalog at run start (PG-9 precedent §12.11) |
| G3 | herdr-dependent dispatch evidence quality | minimal read-only `herdr` listing + tab identity smoke on 0.9.3, recorded before consented run |
| G4 | Worktree/disk hygiene | PRE-8 snapshot shows scenario artifacts clearly namespaced (`m606-e2e-*`); no cleanup of pre-existing worktrees (B2) attempted |
| G5 | Claude legs | PG-1 (pinned binary) — unchanged; Claude stays pending in every matrix |
| G6 | Any cloud model call | M5-03 budget approval + NULL-safe usage rows — out of scope for a *local* E2E; local route only |

## 3. Disposable end-to-end scenario design (Codex + OpenCode legs)

Every artifact lands under `<TMPDIR>/m606-e2e-<runid>/`; **evidence** column is
the artifact the run must emit to count, **abort** is the fail-closed trigger.

| Step | Action | Evidence artifact | Pass criteria | Abort when |
|---|---|---|---|---|
| S0 | Provision run root in TMP; fresh `teamcraft.db` via `migrate_to_head`; record interpreter + all §1.1 versions in `run-manifest.json`; **assert root not inside `/Users/blade/GitHub/teamcraft`** | `run-manifest.json` incl. schema head version | fresh DB at head; manifest complete | path guard trips; versions differ from §1.1 without a recorded note |
| S1 | Installer real run into target (`--target <run>/project`); then `verify_protocol` in target; create project + CEO Attempt pinning `protocol_version/hash` in the *run-local* DB | install journal; `protocol-verify.json` (version+hash); DB rows: project, CEO attempt w/ pair `protocol_version`+`protocol_hash` | managed files byte-identical to canonical manifest; pinned hash == `c5c358bf…742` | any managed conflict with user-shaped file (must fail-before-write); hash mismatch ⇒ protocol-drift abort |
| S2 *(G1)* | Dispatch one **read-mostly** worker task (e.g. "summarize README structure, no edits") to Codex leg via herdr pane; capture dispatch claim, brief digest, herdr session identity hash | dispatch claim row + `attempt_brief` digest + session identity binding + pane/terminal ids | prompt submitted; identity binds post-submit per M2-08; brief immutable | submit lost ⇒ classify **UNKNOWN**, quarantine, halt this leg (never auto-retry); **no host-trusted receipt claimed** — `assumed_default` recorded as such |
| S3 *(G1)* | Enqueue one `ceo.wake`; single `wake-dispatch`; record state transition `pending→sending→submitted|unknown` | wake transition events with timestamps | one send, no duplicates, state terminal-verified | duplicate send or stuck `sending` past timeout ⇒ UNKNOWN + halt |
| S4 | Worker returns commit on task branch in the disposable origin; run verifier profiles (`lint`,`test`) locally against that commit in the run's worktree | verification rows + bounded log digests (artifact-root-relative paths only, per storage triggers) | verifier verdict computed locally from repo facts — *not* from worker self-report | log contains absolute-path/secret shapes (reuse M6-03 patterns as the scanner) ⇒ sanitize-and-rerun once, else fail |
| S5 | Local-only delivery: Diff Gate over the branch diff; mark delivered DONE **local-only** (no push anywhere — the "origin" is a local bare repo); evaluate CLEANABLE and remove the run worktree with an audit line | diff-gate record; delivery DONE row; cleanup audit entry | DONE reachable only with commit+diff evidence; cleanup only CLEANABLE | diff outside task-owned paths ⇒ Diff-Gate refusal stands as the pass condition of a negative probe; never force-clean |
| S6 | Close-out: run `diagnostics.build_preview` over the run DB, preview manifest, export to `evidence/`; write `M6_06_E2E_RUN.md` summary mirroring this table cell-by-cell | sanitized bundle + run log | every claim in the log maps to an artifact with digest | manifest scan finds leak candidates ⇒ bundle withheld, log marks run INCONCLUSIVE |

Then repeat S2–S4 for the **OpenCode leg** (requires G2 healthy route) under the
same S0 root; both legs must consume the *same* protocol hash (R4). A leg that
could not run is written `pending` with its gate id — never omitted, never
implied passed.

**Explicit capability non-claims for the whole scenario:** no prompt-ack
(`prompt_ack` stays `unknown`), no event-stream (`event_stream` stays
`unknown`), no host-trusted protocol receipt, no direct-PR (M4-05 out of local
scope), no Claude rows.

## 4. What is ready now — and what remains gated

**Ready now (safe, no consent needed):** PRE-1…PRE-8 (PRE-1/PRE-2 already
executed green), the S0/S1 scaffolding (tmp root, fresh DB, installer
`--dry-run`/`--target`), S4–S5 in a *scripted-commit* variant (a fixture
commit authored by the test harness instead of a live agent still exercises
verification, Diff Gate, local delivery and cleanup), the evidence tooling, and
the negative probes (Diff-Gate refusal, UNKNOWN classification).

**Gated for explicit consent/evidence:** G1 agent-spawn legs (real S2/S3 —
this is the PG-7 closure event, and until it executes nothing may mark Codex/
OpenCode init/dispatch/Wake REAL), G2 OpenCode model-route confirmation at run
start, G3 herdr 0.9.3 identity smoke, B1/B2 hygiene snapshot at run start, G5
Claude (PG-1), G6 cloud.

**Net position:** the local E2E is environment-ready and evidence-designed; its
two REAL-host rows (init handshake, dispatch/Wake over real herdr) await exactly
one release action — operator consent (G1) plus a green G2/G3 start check — and
everything else the roadmap asks of M6-06 for Codex/OpenCode can then execute
locally, offline-from-cloud, and verifiably.

---
*Evidence pins (all executed read-only or cited): §1.1 versions from
`--version` invocations 2026-11-22; §1.2 live `verify_protocol` output matching
`protocol.manifest.json` and the §12.11-recorded hash; §1.3 from `git diff
--stat`, `git worktree list` (20 entries), `.teamcraft/` census (117 entries,
`teamcraft.db` present), `install.sh` flag lines 34/39, M0 review §12.11
(`provider.no-route`, receipt deferral 12.17), M2-09 status doc, readiness doc
gaps PG-1/PG-7/PG-8/PG-9 at `f0d8867`.*
