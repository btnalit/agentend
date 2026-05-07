# AgentEnd Agentic Orchestration 任务文档

## 1. 任务目标

按垂直切片实现 AgentEnd Agentic Orchestration。每个切片必须形成可运行、可测试、可审查的行为闭环，避免只铺数据表或只写抽象接口。

## 2. 标记说明

- `AFK`：可自动推进。
- `HITL`：需要用户确认产品取舍或外部配置。
- `Blocked`：依赖前置任务。
- `Done`：已完成。

## 3. 推荐落地顺序

```text
Phase O: Agent 执行循环底座
  O1 AgentRun 数据模型和 CLI 只读面
  O2 AgentRunController 单轮执行
  O3 Evaluator + Replanner 循环

Phase P: 工具优先和能力效果
  O4 Tool-first Selector
  O5 Capability Effectiveness Store
  O6 Skill effectiveness CLI 和排序接入

Phase Q: 跨会话记忆优化
  O7 Memory Candidate 模型和提取器
  O8 Memory Consolidator 合并/更新/supersede
  O9 Memory 影响下一次 AgentRun

Phase R: 长任务执行
  O10 Long Task iteration checkpoint 和 progress artifact
  O11 agentend serve worker loop
  O12 task/schedule/inbox 接入 AgentRunController

Phase S: 入口和回归
  O13 可选 chat/Telegram agent mode
  O14 Orchestration/Memory/Worker eval suites
  O15 文档回填和发布前审计
```

## 4. 任务列表

### O1 AgentRun 数据模型和 CLI 只读面 `AFK`

目标：新增 agent run 顶层记录和 iteration 记录，为后续循环提供持久化载体。

范围：

- 新增 `agent_runs` 表。
- 新增 `agent_iterations` 表。
- 新增 DB model。
- 新增 CLI：
  - `agentend agent show <agent_run_id>`
  - `agentend agent iterations <agent_run_id>`
- 不实现执行逻辑，只能展示已有记录。

验收：

```bash
agentend agent show <agent_run_id>
agentend agent iterations <agent_run_id>
```

测试映射：

- 创建 agent run fixture 后可 show。
- iteration 按 index 排序输出。
- unknown id 返回清晰错误。

### O2 AgentRunController 单轮执行 `AFK`

目标：实现最小 agent run，完成 goal -> select -> act -> observe -> finish 的单轮闭环。

范围：

- 新增 `AgentRunController`。
- 新增 `AgentRunRequest`、`AgentRunResult`。
- 新增 `agentend agent run "..."`。
- 首版只允许执行一个 action。
- action 支持 `skill_run`、`workflow_run`、`tool_call`、`llm_reason`、`finish`。
- 复用 Goal Analyzer、WorkflowRunner、ToolRegistry。

验收：

```bash
agentend agent run "列出当前项目测试命令并说明依据"
agentend agent show <agent_run_id>
```

测试映射：

- 对代码/测试类 goal 至少选择 `code.local_task` 或 `shell.run`。
- agent run 最终 status 为 completed。
- iteration 记录 selected_action、observation、evaluation。

### O3 Evaluator + Replanner 循环 `AFK`

目标：让一次失败或未完成 observation 可以驱动下一轮行动。

依赖：O2。

范围：

- 新增 evaluator 输出 schema。
- AgentRunController 支持 max_iterations。
- Replanner suggestion 进入下一轮 selector context。
- stop_reason 支持：
  - `success`
  - `needs_input`
  - `max_iterations_reached`
  - `failed`
  - `cancelled`

验收：

```bash
agentend agent run "使用不可用搜索 provider 生成一段可恢复报告" --max-iterations 3
```

测试映射：

- 第一个 tool action 失败后，第二轮使用 fallback action 或 ask_user。
- 达到 max_iterations 后停止，不无限循环。
- final_result 包含未完成条件。

### O4 Tool-first Selector `AFK`

目标：把 Goal Analyzer 候选能力变成实际执行选择，而不是只写入结果。

依赖：O2。

范围：

- 新增 selector service。
- 读取 Capability Map、enabled skills、workflows、tools。
- 规则化排序：
  - text match
  - required input fit
  - output fit
  - side effect fit
  - eval pass bonus
