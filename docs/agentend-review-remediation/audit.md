# AgentEnd Review Remediation 审计文档

## 1. 审计范围

本审计记录 2026-05-06 新一轮代码审查中发现的实现偏差、复现证据、风险判断和后续验收要求。

审计对象：
- `src/agentend/core/workflow_runner.py`
- `src/agentend/core/context_runtime.py`
- `src/agentend/core/tasks.py`
- `src/agentend/mcp/adapter.py`
- `src/agentend/core/llm_router.py`
- `src/agentend/core/conversation.py`
- CLI、Telegram、Error Taxonomy、Browser fallback 相关链路

## 2. 已执行验证

本轮审查已执行：

```bash
.venv\Scripts\python.exe -m compileall -q src tests
.venv\Scripts\python.exe -m pytest tests -q --basetemp=D:\agentend\.tmp\codex-review-basetemp2 -p no:cacheprovider
git diff --check
agentend eval run runtime-hardening --home D:\agentend\.tmp\review-eval-home
agentend --help
agentend run simple_chat --input hello
agentend chat --message hello
```

结果：
- compileall 通过。
- 全量 pytest 通过：`115 passed, 1 skipped`。
- `git diff --check` 通过。
- `runtime-hardening` eval 通过。
- `agentend run simple_chat --input hello` 失败：顶层 `run` 命令不存在。
- `agentend chat --message hello` 返回 Echo 路径，未证明进入默认 workflow。
- eval 期间 Browser/Playwright fallback 出现未处理 future 异常噪音。

## 3. 已确认发现

### A1 Context Runtime 未进入 LLM 调用

证据：
- `workflow_runner.py` 记录 context ledger 并做预算检查后，仍调用 `routed_llm.complete_response(prompt)`。
- `context_runtime.py` 中 context pack 包含 policy 与 agent profile 等内容，但未转为实际 LLM request。

影响：
- agent.md、memory、retrieval、project profile 和 tool summary 不影响模型行为。
- Context Ledger 给出“已选择上下文”的审计信号，但真实 provider 没收到这些上下文。
- 文档中 Context Runtime 的核心承诺未闭合。

优先级：P1

修复验收：
- LLM fixture 收到的 request 中包含 context pack 内容。
- Ledger 与实际 request 内容一致。
- 超预算裁剪影响实际 request。

### A2 Task waiting-input 状态错配

复现：
- 创建包含 `human_input` 节点的 workflow。
- 通过 `tasks add` 和 `tasks run` 执行。

当前结果：
- task 显示 `completed`。
- 关联 run 显示 `waiting_input`。

影响：
- Scheduler、Task Inbox 或用户会把阻塞任务误判为完成。
- 自动化队列可能跳过仍需人工输入的任务。

优先级：P1

修复验收：
- waiting-input run 对应 task 为 `blocked`。
- resume 后根据 run 结果更新为 `completed` 或 `failed`。

### A3 MCP tool call 未脱敏

复现：
- 设置 `OPENAI_API_KEY=plain-secret-value`。
- 运行 MCP echo workflow，将输入设为 `plain-secret-value`。
- 查询 SQLite。

当前结果：

```text
MCP input: {"text": "plain-secret-value"}
MCP output: {"content": "plain-secret-value"}
Tool input: {"text": "[REDACTED]"}
Tool output: {"content": "[REDACTED]"}
```

影响：
- MCP 工具输入和输出可在 SQLite 中持久化明文 secret。
- 与 Runtime Hardening 审计要求冲突。
- run export 可能泄漏敏感信息。

优先级：P1

修复验收：
- MCP success/failure/event/export 全部使用统一 redaction。
- 测试注入的 secret 不出现在 DB 和 export 中。

### A4 OpenAI-compatible endpoint 重复拼接

证据：
- `llm_router.py` 将 `base_url.rstrip("/")` 后无条件拼接 `/chat/completions`。
- Runtime Hardening 设计要求当 `base_url` 已经以 `/chat/completions` 结尾时不得追加。

影响：
- 用户配置完整 endpoint 时，实际请求 URL 错误。
- 部分 gateway、proxy、兼容 provider 会无法调用。

优先级：P2

修复验收：
- `/v1`、`/v1/`、完整 `/chat/completions`、完整 endpoint 加尾斜杠均正确。

### A5 普通 chat 入口绕过 Agent Runtime

