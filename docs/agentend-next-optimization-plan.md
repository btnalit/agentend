# AgentEnd 后续优化工程方案

## 1. 目标

本文面向当前 `D:\agentend` 项目的后续工程优化。目标不是继续扩展新工具或重写架构，而是把现有 AgentEnd 的关键运行链路进一步收敛成可验证、不可绕过、可恢复的工程规格。

下一阶段优化主线：

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

优先级判断：

- 先补硬约束，再扩能力面。
- 先让已有链路不可绕过，再做更复杂智能。
- 先用 eval 证明真实调用链，再增加高级策略。
- 先解决状态、策略、上下文、幂等性，再考虑多 Agent 或更复杂 worker。

## 2. 当前项目事实

基于当前代码抽查，AgentEnd 已经具备这些基础：

- `AgentRun`、`AgentIteration`、`Run`、`RunStep`、`ToolCall`、`ActionPolicyDecision`、`ContextLedger`、`Checkpoint`、`ClarificationRequest`、`EventLog` 等核心表已经存在。
- `ConversationService` 已把行动类 intent 路由到 `AgentRunController`，闲聊/低风险问答仍走 `simple_chat`。
- `IntentRouter` 已能输出 `IntentDecision`，并对高副作用、disabled tool、generated draft 做能力约束。
- `AgentRunController` 已有目标循环、iteration 记录、progress artifact、clarification gate、blocked gate、resume 基础。
- `ActionPolicy` 已经是工具执行前的统一入口，但当前主要基于 `run_mode + side_effect` 判断。
- `ContextRuntime` 已能构造 `ContextPack(selected, dropped)`，并保留 prompt 项，记录 dropped reason。
- `MemoryConsolidator` 已有 candidate、merge、supersede、needs_review、MemoryUseEvent 等质量基础。
- `EvalHarness` 已有 `runtime-hardening`、`intent-routing`、`orchestration-smoke`、`memory-consolidation`、`long-task-worker`、`context-long` 等 suite。

当前最值得继续优化的不是“有没有模块”，而是这些模块的约束还不够硬：

- 状态字段存在，但状态机和非法流转检查还不集中。
- `ActionPolicyDecision` 记录字段偏少，无法表达 target、data_class、idempotency、visibility、confirmation。
- `ContextItem` 只有 `item_type/source/summary`，还没有 trust level 和 allowed use。
- Evaluator 已有规则化要求，但不可达、重复失败、重复 action、强制停止条件还需要系统化。
- MCP server 目前有连接和 tool 管理，但缺少 server-level trust profile。
- Replay/resume 依赖 checkpoint 和 policy，但 ToolContract 还缺幂等性和 preview/dry-run/compensation 元数据。
- Eval suite 很多，但还需要一组专门断言 invariants 的黄金集。

## 3. 优化原则

下一阶段不建议做：

- 多 Agent。
- 企业权限后台。
- 远程沙箱。
- 分布式队列。
- 自动启用生成工具。
- 更复杂的远程 Skill Market。
- 新的大型工具面。

下一阶段建议聚焦：

- 所有执行不可绕过 ToolRegistry。
- 所有副作用不可绕过 ActionPolicy。
- 所有 LLM 调用不可绕过 ContextRuntime。
- 所有 resume 不可绕过 checkpoint。
- 所有外部写入必须 preview/confirm 或显式 policy allow。
- 所有长期 memory 必须有 provenance/confidence/gate。
- 所有 eval 必须证明真实调用链。

## 4. O45-O52 优化切片

### O45 Core Invariants 和状态机

目标：把运行时关键状态和不可绕过规则落成代码级约束。

范围：

- 新增集中状态常量或 enum-like 模块，例如 `core/runtime_states.py`。
- 定义 `AgentRunStatus`、`AgentIterationStatus`、`RunStatus`、`ToolCallStatus`、`ClarificationStatus`。
- 新增状态流转校验 helper。
- 在 `AgentRunController.run()`、`resume()`、clarification gate、blocked gate 中使用 helper。
- 增加 invariant checker，用于 eval 和 CLI debug。

建议状态：

```text
AgentRun:
created/planning/running/waiting_input/blocked/completed/failed/cancelled/expired

AgentIteration:
created/action_selected/policy_checked/executing/observed/evaluated/checkpointed/completed/failed/skipped/blocked
```

核心 invariants：

- completed agent_run 不允许追加 iteration。
- waiting_input 必须存在 pending clarification。
- blocked run 不允许直接 resume 到 running，除非有确认或策略变更。
- 每个 tool_call 必须有关联 policy decision。
- 每个 LLM call 必须有关联 context ledger。
- 每个 resume 必须有 checkpoint 或 safe restart 标记。

