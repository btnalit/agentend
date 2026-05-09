# AgentEnd Next Optimization 审计文档

## 1. 审计范围

本审计文档记录 AgentEnd Next Optimization 阶段的风险判断、修复边界和验收要求。审计重点不是新增能力，而是验证现有关键链路是否具备硬约束：

- ToolRegistry 是否仍是唯一工具执行入口。
- ActionPolicy 是否覆盖所有副作用工具。
- ContextRuntime 是否覆盖所有 LLM 调用。
- AgentRun/Iteration/Run/ToolCall/Clarification 是否有明确状态机。
- Resume/Replay 是否避免重复不可幂等副作用。
- Memory 是否有写入和读取门禁。
- Eval 是否证明真实调用链，而不是只验证最终文本。

## 2. 当前事实

当前项目已经具备：

- `AgentRun`、`AgentIteration`、`Run`、`RunStep`、`ToolCall`、`ActionPolicyDecision`、`ContextLedger`、`Checkpoint`、`ClarificationRequest`、`EventLog` 等核心表。
- `ConversationService` 已把行动类 intent 路由到 `AgentRunController`。
- `IntentRouter` 已能输出 `IntentDecision` 并约束高副作用工具、disabled tool、generated draft。
- `ActionPolicy` 已接入 ToolRegistry，但当前主要基于 `run_mode + side_effect`。
- `ContextRuntime` 已能构造 selected/dropped context pack，并保留 prompt。
- `MemoryConsolidator` 已支持 merge、supersede、needs_review。
- `EvalHarness` 已覆盖 runtime-hardening、intent-routing、orchestration、memory、worker、context 等 suite。

审计结论：方向正确，基础完整；下一步风险主要来自“约束不够集中”和“部分语义仍偏弱”。

## 3. 关键风险

### R1 状态机分散

风险：

状态字段分散在不同模型和控制器中，非法流转不集中校验。未来添加 resume、replay、worker 时，可能出现 completed run 追加 iteration、blocked run 直接 running、waiting_input 无 clarification 等问题。

控制：

- 新增集中状态常量和 transition helper。
- 新增 invariant checker。
- `runtime-invariants` eval 覆盖关键非法状态。

### R2 ActionPolicy 表达力不足

风险：

当前策略主要按 `run_mode + side_effect` 判断，难以表达外部写入确认、数据分级、target、幂等性、visibility、reversibility。高风险工具可能被过粗的分类误判。

控制：

- 增加 PolicyDecision v2 payload。
- external_write 默认 preview/confirmation 或 block。
- secret/private data 与 external_write 组合必须单独处理。

### R3 Context trust 边界不足

风险：

当前 ContextItem 缺少 trust metadata。外部网页、tool output、MCP 输出、memory 可能被等价拼进 system context，带来 prompt injection 或策略覆盖风险。

控制：

- ContextItem 增加 source_type、trust_level、allowed_use、can_override_policy。
- untrusted item 不得进入 instruction use。
- ContextLedger 记录 trust metadata 和 dropped reason。

### R4 Evaluator 过早 finish 或无限 replan

风险：

Evaluator 如果只检查 observation completed 和文本非空，可能对未满足目标的任务提前 finish，或在不可达任务中循环 replan。

控制：

- 增加 evaluator decision。
- 增加重复错误、重复 action、不可达和 policy_blocked 停止条件。
- 对 code/test/research/file/report 增加 deterministic requirements。

### R5 Memory 污染

风险：

错误事实、模型猜测、一次性任务细节、外部网页事实或低置信总结进入长期 memory 后，会污染后续上下文和能力选择。

控制：

- Memory Write Gate。
- Memory Read Gate。
- 外部网页事实默认不进入长期 memory。
- 中低置信更新进入 needs_review。

### R6 Replay/Resume 重复副作用

风险：

不可幂等工具在 checkpoint 前后中断时，resume/replay 可能重复写文件、执行 shell、调用外部 API 或产生外部可见动作。

控制：

- ToolContract 增加 idempotency 和 preview/dry-run/compensation 元数据。
- ToolCall lifecycle 增加 executing/uncertain。
- replay 默认复用历史输出，contract drift 或不可幂等副作用默认 block。

### R7 MCP 能力扩张

