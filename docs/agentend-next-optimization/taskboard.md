# AgentEnd Next Optimization 任务文档

## 1. 任务目标

本任务板用于把 AgentEnd 下一阶段优化收敛为可执行工程切片。每个任务必须补齐代码、测试、eval 或审计记录，避免只新增文档或只改局部字段。

本阶段核心目标：

```text
让 AgentEnd 的关键运行链路不可绕过、可恢复、可审计、可回归。
```

## 2. 标记说明

- `AFK`：可由工程实现推进。
- `HITL`：需要用户确认策略、风险边界或真实环境配置。
- `Blocked`：依赖前置任务。
- `Done`：已完成。

## 3. 推荐顺序

```text
Phase NO0: Runtime Integrity 基线
  O45 Core Invariants 和状态机
  O52 Runtime Invariant Eval Suite

Phase NO1: 策略和上下文硬化
  O46 ActionPolicy v2
  O47 Trusted Context Runtime

Phase NO2: Agent Loop 质量
  O48 Evaluator 和 Stop Conditions
  O51 Idempotent Resume 和 Replay Safety

Phase NO3: 长期质量收敛
  O50 Memory Gate
  O49 Capability Manifest 收敛

Phase NO4: 文档、审计和发布闭环
  O53 Documentation and release checklist
```

## 4. 任务列表

### O45 Core Invariants 和状态机 `AFK`

状态：`Done (M0)`。

目标：把运行时关键状态和不可绕过规则落成代码级约束。

范围：

- 新增集中状态常量或 enum-like 模块，例如 `src/agentend/core/runtime_states.py`。
- 定义 `AgentRunStatus`、`AgentIterationStatus`、`RunStatus`、`ToolCallStatus`、`ClarificationStatus`。
- 新增状态流转校验 helper。
- 新增 `runtime_invariants.py`，输出 machine-readable issue list。
- 在 `AgentRunController.run()`、`resume()`、clarification gate、blocked gate 中使用最小 helper。

核心 invariants：

- completed agent_run 不允许追加 iteration。
- waiting_input 必须存在 pending clarification。
- blocked run 不允许直接 resume 到 running，除非有确认或策略变更。
- 每个 tool_call 必须有关联 policy decision。
- 每个 LLM call 必须有关联 context ledger。
- 每个 resume 必须有 checkpoint 或 safe restart 标记。

验收：

```powershell
.venv\Scripts\python.exe -m pytest tests\test_agent_run_controller.py tests\test_intent_routing.py tests\test_runtime_invariants.py -q --basetemp=.tmp\o45-runtime-invariants -p no:cacheprovider
```

测试映射：

- completed agent run resume 不追加 iteration。
- waiting_input run 缺 clarification 会产生 invariant issue。
- tool_call 缺 policy decision 会产生 invariant issue。
- LLM run 缺 context ledger 会产生 invariant issue。

落地记录：

- 已新增 `src/agentend/core/runtime_states.py`。
- 已新增 `src/agentend/core/runtime_invariants.py`，输出 machine-readable `InvariantIssue`。
- 首轮覆盖 `waiting_input_missing_clarification`、`tool_call_missing_policy_decision`、`llm_call_missing_context_ledger`、`completed_agent_run_has_active_iteration`。
- 已补强重复调用计数检查：同一 run/step/tool 的多次 ToolCall 必须有匹配数量的 ActionPolicyDecision；同一 run/step/model_stage 的多次 CostUsage 必须有匹配数量的 ContextLedger。
- `blocked run resume`、`resume checkpoint/safe restart` 继续放入 O51 的 idempotent resume/replay slice。

### O52 Runtime Invariant Eval Suite `AFK`

状态：`Done (M0)`。

目标：新增一个小而硬的 eval suite，专门证明关键不变量没有旁路和虚假绿测。

范围：

- `eval_harness.py` 注册 `runtime-invariants`。
- 使用 fake/local fixture，不依赖真实 provider。
- 每个 case 断言用户可见结果和审计对象。
- 失败 case 必须能导出或定位关联 run。

首批 case：

1. tool call has policy decision。`Done (M0)`
2. LLM call has context ledger。`Done (M0)`
3. scheduler network_write blocked。`Done (O46 M0)`
4. waiting_input has clarification request。`Done (M0)`
5. completed agent run cannot append iteration on resume。`Done (M0)`

