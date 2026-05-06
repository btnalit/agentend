# AgentEnd Action Layer 需求文档

## 1. 背景

AgentEnd Lite 首版已经具备 Python 单机运行时、CLI、Telegram、SQLite、workflow、内置基础工具、MCP 单向接入和基础审计。下一阶段目标不是继续增强聊天能力，而是补齐 Agent 的行动层，让系统具备更完整的“手脚”和可复用 Skill 资产。

本阶段命名为 **AgentEnd Action Layer**。

## 2. 目标

Action Layer 要让 AgentEnd 从 workflow runner 进化为可行动的本地 Agent：

```text
Goal Analyzer
  ↓
Skill Library / Skill Market
  ↓
Workflow + Replanner
  ↓
Tool Registry
  ↓
真实工具行动
  ↓
Episode Logger
```

核心结果：

- 工具集从少量基础工具扩展为覆盖搜索、文件、Shell、浏览器、数据库、通信、视觉、发现和生成的行动工具层。
- Skills 从单纯 workflow 文件升级为可安装、可启用、可运行、可验证的 Skill Bundle。
- 默认关联精选 Skills 市场，并内置若干通用高收益 Skill。
- `python.exec` 从进程内 `exec` 升级为 `local_subprocess` 执行后端。
- 引入 Goal Analyzer、Replanner、Episode Logger，让任务能分析、失败能重规划、结果能复盘沉淀。
- 补齐 `doctor`、workspace index、Git 工具、run replay/export、task inbox、capability map 等低成本基础能力，让系统能诊断环境、理解工作区、复现执行和沉淀能力。
- 引入 Context Runtime、Action Policy、HITL Clarification、Agent Eval、Model Routing、Checkpoint、Evidence 和 Storage Governance，让长任务能控上下文、控风险、控成本、可恢复、可评测、可追溯。

## 3. 范围

### 3.1 必须包含

- Tool Registry 增加工具元数据、工具列表、工具详情和工具测试 CLI。
- Tool Contract 标准化，统一 input schema、output schema、超时、副作用、可重试和审计字段。
- Action Policy 执行策略层。
- HITL Clarification Protocol。
- Agent Eval Harness。
- Model Routing 和 Cost Budget。
- Checkpoint / Resume Snapshot。
- Context Runtime：Context Ledger、Context Budgeter、Tool Result Compactor、Memory Store、FTS5 Retrieval、Context Policy、Context Preview。
- `agentend doctor` 环境诊断。
- Workspace Indexer 和 Project Profile。
- File System 工具扩展。
- Shell Runner 工具。
- Git Tool Suite。
- `python.exec` `local_subprocess` 后端。
- Search + Fetch 工具。
- Skill Library。
- Skill Market，默认关联精选市场。
- 默认内置通用高收益 Skills。
- Goal Analyzer。
- Replanner。
- Episode Logger。
- Artifact Manager。
- Run Replay / Run Export。
- Task Inbox 和本地 Scheduler。
- Capability Map。
- Episode to Skill 草稿生成。
- File Inbox Watcher。
- CLI stdin/stdout/clipboard 集成。
- Secrets Manager 和日志脱敏。
- Result Cache。
- Error Taxonomy。
- Browser Agent。
- DB Writer。
- IM Sender。
- Vision Analyzer。
- Tool Discoverer。
- Tool Generator。
- Extension Lifecycle。
- Source / Evidence Manager。
- Retention / Cleanup / Backup Policy。
- 对上述模块的 SQLite 结构化记录和审计事件。

### 3.2 不包含

- 多 Agent 架构。
- 前端 Console。
- 外部队列和分布式调度。
- 完整 ops-gate、审批流或权限治理系统。
- 自动上线不经验证的工具或 Skill。
- 远程云沙箱、Docker 沙箱、Firecracker 或 E2B 沙箱。本阶段 `python.exec` 只做 `local_subprocess`。

## 4. 工具优先级

### 4.1 P0 必须优先实现

