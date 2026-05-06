# AgentEnd Runtime Hardening 审查文档

## 1. 审查范围

本审查文档记录 Runtime Hardening 阶段的已知缺陷、风险判断、修复前假设和验收要求。

审查对象：

- LLM Router。
- Telegram -> WorkflowRunner -> MCP Tool 调用链。
- Builtin Skills。
- Action Policy 和 Tool Contract。
- Result Cache。
- File System 和 Browser artifact 路径。
- Workflow Runner。
- Model Routing 和 Cost Budget。
- Doctor。
- Evidence Manager。

## 2. 当前验证记录

本轮审查已执行：

```bash
python -m compileall -q src tests
git diff --check
.venv\Scripts\python.exe -m pytest tests\test_phase_k_eval_suite_expansion.py tests\test_doctor_evidence_coverage.py -q --basetemp=D:\agentend\codex-test-tmp\basetemp -p no:cacheprovider
.venv\Scripts\python.exe -m pytest tests -q --basetemp=D:\agentend\codex-test-tmp\basetemp -p no:cacheprovider
```

结果：

- compileall 通过。
- diff check 通过。
- R13/R12 相关测试：`8 passed`。
- tests 指定 basetemp 后：`115 passed, 1 skipped`。

R1-R14 已完成源码修复、文档回填并通过全量回归：

- R1：OpenAI-compatible LLM provider 真实请求，默认新 home 使用 fake provider。
- R2：MCP sync 调用可在已有 async event loop 中运行，Telegram `/run` 错误输出改为用户可读。
- R3/R4：`http.request` 按 method 动态分类副作用，POST/PUT/PATCH/DELETE 不进入 Result Cache。
- R5：`fs.*`、`file.*` 和 browser artifact 路径默认限制在 AgentEnd home 或 run artifact 目录内。
- R6：Replay/Scheduler 对高风险副作用采用一致阻断策略，并记录 Action Policy decision。
- R7：Builtin Skills 改为真实工具 workflow，`skills validate` 会校验 `required_tools` 是否由 workflow tool node 覆盖。
- R8：Workflow schema 要求唯一 final，Runner 以 final 节点输出为准，condition 支持 then/else 分支跳过；parallel 首版明确为 fan-in 聚合语义。
- R9：Workflow LLM step 解析 `workflow_step` model route，Context Ledger 记录实际 route provider/model，未配置 route 时回退全局 LLM config。
- R10：每次 LLM step 写入 `cost_usage`，真实 provider usage 优先，fake provider 使用估算 token；`budget show` 汇总 usage calls 和 token。
- R11：Doctor 覆盖 artifacts、sandboxes、Telegram token、MCP server 状态、search provider、skill markets，并保留 browser/vision/local_subprocess 检查。
- R12：Evidence 覆盖 `fs.read_text`、`file.read_text`、`browser.extract`、`browser.screenshot`，截图 source 关联 artifact，`runs export` 输出完整 evidence manifest。
- R13：新增 `runtime-hardening` eval suite，覆盖 LLM fixture、Telegram MCP async、HTTP side effect、path boundary、Skill tool usage、model route/cost 和 evidence export；失败 case 会导出 run。
- R14：README、Runtime Hardening 文档和 Action Layer taskboard 已回填真实 LLM、路径边界、Action Policy、Evidence、Eval 和保留风险说明。

默认 pytest 在当前环境下会被历史临时目录权限阻断，涉及：

- `py-probe-700`
- `pytest-basetemp`
- `test-tmp\pytest-of-btnal`
- `.tmp\pytest-of-btnal`
- `.pytest_cache`

这些是测试环境残留，不属于源码功能失败，但会影响默认 `pytest` 体验。

## 3. 已确认问题

### A1 真实 LLM 未实现

证据：

- `LLMRouter.test()` 只检查环境变量。
- `LLMRouter.complete()` 对非 fake provider 返回 `Echo: {prompt}`。

影响：

- 用户配置 openai provider 后，workflow 不会访问真实模型。
- `llm test` 会给出“configured”的误导性结果。
- Skills、Workflow、Context Eval 中的 LLM 结果并不代表真实 provider 行为。

优先级：P0。

修复验收：

- 本地 HTTP fixture 能收到 Chat Completions 请求。
- 缺 key、401、timeout 均有结构化错误。
- fake provider 测试不依赖网络。

### A2 Telegram 触发 MCP workflow 失败

复现：

在 async 函数中调用：

```python
TelegramMessageRouter(home).handle_text("1", "2", "/run mcp_demo hello")
```

当前结果：

```text
WorkflowRunFailed: asyncio.run() cannot be called from a running event loop
```

原因：

- MCPClient sync 方法内部调用 `asyncio.run()`。
- Telegram handler 运行在 python-telegram-bot 的 async event loop 中。

优先级：P0。

修复验收：

- Telegram async handler 测试覆盖 `/run mcp_demo hello`。
- CLI 同步 MCP workflow 不回退。

### A3 Builtin Skills 是声明强、执行弱

证据：

- `BUILTIN_SKILL_SPECS` 声明 required_tools。
- 生成的 workflow 只有 `llm -> final`。

影响：

- `research.report` 不搜索、不抓取、不写报告。
- Capability Map 会推荐看起来强大的 Skill，但运行结果只是 LLM 文本。
- required_tools 不能作为真实能力保证。