后续 case：

6. replay non-idempotent side effect not rerun。
7. Telegram output redacts home path and raw tool JSON。
8. untrusted context cannot override policy。
9. memory low confidence/untrusted source dropped。

落地记录：

- 已在 `eval_harness.py` 注册 `runtime-invariants`。
- 已新增 CLI eval 覆盖：`tool-call-policy-link`、`llm-context-ledger-link`、`scheduler-network-write-blocked`、`waiting-input-clarification-link`、`completed-agent-run-resume-stable`。
- replay/untrusted/memory 相关 case 不塞进首轮，分别随 O51/O47/O50 落地。

验收：

```powershell
.venv\Scripts\agentend.exe eval run runtime-invariants --home .tmp\runtime-invariants-home
.venv\Scripts\python.exe -m pytest tests\test_runtime_invariants_eval.py tests\test_phase_k_eval_suite_expansion.py -q --basetemp=.tmp\o52-runtime-invariants -p no:cacheprovider
```

### O46 ActionPolicy v2 `AFK`

状态：`Done (M0)`。

目标：把当前 `run_mode + side_effect` 的策略扩展为可解释、可确认、可审计的 policy decision。

范围：

- 新增 PolicyDecision v2 payload。
- 扩展 `record_action_decision()` 的输入上下文。
- 支持 reason_code、risk_level、target、data_class、operation、idempotency、visibility、confirmation。
- 首版可写入 `decision_json` 或 event payload，保留旧字段兼容。
- 对 local_write、external_write、scheduler、replay、telegram 输出增加策略测试。

验收：

```powershell
.venv\Scripts\python.exe -m pytest tests\test_action_policy_v2.py tests\test_phase_c_local_action_tools.py tests\test_phase_o_scheduler_inbox_reliability.py -q --basetemp=.tmp\o46-policy-v2 -p no:cacheprovider
```

落地记录：

- 已扩展 `ActionDecision`，提供 `to_payload()` 结构化 v2 policy payload。
- 已扩展 `decide_action()` 输入上下文，覆盖 `reason_code`、`risk_level`、`actor`、`channel`、`target`、`data_class`、`operation`、`idempotency`、`visibility`、`reversibility`、`requires_preview`、`requires_user_confirmation`、`redactions`。
- 已在 `record_action_decision()` 保留旧 `ActionPolicyDecision` 字段，并新增 `policy.decided.v2` event payload。
- external_write 非 dry-run 默认进入 `require_clarification`；external_write dry-run 保持 allow；scheduler/replay 高副作用继续 block。
- 已把 `scheduler-network-write-blocked` 纳入 `runtime-invariants` eval。

### O47 Trusted Context Runtime `AFK`

状态：`Done (M0)`。

目标：给 ContextItem 加入信任边界，防止外部内容、tool output、memory 或用户输入提升为指令。

范围：

- 扩展 `ContextItem` metadata。
- 默认映射 system/agent profile/project profile/user/tool/web/file/MCP/memory 的 trust level。
- ContextLedger 记录 trust metadata。
- untrusted context 不得进入 instruction use。
- 增加 prompt injection fixture，证明外部内容不能修改 policy/allowed_tools。

验收：

```powershell
.venv\Scripts\python.exe -m pytest tests\test_phase_n_context_policy_budget.py tests\test_trusted_context_runtime.py tests\test_llm_agent_cli.py -q --basetemp=.tmp\o47-trusted-context -p no:cacheprovider
```

落地记录：

- 已扩展 `ContextItem`，新增 `source_type`、`trust_level`、`allowed_use`、`can_override_policy`。
- 已为默认来源建立 trust metadata 映射：system/profile、workflow、user input、memory、web/browser/MCP/tool/file。
- 已扩展 `ContextPackItem` 和 `ContextDroppedItem`，持久化 trust metadata。
- 已在 `init_database()` 增加增量列补齐，兼容旧 SQLite home。
- M0 先完成 ledger 可审计和 dropped item 可追踪。
- 后续 hardening 已收紧 `context_pack_to_messages()`：只有 trusted 且 `allowed_use` 包含 `instruction` 的 item 才能进入 system message；web/tool/file/generated memory 等上下文进入 user message 的 “not instructions” 区块。
- 已把 `prompt-injection-context-boundary` 纳入 `runtime-invariants` eval，证明外部网页和 generated tool output 不会进入 system instruction，也不能携带 `instruction` allowed use。

