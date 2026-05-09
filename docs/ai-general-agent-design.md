# AI 通用智能体设计方案

## 1. 文档目的

本文基于 AgentEnd 从 Lite、Action Layer、Runtime Hardening、Review Remediation、Agentic Orchestration 到 Intent Routing 的完整开发过程，总结一套可迁移的 AI 通用智能体设计方案。

这套方案的目标不是做一个简单聊天机器人，也不是一开始就堆叠多 Agent、企业权限后台、远程沙箱和分布式队列，而是先建立一个真正可运行、可验证、可恢复、可审计、可持续演进的单 Agent 运行时。

核心判断：

- 聊天只是入口，不是智能体的完整形态。
- Workflow 是确定性执行单元，不应该被改造成复杂 Agent。
- Agent 的价值在于围绕目标持续选择行动、观察结果、评估进度、修正计划并沉淀经验。
- LLM 不能直接拥有执行权，执行权必须经过结构化决策、能力约束、Action Policy、审计和 eval。
- 工程可靠性来自真实调用链闭合，而不是文档声明或孤立单元测试。

## 2. 总体定位

推荐的通用智能体形态是：

```text
单 Agent
  + 多入口
  + 意图路由
  + 工具优先执行
  + 确定性 workflow
  + 目标导向循环
  + 结构化记忆
  + 上下文治理
  + 副作用策略
  + HITL 澄清
  + checkpoint/resume
  + evidence/export
  + 任务级 eval
```

它应该优先解决以下问题：

- 用户输入到底是闲聊、问答、任务、工具动作、缺参还是高风险请求。
- 对可执行任务，系统是否真的选择工具、skill 或 workflow，而不是只让模型聊天。
- 每一次工具调用是否经过统一工具契约、风险判断、审计和脱敏。
- 长任务中断后是否能恢复，而不是从头重复执行副作用步骤。
- 记忆是否能帮助下一次执行，而不是无限保存原始聊天。
- 文档承诺的能力是否进入真实调用链。

## 3. 总体架构

```text
CLI / Telegram / Task / Scheduler / File Inbox / Replay
  -> Channel Adapter
  -> Conversation Orchestrator
  -> IntentRouter
  -> Route Executor
     -> simple_chat workflow
     -> AgentRunController
     -> HITL Clarification
     -> Blocked Response
  -> Context Runtime
  -> Capability Selector
  -> WorkflowRunner / ToolRegistry / SkillRegistry / LLMRouter
  -> Action Policy
  -> Execution Backends
     -> OpenAI-compatible LLM
     -> MCP tools
     -> filesystem
     -> shell/local subprocess
     -> browser
     -> http/search
     -> git/db/im/vision
  -> SQLite + Artifacts + Evidence + Export
  -> Eval Harness + Memory Consolidator + Effectiveness Store
```

关键分层：

| 层 | 职责 |
| --- | --- |
| Channel Adapter | 适配 CLI、Telegram、Scheduler、Inbox 等入口，只做输入归一化和用户绑定。 |
| Conversation Orchestrator | 持久化消息，调用 IntentRouter，执行 route，写入 assistant response 或澄清请求。 |
| IntentRouter | 将自然语言输入转为结构化 IntentDecision。 |
| AgentRunController | 执行目标导向循环：plan、act、observe、evaluate、replan、finish。 |
| WorkflowRunner | 执行确定性 YAML workflow DAG。 |
| ToolRegistry | 统一工具注册、schema、调用、审计。 |
| SkillRegistry | 管理可复用技能包，并把技能暴露给 Selector。 |
| ContextRuntime | 构造真实进入 LLM 的 context pack，并记录 ledger。 |
| ActionPolicy | 所有工具执行前的统一副作用和风险门禁。 |
| MemoryConsolidator | 从 run/episode 中提炼短、准、可更新的长期记忆。 |
| EvalHarness | 用任务级回归验证智能体真实能力。 |

## 4. 入口层设计

入口层必须薄。它不应该直接拼 prompt、选择工具、执行 workflow 或绕过安全策略。

推荐统一输入结构：

```json
{
  "channel": "telegram",
  "external_user_id": "chat_id:user_id",
  "conversation_id": "...",
  "text": "...",
  "source": "telegram_text",
  "run_mode": "normal"
}
```

入口职责：

- CLI：本地运行、调试、运维、workflow、memory、eval、export。
- Telegram：远程轻量交互，只负责消息适配、用户绑定和响应发送。
- Task Inbox：保存待执行任务。
- Scheduler：本地周期触发任务。
- File Inbox：文件进入后创建任务。
- Replay：复现历史 run，辅助审计和调试。

Telegram 等多用户入口必须使用 `channel + external_user_id` 精确绑定。不能用“最近一个 pending run”作为恢复、取消或澄清的目标，否则会发生跨用户串话。

## 5. 意图路由设计

通用智能体必须先回答一个问题：用户这句话应该走哪条路径。

推荐 IntentDecision 类型：

