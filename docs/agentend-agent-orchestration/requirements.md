# AgentEnd Agentic Orchestration 需求文档

## 1. 背景

AgentEnd Lite、Action Layer、Runtime Hardening 和 Review Remediation 已经完成了单机 Agent 的主要底座：CLI、Telegram、SQLite、WorkflowRunner、Tool Registry、Skills、MCP、Context Runtime、Memory Store、Episode Logger、Task/Scheduler、Replay/Export、Eval 和 Storage Governance。

当前系统的关键问题已经从“有没有工具”和“调用链是否可用”转移为“智能体是否能围绕目标持续选择工具、观察结果、修正计划并沉淀经验”。现有 `ConversationService` 会执行 Goal Analyzer，但仍固定运行 `simple_chat` workflow；Goal Analyzer 产出的候选 tool/skill/workflow 目前主要用于记录，不直接驱动执行。

本阶段命名为 **AgentEnd Agentic Orchestration**。目标不是引入多 Agent、复杂审批、安全治理或企业权限系统，而是在现有单机架构上补齐稳定、结果导向、工具优先、跨会话记忆和长任务循环能力。

## 2. 目标

本阶段要把 AgentEnd 从“可运行 workflow 的工具型运行时”推进为“目标结果导向的单 Agent 执行器”：

```text
goal
  -> plan
  -> act(tool / skill / workflow)
  -> observe
  -> evaluate
  -> replan or finish
  -> consolidate memory and skill effectiveness
```

核心目标：

- 目标结果导向：每次 agent run 必须有明确 goal、success criteria、stop criteria、iteration limit 和最终验收说明。
- 工具优先执行：对可由工具、skill、workflow 完成的任务，默认先选择行动路径，而不是直接聊天回答。
- 记忆系统完善：跨会话记忆必须短、准、带来源、可更新、可检索、能影响后续 tool/skill 选择。
- 长任务循环优化：长任务必须拆成 iteration，有 heartbeat、progress artifact、checkpoint、resume、max_iterations 和失败恢复策略。
- Skill/Memory 治理偏功能性：治理含义限定为质量、去重、版本、有效性、成功率和可维护性，不包含审批流或复杂安全策略。

## 3. 范围

### 3.1 必须包含

- 新增显式 `AgentRunController`，负责 agent 执行循环。
- 新增 `agentend agent run` CLI 入口，首版不替换现有 `agentend chat` 默认行为。
- AgentRunController 复用现有 `WorkflowRunner`、Tool Registry、Skill Registry、Goal Analyzer、Replanner、Context Runtime、Memory Store、Episode Logger 和 Task Manager。
- Agent run 记录 goal、plan、iterations、actions、observations、evaluation、final_result、stop_reason。
- Tool-first selector 能基于 Goal Analyzer 候选能力、Capability Map、Skill manifest、历史效果选择 action。
- 首版 action 类型必须支持：
  - `tool_call`
  - `skill_run`
  - `workflow_run`
  - `llm_reason`
  - `ask_user`
  - `finish`
- 每轮 iteration 必须记录：
  - selected action
  - action input
  - observation
  - evaluator verdict
  - next decision
  - checkpoint id 或 resume cursor
- 长任务必须支持 max_iterations、max_runtime_seconds、heartbeat_interval_seconds 和 progress artifact。
- `agentend serve` 或等价 worker loop 负责持续处理 task、schedule、inbox 和后续 Telegram 长任务，不依赖用户手动反复 tick。
- Memory Consolidator 在 run/episode 完成后生成候选记忆，并按策略写入或更新 memory。
- 候选记忆类型必须包含：
  - project_fact
  - user_preference
  - successful_procedure
  - failure_lesson
  - tool_effectiveness
  - skill_effectiveness
  - task_state
- 跨会话记忆必须支持 provenance、confidence、scope、tags、last_used、created_by_run_id、evidence_artifact_id。
- Memory Consolidator 必须能去重、合并、更新 confidence、更新 superseded 关系。
- Skill effectiveness 必须进入能力选择：成功率、失败率、最近成功时间、平均 iteration 数、常见失败原因都要能影响排序。
- 新增 orchestration/memory/long-task eval suite，覆盖工具优先、重规划、记忆沉淀、跨会话检索和 worker resume。

### 3.2 不包含

