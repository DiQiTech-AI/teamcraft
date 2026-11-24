# TeamCraft 最佳实践

> 本文中的 `teamctl.py` 和 `herdr` 命令是团队运行时或维护者的操作参考。除首次启动 CEO 外，正常使用时用户只与同一个 CEO 对话，不需要把这些命令当作自己的日常步骤。

## 1. 推荐方式：只通过同一个 CEO 对话控制团队

正常使用时，用户只需要在目标项目中启动一个 CEO，然后始终回到同一个 CEO 对话表达目标、约束、验收标准和授权范围。CEO 负责把自然语言任务转成团队计划，并在当前 Herdr tab 的 sibling panes（同级窗格）中按需创建成员。**用户不需要，也不应通过 shell 命令逐个启动 developer、tester 等成员。**

后续需求、修正、验收和阻塞处理都回到这个 CEO 对话；不要另起一个 CEO，也不要绕过 CEO 直接给 worker 下命令。这样 CEO 才能维护统一的任务拆解、ownership 分配、状态和验收证据。

### 1.1 首次给 CEO 的完整 prompt 模板

下面模板中的尖括号内容由用户填写。这是一段自然语言任务说明，不是 shell 命令。

```text
你是本项目的 CEO。请负责从需求澄清、任务拆解、团队组建到独立验收和交付汇报的完整流程。

目标：
<要交付的可观察结果>

背景与当前状态：
<相关代码/文档、当前分支、未提交改动、已知问题、已尝试方案>

范围与约束：
<必须完成的事项；不得修改的范围；兼容性、依赖、时间或风险限制>

验收标准：
<可执行的检查、预期结果、需要保留的证据>

团队与拓扑授权：
- 只使用当前 Herdr tab 内的 sibling panes；除非我明确要求，不新建 tab、workspace 或 worktree。
- 按需创建完成目标所需的最少成员，不要预建闲置角色，也不要让我手动逐个启动成员。
- 为每个成员写明角色、交付物、验收标准和非重叠的 ownership；不会修改文件的成员标记为 read-only。
- 你负责独立审查实际 diff、改动文件、命令输出和测试证据，不能只接受成员自述或完成状态。
- 遇到权限请求、不可逆操作、范围冲突、证据不足或无法验证的结果时，先报告阻塞并等待我的决定。

执行与汇报：
先给出简短计划、成员拓扑和阶段划分，再开始执行。持续报告里程碑、当前状态、阻塞和下一步；完成后报告交付内容、验证证据和剩余风险。
```

### 1.2 简短追加指令示例

- `按这个计划继续；只在确有并行收益时增加成员，不要扩大 ownership。`
- `新增验收项：<具体检查>。请更新对应成员的交付物和测试，先报告是否影响现有范围。`
- `请让 tester 独立复核实际改动，不要复用实现者的结论；完成后给我证据、状态和剩余风险。`
- `暂停扩员，先报告当前阻塞、已占用 ownership 和下一步；未经我确认不要执行不可逆操作。`

### 1.3 CEO 必须自动完成的事

1. **自动拆解**：把目标拆成可验收的工作项，识别依赖、风险和不需要做的范围。
2. **按需组建**：只在确有独立工作时创建成员，并在当前 Herdr tab 的 sibling panes 中启动；不预建闲置成员。
3. **明确派工**：为每个 worker 写明交付物、验收标准、所需证据，以及互不重叠的项目相对 ownership；只读成员明确标记 read-only。
4. **独立审查**：由 CEO 检查实际改动范围、diff、命令结果和测试证据，必要时安排独立 tester 复核；worker 的 `done`/`idle` 或自述不等于验收通过。
5. **持续汇报**：向用户报告里程碑、当前状态、阻塞、需要决策的事项和下一步，不隐藏未验证的结果。
6. **统一收口**：验收通过后由 CEO 完成任务、释放 ownership，并给出交付摘要和剩余风险。

### 1.4 用户输入与运行时 CLI 的边界

| 层面 | 谁使用 | 典型内容 |
| --- | --- | --- |
| 用户 → CEO | 用户 | 自然语言的目标、约束、验收标准、团队拓扑授权和追加决策 |
| CEO/TeamCraft 运行时 | CEO | `teamctl.py`、`herdr` 等命令，用于拆解、创建成员、派工、检查状态和恢复 |

用户只需在 CEO 对话中表达目标和授权的团队拓扑，不要把命令语法当成用户工作流。不要手工复制 `teamctl.py launch ...` 逐条创建成员，也不要自行维护 worker 的 ownership 或完成状态；这些是 CEO 的运行时职责。手动逐个启动会绕过 CEO 的拆解、去重和 ownership 预留，容易产生重复窗格、范围冲突和失真的进度。

## 2. 在 Herdr 中启动 CEO

正常流程只需要启动一个 CEO。在目标项目根目录执行；需要指定右侧窗格时，加 `--direction right`。这是正常流程中唯一需要用户手动执行的启动步骤。

