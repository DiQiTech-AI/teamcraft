# M6-06 — Claude Adapter & Cross-Harness E2E Readiness (DRAFT, docs-only slice)

| Field | Value |
|---|---|
| Status | **READINESS PLAN + RUN MATRIX. No adapter code written in this slice.** |
| Roadmap item | M0 review §9.6 M6-06 Claude 适配与跨 Harness 真实 E2E (依赖 M1-08、M2-09、M3-06、M4-04、M5-03; direct-PR 另依赖 M4-05) |
| Drafted from | `main` @ `77a7385` (2026-11-22) |
| Host fact | `claude` executable is **not on PATH** on this host (verified 2026-11-22 via `command -v claude`; `codex`, `opencode`, `herdr` ARE present). A user-level `~/.claude/skills/` directory exists but predates/does not imply a working install — it is treated as untrusted environment residue until a pinned binary is available. |
| Hard constraints | No software installation by TeamCraft. No live Claude verification may be claimed. No cloud calls, no network actions, no commit/push in this slice. Only this file is added. |

Acceptance spine (roadmap, quoted): "用同一 protocol version/hash 分别跑
Codex、OpenCode、Claude 的初始化→派工→Wake→验证→交付场景；记录版本/能力矩阵
和降级… Claude 不可用时保持 pending，不能宣称通过；协议无复制漂移".

---

## 1. Inventory — what exists today (evidence-pinned)

### 1.1 Installer (`scripts/install.sh`)

| Fact | Evidence |
|---|---|
| Managed roots today: `.agents/skills/teamcraft`, `.agents/skills/teamcraft-ceo`, `.opencode/agents` profiles; `opencode.json` template never replaces user config | `install.sh` lines 52–53, 92, 226, 246–250 |
| Conflict discipline: pre-existing conflicting managed file ⇒ fail **before** any write unless byte-identical or managed-marker + `--force` | install contract; echoed in `CEO_PROTOCOL.md` line 74 |
| **No `.claude/` handling of any kind exists** | grep of installer: zero `.claude` references |
| Deliberate non-touch: "The installer does not create or edit `AGENTS.md`, `CLAUDE.md`, or other user rules" | `.agents/skills/teamcraft-ceo/references/CEO_PROTOCOL.md` L74; `SKILL.md` L15 |

### 1.2 CEO protocol & initialization

| Fact | Evidence |
|---|---|
| Single canonical artifact `.agents/skills/teamcraft-ceo/references/CEO_PROTOCOL.md`, header-pinned `protocol_version` + SHA-256 verified via detached `protocol.manifest.json`; verifier rejects symlink, non-UTF-8, missing, malformed and version/hash drift | `teamcraft/ceo_protocol.py` (`HEADER_RE`, `verify_protocol`); §12.11 |
| Worktree copies carry the same artifact (per-task clone), integrity checkable at dispatch root | `.teamcraft/worktrees/*/…/CEO_PROTOCOL.md` instances |
| M2-08 initialization state machine: `discovered/read_unverified/initialized/failed/incompatible/unknown`; nonce handshake; conservative `assumed_default` path requires bootstrap transport + post-submit Session binding + second identity check | `teamcraft/ceo_initialization.py` docstring, `CEO_STATES` |
| **Hard allowlist: harness must be `codex` or `opencode`** — `claude` is rejected today at row-level validation | `ceo_initialization.py` line 190 |
| OpenCode thin CEO entry exists (no duplicated protocol); shared skill used by Codex and OpenCode | `install.sh` profile targets; `SKILL.md` L15 |
| Host-trusted Protocol/Skill receipt does not exist for any harness — `read_unverified` audit only | `ceo_initialization.py` docstring ("no host-trusted Protocol/Skill receipt") |

### 1.3 Harness capability probing (M5-01)

