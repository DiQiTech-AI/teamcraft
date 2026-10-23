# 团匠 TeamCraft 实施记录

> 让 AI 各司其职，协作交付。

更新日期：2026-10-23

## 已完成

- 支持 CEO、CTO、研发、设计、测试及自定义角色；角色职责与 harness 类型独立选择，按任务需要创建成员。
- 在当前 Herdr tab 中创建同项目目录的 sibling pane，保持焦点；先解析当前有效 pane ID，避免 pane 移动后布局匹配失败。
- 将成员、任务、责任路径、pane 身份及执行阶段持久化到 `.herdr-team/state.json`；使用原子写入、状态锁和成员生命周期锁。
- 编辑任务必须声明文件或目录范围；拒绝活动任务间的相同或父子路径冲突，支持只读任务及中文、空格路径。
- 提供 `status`、`resume`、`complete`、`release`。恢复时核验 terminal ID 和已有原生 Agent session，复用已记录的 pane。
- 对 split 结果不明、任务可能已发送等情况要求显式选择，默认恢复不会重复发送任务；无法核实实时状态时报告 unknown，不展示旧缓存为当前状态。
- 完成或释放任务前检查 Agent 状态；working、blocked、unknown 需要显式 `--force`。这些命令不关闭 pane 或终止 Agent。
- 新增六种 OpenCode 工种配置，使用 `omlx/Qwen3.8-Flash-Next-oQ4e-mtp`；通过 pane 局部配置选择角色，并合并已有 JSON 配置。本机 `opencode.json` 保持私有，仓库提供 `opencode.example.json`。
- 增加 harness、角色配置文件、任务编码及路径预检、子进程超时处理，以及运行状态和 Python 缓存的 Git 忽略规则。

## M1-02 待验收实现

- Skill 技术名和目录已在工作树中直接改为 `teamcraft`，并同步安装器、中英文 README、测试及角色引用；`.herdr-team/` 运行状态目录和全部 CLI 行为保持不变，待用户验收后才登记任务完成。
- 已新增 Python 3.10+ 的最小 `teamcraft` 包，只定义 opaque ID、UTC Clock、错误类型和无写入的配置加载边界，未引入数据库、Scheduler、Watcher、Web、Worktree 或模型 Router；本机仅用 Python 3.13.14 验证，3.10 和 Linux 仍待 CI 证据。

## 关键文件

| 文件 | 用途 |
| --- | --- |
| `.agents/skills/teamcraft/SKILL.md` | 使用规则、团队职责及操作示例 |
| `.agents/skills/teamcraft/scripts/teamctl.py` | 团队启动、状态、恢复和责任范围管理 |
| `.agents/skills/teamcraft/tests/test_teamctl.py` | 保留的回归测试 |
| `teamcraft/`、`pyproject.toml` | M1-02 最小 Python 包及版本边界 |
| `.opencode/agents/herdr-*.md` | 六类 OpenCode 角色配置 |
| `.gitignore` | 排除本地运行状态和缓存 |

## 验证结果

```bash
python3 -B -m unittest discover -s .agents/skills/teamcraft/tests -v
python3 -B -m unittest discover -s tests -v
python3 -B "${SKILL_CREATOR_DIR}/scripts/quick_validate.py" .agents/skills/teamcraft
```

`SKILL_CREATOR_DIR` 需指向本机已安装的 `skill-creator` Skill 目录；其中的 `quick_validate.py` 是外部校验工具，不属于本仓库依赖。

M1-02 离线回归在 Python 3.13.14 上通过：Skill 40/40，仓库测试 28/28（21 项安装器、7 项最小 Core 边界），退出码均为 0；`quick_validate.py` 对新 Skill 入口校验通过，退出码为 0。测试使用临时目录和 mock Herdr，未连接实际 Herdr 会话；59 项 M1-01 既有契约保持通过，并增加新 Skill 发现/安装与无导入副作用等边界检查。2026-10-18 的 `herdr pane --help` 实机确认仍是历史证据，本轮未重复操作 Herdr。

### 实机示例

2026-10-18 在当前 Herdr tab 中启动了一个真实 OpenCode 团队：

- `demo_ceo` 加载 `Herdr-Ceo · Qwen3.8-Flash-Next-oQ4e-mtp`，按任务创建成员并收集结果。
- `demo_dev` 加载 `Herdr-Developer` 配置，只在 `examples/team-demo/**` 下创建 `index.html`。
- `demo_test` 加载 `Herdr-Tester` 配置，以只读方式验收标题、主标题、CEO/Developer/Tester 角色列表和无外部依赖，结论为通过且无阻塞。
- 实机发现 Herdr 的 `pane current` 响应可能不含成员名；状态核验现会通过 `agent get <recorded-name>` 补全名字，并再次核对 pane、terminal、kind 和原生 session，避免把正确成员误报为无关 occupant。
- 实机还发现已完成成员可能保持 `idle` 而不切换为 `done`；Skill 现明确将两者都视为 settled，禁止只等待 `done`。本次停止 CEO 的错误轮询时 `Ctrl+C` 同时退出了其 OpenCode，但 pane 保留为空闲 shell，已有开发和测试结果未受影响，三个成员随后通过 `teamctl complete` 正常归档。
- 所有交互权限仅按本次本地任务逐项允许；已有用户 Agent 和本机 `opencode.json` 未修改。

## 使用示例

在 Herdr 内进入本项目根目录后执行：

```bash
# 创建 CEO；具体交付目标替换为实际任务
python3 .agents/skills/teamcraft/scripts/teamctl.py launch \
  --role ceo --kind opencode --name ceo \
  --task "分析项目任务，拆分职责并按需创建研发、设计和测试成员。" \
  --agent-arg=--standalone

# 查看团队与成员实时状态
python3 .agents/skills/teamcraft/scripts/teamctl.py status

# 恢复指定成员，默认不重复发送任务
python3 .agents/skills/teamcraft/scripts/teamctl.py resume api_dev

# 核查任务未执行或确定需要重发后，显式提交
python3 .agents/skills/teamcraft/scripts/teamctl.py resume api_dev --prompt-action send

# 验收完成后释放责任范围，不关闭成员 pane
python3 .agents/skills/teamcraft/scripts/teamctl.py complete api_dev
```

`api_dev` 为示例名称，需替换为实际记录的成员。研发等编辑任务需提供 `--ownership 'src/api/**'` 之类的范围；纯审阅可使用 `--read-only`。

## 尚未验证及边界

- CEO、developer、tester 的 OpenCode 角色和模型显示，以及 CEO 派工、developer 产出、tester 验收通过的链路已实机验证；CEO 最终归档由当前控制 Agent 接管。CTO、designer、custom 角色尚未分别启动验证。
- ownership 是协调约束，不是操作系统文件访问隔离；CEO 仍需审查实际改动和测试证据。
- 状态绑定 Herdr socket 会话；跨会话迁移、历史任务归档及成员复用尚未实现，完成或释放后应为新任务使用新成员名。
- `--force` 只释放记录中的责任范围，不会停止 Agent；使用前须自行确认不会继续产生冲突修改。
- 将本 Skill 用到其他项目时，需要在目标项目部署相应 OpenCode 角色配置和模型/provider 配置。

下一步：在真实项目需要这些职责时，再分别验证 CTO、designer 和 custom 配置。
