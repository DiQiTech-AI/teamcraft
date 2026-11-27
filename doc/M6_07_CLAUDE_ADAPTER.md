# M6-07 — Claude 适配（范围、布局与验收登记）

| 字段 | 值 |
|---|---|
| 状态 | **`in progress`；docs-only 登记。本文件不是实现，也不构成 M6-07 验收。** |
| 路线图条目 | M0 review §9.6 M6-06（Claude 行已转 M6-07，§12.46）；依赖 M1-08、M2-09、M3-06、M4-04、M5-03 |
| 真实 Claude leg | **`pending:PG-1`**（本机无 `claude` 可执行文件；见第 4 节） |
| 协议 | `protocol_version=1.0.0`，`sha256=c5c358bf5e1654bc902579972acda4b29984ffde171a547559d289b20ef0a742` |
| 记录基线 | `main` @ `1b52ea2`（M6-06 按路径 B 关闭）；记录时间 2026-11-27 |
| 相关历史文档 | `doc/M6_06_HARNESS_READINESS.md`（M6-06 就绪计划，作为前序证据保留，不改写） |

**登记口径**：M6-06 已按路径 B 关闭（OpenCode + Codex 真实 local-only leg PASS；Claude 延期，`pending:PG-1`，见 §12.45）。本文件把延期后的 Claude 工作固化为 M6-07 的范围、安装布局、发现/漂移校验、退出条件与降级规则，供后续实现与独立验收引用。**本文件不声称真实 Claude 已发现、已初始化、已派工或已通过，也不替代任何测试或操作者验收。**

---

## 1. 目标与非目标

### 1.1 目标

1. **技能布局**：在项目内提供 `.claude/skills/teamcraft/` 与 `.claude/skills/teamcraft-ceo/`，内容与 `.agents/skills/` 下的 canonical 技能树**字节一致**，使 Claude Code 与 Codex/OpenCode 消费同一份协议。放置形式为**受管副本**（默认）或**经验证的 symlink**（仅在满足第 2.2 节条件时）。
2. **项目规则入口**：生成**精简 `CLAUDE.md`**（仅指向已安装技能），带 TeamCraft 受管标记；**永不覆盖或改写用户已有的 `CLAUDE.md`**。
3. **同一协议**：Claude 副本的 `CEO_PROTOCOL.md` 必须与 canonical 一致：`1.0.0 / c5c358bf…a742`；不得出现第二份协议正文或第二套版本号。
4. **发现与漂移校验**：在安装时与初始化时各校验一次协议 version/hash；所有已存在的受管副本必须可清点为 `MATCH`，任一副本漂移即 **fail-closed**（不静默修复、不降级为警告）。
5. **真实 leg**：在固定 Claude Code 版本上真实跑通发现 → 初始化 → 派工 → Wake → 验证 → 交付，并按第 5 节的退出条件留下可复现证据。

### 1.2 非目标

- 不安装、不升级、不探测真实 Claude Code 二进制作为本里程碑的交付物（TeamCraft 从不自行安装软件；安装由操作者完成）。
- 不修改 `AGENTS.md`、`opencode.json` 或用户自有规则文件；不改变 Codex/OpenCode 现有安装行为。
- 不在 `CLAUDE.md` 中复制 CEO 协议正文；`CLAUDE.md` 只做薄指针。
- 不新增 CEO 协议版本、不引入第二状态真源、不放宽 nonce/身份/门禁。
- 不把 Mock、离线夹具或"计划已就绪"写成真实 Claude 证据。
- direct-PR 不因 M6-07 自动纳入；若纳入，另按 M4-05 与预算/审批规则执行。

---

## 2. 安装布局与文件契约

### 2.1 受管根与来源

| 目标路径（项目内） | 来源（canonical） | 约束 |
|---|---|---|
| `.claude/skills/teamcraft/**` | `.agents/skills/teamcraft/**` | 字节一致；受管文件；用户不得手改 |
| `.claude/skills/teamcraft-ceo/**` | `.agents/skills/teamcraft-ceo/**` | 字节一致；含 `references/CEO_PROTOCOL.md` 与 detached manifest |
| `CLAUDE.md` | 安装器内置的精简文本 | 受管标记 + 薄指针；仅缺失时创建，或 managed-marker + `--force` 时升级 |
| `.opencode/agents/**`、`opencode.json` | 既有来源 | **行为不变**；Claude 适配不得改写 |

