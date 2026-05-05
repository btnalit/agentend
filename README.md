# AgentEnd Lite

AgentEnd Lite 是一个 Python 单机 Agent 工作流运行时，提供 CLI 和 Telegram 两个会话入口，使用 SQLite 保存本地状态，支持本地可编辑 `agent.md`、YAML workflow 编排、内置工具和单向 MCP client 接入。

## 功能范围

- Python 后端，无前端。
- 单 Agent Runtime，多 workflow 编排。
- CLI 入口：初始化、会话、LLM 配置、workflow、MCP、run 查询、日志和数据库备份。
- Telegram 入口：long polling bot，复用同一套 Conversation Service。
- 本地 SQLite：保存 conversation、message、run、step、tool call、MCP tool call、artifact、memory 和 event log。
- 本地 `agent.md`：可编辑 Agent Profile，每次 run 记录 profile hash。
- MCP 单向接入：作为 MCP client 连接外部 MCP server，刷新后自动注册工具为 `mcp.<server>.<tool>`。
- Linux 部署：venv 初始化、systemd 常驻、SQLite 备份。

## 快速开始

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'

agentend init
agentend llm set --provider fake --model fake-model
agentend workflows run simple_chat --input "hello"
agentend chat --message "你好"
```

真实 LLM 使用 OpenAI-compatible provider：

```bash
cp .env.example .env
# 编辑 .env，写入 OPENAI_API_KEY
agentend llm set --provider openai --model gpt-4.1
agentend llm test
```

## 常用 CLI

```bash
agentend init
agentend status
agentend chat
agentend chat --message "hello"

agentend llm list
agentend llm set --provider openai --model gpt-4.1
agentend llm current
agentend llm test

agentend agent show
agentend agent edit
agentend agent reload

agentend workflows list
agentend workflows show simple_chat
agentend workflows validate
agentend workflows run simple_chat --input "hello"

agentend runs list
agentend runs show <run_id>
agentend runs resume <run_id> --message "补充信息"
agentend runs cancel <run_id>

agentend logs tail
agentend db backup --output ./agentend.sqlite.bak
```

## MCP 接入

添加 stdio MCP server：

```bash
agentend mcp add filesystem --stdio "npx -y @modelcontextprotocol/server-filesystem /tmp"
agentend mcp refresh filesystem
agentend mcp tools filesystem
```

测试内置 mock MCP server：

```bash
agentend mcp add demo --stdio "mock:echo"
agentend mcp refresh demo
agentend mcp tools demo
```

workflow 中调用 MCP tool：

```yaml
id: mcp_demo
name: MCP Demo
nodes:
  - id: echo
    type: tool
    tool: mcp.demo.echo
    input:
      text: "MCP says {input}"
  - id: final
    type: final
    depends_on: [echo]
```

运行：

```bash
agentend workflows run mcp_demo --input "hello"
```

## Telegram

配置 token：

```bash
cp .env.example .env
# 编辑 .env，写入 TELEGRAM_BOT_TOKEN
agentend telegram serve
```

支持命令：

```text
/start
/new
/workflows
/run <workflow_id> <input>
/status
/cancel
/agent
/help
```

## Linux 部署

推荐部署到 `/opt/agentend`：

```bash
sudo mkdir -p /opt/agentend
sudo chown "$USER":"$USER" /opt/agentend
git clone https://github.com/btnalit/agentend.git /opt/agentend
cd /opt/agentend

python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
agentend init --home /opt/agentend
cp .env.example .env
```

编辑 `/opt/agentend/.env`：

```env
OPENAI_API_KEY=
TELEGRAM_BOT_TOKEN=
```

也可以使用安装脚本：

```bash
bash scripts/install-linux.sh /opt/agentend
```

安装 systemd service：

```bash
sudo cp deploy/agentend.service /etc/systemd/system/agentend.service
sudo systemctl daemon-reload
sudo systemctl enable --now agentend
sudo systemctl status agentend
sudo journalctl -u agentend -f
```

数据库备份：

```bash
mkdir -p /opt/agentend/backups
agentend db backup --home /opt/agentend --output /opt/agentend/backups/agentend.sqlite
```

升级：

```bash
cd /opt/agentend
git pull
. .venv/bin/activate
python -m pip install -e '.[dev]'
agentend db init --home /opt/agentend
sudo systemctl restart agentend
```

## 测试

```bash
python -m pytest -q
```

当前测试覆盖：

- 初始化幂等。
- CLI 会话和 SQLite 持久化。
- LLM 配置和 `agent.md` hash。
- Workflow 校验和执行。
- 内置工具、artifact 和 workflow_call。
- MCP 工具自动注册和调用。
- Telegram router。
- Linux 部署产物和数据库备份。
- run resume/cancel、failed run 和 event log。