- 多 Agent 协作架构。
- 前端 Console。
- 企业审批系统、ops gate、权限治理或安全审计工作流。
- 自动执行外部可见写入的新增策略设计。
- Docker、Firecracker、E2B、远程沙箱或分布式队列。
- 自动从一次成功 episode 直接启用 skill。
- 大模型长期全文记忆或原始聊天记录无限注入上下文。

## 4. 关键需求

### 4.1 AgentRunController

AgentRunController 是本阶段的主入口，不把 `WorkflowRunner` 改造成复杂 agent。`WorkflowRunner` 继续负责确定性 workflow 执行，AgentRunController 负责编排多轮选择、执行、观察和评估。

要求：

- 输入：goal、channel、external_user_id、optional constraints、max_iterations、max_runtime_seconds。
- 输出：status、final_result、stop_reason、run_id、agent_run_id、iterations、artifacts。
- 每次 iteration 最多执行一个主要 action，避免一次计划生成过多不可控动作。
- 每轮先尝试 tool/skill/workflow action；只有没有合适行动能力或需要综合判断时才进入 `llm_reason`。
- 失败时调用现有 Replanner，但 Replanner 结果必须能驱动下一轮 action，而不是只写入 run 结果。
- 达到 success criteria 时立即 finish；达到 max_iterations、预算或 stop criteria 时明确停止并输出剩余事项。

### 4.2 结果导向计划

每个 agent run 必须把自然语言目标转为结构化目标包：

```json
{
  "goal": "完成用户请求的可验证结果",
  "success_criteria": ["可检查条件"],
  "constraints": ["路径、工具、输出格式等约束"],
  "preferred_outputs": ["文件、摘要、命令输出、报告等"],
  "stop_criteria": ["完成、无法继续、需要用户输入、达到上限"],
  "max_iterations": 8
}
```

首版 success criteria 可以由规则 + LLM 生成，必须允许 CLI 覆盖。

### 4.3 工具优先执行

工具优先不是强制每次都调用工具，而是让系统在回答前先判断是否存在可执行路径。

要求：

- Goal Analyzer 输出候选 skill/tool/workflow 后，Selector 必须排序并选择 action。
- 排序因子至少包含：
  - capability text match
  - required input 是否齐全
  - side effect 是否适合当前 run_mode
  - skill/tool 历史成功率
  - 最近失败原因
  - 是否有 eval 通过记录
  - 是否能产出用户想要的 artifact
- 如果选择 `llm_reason`，必须记录为什么不调用工具。
- 对本地代码、文件、搜索、数据分析、报告生成类任务，默认应优先尝试对应 skill 或 workflow。

### 4.4 Memory Consolidator

跨会话最优记忆不是全文历史，也不是无筛选向量库。AgentEnd 的长期记忆必须是结构化、可更新、可解释、能影响后续执行的事实和经验。

要求：

- 在 run 完成、episode summarize、task 完成和 skill run 完成后触发 consolidation。
- 生成候选记忆，不直接把整段对话写入长期 memory。
- 每条候选记忆必须包含：
  - type
  - scope
  - content
  - provenance
  - confidence
  - evidence reference
  - merge key
  - suggested tags
  - expiry policy
- 对已有 memory 做相似查找，优先 update/merge，而不是新增重复条目。
- 支持 superseded：当新事实替代旧事实时，旧 memory 不删除，但不再默认进入 context。
- 写入后必须能被 Context Runtime 检索，并影响 AgentRunController 的 action selection。

### 4.5 记忆分层

本阶段固定五层记忆语义：

| 层 | 用途 | 默认保存方式 |
| --- | --- | --- |
| working | 当前长任务状态、计划、下一步、未完成项 | task/agent_run checkpoint |
| episodic | run/episode 摘要、失败原因、关键产物 | episode memory |
| semantic | 项目事实、用户偏好、长期约束 | project/user memory |
| procedural | 成功流程、常用命令、可复用步骤 | skill/project memory |
| performance | tool/skill 成功率、失败模式、成本和耗时 | effectiveness record |

### 4.6 Skill Effectiveness

Skill 治理在本阶段指能力质量治理：哪些 skill 好用、何时适用、哪些失败模式会导致降权。

要求：

- 每次 skill/workflow/tool action 完成后记录 effectiveness event。
- 至少记录 success、failure、blocked、needs_input、duration、iteration_count、error_code、output_artifact_count。
- Selector 使用 effectiveness score 调整候选排序。
- `skills show` 或新增 CLI 能展示 skill 的最近运行结果和常见失败原因。
- Episode-to-Skill draft 只有在通过 eval 后才能进入候选排序。