| 工具/模块 | 价值 |
| --- | --- |
| Tool CLI + Metadata + Contract | 让用户和 Agent 能知道当前有什么工具、输入/输出 schema 是什么、超时和副作用是什么、能否测试。 |
| Action Policy | 基于 Tool Contract 判断只读、写本地、执行本地命令、外部写入，所有工具统一接入。 |
| Secrets Manager + Redaction | 统一 token 检查、引用和日志脱敏，必须早于 run export、IM、web、tool generator。 |
| Error Taxonomy | 把常见失败分类，让 Replanner、Eval、Replay 不再解析脆弱的 stderr 文本。 |
| Agent Eval Harness | 用任务级回归集验证智能体有没有退化，后续改 Goal、Context、Tool、Skill 都有反馈回路。 |
| Model Routing + Cost Budget | 按步骤选择模型并限制 token、调用次数和成本，避免长任务不可控。 |
| Context Runtime | 管理上下文预算、压缩工具结果、检索记忆、构造 prompt pack，是长任务可靠性的核心。 |
| Checkpoint / Resume Snapshot | 每个稳定步骤保存恢复点，中断后从最近检查点继续，而不是重跑整个任务。 |
| `agentend doctor` | 一键检查 Python、依赖、DB、LLM、MCP、Telegram、Browser、local_subprocess，降低部署和扩展失败率。 |
| Workspace Indexer + Project Profile | 让 Agent 在行动前理解当前项目结构、说明文档、约束和常用命令。 |
| File System | Agent 本地行动基础，支持 list/read/write/copy/move/delete/stat/glob。 |
| Shell Runner | 安装、测试、git、系统任务、脚本执行的核心工具。 |
| Git Tool Suite | 代码型任务的基础手脚，支持 status/diff/show/commit/branch/log。 |
| `python.exec local_subprocess` | 数据处理、临时代码、分析任务的核心执行后端。 |
| Search + Fetch | 实时信息获取，避免 Agent 退化成离线聊天。 |
| Skill Library | 把 workflow 资产化，支持安装、启用、运行和验证。 |
| Episode Logger | 把每次任务从 event log 汇总成可复盘 episode。 |
| Run Replay / Export + Artifact Manager | 支持任务复跑、导出日志和产物，提升调试、交付和审计能力。 |
| Capability Map | 自动汇总 tools、skills、MCP 能力，作为 Goal Analyzer 的能力地图。 |
| HITL Clarification | 信息缺失、目标歧义、高风险动作时统一请求用户输入，并能恢复 workflow。 |
| Goal Analyzer | 把用户输入转成目标、约束、候选 skill/tool。 |
| Replanner | 工具失败、信息不足、结果不满足时生成下一步计划。 |
| Extension Lifecycle | 统一管理 tool、skill、MCP、generated draft 的 draft/installed/enabled/disabled/quarantined/removed 状态。 |

### 4.2 P1 高收益

| 工具/模块 | 价值 |
| --- | --- |
| Browser Agent | 真实网页操作、截图、表单、动态页面提取。 |
| DB Writer | SQLite 优先，后续扩展 Postgres/MySQL。 |
| IM Sender | Telegram 发送消息/文件，后续扩展 Email/Slack。 |
| Vision Analyzer | 图片、截图、扫描件、图表理解。 |
| Tool Discoverer | 统一发现内置工具、MCP 工具、Skill 暴露能力。 |
| Task Inbox + Scheduler | 支持本地任务队列、延迟和周期执行，避免 Agent 只能响应即时聊天。 |
| Episode to Skill | 将成功 episode 生成 skill 草稿，形成可复用能力沉淀。 |
| Source / Evidence Manager | 为搜索、抓取、报告、审计建立来源证据链，避免结果不可追溯。 |
| File Inbox Watcher | 监听本地 inbox 目录，文件进入后自动触发 workflow。 |
| CLI stdin/stdout/clipboard | 支持管道输入、标准输出和剪贴板，便于 Linux 自动化和桌面工作流。 |
| Result Cache | 缓存搜索、抓取、HTTP、LLM 中间结果，减少重复调用。 |
| Retention / Cleanup / Backup | 管理 artifacts、sandboxes、cache、memory、exports 的增长和备份恢复。 |

### 4.3 P2 后置

| 工具/模块 | 价值 |
| --- | --- |
| Tool Generator | 自动生成工具，收益高但风险和复杂度也高，应在 Tool/Skill/Eval 稳定后实现。 |

## 5. Skills 模块需求

### 5.1 Skill Bundle

Skill 不只是 prompt，也不只是 workflow。Skill 是可复用工作流资产：

```text
Skill = manifest + workflow + docs + examples + evals
```

目录结构：

```text
skills/
  research.report/
    skill.yaml
    workflow.yaml
    README.md
    examples/
    evals/
```

`skill.yaml` 必须包含：

- id。
- version。
- description。
- triggers。
- workflow。
- required_tools。
- required_mcp。
- input_schema。
- output_schema。
- enabled。
- source。

### 5.2 Skill Market

系统必须支持三类 Skill 来源：

| 来源 | 说明 |
| --- | --- |
| Builtin Skills | 项目内置，默认可用。 |
| Curated Market | 默认关联的精选市场，用户可刷新和安装。 |
| User Markets | 用户添加的 Git 仓库或本地目录。 |

配置示例：

```toml
[skills]
auto_refresh = true
default_market = "agentend-curated"

[skills.markets.agentend-curated]
type = "git"
url = "https://github.com/btnalit/agentend-skills"
enabled = true

[skills.markets.local]
type = "directory"
path = "./skills"
enabled = true
```

### 5.3 默认内置 Skills

首批默认集成：