- 如果选择 `llm_reason`，必须记录 no_tool_reason。

验收：

```bash
agentend agent run "搜索 AgentEnd 文档并生成报告"
agentend agent run "读取当前项目的 README 和测试命令"
```

测试映射：

- 搜索类 goal 优先 `research.report` 或 `web.search`。
- 文件类 goal 优先 `file.workspace_ops` 或 `fs.*`。
- 无匹配能力时才走 `llm_reason`。

### O5 Capability Effectiveness Store `AFK`

目标：记录 tool/skill/workflow 的实际效果，为后续排序提供反馈。

依赖：O2。

范围：

- 新增 `capability_effectiveness`。
- 新增 `capability_effectiveness_events`。
- 每次 action 完成后记录 status、duration、error_code、artifact count。
- 聚合 attempts/successes/failures/blocked。

验收：

```bash
agentend capabilities effectiveness show shell.run
agentend skills effectiveness show code.local_task
```

测试映射：

- 成功 action 增加 successes。
- 失败 action 增加 failures 并记录 error_code。
- blocked action 不计为 success。

### O6 Skill effectiveness CLI 和排序接入 `AFK`

目标：让 skill 历史效果影响 selector 排序。

依赖：O4、O5。

范围：

- 新增 `skills effectiveness show <skill_id>`。
- Selector 对高成功率 skill 加权。
- Selector 对连续失败 skill 降权。
- 低样本能力只轻微加权。

验收：

```bash
agentend skills effectiveness show research.report
```

测试映射：

- 构造两个同类 skill，一个近期成功，一个连续失败，selector 选择近期成功 skill。
- 样本数不足时不完全压制新 skill。

### O7 Memory Candidate 模型和提取器 `AFK`

目标：从 agent run/episode 中提取候选记忆，不直接写入长期 memory。

依赖：O2。

范围：

- 新增 `memory_candidates`。
- 新增 `memory_links` 或等价 provenance 记录。
- 新增 candidate extractor。
- candidate 类型：
  - project_fact
  - user_preference
  - successful_procedure
  - failure_lesson
  - tool_effectiveness
  - skill_effectiveness
  - task_state

验收：

```bash
agentend memory candidates --agent-run <agent_run_id>
```

测试映射：

- 完成 run 产生至少一个 procedure 或 project_fact 候选。
- 失败 run 产生 failure_lesson 候选。
- 候选包含 merge_key、confidence、source run。

### O8 Memory Consolidator 合并/更新/supersede `AFK`

目标：把候选记忆合并到 Memory Store，避免重复和冲突污染。

依赖：O7。

范围：

- 新增 `memory consolidate --run <run_id>`。
- 相同 merge_key 更新已有 memory。
- 相似内容合并 tags/confidence/provenance。
- 冲突内容 supersede 旧 memory。
- 低置信候选只写 episode/task scope。

验收：

```bash
agentend memory consolidate --run <run_id>
agentend memory search "测试命令"
```

测试映射：

- 重复候选不会新增多条长期 memory。
- 新事实替代旧事实时，旧 memory 不再默认可见。
- memory 保留 created_by_run_id 和 evidence_artifact_id。

### O9 Memory 影响下一次 AgentRun `AFK`

目标：让跨会话记忆真正影响下一次执行，而不是只可查询。

依赖：O4、O8。

范围：

- AgentRunController 构建 controller context 时检索 relevant memory。
- Selector 使用 procedural/performance memory。
- Goal package builder 使用 user/project memory 作为约束参考。
- iteration 记录 memory ids。

验收：

```bash
agentend agent run "按这个项目的习惯跑测试"
agentend agent show <agent_run_id>
```

测试映射：

- 前一次 consolidated 的测试命令在下一次 run 中被检索。
- selector 因 procedural memory 选择正确工具或命令。
- 低置信或过期 memory 不影响选择。

### O10 Long Task iteration checkpoint 和 progress artifact `AFK`

目标：每个长任务 iteration 有可恢复状态和可读进度。

依赖：O2、O3。

范围：

