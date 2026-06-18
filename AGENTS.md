# AI 执行流程通用框架

本文件是 `ai-client-governance` 的通用客户端治理插件事实源，同时也是 `AGENTS.md` 生态的
入口适配文件。治理规则不能绑定某一个模型、客户端或文件名；`AGENTS.md`、
`CLAUDE.md`、`GEMINI.md`、Copilot instructions、Cursor/Cline/Windsurf/Continue
rules 等都只是把同一套执行流程暴露给不同 AI 工具的入口适配层。

本文件只保留跨项目不可绕过的协作边界、生命周期和入口适配原则。可由程序检查、
生成或汇总的细节，优先放入 `src/ai_client_governance/` 包、`scripts/ai_client_governance.py`
统一入口、通用 skill、manifest 或 README。

## 设计原则

以下原则约束 `ai-client-governance` 自身的规则、脚本、skills、入口 adapter、
README 和 manifest 演进；项目业务规则继续留在宿主项目特化层。

- **客户端治理层，不是 agent runtime**：本仓库只治理宿主 AI 编程客户端已有能力
  怎么被使用、记录和验收，不重造 Codex、Claude Code、Cursor、Cline、Windsurf、
  Continue 等客户端，也不把某个模型或工具私有行为写成通用前提。
- **`.ai-client` 是唯一治理布局**：通用规则、脚本、skills、README 和 manifest
  必须作为一个独立 Git 仓库嵌入 `.ai-client/ai-client-governance/`；项目特化层
  只能放在 `.ai-client/project/`。旧 `.codex` 治理布局不保留、不兼容、不作为
  fallback；活体入口、脚本默认值、状态文件和新增记录都不能写回旧目录。
- **入口是 adapter，契约才是事实源**：`AGENTS.md`、`CLAUDE.md`、`GEMINI.md`、
  Copilot/Cursor/Cline/Windsurf/Continue/Roo/Aider 等入口只负责带路，不能各自
  演化出一套规则。真正事实源是通用治理契约、项目规则和 manifest。
- **可确定约束优先组件化**：可重复、易遗漏、可检查或跨会话影响仓库状态的要求，
  优先沉淀为 CLI、runtime component、gate、SQLite 状态表或 skill 能力；散文规则只保留
  不可绕过的边界和设计意图。
- **先压缩本地确定步骤**：生成或运行新命令前，必须先判断能否去重、合并、并行、
  使用 gate-pool/task-run 或复用缓存，避免把确定性命令选择拆成多轮模型 HTTP 往返。
- **结构化事实优先于 Markdown 反解析**：新任务先写 SQLite 事实源
  `.ai-client/project/state/aicg.db`，再按需导出 Markdown 报告；不能把机器门禁依赖
  建在散文和 Markdown 表格的事后反解析上。
- **新设计替代旧活体路径**：更新状态架构时直接淘汰旧机器事实源，不保留默认兼容层、
  默认 JSON 快照、fallback 读写或双轨收口。旧文件最多作为一次性 cleanup 输入，清理后
  必须从脚本、gate、规则和收口路径中删除；已经废弃的运行态文件只允许由脚本 cleanup
  或 discard，不再反向恢复为新状态事实。
- **DB 是唯一活体治理状态**：任务队列、结构化任务记录、sync-check 结果、worktree
  live-state、lifecycle 状态和可查询审计事实默认写入 `.ai-client/project/state/aicg.db`。
  JSON/Markdown 只能通过显式导出命令输出给人读，不能作为后续机器逻辑的默认输入。
- **宿主只追踪稳定治理资产**：宿主 Git 只能追踪 `.ai-client/ai-client-governance`
  的 gitlink、治理配置、项目规则、项目 skills/tools、人读 records 和 agent brief。
  `.ai-client/project/state/`、`logs/`、`tmp/`、`cache/`、`.worktree/`、`doc-index/`、
  `lifecycle/` 以及 agent 运行通信状态都是本地活体产物，必须由安装器写入
  `.gitignore` managed block，并由 `file-ownership audit` 统计和拦截。
- **修改必有隔离与证据**：修改型任务默认通过 worktree、结构化 task record、写锁、
  执行 telemetry、验证记录和最终状态收口；不能只依赖对话记忆或最终回复口头声称完成。
- **项目特化不污染通用层**：目标项目的业务、简历、学习路线、源码快照、目录结构、
  本地交付规则和私有偏好留在 `.ai-client/project/` 或项目原生资产中；通用仓库只收纳
  跨项目可复用的治理流程和工具。
- **保留项目原生资产**：目标项目已有原生规则入口、skills、IDE 配置或团队约定时，
  默认保留、索引并报告冲突；除非用户明确批准，不静默覆盖。
- **同步检查只审计，不替人决策**：同步脚本可以发现 missing、dirty、ahead、behind
  或 diverged，但不自动 pull、push、merge 或删除；需要回写时进入对应 Git 仓库按
  普通 Git 边界处理。
- **live state 优先于快照**：结构化 task record、state 文件和日志用于恢复现场；最终判断
  worktree、Git、session、lock 和 queue 状态时，必须重新核对 live state。
- **跨客户端可移植**：新增规则和能力默认使用 agent-neutral 语义；只有明确属于某个
  客户端或 skill 的局部适配，才写入工具私有细节。

## 读取顺序、入口适配与规则分层