| 类型 | 含义 | 执行方式 |
| --- | --- | --- |
| `chat` | 闲聊、寒暄、低风险普通对话。 | `simple_chat` workflow。 |
| `answer` | 可直接回答的低风险问答。 | `simple_chat` + context pack。 |
| `task` | 需要目标导向执行的任务。 | `AgentRunController.run()`。 |
| `tool_action` | 高置信单步工具动作。 | 通常仍通过 AgentRunController 保留评估和审计。 |
| `skill_action` | 可由 skill 完成的任务。 | SkillRegistry -> WorkflowRunner。 |
| `workflow_action` | 可由明确 workflow 完成的任务。 | WorkflowRunner。 |
| `clarification` | 缺参、歧义或风险不清。 | 创建 HITL ClarificationRequest。 |
| `blocked` | 明显越权、高风险或 prompt injection。 | 返回安全摘要并审计。 |

路由流程：

```text
normalize input
  -> load recent context and capability summary
  -> deterministic rule classifier
  -> capability retriever
  -> model classifier when needed
  -> schema validation
  -> merge with capability constraints
  -> risk gate
  -> persist IntentDecision
```

设计原则：

- 规则适合明确低成本判断，例如闲聊、调研、代码、文件、缺参。
- 模型只在复杂、多意图、长输入、规则分数接近或风险判断不确定时介入。
- 模型最多输出结构化 intent decision，不能直接执行动作。
- IntentRouter 只能限制候选能力、标记风险、触发澄清或阻断。
- ActionPolicy 仍是工具执行前最后安全层，IntentRouter 不能绕过它。

典型 IntentDecision：

```json
{
  "schema_version": "1",
  "intent_type": "task",
  "goal": "调研浏览器自动化工具并形成报告",
  "confidence": 0.86,
  "slots": {
    "topic": "浏览器自动化工具"
  },
  "constraints": [],
  "missing_inputs": [],
  "candidate_actions": [
    {"type": "skill_run", "name": "research.report", "score": 0.91},
    {"type": "tool_call", "name": "web.search", "score": 0.82}
  ],
  "allowed_tools": ["web.search", "web.fetch", "file.write_text"],
  "risk_level": "low",
  "risk_notes": [],
  "clarification_question": null,
  "source": "rule"
}
```

## 6. AgentRunController

AgentRunController 是智能体目标导向执行的主入口。它不替代 WorkflowRunner，而是调用 workflow、tool、skill 和 LLM reasoning 来完成多轮目标循环。

执行循环：

```text
create agent_run
  -> build goal package
  -> retrieve memory and effectiveness
  -> select next action
  -> execute one action
  -> record observation
  -> evaluate progress
  -> checkpoint and progress artifact
  -> finish / continue / replan / ask_user / fail
  -> consolidate memory
  -> record effectiveness
```

每个 agent run 都应该有目标包：

```json
{
  "goal": "完成用户请求的可验证结果",
  "success_criteria": ["可检查条件"],
  "constraints": ["路径、工具、输出格式、风险边界"],
  "preferred_outputs": ["文件、摘要、命令输出、报告"],
  "stop_criteria": ["完成、无法继续、需要用户输入、达到上限"],
  "max_iterations": 8
}
```

每轮 iteration 至少记录：

- selected action。
- action input。
- expected observation。
- actual observation。
- evaluator verdict。
- next decision。
- linked run/tool call。
- checkpoint id。
- error code 或 stop reason。

支持的 action 类型：

- `tool_call`
- `skill_run`
- `workflow_run`
- `llm_reason`
- `ask_user`
- `finish`

关键原则：

- 每轮最多执行一个主要 action，降低副作用和审计复杂度。
- 对代码、文件、搜索、数据处理、报告生成等任务，默认工具优先。
- 只有没有合适行动能力，或需要综合判断时，才选择 `llm_reason`。
- 失败必须进入 observe -> evaluate -> replan，而不是只保存一条失败消息。
- 达到 success criteria 立即 finish。
- 达到 max_iterations、预算或 stop criteria 时明确停止，并输出剩余事项。

## 7. WorkflowRunner 边界

WorkflowRunner 负责确定性 DAG 执行，不负责目标级循环。

推荐节点类型：

| 类型 | 用途 |
| --- | --- |
| `llm` | 构造 prompt/messages，调用 LLM。 |
| `tool` | 调用 ToolRegistry 中的工具。 |
| `condition` | 根据上游输出做分支。 |
| `parallel` | 并行执行无依赖子节点。 |
| `human_input` | 暂停 run，等待用户补充。 |
| `workflow_call` | 调用另一个 workflow。 |
| `final` | 形成最终输出。 |

WorkflowRunner 必须：

- 使用 schema 校验 workflow。
- 在每个节点前后写入 run step。
- LLM step 必须通过 ContextRuntime 构造 context pack。
- Tool step 必须通过 ToolRegistry 和 ActionPolicy。
- human_input 必须创建可恢复的 ClarificationRequest。
- checkpoint/resume 不能重复执行已完成的高风险副作用步骤。

边界对比：

| 模块 | 负责 | 不负责 |
| --- | --- | --- |
| AgentRunController | 目标循环、action selection、evaluation、replan、long task progress。 | 单个 workflow DAG 节点细节。 |
| WorkflowRunner | workflow schema、节点执行、context pack、tool call、checkpoint。 | 跨 workflow 的目标级循环。 |
| Replanner | 给出下一步建议或替代 action。 | 自动绕过策略执行动作。 |
| MemoryConsolidator | 生成、合并和更新长期记忆。 | 直接决定下一步 action。 |

## 8. Tool Registry 与 Tool Contract

