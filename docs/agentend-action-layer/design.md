# AgentEnd Action Layer 设计文档

## 1. 设计目标

Action Layer 在 AgentEnd Lite 首版之上补齐行动能力。首版已经有 CLI、Telegram、SQLite、workflow、MCP client 和基础工具；本阶段重点是：

- 扩展工具注册表，让工具成为可发现、可描述、可测试的能力资产。
- 把 workflow 进一步包装为 Skill Bundle，并支持 Skill Market。
- 将 `python.exec` 改造成 `local_subprocess` 执行后端。
- 增加 Goal Analyzer、Replanner 和 Episode Logger，形成分析、执行、失败恢复和经验沉淀闭环。
- 增加 Context Runtime、Action Policy、Eval Harness、Model Routing、Checkpoint 和 Evidence，让长任务可控、可恢复、可评测、可追溯。

## 2. 总体架构

```text
CLI / Telegram
    ↓
Doctor / Task Inbox / Scheduler
    ↓
Conversation Service
    ↓
Project Profile + Workspace Indexer
    ↓
Context Runtime
    ├─ Context Ledger
    ├─ Context Budgeter
    ├─ Tool Result Compactor
    ├─ Memory Store
    └─ FTS5 Retrieval
    ↓
Goal Analyzer
    ↓
Skill Resolver
    ├─ Builtin Skills
    ├─ Curated Skill Market
    └─ User Skill Markets
    ↓
Workflow Runner
    ↓
Tool Registry
    ├─ Built-in Tools
    ├─ MCP Tools
    ├─ Skill Tools
    └─ Generated Draft Tools
    ↓
Capability Map + Tool Contracts
    ↓
Action Policy + HITL Clarification
    ↓
Execution Backends
    ├─ local_subprocess
    ├─ shell
    ├─ browser
    └─ http/search/db/im/vision
    ↓
SQLite + artifacts + run replay/export + episodes
    ↓
Eval Harness + Evidence + Storage Governance
```

## 3. Tool Registry 设计

### 3.1 Tool Manifest

所有工具都统一转换为 Tool Manifest：

```yaml
name: shell.run
source: builtin
category: local_execution
description: Run a shell command.
risk: local_execution
side_effect: local_execute
timeout_seconds: 120
retryable: false
requires_secrets: []
artifact_policy: capture_stdout_stderr
input_schema:
  type: object
  required: [command]
  properties:
    command:
      type: string
    cwd:
      type: string
    timeout_seconds:
      type: integer
output_schema:
  type: object
  properties:
    stdout:
      type: string
    stderr:
      type: string
    exit_code:
      type: integer
audit_events:
  - tool.called
  - tool.completed
```

### 3.2 Tool Sources

| Source | 示例 |
| --- | --- |
| `builtin` | `fs.list`、`shell.run`、`web.fetch` |
| `mcp` | `mcp.filesystem.read_file` |
| `skill` | Skill 暴露的 workflow wrapper |
| `generated` | Tool Generator 生成的 draft 工具 |

### 3.3 CLI

```bash
agentend tools list
agentend tools show shell.run
agentend tools test shell.run --input '{"command":"echo hello"}'
agentend tools enable shell.run
agentend tools disable shell.run
```

### 3.4 Tool Contract 消费方

Tool Contract 是 Action Layer 的稳定接口。以下模块只能读取 contract，不直接读取工具实现：

- Goal Analyzer：根据 description、category、side_effect、input_schema 推荐工具。
- Replanner：根据 retryable、error code、side_effect 判断重试或换工具。
- Capability Map：把 contract 和 skill manifest 合并成能力索引。
- Run Replay：使用 contract 快照判断历史 run 是否可复现。
- Audit Export：根据 requires_secrets 和 artifact_policy 做脱敏。

## 4. Skills 设计

### 4.1 Skill Bundle 结构

```text
skills/
  research.report/
    skill.yaml
    workflow.yaml
    README.md
    examples/
      basic.input.json
      basic.expected.md
    evals/
      basic.yaml
```

### 4.2 skill.yaml

```yaml
id: research.report
version: 0.1.0
description: Generate a sourced research report.
triggers:
  - research
  - report
  - 调研
workflow: workflow.yaml
required_tools:
  - web.search
  - web.fetch
  - file.write_text
required_mcp: []
input_schema:
  type: object
  required: [topic]
  properties:
    topic:
      type: string
output_schema:
  type: object
  properties:
    report_path:
      type: string
enabled: true
source:
  type: builtin
```

### 4.3 Skill Registry

Skill Registry 负责：

- 扫描 builtin skills。
- 扫描本地 `skills/`。
- 从 market refresh metadata。
- 安装 skill 到本地 cache。
- 校验 manifest 和 workflow。
- 将 enabled skill 暴露给 Goal Analyzer 和 workflow runner。

### 4.4 Skill Market

Market 支持两类 backend：

```text
git
directory
```

SQLite 表：

- `skill_markets`
- `skills`
- `extension_records`
- `extension_versions`

首版 Skill 版本和安装状态收敛在 Extension Lifecycle 中，避免在 Skill Registry 和 Extension Registry 之间重复维护版本状态。