| Skill | 目标 |
| --- | --- |
| `research.report` | 搜索、抓取、总结、生成带来源报告。 |
| `file.workspace_ops` | 本地文件整理、读取、生成文档。 |
| `code.local_task` | 本地代码任务：读文件、改代码、跑测试。 |
| `shell.automation` | Shell 自动化任务。 |
| `data.quick_analysis` | CSV/JSON/SQLite 数据分析。 |
| `mcp.tool_setup` | 辅助接入 MCP server，并生成 workflow 示例。 |

`browser.web_task` 和 `telegram.assistant_ops` 在 Browser Agent、IM Sender 对应工具完成后再作为默认 Skill 启用。

### 5.4 Skill CLI

必须提供：

```bash
agentend skills markets list
agentend skills markets add <name> --git <url>
agentend skills markets add <name> --directory <path>
agentend skills refresh
agentend skills list
agentend skills show <skill_id>
agentend skills install <skill_id>
agentend skills enable <skill_id>
agentend skills disable <skill_id>
agentend skills validate
agentend skills run <skill_id> --input "..."
```

## 6. python.exec local_subprocess 需求

当前 `python.exec` 是进程内 `exec`。本阶段必须升级为可配置后端：

```toml
[tools.python_exec]
backend = "local_subprocess"
timeout_seconds = 120
workspace_root = "./data/sandboxes"
```

执行流程：

```text
create run workspace
  ↓
write script.py
  ↓
run subprocess: python script.py
  ↓
collect stdout/stderr/exit_code
  ↓
collect generated files as artifacts
  ↓
record tool_calls and event_log
```

要求：

- 每次调用使用独立 workspace。
- 保存 `script.py`。
- 记录 stdout、stderr、exit_code、duration。
- 超时后终止子进程。
- 生成文件写入 artifacts。
- 不引入 Docker、E2B、Firecracker 或复杂权限限制。

## 7. 新工具需求

### 7.1 File System

工具名：

- `fs.list`
- `fs.glob`
- `fs.stat`
- `fs.read_text`
- `fs.write_text`
- `fs.copy`
- `fs.move`
- `fs.delete`
- `fs.mkdir`

### 7.2 Shell Runner

工具名：

- `shell.run`

要求：

- 支持 cwd。
- 支持 env 覆盖。
- 支持 timeout。
- 记录 stdout/stderr/exit_code。
- 可被 workflow 调用。

### 7.3 Search + Fetch

工具名：

- `web.search`
- `web.fetch`

要求：

- `web.search` 支持 query、limit。
- `web.fetch` 支持 URL，输出 title、text、links、metadata。
- 首版可使用可配置 provider 或 MCP server 适配。

### 7.4 Browser Agent

工具名：

- `browser.open`
- `browser.click`
- `browser.type`
- `browser.screenshot`
- `browser.extract`

首版建议使用 Playwright。

若本机尚未安装 Playwright 浏览器 executable，Browser Agent 必须返回明确 backend/fallback 信息；不得把静态 fallback 伪装成真实浏览器截图或交互。

### 7.5 DB Writer

工具名：

- `db.query`
- `db.execute`
- `db.write_rows`

首版只要求 SQLite。

### 7.6 IM Sender

工具名：

- `im.telegram.send_message`
- `im.telegram.send_file`

首版只要求 Telegram。

真实发送必须依赖 `TELEGRAM_BOT_TOKEN`；自动化测试必须使用 dry-run 或 fake client，避免产生外部可见副作用。

### 7.7 Vision Analyzer

工具名：

- `vision.describe`
- `vision.ocr`
- `vision.extract_chart`

依赖多模态 LLM provider。

首版可提供 fake provider 作为稳定回归基线，真实 OCR 和图表解析需后续接入多模态 provider。

### 7.8 Tool Discoverer

工具名：

- `tools.discover`
- `tools.describe`

覆盖内置工具、MCP 工具和 Skill 暴露工具。

### 7.9 Goal Analyzer

工具名：

- `goal.analyze`

输出：

- goal。
- constraints。
- candidate_skills。
- candidate_tools。
- missing_inputs。
- risk_notes。

### 7.10 Replanner

工具名：

- `plan.replan`

CLI：

```bash
agentend plan replan --failed-step web.search --error "provider missing"
```

输入：

- goal。
- current_workflow。
- failed_step。
- observations。

输出：

- retry_same_step。
- alternative_tool。
- ask_user。
- switch_skill。
- fail_with_reason。

### 7.11 Tool Generator

工具名：

- `tools.generate`

要求：

- 只能生成 proposal 或本地 draft。
- 必须附带 test workflow 或 eval case。
- 不自动注册为 stable。
- 当前实现生成 `data/generated_tools/<tool_name>/tool.yaml`、`implementation.py`、`test_workflow.yaml`，写入 `generated_tools`，并注册 extension 状态为 `draft`。
- Capability Map 可以展示 draft 能力，但 Tool Registry 不能执行未启用 draft。

## 8. Episode Logger 需求

Episode 是比 event log 更高层的任务复盘单元。

必须从 run 汇总：