| Fact | Evidence |
|---|---|
| `probe_harness("claude")` returns a **static** snapshot: health `unsupported`, reason `adapter_pending`, `skill_rules/project_rules: unsupported`, `event_stream/prompt_ack: unknown` — no probe executed | `providers.py` lines 226–231 |
| `codex`/`opencode`: real bounded `--version` probe (5 s timeout, 4 KiB cap), skill file presence `.agents/skills/teamcraft-ceo/SKILL.md`, rules path `AGENTS.md` (codex) / `.opencode/agents` (opencode); missing binary ⇒ `executable_missing` (exit 127) | `providers.py` lines 232–247 |
| Real probes recorded healthy 2026-11-20 for Codex/OpenCode (+ oMLX loopback) | M0 review §12.25 |

### 1.4 Dispatch / Wake / Verification / Delivery (harness-agnostic cores)

| Fact | Evidence |
|---|---|
| Worker prompt dispatch binds pane/terminal/harness/herdr session-identity hash; UNKNOWN-stays-UNKNOWN on lost response | `runtime_adapter.py` (bindings keys line 627 etc.), `herdr_adapter.py` local subprocess contract |
| Wake pipeline `pending→sending→submitted|unknown`; **explicit note: no real Herdr prompt sent for the recorded implementation evidence** | `doc/M2-09_WAKE_DISPATCH_STATUS.md` lines 61–69 ("No real Herdr prompt was sent… offline/mock evidence") |
| FR-025 CEO init/Wake marked **实机未验证** (host-unverified) | M0 review FR-025 row (line 158) |
| Verification & delivery are harness-agnostic (verifier commands + commit/diff evidence, M4-02/03/04/05 accepted; direct-PR per 12.23) | `verification.py`, `delivery.py`, M0 review §12.23 |
| Test suites that pin the above contracts offline: `test_ceo_protocol_m108.py`, `test_ceo_initialization_m208.py`, `test_runtime_adapter_m107/m402/m403/m404/m405.py`, `test_wake_m209.py` | tests directory |

## 2. Requirement digest — M6-06 broken into checkable obligations

R1 pin a **target Claude Code version** and validate `.claude/skills/` + rules
discovery against that exact version.
R2 decide **managed copy vs verified symlink** for the skill placement.
R3 generate a **thin `CLAUDE.md`** without ever overwriting/editing user files.
R4 run initialization→dispatch→Wake→verification→delivery on Codex, OpenCode,
Claude with **the same protocol version/hash**; record version/capability matrix
and degradations.
R5 when Claude is unavailable ⇒ rows stay **pending**; never claim pass.
R6 prove **no copy drift** of the protocol across harnesses.
R7 report/local-only E2E bound to evidence; direct-PR scenario per M4-05.

## 3. Gap register (blocking items, each with the artifact that would close it)

