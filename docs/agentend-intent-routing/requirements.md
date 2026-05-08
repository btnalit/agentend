# AgentEnd Intent Routing 需求文档

## 1. 背景

AgentEnd 的产品愿景是目标导向 Agent：用户从 CLI、Telegram、Task、Scheduler 或其他入口输入自然语言后，系统应先理解目标，再选择合适的 skill、tool、workflow 或澄清请求，并通过执行、观察、评估和重规划闭环完成任务。

当前实现已经具备 Action Layer 和 Agentic Orchestration 的主要组件，包括 Goal Analyzer、Capability Map、Agent Selector、WorkflowRunner、Context Runtime、Action Policy、HITL Clarification、Eval、Memory 和 Effectiveness Store。但真实入口链路出现了历史分叉：

- `ConversationService.handle_message()` 会调用 `GoalAnalyzer`，但固定执行 `simple_chat`，goal analysis 只进入审计结果。
- `AgentRunController.run()` 会调用 `GoalAnalyzer`、Memory search、Agent Selector、执行器和 evaluator，才真正形成目标导向循环。

本阶段命名为 **AgentEnd Intent Routing**。目标是把多入口统一到一个内部意图/目标决策层，消除“聊天入口”和“Agent run 入口”语义分叉。

## 2. 目标

- 所有用户自然语言入口共享同一套 Intent/Goal 决策链路。
- `simple_chat` 不再是普通聊天入口的硬编码执行路径，而是 intent decision 的一个候选结果。
- `GoalAnalyzer` 从关键词候选器升级为混合路由器：规则快速路径 + 结构化模型判断 + capability/skill/tool 召回。
- 高风险、缺参、歧义目标必须进入 HITL Clarification，而不是静默猜测或直接执行。
- 意图判断、候选行动、上下文、风险和最终路由都必须可审计、可测试、可回放。
- 现有 Context Runtime、Action Policy、Tool Contract、Model Routing、Cost Budget 和 Eval 链路必须继续复用，不能新增旁路。

## 3. 范围

### 3.1 必须包含

- 新增结构化 `IntentDecision` schema。
- 改造 `GoalAnalyzer` 为混合 Intent/Goal Router。
- `ConversationService` 接入统一目标路由，不再固定 `simple_chat`。
- CLI `chat`、Telegram 普通消息、`agent run` 在自然语言处理层统一。
- 保留 `simple_chat` 用于闲聊、解释型问答和低行动意图输入。
- 行动类输入进入 `AgentRunController` 或等价目标导向执行链。
- 缺参类输入创建 `ClarificationRequest`。
- 高风险行动在执行前要求 HITL 或 Action Policy 阻断。
- Selector trace 和 intent decision 都写入可审计记录。
- Intent Eval 覆盖中文/英文、多意图、缺参、高风险、prompt injection、相似工具混淆和聊天回落负例。
- 保持 Telegram `channel + external_user_id` 绑定，不能恢复到“最近 pending run”策略。

### 3.2 不包含

- 多 Agent 架构。
- 前端 Console。
- 外部队列或分布式调度。
- 新增完整审批系统。
- 自动执行不可逆或外部可见动作。
- 用模型替代 Action Policy。
- 让 generated tool 自动进入 stable 执行路径。

## 4. 核心需求

### IR1 统一入口语义

所有用户消息入口必须经过同一个意图决策层：

```text
channel input
  -> Intent/Goal Router
  -> route decision
  -> simple_chat | agent action loop | clarification | blocked
```

要求：

- `chat` 和 Telegram 普通消息不能绕过 selector。
- `agent run` 可以保留 CLI 名称，但内部应复用同一 intent decision schema。
- Conversation、Run、AgentRun 之间必须能追踪同一条用户输入的决策和执行结果。

### IR2 IntentDecision schema

`IntentDecision` 必须至少包含：

```json
{
  "schema_version": "1",
  "intent_type": "chat | answer | task | tool_action | workflow_action | skill_action | clarification | blocked",
  "goal": "...",
  "confidence": 0.0,
  "slots": {},
  "constraints": [],
  "missing_inputs": [],
  "candidate_actions": [],
  "allowed_tools": [],
  "risk_level": "low | medium | high",
  "risk_notes": [],
  "clarification_question": null,
  "routing_reason": "...",
  "source": "rule | model | fallback"
}
```

要求：