- 用户目标。
- 使用的 workflow/skill。
- 调用的 tools。
- 关键 artifacts。
- 失败步骤和错误。
- 最终结果。
- 可复用经验摘要。

CLI：

```bash
agentend episodes list
agentend episodes show <episode_id>
agentend episodes summarize <run_id>
```

SQLite 表：

- `episodes`
- `episode_tools`
- `episode_artifacts`

## 9. 低成本高收益基础能力需求

### 9.1 AgentEnd Doctor

必须提供：

```bash
agentend doctor
agentend doctor --json
```

检查项：

- Python 版本和依赖导入。
- AgentEnd home、SQLite、artifacts、sandboxes 可读写。
- LLM provider 配置和当前模型。
- Telegram token 是否存在。
- MCP server 配置和最近 refresh 状态。
- Browser backend 是否可用。
- `local_subprocess` 是否能启动并返回 stdout。
- Skill market 配置是否可读取。

输出必须分为 `ok`、`warning`、`error`，并给出可执行修复建议。

### 9.2 Tool Contract

每个工具必须有统一 contract：

- name。
- source。
- category。
- description。
- input_schema。
- output_schema。
- timeout_seconds。
- side_effect：`none`、`local_read`、`local_write`、`local_execute`、`network_read`、`network_write`。
- retryable。
- requires_secrets。
- artifact_policy。
- audit_events。

Goal Analyzer、Tool Discoverer、Capability Map、Replanner 必须只读取 contract，不直接依赖工具实现细节。

### 9.3 Workspace Indexer 和 Project Profile

必须提供：

```bash
agentend workspace index
agentend workspace summary
agentend project profile show
agentend project profile edit
```

要求：

- 优先读取 `AGENTS.md`、`README.md`、`CONTEXT.md`、`docs/`、`pyproject.toml`、`package.json`、测试目录和 workflow 目录。
- 生成轻量索引，不保存大文件全文。
- 输出项目类型、主要入口、常用命令、测试命令、约束文档和风险提示。
- Project Profile 可本地编辑，用于记录该项目的固定约束、常用命令和验收方式。

### 9.4 Git Tool Suite

工具名：

- `git.status`
- `git.diff`
- `git.show`
- `git.log`
- `git.branch`
- `git.commit`

要求：

- 默认只读工具优先。
- `git.commit` 必须显式传入 message 和 file list。
- 所有 git 工具必须记录 cwd、command summary、exit_code、stdout/stderr 摘要。
- 不提供 `reset --hard`、强制推送或 destructive checkout 工具。

### 9.5 Artifact Manager、Run Replay 和 Run Export

必须提供：

```bash
agentend artifacts list --run <run_id>
agentend artifacts show <artifact_id>
agentend runs replay <run_id>
agentend runs export <run_id> --output ./exports
```

要求：

- 每次 run 的脚本、截图、报告、下载文件、日志摘要统一归档。
- replay 使用原始 input、workflow、skill 版本和工具 contract 快照。
- export 输出 run metadata、steps、tool calls、tool contract snapshots、artifacts、episode 和 redacted config。
- replay 默认不自动执行外部可见副作用工具，除非用户显式允许。
- 本轮实现的 replay 以新 run 执行原 workflow/input，`channel=replay` 且 `run_mode=replay`；首版不复刻历史工具输出，只复跑可安全执行的 workflow，但必须导出源 run 的工具 contract snapshot，作为后续历史工具输出复用和 contract drift 检测基础。

### 9.6 Task Inbox 和本地 Scheduler

必须提供：

```bash
agentend tasks add "整理 inbox 文件"
agentend tasks list
agentend tasks run <task_id>
agentend tasks resume <task_id>
agentend schedule add --workflow <workflow_id> --cron "0 9 * * *"
agentend schedule run-now <schedule_id>
agentend schedule tick --now "2026-05-06T09:00:00+08:00"
```

要求：

- Task 保存目标、输入、状态、关联 run、失败原因和下一步建议。
- Scheduler 只做单机本地触发，不引入外部队列。
- 周期任务必须记录每次触发的 run_id。
- 首版 Scheduler 不常驻后台；由 `run-now` 或 `tick` 触发，`tick` 支持五段 cron 的 `*`、`*/n`、数字和逗号列表。
- Scheduler 模式运行 workflow 时必须使用 `run_mode=scheduler`，默认阻断 `network_write` 和 `external_write` 工具。

### 9.7 Capability Map

必须提供：

```bash
agentend capabilities refresh
agentend capabilities list
agentend capabilities query "搜索网页并写报告"
```

要求：

- 汇总内置工具、MCP 工具、enabled skills、generated draft tools。
- 记录能力名称、用途、输入摘要、输出摘要、依赖 secret、风险等级和示例。
- Goal Analyzer 默认使用 capability map 做候选召回。

### 9.8 Episode to Skill

必须提供：

