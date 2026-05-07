# AgentEnd Review Remediation 设计文档

## 1. 设计目标

本设计用于修复审查发现的运行链路断点。原则是保持现有单机架构、SQLite 存储、WorkflowRunner 和 ToolRegistry 的主要边界不变，在关键入口补齐真实调用链、状态映射和审计一致性。

## 2. 总体链路

```text
CLI / Telegram / Conversation
    -> ConversationService
    -> Default Workflow Resolver
    -> WorkflowRunner
    -> Context Runtime
    -> Model Route Resolver
    -> LLMRouter / ToolRegistry / MCPAdapter
    -> Action Policy / Error Taxonomy / Redaction
    -> SQLite / Artifacts / Evidence / Export
```

核心变化：
- LLM step 的输入从 `prompt` 升级为 `context pack -> messages`。
- Task 状态从本地乐观完成改为 run 状态派生。
- MCPAdapter 复用统一 redaction。
- OpenAI-compatible URL 构造成为独立 helper。
- 普通 chat 入口改为默认 workflow 入口。

## 3. Context Runtime 到 LLM Request

### 3.1 数据结构

建议新增或复用以下结构：

```python
@dataclass
class LLMContextItem:
    role: str
    content: str
    source: str
    token_estimate: int

@dataclass
class LLMContextPack:
    items: list[LLMContextItem]
    ledger_id: str
    total_token_estimate: int
    dropped_items: list[dict]
```

`ContextRuntime.build_pack(...)` 返回 `LLMContextPack`，而不是只写 ledger。

### 3.2 Message 组装规则

推荐转换规则：

- `system` item 合并为首个 system message。
- `agent_profile`、`project_profile`、`goal` 作为 system 或 developer 风格上下文；如果 provider 只支持 Chat Completions 标准 role，则归并到 system。
- `recent_messages` 使用原始 user/assistant role。
- `workflow_state`、`memory`、`retrieval`、`tool_summary` 作为带标签的 system 上下文块。
- 当前 LLM node 的 prompt 作为最后一个 user message 或当前 step input。

伪代码：

```python
pack = context_runtime.build_pack(run, node, prompt)
messages = context_runtime.to_messages(pack, current_prompt=prompt)
budget.check_input(messages)
response = routed_llm.complete_messages(messages, stage="workflow_step")
```

### 3.3 预算与 Ledger

- 预算检查必须基于 `messages`。
- Ledger 记录 pack 的 item id、source、token estimate、是否进入实际 request。
- 如果上下文被裁剪，Ledger 记录 drop reason。
- Cost usage 记录与同一次 LLM request 绑定。

## 4. Task 状态设计

### 4.1 状态映射

| run.status | task.status | 说明 |
| --- | --- | --- |
| `completed` | `completed` | workflow 已完成 |
| `waiting_input` | `blocked` | 等待人工输入 |
| `failed` | `failed` | workflow 失败 |
| `running` | `running` | 执行中 |
| `pending` | `pending` | 尚未执行 |

如果未来新增 `cancelled`，task 应映射为 `failed` 或新增 `cancelled`，但必须在 task schema 中显式声明。

### 4.2 执行流程

```python
task.status = "running"
run_result = runner.run(...)
task.run_id = run_result.run_id
task.status = map_run_status_to_task_status(run_result.status)
task.result_summary = summarize_run(run_result)
```

`human_input` 不应抛异常来驱动 task 状态，而应通过 run 状态自然映射。

### 4.3 恢复流程

当用户提交等待输入并恢复 run：

- 找到关联 task。
- 重新读取 run.status。
- 如果 run completed，则 task completed。
- 如果 run 仍 waiting_input，则 task 保持 blocked。
- 如果 run failed，则 task failed。

## 5. MCP Redaction 设计

### 5.1 统一脱敏入口

建议把 ToolRegistry 当前使用的 redaction helper 提升为共享模块，例如：

```text
src/agentend/core/redaction.py
```