- 当前 AI 工具自动加载的项目原生规则入口必须先视为项目入口。常见入口包括
  `AGENTS.md`、`CLAUDE.md`、`GEMINI.md`、`.github/copilot-instructions.md`、
  `.github/instructions/*.instructions.md`、`.cursor/rules/*.mdc`、
  `.clinerules/`、`.windsurf/rules/*.md`、`.continue/rules/`、`.roo/rules/`
  和 `CONVENTIONS.md`；具体以目标工具官方文档和目标项目已有文件为准。
- `ai-client-governance` 的通用规则事实源是嵌入式
  `.ai-client/ai-client-governance/AGENTS.md`；旧 `.codex/ai-client-governance/`
  不是迁移期路径，也不是 fallback。这里的 `AGENTS.md` 是入口适配文件名，
  不代表框架只服务 Codex 或 AGENTS 生态。
- 项目特有规则默认入口是 `.ai-client/project/rules/project/AGENTS.md`；如果项目
  未来改用其它内部事实源，必须在根入口 adapter、manifest 和安装配置中同步记录。
- 各工具入口 adapter 应保持薄层：只声明读取顺序、编码、同步检查和边界，不复制
  大段通用规则；能导入时优先导入，不能导入时用明确路径指向同一事实源。
- `.codex/rules/common/`、根 `scripts/`、顶层 `.codex/skills/` 的旧复制模型
  已删除；发现当前入口、脚本或 README 仍引用它们时，直接改成 `.ai-client`
  布局，不新增兼容层。
- 资产优先级固定为：
  1. 目标项目原生资产。
  2. `.ai-client/project/` 项目特化层。
  3. `.ai-client/ai-client-governance/` 通用层。
- 通用规则不得保存项目业务、学习路线、简历规则、源码快照、本地交付细节或
  某个 AI 工具私有的交互偏好。
- 项目规则不能放宽通用安全边界、审批流程、Git 边界和恢复现场要求。
- Windows/PowerShell 读取中文规则文件时，设置本次进程 UTF-8 编码，并使用
  `Get-Content -Raw -Encoding UTF8`；长文件优先用
  `python .ai-client/ai-client-governance/scripts/ai_client_governance.py context-extract` 摘录。

## 核心原则：流程化与可审计

- 一切可重复、易遗漏、会影响仓库状态或会跨会话协作的流程，都必须优先脚本化、
  状态化和门禁化；不能只依赖单次对话记忆或人工口头约定。
- 每个修改型任务必须留下可复盘证据：任务输入拆解、审批、worktree、写锁、
  操作 telemetry、验证、提交、合并、清理和下一步状态，都要能被后续 AI 会话读取。
- 写入前必须先有分析契约：任务理解、范围/写入面、非目标、风险/不确定性、
  验收标准和验证预算必须明确。分析契约不完整时先阻断或澄清，不能用更大的
  测试集合在收口阶段补偿前置分析不清。
- worktree 创建、状态同步、收口检查和安全清理优先使用
  `python .ai-client/ai-client-governance/scripts/ai_client_governance.py worktree-task ...`；当前 worktree
  总览用 `worktree-task status --record-state` 写入 `.ai-client/project/state/aicg.db`。
  活体状态仍以重新运行 Git live-state 命令为准；DB 中的 HEAD 字段必须使用
  `*_at_snapshot` 语义，避免记录动作本身推进 HEAD 后造成误判。
- 创建任务 worktree 前必须经过 `worktree-creation-policy` 节点：计划输出或写入前
  先声明使用 `worktree-task create`、sparse checkout 策略和源码快照目录处理方式。
  裸 `git worktree add` 只作为 break-glass 例外，必须在 task tracking 记录原因、
  稀疏检出/排除策略和为什么不能使用固定脚本。
- 用户要求“合并所有 worktree”“收口 worktree”或 DoD 已把 worktree 合并作为完成条件时，
  已 clean 且已合并的任务 worktree 目录必须在最终回复前用
  `worktree-task remove --execute` 清理；对应本地任务分支在 worktree 移除后用
  `worktree-task cleanup-branch --execute` 删除。只有用户明确要求保留，或存在 dirty、
  未合并、取证/恢复需要时才能不清理，并且必须在输出门禁写明保留原因。
- 多会话、多线程或多 worktree 修改同一范围时，必须通过 `worktree-coord` 的
  session、lock 和 integration queue 记录冲突、整合者、冲突矩阵和验证结果。
- 用户新增流程要求时，先判断是否应升级为脚本能力、生命周期组件、门禁或状态文件；
  默认不要只把要求写成散文规则。

## 会话同步

- 新会话必须运行 `.ai-client/ai-client-governance/check-ai-client-governance-sync.ps1` 或等价 wrapper。
- 跨系统事实逻辑优先来自
  `python .ai-client/ai-client-governance/scripts/ai_client_governance.py sync-check`。
- 每次会话都检查 `.ai-client/ai-client-governance/` 是否存在、是否为 Git 仓库、是否 dirty、
  是否 ahead/behind/diverged。
- 24 小时规则只限制 `git fetch` 频率；本地不一致必须每次提示到同步完成。
- 同步检查不得自动 `pull` 或 `push`。

## 审批与任务队列

- 修改文件、移动文件、运行会改变仓库状态的脚本、批量处理、导出、stage、commit
  或 push 前，必须给出带标签的计划并获得明确批准。
- 批准标签必须可读，例如 `计划-规则入口适配器迁移`；多计划时普通“批准”不够明确。
- 用户回复 `批准：全部` 只批准当前请求批准消息列出的计划。
- 用户消息先进入任务队列 `candidate` 或 `awaiting_approval`，不能直接成为 `active`。
- 只有显式批准并进入 `ready` 的任务才能 `start-next` 成为 `active`。
- 队列事实源是 `.ai-client/project/state/aicg.db`，入口是：
  `python .ai-client/ai-client-governance/scripts/ai_client_governance.py task-queue ...`。