风险：

MCP server 可能一次性暴露 filesystem、browser、database、email、shell、内部 API 等能力。如果只管 tool，不管 server trust profile，会扩大执行面。

控制：

- MCP server policy。
- remote_untrusted 默认 quarantine。
- 需要 manifest review、schema validation、policy assignment、eval 后 enable。

### R8 Eval 虚假信心

风险：

Eval 如果只断言输出文本或命令成功，不能证明 ToolRegistry、ActionPolicy、ContextRuntime、Evidence、Memory 等链路真实生效。

控制：

- 每个 eval case 至少断言一个用户可见结果和一个审计对象。
- 新增 `runtime-invariants` suite。

## 4. 数据边界审计

必须保持：

- Secret refs 只保存名称、来源和存在状态。
- DB、日志、export、MCP 调用、错误详情必须统一脱敏。
- Telegram 输出不得包含 AgentEnd home path、secret 或 raw tool JSON。
- ContextLedger 可以保存摘要和 hash，不保存无限制原始外部内容。
- Evidence 只证明来源被使用，不提升来源可信度。
- Checkpoint 不保存明文 secret。
- Cleanup/retention 不得删除 manual memory、enabled skill、recent checkpoint 和必要审计记录。

## 5. 审计事件要求

建议本阶段强化或新增事件：

| 事件 | 用途 |
| --- | --- |
| `runtime.invariant_checked` | invariant checker 运行。 |
| `runtime.invariant_failed` | 发现 invariant issue。 |
| `state.transitioned` | 关键状态流转。 |
| `policy.decided.v2` | ActionPolicy v2 决策。 |
| `context.trust_applied` | Context trust gate 应用。 |
| `evaluation.completed` | Evaluator 输出 decision。 |
| `memory.write_gate_decided` | Memory 写入门禁。 |
| `memory.read_gate_decided` | Memory 读取门禁。 |
| `replay.plan_created` | Replay plan 生成。 |
| `resume.manual_review_required` | 不可幂等恢复进入人工确认。 |
| `mcp.server_policy_applied` | MCP server policy 应用。 |

## 6. 测试审计要求

每个任务必须至少有一个 focused test 或 eval case。

最低要求：

- O45：状态流转和 invariant checker focused tests。
- O52：`runtime-invariants` eval suite。
- O46：ActionPolicy v2 decision payload 和策略组合 tests。
- O47：untrusted context prompt injection tests。
- O48：stop condition 和 unreachable tests。
- O51：non-idempotent resume/replay tests。
- O50：memory write/read gate tests。
- O49：capability manifest 和 generated draft exclusion tests。

## 7. 发布前检查清单

发布前必须执行：

```powershell
git diff --check
.venv\Scripts\python.exe -m compileall src tests
.venv\Scripts\python.exe -m pytest tests -q --basetemp=.tmp\next-optimization-full -p no:cacheprovider
```

至少运行以下 eval：

```powershell
.venv\Scripts\agentend.exe eval run runtime-invariants --home .tmp\runtime-invariants-home
.venv\Scripts\agentend.exe eval run runtime-hardening --home .tmp\runtime-hardening-home
.venv\Scripts\agentend.exe eval run intent-routing --home .tmp\intent-routing-home
```

如果 eval home 不存在，必须先执行：

```powershell
.venv\Scripts\agentend.exe init --home <fresh-home>
```

## 8. 保留风险

- 首轮 invariant checker 可能只能覆盖关键路径，不能一次性覆盖所有历史 run 形态。
- ActionPolicy v2 如果先写 JSON payload，后续仍需要 schema migration 才能更方便查询。
- Trusted Context Runtime 可能需要逐步迁移 ContextLedger schema，首轮可以先通过 JSON payload 证明行为。
- Evaluator 强化会改变部分 agent run 的完成时机，需要通过 focused eval 保持用户可见行为可解释。
- Memory Gate 过严可能降低短期“学习感”，但能减少长期污染。
- Idempotency metadata 初期依赖手工标注，未标注工具应默认 conservative。

## 9. Implementation Audit - O45/O52 M0

日期：2026-05-09。