```bash
agentend episodes promote <episode_id> --skill-id <skill_id>
```

要求：

- 只生成 skill draft，不自动 enable。
- draft 必须包含 skill.yaml、workflow.yaml、README.md、examples 和 eval skeleton。
- 生成时必须标记来源 episode、使用工具和未解决风险。
- 失败 episode 默认不能 promote；draft 不自动 enable。

### 9.9 File Inbox、stdin/stdout 和 clipboard

必须支持：

- `agentend inbox watch --workflow <workflow_id>`。
- `agentend inbox watch --workflow <workflow_id> --once`。
- `agentend workflows run <id> --stdin --output json|text`。
- `agentend clipboard read` 和 `agentend clipboard write`。

要求：

- inbox watcher 只监听本地配置目录。
- stdin/stdout 必须适配 Linux 管道。
- clipboard 功能不可作为 workflow 默认输出，必须显式调用。
- `--output json` 的稳定字段为 `status`、`run_id`、`output` 或 `error`。
- clipboard 在无系统 clipboard 时必须返回明确错误；测试和无头环境可用 `AGENTEND_CLIPBOARD_FILE` 文件后端。

### 9.10 Secrets、Result Cache 和 Error Taxonomy

Secrets Manager 要求：

- 统一读取 `.env` 和环境变量。
- CLI 可检查 secret 是否存在，但不打印原值。
- event log、episode、export 默认脱敏。

Result Cache 要求：

- 缓存 `web.search`、`web.fetch`、`http.request` 等网络读结果。
- 支持 TTL。
- Replanner 可识别 cache stale。
- 缓存入口必须在 ToolRegistry 统一处理，避免各网络读工具分散实现。
- cache key 必须由 tool name、标准化 input 和配置 hash 组成。
- cache hit/miss 必须记录审计事件。
- TTL 过期后必须重新执行工具并刷新缓存。

Error Taxonomy 要求：

- 至少区分 `missing_config`、`timeout`、`tool_not_found`、`schema_error`、`permission_error`、`network_error`、`external_side_effect_blocked`、`unknown`。
- 工具失败必须尽量写入结构化 error code。

## 10. 上下文、策略、评测和治理需求

### 10.1 Context Runtime

Context Runtime 负责构造每次 LLM 调用的上下文包，不允许各模块自行拼接 prompt。

必须包含：

- Context Ledger：记录每次 LLM 调用使用了哪些上下文块。
- Context Budgeter：按 token 预算选择 system、agent.md、project profile、goal、workflow state、recent messages、memory、retrieval、tool summary。
- Tool Result Compactor：工具原始输出进 artifact/DB，上下文只放摘要。
- Memory Store：本地 SQLite + markdown 记忆存储。
- FTS5 Retrieval：首版使用 SQLite FTS5，不强制引入向量库。
- Context Policy：workflow/skill 可声明上下文策略。
- Context Preview：CLI 可预览某次调用会塞入哪些上下文。

CLI：

```bash
agentend context preview --workflow <id> --input "..."
agentend context ledger show <llm_call_id>
agentend context compact --run <run_id>
agentend memory list --scope project
agentend memory search "部署命令"
agentend memory write --scope project --content "..."
agentend memory edit <memory_id>
agentend memory forget <memory_id>
```

### 10.2 Memory 分层

记忆必须按作用域分层：

| Scope | 用途 |
| --- | --- |
| `session` | 当前会话短期状态。 |
| `task` | 当前任务目标、计划、未完成事项。 |
| `project` | 项目约束、常用命令、架构结论。 |
| `episode` | 历史任务经验、失败原因、成功路径。 |
| `skill` | 某个 skill 的使用经验。 |
| `user` | 用户偏好和长期约束。 |

每条 memory 必须包含：

- scope。
- content。
- source。
- confidence。
- ttl。
- tags。
- created_by_run_id。
- evidence_artifact_id。
- last_used_at。

检索要求：

- 首版使用 SQLite FTS5；FTS5 不可用时允许回退 contains 检索。
- scope、tag、confidence、ttl 过滤必须在 FTS5 和 fallback 下保持一致。
- 命中后必须更新 `last_used_at` 并写入 `memory_retrievals`。

写入策略：

- manual source 可写 project/user 长期记忆。
- web/tool/untrusted source 默认不能直接写入 project/user，只能写 task/episode/session。
- token-like 内容必须脱敏后保存。

### 10.3 Context Policy

workflow 和 skill 可以声明：

```yaml
context:
  recent_turns: 6
  include_project_profile: true
  memory_scopes: [project, task, episode]
  retrieve_top_k: 5
  tool_result_policy: summarize
  max_context_tokens: 32000
```

默认策略必须保守：

- 不把完整大文件、完整网页、完整 shell 输出直接塞入上下文。
- 大工具结果先写 artifact，再生成摘要。
- 长期 memory 只能按需检索。
- 高风险或低置信 memory 不能作为强约束注入。
- policy 合并顺序为 global -> project -> workflow -> skill -> step。
- 下层只能缩小预算或收紧安全策略，不能关闭全局脱敏。