- `task-queue` 不提供默认 JSON 队列文件、heartbeat 文件或 `--queue-file` fallback；
  需要人读报告时使用 `status --format text/json` 输出到 stdout。
- 一次只允许一个 active task；插入任务完成后必须返回原主任务或记录阻塞。

## 强制 Worktree

- 所有修改型任务不论大小，正式改文件、移动文件、格式化、导出、运行写仓库脚本
  或提交前，必须先创建任务级 `git worktree` 和独立分支。
- 只读定位、读取规则、同步检查、状态检查、计划输出不要求 worktree；一旦要落盘，
  先进入 worktree。
- 任务 worktree 默认放在宿主项目 `.ai-client/project/.worktree/<task-slug>/`。
- 修改嵌入式 `.ai-client/ai-client-governance/` 时，仍优先用
  `worktree-task create --repo ai-client-governance` 创建任务 worktree，目标路径放到宿主项目
  `.ai-client/project/.worktree/<task-slug>/`。只有固定脚本不可用时，才从
  ai-client-governance 仓库手工执行 `git worktree add` 并记录 break-glass 原因。
- task tracking 必须记录源仓库、worktree 路径、分支、基准提交和 `git status`。
- coord session、lock 或队列记录不能代替 Git live state；开始修改、恢复任务和最终收口时，
  必须用 `git worktree list`、`worktree-task status --record-state` 或
  `worktree-task reconcile --strict` 复核 worktree 真实存在、分支正确且未被其它会话清理。
- `worktree-task reconcile` 是 live-state 对账节点：以 Git worktree list 为事实源，
  对比 coord session、lock 和 queue。active session 指向不存在的 worktree 必须阻塞；
  只有显式运行 `--mark-missing-stale` 才能把这类 session 标记为
  `stale_or_missing_worktree` 并释放相关 active lock。
- `worktree-task closeout-all` 移除任务 worktree 后，必须在同一脚本链路关闭该
  worktree 对应的 active coord session 并释放 active lock；脚本生成的 closeout
  残留不能要求 AI 或用户手工改运行态数据。
- worktree 合并收口任务的最终 live gate 必须使用
  `worktree-task finalize --require-merged --require-no-task-worktrees` 或等价检查，
  确认没有残留任务 worktree；如因明确保留策略跳过该强门禁，必须记录原因和恢复方式。
- 合并嵌入式 `ai-client-governance` 任务 worktree 后，不能只收口 `.ai-client/ai-client-governance/`
  子仓库。宿主仓库也必须作为同一条 closeout 链路处理：刷新
  `.ai-client/project/state/aicg.db` 中的 worktree live-state，检查并提交 `.ai-client/ai-client-governance` submodule
  gitlink，更新对应 task tracking，并用
  `worktree-task host-closeout --repo ai-client-governance --require-task-tracking` 或
  `worktree-task finalize --require-host-closeout` 核对。最终完成态还要加
  `--require-clean-host`，确认宿主仓库没有遗留 gitlink、state 或 tracking 脏改动。
- 如果工具限制、路径冲突、分支冲突、权限或 Git 状态异常导致无法创建 worktree，
  必须停止修改并向用户确认处理方式，不能退回主工作区直接改。
- 跨 worktree 的 session、锁和 integration queue 使用
  `python .ai-client/ai-client-governance/scripts/ai_client_governance.py worktree-coord ...`。

## ai-client-governance 客户端治理插件与生命周期组件

- `ai-client-governance` 是建立在 Codex、Claude Code、Cursor、Cline、Windsurf、Continue 等
  AI 编程客户端已有 agent 能力之上的治理插件层，不是替代这些客户端的底层
  agent runtime，也不是当前会话里的 LangGraph 克隆。新增可确定的约束时，优先注册为
  输入过滤器、处理拦截器、输出拦截器、横切门禁或报告组件，而不是只追加散文。
- 宿主客户端负责对话上下文、工具调用、文件写入、终端执行、人类批准和会话承载；
  `ai-client-governance` 负责治理这些能力何时可用、如何记录证据、如何检查 worktree/Git/文档边界、
  以及最终输出必须覆盖哪些状态。
- 如果未来自建 agent runner，可以把本节节点映射到 LangGraph 等 workflow 引擎；
  在现有客户端中，`runtime components` 只是 `ai-client-governance` 插件内部的治理节点注册表，
  不能假设它能拦截客户端内部所有行为。
- 输入过滤器负责拆分用户输入、识别要求数量、绑定逐 REQ 行和任务类型，并判断每条
  要求是否必须落盘、是否触发联网/搜索、是否触发子 AI 或黑盒验证。
- 用户输入是强制 `user-message` join point。非纯只读小问答在计划、写入、恢复或最终
  回复前，必须先运行 `lifecycle input-filter`，把 `requirements`、`triggers`、
  `outputs` 和 `events.event_type=input-filter.preflight` 写入结构化 task record；
  缺少这些事实时 `task-record gate --event preflight` 必须 fail closed。
- 输入过滤器还必须把用户目标和用户陈述分开：用户陈述只能作为 claim，不能直接作为事实。
  中/大型、修改型或规则/脚本/correction 任务缺少
  `events.event_type=user-claim-validation.analysis`、claim 的 `trust_level`、
  `risk_flags` 和 `verification_action` 时，`task-record gate --event preflight`
  必须 fail closed。用户说“这是 bug”“应该这样”“现在状态是...”都要先核对
  live state、规则或外部资料，不能盲从。