落地范围：
- 新增 `src/agentend/core/runtime_states.py`，集中保存首轮状态集合。
- 新增 `src/agentend/core/runtime_invariants.py`，提供 `check_run_invariants(session, run_id=None, agent_run_id=None)`。
- 新增 `tests/test_runtime_invariants.py`，覆盖缺 clarification、缺 policy decision、缺 context ledger、completed run active iteration，以及真实 chat run clean path。
- 后续修复已补强审计数量匹配：同一 run/step/tool 的重复 ToolCall 不能被单条 ActionPolicyDecision 冒充通过；同一 run/step/model_stage 的重复 CostUsage 不能被单条 ContextLedger 冒充通过。
- 在 `eval_harness.py` 注册 `runtime-invariants` suite。
- 新增 `tests/test_runtime_invariants_eval.py`，验证 CLI 可列出并运行 `runtime-invariants`。

首轮已验证 invariant：
- `waiting_input_missing_clarification`
- `tool_call_missing_policy_decision`
- `llm_call_missing_context_ledger`
- `completed_agent_run_has_active_iteration`

首轮 eval case：
- `tool-call-policy-link`
- `llm-context-ledger-link`
- `waiting-input-clarification-link`
- `completed-agent-run-resume-stable`

验证结果：
- `.venv\Scripts\python.exe -m pytest tests\test_runtime_invariants.py tests\test_runtime_invariants_eval.py -q --basetemp=.tmp\green-runtime-invariants-2 -p no:cacheprovider`：5 passed。
- `.venv\Scripts\python.exe -m pytest tests\test_intent_routing.py tests\test_phase_k_eval_suite_expansion.py tests\test_runtime_invariants.py tests\test_runtime_invariants_eval.py -q --basetemp=.tmp\next-optimization-o45-o52-stable -p no:cacheprovider`：27 passed。
- `.venv\Scripts\python.exe -m pytest tests\test_runtime_invariants.py::test_repeated_tool_and_llm_calls_require_matching_audit_record_counts -q --basetemp=.tmp\green-invariant-counts -p no:cacheprovider`：1 passed。
- 包含 `tests/test_agent_run_controller.py` 的组合当前失败 6 个既有 case。失败输出显示 agent run 的 test-command goal 在测试 home 下读取到当前仓库未跟踪的 docs/tests/源码改动，导致 dirty git status 干扰原有 2 轮完成预期。该风险应进入 O48 evaluator/stop-condition 或单独测试隔离修复，不归因于 O45/O52 新增 invariant checker。

未完成项：
- scheduler network_write blocked case 已在 O46 M0 落地。
- replay non-idempotent side effect not rerun 放入 O51。
- untrusted context cannot override policy 放入 O47。
- memory low confidence/untrusted source dropped 放入 O50。
- blocked run resume 与 resume checkpoint/safe restart invariant 继续随 O51 实现。

## 10. Implementation Audit - O46 ActionPolicy v2 M0

日期：2026-05-09。

落地范围：
- 扩展 `ActionDecision` 为 v2 payload，支持 `reason_code`、`risk_level`、`actor`、`channel`、`target`、`data_class`、`operation`、`idempotency`、`visibility`、`reversibility`、`requires_preview`、`requires_user_confirmation`、`redactions`。
- 扩展 `decide_action()` 和 `record_action_decision()` 的输入上下文，保持旧调用兼容。
- `record_action_decision()` 继续写入旧 `ActionPolicyDecision` 行，同时新增 `policy.decided.v2` event payload。
- `ToolRegistry` 将工具 input 传入 ActionPolicy，用于 dry-run 外部写入等动态策略。
- `runtime-invariants` eval 增加 `scheduler-network-write-blocked` case，证明 scheduler 模式下 network_write 在工具真实发送前被策略阻断。

策略语义：
- `scheduler + local_execute/network_write/external_write`：block。
- `replay + local_write/local_execute/network_write/external_write`：block。
- `secret + external_write`：block。
- `external_write + dry_run=true`：allow。
- `external_write + dry_run=false`：require_clarification，要求 preview 和 user confirmation。

