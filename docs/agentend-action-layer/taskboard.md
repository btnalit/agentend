# AgentEnd Action Layer 任务文档

## 1. 任务目标

按垂直切片实现 AgentEnd Action Layer。每个切片必须形成可运行、可测试、可被 workflow 调用的能力，不做纯横向铺垫。

## 2. 标记说明

- `AFK`：可自动推进。
- `HITL`：需要用户提供外部账号、token、市场地址或产品取舍。
- `Blocked`：依赖前置任务。
- `Done`：已完成。

## 3. 总体依赖和推荐顺序

排序原则：

- 先做 Tool Contract、脱敏、错误分类、Action Policy，所有后续工具一开始就接入统一策略。
- 再做 Eval、Model Routing、Context、Checkpoint，避免 Goal/Replanner/Skill 后续返工。
- 再补核心本地行动工具和 artifacts/replay，让执行链路可验证、可恢复。
- 再做 Search/Evidence/Skill/Extension/Capability，让能力选择和来源证据稳定。
- 最后做外部副作用、自动化调度和生成类能力。

推荐落地顺序：

```text
Phase A: 策略、反馈和成本底座
  T10 Tool CLI + Metadata + Contract
  T34 Secrets Manager + Redaction
  T35 Result Cache + Error Taxonomy 的 error taxonomy 部分
  T45 Action Policy
  T47 Agent Eval Harness
  T48 Model Routing + Cost Budget
  T26 AgentEnd Doctor

Phase B: 上下文和恢复底座
  T36 Context Ledger
  T38 Tool Result Compactor
  T39 Memory Store + Memory CLI
  T40 FTS5 Retrieval
  T37 Context Budgeter + Context Pack Builder
  T41 Context Policy for Workflow/Skill
  T43 Context Preview/Debug
  T44 Context Regression Tests
  T49 Checkpoint / Resume Snapshot

Phase C: 本地行动闭环
  T11 File System 扩展
  T12 Shell Runner
  T13 python.exec local_subprocess
  T28 Git Tool Suite
  T27 Workspace Indexer + Project Profile
  T29 Artifact Manager + Run Replay/Export
  T52 Retention / Cleanup / Backup

Phase D: 信息获取、证据和技能资产
  T14 Search + Fetch
  T51 Source / Evidence Manager
  T15 Skill Library + Builtin Skills
  T50 Extension Lifecycle
  T16 Skill Market
  T31 Capability Map
  T24 Tool Discoverer

Phase E: 规划、恢复和学习闭环
  T46 HITL Clarification Protocol
  T17 Goal Analyzer
  T18 Replanner
  T19 Episode Logger
  T32 Episode to Skill

Phase F: 高影响行动和自动化
  T20 Browser Agent
  T21 DB Writer
  T22 IM Sender
  T23 Vision Analyzer
  T33 File Inbox + CLI stdin/stdout/clipboard
  T30 Task Inbox + Scheduler
  T25 Tool Generator

Phase G: 可复现、可评测、可追溯增强
  T53 Replay 真实回放增强
  T54 Eval Suite 覆盖扩展
  T55 真实 Search Provider + Evidence Export

Phase H: 技能生态和上下文深水区
  T56 Skill Market 远程市场和版本快照
  T57 Context Policy + Budget 深化
  T58 Browser + Vision 真实能力增强

Phase I: 长期运行和多入口生产化
  T59 Scheduler + Inbox 长期运行可靠性
  T60 Storage Retention 实际清理策略
  T61 Telegram 多用户绑定增强
```

编号保留历史稳定性，实际实施按上面的 Phase 顺序推进。

## 4. 任务列表

### T10 Tool CLI + Metadata `AFK`

目标：工具可发现、可查看、可测试。

实施状态：`Done`。已实现 Tool Manifest 表、Tool Contract 同步、`tools list/show/test/enable/disable` 和工具调用记录。

范围：

- `tool_manifests` 表。
- Tool Manifest 模型和统一 Tool Contract。
- Tool Registry 输出 manifest。
- input_schema、output_schema、timeout、side_effect、retryable、requires_secrets、artifact_policy。
- CLI：`tools list/show/test/enable/disable`。

验收：

```bash
agentend tools list
agentend tools show python.exec
agentend tools test memory.write --input '{"key":"k","content":"v"}'
```

测试映射：

- 内置工具 manifest 可列出。
- MCP 工具 manifest 可列出。
- 每个 manifest 都包含 Tool Contract 必填字段。
- tools test 会记录 tool_call。

### T11 File System 扩展 `AFK`

目标：补齐本地文件行动能力。

实施状态：`Done`。已实现 `fs.list/glob/stat/read_text/write_text/copy/move/delete/mkdir`，并接入 Tool Contract、Action Policy、tool summary 和 artifacts。

范围：

- `fs.list`
- `fs.glob`
- `fs.stat`
- `fs.read_text`
- `fs.write_text`
- `fs.copy`
- `fs.move`
- `fs.delete`
- `fs.mkdir`

验收：

```bash
agentend tools test fs.write_text --input '{"path":"a.txt","content":"hello"}'
agentend tools test fs.list --input '{"path":"."}'
```

测试映射：

- 每个 fs 工具有最小集成测试。
- workflow 可调用 `fs.write_text` 并生成 artifact。

### T12 Shell Runner `AFK`

目标：Agent 可运行本地 shell 命令。

实施状态：`Done`。已实现 `shell.run`，支持 cwd、env、timeout、stdout/stderr/exit_code。

范围：

- `shell.run`。
- cwd、env、timeout。
- stdout/stderr/exit_code/duration。

验收：

```bash
agentend tools test shell.run --input '{"command":"python --version"}'
```

测试映射：

- 成功命令返回 exit_code 0。
- 失败命令记录非 0 exit_code。
- timeout 会终止进程并记录错误。

### T13 python.exec local_subprocess `AFK`

目标：替换当前进程内 `exec` 为本地子进程执行后端。

实施状态：`Done`。已切换为 `local_subprocess`，每次调用生成独立 workspace、script.py、stdout/stderr/exit_code，并记录生成文件 artifact。

范围：

- `PythonExecBackend` 抽象。
- `LocalSubprocessPythonBackend`。
- `data/sandboxes/<run_id>/<tool_call_id>/`。
- stdout/stderr/exit_code/artifacts。

验收：

```bash
agentend tools test python.exec --input '{"code":"print(1+1)"}'
```

测试映射：

- stdout 捕获。
- stderr 捕获。
- exit_code 捕获。
- timeout 捕获。
- 生成文件进入 artifacts。

### T14 Search + Fetch `AFK`

目标：Agent 具备实时信息获取能力。

实施状态：`Done`。已实现 `web.fetch` 和 `web.search` fake provider；`web.fetch` 会记录 source evidence。

范围：

- `web.search`
- `web.fetch`
- Search provider 配置。
- HTML 转文本和链接提取。

