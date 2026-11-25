# M5-06 — 本地 2-bit 与云端对照实验方案（DRAFT，未执行）

| 字段 | 值 |
|---|---|
| 状态 | **方案草案。没有跑过任何一局，本文不含任何测量结果。** |
| 目标问题 | DeepSeek V4.1 Flash 在 512GB Mac Studio 上以 2-bit 运行时，相对云端官方权重，质量/速度/工具可靠性/成本的实际差距 |
| 上游依据 | `doc/TEAMCRAFT_2_0_PLAN.md` §7.3；`doc/TEAMCRAFT_2_0_M0_REVIEW.md` §12.29；`teamcraft/experiment.py` |
| 现任工具边界 | `teamcraft/experiment.py` 只接受 `cloud_only` 与 `mixed` 两个臂；**`local_only` 不是合法臂**。本方案因此分两条轨道，见 §2 |
| 授权状态 | 未授权执行。真实运行涉及云端费用与长时间占用机器，须先获得明确同意 |
| 模型版本 | 本地 `mlx-community/DeepSeek-V4.1-Flash-1017-oQ2e-mtp`；云端 `deepseek/deepseek-v4.1-flash-1017`（或 OpenCode `opencode/deepseek-v4.1-flash`） |

---

## 1. 要回答什么，不回答什么

| 编号 | 假设 | 判定方式 |
|---|---|---|
| H1 | 本地 2-bit 的任务通过率低于云端 | 同一任务集上的 verified rate 差值 |
| H2 | 本地 decode 更慢、prefill 差距更大 | tokens/s、首 token 延迟、总耗时 |
| H3 | 本地工具调用更不可靠 | 无效工具调用率、重试次数、返工次数 |
| H4 | 本地在重度使用下才可能摊平硬件成本 | 云端账单 vs 本机电费+摊销 |

**不回答**：4-bit 本地（512GB 装不下）、多机集群、其他模型、主观"手感"、benchmark 排行榜名次。也不把任何一次运行当作总体结论。

---

## 2. 与现有 M5-06 reducer 的关系（必须先读懂）

`teamcraft/experiment.py` 的臂是硬编码的：

```python
ARMS = ("cloud_only", "mixed")
ARM_REQUIRED_LOCALITIES = {"cloud_only": ("cloud",), "mixed": ("cloud", "local")}
```

因此：

| 轨道 | 臂 | 能否直接用 reducer | 用途 |
|---|---|---|---|
| **A（本方案主体）** | `local_only` vs `cloud_only` | ❌ 不支持，且**不得**把纯本地说成 `mixed` | 直接回答"2-bit 差多少" |
| **B（后续）** | `cloud_only` vs `mixed` | ✅ 现成 | 回答"本地优先+云端升级的混合策略是否划算" |

轨道 A 需要独立的采集格式（§6）与分析脚本；如果想并入 reducer，需要先改代码新增 `local_only` 臂与对应 locality 规则，属于新的实现任务，不在本文范围。

---

## 3. 实验臂定义

| 项 | 本地臂 (L) | 云端臂 (C) |
|---|---|---|
| 权重 | `DeepSeek-V4.1-Flash-1017-oQ2e-mtp`（HF 快照 sha 必填） | 官方托管 `deepseek-v4.1-flash-1017` |
| 运行时 | `mlx-lm` 固定版本号（必填） | OpenCode 固定版本 |
| 硬件 | Mac Studio M3 Ultra 512GB 或 M5 Ultra 512GB（记录型号/带宽/功率模式） | 不适用 |
| 上下文 | 64k（上限也记录） | 64k（同值） |
| 温度 / top_p | 0.6 / 0.95（两臂相同） | 0.6 / 0.95 |
| thinking | 关闭（两臂相同；如需开，单独一组） | 关闭 |
| 系统提示 | 每任务固定，sha256 记入运行记录 | 同左 |
| 工具集 | 同一套 shell/read/edit（同 harness 版本） | 同左 |
| 最大轮次 / 超时 | 30 轮 / 20 分钟（两臂相同） | 同左 |

**禁止**：给某一臂更宽松的验收、更长的超时、"失败后人工提示再试一次"却不记录。

---

## 4. 任务集（8 个，验收全部可机判）