- AgentRunController 每轮写 progress artifact。
- iteration 关联 checkpoint/resume cursor。
- AgentRun 支持 heartbeat_at。
- `agent show` 展示当前进度摘要。

验收：

```bash
agentend agent run "分三步整理当前项目文档" --max-iterations 3
agentend agent show <agent_run_id>
```

测试映射：

- 每轮 iteration 都产生 progress artifact。
- 失败后能展示 next action 或 blocked_on。
- resume 不重跑已完成 iteration。

### O11 agentend serve worker loop `AFK`

目标：新增长期运行服务入口，持续处理本地任务队列。

依赖：O10。

范围：

- 新增 `agentend serve`。
- 参数：
  - `--once`
  - `--poll-interval`
  - `--max-concurrency 1`
- 处理 due schedule、pending task、file inbox。
- 记录 worker heartbeat。
- 首版只要求单并发。

验收：

```bash
agentend serve --once
agentend serve --poll-interval 10 --max-concurrency 1
```

测试映射：

- `serve --once` 能处理一个 pending task。
- 没有任务时输出 no work，不失败。
- running task 超时后可恢复或标记 blocked。

### O12 task/schedule/inbox 接入 AgentRunController `AFK`

目标：自动化入口不再只跑一次 workflow，而是通过 agent loop 处理长任务。

依赖：O11。

范围：

- TaskItem 增加 agent_run_id。
- schedule due 创建 task 后由 AgentRunController 执行。
- inbox task 输入保持 source_path/source_hash/batch_id。
- run_mode 继续传入 action policy。

验收：

```bash
agentend tasks add "读取 docs 并总结下一步" --workflow simple_chat
agentend serve --once
agentend tasks list
```

测试映射：

- task 完成后有关联 agent_run_id。
- schedule 连续失败仍会 auto-pause。
- inbox 去重逻辑不被 agent loop 破坏。

### O13 可选 chat/Telegram agent mode `HITL`

目标：为 chat 和 Telegram 增加可选 agent mode，但首版不改变默认行为。

依赖：O12。

范围：

- 配置：
  - `conversation.default_mode = "workflow" | "agent"`
- CLI 覆盖：
  - `agentend chat --agent`
- Telegram 可配置是否默认使用 agent mode。

验收：

```bash
agentend chat --agent --message "列出项目测试命令"
```

测试映射：

- 默认 chat 仍走原 workflow。
- `--agent` 产生 agent_run_id。
- Telegram 多用户绑定仍按 channel + external_user_id 生效。

### O14 Orchestration/Memory/Worker eval suites `AFK`

目标：新增任务级回归，防止 agent loop、memory 和 worker 退化。

依赖：O3、O8、O11。

范围：

- `orchestration-smoke`
- `tool-first`
- `memory-consolidation`
- `skill-effectiveness`
- `long-task-worker`
- `agent-replan`

验收：

```bash
agentend eval run orchestration-smoke
agentend eval run memory-consolidation
agentend eval run long-task-worker
```

测试映射：

- eval report 输出 agent_run_id。
- 失败 case 能定位 iteration/action/memory candidate。
- suite 可离线运行，不依赖真实外部 API。

### O15 文档回填和发布前审计 `AFK`

目标：回填本目录四件套，形成完成证据。

依赖：O14。

范围：

- 更新 requirements/design/taskboard/audit。
- 标记完成任务。
- 记录验证命令和结果。
- 说明残留风险。

验收：

```bash
.venv\Scripts\python.exe -m pytest tests -q --basetemp=.tmp\agent-orchestration-full -p no:cacheprovider
.venv\Scripts\agentend.exe eval run orchestration-smoke --home .tmp\agent-orchestration-home
.venv\Scripts\agentend.exe eval run memory-consolidation --home .tmp\agent-orchestration-home
.venv\Scripts\agentend.exe eval run long-task-worker --home .tmp\agent-orchestration-home
git diff --check
```

## 5. 首版完成定义

