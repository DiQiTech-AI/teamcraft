# M6-05 — Remote / Multi-Machine RFC (DRAFT)

| Field | Value |
|---|---|
| Status | **DRAFT — proposal only. Not approved, not implemented.** |
| Roadmap item | M0 review §9.6 M6-05 远程/多机独立 RFC (依赖 M4-06、M5-06 退出验收与实际运行数据) |
| Drafted from | `main` @ `fca82c8` (2026-11-22) |
| Deliverable type | RFC per roadmap: "基于实际瓶颈评估 SSH/VPN、授权、TLS、Worker 身份、文件/DB 一致性；不直接实现" — threat model, protocol options, failure scenarios, migration, and an explicit "no change" option. |
| Hard non-goals of this document | No implementation, no network actions, no public listener (not even behind a flag), no changes to any existing module, no dependency additions. This document changes nothing except adding itself. |

> **M6 exit-gate rule this RFC obeys:** "任何远程或多机能力先有单独 RFC；未批准不开放公网监听"
> — any remote/multi-machine capability requires this RFC first, and until approved
> nothing may open a non-loopback listener.

---

## 1. Why this RFC exists now — and why it does NOT unblock itself

TeamCraft 2.0 is currently, by design, single-machine:

* `teamcraft/leader.py` (M1-07): "Deliberate scope limits (CEO M1-07 gate D):
  single machine, local SQLite only, NO background service, **no clock synchronized
  across hosts**."
* `teamcraft/storage.py` (M1-03): refuses network/sync mounts
  (`UnsupportedLocationError`), requires absolute non-symlink paths, and states that
  "WAL success is capability evidence, not proof of locality". The authoritative
  database is intentionally unable to live on shared storage.
* `teamcraft/api/config.py` (M3-01): binds `127.0.0.1` by default; a non-loopback
  bind needs an explicit `allow_non_loopback` opt-in because "localhost is not a
  trust boundary, external exposure needs deliberate opt-in" (PLAN §9.2-1).
* `teamcraft/herdr_adapter.py`: Herdr is a **local CLI subprocess contract**; worker
  sessions are local terminal-tab processes.
* `teamcraft/providers.py` (M5-01): oMLX is probed only over the loopback model
  directory; no provider endpoint beyond loopback is registered.
* `teamcraft/router.py` (M5-02): locality is the closed vocabulary `{local, cloud}`
  — there is **no concept of "remote worker host"** in any existing decision.

The roadmap conditions M6-05 on "实际运行数据" (real running data) and M4-06/M5-06
exit acceptance. **Section 3 records that this evidence is currently missing.** The
honest default conclusion (Section 9) is therefore: **choose "no change" for now**,
and use this RFC as the frozen design space so a later decision is a review, not
reresearch.

## 2. What problem remote execution is supposed to solve

Candidate motivations, ranked by how likely the repository evidence says they are:

1. **CPU/GPU capacity for local inference** — a second machine hosting oMLX or a
   lab machine with more VRAM. Plausible, but unmeasured: no utilization or queueing
   data exists in the repo (GAP-1).
2. **Parallel isolation** — run multiple risky write-tasks on separate physical
   hosts. Already largely served locally by M4-02 per-task git worktrees; marginal
   benefit unknown.
3. **Availability / seat-sharing** — CEO on one machine, workers on another.
   Convenience only; adds the largest identity surface (Section 5).
4. **Cost arbitrage (cloud/GPU spot)** — already served *without* multi-machine
   TeamCraft through the M5-03 approved cloud-escalation path (cloud *model* calls,
   not cloud *worker hosts*). Do not conflate "run a model remotely" (exists,
   budgeted, approved) with "run a worker host remotely" (this RFC).

**Decision-relevant fact:** none of these motivations is currently backed by a
measured bottleneck. The roadmap requires "基于实际瓶颈" — we have no bottleneck log.

## 3. Evidence base and explicit gaps

### 3.1 Evidence that EXISTS in this repository (single-machine proven floor)

