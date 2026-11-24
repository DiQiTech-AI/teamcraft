# TeamCraft 使用流程

## 1. 在 Herdr 中启动 CEO

在目标项目根目录执行。每个项目团队先启动一个 CEO，再按任务需要派工。需要指定右侧窗格时，加 `--direction right`。

```bash
test "${HERDR_ENV:-}" = 1 && herdr --skill
python3 .agents/skills/teamcraft/scripts/teamctl.py launch \
  --role ceo --kind codex --name ceo \
  --task '担任项目 CEO。初始化后等待我分配任务。'
```

等启动命令成功返回，再到 CEO 窗格交代目标和验收标准。用 `teamctl.py status` 检查团队记录，用 `herdr agent get ceo` 核对窗格和运行状态。

## 2. 由 CEO 按需派工

每个任务写明交付物、验收标准和需要回报的证据。编辑成员用 `--ownership` 声明互不重叠的项目相对路径，支持精确文件和 `目录/**`。只做调研或验收的成员用 `--read-only`。

```bash
python3 .agents/skills/teamcraft/scripts/teamctl.py launch \
  --role developer --kind opencode --name api_dev \
  --ownership 'src/api/**' \
  --task '为列表接口增加分页。验收覆盖默认值、边界值和非法参数，请回报改动与测试结果。'
```

### 给不同成员指定模型

角色与运行工具独立选择。一个团队可以同时使用 Codex CEO、DeepSeek Developer 和本地 Qwen Tester。

- **OpenCode 按角色设置**：在 `.opencode/agents/herdr-<角色>.md` 中设置 `model`。例如，在 `herdr-developer.md` 中指定 DeepSeek，在 `herdr-tester.md` 中指定本地 Qwen。
- **OpenCode 按成员设置**：在启动该成员时，通过 `OPENCODE_CONFIG_CONTENT` 设置 `agent.<角色配置名>.model`。TeamCraft 会把配置传给新窗格；如果已有此环境变量，把相应字段合并到原有 JSON 中。
- **Codex 按成员设置**：在启动命令中追加 `--agent-arg=--model --agent-arg=<模型名>`。

例如，启动一个使用 DeepSeek 的只读 Developer：

```bash
OPENCODE_CONFIG_CONTENT='{"agent":{"herdr-developer":{"model":"deepseek/deepseek-flash"}}}' \
  python3 .agents/skills/teamcraft/scripts/teamctl.py launch \
  --role developer --kind opencode --name deepseek_dev --direction right \
  --read-only --task '检查列表接口的分页逻辑，报告边界条件与风险。'
```

启动前用 `opencode models` 核对模型 ID。启动后检查窗格显示的角色和模型。

## 3. 验收后结束任务

CEO 核对改动范围、实际 diff 和测试结果。Herdr 的 `idle` 和 `done` 都表示成员可以接收下一条输入，交付是否通过以验收结果为准。验收通过后，用 `teamctl.py complete api_dev` 完成任务并释放 ownership。成员窗格会保留。

## 4. 遇到启动异常时复用原窗格

- 遇到 `agent_pane_busy` 或启动超时，先用 `teamctl.py status` 和 `herdr pane read <pane-id>` 检查。确认原窗格已进入 shell 提示符后，用 `teamctl.py resume ceo` 检查或恢复。
- 确认初始化提示尚未发送后，才使用 `teamctl.py resume ceo --prompt-action send`。如果提示可能已经送达，先检查投递记录。CEO 的 native Session 延迟出现时，按[身份对账流程](../README.zh-CN.md#ceo-bootstrap-身份对账)处理。
- 只有明确需要清空团队记录时才用 `reset --confirm-project "$(pwd -P)"`。它会归档旧数据库，并保留 Herdr 窗格。
