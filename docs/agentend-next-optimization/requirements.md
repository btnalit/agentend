# AgentEnd Next Optimization 需求文档

## 1. 背景

AgentEnd 已经完成 Lite、Action Layer、Runtime Hardening、Review Remediation、Agentic Orchestration 和 Intent Routing 的主要建设。当前系统已经具备 CLI、Telegram、SQLite、WorkflowRunner、ToolRegistry、ActionPolicy、ContextRuntime、Memory、Eval、AgentRunController、IntentRouter、Evidence、Replay、Scheduler 和 Storage Governance 等基础能力。

本阶段命名为 **AgentEnd Next Optimization**。它不是继续扩大工具面，也不是引入多 Agent 或企业级治理后台，而是把现有关键运行链路进一步收敛为更硬的工程规格：

```text
Runtime Integrity
  -> State Machine
  -> Policy v2
  -> Trusted Context
  -> Evaluator hardening
  -> Memory gate
  -> Idempotent resume/replay
  -> Invariant eval
```

当前主要问题已经不是“有没有模块”，而是：

- 状态字段存在，但状态机和非法流转检查不集中。
- `ActionPolicyDecision` 记录字段偏少，无法表达 target、data_class、idempotency、visibility、confirmation。
- `ContextItem` 只有 `item_type/source/summary`，缺少 trust level 和 allowed use。
- Evaluator 已有规则化雏形，但不可达、重复失败、重复 action、强制停止条件还不够系统。
- MCP server 缺少 server-level trust profile。
- Replay/resume 缺少 ToolContract 层面的幂等性和 preview/dry-run/compensation 元数据。
- Eval suite 已经丰富，但还缺一个专门断言 runtime invariants 的小黄金集。

## 2. 目标

本阶段目标是把现有 AgentEnd 从“能力完整”推进为“关键链路不可绕过”：

- 所有工具执行不可绕过 ToolRegistry。
- 所有副作用不可绕过 ActionPolicy。
- 所有 LLM 调用不可绕过 ContextRuntime 和 ContextLedger。
- 所有 resume 不可绕过 checkpoint 或 safe restart 标记。
- 所有外部写入必须 preview/confirm，除非项目策略显式允许。
- 所有长期 memory 必须有 provenance、confidence、scope 和 gate decision。
- 所有 eval 必须证明真实调用链，而不是只验证最终文本。

## 3. 范围

### 3.1 必须包含

- Core Invariants 和集中状态机。
- Runtime invariant checker 和 `runtime-invariants` eval suite。
- ActionPolicy v2 的扩展 decision 结构。
- ContextItem trust metadata 和 untrusted context gate。
- Evaluator stop condition 和 goal unreachable 判断。
- Capability Manifest 收敛，减少 IntentRouter、GoalAnalyzer、Selector 对 tool/workflow/skill 的重复理解。
- Memory Write Gate 和 Memory Read Gate。
- ToolContract 幂等性、preview、dry-run、compensation 元数据。
- MCP server trust profile。
- 对上述能力的自动化测试、eval 和审计文档回填。

### 3.2 不包含

- 多 Agent 或 sub-agent 编排。
- 企业权限后台。
- 远程沙箱。
- Postgres 迁移。
- 分布式队列。
- 自动晋升 episode-generated skill。
- 远程 MCP 自动启用。
- 大规模 CLI/UX 重做。
- 新的大型工具面。

## 4. 核心需求

### NO1 Core Invariants 和状态机

必须定义集中状态常量或 enum-like 模块，覆盖：

- `AgentRunStatus`
- `AgentIterationStatus`
- `RunStatus`
- `RunStepStatus`
- `ToolCallStatus`
- `ClarificationStatus`

必须实现非法流转检查，至少覆盖：

- completed agent run 不允许追加 iteration。
- waiting_input 必须存在 pending clarification request。
- blocked run 不允许直接 resume 到 running，除非有确认或策略变更。
- 每个 tool call 必须有关联 policy decision。
- 每个 LLM call 必须有关联 context ledger。
- 每个 resume 必须有 checkpoint 或 safe restart 标记。