| Evidence | Source | What it proves |
|---|---|---|
| Single-writer SQLite authority with WAL + checkpoints + verified backup/restore | `teamcraft/storage.py`, `tests/test_storage_m103.py` | A durable local authoritative store with corruption fail-closure |
| Lease/epoch fencing, stale-leader commit rejection | `teamcraft/leader.py` | One authoritative host may serialize and fence writes — **explicitly single-machine** |
| Loopback-default API with explicit opt-in refusal for external binds | `teamcraft/api/config.py`, `serve.py` | The project already has a deliberate "exposure is a decision" gate |
| Worker = local Herdr tab; transport errors normalized; unknown results quarantined | `teamcraft/herdr_adapter.py`, `recovery.py` (M2-05), `wake.py` (M2-09) | Conservative UNKNOWN semantics exist for local session loss |
| Per-task worktrees, Diff Gate, evidenced delivery, CLEANABLE cleanup | `worktrees.py` (M4-02), `verification.py` (M4-03/04), `delivery.py` (M4-04/05), `recovery.py` (M4-06) | Artifact-level evidence chain **with files on the same host as the DB** |
| Capability snapshots probed locally, health staleness fail-closed | `providers.py` (M5-01), `router.py` (M5-02) | Identity-adjacent facts (harness/provider/model versions) are already recorded with source + probe time |
| Sanitized export with manifest digests, path/secret scrubbing | `diagnostics.py` (M6-03) | A proven pattern for **crossing a boundary with artifacts safely** |
| Immutable briefs bound by digest | `briefs.py` (M2-03) | Task intent can be shipped to another machine as verified inert text |

### 3.2 Evidence that is UNAVAILABLE at draft time (marked gaps)

| Gap | Status quote / source | Consequence for this RFC |
|---|---|---|
| **GAP-1 — real multi-machine bottleneck data** | No utilization, queue-depth or wall-clock dataset exists in the repo | Option selection (§4) cannot be justified by "actual bottleneck"; only by architecture reasoning |
| **GAP-2 — M5-06 real cloud-vs-mixed comparison** | M0 review 12.29: "当前条件不足，纯云端与混合模式的真实对照实验延期，未记录实际收益" — tool accepted (`470d5c3`, 90 tests), real experiment deferred; `experiment.py` labels synthetic runs `synthetic_rehearsal` which "can NEVER make a claim" | No quantified benefit statement about remote capacity may be made here |
| GAP-3 — M5-01 independent acceptance | 12.25: "待独立 tester 验收" | Provider/harness fact layer is not independently confirmed; remote identity schemes sit on top of it |
| GAP-4 — M5-03 deferred validations | 12.27: "两项验证延期" | Budget/escalation semantics unverified in real cloud spend |
| GAP-5 — Claude / M6-06 cross-harness E2E | §9.6 M6-06 still open; local Claude not installed | Worker-harness matrix is incomplete before even considering remote hosts |
| GAP-6 — no operational hours with M4-06 cleanup + real parallel load | M4-06 exit gate text exists; long-run data absent | Failure-mode probabilities for remote are guesses, flagged as such |
| GAP-7 — no threat-model review by a second party | none exists | This RFC's threat model (§6) is first-pass only and requires review |

**Rule adopted:** every decision in §10 names the gaps it is blocked on. No gap may
be closed by assumption; each needs the listed artifact.

## 4. Architecture-level option comparison

Common invariant across ALL options (non-negotiable, drawn from §3.1):

> **One host — the CEO host — owns `teamcraft.db`, the Leader lease, approvals,
> the router decisions, and the verification/acceptance verdicts. Remote hosts
> execute and produce artifacts; they never commit authoritative facts.**

### Option A — Single machine, no change (baseline / "不做" option)

Everything stays as at `fca82c8`.

* **Authority:** trivially single-writer; lease/fencing semantics unchanged.
* **Identity surface:** unchanged — none added.
* **Failure modes:** already covered by M2/M4 machinery.
* **Cost:** capped by one host's capacity; cloud *models* remain available via
  M5-03 budgeted escalation without touching this design.
* **Reversibility:** N/A (nothing to undo).

### Option B — SSH-launched remote workers (brokering from the CEO host)

CEO host holds an SSH key per enrolled worker host and launches a thin
"remote worker agent" (in this design, a scripted sequence invoking the same
local worker CLI/Harness commands) over an outbound SSH connection on the
private network. Brief delivery is push (scp/stdin of the digest-bound
`brief.content`); artifacts return as git refs (feature branches only, never
protected bases) plus bounded log/patch bundles validated with
manifest-digest verification patterned on M6-03.

* **Transport:** SSH (port 22, or port-forwarded over a VPN), TLS-equivalent KEX;
  no listening TeamCraft service on the remote side beyond `sshd` itself.
* **Authority:** unchanged — remote agents return *proposals* (branch + report);
  Diff Gate, verifier runs and DONE transitions stay on the CEO host.