所有工具必须统一成 Tool Manifest。

示例：

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

工具来源：

| Source | 示例 |
| --- | --- |
| `builtin` | `fs.read_text`、`shell.run`、`web.fetch` |
| `mcp` | `mcp.filesystem.read_file` |
| `skill` | skill 暴露的 workflow wrapper |
| `generated` | Tool Generator 生成的 draft 工具 |

统一调用链：

```text
ToolRegistry.call
  -> load ToolContract
  -> resolve dynamic side effect
  -> ActionPolicy.decide
  -> execute backend
  -> redact input/output/error
  -> persist ToolCall
  -> write evidence/artifact/event
```

Tool Contract 的消费者：

- Goal Analyzer：根据 description、category、side_effect、input_schema 召回能力。
- Selector：根据 risk、side effect、required input 和历史效果排序。
- Replanner：根据 retryable、error code、side effect 决定重试或换工具。
- Replay：根据 contract snapshot 判断历史 run 是否可复现。
- Export：根据 requires_secrets 和 artifact_policy 做脱敏。

## 9. Skill 体系

Skill 是比工具更高层的可复用能力，通常封装 workflow、提示、工具组合、示例和 eval。

推荐结构：

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

`skill.yaml` 示例：

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

Skill Registry 负责：

- 扫描 builtin skills。
- 扫描本地 skills。
- 安装和验证市场 skill。
- 校验 manifest 和 workflow。
- 维护版本、状态、来源。
- 将 enabled skill 暴露给 Goal Analyzer 和 Selector。

Skill Effectiveness 必须影响排序。记录字段至少包括：

- success / failure / blocked / needs_input。
- duration。
- iteration_count。
- error_code。
- output_artifact_count。
- recent_success_at。
- common_failure_reason。

Episode-to-Skill 只能生成 draft。Draft 只有通过 eval 和验证后才能进入候选排序。

## 10. Context Runtime

ContextRuntime 是长任务可靠性的核心。所有 LLM 调用都必须通过它构造上下文，不允许各模块自行拼接 prompt。

上下文来源：

- system instruction。
- agent profile。
- project profile。
- current goal。
- intent decision。
- workflow state。
- recent messages。
- selected memory。
- retrieval results。
- tool result summary。
- current user prompt。

处理流程：

```text
collect context candidates
  -> apply context policy
  -> compact large tool results
  -> filter memory by scope/confidence/ttl/source
  -> budget selection
  -> produce ContextPack(selected, dropped)
  -> write ContextLedger
  -> send selected items to LLM request
```

关键规则：

- 当前 prompt、目标和安全约束不能被普通预算裁剪丢掉。
- 大工具输出必须进入 artifact/DB，上下文只放摘要。
- 被裁剪条目必须记录 dropped reason。
- ContextLedger 记录实际进入 LLM request 的 item，而不是只记录理论 pack。
- fake provider 和本地 fixture 也要能证明 context 真的进入请求。

常见 dropped reason：

- `budget_exceeded`
- `memory_low_confidence`
- `memory_expired`
- `memory_untrusted_source`
- `tool_output_compacted`
- `policy_excluded`

## 11. 记忆系统

长期记忆不能是原始聊天全文，也不能是无筛选向量库。推荐结构化、可更新、带来源、能影响执行的记忆系统。

五层记忆：

| 层 | 用途 | 保存方式 |
| --- | --- | --- |
| working | 当前长任务状态、计划、下一步、未完成项。 | task/agent_run checkpoint |
| episodic | run/episode 摘要、失败原因、关键产物。 | episode memory |
| semantic | 项目事实、用户偏好、长期约束。 | project/user memory |
| procedural | 成功流程、常用命令、可复用步骤。 | skill/project memory |
| performance | tool/skill 成功率、失败模式、成本耗时。 | effectiveness record |

Memory Candidate 示例：

```json
{
  "type": "successful_procedure",
  "scope": "project",
  "content": "在 Windows 上运行本项目测试时，使用 .venv\\Scripts\\python.exe 并设置独立 basetemp。",
  "merge_key": "project:test-command:windows",
  "confidence": 0.9,
  "source": "agent_consolidator",
  "created_by_run_id": "...",
  "evidence_artifact_id": "...",
  "tags": ["windows", "pytest"],
  "ttl": null
}
```

Memory Consolidator 流程：

```text
run + iterations + observations + artifacts
  -> extract candidates
  -> classify memory type and scope
  -> dedupe by merge_key
  -> compare with existing memory
  -> create / update / merge / supersede
  -> write provenance
  -> affect future retrieval and selection
```

治理规则：

- 相同 merge_key 优先 update，不重复新增。
- 内容相似时更新 confidence、last_seen、tags。
- 内容冲突时新记忆可标记 active，旧记忆标记 superseded。
- 低置信候选只保留在 episode/task scope，不进入 project/user 长期记忆。
- Memory 写入前后都要做 secret redaction。
- 不可信来源不能作为强约束注入 prompt。

## 12. Action Policy 和副作用控制

安全不能靠提示词。所有可产生副作用的工具必须经过 ActionPolicy。

基础副作用分类：