### O48 Evaluator 和 Stop Conditions 硬化 `AFK`

状态：`Done (M0)`。

目标：让 AgentRunController 明确处理完成、继续、重规划、不可达、重复失败、重复 action、预算和用户输入。

范围：

- Evaluator 输出 `finish/continue/replan/ask_user/fail`。
- 增加 max_same_error_count 和 max_same_action_count。
- 增加 `goal_unreachable`。
- policy_blocked 无替代路径时停止。
- 接近 iteration 上限时输出 residual risk 和 next steps。
- 对 code/test/research/file/report 目标增加 deterministic requirement。

验收：

```powershell
.venv\Scripts\python.exe -m pytest tests\test_agent_goal_evaluator.py tests\test_agent_run_controller.py tests\test_agent_stop_conditions.py -q --basetemp=.tmp\o48-evaluator -p no:cacheprovider
```

落地记录：

- 已修复 Evaluator `missing_requirements` 到 Selector 排序的断链。
- 当 GoalAnalyzer 未提供 requirements 时，Selector 会使用上一轮 evaluation 的 `missing_requirements` 匹配 capability contract。
- `test_command_evidence` 缺失时，deterministic next probe `shell.run` 可以突破初始 allowed_tools 的高风险过滤，但仍必须通过 ToolRegistry 和 ActionPolicy。
- 当前 dirty repo 下的 agent test-command 目标已恢复两轮完成。
- max_same_error_count、max_same_action_count、goal_unreachable 和 policy_blocked 细化仍保留给 O48 后续 slice。

### O51 Idempotent Resume 和 Replay Safety `AFK`

状态：`Done (M0)`。

目标：让 checkpoint/resume/replay 对不可幂等工具有明确行为，避免重复副作用。

范围：

- ToolContract 增加 idempotency、preview、dry-run、compensation 元数据。
- ToolCall lifecycle 增加 executing/uncertain 语义。
- backend 执行前先记录 pending/executing tool call。
- resume 遇到 `executing + non-idempotent` 默认 clarification/manual review。
- replay plan 标记 reused/skipped/blocked/contract_drift。

验收：

```powershell
.venv\Scripts\python.exe -m pytest tests\test_phase_g_hitl_resume_replay.py tests\test_phase_j_replay_enhancement.py tests\test_idempotent_resume.py -q --basetemp=.tmp\o51-idempotent-resume -p no:cacheprovider
```

落地记录：

- 已扩展 `ToolContract`，新增 `idempotent`、`idempotency_key_supported`、`preview_supported`、`dry_run_supported`、`compensation_supported`、`compensation_hint`。
- 已让 tool contract snapshot 保留幂等性元数据。
- 已让 replay plan 对每个 tool step 暴露 `idempotency`、`replay_action`、preview/dry-run/compensation 元数据。
- idempotent read replay 使用 `reuse_recorded_output`；non-idempotent side effect 默认 `manual_review_required` 并 block。
- M0 先覆盖 replay safety；后续 hardening 已补齐 `running/executing + non-idempotent` AgentRun resume manual review。

后续硬化记录：

- `AgentRunController.resume()` 现在会在继续迭代前扫描已关联 run 的 `running/executing` tool call。
- 若 tool contract snapshot/manifest 判定为 non-idempotent，会创建 `agent.resume_manual_review` run 和 `resume_manual_review` clarification。
- 命中该门禁时 AgentRun 保持 `waiting_input`，`stop_reason=resume_manual_review_required`，不会追加新的 iteration。
- `runs resume <manual_review_run> --answer reviewed_continue` 现在会记录 review decision，并允许下一次 `agent resume` 跳过已确认的同一 tool call 继续执行；`--answer cancel` 会取消 AgentRun。
- 验证：`.venv\Scripts\python.exe -m pytest tests\test_idempotent_resume.py tests\test_phase_g_hitl_resume_replay.py tests\test_phase_j_replay_enhancement.py tests\test_phase_i_eval_contract_snapshot.py tests\test_runtime_invariants.py -q --basetemp=.tmp\o51-resume-manual-review -p no:cacheprovider`，19 passed。
- 修复后验证：`.venv\Scripts\python.exe -m pytest tests\test_idempotent_resume.py tests\test_runtime_invariants.py tests\test_runtime_invariants_eval.py tests\test_agent_run_controller.py tests\test_phase_g_hitl_resume_replay.py tests\test_phase_j_replay_enhancement.py -q --basetemp=.tmp\review-fix-broader -p no:cacheprovider`，27 passed。