### 4.7 Long Task Worker

`agentend serve` 是本阶段的长期运行入口，负责轮询本地持久化队列，不引入外部分布式系统。

要求：

- 支持处理 due schedules、pending tasks、file inbox batches。
- 每个长任务通过 AgentRunController 执行，而不是直接只跑一次 workflow。
- Worker 必须有 heartbeat event 和 progress artifact。
- Worker 重启后能从 pending/running/blocked 状态恢复。
- 单个任务达到 max_iterations 或连续失败阈值后停止或 blocked，不无限循环。
- 支持 `--once`、`--poll-interval`、`--max-concurrency 1`。首版只要求单并发。

### 4.8 可观察性和验收

所有新行为必须能通过 CLI、DB 记录和 eval 验证。

验收命令必须覆盖：

```bash
agentend agent run "列出当前项目测试命令并说明依据"
agentend agent show <agent_run_id>
agentend memory consolidate --run <run_id>
agentend memory search "测试命令"
agentend skills effectiveness show code.local_task
agentend serve --once
agentend eval run orchestration-smoke
agentend eval run memory-consolidation
agentend eval run long-task-worker
```

## 5. 成功标准

- AgentRunController 能完成至少一个真实工具优先任务，且最终结果说明使用过哪些工具和满足了哪些 success criteria。
- Goal Analyzer 的候选 skill/tool/workflow 会实际影响执行路径。
- 工具失败能进入 observe -> evaluate -> replan -> next action，而不是只把 suggestion 写入失败 run。
- Memory Consolidator 能从完成 run 中沉淀 project/user/procedural/performance 记忆，并在下一次 run 中被检索使用。
- 重复或冲突 memory 不会无限新增；旧记忆能被 superseded 或降权。
- Skill effectiveness 能改变候选排序，失败率高的 skill 不再长期排在首位。
- `agentend serve --once` 可以处理至少一个 pending task 或 due schedule，并产生 heartbeat/progress 记录。
- 长任务中断后能从 checkpoint 或 working memory 恢复，不重跑已完成 iteration。
- 新增 eval suite 全部通过，并且失败报告能定位到 agent run、iteration、action、memory candidate 或 worker heartbeat。

## 6. 参考实践

本阶段吸收外部 Agent 最佳实践，但不整体迁移框架：

- OpenAI Agents/Responses 实践：保留 reasoning/tool call 状态、使用 tracing、对 tool selection 和 final correctness 做 eval、长任务使用后台执行模式。
- Anthropic Agent 实践：Agent 是 LLM、工具和环境反馈组成的循环；复杂任务适合 orchestrator-worker 和 evaluator-optimizer 模式；长任务需要 progress artifact 和可继续执行的 harness。
- LangGraph 实践：区分 thread checkpoint 与长期 memory store；每步持久化；把副作用和非确定性操作放进可追踪 task。

对 AgentEnd 的落地原则是：复用现有 `WorkflowRunner` 和 SQLite 持久化，新增轻量 AgentRunController、Memory Consolidator 和 Long Task Worker，不引入新的重型运行框架。

## 7. Implementation Backfill - 2026-05-07

Status: O1-O15 implemented in the first production slice.

Implemented acceptance scope:
- AgentRunController persists `agent_runs` and `agent_iterations`, runs goal -> select -> act -> observe -> evaluate -> finish, and enforces `max_iterations`.
- Tool-first selector consumes Goal Analyzer output, enabled skills, tool manifests, memory context, and capability effectiveness.
- Memory consolidation creates structured `memory_candidates`, merges by `merge_key`, writes long-term `MemoryItem` rows through `agent_consolidator`, and links provenance.
- Capability effectiveness records event-level and aggregate success/failure/blocked metrics for tool, skill, and workflow actions.
- Long Task Worker adds `agentend serve` and processes pending task/schedule/inbox work through AgentRunController.
- Existing workflow chat remains default; `agentend chat --agent` enables the new loop explicitly.
- New eval suites are registered: `orchestration-smoke`, `tool-first`, `memory-consolidation`, `skill-effectiveness`, `long-task-worker`, `agent-replan`.