| side_effect | 含义 |
| --- | --- |
| `none` | 无副作用。 |
| `local_read` | 本地读取。 |
| `local_write` | 本地写入。 |
| `local_execute` | 本地执行。 |
| `network_read` | 网络读取。 |
| `network_write` | 网络写入。 |
| `external_write` | 外部可见写入，例如 IM、邮件、发布、提交。 |

ActionPolicy 输出：

```text
allow
block
require_clarification
```

运行模式策略：

| run_mode | 默认允许 | 默认阻断 |
| --- | --- | --- |
| `normal` | none/local_read/network_read | 高风险写入和执行需记录或 HITL |
| `replay` | none/local_read/network_read | local_write/local_execute/network_write/external_write |
| `scheduler` | none/local_read/network_read/local_write | local_execute/network_write/external_write |
| `telegram` | 低风险摘要输出 | secret、内部路径、raw tool JSON |

动态副作用必须支持。例如 `http.request`：

```text
GET/HEAD/OPTIONS -> network_read
POST/PUT/PATCH/DELETE -> network_write
```

ActionPolicy 失败时必须默认 block，而不是 allow。

## 13. HITL Clarification

遇到缺参、歧义、高风险、预算不足或需要用户确认时，系统应创建 clarification request，而不是猜。

ClarificationRequest 至少包含：

- run_id / agent_run_id。
- step_id / iteration_id。
- checkpoint_id。
- channel。
- external_user_id。
- question。
- expected input schema。
- risk reason。
- status。
- expires_at。

恢复流程：

```text
create clarification
  -> run.status = waiting_input
  -> user answers through same channel
  -> validate channel + external_user_id
  -> load checkpoint
  -> resume from safe point
  -> continue workflow or agent iteration
```

设计原则：

- Router 可返回 `intent_type=clarification`，但最好由 Orchestrator 创建 DB 记录，保持 Router 纯判断。
- Replanner 可以建议澄清问题，但不能绕过 ActionPolicy。
- Resume 前需要重新执行必要策略检查。
- 已完成的副作用 step 不应重复执行。

## 14. LLM、模型路由和成本预算

LLM Router 屏蔽 provider 差异。首版至少支持：

- Fake provider，用于离线测试。
- OpenAI-compatible provider，用于真实调用。

Provider 接口：

```python
class LLMProvider:
    def complete(self, request: LLMRequest) -> LLMResponse:
        ...

    def test(self) -> LLMTestResult:
        ...
```

Model Routing 按 stage 选择模型：

| stage | 用途 |
| --- | --- |
| `intent_classify` | 意图分类。 |
| `goal_analyze` | 目标包生成。 |
| `workflow_step` | workflow LLM 节点。 |
| `replan` | 失败恢复建议。 |
| `final_evaluate` | 结果验收。 |
| `vision` | 图片理解。 |
| `context_compact` | 上下文压缩。 |

Cost Budget 检查点：

- 调用前检查 max_llm_calls。
- 构造 context 后检查 max_input_tokens。
- 响应后检查 max_output_tokens。
- 记录 provider/model/stage/input_tokens/output_tokens/estimated_cost。

预算失败必须进入 Error Taxonomy，并交给 Replanner 或 HITL，而不是静默降级。

## 15. MCP 接入

MCP 应作为能力接入层，不作为安全边界。

要求：

- 支持 stdio / HTTP / SSE 或 streamable HTTP。
- 连接后发现 tools。
- 将 MCP tool 注册为本地 ToolRegistry 工具。
- 命名格式为 `mcp.<server_name>.<tool_name>`。
- 保存 MCP tool input schema。
- workflow 可直接调用 MCP tool。
- MCP 调用输入、输出、错误、event log、export 必须走统一脱敏。

异步边界要特别注意。Telegram handler 等 async 环境中，不能在已有 event loop 里直接 `asyncio.run()`。可用安全同步运行器把 coroutine 放到独立 worker thread 的 event loop 中执行，避免一次性把整个 WorkflowRunner 改 async。

## 16. Evidence、Artifact 和 Export

智能体输出必须可追溯。

Artifact 保存：

- 工具原始输出。
- 大文本。
- 截图。
- 文件产物。
- run export。
- replay report。
- eval report。

Evidence 保存：

- source type。
- source url/path/query。
- content hash。
- short quote 或摘要。
- fetched_at。
- used_by run/step/report section。

原则：

- Evidence 只证明“当时使用了该来源”，不代表来源一定可信。
- 搜索、抓取、浏览器、文件读取都应进入 evidence manifest。
- 报告类 skill 必须输出 source list。
- Run export 应包含 run、steps、tool calls、context ledger、evidence manifest、artifacts index。
- Export 必须统一 secret redaction。

## 17. Replay 和 Checkpoint

Replay 不是简单重跑。重跑会重复副作用，尤其是写文件、发消息、提交代码、调用外部 API。

推荐 replay 策略：

```text
runs replay --dry-run
  -> build replay plan
  -> compare tool contract snapshot
  -> identify side effects
  -> reuse safe historical outputs
  -> mark blocked or skipped items
```

实际 replay 默认：

- 复用历史无副作用输出。
- 对外部写入和本地执行默认 block。
- contract drift 时标记不可直接复现。
- 需要重跑时仍必须经过 ToolRegistry 和 ActionPolicy。

Checkpoint 保存：

- run state。
- completed steps。
- pending steps。
- redacted config。
- selected context summary。
- workflow cursor 或 agent iteration cursor。
- artifact references。