| Gap | Status now | Closing artifact |
|---|---|---|
| PG-1 Claude binary absent on PATH | confirmed 2026-11-22 via `command -v`; **discrepancy note:** §12.11 (2026-11-04) recorded "Claude Code 本机版本为 2.1.284" — the binary has since been removed or lived off-PATH; historical mention is NOT evidence of a usable install. `~/.claude/skills` exists but version/behavior unknown | locate any leftover install (off-PATH check by the operator), then user-supplied install of a **pinned** version; record `claude --version` output + install channel + binary path/hash |
| PG-2 `.claude/skills/` discovery semantics | unknown for any version | real discovery test on pinned version: does Claude Code read `.claude/skills/<name>/SKILL.md` in project root? precedence vs `~/.claude/skills`? |
| PG-3 prompt-ack / event-stream for Claude sessions through Herdr | `providers.py` marks both `unknown` even for codex/opencode | Herdr + harness observation logged per M2-09 vocab; capability snapshot update with source `local_process` |
| PG-4 harness allowlist `codex|opencode` hard-baked | `ceo_initialization.py` L190 | code change (P3 below) gated on PG-1..PG-3 plus CLAUDE entry contract |
| PG-5 installer has no `.claude` target | §1.1 | P2 implementation with the same fail-before-write discipline |
| PG-6 thin `CLAUDE.md` generator | does not exist; current policy forbids creating it | R3 design decision + installer subcommand (explicit opt-in) |
| PG-7 real Herdr dispatch/Wake runs for **codex/opencode** themselves | deferred per M2-09 status doc; FR-025 实机未验证 | scheduled host runs recorded before any cross-harness E2E claims (R4's non-Claude legs included) |
| PG-8 cost authority for cross-harness E2E model calls | cloud calls require M5-03 budget/approval; two M5-03 validations deferred (12.27) | budget approval + NULL-safe usage rows (M5-04) |
| PG-9 OpenCode model-driven Skill-load unverified | §12.11 smoke failed `provider.no-route` (omlx model unavailable 2026-11-04); discovery-only since | re-run Skill-load smoke after local model route recovery; record version + hash match |

## 4. Concrete adapter implementation plan (future slices; ordered, gated)

> Every phase: new files owned explicitly; no phase may mark roadmap cells REAL
> without the named evidence artifact; none of this is executed in this slice.

**P0 — Target pin & environment evidence (no code).**
Record chosen Claude Code version + acquisition channel in a `M6_06_TARGET.md`
evidence note (user performs install; TeamCraft never installs). Exit: pinned
version string + hash of the binary as run. Blocks: nothing.
Gate: all later phases blocked while PG-1 open.

**P1 — Discovery probe (`providers.py` + `tests/test_providers_m501.py`).**
Replace the static claude branch with a real bounded probe mirroring codex/opencode:
`claude --version` (same 5 s/4 KiB bounds), `skill_rules` = project
`.claude/skills/teamcraft-ceo/SKILL.md` presence, `project_rules` = `CLAUDE.md`
presence; keep `event_stream/prompt_ack` = `unknown` until PG-3 closes; missing
binary keeps returning honest `executable_missing` / stale-unhealthy semantics.
Exit: probe matrix test incl. missing-binary, wrong-version, skill-absent cases.

**P2 — Installer managed copy (`scripts/install.sh` + `tests/test_install.py`).**
Add `.claude/skills/teamcraft-ceo/` as a managed root carrying the **same bytes**
as the canonical skill.
Decision (default): **managed copy, symlink forbidden** — matches the existing
anti-symlink posture (`storage.py` refuses symlinked DB paths), avoids
Windows/synced-folder symlink variance, and keeps copy-drift checkable by the
existing sha256 verify. Drift proof: verify step re-hashes every managed copy
against the canonical manifest and fails closed (no silent repair).
User-file rule preserved: installer still never edits `CLAUDE.md`; a generator
exists only as explicit `--write-claude-md` opt-in that writes **only if absent**
(or managed-marker + `--force`), producing a ≤30-line thin pointer to the skill
(R3). Exit: conflict/identical/force matrix tests parity with the OpenCode profile
rules.

**P3 — Init & dispatch allowlist (`ceo_initialization.py`, `runtime_adapter.py`
+ their tests).**
Extend harness allowlist to `claude` **only when** its capability snapshot is
healthy and skill+rules discovery passed; bind protocol hash identical to the
other harnesses. Without a host-trusted receipt (PG-3), Claude follows the same
`assumed_default` conservatism already documented — bootstrap transport success,
post-submit native session binding, second identity check. Exit: init contract
tests parity: `incompatible` on stale/mismatched hash, refusal when probe stale.

**P4 — Wake mapping (`wake.py` path, tests).**
Reuse the existing `pending→sending→submitted|unknown` machine; Claude inherits
today's honest limits (no ack channel ⇒ submit-unverified + watcher reconciliation;
never a claim of delivery). Exit: PG-3 observation log recorded; capability
snapshot updated; no new trust invented.

**P5 — Verification & delivery.** Expected **zero changes** — these are
harness-agnostic (verifier commands + commit/diff/PR evidence on the local
authority). The plan's obligation is only to *demonstrate* parity in the run
matrix with Claude-authored commits failing/passing identical Diff Gates. Exit:
one local-only scenario artifact.

