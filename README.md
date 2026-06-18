# AI Client Governance

## 快速索引

| 项目 | 说明 |
|---|---|
| 仓库定位 | 可嵌入到任意项目的 AI 编程客户端治理插件层；不替代客户端 agent runtime。 |
| 设计原则 | 客户端治理层、完整嵌入、薄入口、组件化门禁、项目边界和 live state 优先。 |
| 推荐嵌入位置 | 目标项目 `.ai-client/ai-client-governance/`；旧 `.codex` 治理布局不再支持。 |
| 通用规则事实源 | `.ai-client/ai-client-governance/AGENTS.md`，文件名只是入口适配，内容是 agent-neutral 治理契约。 |
| 项目规则入口 | 目标项目 `.ai-client/project/rules/project/AGENTS.md` |
| 结构化事实源 | `.ai-client/project/state/aicg.db`；Markdown 只作为报告导出和历史审计。 |
| 文件归属审计 | `file-ownership audit` 统计 `.ai-client` 追踪/忽略类别；安装器维护 `.gitignore` runtime block。 |
| 入口适配器 | `AGENTS.md`、`CLAUDE.md`、`GEMINI.md`、Copilot/Cursor/Cline/Windsurf/Continue/Roo/Aider 等工具原生规则入口。 |
| 机器清单 | `manifest.json` |
| 嵌入脚本 | `install-ai-client-governance.ps1`，一键嵌入、生成目标项目配置，默认只生成 `AGENTS.md` 薄入口；其它平台 adapter 需显式开启。 |
| 会话检查 | `check-ai-client-governance-sync.ps1` |
| 统一 CLI | `python .ai-client/ai-client-governance/scripts/ai_client_governance.py --list` |
| Python 包 | `.ai-client/ai-client-governance/src/ai_client_governance/` |
| 修改工作区 | 所有修改型任务先创建 `.ai-client/project/.worktree/<task-slug>/` 任务 worktree。 |
| Token 用量统计 | 嵌入后 `.ai-client/ai-client-governance/skills/codex-token-usage/`，这是 Codex 本地日志的可选 skill，不代表框架只服务 Codex。 |
| 推荐嵌入方式 | Git submodule |
| 默认同步策略 | 每次会话检查；最多 24 小时 fetch 一次；不自动 pull/push。 |
| 回写方式 | 进入 `.ai-client/ai-client-governance/` 后使用普通 Git 命令提交和推送。 |

这个仓库不是某个项目的 `.ai-client/rules/common/` 文件夹备份，也不是要替代
Codex、Claude Code、Cursor、Cline、Windsurf、Continue 或其它 AI 编程客户端的
底层 agent runtime。它的定位是：**建立在这些客户端已经具备对话上下文、工具调用、
文件写入、终端执行、人类批准和会话恢复能力之上的治理插件层**。

目标项目应把本仓库作为一个独立 Git 仓库嵌入进来，Git 项目默认用 submodule
记录精确规则版本，让通用规则、入口 adapter、通用 skills、门禁脚本、README 和
manifest 保持完整上下文。

`AGENTS.md` 仍然保留，因为 Codex、Devin、Cursor 等生态已经支持它，也因为
本仓库历史上用它作为通用规则事实源。但新的设计原则是：**文件名是适配器，
治理插件契约才是事实源**。Claude Code 可以通过 `CLAUDE.md` 进入，Gemini CLI
可以通过 `GEMINI.md` 进入，Copilot、Cursor、Cline、Windsurf、Continue、
Roo Code、Aider 等工具也可以通过各自原生入口指向同一套规则。

## 设计原则

`ai-client-governance` 的后续开发先受这些原则约束：

- **客户端治理层，不是 agent runtime**：治理宿主 AI 编程客户端已有能力怎么使用、
  记录和验收，不重造客户端，也不把某个模型或工具私有行为写成通用前提。
- **完整嵌入是事实模型**：通用规则、脚本、skills、README 和 manifest 作为独立
  Git 仓库嵌入 `.ai-client/ai-client-governance/`，不回到 common/scripts/skills 分散复制。
- **入口是 adapter，契约才是事实源**：不同客户端入口只负责导入或指向同一事实源，
  不能各写一套规则。
- **可确定约束优先组件化**：可重复、易遗漏、可检查或跨会话影响仓库状态的要求，
  优先进入 CLI、runtime component、gate、状态文件或 skill。
- **本地命令先压缩**：生成或运行新命令前先用本地 planner 判断去重、并行、gate-pool
  聚合、telemetry 包装或缓存复用，减少确定性步骤反复请求模型。
- **修改必有隔离与证据**：修改型任务默认有 worktree、task tracking、写锁、工具 telemetry、
  验证记录和最终状态。
- **项目特化不污染通用层**：业务、简历、学习路线、源码快照和本地交付规则留在宿主项目。
- **保留项目原生资产**：已有原生规则入口、skills、IDE 配置和团队约定默认保留，
  只补缺、索引和报告冲突。
- **同步检查只审计，不替人决策**：检查 dirty/ahead/behind/diverged，但不自动 pull、
  push、merge 或删除。
- **live state 优先于快照**：最终判断 worktree、Git、session、lock 和 queue 状态时，
  重新核对真实状态。
- **跨客户端可移植**：新增规则默认使用 agent-neutral 语义，工具私有细节只放在局部适配中。

## 当前优势

- **完整 Git 边界**：通用规则有自己的 commit、branch、remote 和 history；
  Git submodule 让父项目只记录一个 gitlink commit，项目内修改通用规则时，
  可以直接在 `.ai-client/ai-client-governance/` 用 Git 回写。
- **跨客户端治理插件层**：核心生命周期、审批、worktree、联网核对、task tracking、
  Git 边界和收口门禁不绑定 Codex、Claude、Gemini、Copilot 或任何单一 IDE。
  但它明确依赖宿主客户端提供 agent 执行能力；`ai-client-governance` 负责治理这些能力怎么被
  使用，而不是重新实现一个 agent runtime。
- **多入口 adapter 目录**：安装脚本默认只生成 `AGENTS.md` 薄入口。
  需要 Claude、Gemini、Copilot、Cursor、Cline、Windsurf、Continue、Roo 或 Aider
  等工具入口时，显式传 `-InstallAgentAdapters`；已存在的项目原生入口默认保留，
  不静默覆盖。
- **只支持完整嵌入模型**：目标项目把本仓库整体放在 `.ai-client/ai-client-governance/`，
  不再生成或维护 common 规则、scripts、skills 的分散复制副本。
- **三层资产边界清楚**：优先级固定为目标项目原生资产 >
  `.ai-client/project/` 项目特化层 > `.ai-client/ai-client-governance/` 通用层。本仓库只维护跨项目
  通用协作规则；项目业务、文档体系、简历、源码快照和本地交付规则留在目标项目。
- **每次会话都能发现不一致**：检查脚本每次运行都会检查 embedded repo 是否
  missing、dirty、ahead、behind 或 diverged；一旦不一致，会持续提示到同步完成。
- **24 小时只限制 fetch**：为了减少远端请求，默认最多 24 小时 fetch 一次；
  但本地 dirty/ahead/behind/diverged 状态每次会话都会检查并提示。
- **脚本能力和上下文可恢复**：脚本不支持当前目标时先走能力适配门禁，
  连续纠错或新要求叠加时把压缩快照写入结构化 task record，避免手工绕过和恢复遗漏。
- **用户要求可验收**：每条用户要求登记为 `REQ-*`，绑定处理状态、实现证据、
  验证证据和最终回复覆盖；`ai_client_governance.py task-gate` 会检查缺失项。
- **结构化记录优先**：新任务优先写入 SQLite 事实源
  `.ai-client/project/state/aicg.db`。`contract describe` 在执行前列出必填字段；
  `task-record apply` 在写入时强制 schema、枚举和外键；gate 直接查数据库，
  不再依赖从 Markdown 表格反解析。
- **强制输入过滤器**：用户消息是 `user-message` join point；非纯只读小问答必须先运行
  `lifecycle input-filter` 并写入 `input-filter.preflight` 事件，后续 preflight/final gate
  才能继续。
- **插件化治理模型**：`ai-client-governance` 是人和 AI 之间的客户端治理插件。输入过滤器拆用户要求，
  处理拦截器管审批/worktree/联网核对，输出拦截器管最终回复和 worktree 完成提示，
  横切门禁管编码、Git、文档、telemetry 和 trace flow。新增可确定约束时注册组件，
  不再只追加口头规则。
