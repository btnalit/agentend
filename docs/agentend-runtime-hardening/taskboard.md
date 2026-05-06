# AgentEnd Runtime Hardening 任务文档

## 1. 任务目标

本任务板用于修复代码审查中发现的运行时闭环问题。每个任务都必须有可运行的 CLI 或 workflow 验收命令，并补充自动化测试。

## 2. 标记说明

- `AFK`：可由工程实现推进。
- `HITL`：需要用户提供真实 provider key、Telegram token 或部署环境。
- `Blocked`：依赖前置任务。
- `Done`：已完成。

## 3. 推荐顺序

```text
Phase R0: 真实调用和异步边界
  R1 OpenAI-compatible LLM provider
  R2 Telegram + MCP async bridge

Phase R1: 副作用和路径安全
  R3 Dynamic side effect for http.request
  R4 Result Cache side-effect guard
  R5 File and artifact path boundary
  R6 Replay/Scheduler high-risk blocking alignment

Phase R2: Skill 和 Workflow 语义
  R7 Builtin Skills real tool workflows
  R8 Workflow final/condition/parallel semantics

Phase R3: 路由、诊断、证据
  R9 Model Routing execution integration
  R10 Cost usage persistence
  R11 Doctor coverage expansion
  R12 Evidence coverage expansion

Phase R4: 回归和发布验收
  R13 Runtime hardening eval suite
  R14 Documentation and release checklist
```

## 4. 任务列表

### R1 OpenAI-compatible LLM provider `AFK`

状态：`Done`。

目标：非 fake provider 必须真实调用 OpenAI-compatible Chat Completions API。

范围：

- LLM request/response dataclass。
- Fake provider adapter。
- OpenAI-compatible provider adapter。
- `llm test` 真实最小请求。
- LLM response usage 解析。
- secret redaction。

验收：

```bash
agentend llm set --provider openai --model <model>
agentend llm test
agentend workflows run simple_chat --input "ping"
```

测试映射：

- 本地 HTTP fixture 模拟 `/chat/completions`。
- 缺 API key 返回 `missing_config`。
- provider 返回 401/500 时错误分类正确。
- fake provider 离线行为不变。

### R2 Telegram + MCP async bridge `AFK`

状态：`Done`。

目标：Telegram async handler 可运行包含 MCP 工具的 workflow。

范围：

- MCPClient sync runner 不再直接在已有 event loop 中调用 `asyncio.run()`。
- 保留 CLI 同步 MCP 行为。
- Telegram handler 异常变成用户可读错误，不泄露内部 traceback。

验收：

```bash
agentend mcp add demo --stdio "mock:echo"
agentend mcp refresh demo
# Telegram /run mcp_demo hello 能返回 MCP says hello
```

测试映射：

- async test 中调用 `TelegramMessageRouter.handle_text("/run mcp_demo hello")`。
- 同步 CLI MCP workflow 仍通过。

### R3 Dynamic side effect for http.request `AFK`

状态：`Done`。

目标：HTTP method 影响 side effect 决策。

范围：

- `http.request` 对 GET/HEAD/OPTIONS 标记 `network_read`。
- POST/PUT/PATCH/DELETE 标记 `network_write`，或首版拒绝。
- Action Policy decision 记录动态 side effect。

验收：

```bash
agentend tools test http.request --input '{"url":"http://127.0.0.1:8000","method":"GET"}'
agentend tools test http.request --input '{"url":"http://127.0.0.1:8000","method":"POST"}'
```

测试映射：

- GET 可缓存。
- POST 不缓存。
- scheduler/replay 默认阻断 POST。

### R4 Result Cache side-effect guard `AFK`

状态：`Done`。

目标：缓存层只缓存明确无副作用结果。

范围：

- Cache 判断接收 dynamic side effect。
- `http.request` 非 GET 不写 cache。
- cache hit/miss/stale 事件仍保留。

验收：

```bash
agentend tools test http.request --input '{"method":"POST","url":"..."}'
```

测试映射：

- POST 连续两次不会 cache hit。
- web.fetch/web.search 仍可 cache hit。

### R5 File and artifact path boundary `AFK`

状态：`Done`。

目标：所有文件和 artifact 写入默认限制在 AgentEnd home 受控目录。

范围：

- 统一路径解析 helper。
- `fs.*` 使用 helper。
- `browser.screenshot/click/type` artifact 输出限制到 `data/artifacts/<run_id>/`。
- 拒绝绝对路径和 `..` 越界。

验收：

```bash
agentend tools test fs.delete --input '{"path":"C:\\Users","recursive":true}'
agentend tools test browser.screenshot --input '{"url":"https://example.com","path":"C:\\tmp\\x.png"}'
```

测试映射：

- home 外路径被拒绝。
- home 内相对路径正常。
- 删除 allowed root 本身被拒绝。

### R6 Replay/Scheduler high-risk blocking alignment `AFK`

状态：`Done`。

目标：Replay 和 Scheduler 对副作用工具采用一致保守策略。

范围：

- replay 默认阻断 `local_write`、`local_execute`、`network_write`、`external_write`。
- scheduler 默认阻断 `local_execute`、`network_write`、`external_write`。
- 可选配置允许特定 side effect，但不在本轮默认开放。

验收：

```bash
agentend runs replay <run_id> --dry-run
agentend schedule run-now <schedule_id>
```

