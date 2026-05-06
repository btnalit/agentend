# AgentEnd Action Layer 审计文档

## 1. 审计范围

本审计面向 AgentEnd Action Layer 设计阶段，覆盖：

- 工具扩展。
- Skill Library 和 Skill Market。
- 默认内置 Skills。
- `python.exec local_subprocess`。
- Goal Analyzer。
- Replanner。
- Episode Logger。
- AgentEnd Doctor。
- Workspace Indexer 和 Project Profile。
- Git Tool Suite。
- Artifact Manager、Run Replay 和 Run Export。
- Task Inbox 和 Scheduler。
- Capability Map。
- Episode to Skill。
- File Inbox、CLI stdin/stdout/clipboard。
- Secrets Manager、Result Cache 和 Error Taxonomy。
- Context Runtime：Context Ledger、Budgeter、Compactor、Memory Store、FTS5 Retrieval、Context Policy、Context Preview。
- Action Policy。
- HITL Clarification Protocol。
- Agent Eval Harness。
- Model Routing 和 Cost Budget。
- Checkpoint / Resume Snapshot。
- Extension Lifecycle。
- Source / Evidence Manager。
- Retention / Cleanup / Backup Policy。
- Tool Generator。
- 新增 SQLite 表和审计事件。

本阶段仍不引入多 Agent、前端 Console、外部队列、完整 ops-gate 或审批系统。

## 2. 当前基础审计

当前主线已有：

- 内置工具：`file.read_text`、`file.write_text`、`http.request`、`python.exec`、`memory.search`、`memory.write`。
- MCP 动态工具：`mcp.<server>.<tool>`。
- workflow 节点：`llm`、`tool`、`condition`、`parallel`、`human_input`、`workflow_call`、`final`。
- SQLite 表：conversation、message、run、run_step、workflow_def、tool_call、MCP、memory、artifact、event_log。
- CLI、Telegram、Linux 部署和测试。

主要缺口：

- 工具不可统一查看和测试。
- 工具数量不足，Agent 行动能力受限。
- Skill 还只是 workflow，没有资产化、市场和 eval。
- `python.exec` 不是真正隔离执行后端。
- 没有任务入口分析、失败重规划和 episode 复盘。
- 没有统一环境诊断，部署失败定位成本高。
- 没有 workspace/project 级上下文索引，Agent 容易在行动前缺少项目约束。
- 没有 git 受控工具，代码任务只能绕到 shell。
- 没有 run replay/export，历史任务难复现、难交付、难审计。
- 没有 task inbox/scheduler，系统仍偏即时响应而非可持续执行。
- 没有统一 capability map，Goal Analyzer 对可用工具和 skill 的选择成本高。
- 没有 secret 脱敏、缓存和结构化错误分类的统一层。
- 没有上下文预算、工具结果压缩和上下文审计，长任务容易被历史消息或工具输出撑爆。
- 没有分层 memory 和检索策略，长期经验无法稳定复用，也容易被错误记忆污染。
- 没有统一 Action Policy，Shell、Git、DB、IM、Replay、Scheduler 的副作用判断会分散。
- 没有任务级 Agent Eval，智能体行为退化难以及时发现。
- 没有模型路由和成本预算，长任务成本和 token 消耗不可控。
- 没有明确 checkpoint snapshot，中断恢复和 HITL resume 会依赖临时状态。
- 没有来源证据链，研究、抓取、报告和审计结果难追溯。
- 没有本地 retention/cleanup/backup，长期运行会积累大量 artifacts、cache、sandboxes。

## 3. 任务等级判断

Action Layer 实施阶段属于 T2 行为改动，原因：

- 新增工具调用链。
- 新增 Skill Registry 和市场同步。
- 新增 workflow 可调用工具。
- 新增 SQLite 表。
- 新增 CLI 行为。
- 新增 run 后处理和 episode 汇总。

若后续允许 Tool Generator 自动启用工具、允许 Shell/DB/IM 进行不可逆外部写入，相关切片应提升为 T3。

新增基础能力中，以下切片需要按 T3 标准审计其局部行为：

- `git.commit`：会修改仓库历史，必须限制 file list 和 commit message。
- Run Replay：可能重复外部副作用，默认必须阻断副作用工具。
- Scheduler：可能周期性重复执行副作用 workflow。
- Secrets Manager：涉及敏感信息边界和脱敏可靠性。
- Episode to Skill：可能把错误模式沉淀为可复用资产，必须保持 draft 状态。
- Action Policy：是副作用控制统一入口，失效会影响所有高影响工具。
- Memory Store：长期记忆可能被 prompt injection 或错误结论污染。
- Model Routing / Cost Budget：预算失效会导致长任务成本不可控。
- Checkpoint / Resume：恢复错误状态可能重复执行工具或跳过必要步骤。
- Storage Cleanup / Restore：错误清理会造成本地数据丢失。

## 4. 关键风险

### R1 Shell Runner 行动能力过强

风险：

`shell.run` 可以执行系统命令，能力很强，也可能误删文件或暴露环境变量。

当前策略：

- 本阶段不引入审批和硬边界。
- 必须完整记录 command、cwd、stdout、stderr、exit_code、duration。
- workflow 调用必须显式指定工具。

建议：

- 默认文档明确 Shell Runner 是高影响工具。
- 后续可提供配置开关，但本阶段不做复杂限制。

### R2 File System delete/move/copy 影响本地数据

风险：

`fs.delete`、`fs.move` 会修改本地文件。

当前策略：

- 所有调用记录 tool_call。
- 产物和影响路径写入 output_json。

建议：

- 测试必须覆盖路径解析。
- 不做隐式递归删除，递归行为必须输入显式字段。

### R3 python.exec local_subprocess 不是强沙箱

风险：

local subprocess 仍然运行在本机用户权限下，不是容器/虚拟机隔离。

当前策略：

- 独立 workspace。
- timeout。
- stdout/stderr/exit_code/artifacts 记录。
- 不承诺安全隔离。

建议：

- README 和工具说明必须明确它是执行后端，不是安全沙箱。

### R4 Skill Market 供应链风险

风险：

外部 Skill market 可能包含恶意 workflow、危险工具调用或错误配置。

当前策略：

- Market refresh 只下载或扫描元数据和 bundle。
- 安装后必须 validate。
- Skill enable 是显式动作。

建议：

- 默认 curated market 应只指向可信仓库。
- 首版测试使用本地 fixture market。
- 记录 skill source、版本和 hash。

### R5 Tool Generator 可能生成不可控能力

风险：

自动生成工具会引入代码执行和供应链风险。

当前策略：

- Tool Generator 只生成 draft。
- 不自动 enable。
- 必须生成 test workflow 或 eval。

建议：

- T25 之前必须已有 Tool CLI、Skill eval、Episode Logger。

### R6 Browser Agent 容易受动态页面影响

风险：

网页状态不稳定、选择器变化、网络失败会导致 workflow 不稳定。

当前策略：

- 首版使用本地 HTML fixture 测试。
- 失败时记录 screenshot artifact 和错误。

建议：

- Browser tool 输出必须包含当前 URL、title、截图路径。

### R7 IM Sender 外部可见副作用

风险：

Telegram send message/file 会产生外部可见动作。

当前策略：

- 仅实现 Telegram。
- 真实发送需要 token 和 chat_id。
- tool_call 记录 payload 摘要。

建议：

- 测试使用 fake client。
- 后续如扩展 Email/Slack，再评估 T3 审批。

### R8 Git Tool Suite 可能误提交或污染仓库

风险：

`git.commit` 会改变本地仓库历史，`git.diff` 和 `git.show` 可能暴露敏感代码内容。

当前策略：

- Git 工具只暴露受控子命令。
- `git.commit` 必须显式 file list，不允许隐式提交整个工作区。
- 不提供 `reset --hard`、强制推送或 destructive checkout。

建议：

- commit 前记录 `git.status` 摘要。
- 所有 git 工具输出进入 tool_call，但 run export 需要支持脱敏。

### R9 Run Replay 可能重复副作用

风险：

历史 run 可能包含发送消息、写数据库、写文件、执行 shell 等副作用。直接 replay 可能重复造成外部或本地影响。

当前策略：

- replay 依赖 Tool Contract 的 side_effect 字段。
- 默认只允许无副作用或只读工具复跑。
- 对 `network_write`、`local_execute`、`local_write` 默认阻断。

建议：

- replay 输出必须说明哪些 step 被跳过以及原因。
- 支持 dry-run 预览。

### R10 Workspace Indexer 可能过度采集

风险：

工作区可能包含密钥、大文件、私有资料或无关目录。

当前策略：

- 只生成轻量摘要，不保存大文件全文。
- 默认优先读取项目规范文件和配置文件。
- 遵循 `.gitignore` 和 AgentEnd 忽略配置。

建议：

- 索引结果应记录来源文件路径和截断状态。
- 后续如加入全文向量索引，需要单独审计。

### R11 Scheduler 可能重复执行危险任务

风险：

周期任务会放大一次错误配置的影响，尤其是 Shell、DB、IM、Git commit。

当前策略：

- Scheduler 只做本地触发。
- 每次触发先创建 task 和 run，保留审计链路。
- 周期配置记录 workflow、输入、cron、最近触发时间和状态。

建议：

- 默认不为含外部副作用工具的 workflow 自动创建 schedule。
- 失败次数过多时自动暂停 schedule。

### R12 Secrets 和 Redaction 失败会泄露敏感信息

风险：

LLM 输出、工具 stdout/stderr、run export、episode 可能包含 token、cookie、API key。

当前策略：

- Secret refs 只保存名称、来源和存在状态。
- 不在 SQLite 明文保存 secret 值。
- event log、episode、export 默认脱敏。

建议：

- 脱敏规则测试必须覆盖常见 token-like 字符串。
- Doctor 检查 secret 时只显示存在性。

### R13 Capability Map 可能推荐过强工具

风险：

如果能力地图只按语义相似度推荐，Goal Analyzer 可能为简单任务选择高风险工具。

当前策略：

- Capability record 必须包含 side_effect、risk level、requires_secrets。
- Goal Analyzer 需要优先选择低副作用工具。

建议：

- 对同等匹配度工具，默认选择只读或无副作用工具。
- 记录推荐理由和未选工具原因。

### R14 Episode to Skill 可能沉淀错误经验

风险：

失败、偶然成功或包含敏感数据的 episode 被 promotion 后，会把错误流程变成可复用 skill。

当前策略：

- 只允许 successful episode 默认 promote。
- 只生成 draft，不自动 enable。
- draft 标记来源 episode、工具和风险。

建议：

- 必须生成 eval skeleton。
- `skills validate` 通过前不能安装为 enabled skill。

### R15 Result Cache 可能使用过期或敏感数据

风险：

搜索、抓取、HTTP 缓存可能过期，也可能缓存包含敏感响应。

当前策略：

- cache key 包含 provider、normalized input 和 config hash。
- 支持 TTL。
- cache metadata 记录来源和过期时间。

