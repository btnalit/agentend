# AgentEnd Agentic Orchestration 设计文档

## 1. 设计目标

本阶段在现有 Action Layer 上新增一层 agentic orchestration。它不替换 `WorkflowRunner`，也不引入多 Agent。设计目标是把已有模块串成可循环、可恢复、可学习的单 Agent：

```text
AgentRunController
  -> Goal Analyzer
  -> Selector
  -> WorkflowRunner / ToolRegistry / SkillRegistry
  -> Observation
  -> Evaluator
  -> Replanner
  -> Memory Consolidator
  -> Effectiveness Store
```

原则：

- `WorkflowRunner` 继续做确定性 workflow DAG 执行。
- `AgentRunController` 负责目标、计划、行动选择、观察、评价和重规划循环。
- Memory Consolidator 只沉淀结构化经验，不保存原始全文历史。
- Long Task Worker 只做单机持久化轮询，不引入外部队列。

## 2. 总体架构

```text
CLI / Telegram / Task / Schedule / Inbox
    ↓
AgentRunController
    ↓
Goal Package Builder
    ↓
Capability + Skill + Workflow Selector
    ├─ ToolRegistry.call(...)
    ├─ SkillRegistry.run(...) -> WorkflowRunner
    ├─ WorkflowRunner.run(...)
    └─ LLMRouter reason/evaluate
    ↓
Observation Recorder
    ↓
Evaluator
    ├─ finish
    ├─ replan
    ├─ ask_user
    └─ continue
    ↓
AgentRun / Iteration / Checkpoint / Progress Artifact
    ↓
Episode Logger + Memory Consolidator + Effectiveness Store
```

## 3. 新增核心对象

### 3.1 AgentRun

`AgentRun` 是一次目标导向 agent 执行的顶层记录。它可以包含一个或多个已有 `Run`，因为一次 agent run 可能调用多个 workflow、skill 或 tool。

建议新增表：

```text
agent_runs
  id
  conversation_id
  channel
  external_user_id
  goal
  goal_package_json
  status
  final_result_json
  stop_reason
  max_iterations
  max_runtime_seconds
  started_at
  heartbeat_at
  completed_at
  created_at
  updated_at
```

状态：

```text
pending -> running -> waiting_input -> completed
                         ↓
                       failed
                         ↓
                      cancelled
```

`waiting_input` 只表示缺少继续执行所需输入，不表示失败。

### 3.2 AgentIteration

每轮循环写入一条 iteration。

```text
agent_iterations
  id
  agent_run_id
  index
  status
  plan_json
  selected_action_json
  observation_json
  evaluation_json
  linked_run_id
  linked_tool_call_id
  checkpoint_id
  error
  started_at
  completed_at
```

Iteration 状态：

```text
planned -> action_running -> observing -> evaluated -> completed
                                              ↓
                                            failed
```

### 3.3 AgentAction

首版不需要独立表；action 可先保存在 `agent_iterations.selected_action_json`。如果后续需要多 action fan-out，再抽成表。

Action schema：

```json
{
  "type": "skill_run",
  "name": "code.local_task",
  "reason": "The goal asks for codebase inspection and test commands.",
  "input": {"task": "..."},
  "expected_observation": "A summary with commands and evidence",
  "fallbacks": ["shell.run", "fs.read_text"]
}
```

支持类型：

- `tool_call`
- `skill_run`
- `workflow_run`
- `llm_reason`
- `ask_user`
- `finish`

## 4. AgentRunController

### 4.1 执行流程

```text
create agent_run
  ↓
build goal package
  ↓
while not stopped:
  retrieve relevant memory and effectiveness
  select next action
  execute action
  record observation
  evaluate progress
  create checkpoint/progress artifact
  if success: finish
  if needs input: waiting_input
  if failed: replan
  if max reached: stop
  ↓
consolidate memory
record effectiveness
summarize episode if linked runs exist
```

### 4.2 伪代码