Checkpoint 不保存明文 secret。

## 18. Task、Scheduler 和 Long Task Worker

通用智能体不能只响应即时聊天。它应该支持本地任务、定时任务和长期 worker。

Task 状态必须跟随真实 run 状态：

| run.status | task.status |
| --- | --- |
| `completed` | `completed` |
| `failed` | `failed` |
| `waiting_input` | `blocked` 或 waiting |
| `cancelled` | `cancelled` |
| `running` | `running` |

Scheduler 设计：

- 首版单机本地触发。
- 可由 tick/run-now/serve 驱动。
- 每次触发先创建 task，再由 AgentRunController 或 WorkflowRunner 执行。
- Scheduler run 使用 `run_mode=scheduler`。
- 默认阻断 `network_write` 和 `external_write`。

Long Task Worker：

```text
agentend serve --once
agentend serve --poll-interval 30 --max-concurrency 1
```

要求：

- 处理 due schedules、pending tasks、file inbox batches。
- 记录 heartbeat。
- 产生 progress artifact。
- 重启后能从 pending/running/blocked 恢复。
- 单个任务达到 max_iterations 或连续失败阈值后停止或 blocked。
- 首版只要求单并发，避免分布式复杂度。

## 19. Result Cache 和 Error Taxonomy

Result Cache 只能缓存明确无副作用结果。

允许缓存：

- `web.fetch`
- `web.search`
- `http.request` 且 method 为 GET/HEAD/OPTIONS

Cache key 应包含：

- tool name。
- normalized input。
- provider/config hash。
- dynamic side effect。

Cache hit 仍必须创建当前 run 的 ToolCall 和 Evidence，不能直接引用历史 run 的审计对象。

Error Taxonomy 用于让 Replanner、Eval、Replay 不再解析脆弱 stderr。

常见错误分类：

- `missing_config`
- `permission_error`
- `network_error`
- `timeout`
- `schema_error`
- `budget_exceeded`
- `external_side_effect_blocked`
- `path_boundary_violation`
- `tool_unavailable`
- `provider_error`

策略阻断必须区别于系统权限错误。例如 ActionPolicy 阻断外部写入应为 `external_side_effect_blocked`，不是普通 `permission_error`。

## 20. Eval 体系

智能体需要任务级 eval，不只是单元测试。

推荐 eval suites：

| Suite | 覆盖 |
| --- | --- |
| `smoke` | init、CLI、基础 workflow、DB。 |
| `tools-smoke` | shell、python、browser、db、im、vision、tool generator。 |
| `skills-smoke` | 内置 skill 和本地 skill draft。 |
| `runtime-hardening` | LLM fixture、Telegram MCP、HTTP side effect、path boundary、model route、evidence。 |
| `context-long` | 长上下文、预算裁剪、memory gate、policy merge。 |
| `orchestration-smoke` | AgentRunController 工具优先闭环。 |
| `tool-first` | Goal Analyzer 候选能力实际影响执行。 |
| `memory-consolidation` | 记忆候选、合并、检索和影响下一次 run。 |
| `skill-effectiveness` | skill 成功率和失败模式影响排序。 |
| `long-task-worker` | serve、task、scheduler、checkpoint/resume。 |
| `intent-routing` | 聊天、行动、缺参、高风险、prompt injection、多入口一致性。 |

每个 eval case 至少断言：

- 一个用户可见结果。
- 一个审计对象，例如 run、tool_call、intent_decision、context_ledger、evidence、memory_candidate。

否则容易出现“eval 通过但没有证明真实能力”的虚假信心。

## 21. 数据模型建议

首版可使用 SQLite，避免引入 Postgres、Redis、Neo4j、pgvector 等外部依赖。

核心表：

- conversations
- messages
- runs
- run_steps
- agent_runs
- agent_iterations
- tool_calls
- mcp_servers
- mcp_tools
- mcp_tool_calls
- workflows
- artifacts
- event_logs
- intent_decisions
- context_ledger
- memory_items
- memory_candidates
- memory_links
- memory_retrievals
- capability_effectiveness
- skill_effectiveness
- clarification_requests
- checkpoints
- tasks
- schedules
- evidence_sources
- eval_runs
- eval_cases
- storage_cleanup_runs

文件系统保存：

```text
data/
  agentend.sqlite
  artifacts/
  sandboxes/
  exports/
  logs/
  skills/
  workflows/
  memory/
  cache/
```

SQLite 保存结构化元数据，文件系统保存大产物。路径写入必须限制在允许根内。

## 22. 安全和数据边界

必须遵守的边界：

- Secret 只保存名称、来源和存在状态，不保存明文值。
- 日志、DB、export、MCP 调用、错误详情都要统一脱敏。
- 文件写入默认限制在 Agent home 或受控 artifacts 目录。
- 删除目录必须显式 recursive，并且目标不能是允许根本身。
- Shell、Git、DB、IM、Browser、Python Exec 都是高影响工具，必须依赖 ActionPolicy。
- Tool Generator 只能生成 draft，不能自动启用。
- Skill Market 远程来源默认 HITL，并支持 quarantine。
- Replay 和 Scheduler 默认阻断外部可见副作用。
- Storage cleanup 必须 dry-run -> plan -> confirm，restore 必须拒绝覆盖已有 DB/home。