- 输入记录属于 turn-start 事实，允许在 worktree 创建前先落库；worktree 证据属于
  prewrite/final 边界，修改型任务最终收口缺 worktree 时必须 fail closed。
- 处理拦截器负责审批、worktree、联网核对、task tracking、脚本能力适配和状态机。
- 输出拦截器负责最终回复覆盖、worktree 完成状态、未合并/未提交/未 push 边界和下一步提示。
- 横切门禁负责编码、文档引用、Git 边界、脚本 telemetry、trace flow 和 correction 扫描。
- 强制输入过滤器入口：
  `python .ai-client/ai-client-governance/scripts/ai_client_governance.py lifecycle input-filter ...`。
- 写入前必须运行 lifecycle preflight，并用 `--task-id` 验证结构化 task record：
  `python .ai-client/ai-client-governance/scripts/ai_client_governance.py lifecycle preflight --task-id <task-id> ...`。
- 收口前必须运行 lifecycle finalize，并根据任务类型触发已注册门禁：
  `python .ai-client/ai-client-governance/scripts/ai_client_governance.py lifecycle finalize ...`。
- 生命周期把输入来源区分为 `user`、`web`、`file`、`tool`、`agent`、`history`。
- 联网输入必须记录 URL 或资料路径；不能把外部资料和用户指令混作同一事实。
- 脚本判断与人工判断不一致时，在 task tracking 记录采用、修正或阻塞原因。
- 计划、诊断、写入、stage、commit 和 push 是不同 join point。中/大型、修改型或
  规则/脚本/correction 任务必须写入
  `events.event_type=plan-approval-boundary.analysis`，其中 `execution_policy`
  说明是否已批准本地执行，`push_policy` 必须为
  `push_requires_separate_approval`。用户只是提问、指出问题或批准当前计划，不等于
  批准新的诊断链、写入链或远端 push。
- 生命周期还必须把当前任务、变更路径、候选命令和记录事实分类为
  `ai-client-governance-common`、`project-specialization`、`native-project-assets`、
  `mixed` 或 `unknown`。中/大型或修改型任务缺少 `scope-classification`
  trigger 和事件 payload 中的 `scope_kind` 时，`task-record gate` 必须 fail closed。
  通用治理变更只写入 `.ai-client/ai-client-governance/`，项目特化事实只写入
  `.ai-client/project/` 或项目原生资产；混合任务必须在 task record 说明边界。
- 新增治理节点必须同时说明触发条件、去重键、失败策略、验证证据和性能边界；
  不能让所有任务无差别慢跑同一检查。
- 生成、选择或运行本地命令前，必须经过 `command-compression` 前置拦截器：
  使用 `python .ai-client/ai-client-governance/scripts/ai_client_governance.py task-run plan ...`
  或等价本地分析记录 `event_type=command-compression.analysis`，说明哪些命令被去重、
  合并、并行、交给 gate-pool 或必须按顺序执行；payload 必须包含非空 `groups`。
  中/大型或修改型任务缺该事件或缺 `groups` 时 `task-record gate` 必须 fail closed。
  runtime registry 必须同时覆盖中/大型任务和所有修改型任务；小型修改不能因为
  `task_size=small` 跳过 `command-compression` 与 `task-run-dag` 节点。
- 命令压缩分析完成后，优先使用
  `python .ai-client/ai-client-governance/scripts/ai_client_governance.py task-run run ...`
  执行确定性本地 DAG：只读/验证组可以并行，显式 `--cache` 时只缓存只读/验证节点；
  状态变更、Git 写入、锁、task-record apply 和未知副作用命令必须顺序执行且不缓存。
- `task-run plan` 的每条命令必须暴露 capability 事实：`capability`、`risk_level`、
  `side_effect`、`cache_eligible`、`parallel_eligible`、`approval_required` 和
  `approval_reason`。这些字段先作为 command-compression event 和后续 policy engine
  的稳定输入；启发式分类不得在同一节点里直接替代审批或安全策略阻断。
- `task-run run`、`gate-pool`、`shell-adapter`、`telemetry record` 和命令适配器
  `tool-invocations run/record`
  默认把执行 span、事件、耗时、失败、cache、scope 和 trace 写入
  `.ai-client/project/state/aicg.db` 的 telemetry 表；`--trace-json` 只作为显式报告输出，
  `--jsonl-artifact-dir` 只允许作为隔离测试或一次性导出 artifact，不能作为默认机器事实源。
  `--no-telemetry` 只能用于隔离测试。宿主客户端裸 shell 调用若无法自动拦截，必须在
  task record 中记录原因或改用 `task-run`、`gate-pool`、`shell-adapter`、
  `tool-invocations run` 补账；这类 telemetry-wrapped 命令只能证明命令被补账，
  不能清空 raw shell gap。
- 脚本生成的状态、telemetry、lock、coord session、trace、doc-index、pycache 或 selftest
  artifact 必须有 owner command、allowed artifacts、cleanup/reconcile 命令和验证证据；
  能入 DB 的状态不得退回默认 JSON/配置文件。确需贴近 Git common dir 的底层锁文件必须
  在 owner command 中声明原因、迁移边界和后续 DB 化任务。