### 4.5 默认内置 Skills

首批内置 Skills 直接随项目发布：

- `research.report`
- `file.workspace_ops`
- `code.local_task`
- `shell.automation`
- `data.quick_analysis`
- `mcp.tool_setup`

`browser.web_task` 和 `telegram.assistant_ops` 可在对应工具完成后启用。

## 5. python.exec local_subprocess 设计

### 5.1 后端抽象

```python
class PythonExecBackend:
    def run(self, code: str, context: ToolContext) -> PythonExecResult:
        ...
```

实现：

```text
LocalSubprocessPythonBackend
```

### 5.2 工作目录

```text
data/sandboxes/<run_id>/<tool_call_id>/
  script.py
  stdout.txt
  stderr.txt
  outputs/
```

### 5.3 执行流程

```text
render code
  ↓
create workspace
  ↓
write script.py
  ↓
subprocess.run(...)
  ↓
capture stdout/stderr/exit_code
  ↓
collect workspace files
  ↓
write artifacts
  ↓
return ToolResult
```

### 5.4 约束

本阶段不加入额外治理限制，不做审批、不做容器隔离。只做：

- 独立 workspace。
- timeout。
- stdout/stderr 捕获。
- artifacts 收集。
- 进程退出码记录。

## 6. 工具设计

### 6.1 File System

新增 `fs.*` 工具，逐步替代现有 `file.*`：

```text
fs.list
fs.glob
fs.stat
fs.read_text
fs.write_text
fs.copy
fs.move
fs.delete
fs.mkdir
```

兼容策略：

- 保留 `file.read_text` 和 `file.write_text`。
- 内部可委托到 `fs.read_text` 和 `fs.write_text`。

### 6.2 Shell Runner

`shell.run` 使用 `subprocess.run`。

输出：

```json
{
  "stdout": "...",
  "stderr": "...",
  "exit_code": 0,
  "duration_ms": 12
}
```

### 6.3 Search + Fetch

接口：

```text
web.search(query, limit)
web.fetch(url)
```

Provider 策略：

- 首版允许 `web.search` 走 configured MCP search server。
- 如果没有 search provider，则给出明确错误。
- `web.fetch` 用 `httpx` 拉取，HTML 转文本，提取链接。

### 6.4 Browser Agent

使用 Playwright。若本机尚未安装 Playwright browser executable，首版允许明确标记的静态 fallback；fallback 不能被记录为真实浏览器截图或真实交互。

状态：

- 首版每次 tool call 可以启动独立 browser context。
- 截图写入 artifacts。
- fallback screenshot 必须写入 artifact 并标记 `fallback=true`。

### 6.5 DB Writer

首版只支持 SQLite：

```text
db.query
db.execute
db.write_rows
```

首版可以直接传入本地 SQLite path。`db.query` 限制为 SELECT；`db.execute` 和 `db.write_rows` 标记为本地写入。

### 6.6 IM Sender

首版只支持 Telegram：

```text
im.telegram.send_message
im.telegram.send_file
```

与现有 `telegram_bot.py` 共享 token 配置。真实发送必须存在 `TELEGRAM_BOT_TOKEN`；测试和 eval 使用 dry-run。

### 6.7 Vision Analyzer

首版提供 fake provider 作为稳定回归基线；真实 OCR、图表解析和图片描述后续走 LLM Router 的多模态接口。

### 6.8 Tool Discoverer

统一查询：

- builtin tools。
- MCP tools。
- enabled skills。
- generated draft tools。

### 6.9 Tool Generator

生成 draft：

```text
data/generated_tools/<tool_id>/
  tool.yaml
  implementation.py
  test_workflow.yaml
```

不自动进入 stable。当前 `tools.generate` 只生成本地 draft，写入 `generated_tools` 表，并把 extension lifecycle 状态设为 `draft`。Tool Registry 不扫描 `generated_tools` 目录，因此 draft 工具不会因为生成而出现在 `tools list/show`。Capability Map 可把 draft 作为 `source=generated` 的候选能力展示，但不允许直接执行。

draft 目录包含：

- `tool.yaml`：工具元数据、输入输出 schema 和 side effect 建议。
- `implementation.py`：待人工 review 的工具实现骨架，默认抛出未实现错误。
- `test_workflow.yaml`：启用前应补齐并运行的测试 workflow。

## 7. Goal Analyzer

Goal Analyzer 是默认入口前置步骤。

输入：

- channel。
- user text。
- recent messages。
- available skills。
- available tools。

输出：

```json
{
  "goal": "...",
  "constraints": [],
  "candidate_skills": ["research.report"],
  "candidate_tools": ["web.search", "web.fetch"],
  "missing_inputs": [],
  "risk_notes": []
}
```

CLI：

```bash
agentend goal analyze "帮我调研..."
```

## 8. Replanner

Replanner 在以下场景触发：

- tool call failed。
- workflow step failed。
- final evaluator 未通过。
- required input missing。

输出：

```json
{
  "decision": "alternative_tool",
  "reason": "web.search unavailable, use mcp.search.search",
  "next_step": {...}
}
```