```python
class AgentRunController:
    def run(self, request: AgentRunRequest) -> AgentRunResult:
        agent_run = self.create_agent_run(request)
        goal_package = self.build_goal_package(request)

        for index in range(goal_package.max_iterations):
            context = self.build_controller_context(agent_run, goal_package)
            action = self.selector.select(goal_package, context)
            iteration = self.record_iteration(agent_run, index, action)

            observation = self.executor.execute(action, agent_run, iteration)
            evaluation = self.evaluator.evaluate(goal_package, observation, context)
            self.record_evaluation(iteration, observation, evaluation)
            self.write_progress(agent_run, iteration)

            if evaluation.decision == "finish":
                return self.finish(agent_run, evaluation)
            if evaluation.decision == "ask_user":
                return self.waiting_input(agent_run, evaluation)
            if evaluation.decision == "replan":
                self.replanner.record(agent_run, iteration, evaluation)
                continue

        return self.stop(agent_run, reason="max_iterations_reached")
```

### 4.3 与 WorkflowRunner 的边界

`WorkflowRunner` 不新增循环语义。AgentRunController 可以多次调用 `WorkflowRunner.run()` 或 `WorkflowRunner.resume()`，每次调用仍是一条确定性的 workflow run。

边界：

| 模块 | 负责 | 不负责 |
| --- | --- | --- |
| AgentRunController | 多轮目标循环、action selection、evaluation、long task progress | 单个 workflow DAG 节点执行细节 |
| WorkflowRunner | workflow schema、节点执行、context pack、tool call、checkpoint | 目标级循环和跨 workflow 重规划 |
| Replanner | 给出下一步建议或替代 action | 自动改写 controller 状态 |
| Memory Consolidator | 生成和合并长期记忆 | 直接决定下一步 action |

## 5. Selector 设计

Selector 负责把 Goal Analyzer 候选能力排序为一个下一步 action。

输入：

- goal package
- Goal Analyzer payload
- Capability Map
- enabled skills
- available workflows
- memory retrievals
- effectiveness stats
- current iteration observations

排序分数：

```text
score =
  text_match
  + output_fit
  + required_inputs_fit
  + recent_success_bonus
  + eval_pass_bonus
  - recent_failure_penalty
  - missing_input_penalty
  - unsuitable_side_effect_penalty
```

首版可以规则化实现，不要求新增复杂 ML 排序。

选择规则：

- 如果 goal 明确是代码/文件/搜索/数据/Telegram 等工具可完成任务，优先 skill 或 tool。
- 如果候选 skill 有通过 eval 且历史成功率更高，优先 skill。
- 如果 skill 连续失败，降级到基础 tool 或 workflow。
- 如果缺少必要输入，返回 `ask_user`。
- 如果没有合适行动能力，返回 `llm_reason`，并记录原因。

## 6. Evaluator 设计

Evaluator 判断本轮 observation 是否让目标更接近完成。

输出：

```json
{
  "decision": "continue",
  "satisfied_criteria": ["..."],
  "missing_criteria": ["..."],
  "confidence": 0.74,
  "reason": "Tool returned project test command but did not verify it.",
  "next_hint": "Run the command or inspect pyproject."
}
```

Decision：

- `finish`
- `continue`
- `replan`
- `ask_user`
- `fail`

首版 evaluator 可以混合规则和 LLM：

- tool error、missing input、budget exceeded 走规则。
- success criteria 判断可走 LLM route `final_evaluate`。
- 所有 evaluator 输出必须写入 iteration。

## 7. Memory Consolidator

### 7.1 触发点

触发点：

- agent run completed/failed
- workflow run completed/failed
- episode summarized
- task completed/blocked
- skill run completed

首版以 agent run 完成后同步触发为主；worker 模式可异步触发。

### 7.2 Pipeline

```text
run + iterations + observations + episode + artifacts
  ↓
extract candidates
  ↓
classify memory type and scope
  ↓
dedupe by merge_key and semantic similarity
  ↓
merge/update/create
  ↓
record memory_consolidation event
```

候选结构：

```json
{
  "type": "successful_procedure",
  "scope": "project",
  "content": "For AgentEnd tests on Windows, use .venv\\Scripts\\python.exe with --basetemp and -p no:cacheprovider.",
  "merge_key": "project:agentend:test-command",
  "confidence": 0.9,
  "source": "agent_consolidator",
  "created_by_run_id": "...",
  "evidence_artifact_id": "...",
  "tags": ["agentend", "pytest", "windows"],
  "ttl": null
}
```

### 7.3 合并策略

- 相同 merge_key：更新已有 memory，追加 provenance。
- 内容高度相似：更新 confidence、last_seen、tags，不新增。
- 内容冲突：新 memory 标记为 active，旧 memory 标记 `superseded`，保留来源链。
- 低置信候选：只写 episode/task scope，不进入 project/user 长期记忆。