- `agentend agent run` 可完成一个工具优先的真实本地任务。
- AgentRunController 支持至少 3 轮 iteration 和明确 stop_reason。
- Goal Analyzer 候选会实际影响 action selection。
- Replanner suggestion 能驱动下一轮 action。
- Memory Consolidator 能生成、合并、更新跨会话记忆。
- 下一次 agent run 能使用上一次沉淀的 project/procedural memory。
- Skill/tool effectiveness 能影响候选排序。
- `agentend serve --once` 能处理 task/schedule/inbox 的最小闭环。
- 新增 eval suite 覆盖 orchestration、memory 和 long task worker。

## 6. Implementation Status - 2026-05-07

All O1-O15 tasks are Done for this first landing slice.

| Task | Status | Evidence |
| --- | --- | --- |
| O1 AgentRun model and read CLI | Done | `agent_runs`, `agent_iterations`, `agent show`, `agent iterations` implemented and tested. |
| O2 AgentRunController single-turn run | Done | `agent run` creates a completed AgentRun with selected action and observation. |
| O3 Evaluator + bounded replanning loop | Done | `max_iterations`, explicit `stop_reason`, and `agent-replan` eval implemented. |
| O4 Tool-first selector | Done | Selector consumes Goal Analyzer candidates and prefers skill/tool/workflow actions before reasoning fallback. |
| O5 Capability effectiveness store | Done | Event and aggregate tables implemented; action completion records success/failure/blocked. |
| O6 Skill effectiveness CLI and ranking | Done | `skills effectiveness show` and effectiveness-aware selector tests pass. |
| O7 Memory candidate model/extractor | Done | Completed AgentRuns produce structured memory candidates with merge keys. |
| O8 Memory consolidator merge/update | Done | `memory consolidate` writes idempotent `agent_consolidator` memories and provenance links. |
| O9 Memory affects next AgentRun | Done | AgentRun plan records retrieved memory ids/summaries for selector context. |
| O10 Progress artifact and heartbeat | Done | Every completed iteration writes a progress artifact linked from iteration/final result/task. |
| O11 `agentend serve` worker loop | Done | `serve --once`, polling loop, and max-concurrency guard implemented. |
| O12 task/schedule/inbox through AgentRunController | Done | Worker claims pending tasks, enqueues due schedules/inbox files, and completes tasks via AgentRun. |
| O13 optional chat agent mode | Done | `agentend chat --agent` implemented; default chat unchanged. |
| O14 orchestration/memory/worker eval suites | Done | Six suites registered; three acceptance suites verified passed. |
| O15 docs/audit backfill | Done | Four orchestration docs updated with implementation and verification evidence. |

Verification commands run:
```bash
.venv\Scripts\python.exe -m pytest tests -q --basetemp=.tmp\agent-orchestration-full-2 -p no:cacheprovider
.venv\Scripts\agentend.exe eval run orchestration-smoke --home .tmp\agent-orchestration-home
.venv\Scripts\agentend.exe eval run memory-consolidation --home .tmp\agent-orchestration-home
.venv\Scripts\agentend.exe eval run long-task-worker --home .tmp\agent-orchestration-home
.venv\Scripts\agentend.exe agent run --home .tmp\agent-orchestration-home "列出项目测试命令并说明依据"
.venv\Scripts\agentend.exe serve --home .tmp\agent-orchestration-home --once
git diff --check
```

Residual risks:
- Selector is still a deterministic first version; broaden eval coverage before relying on it for high-impact autonomous edits.
- Worker remains single-concurrency by design.
- Memory supersede is implemented as merge/update provenance in this slice; semantic conflict detection is future work.

## 7. Next Slice Tasks - Selector and Memory Quality

### O16 Selector trace schema in plan JSON `AFK`

Goal: record why each action was selected.

Acceptance:
- Agent iteration plan contains `selector_trace`.
- Trace contains selected action, goal type, top candidates, score breakdown, and rejected reasons.
- Existing `agent iterations` output exposes this data without a new command.

### O17 Selector score breakdown and recent-effectiveness calibration `AFK`

Goal: make ranking explainable and responsive to recent failures.

Acceptance:
- Score components are named, not just a final number.
- Recent failures penalize a capability before lifetime aggregate successes dominate.
- Existing selector callers remain compatible.

### O18 Selector trace eval/test coverage `AFK`

Goal: prevent regressions in tool-first explainability.