首版只生成建议，不自动改写 workflow 文件。

## 9. Episode Logger

Episode Logger 从 run 汇总高层复盘。

```text
run + steps + tool_calls + artifacts + event_log
  ↓
episode
```

SQLite：

- `episodes`
- `episode_tools`
- `episode_artifacts`

CLI：

```bash
agentend episodes list
agentend episodes show <episode_id>
agentend episodes summarize <run_id>
```

## 10. 可用性基础设施设计

### 10.1 AgentEnd Doctor

`doctor` 是只读诊断模块，直接面向 CLI 和 Telegram 管理入口。

```text
Doctor
  ├─ runtime checks: python, imports, package metadata
  ├─ storage checks: home, sqlite, artifacts, sandboxes
  ├─ provider checks: llm, telegram, mcp, browser
  ├─ execution checks: local_subprocess smoke
  └─ report: ok / warning / error + fix_hint
```

所有检查输出统一为：

```json
{
  "name": "sqlite",
  "status": "ok",
  "message": "database is reachable",
  "fix_hint": null
}
```

### 10.2 Workspace Indexer 和 Project Profile

Workspace Indexer 只生成轻量结构化摘要，不做全文长期索引。

```text
workspace root
  ↓
read AGENTS.md / README / docs / config / tests
  ↓
extract project type, commands, constraints, entrypoints
  ↓
workspace_index table + project_profile.md
```

Project Profile 存放项目固定信息：

- 常用启动命令。
- 常用测试命令。
- 代码审查边界。
- 不允许触碰的目录。
- 用户确认过的验收标准。

Goal Analyzer 在推荐 skill/tool 前读取 workspace summary，避免把通用任务误判成纯聊天。

### 10.3 Git Tool Suite

Git 工具由受控 wrapper 提供，不直接暴露任意 git 子命令。

```text
git.status   -> git status --short --branch
git.diff     -> git diff [-- path]
git.show     -> git show <rev>
git.log      -> git log --oneline -n <limit>
git.branch   -> git branch --show-current / list
git.commit   -> git add <file list> + git commit -m <message>
```

`git.commit` 必须传入明确 file list，不能隐式提交整个工作区。

### 10.4 Artifact Manager、Run Replay 和 Run Export

Artifact Manager 负责统一定位 run 产物：

```text
data/artifacts/<run_id>/
  tool-calls/
  screenshots/
  reports/
  downloads/
  logs/
```

Run Replay 使用历史 run 的快照：

- original input。
- workflow id + version。
- skill id + version。
- tool contract snapshot。
- redacted config snapshot。

Tool Contract Snapshot 在 run 创建后按当前 Tool Registry 写入 `tool_contract_snapshots`。快照保存完整 contract JSON、tool name、run id 和 created_at，用于 export、replay drift 检查和后续历史工具输出复用。

Replay 默认跳过 `network_write`、`local_execute`、`local_write` 等有副作用工具，除非 CLI 明确传入允许参数。

Run Export 输出一个目录或 zip 包，包含 metadata、steps、tool_calls、tool_contract_snapshots、artifacts、episode 和 redacted config，并额外写出 `tool_contracts.json` 便于审计工具独立消费。

本轮 replay 首版采用“重新执行而非模拟回放”：

```text
source run
  ↓
read workflow_id + input_json
  ↓
WorkflowRunner.run(channel=replay, run_mode=replay)
  ↓
Action Policy blocks network_write/external_write
  ↓
new replay run with replay metadata event
```

这样先得到可审计、可失败、可恢复的 replay 闭环；历史工具输出复用仍保留为后续增强，但 contract snapshot 必须在本轮落地，避免 replay/export 只能依赖当前工具定义。

### 10.5 Task Inbox 和 Scheduler

Task Inbox 是单机持久化任务队列：

```text
tasks
  ├─ pending
  ├─ running
  ├─ blocked
  ├─ completed
  └─ failed
```

Scheduler 只负责本地触发 workflow，不引入外部队列和分布式锁。每次触发都会创建 task，再由现有 runner 执行。

首版 Scheduler 采用显式 tick 模式：

```text
schedule add
  ↓
schedules row(status=active)
  ↓
schedule tick/run-now
  ↓
tasks row(source=scheduler)
  ↓
WorkflowRunner(channel=task, run_mode=scheduler)
  ↓
update task + schedule last_task_id/last_run_id
```

`schedule tick` 支持常见五段 cron 的 `*`、`*/n`、数字和逗号列表，并用 `last_triggered_at` 避免同一分钟重复触发。Scheduler 不作为后台 daemon 常驻，后续可由 systemd timer、cron 或外部 MCP 调用 `schedule tick`。

`run_mode=scheduler` 会传入 ToolContext，Action Policy 默认阻断 `network_write` 和 `external_write`，防止周期任务重复触发外部可见副作用。

### 10.6 Capability Map

Capability Map 是 Goal Analyzer 的召回层。

```text
tool contracts + skill manifests + MCP tools + generated drafts
  ↓
normalize capability records
  ↓
query by goal text / required action / side effect / dependency
```

能力记录包含：

