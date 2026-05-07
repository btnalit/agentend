# AgentEnd Agentic Orchestration 审计文档

## 1. 审计范围

本审计记录 2026-05-07 对 AgentEnd 下一阶段 Agentic Orchestration 的设计前置检查。目标是确认当前实现与“目标结果导向、工具优先、跨会话记忆、长任务循环”之间的差距，并把差距转成后续验收要求。

已参考项目文档：

- `docs/agentend-lite/*`
- `docs/agentend-action-layer/*`
- `docs/agentend-runtime-hardening/*`
- `docs/agentend-review-remediation/*`

已抽查实现链路：

- `src/agentend/core/conversation.py`
- `src/agentend/core/goal_analyzer.py`
- `src/agentend/core/workflow_runner.py`
- `src/agentend/core/workflow_schema.py`
- `src/agentend/core/memory_store.py`
- `src/agentend/core/tasks.py`
- `src/agentend/core/episodes.py`
- `src/agentend/db/models.py`
- `src/agentend/cli.py`

## 2. 当前已具备的底座

### B1 WorkflowRunner 已经是稳定执行底座

证据：

- `workflow_runner.py` 已负责 context pack、model routing、tool call、checkpoint、cost usage 和 run 状态。
- `workflow_schema.py` 已要求 workflow exactly one final node。
- `workflow_runner.py` 已支持 resume by answer 或 checkpoint。

判断：

不应该把 `WorkflowRunner` 改造成复杂 agent loop。下一阶段应新增 AgentRunController，把 WorkflowRunner 当作 action executor 使用。

### B2 Goal Analyzer 已能召回候选能力

证据：

- `goal_analyzer.py:17` 定义 `analyze_goal`。
- `goal_analyzer.py:27-66` 生成 `candidate_skills`、`candidate_tools`、`candidate_workflows`。
- 规则已经能对 research、code、file 等意图补候选 skill/tool。

判断：

Goal Analyzer 可以复用，但需要 selector 把候选转成实际 action。

### B3 Memory Store 已有基础字段

证据：

- `models.py:315-328` 的 `MemoryItem` 已包含 scope、content、source、confidence、ttl、tags、created_by_run_id、evidence_artifact_id、last_used_at。
- `models.py:333-340` 的 `MemoryRetrieval` 已包含 memory_id、run_id、query。
- `memory_store.py:63-68` 支持检索后记录 retrieval。
- `memory_store.py:89-113` 已有 confidence、ttl、trusted source 的 context drop reason。

判断：

底层 memory 表可以继续用，但缺少自动 consolidation、去重、supersede 和 utility/effectiveness 反馈。

### B4 Task/Scheduler/InBox 已有单机持久化入口

证据：

- `tasks.py:112` 有 `run_task`。
- `tasks.py:196-238` 有 `run_schedule_now` 和 `run_due_schedules`。
- `tasks.py:270-322` 有 file inbox scan、hash 去重、batch_id 和 retry_after。
- `tasks.py:496-515` 已有 schedule 连续失败 auto-pause。

判断：

长期服务不需要上外部队列，首版 `agentend serve` 可以复用 TaskManager。

### B5 Episode to Skill 已形成经验资产雏形

证据：

- `episodes.py:15` 支持从 run summarize episode。
- `episodes.py:101` 支持 completed episode promote to skill draft。
- `episodes.py:154-164` 会生成 `evals/smoke.json`。

判断：

Episode 已能形成 skill draft，但缺少“成功经验进入记忆”和“skill 成效进入选择排序”的闭环。

## 3. 已确认差距

### A1 Goal Analyzer 不驱动默认执行

证据：

- `conversation.py:60` 调用 `analyze_goal(self.home, session, text)`。
- `conversation.py:63` 随后固定读取 `simple_chat`。
- `conversation.py:64-70` 固定通过 `WorkflowRunner.run(workflow, text, ...)` 执行。
- `conversation.py:75-77` 只是把 `goal_analysis` 写回 run result。

影响：

- 用户提出代码、文件、搜索或长任务目标时，系统不会默认根据候选 skill/tool 选择行动。
- “工具优先执行”在 chat 入口未闭合。

后续验收：

- `agentend agent run "读取项目测试命令"` 必须实际选择 code/file 相关 skill/tool。
- Agent iteration 必须记录候选能力、选择原因和未选择原因。

### A2 Replanner 当前是失败记录，不是循环控制器

证据：