证据：
- `conversation.py` 当前存储并返回 `Echo: {text}`。
- CLI `agentend chat --message hello` 实测返回 Echo。

影响：
- 普通 chat 和 Telegram 普通文本不经过 WorkflowRunner。
- context、model routing、tool call、cost usage、evidence、audit 不生效。
- Lite 文档中统一 Conversation Service/Agent Runtime 的要求未满足。

优先级：P2

修复验收：
- chat 入口产生 run 记录。
- Telegram 普通文本产生同等 run 记录。
- Echo 只作为 fake provider 行为存在。

## 4. 关联改进项

### B1 顶层 `agentend run` 缺失

证据：
- `agentend --help` 无顶层 `run`。
- `agentend run simple_chat --input hello` 报 `No such command 'run'`。

建议：
- 增加顶层 alias，并与 `agentend workflows run` 共享实现。

### B2 Error Taxonomy 未区分策略阻断

证据：
- policy blocked side effect 当前可能被归为 `permission_error`。

建议：
- Action Policy、Replay、Scheduler 阻断统一使用 `external_side_effect_blocked`。

### B3 Browser fallback 日志噪音

证据：
- `runtime-hardening` eval 通过，但 Windows 环境输出 Playwright `Future exception was never retrieved` / `PermissionError` 栈。

建议：
- 收敛 Playwright 异步异常，并把原因放入结构化 warning 或 Doctor fix_hint。

## 5. 安全审计要求

### S1 Secret

- MCP、ToolRegistry、LLM、event log、run export 必须使用同一脱敏规则。
- 测试注入 secret 后，DB 和 export 不得出现明文。
- 错误信息不得包含 API key、token、password、Authorization header。

### S2 State

- Task 状态不得与 run 状态冲突。
- waiting-input 不得被标记为 completed。
- failed run 必须传播到 task。

### S3 Context

- Context Ledger 不能成为“虚假审计”。
- Ledger 中标记为 included 的 item 必须实际进入 LLM request。
- 被 drop 的 item 必须记录原因。

### S4 Provider

- URL 构造必须可测试。
- provider 配置不得在错误或导出中暴露 secret。

### S5 Entry Point

- CLI、Telegram、Conversation 不得维护彼此分叉的默认业务逻辑。
- 默认 chat flow 必须只有一个 resolver。

## 6. 发布前检查清单

发布前必须执行：

```bash
python -m compileall -q src tests
python -m pytest tests -q --basetemp=<writable-temp> -p no:cacheprovider
git diff --check
agentend eval run runtime-hardening --home <temp-home>
agentend chat --message "hello"
agentend run simple_chat --input "hello"
agentend workflows run simple_chat --input "hello"
```

还必须执行定向检查：
- MCP redaction DB 查询。
- human_input task 状态查询。
- OpenAI-compatible URL fixture 请求路径检查。
- Browser fallback stderr 检查。

## 7. 残留风险

- Context pack 转 messages 的格式会影响真实模型输出，需要保持 provider 兼容性。
- Conversation 默认 workflow 可能改变现有 Echo 测试，需要更新测试语义而不是保留旧实现。
- MCP 历史审计数据如果已经保存过 secret，本阶段不自动清理，需要单独维护任务。
- Browser/Playwright 在不同 OS 权限模型下仍可能出现环境差异，Doctor 需要给出可操作提示。

## 8. 本阶段已落地验证

2026-05-06 已完成第一批修复：Q1、Q2、Q6。

新增并确认先失败后通过的回归测试：

```bash
.venv\Scripts\python.exe -m pytest tests\test_mcp_cli.py::test_mcp_tool_call_audit_redacts_configured_secret_values tests\test_phase_f_inbox_tasks_tool_generator.py::test_task_run_marks_waiting_input_run_as_blocked tests\test_llm_agent_cli.py::test_openai_compatible_llm_accepts_full_chat_completions_endpoint -q --basetemp=D:\agentend\.tmp\review-remediation-green -p no:cacheprovider
```

结果：`3 passed`。

周边回归：

```bash
.venv\Scripts\python.exe -m pytest tests\test_mcp_cli.py tests\test_llm_agent_cli.py -q --basetemp=D:\agentend\.tmp\review-remediation-mcp-llm -p no:cacheprovider
.venv\Scripts\python.exe -m pytest tests\test_phase_f_inbox_tasks_tool_generator.py tests\test_phase_o_scheduler_inbox_reliability.py -q --basetemp=D:\agentend\.tmp\review-remediation-tasks -p no:cacheprovider
```