建议新增表：

```text
memory_candidates
  id
  agent_run_id
  run_id
  type
  scope
  content
  merge_key
  confidence
  status
  decision_reason

memory_links
  id
  memory_id
  source_type
  source_id
  relation
```

### 7.4 记忆进入下一次执行

AgentRunController 在 build controller context 时读取：

- project/user semantic memory
- relevant procedural memory
- tool/skill performance memory
- current task working memory

Context Runtime 仍负责 LLM prompt 的 memory 注入；Selector 额外读取 performance/procedural memory 影响 action 排序。

## 8. Effectiveness Store

Skill/tool effectiveness 是功能性治理，不是审批治理。

建议新增表：

```text
capability_effectiveness
  id
  capability_type
  capability_id
  goal_type
  attempts
  successes
  failures
  blocked
  avg_duration_ms
  avg_iterations
  last_success_at
  last_failure_at
  common_error_json
  updated_at

capability_effectiveness_events
  id
  agent_run_id
  iteration_id
  capability_type
  capability_id
  status
  error_code
  duration_ms
  output_artifact_count
  created_at
```

Selector 查询 effectiveness summary，避免一直选择近期失败的能力。

## 9. Long Task Worker

### 9.1 Worker loop

```text
agentend serve
  ↓
doctor light check
  ↓
while running:
  run_due_schedules
  scan_inbox_once
  claim pending task
  run task through AgentRunController
  update heartbeat
  sleep poll_interval
```

首版单并发：

```bash
agentend serve --once
agentend serve --poll-interval 10 --max-concurrency 1
```

### 9.2 Task 运行方式

TaskManager 当前可以创建 task、run task、run schedule、scan inbox。本阶段 worker 不绕过 TaskManager，而是增加：

- task claim
- task heartbeat
- task progress artifact
- running 超时恢复
- agent_run_id 关联

建议扩展 `tasks`：

```text
agent_run_id
heartbeat_at
progress_artifact_id
claimed_at
worker_id
resume_cursor_json
```

### 9.3 Progress Artifact

长任务每轮写入一个简短进度文件，便于用户和后续 resume 理解状态：

```json
{
  "agent_run_id": "...",
  "task_id": "...",
  "current_iteration": 3,
  "goal": "...",
  "completed": ["read docs", "ran tests"],
  "current_action": "shell.run pytest",
  "next": "summarize failures",
  "blocked_on": null
}
```

## 10. CLI 设计

新增：

```bash
agentend agent run "..."
agentend agent show <agent_run_id>
agentend agent iterations <agent_run_id>
agentend agent resume <agent_run_id>
agentend agent cancel <agent_run_id>
agentend memory consolidate --run <run_id>
agentend memory candidates --agent-run <agent_run_id>
agentend skills effectiveness show <skill_id>
agentend capabilities effectiveness show <capability_id>
agentend serve --once
agentend serve --poll-interval 10 --max-concurrency 1
```

现有 `agentend chat` 首版保持默认 workflow 路径。后续可以加配置：

```toml
[conversation]
default_mode = "workflow" # workflow | agent
```

## 11. Eval 设计

新增 suites：

| Suite | 目标 |
| --- | --- |
| `orchestration-smoke` | 验证 goal -> action -> observe -> evaluate -> finish |
| `tool-first` | 验证代码/文件/搜索类任务优先调用工具或 skill |
| `memory-consolidation` | 验证 run 完成后生成、合并、检索记忆 |
| `skill-effectiveness` | 验证历史成功/失败影响 selector 排序 |
| `long-task-worker` | 验证 serve --once 处理 task/schedule/inbox 并产生 heartbeat |
| `agent-replan` | 验证工具失败后进入下一轮替代 action |

每个 eval 必须关联 agent_run_id、iteration_id、run_id 或 memory_candidate_id。

## 12. 迁移策略

阶段落地顺序：

1. 新增数据模型和 CLI 只读展示。
2. 新增 AgentRunController skeleton，先支持单轮 skill/workflow action。
3. 增加 evaluator 和 replan loop。
4. 增加 Memory Consolidator 候选生成和合并。
5. 增加 effectiveness 记录和 selector 排序。
6. 增加 Long Task Worker。
7. 将 task/schedule/inbox 接到 AgentRunController。
8. 增加可选 chat/Telegram agent mode。