- capability id。
- source。
- action verbs。
- input/output 摘要。
- required secrets。
- side effect。
- risk level。
- example invocation。

首版 Goal Analyzer 不新增外部 LLM 调用，直接使用规则化 intent 识别和 Capability Map 召回：

```text
user text + workspace summary + workflows + enabled skills + capabilities
  ↓
goal, constraints, candidate_skills, candidate_tools, candidate_workflows, missing_inputs, risk_notes
```

首版 Replanner 直接读取 Error Taxonomy：

```text
failed_step + error_code + error message + observations
  ↓
retry / fix_input / ask_user / alternative_tool / switch_skill / fail_with_reason
```

### 10.7 Episode to Skill

Episode to Skill 从成功 episode 生成 skill draft：

```text
episode
  ↓
extract goal, inputs, workflow path, tools, artifacts
  ↓
generate skill.yaml + workflow.yaml + README.md + examples + eval skeleton
  ↓
save to data/skill_drafts/<skill_id>/
```

draft 不自动 enable。用户必须运行 `skills validate` 后再安装或启用。

### 10.8 File Inbox、stdin/stdout 和 clipboard

File Inbox Watcher 监听本地 inbox 目录。检测到新文件后创建 task，并把文件路径作为 workflow input。

CLI 管道支持：

```bash
cat input.md | agentend workflows run research.report --stdin --output json
agentend workflows run file.workspace_ops --input "..." --output text
```

`workflows run --output json` 输出稳定结构：

```json
{"status":"completed","run_id":"...","output":"..."}
```

失败时输出：

```json
{"status":"failed","run_id":"...","error":"..."}
```

File Inbox 用 `source=file_inbox` 和 `source_path` 去重，同一个文件不会在一次或多次 scan 中重复创建 task。首版提供 `--once` 方便脚本和测试；不传 `--once` 时按 interval 轮询。

Clipboard 工具是显式 CLI/helper 能力，不作为默认最终输出。无系统 clipboard 时返回明确错误；无头测试环境可设置 `AGENTEND_CLIPBOARD_FILE` 使用本地文件后端。

### 10.9 Secrets、Result Cache 和 Error Taxonomy

Secrets Manager 只保存 secret 名称、来源和存在状态，不在 SQLite 明文保存 secret 值。

Result Cache 使用 SQLite metadata + artifacts text/blob：

```text
ToolRegistry.call
  ↓
if tool in cacheable network-read set
  ↓
cache key = tool name + normalized input + provider/config hash
  ↓
hit: return ToolResult from result_cache
miss/stale: execute tool and upsert result_cache
```

首版缓存 `web.fetch`、`web.search`、`http.request` 的 text/json 结果，不缓存本地写入、外部写入和 local_execute。缓存 hit/miss/stale 写入 event log，TTL 由工具输入 `cache_ttl_seconds` 或默认值控制。

Error Taxonomy 统一工具错误输出：

```json
{
  "code": "missing_config",
  "message": "web.search provider is not configured",
  "retryable": false,
  "suggested_action": "configure_provider"
}
```

Replanner 优先使用结构化 error code，而不是解析 stderr 文本。

## 11. Context Runtime 和治理底座设计

### 11.1 Context Ledger

Context Ledger 是每次 LLM 调用的上下文审计记录。

```text
llm call
  ↓
context pack
  ├─ fixed: system, agent.md, project profile
  ├─ task: goal, constraints, workflow state
  ├─ short-term: recent messages, current run summary
  ├─ retrieved: memory, workspace index, sources
  └─ compacted: tool result summaries
  ↓
context_ledger + context_pack_items
```

Ledger 记录：

- llm_call_id。
- run_id。
- workflow_step_id。
- model route。
- max_context_tokens。
- estimated_input_tokens。
- pack item 类型、来源、摘要、token estimate、hash。

### 11.2 Context Budgeter 和 Pack Builder

Context Budgeter 负责在 token 预算内选择上下文块。默认预算：

| 类别 | 默认占比 |
| --- | --- |
| 固定规则 | 15% |
| 当前任务 | 20% |
| 最近对话和 run state | 20% |
| 检索内容 | 25% |
| tool result summary | 15% |
| 安全余量 | 5% |

Pack Builder 不读取原始工具大输出，只读取 compactor 生成的摘要和 artifact 引用。

### 11.3 Tool Result Compactor

工具执行结果分为两层：

```text
raw output -> artifact / DB
summary    -> active context
```

每类工具有默认压缩策略：

- Shell：command、cwd、exit_code、stderr 摘要、关键路径、最后 N 行。
- Web：title、URL、摘要、链接、source id。
- Browser：当前 URL、title、截图 artifact、抽取摘要。
- File：path、operation、diff/摘要、artifact id。
- DB：SQL 摘要、行数、schema、样例行。

### 11.4 Memory Store 和 FTS5 Retrieval

首版 Memory Store 使用 SQLite 表 + markdown 文件，不引入独立向量库。

```text
memory_items
  ├─ scope: session/task/project/episode/skill/user
  ├─ content
  ├─ source
  ├─ confidence
  ├─ ttl
  ├─ tags
  ├─ evidence_artifact_id
  └─ last_used_at
```