建议：

- 默认只缓存网络读工具，不缓存外部写入结果。
- Replanner 识别 `cache_stale` 时重新获取。

### R16 Error Taxonomy 误分类会误导 Replanner

风险：

错误分类不准确时，Replanner 可能反复重试无效步骤或跳过必要用户确认。

当前策略：

- 标准 error code 固定且可测试。
- 未识别错误归类为 `unknown`，不做激进自动恢复。

建议：

- 每类 error code 都需要 fixture 测试。
- Replanner 输出保留原始错误摘要。

### R17 Context Runtime 可能丢失关键上下文

风险：

预算裁剪、摘要压缩或检索排序错误时，Agent 可能遗漏关键约束、用户目标或安全规则。

当前策略：

- Context Ledger 记录每次上下文包。
- 固定规则、当前目标、Action Policy 不允许被普通裁剪移除。
- Context Preview 可在不调用 LLM 的情况下查看上下文。

建议：

- 增加 context regression eval。
- 对每个被裁剪条目记录 reason。

### R18 Tool Result Compactor 可能压缩掉关键证据

风险：

Shell 错误、网页来源、DB 查询结果、文件 diff 中的关键细节可能被摘要遗漏。

当前策略：

- 原始输出必须进入 artifact 或 DB。
- context 只放摘要和 artifact 引用。
- Source / Evidence Manager 记录来源和 hash。

建议：

- 对失败命令保留 stderr 摘要和最后 N 行。
- 报告类输出必须引用 evidence，而不是只引用摘要。

### R19 Memory Store 可能被污染

风险：

网页内容、工具输出或错误推断直接写入长期 memory，会在后续任务中被当成可信上下文。

当前策略：

- Memory Write Policy 按 scope、source、confidence、ttl 控制写入。
- untrusted web/tool output 默认不能直接写 project/user memory。
- memory 可 forget、edit、expire。

建议：

- 默认长期 memory 写入需要来源证据。
- 低置信 memory 不作为强约束进入 context。

### R20 Action Policy 决策错误会放大副作用

风险：

如果工具 side_effect 标注错误或策略实现遗漏，高影响工具可能绕过统一判断。

当前策略：

- 所有工具执行前写 `action_policy_decisions`。
- Replay 和 Scheduler 默认阻断外部写入。
- high-risk action 可转为 HITL clarification。

建议：

- Tool Contract 测试必须校验 side_effect。
- Action Policy 失败时默认 block，而不是 allow。

### R21 HITL Clarification 可能中断后无法恢复

风险：

用户回答缺参或高风险确认后，如果没有稳定 checkpoint，workflow 可能从错误位置恢复或重复执行步骤。

当前策略：

- clarification request 必须带 resume_token。
- 恢复依赖 checkpoint snapshot。
- expired request 不能恢复。

建议：

- 所有 HITL request 关联 run、step 和 checkpoint。
- Telegram 和 CLI 共享同一恢复路径。

### R22 Agent Eval 可能产生虚假信心

风险：

Eval case 过少、断言太弱或 mock 过度，会让行为退化无法被发现。

当前策略：

- Eval 覆盖 output、artifact、tool calls、policy decisions、context ledger。
- 默认 skill 至少一个 smoke eval。
- fake LLM 只用于稳定回归，不替代真实集成验证。

建议：

- 失败 eval 必须输出关联 run export。
- 新增高影响工具时必须补 eval。

### R23 Model Routing 和 Cost Budget 可能选错模型

风险：

便宜模型用于高难推理会降低质量；强模型滥用会提高成本。

当前策略：

- 按阶段配置 model route。
- workflow 可设置 max_llm_calls、max_tokens、max_estimated_cost。
- 超预算返回 `budget_exceeded` 结构化错误。

建议：

- Eval 报告展示每个阶段的模型和 token。
- 预算失败由 Replanner 或 HITL 处理，不静默降级。

### R24 Checkpoint / Resume 可能重复执行副作用

风险：

恢复点如果落在工具调用中间，可能重复执行 IM、DB、Shell、Git commit 等副作用。

当前策略：

- 只在 completed step 后生成 checkpoint。
- checkpoint 保存 policy decisions 和 artifacts manifest。
- Resume 前重新执行 Action Policy。

建议：

- 不从半完成 tool call 中恢复。
- 恢复输出必须说明从哪个 checkpoint 开始。

### R25 Extension Lifecycle 可能允许未验证扩展上线

风险：

Skill、MCP、Generated Tool 或 Market 如果绕过 validate，可能引入恶意或错误能力。

当前策略：

- 生命周期统一为 draft、installed、enabled、disabled、quarantined、removed。
- validate 失败进入 quarantined。
- disabled/quarantined 不进入 Capability Map。

建议：

- 记录 source、hash、version、last_validated_at。
- Tool Generator 只能产出 draft。

### R26 Source / Evidence Manager 可能记录不可信来源

风险：

网页、动态页面、用户文件可能过期、被篡改或包含注入内容。

当前策略：

- source record 记录 fetched_at、hash、title、URL/path。
- report artifact 通过 evidence_links 引用来源。
- 不把外部来源自动写入长期 trusted memory。

建议：

- Evidence 只证明“当时使用了该来源”，不等于来源一定可信。
- 对网页来源保留抓取时间和 hash。

### R27 Storage Cleanup / Backup 可能造成数据丢失

风险：

cleanup 误删 artifacts、memory、checkpoint、skill draft 会破坏恢复、审计和经验沉淀。

当前策略：

- cleanup 默认 dry-run。
- pinned episode、enabled skill、manual memory、最近 checkpoint 不默认删除。
- backup/restore 覆盖 SQLite 和关键目录。

建议：

- cleanup run 记录被删除路径。
- restore 必须支持临时 home 验证。

## 5. 数据边界审计

新增数据仍位于 AgentEnd home：

```text
<home>/
  skills/
  data/
    agentend.sqlite
    artifacts/
    sandboxes/
    generated_tools/
    skill_drafts/
    exports/
    inbox/
    cache/
    workspace_index/
    context/
    memories/
    evals/
    checkpoints/
    sources/
    backups/
```

规则：

- SQLite 保存结构化元数据。
- Skill bundle 可以存在项目内置目录、本地 skills 目录或 cache。
- Market 下载内容必须记录 source 和 hash。
- `local_subprocess` workspace 保存在 `data/sandboxes`。
- 生成工具 draft 保存在 `data/generated_tools`。
- Episode to Skill draft 保存在 `data/skill_drafts`。
- Run Export 保存在用户指定目录或 `data/exports`。
- File Inbox 默认保存在 `data/inbox`。
- Result Cache 保存在 `data/cache` 和 SQLite metadata。
- Workspace Index 只保存轻量摘要和来源路径，不保存大文件全文。
- Secret refs 只保存 secret 名称、来源和存在状态，不保存原始 secret 值。
- Context Ledger 只保存上下文条目摘要、hash 和 token estimate，不复制无限制全文。
- Memory Store 按 scope 保存，manual memory 和 project memory 默认不被 cleanup 删除。
- Eval fixture 必须脱离真实外部账号和真实 secret。
- Checkpoint 不保存明文 secret，只保存 redacted config 和 state。
- Source / Evidence 保存来源 metadata、短 quote 摘要和 hash，不把外部内容默认提升为 trusted memory。
- Backup 目录属于本地高敏数据，应继承 AgentEnd home 的访问边界。

## 6. 审计事件要求

新增事件：

| 事件 | 触发 |
| --- | --- |
| `tool.manifest_registered` | 工具 manifest 注册。 |
| `tool.enabled` | 工具启用。 |
| `tool.disabled` | 工具禁用。 |
| `skill.market_added` | 添加 Skill Market。 |
| `skill.market_refreshed` | 刷新 Skill Market。 |
| `skill.installed` | 安装 Skill。 |
| `skill.enabled` | 启用 Skill。 |
| `skill.disabled` | 禁用 Skill。 |
| `skill.run_started` | Skill run 开始。 |
| `skill.run_completed` | Skill run 完成。 |
| `python_exec.subprocess_started` | Python 子进程开始。 |
| `python_exec.subprocess_completed` | Python 子进程结束。 |
| `goal.analyzed` | Goal Analyzer 输出结果。 |
| `plan.replanned` | Replanner 输出结果。 |
| `episode.created` | Episode 汇总完成。 |
| `tool.generated_draft` | Tool Generator 生成 draft。 |
| `doctor.run_started` | Doctor 开始诊断。 |
| `doctor.run_completed` | Doctor 完成诊断。 |
| `workspace.indexed` | Workspace Indexer 更新索引。 |
| `project_profile.updated` | Project Profile 被修改。 |
| `git.tool_called` | Git 工具被调用。 |
| `capability_map.refreshed` | Capability Map 刷新。 |
| `artifact.manifest_created` | Artifact manifest 创建。 |
| `run.replay_started` | Run Replay 开始。 |
| `run.replay_blocked_step` | Replay 阻断副作用步骤。 |
| `run.resumed` | Run 从 clarification 或 checkpoint 恢复。 |
| `run.exported` | Run Export 完成。 |
| `task.created` | Task Inbox 新建任务。 |
| `task.started` | Task 开始执行。 |
| `task.completed` | Task 完成。 |
| `task.failed` | Task 失败。 |
| `schedule.created` | 创建本地 schedule。 |
| `schedule.triggered` | schedule 触发 run。 |
| `episode.promoted_to_skill_draft` | Episode 生成 skill draft。 |
| `inbox.file_detected` | File Inbox 检测到新文件。 |
| `secret.checked` | secret 存在性检查。 |
| `secret.redacted` | 输出内容被脱敏。 |
| `cache.hit` | Result Cache 命中。 |
| `cache.miss` | Result Cache 未命中。 |
| `error.classified` | 工具错误被结构化分类。 |
| `context.ledger_created` | LLM 上下文 ledger 创建。 |
| `context.compacted` | 工具结果或历史上下文被压缩。 |
| `context.previewed` | 用户预览上下文包。 |
| `memory.created` | 新 memory 写入。 |
| `memory.updated` | memory 被编辑。 |
| `memory.forgotten` | memory 被遗忘或软删除。 |
| `memory.retrieved` | memory 被检索进入候选上下文。 |
| `action_policy.decided` | 工具调用前产生策略决策。 |
| `action_policy.blocked` | 策略阻断工具调用。 |
| `clarification.created` | HITL 请求创建。 |
| `clarification.answered` | 用户回答 HITL 请求。 |
| `clarification.expired` | HITL 请求过期。 |
| `eval.run_started` | Agent Eval 开始。 |
| `eval.run_completed` | Agent Eval 完成。 |
| `model.route_selected` | 选择模型路由。 |
| `budget.exceeded` | workflow/run 超出预算。 |
| `checkpoint.created` | 创建 run checkpoint。 |
| `checkpoint.resumed` | 从 checkpoint 恢复。 |
| `extension.status_changed` | 扩展状态变化。 |
| `extension.quarantined` | 扩展验证失败隔离。 |
| `source.recorded` | 来源记录创建。 |
| `evidence.linked` | 来源和产物建立引用。 |
| `storage.usage_reported` | 输出存储用量。 |
| `storage.cleanup_dry_run` | 清理 dry-run 完成。 |
| `storage.cleanup_completed` | 清理完成。 |
| `storage.backup_created` | 创建备份。 |
| `storage.restore_completed` | 恢复备份。 |

