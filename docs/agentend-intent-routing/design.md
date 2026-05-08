# AgentEnd Intent Routing 设计文档

## 1. 设计目标

本阶段把 AgentEnd 的自然语言入口统一成目标导向路由。外部入口可以继续存在多个，但内部必须共享同一条决策链路：

```text
CLI chat / Telegram / Agent run / Task / Scheduler
    -> IntentRouter
    -> IntentDecision
    -> Conversation Orchestrator
    -> simple_chat | AgentRunController | Clarification | Blocked
```

设计原则：

- 不删除 `WorkflowRunner`，它继续负责确定性 workflow DAG 执行。
- 不删除 `AgentRunController`，它继续负责目标导向执行循环。
- 不让模型直接执行动作；模型最多产出结构化 intent decision。
- `simple_chat` 只作为低风险聊天/问答 intent 的路由结果。
- Action Policy 保持工具执行前最终安全层。
- 所有新增决策进入审计、eval 和 replay/export 链路。

## 2. 当前分叉

当前实现中存在两条语义路径：

```text
ConversationService.handle_message
  -> analyze_goal
  -> WorkflowRunner(simple_chat)
  -> run.result_json.goal_analysis
```

这条路径会分析目标，但不使用目标分析结果做路由。

```text
AgentRunController.run
  -> analyze_goal
  -> memory search
  -> select_next_action_with_trace
  -> execute skill/tool/workflow
  -> evaluate
  -> continue/finish
```

这条路径才是真正目标导向闭环。

本阶段不是保留这两套逻辑，而是把第一条入口接入第二条能力，并在前面加一层更明确的 `IntentRouter`。

## 3. 总体架构

```text
User Channel
  -> Channel Adapter
     - CLI chat
     - Telegram text
     - Agent run
     - Task/Scheduler natural language goal
  -> Conversation Orchestrator
  -> IntentRouter
     - RuleIntentClassifier
     - Capability Retriever
     - ModelIntentClassifier
     - IntentDecision Validator
  -> Route Executor
     - simple_chat workflow
     - AgentRunController
     - HITL Clarification
     - blocked response
  -> Audit/Event/Eval
```

### 3.1 Channel Adapter

Channel Adapter 只负责通道绑定和输入归一化：

- `channel`
- `external_user_id`
- `conversation_id`
- `text`
- `source`
- `run_mode`

Telegram 继续使用 `chat_id:user_id` 作为 `external_user_id`。通道层不能自行选择 workflow 或工具。

### 3.2 Conversation Orchestrator

新增或改造一个薄服务，承接原 `ConversationService` 的职责：

```text
receive message
  -> create/load conversation
  -> persist user message
  -> call IntentRouter
  -> execute route
  -> persist assistant message or clarification prompt
```

它是聊天入口和目标执行入口的汇合点，不做规则分类细节。

### 3.3 IntentRouter

IntentRouter 是 `GoalAnalyzer` 的升级形态。首版可以放在 `agentend.core.intent_router`，并让 `goal_analyzer` 保留兼容 wrapper。

输入：

```json
{
  "text": "...",
  "channel": "cli",
  "external_user_id": "local",
  "conversation_id": "...",
  "recent_messages": [],
  "workspace_summary": [],
  "available_capabilities": [],
  "context_policy": {}
}
```

输出：

```json
{
  "schema_version": "1",
  "intent_type": "task",
  "goal": "...",
  "confidence": 0.86,
  "slots": {"topic": "浏览器自动化工具"},
  "constraints": [],
  "missing_inputs": [],
  "candidate_actions": [
    {"type": "skill_run", "name": "research.report", "score": 0.91},
    {"type": "tool_call", "name": "web.search", "score": 0.82}
  ],
  "allowed_tools": ["web.search", "web.fetch"],
  "risk_level": "low",
  "risk_notes": [],
  "clarification_question": null,
  "routing_reason": "Research terms and capability match.",
  "source": "rule"
}
```

## 4. IntentDecision 类型

### 4.1 `chat`

闲聊、寒暄、无需项目上下文的普通对话。执行：

```text
WorkflowRunner(simple_chat)
```

### 4.2 `answer`

可直接回答的低风险问答。执行：

```text
simple_chat with context pack
```

如果需要项目事实，Context Runtime 应注入 workspace/profile/memory 摘要。

### 4.3 `task`

需要目标导向执行的任务。执行：

```text
AgentRunController.run(goal, intent_decision=...)
```

首版可先把 intent decision 放入 goal analysis 兼容结构，再由 selector 消费。

### 4.4 `tool_action` / `skill_action` / `workflow_action`

高置信单步能力调用。仍建议通过 AgentRunController 执行，以保留 evaluator、effectiveness 和 progress artifact。

### 4.5 `clarification`

缺参、多义或风险不明确。执行：

```text
create ClarificationRequest
run.status = waiting_input
```

### 4.6 `blocked`

明显越权、危险、违反 policy 或 prompt injection。执行：

```text
record decision
return safe summary
```

## 5. 混合分类流程

```text
normalize input
  -> load context summary and capabilities
  -> run deterministic rules
  -> if high confidence and low risk: validate and return
  -> if ambiguous/complex/low confidence: call model classifier
  -> validate model output
  -> merge with capability constraints
  -> enforce policy gates
  -> return IntentDecision
```

### 5.1 规则快速路径

规则适合明确输入：

- 闲聊：你好、hello、谢谢、你是谁。
- 调研：调研、搜索、查找、research、report。
- 代码：测试、pytest、修复代码、bug、review。
- 文件：读取、列出目录、写入文件。
- 缺参：发送消息但没有对象、写文件但没有路径或内容。