FTS5 Retrieval 按 scope、tag、source、confidence、ttl 过滤，再做全文检索。后续可在不改变上层接口的情况下增加 embedding provider。

FTS5 虚表：

```text
memory_items_fts(memory_id UNINDEXED, scope UNINDEXED, content, tags UNINDEXED)
```

写入、编辑和遗忘 memory 时同步 FTS。检索结果先按 FTS relevance 召回，再按 confidence、created_at 排序；命中后更新 `last_used_at` 并写入 `memory_retrievals`。如果 SQLite 构建不支持 FTS5，则回退 contains 检索，但不改变 CLI 输出和过滤语义。

### 11.5 Context Policy

workflow/skill manifest 可声明 context policy。Runner 在调用 LLM 前合并：

```text
global policy
  ↓
project profile policy
  ↓
workflow policy
  ↓
skill policy
  ↓
step override
```

冲突规则：越靠近当前任务的 policy 优先，但不能放宽全局安全和脱敏策略。

合并规则：

- `max_items`、`max_context_tokens` 取更小值。
- `redact_secrets` 一旦上层为 true，下层不能改成 false。
- `include_memory=false` 可由任意下层收紧。
- `memory_scopes` 取交集；未声明时继承上层。

`context preview` 输出最终 merged policy，Runner 在 LLM step 中传入 workflow policy 和 step override 后构造 context pack。

Memory Write Policy 作为 Context Runtime 的写入守门：

```text
memory write request
  ↓
source + scope + content redaction
  ↓
allow / reject
  ↓
MemoryItem + FTS sync
```

`manual` source 可写 project/user；`web`、`tool`、`untrusted` 默认只能写 session/task/episode，避免把未经确认的网页或工具输出提升为长期事实。

### 11.6 Action Policy

Action Policy 在 Tool Registry 和 Execution Backend 之间执行。

```text
tool call request
  ↓
tool contract + workflow context + run mode
  ↓
policy decision: allow / block / require_clarification
  ↓
execution or HITL
```

决策依据：

- side_effect。
- tool source。
- workflow run mode。
- replay/scheduler 标记。
- required secrets。
- user/channel。

首版不做完整审批系统，只做统一策略记录和可恢复阻断。

### 11.7 HITL Clarification

Clarification Request 是可持久化的 workflow pause：

```json
{
  "type": "missing_input",
  "question": "请提供 Telegram chat_id",
  "choices": [],
  "free_text_allowed": true,
  "resume_token": "resume_...",
  "expires_at": "2026-05-05T10:00:00Z"
}
```

CLI 和 Telegram 只负责展示和回收回答，恢复逻辑由 Runner 使用 checkpoint 执行。

首版落点：

```text
human_input node
  ↓
RunStep(status=waiting_input)
  ↓
ClarificationRequest(status=pending, resume_token=...)
  ↓
runs resume --answer
  ↓
mark request answered + complete waiting step
  ↓
continue remaining workflow nodes in the same run
```

`clarification_requests` 是 CLI 和 Telegram 共享表。CLI 提供 list/show 读取 pending request；Telegram router 会在普通消息进入默认对话前查找最近的 pending Telegram run，并调用同一 `WorkflowRunner.resume(..., answer=...)` 入口。

### 11.8 Agent Eval Harness

Eval Harness 运行完整 AgentEnd 垂直链路：

```text
eval case
  ↓
fixture workspace + fake llm/tool/provider
  ↓
workflow/skill run
  ↓
assertions over final output, artifacts, tool calls, policy decisions, context ledger
```

Eval Result 使用统一 payload：

```json
{
  "suite": "context-smoke",
  "status": "passed",
  "cases": [
    {
      "id": "memory-retrieval",
      "status": "passed",
      "run_id": "...",
      "assertions": [
        {"name": "memory item enters context", "status": "passed"}
      ]
    }
  ]
}
```

`smoke` 负责基础 runtime 检查，并内嵌执行轻量 `context-smoke`；`context-smoke` 负责 lost-context、tool-output-bloat、memory-retrieval、policy-merge 四类上下文回归。Eval case 应尽量通过真实 WorkflowRunner、ToolRegistry、Context Ledger 和本地 SQLite 运行，fake LLM 只用于稳定输出，不 mock 内部 collaborator。

Eval 不替代单元测试；它用于发现智能体行为退化。

### 11.9 Model Routing 和 Cost Budget

Model Router 在 LLM Router 之上增加任务阶段：

```text
goal_analyze
context_compact
workflow_step
replan
vision
final_evaluate
```

Cost Budget 记录：

- max_llm_calls。
- max_input_tokens。
- max_output_tokens。
- max_estimated_cost。
- current usage。

超预算输出标准 error code：`budget_exceeded`。

### 11.10 Checkpoint / Resume Snapshot

Checkpoint 是 run 的稳定恢复点：

```text
checkpoint
  ├─ workflow version
  ├─ step cursor
  ├─ state json
  ├─ context summary
  ├─ artifacts manifest
  ├─ policy decisions
  └─ pending clarification
```

Runner 只从 completed step 后恢复，不从半个工具调用中间恢复。