### NO2 Runtime Invariant Eval

必须新增 `runtime-invariants` eval suite，作为后续优化的第一道回归门。

首批 case 至少覆盖：

- tool call has policy decision。
- LLM call has context ledger。
- scheduler network_write blocked。
- replay non-idempotent side effect not rerun。
- waiting_input has clarification request。
- completed agent run cannot append iteration on resume。

M0 说明：首轮先落地 tool policy link、LLM context ledger link、waiting_input clarification link、completed resume stable 四个本地确定性 case；scheduler、replay 等副作用 case 分别随 O46 和 O51 扩展。

每个 case 必须断言：

- 用户可见结果。
- run/agent_run status。
- 至少一个审计对象。
- 相关 event、ledger 或 policy decision。

### NO3 ActionPolicy v2

必须扩展 ActionPolicy 的语义和审计信息。Policy decision 至少表达：

- `decision`
- `reason_code`
- `risk_level`
- `actor`
- `channel`
- `target`
- `data_class`
- `operation`
- `idempotency`
- `visibility`
- `reversibility`
- `requires_preview`
- `requires_user_confirmation`
- `redactions`

策略必须支持：

- `external_write` 默认 confirmation 或 block。
- `secret + external_write` 直接 block。
- `scheduler + network_write/external_write` block。
- `replay + non-idempotent side effect` block。
- `telegram` 输出强制 redaction。
- `local_write` 按 artifacts、workspace、config、secret file、delete 区分风险。

M0 说明：首轮已用 v2 event payload 落地 reason_code/risk/context 语义，覆盖 scheduler network_write block、secret external_write block、external_write dry-run allow、external_write require_clarification。更细的 local_write target 分类和 telegram 输出策略留给后续切片。

### NO4 Trusted Context Runtime

`ContextItem` 必须增加 trust metadata：

```json
{
  "source_type": "web | file | user | system | tool | memory | workflow | mcp",
  "trust_level": "trusted | user_controlled | external_untrusted | generated",
  "allowed_use": ["instruction", "answer_context", "evidence", "not_instruction"],
  "can_override_policy": false
}
```

要求：

- 外部网页、browser、MCP、file、tool output 默认不能作为 instruction。
- memory 只有通过 Read Gate 后才能进入 context。
- untrusted context 只能作为 evidence/context，不得影响 allowed_tools 或 ActionPolicy。
- ContextLedger 必须记录 trust metadata 和 dropped reason。

M0 说明：首轮已完成 `ContextItem` trust metadata 和 selected/dropped ledger 持久化；后续 hardening 已完成 system instruction 与 untrusted/generated context 的 message 层隔离，并把 prompt injection adversarial fixture 纳入 `runtime-invariants` eval。更细的 Read Gate 展示继续放入后续 O50 切片。

### NO5 Evaluator 和 Stop Conditions

Evaluator 必须输出结构化 decision：

```text
finish | continue | replan | ask_user | fail
```

必须支持停止条件：

- `max_iterations`
- `max_same_error_count`
- `max_same_action_count`
- `max_cost`
- `max_wall_time`
- `success_criteria_satisfied`
- `requires_user_input`
- `policy_blocked`
- `goal_unreachable`

Evaluator 必须优先使用 deterministic checks，例如 artifact、tool status、schema、source evidence、test command evidence；LLM judge 只作为自然语言质量和覆盖度的补充。

M0 说明：首轮已修复 deterministic `test_command_evidence` 从 evaluator missing requirement 到 selector next probe 的链路；完整 `finish/continue/replan/ask_user/fail` decision schema、重复失败和 goal_unreachable 仍留给后续 O48 切片。

### NO6 Capability Manifest 收敛

Tool、Workflow、Skill、Generated Draft 必须统一暴露为 capability summary。

Capability 至少包含：

- id
- type
- description
- input_schema
- output_schema
- side_effect_upper_bound
- risk_profile
- required_tools
- eval_status
- policy_tags
- enabled
- version/source

要求：

