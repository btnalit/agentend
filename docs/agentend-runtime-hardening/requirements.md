# AgentEnd Runtime Hardening 需求文档

## 1. 背景

AgentEnd Lite 和 AgentEnd Action Layer 已经形成了单机 Agent 运行时的主要框架：CLI、Telegram、SQLite、workflow、工具注册、Skills、MCP、Eval、Replay、Scheduler、Storage 等模块均已具备基础实现。

本轮代码审查发现，当前实现仍存在若干“文档已声明完成，但真实调用链尚未闭合”的问题。这些问题不会阻止本地 smoke 测试通过，但会在真实 LLM、Telegram 触发 MCP、真实 Skill 行动、外部副作用控制和复杂 workflow 中导致功能异常或安全边界不清。

本阶段命名为 **AgentEnd Runtime Hardening**。

## 2. 目标

本阶段目标不是继续扩展新工具，而是把已经规划和标记完成的关键运行链路补实：

- OpenAI-compatible LLM provider 必须真实发起请求，而不是只做环境变量检查和 Echo。
- Telegram 入口必须能安全触发包含 MCP 工具的 workflow。
- 内置 Skills 必须与其声明的 required_tools 对齐，至少高优先级 Skill 要真实调用工具。
- Action Policy 必须正确区分只读、网络写入、本地写入、本地执行和外部写入。
- 文件系统、浏览器 artifact 和清理能力必须有明确路径边界。
- Workflow Runner 必须对 condition、parallel、final 语义给出稳定、可验证的行为。
- Model Routing 和 Cost Budget 必须进入 LLM 执行链路。
- Doctor 和 Evidence 覆盖必须与文档承诺对齐。

## 3. 范围

### 3.1 必须包含

- 真实 OpenAI-compatible Chat Completions 调用。
- LLM request/response 错误分类、超时和脱敏。
- Telegram async 环境下 MCP 调用链修复。
- MCP client 同步/异步边界梳理。
- `http.request` side effect 动态分类或方法限制。
- Result Cache 只缓存无副作用网络读。
- `fs.*` 路径边界策略。
- `browser.*` artifact 路径边界策略。
- Replay/Scheduler 对 `local_write`、`local_execute`、`network_write`、`external_write` 的一致阻断策略。
- 内置 Skills 的 workflow 与 required_tools 一致性校验。
- `research.report`、`file.workspace_ops`、`code.local_task`、`data.quick_analysis` 至少具备真实工具行动路径。
- Workflow schema 对 `final`、`condition`、`parallel` 的明确规则。
- Model Routing 在 LLM step 中生效。
- Cost usage 表或等价记录。
- Doctor 增加 Telegram、MCP、Skill Market、artifacts、sandboxes、search provider 检查。
- Evidence 覆盖 web、browser、file source，并进入 run export。
- 回归测试覆盖上述链路。

### 3.2 不包含

- 多 Agent 架构。
- 前端 Console。
- Docker、Firecracker、E2B 或远程沙箱。
- 完整审批系统或企业权限治理。
- 自动启用 generated tool。
- 真实第三方 Skill Market 的信任治理扩展。

## 4. 问题清单

### P0-1 真实 LLM 未实现

当前 `LLMRouter.test()` 只检查 API key 环境变量，`complete()` 对非 fake provider 返回 `Echo`。这会导致用户以为已接入真实模型，但实际 workflow 不会访问 provider。

要求：

- `provider=fake` 保持离线可测。
- OpenAI-compatible provider 使用 `base_url`、`api_key_env`、`model`、`temperature`、`max_tokens` 发起真实请求。
- 支持 request timeout。
- 错误进入 Error Taxonomy。
- 不在 DB、event log、export 中保存 API key。

### P0-2 Telegram + MCP 调用链失败

当前 MCP client 同步接口内部使用 `asyncio.run()`。Telegram handler 已在 async event loop 中运行时，触发 MCP workflow 会抛出 `asyncio.run() cannot be called from a running event loop`。

要求：

- Telegram 入口可以运行包含 MCP tool 的 workflow。
- MCP client 提供清晰的 sync/async 双接口，或统一通过线程隔离执行同步 workflow。
- 自动化测试必须覆盖 Telegram async handler -> workflow -> MCP tool。

### P0-3 副作用策略不准确

`http.request` 支持任意 HTTP method，但 Tool Contract 固定为 `network_read`，且 Result Cache 会缓存 `http.request`。POST/PUT/PATCH/DELETE 可能被当作只读工具绕过 Scheduler/Replay 阻断。