- 新项目初始化或重新安装治理框架时，安装器必须创建或更新根 `.gitignore` 中的
  `AI Client Governance generated runtime` managed block；日常审计使用
  `python .ai-client/ai-client-governance/scripts/ai_client_governance.py file-ownership audit --strict`
  统计 `.ai-client` 路径的追踪类别、忽略产物和违规 tracked live-state。发现脚本生成
  产物被 Git 追踪时，先修脚本/初始化/ignore 策略，再用 `git rm --cached` 解除索引追踪；
  不把本地 DB、日志或 worktree 作为宿主提交资产。closeout/host-closeout 只能刷新
  ignored DB 运行态，不能把 `.ai-client/project/state/` 或其他 live-state 通过
  `git add -f` 重新纳入宿主提交。
  规则/脚本任务缺少 `events.event_type=state-artifact-ownership.analysis` 时
  `task-record gate` 必须 fail closed；脚本生成的数据出问题时先修脚本或走脚本修复
  入口，不手工改运行态 telemetry、coord 或 lock 数据。
- 重要本地命令的强制适配入口是
  `python .ai-client/ai-client-governance/scripts/ai_client_governance.py shell-adapter run -- ...`。
  `shell-adapter` 会写入 SQLite telemetry，并在事件中记录 `adapter_enforcement`、
  `scope_kind`、`scope_reason` 和 task id。PowerShell 可用
  `shell-adapter profile-snippet` 或 `shell-adapter install-powershell --execute`
  安装 profile shim；profile shim 是显式适配器，不声称能拦截宿主客户端内部所有裸
  shell。收口诊断必须区分 shell-adapter auto-intercept、shell-adapter telemetry、
  telemetry-wrapped command 和 raw shell gap；需要强制覆盖时使用
  `task-run diagnose --require-raw-shell-coverage` 或
  `shell-adapter diagnose --require-auto-intercept` fail closed。
- 运行状态和资源遗漏检查使用
  `python .ai-client/ai-client-governance/scripts/ai_client_governance.py task-run diagnose ...`；
  它报告 execution telemetry 失败、重复终态命令、cache hit/miss、coord lock/session 和裸 shell
  自动拦截缺口，同时报告 task-record 与 task-queue 的数量差、当前任务是否两边都存在；
  可用 `--task-id`、`--trace-id`、`--since`、`--until` 收敛到当前任务。
  该差值是恢复和监控信号，不代表二者职责必须完全相同。
- 执行 telemetry 记录入口是
  `python .ai-client/ai-client-governance/scripts/ai_client_governance.py telemetry record ...`；
  命令只是 `span_kind=command`、`subject_type=command` 的一种载荷，模型 HTTP、子 AI、
  token usage 和外部 API 统计必须扩展同一 `execution_spans`/`execution_events` schema。
- 执行统计和数据分析入口是
  `python .ai-client/ai-client-governance/scripts/ai_client_governance.py telemetry report ...`；
  它读取 `execution_spans` 和 `execution_events`，统计 top operations、top subjects、span kind、
  subject type、重复执行、失败率、duration p50/p95/max、cache hit/miss、scope 分布和
  adapter enforcement 分布，并输出 OpenTelemetry/W3C Trace Context 风格的 trace context
  摘要。当前采用报告层映射，复用已有 `trace_id`、`span_id`、`parent_span_id`
  和 attributes，不为了报告先迁移 SQLite schema。
- 效果分析入口是
  `python .ai-client/ai-client-governance/scripts/ai_client_governance.py telemetry effectiveness ...`；
  它比较 before/after trace、task 或时间窗口，量化耗时、验证耗时、命令数、失败率、
  cache hit/miss、重复 subject 和 gate/completion/final-gate 数量差异。
  新增模型 HTTP、子 AI、token usage 或外部 API 调用统计时，必须扩展同一 telemetry
  span/event 模型，不能再新增并行日志体系。
- `task-queue lifecycle` 是 task queue 与 structured task record 的只读统一生命周期视图：
  queue `completed` 与 task record `done` 都归一为 lifecycle `done`，并报告缺失、状态漂移
  和 trace_id 漂移；它不隐式写回任一事实源。
- 设计新的治理执行结构、缓存策略或观测模型前，必须先联网核对官方或一手资料，
  并在 task record 记录来源、采用结论和不采用边界。
- 治理节点采用强制执行单元模型，至少声明 `id`、`phase`、`events`、
  `condition`、`requires_facts`、`produces_facts`、`effect`、`fail_policy`、
  `dedupe_key` 或 `gate_step`、`performance_budget`。`skill` 只能作为 capability
  plugin 被路由和调用，不能替代审批、worktree、测试、Git 边界或输出门禁。
- 节点事件语义固定：用户输入走 `user-message`，计划输出走 `plan-output`，
  状态回复走 `status-output`，写入前走 `write-intent`，修改后走 `after-change`，
  完成测试走 `completion-test`，最终回复走 `final-output`，恢复任务走 `resume`，
  合并清理走 `merge-cleanup`。
- `gate-pool` 以 `gate_step` 聚合同类门禁；多个组件指向同一 `gate_step` 时，
  应聚合 changed paths 后运行一次，并在 dry-run、trace 或结构化 task record 中可见。

## 结构化任务记录、Task Tracking 与恢复现场

- 中/大任务、修改型任务、规则/脚本/skill、文档、Git、简历、long-running、
  multi-agent 或 correction 任务必须有结构化 task record。
- 新任务的机器事实优先写入 `.ai-client/project/state/aicg.db`。入口是
  `python .ai-client/ai-client-governance/scripts/ai_client_governance.py task-record ...`。
- 执行前必须先用 `contract describe` 明确本任务必填字段、枚举、gate 和
  写入命令；不要靠最终 `task-gate` 从 Markdown 里反推缺什么。