- **门禁池和执行链路**：`ai_client_governance.py gate-pool` 可以按同一 `trace_id` 编排多个
  门禁，`ai_client_governance.py telemetry` 负责统一记录/统计，`tool-invocations`
  作为命令适配器，`ai_client_governance.py tool-flow` 验证调用链路。
- **Agent Context Reuse**：多智能体协作不写死小数量上限；总控先按任务树、写范围、验证风险
  和上下文复用命中率决定继续复用、创建新 agent 或收束回主线程。每个 agent 都要有 reuse key、
  Agent Brief、最小读取清单、context capsule 和 token usage 来源或代理指标，避免重复灌入完整历史。
- **本地 token 用量统计**：`codex-token-usage` skill 读取本机 Codex session
  JSONL 的 `token_count` 事件，输出总量、净用量、缓存命中率、峰值日和最忙周。
- **适合 GitHub 展示和复用**：README、manifest、规则事实源、adapter 目录和脚本
  共同说明如何嵌入、如何维护边界、如何同步和如何把改动回写给上游。

每次准备把本仓库上传或推送到 GitHub 前，都要回看本 README 的“当前优势”和
`manifest.json`，确认它们真实反映当前仓库能力。新增门禁、skill、脚本、
入口 adapter 或嵌入策略后，README 需要同步更新，方便别人一眼看懂这个仓库的价值。

## 核心定位：客户端治理插件层

`ai-client-governance` 的设计前提是：现代 AI 编程客户端已经拥有 agent 执行环境。它们能读取
上下文、调用工具、改文件、跑命令、请求用户批准、保持会话，并在一定程度上恢复任务。
因此本仓库不再把自己设计成 LangGraph 式的底层流程引擎，也不把 LangGraph 当成当前
Codex/Claude/Cursor 会话的强制执行层。

`ai-client-governance` 补的是宿主客户端通常不会天然做好的治理能力：

- 用户输入拆解、任务类型判断、验收标准提取和最终回复覆盖。
- 修改型任务的审批、task tracking、worktree 隔离和 Git 边界。
- 修改后的文档影响面、引用反查、完成测试计划和输出状态审计。
- 多会话、多 worktree、coord/session/lock 与 Git live state 的对账。
- 节点是否触发、是否去重、是否跳过、是否太慢的可观测证据。

用 Spring 类比，宿主 AI 客户端更像应用运行环境，`ai-client-governance` 更像一组可嵌入的
Starter、Interceptor、Validator 和 Actuator。`manifest.json` 和
`runtime components` 注册表描述可扩展组件；`task-gate`、`gate-pool`、
`worktree-task`、`doc-index` 等脚本提供可审计的强制门禁；入口 adapter 把
Codex、Claude、Cursor、Cline、Windsurf、Continue 等客户端带回同一套事实源。

用户消息按 AOP/filter-chain 处理：`runtime components --event user-message --kind input-filter`
必须能看到 `input.filter.user-message-preflight`，它要求先由 `lifecycle input-filter`
拆出 REQ、记录联网/验证/验收判定，并写入 `events.event_type=input-filter.preflight`。
之后 `task-record gate --event preflight` 会 fail closed；缺 trigger 或事件行时，不能继续
写入、恢复或最终回复。

同一条用户消息里，用户目标和用户陈述会分开记录。`input.filter.user-claim-validation`
把“用户要求我做什么”和“用户声称当前事实是什么”拆开，写入
`events.event_type=user-claim-validation.analysis`。每条 claim 都要有 `trust_level`、
`risk_flags` 和 `verification_action`；凡会影响写入、Git、脚本 telemetry 或规则升级的陈述，
先核对 live state、规则或外部资料，再决定执行、澄清或阻塞。

计划、诊断、写入、commit 和 push 也是独立边界。`plan-approval-boundary.analysis`
记录本次批准范围、`execution_policy` 和 `push_policy`；push policy 固定为
`push_requires_separate_approval`。因此 closeout 可以在本地 commit，但不会因为用户批准
当前计划就自动 push。

命令执行也按同一 AOP 思路处理：中/大型或修改型任务在生成或运行新命令前，必须能看到
`preflight.interceptor.command-compression`，并把 `events.event_type=command-compression.analysis`
写入结构化 task record。推荐入口是 `task-run plan`：它把候选本地命令去重、分组为
只读并行批次、验证批次和必须顺序执行的状态变更步骤，再输出可直接入库的事件 payload；
payload 必须包含非空 `groups`，否则 `task-record gate` 不会放行。
registry 同时有 mutating fast path：即使任务被归类为 small，只要属于修改型任务，
`runtime components` 也必须显示 `command-compression` 和 `task-run-dag` 节点。
随后可用 `task-run run` 执行压缩后的本地 DAG：只读/验证批次可并行，显式 `--cache`
时只缓存只读/验证节点；状态变更、Git 写入、锁和未知副作用命令保持顺序且不缓存。
每个执行节点默认写入 `.ai-client/project/state/aicg.db` 的 `execution_spans` 和
`execution_events`，并可用 `--trace-json` 输出完整 trace。命令只是
`span_kind=command`、`subject_type=command` 的一种执行载荷；未来模型 HTTP、子 AI、
token usage 和外部 API 调用也扩展同一 span/event schema。`telemetry record` 是非命令
执行的通用记录入口；`telemetry report` 汇总 top operations、
top subjects、span kind、subject type、重复执行、失败率、duration p50/p95/max、cache hit/miss、
scope 分布和 adapter enforcement 分布；`task-run diagnose` 读取同一 DB 和 worktree-coord 状态，报告重复终态命令、
失败、cache hit/miss、活动锁、task-record/task-queue 口径差和裸 shell 自动拦截缺口。
`task-run`、`gate-pool` 或 `tool-invocations` telemetry 只证明命令被 wrapper 补账，
不会清空 raw shell gap；需要强制覆盖时运行
`task-run diagnose --require-raw-shell-coverage` 或
`shell-adapter diagnose --require-auto-intercept`。
这不是替代宿主客户端，而是在命令进入宿主 shell 前提供强制过滤器、执行器和诊断层，
减少多轮模型 HTTP 往返。
诊断默认可看全局 telemetry，也可用 `--task-id`、`--trace-id`、`--since`、`--until`
收敛到当前任务，避免历史失败或重复命令淹没本轮信号。
隔离测试和 selftest 可以用 `AICG_STATE_DB` 或显式 `--db` 指向 run directory 内的临时
SQLite；JSONL 只通过 `--jsonl-artifact-dir` 作为显式 artifact，不作为默认事实源。

这个设计按用户要求先联网核对官方资料后落地：AOP/filter-chain 语义参考 Spring 对
join point、pointcut、advice 的定义；缓存边界参考 Bazel remote cache 和 Gradle build
cache 对可复用输出的要求；运行观测参考 OpenTelemetry trace/span 模型。落到本项目后，
缓存键包含 runner 版本、命令、cwd、节点类型、任务类型、changed paths、声明输入哈希和
Git HEAD；不缓存 live worktree 探测和状态变更命令。

如果未来自建 agent runner，LangGraph 可以作为底层 workflow/checkpoint/human-in-the-loop
执行器来承载这些节点；但在现有客户端里，LangGraph 不能直接拦截客户端内部行为。
当前架构的优先级是把 `ai-client-governance` 做成跨客户端可移植的治理插件，而不是重造客户端。

## 结构化事实源

新的任务记录默认使用 SQLite，而不是把机器事实写进 Markdown 后再反解析。

```powershell
python .ai-client/ai-client-governance/scripts/ai_client_governance.py contract describe `
  --task-type rules-script --task-type docs --event write-intent

python .ai-client/ai-client-governance/scripts/ai_client_governance.py lifecycle input-filter `
  --message "<user-message>" `
  --task-id <task-id> `
  --task-record-json .ai-client/project/tmp/task-records/<task-id>.json

python .ai-client/ai-client-governance/scripts/ai_client_governance.py task-record init
python .ai-client/ai-client-governance/scripts/ai_client_governance.py task-record apply --json task-record.json
python .ai-client/ai-client-governance/scripts/ai_client_governance.py task-record gate `
  --task-id <task-id> --event preflight