Verification evidence:
- `.venv\Scripts\python.exe -m pytest tests -q --basetemp=.tmp\agent-orchestration-full-2 -p no:cacheprovider` -> 132 passed.
- `.venv\Scripts\agentend.exe eval run orchestration-smoke --home .tmp\agent-orchestration-home` -> passed, Eval `678d20a5-951f-49ed-b0ba-de0d24b9e6ca`.
- `.venv\Scripts\agentend.exe eval run memory-consolidation --home .tmp\agent-orchestration-home` -> passed, Eval `4fcfe147-6d3a-4bf4-9cad-1bd01bcc5b4d`.
- `.venv\Scripts\agentend.exe eval run long-task-worker --home .tmp\agent-orchestration-home` -> passed, Eval `8746c0b6-8652-4168-8dae-558461d5cfc2`.
- `.venv\Scripts\agentend.exe agent run --home .tmp\agent-orchestration-home "列出项目测试命令并说明依据"` -> completed, AgentRun `cc37ded2-695c-42c1-81b2-b805090abe04`.
- `.venv\Scripts\agentend.exe serve --home .tmp\agent-orchestration-home --once` -> no work, exit 0.
- `git diff --check` -> exit 0; Windows CRLF warnings only.

Residual risks:
- First selector is rule-based and intentionally conservative; more eval cases are needed before trusting it for broad autonomous code modification.
- Worker concurrency is intentionally limited to `--max-concurrency 1`.
- Supersede behavior is represented by merge/update provenance in this slice; richer conflict classification can be added later without changing the tables.

## 8. Selector Trace and Memory Supersede Requirements - 2026-05-07

Scope for the next slice:
- Prioritize selector explainability and calibration before worker concurrency.
- Then add explicit memory supersede/conflict/reinforce decisions.
- Keep the implementation local and deterministic; do not introduce a ranking model, remote service, approval layer, or distributed worker.

Selector requirements:
- The selector must persist a trace for every AgentRun iteration.
- The trace must include the selected action, goal type, top candidates, score breakdown, and rejected reasons.
- Score breakdown must separate at least: base score, Goal Analyzer candidate bonus, trigger/text match, fallback match, input fit, side-effect fit, recent failure penalty, and effectiveness signal.
- Effectiveness should use recent events before lifetime aggregates so old successes do not permanently dominate.
- A failed previous observation must create an explicit rejection or penalty in the next selector trace.
- `agentend agent show` and `agentend agent iterations` must expose the trace through existing iteration plan JSON.
- Existing `select_next_action(...)` must remain compatible for callers that only need the selected action.

Memory supersede requirements:
- Consolidator must recognize explicit candidate tags:
  - `supersedes:<memory_id>`: create or keep the new memory active, mark the old memory `superseded`, remove it from default memory retrieval, and record provenance.
  - `conflicts:<memory_id>`: if confidence is not strong enough to supersede, keep the old memory active, mark the candidate `conflict`, and record a conflict link.
  - same merge key and compatible content: reinforce or merge the existing memory without creating duplicates.
- Superseded memories must not appear in `memory search` default results.
- Consolidation results must report created, merged, skipped, superseded, conflict, and reinforced counts.
- Candidate decisions must be inspectable through `memory candidates`.

Acceptance:
- A selector trace test proves top candidates and score breakdown are recorded in AgentIteration plan JSON.
- A calibration test proves recent failures can lower a capability below a viable alternative.
- A supersede test proves old memory becomes `superseded` and disappears from search.
- A conflict test proves low-confidence contradictory memory does not overwrite active memory.

## 9. Selector and Supersede Implementation Backfill - 2026-05-07

Status: O16-O21 implemented.

Implemented:
- `select_next_action_with_trace(...)` now returns a selected action plus selector trace.
- `select_next_action(...)` remains a compatibility wrapper.
- AgentRunController writes `selector_trace` into `AgentIteration.plan_json`.
- Selector trace includes selected action, goal type, ranked candidates, score breakdown, rejected reasons, and input preview.
- Recent `CapabilityEffectivenessEvent` rows now influence selector scoring before lifetime aggregate fallback.
- Memory consolidator now handles `supersedes:<memory_id>`, `conflicts:<memory_id>`, same-key `reinforced`, and same-key `merged` decisions.
- Consolidation output reports `superseded`, `conflicts`, and `reinforced` counts.