要求：

- GET/HEAD/OPTIONS 才能视为 `network_read`。
- POST/PUT/PATCH/DELETE 视为 `network_write`，或首版直接拒绝。
- Cache 只允许缓存明确无副作用的网络读。
- Replay/Scheduler 默认阻断 `network_write`、`external_write`，并重新评估 `local_write`、`local_execute` 默认策略。

### P0-4 路径边界不完整

`fs.*` 和部分 browser artifact 工具允许绝对路径。`fs.delete recursive=true` 可删除任意可访问绝对目录。

要求：

- 默认所有 `fs.*` 只能操作 AgentEnd home 或显式配置的 workspace root。
- 删除目录必须要求 `recursive=true`，且目标必须在允许根内，且不得是允许根本身。
- browser artifact 输出默认写入 `data/artifacts/<run_id>/`，绝对路径默认拒绝。
- 错误信息必须说明被拒绝的路径和允许根。

### P1-1 内置 Skills 仍是 stub

内置 Skills 声明 required_tools，但生成 workflow 只有 `llm -> final`。`research.report` 不会实际调用 `web.search`、`web.fetch`、`fs.write_text`。

要求：

- 内置 Skill 的 workflow 必须实际引用 required_tools，或 required_tools 降级为 optional_tools。
- `research.report` 至少执行 fake/real search、fetch 摘要、写入 report artifact。
- `file.workspace_ops` 至少调用 `fs.list` 或 `fs.read_text`。
- `code.local_task` 至少调用 `git.status` 和可配置测试命令。
- `data.quick_analysis` 至少调用 `python.exec` 或 `db.query`。

### P1-2 Workflow 复杂语义未闭合

`condition` 当前只返回 true/false，不选择分支；`parallel` 只是聚合依赖输出；final 输出取 YAML 原始最后一个节点。

要求：

- schema 明确 `final` 节点必须唯一，或 runner 明确选择 `type=final` 的节点。
- `condition` 支持 `then`/`else` 或 `branches`。
- `parallel` 如果首版不做真正并发，必须重命名语义为 fan-in，或实现并发执行无依赖子节点。
- workflow validate 对不支持的字段和不完整图给出错误。

### P1-3 Model Routing 未接入执行

routes 可配置，但 LLM step 仍使用全局 LLM config。

要求：

- LLM step 根据 stage 读取 `model_routes`。
- Goal Analyze、Context Compact、Workflow Step、Replan、Vision 至少使用各自 route。
- 记录 cost usage：调用次数、输入 token estimate、输出 token estimate、provider、model。
- 超预算必须阻断并产生 `budget_exceeded`。

### P2-1 Doctor 和 Evidence 覆盖不足

Doctor 未覆盖 Telegram、MCP、Skill Market、artifacts、sandboxes 等文档承诺项。Evidence 主要覆盖 web/search，browser/file source 未完整接入。

要求：

- Doctor 输出所有关键依赖的 `ok/warning/error/fix_hint`。
- Evidence Manager 覆盖 `web.fetch`、`web.search`、`browser.extract`、`browser.screenshot`、`fs.read_text`、`file.read_text`。
- `runs export` 包含 evidence manifest。

## 5. 非功能要求

- 继续保持单机 SQLite 架构。
- 不能引入外部队列。
- 不保存明文 secret。
- 高风险工具默认保守阻断，不能默认 allow。
- 新增行为必须有自动化测试。
- 修改必须保持现有 `pytest tests` 回归通过。

## 6. 成功标准

- `agentend llm test` 对 OpenAI-compatible provider 发起真实最小请求。
- `agentend workflows run simple_chat` 在 fake provider 和真实 provider 下都能返回模型结果。
- Telegram async handler 可触发含 `mcp.demo.echo` 的 workflow。
- `http.request` POST 不会被当作 `network_read` 缓存或在 scheduler/replay 中执行。
- `fs.delete` 不能删除 AgentEnd home 之外路径。
- `research.report` 运行后产生 source evidence 和 report artifact。
- `agentend models routes set workflow_step ...` 后 LLM step 使用指定 route。
- `agentend doctor --json` 覆盖 runtime、storage、LLM、Telegram、MCP、Browser、Vision、Skill Market、local_subprocess。
- `python -m pytest tests -q` 通过，且新增测试覆盖本阶段 P0/P1 项。