任务必须来自真实仓库，且验收命令客观可重复（pytest / 编译 / 结构断言），不接受模型自述"已完成"。

| ID | 类别 | 难度 | 验收方式 | 预估上下文 |
|---|---|---|---|---|
| T1 | 单文件 bug 修复 | low | 目标 pytest 用例由红转绿 | < 8k |
| T2 | 跨文件重构（改接口+改调用点） | medium | 全量测试 + `ruff`/`mypy` 干净 | 20–40k |
| T3 | 补测试（提高覆盖率到指定行） | medium | 覆盖率阈值 + 新测试全绿 | 20–40k |
| T4 | 长上下文检索（在 60k 文档里定位约束并改代码） | high | 针对性的隐藏用例 | 60k |
| T5 | 多步工具链（读日志→定位→改配置→验证） | high | 脚本化断言最终文件状态 | 20–40k |
| T6 | 依赖升级引发的兼容修复 | high | 全量测试 + 版本断言 | 30–60k |
| T7 | 性能修复（去掉 O(n²)） | medium | 基准阈值（固定数据规模） | 20–40k |
| T8 | 规格实现（按 description 从零写小模块） | low | 该模块的验收测试 | < 10k |

任务集与每条的验收脚本必须先打 digest 固定；中途修改任何一条，等于换了一个实验，需重新开始。

---

## 5. 运行协议

1. **预热**：每臂先跑 1 个不计入统计的 warm-up（避免首次加载、编译、缓存差异）。
2. **重复**：每个 (臂, 任务) 跑 **N=3**，共 8×2×3 = 48 局。MVP 可先做 4 任务 × 3 = 24 局。
3. **顺序**：随机化任务顺序与臂顺序，避免热节流/缓存偏向某一臂；记录顺序。
4. **隔离**：每局全新会话、干净 worktree（从同一 base commit 复制），不复用上下文。
5. **不干预**：中途不给人提示。若不得不干预，记录 `intervention_count += 1` 与原因，并在分析时单列。
6. **失败保留**：超时、崩溃、工具错误全部作为结果保留，禁止删除"难看"的局。
7. **环境记录**：室温/功率模式/是否接电源/其他负载；本地臂每局记录开始与结束时的内存压力与节流状态。
8. **不并发**：同一时间只跑一局，避免带宽争用（尤其本地臂）。

---

## 6. 记录格式（JSONL，每局一行）

```json
{
  "run_id": "r-0001",
  "arm": "local_2bit",
  "task_id": "T2",
  "repeat": 1,
  "started_at": "2026-11-23T10:00:00+08:00",
  "ended_at": "2026-11-23T10:07:31+08:00",
  "model": {"id": "deepseek-v4.1-flash-1017-oQ2e-mtp", "weights_sha": "…", "runtime": "mlx-lm 0.x.y"},
  "hardware": {"machine": "Mac Studio", "chip": "M3 Ultra", "memory_gb": 512, "power_mode": "high"},
  "pins": {"task_set_digest": "…", "acceptance_digest": "…", "system_prompt_sha": "…", "context_limit": 65536, "temperature": 0.6},
  "usage": {"input_tokens": 0, "output_tokens": 0, "cache_read": 0, "reasoning_tokens": 0},
  "timing": {"ttft_ms": 0, "decode_tps": 0.0, "prefill_tps": 0.0, "wall_ms": 0},
  "tools": {"calls": 0, "invalid_calls": 0, "retries": 0, "reverts": 0},
  "outcome": "verified | failed | blocked | timeout",
  "acceptance": {"command": "…", "exit_code": 0, "stdout_digest": "…"},
  "intervention_count": 0,
  "notes": ""
}
```

`null` 表示"没测到"，**不得写成 0**。这条沿用 `teamcraft/usage.py` 与 `experiment.py` 的既有语义。

---

## 7. 指标定义