- `workflow_runner.py:546-564` 在失败时调用 `replan_failure` 并写入 `replan_suggestion`。
- 该 suggestion 随后进入 failed result，不会自动驱动下一轮 action。

影响：

- 工具失败后无法形成 observe -> evaluate -> replan -> act 的 agent loop。

后续验收：

- 构造第一个 action 失败的 eval，第二轮必须使用 fallback action 或进入 ask_user。
- failed suggestion 必须关联到 agent iteration，并被 selector 消费。

### A3 WorkflowRunner 是一次性 DAG 执行，不适合直接承载长任务循环

证据：

- `workflow_runner.py:109` 和 `workflow_runner.py:375` 都按 `_ordered_nodes(workflow)` 顺序遍历节点。
- `workflow_runner.py:598-605` 对 workflow 做拓扑排序并检测 cycle。
- `workflow_schema.py:7` 的节点类型不包含 loop、retry、evaluate。

影响：

- 直接把长任务循环塞进 workflow schema 会扩大改动面。
- 更合适的边界是 AgentRunController 外层循环，多次调用 WorkflowRunner。

后续验收：

- Workflow schema 不需要新增 loop 节点即可完成 agent run 多轮执行。
- 每轮 action 作为 agent iteration，而非 workflow DAG 中的循环节点。

### A4 Memory 仍缺自动沉淀和质量治理

证据：

- `memory_store.py:23-44` 支持写 memory item，但调用方需要显式写入。
- `memory_store.py:63-68` 支持检索，但当前检索只按 query/scope 返回 memory。
- `MemoryRetrieval` 模型已有 `run_id`，但 `record_memory_retrievals` 当前只传入 query，没有把当前 run 作为必填上下文。

影响：

- 跨会话记忆依赖手工写入，不能自动从成功/失败任务中学习。
- 记忆无法系统性去重、合并、替换和评价有效性。

后续验收：

- 完成 run 后自动生成 memory candidates。
- 相同 merge_key 的候选更新旧 memory，而不是新增重复项。
- 下一次 agent run 能检索并使用上一次 consolidated memory。

### A5 Task/Scheduler 仍是 tick/run-now 驱动，不是长期 worker

证据：

- `cli.py:1894` 暴露 `schedule run-now`。
- `cli.py:1912` 暴露 `schedule tick`。
- `cli.py:2098-2104` 有 `telegram serve`，但没有通用 `agentend serve`。
- `tasks.py:238-268` 的 `run_due_schedules` 是被调用时执行，不是常驻服务。

影响：

- 本地长任务、周期任务和 inbox 仍依赖外部反复调用 CLI。
- 没有统一 heartbeat、claim、progress artifact 和 worker resume。

后续验收：

- `agentend serve --once` 可处理一个 pending task 或 due schedule。
- `agentend serve --poll-interval 10` 可持续轮询并写 heartbeat。
- worker 重启后能识别 running/blocked/pending 状态。

### A6 Skill 资产缺效果反馈

证据：

- Episode 可以 promote 成 skill draft，但 selector 当前没有读取 skill 历史成功率。
- Goal Analyzer 当前候选更多来自关键词和 Capability Map 文本召回。

影响：

- 失败率高或最近不可用的 skill 可能继续被推荐。
- 成功流程不能自然变成更高优先级能力。

后续验收：

- 每次 skill/tool/workflow action 写 effectiveness event。
- selector 使用 success/failure/blocked/last_success 调整排序。
- `skills effectiveness show <skill_id>` 能展示实际运行质量。

## 4. 下一阶段审计重点

实现过程中每个任务必须验证以下问题：

- Agent run 是否真的产生 iteration，而不是只包一层 workflow run。
- Goal Analyzer 结果是否进入 selector，并影响 action。
- Selector 选择 `llm_reason` 时是否记录了不用工具的原因。
- Replanner suggestion 是否进入下一轮，而不是只写 failed result。
- Memory candidate 是否短、准、带来源、可合并。
- Memory consolidation 是否避免重复长期记忆。
- Long Task Worker 是否有 heartbeat、progress artifact、resume cursor。
- `serve --once` 是否可离线测试，不依赖真实 Telegram 或外部 API。
- 新增 eval 失败时是否能定位 agent_run_id、iteration_id、action 或 memory_candidate_id。

## 5. 发布前验证清单