兼容性：

- 现有 workflow、skill、tools 不需要立即改 schema。
- 现有 `WorkflowRunner.run` 继续可直接调用。
- 现有 `agentend chat` 首版不改变默认行为。
- 新表可通过迁移或初始化时自动创建。

## 13. 风险和控制

- Agent loop 可能无限循环：用 max_iterations、max_runtime_seconds 和 evaluator stop_reason 控制。
- Tool-first 可能选错工具：首版保留 rule-based selector，并用 eval 覆盖高频场景。
- Memory 可能污染上下文：只写结构化候选，低置信只进 episode/task scope，长期记忆必须 merge/dedupe。
- Skill effectiveness 可能早期样本太少：低样本只做轻微加权，不直接禁用能力。
- Worker 可能重复执行任务：使用 task claim、heartbeat、resume cursor 和同一 task 状态机。

## 14. 验收策略

本阶段完成时必须通过：

```bash
.venv\Scripts\python.exe -m pytest tests -q --basetemp=.tmp\agent-orchestration-full -p no:cacheprovider
.venv\Scripts\agentend.exe eval run orchestration-smoke --home .tmp\agent-orchestration-home
.venv\Scripts\agentend.exe eval run memory-consolidation --home .tmp\agent-orchestration-home
.venv\Scripts\agentend.exe eval run long-task-worker --home .tmp\agent-orchestration-home
.venv\Scripts\agentend.exe agent run --home .tmp\agent-orchestration-home "列出项目测试命令"
.venv\Scripts\agentend.exe serve --home .tmp\agent-orchestration-home --once
git diff --check
```

## 15. Implementation Backfill - 2026-05-07

Code mapping:
- `src/agentend/core/agent_run.py`: AgentRunController, request/result dataclasses, iteration loop, evaluator, progress artifact writer, show/iterations/cancel helpers.
- `src/agentend/core/agent_selector.py`: tool-first selector with skill/tool scoring, Goal Analyzer candidate consumption, memory/context plan input, and effectiveness-aware ranking.
- `src/agentend/core/effectiveness.py`: event and aggregate scoring store for tools, skills, and workflows.
- `src/agentend/core/memory_consolidator.py`: candidate extraction, merge-key idempotency, provenance links, and Memory Store writes.
- `src/agentend/core/worker.py`: single-node worker loop for pending tasks, due schedules, and file inbox batches.
- `src/agentend/db/models.py`: new AgentRun, AgentIteration, MemoryCandidate, MemoryLink, CapabilityEffectiveness, CapabilityEffectivenessEvent models and TaskItem worker columns.
- `src/agentend/db/session.py`: incremental SQLite column compatibility for old homes.
- `src/agentend/cli.py`: new agent, memory, effectiveness, serve, and chat agent-mode commands.
- `src/agentend/core/eval_harness.py`: six orchestration eval suites.

Boundary decisions preserved:
- `WorkflowRunner` remains a DAG workflow executor and is reused as an action executor; it was not turned into an agent loop.
- Existing `agentend chat` default behavior remains workflow mode. Agent mode is opt-in via `--agent`.
- No approval/security governance, distributed queue, remote sandbox, or multi-agent scheduler was introduced.

Verification:
- Full suite: 132 passed in 439.17s.
- Orchestration, memory, and worker eval suites passed offline.
- `git diff --check` returned exit code 0 with CRLF warnings only.

## 16. Selector Trace and Memory Supersede Design - 2026-05-07

Selector design:
- Add `select_next_action_with_trace(...)` beside the existing `select_next_action(...)`.
- Keep `select_next_action(...)` as a compatibility wrapper returning only `SelectedAction`.
- Introduce an internal candidate score record with:
  - action type and name
  - final score
  - score_breakdown map
  - rejected_reasons list
  - input preview
- Candidate generation stays deterministic: skill candidates first, tool candidates second, workflow fallback last.
- Candidate scoring becomes explainable by accumulating named score components instead of one opaque float.
- Recent effectiveness is computed from recent `CapabilityEffectivenessEvent` rows, then falls back to aggregate `CapabilityEffectiveness`.
- AgentRunController stores the trace in `AgentIteration.plan_json["selector_trace"]` before action execution.