**P6 — Cross-harness E2E runner & matrix record (`scripts/` evidence note +
`doc/M6_06_RUN_LOG.md`, future).**
Same protocol version/hash pinned in one manifest; scenario script executes (or
records as pending) init→dispatch→Wake→verify→delivery per harness; each row
stores harness/version/hash + real/mock origin + timing; report and local-only
scenarios in scope, direct-PR only under M4-05 rules and explicit approval
(PG-8 budget). Real and mock results are **columns that never merge**.

## 5. Run matrix — vocabulary and current state

Vocabulary (never collapsed):
**REAL(✓…)** = executed against the live tool, evidence ref given ·
**REAL-PENDING** = host has the tool; run not yet executed/recorded ·
**STUB** = offline/mock contract exists in CI ·
**UNAVAILABLE** = tool not present on host ·
**NOT-BUILT** = adapter code path absent in repo.

| Scenario | Codex | OpenCode | Claude | Mock/Fake (CI) |
|---|---|---|---|---|
| Protocol read + version/hash pin (init 1/2) | REAL ✓ §12.11: Codex CLI 0.166.0 live call discovered/loaded `teamcraft-ceo`, returned `1.0.0` + manifest-matching SHA-256 | PARTIAL ✓ discovery via `opencode debug agents` (v2.0.29); model-driven Skill-load smoke FAILED (`provider.no-route`, omlx model) ⇒ REAL-PENDING re-run after router recovery | UNAVAILABLE (PG-1); probe path NOT-BUILT (P1) | STUB — `test_ceo_protocol_m108` |
| Skill/rules discovery (`.agents`/`AGENTS.md`/`.opencode`; `.claude`/`CLAUDE.md`) | REAL ✓ probe healthy §12.25 (2026-11-20) | REAL ✓ same | UNAVAILABLE; `.claude` semantics unknown (PG-2); installer target NOT-BUILT (P2) | STUB — `test_providers_m501` incl. static-unsupported branch |
| CEO initialization handshake | REAL-PENDING (FR-025 实机未验证; mock contract green) | REAL-PENDING (same) | NOT-BUILT — allowlist rejects `claude` (L190); after P3 becomes REAL-PENDING | STUB — `test_ceo_initialization_m208` |
| Worker dispatch (prompt → herdr pane) | REAL-PENDING — binary present; real prompt recorded not found (PG-7) | REAL-PENDING — same | UNAVAILABLE / NOT-BUILT | STUB — `test_runtime_adapter_*` |
| Wake (ceo.wake submit + reconcile) | REAL-PENDING — "No real Herdr prompt was sent" per M2-09 status doc | REAL-PENDING — same | NOT-BUILT (inherits P3/P4) | STUB — `test_wake_m209` |
| Verification (verifier cmds + evidence) | REAL ✓ M4-03/04 accepted evidence chain (§12.2x) | harness-agnostic — same | harness-agnostic — pending worker existence | STUB — `test_verification_*` |
| Delivery: commit/diff/local-only | REAL ✓ M4-04 exit | same | pending (no Claude-authored commits possible) | STUB — `test_delivery_*` |
| Delivery: direct-PR (M4-05) | REAL ✓ recorded accepted (12.23, `gh`-backed) | same path | pending + budget gate PG-8 | STUB — `test_delivery_m405`, `test_github_adapter_m405` |
| Cross-harness same-hash E2E (R4) | — | — | cannot start: blocked by PG-1..PG-8 | runner NOT-BUILT (P6) |

**Copy-drift check (R6):** all harness entries currently reference the single
canonical `CEO_PROTOCOL.md`; worktree copies are clone instances. `.claude` copy
will add the first *relocated* copy — hence P2 mandates hash-verify against the
canonical manifest at install and at init, failing closed.

## 6. Honest "pending" declaration (roadmap compliance)

