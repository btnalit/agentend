# AgentEnd Lite 需求文档

## 1. 背景

当前项目已有三份 AI Agent 设计材料：

- `ai-agent-architecture.html`：强调通用 Agent 的目标层、规划层、工具层、记忆层、进化层和硬边界。
- `complete-ai-agent-design.html`：强调职责清晰、唯一信任边界、Skills 主线、工具集和自主进化。
- `aiagent.md`：在前两份基础上补充生产级视角，包括目标契约、状态机、权限、评估和审计。

本项目不采用完整 AgentOS 的重型方案，而是裁剪为单机可落地版本：Python 后端、本地 CLI、Telegram 会话入口、SQLite 本地数据库、单 Agent 架构、多 workflow 编排、单向 MCP 接入。

## 2. 目标

AgentEnd Lite 要提供一个可在 Linux 单机上便捷部署的本地 Agent 工作流运行时。用户可以通过 CLI 或 Telegram 与同一个单 Agent 会话系统交互；Agent 根据本地可编辑的 `agent.md`、LLM 配置、workflow 定义和已注册工具完成任务；所有会话、运行状态、工具调用、MCP 注册信息、记忆和产物索引统一存入 SQLite。

## 3. 范围

### 3.1 必须包含

- Python 后端运行时。
- CLI 会话入口。
- Telegram Bot 会话入口。
- 本地 SQLite 数据库。
- 本地 artifacts 文件产物目录。
- 单 Agent Runtime。
- 多 workflow 编排能力。
- 本地可编辑的 `agent.md` Agent Profile。
- LLM provider/model 配置、查看、切换和测试。
- MCP client 模块，单向接入外部 MCP server。
- MCP 工具发现后自动注册为本地可调用工具。
- Linux 初始化、便捷部署和 systemd 常驻运行方案。
- 基础运行审计：run、step、tool call、MCP tool call、event log。

### 3.2 明确不包含

- 前端 Console。
- 多 Agent / Sub-Agent 架构。
- 对外暴露 MCP server。
- 分布式任务队列。
- Postgres、Redis、Neo4j、pgvector 等外部数据库依赖。
- 完整 ops-gate / 硬边界治理系统。
- 自动生成工具并直接上线。
- A/B、灰度、自进化治理链。
- 企业级权限、多租户和组织隔离。

## 4. 用户角色

| 角色 | 诉求 |
| --- | --- |
| 本地使用者 | 通过 CLI 快速运行 Agent、配置模型、执行 workflow、查看历史运行。 |
| Telegram 使用者 | 通过 Telegram 与同一个 Agent 会话，触发 workflow，查看状态。 |
| 运维部署者 | 在 Linux 上初始化配置、设置密钥、启动 Telegram 常驻服务、查看日志和备份数据库。 |
| Workflow 编写者 | 用 YAML 定义可复用 workflow，让单 Agent 可以按步骤执行任务。 |

## 5. 功能需求

### 5.1 初始化

系统必须提供 `agentend init` 命令，完成以下动作：

- 创建配置文件 `config.toml`。
- 创建 `.env.example`。
- 创建默认 `agent.md`。
- 创建 SQLite 数据库文件。
- 创建 `data/artifacts/` 和 `data/logs/`。
- 写入示例 workflow。
- 初始化数据库表结构。

初始化必须可重复执行，不应覆盖用户已编辑的 `agent.md`、`.env` 或已有数据库，除非用户显式传入覆盖参数。

### 5.2 单 Agent Profile

系统必须使用本地 `agent.md` 作为单 Agent 的行为说明文件。

要求：

- `agent.md` 可由用户直接编辑。
- 每次 run 启动时读取当前 `agent.md`。
- 每次 run 记录 `agent_profile_path` 和 `agent_profile_hash`。
- CLI 可查看、编辑、重载 `agent.md`。
- Telegram 可查看当前 Agent Profile 摘要。

### 5.3 CLI

CLI 必须覆盖基础运维和日常使用。

必备命令：

```bash
agentend init
agentend status
agentend chat
agentend run <workflow_id>

agentend llm list
agentend llm set --provider <provider> --model <model>
agentend llm current
agentend llm test

agentend agent show
agentend agent edit
agentend agent reload

agentend workflows list
agentend workflows show <workflow_id>
agentend workflows validate
agentend workflows run <workflow_id>

agentend mcp add <name>
agentend mcp list
agentend mcp refresh <name>
agentend mcp tools <name>
agentend mcp test <name>
agentend mcp remove <name>

agentend runs list
agentend runs show <run_id>
agentend runs resume <run_id>
agentend runs cancel <run_id>

agentend db init
agentend db migrate
agentend db backup

agentend logs tail
agentend telegram serve
```