涉及文件：

- `src/agentend/db/models.py`
- `src/agentend/core/agent_run.py`
- `src/agentend/core/workflow_runner.py`
- `src/agentend/core/run_control.py` 或新增 `src/agentend/core/runtime_states.py`
- `tests/test_agent_run_controller.py`
- 新增 `tests/test_runtime_invariants.py`

验收：

```powershell
.venv\Scripts\python.exe -m pytest tests\test_agent_run_controller.py tests\test_intent_routing.py tests\test_runtime_invariants.py -q --basetemp=.tmp\o45-runtime-invariants -p no:cacheprovider
```

### O46 ActionPolicy v2

目标：把当前 `run_mode + side_effect` 的策略扩展为可解释、可确认、可审计的 policy decision。

新增 PolicyDecision 字段：

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
- `redactions_json`

建议先兼容老表：字段可先放入 `decision_json`，再做迁移列扩展，避免一次性大迁移。

策略增强：

- `external_write` 默认 `require_clarification` 或 block。
- `secret + external_write` 直接 block。
- `scheduler + network_write/external_write` block。
- `replay + local_write/local_execute/network_write/external_write` block。
- `telegram` 输出强制 redaction。
- `local_write` 按 target 区分 artifacts、workspace、config、secret file、delete。

涉及文件：

- `src/agentend/core/action_policy.py`
- `src/agentend/core/tool_registry.py`
- `src/agentend/core/tool_contracts.py`
- `src/agentend/core/errors.py`
- `src/agentend/db/models.py`
- `tests/test_phase_c_local_action_tools.py`
- `tests/test_phase_o_scheduler_inbox_reliability.py`
- 新增 `tests/test_action_policy_v2.py`

验收：

```powershell
.venv\Scripts\python.exe -m pytest tests\test_action_policy_v2.py tests\test_phase_c_local_action_tools.py tests\test_phase_o_scheduler_inbox_reliability.py -q --basetemp=.tmp\o46-policy-v2 -p no:cacheprovider
```

### O47 Trusted Context Runtime

目标：给 ContextItem 加入信任边界，防止外部内容、tool output、memory 或用户输入提升为指令。

新增字段：

```json
{
  "source_type": "web | file | user | system | tool | memory | workflow | mcp",
  "trust_level": "trusted | user_controlled | external_untrusted | generated",
  "allowed_use": ["instruction", "answer_context", "evidence", "not_instruction"],
  "can_override_policy": false
}
```

行为要求：

- `system`、agent profile、project policy 才能进入 instruction use。
- web/browser/MCP/file/tool output 默认 `external_untrusted` 或 `generated`。
- memory 只有通过 Read Gate 后才能进入 context。
- untrusted context 只能作为 evidence/context，不得影响 allowed_tools 或 policy。
- ledger 记录 trust metadata 和 dropped reason。

涉及文件：

- `src/agentend/core/context_runtime.py`
- `src/agentend/core/context_policy.py`
- `src/agentend/core/workflow_runner.py`
- `src/agentend/core/intent_router.py`
- `src/agentend/db/models.py`
- `tests/test_phase_n_context_policy_budget.py`
- 新增 `tests/test_trusted_context_runtime.py`

验收：

```powershell
.venv\Scripts\python.exe -m pytest tests\test_phase_n_context_policy_budget.py tests\test_trusted_context_runtime.py tests\test_llm_agent_cli.py -q --basetemp=.tmp\o47-trusted-context -p no:cacheprovider
```

### O48 Evaluator 和 Stop Conditions 硬化

目标：让 AgentRunController 不只靠“observation completed + 文本非空”判断完成，而是明确处理不可达、重复失败、重复 action、预算和用户输入。

新增能力：

- 结构化 evaluator decision：`finish/continue/replan/ask_user/fail`。
- `max_same_error_count`。
- `max_same_action_count`。
- `goal_unreachable`。
- `policy_blocked` 停止。
- 接近 iteration 上限时输出 remaining risk 和 next steps。
- 对 code/test/research/file/report 目标增加更多 deterministic requirement。

建议 evaluator 分层：

1. artifact/tool/schema 规则检查。
2. goal requirement 检查。
3. error taxonomy 检查。
4. repeat failure/action 检查。
5. LLM judge 作为可选补充。

涉及文件：

- `src/agentend/core/agent_evaluator.py`
- `src/agentend/core/agent_run.py`
- `src/agentend/core/agent_selector.py`
- `tests/test_agent_goal_evaluator.py`
- `tests/test_agent_run_controller.py`
- 新增 `tests/test_agent_stop_conditions.py`

验收：

