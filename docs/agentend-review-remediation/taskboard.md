# AgentEnd Review Remediation 任务文档

## 1. 任务目标

本任务板用于把 2026-05-06 审查发现转化为可执行修复项。每个任务必须有代码修复、回归测试和可运行验收命令。

## 2. 状态说明

- `Planned`：尚未实现。
- `In Progress`：正在实现。
- `Blocked`：需要用户输入、真实凭据或外部环境。
- `Done`：代码、测试、文档和审计均完成。

## 3. 推荐顺序

```text
Phase Q0: 安全与状态正确性
  Q1 MCP redaction
  Q2 Task waiting-input status

Phase Q1: 调用链闭合
  Q3 Context Runtime enters LLM request
  Q4 Conversation default workflow
  Q5 Top-level run alias

Phase Q2: Provider 与错误分类
  Q6 OpenAI endpoint URL normalization
  Q7 Error taxonomy policy block

Phase Q3: 环境稳定性与回归
  Q8 Browser fallback logging
  Q9 Eval and documentation closure
```

## 4. 任务列表

### Q1 MCP redaction `AFK`

状态：`Done`

目标：MCP 工具调用审计与标准 ToolRegistry 使用同一脱敏规则。

范围：
- 提取或复用统一 redaction helper。
- `mcp_tool_calls.input`、`mcp_tool_calls.output`、error、event log、export 写入前脱敏。
- 成功和失败路径都覆盖。

验收：

```bash
set OPENAI_API_KEY=plain-secret-value
agentend mcp add demo --stdio "mock:echo"
agentend workflows run mcp_secret_demo --input "plain-secret-value"
```

检查：
- SQLite `mcp_tool_calls` 中不出现 `plain-secret-value`。
- `runs export` 中不出现 `plain-secret-value`。

测试映射：
- `tests/test_mcp_redaction.py`
- 当前实现测试：`tests/test_mcp_cli.py::test_mcp_tool_call_audit_redacts_configured_secret_values`

### Q2 Task waiting-input status `AFK`

状态：`Done`

目标：Task 状态必须反映关联 run 的真实状态。

范围：
- 增加 `map_run_status_to_task_status()`。
- `tasks run` 根据 run.status 写入 `blocked/completed/failed`。
- task show/list 展示 run status。
- 恢复 waiting input 后同步 task 状态。

验收：

```bash
agentend tasks add ask_demo --input "hello"
agentend tasks run <task_id>
agentend tasks list
agentend runs list
```

期望：
- human_input workflow 后，task 为 `blocked`，run 为 `waiting_input`。
- resume 后，task 才变为 `completed`。

测试映射：
- `tests/test_task_status_waiting_input.py`
- 当前实现测试：`tests/test_phase_f_inbox_tasks_tool_generator.py::test_task_run_marks_waiting_input_run_as_blocked`

### Q3 Context Runtime enters LLM request `AFK`

状态：`Done`

目标：Context Runtime 生成的上下文包必须进入实际 LLM request。

范围：
- `ContextRuntime.build_pack()` 返回可执行 context pack。
- WorkflowRunner 将 context pack 转为 messages。
- LLMRouter 支持 `complete_messages()` 或等效接口。
- Ledger 记录实际进入 request 的 context item。
- Budget 基于实际 messages 检查。

验收：

```bash
agentend workflows run simple_chat --input "context check"
agentend runs export <run_id>
```

期望：
- fake/openai fixture 接收到的 request 包含 agent.md 或 project profile 内容。
- ledger 与 request 使用内容一致。

测试映射：
- `tests/test_context_runtime_llm_input.py`
- 当前实现测试：`tests/test_llm_agent_cli.py::test_workflow_llm_request_includes_context_pack_items`
- 最终自审查追加测试：`tests/test_llm_agent_cli.py::test_workflow_llm_keeps_prompt_when_context_budget_is_tight`

### Q4 Conversation default workflow `AFK`

状态：`Done`

目标：CLI chat 与 Telegram 普通文本进入默认 workflow，不再由 ConversationService 直接 Echo。

范围：
- 默认 workflow resolver。
- `agentend chat --message` 调用 WorkflowRunner。
- Telegram 普通文本复用 ConversationService。
- Conversation messages 关联 run_id。
- 更新既有 Echo 测试。