- 中/大型或修改型任务还必须写入 `events.event_type=command-compression.analysis`。
  该事件记录本轮候选命令的压缩决策、去重数量、并行/顺序分组、telemetry 策略和是否选择
  gate-pool/task-run 本地路径。
- `task-record apply --json <file>` 是结构化写入入口；它必须在落库前校验
  `tasks`、`requirements`、`triggers`、`outputs`、`events`、`worktrees`、`validations`
  的必填字段、枚举和外键。校验失败不得生成半成品记录。
- `task-gate --task-id <task-id>` 和 `session-gate --task-id <task-id>` 读取
  SQLite 事实源；Markdown task tracking 只作为历史审计和 `task-record export-md`
  生成的人类可读报告，不作为新任务机器门禁输入。
- task tracking Markdown 导出或历史记录放在 `.ai-client/project/records/task-tracking/`。
- pending 恢复入口放在 `.ai-client/project/records/pending-tasks/`。
- correction 记录放在 `.ai-client/project/records/corrections/`。
- 运行态、日志和 telemetry 放在 `.ai-client/project/state/`、`.ai-client/project/logs/`
  和 `.ai-client/project/tmp/`，不写回通用仓库。
- 结构化 task record 至少记录：用户输入拆解、用户要求、触发日志、任务类型、
  worktree 证据、Worktree 完成记录、影响面、操作 telemetry、验证记录、DoD、Git 状态
  和恢复现场。
- 发现“设计不好但需要框架级改造窗口才能统一处理”的问题时，写入
  `framework-debt` 表，而不是散落在对话或临时注释里。入口是
  `python .ai-client/ai-client-governance/scripts/ai_client_governance.py framework-debt ...`；
  记录至少包含问题、影响、期望改法、为什么需要框架改造、当前 workaround、
  下次触发条件和关联任务。
- 计划、恢复或收口治理架构任务时，必须让开放架构债可见：使用
  `framework-debt report` 或由 `gate-pool` 自动触发的 `framework-debt` report-only gate
  汇总 P0/P1 和当前范围相关项。已修复或被新架构替代的 debt 必须及时标记为
  resolved/rejected/deferred；在专用清理命令实现前，用 `add --replace` 更新状态。
- 多分支任务必须记录 `## 主任务分支状态门禁`，覆盖每个分支的状态、证据和下一步。
- 模板由程序输出：
  `python .ai-client/ai-client-governance/scripts/ai_client_governance.py templates task-tracking`。

## 任务类型门禁

- 每轮执行和收口前必须选择实际命中的任务类型：
  `code-debug`、`correction`、`rules-script`、`docs`、`git`、`frontend`、
  `resume`、`multi-agent`、`long-running`。
- 任务类型不是标签装饰；选中后必须在结构化 task record 写证据。
- `rules-script` 默认要求联网核对外部成熟做法；不能只口头声称参考过。
- 任务门禁入口：
  `python .ai-client/ai-client-governance/scripts/ai_client_governance.py task-gate ...`。
- session 收口门禁入口：
  `python .ai-client/ai-client-governance/scripts/ai_client_governance.py session-gate ...`。
- 门禁脚本只读检查，不得自动修改规则入口 adapter、README、pending、
  corrections 或 Git。
- 门禁失败时，先修证据、修脚本能力或记录阻塞，不得带失败门禁发送完成态回复。

## 用户要求、触发日志与输出门禁

- 收到用户输入后，先记录 `## 用户输入拆解门禁`：原始输入或最新指令、拆出的
  任务数/要求数，以及逐 REQ 表。逐 REQ 表必须至少包含 `REQ ID`、用户要求摘要、
  记录判定、联网/搜索判定、子 AI/验证判定和验收/最终回复覆盖口径。
- 对新任务，机器事实必须由 `lifecycle input-filter` 或等价 AOP/filter-chain 前置步骤写入
  SQLite：至少包含逐 REQ 行、`trigger_type=user-message` 或 `input-filter` 的触发日志、
  `event_type=input-filter.preflight` 和需要时的 `event_type=command-compression.analysis`
  事件行。只在对话里分析或只写 Markdown 不满足 preflight。
- `task-gate` 必须按表格行解析输入拆解门禁，并与 `## 用户要求追踪门禁` 的 REQ
  行交叉校验；散文式“记录/搜索/验证”关键词不能代替逐 REQ 判定。
- 用户输入中出现“联网、搜索、查、核对、最新、资料、URL、引用”等要求时，必须在
  输入拆解和触发日志中登记，后续验证记录要说明已联网、无需联网或阻塞原因。
- 用户新增要求、纠正要求、批准计划、改变优先级或询问是否实现时，必须登记到
  `## 用户要求追踪门禁`。
- 每条要求使用稳定 ID，记录状态、动作、实现证据、验证证据和最终回复覆盖口径。
- 同一轮多个要求不能只处理最近一条；最终回复前逐条回看。
- 触发用户要求、任务类型、安全要求、脚本能力适配、命令压缩、上下文压缩、脚本 telemetry
  或最终门禁时，必须在 `## 要求触发日志` 记录 TRG 行。
- 最终回复前必须用 `## 输出信息门禁` 或等价记录覆盖已完成项、未处理项、
  未验证项、阻塞项、active pending、Git/worktree 状态和下一步。
- 修改型任务的输出门禁必须提示 worktree 是否完成、是否已合并、是否 stage/commit、
  是否 push、是否已清理 worktree 目录和本地任务分支；一般不会自动合并时需要用户确认
  的下一步。