优先级：P1。

修复验收：

- Skill validate 检查 required_tools 被 workflow 使用。
- `research.report` 产生 tool_call、source evidence 和 report artifact。

### A4 `http.request` 副作用分类错误

证据：

- Tool Contract 把 `http.request` 固定为 `network_read`。
- Tool 实现允许任意 method。
- Result Cache 把 `http.request` 列为 cacheable。

影响：

- POST/PUT/PATCH/DELETE 可绕过 scheduler/replay 的网络写入阻断。
- 非幂等请求可能被缓存。
- Action Policy 记录的 decision 与真实副作用不一致。

优先级：P0。

修复验收：

- POST 被分类为 `network_write` 或被拒绝。
- POST 不进入 result cache。
- replay/scheduler 默认阻断 POST。

### A5 路径边界不完整

证据：

- `fs._resolve()` 直接接受绝对路径。
- `fs.delete` 在 `recursive=true` 时直接 `shutil.rmtree(path)`。
- `browser._artifact_path()` 接受绝对路径。

影响：

- Workflow 可删除或写入 AgentEnd home 之外的本机路径。
- Browser screenshot 可写到任意可访问位置。
- 与“本地数据统一位于 AgentEnd home”的文档边界冲突。

优先级：P0。

修复验收：

- home 外绝对路径默认拒绝。
- 删除 allowed root 本身被拒绝。
- browser artifact 只能进入 `data/artifacts/<run_id>/`。

### A6 Workflow 语义未达到复杂编排要求

证据：

- final 输出取 `workflow.nodes[-1]`。
- condition 只返回 true/false。
- parallel 只聚合 depends_on 输出。

影响：

- YAML 中 final 不在最后时输出错误。
- condition 不能真正选择分支。
- parallel 名称与实际执行行为不一致。

优先级：P1。

修复验收：

- workflow 必须唯一 final。
- Runner 输出 final 节点。
- condition demo 能只执行选中分支。

### A7 Model Routing 未接入 LLM 执行

证据：

- `model_routes` 可配置。
- WorkflowRunner 的 LLM step 仍使用 `llm.config.llm.provider/model`。
- 未发现 `cost_usage` 写入链路。

影响：

- 用户设置 routes 后不会影响实际模型选择。
- budget 只能做部分 token/调用数限制，缺少使用记录。
- Eval report 无法定位不同 stage 的模型选择。

优先级：P1。

修复验收：

- `models routes set workflow_step ...` 后 ledger 记录 route 模型。
- 每次 LLM step 写入 cost usage。

### A8 Doctor 和 Evidence 覆盖不足

证据：

- Doctor 只覆盖 Python、dependencies、home、sqlite、llm、browser、vision、local_subprocess。
- Evidence helper 主要覆盖 web fetch/search。

影响：

- Telegram、MCP、Skill Market、artifacts、sandboxes 故障无法通过 doctor 提前发现。
- Browser/File 来源无法在 run export 中完整追溯。

优先级：P2。

修复验收：

- doctor JSON 包含所有文档承诺检查项。
- file/browser sources 出现在 `sources list` 和 export evidence manifest。

当前状态：

- 已修复。回归用例：`tests/test_doctor_evidence_coverage.py`。

## 4. 安全审查要求

### S1 Secret

- API key 只能从环境变量或 `.env` 读取。
- LLM request headers 不进入 DB。
- error、tool_call、event_log、export 都必须脱敏。

### S2 Path

- 所有 destructive path 操作必须先 resolve。
- 目标必须位于允许根内。
- 不允许删除 allowed root 本身。
- 错误中不能建议用户手动删除系统目录。

### S3 Side Effect

- Tool Contract 与真实 input 副作用不一致时，以更高风险为准。
- Action Policy 失败默认 block。
- Replay 默认不重复写入或执行。

### S4 Async Boundary

- 不允许在已有 event loop 中直接 `asyncio.run()`。
- async handler 的异常必须转成用户可读失败信息。

## 5. 测试审查要求

必须新增或更新测试：

- `test_llm_openai_compatible_provider.py`
- `test_telegram_mcp_async_bridge.py`
- `test_http_side_effect_policy.py`
- `test_fs_path_boundaries.py`
- `test_builtin_skills_real_workflows.py`
- `test_workflow_semantics.py`
- `test_model_routing_execution.py`
- `test_doctor_evidence_coverage.py`

每个测试应使用本地 fixture、fake provider 或 mock server，不依赖真实公网和真实 secret。

## 6. 发布前检查清单

发布前必须执行：

```bash
python -m compileall -q src tests
python -m pytest tests -q
git diff --check
agentend eval run runtime-hardening
```

如果 Windows 或沙箱环境存在 pytest temp 权限问题，应显式使用：

```bash
python -m pytest tests -q --basetemp=<writable-temp> -p no:cacheprovider
```

## 7. 保留风险

- `python.exec` 仍是本地子进程，不是安全沙箱。
- Shell/Git/DB 工具仍具备高影响本地能力，需要依赖 Action Policy 和用户输入控制。
- 真实 LLM provider 引入网络、费用和 provider schema 差异。
- Browser Playwright 在不同 OS/权限环境下可用性不稳定，需要 Doctor 给出明确修复建议。
- Skill Market 供应链治理不在本阶段完整解决。