```bash
test "${HERDR_ENV:-}" = 1 && herdr --skill
python3 .agents/skills/teamcraft/scripts/teamctl.py launch \
  --role ceo --kind codex --name ceo \
  --task '担任项目 CEO。初始化后等待我分配任务。'
```

等启动命令成功返回，再到 CEO 窗格发送上面的完整任务 prompt。之后的目标、追加需求和验收都在这个 CEO 对话中完成。用 `teamctl.py status` 检查团队记录，用 `herdr agent get ceo` 核对窗格和运行状态；这些检查通常也由 CEO 执行，用户不必自己操作。

## 3. 由 CEO 按需创建成员

CEO 收到任务后负责拆解和派工。每个成员的任务由 CEO 在运行时创建，用户不需要在 shell 中复制启动命令。派工时 CEO 必须写清：

- **交付物**：成员要产出什么，以及允许修改的项目相对路径。
- **验收标准**：可执行的检查、预期结果和需要回报的证据。
- **ownership**：互不重叠的项目相对路径，支持精确文件和 `目录/**`；活动编辑成员之间不能重叠。
- **只读边界**：只做调研、审查或验收的成员必须标记为 read-only，不能同时声明 ownership。
- **依赖与阻塞**：先完成哪些工作、哪些决定需要用户确认，以及失败时如何报告。

成员应创建在当前 Herdr tab 的 sibling panes 中。不要预建闲置成员，也不要让多个编辑成员占用相同、父级或子级范围。ownership 是协作元数据，不是操作系统级隔离；CEO 仍需检查成员的实际改动。

### 模型与工具选择

角色与运行工具相互独立。用户可以在任务 prompt 或追加指令中直接表达偏好，例如：`developer 用 DeepSeek，tester 用本地 Qwen；不确定时先用当前默认配置并报告。` CEO 负责在创建成员时落实，并核对窗格中实际显示的角色和模型。

以下是团队运行时细节，由 CEO 或维护者使用，不是用户步骤：

- OpenCode 的角色默认模型在 `.opencode/agents/herdr-<角色>.md` 中配置。
- 单个 OpenCode 成员可由运行时通过 `--opencode-model PROVIDER/MODEL` 指定；不确定模型 ID 时先用 `opencode models` 核对。
- Codex 成员可通过运行时的 `--agent-arg=--model --agent-arg=<模型名>` 传递模型参数。

不要要求用户为每个成员手工拼装环境变量或逐条执行 `launch` 命令。

## 4. 验收后结束任务

CEO 核对改动范围、实际 diff 和测试结果。Herdr 的 `idle` 和 `done` 都表示成员可以接收下一条输入，交付是否通过以验收结果为准。用户只需在同一个 CEO 对话中确认是否接受交付；不要把 worker 的完成状态或自述当作验收通过。验收通过后，由 CEO 用 `teamctl.py complete <成员名>` 完成任务并释放 ownership。成员窗格会保留。

## 5. 遇到启动异常时复用原窗格

这些排障操作通常由 CEO 或维护者执行。用户只需在 CEO 对话中说明异常并要求排障，不要自行重启或重发任务。