来源是仓库内 `.agents/skills/`；`protocol.manifest.json` 是 version/hash 的 detached 权威记录。安装器只做复制/校验，不做协议改写。

### 2.2 受管副本 vs 验证 symlink（决策规则）

- **默认：受管副本。** 理由：与现有 anti-symlink 姿态一致，避免 Windows/同步目录/容器挂载下的 symlink 差异，且副本可直接用既有 sha256 校验发现漂移。
- **symlink 仅作为可选实现**，且必须同时满足：
  1. 链接目标解析后仍位于本项目内，且指向 canonical `.agents/skills/` 对应路径；不得指向用户目录或项目外；
  2. 在钉死的 Claude Code 版本上，真实发现测试证明 Claude 能通过该链接加载 `SKILL.md`；
  3. 漂移校验读取**解析后的内容**计算 sha256，而不是只比较链接字符串；
  4. 任一条件缺证据即回退为受管副本，不得以"symlink 更简单"为由跳过验证。
- 无论副本还是 symlink，`.claude/skills/` 都不得成为用户可编辑的第二真源；发现漂移即 fail-closed。

### 2.3 精简 `CLAUDE.md` 契约

- 带受管标记（行首注释），内容为**薄指针**：指向 `.claude/skills/teamcraft/SKILL.md` 与 `.claude/skills/teamcraft-ceo/SKILL.md`，说明由 TeamCraft 管理、升级需重跑安装器；不复制协议正文、不写角色/流程细节。
- **用户文件优先**：目标已存在且字节不相同、又没有 TeamCraft 受管标记时，安装必须 **fail-before-write**；即使 `--force` 也不得替换用户文件。只有 byte-identical 或 managed-marker + `--force` 才允许写入/升级。
- 冲突时零部分写入：预检全部通过后才落盘；不得出现技能已装、`CLAUDE.md` 半写的状态。

### 2.4 冲突与幂等（与现有安装器一致）

- 预检所有受管目标；目标为非普通文件（目录、设备、悬空链接）即拒绝。
- 首次安装创建；重复安装幂等（内容一致则不改写）；受管文件差异需显式 `--force`。
- dry-run 必须零写入，且报告将创建/更新/保持的文件清单。
- 不得修改 `.agents/skills/` 之外的来源，也不得反向从 `.claude` 覆盖 `.agents`。

---

## 3. 协议一致性与发现/漂移校验

### 3.1 权威值

| 项 | 值 |
|---|---|
| canonical 正文 | `.agents/skills/teamcraft-ceo/references/CEO_PROTOCOL.md` |
| detached manifest | `.agents/skills/teamcraft-ceo/references/protocol.manifest.json` |
| `protocol_version` | `1.0.0` |
| `sha256` | `c5c358bf5e1654bc902579972acda4b29984ffde171a547559d289b20ef0a742` |

本机复核（2026-11-27）：`shasum -a 256` 对 canonical 正文的输出与 manifest 完全一致；该复核只证明当前 canonical 字节，不证明 `.claude` 副本存在或 Claude 可用。

### 3.2 校验点

1. **安装时**：每个 `.claude` 受管副本写入前，与来源逐文件比较；写入后（或在 dry-run 的计划中）对 `CEO_PROTOCOL.md` 重算 sha256 并与 manifest 比对。任何缺失、不可读、非 UTF-8、symlink 不安全、version/hash 不符均拒绝安装。
2. **初始化时**：CEO 初始化在解析 Claude 行时，对**实际加载路径**的 `CEO_PROTOCOL.md` 再次执行 `verify_protocol(..., expected_version="1.0.0", expected_sha256=…)`；失败即 `incompatible`/拒绝，不得进入派工。
3. **清点/审计时**：对所有已存在副本（`.agents`、worktree clone、`.claude`）逐一给出 `MATCH`/`DRIFT`/`MISSING`；**不存在的副本不得写成 `MATCH`**。M6-06 关闭时记录为现有副本 21/21 MATCH、`.claude` 未安装（§12.45）；M6-07 安装后需重新清点并报告总数。
4. **漂移处理**：fail-closed，保留证据（路径、期望/实际 hash、来源）；不做静默修复，不自动重装，不继续派工。

### 3.3 发现语义（待真实证据关闭）