Acceptance:
- Tests prove trace persistence.
- Tests prove calibration changes candidate ordering.

### O19 Memory supersede explicit candidate relation `AFK`

Goal: allow new memory to replace old memory without deleting evidence.

Acceptance:
- Candidate tag `supersedes:<memory_id>` marks old memory `superseded`.
- New memory is active and linked to old memory.
- Default memory search no longer returns the superseded memory.

### O20 Memory conflict and reinforce decisions `AFK`

Goal: avoid overwriting active memory with weak contradictory evidence and avoid duplicate memory for repeated evidence.

Acceptance:
- Candidate tag `conflicts:<memory_id>` with low confidence becomes `conflict`.
- Same merge key and compatible content becomes `reinforced`.
- Consolidation result reports conflict and reinforced counts.

### O21 Documentation and audit backfill `AFK`

Goal: update requirements/design/taskboard/audit with implementation evidence.

Acceptance:
- Four docs include second-slice status and verification commands.
- Residual risks are explicit.

## 8. O16-O21 Completion - 2026-05-07

| Task | Status | Evidence |
| --- | --- | --- |
| O16 Selector trace schema in plan JSON | Done | AgentRun iterations now persist `plan.selector_trace`. |
| O17 Selector score breakdown and recent-effectiveness calibration | Done | Score components are named; recent failure events penalize candidates before aggregate fallback. |
| O18 Selector trace eval/test coverage | Done | `tests/test_agent_selector_trace.py` covers trace persistence and recent-failure override. |
| O19 Memory supersede explicit candidate relation | Done | `supersedes:<memory_id>` marks old memory `superseded`, creates replacement, and links provenance. |
| O20 Memory conflict and reinforce decisions | Done | `conflicts:<memory_id>` keeps old memory active; same-key compatible content becomes `reinforced`. |
| O21 Documentation and audit backfill | Done | Requirements/design/taskboard/audit updated with second-slice evidence. |

Verification commands:
```bash
.venv\Scripts\python.exe -m pytest tests\test_agent_selector_trace.py tests\test_agent_memory_consolidator.py -q --basetemp=.tmp\agent-selector-memory-green2 -p no:cacheprovider
.venv\Scripts\python.exe -m pytest tests -q --basetemp=.tmp\agent-orchestration-full-3 -p no:cacheprovider
.venv\Scripts\python.exe -m compileall -q src tests
.venv\Scripts\agentend.exe eval run tool-first --home .tmp\agent-selector-memory-home
.venv\Scripts\agentend.exe eval run skill-effectiveness --home .tmp\agent-selector-memory-home
.venv\Scripts\agentend.exe eval run memory-consolidation --home .tmp\agent-selector-memory-home
```

Notes:
- Keep eval runs sequential when sharing one SQLite home.
- Automatic semantic contradiction detection remains a later task.

## 9. Next Slice Tasks - Memory Relation and Init Stability

### O22 MemoryRelationClassifier metadata shortlist `AFK`

Goal: classify candidate-to-memory relationships without embeddings or broad hard rules.

Acceptance:
- New classifier shortlists active memories by type, scope, merge key, subject tags, and ordinary tags.
- Classifier returns structured relation decisions.
- Tests cover `updates`, `conflicts`, `reinforces`, and low-confidence `needs_review` behavior.

### O23 Candidate statuses and auto relation consolidation `AFK`

Goal: let consolidation use relation decisions safely.

Acceptance:
- Candidate statuses include `needs_review`, `conflict_candidate`, `reinforced`, and `superseded`.
- High-confidence update supersedes old memory and preserves links.
- Low-confidence conflict does not overwrite active memory.
- Explicit relation tags still override auto relation classification.

### O24 `memory consolidate --auto-relations` CLI `AFK`

Goal: expose safe auto relation behavior while keeping an opt-out.

Acceptance:
- `--auto-relations/--no-auto-relations` exists.
- Auto relations are enabled by default.
- `--no-auto-relations` keeps the older explicit-tag/merge-key behavior.

### O25 DB initialization stability `AFK`

Goal: make local SQLite startup robust under concurrent eval or worker initialization.