测试映射：

- replay 不重复 `fs.write_text`。
- scheduler 阻断 `shell.run`。
- ActionPolicyDecision 写入阻断原因。

### R7 Builtin Skills real tool workflows `AFK`

状态：`Done`。

目标：内置 Skills 不再只是 LLM stub。

范围：

- `research.report` 调用 `web.search`、`web.fetch`、`fs.write_text`。
- `file.workspace_ops` 调用 `fs.list`/`fs.glob`。
- `code.local_task` 调用 `git.status`，可选 `shell.run` 测试命令。
- `data.quick_analysis` 调用 `python.exec` 或 `db.query`。
- Skill validate 检查 required_tools 是否被 workflow tool node 使用。

验收：

```bash
agentend skills validate
agentend skills run research.report --input '{"topic":"AgentEnd"}'
agentend artifacts list --run <run_id>
agentend sources list --run <run_id>
```

测试映射：

- 每个默认 Skill 至少产生一个真实 tool_call 或 artifact。
- required_tools 缺失时 validate 失败。

### R8 Workflow final/condition/parallel semantics `AFK`

状态：`Done`。

目标：复杂 workflow 语义明确且可验证。

范围：

- schema 要求唯一 final。
- Runner 最终输出 final 节点。
- condition 支持 then/else 或 branches。
- parallel 如果不真正并发，改为明确 fan-in 语义并更新文档。

验收：

```bash
agentend workflows validate
agentend workflows run conditional_demo --input "..."
```

测试映射：

- 无 final 或多个 final 校验失败。
- final 不是 YAML 最后节点时仍正确输出 final。
- condition 只执行选中分支。

### R9 Model Routing execution integration `AFK`

状态：`Done`。

目标：LLM step 使用配置的 model route。

范围：

- `resolve_model_route(stage)`。
- Workflow LLM step 使用 `workflow_step` route。
- Goal Analyzer/Replanner 后续接入对应 stage。
- Context Ledger 记录实际 route。

验收：

```bash
agentend models routes set workflow_step --provider fake --model route-model
agentend workflows run simple_chat --input "route check"
agentend context ledger show <llm_call_id>
```

测试映射：

- ledger 中 provider/model 等于 route。
- 未配置 route 时回退全局 config。

### R10 Cost usage persistence `AFK`

状态：`Done`。

目标：LLM 成本和 token 使用进入 DB。

范围：

- 增加或补齐 `cost_usage` 表。
- 每次 LLM 调用记录 input/output token estimate。
- provider usage 优先使用真实返回，缺失时使用估算。

验收：

```bash
agentend budget show --workflow simple_chat
agentend workflows run simple_chat --input "..."
```

测试映射：

- 每次 LLM step 写入 cost usage。
- max_llm_calls、max_input_tokens、max_output_tokens 均可阻断。

### R11 Doctor coverage expansion `AFK`

状态：`Done`。

目标：Doctor 覆盖文档承诺的主要运行依赖。

范围：

- artifacts 可写。
- sandboxes 可写。
- Telegram token。
- MCP server 最近状态。
- search provider。
- skill markets。
- browser/vision/local_subprocess 保持。

验收：

```bash
agentend doctor
agentend doctor --json
```

测试映射：

- 缺 Telegram token 为 warning。
- unhealthy MCP server 为 warning/error。
- skill market path 不存在为 warning。

### R12 Evidence coverage expansion `AFK`

状态：`Done`。

目标：browser/file 来源进入 evidence manifest。

范围：

- `fs.read_text` 和 `file.read_text` 记录 `file_read` source。
- `browser.extract` 记录 `browser_extract` source。
- `browser.screenshot` 记录 `browser_screenshot` source 和 artifact link。
- `runs export` 包含上述 sources。

验收：

```bash
agentend tools test fs.read_text --input '{"path":"README.md"}'
agentend sources list --run <run_id>
agentend runs export <run_id> --output ./exports
```

测试映射：

- file read source 有 path、hash。
- browser source 有 URL、title、hash。
- export 中 evidence manifest 完整。

### R13 Runtime hardening eval suite `AFK`

状态：`Done`。

目标：新增一组任务级 eval 覆盖本阶段修复。

范围：

- `eval run runtime-hardening`。
- 覆盖 LLM fixture、Telegram MCP、HTTP side effect、path boundary、Skill tool usage、model route、evidence。

验收：

```bash
agentend eval list
agentend eval run runtime-hardening
agentend eval report <eval_run_id>
```

测试映射：

- suite case 全部本地 fixture 可运行。
- 失败 eval 自动导出 run。

### R14 Documentation and release checklist `AFK`

状态：`Done`。

目标：更新相关文档并形成发布前检查清单。

范围：

- 更新 README 的真实 LLM、路径边界、Action Policy 说明。
- 回填本目录 audit.md。
- 更新 Action Layer taskboard 中相关保留风险。

验收：

```bash
python -m pytest tests -q
git diff --check
```

测试映射：

- 文档命令与 CLI 实现一致。

## 5. 首版完成定义

- R1-R6 全部完成。
- R7 至少完成 `research.report` 和 `file.workspace_ops`。
- R8 至少完成唯一 final 和 final 输出选择。
- R9-R12 全部完成。
- `runtime-hardening` eval 可运行。
- 全量 `tests/` 回归通过。