- 需要在钉死版本上验证：Claude Code 是否读取项目级 `.claude/skills/<name>/SKILL.md`；与 `~/.claude/skills/` 的优先级；`CLAUDE.md` 是否作为项目规则被加载；技能内相对引用是否可解析。
- 本机存在 `~/.claude/skills` 目录，但其来源/版本/行为未知，**只视为环境残留，不作为安装或发现证据**。
- 在真实发现证据落盘前，能力探测只能报告观测事实：无注入 runner 时保持静态 `unsupported / adapter_pending`；有界探测即使看到版本，也因未钉死兼容版本集合而不得升级为 `healthy`；`event_stream`/`prompt_ack` 保持 `unknown`。

---

## 4. 当前状态（2026-11-27）

| 项 | 状态 | 说明 |
|---|---|---|
| `claude` 二进制 | **缺失（PG-1）** | 2026-11-27 复检 `command -v claude` 无输出；无版本/PATH/hash 证据 |
| 项目 `.claude/` | **不存在** | 尚未安装；不得报告副本 MATCH |
| `~/.claude/skills` | 存在但不可信 | 环境残留；不构成可用安装或发现证据 |
| M6-07 安装器适配（m607_installer） | `in progress` | 以 `scripts/install.sh` 的未提交改动为准；本文件不宣称其测试通过 |
| CEO allowlist（m607_ceo_allowlist） | `in progress` | 记录快照时 `teamcraft/ceo_initialization.py` 仍只接受 `codex`/`opencode`；以该任务报告为准 |
| `probe_harness("claude")` | 观测路径存在、兼容版本未钉死 | 未注入 runner 时为静态 `unsupported/adapter_pending`；注入 runner 时只做有界 `claude --version` 与文件存在性观测，health 不因观测自身升级 |
| 真实 Claude leg | **`pending:PG-1`** | 未发现、未初始化、未派工、未 Wake、未验证、未交付 |
| 跨 Harness 对照 | 2/3 可记录 | Codex/OpenCode 的 local-only leg 已在 M6-06 记录；Claude 不参与任何"三 Harness 全通过"表述 |

**结论**：M6-07 处于范围登记与实现进行中；**没有任何真实 Claude 证据，M6-07 未通过**。所有涉及 Claude 的矩阵/看板/报告单元必须带 `pending:PG-1`，直到第 5 节全部满足。

---

## 5. 退出条件（全部满足才可离开 `pending:PG-1`）

| ID | 退出条件 | 必需证据 |
|---|---|---|
| **E1** | 固定 Claude Code 版本 | `claude --version` 精确字符串 + 二进制路径 + 二进制 sha256 + 安装渠道；运行期间版本冻结 |
| **E2** | 真实发现 | 在 E1 版本上验证项目级 `.claude/skills/<name>/SKILL.md` 发现、与 `~/.claude/skills` 的优先级、`CLAUDE.md` 规则加载；保留原始输出 |
| **E3** | 真实初始化 | CEO 初始化真实走通 `discovered→initialized` 并绑定原生 session；`assumed_default` 必须有 bootstrap 传输 + post-submit 绑定 + 二次身份校验 |
| **E4** | 真实派工 | 一次真实 prompt 提交到 Claude pane；记录 `pending→sending→submitted|unknown`，身份/pane/terminal/session 绑定可核验 |
| **E5** | 真实 Wake | 一次真实 `ceo.wake` 投递；无 ack 通道时如实记录 `unknown`，不得写成 delivered/acknowledged |
| **E6** | 真实验证与交付 | verifier 命令输出 + commit/diff 证据；Diff Gate 结论与其它 harness 一致；local-only 交付按 M4-04 留证 |
| **E7** | Mock 与真实分栏 | 同一场景的 `real` 与 `mock` 结果分列，各自带 harness/version/hash/timestamp/origin，永不合并；Mock 不替代真实 |
| **E8** | 协议 0 漂移 | `1.0.0 / c5c358bf…a742` 在 canonical、`.agents` 副本、`.claude` 副本与加载路径全部 MATCH；全量清点 0 DRIFT |

**状态迁移（单向、不得跳级）**：
`pending:PG-1` →（E1–E8 全绿 + 操作者按 HG-F 显式同意）→ `REAL-PENDING`（工具在、证据待跑）→ 真实运行证据落盘 + 独立验收 → `REAL ✓`。
任一条不满足即停在上一状态；文档、计划、Mock 测试或安装器单测都不能推进该状态。