## 脚本与包结构

- `ai-client-governance` 的 Python 代码采用包结构：`src/ai_client_governance/` 是唯一实现层。
- `scripts/` 只保留一个公开入口：
  `python .ai-client/ai-client-governance/scripts/ai_client_governance.py <command> ...`。
- 宿主项目根目录的 `scripts/ai_client_governance.py` 不是合法 fallback；运行器只能解析
  嵌入式 `.ai-client/ai-client-governance/` 或治理仓库自身的 `scripts/` 入口。
- 新增能力不得再创建 `scripts/codex_*.py`、`scripts/validate_*.py`、
  `scripts/agent_*.py` 这类平铺旧入口；发现旧入口属于当前改动范围时直接移除或迁移。
- 通用脚本默认使用 Python 标准库实现；PowerShell/Bash 只作为 wrapper 或平台入口。
- 新增或修改脚本后，必须验证 `--help`、一个真实成功路径、必要的失败/警告路径，
  并运行 `python .ai-client/ai-client-governance/scripts/ai_client_governance.py selftest` 覆盖黑盒强制行为。
- 脚本能力不支持当前目标时，先记录 `## 脚本能力适配门禁`；不得手工改运行态、
  锁、telemetry 或派生报告来伪造脚本能力。
- 修改长文件、热点治理文件或多 worktree 可能同时修改的文件前，必须先做 patch
  preflight：用 `rg`/`context-extract` 确认锚点唯一或重新提取更窄上下文，小步应用
  patch，并记录 `events.event_type=patch-preflight.analysis`。缺少该事件时，
  规则/脚本、docs 或 correction 任务的 `task-record gate` 必须 fail closed。
- Python 运行产生的缓存必须重定向到 `.ai-client/project/cache/python-pycache`；selftest
  或隔离测试可把同类路径重定向到自己的 run directory，并在 artifact manifest 中声明。
- selftest 或隔离测试需要写治理状态时，必须用 `AICG_STATE_DB` 或显式 `--db` 指向
  run directory 内的临时 SQLite；不能为了测试方便把默认 DB 降级成 JSON/JSONL 状态源。
- 文本文件必须用 UTF-8；JSON 不应带 UTF-8 BOM。
- 编码验证入口：
  `python .ai-client/ai-client-governance/scripts/ai_client_governance.py validate-encoding ...`。

## 文档、引用与交付

- 新文档、重构、README/索引/引用维护必须做影响面扫描、循环引用检查和 DoD。
- 功能、脚本、规则、skill、manifest 或入口 adapter 改动后，必须进入 post-change
  文档影响面节点：判断是否需要同步 README、命令说明、manifest、规则入口、索引或引用记录。
- 如果影响文档，必须先更新相关文档和引用；如果判断不影响，必须在结构化 task record
  记录 no-impact 理由，不能静默跳过。
- 文档影响面和引用反查默认由 `gate-pool` 聚合触发一次
  `ai_client_governance.py doc-index check --changed-path ...`；不要对同一 changed-path 集合重复跑同一节点。
- 文档引用图入口：
  `python .ai-client/ai-client-governance/scripts/ai_client_governance.py doc-index ...`。
- 文档任务验证入口：
  `python .ai-client/ai-client-governance/scripts/ai_client_governance.py validate-doc ...`。
- 仓库内部 Markdown 链接使用相对路径，不写本机绝对路径。
- 外部运行目标、日志、构建输出和临时验证根等复现证据必须记录真实路径。

## Git 边界

- 用户只要求 commit 时默认只本地 commit，不 push。
- 只有用户明确说 push、推送或提交并推送时，才允许 `git push`。
- `worktree-task closeout-all` 只能做本地收口：merge、移除 worktree、删本地任务分支、
  刷新状态和提交宿主 gitlink/state；它不能包含 `--push` 或执行 `git push`。
  closeout 后需要推送时，必须作为单独步骤重新说明远端、分支和风险并获得明确批准。
- stage/commit/push 前仍要遵守审批和 worktree 规则。
- 工作区有无关脏改动时，只 stage 本次任务相关文件。
- commit 后检查提交号、最新提交信息和剩余工作区状态。
- 多个 worktree 或子任务修改同一热点文件时，进入 integration queue，
  由单一整合者合并、记录冲突矩阵和验证结果。

## 子 AI 协作

- 大任务或用户明确要求多 AI 分工时，总控先拆任务树、写范围和验证边界。
- 子 AI 数量不使用固定小上限；由任务树叶子数、写范围冲突矩阵、上下文复用收益、
  验证风险和宿主客户端并发能力共同决定。能并行的独立叶子可以继续拆分；一旦写范围
  重叠、复用命中低、验证成本超过收益或用户要求收束，必须收敛到更少 agent 或主线程整合。
- 创建新子 AI 前必须先做 `agent-context-reuse` 判定：同一 task id、同一写范围或相邻只读
  问题、已有 agent 保留关键上下文、heartbeat 新鲜且无污染风险时，优先复用该 agent 的上下文
  继续 `send_input`；跨任务、跨安全边界、旧 UI 残留、事实来源不明或上下文已经污染时，不得复用，
  只能创建新 agent 或回到主线程读取结构化事实源。
- Agent Brief 或等价短输入包必须写明 `context_reuse`：复用/新建/关闭决策、reuse key、
  已保留事实、必须跳过的重复输入、最小恢复读取清单、压缩摘要路径、token usage 来源或代理指标。