## 7. 测试审计要求

实施阶段必须满足：

- 每个新增工具至少一个测试。
- 每个 P0 工具至少一个 workflow 集成测试。
- Skill Market 不依赖真实网络，用 fixture 覆盖。
- `python.exec local_subprocess` 覆盖 stdout、stderr、exit_code、timeout、artifact。
- Shell Runner 覆盖成功、失败、timeout。
- File System 覆盖写入、读取、glob、删除。
- Episode Logger 覆盖 success run 和 failed run。
- Tool Generator 测试必须确认 draft 不自动 enable。
- Doctor 覆盖 ok、warning、error 三种诊断状态。
- Workspace Indexer 覆盖 README、AGENTS.md、pyproject、测试目录和忽略规则。
- Git Tool Suite 使用临时 git repo 覆盖 status、diff、show、commit。
- Run Replay 覆盖无副作用复跑和副作用默认阻断。
- Run Export 覆盖 artifact manifest 和 secret redaction。
- Capability Map 覆盖 builtin、MCP、skill、generated draft 来源。
- Task Inbox 覆盖创建、运行、失败、恢复。
- Scheduler 使用 fake clock 覆盖触发和失败暂停策略。
- Episode to Skill 覆盖 draft 生成、validate、默认不 enable。
- File Inbox 覆盖新文件触发 task。
- CLI stdin/stdout 覆盖管道输入和 json/text 输出。
- Secrets Manager 覆盖 `.env`、环境变量和 token-like 字符串脱敏。
- Result Cache 覆盖 miss、hit、TTL 过期。
- Error Taxonomy 覆盖全部标准 error code。
- Context Runtime 覆盖 ledger、budget、pack builder、compaction、preview。
- Memory Store 覆盖 scope、FTS5 retrieval、TTL、forget、write policy。
- Action Policy 覆盖 allow、block、require_clarification，失败默认 block。
- HITL Clarification 覆盖 CLI/Telegram 共用 request 和 checkpoint resume。
- Agent Eval Harness 覆盖 smoke eval、失败报告和 fake LLM。
- Model Routing 覆盖阶段路由、token 统计和 budget exceeded。
- Checkpoint / Resume 覆盖稳定 step 恢复和副作用不重复执行。
- Extension Lifecycle 覆盖 installed/enabled/disabled/quarantined/rollback。
- Source / Evidence Manager 覆盖 web/browser/file source 和 artifact/report 链接。
- Storage Governance 覆盖 usage、cleanup dry-run、backup、restore。

## 8. 当前文档审计结论

当前四份 Action Layer 文档覆盖：

- 需求边界。
- 架构设计。
- 任务拆分。
- 风险审计。
- 低成本高收益基础能力：doctor、workspace index、git、replay/export、task inbox、capability map、secrets/cache/error taxonomy。
- 长任务可靠性底座：context runtime、action policy、HITL clarification、agent eval、model routing、checkpoint、evidence、storage governance。

未进入当前阶段：

- 代码实现。
- 数据库迁移。
- 默认 Skill 市场仓库实际创建。
- 真实外部 Search API、Telegram 发送、Vision provider 联调。
- 完整权限审批系统。
- 远程强隔离沙箱。
- 向量数据库和复杂 RAG 管线。
- 企业级多租户权限和集中审计后台。

这些应在用户确认文档后，按 `taskboard.md` 逐个垂直切片推进。

## 9. Phase A 实施审计回填

本轮已按 taskboard 推荐顺序完成 Phase A 的基础切片：

- T10 Tool CLI + Metadata + Contract。
- T26 AgentEnd Doctor。
- T34 Secrets Manager + Redaction。
- T35 Error Taxonomy 部分。
- T45 Action Policy。
- T47 Agent Eval Harness。
- T48 Model Routing + Cost Budget。

新增运行时数据表：

- `tool_manifests`
- `action_policy_decisions`
- `error_records`
- `secret_refs`
- `model_routes`
- `cost_budgets`
- `eval_runs`

验证命令：

```bash
python -m pytest tests/test_phase_a_foundation.py -q
python -m pytest -q
```

验证结果：

- `tests/test_phase_a_foundation.py`：4 passed。
- 全量测试：22 passed。

保留风险：

- T35 的 Result Cache 尚未实现，需在 `web.search/web.fetch` 落地时补齐。
- Action Policy 当前只做统一决策记录和基础 block 语义，完整审批流仍不在本阶段范围。
- Eval Harness 当前是 smoke 基线，后续每个默认 skill 和高影响工具都必须补任务级 eval。

## 10. Phase B 实施审计回填

本轮已按 taskboard 推荐顺序推进 Phase B 上下文和恢复底座：

- T36 Context Ledger。
- T37 Context Budgeter + Context Pack Builder 基础版。
- T38 Tool Result Compactor。
- T39 Memory Store + Memory CLI。
- T40 Retrieval 基础版。
- T41 Context Policy 基础 schema。
- T42 Memory Write Policy 基础脱敏。
- T43 Context Preview/Debug。
- T44 Context Regression Tests 基础覆盖。
- T49 Checkpoint / Resume Snapshot 基础 checkpoint。

新增运行时数据表：

- `context_ledgers`
- `context_pack_items`
- `context_summaries`
- `memory_items`
- `memory_retrievals`
- `checkpoints`

验证命令：

```bash
python -m pytest tests/test_phase_b_context_runtime.py -q
python -m pytest -q
```

验证结果：

- `tests/test_phase_b_context_runtime.py`：4 passed。
- 全量测试：26 passed。

保留风险：

- T40 当前是 SQLite contains 检索，FTS5 virtual table 待增强。
- T41 当前只完成 workflow context schema 和 preview/ledger 读取，多层 policy merge 待增强。
- T42 当前只做 secret/token-like 脱敏，source trust 和 scope write policy 待增强。
- T44 当前是 pytest 级回归，`agentend eval run context-smoke` 待 Eval Harness 扩展。
- T49 当前生成 checkpoint 和支持 list，指定 checkpoint resume 需在 HITL/Resume 切片继续补齐。

## 11. Phase C 本地行动工具实施审计回填

本轮已完成 Phase C 的核心本地行动工具：

- T11 File System 扩展。
- T12 Shell Runner。
- T13 `python.exec local_subprocess`。
- T28 Git Tool Suite。

新增运行时能力：

- `fs.list`
- `fs.glob`
- `fs.stat`
- `fs.read_text`
- `fs.write_text`
- `fs.copy`
- `fs.move`
- `fs.delete`
- `fs.mkdir`
- `shell.run`
- `git.status`
- `git.diff`
- `git.show`
- `git.log`
- `git.branch`
- `git.commit`

行为审计：

- 上述工具均进入 Tool Contract。
- 上述工具调用均经过 Action Policy。
- 工具结果会生成 Context Summary。
- `python.exec` 生成文件会进入 Artifact 记录。
- `git.commit` 要求显式 file list，不提供 destructive git 操作。
- `fs.delete` 删除目录必须显式传入 recursive。

验证命令：

```bash
python -m pytest tests/test_phase_c_local_action_tools.py -q
python -m pytest -q
```

验证结果：

- `tests/test_phase_c_local_action_tools.py`：4 passed。
- 全量测试：30 passed。

保留风险：

- `shell.run` 和 `python.exec` 运行在本机用户权限下，不是安全沙箱。
- `fs.*` 当前以本地 home 和显式绝对路径为操作边界，后续如需更强路径治理，应接入更严格 policy。
- Workspace Indexer、Run Replay/Export、Storage Governance 尚未在本轮实现。

## 12. Phase C 工作区、产物和存储实施审计回填

本轮继续完成 Phase C 剩余基础设施：

- T27 Workspace Indexer + Project Profile。
- T29 Artifact Manager + Run Export 部分。
- T52 Retention / Cleanup / Backup。

新增运行时数据表：

- `workspace_indexes`
- `project_profiles`
- `run_exports`
- `storage_cleanup_runs`

新增 CLI：

- `workspace index`
- `workspace summary`
- `project profile show`
- `project profile edit`
- `artifacts list`
- `artifacts show`
- `runs export`
- `storage usage`
- `storage cleanup`
- `storage backup`
- `storage restore`

验证命令：

```bash
python -m pytest tests/test_phase_c_workspace_artifacts_storage.py -q
python -m pytest -q
```

验证结果：

- `tests/test_phase_c_workspace_artifacts_storage.py`：3 passed。
- 全量测试：33 passed。

保留风险：

- `runs replay` 尚未实现，T29 仍为 Partial。
- `storage cleanup` 当前只完成 dry-run 和记录框架，实际删除策略需要在 retention 规则稳定后启用。
- Workspace Indexer 当前是轻量文件摘要，不做全文向量索引。

## 13. Phase D 信息获取、证据和能力地图实施审计回填

本轮完成 Phase D 前半段：

- T14 Search + Fetch。
- T24 Tool Discoverer。
- T31 Capability Map。
- T51 Source / Evidence Manager。

新增运行时数据表：

- `capabilities`
- `source_records`
- `evidence_links`

新增工具和 CLI：

- `web.fetch`
- `web.search`
- `tools.discover`
- `tools.describe`
- `capabilities refresh/list/query`
- `sources list/show`

行为审计：

- `web.fetch` 记录 URL、title、content hash、quote 和 run_id。
- Capability Map 当前从 enabled Tool Contract 生成。
- Tool Discoverer 优先查询 Capability Map，没有能力记录时回退 Tool Manifest。
- `web.search` 首版支持 fake provider；真实 provider 仍需后续配置切片。

验证命令：

```bash
python -m pytest tests/test_phase_d_search_evidence_capabilities.py -q
python -m pytest -q
```

验证结果：

- `tests/test_phase_d_search_evidence_capabilities.py`：2 passed。
- 全量测试：35 passed。

保留风险：

- `web.search` 真实外部 provider 尚未接入。
- Evidence 只证明来源被使用，不代表来源可信。
- Skill Library、Skill Market 和 Extension Lifecycle 已在下一轮 Phase D 回填中实现。

## 14. Phase D Skill、Market 和 Extension 实施审计回填

本轮继续完成 Phase D 的 Skill 资产与扩展生命周期切片：

- T15 Skill Library + Builtin Skills。
- T16 Skill Market。
- T50 Extension Lifecycle。
- T31 Capability Map 的 enabled skill 覆盖。

新增运行时数据表：

- `skill_markets`
- `skills`
- `extension_records`
- `extension_versions`

新增 CLI：