Resume 从 checkpoint 继续时复用原 run：

```text
checkpoint(node_id=N)
  ↓
load completed outputs up to N
  ↓
skip nodes through N
  ↓
execute remaining nodes with run_mode=normal/replay/scheduler
  ↓
append new RunStep rows and update original run status
```

如果 resume 前用户修正了 workflow 文件，后续节点按当前 workflow 定义执行；旧 failed step 保留在审计记录中，不删除。

### 11.11 Extension Lifecycle

扩展状态统一由 `extension_records` 管理：

```text
draft -> installed -> enabled -> disabled -> quarantined -> removed
```

覆盖 Skill、MCP server、Generated Tool 和 User Market。Skill Registry、MCP Registry、Tool Generator 都只更新 lifecycle，不直接绕过 enable/disable。

### 11.12 Source / Evidence Manager

Evidence Manager 负责把外部来源与产物绑定：

```text
web.fetch/browser.extract/file.read
  ↓
source_records
  ↓
evidence_links
  ↓
artifact / report / episode / run export
```

报告类 skill 必须输出 source list。Evidence 只记录短 quote 摘要和 hash，不保存无限制全文。

### 11.13 Retention / Cleanup / Backup

Storage Governance 统一处理本地数据增长：

- `storage usage` 汇总 SQLite、artifacts、sandboxes、cache、exports、memory、skill drafts。
- `storage cleanup --dry-run` 先输出将删除内容。
- pinned episode、enabled skill、manual memory、最近 checkpoint 不默认删除。
- backup/restore 覆盖 SQLite 和关键目录。

## 12. 数据模型增量

新增表：

| 表 | 用途 |
| --- | --- |
| `tool_manifests` | 工具统一元数据。 |
| `skill_markets` | Skill 市场配置。 |
| `skills` | Skill 主表。 |
| `episodes` | run 复盘摘要。 |
| `episode_tools` | episode 使用工具。 |
| `episode_artifacts` | episode 关联产物。 |
| `generated_tools` | Tool Generator draft。 |
| `workspace_indexes` | 工作区轻量索引。 |
| `project_profiles` | 项目固定约束和常用命令。 |
| `capabilities` | tools、skills、MCP 的统一能力地图。 |
| `tool_contract_snapshots` | run replay 使用的工具 contract 快照。 |
| `tasks` | 本地任务队列。 |
| `schedules` | 本地周期触发配置。 |
| `artifact_manifests` | run 产物目录和元数据。 |
| `run_exports` | run export 记录。 |
| `replan_suggestions` | workflow/tool 失败后的结构化重规划建议。 |
| `skill_drafts` | episode/tool generator 生成的 skill 草稿。 |
| `result_cache` | 网络读和中间结果缓存。 |
| `error_records` | 结构化错误分类记录。 |
| `secret_refs` | secret 名称、来源、存在状态和脱敏策略。 |
| `context_ledgers` | 每次 LLM 调用的上下文审计记录。 |
| `context_pack_items` | 单个上下文包中的条目。 |
| `context_summaries` | run、tool result、memory 的压缩摘要。 |
| `memory_items` | 分 scope 的长期和短期记忆。 |
| `memory_retrievals` | 检索命中记录和使用情况。 |
| `context_policies` | workflow/skill/project 的上下文策略。 |
| `action_policy_rules` | 执行策略规则。 |
| `action_policy_decisions` | 每次工具调用前的策略决策。 |
| `clarification_requests` | HITL 缺参、歧义和高风险请求。 |
| `eval_suites` | agent eval 套件。 |
| `eval_cases` | agent eval 用例。 |
| `eval_runs` | eval 执行记录。 |
| `model_routes` | 按阶段的模型路由。 |
| `cost_budgets` | workflow/skill/run 的预算配置。 |
| `cost_usage` | LLM token、调用次数和估算成本。 |
| `checkpoints` | run 恢复点。 |
| `extension_records` | 扩展统一状态。 |
| `extension_versions` | 扩展版本、hash 和验证时间。 |
| `source_records` | 外部来源和本地文件来源。 |
| `evidence_links` | source 与 artifact/episode/report 的引用关系。 |
| `storage_retention_rules` | 存储清理规则。 |
| `storage_cleanup_runs` | 清理和 dry-run 记录。 |

## 13. 测试策略