Verification:
- `.venv\Scripts\python.exe -m pytest tests\test_agent_selector_trace.py tests\test_agent_memory_consolidator.py -q --basetemp=.tmp\agent-selector-memory-green2 -p no:cacheprovider` -> 5 passed.
- `.venv\Scripts\python.exe -m pytest tests\test_agent_run_controller.py tests\test_agent_tool_first_selector.py tests\test_agent_memory_consolidator.py tests\test_agent_effectiveness.py tests\test_agent_worker.py tests\test_agent_orchestration_eval.py tests\test_agent_selector_trace.py -q --basetemp=.tmp\agent-orchestration-selector-memory -p no:cacheprovider` -> 14 passed.
- `.venv\Scripts\python.exe -m pytest tests -q --basetemp=.tmp\agent-orchestration-full-3 -p no:cacheprovider` -> 136 passed.
- `.venv\Scripts\python.exe -m compileall -q src tests` -> passed.
- `.venv\Scripts\agentend.exe eval run tool-first --home .tmp\agent-selector-memory-home` -> passed, Eval `2bfb0c23-a973-42ba-97b0-2f1394a0c97b0`.
- `.venv\Scripts\agentend.exe eval run skill-effectiveness --home .tmp\agent-selector-memory-home` -> passed, Eval `8d03a640-57e5-417e-bdf7-1327dbe8afa0`.
- `.venv\Scripts\agentend.exe eval run memory-consolidation --home .tmp\agent-selector-memory-home` -> passed, Eval `fa78fa96-30ab-45e8-9a91-67d70ca8cdc0`.

Residual:
- Selector is still deterministic rule scoring; trace makes tuning inspectable but does not replace broader eval calibration.
- Supersede/conflict decisions require explicit candidate relation tags in this slice; automatic semantic contradiction detection remains future work.
- Eval runs should use separate homes or run sequentially against one SQLite home; concurrent initialization against one home can race on builtin skill registration.

## 10. Memory Relation and Init Stability Requirements - 2026-05-07

Scope for the next slice:
- Add semantic memory relation classification without embeddings or a broad hard-rule engine.
- Keep relation classification metadata-first: shortlist by `type`, `scope`, `merge_key`, `subject` tags, and ordinary tags, then classify the relationship.
- Make database initialization safe for concurrent eval or worker startup on one SQLite home.
- Make eval suite execution isolated by default when the CLI can own the eval home.

MemoryRelationClassifier requirements:
- Introduce a `MemoryRelationClassifier` service used only by Memory Consolidator.
- Candidate matching must first build a small shortlist from existing active memories using:
  - candidate `type`
  - candidate `scope`
  - candidate `merge_key`
  - `subject:<value>` tags
  - non-control candidate tags
- The relationship classifier output must be structured and fixed to:
  - `reinforces`
  - `updates`
  - `conflicts`
  - `unrelated`
- The output must also include `target_memory_id`, `confidence`, `replacement_content`, `reason`, and evidence references.
- The first implementation may use deterministic local classification when no LLM route is configured, but its public contract must be the same structured schema.
- Low confidence relation decisions must not write active long-term memory.
- Medium confidence reinforces may update provenance only.
- High confidence updates may supersede the old memory.
- High confidence conflicts may supersede only when the candidate carries direct evidence tags such as `evidence:tool`, `evidence:test`, `evidence:file`, or `evidence:artifact`; otherwise the candidate must become `conflict_candidate`.

Candidate status requirements:
- Extend accepted candidate statuses with:
  - `needs_review`
  - `conflict_candidate`
  - `reinforced`
  - `superseded`
- Existing statuses `pending`, `created`, `merged`, `skipped`, and `conflict` remain valid for compatibility.
- `memory candidates` must expose the new statuses through the existing listing output.

CLI requirements:
- `agentend memory consolidate` must gain `--auto-relations/--no-auto-relations`.
- `--auto-relations` is enabled by default.
- Explicit tags (`supersedes:<id>`, `conflicts:<id>`) still take precedence over auto relation classification.

DB initialization stability requirements:
- SQLite engines must set a finite busy timeout suitable for concurrent local startup.
- Database initialization must be guarded by a home-local file lock.
- Builtin skill registration must be idempotent at the database write level and must tolerate duplicate startup attempts.
- Short SQLite lock conflicts during initialization should retry with bounded backoff.

Eval home requirements:
- `agentend eval run <suite> --home <base-home>` should run the suite in a suite-specific child home by default.
- `agentend eval run <suite> --home <base-home> --shared-home` should preserve the old behavior and write results into the provided home.
- `agentend eval report <eval_id> --home <base-home>` must find reports written by suite-isolated eval homes.
- Suite-isolated eval results must include the effective eval home path in the report payload.