- `skills list`
- `skills show`
- `skills install`
- `skills validate`
- `skills run`
- `skills enable`
- `skills disable`
- `skills refresh`
- `skills markets list`
- `skills markets add`
- `skills markets remove`
- `extensions list`
- `extensions show`
- `extensions rollback`

行为审计：

- 内置 Skill 首次访问时写入 `<home>/skills/builtin` 并注册到 SQLite。
- Skill 运行复用 WorkflowRunner、Run、Step、Context Ledger 和 EventLog。
- disabled Skill 不能运行，且刷新 Capability Map 后不会进入能力召回结果。
- directory market refresh 会扫描 `skill.yaml` 和 `workflow.yaml` 并注册 Skill。
- Extension lifecycle 记录 source、version、hash、status 和 validated version。
- rollback 当前回滚扩展版本元数据和 Skill 版本/启用状态；真实 Skill 文件内容版本恢复需在 market cache 版本快照增强后补齐。

验证命令：

```bash
python -m pytest tests/test_phase_d_skills_lifecycle_market.py -q
python -m pytest tests/test_phase_d_search_evidence_capabilities.py tests/test_phase_d_skills_lifecycle_market.py -q
```

验证结果：

- `tests/test_phase_d_skills_lifecycle_market.py`：3 passed。
- Phase D 相关测试：5 passed。

保留风险：

- 默认 curated market URL 尚未接入，真实远程 git market 需要后续凭证、网络和供应链审计。
- `extensions rollback` 当前是元数据级回滚，不复制恢复历史 Skill 文件内容。
- Skill validate 对已安装坏包会 quarantine extension；market refresh 的坏包隔离和错误报告还可继续增强。

## 15. Phase E 实施前审计口径

T17/T18/T19 首版按低成本本地规则实现：

- Goal Analyzer 只读取本地 Capability Map、Workflow Registry、Skill Registry 和 Workspace Summary，不调用外部 LLM。
- Replanner 只基于 Error Taxonomy、失败 step 和观测信息生成结构化建议，不自动重试高风险工具。
- WorkflowRunner 失败时记录 replanner suggestion，但不绕过 Action Policy、不自动执行后续动作。
- Episode Logger 只汇总本地 run、step、tool_call、artifact、error 和 replanner suggestion，不把 episode 自动提升为 Skill。

主要风险：

- 规则化 Goal Analyzer 的召回质量有限，后续可接模型路由增强，但必须保留可测试的 deterministic fallback。
- Replanner 建议可能过于保守，首版宁可 ask_user 或 alternative_tool，也不自动执行外部可见副作用。
- Episode 摘要来自本地记录，不保证业务结论正确，只作为可追溯复盘单元。

## 16. Phase E Goal、Replanner 和 Episode 实施审计回填

本轮完成 Phase E 的前三个规划和复盘切片：

- T17 Goal Analyzer。
- T18 Replanner。
- T19 Episode Logger。

新增运行时数据表：

- `replan_suggestions`
- `episodes`
- `episode_tools`
- `episode_artifacts`

新增工具和 CLI：

- `goal.analyze`
- `plan.replan`
- `goal analyze`
- `plan replan`
- `episodes list`
- `episodes show`
- `episodes summarize`

行为审计：

- Goal Analyzer 读取 Capability Map、enabled Skills、workflow registry 和 workspace summary，首版不调用外部 LLM。
- ConversationService 会把 `goal_analysis` 写入 run result，保留聊天入口下的可审计目标分析。
- Replanner 基于 Error Taxonomy 输出 `retry`、`fix_input`、`ask_user`、`alternative_tool`、`fail_with_reason` 等结构化动作。
- WorkflowRunner 失败时写入 `replan_suggestions`，并把建议同步写入 run result。
- Episode Logger 从 run 汇总 tools、artifacts、error 和 replanner suggestion，不自动 promote skill。

验证命令：

```bash
python -m pytest tests/test_phase_e_planning_episode.py -q
python -m pytest tests/test_phase_d_search_evidence_capabilities.py tests/test_phase_d_skills_lifecycle_market.py tests/test_phase_e_planning_episode.py -q
```

验证结果：

- `tests/test_phase_e_planning_episode.py`：3 passed。
- Phase D + Phase E 相关测试：8 passed。

保留风险：

- Goal Analyzer 当前是 deterministic fallback，复杂目标拆解质量有限，后续可接模型路由增强。
- Replanner 当前只生成建议，不自动执行恢复动作；这是为了避免绕过 Action Policy。
- Episode summary 当前是结构化摘要，不做业务质量评判；后续 Episode to Skill 必须继续验证成功 episode 才能生成 draft。

## 17. Phase E Episode to Skill 实施审计回填

本轮完成 T32 Episode to Skill：

- 新增 `skill_drafts` 表。
- 新增 `episodes promote <episode_id> --skill-id <skill_id>`。
- 新增 `skills validate --path <draft_dir>`。

行为审计：

- 只有 `completed` episode 可 promote，failed episode 默认拒绝。
- promote 只生成 draft，不写入 `skills` 主表，不自动 enable。
- draft 输出 `skill.yaml`、`workflow.yaml`、`README.md`、`examples/input.json`、`evals/smoke.json`。
- draft metadata 记录 source episode、source run、使用工具、产物和 review 风险。

验证命令：

```bash
python -m pytest tests/test_phase_e_planning_episode.py -q
```

验证结果：

- `tests/test_phase_e_planning_episode.py`：5 passed。

保留风险：

- 生成的 workflow 是可校验草稿，不保证完全复刻原 episode 的所有步骤；启用前必须人工 review。
- eval skeleton 当前是占位断言，后续应接 Agent Eval Harness 生成更强自动化断言。

## 18. Phase F 高影响行动工具实施审计回填

本轮进入 Phase F 并完成首批高影响行动工具：

- T20 Browser Agent。
- T21 DB Writer。
- T22 IM Sender。
- T23 Vision Analyzer。

新增工具：

- `browser.open`
- `browser.click`
- `browser.type`
- `browser.screenshot`
- `browser.extract`
- `db.query`
- `db.execute`
- `db.write_rows`
- `im.telegram.send_message`
- `im.telegram.send_file`
- `vision.describe`
- `vision.ocr`
- `vision.extract_chart`

行为审计：

- Browser Agent 优先使用 Playwright；若本机缺 Playwright browser executable，则 open/extract/click/type 使用静态 HTML fallback，screenshot 生成明确标记的占位 artifact。
- Browser click/type 标记为 `network_write`，open/extract/screenshot 标记为 `network_read`。
- DB Writer 仅实现 SQLite，`db.query` 只接受 SELECT，`db.execute` 和 `db.write_rows` 标记为 `local_write`。
- IM Sender 仅实现 Telegram，真实发送需要 `TELEGRAM_BOT_TOKEN`，测试路径使用 `dry_run`。
- Vision Analyzer 当前是 fake provider，返回图片元数据、占位 OCR 和图表结构骨架。

验证命令：

```bash
python -m pytest tests/test_phase_f_browser_agent.py tests/test_phase_f_db_writer.py tests/test_phase_f_im_sender.py tests/test_phase_f_vision_analyzer.py -q
```

验证结果：

- Phase F 高影响工具测试：4 passed。

保留风险：

- Browser Agent 在未安装 Playwright 浏览器时不是完整真实浏览器行为；需要用户运行 `playwright install` 后才能获得真实截图和交互。
- IM Sender 的真实 Telegram 发送未在本地自动化测试中调用，避免产生外部可见副作用。
- Vision Analyzer 需后续接入真实多模态 provider 才能输出真正 OCR 和图表解析。

## 19. Phase F 自动化入口和生成工具实施审计回填

本轮完成：

- T33 File Inbox + CLI stdin/stdout/clipboard。
- T30 Task Inbox + Scheduler。
- T25 Tool Generator。

新增入口和模型：

- `workflows run <id> --stdin --output json|text`。
- `clipboard read/write`，支持 `AGENTEND_CLIPBOARD_FILE` 文件后端。
- `inbox watch --workflow <workflow_id> --once`。
- `tasks add/list/run/resume`。
- `schedule add/list/remove/run-now/tick`。
- `tools.generate`。
- `tasks`、`schedules`、`generated_tools` 表。

行为审计：

- File Inbox 默认扫描 `data/inbox`，检测到新文件后创建 `source=file_inbox` 的 task，workflow input 为文件路径，并用 `source_path` 去重。
- `workflows run --output json` 输出稳定 `status/run_id/output` 或 `status/run_id/error` 字段，适配管道调用。
- Clipboard 不进入 workflow 默认输出；无系统 clipboard 时返回明确错误，测试使用文件后端。
- Task 运行复用 `WorkflowRunner`，完成后回写 task status、run_id 和 error。
- Scheduler 首版不常驻后台，由 `run-now` 或 `tick` 显式触发；`tick` 支持基础五段 cron，并用 `last_triggered_at` 避免同一分钟重复运行。
- Scheduler 运行 workflow 时传入 `run_mode=scheduler`，Action Policy 默认阻断 `external_write` 和 `network_write`。
- Tool Generator 只生成 draft 文件和 `generated_tools` 行，不把 draft 注册到 Tool Registry，也不自动 enable。
- Tool Generator 生成 extension lifecycle 记录，状态为 `draft`。
- Capability Map 会把 `generated_tools.status=draft` 的记录作为 `source=generated` 能力暴露给 Goal/Discover 层，但仍不作为可执行工具。

验证命令：

```bash
python -m pytest tests/test_phase_f_inbox_tasks_tool_generator.py -q
```

验证结果：

- `tests/test_phase_f_inbox_tasks_tool_generator.py`：6 passed。

保留风险：

- Scheduler 的 cron 支持是首版轻量解析器，不支持范围、L/W/# 等高级 cron 语法；后续需要再引入专门解析器或明确文档限制。
- `inbox watch` 是轮询模式，不是 OS 原生文件事件；大量文件投递时需要后续做批量限流和 backoff。
- Tool Generator 生成的是骨架，不具备自动质量判断；启用前仍必须人工 review、补实现并走验证门。

## 20. T46/T49/T29 实施前审计假设

本轮将 HITL、Resume 和 Replay 合并为一个恢复链路切片，原因是三者共享 checkpoint、run status、Action Policy 和 event log。实施前约束：

- Clarification Request 只保存问题、选项、状态、answer 和 resume token，不保存 secret 原值。
- `runs resume --answer` 必须在同一个 run 上继续执行，不新建一条替代 run。
- `runs resume --checkpoint` 只从 completed checkpoint 后继续，不从半个工具调用中恢复。
- Replay 首版创建新 run 重新执行原 workflow/input，不复用历史工具输出。
- Replay 使用 `run_mode=replay`，默认阻断 `network_write` 和 `external_write`，避免复跑外部可见副作用。
- 旧 failed step 和旧 tool call 保留为审计记录，不在 resume 时删除。

## 21. T46/T49/T29 实施审计回填

本轮完成：