python .ai-client/ai-client-governance/scripts/ai_client_governance.py task-gate --task-id <task-id>
```

`task-record apply` 会在写入前检查 `tasks`、`requirements`、`triggers`、
`outputs`、`events`、`worktrees`、`validations` 等表的必填字段、枚举和外键。缺少必填
字段时直接拒绝写入，避免 AI 到最终 gate 才发现机器事实缺失。
`task-record gate --event preflight` 还会检查每条 requirement 的记录/联网/验证/验收判定、
`trigger_type=user-message` 或 `input-filter` 的触发行，以及 `input-filter.preflight`
事件行；这些事实缺失时直接失败。对中/大型、修改型或规则/脚本任务，还会检查
`command-compression.analysis`、`plan-approval-boundary.analysis`、
`user-claim-validation.analysis`、`state-artifact-ownership.analysis` 和
`patch-preflight.analysis` 等事件，避免只靠模型记忆补规则。
输入分析是 turn-start 事实，可以在 worktree 创建前先落库；worktree 证据在写入和最终
收口边界强制检查，不能反过来阻塞输入记录。

Markdown 只能通过 `task-record export-md` 作为人类阅读报告生成；DB 是新任务 gate
的事实源。历史 `.md` task tracking 保留审计意义，不再作为当前任务的机器门禁输入。
`task-record status` 是只读自省命令：目标 DB 不存在时返回空摘要，不创建
`.ai-client/project/state/aicg.db`。

## 多工具入口适配器

`ai-client-governance` 把主流 AI 编码工具的规则文件分成三类：

- **核心事实源**：`.ai-client/ai-client-governance/AGENTS.md` 和
  `.ai-client/project/rules/project/AGENTS.md`。它们保存真正的流程、边界和项目特化规则。
- **项目原生入口**：工具会自动读取或用户已经维护的规则文件，例如根 `AGENTS.md`、
  `CLAUDE.md`、`GEMINI.md`、`.github/copilot-instructions.md` 或
  `.cursor/rules/*.mdc`。
- **adapter 薄入口**：安装脚本生成的短文件，只写读取顺序、同步检查、
  编码和边界；不复制大段通用规则。默认仅生成 `AGENTS.md`，其它工具入口
  通过 `-InstallAgentAdapters` 按需生成。

当前内置 adapter 目录：

| 工具/生态 | 入口 adapter | 处理策略 |
|---|---|---|
| Codex / AGENTS 生态 / Devin | `AGENTS.md` | 默认生成薄入口；已存在时保留，除非显式 `-ForceRootEntry`。 |
| Claude Code | `CLAUDE.md` | 仅在 `-InstallAgentAdapters` 或 `-ForceAgentAdapters` 时生成；如果工具支持导入，应优先导入 `AGENTS.md` 或通用事实源。 |
| Gemini CLI | `GEMINI.md` | 仅在 `-InstallAgentAdapters` 或 `-ForceAgentAdapters` 时生成；可通过 `@` 导入方式复用通用事实源。 |
| GitHub Copilot | `.github/copilot-instructions.md`、`.github/instructions/ai-client-governance.instructions.md` | 仅在 `-InstallAgentAdapters` 或 `-ForceAgentAdapters` 时生成，避免默认写入团队已有 Copilot 指令目录。 |
| Cursor | `.cursor/rules/ai-client-governance.mdc` | 仅在 `-InstallAgentAdapters` 或 `-ForceAgentAdapters` 时生成；使用 `alwaysApply: true` 的项目规则 adapter，已存在则保留。 |
| Cline | `.clinerules/ai-client-governance.md` | 仅在 `-InstallAgentAdapters` 或 `-ForceAgentAdapters` 时生成；Workspace rules adapter，已存在则保留。 |
| Windsurf | `.windsurf/rules/ai-client-governance.md` | 仅在 `-InstallAgentAdapters` 或 `-ForceAgentAdapters` 时生成；Project rules adapter，已存在则保留。 |
| Continue | `.continue/rules/ai-client-governance.md` | 仅在 `-InstallAgentAdapters` 或 `-ForceAgentAdapters` 时生成；Project-specific rules adapter，已存在则保留。 |
| Roo Code | `.roo/rules/ai-client-governance.md` | 仅在 `-InstallAgentAdapters` 或 `-ForceAgentAdapters` 时生成；Project rules adapter，已存在则保留。 |
| Aider | `CONVENTIONS.md` | 仅在 `-InstallAgentAdapters` 或 `-ForceAgentAdapters` 时生成；已存在时保留，因为它常承载项目真实约定。 |

这个清单是入口适配清单，不是优先级反转。目标项目已有的原生规则文件最高优先级；
`.ai-client/project/` 是项目特化层；`.ai-client/ai-client-governance/` 是通用层。adapter 只负责把不同
AI 工具带回同一套事实源，不能静默覆盖已有规则，也不能把项目业务规则写回通用仓库。

外部依据优先来自官方文档：OpenAI Codex `AGENTS.md`、Anthropic Claude Code
`CLAUDE.md`、Google Gemini CLI `GEMINI.md`、GitHub Copilot custom instructions、
Cursor rules、Cline rules、Continue rules、Devin rules 和 Aider conventions。
官方没有稳定声明或不同版本行为不一致时，README 与 manifest 只记录“adapter 候选”，
不把它当成强制自动加载事实。

## 给其它 AI 的使用约定

当用户在另一个项目里告诉你本仓库位置时，按这个顺序处理：

1. 读取本仓库 `README.md` 和 `manifest.json`，确认当前 schema、嵌入位置和同步策略。
2. 在目标项目中嵌入完整仓库，推荐路径为 `.ai-client/ai-client-governance/`；
   目标项目是 Git 仓库时默认使用 Git submodule。
3. 默认只生成或检查 `AGENTS.md` 入口；如果当前项目确实需要 Claude、Gemini、
   Copilot、Cursor、Cline 等原生规则入口，安装时显式传 `-InstallAgentAdapters`。
4. 确认所有 adapter 都指向 `.ai-client/ai-client-governance/AGENTS.md` 和
   `.ai-client/project/rules/project/AGENTS.md`，且项目特有规则不写回本仓库。
5. 每次新会话先运行 `.ai-client/ai-client-governance/check-ai-client-governance-sync.ps1` 或目标项目根部
   等价 wrapper；若提示不一致，必须提醒用户，直到 Git 同步完成。

## 嵌入方式

### 推荐：Git submodule

目标项目已经是 Git 仓库，且希望父仓库记录 ai-client-governance 的精确版本时，使用 submodule：

```powershell
git submodule add <ai-client-governance-url> .ai-client/ai-client-governance
git submodule update --init --recursive
```

如果 `<ai-client-governance-url>` 是本机路径，尤其是相对路径或 `file://` URL，Git 可能默认
禁止 file transport。手工执行时使用：

```powershell
git -c protocol.file.allow=always submodule add <local-ai-client-governance-path> .ai-client/ai-client-governance
git submodule update --init --recursive
```

后续更新：

```powershell
git -C .ai-client/ai-client-governance fetch origin
git -C .ai-client/ai-client-governance pull --ff-only
git add .gitmodules .ai-client/ai-client-governance
git commit -m "chore: update embedded ai-client-governance"
```

Git 官方文档把 submodule 定义为“把一个 Git 仓库作为另一个 Git 仓库的子目录”，
这正是本仓库推荐模型：父项目记录所使用的规则版本，规则仓库保留独立历史。

### 备选：嵌套 clone

如果暂时不想让父仓库记录 submodule，也可以直接 clone：

```powershell
git clone <ai-client-governance-url-or-local-path> .ai-client/ai-client-governance
```

这种方式更轻，但父仓库不会自然记录嵌入规则版本。需要在目标项目 README、
`.ai-client/ai-client-governance-config.json` 或 task tracking 中记录当前 commit。

### 脚本辅助嵌入

本仓库提供一个一键安装入口。常见流程是先把本仓库 clone 到本机，然后从
本仓库目录运行安装脚本，并传入目标项目根路径：

```powershell
powershell -ExecutionPolicy Bypass -File <ai-client-governance-path>\install-ai-client-governance.ps1 `
  -TargetProjectPath <target-project-path> `
  -RemoteUrl <ai-client-governance-url>
```

如果省略 `-RemoteUrl`，脚本会优先读取当前 `ai-client-governance` 仓库的 `origin` URL；
如果没有 remote，才退回使用本地仓库路径作为来源。脚本默认使用当前
`ai-client-governance` 分支作为 submodule branch，也可以显式传入 `-Branch main`。

如果你已经把完整 `ai-client-governance` 仓库准备在目标项目
`.ai-client/ai-client-governance/`，使用 `-Mode existing`。这种模式只生成配置、
根入口和项目规则占位，不会再次 clone，也不会执行 `git submodule add`：

```powershell
powershell -ExecutionPolicy Bypass -File <ai-client-governance-path>\install-ai-client-governance.ps1 `
  -TargetProjectPath <target-project-path> `
  -Mode existing
```

安装前可先预演：

```powershell
powershell -ExecutionPolicy Bypass -File <ai-client-governance-path>\install-ai-client-governance.ps1 `
  -TargetProjectPath <target-project-path> `
  -PlanOnly
```

或者使用 PowerShell 原生预演：

```powershell
powershell -ExecutionPolicy Bypass -File <ai-client-governance-path>\install-ai-client-governance.ps1 `
  -TargetProjectPath <target-project-path> `
  -WhatIf
```

脚本默认完成：

- 把完整 `ai-client-governance` 仓库嵌入到 `.ai-client/ai-client-governance/`，Git 项目默认用 submodule。
- 目标项目根 `AGENTS.md` 缺失时写入薄入口；已存在时默认保留。
- 默认不生成常见 AI 工具入口 adapter；需要时传 `-InstallAgentAdapters` 写入缺失
  薄入口，或传 `-ForceAgentAdapters` 备份后重写。
- 仅在缺失时创建 `.ai-client/project/rules/project/AGENTS.md` 项目规则占位。
- 写 `.ai-client/ai-client-governance-config.json`，记录 source URL、submodule path、branch
  同步策略和 adapter catalog。
- 安装后运行 `.ai-client/ai-client-governance/check-ai-client-governance-sync.ps1 -NoFetch` 做本地一致性检查。

已有文件和目录的处理策略：

- `AGENTS.md` 已存在：默认保留，不覆盖；它属于目标项目已有入口。
  如果确实要用生成薄入口替换，必须显式传 `-ForceRootEntry`，脚本会按备份策略处理。
- `CLAUDE.md`、`GEMINI.md`、`.github/copilot-instructions.md`、
  `.github/instructions/*.instructions.md`、`.cursor/rules/*.mdc`、`.clinerules/`、
  `.windsurf/rules/`、`.continue/rules/`、`.roo/rules/`、`CONVENTIONS.md`
  等工具原生入口：默认不生成；已存在时保留，不覆盖；如果确实要补齐缺失 adapter，
  传 `-InstallAgentAdapters`；如果确实要统一重写 adapter，必须显式传
  `-ForceAgentAdapters`。
- `.ai-client/ai-client-governance-config.json` 已存在：默认备份后重写，保持配置事实源最新。
- `.ai-client/project/rules/project/AGENTS.md` 已存在：默认保留，不覆盖。
- 原生 `skills/` 或其它项目已有 skill：默认保留、只读索引和报告冲突；
  除非用户明确要求修改原生 skill，否则安装脚本和 ai-client-governance 维护任务都不能改它。
  如果目标项目仍有 `.codex/skills/`，它属于旧布局残留，不作为治理 skill 来源；
  需要在当前改造范围内迁移或删除，而不是新增兼容层。
- `.ai-client/project/skills/` 是项目特化层：用于放置根据 ai-client-governance 流程、纠错和当前项目
  经验生成的 skill，不用于承接通用 skill 的复制副本。
- skill 同名时按“原生项目 skill > `.ai-client/project` skill > `ai-client-governance` skill”选择，
  后续由架构守卫或人工 review 报告冲突。
- `.ai-client/ai-client-governance/` 已存在且是已注册 submodule：不重复添加，只继续配置检查。
- `.ai-client/ai-client-governance/` 已存在且你不想注册 submodule：传 `-Mode existing`，
  脚本会复用这个已准备好的 Git 仓库，不再 clone 或 submodule add。
- `.ai-client/ai-client-governance/` 已存在但不是 Git 仓库：停止并提示用户手工处理。
- `.ai-client/ai-client-governance/` 是 Git 仓库但未注册为 submodule：默认停止；确认要接管时，
  重新运行并传 `-AdoptExistingGitRepo`。
- 目标项目不是 Git 仓库且使用默认 submodule 模式：停止；可先 `git init`，
  或显式传 `-Mode clone`。

脚本不会自动 commit 或 push。`git submodule add` 会修改父项目的 `.gitmodules`
和 gitlink，安装后仍要人工 review：

```powershell
git status
git diff --cached
git add .gitmodules .ai-client/ai-client-governance AGENTS.md `
  .ai-client/ai-client-governance-config.json `
  .ai-client/project/rules/project/AGENTS.md
git commit -m "chore: embed ai-client-governance"
```

如果本次显式传了 `-InstallAgentAdapters` 或 `-ForceAgentAdapters`，再按实际生成内容
额外 stage `CLAUDE.md`、`GEMINI.md`、`CONVENTIONS.md`、`.github`、`.cursor`、
`.clinerules`、`.windsurf`、`.continue` 或 `.roo`。

如果目标项目不是 Git 仓库，或明确不希望父仓库记录 ai-client-governance 提交，才传
`-Mode clone` 使用 nested clone。

## 目标项目结构

```text
target-project/
├── AGENTS.md
├── CLAUDE.md
├── GEMINI.md
├── CONVENTIONS.md
├── .github/
│   ├── copilot-instructions.md
│   └── instructions/
│       └── ai-client-governance.instructions.md
├── .cursor/
│   └── rules/
│       └── ai-client-governance.mdc
├── .clinerules/
│   └── ai-client-governance.md
├── .windsurf/
│   └── rules/
│       └── ai-client-governance.md
├── .continue/
│   └── rules/
│       └── ai-client-governance.md
├── .roo/
│   └── rules/
│       └── ai-client-governance.md
└── .ai-client/
    ├── ai-client-governance/
    │   ├── AGENTS.md
    │   ├── README.md
    │   ├── manifest.json
    │   ├── scripts/
    │   │   └── ai_client_governance.py
    │   ├── src/
    │   │   └── ai_client_governance/
    │   └── skills/
    ├── ai-client-governance-config.json
    └── project/
        ├── rules/
        │   └── project/
        │       └── AGENTS.md
        ├── skills/
        ├── records/
        ├── agents/
        ├── logs/
        ├── state/
        ├── tools/
        ├── cache/
        └── tmp/
```

- `AGENTS.md`、`CLAUDE.md`、`GEMINI.md`、Copilot/Cursor/Cline/Windsurf/
  Continue/Roo/Aider 入口：目标项目拥有的 adapter 文件，通常只承载读取顺序、
  同步检查和边界说明。
- `.ai-client/ai-client-governance/`：本仓库的完整 Git 工作树，是通用规则事实源。
- `.ai-client/project/rules/project/`：目标项目维护的本地规则，不写回本仓库。
- schema 4 不提供 common 规则、scripts 或 skills 的分散复制目标；需要通用内容时，
  直接读取 `.ai-client/ai-client-governance/` 内的仓库文件。

## 文件归属和 `.gitignore`

宿主仓库只应追踪稳定治理资产：`.ai-client/ai-client-governance` 的 Git
gitlink、`.ai-client/ai-client-governance-config.json`、项目规则、项目
skills/tools、人读 records 和 agent briefs。`.ai-client/ai-client-governance/`
内部普通文件属于嵌入式通用仓库自身，不应作为宿主普通文件提交。

本地活体产物不进入宿主 Git：`.ai-client/project/state/`、`logs/`、`tmp/`、
`cache/`、`.worktree/`、`doc-index/`、`lifecycle/`、agent 通信运行目录和
`agents/groups/`。安装器会在根 `.gitignore` 中创建或更新
`AI Client Governance generated runtime` managed block；用户自定义 ignore
规则不会被覆盖。

审计和统计入口：

```powershell
python .ai-client\ai-client-governance\scripts\ai_client_governance.py file-ownership audit `
  --root . `
  --strict `
  --record-state
```

审计报告会按类别统计 tracked `.ai-client` 文件、ignored 运行态路径、违规 tracked
live-state 和 `.gitignore` managed block 状态；`--record-state` 将摘要写入
`.ai-client/project/state/aicg.db`，不生成新的 JSON 快照。

`worktree-task closeout-all` 只能提交稳定宿主资产，例如嵌入式治理仓库 gitlink 或显式
传入的 task-tracking 文件；它不得把 `.ai-client/project/state/aicg.db`、旧 JSON
状态、日志、缓存或 task worktree 通过 `git add -f` 重新纳入宿主 Git。

## 脚本包结构与统一入口

`ai-client-governance` 的实现代码采用类似 Spring Boot 根包的组织方式：

```text
.ai-client/ai-client-governance/
├── src/ai_client_governance/
│   ├── cli.py
│   ├── agents/
│   ├── audit/
│   ├── common/
│   ├── docs/
│   ├── gates/
│   ├── lifecycle/
│   ├── records/
│   ├── runtime/
│   ├── sync/
│   ├── validation/
│   └── worktree/
└── scripts/
    └── ai_client_governance.py
```

- `src/ai_client_governance/` 是真实实现，按功能域分层。
- `scripts/ai_client_governance.py` 是唯一公开 Python 入口，使用子命令分发到各模块。
- 不再生成 `scripts/codex_*.py`、`scripts/validate_*.py`、`scripts/agent_*.py`
  这类平铺旧入口；当前范围内发现旧入口时直接迁移或移除。
- 新增能力优先放进 `src/ai_client_governance/<domain>/`，并通过统一 CLI 暴露。

常用命令：

```powershell
python .ai-client\ai-client-governance\scripts\ai_client_governance.py --list
python .ai-client\ai-client-governance\scripts\ai_client_governance.py task-gate --help
python .ai-client\ai-client-governance\scripts\ai_client_governance.py templates task-tracking
```

## 每次会话检查

在目标项目根目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\.ai-client\ai-client-governance\check-ai-client-governance-sync.ps1 `
  -TargetProjectPath .
```

跨系统入口优先使用统一 CLI：

```powershell
python .\.ai-client\ai-client-governance\scripts\ai_client_governance.py sync-check --target-project-path .
```

检查脚本只读检查嵌入仓库状态，默认行为如下：

- 每次会话都检查 `.ai-client/ai-client-governance` 是否存在、是否为 Git 仓库、是否有 dirty 改动。
- 如果上次 fetch 已超过 24 小时，执行一次 `git fetch`；未超过 24 小时则跳过 fetch。
- 无论是否 fetch，都会比较本地 HEAD 与 upstream，发现 ahead、behind 或 diverged
  就提示用户。
- 不自动 `git pull`，因为 pull 可能修改规则工作树。
- 不自动 `git push`，因为 push 需要用户明确确认远端边界。
- warning 会每次出现，直到用户在 `.ai-client/ai-client-governance/` 中完成同步。
- 支持 JSON 输出，供门禁或其它脚本复用：

  ```powershell
  python .\.ai-client\ai-client-governance\scripts\ai_client_governance.py sync-check `
    --target-project-path . `
    --format json
  ```

## 长文件安全读取

通用规则、项目规则、长 task tracking 或长日志不要直接整段输出到终端。
优先用 Python 摘录脚本读取标题索引、关键词命中段或明确行号范围：

```powershell
python .\.ai-client\ai-client-governance\scripts\ai_client_governance.py context-extract `
  .ai-client\ai-client-governance\AGENTS.md `
  --headings
python .\.ai-client\ai-client-governance\scripts\ai_client_governance.py context-extract `
  .ai-client\ai-client-governance\AGENTS.md `
  --match 同步 `
  --context 2
python .\.ai-client\ai-client-governance\scripts\ai_client_governance.py context-extract `
  .ai-client\ai-client-governance\AGENTS.md `
  --range 49:64 `
  --format markdown
```

脚本输出行数、字节数、SHA256、实际摘录行数和 `truncated` 标记；它只读文件，
不修改规则、tracking 或 Git 状态。

常见处理命令：

```powershell
git -C .ai-client/ai-client-governance status
git -C .ai-client/ai-client-governance pull --ff-only
git -C .ai-client/ai-client-governance push
```

如果出现 diverged 或冲突，停止自动处理，保留现场，让用户决定 merge、rebase
或拆分提交。

## 写回通用规则

修改通用规则时，直接在嵌入仓库中工作：

```powershell
cd .ai-client/ai-client-governance
git status
git add AGENTS.md README.md manifest.json scripts
git commit -m "docs: update common AI Client Governance"
git push origin main
```

不要把目标项目 `.ai-client/project/rules/project/`、`.ai-client/project/skills/`、
`.ai-client/project/records/task-tracking/`、`.ai-client/project/records/pending-tasks/`、
`.ai-client/project/records/corrections/`、`.ai-client/project/logs/`、`.ai-client/project/state/`
或业务文档写回本仓库。

## 强制 Worktree 工作区

核心原则是“一切流程化 + 可审计”：重复步骤必须沉淀到脚本，跨会话状态必须写入
机器可读文件，最终结果必须能通过 task tracking、Git 提交和 worktree 状态快照复盘。

所有修改型任务不论任务量大小，都必须先创建任务级 `git worktree` 和独立分支，再改文件、
运行会写入仓库的脚本、格式化、导出或提交。只读定位、读取规则、同步检查、状态检查和
计划输出可以在原工作区完成；一旦要落盘修改，先进入 worktree。

目标路径固定为宿主项目 `.ai-client/project/.worktree/<task-slug>/`。即使修改的是嵌入式
`.ai-client/ai-client-governance/` 仓库，也优先通过 `worktree-task create --repo ai-client-governance`
创建任务 worktree，把新工作区放到宿主项目的 `.ai-client/project/.worktree/` 下。

优先使用固定脚本入口：

```powershell
python .ai-client\ai-client-governance\scripts\ai_client_governance.py worktree-task create `
  --title "worktree fixed task script" `
  --repo ai-client-governance `
  --task-slug worktree-task-script `
  --scope src/ai_client_governance/worktree `
  --scope src/ai_client_governance/cli.py `
  --task-tracking .ai-client/project/records/task-tracking/2026-06-16-worktree固定脚本.md `
  --register-session

python .ai-client\ai-client-governance\scripts\ai_client_governance.py worktree-task status --record-state
python .ai-client\ai-client-governance\scripts\ai_client_governance.py worktree-task status --format json
python .ai-client\ai-client-governance\scripts\ai_client_governance.py worktree-task close `
  --repo ai-client-governance `
  --task-slug worktree-task-script
python .ai-client\ai-client-governance\scripts\ai_client_governance.py worktree-task remove `
  --repo ai-client-governance `
  --task-slug worktree-task-script `
  --execute
python .ai-client\ai-client-governance\scripts\ai_client_governance.py worktree-task cleanup-branch `
  --repo ai-client-governance `
  --task-slug worktree-task-script `
  --execute
python .ai-client\ai-client-governance\scripts\ai_client_governance.py worktree-task finalize `
  --require-merged `
  --require-no-task-worktrees `
  --record-state
```

创建 `self` 仓库任务 worktree 时，脚本默认用 sparse-checkout 排除
`.source-projects/`，因为源码快照通常只在学习、简历证据或源码排查任务中才需要。
确实需要完整源码快照时，显式加 `--include-source-projects`；如果还要排除其它大目录，
可以重复传 `--exclude-path <repo-relative-path>`。这些排除只影响新建的任务
worktree，不删除主工作区文件，也不改变 Git 历史。

创建或恢复任务 worktree 前，执行链路要命中 `worktree-creation-policy` 节点：
计划输出和写入意图阶段必须先在 task tracking 的 `## Worktree 证据` 中记录
`worktree-task create` 创建方式、sparse checkout 策略和 `.source-projects`/源码快照
处理口径。裸 `git worktree add` 只能作为 break-glass 例外；如果固定脚本不可用，
必须写明例外原因、手工命令、稀疏检出或排除策略，以及为什么不会把不需要的大目录
带进任务 worktree。对应门禁可单独运行：

```powershell
python .ai-client\ai-client-governance\scripts\ai_client_governance.py task-gate `
  --task-tracking .ai-client/project/records/task-tracking/<task>.md `
  --only-worktree-creation-policy
```

`status --record-state` 会把 self 和 ai-client-governance 两个仓库下每个任务
worktree 的路径、分支、`head_at_snapshot`、dirty 状态和是否已合并到目标分支写入
`.ai-client/project/state/aicg.db`。DB 是机器事实源；需要人读报告时使用
`status --format text/json` 输出到 stdout。后续 AI 会话必须重新运行同一个脚本、
`worktree-task reconcile --strict` 或结合 `git worktree list --porcelain` 核对真实
Git 状态，最终回复前也必须做一次 live status 校验。

`reconcile` 是比 `status` 更严格的 live-state 对账节点：它读取 Git live worktree、
coord session、active locks 和 integration queue。如果 coord 仍认为某个 session
active，但 Git 已经没有对应 worktree，会直接失败。只有显式传
`--mark-missing-stale` 时，它才会把这类 session 标记为
`stale_or_missing_worktree` 并释放关联 active lock。这个 repair flag 面向外部手工删除、
历史损坏或 break-glass 清理；正常 closeout-owned worktree 不能依赖它收口。

```powershell
python scripts\ai_client_governance.py worktree-task reconcile --strict
python scripts\ai_client_governance.py worktree-task reconcile --repo ai-client-governance --mark-missing-stale
```

`remove` 默认只输出 dry-run 计划；只有显式传 `--execute` 才会调用
`git worktree remove`，且默认拒绝移除 dirty worktree。固定脚本会把路径限制在宿主
项目 `.ai-client/project/.worktree/<task-slug>/`，创建时可自动登记
`worktree-coord` session 和写锁。

当用户要求合并、收口或清理所有 worktree 时，清理目录不是可选的后续建议，而是本轮
DoD 的一部分。合并后必须先确认 task worktree clean 且 merged，再执行
`worktree-task remove --execute` 删除任务 worktree 目录；如还要清理本地任务分支，
先移除 worktree，再执行 `worktree-task cleanup-branch --execute`。最终用
`worktree-task finalize --require-merged --require-no-task-worktrees --record-state`
做 live gate；除非用户明确要求保留或存在取证/恢复需要，否则最终回复不能把残留
`.ai-client/project/.worktree/<task-slug>/` 当成“已完成”。

批量收口优先使用 `closeout-all`，先看 plan，再显式 execute：

```powershell
python .ai-client\ai-client-governance\scripts\ai_client_governance.py worktree-task closeout-all --plan
python .ai-client\ai-client-governance\scripts\ai_client_governance.py worktree-task closeout-all --execute
```

`closeout-all` 按 `ai-client-governance`、`self` 的依赖顺序处理，只接受 clean 且已提交的
task worktree。它会阻塞 dirty worktree、锁定 worktree、缺失 target ref、源仓库未在
target 分支、宿主仓库存在非收口路径脏改动或 merge conflict。
执行模式会合并未合并分支、移除已收口 worktree、删除已合并本地任务分支，把
worktree live-state 和 sync-check 结果写入 `.ai-client/project/state/aicg.db`，运行
CLI list、`git diff --check` 和 sync-check，并只 stage/commit 宿主 gitlink、DB state
和显式传入的 `--task-tracking` 路径。移除任务 worktree 后，它会同步关闭该 worktree 对应的 active
coord session 并释放 active lock，确保后续 `worktree-task reconcile --strict` 不需要
再用 repair flag 清理 closeout 自己生成的残留。`closeout-all` 不执行 `git push`；push
必须作为后续单独步骤，在用户明确批准远端边界后进入对应仓库执行。
如果 closeout、reconcile 或 selftest 生成的状态残留无法由现有命令清掉，应先修复脚本
owner/cleanup 链路，再运行脚本恢复；不要手工编辑 runtime telemetry、coord state 或 lock
来掩盖脚本 bug。

嵌入式 `ai-client-governance` 是宿主仓库的 submodule 时，合并 `ai-client-governance` 任务 worktree 还会改变
宿主仓库记录的 gitlink。这个收口不能只在 `.ai-client/ai-client-governance/` 子仓库内完成：

```powershell
python .ai-client\ai-client-governance\scripts\ai_client_governance.py worktree-task status --record-state
python .ai-client\ai-client-governance\scripts\ai_client_governance.py worktree-task host-closeout `
  --repo ai-client-governance `
  --task-slug <task-slug> `
  --task-tracking .ai-client\project\records\task-tracking\<tracking>.md `
  --require-task-tracking
git add .ai-client\ai-client-governance .ai-client\project\state\aicg.db `
  .ai-client\project\records\task-tracking\<tracking>.md
git commit -m "chore: record ai-client-governance worktree merge"
python .ai-client\ai-client-governance\scripts\ai_client_governance.py worktree-task host-closeout `
  --repo ai-client-governance `
  --task-slug <task-slug> `
  --task-tracking .ai-client\project\records\task-tracking\<tracking>.md `
  --require-task-tracking `
  --require-clean-host
```

`host-closeout` 会比较宿主 index 中 `.ai-client/ai-client-governance` 的 gitlink、嵌入仓库当前 HEAD、
`.ai-client/project/state/aicg.db` 记录的 ai-client-governance HEAD，以及相关 task tracking
是否写到当前 HEAD。这样能防止“子仓库已合并，但宿主仓库还指向旧规则版本”的漏收口。

手工 break-glass 示例：

```powershell
New-Item -ItemType Directory -Force .ai-client\project\.worktree | Out-Null
git -C .ai-client\ai-client-governance worktree add `
  .ai-client\project\.worktree\ai-client-governance-<task-slug> `
  -b codex/ai-client-governance-<task-slug>
```

task tracking 或最终验证记录必须写清源仓库路径、worktree 路径、分支名、基准提交和
`git status` 摘要。`.ai-client/project/.worktree/` 是本地隔离工作区，不应被 stage 或提交；
如果目标项目还没有忽略该路径，应在收口前补上忽略规则或明确记录不提交该目录。

## 并行 Worktree 协调

`ai_client_governance.py worktree-coord` 用来协调同一 Git 仓库的多个 worktree、会话和
智能体组。它把运行态写入 `git rev-parse --git-common-dir` 下的
`ai-client-runtime/worktree-coord/state.db` SQLite 数据库，不进入提交。

常用命令：

```powershell
python scripts\ai_client_governance.py worktree-coord status --active-only
python scripts\ai_client_governance.py worktree-coord session register `
  --title "task" `
  --scope "docs" `
  --metadata-kv phase=planning
python scripts\ai_client_governance.py worktree-coord lock acquire --session-id <id> --scope "docs"
python scripts\ai_client_governance.py worktree-coord queue add --session-id <id> --summary "integration item"
python scripts\ai_client_governance.py worktree-coord validate
python scripts\ai_client_governance.py worktree-task reconcile --strict
```

脚本只登记 session、写锁和 integration queue，不自动 merge、rebase、commit
或 push。最终冲突仍由整合者在独立 worktree 中判断和验证。

在 Windows/PowerShell 中传递结构化 metadata 时，优先使用可重复的
`--metadata-kv key=value`，或把 JSON 对象保存为 UTF-8 文件后使用
`--metadata-file <path>`；不要依赖命令行内联 JSON 字符串，因为引号可能被
原生命令参数传递层剥离。`--metadata` 仍保留给能稳定传入严格 JSON 的环境。

示例：

```powershell
python scripts\ai_client_governance.py worktree-coord session register `
  --title "gate runner" `
  --scope "scripts" `
  --metadata-kv phase=implementation `
  --metadata-kv requested_by=user

$metadata = @{ phase = "planning"; requested_by = "user" } | ConvertTo-Json -Compress
$metadata | Set-Content -Encoding UTF8 .ai-client\project\tmp\worktree-metadata.json
python scripts\ai_client_governance.py worktree-coord session register `
  --title "gate runner" `
  --scope "scripts" `
  --metadata-file .ai-client\project\tmp\worktree-metadata.json
```

## Agent Context Reuse

智能体数量不再靠固定小上限控制。默认策略是先拆任务树，再按每个叶子的写范围、输入依赖、
验证风险、预计上下文大小和可复用事实决定调度方式：

- `reuse`：同一 task id、相同或相邻 scope、已有 agent heartbeat 新鲜、关键文件已读且未污染时，
  复用原 agent，上下文通过 `send_input` 追加最小增量。
- `spawn`：任务叶子独立、写范围不重叠、复用命中低或需要隔离推理时，创建新 agent。
- `merge`：多个 agent 将触碰同一热点文件、锁冲突或验证成本高于并行收益时，由主线程或指定整合者收束。
- `close`：agent 完成后关闭工具侧会话，并把可复用事实写成 context capsule。

推荐调度顺序：

1. 先把用户要求拆成任务树叶子，为每个叶子标注 `read_scope`、`write_scope`、验证责任和风险。
2. 用 `TASK_ID:SCOPE:ROLE:CONTEXT_VERSION` 生成 reuse key，查找同任务同范围的 active agent、heartbeat 和 capsule。
3. 命中同 task、相同或相邻 scope、heartbeat 新鲜、capsule 有结论/已读文件/验证证据且无污染边界时，优先 `reuse`。
4. 独立叶子、不同写范围、需要隔离推理或复用命中低时才 `spawn`；写范围重叠或锁冲突时 `merge`。
5. 每个完成 agent 必须 `close` 并留下 capsule；后续 agent 只读取 capsule、必要行号和新增输入。

Agent Brief 必须包含 `context_reuse`、`reuse_key`、`retained_facts`、`skip_inputs`、
`context_capsule`、`context_ttl`、`contamination_boundary`、`minimal_resume_inputs`、
`token_budget`、`token_proxy_metrics` 和 `token_usage_source`。`skip_inputs` 用来明确哪些规则、
文件或历史输出已经通过 capsule 覆盖，后续 agent 不再重复读取。没有真实 token 统计时，只能记录
brief 行数、必读文件数、预计读取行数、跳过输入数和缓存命中率等代理指标，不能声称精确节省 token。

context capsule 的最小结构：

- `stable_facts`：可复用的结论和来源行号。
- `files_read`：已读文件、行号范围和摘要。
- `decisions`：reuse/spawn/merge/close 的理由。
- `validation`：已跑命令、结果和失败路径。
- `open_questions`：未决事项和下一次最小提示。
- `contamination_boundary`：不得复用的输入、旧假设、敏感信息或失败推理。

推荐通信记录：

```powershell
python .ai-client\ai-client-governance\scripts\ai_client_governance.py agent-comm register GROUP AGENT `
  --brief .ai-client/project/agents/briefs/<brief>.md `
  --reuse-key TASK:SCOPE:ROLE `
  --context-reuse reuse `
  --context-capsule .ai-client/project/agents/briefs/<capsule>.md `
  --context-ttl current-task `
  --contamination-boundary clean-current-task `
  --minimal-resume-input capsule:<capsule> `
  --token-proxy-metric brief_lines=40 `
  --token-usage-source codex-token-usage-or-proxy
```

## 门禁池与调用链路

`ai_client_governance.py gate-pool` 用来把固定门禁编排成一次可追踪运行。它不会自动
修改规则、结构化 task record、corrections、pending 或 Git 状态；每个子门禁都通过
统一 execution telemetry API 写入 `.ai-client/project/state/aicg.db`，并共享同一个
`trace_id`。

从治理插件角度看，门禁池类似一条可注册的执行链：

- 输入过滤器：先把用户原始输入拆成任务数和逐 REQ 表，逐行记录用户要求摘要、
  记录判定、联网/搜索判定、子 AI/验证判定和验收口径。
- 处理拦截器：在改文件前检查审批、worktree、任务类型、联网核对和脚本能力。
- 写入前 live-state 节点：在 `write-intent`、`resume`、`merge-cleanup` 和
  `final-output` 边界运行 `worktree-task reconcile`，确认 coord/session/queue 没有
  偏离 Git live worktree 事实。
- 完成测试节点：根据 changed paths、任务类型和验收口径生成测试计划，避免只靠最终
  回复口头声称完成。
- 输出拦截器：最终回复前检查完成项、未完成项、未验证项、阻塞项、active pending、
  Git/worktree 状态、是否合并、是否提交、是否 push 和下一步用户确认。
- 横切门禁：编码、文档引用、correction 扫描、execution telemetry 和 trace flow。

新增门禁时优先注册到 `task-gate`、`session-gate`、`gate-pool` 或 `lifecycle` 的
相应位置，让它自动进入最终收口链路。

`runtime components` 是治理节点注册表。这里保留 `runtime` 这个命令名，是为了表达
`ai-client-governance` 插件内部的生命周期链路，不表示本仓库要替代宿主客户端的 agent runtime。
每个节点声明 `events`、`condition`、
`requires_facts`、`produces_facts`、`effect`、`fail_policy`、`gate_step`、
`dedupe_key` 和 `performance_budget` 等字段。`skill` 只作为 capability plugin
被 `input.filter.skill-router` 选中，不能跳过 `ai-client-governance` 的审批、worktree、
测试和输出门禁。

`gate-pool` 会读取匹配组件的 `gate_step`，并把相同 `gate_step` 去重后执行。
因此多个节点都要求 `doc-index` 时，门禁池只会把所有 `--changed-path` 聚合起来运行一次，
而不是按节点或按文件反复慢跑。

`task-run plan` 是更前置的命令压缩入口。它不等待模型逐条思考下一步命令，而是把候选
命令在本地归一化、去重、按只读/验证/状态变更分类，并输出
`command-compression.analysis` 事件。结构化 task gate 会检查该事件；缺失时，中/大型或
修改型任务不能继续收口。小型修改任务也有独立 mutating 节点；可用
`runtime components --event write-intent --task-type code-debug` 复核它不会漏掉
`command-compression`。当前 execution telemetry 已经能记录 `gate-pool` 子门禁、`task-run`
DAG、`shell-adapter`、`telemetry record` 和显式命令适配器 `tool-invocations run/record`，但宿主客户端裸 shell 调用
不能被本仓库自动拦截，所以重要命令要通过 wrapper 执行或在 task record 说明例外。
wrapped telemetry 是补偿证据，不等同于 shell-adapter auto-intercept。

当前自我检测机制已经有效覆盖输入拆解、结构化 task record、worktree live state、gate-pool
去重、工具 telemetry、completion-test、task-run DAG/cache/diagnose 和 selftest；这能发现
“没记录输入”“缺 worktree 证据”“最终门禁没跑”“重复终态命令”“cache 未命中/命中边界”
等硬问题。`task-run run` 的安全缓存键包含 HEAD、changed paths、declared input hashes、
task types、command、cwd、node kind 和 runner version；`task-run diagnose` 把 stale coord
session、重复验证、失败 telemetry、task-record/task-queue 口径差和裸 shell 拦截缺口做成
自动诊断报告，而不是等人工发现。
默认命令和诊断脚本解析只认嵌入式 `.ai-client/ai-client-governance/` 或治理仓库自身入口；
宿主根目录的 `scripts/ai_client_governance.py` 不作为旧路径 fallback。

`gate-pool` 的 live-state 节点只运行只读 `worktree-task reconcile --strict`。需要记录
worktree live-state 时，必须显式运行
`worktree-task status --record-state` 或 `worktree-task finalize --record-state`，并在收口提交
中处理 `.ai-client/project/state/aicg.db`；门禁池不能先写状态再要求 clean host。

文档联动节点属于 post-change 链路。修改功能、脚本、规则、skill、manifest、
README 或入口 adapter 后，必须判断是否影响用户可读文档、命令说明、索引、
Markdown 链接或 `.references` 记录。影响到文档时先同步文档和引用；不影响时在
结构化 task record 写明 no-impact 理由。对应的机器检查是：

```powershell
python scripts\ai_client_governance.py runtime components `
  --task-type rules-script `
  --changed-path src\ai_client_governance\runtime\registry.py `
  --final

python scripts\ai_client_governance.py gate-pool `
  --task-id <task-id> `
  --task-type rules-script `
  --changed-path src\ai_client_governance\runtime\registry.py `
  --changed-path README.md `
  --event final-output `
  --final `
  --dry-run

python scripts\ai_client_governance.py completion-test `
  --task-type rules-script `
  --changed-path src\ai_client_governance\runtime\registry.py
```

`gate-pool --dry-run` 是只读规划入口；没有 `--task-id` 或 `--task-tracking` 时会用
`<task-id>` 占位展示门禁链路，真正执行时仍必须提供结构化 task id 或历史 tracking。

验收时至少看五件事：`runtime components` 能看到相关节点，`task-run plan/run/diagnose`
和 `telemetry report` 能证明本地命令压缩、telemetry 和 cache 行为，`gate-pool --dry-run`
能看到一次聚合后的 `ai_client_governance.py doc-index` 和 completion/worktree 节点，
`completion-test` 能生成测试计划，`tool-flow` 能看到最终门禁和报告。
如果链路没有触发、重复触发或明显拖慢任务，必须在结构化 task record 记录原因并修正
触发条件或 `gate_step` 去重策略。

用户输入中出现联网、搜索、核对、最新、资料、URL、引用等触发词时，输入过滤器必须
把它登记成可追踪要求；后续要么记录联网证据，要么记录无需联网或阻塞原因。
`task-gate` 会把输入拆解表与用户要求追踪表交叉校验，缺任意 REQ、缺判定列或
用散文关键词代替逐 REQ 表都会失败。

子 AI 用于验收门禁时，输出不能只写“通过”。task tracking 需要有 `## 子 AI 验收矩阵`，
逐行覆盖 REQ、门禁、失败路径、成功路径、发现问题和修复复测，确保子 AI 测的是整条
生命周期链，而不是随机抽样。`task-gate` 会按矩阵行解析，只有一句话式“全面覆盖”
不能替代结构化矩阵。

常用命令：

```powershell
python scripts\ai_client_governance.py gate-pool `
  --task-tracking .ai-client\project\records\task-tracking\example.md `
  --task-type correction `
  --task-type rules-script `
  --changed-path .ai-client\ai-client-governance\AGENTS.md `
  --changed-path .ai-client\ai-client-governance\src\ai_client_governance\gates\task_gate.py `
  --final
```

只查看计划：

```powershell
python scripts\ai_client_governance.py gate-pool `
  --task-tracking .ai-client\project\records\task-tracking\example.md `
  --dry-run
```

链路报告可单独运行：

```powershell
python scripts\ai_client_governance.py tool-flow `
  --trace-id <trace-id> `
  --require-final-gate `
  --require-report `
  --require-trace
```

## 输出门禁与 Worktree 收口

修改型任务通常不会自动合并 worktree，也不会自动 stage、commit 或 push。
因此最终回复前，结构化 task record 必须有 worktree 完成记录和输出边界记录，至少写清：

- worktree 路径、分支、基准提交和当前 `git status`。
- worktree 任务是否完成。
- 是否已经合并回源仓库；未合并时写明等待用户确认。
- 是否 stage/commit；未提交时写明没有本地 commit。
- 是否 push；未推送时写明远端未变。
- 是否已移除任务 worktree 目录、是否已清理本地任务分支；如果保留，写明原因。
- 用户下一步要决定的是合并、提交、推送、继续验证、保留取证还是放弃。

`ai_client_governance.py task-gate` 会把这些当作输出拦截器检查，缺少时最终门禁失败。
Git push 状态检查不是定时任务；它属于 `plan-output`、`status-output` 和
`final-output` 的输出边界审计。默认只报告 dirty、ahead、behind、diverged、未 push
或已 push 的事实，不自动执行 `git push`。

## Codex Token 用量统计

`.ai-client/ai-client-governance/skills/codex-token-usage/` 提供本地只读
统计能力，用于回答“统计最近 30 天 Codex token 用量”“统计某个自然月的缓存命中率和净用量”
这类问题。它默认读取当前用户 `~/.codex/sessions/**/rollout-*.jsonl`，
只汇总 `token_count` 事件中的 `last_token_usage`，避免累加会话内累计字段
`total_token_usage` 导致重复计算。

常用命令：

```powershell
python .ai-client\ai-client-governance\skills\codex-token-usage\scripts\codex_token_usage.py `
  --days 30 `
  --timezone Asia/Shanghai