验收：

```bash
agentend tools test web.fetch --input '{"url":"https://example.com"}'
agentend tools test web.search --input '{"query":"AgentEnd", "limit":3}'
```

HITL：

- 如果选择外部搜索 API，需要用户提供 API key。
- 若使用 MCP search server，需要用户提供 MCP server 配置。

测试映射：

- `web.fetch` 用本地 HTTP fixture。
- `web.search` 用 fake provider 或 mock MCP。

### T15 Skill Library + Builtin Skills `AFK`

目标：Skill Bundle 可被扫描、校验、运行。

实施状态：`Done`。已实现 Skill manifest 扫描、内置 Skill 初始化、`skills list/show/install/validate/run/enable/disable`，Skill run 复用现有 WorkflowRunner 和本地 SQLite 审计链路。

范围：

- Skill manifest schema。
- Skill Registry。
- `skills list/show/validate/run/enable/disable`。
- `skills install`。
- 内置 Skills：`research.report`、`file.workspace_ops`、`code.local_task`、`shell.automation`、`data.quick_analysis`、`mcp.tool_setup`。

验收：

```bash
agentend skills list
agentend skills validate
agentend skills run file.workspace_ops --input '{"task":"list files"}'
```

测试映射：

- skill.yaml 缺字段会校验失败。
- enabled skill 可运行对应 workflow。
- disabled skill 不可运行。

### T16 Skill Market `AFK`

目标：支持默认 curated market 和用户 market。

实施状态：`Done`。已实现 `skill_markets`、directory market、local git working tree market 基础同步、`skills markets list/add/remove` 和 `skills refresh`。默认 curated market URL 仍作为 HITL 配置项保留。

范围：

- `skill_markets` 表。
- market config。
- directory market。
- git market。
- `skills markets list/add/remove`。
- `skills refresh`。

验收：

```bash
agentend skills markets list
agentend skills markets add local --directory ./fixtures/skills-market
agentend skills refresh
```

HITL：

- 默认 curated market URL 可后续由用户确认。

测试映射：

- directory market fixture 可 refresh。
- git market 用本地临时 git repo fixture。

### T17 Goal Analyzer `AFK`

目标：用户自然语言输入可推荐 skill/tool/workflow。

实施状态：`Done`。已实现 `goal.analyze` 工具、`goal analyze` CLI、ConversationService 的 `goal_analysis` 记录，以及基于 Capability Map、Skill、Workflow 和 Workspace Summary 的规则化召回。

实施切片：首版使用规则化分析器，从 Capability Map、enabled Skills、workflow registry 和 workspace summary 召回候选，不新增外部 LLM 依赖。

范围：

- `goal.analyze` 工具。
- CLI：`agentend goal analyze "<text>"`。
- Conversation Service 接入可选 goal analyze。

验收：

```bash
agentend goal analyze "帮我调研浏览器自动化工具"
```

测试映射：

- 对“调研”推荐 `research.report`。
- 对“跑测试”推荐 `code.local_task` 或 `shell.run`。

### T18 Replanner `AFK`

目标：工具失败后能给出可执行下一步。

实施状态：`Done`。已实现 `plan.replan` 工具、`plan replan` CLI、`replan_suggestions` 表，并在 WorkflowRunner 失败时记录结构化 replanner suggestion。

实施切片：首版基于 Error Taxonomy 生成结构化建议，并在 WorkflowRunner 失败时持久化 replanner suggestion。

范围：

- `plan.replan` 工具。
- CLI：`agentend plan replan --failed-step ... --error ...`。
- failed step 输入 schema。
- workflow runner 在失败时记录 replanner suggestion。

验收：

```bash
agentend tools test plan.replan --input '{"failed_step":"web.search","error":"provider missing"}'
```

测试映射：

- provider missing 建议 ask_user 或 alternative_tool。
- unknown tool 建议 tools.discover。

### T19 Episode Logger `AFK`

目标：run 可汇总成 episode。

实施状态：`Done`。已实现 `episodes`、`episode_tools`、`episode_artifacts`，以及 `episodes list/show/summarize` CLI，可汇总 completed/failed run、tool calls、artifacts、error 和 replanner suggestion。

实施切片：首版通过 CLI 对指定 run 汇总 episode，记录 run 状态、workflow/skill、tool calls、artifacts、错误和 replanner suggestion。

范围：

- `episodes`、`episode_tools`、`episode_artifacts`。
- CLI：`episodes list/show/summarize`。
- run completed 后可选自动 summarize。

验收：

```bash
agentend episodes summarize <run_id>
agentend episodes list
agentend episodes show <episode_id>
```

测试映射：

- episode 包含 workflow、tools、artifacts、result。
- failed run episode 包含 error。

### T20 Browser Agent `AFK`

目标：支持真实浏览器操作。

实施状态：`Done`。已实现 `browser.open/click/type/screenshot/extract`，优先使用 Playwright；当本机未安装 Playwright 浏览器时，open/extract/click/type 使用静态 HTML fallback，screenshot 生成明确标记的占位 artifact。

范围：

- Playwright 依赖。
- `browser.open`
- `browser.click`
- `browser.type`
- `browser.screenshot`
- `browser.extract`

验收：

```bash
agentend tools test browser.open --input '{"url":"https://example.com"}'
```

测试映射：

- 用本地 HTML fixture。
- screenshot 写入 artifact。

### T21 DB Writer `AFK`

目标：支持 SQLite 结构化数据读写。

实施状态：`Done`。已实现 `db.query/db.execute/db.write_rows`，支持本地 SQLite 查询、执行和批量插入。

范围：

- `db.query`
- `db.execute`
- `db.write_rows`

验收：

```bash
agentend tools test db.query --input '{"sql":"select 1"}'
```

测试映射：

- query 返回 rows。
- execute 可创建临时表。
- write_rows 可插入数据。

### T22 IM Sender `AFK`

目标：支持 Telegram 发送消息和文件。

实施状态：`Done`。已实现 `im.telegram.send_message/im.telegram.send_file`，支持 dry-run 测试路径；真实发送需要 `TELEGRAM_BOT_TOKEN`。

范围：

- `im.telegram.send_message`
- `im.telegram.send_file`

验收：

```bash
agentend tools show im.telegram.send_message
```

HITL：

- 真实发送需要 `TELEGRAM_BOT_TOKEN` 和 chat_id。

测试映射：

- fake Telegram client 验证 payload。

### T23 Vision Analyzer `AFK`

目标：支持图片、截图、OCR 和图表描述。

实施状态：`Done`。已实现 `vision.describe/vision.ocr/vision.extract_chart` 的 fake provider 基础版，返回图片元数据、占位 OCR 和图表结构骨架。

范围：

- `vision.describe`
- `vision.ocr`
- `vision.extract_chart`

验收：

```bash
agentend tools show vision.describe
```

HITL：

- 真实 vision 需要支持多模态的 LLM provider。