验收：

```bash
agentend chat --message "hello"
agentend runs list
```

期望：
- 输出来自 workflow final。
- 产生 run、context ledger、cost usage 或等效审计记录。

测试映射：
- `tests/test_conversation_default_workflow.py`
- 当前实现测试：`tests/test_llm_agent_cli.py::test_chat_run_records_agent_profile_hash_and_llm_config`
- 当前实现测试：`tests/test_telegram_entry.py::test_telegram_router_handles_start_plain_message_and_run_command`

### Q5 Top-level run alias `AFK`

状态：`Done`

目标：补齐文档承诺的 `agentend run <workflow_id>`。

范围：
- 新增顶层 Typer command。
- 与 `agentend workflows run` 共享实现。
- 帮助文案与 README/需求文档保持一致。

验收：

```bash
agentend run simple_chat --input "hello"
agentend workflows run simple_chat --input "hello"
```

期望：
- 两条命令行为一致。

测试映射：
- `tests/test_cli_run_alias.py`
- 当前实现测试：`tests/test_workflows_cli.py::test_top_level_run_alias_runs_workflow`

### Q6 OpenAI endpoint URL normalization `AFK`

状态：`Done`

目标：OpenAI-compatible provider 支持 base URL 与完整 endpoint 两种配置。

范围：
- 新增 URL helper。
- 替换当前直接字符串拼接。
- 覆盖 trailing slash 和完整 endpoint。

验收：

```bash
agentend llm set --provider openai --base-url "https://api.example.com/v1/chat/completions"
agentend llm test
```

测试映射：
- `tests/test_llm_openai_endpoint_url.py`
- 当前实现测试：`tests/test_llm_agent_cli.py::test_openai_compatible_llm_accepts_full_chat_completions_endpoint`

### Q7 Error taxonomy policy block `AFK`

状态：`Done`

目标：策略阻断副作用时使用 `external_side_effect_blocked`，不再混为 `permission_error`。

范围：
- 新增专用异常或 error code 映射。
- ActionPolicy 阻断、Replay 阻断、Scheduler 阻断统一使用该 code。
- 普通 OS/provider permission error 保持原分类。

验收：

```bash
agentend schedule run-now <schedule_id_with_network_write>
agentend runs export <run_id>
```

期望：
- error code 为 `external_side_effect_blocked`。

测试映射：
- `tests/test_error_taxonomy_policy_block.py`
- 当前实现测试：`tests/test_phase_f_inbox_tasks_tool_generator.py::test_scheduler_run_mode_blocks_external_write_tools`

### Q8 Browser fallback logging `AFK`

状态：`Done`

目标：Playwright 不可用或权限不足时，fallback 可用且 stderr 无未处理 future 异常。

范围：
- 收敛 Playwright transport/future 异常。
- tool result 增加 warning。
- Doctor fix_hint 明确。

验收：

```bash
agentend eval run runtime-hardening --home <temp-home>
```

期望：
- eval 通过。
- stderr 不出现 `Future exception was never retrieved`。

测试映射：
- `tests/test_browser_fallback_logging.py`
- 当前实现测试：`tests/test_phase_k_eval_suite_expansion.py::test_runtime_hardening_eval_covers_repaired_runtime_paths`

### Q9 Eval and documentation closure `AFK`

状态：`Done`

目标：完成本阶段回归与文档回填。

范围：
- 新增 review-remediation eval 或扩展 runtime-hardening eval。
- 更新本目录 audit。
- 确认 README/现有阶段文档不再与实现冲突。

验收：

```bash
python -m compileall -q src tests
python -m pytest tests -q --basetemp=<writable-temp> -p no:cacheprovider
git diff --check
agentend eval run runtime-hardening --home <temp-home>
```

期望：
- 全部通过。
- 无 secret 泄漏。
- 无未处理异步异常噪音。

## 5. 完成定义

任务标记为 `Done` 前必须满足：

- 有直接对应的测试或 CLI 验收。
- 修复没有扩大文件系统、网络或执行权限边界。
- 相关 docs 与 README 不再声明错误入口或错误行为。
- 审计文档记录修复证据、残留风险和验证命令。
