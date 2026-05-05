# AgentEnd Lite 设计文档

## 1. 设计目标

AgentEnd Lite 是一个 Python 单机 Agent 工作流运行时。它吸收既有三份设计文档中的目标契约、状态机、工具编排、记忆、workflow 和审计思想，但不采用完整 AgentOS 的多服务、多数据库、多 Agent 和前端 Console 设计。

最终架构目标：

```text
CLI / Telegram
    ↓
Channel Adapter
    ↓
Conversation Service
    ↓
Single Agent Runtime
    ↓
Workflow Orchestrator
    ↓
Tool Registry
    ├─ Built-in Tools
    └─ MCP Client Module
    ↓
SQLite + local artifacts + editable agent.md
```

## 2. 核心原则

- 单 Agent：系统内只有一个 Agent Runtime，不引入多 Agent 协作。
- 多 workflow：复杂能力通过 workflow 编排表达，而不是通过多个 Agent 表达。
- 本地优先：SQLite 和 artifacts 目录承载全部本地状态。
- 入口复用：CLI 和 Telegram 只做通道适配，业务逻辑复用同一套服务。
- MCP 单向接入：系统只作为 MCP client 接入外部 MCP server。
- Profile 外置：Agent 行为说明放在本地 `agent.md`，允许用户编辑。
- 可审计但不重治理：记录关键事件和调用链，不实现完整 ops-gate。

## 3. 模块划分

```text
agentend/
  cli.py
  telegram_bot.py
  config.py
  agent.md

  core/
    app.py
    conversation.py
    runtime.py
    contract.py
    state.py
    llm_router.py
    workflow_runner.py
    workflow_registry.py
    tool_registry.py
    evaluator.py
    events.py

  mcp/
    client.py
    manager.py
    adapter.py
    schemas.py

  tools/
    base.py
    file.py
    http.py
    python_exec.py
    memory.py

  db/
    models.py
    session.py
    migrations/

  workflows/
    definitions/

  data/
    agentend.sqlite
    artifacts/
    logs/
```

## 4. 入口层设计

### 4.1 CLI

CLI 使用 Typer 实现。CLI 不直接调用 LLM 或工具，而是调用应用服务。

主要职责：

- 初始化项目。
- 管理配置。
- 启动本地会话。
- 执行 workflow。
- 管理 LLM。
- 管理 MCP server。
- 查询 run、日志和数据库状态。

### 4.2 Telegram

Telegram 使用 `python-telegram-bot` long polling。首版不要求 webhook，避免部署复杂度。

Telegram Adapter 负责：

- 将 Telegram user/chat 映射为本地 conversation。
- 将 `/run <workflow_id>` 转换为 workflow run。
- 将普通文本消息交给 Conversation Service。
- 将 Agent 输出拆分为 Telegram 消息。
- 将用户取消、状态查询、帮助命令转换为服务调用。

Telegram 不持有独立状态，所有状态写入 SQLite。

## 5. Conversation Service

Conversation Service 是 CLI 和 Telegram 的统一入口。

职责：

- 创建和恢复 conversation。
- 写入 user/assistant/system messages。
- 选择默认 workflow 或显式 workflow。
- 调用 Single Agent Runtime。
- 处理 human_input 节点的等待和恢复。
- 将输出转换为 channel response。

核心接口：

```python
class ConversationService:
    async def handle_message(self, channel: str, external_user_id: str, text: str) -> ConversationResponse:
        ...

    async def run_workflow(self, conversation_id: str, workflow_id: str, input_text: str) -> RunResult:
        ...

    async def cancel_run(self, conversation_id: str) -> None:
        ...

    async def get_status(self, conversation_id: str) -> ConversationStatus:
        ...
```

## 6. Single Agent Runtime

Single Agent Runtime 负责单次 run 的主控。它不是多 Agent 调度器，只负责：

- 读取 `agent.md`。
- 生成轻量 Goal Contract。
- 选择或接收 workflow。
- 调用 Workflow Runner。
- 在必要时调用 Evaluator。
- 写入 run 状态和 event log。

