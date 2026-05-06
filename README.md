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

`llm test` 会向配置的 OpenAI-compatible `/chat/completions` 端点发送最小请求；workflow 中的 LLM step 也会使用同一 provider。自定义兼容端点可编辑 `config.toml` 中的 `[llm.providers.openai].base_url` 和 `api_key_env`。

## 常用 CLI

```bash
agentend init
agentend status
agentend doctor
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
agentend runs export <run_id> --output ./exports

agentend logs tail
agentend db backup --output ./agentend.sqlite.bak
```

## 运行边界和审计

- 文件工具默认只允许访问 AgentEnd home 内的相对路径；`file.write_text` 和 browser screenshot/click/type 产物写入 `data/artifacts/<run_id>/`。
- `fs.delete recursive=true` 不能删除 AgentEnd home root；绝对路径和 `..` 越界路径会被拒绝。
- ToolRegistry 会为每次工具调用记录 Tool Contract snapshot、Action Policy decision、tool call、event log 和必要的 artifact。
- `http.request` 按 method 动态分类副作用：GET/HEAD/OPTIONS 是 `network_read`，POST/PUT/PATCH/DELETE 是 `network_write`。
- Result Cache 只缓存网络读结果；Replay 默认阻断本地写入/执行和网络写入，Scheduler 默认阻断本地执行、网络写入和外部写入。
- `sources list/show` 和 `runs export` 会输出 web、file、browser 来源证据；secret 和 AgentEnd home 路径会尽量脱敏。

## Eval 和发布检查

```bash
agentend eval list
agentend eval run runtime-hardening
agentend eval report <eval_run_id>

python -m compileall -q src tests
python -m pytest tests -q
git diff --check
```

`runtime-hardening` suite 使用本地 fixture 覆盖真实 LLM 调用链、Telegram + MCP、HTTP 副作用分类、路径边界、内置 Skill 工具调用、model route/cost usage 和 evidence export。

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
- OpenAI-compatible LLM fixture、LLM 配置和 `agent.md` hash。
- Workflow 校验、final/condition 语义和执行。
- 内置工具、Action Policy、artifact、evidence 和 workflow_call。
- MCP 工具自动注册和调用。
- Telegram router 和 MCP async bridge。
- Linux 部署产物和数据库备份。
- run resume/cancel、failed run 和 event log。
- runtime-hardening eval、tools-smoke、skills-smoke 和 context eval。