## 23. 评审后收敛约束

本方案后续优化的重点不是继续扩展模块数量，而是把关键链路变成不可绕过的工程约束。以下约束应优先于便利性和局部实现速度。

### 23.1 Core Invariants

系统必须长期满足这些不变量：

1. 不允许任何工具执行绕过 ToolRegistry。
2. 不允许任何有副作用动作缺少 ActionPolicy decision。
3. 不允许任何 LLM request 缺少 ContextRuntime 和 ContextLedger。
4. 不允许没有 checkpoint 的 resume。
5. 不允许外部写入在无 preview/confirmation 且无显式 policy 授权时直接执行。
6. 不允许长期 memory 缺少 provenance、confidence 和 scope。
7. 不允许 replay 重复执行已完成的不可幂等副作用。
8. 不允许对用户展示未经脱敏的 secret、内部路径或 raw tool JSON。
9. 不允许 IntentRouter、Selector 或 Replanner 扩大 ActionPolicy 允许范围。
10. 不允许 eval 只断言最终文本而不验证至少一个审计对象。

### 23.2 职责矩阵

| 模块 | 可以决定 | 不能决定 |
| --- | --- | --- |
| IntentRouter | `intent_type`、初始风险、候选能力范围、缺参、是否建议澄清或阻断。 | 不能执行工具，不能最终放行副作用。 |
| GoalAnalyzer | goal package、success criteria、constraints、preferred outputs。 | 不能修改 ActionPolicy，不能扩大 allowed tools。 |
| Capability Selector | 在有效能力集合内排序下一步 action。 | 不能绕过 `allowed_tools`、run mode policy 或 tool contract。 |
| AgentRunController | 本轮执行哪个 action、何时 finish/continue/ask_user/fail。 | 不能直接调用 execution backend。 |
| Replanner | 基于 observation/error/evaluation 给出下一步建议。 | 不能自动执行建议，不能绕过 policy。 |
| ActionPolicy | `allow/block/require_clarification`、风险等级、确认要求。 | 不负责规划，也不负责生成 action input。 |
| ToolRegistry | contract 校验、policy 调用、backend 执行入口、tool call 审计。 | 不负责目标理解和最终用户总结。 |
| ContextRuntime | context pack、budget、trust gate、ledger。 | 不能改变目标、policy 或 tool 权限。 |
| MemoryConsolidator | memory candidate 提取、合并、降权、supersede。 | 不能直接决定下一步 action。 |

有效工具集合应按交集收敛：

```text
effective_allowed_tools =
  route_allowed_tools
  ∩ capability_policy_allowed_tools
  ∩ run_mode_allowed_tools
  ∩ user/project_policy_allowed_tools
  ∩ ActionPolicy-compatible tools
```

Selector 和 Replanner 只能在该集合内行动。

### 23.3 状态机和状态不变量

`agent_run.status` 推荐收敛为：

```text
created -> planning -> running -> waiting_input -> running -> completed
                                  -> blocked
                                  -> failed
                                  -> cancelled
                                  -> expired
```

`agent_iteration.status` 推荐收敛为：

```text
created
  -> action_selected
  -> policy_checked
  -> executing
  -> observed
  -> evaluated
  -> checkpointed
  -> completed

异常终态：
failed / skipped / blocked
```

必须满足：

- completed run 不能再追加 iteration。
- blocked run 只能进入 waiting_input、cancelled 或保持 blocked。
- waiting_input 必须有 active clarification_request。
- 每个 tool_call 必须有关联 policy decision。
- 每个 LLM call 必须有关联 context_ledger。
- 每个 resume 必须有 checkpoint 或明确标记为 safe restart。
- 不可幂等且状态为 `executing` 的工具调用，在 resume 时默认进入 clarification/manual review。

### 23.4 Capability Manifest

Tool、Workflow、Skill 应统一暴露为 capability，避免 IntentRouter 和 Selector 分别理解多套对象。

```yaml
capability:
  id: research.report
  type: tool | workflow | skill
  description: Generate a sourced research report.
  input_schema: {}
  output_schema: {}
  risk_profile:
    side_effect_upper_bound: network_read
    data_classes: [public, internal]
  required_tools:
    - web.search
    - web.fetch
  eval_status: passed
  enabled: true
  source:
    type: builtin
    version: 0.1.0
```

边界：

- Tool 是唯一可以接触 execution backend 的原子接口。
- Workflow 编排 tool、LLM、condition、human_input。
- Skill 是 capability metadata、workflow implementation、examples、eval 和 effectiveness 的包。
- Skill 不执行，Workflow 不越权，Tool 不规划，Agent 只选择。

### 23.5 ActionPolicy v2

副作用分类仍保留，但 PolicyDecision 需要加入更多工程维度：

```json
{
  "decision": "allow",
  "reason_code": "local_read_allowed",
  "risk_level": "low",
  "actor": "user",
  "channel": "cli",
  "target": "workspace",
  "data_class": "internal",
  "operation": "read",
  "idempotency": "idempotent",
  "visibility": "local",
  "reversibility": "reversible",
  "requires_preview": false,
  "requires_user_confirmation": false,
  "redactions": []
}
```

外部写入推荐默认流程：

```text
plan -> preview/diff -> user confirmation -> execute -> evidence
```

数据分级建议：