* **Identity:** SSH key pair per host + a registered fingerprint in a future
  `worker_hosts` table (append-only); key revocation = delete/fence registration.
* **Consistency:** git is the only shared mutable medium; it is offline-tolerant
  and converges by fetch; no shared filesystem, no DB over network (banned by
  `storage.py` policy).
* **Failure modes:** SSH drop mid-task → the attempt stays UNKNOWN and follows the
  M2-05 conservative path (never auto re-dispatch); stale-agent writes surface as
  unexpected branch content and die at the local Diff Gate.
* **Added surfaces:** SSH private keys on the CEO host (must live outside the DB —
  consistent with M5-01 "不读取/存储明文密钥"); `sshd` on worker hosts.
* **Complexity:** low-to-medium; operationally the most boring option.

### Option C — Private-network service plane (VPN mesh + remote worker daemons)

Worker hosts run long-lived daemons that dial out over a private overlay
(e.g. Tailscale-class VPN or site-to-site WireGuard; **never plain public
Internet**) to a CEO-host listener, receive work items, stream progress, and push
artifacts over mTLS or short-lived bearer tokens.

* **Transport:** overlay network + authenticated HTTPS/WebSocket; requires
  enabling the currently refused non-loopback bind (`allow_non_loopback`) with a
  real TLS story, token issuance, rotation and revocation.
* **Authority:** same invariant as B, but enforcement shifts from "the remote has
  no DB and no SSH" to "the API authenticates and authorizes every call" — a
  strictly larger correctness-and-security burden (every endpoint must stay
  authz-correct forever).
* **Identity:** machine enrolment ceremony, per-daemon identity, short-lived
  tokens; mapping daemon identity ↔ recorded Attempt/harness snapshot
  (M5-01-style facts with source + probe time).
* **Consistency:** same artifact-only rule; streaming adds ordering/replay
  concerns; must reuse M2-04/M2-09 seq/dedupe discipline on any event stream.
* **Failure modes:** partition storms, token replay, clock skew (cross-host epoch
  is explicitly *not* available — see `leader.py`), daemon-version drift between
  two harness installs.
* **Complexity:** high; largest credential surface; strongest anti-requirements in
  the roadmap ("多机一致性和凭证面显著扩大").

### Comparison summary

| Dimension | A: no change | B: SSH broker | C: service plane |
|---|---|---|---|
| Authoritative DB location | local | local (invariant) | local (invariant) |
| New credential surface | none | SSH keys on CEO host | TLS + token PKI + enrolment |
| New listeners | none | `sshd` on workers (pre-existing) | TeamCraft listener off-loopback |
| Cross-host clock dependency | none | none required | token/lease lifetimes need drift handling |
| Consistency mechanism | n/a | git + digest-verified bundles | git + streaming dedupe/replay |
| Failure conservatism | existing M2/M4 | maps onto M2-05 UNKNOWN | new partition semantics to prove |
| Rollback simplicity | n/a | revoke key, stop launching | revoke plane-wide, drain sessions |
| Ops burden | none | low–medium | high |
| Evidence to choose (gaps) | — | GAP-1, GAP-7 | GAP-1, GAP-2, GAP-7 + TLS/token review |

**Architecture preference if ever approved: B before C.** C is only considered if
B demonstrably pays its way and operational data (GAP-1) shows a real need for
push/streaming semantics.

## 5. Worker identity and trust boundaries

* **Trust root stays singular.** The CEO host's SQLite + lease is the only place
  a fact becomes true. A remote host is an **untrusted execution cell**: it may
  read only the specific immutable brief content it was dispatched (digest-bound
  via M2-03), the repo checkout pinned to the task's worktree commit, and its own
  toolchain.
* **Enrolment (future, not built here):** one row per worker host recording
  {host id, public key fingerprint or cert identity, harness/provider capability
  snapshot from M5-01-style probes, registered-by actor, status ∈
  {enrolled, fenced, retired}}. Append-only; fencing never deletes history.
* **Attempt binding:** each remote Attempt stores the worker-host identity
  alongside the existing session/harness facts, so every remote action remains
  traceable — the same "可追溯到来源" principle as M5-04 usage rows.
* **What remote identity NEVER grants:**
  * write access to `teamcraft.db` or any authoritative table;
  * approval authority — Approvals stay human-local (M1-05/M4-05 pattern:
    merge Commands always bound to a human Approval executed by the fenced Leader);
  * router policy — remote hosts consume routing decisions, never set them
    (`local`/`cloud` locality stays a property of the *model call*, while remote
    workers would be a separate, orthogonal host concept — see D3);
  * ownership takeover — Leader lease semantics do not extend across hosts; a
    remote agent cannot "become leader" under any network condition.
