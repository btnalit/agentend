# AgentEnd Lite 任务文档

## 1. 任务目标

按垂直切片实现一个 Python 单机 Agent 工作流运行时。每个切片都必须形成可运行、可测试的用户可见能力，避免先堆横向基础设施却没有可演示路径。

## 2. 任务标记

- `AFK`：可由工程实现自动推进。
- `HITL`：需要用户提供密钥、外部 token、部署环境或产品取舍。
- `Blocked`：依赖前置任务。
- `Done`：已完成。

## 3. 总体依赖

```text
T1 初始化骨架
  ↓
T2 CLI + SQLite 基础会话
  ↓
T3 LLM 配置与 agent.md
  ↓
T4 Workflow Runner
  ↓
T5 Tool Registry + 内置工具
  ↓
T6 MCP 接入
  ↓
T7 Telegram 入口
  ↓
T8 Linux 部署
  ↓
T9 审计、恢复和验收补齐
```

## 4. 任务列表

### T1 项目初始化骨架 `AFK`

目标：建立 Python 包、配置加载、目录结构和测试入口。

范围：

- 创建 Python package。
- 引入 Typer、Pydantic、SQLAlchemy、pytest。
- 建立 `agentend init` 的最小命令。
- 创建默认数据目录。

验收：

```bash
agentend --help
agentend init
pytest
```

完成标准：

- 命令可执行。
- `data/` 目录可生成。
- 测试框架可运行。

### T2 CLI + SQLite 基础会话 `AFK`

目标：CLI 可以启动本地会话，并将 messages/runs 写入 SQLite。

范围：

- SQLite session 和 models。
- `conversations`、`messages`、`runs`、`event_log`。
- `agentend chat`。
- `agentend runs list/show`。

验收：

```bash
agentend db init
agentend chat
agentend runs list
agentend runs show <run_id>
```

测试映射：

- CLI chat 创建 conversation。
- user message 和 assistant message 可持久化。
- run 状态可查询。

### T3 LLM 配置与本地 agent.md `AFK`

目标：用户可通过 CLI 管理模型配置和 Agent Profile。

范围：

- `config.toml` 和 `.env` 加载。
- LLM Router。
- `agentend llm set/current/list/test`。
- 默认 `agent.md`。
- `agentend agent show/edit/reload`。
- run 记录 `agent_profile_hash`、provider、model。

验收：

```bash
agentend llm set --provider openai --model gpt-4.1
agentend llm current
agentend llm test
agentend agent show
```

HITL：

- 用户需要提供可用 LLM API key。

测试映射：

- 配置写入后可读取。
- 缺少 API key 时错误清晰。
- 修改 `agent.md` 后新 run hash 变化。

### T4 Workflow Runner 垂直闭环 `AFK`

目标：YAML workflow 可以加载、校验、执行，并记录 step。

范围：

- Workflow schema。
- Workflow Registry。
- Workflow Runner。
- 节点类型：`llm`、`final`。
- `agentend workflows list/show/validate/run`。

验收：

```bash
agentend workflows validate
agentend workflows run simple_chat
```

测试映射：

- 无效 YAML 返回字段级错误。
- `llm -> final` workflow 可执行。
- `run_steps` 写入每个节点状态。

### T5 Tool Registry 和内置工具 `AFK`

目标：workflow 可以调用内置工具并保存产物。

范围：

- Tool base interface。
- Tool Registry。
- 内置工具：`file.read_text`、`file.write_text`、`http.request`、`python.exec`、`memory.search`、`memory.write`。
- 节点类型：`tool`、`condition`、`parallel`、`workflow_call`、`human_input`。
- artifacts store。

验收：

```bash
agentend workflows run write_file_demo
agentend runs show <run_id>
```

测试映射：

- workflow 可调用 `file.write_text` 写入 artifact。
- tool call 被记录。
- human_input 可暂停并恢复。
- workflow_call 可调用子 workflow。

### T6 MCP 单向接入 `AFK`

目标：系统可作为 MCP client 接入外部 MCP server，发现工具并自动注册。

范围：

- MCP server 配置模型。
- MCP Manager。
- MCP Client。
- MCP Tool Adapter。
- `mcp_servers`、`mcp_tools`、`mcp_tool_calls`。
- CLI：`mcp add/list/refresh/tools/test/remove`。
- Tool Registry 注册 `mcp.<server>.<tool>`。

验收：

```bash
agentend mcp add filesystem --stdio "npx -y @modelcontextprotocol/server-filesystem /tmp"
agentend mcp refresh filesystem
agentend mcp tools filesystem
agentend mcp test filesystem
agentend workflows run mcp_demo
```

HITL：

- 用户需要提供目标 MCP server 的启动命令或 URL。

测试映射：

- mock MCP server 可返回工具列表。
- refresh 后工具写入 SQLite。
- workflow 可调用注册后的 MCP 工具。
- MCP server 失败时状态标记为 unhealthy。

### T7 Telegram 会话入口 `AFK`

目标：Telegram Bot 复用 Conversation Service 与 Agent Runtime。

范围：

- `python-telegram-bot` 集成。
- `/start`、`/new`、`/workflows`、`/run`、`/status`、`/cancel`、`/agent`、`/help`。
- Telegram user/chat 到 conversation 映射。
- 长消息拆分发送。
- `agentend telegram serve`。

验收：

```bash
agentend telegram serve
```

HITL：

- 用户需要提供 `TELEGRAM_BOT_TOKEN`。

测试映射：

- command handler 调用 Conversation Service。
- 普通消息进入默认会话。
- `/run <workflow_id>` 启动 workflow。

### T8 Linux 初始化与部署 `AFK`

目标：Linux 上可便捷安装、初始化和 systemd 常驻运行。

范围：

- `deploy/agentend.service`。
- `scripts/install-linux.sh`。
- `agentend init --home <path>`。
- `.env.example`。
- `agentend db backup`。
- README 部署段落。

验收：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
agentend init --home /opt/agentend
agentend db backup
```

手工部署验收：

```bash
sudo systemctl enable --now agentend
sudo journalctl -u agentend -f
```

### T9 审计、恢复和验收补齐 `AFK`

目标：完成可观测、恢复和发布前验证。

范围：

- event log 标准化。
- run resume/cancel。
- failed run 错误详情。
- logs tail。
- 测试覆盖补齐。
- 文档更新。

验收：

```bash
agentend runs resume <run_id>
agentend runs cancel <run_id>
agentend logs tail
pytest
```

测试映射：

- 进程中断后的 run 可以查询。
- waiting_input 状态可恢复。
- failed 状态记录错误堆栈摘要。

## 5. 首版完成定义

首版完成必须同时满足：

- CLI 初始化、chat、run workflow、查询 run 可用。
- LLM 可配置、可测试。
- `agent.md` 可编辑，run 记录 hash。
- SQLite 保存 conversation、message、run、step、tool call、event log。
- 至少两个 workflow 可运行。
- MCP server 可接入并自动注册工具。
- Telegram 可启动并触发 workflow。
- Linux systemd 部署文档和 service 文件可用。
- 自动化测试覆盖核心路径。

