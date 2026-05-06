# AgentEnd Runtime Hardening 设计文档

## 1. 设计目标

Runtime Hardening 的设计目标是补齐已有架构中的关键断点。当前系统已经有工具注册、workflow、MCP、Skills、Eval、Replay 和 Storage，不需要重新设计平台。改造原则是：

- 优先修复真实调用链，而不是新增能力面。
- 保持 Tool Contract、Action Policy、Error Taxonomy、Context Ledger、Evidence、Run Export 这些统一入口。
- 对高风险行为采用默认保守策略。
- 保持 fake provider 和本地 fixture 可离线测试。

## 2. 总体链路

```text
CLI / Telegram
    ↓
Conversation / WorkflowRunner
    ↓
Model Router + Context Runtime + Cost Budget
    ↓
LLM Router / Tool Registry
    ↓
Action Policy
    ↓
Execution Backends
    ├─ OpenAI-compatible LLM
    ├─ MCP sync/async bridge
    ├─ fs/browser/http/git/db/im tools
    └─ local_subprocess
    ↓
SQLite + Artifacts + Evidence + Export
```

## 3. LLM Router 设计

### 3.1 Provider 接口

新增内部 provider adapter：

```python
class LLMProvider:
    def complete(self, request: LLMRequest) -> LLMResponse:
        ...

    def test(self) -> LLMTestResult:
        ...
```

首版实现：

- `FakeLLMProvider`
- `OpenAICompatibleProvider`

### 3.2 OpenAI-compatible 请求

请求使用 `POST {base_url}/chat/completions`，除非 `base_url` 已经以 `/chat/completions` 结尾。

输入：

- model。
- messages。
- temperature。
- max_tokens。
- timeout_seconds。

输出：

- content。
- provider。
- model。
- usage input/output token，如果 provider 返回 usage。
- latency_ms。

错误：

- 缺 key：`missing_config`。
- HTTP 401/403：`permission_error`。
- timeout：`timeout`。
- 连接失败：`network_error`。
- schema 不符合预期：`schema_error`。

### 3.3 LLM test

`llm test` 对 fake provider 保持本地通过。对 OpenAI-compatible provider 发起最小请求：

```text
messages=[{"role":"user","content":"ping"}]
max_tokens=8
temperature=0
```

返回时只展示成功、模型名和简短响应，不展示请求 headers 或 secret。

## 4. Model Routing 和 Cost Budget 设计

WorkflowRunner 在每个 LLM step 前解析 route：

```text
stage=workflow_step
  ↓
model_routes[workflow_step] or config.llm
  ↓
LLMRouter.complete(route, prompt/context)
```

Goal Analyzer、Replanner、Vision 后续也使用同一 resolver：

```python
resolve_model_route(session, home, stage) -> LLMConfigView
```

Cost usage 在每次 LLM 调用后写入：

- run_id。
- step_id。
- stage。
- provider。
- model。
- input_tokens。
- output_tokens。
- estimated_cost。

Budget 检查点：

- 调用前检查 max_llm_calls。
- 构造 context 后检查 max_input_tokens。
- 响应后检查 max_output_tokens。

## 5. MCP sync/async 边界设计

### 5.1 问题

MCPClient 当前同步方法使用 `asyncio.run()`。在 Telegram async handler 中调用同步 workflow 时，event loop 已存在，`asyncio.run()` 会失败。

### 5.2 方案

首选方案：MCPClient 提供 sync 方法时使用“安全同步运行器”：

```python
def run_coro_sync(coro):
    if no_running_loop:
        return asyncio.run(coro)
    run coro in a dedicated worker thread with its own loop
```

这样不需要立即把 WorkflowRunner 全部改成 async，也不破坏 CLI 同步路径。

后续可选方案：WorkflowRunner async 化，但这属于更大改造，不进入本轮。

### 5.3 测试

新增测试：

- sync CLI 调用 `mcp.demo.echo`。
- async 函数中调用 `TelegramMessageRouter.handle_text("/run mcp_demo ...")`。
- 确认不出现 `asyncio.run() cannot be called from a running event loop`。

## 6. Action Policy 和 Tool Contract 设计

### 6.1 动态副作用

部分工具副作用不能只由工具名决定，例如 `http.request`：

```text
GET/HEAD/OPTIONS -> network_read
POST/PUT/PATCH/DELETE -> network_write
```

Tool Contract 保留默认 side_effect，但 ToolRegistry 在执行前允许工具提供动态副作用：

```python
class Tool:
    def side_effect_for_input(self, input_data) -> str:
        return self.default_side_effect
```

首版可用 helper：

```python
resolve_side_effect(tool_name, manifest.side_effect, input_data)
```

### 6.2 Replay/Scheduler 策略

默认策略：