```powershell
.venv\Scripts\python.exe -m pytest tests\test_agent_goal_evaluator.py tests\test_agent_run_controller.py tests\test_agent_stop_conditions.py -q --basetemp=.tmp\o48-evaluator -p no:cacheprovider
```

### O49 Capability Manifest 收敛

目标：让 tool、workflow、skill、generated draft 统一暴露为 capability，减少 IntentRouter、GoalAnalyzer、Selector 的重复理解。

当前 `core/capabilities.py` 已有能力索引，但字段还偏轻。下一步增强：

- capability type：tool/workflow/skill/generated。
- `side_effect_upper_bound`。
- `risk_profile_json`。
- `eval_status`。
- `policy_tags`。
- `required_tools_json`。
- `version`。
- generated draft 默认不进入 executable pool。

行为要求：

- IntentRouter 只召回 capability summary。
- Selector 只在 effective allowed capability 集合中排序。
- GoalAnalyzer 逐步降级为 compatibility wrapper。

涉及文件：

- `src/agentend/core/capabilities.py`
- `src/agentend/core/intent_router.py`
- `src/agentend/core/goal_analyzer.py`
- `src/agentend/core/agent_selector.py`
- `src/agentend/db/models.py`
- `tests/test_intent_routing.py`
- `tests/test_agent_selector_trace.py`

验收：

```powershell
.venv\Scripts\python.exe -m pytest tests\test_intent_routing.py tests\test_agent_selector_trace.py tests\test_agent_tool_first_selector.py -q --basetemp=.tmp\o49-capabilities -p no:cacheprovider
```

### O50 Memory Gate

目标：把 memory 写入和读取从“已有质量规则”进一步收敛为明确门禁。

Write Gate：

- 自动写入只允许：用户明确偏好、项目稳定事实、成功 run 验证过的 procedure、performance 统计。
- 外部网页事实默认不进入长期 memory。
- 低置信、冲突、中置信更新进入 needs_review。
- secret/private data 默认 redacted 或 block。

Read Gate：

- 过期、低置信、不可信来源不进入 strong context。
- untrusted memory 可作为 weak hint 或 dropped reason。
- context ledger 记录 memory gate decision。

涉及文件：

- `src/agentend/core/memory_consolidator.py`
- `src/agentend/core/memory_store.py`
- `src/agentend/core/context_runtime.py`
- `src/agentend/tools/memory.py`
- `tests/test_agent_memory_consolidator.py`
- `tests/test_agent_memory_quality.py`
- `tests/test_agent_memory_relation.py`
- 新增 `tests/test_memory_gate.py`

验收：

```powershell
.venv\Scripts\python.exe -m pytest tests\test_agent_memory_consolidator.py tests\test_agent_memory_quality.py tests\test_agent_memory_relation.py tests\test_memory_gate.py -q --basetemp=.tmp\o50-memory-gate -p no:cacheprovider
```

### O51 Idempotent Resume 和 Replay Safety

目标：让 checkpoint/resume/replay 对不可幂等工具有明确行为，避免重复副作用。

ToolContract 增加：

- `idempotent`
- `idempotency_key_supported`
- `preview_supported`
- `dry_run_supported`
- `compensation_supported`
- `compensation_hint`

ToolCall 状态增强：

```text
planned -> policy_checked -> executing -> completed
                                      -> failed
                                      -> blocked
                                      -> uncertain
```

行为要求：

- 执行 backend 前先创建 `tool_call(status=executing)` 或等价 pending 记录。
- resume 遇到 `executing + non-idempotent` 默认 clarification/manual review。
- replay 默认复用历史输出，不重跑不可幂等 side effect。
- contract drift 时 replay plan 标记 blocked/skip。

涉及文件：

- `src/agentend/core/tool_contracts.py`
- `src/agentend/core/tool_registry.py`
- `src/agentend/core/workflow_runner.py`
- `src/agentend/core/replay.py`
- `src/agentend/core/context_runtime.py`
- `tests/test_phase_g_hitl_resume_replay.py`
- `tests/test_phase_j_replay_enhancement.py`
- 新增 `tests/test_idempotent_resume.py`

验收：

```powershell
.venv\Scripts\python.exe -m pytest tests\test_phase_g_hitl_resume_replay.py tests\test_phase_j_replay_enhancement.py tests\test_idempotent_resume.py -q --basetemp=.tmp\o51-idempotent-resume -p no:cacheprovider
```

### O52 Runtime Invariant Eval Suite

目标：新增一个小而硬的 eval suite，专门证明关键不变量，没有旁路和虚假绿测。

建议 suite：`runtime-invariants`

建议 case：