测试映射：

- fake vision provider。
- 本地图片 fixture。

### T24 Tool Discoverer `AFK`

目标：统一发现所有可用行动能力。

实施状态：`Done`。已实现 `tools.discover` 和 `tools.describe`，可查询 Capability 或 Tool Manifest。

范围：

- `tools.discover`
- `tools.describe`
- 内置工具、MCP 工具、Skill 暴露工具、generated draft tools。

验收：

```bash
agentend tools test tools.discover --input '{"query":"search"}'
```

测试映射：

- 搜索 query 能返回相关工具。
- disabled 工具默认不返回。

### T25 Tool Generator `AFK`

目标：生成工具 draft，不自动上线。

实施状态：`Done`。已实现 `tools.generate`，生成 `tool.yaml/implementation.py/test_workflow.yaml` 到 `data/generated_tools/<tool_name>/`，写入 `generated_tools`，并保持 draft 不进入 Tool Registry；Capability Map 会以 `source=generated` 暴露 draft 能力。

范围：

- `tools.generate`
- `generated_tools` 表。
- draft 目录。
- 生成 `tool.yaml`、`implementation.py`、`test_workflow.yaml`。

验收：

```bash
agentend tools test tools.generate --input '{"goal":"parse CSV summary"}'
```

测试映射：

- 生成 draft 文件。
- 不自动 enable。
- validate 失败时不能注册。
- `tools show <generated_tool>` 不会因为 draft 存在而成功。

### T26 AgentEnd Doctor `AFK`

目标：一键诊断本地运行环境和关键依赖。

实施状态：`Done`。已实现 `doctor` 和 `doctor --json`，覆盖 Python、依赖、home、SQLite、artifacts、sandboxes、LLM、search、Telegram token、MCP server 状态、Skill Market、Browser、Vision 和 local subprocess。

范围：

- CLI：`doctor`、`doctor --json`。
- 检查 Python、依赖导入、home、SQLite、artifacts、sandboxes。
- 检查当前 LLM、Telegram token、MCP 配置、Browser backend、local_subprocess。
- 输出 `ok`、`warning`、`error` 和 fix_hint。

验收：

```bash
agentend doctor
agentend doctor --json
```

测试映射：

- fake config 下可输出 ok。
- 缺少 LLM provider 时输出 warning。
- SQLite 不可写时输出 error。

### T27 Workspace Indexer + Project Profile `AFK`

目标：让 Agent 行动前理解当前项目结构和约束。

实施状态：`Done`。已实现 `workspace index/summary`、`workspace_indexes` 和 `project profile show/edit` 基础能力。

范围：

- `workspace_indexes`、`project_profiles` 表。
- CLI：`workspace index`、`workspace summary`、`project profile show/edit`。
- 读取 `AGENTS.md`、`README.md`、`CONTEXT.md`、`docs/`、`pyproject.toml`、`package.json`、测试目录。
- 输出项目类型、入口、常用命令、测试命令、约束文档和风险提示。

验收：

```bash
agentend workspace index
agentend workspace summary
agentend project profile show
```

测试映射：

- 临时项目 fixture 可提取 README 和 AGENTS.md。
- Python 项目可识别 `pyproject.toml` 和 pytest 命令。
- Goal Analyzer 可读取 workspace summary。

### T28 Git Tool Suite `AFK`

目标：补齐代码任务中的版本控制手脚。

实施状态：`Done`。已实现 `git.status/diff/show/log/branch/commit`，其中 commit 必须显式 file list，并使用受控 git wrapper。

范围：

- `git.status`
- `git.diff`
- `git.show`
- `git.log`
- `git.branch`
- `git.commit`

验收：

```bash
agentend tools test git.status --input '{"cwd":"."}'
agentend tools test git.diff --input '{"cwd":"."}'
```

测试映射：

- 临时 git repo 中 status 可返回变更。
- diff 可按 path 过滤。
- commit 必须显式 file list，不允许隐式提交整个工作区。
- 不提供 destructive git 操作。

### T29 Artifact Manager + Run Replay/Export `AFK`

目标：任务可复现、可导出、可审计。

实施状态：`Done`。已实现 `artifacts list/show`、`runs export`、`run_exports`、export manifest、tool contract snapshot 落表和 `runs replay` 首版重新执行语义；历史工具输出复用继续留作后续增强项。

范围：

- `artifact_manifests`、`tool_contract_snapshots`、`run_exports` 表。
- CLI：`artifacts list/show`、`runs replay`、`runs export`。
- 统一归档脚本、截图、报告、下载文件、日志摘要。
- replay 使用原始 input、workflow/skill 版本和 tool contract 快照。
- 首版 replay 使用原 workflow/input 创建新 run，`channel=replay`、`run_mode=replay`，默认阻断外部可见副作用。

验收：

```bash
agentend artifacts list --run <run_id>
agentend runs replay <run_id>
agentend runs export <run_id> --output ./exports
```

测试映射：

- 无副作用 workflow 可 replay。
- 默认拒绝 replay 外部可见副作用工具。
- export 输出 redacted metadata、steps、tool_calls、tool_contract_snapshots、artifacts manifest。

### T30 Task Inbox + Scheduler `AFK`

目标：支持本地任务队列和单机周期触发。

实施状态：`Done`。已实现 `tasks add/list/run/resume`、`schedule add/list/remove/run-now/tick`、`tasks`/`schedules` 表和 scheduler run mode 副作用阻断。

范围：

- `tasks`、`schedules` 表。
- CLI：`tasks add/list/run/resume`。
- CLI：`schedule add/list/remove/run-now/tick`。
- 任务状态：pending、running、blocked、completed、failed。
- Scheduler 触发后创建 task 和 run。
- `tick` 支持 `*`、`*/n`、数字和逗号列表，并用 `last_triggered_at` 避免同一分钟重复触发。

验收：

```bash
agentend tasks add "整理 inbox 文件"
agentend tasks list
agentend tasks run <task_id>
agentend schedule add --workflow simple_chat --cron "0 9 * * *"
agentend schedule tick --now "2026-05-06T09:00:00+08:00"
```

测试映射：

- task 可创建、运行、失败后 resume。
- fake clock 下 scheduler 能触发 run。
- 周期任务记录每次触发的 run_id。
- scheduler run mode 默认阻断 `external_write`。

### T31 Capability Map `AFK`

目标：给 Goal Analyzer 提供统一能力地图。

实施状态：`Done`。已实现 `capabilities refresh/list/query` 和 `capabilities` 表，当前覆盖 enabled tool contracts 和 enabled skills；disabled tool/skill 会从能力地图移除。

范围：

- `capabilities` 表。
- CLI：`capabilities refresh/list/query`。
- 汇总 builtin tools、MCP tools、enabled skills、generated draft tools。
- 使用 Tool Contract 和 Skill Manifest 生成能力记录。

验收：

```bash
agentend capabilities refresh
agentend capabilities query "搜索网页并写报告"
```