Memory supersede design:
- Use candidate tags for explicit conflict semantics rather than adding a new semantic search dependency.
- `supersedes:<memory_id>` creates an active replacement memory and marks the referenced memory as `superseded`.
- `conflicts:<memory_id>` marks the candidate as `conflict` unless it also has a supersede tag.
- Same merge key with compatible content becomes `reinforced` when the candidate adds no materially new content, or `merged` when content differs.
- Use `MemoryLink` for provenance:
  - new memory relation `supersedes` to old memory id
  - old memory relation `superseded_by` to new memory id
  - active memory relation `conflicts_with` to candidate id
  - active memory relation `reinforced_by` to candidate id
- Reuse `MemoryItem.status` for `superseded`; existing search visibility already filters to active memory.

Compatibility:
- Existing memory rows remain valid.
- Existing candidate extraction still works; explicit supersede/conflict tags are optional.
- Existing CLI commands continue to work; their output gains extra counts and candidate statuses.

## 17. Selector and Supersede Implementation Backfill - 2026-05-07

Implementation mapping:
- `src/agentend/core/agent_selector.py`
  - Added `SelectionResult`.
  - Added `select_next_action_with_trace(...)`.
  - Reworked scoring into named score components.
  - Added recent-event effectiveness calibration and fallback to aggregate effectiveness.
- `src/agentend/core/agent_run.py`
  - Stores selector trace in iteration plan JSON.
- `src/agentend/core/memory_consolidator.py`
  - Added supersede, conflict, reinforce, and merge decisions.
  - Added new consolidation counters.
  - Uses `remove_memory_fts(...)` when old memory is superseded.
- `src/agentend/cli.py`
  - `memory consolidate` can process all pending candidates when no run filter is supplied.
  - Consolidation output includes superseded/conflicts/reinforced counts.

Trace shape:
```json
{
  "goal_type": "code",
  "selected": {"type": "skill_run", "name": "code.local_task", "score": 7.4},
  "candidates": [
    {
      "type": "skill_run",
      "name": "code.local_task",
      "score": 7.4,
      "score_breakdown": {
        "base": 2.0,
        "goal_analyzer_candidate": 2.0,
        "trigger_match": 1.5,
        "text_match": 0.4,
        "fallback_match": 2.0,
        "input_fit": 1.0,
        "side_effect_fit": 0.5,
        "effectiveness": 0.4,
        "recent_failure_penalty": 0.0
      },
      "rejected_reasons": []
    }
  ]
}
```

Memory relation tags:
- `supersedes:<memory_id>` creates a replacement and marks old memory `superseded`.
- `conflicts:<memory_id>` marks candidate `conflict` without changing active memory.
- same `merge:<merge_key>` and compatible content marks candidate `reinforced`.

## 18. Memory Relation and Init Stability Design - 2026-05-07

MemoryRelationClassifier:
- New module: `src/agentend/core/memory_relation.py`.
- Public contract:
```python
@dataclass(frozen=True)
class MemoryRelationDecision:
    relation: str
    target_memory_id: str | None
    confidence: float
    replacement_content: str | None
    reason: str
    evidence_refs: list[str]
```
- Main API:
```python
class MemoryRelationClassifier:
    def shortlist(self, session, candidate) -> list[MemoryItem]: ...
    def classify(self, session, candidate) -> MemoryRelationDecision: ...
```
- Shortlist is metadata-only. No embeddings are introduced.
- Control tags are ignored for subject matching: `merge:`, `agent_run:`, `run:`, `type:`, `supersedes:`, `conflicts:`, `evidence:`.
- `subject:<value>` tags have the highest shortlist weight.
- Same `merge_key` remains a direct relation path and does not need LLM classification.
- Structured LLM JSON decisions can replace the fallback when a usable LLM route is available.
- The deterministic fallback classifier uses simple compatibility and contradiction cues only after shortlist narrowing.

Consolidator decision gates:
- Explicit relation tags run first and override auto relation classification.
- If no explicit relation tag exists and `auto_relations=True`, the consolidator calls `MemoryRelationClassifier`.
- Relation handling:
  - `reinforces`: mark candidate `reinforced`, link `reinforced_by`, update tags/provenance/confidence without duplicate memory.
  - `updates` with confidence >= 0.85: create replacement memory, mark old memory `superseded`, remove old FTS entry, link `supersedes` and `superseded_by`, mark candidate `superseded`.
  - `conflicts` with confidence >= 0.85 and direct evidence: same replacement path as update.
  - `conflicts` without direct evidence: mark `conflict_candidate`, link `conflicts_with`, keep old active.
  - confidence < 0.65: mark `needs_review`, do not write active long-term memory.
  - `unrelated`: fall through to existing create/merge confidence rules.