验证结果：
- `.venv\Scripts\python.exe -m pytest tests\test_action_policy_v2.py tests\test_runtime_invariants_eval.py -q --basetemp=.tmp\green-policy-v2 -p no:cacheprovider`：4 passed。
- `.venv\Scripts\python.exe -m pytest tests\test_action_policy_v2.py tests\test_runtime_invariants.py tests\test_runtime_invariants_eval.py tests\test_phase_c_local_action_tools.py tests\test_phase_f_inbox_tasks_tool_generator.py tests\test_phase_o_scheduler_inbox_reliability.py -q --basetemp=.tmp\o46-policy-v2 -p no:cacheprovider`：28 passed。
- `.venv\Scripts\agentend.exe eval run runtime-invariants --home .tmp\runtime-invariants-o46-home`：passed，Eval ID `a71c3d0f-1e3e-4cf7-a89f-407b5cdb3787`，5 cases。

保留风险：
- v2 payload 当前通过 event 记录，尚未迁移为 `ActionPolicyDecision.decision_json` 或显式列。
- external_write 的确认恢复链路尚未接入 HITL preview，这应进入后续 O46/O51 的 confirmation/resume slice。
- telegram raw tool JSON/redaction 仍未在本切片处理，保留给后续 telegram-facing policy/UX 切片。

## 11. Implementation Audit - O47 Trusted Context Runtime M0

日期：2026-05-09。

落地范围：
- `ContextItem` 增加 `source_type`、`trust_level`、`allowed_use`、`can_override_policy`。
- `ContextItem` 默认按 item/source 推导 trust metadata，现有调用保持兼容。
- `build_context_pack()` 对 manual memory 标记为 trusted answer context，对非 manual 或 dropped memory 标记为 generated/not_instruction。
- `ContextPackItem` 和 `ContextDroppedItem` 增加 trust metadata 字段。
- `init_database()` 对旧 SQLite home 增加 context trust metadata 增量列。
- `record_context_ledger()` 持久化 selected/dropped context 的 trust metadata。
- `context_pack_to_messages()` 后续 hardening 已强制只允许 trusted instruction item 进入 system message；untrusted/generated context 进入 user message 的 “not instructions” 区块。
- `runtime-invariants` 新增 `prompt-injection-context-boundary` case，构造外部网页注入和 generated tool output，验证它们只能作为 user context/evidence，不能进入 system instruction 或携带 `instruction` allowed use。

验证结果：
- `.venv\Scripts\python.exe -m pytest tests\test_trusted_context_runtime.py -q --basetemp=.tmp\green-trusted-context -p no:cacheprovider`：2 passed。
- `.venv\Scripts\python.exe -m pytest tests\test_trusted_context_runtime.py tests\test_phase_b_context_runtime.py tests\test_phase_h_context_reliability.py tests\test_phase_n_context_policy_budget.py tests\test_llm_agent_cli.py -q --basetemp=.tmp\o47-trusted-context -p no:cacheprovider`：22 passed。
- `.venv\Scripts\python.exe -m pytest tests\test_trusted_context_runtime.py tests\test_phase_b_context_runtime.py tests\test_phase_h_context_reliability.py tests\test_phase_n_context_policy_budget.py tests\test_llm_agent_cli.py -q --basetemp=.tmp\o47-context-message-boundary-2 -p no:cacheprovider`：23 passed。
- `.venv\Scripts\python.exe -m pytest tests\test_runtime_invariants_eval.py::test_runtime_invariants_eval_suite_covers_core_audit_links -q --basetemp=.tmp\green-o47-prompt-injection-eval -p no:cacheprovider`：1 passed。

保留风险：
- Context ledger CLI 目前仍主要展示原有字段，后续可补充 trust metadata 视图。

## 12. Implementation Audit - O48 Evaluator/Selector M0

日期：2026-05-09。

落地范围：
- 修复 Selector 对 Evaluator `missing_requirements` 的使用：当 GoalAnalyzer 未提供 requirements 时，Selector 使用上一轮 evaluation 的 missing requirements 匹配 capability contract。
- 为 `test_command_evidence` 增加 deterministic probe bridge：Evaluator 产出的 `next_probe=shell.run` 能驱动下一轮 action selection。
- 对 evaluator-required probe 开窄口，允许 `shell.run` 作为 `test_command_evidence` 的 deterministic next probe 进入候选排序；执行仍必须经过 ToolRegistry 与 ActionPolicy。