- 遇到 `agent_pane_busy` 或启动超时，先用 `teamctl.py status` 和 `herdr pane read <pane-id>` 检查。确认原窗格已进入 shell 提示符后，用 `teamctl.py resume ceo` 检查或恢复。
- 确认初始化提示尚未发送后，才使用 `teamctl.py resume ceo --prompt-action send`。如果提示可能已经送达，先检查投递记录。CEO 的 native Session 延迟出现时，按[身份对账流程](../README.zh-CN.md#ceo-bootstrap-身份对账)处理。
- 只有明确需要清空团队记录时才用 `reset`；完整命令和安全顺序见第 7 节。它会归档旧数据库、安装新的空 schema，并保留 Herdr 窗格。

## 6. 收工并关闭 CEO

先在同一个 CEO 对话中交代收尾范围。可以直接发送：

```text
准备收工。请停止派发新任务，确认现有成员的任务状态、未提交改动和交付记录。收尾已验收的任务并释放对应 ownership。对于未完成的任务，记录分支、改动路径、阻塞和下次继续的位置。如果还有成员在写入文件或有操作结果待确认，先告诉我，等这些操作处理完再通知我可以退出 CEO。
```

1. 等 CEO 给出收尾摘要，确认需要保留的文件已写入磁盘。根据项目的交付要求核对提交、推送和验收状态；不要用退出进程代替任务验收或交付记录。
2. 如果只结束本次工作而保留团队环境，可以让 CEO 安排已完成的成员逐一正常退出。等成员回到输入提示符后，在各自的 Codex 界面输入 `/quit`，确认 Codex 已退出并回到该 Herdr 窗格中的 shell；其他 harness 使用其正常退出方式，也应先确认回到 shell。
3. 成员处理完后，等 CEO 完成最后的收尾摘要并回到输入提示符。在 CEO 的 Codex 界面输入 `/quit`，确认 CEO 也正常退出并回到 shell。不要直接关闭仍在写文件、运行命令或等待确认的成员/CEO 窗格。
4. 如果只关闭某个已经回到 shell 的窗格，可以在该 shell 输入 `exit`；如果还要检查或清除 TeamCraft 记录，先保留至少一个 Herdr shell 和 Herdr 服务，按第 7 节完成后再关窗格。

CEO 用于结清成员任务的 `teamctl.py complete <成员名>` 会释放 ownership，成员进程和窗格需要另行退出。下次继续未完成的工作时，保留 TeamCraft 记录供对账。

## 7. 退出整个开发环境

完全退出时按“业务收尾 → 正常退出 Agent → 检查/按需清除记录 → 关闭窗格 → 最后停止服务”的顺序操作：

1. 先按上一节通知 CEO 收工。请 CEO 核对本项目涉及的成员窗格和后台进程，完成验收、交付记录和 ownership 释放；未完成的工作则记录改动路径、阻塞和续接位置。遇到仍在写入文件、运行测试或等待确认的成员，先处理该成员的状态，不要直接关窗格。
2. 逐一正常退出成员，最后退出 CEO。Codex 都应在各自输入提示符执行 `/quit` 并回到 Herdr 窗格中的 shell；不要把关闭窗格或停止 Herdr 服务当作正常退出。
3. 此时先不要关闭全部窗格或 Herdr 服务。选择一个已经回到 shell 的 Herdr 窗格，进入本项目根目录，在 Herdr 仍运行时检查记录：

   ```bash
   cd /absolute/path/to/project
   PROJECT_ROOT="$(pwd -P)"
   python3 .agents/skills/teamcraft/scripts/teamctl.py status --cwd "$PROJECT_ROOT"
   ```

4. 默认保留 TeamCraft 数据库，便于下次对账和续接。**只有明确选择清除本项目全部 TeamCraft 运行记录时**，才在同一个项目根目录 Herdr shell 中执行：

   ```bash
   python3 .agents/skills/teamcraft/scripts/teamctl.py reset \
     --cwd "$PROJECT_ROOT" \
     --confirm-project "$PROJECT_ROOT"
   ```

   `--confirm-project` 必须与 `--cwd` 解析后的项目根完全一致。`reset` 会拒绝清理仍在 Herdr 中存活且记录为非终态的 TeamCraft 成员；遇到拒绝时，应返回对应窗格完成收尾并正常退出，再重新检查，而不是绕过保护。成功时，它先创建经过校验的 SQLite 在线备份，并把旧数据库及 WAL 相关工件保留到唯一的 `.teamcraft/archive-*` 归档目录，然后原子安装一个新初始化的空 schema。归档是恢复副本；`reset` 不修改项目源码，也不关闭 Herdr 窗格。
5. 状态检查以及可选的 `reset` 完成后，再关闭已经回到 shell 的成员和 CEO 窗格；本项目独占一个 Herdr 标签页时，也可以关闭该标签页。
6. 如果还要停止当前 Herdr 服务及它管理的全部窗格进程，把它放在最后。先确认其他标签页和项目也可以停止，再从该会话对应的终端执行 `herdr server stop`；该命令可能影响多个项目。

### 暂时退出 Herdr 并保留后台进程

在 Herdr 界面按默认快捷键 **Ctrl+B**，松开后再按 **Q**。这会分离当前 Herdr 客户端，后台服务、窗格和正在运行的 CEO、成员及其他进程会继续运行。暂时离开时可以直接分离。

回来后，在终端运行 `herdr` 重新连接默认会话。命名会话使用 `herdr session attach <会话名>`。完整停机使用上一节的 `herdr server stop` 流程。 [Herdr 分离与重连文档](https://herdr.dev/tui/docs/persistence-remote/)

### CEO 窗格已关闭后的续接

优先使用上一节的分离方式保留 CEO 窗格和对话。若 CEO 窗格已经关闭、Herdr 服务仍在运行，在同一 Herdr 会话的项目 shell 中执行：

```bash
PYTHONPATH="$PWD" python3 .agents/skills/teamcraft/scripts/teamctl.py resume ceo \
  --from-pane "$HERDR_PANE_ID"
```

该命令会检查记录的窗格和身份；原窗格不可用时可尝试从指定父窗格重建。默认只检查，不会重发已记录的 CEO 任务。重建后的 CEO 身份和会话绑定仍须核对，确认无误后再继续派工。

如需恢复原来的 Codex 对话，在新窗格进入项目目录后执行 `codex resume <原 CEO 的 Codex 会话 ID>`。这会恢复 Codex 对话，TeamCraft 对新窗格和新进程的绑定仍需要对账。

Herdr 服务已经停止时，数据库依然可用于对账。重启服务后可能出现 Herdr 会话身份不匹配，应先检查状态并处理身份对账；为了续接工作不需要执行 `reset`。

已保存的源码、Git 提交和 TeamCraft 数据库可用于下次继续工作。收工时保留数据库，按需对账后再重新派工。