python .ai-client\ai-client-governance\skills\codex-token-usage\scripts\codex_token_usage.py `
  --month 2026-04 `
  --format json
```

统计口径：

- 非缓存 Input = `Input - Cached input`。
- 净用量 = `非缓存 Input + Output`。
- 缓存命中率 = `Cached input / Input`。
- 脚本只输出聚合指标，不上传日志、不输出原始会话内容。

## 通用与项目规则边界

纳入本仓库：

- AI 协作审批、任务量评估、恢复现场、Git 边界和子 AI 协作。
- corrections、pending、task tracking、门禁脚本、编码检查和脚本维护要求。
- 脚本能力适配、主对话上下文压缩快照和对应只读 task gate。
- 用户要求追踪门禁、门禁池编排器和脚本调用 trace/flow 报告。
- 可跨项目复用的 AI skills 和只读维护脚本。
- 本地 Codex session token 用量统计 skill；这是可选适配能力，不是框架绑定条件。
- 嵌入、同步检查、Git 回写和 README/manifest 自描述规则。

不纳入本仓库：

- 目标项目业务规则、目录结构、学习路线、简历规则和交付物规则。
- 目标项目 `.ai-client/project/rules/project/`。
- 目标项目 task tracking、pending、corrections、execution telemetry 数据。
- 外部项目状态、日志、源码快照、构建产物和本地临时验证目录。

## Schema 4 边界

schema 4 只描述 `.ai-client` 单一布局和结构化事实源：`.ai-client/ai-client-governance/`
是通用规则、README、manifest、scripts 和 skills 的唯一通用事实源；
`.ai-client/project/state/aicg.db` 是新任务的机器事实源。安装脚本默认只生成
`AGENTS.md` 薄入口、`.ai-client/ai-client-governance-config.json` 和缺失时的
项目规则占位，不再声明或写出旧 common 副本、scripts 副本或 skills 副本目标。
Claude、Gemini、Copilot、Cursor、Cline、Windsurf、Continue、Roo 和 Aider
`CONVENTIONS.md` 等平台 adapter 只在显式传 `-InstallAgentAdapters` 或
`-ForceAgentAdapters` 时生成。

改造旧项目时，直接把完整仓库嵌入 `.ai-client/ai-client-governance/`，并让目标项目
已有入口或显式生成的 adapter 指向 `.ai-client/ai-client-governance/AGENTS.md` 和
`.ai-client/project/rules/project/AGENTS.md`。旧 `.codex` 治理布局、common 副本、
脚本副本或 skill 副本不参与读取、安装、同步或 gate；属于当前改造范围时直接删除、
替换或迁移到 `.ai-client`。

如果目标项目已经有 `AGENTS.md`、`CLAUDE.md`、`GEMINI.md`、Copilot/Cursor/Cline
等原生规则或本地 skills，先把它们当作原生项目事实源管理。`ai-client-governance`
默认不生成非 `AGENTS.md` 平台入口；显式安装时也只能补缺、索引和报告冲突，
不能静默覆盖；根据纠错或项目流程生成的项目特化 skill 统一维护在
`.ai-client/project/skills/`，通用 skill 保持在 `.ai-client/ai-client-governance/skills/`。

## 验证建议

修改本仓库后，至少运行：

```powershell
$env:PYTHONUTF8 = "1"
$env:PYTHONPYCACHEPREFIX = ".ai-client\project\cache\python-pycache"
$pyRoots = @(
  ".ai-client\ai-client-governance\scripts",
  ".ai-client\ai-client-governance\src"
)
$pyFiles = Get-ChildItem -Recurse -Filter *.py $pyRoots
python -m py_compile ($pyFiles | ForEach-Object { $_.FullName })
python .ai-client\ai-client-governance\scripts\ai_client_governance.py --list
python .ai-client\ai-client-governance\scripts\ai_client_governance.py file-ownership audit `
  --root . `
  --strict `
  --record-state
python .ai-client\ai-client-governance\scripts\ai_client_governance.py validate-encoding `
  --paths .ai-client\ai-client-governance\AGENTS.md `
  .ai-client\ai-client-governance\README.md `
  .ai-client\ai-client-governance\manifest.json `
  AGENTS.md CLAUDE.md GEMINI.md CONVENTIONS.md `
  .github .cursor .clinerules .windsurf .continue .roo `
  .ai-client\ai-client-governance\scripts `
  .ai-client\ai-client-governance\src `
  --require-paths
python .ai-client\ai-client-governance\scripts\ai_client_governance.py session-gate --help
python .ai-client\ai-client-governance\scripts\ai_client_governance.py task-gate --help
python .ai-client\ai-client-governance\scripts\ai_client_governance.py tool-flow --help
python .ai-client\ai-client-governance\scripts\ai_client_governance.py gate-pool --help
python .ai-client\ai-client-governance\scripts\ai_client_governance.py task-run diagnose --format json
python .ai-client\ai-client-governance\scripts\ai_client_governance.py shell-adapter diagnose --format json
python .ai-client\ai-client-governance\scripts\ai_client_governance.py worktree-coord --help
python .ai-client\ai-client-governance\scripts\ai_client_governance.py selftest --root .
```

`selftest` 默认把 run directory 放到 `.ai-client/project/tmp/ai-client-governance-selftest/`
并声明 artifact manifest。测试进程会把 Python pycache 和 doc-index 输出重定向到 run
directory；通过且未传 `--keep` 时会清理 run directory 和本轮新建的空父目录。如果新增
脚本在 run directory 外留下 `.ai-client/project/doc-index`、`lifecycle`、`cache` 等未声明
产物，`selftest-artifact-manifest` 会失败。

修改 PowerShell 脚本后，还要用 PowerShell Parser 做语法检查，并在临时目录跑最小
真实用例：嵌入仓库、写 config、运行每次会话检查、验证 warning/OK 输出符合预期。