修复的真实问题：
- 当前 dirty repo 下，`code.local_task` 第一轮输出 git status 噪声但缺少 test command evidence。
- Evaluator 正确输出 `missing_requirements=["test_command_evidence"]` 和 `next_probe="shell.run"`。
- Selector 之前仍选择 `file.workspace_ops`，导致两轮内失败。
- 修复后第二轮选择 `shell.run`，`python -m pytest --version` 满足 test command evidence。

验证结果：
- `.venv\Scripts\python.exe -m pytest tests\test_agent_run_controller.py::test_agent_run_cli_records_iteration_progress_effectiveness_and_memory_candidate -q --basetemp=.tmp\o48-green-single-2 -p no:cacheprovider`：1 passed。
- `.venv\Scripts\python.exe -m pytest tests\test_agent_run_controller.py tests\test_agent_selector_trace.py tests\test_agent_tool_first_selector.py -q --basetemp=.tmp\o48-agent-run -p no:cacheprovider`：12 passed。
- `.venv\Scripts\python.exe -m pytest tests\test_runtime_invariants.py tests\test_runtime_invariants_eval.py tests\test_action_policy_v2.py tests\test_trusted_context_runtime.py tests\test_agent_run_controller.py tests\test_intent_routing.py tests\test_phase_k_eval_suite_expansion.py -q --basetemp=.tmp\next-optimization-o46-o48 -p no:cacheprovider`：40 passed。
- `.venv\Scripts\python.exe -m compileall src tests`：passed。
- `git diff --check`：exit 0，仅 LF/CRLF 工作区提示。

保留风险：
- `max_same_error_count`、`max_same_action_count`、`goal_unreachable`、`policy_blocked` 的显式 decision schema 尚未落地。
- deterministic probe override 当前只覆盖 `test_command_evidence -> shell.run`。
- O49 pure capability policy 已接入 selector；deterministic probe 仍保持最小例外，执行继续受 ToolRegistry 与 ActionPolicy 约束。

## 13. Implementation Audit - O51 Idempotent Resume/Replay M0

日期：2026-05-09。

落地范围：
- `ToolContract` 增加 `idempotent`、`idempotency_key_supported`、`preview_supported`、`dry_run_supported`、`compensation_supported`、`compensation_hint`。
- `contract_for_tool()` 根据 side_effect 和工具名生成首版幂等性 metadata。
- `snapshot_tool_contracts()` 在 run snapshot 中保留这些 metadata。
- `build_replay_plan()` 对 tool step 输出 `idempotency`、`replay_action`、preview/dry-run/compensation 信息。
- idempotent read 使用 `reuse_recorded_output`，non-idempotent side effect 使用 `manual_review_required` 并 block。
- `AgentRunController.resume()` 增加恢复安全门，遇到历史 linked run 中仍为 `running/executing` 的 non-idempotent tool call 时，创建 `agent.resume_manual_review` clarification，不追加新 iteration。
- 新增 `resume.manual_review_required` event，记录 tool_call、side_effect、idempotency、preview/dry-run/compensation metadata。
- `WorkflowRunner.resume()` 增加 `agent.resume_manual_review` 专用回答路径：`reviewed_continue` 会记录 review decision 并让下一次 `agent resume` 跳过已确认的同一 tool call；`cancel` 会取消 AgentRun。

验证结果：
- `.venv\Scripts\python.exe -m pytest tests\test_idempotent_resume.py -q --basetemp=.tmp\green-o51-idempotent -p no:cacheprovider`：3 passed。
- `.venv\Scripts\python.exe -m pytest tests\test_idempotent_resume.py tests\test_phase_g_hitl_resume_replay.py tests\test_phase_j_replay_enhancement.py tests\test_phase_i_eval_contract_snapshot.py -q --basetemp=.tmp\o51-idempotent-resume -p no:cacheprovider`：14 passed。
- `.venv\Scripts\python.exe -m pytest tests\test_idempotent_resume.py tests\test_phase_g_hitl_resume_replay.py tests\test_phase_j_replay_enhancement.py tests\test_phase_i_eval_contract_snapshot.py tests\test_runtime_invariants.py -q --basetemp=.tmp\o51-resume-manual-review -p no:cacheprovider`：19 passed。
- `.venv\Scripts\python.exe -m pytest tests\test_idempotent_resume.py::test_agent_resume_manual_review_answer_allows_next_resume -q --basetemp=.tmp\green-manual-review-answer-2 -p no:cacheprovider`：1 passed。
- `.venv\Scripts\python.exe -m pytest tests\test_idempotent_resume.py tests\test_runtime_invariants.py tests\test_runtime_invariants_eval.py tests\test_agent_run_controller.py tests\test_phase_g_hitl_resume_replay.py tests\test_phase_j_replay_enhancement.py -q --basetemp=.tmp\review-fix-broader -p no:cacheprovider`：27 passed。