- IntentRouter 只召回 capability summary。
- Selector 只在 effective allowed capability 集合内排序。
- GoalAnalyzer 逐步降级为 compatibility wrapper。
- Generated draft 不进入 executable pool。

M0 说明：首轮已新增 `capability_manifest()` 与 `query_executable_capabilities()`，统一 manifest 先落在 `Capability.example_json.manifest`。`GoalAnalyzer` 已输出 `candidate_capabilities` 和 `allowed_capabilities`，并只消费 executable capability；Selector 已能从 `candidate_capabilities` 召回候选并用 `allowed_capabilities` 拒绝越权 skill/tool。generated draft 保留展示但 `executable=false`。

### NO7 Memory Gate

必须新增 Memory Write Gate 和 Memory Read Gate。

Write Gate 自动允许的类型：

- 用户明确偏好。
- 项目稳定事实。
- 成功 run 验证过的 procedure。
- tool/skill performance 统计。

Write Gate 自动拒绝或进入 review 的类型：

- 模型猜测。
- 一次性任务细节。
- 外部网页事实。
- 失败中间结论。
- 低置信总结。
- 含 secret/private data 的候选。

Read Gate 必须决定 memory 是否能作为 strong context、weak hint 或 dropped item。

M0 说明：首轮已落地 `memory_gate.py`，集中输出 Memory Write/Read Gate decision；`write_memory_item()` 已记录 `memory.write_gate_decided`，context memory drop 已统一走 Read Gate。低置信 manual memory 暂不禁止写入，而是在读取进入 context 时 drop，以保持现有 CLI 行为兼容。

### NO8 Idempotent Resume 和 Replay Safety

ToolContract 必须增加：

- `idempotent`
- `idempotency_key_supported`
- `preview_supported`
- `dry_run_supported`
- `compensation_supported`
- `compensation_hint`

要求：

- 不可幂等工具执行前必须先创建 executing 或等价 pending 记录。
- resume 遇到 `executing + non-idempotent` 默认进入 clarification/manual review。
- replay 默认复用历史输出，不重跑不可幂等副作用。
- contract drift 时 replay plan 标记 blocked/skip。

M0 说明：首轮已落地 ToolContract 幂等性 metadata 和 replay plan 的 `idempotency/replay_action` 标记。后续 hardening 已补齐 `running/executing + non-idempotent` AgentRun resume manual review；`uncertain` lifecycle 仍留给后续 schema 迁移。

### NO9 MCP Trust Profile

MCP server 必须有 server-level trust profile：

```yaml
mcp_server_policy:
  trust_level: local_trusted | local_untrusted | remote_untrusted
  allowed_tools: []
  denied_tools: []
  max_side_effect: network_read
  requires_human_approval_for_install: true
  quarantine_until_eval_passed: true
```

远程 MCP 默认不进入可执行候选能力池。必须经过 install、manifest review、schema validation、policy assignment、eval、enable。

## 5. 非功能需求

- 不引入真实外部网络依赖到默认测试。
- 不破坏现有 `runtime-hardening`、`intent-routing`、`orchestration-smoke`、`memory-consolidation`、`long-task-worker`。
- 新增 schema 字段优先兼容旧 DB，可先使用 JSON 扩展字段，再做列迁移。
- 所有 secret、token-like 字符串、home path、raw tool JSON 输出必须继续脱敏。
- 所有新行为必须有自动化测试或 eval case。
- 每个切片都必须保持 `git diff --check` 通过。

## 6. 成功标准

- `runtime-invariants` eval 可运行，并能证明关键链路不可绕过。
- 状态机 helper 覆盖 AgentRun、AgentIteration、ToolCall、Clarification 的关键非法流转。
- ActionPolicy 能表达风险、确认、数据分级、幂等性要求。
- ContextRuntime 能区分 trusted instruction 和 untrusted evidence。
- Evaluator 能处理完成、继续、重规划、不可达、需要用户输入和策略阻断。
- Memory Gate 能阻止低质量 memory 污染强上下文。
- Replay/resume 不重复执行不可幂等副作用。
- 后续优化有明确 taskboard 和审计矩阵。