- T46 HITL Clarification Protocol。
- T49 Checkpoint / Resume Snapshot 真实恢复语义。
- T29 Run Replay 首版。

新增能力：

- `clarification_requests` 表。
- `clarifications list/show`。
- `human_input` 节点自动创建 pending clarification request。
- `runs resume <run_id> --answer "..."`。
- `runs resume <run_id> --checkpoint <checkpoint_id>`。
- Telegram router 普通消息优先回答最近 pending clarification。
- `runs replay <run_id>`。

行为审计：

- Clarification request 保存 request type、question、reason、choices、resume token、status、answer 和 expires_at；不保存 secret 原值。
- 过期 clarification 会标记为 `expired`，并拒绝 resume。
- `runs resume --answer` 在同一个 run 上完成 waiting step，创建对应 checkpoint，然后继续后续节点。
- `runs resume --checkpoint` 跳过 checkpoint 之前已完成节点，只追加新的后续 RunStep，不删除旧 failed step/tool_call。
- Replay 从源 run 读取 workflow_id 和 input，新建 `channel=replay` run 并使用 `run_mode=replay`。
- Replay 下 `external_write` 和 `network_write` 由 Action Policy 阻断。

验证命令：

```bash
python -m pytest tests/test_phase_g_hitl_resume_replay.py -q
python -m pytest tests/test_phase_b_context_runtime.py tests/test_phase_c_workspace_artifacts_storage.py tests/test_phase_f_inbox_tasks_tool_generator.py tests/test_phase_g_hitl_resume_replay.py -q
python -m pytest -q
```

验证结果：

- `tests/test_phase_g_hitl_resume_replay.py`：5 passed。
- 恢复链路组合回归：18 passed。
- 全量回归：58 passed。

保留风险：

- Replay 首版是重新执行，不是历史工具输出模拟回放；对非确定性 LLM 或网络读结果可能产生不同输出。
- `tool_contract_snapshots` 尚未作为独立表落地，当前 replay 依赖现行 Tool Contract 和 Action Policy。
- Telegram pending request 查找是单机最近 pending run 策略，后续多用户场景需要按 chat/user 精确绑定 run。

## 22. T35/T40/T41/T42 实施前审计假设

本轮进入上下文与可靠性收尾：

- T35 Result Cache。
- T40 FTS5 Retrieval。
- T41 Context Policy merge。
- T42 Memory Write Policy。

实施前约束：

- Result Cache 只缓存网络读工具，不缓存 local_write、local_execute、network_write、external_write。
- Cache hit/miss/stale 必须进入 event log，且 cache hit 仍走 ToolCall 审计路径。
- FTS5 检索不可用时允许 fallback，但 scope、confidence、TTL、last_used_at 行为必须保持。
- Context Policy 下层不能关闭上层 `redact_secrets=true`，预算只能缩小不能放大。
- 不可信 source 不能写入 project/user 长期记忆，避免网页或工具输出污染长期事实。

## 23. T35/T40/T41/T42 实施审计回填

本轮完成：

- T35 Result Cache。
- T40 FTS5 Retrieval。
- T41 Context Policy merge。
- T42 Memory Write Policy。

新增模型和模块：

- `result_cache` 表。
- `context_policies` 表。
- `agentend.core.result_cache`。
- `agentend.core.memory_store`。
- `agentend.core.context_policy`。

行为审计：

- ToolRegistry 对 `web.fetch`、`web.search`、`http.request` 做统一 Result Cache，cache hit 仍创建 ToolCall 并走 Action Policy。
- Result Cache 记录 `cache.miss`、`cache.hit`、`cache.stale`，TTL 过期后重新执行工具并刷新缓存。
- `agentend init` 会初始化 SQLite 表，避免直接进入 Telegram/memory/context 时表不存在。
- Memory 写入同步 `memory_items_fts`；FTS5 不可用或 query 无法生成 FTS token 时回退 contains 检索。
- Memory search 会按 scope 过滤、按 confidence 排序，并更新 `last_used_at` 与 `memory_retrievals`。
- Context Policy 按 global -> workflow -> step 合并；`redact_secrets=true` 不能被下层关闭，`max_items` 只能缩小。
- Memory Write Policy 允许 manual 写 project/user，拒绝 web/tool/untrusted 直接写 project/user；不可信来源可写 session/task/episode。

验证命令：

```bash
python -m pytest tests/test_phase_h_context_reliability.py -q
python -m pytest tests/test_phase_b_context_runtime.py tests/test_phase_d_search_evidence_capabilities.py tests/test_phase_h_context_reliability.py -q
python -m pytest -q
```

验证结果：

- `tests/test_phase_h_context_reliability.py`：4 passed。
- 上下文/搜索/缓存组合回归：10 passed。
- 全量回归：62 passed。

保留风险：

- Result Cache 当前缓存 text/json ToolResult，不缓存 artifact 型工具结果。
- FTS5 中文分词能力依赖 SQLite tokenizer；中文 query 当前会回退 contains 检索。
- Context Policy 当前实现 global/workflow/step 三层，project/skill policy 表结构已在，但 CLI 管理和 skill policy merge 仍可增强。

## 24. T44/T47/T29 治理闭环实施前审计假设

本轮进入剩余治理闭环：

- T47 Agent Eval Harness 增强。
- T44 Context Regression Tests 的 eval 接入。
- T29 Tool Contract Snapshot。

依赖排序说明：

- T44 依赖 T47 的 suite/case/assertion/report 结构，因此先做 Eval Harness 的基础增强，再接 `context-smoke`。
- T29 snapshot 依赖 Tool Contract 已稳定，放在 eval 后补齐，避免 run export/replay 继续依赖当前工具定义。

实施前约束：

- `context-smoke` 使用本地 SQLite、fake LLM 和真实 WorkflowRunner/ToolRegistry，不访问真实外部账号和网络写入。
- Eval report 必须保留 machine-readable JSON，包含 suite、case、assertion、status、关联 run_id 和关键审计对象。
- `smoke` 可以变重，但不能依赖真实 API key；失败必须能通过 `eval report` 定位到 context ledger 或 tool/policy/artifact。
- Tool Contract Snapshot 在 run 级别保存完整 contract JSON，export 必须包含该快照；snapshot 不保存 secret 原值。
- Replay 本轮仍是重新执行语义，不复用历史工具输出。

## 25. T44/T47/T29 治理闭环实施审计回填

本轮完成：

- T47 Agent Eval Harness 增强。
- T44 Context Regression Tests 的 eval 接入。
- T29 Tool Contract Snapshot。

新增模型和模块：

- `tool_contract_snapshots` 表。
- `agentend.core.tool_contracts.snapshot_tool_contracts`。
- `agentend.core.tool_contracts.snapshot_to_dict`。
- `agentend.core.eval_harness.list_eval_suites`。
- `context-smoke` eval suite。

行为审计：

- WorkflowRunner 在 run 创建后同步 Tool Manifest 并创建 run 级 Tool Contract Snapshot。
- ToolRegistry 调用路径会幂等补 snapshot，覆盖 `tools test` 和旧 run 兼容场景。
- `runs export` 在 `run.json` 内输出 `tool_contract_snapshots`，并额外写出 `tool_contracts.json`。
- `eval list` 输出 `smoke` 和 `context-smoke`。
- `eval run context-smoke` 使用本地 fixture workflow、fake/echo LLM 路径、真实 WorkflowRunner、ToolRegistry、Context Ledger 和 SQLite，覆盖 lost-context、tool-output-bloat、memory-retrieval、policy-merge。
- `eval run smoke` 内嵌 context-smoke 结果，并在 `checks.context_smoke_passed` 中显式体现。
- `eval report` 保持 machine-readable JSON，包含 suite、checks、cases、assertions、run_id、context_ledger_id、tool_call_id、policy_decision_id 和 artifact_id。

验证命令：

```bash
python -m pytest tests/test_phase_i_eval_contract_snapshot.py -q
python -m pytest tests/test_phase_a_foundation.py tests/test_phase_b_context_runtime.py tests/test_phase_c_workspace_artifacts_storage.py tests/test_phase_g_hitl_resume_replay.py tests/test_phase_h_context_reliability.py tests/test_phase_i_eval_contract_snapshot.py -q
python -m pytest -q
git diff --check
```

验证结果：

- `tests/test_phase_i_eval_contract_snapshot.py`：3 passed。
- 治理闭环组合回归：23 passed。
- 全量回归：65 passed。
- `git diff --check`：通过；仅有 Windows 行尾转换 warning，无 whitespace error。

保留风险：

- Replay 仍是重新执行语义，不复用历史工具输出；snapshot 已为后续 contract drift 和工具输出复用打基础。
- `context-smoke` 目前覆盖核心上下文退化类型，但还没有覆盖真实长对话、多 workflow 并发和真实外部搜索 provider。

## 26. 后续增强任务审计口径

本轮不改运行时代码，只把剩余增强项正式进入四份规范产物。增强任务编号从 T53 开始，避免改变已完成 T10-T52 的历史任务含义。

推荐优先级：

1. T53 Replay 真实回放增强。
2. T54 Eval Suite 覆盖扩展。
3. T55 真实 Search Provider + Evidence Export。
4. T56 Skill Market 远程市场和版本快照。
5. T57 Context Policy + Budget 深化。
6. T58 Browser + Vision 真实能力增强。
7. T59 Scheduler + Inbox 长期运行可靠性。
8. T60 Storage Retention 实际清理策略。
9. T61 Telegram 多用户绑定增强。

排序理由：

- T53 优先使用已落地的 tool contract snapshot，补齐 replay plan、历史输出复用和 contract drift，能直接增强调试和审计。
- T54 紧随其后，把后续所有真实 provider、skill 和自动化入口都纳入任务级回归。
- T55 再接真实 search provider，因为联网能力收益高，但必须依赖 eval、evidence、secret、cache 和 error taxonomy。
- T56 远程 skill market 会扩大供应链边界，必须在 eval 和 extension lifecycle 稳定后推进。
- T57 放在真实 search/skill 后，是因为上下文来源复杂度会显著上升，届时补 policy CLI、裁剪 reason 和长上下文 eval 才最有效。
- T58 依赖本机 Playwright 和多模态 provider，属于高收益但环境依赖更重的增强。
- T59/T60 面向长期运行和数据增长，适合在核心执行、评测和真实外部能力稳定后推进。
- T61 当前单用户路径可用，多用户绑定是进入真实多人 Telegram 使用前的必要治理补强。

增强任务统一约束：

- 不引入多 Agent，不引入前端 Console，不引入外部队列作为默认依赖。
- 所有新增工具和 provider 必须进入 Tool Contract、Action Policy、Secret Redaction、Error Taxonomy、Context Ledger、Source Evidence 和 Run Export。
- 所有新增外部 provider 必须保留 fake/local fallback，保证离线 eval 可运行。
- T55、T56、T58 涉及外部账号、远程市场或真实 provider，默认标记 HITL；没有用户确认前只实现本地 fixture 或 fake provider。
- 任何 cleanup actual、外部写入、自动启用 skill/generated tool 的行为都必须有 dry-run 或 validate 门。