保留风险：
- `running/executing + non-idempotent` AgentRun resume manual-review 已在 O51 hardening 中落地。
- ToolCall lifecycle 仍使用现有 `running/completed/failed/reused`，尚未引入 `uncertain`。
- preview/dry-run/compensation metadata 仍是静态首版推导，后续应迁移到更明确的 ToolContract authoring。

## 14. Implementation Audit - O50 Memory Gate M0

日期：2026-05-09。

落地范围：
- 新增 `src/agentend/core/memory_gate.py`，定义 `MemoryGateDecision`、`decide_memory_write()`、`decide_memory_read()`。
- Memory Write Gate 明确阻断 untrusted source 写入 `project/user` 长期 memory，并允许 untrusted source 仅进入 `session/task/episode` 短期 scope。
- `write_memory_item()` 接入 Write Gate，并记录 `memory.write_gate_decided` event payload。
- Memory Read Gate 统一处理 status、scope、ttl、source trust、confidence，并输出 `strong/weak/drop` decision。
- `memory_context_drop_reason()` 改为委托 Read Gate，保持现有 ContextRuntime dropped reason 兼容。

验证结果：
- `.venv\Scripts\python.exe -m pytest tests\test_memory_gate.py -q --basetemp=.tmp\green-o50-memory-gate -p no:cacheprovider`：3 passed。
- `.venv\Scripts\python.exe -m pytest tests\test_memory_gate.py tests\test_agent_memory_consolidator.py tests\test_agent_memory_quality.py tests\test_agent_memory_relation.py tests\test_phase_h_context_reliability.py tests\test_phase_n_context_policy_budget.py -q --basetemp=.tmp\o50-memory-gate -p no:cacheprovider`：25 passed。

保留风险：
- M0 只记录 write gate event；read gate decision 目前通过 ContextLedger dropped reason 间接呈现，尚未新增 `memory.read_gate_decided` event。
- 低置信 manual memory 仍允许写入，读取进入 context 时 drop；这是为了兼容当前 CLI 和既有质量测试，后续如需更严格可增加 review workflow。
- Memory Write Gate 尚未接入完整 data_class/secret/private content 检测，当前 secret 仍主要依赖现有 redaction。

## 15. Implementation Audit - O49 Capability Manifest M0

日期：2026-05-09。

落地范围：
- `Capability.example_json` 增加统一 `manifest` payload，覆盖 tool、skill、generated draft。
- 新增 `capability_manifest(row)`，作为 capability manifest 的兼容读取入口。
- 新增 `query_executable_capabilities()`，只返回 manifest 中 `executable=true` 的 capability。
- generated draft capability 明确 `enabled=false`、`executable=false`、`eval_status=draft`、`policy_tags=["generated","draft"]`。
- `capabilities refresh` 先确保 builtin skills，再刷新 capability map，避免 skill capability 缺失。
- `GoalAnalyzer` 输出 `candidate_capabilities`，并只从 executable capability 中召回候选。
- `tools.discover` 在 capability map 存在时只返回 executable capabilities，避免 draft 被展示为可调用工具。
- `GoalAnalyzer` 输出 `allowed_capabilities`，把 intent/action policy 的可执行边界收敛为 capability 级 allowed 集合。
- `AgentSelector` 从 `candidate_capabilities` 召回 tool/skill 候选，并用 `allowed_capabilities` 拒绝越权 candidate；被拒绝候选进入 selector trace。
- Selector 保留 evaluator-required probe 窄口，避免 O48 的 `test_command_evidence -> shell.run` deterministic replan 被 capability policy 误杀；真正执行仍走 ToolRegistry 与 ActionPolicy。