测试映射：

- builtin tool 能进入 capability map。
- enabled skill 能进入 capability map。
- disabled tool/skill 默认不进入查询结果。
- Goal Analyzer 优先从 capability map 召回候选能力。

### T32 Episode to Skill `AFK`

目标：把成功任务沉淀成本地 skill 草稿。

实施状态：`Done`。已实现 `skill_drafts` 表、`episodes promote <episode_id> --skill-id <skill_id>`，生成 `skill.yaml/workflow.yaml/README.md/examples/evals`，并支持 `skills validate --path <draft_dir>` 校验草稿。

范围：

- `skill_drafts` 表。
- CLI：`episodes promote <episode_id> --skill-id <skill_id>`。
- 生成 `skill.yaml`、`workflow.yaml`、`README.md`、`examples/`、`evals/`。
- 标记来源 episode、使用工具和未解决风险。

验收：

```bash
agentend episodes promote <episode_id> --skill-id research.custom_report
agentend skills validate --path data/skill_drafts/research.custom_report
```

测试映射：

- successful episode 可生成 skill draft。
- failed episode 默认不能 promote。
- draft 不自动 enable。

### T33 File Inbox + CLI stdin/stdout/clipboard `AFK`

目标：让 Agent 接入 Linux 管道和本地文件投递工作流。

实施状态：`Done`。已实现 `inbox watch --once` 创建 file inbox task、`workflows run --stdin --output json|text`、`clipboard read/write`，并提供 `AGENTEND_CLIPBOARD_FILE` 无头测试后端。

范围：

- CLI：`inbox watch --workflow <workflow_id>`。
- CLI：`workflows run <id> --stdin --output json|text`。
- CLI：`clipboard read/write`。
- inbox watcher 检测新文件后创建 task。

验收：

```bash
Get-Content .\README.md | agentend workflows run simple_chat --stdin --output json
agentend inbox watch --workflow file.workspace_ops
```

测试映射：

- stdin 内容进入 workflow input。
- json/text 输出格式稳定。
- inbox fixture 新文件可创建 task。
- clipboard 工具在无系统 clipboard 时返回明确错误。
- `AGENTEND_CLIPBOARD_FILE` 文件后端可稳定覆盖 clipboard read/write。

### T34 Secrets Manager + Redaction `AFK`

目标：统一 secret 检查和日志脱敏，降低扩展工具后的泄露风险。

实施状态：`Done`。已实现 `secrets list/check`、`secret_refs` 和工具输入/输出/error 的基础脱敏。

范围：

- `secret_refs` 表。
- CLI：`secrets list/check`。
- 统一 `.env` 和环境变量读取。
- event log、episode、run export 默认脱敏。

验收：

```bash
agentend secrets list
agentend secrets check TELEGRAM_BOT_TOKEN
```

测试映射：

- 存在 secret 时只显示名称和来源，不显示原值。
- run export 不包含原始 token。
- 工具 output 中的 token-like 文本会被 redaction 处理。

### T35 Result Cache + Error Taxonomy `AFK`

目标：减少重复调用，并让 Replanner 基于结构化错误工作。

实施状态：`Done`。已完成 Error Taxonomy、`error_records`、工具失败结构化记录和 Result Cache。ToolRegistry 统一缓存 `web.fetch`、`web.search` 和网络读形态的 `http.request`，按 normalized input + config hash 生成 cache key，记录 hit/miss/stale 事件；POST/PUT/PATCH/DELETE 等 `network_write` 不进入缓存。

范围：

- `result_cache`、`error_records` 表。
- 网络读工具缓存：`web.search`、`web.fetch`、`http.request`。
- 标准 error code：`missing_config`、`timeout`、`tool_not_found`、`schema_error`、`permission_error`、`network_error`、`external_side_effect_blocked`、`unknown`。
- Replanner 读取 error code，而不是解析 stderr。

验收：

```bash
agentend tools test web.fetch --input '{"url":"https://example.com"}'
agentend tools test plan.replan --input '{"failed_step":"web.search","error_code":"missing_config"}'
```

测试映射：

- 相同 fetch 输入可命中 cache。
- TTL 过期后重新获取。
- missing_config 会建议配置 provider 或切换可用 MCP。
- timeout 会建议重试或增加 timeout。

### T36 Context Ledger `AFK`

目标：记录每次 LLM 调用实际使用的上下文包。

实施状态：`Done`。已实现 `context_ledgers`、`context_pack_items`，LLM workflow step 会记录上下文包，CLI 可查看 ledger。

依赖：T10、T34、T48。

范围：

- `context_ledgers`、`context_pack_items` 表。
- LLM Router 调用前后写入 ledger。
- 记录 run、step、model route、pack item、token estimate、hash。

验收：

```bash
agentend context ledger show <llm_call_id>
```

测试映射：

- fake LLM 调用会生成 ledger。
- pack item 包含 type、source、summary、token estimate。
- ledger 不保存明文 secret。

### T37 Context Budgeter + Context Pack Builder `AFK`

目标：统一构造 LLM 上下文，避免各模块自行拼 prompt。

实施状态：`Done`。已实现基础 Context Pack Builder 和 token estimate，已接入 preview 和 LLM ledger；后续可继续增强预算裁剪策略。

依赖：T36、T38、T39、T40。

范围：

- Context Budgeter。
- Context Pack Builder。
- 默认预算比例。
- 固定规则、当前任务、最近消息、检索内容、tool summary 合并。

验收：

```bash
agentend context preview --workflow simple_chat --input "hello"
```

测试映射：

- 超预算时裁剪低优先级内容。
- 固定规则和当前任务默认保留。
- tool raw output 不直接进入 context pack。

### T38 Tool Result Compactor `AFK`

目标：工具大输出进入 artifact，上下文只保留摘要。

实施状态：`Done`。已实现 `context_summaries`，工具调用完成后生成 tool result summary 并保留 artifact 引用。

依赖：T10、T34、T35。

范围：

- `context_summaries` 表。
- shell、web、file、db 默认摘要策略。
- workflow runner 调用工具后生成 summary。

验收：

```bash
agentend context compact --run <run_id>
```

测试映射：

- shell 输出多行时只保留关键摘要和末尾片段。
- web fetch 输出保留 title、URL、source id、摘要。
- 原始输出仍可从 artifact 读取。

### T39 Memory Store + Memory CLI `AFK`

目标：提供本地分层记忆。

实施状态：`Done`。已实现 `memory_items`、`memory list/search/write/edit/forget` 和 scope/status 基础能力。

依赖：T34、T36。

范围：

- `memory_items` 表。
- scope：session、task、project、episode、skill、user。
- CLI：`memory list/search/write/edit/forget`。
- TTL、confidence、tags、source、evidence_artifact_id。

验收：

```bash
agentend memory write --scope project --content "测试命令是 python -m pytest -q"
agentend memory search "测试命令"
```