DB initialization stability:
- `create_sqlite_engine(home)` configures:
  - `connect_args={"timeout": 30}`
  - `PRAGMA busy_timeout=30000`
  - `PRAGMA journal_mode=WAL`
- `init_database(home)` obtains a home-local `.agentend-init.lock` file lock before create-all, incremental columns, and builtin initialization paths that call it.
- File lock implementation stays local and cross-process on Windows using an atomic lock file with bounded wait and stale lock cleanup.
- Builtin skill registration is made idempotent with retry around duplicate insert / locked database windows, preserving existing skill `enabled` state.

Eval suite home isolation:
- `eval run` gets `--shared-home/--isolated-home`.
- Default behavior with `--home <base>`:
  - effective home is `<base>/eval-homes/<suite>-<eval_id_prefix or timestamp>`.
  - the effective home is initialized before running the suite.
  - the report row is also indexed into `<base>` so `eval report --home <base>` can find it.
- `--shared-home` uses the provided home exactly as before.
- Reports include:
```json
{
  "suite": "tool-first",
  "effective_home": "...",
  "shared_home": false
}
```

Compatibility:
- Existing tests that call `run_eval_suite(home, session, suite)` directly still use the provided home.
- CLI-driven eval gains isolation by default.
- Existing memory candidates without subject tags still follow merge-key and confidence behavior.

## 19. Memory Relation and Init Stability Implementation Backfill - 2026-05-07

Implementation mapping:
- `src/agentend/core/memory_relation.py`
  - Added `MemoryRelationClassifier`.
  - Added `MemoryRelationDecision`.
  - Added metadata shortlist by scope, merge key, `subject:` tags, `type:` tags, and ordinary tags.
  - Added structured LLM JSON decision support for `reinforces`, `updates`, `conflicts`, and `unrelated`.
  - Added schema-compatible deterministic fallback for offline operation.
- `src/agentend/core/memory_consolidator.py`
  - Added `auto_relations` parameter.
  - Added relation gates for `needs_review`, `conflict_candidate`, `reinforced`, and `superseded`.
  - Reused explicit `supersedes:` and `conflicts:` tags as higher-priority overrides.
  - Added relation links without deleting old memory evidence.
- `src/agentend/cli.py`
  - Added `memory consolidate --auto-relations/--no-auto-relations`.
  - Added `eval run --shared-home/--isolated-home`.
  - Added base-home report indexing for isolated eval runs.
- `src/agentend/db/session.py`
  - Added SQLite `timeout=30`, `PRAGMA busy_timeout=30000`, and `PRAGMA journal_mode=WAL`.
  - Added home-local init file lock and bounded retry for lock-sensitive init operations.
- `src/agentend/core/skills.py`
  - Changed skill and extension registration to SQLite upsert semantics.
  - Existing skill `enabled` state is preserved when builtin metadata is refreshed.

Decision flow:
```text
candidate
  -> explicit relation tags?
  -> metadata shortlist
  -> structured relation decision
  -> confidence/direct-evidence gate
  -> reinforce / supersede / conflict_candidate / needs_review / create
```

Eval home flow:
```text
agentend eval run <suite> --home <base>
  -> init base home
  -> init <base>/eval-homes/<suite>-<id>
  -> run suite in child home
  -> write EvalRun in child home
  -> index same EvalRun id and report payload in base home
```

Shared-home flow:
```text
agentend eval run <suite> --home <base> --shared-home
  -> run suite directly in base home
  -> preserve old stateful eval behavior
```

## 20. Review Remediation Design - 2026-05-07

AgentRun resume:
- Add an `AgentRunController.resume(...)` entrypoint.
- Load the existing AgentRun and its iterations.
- Rebuild `previous_observations` from persisted iteration observations and selected action names.
- Continue the loop on the same `agent_run_id` starting at `max(iteration_index) + 1`.
- Treat the resume `--max-iterations` value as additional iterations, not as a new run total.
- If the existing run is already completed, return its current final result without creating new iterations.