规则输出也必须走 schema validator。

### 5.2 模型结构化判断

触发条件：

- 多意图。
- 中英文混合复杂任务。
- 能力相似，例如 `fs.read_text` 与 `workspace.summary`。
- 输入很长。
- 规则候选分数接近。
- 缺参和高风险判断不确定。

模型 route 使用 `model_routes.intent_classify`。如果该 route 不存在，首版回退 `goal_analyze` 或便宜默认模型。

模型 prompt 必须只包含摘要：

- 用户输入。
- 最近消息摘要。
- workspace summary 摘要。
- capability name/description/side_effect/input summary。
- policy 摘要。

不得把完整文件、完整网页、原始 tool output 或 secret 放入 intent prompt。

### 5.3 校验和 fallback

校验失败时：

- 记录 `intent.validation_failed`。
- 如果输入低风险，回落 `simple_chat`。
- 如果输入涉及写入、执行、外部发送，回落 `clarification` 或 `blocked`。

## 6. 与现有模块集成

### 6.1 GoalAnalyzer 兼容层

`analyze_goal()` 保留，内部调用 IntentRouter，然后输出旧结构：

```json
{
  "goal": "...",
  "constraints": [],
  "requirements": [],
  "candidate_skills": [],
  "candidate_tools": [],
  "candidate_workflows": [],
  "missing_inputs": [],
  "risk_notes": [],
  "intent_decision": {}
}
```

这样现有 `AgentRunController` 和测试可以分阶段迁移。

### 6.2 AgentSelector

Selector 增强点：

- 读取 `intent_decision.candidate_actions` 作为优先候选。
- 用 `allowed_tools` 收紧工具集合。
- 用 `slots` 生成更准确的 tool input。
- `risk_level=high` 只作为前置治理和审计信号；只有 `intent_type=blocked`、`intent_type=clarification` 或 `missing_inputs` 会在 AgentRun 前置 gate 拦截。由 capability guardrail 排除的高副作用工具不能进入 `allowed_tools`，但普通 code/research skill 仍可继续走 AgentRun。
- trace 中记录 intent influence。

### 6.3 ConversationService

改造后：

```text
handle_message
  -> persist message
  -> intent = IntentRouter.decide(...)
  -> if chat/answer: run simple_chat
  -> if task/action: AgentRunController.run(...)
  -> if clarification: create request
  -> if blocked: return blocked summary
```

旧行为只保留为兼容 fallback，不作为默认主路径。

### 6.4 WorkflowRunner 和 ContextRuntime

WorkflowRunner 不需要理解 intent。它继续执行 workflow。IntentDecision 只通过 workflow input、goal analysis 或 context item 进入 LLM step。

ContextRuntime 新增可选 `intent_decision` context item，用于让后续 LLM step 知道当前目标、约束和已确认 slots。

### 6.5 HITL Clarification

IntentRouter 可直接创建 clarification request，或返回 `intent_type=clarification` 交给 Orchestrator 创建。首选后者，避免 router 产生 DB 副作用，便于测试。

### 6.6 Action Policy

Action Policy 不变。IntentRouter 只能：

- 限制候选工具。
- 标记风险。
- 触发 clarification。
- 返回 blocked。

它不能把被 Action Policy 阻断的 side effect 改成 allow。

## 7. 数据和审计

建议新增表：

```text
intent_decisions
  id
  conversation_id
  run_id
  agent_run_id
  channel
  external_user_id
  input_hash
  schema_version
  intent_type
  confidence
  risk_level
  source
  decision_json
  context_summary_json
  model_provider
  model_model
  created_at
```

如果首版不新增表，可以先把 intent decision 写入：

- `Run.result_json.intent_decision`
- `AgentIteration.plan_json.intent_decision`
- `EventLog` 的 `intent.decided`

但 taskboard 应把独立表作为稳定目标，因为 eval、audit、export 需要查询。

## 8. 错误处理

- 模型 provider 缺失：回落 deterministic rules，并记录 warning。
- schema 校验失败：保守回落 chat/clarification/blocked。
- capability map 为空：刷新 capabilities；仍为空则只允许 `simple_chat` 和 clarification。
- slot 抽取不完整：clarification。
- 高风险低置信：clarification 或 blocked。
- Action Policy 阻断：按现有 tool failure 和 replan 链路处理。

## 9. 测试策略

测试分三层：

1. Unit：IntentDecision schema、规则分类、slot 抽取、schema fallback。
2. Integration：`chat` 与 `agent run` 对相同行动输入产生一致 intent route。
3. Eval：`intent-routing` suite 覆盖行为回归。

关键验收命令：

```bash
agentend intent decide "帮我调研浏览器自动化工具"
agentend chat --message "帮我调研浏览器自动化工具"
agentend agent run "帮我调研浏览器自动化工具"
agentend eval run intent-routing
```

现有回归：

```bash
python -m pytest tests/test_phase_e_planning_episode.py tests/test_agent_selector_trace.py tests/test_llm_agent_cli.py -q
python -m pytest tests/test_phase_g_hitl_resume_replay.py tests/test_phase_q_telegram_multi_user.py -q
```

## 10. 迁移策略

阶段化迁移：

1. 先新增 IntentDecision 和 IntentRouter，不改变外部行为。
2. `GoalAnalyzer` wrapper 接入 IntentRouter，保持旧字段。
3. AgentSelector 消费 intent decision。
4. ConversationService 改为通过 Orchestrator 执行 route。
5. Telegram 普通消息改用同一 Orchestrator。
6. 新增 intent eval 并纳入 smoke/runtime-hardening。

这样可以避免一次性重写入口导致 CLI、Telegram、workflow smoke 同时回归。