轻量状态机：

```text
created
  ↓
contracting
  ↓
planning
  ↓
running
  ↓
waiting_input ── 用户补充 ──> running
  ↓
verifying
  ↓
completed

异常终态：
failed
cancelled
```

每次状态转换必须写入 `event_log`。

## 7. agent.md 设计

`agent.md` 是本地可编辑 Agent Profile。

默认模板：

```markdown
# Agent Profile

你是一个本地单机工作流 Agent。

## 工作方式
- 默认使用中文。
- 优先使用已有 workflow。
- 需要更多信息时先澄清。
- 调用工具前简短说明目的。

## 能力范围
- 可以调用已注册内置工具。
- 可以调用已注册 MCP 工具。
- 可以读写本地 SQLite 记忆。
- 可以生成本地产物文件。

## 输出偏好
- 结论先行。
- 给出可执行命令或下一步。
- 不暴露内部链式推理。
```

run 启动时读取文件内容并计算 hash：

```text
agent_profile_path = <config.agent_profile_path>
agent_profile_hash = sha256(agent.md bytes)
```

这两个字段写入 `runs` 表。

## 8. LLM Router

LLM Router 屏蔽 provider 差异。

配置示例：

```toml
[llm]
provider = "openai"
model = "gpt-4.1"
temperature = 0.2
max_tokens = 4096

[llm.providers.openai]
api_key_env = "OPENAI_API_KEY"
base_url = "https://api.openai.com/v1"
```

接口：

```python
class LLMRouter:
    async def complete(self, messages: list[Message], options: LLMOptions) -> LLMResponse:
        ...

    async def test(self) -> LLMTestResult:
        ...
```

每次 run 记录 provider、model、temperature、max_tokens。

## 9. Workflow 设计

Workflow 使用 YAML 定义，用 Pydantic 做 schema 校验。

示例：

```yaml
id: research_report
name: 研究报告
description: 搜索资料并生成带来源的 Markdown 报告

input:
  required:
    - topic

nodes:
  - id: understand
    type: llm
    prompt: "将用户输入整理为研究目标、约束和输出格式。"

  - id: search
    type: tool
    tool: mcp.search.web_search
    depends_on: [understand]

  - id: write
    type: llm
    prompt: "根据资料生成 Markdown 报告，保留来源。"
    depends_on: [search]

  - id: save
    type: tool
    tool: file.write_text
    depends_on: [write]

  - id: final
    type: final
    depends_on: [save]
```

节点类型：

| 类型 | 说明 |
| --- | --- |
| `llm` | 构造 prompt，调用 LLM。 |
| `tool` | 调用 Tool Registry 中的工具。 |
| `condition` | 根据表达式选择分支。 |
| `parallel` | 并行执行子节点。 |
| `human_input` | 暂停 run，等待用户补充。 |
| `workflow_call` | 调用另一个 workflow。 |
| `final` | 生成最终响应。 |

Workflow Runner 必须在每个节点前后写入 `run_steps`。

## 10. Tool Registry

Tool Registry 统一管理内置工具和 MCP 工具。

本地工具接口：

```python
class Tool:
    name: str
    description: str
    input_schema: dict

    async def call(self, input_data: dict, context: ToolContext) -> ToolResult:
        ...
```

工具命名：

```text
file.read_text
file.write_text
http.request
python.exec
memory.search
memory.write
mcp.<server_name>.<tool_name>
```

Tool Registry 必须支持：

- 注册工具。
- 禁用工具。
- 查询工具 schema。
- 调用工具。
- 记录 tool call。

## 11. MCP Client 模块

MCP 模块包含四层：

```text
MCP config
    ↓
MCP Manager
    ↓
MCP Client
    ↓
MCP Tool Adapter
    ↓
Tool Registry
```

### 11.1 配置

