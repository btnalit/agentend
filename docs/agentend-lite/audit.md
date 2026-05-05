# AgentEnd Lite 审计文档

## 1. 审计范围

本审计面向当前需求和设计阶段，不涉及代码实现审计。审计对象包括：

- 既有三份设计文档的取舍。
- 当前 AgentEnd Lite 需求边界。
- Python 单机架构。
- CLI 和 Telegram 双入口。
- SQLite 本地持久化。
- 本地 `agent.md`。
- MCP 单向接入。
- Linux 初始化和部署。
- 后续实施任务的风险与验证要求。

## 2. 来源材料审计

### 2.1 `ai-agent-architecture.html`

可吸收内容：

- 目标层、规划层、工具层、记忆层的分层思想。
- Perceive、Think、Plan、Act、Observe、Evaluate 的执行闭环。
- 工具编排和动态注册思想。
- 工作记忆、情节记忆、语义记忆、程序记忆的记忆分层。

裁剪内容：

- 工具自增殖。
- 完整自进化。
- 多类外部数据库。
- 重型硬边界。

原因：

AgentEnd Lite 是单机本地运行时，首版重点是可部署、可配置、可调用 workflow，不承担完整通用 AgentOS 的治理目标。

### 2.2 `complete-ai-agent-design.html`

可吸收内容：

- 职责清晰。
- Skills 是资产。
- 工具是能力基础。
- 唯一路径和可观测思想。

裁剪内容：

- self-evolution-governor。
- ops-gate。
- 用户批准后提案执行链。
- 10 个通用工具一次性全部实现。

原因：

当前用户已明确“不需要设计文档的硬边界设计”，因此完整治理链不进入首版。

### 2.3 `aiagent.md`

可吸收内容：

- 目标契约。
- 状态机执行。
- Evaluator。
- 审计事件。
- 数据模型。

裁剪内容：

- 11 层 AgentOS。
- 多 Agent。
- 前端 Console。
- Postgres、Redis、ClickHouse、OpenTelemetry 等生产级外部组件。

原因：

这些内容适合生产级平台，不适合当前 Python 单机 MVP。

## 3. 当前需求等级判断

当前任务是文档产出，不修改运行时代码，按项目规范属于 T1 文档任务。

后续真正实现时会变为 T2 行为改动，原因：

- 新增 CLI 行为。
- 新增 Telegram 行为。
- 新增 LLM 调用链。
- 新增 SQLite 持久化。
- 新增 MCP 工具接入。
- 新增 workflow 执行状态流。

如果后续加入权限、不可逆操作、外部写入审批或发布门，则升级为 T3。

## 4. 关键架构风险

### R1 MCP server 不可信

风险：

MCP server 是外部能力提供方，可能返回不稳定 schema、执行失败、暴露过宽工具或产生副作用。

当前设计处理：

- MCP 只作为 client 单向接入。
- MCP tool 自动注册但必须记录 schema。
- MCP server 有 enabled/status。
- MCP 调用写入 `mcp_tool_calls`。
- workflow 显式引用 MCP 工具。

剩余风险：

首版不做完整 ops-gate，因此用户应只接入可信 MCP server。

后续建议：

- 增加 MCP server allowlist。
- 增加 per-tool enabled 开关。
- 增加只读/写入标签。

### R2 agent.md 可编辑导致行为漂移

风险：

用户编辑 `agent.md` 后，Agent 行为会变化，历史 run 难以复盘。

当前设计处理：

- 每次 run 记录 `agent_profile_path`。
- 每次 run 记录 `agent_profile_hash`。

剩余风险：

只记录 hash 不记录全文时，若文件后来变更，无法完全还原旧 profile。

后续建议：

- 可选记录 `agent_profile_snapshot`。
- 或将 profile snapshot 存入 artifacts。

### R3 LLM 配置和密钥管理

风险：

API key 泄露或配置混乱会导致请求失败和成本不可控。

当前设计处理：