### 5.4 Telegram

Telegram Bot 必须复用 CLI 相同的 Conversation Service、Agent Runtime、Workflow Runner 和数据库。

必备命令：

```text
/start
/new
/workflows
/run <workflow_id>
/status
/cancel
/agent
/help
```

普通文本消息必须进入默认会话流程。Telegram 入口不允许实现独立业务逻辑，只负责消息适配、用户标识映射和响应发送。

### 5.5 LLM 配置

系统必须支持通过配置文件和 CLI 管理 LLM。

要求：

- 支持至少一个 OpenAI-compatible provider。
- provider、model、temperature、max_tokens 可配置。
- API key 从环境变量读取，不写入 SQLite 明文。
- `agentend llm test` 必须能发起最小请求并返回成功或错误详情。
- 每次 run 记录使用的 provider、model 和关键生成参数。

### 5.6 Workflow 编排

系统必须支持多个 YAML workflow。

必备节点类型：

| 类型 | 用途 |
| --- | --- |
| `llm` | 调用 LLM 完成理解、生成、总结、判断。 |
| `tool` | 调用内置工具或 MCP 工具。 |
| `condition` | 根据上游输出做分支。 |
| `parallel` | 并行执行无依赖子节点。 |
| `human_input` | 等待 CLI 或 Telegram 用户补充信息。 |
| `workflow_call` | 调用另一个 workflow。 |
| `final` | 形成最终输出。 |

workflow 必须经过 schema 校验，失败时给出具体字段和节点位置。

### 5.7 MCP 单向接入

系统必须作为 MCP client 接入外部 MCP server。

要求：

- 支持 stdio MCP server。
- 支持 HTTP/SSE 或 streamable HTTP 形式的 MCP server，具体协议以实现阶段所选 Python MCP SDK 为准。
- 可在配置文件中声明 MCP server。
- 可通过 CLI 添加、查看、刷新、测试和删除 MCP server。
- 连接 MCP server 后自动发现 tools。
- 自动将 MCP tool 注册为本地 Tool Registry 中的工具。
- 本地工具名格式为 `mcp.<server_name>.<tool_name>`。
- MCP tool 的 input schema 必须保存到 SQLite。
- workflow 可以直接调用已注册 MCP 工具。

MCP 只作为能力接入层，不作为安全边界。MCP server 的可用性、工具列表、输入 schema 和调用结果都必须被记录。

### 5.8 内置工具

MVP 内置工具：

| 工具 | 用途 |
| --- | --- |
| `file.read_text` | 读取本地文本文件。 |
| `file.write_text` | 写入本地产物文件。 |
| `http.request` | 发起 HTTP 请求。 |
| `python.exec` | 受限执行 Python 片段，用于数据处理。 |
| `memory.search` | 查询本地记忆。 |
| `memory.write` | 写入本地记忆。 |

后续可以添加搜索、文档解析和浏览器工具，但不属于首版必需项。

### 5.9 数据持久化

系统必须统一使用 SQLite 存储结构化数据。文件产物必须存入本地 artifacts 目录，SQLite 只保存路径、摘要和元数据。

必须记录：

- conversation。
- message。
- run。
- run step。
- workflow definition。
- tool call。
- MCP server。
- MCP tool。
- MCP tool call。
- memory。
- artifact。
- event log。

### 5.10 Linux 部署

系统必须提供 Linux 便捷部署路径：

- venv 安装。
- `agentend init` 初始化。
- `.env` 管理密钥。
- `agentend telegram serve` 运行 Telegram。
- systemd service 常驻。
- `journalctl` 查看日志。
- `agentend db backup` 备份 SQLite。

## 6. 非功能需求

| 类别 | 要求 |
| --- | --- |
| 简单性 | 单进程优先，首版不引入外部服务依赖。 |
| 可恢复 | run 和 step 状态必须持久化，异常后可查看失败位置。 |
| 可观测 | 关键动作写入 event log。 |
| 可配置 | LLM、Telegram、MCP、数据目录均可配置。 |
| 可迁移 | 数据集中在配置目录，便于备份和迁移。 |
| 可测试 | CLI、workflow runner、MCP 注册、Telegram handler 可自动化测试。 |

## 7. 成功标准

- `agentend init` 后可以直接进入 `agentend chat`。
- CLI 可以设置、查看、测试 LLM。
- Telegram 可以启动并复用同一套会话服务。
- 至少两个 YAML workflow 可被加载、校验和运行。
- MCP server 接入后，其 tools 自动注册为本地工具。
- workflow 可以调用内置工具和 MCP 工具。
- SQLite 中可以查到每次 run 的 profile hash、LLM 配置、step、tool call、event log。
- Linux 上可以通过 systemd 常驻 Telegram Bot。