测试映射：

- memory 可按 scope 写入和搜索。
- expired memory 默认不进入检索结果。
- forget 后不再返回。

### T40 FTS5 Retrieval `AFK`

目标：首版用 SQLite FTS5 做低成本检索。

实施状态：`Done`。已实现 SQLite FTS5 virtual table、scope filter、confidence 排序、last_used_at 更新和 contains fallback。

依赖：T39。

范围：

- memory FTS5 virtual table。
- scope、tag、confidence、ttl 过滤。
- 检索结果排序和 last_used_at 更新。

验收：

```bash
agentend memory search "部署 Linux"
```

测试映射：

- query 命中相关 memory。
- scope filter 生效。
- 低 confidence memory 默认降权。

### T41 Context Policy for Workflow/Skill `AFK`

目标：workflow 和 skill 可声明上下文策略。

实施状态：`Done`。Workflow schema 已支持 workflow/step `context` 字段，preview/ledger 可读取；已补齐 global/workflow/step policy merge 和冲突规则。

依赖：T37、T39、T40。

范围：

- `context_policies` 表。
- workflow.yaml / skill.yaml 支持 `context` 字段。
- global/project/workflow/skill/step policy 合并。

验收：

```bash
agentend workflows validate simple_chat
agentend context preview --workflow simple_chat --input "..."
```

测试映射：

- workflow context policy 被读取。
- step override 可以缩小预算。
- step override 不能放宽全局脱敏策略。

### T42 Memory Write Policy + Redaction `AFK`

目标：限制长期记忆写入，避免污染 memory。

实施状态：`Done`。已接入 secret/token-like 脱敏、source trust、scope 限制和 untrusted web/tool output 拦截。

依赖：T34、T39、T45。

范围：

- memory write policy。
- 敏感信息拒写或脱敏。
- untrusted web/tool output 默认不能直接写 project/user memory。

验收：

```bash
agentend memory write --scope project --content "API key is sk-test"
```

测试映射：

- token-like 内容被拒写或脱敏。
- web source 内容默认只能写 episode/task scope。
- manual memory 可写入 project scope。

### T43 Context Preview/Debug `AFK`

目标：让用户和开发者可见上下文构造结果。

实施状态：`Done`。已实现 `context preview`、`context ledger show`，可查看上下文来源、摘要和 token estimate。

依赖：T36、T37、T41。

范围：

- CLI：`context preview`、`context ledger show`。
- 输出 pack item、token estimate、裁剪原因、policy 来源。

验收：

```bash
agentend context preview --workflow research.report --input "Agent memory"
```

测试映射：

- preview 不执行真实 LLM。
- 输出包含每个 pack item 的来源。
- 被裁剪内容有 reason。

### T44 Context Regression Tests `AFK`

目标：建立上下文管理回归测试。

实施状态：`Done`。已新增 Phase B 自动化测试覆盖 ledger、preview、compaction、memory redaction；已通过 Eval Harness 增强接入 `agentend eval run context-smoke`，并让 `smoke` 纳入 context-smoke 基线。

依赖：T37、T43、T47。

范围：

- context eval fixtures。
- lost-context、tool-output-bloat、memory-retrieval、policy-merge 四类用例。
- 纳入 `agentend eval run smoke`。

验收：

```bash
agentend eval run context-smoke
```

测试映射：

- 大工具输出不会挤掉当前任务目标。
- project memory 可被检索进入 context。
- 低优先级历史消息会被裁剪。
- workflow/step/global policy merge 结果可从 eval assertion 和 context ledger 追踪。

### T45 Action Policy `AFK`

目标：所有工具执行前统一判断 side effect 和执行策略。

实施状态：`Done`。已实现 `action_policy_decisions` 和工具调用前 allow/block 决策，后续高影响工具复用该入口。

依赖：T10、T34、T35。

范围：

- `action_policy_rules`、`action_policy_decisions` 表。
- side effect：none、local_read、local_write、local_execute、network_read、network_write、external_write。
- 决策：allow、block、require_clarification。
- Tool Runner 接入 policy。

验收：

```bash
agentend tools test shell.run --input '{"command":"python --version"}'
```

测试映射：

- 只读工具默认 allow。
- replay/scheduler 下 external_write 默认 block。
- high-risk 工具可返回 require_clarification。

### T46 HITL Clarification Protocol `AFK`

目标：统一缺参、歧义、高风险动作的人类输入请求。

实施状态：`Done`。已实现 `clarification_requests` 表、`clarifications list/show`、`human_input` 自动创建 pending request、CLI `runs resume --answer` 和 Telegram 普通消息回答 pending request 的共享恢复路径。

依赖：T45、T49。

范围：

- `clarification_requests` 表。
- CLI：`clarifications list/show`。
- CLI 和 Telegram 共享 request/resume。
- 类型：missing_input、ambiguous_goal、high_risk_action。
- workflow runner pause/resume。

验收：

```bash
agentend runs resume <run_id> --answer "..."
```

测试映射：

- 缺少必填 input 时创建 request。
- 用户回答后从 checkpoint 继续。
- expired request 不能恢复。

### T47 Agent Eval Harness `AFK`

目标：提供任务级智能体回归评测。

实施状态：`Done`。已实现 `eval run smoke`、`eval report` 和 `eval_runs` 记录；已增强 suite list、case/assertion payload、context-smoke 嵌套结果和失败定位字段。

依赖：T10、T34、T35。

范围：

- `eval_suites`、`eval_cases`、`eval_runs` 表。
- CLI：`eval list/run/report`。
- fixture workspace、fake LLM、fake tool。
- assertions 覆盖 output、artifact、tool calls、policy decisions、context ledger。

验收：

```bash
agentend eval run smoke
agentend eval report <eval_run_id>
```

测试映射：

- smoke eval 可运行并生成报告。
- 失败 eval 输出失败断言和关联 run。
- fake LLM 不依赖真实网络。
- `eval report` 输出 machine-readable JSON，包含 suite、cases、assertions、run_id、ledger/tool/policy/artifact 引用。

### T48 Model Routing + Cost Budget `AFK`

目标：按任务阶段选择模型并控制成本。

实施状态：`Done`。已实现 `models routes list/set`、`budget show/set`、`model_routes` 和 `cost_budgets`。

依赖：现有 LLM config。

范围：

- `model_routes`、`cost_budgets`、`cost_usage` 表。
- CLI：`models routes list/set`、`budget show/set`。
- 阶段：goal_analyze、context_compact、workflow_step、replan、vision、final_evaluate。
- budget exceeded 标准错误。

验收：

```bash
agentend models routes list
agentend budget set --workflow simple_chat --max-llm-calls 3
```

测试映射：

- 不同阶段可选不同模型。
- 超出 max_llm_calls 返回 `budget_exceeded`。
- cost usage 进入 run 记录。

### T49 Checkpoint / Resume Snapshot `AFK`