```bash
.venv\Scripts\python.exe -m compileall -q src tests
.venv\Scripts\python.exe -m pytest tests -q --basetemp=.tmp\agent-orchestration-full -p no:cacheprovider
.venv\Scripts\agentend.exe init --home .tmp\agent-orchestration-home
.venv\Scripts\agentend.exe agent run --home .tmp\agent-orchestration-home "列出项目测试命令并说明依据"
.venv\Scripts\agentend.exe eval run orchestration-smoke --home .tmp\agent-orchestration-home
.venv\Scripts\agentend.exe eval run memory-consolidation --home .tmp\agent-orchestration-home
.venv\Scripts\agentend.exe eval run long-task-worker --home .tmp\agent-orchestration-home
.venv\Scripts\agentend.exe serve --home .tmp\agent-orchestration-home --once
git diff --check
```

## 6. 当前状态

状态：规划文档已建立，尚未开始实现。

建议下一步从 O1-O3 开始，先形成最小可运行 AgentRunController，再接 Memory Consolidator 和 Long Task Worker。这样能最快验证“目标结果导向”和“工具优先执行”两条主线是否闭合。

## 7. Post-Implementation Audit - 2026-05-07

Status: O1-O15 first slice is implemented and verified.

Closed gaps:
- A1 Goal Analyzer now feeds `AgentRunController` selector through `agentend agent run` and `chat --agent`; default chat remains workflow mode.
- A2 Replanning now has an outer bounded AgentRun loop with observation/evaluation records and explicit stop reasons.
- A3 WorkflowRunner remains unchanged as a DAG executor; AgentRunController owns iteration state.
- A4 Memory now has candidate extraction, merge-key consolidation, MemoryLink provenance, and next-run retrieval in the plan.
- A5 Worker entry exists as `agentend serve`; tasks get claim, heartbeat, progress artifact, resume cursor, and agent_run_id.
- A6 Skill/tool/workflow actions now record effectiveness events and aggregates; selector consumes those aggregates.

Verification evidence:
- New focused orchestration tests: 10 passed.
- Full test suite: 132 passed in 439.17s.
- Eval `orchestration-smoke`: passed.
- Eval `memory-consolidation`: passed.
- Eval `long-task-worker`: passed.
- Manual AgentRun command completed and selected a real skill/tool-backed path.
- `serve --once` exited cleanly with no pending work.
- `git diff --check` exit 0; only CRLF conversion warnings were emitted.

Remaining audit notes:
- `agent-replan` currently verifies bounded loop behavior and explicit stop reason; a stronger future case should force a first-action failure and prove a different second action.
- Memory supersede can now update by merge key, but semantic contradiction detection is not implemented.
- Telegram agent-mode config is not defaulted on; this slice keeps Telegram/workflow behavior unchanged unless a later config decision enables it.

## 8. Pre-Implementation Audit for Selector and Memory Quality - 2026-05-07

Current selector gap:
- `select_next_action(...)` returns only one `SelectedAction`.
- The caller cannot inspect top rejected candidates or named score components.
- Effectiveness is consumed as aggregate counts, so old successes can outweigh fresh failures.
- AgentRun iteration plan stores memory ids and previous observations, but not the selector ranking trace.

Required fix:
- Add a trace-returning selector API while keeping the current API.
- Persist the trace in iteration plan JSON.
- Use recent effectiveness events for calibration before aggregate fallback.

Current memory gap:
- Consolidation can create and merge by `merge_key`.
- It does not distinguish replace, conflict, or reinforce as separate decisions.
- Old memory is not marked `superseded` by explicit replacement evidence.

Required fix:
- Interpret explicit relation tags on memory candidates.
- Mark old memory `superseded` for replacement.
- Keep weak conflicts as candidates instead of polluting long-term active memory.
- Record `MemoryLink` provenance for supersede/conflict/reinforce.

Audit checks for this slice:
- Selector trace appears under AgentIteration plan JSON.
- Superseded memory is absent from default `memory search`.
- Conflict candidate keeps old memory active.
- No changes to default chat or worker concurrency.

## 9. Post-Implementation Audit for Selector and Memory Quality - 2026-05-07

Status: implemented and verified.

Closed audit items:
- Selector trace is persisted in AgentIteration plan JSON.
- Trace contains goal type, selected action, ranked candidates, score breakdown, rejected reasons, and action input preview.
- Recent effectiveness events can lower a previously successful capability below a viable alternative.
- Explicit supersede candidate tags mark old memory `superseded` and remove it from default search.
- Conflict candidate tags no longer overwrite active memory.
- Same merge-key compatible evidence reinforces existing memory instead of creating duplicates.