- 每个新增工具至少一个 CLI 或 workflow 集成测试。
- Skill Market 用本地 fixture git/directory 测试，不依赖真实网络。
- `python.exec local_subprocess` 必须测试 stdout、stderr、exit_code、timeout、artifact 收集。
- Goal Analyzer 用 fake LLM 或规则模式测试推荐 skill/tool。
- Replanner 用失败 tool call fixture 测试。
- Episode Logger 用完整 run fixture 测试汇总结果。
- `doctor` 用可控 fake config 覆盖 ok、warning、error。
- Workspace Indexer 用临时项目 fixture 覆盖 README、AGENTS.md、测试命令提取。
- Git Tool Suite 用临时 git repo 覆盖 status、diff、show、commit。
- Run Replay 使用无副作用 workflow fixture，确认会拒绝默认复跑外部副作用工具。
- Run Export 校验脱敏后的 metadata、tool_calls 和 artifacts manifest。
- Capability Map 覆盖 builtin tool、MCP tool、enabled skill 三类来源。
- Task Inbox 和 Scheduler 用冻结时间或 fake clock 覆盖状态流转。
- Episode to Skill 校验生成 draft 且不自动 enable。
- Result Cache 覆盖命中、过期和 cache stale error。
- Error Taxonomy 覆盖至少 8 类标准 error code。
- Context Ledger 覆盖 pack item 记录和 token 估算。
- Context Budgeter 覆盖超预算裁剪、固定规则保留和检索内容排序。
- Tool Result Compactor 覆盖 shell、web、file、db 四类摘要。
- Memory Store 覆盖 scope、TTL、forget、FTS5 search。
- Context Policy 覆盖 global/project/workflow/skill/step 合并和冲突规则。
- Action Policy 覆盖 allow、block、require_clarification。
- HITL Clarification 覆盖 CLI 和 Telegram 共用 request、回答后从 checkpoint 恢复。
- Agent Eval Harness 覆盖 fake LLM、fake tool、fixture workspace 和失败报告。
- Model Routing 覆盖按阶段选择模型和 budget exceeded。
- Checkpoint / Resume 覆盖 step 完成后恢复和 secret 脱敏。
- Extension Lifecycle 覆盖 validate 失败进入 quarantined 和 rollback。
- Source / Evidence Manager 覆盖 web/browser/file source 和 report 引用。
- Storage Governance 覆盖 usage、cleanup dry-run、backup、restore。

## 14. 后续增强设计路线

后续增强不改变“单 Agent、本地 SQLite、CLI/Telegram 入口、统一 Tool Contract”的主架构。所有新增能力必须进入 API -> Service -> DAO/DB 或 CLI -> Core Service -> DB 的现有分层，不新增旁路状态。

### 14.1 Replay 真实回放增强

Replay 增强使用三类数据源决策：

```text
source run
  ├─ run steps
  ├─ tool calls
  ├─ tool_contract_snapshots
  └─ artifacts / result cache
      ↓
replay planner
      ↓
strategy per step: reuse_output / rerun / skip / block
      ↓
replay report + optional replay run
```

`runs replay --dry-run` 只生成 replay plan，不执行工具。真实 replay 默认复用历史无副作用工具输出；需要重跑时仍走 Action Policy。若 snapshot 与当前 Tool Contract 不一致，replay plan 必须标记 contract drift，并要求 HITL 或降级为 skip。

### 14.2 Eval Suite 扩展

Eval Suite 从内置函数逐步扩展为本地 eval registry：

```text
eval suites
  ├─ builtins/context-smoke
  ├─ tools/*.yaml
  ├─ skills/<skill_id>/evals/*.yaml
  └─ generated skill drafts/evals/*.json
```

执行器仍复用 WorkflowRunner 和 ToolRegistry。Eval report 统一输出 cases、assertions、run_id、export_path 和失败定位字段。失败 eval 可以触发 run export，但不能自动修改 workflow、skill 或 tool。

T54 后 Eval Harness 内置四类 suite：

- `smoke`：基础配置、工具注册和 context-smoke 嵌套基线。
- `context-smoke`：上下文 ledger、摘要、memory retrieval 和 policy merge 回归。
- `tools-smoke`：Shell、Python Exec、Browser、本地 DB、IM dry-run、Vision fake provider、Tool Generator 的真实 ToolRegistry 回归。
- `skills-smoke`：默认 built-in skills 回归；可通过 `--skill` 限定 installed skill，或通过 `--skill-path` 运行本地 skill draft/bundle。

失败工具或 skill case 会创建或关联真实 run，并导出到 `data/eval_exports/<suite>/<case>/<run_id>/`，包含 `run.json` 和 `tool_contracts.json`。Episode-to-Skill 生成的 `evals/smoke.json` 使用 `skills-smoke` schema，`skills validate --path` 通过后可直接由 `eval run skills-smoke --skill-path` 执行。

### 14.3 真实 Search Provider 与 Evidence Export

Search Provider 通过 provider adapter 接入：

```text
web.search input
  ↓
provider adapter
  ↓
normalized results
  ↓
source_records + result_cache
  ↓
context summary / run export / evidence manifest
```

Provider adapter 只返回规范化结果，不直接写 context。证据可信度由 Evidence Manager 记录，不把搜索结果直接提升为长期 memory。真实 provider 的 secret 检查走 Secrets Manager，失败进入 Error Taxonomy。

T55 后首个真实 provider 为 Brave Search API：

```toml
[search]
provider = "brave"

[search.providers.brave]
api_key_env = "BRAVE_SEARCH_API_KEY"
base_url = "https://api.search.brave.com/res/v1/web/search"
```

`web.search` 运行流程：

```text
load search config / tool input override
  ↓
secret env check
  ↓
provider adapter request
  ↓
normalize results: title/url/snippet
  ↓
record source_records + evidence_links
  ↓
ToolResult + ResultCache
```