### 10.4 Action Policy

Action Policy 是工具执行前的统一策略层。

工具 side effect 等级：

- `none`
- `local_read`
- `local_write`
- `local_execute`
- `network_read`
- `network_write`
- `external_write`

要求：

- 所有工具执行前必须生成 `action_policy_decision`。
- 默认允许只读工具。
- `local_write`、`local_execute`、`network_write`、`external_write` 必须记录理由。
- Replay 和 Scheduler 默认阻断外部可见副作用。
- 不做完整审批系统，但为 HITL 和后续审批保留统一入口。

### 10.5 HITL Clarification Protocol

必须统一三类用户输入请求：

- `missing_input`：缺少必要参数、token、路径、chat_id。
- `ambiguous_goal`：目标多解，继续执行会偏离用户意图。
- `high_risk_action`：即将执行不可逆或外部可见动作。

CLI 和 Telegram 必须复用同一张 clarification request 表。

要求：

- request 包含 question、reason、choices、free_text_allowed、resume_token、expires_at。
- 用户回答后 workflow 从对应 checkpoint 恢复。
- Replanner 可生成 clarification request，但不能绕过 Action Policy。
- 本轮实现先以 `human_input` workflow 节点作为统一 pause 入口；缺参、歧义、高风险请求都落入同一张 `clarification_requests` 表。
- `runs resume <run_id> --answer "..."` 必须完成 pending request，并在同一个 run 上继续执行后续节点。
- Telegram 会优先把普通消息路由给最近的 pending clarification，复用同一个 runner resume 入口。

### 10.6 Agent Eval Harness

必须提供任务级评测：

```bash
agentend eval list
agentend eval run smoke
agentend eval run context-smoke
agentend eval run --skill research.report
agentend eval report <eval_run_id>
```

Eval case 格式：

```yaml
id: code.local_task.smoke
input: "列出项目测试命令"
allowed_tools: [fs.read_text, workspace.summary]
expected:
  contains:
    - pytest
  artifacts:
    - type: text
```

要求：

- 支持 fake LLM、fake tool、fixture workspace。
- 覆盖 Goal Analyzer、Replanner、Context、Tool、Skill 的端到端行为。
- 每个默认 skill 至少一个 smoke eval。
- Eval report 必须输出 suite、case、assertion、status、关联 run_id 和关键审计对象，失败时能直接定位 context ledger、tool call、policy decision 或 artifact。
- `context-smoke` 必须覆盖 lost-context、tool-output-bloat、memory-retrieval、policy-merge 四类上下文回归。
- `smoke` 必须纳入基础 context-smoke 结果，避免上下文管理退化只在单测中被发现。

### 10.7 Model Routing 和 Cost Budget

必须支持：

```bash
agentend models routes list
agentend models routes set goal_analyze --provider openai --model gpt-5.4-mini
agentend budget show
agentend budget set --workflow research.report --max-llm-calls 20 --max-tokens 200000
```

默认路由：

- goal analyze：便宜模型。
- context summarize / compact：便宜模型。
- replanner：强模型或当前主模型。
- code / reasoning：强模型。
- vision：支持多模态的模型。

要求：

- workflow 可设置最大 LLM 调用次数、最大 token、最大估算成本。
- 超预算时必须产生结构化错误，交给 Replanner 或 HITL。

### 10.8 Checkpoint / Resume Snapshot

每个 workflow step 完成后必须可选保存 checkpoint：

- workflow id/version。
- current step。
- input。
- state。
- artifacts。
- context summary。
- tool contract snapshot。
- pending clarification。
- policy decisions。

CLI：

```bash
agentend checkpoints list --run <run_id>
agentend runs resume <run_id> --checkpoint <checkpoint_id>
```

要求：

- 长任务、Scheduler、Replay、HITL 都必须使用 checkpoint。
- checkpoint 不保存明文 secret。
- `runs resume <run_id> --checkpoint <checkpoint_id>` 从指定 completed step 后继续执行，不重复该 checkpoint 之前的节点。
- checkpoint resume 使用当前 workflow 定义继续后续节点，适配用户修正 workflow 或输入后的恢复场景。

### 10.9 Extension Lifecycle

所有扩展统一生命周期：

```text
draft -> installed -> enabled -> disabled -> quarantined -> removed
```

覆盖对象：

- Skill。
- MCP server。
- Generated Tool。
- User Market。

要求：

- 记录 source、version、hash、installed_at、last_validated_at、status。
- validate 失败进入 `quarantined`。
- 支持 rollback 到上一个 validated 版本。

### 10.10 Source / Evidence Manager

必须为外部信息和报告建立证据链：

- URL。
- 本地文件路径。
- title。
- fetched_at。
- content_hash。
- quote 摘要。
- used_by_run_id。
- used_by_artifact_id。