Acceptance:
- SQLite busy timeout is configured.
- Init is protected by a home-local file lock.
- Builtin skill registration tolerates duplicate startup attempts.
- Repeated initialization produces one builtin skill row per skill.

### O26 Eval suite home isolation `AFK`

Goal: avoid concurrent eval pollution and builtin initialization races.

Acceptance:
- CLI eval uses suite-isolated child homes by default.
- `--shared-home` preserves old behavior.
- Base-home `eval report` can read isolated eval reports.
- Report payload records effective home and shared-home mode.

## 10. O22-O26 Pre-Implementation Checklist - 2026-05-07

Planned verification:
```bash
.venv\Scripts\python.exe -m pytest tests\test_agent_memory_relation.py tests\test_agent_memory_consolidator.py tests\test_agent_db_init_stability.py tests\test_agent_orchestration_eval.py -q --basetemp=.tmp\agent-memory-relation-red -p no:cacheprovider
.venv\Scripts\python.exe -m pytest tests -q --basetemp=.tmp\agent-orchestration-full-4 -p no:cacheprovider
.venv\Scripts\python.exe -m compileall -q src tests
.venv\Scripts\agentend.exe eval run memory-consolidation --home .tmp\agent-memory-relation-home
.venv\Scripts\agentend.exe eval run tool-first --home .tmp\agent-memory-relation-home --shared-home
git diff --check
```

## 11. O22-O26 Completion - 2026-05-07

| Task | Status | Evidence |
| --- | --- | --- |
| O22 MemoryRelationClassifier metadata shortlist | Done | `src/agentend/core/memory_relation.py` added with structured decision contract and metadata shortlist. |
| O23 Candidate statuses and auto relation consolidation | Done | Consolidator now handles `needs_review`, `conflict_candidate`, `reinforced`, and `superseded`. |
| O24 `memory consolidate --auto-relations` CLI | Done | CLI defaults to auto relations and supports `--no-auto-relations`. |
| O25 DB initialization stability | Done | SQLite busy timeout/WAL, init file lock, and builtin skill upsert implemented. |
| O26 Eval suite home isolation | Done | CLI eval defaults to isolated child homes; `--shared-home` preserves stateful eval behavior. |

Verification commands:
```bash
.venv\Scripts\python.exe -m pytest tests\test_agent_memory_relation.py tests\test_agent_db_init_stability.py tests\test_agent_orchestration_eval.py -q --basetemp=.tmp\agent-memory-relation-green2 -p no:cacheprovider
.venv\Scripts\python.exe -m pytest tests\test_agent_run_controller.py tests\test_agent_tool_first_selector.py tests\test_agent_selector_trace.py tests\test_agent_memory_consolidator.py tests\test_agent_memory_relation.py tests\test_agent_effectiveness.py tests\test_agent_worker.py tests\test_agent_orchestration_eval.py tests\test_agent_db_init_stability.py -q --basetemp=.tmp\agent-memory-relation-related2 -p no:cacheprovider
.venv\Scripts\python.exe -m pytest tests -q --basetemp=.tmp\agent-orchestration-full-6 -p no:cacheprovider
.venv\Scripts\python.exe -m compileall -q src tests
.venv\Scripts\agentend.exe eval run memory-consolidation --home .tmp\agent-memory-relation-home
.venv\Scripts\agentend.exe eval run tool-first --home .tmp\agent-memory-relation-home --shared-home
git diff --check
```

Results:
- Focused tests: 9 passed.
- Related orchestration tests: 22 passed.
- Full suite: 144 passed.
- `memory-consolidation` eval: passed, suite-isolated home.
- `tool-first` eval: passed, shared home.
- `git diff --check`: exit 0 with CRLF warnings only.

Notes:
- Existing eval tests that intentionally mutate base-home state now use `--shared-home`.
- Default CLI eval isolation reduces accidental cross-suite pollution.

## 12. Review Remediation Tasks - 2026-05-07

### O27 True AgentRun resume `AFK`

Goal: make `agentend agent resume <agent_run_id>` continue the existing AgentRun instead of starting a separate run with the same goal.