目标：长任务可从稳定步骤恢复。

实施状态：`Done`。已实现 `checkpoints` 表、step completed 后 checkpoint snapshot、`checkpoints list`、`runs resume --checkpoint` 和 `runs resume --answer` 的真实继续执行语义。

依赖：T36、T38、T45。

范围：

- `checkpoints` 表。
- step completed 后保存 workflow version、step cursor、state、context summary、artifacts、policy decisions。
- `runs resume --checkpoint`。
- `runs resume --answer`。

验收：

```bash
agentend checkpoints list --run <run_id>
agentend runs resume <run_id> --checkpoint <checkpoint_id>
```

测试映射：

- workflow 中断后可从 checkpoint 继续。
- checkpoint 不包含明文 secret。
- 半完成 tool call 不生成 checkpoint。

### T50 Extension Lifecycle `AFK`

目标：统一管理 Skill、MCP、Generated Tool、User Market 的生命周期。

实施状态：`Done`。已实现 `extension_records`、`extension_versions`，Skill/Market 安装时自动注册扩展，Skill enable/disable 会同步扩展状态，`extensions list/show/rollback` 可查看和回滚已验证版本元数据。

依赖：T10、T15。

范围：

- `extension_records`、`extension_versions` 表。
- 状态：draft、installed、enabled、disabled、quarantined、removed。
- version、hash、source、last_validated_at。
- rollback。

验收：

```bash
agentend extensions list
agentend extensions show <extension_id>
```

测试映射：

- validate 失败进入 quarantined。
- disabled extension 不进入 capability map。
- rollback 回到上一 validated version。

### T51 Source / Evidence Manager `AFK`

目标：为搜索、抓取、报告和审计建立来源证据链。

实施状态：`Done`。已实现 `source_records`、`evidence_links` 表结构、`sources list/show`；`web.fetch`、`web.search`、`fs.read_text`、`file.read_text`、`browser.extract` 和 `browser.screenshot` 已接入 source record，screenshot source 会关联 artifact。

依赖：T14、T29、T34。

范围：

- `source_records`、`evidence_links` 表。
- web/search、browser extract/screenshot、file read source 记录。
- run export 包含 evidence manifest。

验收：

```bash
agentend sources list --run <run_id>
agentend sources show <source_id>
```

测试映射：

- web.fetch 创建 source record。
- report artifact 可链接 source。
- export 包含 evidence manifest 且脱敏。

### T52 Retention / Cleanup / Backup `AFK`

目标：控制本地数据增长，并支持备份恢复。

实施状态：`Done`。已实现 `storage usage`、`storage cleanup --dry-run`、`storage backup`、`storage restore` 和 cleanup 记录基础版。

依赖：T29、T34、T39。

范围：

- `storage_retention_rules`、`storage_cleanup_runs` 表。
- CLI：`storage usage/cleanup/backup/restore`。
- artifacts、sandboxes、cache、exports、memory、skill drafts 统计。
- cleanup dry-run。

验收：

```bash
agentend storage usage
agentend storage cleanup --older-than 30d --dry-run
agentend storage backup --output ./backups
```

测试映射：

- dry-run 不删除文件。
- pinned episode、enabled skill、manual memory 不默认删除。
- backup 后可 restore 到临时 home。

### T53 Replay 真实回放增强 `AFK`

目标：让历史 run 不只是重新执行，而是可规划、可复用、可解释地 replay。

实施状态：`Done`。已落地 replay plan、`runs replay --dry-run`、历史工具/步骤输出复用、contract drift 检测和外部可见写入 block 报告；不引入新队列或多 Agent。

优先级：后续增强第 1 位。T29 已有 tool contract snapshot，先做 replay 增强能立刻提升调试、审计和 eval 失败定位质量。

依赖：T29、T35、T45、T47。

范围：

- `runs replay --dry-run`。
- replay plan：per-step `reuse_output/rerun/skip/block`。
- 历史 tool output 复用。
- contract drift 检测。
- replay report 输出 skip reason、contract diff、source run。

验收：

```bash
agentend runs replay <run_id> --dry-run
agentend runs replay <run_id>
```

测试映射：

- 无副作用 tool output 可复用。
- contract 变化时 report 标记 drift。
- 外部写入默认 block 并说明原因。

### T54 Eval Suite 覆盖扩展 `AFK`

目标：把 Eval 从 smoke 基线扩展成持续回归反馈回路。

实施状态：`Done`。已落地 `tools-smoke`、`skills-smoke`、`runtime-hardening`、失败 eval 自动 run export、human summary 和 machine-readable JSON report；所有 case 使用本地、fake 或 dry-run 输入，不依赖真实外部 API key。`skills-smoke` 同时支持默认 built-in skills、`--skill` 单 skill 和 `--skill-path` 本地 skill draft。

优先级：后续增强第 2 位。T53 后立即增强 eval，可让后续真实 provider、skill market 和调度增强都有任务级回归。

依赖：T47、T44、T53。

范围：

- 高影响工具 eval：Shell、Python Exec、Browser、DB、IM、Vision、Tool Generator。
- 默认 built-in skill eval。
- Episode-to-Skill draft eval 生成。
- 失败 eval 自动关联 run export。
- human summary + machine-readable JSON report。

验收：

```bash
agentend eval list
agentend eval run tools-smoke
agentend eval run skills-smoke
agentend eval run runtime-hardening
agentend eval run skills-smoke --skill-path ./data/skill_drafts/demo.promoted
agentend eval report <eval_run_id>
```

测试映射：

- `tests/test_phase_k_eval_suite_expansion.py::test_tools_smoke_eval_covers_high_impact_tools_and_summary` 覆盖高影响工具 eval case 和 human summary。
- `tests/test_phase_k_eval_suite_expansion.py::test_skills_smoke_eval_runs_builtin_skills` 覆盖默认 built-in skill eval。
- `tests/test_phase_k_eval_suite_expansion.py::test_runtime_hardening_eval_covers_repaired_runtime_paths` 覆盖 LLM fixture、Telegram MCP、HTTP side effect、path boundary、Skill tool usage、model route 和 evidence。
- `tests/test_phase_k_eval_suite_expansion.py::test_runtime_hardening_eval_exports_failed_case_run` 覆盖 runtime-hardening 失败 case 导出 run。
- `tests/test_phase_k_eval_suite_expansion.py::test_failed_tools_smoke_eval_exports_failed_run` 覆盖失败 eval 输出 run export 路径。
- `tests/test_phase_k_eval_suite_expansion.py::test_episode_skill_draft_eval_runs_after_validation` 覆盖 draft skill eval 在 `skills validate --path` 后运行。

验证记录：

```bash
python -m pytest tests\test_phase_k_eval_suite_expansion.py -q
python -m pytest tests\test_phase_i_eval_contract_snapshot.py tests\test_phase_d_skills_lifecycle_market.py tests\test_phase_e_planning_episode.py tests\test_phase_c_local_action_tools.py -q
python -m pytest -q
git diff --check
```

