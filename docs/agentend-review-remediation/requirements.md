# AgentEnd Review Remediation 需求文档

## 1. 背景

本阶段来源于 2026-05-06 新一轮代码审查。审查对象为 `docs/` 中已经声明完成的 AgentEnd Lite、AgentEnd Action Layer 与 AgentEnd Runtime Hardening 能力，以及 `src/agentend` 中对应实现。

全量回归测试当前可以通过，但审查发现仍有若干“文档已承诺、代码链路未闭合”的问题。它们不会全部表现为单元测试失败，却会在真实 LLM 调用、任务调度、MCP 审计、CLI/Telegram 默认入口和 OpenAI-compatible provider 配置中造成行为异常。

本阶段命名为 **AgentEnd Review Remediation**。

## 2. 目标

- 让 Context Runtime 的输出真正进入 LLM 调用链路，而不只是生成 ledger。
- 让 Task Inbox/Scheduler 正确反映 Workflow Run 的等待、阻塞、失败和完成状态。
- 让 MCP 工具调用的输入、输出、错误和导出遵守统一脱敏规则。
- 让 OpenAI-compatible provider 正确处理 `base_url` 与完整 `/chat/completions` endpoint。
- 让普通 chat 与 Telegram 普通文本入口复用 Agent Runtime、WorkflowRunner、DB、工具、审计和模型路由链路。
- 补齐顶层 CLI 入口、错误分类和浏览器 fallback 日志质量等审查中识别的对齐问题。

## 3. 范围

### 3.1 必须包含

- `workflow_runner.py` 中 LLM step 的 context pack 构造、预算检查、ledger 记录和最终 prompt/messages 输入。
- `context_runtime.py` 中 context pack 到 LLM request 的可执行格式转换。
- `tasks.py` 中 task 状态与 run 状态的映射规则。
- `mcp/adapter.py` 中 MCP tool call 的输入、输出、错误、event log、export 脱敏。
- `llm_router.py` 中 OpenAI-compatible endpoint URL 构造规则。
- `conversation.py`、Telegram 普通文本 handler 与 CLI chat 命令的默认工作流入口。
- 顶层 `agentend run <workflow_id>` 与现有 `agentend workflows run` 的兼容入口。
- Error Taxonomy 中 policy blocked side effect 与普通 permission error 的区分。
- Browser/Playwright fallback 异常日志的可控输出。
- 针对上述能力的自动化回归测试与审计记录更新。

### 3.2 不包含

- 新增多 Agent 架构。
- 新增前端 Console。
- 更换数据库或引入外部队列。
- 引入真实远程沙箱。
- 重新设计 Skill Market 供应链治理。
- 大规模重构 WorkflowRunner 或把整个运行时改成 async。

## 4. 需求清单

### RR-1 Context Runtime 必须影响 LLM 行为

当前问题：
- `workflow_runner.py` 会记录 context ledger 并检查预算，但最终仍将原始 prompt 传给 `routed_llm.complete_response()`。
- `agent.md`、项目 profile、memory、retrieval、工具摘要等只影响审计，不影响模型输入。

要求：
- LLM step 必须先通过 Context Runtime 构造 context pack。
- context pack 必须包含按策略选择的 system、agent.md profile、project profile、goal、workflow state、recent messages、memory、retrieval、tool summary 和当前用户输入。
- 预算检查必须基于实际发送给模型的内容。
- Context Ledger 必须记录实际使用的 context item、token estimate、被截断或丢弃的内容和原因。
- LLM provider 接口必须接收结构化 messages 或等效的上下文字符串，不能只接收原始 prompt。
- fake provider 必须保留离线可测行为，但返回内容需要能证明 context 已进入输入。

验收：
- 新增测试证明 agent.md 或 project profile 中的指令会出现在 fake/openai fixture 接收到的 LLM request 中。
- 新增测试证明超预算时被裁剪的是实际发送内容，而不是只裁剪 ledger。
- `context_ledger` 与 LLM request 中的 provider/model/stage 保持一致。

### RR-2 Task 状态必须跟随 Run 状态

当前问题：
- `tasks.py` 将 `WorkflowRunner.run()` 的结果无条件映射为 `task.status = completed`。
- 当 workflow 进入 `waiting_input` 时，task list 已显示 completed。

要求：
- Task 状态必须由 run 状态映射而来。
- `run.status = waiting_input` 时，task 状态必须为 `blocked` 或等效等待状态。
- `run.status = failed` 时，task 状态必须为 `failed`，并保留失败摘要。
- 只有 `run.status = completed` 时，task 才能标为 `completed`。
- Scheduler 不得把 `blocked` task 当作已完成任务继续推进。
- CLI list/show 必须显示 task 当前状态和关联 run 状态，避免用户误判。

验收：
- human_input workflow 经 `tasks run` 后，task 为 `blocked`，关联 run 为 `waiting_input`。
- 用户补充 input 并恢复 run 后，task 才能变为 `completed`。
- failed workflow 经 `tasks run` 后，task 为 `failed`。

### RR-3 MCP 审计必须统一脱敏

当前问题：
- `mcp/adapter.py` 直接将 `input_data` 和 `result.data` 写入 `mcp_tool_calls`。
- 标准 `tool_calls` 已脱敏，但 MCP 路径绕过了统一规则。