验证结果：
- `.venv\Scripts\python.exe -m pytest tests\test_capability_manifest.py -q --basetemp=.tmp\green-o49-capability-manifest-2 -p no:cacheprovider`：2 passed。
- `.venv\Scripts\python.exe -m pytest tests\test_capability_manifest.py tests\test_intent_routing.py tests\test_agent_selector_trace.py tests\test_agent_tool_first_selector.py tests\test_agent_capability_contracts.py tests\test_phase_d_search_evidence_capabilities.py tests\test_phase_f_inbox_tasks_tool_generator.py tests\test_phase_e_planning_episode.py -q --basetemp=.tmp\o49-capability-manifest -p no:cacheprovider`：37 passed。
- `.venv\Scripts\python.exe -m pytest tests\test_agent_capability_contracts.py::test_selector_honors_pure_capability_policy_without_legacy_candidate_lists -q --basetemp=.tmp\green-o49-selector-policy -p no:cacheprovider`：1 passed。
- `.venv\Scripts\python.exe -m pytest tests\test_capability_manifest.py::test_goal_analyzer_emits_allowed_capabilities_from_intent_policy -q --basetemp=.tmp\green-o49-goal-allowed-capabilities -p no:cacheprovider`：1 passed。
- `.venv\Scripts\python.exe -m pytest tests\test_agent_capability_contracts.py tests\test_capability_manifest.py tests\test_runtime_invariants_eval.py -q --basetemp=.tmp\green-o47-o49-focused -p no:cacheprovider`：7 passed。

保留风险：
- M0 仍保留 `candidate_tools/candidate_skills/allowed_tools` 兼容字段，后续可继续压缩 IntentRouter/GoalAnalyzer 对 legacy 字段的依赖。
- manifest 目前落在 `example_json.manifest`，后续如需高频查询应迁移为显式列或独立 snapshot 表。
- workflow capability 尚未作为单独 capability source 落库；当前 workflow 仍主要通过 fallback 和 skill/tool path 使用。

## 16. Implementation Audit - O53 Documentation and Release Checklist M0

日期：2026-05-09。

落地范围：
- 回填 `requirements.md`、`design.md`、`taskboard.md`、`audit.md` 的 O45/O52/O46/O47/O48/O51/O50/O49 状态和实现记录。
- 对本阶段新增核心测试做跨切片回归。
- 运行发布前 eval：`runtime-invariants`、`runtime-hardening`、`intent-routing`。
- 运行全量 test suite。
- 运行编译和 diff 检查。

验证结果：
- `.venv\Scripts\agentend.exe eval run runtime-invariants --home .tmp\runtime-invariants-o53-home`：passed，Eval ID `a8ca8f14-af0b-470b-a3bd-b558e1b4d03d`。
- `.venv\Scripts\agentend.exe eval run runtime-hardening --home .tmp\runtime-hardening-o53-home`：passed，Eval ID `b1bc8af2-882c-4d31-9168-55caf1d3f278`。
- `.venv\Scripts\agentend.exe eval run intent-routing --home .tmp\intent-routing-o53-home`：passed，Eval ID `49455692-cb35-4f5f-a146-5831b988ec5c`。
- `.venv\Scripts\python.exe -m pytest tests -q --basetemp=.tmp\next-optimization-full -p no:cacheprovider`：201 passed, 1 skipped。
- `.venv\Scripts\python.exe -m pytest tests -q --basetemp=.tmp\full-o47-o49 -p no:cacheprovider`：205 passed, 1 skipped。
- `.venv\Scripts\python.exe -m pytest tests -q --basetemp=.tmp\full-review-fixes -p no:cacheprovider`：207 passed, 1 skipped。
- `.venv\Scripts\agentend.exe eval run runtime-invariants --home .tmp\runtime-invariants-o47-o49-home`：passed，Eval ID `4d8435f0-5291-4bb8-92d5-15c073e55eb4`。
- `.venv\Scripts\python.exe -m compileall src tests`：passed。
- `git diff --check`：exit 0，仅 LF/CRLF 工作区提示。