新增保留风险：

- Replay 历史输出复用如果处理不当，可能掩盖真实工具行为变化；必须输出 replay strategy 和 contract drift。
- Eval 扩展如果断言过弱，会带来虚假信心；每个 case 必须至少断言一个用户可见结果和一个审计对象。
- 真实 search provider 会引入成本、限流、不可复现结果和来源可信度问题；必须走 result cache 和 evidence manifest。
- 远程 skill market 是供应链入口；默认 curated URL 需要用户确认，远程包必须 validate 后才能 enable。
- Context Policy 深化可能影响所有 LLM 调用；必须通过 context regression eval 和 preview 可视化验证。
- Browser/Vision 真实 provider 会引入环境差异；必须在 doctor 和 eval 中区分 real/fallback。

## 27. T54 Eval Suite 覆盖扩展实施前审计

目标：把 Eval Harness 从 `smoke/context-smoke` 扩展为可以持续回归工具、skills 和失败定位的本地反馈回路。

实施约束：

- `tools-smoke` 只使用本地、fake 或 dry-run 输入：Shell、Python Exec、Browser、DB、IM、Vision、Tool Generator 均通过真实 `ToolRegistry` 执行，但不依赖真实外部 API key。
- Browser case 只访问本机临时 HTTP fixture；IM case 必须使用 Telegram `dry_run=true`。
- `skills-smoke` 必须通过真实 built-in skill workflow 和 `WorkflowRunner` 执行，不直接伪造 skill 结果。
- 失败 eval 必须创建或关联真实 run，并导出本地 run export，便于定位 tool call、artifact、policy decision 和 contract snapshot。
- Eval report 继续保留 machine-readable JSON，同时新增简洁 `human_summary`，供 CLI 和自动化读取。

成功标准：

- `agentend eval list` 输出 `tools-smoke` 和 `skills-smoke`。
- `agentend eval run tools-smoke` 覆盖 Shell、Python Exec、Browser、DB、IM、Vision、Tool Generator，每个 case 至少包含一个用户可见断言和一个审计对象断言。
- `agentend eval run skills-smoke` 覆盖默认 built-in skills，每个 case 关联 workflow run。
- 失败 case 在 `eval report <eval_run_id>` 中输出 `export_path`，且该路径下包含 `run.json` 和 `tool_contracts.json`。

风险与控制：

- Eval 断言过弱会造成虚假绿测；本轮每个 case 同时断言输出和 run/tool/workflow 审计对象。
- 外部写入 eval 可能产生真实副作用；本轮只允许 IM dry-run，不触发真实 Telegram API。
- Browser 环境可能缺 Playwright；本轮通过本机 HTTP fixture 和 httpx fallback 保证离线可运行。

实施结果：

- `list_eval_suites` 新增 `tools-smoke` 和 `skills-smoke`。
- `tools-smoke` 通过真实 `ToolRegistry` 执行 Shell、Python Exec、Browser、本地 DB、Telegram dry-run、Vision fake provider 和 Tool Generator；每个 case 输出 run_id、tool_call_id、policy_decision_id、assertions 和 result preview。
- `skills-smoke` 通过真实 `WorkflowRunner` 执行默认 built-in skills；支持 `--skill` 过滤 installed skill，支持 `--skill-path` 执行本地 skill draft/bundle。
- Episode-to-Skill 的 `evals/smoke.json` 已从 `manual_review_required` 升级为可运行的 `skills-smoke` eval 输入。
- 失败 case 会导出到 `data/eval_exports/<suite>/<case>/<run_id>/`，包含 `run.json` 和 `tool_contracts.json`，并在 eval report case 中返回 `export_path`。

验证记录：

- `python -m pytest tests\test_phase_k_eval_suite_expansion.py -q`：4 passed。
- `python -m pytest tests\test_phase_i_eval_contract_snapshot.py tests\test_phase_d_skills_lifecycle_market.py tests\test_phase_e_planning_episode.py tests\test_phase_c_local_action_tools.py -q`：15 passed。
- `python -m pytest -q`：72 passed。
- `git diff --check`：通过；仅有 Windows 行尾转换 warning，无 whitespace error。

保留风险：

- `tools-smoke` 目前仍是每个高影响工具一个代表性 case，不覆盖全部参数组合、异常分支和真实外部 provider。
- `skills-smoke` 默认验证 built-in skill workflow 可运行和输出非空，尚未对每个 skill 的领域语义做强断言。
- Browser 使用本机 HTTP fixture，能保证 fallback 可用，但不能替代真实浏览器交互能力回归；T58 仍需深化。
- Scheduler/Inbox 生产化会放大错误；连续失败自动暂停和去重必须先于更复杂并发执行。
- Storage actual cleanup 有数据删除风险；必须保留 dry-run、confirm、rule 和 restore 验证。
- Telegram 多用户绑定错误会造成跨用户信息泄露；必须以 chat/user 精确匹配为硬边界。

本轮文档验证：

```bash
git diff --check
```

## 29. T55 真实 Search Provider + Evidence Export 实施前审计

目标：把 `web.search` 从 fake-only 扩展为可配置真实 provider，并把 search/fetch 来源证据完整带入 run export。

外部资料核对：

- Brave Search API Web Search endpoint 使用 `https://api.search.brave.com/res/v1/web/search`，认证 header 为 `X-Subscription-Token`，查询字段为 `q`，结果数量字段为 `count`，官方文档说明 `count` 最大为 20。

实施范围：

- 增加 search provider 配置读取，默认仍为 `fake`，首个真实 adapter 为 `brave`。
- `web.search` 支持从 config 或 tool input 读取 provider/base_url/api_key_env；缺 secret 时返回结构化 missing_config，不泄露 secret value。
- `web.search` 真实 provider 返回规范化 `{title,url,snippet,source_id}` 结果，并写入 `source_records`/`evidence_links`。
- `web.fetch` 继续写入 source evidence，并补齐 evidence link。
- `runs export` 输出 `evidence_manifest.json`，`run.json` 同步包含 evidence manifest，字段包括 source id、type、url/path、title、content_hash、fetched_at、query 和 used_by_run_id/tool_call_id。
- cache hit 不能复用旧 run 的 source id；网络读工具命中 cache 时必须为当前 run 重建 source evidence。

实施约束：

- 不把 API key 写入 DB、tool input/output、cache 或 export。
- 测试使用本机 HTTP fixture 模拟 Brave 结构，不依赖真实 Brave 账号或真实网络调用。
- Provider adapter 只做请求和响应归一化，不直接写 context、不写 memory。
- 失败分类复用 Error Taxonomy，Replanner 继续通过 `missing_config/network_error` 识别恢复路径。

红测计划：

- Brave-compatible 本机 fixture：`web.search` provider=brave 返回结果，写入 source evidence 和 result cache，`sources list/show` 可查看来源。
- 缺 secret：provider=brave 且 env 不存在时 `tools test` 返回失败，DB 中记录 `missing_config`，输出/export 不泄露 secret。
- `runs export`：search/fetch run 导出 `evidence_manifest.json`，包含 query、content_hash、fetched_at、used_by_run_id 和 tool_call_id。

## 30. T55 真实 Search Provider + Evidence Export 审计回填

本轮完成：

- Config 新增 `[search]` 和 `search.providers.*`，默认 provider 为 `fake`，内置 `brave` provider 配置。
- `web.search` 支持 `fake` 和 `brave`，并允许 tool input 覆盖 provider/base_url/api_key_env。
- Brave adapter 使用 `X-Subscription-Token` header 和 `q/count` 参数，返回统一 `title/url/snippet/source_id` 结果。
- `web.fetch` 和 `web.search` 改为通过 `agentend.core.evidence` 写入 `source_records` 和 `evidence_links`。
- ToolRegistry 在 cache hit 时调用 evidence rehydrate，为当前 run 重建 source evidence，避免复用历史 source id。
- `runs export` 输出 `evidence_manifest.json`，并把同一份 manifest 内嵌到 `run.json.evidence_manifest`。
- `configured_secret_names` 纳入 search provider 的 `api_key_env`，secret redaction 覆盖 search provider secret。

安全审计：

- API key 只从环境变量读取，不进入 tool input、ToolResult、ResultCache、SourceRecord 或 export。
- 缺 secret 抛出 `Search provider secret is not set: <ENV>`，由 Error Taxonomy 分类为 `missing_config`。
- 测试使用本机 HTTP fixture 模拟 Brave response，不依赖真实 Brave 账号或真实外网调用。
- Evidence manifest 只证明 run 使用过某来源，不提升来源可信度，也不写长期 memory。

验证记录：

- `python -m pytest tests\test_phase_l_search_provider_evidence_export.py -q`：3 passed。
- `python -m pytest tests\test_phase_d_search_evidence_capabilities.py tests\test_phase_h_context_reliability.py tests\test_phase_c_workspace_artifacts_storage.py tests\test_phase_k_eval_suite_expansion.py -q`：13 passed。
- `python -m pytest tests\test_init_cli.py tests\test_llm_agent_cli.py tests\test_phase_i_eval_contract_snapshot.py -q`：9 passed。
- `python -m pytest -q`：75 passed。
- `git diff --check`：通过；仅有 Windows 行尾转换 warning，无 whitespace error。

保留风险：

- T55 只接入 Brave Web Search；SearXNG、Tavily、Exa、SerpAPI 等 provider 仍需后续按同一 adapter/evidence contract 扩展。
- Brave API 的成本、限流和账号状态由用户环境负责；本地测试只验证协议和治理链路。
- Search 结果只做 title/url/snippet 归一化，不做内容可信度评分或交叉验证；报告类 skill 仍需在后续增强中处理来源可信度和引用质量。

## 31. T56 Skill Market 远程市场和版本快照实施前审计

目标：让 Skill Market 从“刷新目录并登记 metadata”升级为“可缓存、可验证、可回滚文件内容”的供应链入口。

实施范围：

- `git` market 支持本地 git fixture path 和远程 git URL；远程 URL 只在用户显式 `skills markets add ... --git <url>` 后刷新，不默认联网拉取。
- `directory` 和 `git` market refresh 都写入 `skills/market-cache/<market>/source`，安装 skill 的 `source_location` 指向 cache 内路径。
- 每个 validated skill bundle 写入 `skills/market-cache/<market>/snapshots/<skill_id>/<version>-<hash>/`，`ExtensionVersion.source` 指向 snapshot。
- `ExtensionVersion.content_hash` 使用 skill bundle 文件内容 hash，而不是仅用 source/version 组合 hash。
- `extensions rollback skill:<skill_id> --version <version>` 从 snapshot 恢复 `skill.yaml/workflow.yaml` 和资源文件到当前 installed cache 路径，并同步 Skill metadata。
- 坏包 refresh 不阻断其他 valid skill；坏包写入 quarantine report，ExtensionRecord 状态为 `quarantined`，不进入 Capability Map。

实施约束：