Acceptance:
- Tests prove auto relation classification turns a high-confidence update into a supersede without explicit `supersedes:<id>` tag.
- Tests prove low-confidence conflict becomes `needs_review` or `conflict_candidate` and does not overwrite active memory.
- Tests prove `memory consolidate --no-auto-relations` leaves an otherwise related low-confidence candidate unpromoted by relation logic.
- Tests prove repeated builtin skill initialization against the same home is idempotent.
- Tests prove SQLite engine configuration includes a busy timeout.
- Tests prove eval CLI uses suite-isolated child homes by default and `--shared-home` preserves old behavior.

## 11. Memory Relation and Init Stability Implementation Backfill - 2026-05-07

Status: O22-O26 implemented.

Implemented acceptance scope:
- `MemoryRelationClassifier` shortlists active memories by metadata and returns structured relation decisions.
- `memory consolidate` defaults to `--auto-relations` and supports `--no-auto-relations`.
- Auto relation decisions can mark candidates `needs_review`, `conflict_candidate`, `reinforced`, or `superseded`.
- High-confidence update/conflict with direct evidence supersedes old memory and keeps provenance links.
- Low-confidence conflict becomes inspectable `needs_review` and does not write a new active long-term memory.
- SQLite engine now configures busy timeout and WAL.
- `init_database()` is protected by a home-local `.agentend-init.lock`.
- Builtin skill registration now uses SQLite upsert semantics and preserves existing `enabled` state on updates.
- CLI eval runs in a suite-isolated child home by default and supports `--shared-home`.
- Isolated eval reports are indexed back into the base home and include `effective_home` and `shared_home`.

Verification evidence:
- Red test run before implementation: 6 failed, 2 passed.
- `.venv\Scripts\python.exe -m pytest tests\test_agent_memory_relation.py tests\test_agent_db_init_stability.py tests\test_agent_orchestration_eval.py -q --basetemp=.tmp\agent-memory-relation-green2 -p no:cacheprovider` -> 9 passed.
- `.venv\Scripts\python.exe -m pytest tests\test_agent_run_controller.py tests\test_agent_tool_first_selector.py tests\test_agent_selector_trace.py tests\test_agent_memory_consolidator.py tests\test_agent_memory_relation.py tests\test_agent_effectiveness.py tests\test_agent_worker.py tests\test_agent_orchestration_eval.py tests\test_agent_db_init_stability.py -q --basetemp=.tmp\agent-memory-relation-related2 -p no:cacheprovider` -> 22 passed.
- `.venv\Scripts\python.exe -m pytest tests -q --basetemp=.tmp\agent-orchestration-full-6 -p no:cacheprovider` -> 144 passed.
- `.venv\Scripts\python.exe -m compileall -q src tests` -> passed.
- `.venv\Scripts\agentend.exe eval run memory-consolidation --home .tmp\agent-memory-relation-home` -> passed, Eval `2e163b23-2339-4352-bf49-0c0f6fc3a8e2`, `shared_home=false`.
- `.venv\Scripts\agentend.exe eval run tool-first --home .tmp\agent-memory-relation-home --shared-home` -> passed, Eval `9c8684ab-6e81-43dc-955b-1be4d01fbbe9`, `shared_home=true`.
- `git diff --check` -> exit 0; Windows CRLF warnings only.

Residual:
- The classifier is schema-driven and metadata-scoped. It can consume structured LLM JSON when a relation backend is available, and falls back to the verified local conservative classifier for offline operation.
- Auto supersede is intentionally gated by confidence and direct evidence to avoid polluting long-term memory.
- Eval isolation changes CLI defaults; tests or workflows that intentionally depend on base-home state must pass `--shared-home`.

## 12. Review Remediation Requirements - 2026-05-07

Status: planned for immediate remediation.

Requirements:
- `agentend agent resume <agent_run_id>` must resume the same AgentRun record and append iterations after the last recorded iteration.
- Resume must not re-execute previous iterations. Existing observations must be used only as selector/evaluator context.
- A completed AgentRun must not be duplicated by resume; the command should report the existing final result.
- `agent-replan` eval must create a deterministic offline first-action failure and prove a different second action was selected.
- Medium-confidence auto relation `updates` decisions must become `needs_review` instead of falling through to ordinary active memory creation.