Acceptance:
- Resume appends new iterations to the same `agent_runs.id`.
- Existing completed/failed iterations are not rerun.
- Previous observations are passed back to the selector so a failed action can be penalized.
- Completed runs return the existing final result without creating a duplicate run.

### O28 Strong agent-replan eval `AFK`

Goal: make `agent-replan` prove observe -> evaluate -> replan -> different next action.

Acceptance:
- The eval fixture forces the first selected action to fail offline.
- The AgentRun records at least two iterations.
- The second selected action differs from the first selected action.
- The eval report includes the first and second action names for failure diagnosis.

### O29 Medium-confidence memory update review gate `AFK`

Goal: prevent medium-confidence `updates` relation decisions from creating a second active long-term memory.

Acceptance:
- `updates` with confidence below the automatic supersede threshold becomes `needs_review`.
- The target active memory remains active.
- No replacement memory is created until the relation confidence is high enough.

## 13. O27-O29 Completion - 2026-05-07

| Task | Status | Evidence |
| --- | --- | --- |
| O27 True AgentRun resume | Done | `AgentRunController.resume(...)` appends iterations to the same AgentRun and `agent resume` uses it. |
| O28 Strong agent-replan eval | Done | `agent-replan` now forces a failed first action and verifies a different second action. |
| O29 Medium-confidence memory update review gate | Done | Medium-confidence `updates` relation decisions become `needs_review` and do not create active duplicate memory. |

Verification commands:
```bash
.venv\Scripts\python.exe -m pytest tests\test_agent_run_controller.py::test_agent_resume_appends_to_existing_run_without_rerunning_iterations tests\test_agent_orchestration_eval.py::test_agent_replan_eval_proves_failed_action_then_different_action tests\test_agent_memory_relation.py::test_medium_confidence_update_needs_review_instead_of_active_duplicate -q --basetemp=.tmp\agent-review-remediation-green -p no:cacheprovider
.venv\Scripts\python.exe -m pytest tests\test_agent_run_controller.py tests\test_agent_tool_first_selector.py tests\test_agent_selector_trace.py tests\test_agent_memory_consolidator.py tests\test_agent_memory_relation.py tests\test_agent_effectiveness.py tests\test_agent_worker.py tests\test_agent_orchestration_eval.py tests\test_agent_db_init_stability.py -q --basetemp=.tmp\agent-review-remediation-related -p no:cacheprovider
.venv\Scripts\python.exe -m pytest tests -q --basetemp=.tmp\agent-review-remediation-full -p no:cacheprovider
.venv\Scripts\python.exe -m compileall -q src tests
.venv\Scripts\agentend.exe eval run agent-replan --home .tmp\agent-review-remediation-home
.venv\Scripts\agentend.exe eval run memory-consolidation --home .tmp\agent-review-remediation-home
.venv\Scripts\agentend.exe eval run orchestration-smoke --home .tmp\agent-review-remediation-home
.venv\Scripts\agentend.exe eval run long-task-worker --home .tmp\agent-review-remediation-home
```

Results:
- Remediation red tests: 3 failed before implementation.
- Remediation focused tests: 3 passed.
- Related orchestration tests: 25 passed.
- Full suite: 147 passed.
- Compileall: passed.
- `agent-replan`, `memory-consolidation`, `orchestration-smoke`, and `long-task-worker` evals: passed.

## 14. Review Remediation Tasks - Evaluator, Eval Fixture, Resume Memory - 2026-05-07

### O30 Goal satisfaction evaluator gate `AFK`

Goal: prevent AgentRun from marking a completed-but-irrelevant non-empty observation as success.

Acceptance:
- Test-command goals require test-command evidence such as pytest/test command output before `stop_reason=success`.
- Incomplete observations continue/replan until max iterations.
- Final failed result records missing criteria.

### O31 Restore agent-replan shared-home fixture `AFK`

Goal: keep `agent-replan` eval deterministic without leaving the builtin `code.local_task` workflow broken in shared homes.

Acceptance:
- `agent-replan --shared-home` restores the original builtin workflow content before returning.
- The eval report still proves first action failed and second action differed.
- A subsequent normal `agent run` in the same home does not hit `__agentend_missing_replan_fixture__`.

### O32 Resume memory candidate refresh `AFK`