- 不自动启用远程来源的未知 skill；只继承 manifest enabled 默认值，坏包必须禁用或不创建 Skill row。
- 不把远程 market 的文件直接当 trusted memory/context；只登记为 skill extension。
- rollback 只允许回到 `validated` ExtensionVersion，不允许回滚到 quarantined 版本。
- 本轮测试使用本地 fixture，不依赖真实远程网络。

红测计划：

- 本地 git fixture market refresh 后，Skill source_location 位于 market-cache，ExtensionVersion.source 指向 snapshot，content_hash 为目录内容 hash。
- 同一 skill 发布 v0.2.0 后，rollback 到 v0.1.0 能恢复 `skill.yaml/workflow.yaml` 文件内容和 Skill metadata。
- 坏 skill bundle refresh 后进入 quarantined，生成 error report，不创建 enabled capability，且不阻断同一 market 中其他 valid skill。

## 32. T56 Skill Market 远程市场和版本快照审计回填

本轮完成：

- `directory` 和 `git` market refresh 都写入 `skills/market-cache/<market>/source`。
- `git` market location 是本地 path 时复制本地 working tree；非本地 path 时使用 `git clone --depth 1` 拉取远程 URL。
- Valid skill bundle 写入 `snapshots/<skill_id>/<version>-<content_hash_prefix>/`，`ExtensionVersion.source` 指向 snapshot。
- `ExtensionVersion.content_hash` 改为 skill bundle 文件内容 hash。
- `extensions rollback skill:<skill_id> --version <version>` 从 validated snapshot 恢复 `skill.yaml/workflow.yaml` 和资源文件，并重新加载 Skill metadata。
- Invalid skill bundle 写入 `quarantine/<skill_id>/error.json`，ExtensionRecord 状态为 `quarantined`，不会阻断同 market 的其他 valid skill。

安全审计：

- 真实远程 git URL 不会默认启用，必须用户显式 `skills markets add ... --git <url>`。
- Bad bundle 不创建 enabled Skill；如果已有同名 Skill，会被禁用并移出 Capability Map。
- Rollback 只选择 `validated` ExtensionVersion，拒绝 quarantined version。
- Market cache/snapshot 是本地文件恢复点，不把远程 skill 内容提升为 memory/context。

验证记录：

- `python -m pytest tests\test_phase_m_skill_market_snapshots.py -q`：2 passed。
- `python -m pytest tests\test_phase_d_skills_lifecycle_market.py tests\test_phase_k_eval_suite_expansion.py tests\test_phase_l_search_provider_evidence_export.py -q`：10 passed。
- `python -m pytest -q`：77 passed。
- `git diff --check`：通过；仅有 Windows 行尾转换 warning，无 whitespace error。

保留风险：

- 远程 git clone 仅做浅克隆，尚未做签名校验、allowlist、SBOM 或恶意 workflow 静态扫描。
- Rollback 以 version 参数定位，若同 version 多个不同 content hash 并存，目前选择最新 validated 记录；后续可增加 `--content-hash` 精确回滚。
- Quarantine report 是本地 JSON，尚未接入更完整的供应链审计报告或用户确认工作流。

## 27. T53 Replay 真实回放增强实施前审计假设

本轮进入 T53。实施目标是让 replay 从“重新执行 workflow”升级为“先规划、再按计划复用或阻断”，但仍保持单机本地 SQLite 架构。

实施范围：

- `runs replay --dry-run` 输出 replay plan，不执行工具、不创建新 replay run。
- `runs replay <run_id>` 基于 replay plan 创建新的 `channel=replay` run。
- 已完成的 LLM/final/无外部可见副作用 tool step 默认复用历史 `RunStep.output_json` 和 `ToolCall.output_json`，不重新执行工具。
- `network_write`、`external_write` 默认 block，并在 report 中输出 skip reason。
- 比较源 run 的 `tool_contract_snapshots` 和当前 Tool Contract，发现 contract drift 时标记 diff 并阻断实际 replay。

实施约束：

- 不复用失败 tool call 作为成功输出。
- 不从半完成 step 中恢复 replay。
- 不绕过 Action Policy；需要重跑的路径后续仍必须走现有 ToolRegistry 和 run_mode。
- Dry-run 只读 DB 和 workflow/tool manifests，不写 run、step、tool_call。
- Actual replay 必须保留 source_run_id、replay_strategy、skip_reason、contract_diff 等 machine-readable report。
- 本轮不做外部写入 allow 参数，不做复杂 HITL 审批；遇到外部写入或 contract drift 先 block。

红测计划：

- 无副作用 workflow 的 replay dry-run 输出 `reuse_output`，actual replay 不新增重复 source tool side effect，replay run 中 tool call 标记为 reused。
- 修改源 run 的 tool contract snapshot 后，dry-run 标记 `contract_drift`，actual replay 阻断并说明原因。
- 外部写入工具 replay dry-run/actual 都标记 block，actual 返回非零。

## 28. T53 Replay 真实回放增强实施审计回填

本轮完成：

- `runs replay --dry-run`。
- replay plan：per-step `reuse_output/rerun/skip/block` 首版，其中本轮实际使用 `reuse_output/skip/block`。
- 历史 `RunStep.output_json` 和 `ToolCall.output_json` 复用。
- replay run 创建 `channel=replay` conversation，并复制 reused step/tool_call 审计记录。
- contract drift 检测。
- 外部可见写入 side effect 默认 block。

新增模块：

- `agentend.core.replay`。

行为审计：

- Dry-run 只构建 JSON replay plan，不创建 run、step 或 tool_call。
- Actual replay 创建新的 replay run，不重新执行已完成的历史 step/tool；复用的 tool_call 在 replay run 中标记为 `status=reused`。
- 若源 run 的 `tool_contract_snapshots` 与当前 Tool Contract 不一致，dry-run 标记 `contract_drift=true` 和 `contract_diff`，actual replay 阻断。
- `network_write` 和 `external_write` 在 replay plan 中标记 `block`，actual replay 返回失败 run 和错误说明。
- Replay report 写入 replay run 的 `result_json.replay_report`，包含 source_run_id、workflow_id、steps、strategy、skip_reason 和 output。

验证命令：

```bash
python -m pytest tests/test_phase_j_replay_enhancement.py -q
python -m pytest tests/test_phase_g_hitl_resume_replay.py tests/test_phase_i_eval_contract_snapshot.py tests/test_phase_j_replay_enhancement.py -q
python -m pytest tests/test_phase_a_foundation.py tests/test_phase_c_workspace_artifacts_storage.py tests/test_phase_g_hitl_resume_replay.py tests/test_phase_i_eval_contract_snapshot.py tests/test_phase_j_replay_enhancement.py -q
python -m pytest -q
git diff --check
```

验证结果：

- `tests/test_phase_j_replay_enhancement.py`：3 passed。
- replay/export/resume 组合回归：11 passed。
- 受影响范围回归：18 passed。
- 全量回归：68 passed。
- `git diff --check`：通过；仅有 Windows 行尾转换 warning，无 whitespace error。

保留风险：

- 本轮 replay 不执行 `rerun` 分支，只对可安全复用的历史输出进行复用；未来如允许重跑只读工具，仍必须走 Action Policy 和 Result Cache。
- Replay 复制历史输出用于调试和审计，不证明外部世界状态仍一致。

## 33. T57 Context Policy + Budget 深化实施前审计假设

本轮进入 T57。实施目标是让长任务上下文从“能预览/记录”升级为“可配置、可解释、可评估、受预算约束”，仍保持单机 SQLite 与单 Agent 架构。

实施范围：

- 新增 `context policy` CLI，管理 `global/project/skill` scope 的 policy row。
- `resolve_context_policy` 合并 project/skill policy，仍保持收紧型字段不能被 workflow 或 skill 放宽。
- Context Runtime 记录 selected items 和 dropped items，dropped item 必须带 reason。
- Memory context 守门：低置信、过期和不可信 source 不进入 selected context。
- Workflow LLM step 执行 `cost_budgets` 的 `max_llm_calls/max_input_tokens/max_output_tokens`。
- 新增 `context-long` eval suite，覆盖长输入、多 workflow、真实 search provider fixture、skill policy merge、dropped reason 和 memory 守门。

实施约束：

- 不引入向量数据库或外部队列；继续使用本地 SQLite。
- 不允许 workflow/skill 放宽 global redaction。
- 不把 untrusted memory 当强约束注入 prompt；只保留 dropped reason 供审计。
- 真实 search provider eval 使用本地 HTTP fixture 和环境变量，不依赖公网或真实 API key。
- Budget 失败必须进入 Error Taxonomy，不以普通 unknown error 结束。

红测计划：

- `context policy set/show` 写入 project/skill policy，preview/ledger 能看到合并结果。
- `context_dropped_items` 对 max_items、低置信、过期和不可信 source memory 记录 reason。
- skill policy 不能放宽 global redaction。
- `budget set --max-llm-calls/--max-input-tokens/--max-output-tokens` 能阻断超限 workflow，并分类为 `budget_exceeded`。
- `eval run context-long` 输出通过的 long-input、multi-workflow、real-search-provider、skill-policy-merge 和 memory-guard cases。

## 34. T57 Context Policy + Budget 深化实施中审计记录

本轮已实现：

- 新增 `ContextDroppedItem` / `context_dropped_items`，用于保存每个 dropped context item 的 reason。
- `context policy set/show` CLI 支持 `global/project/skill` scope，并支持 JSON policy 与常用显式参数。
- `resolve_context_policy` 支持按 default、project workflow target 和 skill target 合并 policy，收紧型字段不可被下游放宽。
- Context Runtime 改为构造 `ContextPack(selected, dropped)`，preview 和 ledger show 均展示 dropped context items。
- Memory context 守门新增 `memory_low_confidence`、`memory_expired`、`memory_untrusted_source` 和 `memory_scope_not_allowed` dropped reason。
- Workflow Runner 在 LLM step 记录 ledger 后执行 `max_llm_calls/max_input_tokens`，在 LLM 输出后执行 `max_output_tokens`，超限分类为 `budget_exceeded`。
- 新增 `context-long` eval suite，覆盖长输入、多 workflow ledger、Brave-compatible 本地 search provider fixture、skill policy merge 和 memory guard dropped reason。

验证结果：

- 新增测试首次红测：`ContextDroppedItem` 缺失导致收集失败，符合预期。
- 实施后新增测试已有 2 个通过；`skill policy` 失败已修复为直接把当前执行 workflow 传入 context runtime。
- 补跑 `tests/test_phase_n_context_policy_budget.py`：4 passed。
- 补跑 context/search/skill-market/eval 受影响回归：13 passed。
- 补跑全量 `tests/` 回归：81 passed。
- `git diff --check`：通过；仅有 Windows 行尾转换 warning，无 whitespace error。

保留风险：

- `context-long` 已跑通本地 fixture 路径；真实外部 search provider 仍依赖用户提供 API key 和网络环境。
- 本轮 budget enforcement 覆盖 LLM step 的调用数、输入 token 和输出 token 守门；后续若新增非 LLM 成本项，需要继续接入同一 cost budget 审计链路。