结果：
- MCP + LLM 周边：`8 passed`。
- Task + Scheduler 周边：`13 passed`。
- 全量回归复跑：`120 passed, 1 skipped`。
- `compileall` 通过，`git diff --check` 通过；`git diff --check` 仅输出 Windows 行尾提示。

状态：
- Q1 MCP redaction：已修复。MCP adapter 写入 `mcp_tool_calls` 前会对 input、output、error 调用统一 `redact_text`。
- Q2 Task waiting-input status：已修复。TaskManager 会读取关联 run.status，并将 `waiting_input` 映射为 `blocked`。
- Q6 OpenAI endpoint URL normalization：已修复。LLMRouter 使用 `chat_completions_url()`，完整 `/chat/completions` endpoint 不再重复追加路径。
- Q3 Context Runtime enters LLM request：已修复。WorkflowRunner 会构造 context pack、用同一份 pack 记录 ledger，并把 pack 转成 Chat Completions messages 传入 LLMRouter。
- Q4 Conversation default workflow：已修复。ConversationService 会复用会话并通过默认 `simple_chat` workflow 执行普通 chat/Telegram 文本。
- Q5 Top-level run alias：已修复。顶层 `agentend run` 与 `agentend workflows run` 共享同一执行函数。
- Q7 Error taxonomy policy block：已修复。Action Policy 阻断被分类为 `external_side_effect_blocked`，不再混入普通 `permission_error`。
- Q8 Browser fallback logging：已修复。Playwright transport 失败时会 drain 内部 error future，runtime-hardening eval 不再输出未处理 Future 异常。
- Q9 Eval and documentation closure：已完成。本目录任务板和审计文档已按本轮落地结果回填。

## 9. 最终自审查补充

2026-05-06 最终自审查中重新检查 Q3 Context Runtime 调用链，发现并修复一个追加问题：当 step context 设置较小 `max_items` 时，预算筛选可能裁掉当前 LLM step prompt，导致真实 provider 请求的最后一条 user message 为空。

修复结果：
- `prompt` 类型 context item 现在作为必选项进入 context pack，不会被 `max_items` 裁掉。
- Context Ledger 记录的 included prompt 与真实 LLM messages 保持一致。
- 新增回归测试 `tests/test_llm_agent_cli.py::test_workflow_llm_keeps_prompt_when_context_budget_is_tight`，通过真实 OpenAI-compatible HTTP fixture 检查 provider 请求内容。

验证命令：

```bash
.venv\Scripts\python.exe -m pytest tests/test_llm_agent_cli.py::test_workflow_llm_keeps_prompt_when_context_budget_is_tight -q --basetemp .tmp\self-review-tight-context -p no:cacheprovider
.venv\Scripts\python.exe -m pytest tests/test_llm_agent_cli.py -q --basetemp .tmp\self-review-llm-agent -p no:cacheprovider
.venv\Scripts\python.exe -m pytest tests/test_phase_n_context_policy_budget.py tests/test_phase_h_context_reliability.py -q --basetemp .tmp\self-review-context-policy -p no:cacheprovider
.venv\Scripts\python.exe -m pytest tests -q --basetemp .tmp\self-review-full -p no:cacheprovider
.venv\Scripts\python.exe -m compileall -q src tests
git diff --check
.venv\Scripts\agentend.exe init --home .tmp\self-review-eval-home
.venv\Scripts\agentend.exe eval run runtime-hardening --home .tmp\self-review-eval-home
.venv\Scripts\agentend.exe run simple_chat --home .tmp\self-review-eval-home --input "hello self review"
.venv\Scripts\agentend.exe chat --home .tmp\self-review-eval-home --message "hello self review"
```

结果：
- 定向新增回归：`1 passed`。
- LLM/CLI 周边：`7 passed`。
- Context policy/reliability 周边：`9 passed`。
- 全量回归：`121 passed, 1 skipped`。
- compileall 通过。
- `git diff --check` 通过，仅输出 Windows 换行提示。
- `runtime-hardening` eval：`passed`。
- 顶层 `agentend run` 和普通 `agentend chat` 均返回 workflow run id 与 `Fake LLM` 输出。