As of `77a7385` on this host: **Claude columns are UNAVAILABLE or NOT-BUILT and
must be reported as pending; no M6-06 acceptance claim is made.** Codex/OpenCode
legs mix REAL (protocol pin on Codex §12.11; probes §12.25; M1/M4/M5 accepted
chains) with REAL-PENDING (OpenCode model-driven Skill-load PG-9;
init/dispatch/Wake host runs PG-7). Cross-harness E2E cannot be attempted
before PG-1 and PG-7 close — and note the E2E needs a working model route for
*every* harness leg, not just Claude — while any model-invoking leg additionally
requires M5-03 budget approval with NULL-safe usage accounting (M5-04).

## 7. Decisions requiring later evidence

| ID | Decision | Blocking evidence |
|---|---|---|
| D1 | Target Claude Code version + acquisition channel | PG-1 artifact (pinned version, hash) |
| D2 | managed copy vs verified symlink | default = copy (§4 P2 rationale); symlink re-opened only with cross-platform symlink evidence |
| D3 | Claude ack/event capabilities | PG-3 host observations |
| D4 | `CLAUDE.md` opt-in generator contract | PG-6 + user-consent review (never overwrite) |
| D5 | Which E2E legs are allowed to invoke paid models | PG-8 budget/approval under M5-03 |
| D6 | Whether cross-harness E2E gates M6 exit or ships as evidence-only | roadmap 12.x review, after first recorded run |

## 8. Non-goals of this slice (and until when)

No code, schema, installer, or config changes; no install of any software; no
network/cloud calls; no Herdr session manipulation; no claims of real Claude
behavior. This document is the M6-06 first slice: inventory, plan, matrix, and
the gap gates that keep every future claim tied to recorded evidence.

---
*Evidence pins: `scripts/install.sh` (L20–250), `teamcraft/ceo_protocol.py`,
`teamcraft/ceo_initialization.py` (docstring; L190), `teamcraft/providers.py`
(L220–247), `teamcraft/runtime_adapter.py` binding keys,
`doc/M2-09_WAKE_DISPATCH_STATUS.md` (L38–69), M0 review §9.6 M6-06 row,
§12.11/§12.23/§12.25/§12.27, FR-025 row (L158); host `command -v` results
2026-11-22. Recorded at `main` 77a7385; all gaps PG-*/decisions D* unclosed at
draft time.*

---

## Claude 行（PG-1）pending 与降级规范

> **状态声明：** 本机 `claude` 可执行文件**缺失**（PG-1，`command -v claude` 无输出）。
> 因此本文件中一切 Claude 相关矩阵行**必须保持 `pending:PG-1`**，不得升级为
> REAL/✓，不得计入 M6-06 通过。本轮只写规范，不安装、不派工、不调用任何真实
> Claude / Herdr / GitHub Actions。

### 1. 权威状态 token

| 项 | 当前必须的表述 | 禁止的表述 |
|---|---|---|
| Claude 矩阵单元 | `pending:PG-1`（等价展开可写 `UNAVAILABLE (PG-1)` / `NOT-BUILT (P1–P4, gated by PG-1)`） | `REAL ✓`、`PARTIAL ✓`、`passed`、`done` |
| `probe_harness("claude")` | 静态 `health=unsupported`、`reason_code=adapter_pending`；二进制缺失时 `executable_missing` | 伪造 `healthy` / 版本号 / hash |
| 跨 harness E2E (R4) | 只能写“2/3 harnesses recordable，Claude pending:PG-1” | “cross-harness E2E complete” |
| 复制漂移 (R6) | 仅对**已存在**的副本给出 MATCH/DRIFT | 对不存在的 `.claude` 副本声称 MATCH |

`pending:PG-1` 是**规范 token**；任何报告、看板、矩阵、PR 描述若描述 Claude，
都必须带上它，直到第 4 节的退出条件全部满足。

### 2. 不可用时的降级行为（硬规则）

1. **不派工：** 不向任何 `claude` pane 发送 Prompt；不创建 Claude worktree/branch；
   不执行 `claude` CLI；不安装/升级 Claude Code（TeamCraft 从不自行安装软件）。