CLI：

```bash
agentend sources list --run <run_id>
agentend sources show <source_id>
```

要求：

- `research.report` 默认输出来源列表。
- `web.fetch` 和 Browser extract 必须记录 source。
- Run Export 必须包含 evidence manifest。

### 10.11 Retention / Cleanup / Backup

必须提供：

```bash
agentend storage usage
agentend storage cleanup --older-than 30d
agentend storage backup --output ./backups
agentend storage restore <backup_path>
```

要求：

- 统计 SQLite、artifacts、sandboxes、cache、exports、memory、skill drafts。
- cleanup 必须支持 dry-run。
- 默认不删除 pinned episode、enabled skill、manual memory、最近 checkpoint。
- backup/restore 覆盖 SQLite 和 AgentEnd home 下关键目录。

## 11. 后续增强需求

首版 Action Layer 完成后，后续增强按以下优先级推进。排序原则是先补强可复现、可评测和可追溯，再扩展真实外部能力和生产化运行边界。

### 11.1 Replay 真实回放增强

必须在现有 run export、tool contract snapshot 和 Action Policy 基础上补齐：

- replay dry-run，预览将复用、重跑、跳过或阻断的 step。
- 历史工具输出复用，优先复用无副作用工具和已缓存/已导出的 tool output。
- contract drift 检测，比较历史 snapshot 与当前 Tool Contract。
- replay report 输出每个 step 的 replay_strategy、skip_reason、contract_diff 和 source_run_id。
- 默认仍阻断外部可见副作用；只有显式允许时才可重跑对应工具。

### 11.2 Eval Suite 覆盖扩展

必须把 Eval 从 smoke 基线扩展成持续回归反馈回路：

- 高影响工具至少一个 eval case：Shell、Python Exec、Browser、DB、IM、Vision、Tool Generator。
- 默认 built-in skill 至少一个 eval case。
- Episode-to-Skill draft 生成后必须产生可运行 eval，而不是占位断言。
- 失败 eval 必须关联 run export，便于定位 artifact、tool_call、context ledger 和 policy decision。
- Eval report 保持 machine-readable JSON，同时可生成简洁 human summary。
- `tools-smoke` 必须离线可运行；Browser 只访问本机 fixture，Telegram 只使用 dry-run。
- `skills-smoke` 必须支持默认 built-in skills、单个 installed skill，以及本地 skill draft 路径。

新增验收命令：

```bash
agentend eval list
agentend eval run tools-smoke
agentend eval run skills-smoke
agentend eval run skills-smoke --skill research.report
agentend eval run skills-smoke --skill-path ./data/skill_drafts/demo.promoted
agentend eval report <eval_run_id>
```

### 11.3 真实 Search Provider 和 Evidence Export

必须把 `web.search` 从 fake provider 扩展为可配置真实 provider：

- 支持 provider 配置和 secret 检查，不把 API key 写入 DB 或 export。
- search/fetch 结果必须写入 source evidence、result cache 和 run export。
- Evidence export 必须包含 source manifest、content_hash、fetched_at、query 和使用位置。
- 搜索失败使用 Error Taxonomy 分类，并能被 Replanner 识别。
- 默认 provider 仍为 `fake`，首个真实 provider 为 `brave`，通过 `BRAVE_SEARCH_API_KEY` 或配置中的 `api_key_env` 读取 secret。
- `web.search` cache hit 必须为当前 run 重建 source evidence，不能复用历史 run 的 source id。
- `runs export` 必须同时输出 `run.json` 内嵌 evidence manifest 和独立 `evidence_manifest.json`。

新增验收命令：

```bash
agentend secrets check BRAVE_SEARCH_API_KEY
agentend tools test web.search --input '{"query":"agentend","provider":"brave","limit":3}'
agentend sources list --run <run_id>
agentend runs export <run_id> --output ./exports
```

### 11.4 Skill Market 远程市场和版本快照

必须让 Skill Market 从本地 fixture 进入真实可运营状态：

- 支持默认 curated market URL 和用户自定义远程 git market。
- market refresh 写入 market cache、版本、hash、来源和校验结果。
- extension rollback 必须能恢复真实 skill 文件内容，而不仅是元数据回滚。
- 坏包隔离为 quarantined，并输出可读错误报告。
- 远程市场默认需要 HITL 确认来源信任边界。
- `git` market refresh 必须支持本地 git fixture path；远程 git URL 只在用户显式添加后拉取。
- ExtensionVersion 必须指向可恢复 snapshot，`content_hash` 必须来自 skill bundle 文件内容。
- Quarantined skill 不得进入 Capability Map；同一 market 中其他 valid skill 仍可安装。

新增验收命令：

```bash
agentend skills markets add curated --git <url>
agentend skills refresh
agentend extensions rollback skill:<skill_id> --version <version>
```

### 11.5 Context Policy 和 Budget 深化