```text
public
internal
private
secret
regulated
```

典型策略：

- `secret + any external_write = block`
- `private + external_write = require_confirmation`
- `internal path + Telegram response = redact`
- `scheduler + network_write = block by default`
- `replay + non-idempotent side effect = block by default`

### 23.6 Untrusted Context Model

每个 ContextItem 应带信任和使用边界：

```json
{
  "source_type": "web",
  "trust_level": "external_untrusted",
  "allowed_use": ["answer_context", "evidence"],
  "can_override_policy": false
}
```

强制规则：

- 外部网页内容不能成为 system instruction。
- tool output 不能修改 allowed_tools。
- memory 不能提升权限。
- 用户输入不能覆盖 ActionPolicy。
- MCP/browser/email/file 等外部或用户可控内容默认是 untrusted evidence，不是 instruction。

### 23.7 Evaluator 设计

Evaluator 应同时使用规则检查和必要的 LLM 判断。优先级：

1. 结构化要求检查，例如 artifact 是否存在、tool_call 是否成功、schema 是否满足。
2. 目标类型规则，例如测试命令、来源证据、文件写入、report path。
3. ErrorTaxonomy 和 policy result。
4. 预算、迭代数、重复失败、重复 action。
5. LLM judge 只用于自然语言质量、覆盖度和难以规则化的验收。

Evaluator 输出应包含：

```json
{
  "decision": "finish | continue | replan | ask_user | fail",
  "confidence": 0.82,
  "satisfied_criteria": [],
  "missing_criteria": [],
  "evidence_refs": [],
  "unreachable_reason": null,
  "next_probe": "shell.run"
}
```

停止条件必须显式：

- `max_iterations`
- `max_same_error_count`
- `max_same_action_count`
- `max_cost`
- `max_wall_time`
- `success_criteria_satisfied`
- `requires_user_input`
- `policy_blocked`
- `goal_unreachable`

### 23.8 Memory Gate

Memory 写入和读取都需要门禁。

Memory Write Gate 决定：

- 是否值得记。
- 写入哪个 scope。
- TTL 多久。
- confidence 多少。
- 是否需要用户确认。
- 是否包含敏感信息。
- 是否来自可信来源。

初期只允许自动写入：

- 用户明确偏好。
- 项目稳定事实。
- 经过成功 run 验证的 procedure。
- tool/skill performance 统计。

初期不应自动写入：

- 模型猜测。
- 一次性任务细节。
- 外部网页事实。
- 失败中间结论。
- 低置信总结。

Memory Read Gate 决定：

- 是否适合本次任务。
- 是否过期。
- 来源是否可信。
- 能否作为强约束。
- 只能作为弱提示还是可进入 prompt。

Memory 的工程验收标准是：它必须在 eval 中证明影响了下一次 action selection 或 context selection。

### 23.9 Idempotency 和 Compensation

ToolContract 应增加恢复相关字段：

```yaml
idempotent: true
idempotency_key_supported: false
preview_supported: true
dry_run_supported: true
compensation_supported: false
compensation_hint: ""
```

不可幂等工具执行策略：

```text
create tool_call(status=executing)
  -> execute backend
  -> update completed/failed
```

如果进程在 `executing` 状态中断，resume 时不能假定失败或重试。默认进入 clarification/manual review，除非工具 contract 声明有幂等键或可验证补偿。

### 23.10 MCP Trust Profile

MCP server 会快速扩大系统能力面，因此不仅要约束 MCP tool，还要约束 MCP server。

```yaml
mcp_server_policy:
  trust_level: local_trusted | local_untrusted | remote_untrusted
  allowed_tools: []
  denied_tools: []
  max_side_effect: network_read
  requires_human_approval_for_install: true
  quarantine_until_eval_passed: true
```

远程 MCP 默认不进入可执行候选能力池。推荐流程：

```text
install
  -> manifest review
  -> schema validation
  -> policy assignment
  -> eval
  -> enable
```

### 23.11 User-Facing Response Contract

内部强治理不应暴露为高噪声用户体验。建议统一用户可见响应类型：

```text
chat_response
answer_response
task_started
progress_update
clarification_request
blocked_response
task_result
task_failed_with_next_steps
```

用户响应不得包含：

- raw tool JSON。
- 内部绝对路径，除非用户通过 CLI 本地调试明确要求。
- secret 或 token-like 字符串。
- policy object 原始结构。
- 未解释的 run/checkpoint 内部 ID。

blocked response 应说明风险、可预览内容和下一步确认方式，而不是只输出内部错误码。

## 24. 推荐实施路线

### Phase 1: Lite Runtime

目标：单机可运行。

- Python package。
- CLI。
- SQLite。
- agent profile。
- basic workflow runner。
- builtin tools。
- MCP client。
- Telegram entry。
- Linux deploy。
- run/step/tool/event audit。

完成标准：

- `init` 后可以 chat/run workflow。
- workflow 可调用 builtin tool 和 MCP tool。
- Telegram 复用同一 ConversationService。
- SQLite 可查询 run、step、tool call。

### Phase 2: Action Layer

目标：补齐行动能力和治理底座。

- Tool Contract。
- Skill Registry。
- ActionPolicy。
- ContextRuntime。
- MemoryStore。
- ResultCache。
- ErrorTaxonomy。
- EvalHarness。
- ModelRouting/CostBudget。
- Checkpoint/Resume。
- Evidence/Export。
- Task/Scheduler。
- StorageGovernance。