- `confidence` 低于阈值时不得直接执行高副作用工具。
- `missing_inputs` 非空时优先生成 clarification。
- `allowed_tools` 必须用于限制本轮 selector 的工具面。
- `slots` 用于保存已抽取参数，例如 path、command、topic、url、chat_id、workflow_id。
- `source` 记录使用规则、模型或 fallback 的决策来源。

### IR3 混合路由策略

路由器必须支持三层判断：

1. 高置信规则快速路径：明确闲聊、明确调研、明确代码/测试、明确文件读写、明确缺参。
2. 结构化模型判断：规则低置信、多意图、长输入、复杂中文、工具相似、风险不明时调用便宜模型。
3. 保守 fallback：模型不可用或输出非法时，只允许 `simple_chat`、`clarification` 或低风险只读能力。

要求：

- 规则结果不能绕过 schema。
- 模型输出必须校验 schema。
- 模型只给意图和候选，不直接执行工具。
- Provider 不可用时必须有 deterministic fallback。

### IR4 目标导向执行收口

行动类 intent 必须进入目标导向闭环：

```text
IntentDecision
  -> AgentRunController
  -> AgentSelector
  -> ToolRegistry / Skill / WorkflowRunner
  -> Observation
  -> Evaluator
  -> continue | finish | clarification | failed
```

要求：

- `simple_chat` 是 `intent_type=chat/answer` 的执行结果，不是入口默认。
- Agent selector 读取 `candidate_actions`、`allowed_tools`、`slots`、`constraints`。
- 执行后 evaluator 判断目标完成，不只判断非空输出。
- 未完成时继续走已有 iteration 和 effectiveness 机制。

### IR5 Context 和 Memory 治理

Intent Router 必须复用 Context Runtime 和 Memory Store：

- workspace summary 可进入 intent 决策上下文。
- project/user/task memory 可按 policy 检索。
- 低置信、过期、不可信来源 memory 不得作为强约束。
- Intent 决策时不能把完整大文件、完整网页或原始工具输出直接塞入 prompt。
- Intent 决策输入必须可 preview 或可通过审计记录复查摘要。

### IR6 HITL 和风险控制

以下情况必须创建 clarification 或 block：

- 必要参数缺失，例如文件路径、目标 chat、外部账号、URL、确认对象。
- 用户目标多义，继续执行会偏离明显意图。
- 即将执行 `local_write`、`local_execute`、`network_write`、`external_write`，且 intent confidence 不足。
- 输入中出现明显 prompt injection 或要求绕过策略。

Action Policy 仍是工具执行前最终策略层。Intent Router 只能收紧工具面，不能放宽 Action Policy。

### IR7 可观测性

每次决策必须可审计：

- intent decision。
- 输入摘要和上下文来源。
- candidate action 列表。
- 被拒绝候选及原因。
- 最终 route。
- 是否触发 clarification。
- 是否调用模型和模型 route。
- schema 校验错误或 fallback 原因。

### IR8 Eval 覆盖

新增 `intent-routing` eval suite，至少覆盖：

- 中文闲聊回落 `simple_chat`。
- 英文闲聊回落 `simple_chat`。
- 中文调研命中 `research.report` 或 `web.search/web.fetch`。
- 代码/测试任务命中 `code.local_task`、`shell.run` 但不直接执行危险命令。
- 文件读取抽取 path slot。
- 文件写入缺 path 或内容时创建 clarification。
- 多意图输入拆分或要求澄清。
- prompt injection 不进入高风险工具。
- provider 不可用时 deterministic fallback。
- Telegram pending clarification 仍按 `channel + external_user_id` 绑定。

## 5. 非功能要求

- 保持单机 SQLite 架构。
- 不保存明文 secret。
- 不引入外部队列。
- 不新增未审计工具执行旁路。
- 新增模型调用必须进入 Model Routing 和 Cost Usage。
- 所有用户可见输出必须避免泄露内部路径、原始 tool JSON 和 secret。
- 当前 `agentend workflows run simple_chat` smoke 行为应保持可用。
- 当前 `agentend agent run` 目标导向行为应保持可用，并逐步成为统一入口的底层能力。

## 6. 成功标准

- 同一句行动类输入通过 `chat` 和 `agent run` 得到同一类 intent decision。
- 闲聊输入通过 `chat` 仍得到普通 assistant 回复。
- 行动类输入通过 `chat` 不再固定进入 `simple_chat`。
- 缺参输入创建可恢复 clarification。
- 高风险输入不因模型误判绕过 Action Policy。
- `intent-routing` eval 通过。
- `context-smoke`、`runtime-hardening`、现有 agent selector 测试继续通过。