保留风险：

- `runtime-hardening` 使用本地 fixture 覆盖真实 provider 协议和关键调用链，不替代用户生产环境中的真实 API key、网络、限流和费用验收。
- Browser/Playwright 的真实截图能力仍受本机浏览器安装和系统权限影响；eval 保证 fallback 可运行，真实浏览器状态由 `doctor` 暴露。
- 后续新增 HTTP method、provider 或外部写入工具时，仍必须保留动态 side effect、Action Policy、Result Cache guard 和 evidence/export 回归。

### T55 真实 Search Provider + Evidence Export `HITL`

目标：把 `web.search` 从 fake provider 扩展为可配置真实信息获取能力。

实施状态：`Done`。已落地可配置 provider 架构、Brave Search API adapter、本地 fixture 可测路径、Secret 检查、search/fetch evidence manifest、run export evidence manifest 和缺 secret 的结构化错误；真实外部调用需要用户在环境变量中提供 API key，不把 key 写入 DB 或 export。Result Cache 命中时会为当前 run 重建 source evidence。

优先级：后续增强第 3 位。真实信息获取是通用智能体核心手脚，但需要 provider、secret 和证据治理同步落地。

依赖：T14、T34、T35、T51、T54。

范围：

- search provider 配置和 secret check。
- `web.search` 真实 provider adapter。
- search/fetch source manifest。
- run export evidence manifest 完整输出。
- search error taxonomy 和 replanner 映射。

验收：

```bash
agentend tools test web.search --input '{"query":"agentend"}'
agentend tools test web.search --input '{"query":"agentend","provider":"brave","limit":3}'
agentend sources list --run <run_id>
agentend runs export <run_id> --output ./exports
```

测试映射：

- `tests/test_phase_d_search_evidence_capabilities.py` 覆盖 fake provider 仍可离线测试。
- `tests/test_phase_l_search_provider_evidence_export.py::test_brave_search_provider_records_sources_cache_and_export` 覆盖 Brave-compatible provider、source evidence、result cache 和 export manifest。
- `tests/test_phase_l_search_provider_evidence_export.py::test_web_search_cache_hit_recreates_evidence_for_current_run` 覆盖 cache hit 重建当前 run 的 evidence。
- `tests/test_phase_l_search_provider_evidence_export.py::test_missing_search_secret_records_structured_error_without_leaking_value` 覆盖真实 provider 缺 secret 时返回结构化错误。

验证记录：

```bash
python -m pytest tests\test_phase_l_search_provider_evidence_export.py -q
python -m pytest tests\test_phase_d_search_evidence_capabilities.py tests\test_phase_h_context_reliability.py tests\test_phase_c_workspace_artifacts_storage.py tests\test_phase_k_eval_suite_expansion.py -q
python -m pytest tests\test_init_cli.py tests\test_llm_agent_cli.py tests\test_phase_i_eval_contract_snapshot.py -q
python -m pytest -q
git diff --check
```

### T56 Skill Market 远程市场和版本快照 `HITL`

目标：让 Skill Market 支持真实远程市场、缓存和文件级 rollback。

实施状态：`Done`。已落地本地 git fixture/远程 git URL 共用的 market cache、每个 skill bundle 的 validated snapshot、坏包 quarantine report 和 `extensions rollback` 文件级恢复；真实远程 URL 仍需用户显式添加 market，不默认自动拉取。

优先级：后续增强第 4 位。真实市场会扩大能力面，必须在 eval 和 evidence 基础稳定后进入。

依赖：T15、T16、T50、T54。

范围：

- 默认 curated market URL。
- 远程 git market refresh。
- market cache 和 version snapshot。
- extension rollback 恢复真实 skill 文件内容。
- 坏包 quarantined 和错误报告。

验收：

```bash
agentend skills markets add curated --git <url>
agentend skills refresh
agentend extensions rollback skill:<skill_id>
```

测试映射：

- `tests/test_phase_m_skill_market_snapshots.py::test_git_market_refresh_writes_cache_snapshots_and_file_rollback` 覆盖本地 git fixture market refresh、cache snapshot 和文件级 rollback。
- `tests/test_phase_m_skill_market_snapshots.py::test_bad_skill_bundle_is_quarantined_without_blocking_valid_market_skills` 覆盖坏包 quarantine、错误报告和 Capability Map 隔离。

验证记录：

```bash
python -m pytest tests\test_phase_m_skill_market_snapshots.py -q
python -m pytest tests\test_phase_d_skills_lifecycle_market.py tests\test_phase_k_eval_suite_expansion.py tests\test_phase_l_search_provider_evidence_export.py -q
python -m pytest -q
git diff --check
```

### T57 Context Policy + Budget 深化 `AFK`

目标：增强长任务上下文稳定性和可解释性。

实施状态：`Done`。已落地 context policy CLI、dropped context reason、memory 守门、workflow budget 执行守门和 `context-long` eval；验证已补跑通过。

优先级：后续增强第 5 位。真实 search/skill 后上下文来源变复杂，需要补 policy CLI、裁剪 reason 和更强 eval。

依赖：T37、T40、T41、T42、T44、T55、T56。

范围：

- project/skill context policy CLI。
- dropped context reason。
- 长对话、多 workflow、真实 search provider 的 context eval。
- 低置信/过期/不可信 memory 的 context 守门。
- workflow budget 执行守门：`max_llm_calls`、`max_input_tokens` 和 `max_output_tokens` 不再只保存配置。

验收：

```bash
agentend context policy set --scope project --target default --json '{"max_items":8}'
agentend context preview --workflow <workflow_id> --input "..."
agentend eval run context-long
```

测试映射：

- policy CLI 写入后 preview/ledger 生效。
- 被裁剪条目包含 reason。
- skill policy 不能放宽 global redaction。
- 预算超限时 workflow 失败并输出 `budget_exceeded` 分类。
- `context-long` eval 覆盖长输入、多 workflow、真实 search provider fixture、skill policy merge 和 memory 守门。

当前记录：

- 新增红测已写入 `tests/test_phase_n_context_policy_budget.py`。
- 核心实现和验证已完成：T57 新增测试、受影响回归、全量 `tests/` 回归和 `git diff --check` 均已通过。

### T58 Browser + Vision 真实能力增强 `HITL`

目标：提升 Browser Agent 和 Vision Analyzer 的真实可用性。

实施状态：`Done`。已按 Linux 运行环境设计 Browser Playwright/Chromium 检查和 fallback 诊断；browser action 会记录 URL、title、DOM excerpt、screenshot artifact 和 fallback reason。Vision 已支持 `fake`、OpenAI-compatible 和 Gemini provider，真实 provider 需要显式 provider/secret 配置，默认仍可离线 eval。

优先级：后续增强第 6 位。Browser/Vision 是高收益手脚，但依赖本机浏览器和多模态 provider，应在 eval 扩展后进入。