- 子 AI 完成时必须产出可复用 context capsule：任务结论、已读文件、稳定事实、未决问题、
  artifact、验证结果、不能复用的污染点和下一次最小提示；后续 agent 只读取 capsule 和必要行号，
  不重复灌入完整历史。
- reuse key 至少包含 `task_id`、scope、role 和上下文版本；复用 TTL 默认只在当前任务和新鲜
  heartbeat 内有效，跨任务、跨安全边界、跨 worktree 或出现 prompt injection/敏感信息/错误事实污染时
  必须失效。没有真实 token usage 时，必须记录 brief 行数、预计读取行数、必读文件数、跳过输入数等
  代理指标，不能声称精确节省 token。
- 创建子 AI 前必须准备 Agent Brief 或等价短输入包。
- Agent Brief 模板由程序输出：
  `python .ai-client/ai-client-governance/scripts/ai_client_governance.py templates agent-brief`。
- 文件型通信总线入口：
  `python .ai-client/ai-client-governance/scripts/ai_client_governance.py agent-comm ...`。
- 状态看板入口：
  `python .ai-client/ai-client-governance/scripts/ai_client_governance.py agent-groups ...`。
- 多 agent 写入前必须登记 write scope 并获取锁。
- 子 AI 用于测试门禁时，不能只抽查单点；必须记录 `## 子 AI 验收矩阵` 的结构化
  表格行，覆盖本轮全部触发子 AI 的 REQ、输入门禁、输出门禁、任务类型门禁、
  Git/worktree 门禁、失败路径、成功路径、发现问题和修复复测。
- `task-gate` 必须按矩阵行校验覆盖的 REQ、门禁、失败路径、成功路径和修复复测；
  一句话式“全面覆盖、失败成功均通过”不能替代矩阵。

## Corrections 与规则自迭代

- 用户指出 AI 助手漏处理、错做、违反规则、验证不足或流程失效时，默认按高严重度
  correction 处理，除非用户明确说只是轻微问题。
- correction 独立文件是事实源；`index.md` 是派生汇总。
- 新增或更新 correction 后必须同步独立文件、索引、当前 tracking 和返回动作。
- correction 模板由程序输出：
  `python .ai-client/ai-client-governance/scripts/ai_client_governance.py templates correction`。
- 扫描入口：
  `python .ai-client/ai-client-governance/scripts/ai_client_governance.py scan-corrections ...`。
- 不把一次性用户原话直接堆进规则入口 adapter；先沉淀 correction，再判断是否
  升级规则、脚本、skill、manifest 或 README。

## Skills

- 通用 skill 事实源在 `.ai-client/ai-client-governance/skills/`。
- 项目特化 skill 放 `.ai-client/project/skills/`。
- 目标项目原生 skill 优先级最高；同名冲突必须记录，不能静默覆盖。
- 修改任意 skill 后必须运行对应 skill 校验；带脚本的 skill 还要跑最小真实用例。

## 收口

- 收口前按任务类型运行 gate pool 或等价门禁：
  `python .ai-client/ai-client-governance/scripts/ai_client_governance.py gate-pool ...`。
- 声称任务完成前必须运行或记录 completion test 节点：
  `python .ai-client/ai-client-governance/scripts/ai_client_governance.py completion-test ...`。该节点根据
  changed paths、任务类型和验收标准生成测试计划；规则/脚本变更至少覆盖
  runtime components、gate-pool dry-run、task-run plan/run/diagnose 和 focused regression；
  worktree/coord 变更还要覆盖 `worktree-task reconcile --strict`。
- `completion-test` 必须携带验证预算：默认优先 fast/focused changed-surface
  检查，完整 selftest 只在 full profile、高风险/发布级变更或显式升级预算时强制。
  如果 required checks 超出预算，先缩小范围、拆任务或显式升级，不允许反复补跑
  expensive checks 直到收口。
- `lifecycle preflight` 和 `gate-pool` 命中 `analysis-contract` 组件时，必须 fail closed
  地要求 `analysis-summary`、`analysis-scope`、`non-goal`、`risk`、`acceptance` 和验证预算；
  final-output 的 completion-test 也要复核该契约。`completion-test` 和 `telemetry report`
  必须暴露计划慢项、实际最慢 validation/completion/final-gate span 和预算压力，避免只用
  “跑了很多测试”替代耗时根因分析。
- Git push 状态不是定时任务；它属于输出边界审计。计划、状态和最终答复前只报告
  dirty/ahead/behind/push 边界，不自动 push，除非用户明确批准推送。
- 规则/脚本/文档链路变更后，最终记录必须覆盖治理节点是否可见、门禁是否执行、
  是否发生去重、是否有跳过理由和是否存在明显性能问题。
- 规则/脚本强制执行能力变更后，收口前必须运行：
  `python .ai-client/ai-client-governance/scripts/ai_client_governance.py selftest --root <target-project>`。
- 通用 execution telemetry 记录入口：
  `python .ai-client/ai-client-governance/scripts/ai_client_governance.py telemetry record ...`。
- 命令适配器入口：
  `python .ai-client/ai-client-governance/scripts/ai_client_governance.py tool-invocations run/record ...`。
- 执行统计分析入口：
  `python .ai-client/ai-client-governance/scripts/ai_client_governance.py telemetry report ...`。
- 调用链路报告入口：
  `python .ai-client/ai-client-governance/scripts/ai_client_governance.py tool-flow ...`。
- 最终回复必须说明：本轮任务完成状态、原主任务状态、active pending、验证结果、
  Git/worktree 状态、worktree 是否完成、是否合并/提交/push、未处理项和下一步。