Replan eval:
- Keep the production controller generic.
- In the `agent-replan` eval fixture, temporarily make the first selected builtin skill fail while still satisfying builtin skill required-tool validation.
- Verify that the selector consumes the failed previous observation and selects a different viable tool/skill on the second iteration.

Memory relation update gate:
- Keep high-confidence `updates` as automatic supersede.
- Convert medium-confidence `updates` to `needs_review`.
- Link the candidate to the target memory with `needs_review_for` and `relation_decision`.
- Do not create a new active memory for that candidate until a later high-confidence decision supersedes the target.

## 21. Review Remediation Implementation Backfill - 2026-05-07

Code mapping:
- `src/agentend/core/agent_run.py`
  - Added `AgentRunController.resume(...)`.
  - Split execution into `_run_loop(...)` so new runs and resumed runs share the same action/observe/evaluate logic.
  - Added previous-observation reconstruction from persisted iterations.
- `src/agentend/cli.py`
  - `agent resume` now calls controller resume instead of starting a new run.
- `src/agentend/core/agent_selector.py`
  - Strengthened previous-iteration failure penalty so fallback actions can overtake the failed action.
- `src/agentend/core/eval_harness.py`
  - `agent-replan` now prepares a failing builtin-skill fixture and verifies a different second action.
- `src/agentend/core/memory_consolidator.py`
  - Medium-confidence `updates` relation decisions now become `needs_review`.

Behavioral notes:
- Resume `--max-iterations` is treated as additional iterations for the existing run.
- The failed action remains visible in the iteration history and selector trace; it is not deleted or rerun.
- Medium-confidence update candidates preserve provenance for later review without writing active long-term memory.

## 22. Review Remediation Design - Evaluator, Eval Fixture, Resume Memory - 2026-05-07

Goal satisfaction evaluator:
- Keep the first implementation rule-based.
- Extract missing criteria from the goal and observation payload.
- For test-command goals, require observable test-command evidence in the output.
- If the action completed but the criteria are missing, mark evaluation incomplete and let the existing loop replan or hit `max_iterations_reached`.
- Treat goal echoes as non-evidence. The evidence matcher accepts concrete test-tool markers such as `pytest`, `python -m pytest`, `unittest`, `tox`, `py.test`, and `nox`.

Selector replan probe:
- Preserve the existing first-iteration tool-first/skill scoring.
- When a code/test goal has any previous failed or incomplete observation, add a `replan_probe` score to `shell.run`.
- The probe keeps the selector rule-based while making the second iteration gather direct command evidence instead of cycling through high-scoring non-evidence skills.
- The selected action and trace expose the probe score for eval/debug reports.

Eval fixture restoration:
- Capture the original `code.local_task` workflow content before writing the failing fixture.
- Run and inspect the eval inside a `try/finally`.
- Restore the original file in `finally` so shared homes are not left with the failure fixture.

Resume memory refresh:
- Keep existing candidate idempotency for repeated consolidation.
- When an AgentRun status changes from failed to completed, candidate extraction must not return only the stale failure candidate.
- Add a status-sensitive candidate check so the final successful procedure can be extracted and consolidated.

## 23. Review Remediation Implementation Backfill - Evaluator, Eval Fixture, Resume Memory - 2026-05-07

Code mapping:
- `src/agentend/core/agent_run.py`
  - `_evaluate_observation(...)` now receives the goal and emits goal-specific incomplete conditions.
  - Test-command goals require concrete command evidence and mark missing-evidence observations as `goal_incomplete`.
  - Previous observations reconstructed from persisted iterations preserve incomplete evaluation status for resume.
- `src/agentend/core/agent_selector.py`
  - Added `replan_probe` scoring for `shell.run` when a test-command goal has previous failed/incomplete observations.
  - Selector trace records the probe in `score_breakdown`.
- `src/agentend/core/eval_harness.py`
  - `agent-replan` uses `try/finally` to restore the mutated builtin workflow.
  - The eval report includes first and second action/observation payloads.
- `src/agentend/core/memory_consolidator.py`
  - AgentRun candidate extraction now checks for the candidate type matching the current final run status before returning existing rows.

Behavioral notes:
- `status=completed` and non-empty output remain necessary but are no longer sufficient for simple goal-specific cases.
- The current evaluator is intentionally rule-based and narrow; it is not a broad semantic judge.
- The command probe is activated only after a prior failed/incomplete observation, so initial selection remains compatible with the existing selector calibration.