Goal: after a failed AgentRun resumes successfully, memory consolidation must learn the final successful procedure.

Acceptance:
- A failed run can create a `failure_lesson` candidate.
- Resuming the same AgentRun to completion creates or updates a successful candidate for the final status.
- The successful candidate can be consolidated into active project memory.

## 15. O30-O32 Completion - 2026-05-07

| Task | Status | Evidence |
| --- | --- | --- |
| O30 Goal satisfaction evaluator gate | Done | Test-command goals now require concrete command evidence such as `pytest`; goal text echo alone is incomplete. |
| O31 Restore agent-replan shared-home fixture | Done | `agent-replan --shared-home` restores `code.local_task` workflow content in `finally` and still proves a different second action. |
| O32 Resume memory candidate refresh | Done | Status-sensitive extraction allows failed-then-completed AgentRuns to keep failure evidence and add a successful procedure candidate. |

Additional selector fix:
- When a test-command goal has a previous failed/incomplete observation, selector trace gives `shell.run` a `replan_probe` boost so the next iteration can gather command evidence instead of looping across non-evidence skills.

Verification commands:
```bash
.venv\Scripts\python.exe -m pytest tests\test_agent_run_controller.py::test_test_command_goal_does_not_complete_without_test_command_evidence tests\test_agent_run_controller.py::test_resume_success_refreshes_memory_candidates_after_initial_failure tests\test_agent_orchestration_eval.py::test_agent_replan_shared_home_restores_builtin_skill_fixture -q --basetemp=.tmp\agent-review-remediation-2-green -p no:cacheprovider
.venv\Scripts\python.exe -m pytest tests\test_agent_run_controller.py::test_agent_run_cli_records_iteration_progress_effectiveness_and_memory_candidate tests\test_agent_run_controller.py::test_agent_resume_appends_to_existing_run_without_rerunning_iterations tests\test_agent_orchestration_eval.py::test_agent_orchestration_eval_suites_are_listed_and_runnable -q --basetemp=.tmp\agent-review-remediation-selector-green -p no:cacheprovider
.venv\Scripts\python.exe -m pytest tests\test_agent_run_controller.py tests\test_agent_tool_first_selector.py tests\test_agent_selector_trace.py tests\test_agent_memory_consolidator.py tests\test_agent_memory_relation.py tests\test_agent_effectiveness.py tests\test_agent_worker.py tests\test_agent_orchestration_eval.py tests\test_agent_db_init_stability.py -q --basetemp=.tmp\agent-review-remediation-2-related-rerun2 -p no:cacheprovider
.venv\Scripts\python.exe -m pytest tests -q --basetemp=.tmp\agent-review-remediation-2-full-rerun2 -p no:cacheprovider
.venv\Scripts\python.exe -m compileall -q src tests
.venv\Scripts\agentend.exe eval run agent-replan --home .tmp\agent-review-remediation-2-home-rerun --shared-home
.venv\Scripts\agentend.exe eval run orchestration-smoke --home .tmp\agent-review-remediation-2-home-rerun
.venv\Scripts\agentend.exe eval run memory-consolidation --home .tmp\agent-review-remediation-2-home-rerun
.venv\Scripts\agentend.exe eval run long-task-worker --home .tmp\agent-review-remediation-2-home-rerun
.venv\Scripts\agentend.exe agent run --home .tmp\agent-review-remediation-2-home-rerun --max-iterations 2 "List the project test command and explain evidence."
git diff --check
```

Results:
- Focused O30-O32 tests: 3 passed.
- Selector replan regression tests: 3 passed.
- Related orchestration tests: 28 passed.
- Closeout resume boundary tests added for completed/cancelled runs.
- Closeout focused tests: 7 passed.
- Closeout related orchestration tests: 30 passed.
- Full suite: 152 passed.
- Compileall: passed.
- `agent-replan --shared-home`, `orchestration-smoke`, `memory-consolidation`, and `long-task-worker` evals passed.
- Normal `agent run` after shared-home `agent-replan` completed with `pytest 8.4.2` output and did not hit the missing fixture.
- `git diff --check`: exit 0 with CRLF warnings only.