保留风险：
- 本轮未做 commit/stage，发布动作需用户另行确认。
- O49 pure capability policy 已完成 selector/goal analyzer 最小迁移；后续风险主要是 workflow capability 入池和 legacy 字段继续收敛。
- O51 的 `uncertain` ToolCall lifecycle schema 迁移仍是后续切片。

## 17. 当前审计结论

AgentEnd Next Optimization 的 M0 已经形成闭环：运行时 invariant、ActionPolicy v2、Trusted Context、Evaluator/Selector 修复、Replay 幂等性、Memory Gate、Capability Manifest 和发布前 eval/checklist 均已落地并通过验证。后续优化应继续围绕剩余 hardening 风险推进，而不是再扩大首版范围。

## 18. Closure Audit - Changed Operation Surfaces

日期：2026-05-09。

审计范围：
- Tool 执行链路：`ToolRegistry.call()` 仍统一创建 ToolCall、同步/快照 ToolContract、调用 ActionPolicy、记录 compact/evidence/cache。
- ActionPolicy v2：scheduler/replay 高副作用阻断、external_write confirmation、secret external_write block、dry-run allow 均有结构化 event。
- ContextRuntime：trusted instruction 与 untrusted/generated context 已在 message 层隔离，ContextLedger selected/dropped item 持久化 trust metadata。
- AgentRun/WorkflowRunner resume：non-idempotent running/executing tool call 会进入 `resume_manual_review`；`reviewed_continue` 可解除同一 tool call 的恢复门禁，`cancel` 会取消 AgentRun。
- Runtime invariants：tool_call/policy 和 cost_usage/context_ledger 不只检查存在性，也检查同一 run/step/tool 或 run/step/model_stage 的记录数量匹配。
- Replay/idempotency：idempotent read 复用历史输出，non-idempotent side effect 标记 `manual_review_required` 并默认 block。
- Memory Gate：长期 memory 写入由 Write Gate 控制，ContextRuntime 读取 memory 时统一走 Read Gate drop reason。
- Capability policy：GoalAnalyzer 输出 `candidate_capabilities/allowed_capabilities`；Selector 只在 allowed capability 内排序，保留 legacy 字段兼容。
- CLI/docs/eval：`capabilities refresh/query` 先确保 builtin skills；`runtime-invariants`、`runtime-hardening`、`intent-routing` 均可通过 CLI 执行。

收口验证结果：
- `.venv\Scripts\python.exe -m pytest tests\test_action_policy_v2.py tests\test_trusted_context_runtime.py tests\test_memory_gate.py tests\test_capability_manifest.py tests\test_agent_capability_contracts.py tests\test_idempotent_resume.py tests\test_runtime_invariants.py tests\test_runtime_invariants_eval.py tests\test_agent_run_controller.py tests\test_phase_c_local_action_tools.py tests\test_phase_g_hitl_resume_replay.py tests\test_phase_j_replay_enhancement.py tests\test_phase_o_scheduler_inbox_reliability.py -q --basetemp=.tmp\closure-focused -p no:cacheprovider`：55 passed。
- `.venv\Scripts\agentend.exe eval run runtime-invariants --home .tmp\closure-runtime-invariants`：passed，Eval ID `1b901b22-3289-40da-a617-3c82d256ff31`。
- `.venv\Scripts\agentend.exe eval run runtime-hardening --home .tmp\closure-runtime-hardening`：passed，Eval ID `9a182bbe-c9c5-46eb-a641-033fa06573cc`。
- `.venv\Scripts\agentend.exe eval run intent-routing --home .tmp\closure-intent-routing`：passed，Eval ID `3093c98f-247c-411c-abfe-e3ab970cb738`。
- `.venv\Scripts\python.exe -m compileall src tests`：passed。
- `git diff --check`：exit 0，仅 LF/CRLF 工作区提示。
- `.venv\Scripts\python.exe -m pytest tests -q --basetemp=.tmp\closure-full -p no:cacheprovider`：207 passed, 1 skipped。

收口结论：
- 本次未发现阻断提交的正确性、安全或调用链问题。
- 仍未执行 stage/commit/push。
- 剩余事项保持为已记录的后续 hardening：workflow capability 入池、legacy capability 字段进一步收敛、`uncertain` ToolCall lifecycle schema、Memory Read Gate 独立 event/展示增强。