`web.fetch` 和 `web.search` 都通过 Evidence helper 写入来源证据。Result Cache 命中时，ToolRegistry 会为当前 run 重新写入 source evidence，并替换输出中的 `source_id`，避免导出时指向历史 run。`runs export` 输出 `evidence_manifest.json`，并在 `run.json.evidence_manifest` 中内嵌同一份 machine-readable manifest。

### 14.4 Skill Market 远程市场和版本快照

Skill Market 增强引入 market cache：

```text
remote git market
  ↓
fetch into cache path
  ↓
validate bundles
  ↓
write Skill + ExtensionVersion + content hash
  ↓
enable only after validation
```

Rollback 从 `extension_versions` 找到上一 validated version，并恢复对应 cache snapshot 中的真实 `skill.yaml`、`workflow.yaml` 和资源文件。坏包只进入 quarantined，不进入 Capability Map。

T56 后 market cache 目录约定：

```text
skills/market-cache/<market>/
  source/                         # 当前刷新出来的 market working copy
  snapshots/<skill_id>/<version>-<hash>/
  quarantine/<skill_id>/error.json
```

`refresh_markets` 对 `directory` 和 `git` backend 都先写入 `source/`。如果 git location 是本地 path，则复制本地 working tree；如果不是本地 path，则通过 `git clone --depth 1` 拉取远程 URL。每个 valid bundle 会计算目录内容 hash，复制到 `snapshots/`，并把 `ExtensionVersion.source` 指向 snapshot path。`extensions rollback` 对 skill extension 会把 snapshot 复制回当前 installed cache path，再重新加载 `skill.yaml` 同步 Skill metadata。

### 14.5 Context Policy 和 Budget 深化

Context Runtime 增加裁剪记录：

```text
candidate context items
  ↓
budget scoring
  ↓
selected items + dropped items(reason)
  ↓
context_ledger + context_pack_items + context_dropped_items
```

T57 为了支持跨 eval 统计，新增 `context_dropped_items` 表，而不是只把 dropped reason 放入 ledger metadata。每条 dropped item 记录 `ledger_id`、`item_type`、`source`、`content_hash`、`token_estimate` 和 `reason`。

Policy 合并顺序：

```text
default policy
  ↓
global:default
  ↓
project:default / project:<workflow_id>
  ↓
skill:<skill_id>       # 仅 workflow id 为 skill.<skill_id> 时进入
  ↓
workflow context
  ↓
step context
```

收紧型字段只允许更严格：`max_items/max_context_tokens/retrieve_top_k` 取更小值，`redact_secrets` 只能保持或开启，`include_memory` 只能保持或关闭，`memory_scopes` 取交集。Skill policy 因此不能放宽 global redaction。

Context Budgeter 对候选 item 先做 memory 守门，再按 `max_items` 和 `max_context_tokens` 选择。低置信、过期和不可信 source memory 会进入 dropped 记录，reason 分别为 `memory_low_confidence`、`memory_expired`、`memory_untrusted_source`，不会作为强约束进入 context pack。

Workflow budget 在每个 LLM step 记录 context ledger 后执行：

```text
record context ledger
  ↓
check cost_budgets for workflow_id
  ├─ max_llm_calls exceeded -> budget_exceeded
  ├─ max_input_tokens exceeded -> budget_exceeded
  └─ max_output_tokens exceeded after completion -> budget_exceeded
```

预算失败走 Error Taxonomy 和 Replanner，不绕过现有 run/step 审计链路。

### 14.6 Browser 和 Vision 真实能力增强

Browser Agent 采用 provider/fallback 双路径：

```text
browser tool
  ↓
playwright available?
  ├─ yes: real browser context
  └─ no: explicit fallback artifact
```

Vision Analyzer 采用 fake/real provider 双路径，fake provider 只用于本地 eval 和无 key 环境。真实 Vision provider 必须走 Model Routing、Cost Budget、Secrets Manager 和 Error Taxonomy。

### 14.7 Scheduler、Inbox 和长期运行可靠性

Scheduler 增强必须围绕任务状态机：

```text
schedule due
  ↓
create task(source=scheduler)
  ↓
run task with run_mode=scheduler
  ↓
record success/failure count
  ↓
auto pause on threshold
```

Inbox watch 增加 batch id、file hash 和 backoff。重复文件只创建一个 task，批量投递时先进入 pending，不直接并发执行所有 workflow。

### 14.8 Storage Retention 实际清理策略

Storage cleanup 分两阶段：

```text
plan cleanup
  ↓
dry-run report
  ↓
actual cleanup with same plan id
  ↓
record deleted paths and bytes
```

Actual cleanup 必须基于 dry-run plan 或显式 `--confirm`，并记录每个删除项的 retention rule。Restore 测试优先恢复到临时 home，不能覆盖当前 home。

### 14.9 Telegram 多用户绑定增强

Telegram 会话绑定从“最近 pending run”改为精确索引：

```text
telegram chat_id/user_id
  ↓
conversation.external_user_id
  ↓
run
  ↓
clarification_request
```

查找 pending request 时必须同时匹配 channel、chat_id/user_id 和 status。多用户并发只共享同一个本地 DB，不共享 pending answer 路由。