### O50 Memory Gate `AFK`

状态：`Done (M0)`。

目标：把 memory 写入和读取从“已有质量规则”进一步收敛为明确门禁。

范围：

- 新增 Memory Write Gate。
- 新增 Memory Read Gate。
- 自动写入只允许 user preference、project fact、successful procedure、performance stats。
- 外部网页事实、模型猜测、失败中间结论默认 reject 或 needs_review。
- Read Gate 输出 strong/weak/dropped decision，并进入 ContextLedger。

验收：

```powershell
.venv\Scripts\python.exe -m pytest tests\test_agent_memory_consolidator.py tests\test_agent_memory_quality.py tests\test_agent_memory_relation.py tests\test_memory_gate.py -q --basetemp=.tmp\o50-memory-gate -p no:cacheprovider
```

落地记录：

- 已新增 `src/agentend/core/memory_gate.py`，集中定义 Memory Write Gate / Read Gate decision。
- `write_memory_item()` 现在先通过 `decide_memory_write()`，拒绝 untrusted long-term memory，并记录 `memory.write_gate_decided` 事件。
- `memory_context_drop_reason()` 现在通过 `decide_memory_read()` 输出 strong/weak/drop 后再兼容返回现有 dropped reason。
- M0 保持现有低置信 manual memory 可写入，但在 context read 阶段按 `memory_low_confidence` drop，避免破坏 CLI 和既有质量测试。

### O49 Capability Manifest 收敛 `AFK`

状态：`Done (M0)`。

目标：让 tool、workflow、skill、generated draft 统一暴露为 capability，减少 IntentRouter、GoalAnalyzer、Selector 的重复理解。

范围：

- 扩展 `Capability` 字段或 payload。
- 增加 type、side_effect_upper_bound、risk_profile、eval_status、policy_tags、required_tools、version。
- IntentRouter 改为消费 capability summary。
- Selector 只在 effective allowed capability 中排序。
- Generated draft 默认不进入 executable pool。

验收：

```powershell
.venv\Scripts\python.exe -m pytest tests\test_intent_routing.py tests\test_agent_selector_trace.py tests\test_agent_tool_first_selector.py -q --basetemp=.tmp\o49-capabilities -p no:cacheprovider
```

落地记录：

- 已在 `src/agentend/core/capabilities.py` 中新增 `capability_manifest()` 和 `query_executable_capabilities()`。
- `Capability.example_json` 现在保存统一 manifest payload，包含 `type`、`side_effect_upper_bound`、`risk_profile`、`required_tools`、`eval_status`、`policy_tags`、`enabled`、`executable`、`version`。
- generated draft 仍保留为 capability 展示对象，但 `enabled=false`、`executable=false`、`eval_status=draft`，不会进入 executable capability 查询。
- `GoalAnalyzer` 输出 `candidate_capabilities`，并只从 executable capability 中召回兼容候选。
- `tools.discover` 在 capability map 存在时只返回 executable capabilities，避免 draft 被当作可调用工具展示。
- `GoalAnalyzer` 现在同时输出 `allowed_capabilities`，把 intent/action policy 收敛到 capability 级 allowed 集合。
- `AgentSelector` 已从 `candidate_capabilities` 召回 tool/skill，并用 `allowed_capabilities` 拒绝越权候选；legacy `candidate_tools/candidate_skills/allowed_tools` 仍保留兼容。

### O53 Documentation and release checklist `AFK`

状态：`Done (M0)`。

目标：完成本阶段文档、审计和发布前检查闭环。

范围：