**验收边界**：即使 E1–E8 全绿，M6-07 的关闭仍需独立验收/操作者确认；本文件作者不自我验收。

---

## 6. 降级与禁止行为

1. **Claude 不可用时不派工**：不创建 Claude pane/worktree/branch，不发送 Prompt，不执行 `claude` CLI，不安装或升级二进制。
2. **不声称通过**：不把 Claude 行标为 `REAL`/`✓`/`passed`；不以"适配已实现"替代真实运行；不把"2/3 harness"表述为跨 Harness 完成。
3. **不伪造证据**：不编造版本、路径/hash、发现输出、`prompt_ack`/`event_stream` 观测；`assumed_default` 原样记录，绝不写成 host-trusted receipt。
4. **失败即降级**：探测超时、版本不符、发现失败、协议漂移、`CLAUDE.md` 冲突 → 保持 `pending` 或 fail-closed，记录原因码；不自动重试、不静默修复、不切换到其它 harness 冒充。
5. **允许的动作**：文档/计划、离线契约测试、只读 `command -v`、对**已存在**副本做 sha256 清点、安装器 dry-run（零写入）。
6. **不影响其它 leg**：Codex/OpenCode 可各自推进，但不得据此声称与 Claude 对等；模型调用仍受预算/审批约束。

---

## 7. 责任分工与登记状态

| Owner | 范围 | 状态 |
|---|---|---|
| **m607_installer** | `scripts/install.sh`、`tests/test_install*.py`：`.claude` 受管副本、`CLAUDE.md` 生成/冲突、幂等/dry-run、副本 hash 一致 | `in progress` |
| **m607_ceo_allowlist** | `teamcraft/ceo_initialization.py`、`tests/test_ceo_initialization_m208.py`：Claude 进入同一协议/身份契约；不放宽 pin/nonce/门禁 | `in progress` |
| **m607_docs（本文件）** | `doc/M6_07_CLAUDE_ADAPTER.md`、`doc/TEAMCRAFT_2_0_M0_REVIEW.md`：范围、布局、验收与路线图登记 | `in progress` |

各 owner 的报告与测试证据互相独立；本文件只登记范围与边界，不替任何 owner 宣称完成。

---

## 8. 风险与不确定项

| 风险 | 现状 | 处理 |
|---|---|---|
| PG-2 `.claude/skills/` 发现语义未知 | 无钉死版本可测 | E2 真实发现测试；未测前不写 `supported` |
| PG-3 Claude ack/event 能力未知 | `event_stream`/`prompt_ack` 保持 `unknown` | E5 如实记录；不新造信任 |
| 兼容版本集合为空 | 观测路径存在但 health 不升级 | E1 钉死版本后再建立显式契约 |
| Windows/同步目录 symlink 差异 | 副本为默认 | symlink 仅在满足 2.2 条件时启用 |
| 用户 `CLAUDE.md` 冲突 | fail-before-write 设计 | 冲突时要求用户自行移动；不覆盖 |
| 版本升级导致布局/规则变化 | 未验证 | 每次换版本重跑 E2；能力矩阵按版本记录 |
| 模型调用成本/审批 | 未授权真实调用 | 付费 leg 需预算批准；未批准不跑 |

**未决问题**：M6-07 的关闭是否要求 direct-PR 场景（当前按 M4-05 另议）；`.claude` 副本清点总数在安装后才能确定；`~/.claude/skills` 残留是否需要操作者清理尚无结论（不影响 `pending:PG-1` 判定）。

---

## 9. 证据索引

- M6-06 关闭登记与 Claude 延期：`doc/TEAMCRAFT_2_0_M0_REVIEW.md` §12.45；M6-07 登记：§12.46。
- M6-06 就绪计划与差距寄存器（PG-1…PG-9）：`doc/M6_06_HARNESS_READINESS.md`。
- 协议与 manifest：`.agents/skills/teamcraft-ceo/references/CEO_PROTOCOL.md`、`protocol.manifest.json`。
- 本机快照（2026-11-27）：`command -v claude` 无输出；项目 `.claude/` 不存在；canonical sha256 与 manifest 一致。
- 相关代码（只读引用，不代表已验证）：`scripts/install.sh`、`teamcraft/ceo_initialization.py`、`teamcraft/providers.py`。

---

*本文件为 M6-07 文档登记，状态 `in progress`。真实 Claude leg 为 `pending:PG-1`；未 commit/push；不代表验收通过。*