必须增强长任务上下文稳定性：

- 提供 project/skill context policy CLI 管理。
- Context Budgeter 记录每个被裁剪条目的 reason。
- Context Regression Eval 覆盖长对话、多 workflow、真实搜索 provider 和 skill policy merge。
- 低置信 memory、过期 memory 和不可信 source 不应作为强约束进入 context。
- `budget set` 写入的 `max_llm_calls`、`max_input_tokens` 和 `max_output_tokens` 必须在 workflow LLM step 执行时生效。
- `context ledger show` 必须能展示 selected context items 与 dropped context items，便于审计为什么某条上下文未进入 prompt。

### 11.6 Browser 和 Vision 真实能力增强

必须把 fake/fallback 能力逐步替换为真实 provider 路径：

- Browser Doctor 检查 Playwright 浏览器安装状态，并给出修复命令。
- Browser action 记录当前 URL、title、截图、DOM excerpt、fallback 标记。
- Vision 支持真实多模态 provider、OCR 和图表解析，同时保留 fake provider 作为 eval fallback。
- 真实外部 provider 的 secret、成本和失败必须进入现有治理链路。

### 11.7 Scheduler、Inbox 和长期运行可靠性

必须增强持续执行能力：

- Scheduler 支持更完整 cron 语法或明确限制并在 validate 中阻断不支持语法。
- schedule 连续失败达到阈值后自动暂停。
- inbox watch 支持批量限流、backoff 和重复文件去重。
- 自动化入口创建的 run 必须保持 run_mode、source 和 task/schedule/inbox 关联。

### 11.8 Storage Retention 实际清理策略

必须把 storage cleanup 从 dry-run 推进到可控实际清理：

- 定义 retention rule，覆盖 artifacts、exports、cache、sandboxes、old checkpoints 和 skill drafts。
- cleanup actual mode 必须记录删除路径、大小、原因和 dry-run 对照。
- pinned episode、enabled skill、manual memory、最近 checkpoint 默认保留。
- restore 必须支持临时 home 验证。

### 11.9 Telegram 多用户绑定增强

必须修正单机最近 pending request 策略：

- Telegram conversation、run、clarification request 必须按 chat_id/user_id 绑定。
- pending clarification 只能由对应 chat/user 回答。
- 多用户并发测试覆盖两个 chat 同时 waiting_input 的场景。
- Telegram 输出不暴露 secret、内部路径和未脱敏 tool output。

## 12. 成功标准

- `agentend tools list/show/test` 可展示内置工具、MCP 工具和 Skill 工具。
- 每个工具都有统一 Tool Contract，并可被 `agentend capabilities refresh` 消费。
- Action Policy 能为每次工具调用生成执行决策。
- HITL Clarification 能在缺参、高风险和目标歧义时创建可恢复请求。
- Agent Eval Harness 能跑通 smoke eval。
- Context Regression Tests 能通过 `agentend eval run context-smoke` 运行，并能在 `eval report` 中看到 case/assertion 结果。
- Model Routing 能按任务阶段选择模型，并能限制 workflow 预算。
- Context Runtime 能构造、预览和记录 LLM 上下文包。
- Tool Result Compactor 能把大工具输出转为 artifact + 摘要。
- Memory Store 能按 scope 写入、搜索、遗忘记忆。
- Checkpoint / Resume 能从指定 step 恢复 run。
- `agentend doctor` 能完成本地环境诊断并输出修复建议。
- Workspace Indexer 能生成项目 summary，并被 Goal Analyzer 读取。
- `python.exec` 默认使用 `local_subprocess`，并能记录 stdout/stderr/artifacts。
- File System、Shell Runner、Search + Fetch 可以被 workflow 调用。
- Git Tool Suite 至少支持 status、diff、show、commit 的受控调用。
- Skill Library 可以安装、启用、禁用、运行 Skill。
- 默认内置 Skills 至少有 4 个可运行。
- 默认 curated market 可配置并可 refresh。
- Goal Analyzer 可以为自然语言输入推荐至少一个 skill 或 workflow。
- Replanner 可以在工具失败时给出可执行下一步。
- Episode Logger 可以从 run 生成 episode。
- Run Replay 可以复跑无外部副作用的历史 run。
- Run Export 可以导出完整 redacted 调试包，并包含该 run 执行时的 Tool Contract Snapshot。
- Task Inbox 可以保存、运行、恢复本地任务。
- Episode to Skill 可以生成可校验的 skill draft。
- Extension Lifecycle 能统一管理 skill、MCP、generated tool 的状态和版本。
- Source / Evidence Manager 能记录 web/browser/file 来源并进入 run export。
- Storage Governance 能统计、清理、备份和恢复本地数据。
- 所有新增行为有自动化测试覆盖。
- 后续增强任务必须继续遵循统一 Tool Contract、Action Policy、Eval、Context Ledger、Source Evidence 和 Run Export 链路，不允许新增旁路实现。