提供：

```python
redact_value(value: Any, secrets: SecretSet | None = None) -> Any
redact_text(text: str, secrets: SecretSet | None = None) -> str
collect_runtime_secrets(config, env) -> SecretSet
```

ToolRegistry、MCPAdapter、event log、run export 统一调用该模块。

### 5.2 MCP 写入策略

MCP 调用落库前：

```python
safe_input = redact_value(input_data)
safe_output = redact_value(result.data)
safe_error = redact_text(error_message)
```

保留字段结构，不保留 secret 明文。

### 5.3 失败路径

异常路径必须先脱敏再写入：

```python
except Exception as exc:
    safe_error = redact_text(str(exc))
    record_mcp_call(error=safe_error)
    raise WorkflowRunFailed(safe_error)
```

## 6. OpenAI-compatible URL 构造

### 6.1 Helper

新增纯函数：

```python
def chat_completions_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/chat/completions"):
        return normalized
    return f"{normalized}/chat/completions"
```

### 6.2 测试矩阵

| 输入 | 输出 |
| --- | --- |
| `https://api.example.com/v1` | `https://api.example.com/v1/chat/completions` |
| `https://api.example.com/v1/` | `https://api.example.com/v1/chat/completions` |
| `https://api.example.com/v1/chat/completions` | 原样 |
| `https://api.example.com/v1/chat/completions/` | 去尾斜杠后原样 |

## 7. Conversation 默认 Workflow 设计

### 7.1 默认 workflow resolver

ConversationService 增加默认 workflow 解析：

```python
resolve_default_workflow(home) -> str
```

优先级：

1. 用户配置的 default conversation workflow。
2. 内置 `simple_chat`。
3. 如果内置 workflow 缺失，创建只含 LLM/final 的安全默认 workflow。

### 7.2 CLI chat

`agentend chat --message hello`：

```text
store user message
run default workflow with input
store assistant message with run_id
return final output
```

### 7.3 Telegram 普通文本

Telegram 非命令文本调用同一 ConversationService。

`/run` 继续支持显式 workflow；普通文本不再走 Echo。

### 7.4 Echo 行为

Echo 只允许存在于 fake provider 或测试 fixture。ConversationService 不直接构造 `Echo: text`。

## 8. 顶层 CLI Alias

设计：

- 保留 `agentend workflows run`。
- 新增 `agentend run`，内部调用同一个 handler。
- 参数、输出、错误码保持一致。

避免复制实现：

```python
def run_workflow_command(...):
    ...

@app.command("run")
def run_alias(...):
    return run_workflow_command(...)

@workflows_app.command("run")
def workflows_run(...):
    return run_workflow_command(...)
```

## 9. Error Taxonomy

Action Policy 阻断应产生结构化错误：

```text
code = external_side_effect_blocked
category = policy
retryable = false
```

普通 OS 权限、provider 403、文件权限错误仍保持：

```text
code = permission_error
category = permission
```

WorkflowRunner 捕获 PermissionError 时不能只按异常类型判断，应该优先识别 ActionPolicyDecision 或专用异常类型。

## 10. Browser Fallback 日志

Browser 工具捕获 Playwright 初始化、权限、transport 异常后：

- 返回结构化 fallback result。
- 不留下未 await 或未 retrieve 的 future。
- 将可读原因写入 tool result warning。
- Doctor 中给出安装、权限或浏览器依赖修复建议。

## 11. 测试设计

新增或更新测试：

- `test_context_runtime_llm_input.py`
- `test_task_status_waiting_input.py`
- `test_mcp_redaction.py`
- `test_llm_openai_endpoint_url.py`
- `test_conversation_default_workflow.py`
- `test_cli_run_alias.py`
- `test_error_taxonomy_policy_block.py`
- `test_browser_fallback_logging.py`

默认使用 fake provider、本地 HTTP fixture、mock MCP server 和临时 AgentEnd home，不依赖真实外网或真实 secret。