* **Secrets:** private keys/tokens live in the OS credential store or a
  key file outside the runtime directory, never in the DB, never in diagnostics
  bundles (M6-03 scrubbing must be re-verified to cover new token shapes —
  test obligation, GAP-adjacent).
* **Revocation:** fencing a host must stop all future dispatch instantly and
  mark its in-flight attempts UNKNOWN→quarantine (no credit for self-reported
  completion until reconciled against repo/PR facts, exactly the M2-06/M4-04
  discipline).

## 6. Threat model (first-pass — requires second-party review, GAP-7)

| # | Threat | Surface | Mitigation (design-level) | Status |
|---|---|---|---|---|
| T1 | Compromised worker host fabricates "done" | report channel | Done is never self-reported: verifier commands re-run on CEO host against returned commit; Diff Gate unchanged | pattern exists (M2-06/M4-03) |
| T2 | Stale worker acts after fencing | dispatch channel | Local epoch fencing can't reach remote; mitigation = fencing + immediate drain + UNKNOWN quarantine; success commits from fenced hosts rejected locally by identity check at ingest | needs new ingest-time host-id check (future work) |
| T3 | SSH key / token theft | credential store | OS keystore, short-lived tokens (C only), revoke-and-fence runbook; DB never stores plaintext (M5-01 principle) | review needed |
| T4 | Artifact exfiltration (source/logs) | return channel | Bundles pass M6-03-style sanitization + manifest digests; size bounds; retention policy for returned logs | pattern exists |
| T5 | MITM / endpoint impersonation | transport | SSH KEX (B) or TLS + pinned identities over private overlay (C); never plain TCP, never public listener (roadmap gate) | design only |
| T6 | Replay of old briefs/results | dispatch/return | Briefs are immutable + digest-bound; results carry Attempt id + expected task version; CAS on task version at ingest | pattern exists |
| T7 | Toolchain drift on remote host changes verification meaning | environment | Capability snapshots with probe timestamps at dispatch (M5-01); verification executes inside the same enrolled image or re-runs locally | needs evidence, D4 |
| T8 | Scope creep (mesh ambitions) | governance | This RFC splits B/C; C additionally gated on B's operational data | governance |
| T9 | DNS/routing spoofing (C) | overlay | Overlay-native identity (cert/key), not DNS trust | design only |

## 7. Data & DB authority rules (all options)

1. `teamcraft.db` stays on the CEO host's verified local disk (`storage.py`
   boundary unchanged; **banned: SQLite over NFS/SMB/synced folders, WAL over
   network, shared-write DB, DB replication to worker hosts**).
2. Git is the only shared mutable state, and only via explicit refs: task
   feature branches per M4-02 worktree naming; remote hosts may fetch the pinned
   base commit and push *only* the task's own branch; protected bases stay out
   of reach (M4-05 "不 push 受保护主分支" rule extends verbatim).
3. What may cross the boundary: digest-bound brief content, repo objects,
   bounded/sanitized logs and artifacts, capability snapshots, usage numbers
   (NULL-safe per M5-04). What never crosses: the DB, approvals, lease state,
   router policy, secrets.
4. Ingest of returned artifacts is an *event producer* on the CEO host only:
   remote bytes become events/verifications after local validation, with the same
   idempotency keys so network retry cannot double-record (M1-05/M2-04
   discipline).

## 8. Failure, recovery, migration, rollback

**Failure scenarios and conservative mapping (no new trust invented for any):**

| Scenario | Behaviour |
|---|---|
| SSH/overlay drop mid-dispatch | dispatch result UNKNOWN → reconcile against repo refs; no auto re-dispatch (M2-05/M2-09 semantics) |
| Worker host dies mid-task | attempt UNKNOWN → escalated review; worktree/branch retained (M4-06: dirty evidence never auto-cleaned) |
| Branch pushed but bundle lost | task stays non-DONE; re-fetch allowed (git idempotent), digest mismatch → refusal + NEEDS_HUMAN |
| Host fenced mid-flight | self-reported completion quarantined; verifier verdicts only ever computed from commits already local |
| Clock skew | irrelevant in B by construction; in C tokens must use monotonic/relative lifetimes or overlay-native clocks — open item D5 |