```toml
[mcp.servers.filesystem]
transport = "stdio"
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "/home/user/work"]
enabled = true

[mcp.servers.search]
transport = "http"
url = "http://127.0.0.1:3333/mcp"
enabled = true
```

### 11.2 注册流程

```text
agentend mcp refresh filesystem
    ↓
connect server
    ↓
list tools
    ↓
normalize tool schemas
    ↓
persist mcp_tools
    ↓
register mcp.filesystem.<tool_name>
```

### 11.3 调用流程

```text
workflow node
    ↓
Tool Registry
    ↓
MCP Tool Adapter
    ↓
MCP Client
    ↓
external MCP server
    ↓
ToolResult
```

每次 MCP tool 调用必须记录：

- run_id。
- step_id。
- server_name。
- tool_name。
- input_json。
- output_json。
- status。
- error。
- latency_ms。

## 12. SQLite 数据模型

核心表：

| 表 | 用途 |
| --- | --- |
| `conversations` | 会话。 |
| `messages` | 会话消息。 |
| `runs` | 每次 Agent 运行。 |
| `run_steps` | workflow 节点执行记录。 |
| `workflow_defs` | workflow 定义和版本。 |
| `tool_calls` | 内置工具调用记录。 |
| `mcp_servers` | MCP server 配置摘要和状态。 |
| `mcp_tools` | MCP 工具 schema 和启用状态。 |
| `mcp_tool_calls` | MCP 工具调用记录。 |
| `memories` | 本地记忆。 |
| `artifacts` | 本地产物索引。 |
| `event_log` | 关键事件日志。 |

`runs` 关键字段：

```text
id
conversation_id
workflow_id
status
agent_profile_path
agent_profile_hash
llm_provider
llm_model
input_json
result_json
created_at
updated_at
```

## 13. Artifacts

文件产物统一存入：

```text
data/artifacts/<run_id>/
```

SQLite `artifacts` 表保存：

- run_id。
- path。
- kind。
- mime。
- size_bytes。
- sha256。
- metadata_json。

## 14. Linux 部署设计

### 14.1 单用户安装

```bash
git clone <repo>
cd agentend
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
agentend init
agentend llm set --provider openai --model gpt-4.1
agentend llm test
agentend chat
```

### 14.2 Telegram 常驻

```bash
sudo mkdir -p /opt/agentend
sudo chown "$USER":"$USER" /opt/agentend
git clone <repo> /opt/agentend
cd /opt/agentend
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
agentend init --home /opt/agentend
agentend telegram serve
```

### 14.3 systemd

```ini
[Unit]
Description=AgentEnd Telegram Bot
After=network.target

[Service]
WorkingDirectory=/opt/agentend
EnvironmentFile=/opt/agentend/.env
ExecStart=/opt/agentend/.venv/bin/agentend telegram serve
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

启用：

```bash
sudo cp deploy/agentend.service /etc/systemd/system/agentend.service
sudo systemctl daemon-reload
sudo systemctl enable --now agentend
sudo journalctl -u agentend -f
```

## 15. 错误处理

| 场景 | 处理 |
| --- | --- |
| LLM 配置缺失 | `llm test` 返回明确缺失项，不启动 run。 |
| Telegram token 缺失 | `telegram serve` 失败并提示环境变量。 |
| workflow schema 错误 | validate 输出具体节点和字段。 |
| MCP server 连接失败 | 标记 server unhealthy，不删除已有工具。 |
| MCP tool 调用失败 | step failed，记录错误，可由 workflow 决定是否继续。 |
| 进程中断 | 已持久化 step 保留，run 可 resume 或标记 failed。 |

## 16. 测试策略

首版自动化测试重点：

- `agentend init` 幂等性。
- LLM 配置读写和缺失 key 错误。
- Workflow YAML schema 校验。
- Workflow Runner 顺序、条件、human_input、workflow_call。
- Tool Registry 注册和调用。
- MCP 工具发现后的本地注册。
- Telegram command handler 到 Conversation Service 的映射。
- SQLite run/step/tool/event 持久化。