Verification:
- Focused tests: 5 passed.
- Agent orchestration related tests: 14 passed.
- Full suite: 136 passed in 448.15s.
- Compileall: passed.
- Eval `tool-first`: passed.
- Eval `skill-effectiveness`: passed.
- Eval `memory-consolidation`: passed.

Residual risks:
- Selector calibration still needs more domain eval cases before using it for broad autonomous code edits.
- Supersede/conflict currently depends on explicit relation tags; automatic semantic conflict extraction is future work.
- Running multiple eval processes against the same SQLite home can race during builtin skill registration; sequential evals are the supported verification path for now.

## 10. Pre-Implementation Audit for Memory Relation and Init Stability - 2026-05-07

Current memory relation gap:
- Existing supersede/conflict support depends on explicit `supersedes:<id>` or `conflicts:<id>` tags.
- The consolidator does not yet compare candidate metadata against active memories to infer `reinforces`, `updates`, `conflicts`, or `unrelated`.
- Low-confidence candidates are skipped for project/user memory, but there is no `needs_review` status that preserves a relation decision for later inspection.
- Weak conflicts can be represented only when an explicit tag exists; automatic relation evidence is not captured.

Required fix:
- Add a metadata-only shortlist and structured relation decision.
- Keep LLM/classifier output as schema-driven relation metadata, not as direct write authority.
- Add safe status transitions for `needs_review`, `conflict_candidate`, `reinforced`, and `superseded`.
- Preserve provenance through `MemoryLink` on every non-trivial relation.

Current initialization gap:
- `create_sqlite_engine` currently creates a plain SQLite engine without explicit busy timeout.
- `init_database` performs create-all and incremental columns without a home-level init lock.
- Builtin skill registration uses read-then-insert ORM upsert and can race if two processes initialize the same home at once.

Required fix:
- Add SQLite busy timeout and WAL pragmas.
- Guard init with a home-local file lock.
- Make builtin skill registration idempotent under duplicate startup.
- Add bounded retry around lock-sensitive init paths.

Current eval gap:
- CLI `eval run --home <home>` currently writes directly into that home.
- Concurrent suites against one home can race and can pollute shared state.

Required fix:
- CLI eval should use isolated child homes by default.
- `--shared-home` should remain available for tests that intentionally need shared state.
- Reports created from isolated homes must be discoverable from the base home.

Audit checks for this slice:
- Auto relation update supersedes without explicit tag.
- Low-confidence conflict is inspectable but does not change active memory.
- `--no-auto-relations` preserves explicit-only behavior.
- Repeated initialization produces no duplicate skill rows or integrity errors.
- Eval report from a suite-isolated run is readable from the base home.

## 11. Post-Implementation Audit for Memory Relation and Init Stability - 2026-05-07

Status: implemented and verified.

Closed audit items:
- Auto relation classification now works without explicit `supersedes:` or `conflicts:` tags.
- Relation classification is metadata-scoped before content comparison, avoiding unbounded full-memory scans.
- Low-confidence relation decisions become `needs_review` instead of writing active long-term memory.
- Weak conflicts become `conflict_candidate` unless direct high-confidence evidence allows supersede.
- Explicit relation tags still override auto relation classification.
- SQLite engine setup now uses busy timeout and WAL.
- `init_database()` now uses a home-local init lock.
- Builtin skill registration is now idempotent through SQLite upsert.
- CLI eval is isolated by default and can preserve old shared-state behavior with `--shared-home`.

Regression found and fixed during verification:
- Full-suite run initially failed 2 tests because those tests disabled tools in the base home and expected eval to see that mutated state.
- The correct fix was to update those tests to pass `--shared-home`, because they are explicitly testing shared-state failure/export behavior.
- After that change, the full suite passed.

Verification:
- Red test run: 6 failed, 2 passed before implementation.
- Focused tests: 9 passed.
- Related orchestration tests: 22 passed.
- Full suite: 144 passed.
- Compileall: passed.
- Eval `memory-consolidation`: passed with `shared_home=false`.
- Eval `tool-first`: passed with `shared_home=true`.
- `git diff --check`: exit 0; CRLF warnings only.

Residual risks:
- The classifier is intentionally conservative and metadata-scoped. It is not an embedding search and not a broad natural-language contradiction engine.
- Structured LLM JSON decisions are supported behind the same contract; the offline conservative fallback is also verified and remains the default when no usable LLM route is configured.
- File locking protects init, not every normal worker write. Task claim and heartbeat retry behavior can be further hardened if worker concurrency is raised later.