**Migration (only if B approved later, staged):**
M0 enrol 1 non-privileged read-mostly host (docs/test tasks only, no auth/keys/
deploy tags — reusing M5-02 high-risk tag vocabulary as a *disallow* list);
M1 canary task set with all artifacts returned and locally verified; M2 measure
against GAP-1 metrics (success rate, wall-clock, human interventions, recovery
events, added ops cost) for a fixed period; M3 only then consider widening scope.
Each stage has an explicit stop rule: any fabricated-completion incident, any
credential loss, or any DB-integrity anomaly ⇒ halt and revoke, back to Option A.

**Rollback:** Option B→A = revoke keys, fence host rows, stop dispatch; nothing in
the DB depends on remote capability, so there is no schema downgrade; history of
remote attempts remains as append-only evidence. Option C additionally drains
tokens and disables the off-loopback bind — this is why C is gated strictly behind
B's demonstrated need.

## 9. Conditions for choosing "no change" (recommended today)

Select Option A (no change) while ANY of the following holds — which, as of
`fca82c8`, is ALL of them:

1. GAP-1: no measured single-machine bottleneck (utilization/queue data absent).
2. GAP-2: M5-06 real comparison deferred; "未记录实际收益" — no proven benefit.
3. GAP-3/4/5: upstream independent acceptances (M5-01, M5-03 validations, M6-06
   cross-harness) still open — remote identity and routing sit on those layers.
4. GAP-7: this threat model has had no second-party security review.
5. No approved credential story (SSH key custody or token PKI) exists or is
   reviewed.
6. The existing conservative machinery (worktrees, cloud-model escalation,
   budgeting) already covers the two real near-term needs: parallel write
   isolation and model capacity.

"No change" is itself a roadmap-sanctioned outcome ("RFC … 和'不做'选项"), and it
is the honest recommendation until evidence, not enthusiasm, moves the numbers.

## 10. Decisions requiring later evidence (each names its gate)

| ID | Decision | Required evidence before it can be taken |
|---|---|---|
| D1 | Approve remote at all (A vs B) | GAP-1 measured bottleneck + GAP-2 class experiment on remote capacity + second-party review (GAP-7) |
| D2 | SSH key custody & rotation model | credential-store design review; revocation drill recorded (GAP-7) |
| D3 | Add `remote` host-locality to the router model, or keep locality = model-call-only and host-routing separate | router change requires M5-02 independent acceptance (GAP-3) first; must prove routing enforcement can't be weakened by host preferences |
| D4 | May verification ever execute ON the remote host and still count for M4 gates | reproducible-environment evidence: pinned image + capability snapshot + re-verification sample comparison (new evidence class; default NO) |
| D5 | Clock-sensitive leases/tokens across hosts | measured cross-host drift data; default remains "no cross-host epoch" per `leader.py` |
| D6 | Progress streaming (C-lite) worth the auth burden | B ran ≥ the canary period with ops metrics favorable; GAP-1 shows streaming necessity |
| D7 | Approve Option C at all | D1–D6 green + TLS/token PKI review + firewall/overlay configuration audit; public listener remains forbidden regardless (roadmap gate) |

## 11. Explicit "do not do" list (permanent for M6)

* No public/internet listener, no `allow_non_loopback` enablement in this cycle.
* No SQLite (or WAL) on networked storage, no multi-writer DB, no DB replication.
* No remote approval casting; no remote lease/leadership.
* No shared home directories or synced project folders as an artifact channel.
* No k8s/container orchestration substitution — out of scope by roadmap.
* No implementation code, dependencies, schema migrations, or config keys as part
  of this RFC.

## 12. Verdict

**Recommendation: Option A — no change — until the gap gates in §9/§10 are met.**
This RFC provides the required threat model, protocol options, failure scenarios,
migration staging, rollback paths, and the explicit "no change" option. If a future
objective proves a measured bottleneck (GAP-1), the first candidate is the boring
one: SSH-brokered, artifact-only, authority-local Option B — with everything else
this document refuses still refused.

---
*Drafted 2026-11-22 from `main` @ `fca82c8`. Evidence pins: M0 review §9.6 (M6-05
row), §9.6 exit gate, §12.25/12.27/12.29 status notes, and the module docstrings of
`leader.py`, `storage.py`, `api/config.py`, `herdr_adapter.py`, `providers.py`,
`router.py`, `briefs.py`, `diagnostics.py`, `experiment.py`. All statements about
unavailable evidence are marked GAP-*; nothing in this file claims unmeasured
benefit.*