依赖：T20、T23、T26、T34、T48、T54。

范围：

- Browser Doctor 检查 Playwright 安装。
- browser action 记录 URL、title、screenshot、DOM excerpt、fallback。
- Vision real provider adapter。
- OCR、图表解析和图片描述真实路径。

验收：

```bash
agentend doctor
agentend tools test browser.screenshot --input '{"url":"http://127.0.0.1:8000"}'
agentend tools test vision.ocr --input '{"path":"./image.png"}'
```

测试映射：

- 无 Playwright 或当前环境无法启动 Playwright 时输出明确 fallback artifact 和 fallback reason。
- 有 Playwright/Chromium 时生成真实 screenshot artifact。
- Vision fake provider 仍可离线跑 eval。
- OpenAI-compatible provider 使用 Chat Completions data URL 图片输入。
- Gemini provider 使用 `generateContent` inline image data。
- 缺真实 Vision provider secret 时记录结构化 `missing_config` 错误且不泄露 secret value。

验证记录：

```bash
python -m pytest tests\test_phase_f_browser_agent.py tests\test_phase_f_vision_analyzer.py -q
python -m pytest tests\test_phase_f_browser_agent.py::test_browser_screenshot_uses_playwright_when_available -q
```

### T59 Scheduler + Inbox 长期运行可靠性 `AFK`

目标：让单机长期任务入口具备失败隔离、限流和去重。

实施状态：`Done`。已落地 cron validate、调度连续失败自动暂停、inbox 批量上限和 hash 去重、watch backoff，以及 task/schedule/inbox 的 source、run_mode、batch_id 关联。

优先级：后续增强第 7 位。自动化入口会放大错误，必须在 replay/eval/search/context 稳定后生产化。

依赖：T30、T33、T45、T49、T54。

范围：

- cron 语法增强或 validate 明确阻断。
- 连续失败阈值自动暂停 schedule。
- inbox 批量限流、backoff、文件 hash 去重。
- task/schedule/inbox 与 run_mode/source 的完整关联。

验收：

```bash
agentend schedule validate --cron "*/5 * * * *"
agentend schedule tick
agentend inbox watch --once
```

测试映射：

- 连续失败后 schedule 自动 paused。
- 相同文件不会重复创建 task。
- scheduler run 默认阻断外部写入。

验证记录：

```bash
python -m pytest tests\test_phase_f_inbox_tasks_tool_generator.py tests\test_phase_o_scheduler_inbox_reliability.py -q
python -m pytest -q
git diff --check
```

### T60 Storage Retention 实际清理策略 `AFK`

目标：把 storage cleanup 从 dry-run 推进到可控实际清理。

实施状态：`Done`。已实现 storage retention 规则记录、cleanup plan id、`--dry-run` 计划、`--confirm` actual mode、删除项路径/大小/原因/rule 审计、受保护数据默认保留，以及 restore 到新 home 且拒绝覆盖已有 DB。

优先级：后续增强第 8 位。长期运行可靠性完成后，数据增长会成为真实问题，再启用实际清理更稳。

依赖：T29、T34、T39、T52、T59。

范围：

- retention rules。
- cleanup plan id。
- `storage cleanup --confirm` actual mode。
- 删除路径、大小、原因、rule 记录。
- restore 到临时 home 验证。

验收：

```bash
agentend storage cleanup --older-than 30d --dry-run
agentend storage cleanup --older-than 30d --confirm
agentend storage restore <backup_path> --home <temp_home>
```

测试映射：

- actual cleanup 只能删除 dry-run plan 覆盖项或显式 confirm 项。
- pinned/manual/enabled/recent 数据默认保留。
- restore 不覆盖当前 home。

验证记录：

```bash
python -m pytest tests\test_phase_c_workspace_artifacts_storage.py tests\test_phase_p_storage_retention.py -q
python -m pytest tests\test_phase_c_workspace_artifacts_storage.py tests\test_phase_p_storage_retention.py tests\test_phase_d_skills_lifecycle_market.py tests\test_phase_g_hitl_resume_replay.py tests\test_phase_j_replay_enhancement.py tests\test_phase_o_scheduler_inbox_reliability.py -q
python -m pytest -q
git diff --check
```

### T61 Telegram 多用户绑定增强 `AFK`

目标：修正 Telegram pending request 的最近 run 策略，支持多用户并发。

实施状态：`Done`。已将 Telegram workflow run 绑定到 `chat_id:user_id`，pending clarification、status 和 cancel 均按 channel + external_user_id 精确匹配；Telegram 输出会脱敏 secret、隐藏 AgentEnd home 路径，并省略原始工具 JSON 输出。

优先级：后续增强第 9 位。当前单机单用户可用，真实多人使用前必须补齐绑定。

依赖：T46、T49。

范围：

- conversation.external_user_id 绑定 chat_id/user_id。
- clarification request 查找按 channel + chat/user + status 精确匹配。
- 多 chat 并发 waiting_input 测试。
- Telegram 输出脱敏边界。

验收：

```bash
agentend telegram serve
agentend clarifications list
```

测试映射：

- 两个 chat 同时 pending 时不会串答。
- 非对应 chat 不能回答别人的 request。
- Telegram 不输出未脱敏 secret、内部路径和 raw tool output。

验证记录：

```bash
python -m pytest tests\test_telegram_entry.py tests\test_phase_g_hitl_resume_replay.py tests\test_phase_q_telegram_multi_user.py -q
python -m pytest -q
git diff --check
```

## 5. 首版 Action Layer 完成定义

- Tool CLI 可列出、查看、测试工具。
- Tool Contract 字段完整，并可生成 Capability Map。
- Action Policy 可为工具调用生成 allow/block/require_clarification。
- Agent Eval Harness 可运行 smoke eval。
- Model Routing 和 Cost Budget 可限制 LLM 调用。
- Context Runtime 可记录、压缩、检索和预览上下文。
- Checkpoint / Resume 可恢复中断 run。
- `agentend doctor` 可诊断本地环境。
- Workspace Indexer 可生成项目 summary。
- P0 工具全部可被 workflow 调用。
- Git Tool Suite 支持只读操作和受控 commit。
- `python.exec` 使用 `local_subprocess`。
- Skill Library 可运行内置 Skills。
- Skill Market 可 refresh 本地 fixture market。
- Goal Analyzer 可推荐 skill/tool。
- Replanner 可对失败生成下一步建议。
- Episode Logger 可生成 run 复盘。
- Run Replay/Export 可复现和导出无副作用任务。
- Task Inbox 可保存、运行和恢复本地任务。
- Episode to Skill 可生成可校验 skill draft。
- Extension Lifecycle 可管理扩展状态和版本。
- Source / Evidence Manager 可输出来源证据链。
- Storage Governance 可统计、清理、备份和恢复本地数据。
- 自动化测试覆盖全部新增行为。