- 回填 `audit.md` 的 implementation audit。
- 更新 taskboard 状态和测试映射。
- 如新增 CLI 或 eval，更新 README 或相关 docs。
- 运行 focused tests、compileall、git diff check。
- 运行 `runtime-hardening`、`intent-routing`、`runtime-invariants`。

验收：

```powershell
git diff --check
.venv\Scripts\python.exe -m compileall src tests
.venv\Scripts\agentend.exe eval run runtime-invariants --home .tmp\runtime-invariants-home
.venv\Scripts\agentend.exe eval run runtime-hardening --home .tmp\runtime-hardening-home
.venv\Scripts\agentend.exe eval run intent-routing --home .tmp\intent-routing-home
```

落地记录：

- 已完成 O45/O52/O46/O47/O48/O51/O50/O49 的需求、设计、任务和审计回填。
- 已运行 `runtime-invariants`、`runtime-hardening`、`intent-routing` 三个发布前 eval suite。
- 已运行全量测试：`207 passed, 1 skipped`。
- `compileall` 通过；`git diff --check` 仅保留 Windows 工作区 LF/CRLF 提示。

## 5. 首轮完成定义

首轮建议只做 `O45 + O52` 的最小闭环：

- 状态常量和流转 helper。
- 3-5 个最关键 invariant checker。
- `runtime-invariants` eval 的前 4 个 M0 case。
- 对现有 agent_run、clarification、tool call、context ledger 路径做最小接入。
- 不重写 AgentRunController。
- 不破坏 `runtime-hardening` 和 `intent-routing`。

首轮验证：

```powershell
.venv\Scripts\python.exe -m pytest tests\test_agent_run_controller.py tests\test_intent_routing.py tests\test_phase_k_eval_suite_expansion.py tests\test_runtime_invariants.py tests\test_runtime_invariants_eval.py -q --basetemp=.tmp\next-optimization-o45-o52 -p no:cacheprovider
```

当前验证记录：

- `tests/test_runtime_invariants.py tests/test_runtime_invariants_eval.py`：5 passed。
- `tests/test_intent_routing.py tests/test_phase_k_eval_suite_expansion.py tests/test_runtime_invariants.py tests/test_runtime_invariants_eval.py`：27 passed。
- `tests/test_action_policy_v2.py tests/test_runtime_invariants.py tests/test_runtime_invariants_eval.py tests/test_phase_c_local_action_tools.py tests/test_phase_f_inbox_tasks_tool_generator.py tests/test_phase_o_scheduler_inbox_reliability.py`：28 passed。
- `tests/test_trusted_context_runtime.py tests/test_phase_b_context_runtime.py tests/test_phase_h_context_reliability.py tests/test_phase_n_context_policy_budget.py tests/test_llm_agent_cli.py`：22 passed。
- `tests/test_agent_run_controller.py tests/test_agent_selector_trace.py tests/test_agent_tool_first_selector.py`：12 passed。
- `tests/test_idempotent_resume.py tests/test_phase_g_hitl_resume_replay.py tests/test_phase_j_replay_enhancement.py tests/test_phase_i_eval_contract_snapshot.py`：14 passed。
- `tests/test_runtime_invariants.py tests/test_runtime_invariants_eval.py tests/test_action_policy_v2.py tests/test_trusted_context_runtime.py tests/test_agent_run_controller.py tests/test_intent_routing.py tests/test_phase_k_eval_suite_expansion.py`：40 passed。
- `compileall src tests`：passed。
- `git diff --check`：exit 0，仅 LF/CRLF 工作区提示。
- `agentend eval run runtime-invariants --home .tmp\runtime-invariants-o46-home`：passed，5 cases。
- `.venv\Scripts\python.exe -m pytest tests -q --basetemp=.tmp\full-o47-o49 -p no:cacheprovider`：205 passed, 1 skipped。
- `.venv\Scripts\python.exe -m pytest tests -q --basetemp=.tmp\full-review-fixes -p no:cacheprovider`：207 passed, 1 skipped。
- `.venv\Scripts\agentend.exe eval run runtime-invariants --home .tmp\runtime-invariants-o47-o49-home`：passed，Eval ID `4d8435f0-5291-4bb8-92d5-15c073e55eb4`。
- 早前 dirty repo 下 `tests/test_agent_run_controller.py` 的 6 个失败已通过 O48 M0 修复覆盖。