完成标准：

- 所有工具统一接入 ToolRegistry 和 ActionPolicy。
- ContextRuntime 能构造、预览、记录真实上下文。
- HITL 可创建澄清并 resume。
- Eval 能跑 smoke 和工具回归。

### Phase 3: Runtime Hardening

目标：修复文档承诺但真实调用链未闭合的问题。

- 真实 LLM provider。
- Telegram async -> MCP workflow。
- 动态副作用。
- 路径边界。
- workflow 复杂语义。
- model route 进入真实 LLM 调用。
- evidence 覆盖 browser/file/search。
- runtime-hardening eval。

完成标准：

- 默认测试离线可跑。
- 真实 provider 走 OpenAI-compatible adapter。
- MCP/Telegram 不受 event loop 问题影响。
- ActionPolicy 与真实 input 副作用一致。

### Phase 4: Review Remediation

目标：用代码审查验证真实链路。

重点检查：

- ContextRuntime 是否真的进入 LLM request。
- Task 状态是否跟随 run 状态。
- MCP 审计是否统一脱敏。
- OpenAI endpoint 是否正确拼接。
- 普通 chat 是否复用 Agent Runtime。
- Browser fallback 是否无未处理异步噪音。

完成标准：

- 每个审查发现有回归测试。
- 文档、实现、eval 三者一致。

### Phase 5: Agentic Orchestration

目标：从 workflow runtime 进化为目标导向 agent。

- AgentRunController。
- tool-first selector。
- evaluator。
- replanner 驱动下一轮 action。
- memory consolidator。
- skill effectiveness。
- long task worker。
- orchestration/memory/worker eval。

完成标准：

- 工具优先任务能完成并说明证据。
- 失败能 replan。
- 记忆能沉淀并影响下一次 run。
- worker 可恢复 pending/running/blocked 任务。

### Phase 6: Intent Routing

目标：统一自然语言入口。

- IntentDecision schema。
- RuleIntentClassifier。
- ModelIntentClassifier。
- capability constrained merge。
- GoalAnalyzer compatibility wrapper。
- ConversationService route orchestration。
- Telegram route orchestration。
- intent audit persistence。
- intent-routing eval。

完成标准：

- 行动类 chat 不再只进入 simple_chat。
- 缺参进入 clarification。
- 高风险进入 blocked 或 HITL。
- CLI/Telegram/Agent run/Task/Scheduler 共享同一决策链路。

## 25. 关键工程经验

这次 AgentEnd 开发过程沉淀出的核心经验：

- 文档必须成为真实验收边界，而不是宣传材料。
- 先闭合调用链，再扩能力面。
- 所有入口都应该复用同一 Orchestrator。
- Workflow 和 Agent 的职责必须分开。
- LLM 不能直接拥有执行权。
- Tool Contract 是能力系统的稳定接口。
- ActionPolicy 是副作用控制的统一入口。
- ContextRuntime 如果不进入真实 LLM request，就是假治理。
- Memory 不能保存全文历史，必须结构化、带来源、可合并、可降权。
- HITL 必须绑定 checkpoint，否则中断后无法安全恢复。
- Replay 和 Scheduler 会放大副作用，必须默认保守。
- Eval 不能只断言命令成功，还要断言审计对象。
- Telegram 等外部通道必须脱敏，不暴露内部路径、secret 或 raw tool JSON。
- 每次发现“文档完成但功能异常”，都要沿真实请求路径验证，而不是只看局部代码。

## 26. 推荐首版工程闭环清单

如果要从零实现一套通用智能体，建议首版先打穿这些工程闭环：

- CLI。
- 一个外部入口，例如 Telegram、Slack 或 HTTP API。
- SQLite + artifacts。
- Agent profile。
- WorkflowRunner。
- ToolRegistry。
- MCP client。
- ContextRuntime。
- ActionPolicy。
- IntentRouter。
- AgentRunController。
- MemoryStore。
- EvalHarness。
- Evidence/Export。
- Checkpoint/Resume。
- HITL Clarification。

首版明确不做：

- 多 Agent。
- 企业权限系统。
- 远程沙箱。
- 分布式队列。
- 自动启用生成工具。
- 无限长期全文记忆。
- 没有 eval 的远程 skill market。
- 绕过 ToolRegistry 的临时工具调用。

## 27. 总结

一套好的 AI 通用智能体架构，不应该把智能押注在一次 LLM 回复上，而应该把智能拆成可验证的工程闭环：

```text
意图判断
  -> 目标建模
  -> 能力选择
  -> 安全执行
  -> 观察记录
  -> 结果评估
  -> 重规划
  -> 记忆沉淀
  -> 持续 eval
```

AgentEnd 这次实践的价值在于：它把 LLM、工具、workflow、上下文、记忆、安全、审计、恢复和 eval 放进同一条真实调用链里。LLM 负责理解和结构化判断，执行权由 Tool Contract、Action Policy、Context Runtime、Checkpoint、Evidence 和 Eval 共同约束。

这就是它区别于普通 agent demo 的地方：不是“能聊、能调工具”，而是“能围绕目标可靠地行动，并留下可复查的工程证据”。