Verification targets:
```bash
.venv\Scripts\python.exe -m pytest tests\test_agent_run_controller.py tests\test_agent_orchestration_eval.py tests\test_agent_memory_relation.py -q --basetemp=.tmp\agent-review-remediation-red -p no:cacheprovider
.venv\Scripts\agentend.exe eval run agent-replan --home .tmp\agent-review-remediation-home
```

## 13. Review Remediation Implementation Backfill - 2026-05-07

Status: O27-O29 implemented.

Implemented acceptance scope:
- `agentend agent resume <agent_run_id>` now resumes the existing AgentRun through `AgentRunController.resume(...)`.
- Resume rebuilds selector context from persisted previous observations and appends new iterations after the last recorded iteration.
- Completed AgentRuns return the existing final result instead of creating duplicates.
- The selector now applies a stronger previous-iteration failure penalty, allowing a viable fallback action to win on the next loop.
- `agent-replan` eval now forces an offline first-action failure and asserts that the second action differs.
- Medium-confidence auto relation `updates` decisions now become `needs_review`, link to the target memory, and keep the target active.

Verification evidence:
- Red tests before implementation: 3 failed.
- Focused remediation tests: 3 passed.
- Related orchestration tests: 25 passed.
- Full suite: 147 passed.
- Compileall: passed.
- `agent-replan`, `memory-consolidation`, `orchestration-smoke`, and `long-task-worker` evals passed.

## 14. Review Remediation Requirements - Evaluator, Eval Fixture, Resume Memory - 2026-05-07

Status: planned for immediate remediation.

Requirements:
- AgentRun evaluation must check simple goal-specific satisfaction signals, not only `status=completed` and non-empty output.
- For test-command goals, success requires output that includes test-command evidence such as `pytest` or an explicit test command.
- Echoing the goal text, including words like `test command`, must not satisfy the evidence gate unless real test tooling evidence is present.
- `agent-replan` eval must restore any builtin skill workflow it mutates, including when run with `--shared-home`.
- Resuming a previously failed AgentRun to completion must allow memory candidate extraction to reflect the new final status.
- After a missing-evidence observation on a test-command goal, the selector must prefer a direct command probe over another non-evidence skill loop when such a tool is available.

Verification targets:
```bash
.venv\Scripts\python.exe -m pytest tests\test_agent_run_controller.py tests\test_agent_orchestration_eval.py tests\test_agent_memory_consolidator.py -q --basetemp=.tmp\agent-review-remediation-2-red -p no:cacheprovider
.venv\Scripts\agentend.exe eval run agent-replan --home .tmp\agent-review-remediation-2-home --shared-home
```

## 15. Review Remediation Implementation Backfill - Evaluator, Eval Fixture, Resume Memory - 2026-05-07

Status: O30-O32 implemented.

Implemented acceptance scope:
- AgentRun evaluator now marks completed observations incomplete when a test-command goal lacks concrete test-tool evidence.
- Test-command evidence is restricted to command/tool markers such as `pytest`, `python -m pytest`, `unittest`, `tox`, `py.test`, or `nox`; goal text echo is not enough.
- Missing evidence is persisted in `incomplete_conditions`, and the action is recorded as effectiveness failure with `goal_incomplete`.
- Selector replan scoring now boosts `shell.run` with `replan_probe` after a test-command goal has failed or incomplete previous observations.
- `agent-replan --shared-home` captures the original `code.local_task` workflow and restores it in `finally`.
- Memory candidate extraction is status-sensitive for AgentRun candidates, so a failed-then-completed resume can add `successful_procedure` after an earlier `failure_lesson`.

Verification evidence:
- Focused O30-O32 tests: 3 passed.
- Selector replan regression tests: 3 passed.
- Related orchestration tests: 28 passed.
- Closeout resume boundary tests added for completed/cancelled runs.
- Closeout focused tests: 7 passed.
- Closeout related orchestration tests: 30 passed.
- Full suite: 152 passed.
- Compileall: passed.
- `agent-replan --shared-home`, `orchestration-smoke`, `memory-consolidation`, and `long-task-worker` evals passed.
- A normal `agent run` in the same home after shared-home `agent-replan` completed with `pytest 8.4.2`, proving fixture restoration and selector command probing.
- `git diff --check`: exit 0; Windows CRLF warnings only.