| 指标 | 定义 | 备注 |
|---|---|---|
| verified_rate | verified 局数 / 总局数 | 分母含超时与崩溃 |
| pass@1 | 首次提交即通过的比例 | 与 verified_rate 分开，反映返工 |
| rework | `retries + reverts`（同一局内） | 工具调用失败后的重试单列 |
| invalid_tool_rate | 无效工具调用 / 总调用 | 参数错误、指向不存在文件、权限拒绝 |
| ttft_ms | 请求到首 token | 仅首轮，不含工具往返 |
| decode_tps | 输出 token / 纯生成耗时 | 排除工具执行时间 |
| prefill_tps | 输入 token / 首 token 前耗时 | 长上下文任务重点看 |
| wall_ms | 端到端耗时 | 用户真正感受到的 |
| intervention_count | 人工介入次数 | 必须可从日志佐证 |
| cloud_cost | 云端账单（按固定 price version） | 本地臂为 0 且单列电费 |

分析：按任务配对（同一 T 的 L 与 C 比），报告中位数与四分位距；样本小就写"范围"，不写"平均提升 X%"。

---

## 8. 成本核算（不要混算）

- **云端**：固定 `price_version`（例如 OpenCode Console 2026-11 价目），按实际 usage 计；未知用量保持 NULL，不按 0 计。
- **本地**：电费（实测功率 × 时长 × 电价）+ 硬件摊销（单列，单独给出假设，不与云端单价直接相加）。
- **结论表述**：只写"在 X 假设下，本地摊平需要 Y 年/天"，不写"本地更省钱"这种无条件结论。

---

## 9. 判读规则（先定，避免事后解释）

| 观察 | 结论 |
|---|---|
| L 的 verified_rate ≥ C − 10pp，且 invalid_tool_rate 无明显恶化 | 低/中难度任务可用本地 |
| L 在 T4/T6（长上下文/复杂修复）明显掉档 | 这些类别固定走云端 |
| L 的 wall_ms 超过 C 的 2× | 即使质量够，也不适合交互式使用 |
| 任一项出现 OOM/崩溃 | 直接判该配置不可用，不做"调参后再看"的模糊结论 |

MVP 通过标准：48（或 24）局全部有记录，无缺失，失败局保留，报告中每个数字都能追到一条 JSONL 与一条验收命令输出。

---

## 10. 威胁与对策

| 威胁 | 对策 |
|---|---|
| 量化配方差异（oQ2e vs Q2_K_XL） | 固定一种；如换配方，视为新实验 |
| 热节流导致后跑的局变慢 | 随机化顺序 + 记录功率/温度 + 每局间隔冷却 |
| prompt cache 让重复任务变快 | 每局新会话；缓存读 token 单独记录 |
| 云端模型悄悄更新 | 记录响应里的 model 字段；版本变化即作废该臂 |
| 样本太小 | 明确写 N 与置信区间，不下总体结论 |
| 本地 64k 截断而云端没截断 | 两臂同 context_limit；超限任务记 blocked，不偷偷加长 |
| 把失败局删掉 | JSONL append-only；分析脚本只读不改 |

---

## 11. 执行清单

- [ ] 得到用户对"真实运行 + 云端费用"的明确授权
- [ ] 固定任务集、验收脚本、系统提示，生成三个 digest
- [ ] 固定模型快照 sha 与运行时版本
- [ ] 先跑 1 局本地 + 1 局云端冒烟，确认记录链路可用
- [ ] 跑 MVP（4 任务 × 3 次 × 2 臂）
- [ ] 跑全量（8 任务 × 3 次 × 2 臂）
- [ ] 产出报告：范围、失败案例、成本、限制；不写"提升 X%"

## 12. 交付物

| 文件 | 内容 |
|---|---|
| `doc/M5_06_LOCAL_2BIT_VS_CLOUD_PLAN.md` | 本文（方案） |
| `doc/M5_06_LOCAL_2BIT_VS_CLOUD_RESULTS.md` | 执行后才有；没有数据就不创建 |
| `runs/m506-local-cloud/*.jsonl` | 原始记录（本地运行目录，不入库） |
| `scripts/analyze_m506_local_cloud.py` | 分析脚本（尚未编写） |

## 13. 明确未做的事

- 没有运行任何一局；本文所有性能数字都来自上一轮对话的**估算**，不是测量。
- 没有改动 `teamcraft/experiment.py`；`local_only` 臂仍不支持。
- 没有下载 313GB 权重，也没有产生云端费用。
- 没有把本地运行标成 `mixed` 来"凑"现有 reducer。