要求：
- MCP tool call 的 input、output、error、event log、run export 必须使用与 ToolRegistry 相同的 secret redaction。
- 脱敏规则必须覆盖环境变量中的 API key、token、secret、password、bearer token 和已知 secret 值。
- MCP 审计可以保留结构形状，但不得保留明文 secret。
- 失败路径与成功路径都必须脱敏。
- 现有历史数据不做自动迁移；如需要清理历史 DB，另开显式维护任务。

验收：
- 设置 `OPENAI_API_KEY=plain-secret-value` 后运行 MCP echo workflow，`mcp_tool_calls`、event log、export 中不得出现 `plain-secret-value`。
- 对比标准 `tool_calls` 与 `mcp_tool_calls`，两者脱敏行为一致。
- 失败 MCP 调用的 error detail 也不得泄漏 secret。

### RR-4 OpenAI-compatible endpoint 必须避免重复拼接

当前问题：
- `llm_router.py` 总是将 `/chat/completions` 拼到 `base_url` 后。
- 当用户配置完整 endpoint 时，会变成 `/chat/completions/chat/completions`。

要求：
- 如果 `base_url` 已经以 `/chat/completions` 结尾，则直接使用该 URL。
- 如果 `base_url` 以 `/v1` 或 provider 根路径结尾，则追加 `/chat/completions`。
- URL 规范化不得破坏 scheme、host、port、path 和 query-free path。
- 测试必须覆盖 trailing slash、完整 endpoint、`/v1` endpoint 和 provider root。

验收：
- `https://api.example.com/v1` -> `https://api.example.com/v1/chat/completions`。
- `https://api.example.com/v1/chat/completions` -> 原样使用。
- `https://api.example.com/v1/chat/completions/` -> 去尾斜杠后原样使用。

### RR-5 普通 chat 入口必须复用 Agent Runtime

当前问题：
- `Conversation Service` 当前直接返回 `Echo: {text}`。
- CLI chat 与 Telegram 普通文本绕过 WorkflowRunner、工具、审计、context、model routing 和 cost usage。

要求：
- 普通 chat 必须进入默认 conversation flow。
- 默认 conversation flow 可以是配置项，也可以是内置 `simple_chat` workflow，但必须经 WorkflowRunner 执行。
- Conversation Service 必须保留 conversation message 入库能力，同时关联 run_id。
- Telegram 普通文本必须复用同一 Conversation Service 和默认 workflow。
- Echo 行为只能保留在 fake provider 或测试 fixture 层，不能作为 conversation 主实现。

验收：
- `agentend chat --message hello` 产生 run 记录、LLM/tool/cost/context 审计记录，并返回 workflow final 输出。
- Telegram 普通文本产生同等 run 记录。
- 配置默认 workflow 后，chat 使用该 workflow；未配置时使用内置安全默认 workflow。

### RR-6 顶层 CLI 入口必须与文档对齐

当前问题：
- Lite 需求文档要求 `agentend run <workflow_id>`。
- 当前只有 `agentend workflows run`，顶层 `agentend run` 不存在。

要求：
- 增加顶层 `agentend run <workflow_id>` 作为 `agentend workflows run <workflow_id>` 的兼容别名；或显式修订所有文档与 README。
- 首选补 alias，避免破坏既有文档和用户预期。

验收：
- `agentend run simple_chat --input hello` 与 `agentend workflows run simple_chat --input hello` 行为一致。

### RR-7 Error Taxonomy 必须区分策略阻断

当前问题：
- policy blocked side effect 与普通 permission error 被混成 `permission_error`。

要求：
- 新增或启用 `external_side_effect_blocked`，并覆盖 replay/scheduler/action policy 阻断。
- 文件权限、系统权限、provider 403 等仍保留 `permission_error`。

验收：
- Scheduler 阻断 network_write 时错误分类为 `external_side_effect_blocked`。
- 文件系统 OS 权限错误仍分类为 `permission_error`。

### RR-8 Browser fallback 异常日志必须可控

当前问题：
- `runtime-hardening` eval 在 Playwright fallback 场景下可以通过，但 Windows 环境会输出 `Future exception was never retrieved` 异步栈。

要求：
- fallback 仍应返回结构化 tool result。
- Playwright transport 异常不得以未处理 future 形式污染 stderr。
- Doctor 或 tool result 中应包含可读的修复建议。

验收：
- 无 Playwright 权限时，eval 通过且 stderr 不出现未处理 future 异常栈。

## 5. 非功能需求

- 不引入真实网络依赖到默认测试。
- 不在 DB、日志、导出、错误信息中保存明文 secret。
- 不扩大文件系统写入边界。
- 不破坏现有 `agentend workflows run`、fake provider 和离线测试。
- 所有修复必须有自动化测试或明确的 CLI 验收命令。
- 修改完成后必须通过全量 `pytest`、`compileall`、`git diff --check` 和相关 eval。

## 6. 成功标准

- 以上 RR-1 到 RR-8 全部完成。
- 代码审查中 5 个主发现均有对应回归测试。
- `agentend eval run runtime-hardening` 通过且无未处理异步异常噪音。
- `agentend chat`、Telegram 普通文本、`agentend run`、`agentend workflows run` 的调用链与文档一致。
- 审计导出中不出现本轮测试注入的 secret 明文。