1. tool call has policy decision。
2. LLM call has context ledger。
3. external write blocked or requires confirmation。
4. scheduler network_write blocked。
5. replay non-idempotent side effect not rerun。
6. Telegram output redacts home path and raw tool JSON。
7. untrusted context cannot override policy。
8. memory low confidence/untrusted source dropped。
9. waiting_input has clarification request。
10. completed agent run cannot append iteration on resume。

每个 case 必须断言：

- 用户可见结果。
- run/agent_run status。
- 至少一个审计对象。
- 相关 event 或 ledger。

涉及文件：

- `src/agentend/core/eval_harness.py`
- `tests/test_phase_k_eval_suite_expansion.py`
- 新增 `tests/test_runtime_invariants_eval.py`

验收：

```powershell
.venv\Scripts\agentend.exe eval run runtime-invariants --home .tmp\runtime-invariants-home
.venv\Scripts\python.exe -m pytest tests\test_runtime_invariants_eval.py tests\test_phase_k_eval_suite_expansion.py -q --basetemp=.tmp\o52-runtime-invariants -p no:cacheprovider
```

## 5. 推荐执行顺序

推荐顺序：

```text
O45 Core Invariants 和状态机
  -> O52 Runtime Invariant Eval Suite 基线
  -> O46 ActionPolicy v2
  -> O47 Trusted Context Runtime
  -> O48 Evaluator 和 Stop Conditions
  -> O51 Idempotent Resume 和 Replay Safety
  -> O50 Memory Gate
  -> O49 Capability Manifest 收敛
```

原因：

- O45 是所有后续行为的状态基础。
- O52 先建立小黄金集，后续每个优化都能回归。
- O46/O47 是最大安全收益。
- O48 直接提升 agent 有用性，避免无限 replan 或过早 finish。
- O51 保护 checkpoint/resume/replay，防止重复副作用。
- O50/O49 是长期质量和架构收敛，依赖前面基础更稳。

## 6. 首轮建议落地范围

如果下一轮只做一个工程切片，建议做：

```text
O45 + O52 的最小闭环
```

首轮不要一次性改 Policy v2、Context trust、Memory Gate。先做：

- 状态常量和流转 helper。
- 3-5 个最关键 invariant checker。
- `runtime-invariants` eval 的前 5 个 case。
- 对现有 `agent_run`、clarification、tool call、context ledger 路径做最小接入。

首轮验收目标：

- 不破坏现有 `runtime-hardening` 和 `intent-routing`。
- 新增 invariant eval 能抓住无 policy decision、无 context ledger、waiting_input 无 clarification 这类问题。
- 代码变更集中，不重写 AgentRunController。

建议首轮验证：

```powershell
.venv\Scripts\python.exe -m pytest tests\test_agent_run_controller.py tests\test_intent_routing.py tests\test_phase_k_eval_suite_expansion.py tests\test_runtime_invariants.py tests\test_runtime_invariants_eval.py -q --basetemp=.tmp\next-optimization-o45-o52 -p no:cacheprovider
```

## 7. 发布前检查

每个优化切片完成前至少执行：

```powershell
git diff --check
.venv\Scripts\python.exe -m compileall src tests
.venv\Scripts\python.exe -m pytest <focused tests> -q --basetemp=.tmp\<slice-name> -p no:cacheprovider
```

涉及 eval 的切片还应执行：

```powershell
.venv\Scripts\agentend.exe eval list
.venv\Scripts\agentend.exe eval run <suite> --home <fresh-home>
.venv\Scripts\agentend.exe eval report <eval_run_id> --home <fresh-home>
```

在 Windows 环境中，测试建议始终使用 fresh `--basetemp` 和 `-p no:cacheprovider`，eval home 需要先 `agentend init --home <fresh-home>`，除非 CLI eval 自身负责初始化。

## 8. 不建议现在做的优化

暂缓：

- 多 Agent 或 sub-agent 编排。
- 远程沙箱。
- Postgres 迁移。
- 复杂审批后台。
- 自动晋升 episode-generated skill。
- 远程 MCP 自动启用。
- 大规模 CLI/UX 重做。

这些不是不重要，而是应该等状态机、Policy v2、Context trust、Evaluator 和 invariant eval 稳定后再做。

## 9. 成功标准

下一阶段优化完成后，AgentEnd 应能证明：

- 关键运行状态有集中定义和非法流转保护。
- 工具执行、LLM 调用、resume、external write 都有不可绕过的审计链路。
- ActionPolicy 能表达风险、确认、数据分级和幂等性要求。
- ContextRuntime 能区分 trusted instruction 和 untrusted evidence。
- Evaluator 能处理完成、继续、重规划、不可达和用户输入。
- Memory 写入/读取有明确 gate，低质量 memory 不污染强上下文。
- Replay/resume 不重复执行不可幂等副作用。
- `runtime-invariants` eval 能作为后续优化的第一道回归门。