| run_mode | 默认允许 | 默认阻断 |
| --- | --- | --- |
| normal | none/local_read/network_read | 仅记录 local_write/local_execute/network_write/external_write |
| replay | none/local_read/network_read | local_write/local_execute/network_write/external_write |
| scheduler | none/local_read/network_read/local_write | local_execute/network_write/external_write |

说明：

- replay 默认不重复本地写入和本地执行，避免重复修改文件或执行 shell。
- scheduler 可允许受控本地写入，但应保留配置项。

### 6.3 Result Cache

Cache 只允许：

- `web.fetch`
- `web.search`
- `http.request` 且 method 是 GET/HEAD/OPTIONS

Cache key 必须包含：

- tool name。
- normalized input。
- provider/config hash。
- dynamic side effect。

## 7. 路径边界设计

### 7.1 允许根

默认允许根：

- AgentEnd home。
- `data/artifacts`。
- `data/sandboxes`。
- 配置中的 workspace root，后续可加入。

统一 helper：

```python
resolve_allowed_path(home, raw_path, *, allowed_roots, allow_absolute=False) -> Path
```

规则：

- 相对路径解析到 home 下。
- 绝对路径默认拒绝。
- 解析后必须位于 allowed_roots 之一。
- 删除目录不能删除 allowed root 本身。

### 7.2 fs 工具

- `fs.read_text/list/glob/stat`：只读，允许 home 内。
- `fs.write_text/copy/move/mkdir`：本地写入，允许 home 内。
- `fs.delete`：本地写入，高风险；目录删除必须 `recursive=true`，且目标不是 root。

### 7.3 browser artifact

browser screenshot/click/type 的输出路径统一写入：

```text
data/artifacts/<run_id>/<safe_file_name>
```

首版不接受绝对 output path。如需导出，用户使用 `runs export` 或 artifact CLI。

## 8. 内置 Skills 设计

### 8.1 原则

Skill manifest 的 `required_tools` 必须由 workflow 实际调用。校验器应检查 workflow tool nodes 是否覆盖 required_tools。

### 8.2 research.report

```text
input topic
  ↓
web.search
  ↓
web.fetch first N urls
  ↓
llm summarize with evidence
  ↓
fs.write_text report.md
  ↓
final report path + source list
```

fake provider 下仍能离线运行。

### 8.3 file.workspace_ops

```text
fs.list / fs.glob
  ↓
llm summarize
  ↓
final
```

### 8.4 code.local_task

```text
git.status
  ↓
fs.read_text selected docs or project summary
  ↓
shell.run configured test command when requested
  ↓
final
```

### 8.5 data.quick_analysis

```text
python.exec or db.query
  ↓
fs.write_text analysis artifact
  ↓
final
```

## 9. Workflow 语义设计

### 9.1 final 节点

Schema 规则：

- workflow 必须有且只有一个 `type=final` 节点。
- runner 最终输出 final 节点输出，而不是 YAML 列表最后一个节点。

### 9.2 condition 节点

首版支持：

```yaml
type: condition
input:
  left: "{some_output}"
  equals: "ok"
then: [success_node]
else: [fallback_node]
```

Runner 根据条件结果跳过未选分支。所有分支最终必须汇入 final。

### 9.3 parallel 节点

首版选择 fan-in 语义：`parallel` 聚合 `depends_on` 输出，不做线程或 async 并发执行。

## 10. Doctor 设计

Doctor 检查项扩展为：

- Python 版本。
- dependencies。
- home。
- SQLite。
- artifacts 可写。
- sandboxes 可写。
- LLM provider。
- search provider。
- vision provider。
- Telegram token。
- MCP servers 最近状态。
- Browser Playwright。
- local_subprocess。
- Skill Market 配置和 cache 可读。

输出继续保持：

```json
{"name":"...", "status":"ok|warning|error", "message":"...", "fix_hint":"..."}
```

## 11. Evidence 设计

新增 source 类型：

- `web`
- `web_search`
- `browser_extract`
- `browser_screenshot`
- `file_read`

Tool 接入点：

- `web.fetch`
- `web.search`
- `browser.extract`
- `browser.screenshot`
- `fs.read_text`
- `file.read_text`

Evidence 仍只记录摘要、hash、来源路径/URL，不保存无限制全文。

## 12. 验证策略

新增测试优先覆盖：

- OpenAI-compatible LLM 本地 HTTP fixture。
- Telegram async + MCP mock echo。
- HTTP POST 被分类为 network_write 且不缓存。
- Replay 不重复 local_write/local_execute。
- `fs.delete` 拒绝 home 外绝对路径。
- browser screenshot 拒绝绝对 output path。
- `research.report` 真实调用 required_tools。
- final 节点唯一性校验。
- model route 在 workflow_step 生效。
- doctor 新增检查项。
- file/browser evidence 进入 export。