## 35. T58 Browser + Vision 真实能力增强实施审计回填

本轮进入 T58。用户明确后续运行环境以 Linux 为准，允许本机安装 Playwright/Chromium 做真实路径验证；Vision 真实 provider 按 OpenAI-compatible 和 Gemini 两条路径实现，默认仍保持 fake provider 作为离线 eval fallback。

本轮完成：

- `playwright` 纳入运行依赖，但 Chromium browser 仍通过独立安装步骤管理，避免把浏览器二进制隐式混入 Python 包安装。
- `agentend doctor` 新增 `browser_playwright` 检查；缺包、缺浏览器或当前环境无法启动时输出 warning，并给出 Linux 部署命令 `python -m playwright install --with-deps chromium`。
- Browser open/extract/screenshot/click/type 输出补齐 `fallback`、`fallback_reason` 和 `dom_excerpt`；click/type 也会写入 screenshot artifact。
- Vision config 新增 `[vision]`，默认 provider 为 `fake`；内置 `openai` 和 `gemini` provider 配置。
- `vision.describe/ocr/extract_chart` 支持 tool input 覆盖 `provider/base_url/model/api_key_env`，便于本地 fixture、私有 OpenAI-compatible 网关和真实 provider 共用同一 adapter。
- OpenAI-compatible 路径使用 Chat Completions data URL 图片输入；Gemini 路径使用 `generateContent` inline image data。
- Vision 真实 provider 缺 secret 时返回结构化 `missing_config`，只记录 secret env 名称，不输出 secret value。
- Vision tool contract 从 `local_read` 提升为 `network_read`，反映真实 provider 可能外呼的治理边界。

验证结果：

- `tests/test_phase_f_browser_agent.py tests/test_phase_f_vision_analyzer.py`：7 passed、1 skipped。skip 为当前非外部权限环境下 Playwright driver 无法启动，fallback 路径已覆盖。
- 外部权限下补跑真实 Playwright screenshot 测试：1 passed。

安全审计：

- Browser fallback 不再伪装成真实浏览器，输出明确 `backend=httpx_fallback` 和 fallback reason。
- Playwright/Chromium 的 Linux 安装作为 doctor fix hint 暴露，不在代码中硬编码 Windows 路径或本机缓存路径。
- Vision API key 只从环境变量读取；tool input/output、ToolResult、DB artifact metadata 和测试输出不包含 secret value。
- OpenAI-compatible/Gemini 测试均使用本地 HTTP fixture，不依赖真实外部 API key 或公网调用。

保留风险：

- 本轮未接入模型成本统计到 Vision provider 调用，后续如要精确计费，应把 provider usage 字段接入现有 cost usage 链路。
- Gemini/OpenAI-compatible provider 当前只做同步图片理解请求；大文件上传、视频、流式输出和高级结构化输出留作后续增强。

## 36. T59 Scheduler + Inbox 长期运行可靠性实施审计回填

本轮进入 T59，目标是让单机长期任务入口在没有外部队列的前提下具备基本生产化可靠性：可校验调度表达式、失败自动隔离、inbox 限流和内容去重，并保留完整 source/run_mode 关联。

本轮完成：

- 新增 `agentend schedule validate --cron ...`，支持五段 cron 的 `*`、`*/n`、数字、逗号列表、范围和范围步进校验；非法表达式在添加或 tick 时明确阻断。
- `schedules` 增加 `consecutive_failures`、`max_consecutive_failures`、`paused_reason` 和 `last_error`；调度任务连续失败达到阈值后自动 `paused`。
- scheduler 创建的 task 固定记录 `source=scheduler`、`schedule_id` 和 `run_mode=scheduler`，并继续走 Action Policy，因此默认阻断 `external_write`。
- `tasks` 增加 `source_hash`、`batch_id`、`run_mode` 和 `retry_after_at`；SQLite 初始化补齐增量列，兼容已有本地库。
- `inbox watch --once --limit ...` 按批次上限创建 task，并以文件 SHA-256 和源路径双重去重；watch 常驻模式新增错误 backoff。

验证结果：

- `tests/test_phase_f_inbox_tasks_tool_generator.py tests/test_phase_o_scheduler_inbox_reliability.py`：10 passed。
- 全量 `tests/` 回归：90 passed、1 skipped。
- `git diff --check`：通过；仅有 Windows 行尾转换 warning，无 whitespace error。

安全审计：

- 没有引入外部队列、后台 daemon 或真实外部写入；scheduler 仍由显式 CLI tick/run-now 驱动。
- 自动暂停只修改 schedule 状态，不删除任务、不移动 inbox 文件、不清理历史运行记录。
- Inbox 去重基于本地文件 hash 和 source_path，不读取或输出文件内容到最终消息。

保留风险：

- 当前 cron 仍是轻量五段实现，不支持命名月份/星期、时区策略、秒级调度或复杂日历语义。
- 常驻 `inbox watch` 的 backoff 是单进程内存状态；进程重启后不会恢复错误窗口。若未来引入长期 daemon，可把 backoff 状态持久化到 DB。
- Inbox 只创建 task，不处理文件归档或隔离目录；后续如要移动已处理文件，需要先设计可审计且非破坏性的归档策略。

## 37. T60 Storage Retention 实际清理策略实施审计回填

本轮进入 T60，目标是把 storage cleanup 从记录型 dry-run 推进到可控 actual cleanup，同时继续避免误删用户数据。实现仍保持单机 SQLite 和本地文件系统路径，不引入外部队列或后台服务。

本轮完成：

- 新增 `agentend.core.storage`，统一构建 cleanup plan、执行计划、记录清理运行和计算 storage usage。
- `storage cleanup --dry-run` 会生成 `plan_id`，列出候选路径/DB row、大小、原因和 rule id，不删除文件或 DB 行。
- `storage cleanup --confirm --plan-id <plan>` 只执行对应 dry-run plan；`storage cleanup --confirm` 则视为显式确认当前计划。未提供 `--dry-run` 或 `--confirm` 时直接拒绝。
- 清理范围限定在 AgentEnd home 下的受管目录：`data/artifacts`、`data/sandboxes`、`data/eval_exports`、`data/exports`、`data/cache`、`data/skill_drafts` 和 `skills/market-cache`。
- 支持旧 checkpoint DB 行清理，但每个 run 默认保留最新 checkpoint；manual memory 不参与删除。
- enabled skill 的 `source_location/workflow_path`、enabled extension source、episode artifact 路径都会作为 protected path，actual 执行时也会二次检查。
- `storage_cleanup_runs` 增加 `plan_id/source_plan_id/status/rules_json/total_bytes/deleted_count/error`；新增 `storage_retention_rules` 表保存默认 retention rule 记录。
- `storage restore` 改为只恢复到没有既有 AgentEnd DB 的 home，拒绝覆盖当前 home 或已初始化 home。

验证结果：

- `tests/test_phase_c_workspace_artifacts_storage.py tests/test_phase_p_storage_retention.py`：7 passed。
- storage/skills/checkpoint/replay/scheduler 受影响回归：22 passed。
- 全量 `tests/` 回归：94 passed、1 skipped。
- `git diff --check`：通过；仅有 Windows 行尾转换 warning，无 whitespace error。

安全审计：

- actual cleanup 必须显式 `--confirm`，否则不会删除任何文件或 DB 行。
- 所有文件/目录删除前都会解析绝对路径，并要求目标位于 AgentEnd home 的受管 cleanup roots 内，且不能删除 root 本身。
- dry-run plan 与 actual result 都记录删除项路径、大小、原因和 rule id；actual 中如果路径后来变成 protected，会跳过并记录 `protected`。
- restore 默认不覆盖已有 DB，符合“临时 home 验证”路径。

保留风险：

- 当前 retention rule 仍是默认规则记录，尚未提供 `storage retention set/list` CLI 给用户长期管理规则。
- Cleanup 删除 artifact 文件后保留历史 run/artifact 审计行；这保留了审计线索，但 artifact show 可能遇到文件已被清理的路径。
- 本轮未清理 result cache 等纯 DB 历史表；后续如要做 DB 级 retention，需要逐表定义保留策略和可恢复边界。

## 38. T61 Telegram 多用户绑定增强实施审计回填

本轮进入 T61，目标是修正 Telegram pending request 的“最近 run”策略，避免多用户或多 chat 并发时把回答串到别人的等待请求上。

本轮完成：

- `WorkflowRunner.run` 新增 `external_user_id` 参数，默认保持 `workflow`；Telegram `/run` 传入 `chat_id:user_id`，使 workflow conversation 与普通 Telegram conversation 使用同一绑定口径。
- `WorkflowRunner.resume` 新增可选 `expected_channel` 和 `expected_external_user_id` 校验；Telegram resume 会验证 run 属于当前 chat/user。
- `TelegramMessageRouter` 的 pending lookup 改为按 `Conversation.channel == "telegram"`、`Conversation.external_user_id == chat:user`、`Run.status == waiting_input`、`ClarificationRequest.status == pending` 精确匹配。
- `/status` 只查看当前 chat/user 的最近 run；`/cancel` 只取消当前 chat/user 的 pending run，并同步把对应 clarification 标记为 `cancelled`。
- `clarifications list/show` 输出 channel 和 external_user_id，便于运维确认 pending request 归属。
- Telegram 输出统一经过安全处理：secret redaction、AgentEnd home 路径隐藏、疑似原始工具 JSON 输出省略。

验证结果：

- `tests/test_telegram_entry.py tests/test_phase_g_hitl_resume_replay.py tests/test_phase_q_telegram_multi_user.py`：11 passed。
- Telegram/HITL/eval/IM/CLI 受影响回归：19 passed。
- 全量 `tests/` 回归：98 passed、1 skipped。
- `git diff --check`：通过；仅有 Windows 行尾转换 warning，无 whitespace error。

安全审计：

- 非对应 chat/user 的普通消息不会命中别人的 pending clarification，会走自己的普通 conversation 路径。
- 即使持有 run id，Telegram resume 路径也会校验 channel 和 external_user_id，避免跨用户恢复。
- Telegram `/agent` 不再返回本机 agent profile 路径，只返回 profile hash。
- Telegram 输出中的环境 secret value 和 AgentEnd home 绝对路径会被替换；工具 JSON 输出不直接发到 Telegram。

保留风险：

- 当前绑定键为 `chat_id:user_id`，适合私聊和群内用户区分；如果后续需要群级共享会话，需要显式设计 chat-level scope，而不是复用当前 user-level 绑定。
- `cancelled` clarification 状态已用于 Telegram cancel，但尚未做专门的 clarification 状态枚举治理；后续若增加状态机，应把 `cancelled` 纳入文档化状态。
- Telegram 输出省略工具 JSON 是保守策略；如果未来需要移动端查看结构化结果，应设计脱敏摘要而不是直接放开 raw tool output。