- API key 从环境变量读取。
- SQLite 不保存明文 key。
- `llm test` 提供最小验证。
- run 记录 provider/model。

剩余风险：

首版不做预算上限和成本统计。

后续建议：

- 增加 per-run max_tokens。
- 增加每日调用次数软上限。

### R4 Telegram 入口状态一致性

风险：

Telegram 多消息并发可能导致同一 conversation 内 run 状态错乱。

当前设计处理：

- Telegram 复用 Conversation Service。
- run 状态写入 SQLite。
- `/status` 和 `/cancel` 走统一服务。

剩余风险：

SQLite 并发写入能力有限。

后续建议：

- 对单 conversation 加应用级锁。
- 长任务运行时拒绝同会话重复启动 run，除非用户取消。

### R5 Workflow 递归和长循环

风险：

`workflow_call` 可能形成递归；`condition` 和 `human_input` 可能导致长时间卡住。

当前设计处理：

- run 和 step 持久化。
- 状态机包含 `waiting_input`。

剩余风险：

需要实现阶段增加最大 workflow depth、最大 step 数和超时配置。

后续建议：

- `max_steps_per_run`。
- `max_workflow_call_depth`。
- `run_timeout_seconds`。

### R6 Python exec 工具风险

风险：

`python.exec` 可执行代码，可能读取本地文件、消耗资源或产生副作用。

当前设计处理：

- 首版只定位为受限数据处理工具。

剩余风险：

如果未做沙箱，风险较高。

后续建议：

- 首版默认禁用 `python.exec`，由配置启用。
- 限制执行目录为 run artifact 目录。
- 设置超时。
- 不向其注入敏感环境变量。

## 5. 数据边界审计

本地数据统一位于配置 home 下：

```text
<home>/
  config.toml
  .env
  agent.md
  data/
    agentend.sqlite
    artifacts/
    logs/
```

边界规则：

- SQLite 保存结构化状态。
- artifacts 保存大文本和文件。
- `.env` 保存密钥，不提交版本库。
- `agent.md` 可编辑，属于运行配置。
- MCP server 配置可保存启动命令和 URL，但不保存明文密钥。

## 6. 审计事件要求

必须写入 `event_log` 的事件：

| 事件 | 触发 |
| --- | --- |
| `conversation.created` | 新建会话。 |
| `message.received` | 收到 CLI/Telegram 用户消息。 |
| `run.created` | 创建 run。 |
| `run.state_changed` | run 状态变化。 |
| `workflow.loaded` | workflow 加载。 |
| `step.started` | workflow 节点开始。 |
| `step.completed` | workflow 节点完成。 |
| `step.failed` | workflow 节点失败。 |
| `tool.called` | 内置工具调用。 |
| `mcp.server_refreshed` | MCP server 刷新。 |
| `mcp.tool_registered` | MCP tool 注册。 |
| `mcp.tool_called` | MCP tool 调用。 |
| `artifact.created` | 产物创建。 |
| `run.completed` | run 成功完成。 |
| `run.failed` | run 失败。 |
| `run.cancelled` | run 被取消。 |

## 7. 测试与验收审计要求

后续实现阶段必须满足：

- 行为改动必须有自动化测试。
- MCP 必须使用 mock server 或 fake client 做工具发现和调用测试。
- Telegram handler 必须测试命令到 Conversation Service 的映射。
- SQLite 必须测试 run、step、tool call、event log 落库。
- `agent.md` hash 必须测试。
- Linux 部署脚本至少做 shellcheck 或最小命令校验。

## 8. 当前文档产物审计结论

当前四份文档覆盖：

- 需求边界。
- 架构设计。
- 任务拆分。
- 风险审计。

未进入当前阶段的内容：

- 代码实现。
- 数据库迁移文件。
- CLI 命令实际行为。
- Telegram Bot 实际运行。
- MCP SDK 版本绑定。

这些内容应在实施阶段按 `taskboard.md` 逐个垂直切片推进。