2. **不声称通过：** 不把任何 Claude 行标为 REAL/✓/passed；不得以“计划已就绪”替代
   真实执行；不得让 Claude 行的缺失阻塞被表述为“已通过”。
3. **不伪造证据：** 不编造版本字符串、二进制路径/hash、发现结果、`prompt_ack`/
   `event_stream` 观测；`assumed_default` 必须按原样记录，绝不写成 host-trusted receipt。
4. **允许的动作：** 文档/计划、离线契约测试（STUB 列）、只读 `command -v` 探测、
   对**已存在**副本做 sha256 清点。
5. **对其它 leg 的影响：** Codex/OpenCode leg 可各自推进，但不得据此声称与 Claude
   对等；它们的 init/dispatch/Wake 仍受 PG-7 约束，模型调用仍受 PG-8 预算约束。
6. **失败即降级，不静默重试：** Claude 探测失败/超时/版本不符 ⇒ 保持 `pending`，
   记录原因码，不自动重试、不切换到其它 harness 冒充。

### 3. 与现有 protocol 的一致性要求

Claude 一旦可用，其初始化/派工必须与现有 harness **消费同一份** canonical 协议：
`protocol_version = 1.0.0`，`sha256 = c5c358bf5e1654bc902579972acda4b29984ffde171a547559d289b20ef0a742`，
且 `verify_protocol(..., expected_version="1.0.0", expected_sha256=c5c358bf…742)`
在安装副本上返回 PASS；`.claude` 副本是**首个 relocated copy**，必须在 install 时
与 init 时各校验一次，漂移即 **fail-closed**（不静默修复）。

### 4. 未来可用时的退出条件（全部满足才可离开 `pending:PG-1`）

| ID | 退出条件 | 需要的证据（真实、可复现） |
|---|---|---|
| **E1** | 固定 Claude 版本 | `claude --version` 精确字符串 + 二进制路径 + 二进制 sha256 + 安装渠道；运行期间版本冻结 |
| **E2** | 真实发现 | 在钉死版本上验证 `.claude/skills/<name>/SKILL.md` 的项目级发现与 `~/.claude/skills` 优先级（关闭 PG-2），记录原始输出 |
| **E3** | 真实初始化 | CEO 初始化状态机真实走通 `discovered→initialized`，绑定 native session；`assumed_default` 路径须有 bootstrap 传输 + post-submit 绑定 + 二次身份校验 |
| **E4** | 真实派工 + Wake | 一次真实 prompt 提交 + 一次 `ceo.wake`，记录 `pending→sending→(submitted 或 unknown)`；无 ack 通道时必须如实标 `unknown` |
| **E5** | 真实验证 | verifier 命令输出 + commit/diff 证据，且 Diff Gate 结论与其它 harness 一致 |
| **E6** | 协议 version/hash 一致 | 第 3 节 PASS，且全量副本 0 DRIFT |
| **E7** | Mock 与真实分栏 | 同一行的 `real` 与 `mock` 结果**分列**，各自带 harness/version/hash/timestamp/origin；永不合并 |
| **E8** | 预算授权 | 任何调用付费模型的 leg 需 PG-8 预算批准 + NULL-safe 用量（M5-04） |

**状态迁移（单向）：**
`pending:PG-1` →（E1–E8 全绿 + 操作者按 HG-F 显式同意 G1）→ `REAL-PENDING`
（工具在、已记录待跑）→ 真实运行证据落盘后 → `REAL ✓`。
任一条不满足即停在上一状态；**不得跳级**，也不得由文档或计划自行推进。

### 5. 边界声明

本节**未执行**任何需要操作者同意的真实派工，**未安装** Claude，**未声称** Claude
发现/初始化/派工/验证已通过，也**未声称**真实 Herdr / Claude / GitHub Actions 已运行。
PG-1 仍未关闭；Claude 行在矩阵中保持 `pending:PG-1`。
