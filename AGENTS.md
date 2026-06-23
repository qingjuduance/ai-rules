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
- **默认 DB 路径归宿主项目**：从嵌入式 `.ai-client/ai-client-governance/` 仓库或
  `.ai-client/project/.worktree/<task-slug>/` 任务 worktree 中运行治理 CLI 时，未显式传
  `--db` 的结构化状态命令必须解析到宿主项目 `.ai-client/project/state/aicg.db`。
  不能在治理仓库或任务 worktree 内部自动创建新的 `.ai-client/project/state/aicg.db`；
  隔离测试只能通过 `AICG_STATE_DB` 或显式 `--db` 写到声明的 run directory。
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
  `.clinerules/`、`.windsurf/rules/*.md`、`.continue/rules/`、`.roo/rules/`、
  `.trae/rules/*.md`、`.codebuddy/rules/*/RULE.mdc`（CodeBuddy；无 `CODEBUDDY.md`
  时根 `AGENTS.md` 会被自动加载）和 `CONVENTIONS.md`；具体以目标工具官方文档和目标项目已有文件为准。
- `ai-client-governance` 的通用规则事实源是嵌入式
  `.ai-client/ai-client-governance/AGENTS.md`；旧 `.codex/ai-client-governance/`
  不是迁移期路径，也不是 fallback。这里的 `AGENTS.md` 是入口适配文件名，
  不代表框架只服务 Codex 或 AGENTS 生态。
- 项目特有规则默认入口是 `.ai-client/project/rules/project/AGENTS.md`；如果项目
  未来改用其它内部事实源，必须在根入口 adapter、manifest 和安装配置中同步记录。
- 各工具入口 adapter 应保持薄层：只声明读取顺序、编码、同步检查和边界，不复制
  大段通用规则；能导入时优先导入，不能导入时用明确路径指向同一事实源。
- 安装器处理已有入口时必须先分类：项目原生规则默认保留；确认是旧
  ai-client 生成 adapter 或仍指向旧治理布局的专用 adapter 时，先备份再升级；
  无法确定归属的混合规则不得整文件覆盖。
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
- 能由代码、gate、adapter、policy 或 SQLite schema 强制的要求，必须先实现或登记
  对应 framework-debt/后续任务，再更新文档说明使用方法。文档不能替代强制执行；
  若本轮只允许写方案，task record 必须把 enforcement status 标成 `design_only`，
  并写明后续实现任务、验收任务和不能声称已强制生效的边界。
- 历史 Markdown、旧 pending、旧 corrections 汇总、聊天记录或导出的 task tracking
  只能作为审计和一次性迁移输入。当前任务列表、问题列表、worktree 状态和完成状态
  必须从 `.ai-client/project/state/aicg.db` 和 Git live state 重新查询；发现旧文档仍有
  “当前 active/pending/已完成”口径时，必须创建 cleanup 任务或在本任务中清理，不能继续
  让后续 AI 把它当作当前事实源。

## AI 生命周期强制流程

- 非纯只读小问答、修改型任务、规则/脚本/docs/git/correction/multi-agent/long-running
  任务必须按同一条生命周期执行，不能跳过到最终回复：
  1. `sync-check`：会话开始先运行同步检查，只审计不自动 pull/push。
  2. `lifecycle input-filter`：解析用户输入、REQ、触发器、scope、client/model identity、
     claim 验证、联网决策、agent 决策、readonly side-effect 和 shell proxy 计划。
  3. `task-queue enqueue/transition`：用户消息先进 queue，获得批准后进入 ready/active；
     一次只允许一个 active 根任务。
  4. `task-record apply`：把输入拆解、审批、事件、输出边界和后续 worktree/validation
     facts 写入 SQLite；修改型和中/大型任务缺 task record 时不得进入 write-intent。
  5. `worktree-task create`：写文件前创建任务 worktree；input-filter facts 可在
     worktree 前落库，写入证据和 final gate 不可缺 worktree。
  6. `lifecycle preflight` / `task-record gate --event preflight`：验证分析契约、
     command-compression、approval、scope、agent、history、readonly、shell proxy 等事实。
  7. `task-run plan/run`、`gate-pool`、`shell-adapter` 或 `tool-invocations run`：本地命令
     通过治理入口执行并写 telemetry；只读/验证可并行，状态变更、Git 写入、锁和
     task-record 写入必须顺序执行。
  8. `completion-test` 和任务特定验证：按 changed paths、任务类型和验收标准跑 focused
     checks，预算不足时拆任务或升级预算。
  9. `final-output` / `task-record gate --event final`：检查 REQ 覆盖、发现问题记录、
     worktree/Git/validation/telemetry/adapter facts 和输出边界；multi-agent 任务还必须
     有 reviewer Agent 对 executor Agent task id 的结构化通过/不通过结论。
  10. `worktree-task finalize` / `closeout-all` / `host-closeout`：只有用户明确要求合并、
      清理或 push 时进入对应链路；否则最终回复必须报告未合并、未 push 和保留原因。
  11. `task-queue transition --to done`：只有 task record 存在、final gate 通过且 live
      worktree 状态已复核后，才能把 queue 推到 completed/done。
- 生命周期每一阶段都要写入或引用结构化事实。没有事实时后续 gate 必须 fail closed；
  AI 的口头总结、Todo UI、Markdown tracking、裸 `git status` 或一次性截图不能替代
  `.ai-client/project/state/aicg.db`、Git live state 和 telemetry。
- `task-queue` 与 `task-record` 不能分叉收口。若 `task-queue lifecycle` 报告
  `missing_in_task_record`、状态漂移、trace 漂移或 current task 不存在，不能声称任务完成；
  先补齐 task record 或记录阻塞，再进入 final-output。
- 用户要求“先写方案，让另一个 AI 实现，再由第三方验收”时，当前任务仍然必须落入
  生命周期：方案作为 design package 写入 task record，至少包含问题、目标、非目标、
  架构、数据模型、policy/gate、迁移/清理、验证计划、风险、implementation tasks、
  reviewer acceptance criteria 和 handoff capsule。executor Agent 必须读取 design package，
  reviewer Agent 必须用独立 validation facts、live state 和通过/不通过结论核对；不能只凭上一位
  Agent 的总结验收。
- reviewer Agent 必须按 executor Agent 的 task id 或 leaf id 判断当前是否通过，记录
  `agent-review-result.analysis` 结构化事实：至少包含 reviewer agent/client_type、
  executor agent/client_type、reviewed task id 或 leaf id、pass/fail 结论、生命周期事实核对、
  提交状态核对、未处理项、处理不佳项、证据、整改建议、复测计划和复测结果。
  生命周期事实核对必须读取结构化事实和 Git live state，至少覆盖 task queue lifecycle、
  task record status、requirements/triggers/outputs/worktrees/validations/events 行数和状态、
  final gate、worktree 路径、分支、HEAD、dirty 状态、commit/merge/push 状态、验证结果、
  telemetry/raw shell gap 和 active/pending 状态。缺任一关键事实或发现记录状态与 live state
  冲突时，结论必须为不通过或阻塞，不能只凭 executor Agent 总结判断完成。
  结论为不通过、存在未解释的未处理项或整改复测未通过时，不能进入 merge/closeout，也不能把
  root task 标记为 done；只能退回 executor Agent、登记 follow-up task 或记录阻塞。
  `task-gate` 和 `task-record gate --event final` 必须自动检查该结构化事实；在
  `worktree-task closeout-all`、`host-closeout` 和 agent 看板尚未接入前，这些收口/展示点
  必须标为后续实现范围，不能声称已经全链路强制生效。
- 设计包、执行任务和验收任务要用同一个 root task 或明确 parent/child 关系连接
  `tasks`、`requirements`、`framework-debt`、`corrections` 和 worktree rows。
  不再把大量临时字段塞进 agent brief 或 AGENTS 散文；agent brief 只保存最小上下文、
  scope、禁止路径、return capsule 和验证命令，结构化事实归 DB。

## 任务执行子生命周期

任务从 `active` 到 `done` 的执行阶段，每一步都必须有结构化证据：

1. **worktree 就绪**：确认 worktree 已创建、分支正确、Git 状态 clean。多 worktree
   并行前必须写入冲突矩阵：写范围不重叠可并行，重叠时通过 `worktree-coord` 记录锁。
2. **命令压缩分析**：中/大型或修改型任务必须先运行 `command-compression.analysis`，
   去重、合并、标记只读/并行/顺序组。小型修改不能跳过此节点。
3. **命令执行治理入口**：
   - 只读和验证命令通过 `task-run run` 或 `gate-pool` 的可并行组执行，支持缓存。
   - 状态变更、Git 写入、锁和 task-record 写入必须顺序执行且不可缓存。
   - 所有命令必须通过 `shell-adapter proxy-powershell` 代理，禁止裸 shell。
   - 高风险 inline PowerShell 不能继续依赖人工转义经验；代理默认应把复杂 inline
     命令重写为临时 UTF-8 command file 执行，或在明确要求时 fail closed。
   - 每次调用写入 `execution_spans`/`execution_events` telemetry。
4. **文件修改前后**：
   - 修改前运行 `lifecycle preflight`（分析契约、approval、scope、shell proxy 等）。
   - 修改后运行 `policy assess` 检查安全风险、`patch-preflight.analysis` 确认锚点唯一。
   - 每步失败必须写入 `command-error.analysis` 事件，不能只在对话里记录。
5. **验证执行**：按 `completion-test` 规划的检查项运行，区分 fast/full profile。
   验证结果写入 `validations` 表，每条至少包含 command、cwd、result、summary。
6. **commit 收束**：Git stage/commit 必须通过 `task-run run` 或 `tool-invocations run`
   治理链路；裸 git 命令只作为 break-glass 并记录 `raw_git_write_exception.analysis`。
   commit 在 worktree 分支上，不自动合并到 main。
7. **状态刷新**：每次修改后刷新 task record 中的 worktree 状态行、HEAD（`*_at_snapshot`
   语义）、dirty 状态；`worktree-task reconcile --strict` 做 live-state 对账。
8. **post-change 文档影响**：功能/脚本/规则修改后必须进入文档影响面节点，判断是否需同步
   README、manifest、引用记录；不影响的写明 no-impact 理由。

## 任务检查子生命周期

任务从 `candidate` 到 `done` 的每个门禁检查点，任意一个 fail closed 即阻塞：

1. **input-filter.preflight**：非纯只读小问答必须记录逐 REQ 行、触发器、
   client/model identity、user-claim-validation、agent-decision、
   data-confirmation 和 shell-proxy-usage 事实。
2. **task-record gate --event preflight**：验证 tasks/requirements/triggers/outputs/events
   表完整；中/大型或修改型任务缺以下任意事件则 fail closed：
   `command-compression.analysis`、`scope-classification`、
   `plan-approval-boundary.analysis`、`analysis-contract.preflight`
   （含 summary/scope/non-goals/risks/acceptance）、`patch-preflight.analysis`
   （规则/脚本/docs/correction 任务）。
3. **task-record gate --event final**：验证 output 类型完整、requirements 全部关闭、
   worktree 证据存在、validations 至少一条 pass、rules-script 有 approval、
   docs 有 validate-doc/doc-index、resume 有 PDF 检查。
4. **task-gate**：按实际命中的任务类型（code-debug/correction/rules-script/docs/git/
   frontend/resume/multi-agent/long-running）执行对应门禁，缺证据即 fail。
5. **session-gate**：会话收口门禁，检查 task-gate 已通过、task record 存在、
   worktree 已复核、telemetry 覆盖、raw shell gap 已记录或补偿。
6. **completion-test**：按 changed paths 和任务类型规划验证检查项，fast profile 预算
   90s、full profile 预算 600s；预算不足时拆任务或升级。慢检查归因和冗余检测写入
   `validation_attribution`。
7. **telemetry 归因**：`telemetry report` 输出 top operations、slowest spans、
   cache hit/miss、重复 subject、adapter enforcement 分布、命令失败分类、
   未分类失败数、需要 command file 的失败数和 inline command warning；`task-run diagnose`
   暴露失败 telemetry、重复命令、missing worktree session、command-file-required
   failures 和 raw shell gap。
8. **live gate 收口**：`worktree-task finalize --require-merged --require-no-task-worktrees`
   确认无残留 worktree；`file-ownership audit --strict` 确认 live-state 未被 Git 追踪；
   宿主 closeout 检查 submodule gitlink、task tracking 和宿主脏改动。

### 执行链路的门禁覆盖矩阵

| 生命周期阶段 | 执行子生命周期 | 检查子生命周期 |
|-------------|---------------|---------------|
| 输入 | — | input-filter.preflight |
| 入队审批 | — | task-queue transition |
| 规划写入前 | worktree 就绪、命令压缩 | task-record gate preflight、analysis-contract |
| 执行中 | task-run/shell-adapter/修改 | policy assess、patch-preflight |
| 验证 | completion-test 规划执行 | completion-test 输出、validations |
| commit | task-run 治理链路 | task-record gate final、task-gate |
| 收口 | worktree closeout、状态刷新 | session-gate、telemetry、live gate |

### 全局命令族与生命周期地图

后续 AI 处理 `ai-client-governance` 时，先按本地图定位能力归属，避免每轮重新扫描
整仓库、重复试错命令或把散文规则当成强制执行：

| 生命周期/问题域 | 事实源或代码域 | 首选命令族 | 收口证据 |
|---|---|---|---|
| 入口和同步 | `sync/`、根 adapter、manifest | `sync-check`、`rule-audit`、`runtime manifest-report` | 同步 warning/OK、adapter 边界、manifest 零漂移 |
| 用户输入和分析契约 | `lifecycle/`、`runtime/registry.py`、`records/task_record.py` | `lifecycle input-filter/preflight/finalize`、`contract describe` | REQ、claim、client identity、agent decision、analysis-contract 事件 |
| 队列与任务记录 | `records/task_queue.py`、`records/task_record.py`、`records/state_store.py` | `task-queue lifecycle/transition`、`task-record init/apply/gate/status` | queue 与 task-record 无阻断漂移、requirements 和 outputs 闭合 |
| worktree 与协作锁 | `worktree/task.py`、`worktree/coord.py` | `worktree-task create/status/reconcile/finalize/closeout-all/host-closeout`、`worktree-coord` | Git live state、coord session、lock、branch/merge/push 状态一致 |
| 命令规划和执行 | `runtime/task_run.py`、`runtime/shell_adapter.py`、`gates/policy.py` | `task-run plan/run/diagnose`、`shell-adapter proxy-powershell/diagnose`、`policy assess` | command-compression、policy、shell proxy、command-error telemetry |
| telemetry 和调用链 | `records/telemetry.py`、`records/tool_flow.py`、`records/tool_invocations.py` | `telemetry report/effectiveness/snapshot/trend`、`tool-flow`、`tool-invocations` | compact trace、失败分类、耗时归因、重复命令和 raw shell gap |
| 文档和引用 | `docs/doc_index.py`、`docs/validate_doc_task.py` | `doc-index check/build`、`validate-doc` | 目录事件冒泡、断链/入链/锚点、doc-impact 或 no-impact facts |
| 门禁和完成测试 | `gates/`、`validation/completion.py`、`validation/selftest.py` | `gate-pool`、`task-gate`、`session-gate`、`completion-test`、`selftest` | 聚合后门禁、验证预算、focused/full 检查和 selftest 结果 |
| 文件归属和安全边界 | `audit/file_ownership.py`、`gates/architecture_guard.py` | `file-ownership audit`、`architecture-guard`、`validate-encoding` | ignored runtime、无 tracked live-state、编码和架构边界通过 |
| corrections 和框架债 | `records/corrections.py`、`records/framework_debt.py`、`records/scan_corrections.py` | `corrections`、`scan-corrections`、`framework-debt list/report/add --replace` | correction 生命周期、开放 P0/P1/P2 debt 和 next trigger 可见 |
| 多 agent 协作 | `agents/comm.py`、`agents/group_status.py` | `agent-comm`、`agent-groups`、`templates agent-brief` | brief、reuse key、heartbeat、capsule、review pass/fail 结论 |

命令族使用原则：

- 先用 `python scripts/ai_client_governance.py --list` 确认入口存在；需要命令细节时用
  对应子命令 `--help`，不要猜参数顺序。发现 nested argparse 顺序问题时，把 root/global
  参数放在支持的位置，并把缺陷登记到 `framework-debt`，不要反复试错。
- 只读诊断、文档检查、manifest 检查、telemetry 报告和 status 查询可以并行；会写 DB、
  改文件、改 Git、改 lock/session、transition 或 closeout 的命令必须按生命周期顺序串行。
- 每次新增 CLI、runtime component、gate、状态表或 artifact 类型，都必须同步更新本地图、
  README、manifest 和 selftest/focused regression；如果只能先设计，必须登记
  `design_only` 或 framework-debt，不能声称已强制执行。
- 运行输出可能很长的命令时，优先选择 `--format json` 加 task/trace/time 过滤、`--top`
  或 compact 报告；读取长中文规则用 `context-extract` 或行号范围，避免终端截断造成
  token 浪费和漏读。

### 运行态产物生命周期

- 默认活体状态只写宿主项目 `.ai-client/project/state/aicg.db`。从治理仓库本身或
  `.ai-client/project/.worktree/<task-slug>/` 运行 CLI 时，不得在当前 cwd 下新建
  `.ai-client/project/state/aicg.db`；隔离测试必须显式使用 `AICG_STATE_DB` 或 `--db`。
- `tmp/`、`cache/`、`logs/`、`doc-index/`、`lifecycle/`、`agents/comm/groups/`、
  `agents/groups/`、`.worktree/` 和 Python `__pycache__` 都是运行态或临时产物。
  它们必须有 owner command、artifact manifest、allowed path 和 cleanup/reconcile 策略；
  未声明产物出现在任务 worktree 时是 dirty blocker。
- selftest、doc-index、gate-pool、completion-test 和临时探针不得为了方便写入源码目录。
  如果发现未跟踪 `.ai-client/`、DB、doc-index、pycache 或 lifecycle 产物，先定位 owner
  command，修默认路径或补隔离参数，再删除运行态；不能把它们 stage 成源码改动。
- `file-ownership audit --strict` 是宿主边界门禁；`git status` 只能告诉当前脏不脏，
  不能证明 `.ai-client` 运行态归属正确。

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
- `task-queue` 不提供默认 JSON 队列文件、heartbeat 文件，也不能通过 `--queue-file`
  读取旧队列；
  需要人读报告时使用 `status --format text/json` 输出到 stdout。
- 一次只允许一个 active task；插入任务完成后必须返回原主任务或记录阻塞。
- 这里的 active task 是治理事务边界，不是工作量上限。一个 active task 内部可以
  拆出多个子任务、多个 task-node、多个 worktree 和多个委派 Agent 分支并行执行；这些并行
  单元必须共享同一个根 `task_id` 或明确的 `parent_task_id`，不能为了并行而在
  task queue 中开启第二个 active task。
- 主任务必须记录内部并行拓扑：子任务/叶子节点、worktree slug、owner agent、写入范围、
  禁止路径、验证命令、return capsule、合并状态和最终整合者。没有这些事实时，最终输出
  不能把“多 worktree/多 agent 并行”说成已治理。

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
- 一个 active task 可以拥有多个 worktree，用于并行处理独立叶子任务或不同仓库/模块的
  写入面。每个 worktree 必须记录 `task_id`、`task_node_id` 或 leaf id、repo、slug、
  branch、base commit、owner、write scope、forbidden paths、validation command、
  commit/merge/push 状态和 return capsule。多 worktree 不是多个 active task；它们必须
  回到同一个主任务的 integration queue 或整合节点统一验收。
- 多 worktree 的写入范围必须先做冲突矩阵。范围不重叠时可以并行；范围重叠、热点文件
  相同、验证资源共享或顺序依赖不清时，必须通过 `worktree-coord` 记录锁、等待关系和
  单一整合者，不能让多个 agent 自行合并同一目标分支。
- 用户没有明确说“合并 worktree / merge / 收口合并”时，修改完成后默认只在任务
  worktree 上 commit，不能自动合并回 main 或执行 `worktree-task closeout-all --execute`。
  用户没有明确说“push / 推送 / 提交并推送”时，也不能把 worktree commit 推到远端。
  最终回复必须提示用户：worktree 已 commit、尚未合并、尚未 push，用户可以继续在该
  worktree 上测试效果；后续只有收到明确合并或 push 指令后才进入对应链路。
- task tracking 必须记录源仓库、worktree 路径、分支、基准提交和 `git status`。
- 一个 active 根任务可以有多个 worktree，但每个 worktree 都必须记录在同一个
  task record 或明确 `parent_task_id` 下。记录字段至少包括：`root_task_id`、
  `task_node_id` 或 leaf id、repo、slug、branch、base commit、owner agent、write scope、
  forbidden paths、validation command、return capsule、commit/merge/push 状态、integration
  owner、冲突矩阵和下一步。缺少这些字段时，final-output 不能声称多 worktree 已治理。
- 多 worktree 并行前必须记录冲突矩阵：写入范围不重叠时可并行；热点文件、验证资源、
  目标分支或顺序依赖重叠时必须通过 `worktree-coord` 记录锁、等待关系和单一整合者。
  子 agent 继续拆子 agent 时同样继承根 task id、父节点、scope、禁止路径和 return capsule。
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
- 客户端运行时治理的目标模型是“能力网关”，不是事后散文检查器。宿主客户端的
  文件写入、shell 执行、Todo 同步、审批捕获、委派 Agent 派发和最终回复必须逐步收束到
  `file-write-adapter`、`shell-exec-adapter`、`todo-adapter`、
  `approval-adapter`、`agent-dispatch-adapter` 和 `final-output-adapter`。
  每个 adapter 都必须有 schema 化输入、policy 决策和结构化结果，事件语义至少覆盖
  `capability.requested`、`capability.policy_decided`、
  `capability.executed`、`capability.blocked` 和 `capability.exception`。
- 能力网关分三层：Capability Adapter 层负责把宿主能力调用转成结构化 intent；
  Policy Gateway 层只查询 `.ai-client/project/state/aicg.db` 中的 task、worktree、
  approval、path ownership、agent decision、readonly side-effect 和 capability facts；
  Evidence/Gate 层把每次调用写成 OpenTelemetry-style span/event/attributes，并用
  task id、trace id、span id 串联到 final gate。最终回复不能再只问“是否遵守规则”，
  而要查对应 capability facts、telemetry、失败传播和 break-glass 记录是否存在。
- 能力网关设计借鉴但不照搬外部规范：MCP Tools 的 `name`、schema 和 call result
  适合作为 adapter 输入/输出形态；MCP 安全建议中的授权、审计和敏感操作保护
  适合作为 policy gateway 约束；OpenTelemetry trace span/event/attributes 和
  W3C Trace Context 适合作为 evidence 和 trace 传播模型；VS Code command/workspace API
  只说明客户端/扩展层可以包装能力调用，不能作为系统级强制拦截的依据。
- host-native 集成的边界必须保守：允许在 Codex、Trae、Claude Code、VS Code 扩展或
  MCP server 等客户端插件层包装 AI 发起的能力调用；禁止为了追求“100% 拦截”去修改
  用户电脑的全局 shell、环境变量、profile、注册表、PATH、执行策略、后台服务或系统命令。
  如果宿主客户端不提供调用前拦截能力，必须承认 raw capability gap，并通过 adapter
  wrapper、final gate、telemetry 和 explicit exception 暴露风险；不能用污染用户环境换强制。
- 非侵入性可操作边界：以下行为属于污染用户环境而禁止——修改 `$PROFILE`、
  `$PSModulePath`、用户或系统 `PATH` 环境变量、注册表 `HKLM`/`HKCU` 下的 shell
  相关键、`ExecutionPolicy` 的 `CurrentUser` 或 `LocalMachine` 范围、系统服务
  注册/启停、系统级计划任务、`.bashrc`/`.zshrc`/`.profile` 等 shell 初始化文件、
  全局 npm/pip/cargo 包管理器配置。以下行为不属于污染——在隔离 worktree 内创建
  治理脚本、在 `.ai-client/project/state/` 写入 SQLite 运行态、通过
  `shell-adapter proxy-powershell` 以 `-NoProfile -NonInteractive` 执行单次命令、
  在进程级设置环境变量。边界不明确的修改必须在 task record 写入
  `events.event_type=host-boundary-check.analysis` 并记录决策依据。
- Windows 上所有 AI 发起的 PowerShell 命令必须通过
  `python .ai-client/ai-client-governance/scripts/ai_client_governance.py shell-adapter`
  的 `proxy-powershell --powershell-command "..."` 执行，禁止通过宿主裸 shell 直接运行
  PowerShell。`shell-adapter proxy-powershell` 以 `-NoProfile -NonInteractive` 隔离
  运行，不触碰用户 profile 或全局状态，并写入 command-proxy telemetry span 使
  `raw-shell-coverage` 诊断可区分已治理命令和裸 shell gap。未经 proxy 的 PowerShell
  调用即使命令本身成功，也必须在 task record 中记录为 raw shell gap。
- Final gate 必须查询 telemetry capability facts，不能仅依赖 prose 声明：final gate
  必须检查 `.ai-client/project/state/aicg.db` 中是否存在对应 capability 事件的
  telemetry span。缺少 capability facts 时 final gate 必须 fail closed——散文规则声明
  如"已遵守非侵入原则""所有命令已通过 proxy 执行"不能替代 telemetry 证据，telemetry
  中缺少 `shell-adapter` span 且 task record 中没有 `shell-proxy-usage` 事件时，无论
  prose 如何声明都视为 raw shell gap。
- 输入过滤器负责拆分用户输入、识别要求数量、绑定逐 REQ 行和任务类型，并判断每条
  要求是否必须落盘、是否触发联网/搜索、是否触发委派 Agent 或黑盒验证。
- 用户输入是强制 `user-message` join point。非纯只读小问答在计划、写入、恢复或最终
  回复前，必须先运行 `lifecycle input-filter`，把 `requirements`、`triggers`、
  `outputs` 和 `events.event_type=input-filter.preflight` 写入结构化 task record；
  缺少这些事实时 `task-record gate --event preflight` 必须 fail closed。
- 同一输入过滤器还必须记录当前 AI 客户端类型和模型标识：
  `events.event_type=client-identity.analysis` 的 payload 至少包含 `client_type`
  和 `model_id`。宿主客户端无法暴露时写 `unknown` 并保留 `identity_source`，
  不能省略；后续 telemetry report 按客户端/模型聚合，用来发现哪些组合没有按标准流程执行。
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
  `approval_reason`，并附带统一 policy 事实：`policy_decision`、`policy_severity`
  和 `policy_findings`。`task-run run` 默认在 subprocess 前阻断 `block` 或未携带明确
  `--policy-approval-label` 的 `approval_required` 命令；启发式 capability 分类只能作为
  policy 输入，不能绕过审批、敏感信息、命令注入、供应链、Git 写入或删除风险门禁。
- 统一安全策略入口是
  `python .ai-client/ai-client-governance/scripts/ai_client_governance.py policy assess ...`。
  `security-policy` gate 只检查已进入治理路径的命令、文本或文件；宿主客户端内部裸 shell
  不属于本仓库可直接拦截的 runtime surface，必须通过 fail-closed 诊断、wrapper 执行
  或 task record 中的显式 bypass 风险记录治理。
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
- Windows PowerShell 重要本地命令的强制 raw-shell 覆盖入口是
  `python .ai-client/ai-client-governance/scripts/ai_client_governance.py shell-adapter`
  的 `proxy-powershell --powershell-command ...` 子命令。
  显式 `shell-adapter run -- ...` 只证明 wrapper telemetry，不等同于 raw-shell 覆盖。
  `shell-adapter` 会写入 SQLite telemetry，并在事件中记录 `adapter_enforcement`、
  `scope_kind`、`scope_reason` 和 task id。Windows PowerShell 强制代理必须使用
  no-profile command proxy：临时脚本、`-NoProfile`、不写用户 `$PROFILE`。
  raw shell enforcement 必须是 non-invasive、per-command、process-scoped：
  不修改系统或用户 `PATH`，不改 PowerShell profile，不执行 `Set-ExecutionPolicy`
  的 `CurrentUser` 或 `LocalMachine` 范围，不改注册表，不安装全局 shim/hook，
  不替换 `powershell.exe`/`pwsh.exe`，不常驻后台服务，不接管用户手动打开的终端。
  允许的执行方式只是在 AI 发起命令时启动临时 `-NoProfile`、`-NonInteractive`
  子进程，并将环境变量、执行策略和临时脚本限制在当前进程或当前任务临时目录中。
  Microsoft PowerShell 文档中的 `-NoProfile` 和 execution policy `Process` scope
  只能作为进程级隔离依据，不能被解释为允许修改用户级或机器级环境。
  `shell-adapter profile-snippet` 或 `shell-adapter install-powershell --execute`
  只属于用户显式批准的可撤销 profile shim，不得作为默认强制方案。收口诊断必须区分
  shell-adapter auto-intercept、no-profile command proxy、shell-adapter telemetry、
  telemetry-wrapped command 和 raw shell gap；需要强制覆盖时使用
  `task-run diagnose --require-raw-shell-coverage` 或
  `shell-adapter diagnose --require-raw-shell-coverage` fail closed。
- 使用 Windows PowerShell 代理时，简单命令可用 `--powershell-command`；复杂命令、
  管道、正则 `|`、变量、重定向、多命令组、here-string、嵌套引号、多行命令、inline JSON、
  `python -c` 或包含大量参数的命令必须走文件化执行，避免 shell 多层转义破坏参数。
  `shell-adapter proxy-powershell` 默认会对高风险 inline 命令生成临时 UTF-8
  `AicgUserCommand.ps1` 并通过 wrapper `-File` 执行，telemetry 中必须能看到
  `command_file_used=true` 和 `command_file_source=auto|provided`。需要强制阻断而不是自动重写时，
  使用 `--no-auto-command-file --fail-on-inline-risk`，风险命令必须在 subprocess 前退出。
  用户或脚本已经有稳定命令文件时，优先显式传 `--powershell-command-file`。命令文件是任务临时输入，
  不写用户 profile，不作为默认状态源；用完后按 artifact ownership 规则清理或声明保留原因。
  因工具限制暂时不能使用代理时，必须在 task record 写入
  `events.event_type=shell-proxy-usage.analysis`，记录 `exception_reason`、补偿验证和剩余
  raw shell gap；收口时如果记录 `used_proxy=true`，还必须写入 `telemetry_evidence` 或
  `proxy_invocation_id`，不能只在对话里解释。
- 多命令组必须 fail closed：任何关键命令失败后要立即退出或使用能传播失败的
  task-run/gate-pool 节点；不得让后续成功命令掩盖前序失败。发现代理、task-run 或
  closeout 脚本掩盖中间失败时，按高严重度 correction 处理并补回验证。
- 声明“只读”的任务必须写入
  `events.event_type=readonly-side-effect-policy.analysis`，明确
  `readonly_contract`、`db_write_allowed`、`record_state_allowed`、
  `side_effect_class` 和 `dry_run_supported`。`readonly_contract=true` 时不得运行
  `--record-state`、会写 `governance_state` 的 sync/status 命令或 telemetry 写入；
  如宿主工具本身会写运行态，必须先记录例外和隔离策略，不能把“只读查询”和
  “记录快照”混称为只读。
- 运行状态和资源遗漏检查使用
  `python .ai-client/ai-client-governance/scripts/ai_client_governance.py task-run diagnose ...`；
  它报告 execution telemetry 失败、重复终态命令、cache hit/miss、coord lock/session 和裸 shell
  自动拦截缺口，同时报告 task-record 与 task-queue 的数量差、当前任务是否两边都存在；
  可用 `--task-id`、`--trace-id`、`--since`、`--until` 收敛到当前任务。
  该差值是恢复和监控信号，不代表二者职责必须完全相同。
- 执行 telemetry 记录入口是
  `python .ai-client/ai-client-governance/scripts/ai_client_governance.py telemetry record ...`；
  命令只是 `span_kind=command`、`subject_type=command` 的一种载荷，模型 HTTP、委派 Agent、
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
  `telemetry effectiveness snapshot` 必须把可复用指标写入 `governance_state`，
  `telemetry effectiveness trend` 从 DB 快照中产出趋势；不能新增默认 JSON/Markdown 活体事实源。
  新增模型 HTTP、委派 Agent、token usage 或外部 API 调用统计时，必须扩展同一 telemetry
  span/event 模型，不能再新增并行日志体系。
- `task-queue lifecycle` 是 task queue 与 structured task record 的统一生命周期视图：
  queue `completed` 与 task record `done` 都归一为 lifecycle `done`，并报告缺失、状态漂移
  和 trace_id 漂移；`--fail-on-drift` 可作为只读强门禁。生命周期写入必须使用
  `task-queue transition --task-id <id> --to <status>` 这类显式命令，同步 queue 与
  task-record 并写入事件，不能由只读报告隐式写回任一事实源。
- `task-queue transition --to done` 是最终状态写入节点，不是口头完成按钮。执行前必须
  证明：task record 存在；对应 requirements 已 done/blocked/deferred/cancelled；
  final-output、git_worktree、validation、worktree 和 telemetry facts 已覆盖；live
  `worktree-task status --record-state` 或 `finalize --record-state` 最新；无未解释的
  dirty worktree、未跟踪产物、missing_in_task_record、policy-blocked 命令或 raw shell gap。
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
- 每个生命周期阶段都要产出明确 event/fact。至少需要：
  `input-filter.preflight`、`client-identity.analysis`、`user-claim-validation.analysis`、
  `data-confirmation.analysis`、`history-requirement-recovery.analysis`、`agent-decision.analysis`、
  `plan-approval-boundary.analysis`、`scope-classification` trigger、
  `command-compression.analysis`、`readonly-side-effect-policy.analysis`、
  `shell-proxy-usage.analysis`、`patch-preflight.analysis`、worktree row、validation row、
  final output row 和必要的 command-error/capability telemetry facts。
- `task-gate --task-id <task-id>` 和 `session-gate --task-id <task-id>` 读取
  SQLite 事实源；Markdown task tracking 只作为历史审计和 `task-record export-md`
  生成的人类可读报告，不作为新任务机器门禁输入。
- task tracking Markdown 导出或历史记录放在 `.ai-client/project/records/task-tracking/`。
- pending 恢复入口放在 `.ai-client/project/records/pending-tasks/`。
- correction 机器事实源是 SQLite `corrections` 表；
  `.ai-client/project/records/corrections/` 只放 `export-md`/`import-md` 生成或回灌的
  人类可读 `.md` 副本与历史。
- 运行态、日志和 telemetry 放在 `.ai-client/project/state/`、`.ai-client/project/logs/`
  和 `.ai-client/project/tmp/`，不写回通用仓库。
- 结构化 task record 至少记录：用户输入拆解、用户要求、触发日志、任务类型、
  worktree 证据、Worktree 完成记录、影响面、操作 telemetry、验证记录、DoD、Git 状态
  和恢复现场。
- 命令失败必须结构化记录，不能只在对话里承认“命令输错”。任一治理命令、shell 命令、
  Git 命令、验证命令或子 agent 命令出现非预期失败时，必须写入
  `events.event_type=command-error.analysis` 或等价 command-error fact，payload 至少包含：
  `failed_command`、`exit_code`、`phase`、`parser_or_shell`、`failure_category`、
  `root_cause`、`corrected_command`、`retry_count`、`dedupe_key`、`preventive_rule`、
  `telemetry_span_id` 或 telemetry 查询条件、是否影响仓库状态、是否需要 framework-debt。
  当前运行时分类至少包括 `python_c_inline_quoting`、`inline_json_quoting`、
  `powershell_inline_complex_command`、`argparse_usage_error`、`git_command_failed`、
  `working_directory_command_failed`、`powershell_command_not_found` 和
  `unclassified_command_failure`。分类不能只用于事后报告：需要 command file 的分类必须改用
  文件化命令或触发 fail-closed；`unclassified_command_failure` 必须保留为未识别盲区，
  不能被统计成已分类成功。同一 `dedupe_key` 重复出现时必须进入
  `task-run diagnose`/telemetry 报告或 framework-debt，作为命令错误拦截器和命令模板重构输入。
- `telemetry report` 和 `task-run diagnose` 是命令错误看板，不是大 JSON 倾倒入口。
  默认使用 `--task-id`、`--trace-id`、`--since`、`--until`、`--top` 收敛范围；
  `tool-flow --format json` 默认必须输出 compact invocation 并省略 raw payload，
  只有取证时才显式使用 `--include-raw-json`。发现报告输出被截断、raw payload 过大或
  latest spans 携带完整原始事件时，按 P0 命令效率问题处理。
- 裸 `git add`、`git commit`、`git merge`、`git push`、`git rm`、`git mv` 不是
  governance-native 写入。即使它们通过 `shell-adapter proxy-powershell` 执行，也只能说明
  shell 有 telemetry；stage/commit/merge/push 仍必须经过 `policy assess`、`task-run run`
  或更高层 `worktree-task`/`task-queue transition` 链路。直接裸 git 写命令只作为
  break-glass，必须记录 `raw_git_write_exception.analysis`、批准标签、原因、影响路径和
  后续补偿验证。
- 发现“设计不好但需要框架级改造窗口才能统一处理”的问题时，写入
  `framework-debt` 表，而不是散落在对话或临时注释里。入口是
  `python .ai-client/ai-client-governance/scripts/ai_client_governance.py framework-debt ...`；
  已登记的 P1 架构项必须继续从 DB 读 live status，不要把 AGENTS 散文当成完成态。
  当前已知 P1 包括：doc-index 事件冒泡自动写 task record/final gate 证据、
  design-package handoff/review workflow、runtime registry/manifest declarative generation、
  task queue/task-record lifecycle 进一步统一。已经有代码或 selftest 的项只能按
  `in_progress` 描述剩余 gap；尚未实现自动 gate 的项必须保持 `design_only` 或 open debt，
  不能因为写入规则就声称已强制生效。
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
  记录判定、联网/搜索判定、Agent/验证判定和验收/最终回复覆盖口径。
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
- 普通治理 CLI 命令即使从 `.ai-client/ai-client-governance/` 或任务 worktree cwd 运行，
  默认状态 DB 也必须落到宿主项目 `.ai-client/project/state/aicg.db`。如果发现当前
  worktree 内出现未跟踪 `.ai-client/project/state/aicg.db`，应视为脚本路径解析缺陷或
  漏传隔离 `--db` 的测试缺陷，先修代码/测试并删除该运行态产物，不能把它当作源码改动保留。
- selftest、doc-index、completion-test、gate-pool 或临时探针产生的 `.ai-client/project/tmp/`、
  `doc-index/`、`cache/`、`lifecycle/`、`__pycache__/` 等产物必须有 owner command、
  artifact manifest、allowed path 和 cleanup/reconcile 策略。产物出现在任务 worktree 中且
  未声明为允许 artifact 时，worktree 视为 dirty blocker；不能把带未跟踪运行态产物的
  worktree 标记为 completed。
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
- 文档修改采用目录事件冒泡：从 changed file 所在目录开始，检查同目录 README、
  `.references/`、上级 README/AGENTS、manifest/命令说明和跨目录入链；只有发现入链、
  断链、缺锚点或入口职责变化时才扩大到全局。冒泡结果必须写入 task record 的
  doc-impact 或 validation facts，不能只说“看过文档索引”。
- 文档引用图入口：
  `python .ai-client/ai-client-governance/scripts/ai_client_governance.py doc-index ...`。
- 文档任务验证入口：
  `python .ai-client/ai-client-governance/scripts/ai_client_governance.py validate-doc ...`。
- 仓库内部 Markdown 链接使用相对路径，不写本机绝对路径。
- 外部运行目标、日志、构建输出和临时验证根等复现证据必须记录真实路径。

## Git 边界

- 用户只要求 commit 时默认只本地 commit，不 push。
- 用户只要求修改、实现、提交或测试时，不等于批准合并 worktree 或 push；
  merge/closeout/push 必须有单独明确指令。允许在任务 worktree 中 commit，并在最终
  状态里报告分支、提交号、worktree 路径、“未合并”和“未 push”状态。
- 只有用户明确说 push、推送或提交并推送时，才允许 `git push`。
- `worktree-task closeout-all` 只能做本地收口：merge、移除 worktree、删本地任务分支、
  刷新状态和提交宿主 gitlink/state；它不能包含 `--push` 或执行 `git push`。
  closeout 后需要推送时，必须作为单独步骤重新说明远端、分支和风险并获得明确批准。
- stage/commit/push 前仍要遵守审批和 worktree 规则。
- 工作区有无关脏改动时，只 stage 本次任务相关文件。
- commit 后检查提交号、最新提交信息和剩余工作区状态。
- 多个 worktree 或子任务修改同一热点文件时，进入 integration queue，
  由单一整合者合并、记录冲突矩阵和验证结果。

## 多 Agent / 委派 Agent 协作

- 大任务或用户明确要求多 AI 分工时，总控先拆任务树、写范围和验证边界。
- 委派 Agent 协作允许多层级：总控可以派发父 agent，父 agent 可以继续拆出子 agent 或叶子
  worktree，但每一层都必须继承根 `task_id`、父节点 id、写入范围、禁止路径、验证预算和
  返回契约。子 agent 的再派发不能绕过主任务审批、worktree 规则、shell-adapter 规则、
  telemetry、task-record gate 或最终整合门禁。
- 多层 agent 的事实必须结构化记录：父子关系、context reuse 决策、brief/capsule 路径、
  heartbeat、owner、输入摘要、已读文件、产出 artifact、验证结果、失败传播和合并状态。
  只在聊天里说“让子 agent 做了”不满足治理要求。
- 中/大型、修改型、规则/脚本、correction、git/worktree、long-running 或用户曾明确强调
  需要多 AI 的任务，必须在 preflight 前写入
  `events.event_type=agent-decision.analysis`：至少包含 `agent_group_decision`、
  `spawn_count`、`no_spawn_reason`、`context_pack_ref` 和
  `data_confirmation_evidence`；`spawn_count=0` 时还必须包含
  `alternative_validation` 和 `residual_risk`。即使最终不创建委派 Agent，也必须记录不创建原因、
  替代验证、剩余风险和下一次触发条件；缺失时 `task-record gate --event preflight`
  fail closed。`multi-agent` 任务的 final gate 必须有 `spawned`、`reused` 或
  `merged` 证据，不能用 `required`、空原因或纯计划态冒充完成。
- correction、规则/脚本、多 agent 和 long-running 任务必须显式恢复历史用户要求，写入
  `events.event_type=history-requirement-recovery.analysis`：记录读取的历史来源、
  找回的高频要求、未采纳项和 no-action 理由。用户说“之前提过”“老是忘记”“都找出来”
  时，该事件必须覆盖当前可见对话、项目 chat 记录、corrections、pending/task records
  和 framework debt；找不到来源时要同时写 `no_history_source_reason` 和
  `no_action_reason`。有历史来源时 `recovered_requirements` 不能为空。
- 写入或执行前还必须写入 `events.event_type=data-confirmation.analysis`，把用户陈述、
  live state、历史记录、外部资料或委派 Agent 结论分开列为 `confirmation_sources` 和
  `checked_facts`；未确认项必须进入 `unverified_items`，不能把“用户说过”直接当作已证实事实。
- 委派 Agent 数量不使用固定小上限；由任务树叶子数、写范围冲突矩阵、上下文复用收益、
  验证风险和宿主客户端并发能力共同决定。能并行的独立叶子可以继续拆分；一旦写范围
  重叠、复用命中低、验证成本超过收益或用户要求收束，必须收敛到更少 agent 或主线程整合。
- 创建新委派 Agent 前必须先做 `agent-context-reuse` 判定：同一 task id、同一写范围或相邻只读
  问题、已有 agent 保留关键上下文、heartbeat 新鲜且无污染风险时，优先复用该 agent 的上下文
  继续 `send_input`；跨任务、跨安全边界、旧 UI 残留、事实来源不明或上下文已经污染时，不得复用，
  只能创建新 agent 或回到主线程读取结构化事实源。
- Agent Brief 或等价短输入包必须写明 `context_reuse`：复用/新建/关闭决策、reuse key、
  已保留事实、必须跳过的重复输入、最小恢复读取清单、压缩摘要路径、token usage 来源或代理指标。
- 委派 Agent 完成时必须产出可复用 context capsule：任务结论、已读文件、稳定事实、未决问题、
  artifact、验证结果、不能复用的污染点和下一次最小提示；后续 agent 只读取 capsule 和必要行号，
  不重复灌入完整历史。
- reuse key 至少包含 `task_id`、scope、role 和上下文版本；复用 TTL 默认只在当前任务和新鲜
  heartbeat 内有效，跨任务、跨安全边界、跨 worktree 或出现 prompt injection/敏感信息/错误事实污染时
  必须失效。没有真实 token usage 时，必须记录 brief 行数、预计读取行数、必读文件数、跳过输入数等
  代理指标，不能声称精确节省 token。
- 创建委派 Agent 前必须准备 Agent Brief 或等价短输入包。
- Agent Brief 模板由程序输出：
  `python .ai-client/ai-client-governance/scripts/ai_client_governance.py templates agent-brief`。
- 文件型通信总线入口：
  `python .ai-client/ai-client-governance/scripts/ai_client_governance.py agent-comm ...`。
- 状态看板入口：
  `python .ai-client/ai-client-governance/scripts/ai_client_governance.py agent-groups ...`。
- 多 agent 写入前必须登记 write scope 并获取锁。
- 委派 Agent 用于测试门禁时，不能只抽查单点；必须记录 `## 多 Agent 验收矩阵` 的结构化
  表格行，覆盖本轮全部触发委派 Agent 的 REQ、输入门禁、输出门禁、任务类型门禁、
  Git/worktree 门禁、失败路径、成功路径、发现问题、修复复测和执行客户端。
- `task-gate` 必须按矩阵行校验覆盖的 REQ、门禁、失败路径、成功路径和修复复测；
  一句话式“全面覆盖、失败成功均通过”不能替代矩阵。
- 多 Agent 执行链路还必须有 `## 多 Agent 审批结论` 或 DB 中
  `agent-review-result.analysis` 结构化事实，由 reviewer Agent 对每个 executor Agent 的
  task id/leaf id 说明当前是否通过。结论必须说明双方 client_type、哪些 REQ 未处理、
  哪些处理质量不足、生命周期 facts 和提交状态是否闭合、依据是什么、应如何修改以及
  复测命令或复核方式。结论为不通过、
  缺少整改建议、缺少复测结果或仍有 P0/P1 问题时，禁止合并对应 worktree，
  禁止 closeout-all，禁止把 root task transition 到 done。
- 已实现的机器检查至少覆盖：`task-gate` 对 `## 多 Agent 验收矩阵` 和
  `## 多 Agent 审批结论` 的表格校验、`agent-review-result.analysis` schema 校验、
  runtime component 注册、`task-record gate --event final` 对 multi-agent task id 的结论检查，
  以及 pass/fail、自带整改建议、复测通过/失败路径的 selftest。`worktree-task closeout-all`、
  `host-closeout` 合并前阻断和 agent-groups/agent-comm 看板展示仍属于后续接入点。

## Corrections 与规则自迭代

- 用户指出 AI 助手漏处理、错做、违反规则、验证不足或流程失效时，默认按高严重度
  correction 处理，除非用户明确说只是轻微问题。
- correction 事实源是 SQLite `aicg.db` 的 `corrections` 表（与 task record、framework-debt 一致）；
  `.ai-client/project/records/corrections/*.md` 只是 `corrections export-md` 生成或
  人类编辑后经 `corrections import-md` 回灌的派生/历史形式，不是机器事实源。
- 新增或更新 correction 必须写入 `corrections` 表（`corrections add`/`corrections init`）；
  已有 legacy `.md` 文件通过 `corrections import-md` 一次性导入并保持幂等。
  如果仍保留 `.md` 作为人类可读副本，必须以 `corrections export-md` 重新生成或
  使用 `corrections import-md --replace` 回灌，避免 DB 与 `.md` 漂移。
- correction 模板由程序输出，且字段与 `corrections` 表 schema 对齐：
  `python .ai-client/ai-client-governance/scripts/ai_client_governance.py templates correction`。
- 扫描入口（gate-pool `scan-corrections` 步骤直接复用该命令）：
  `python .ai-client/ai-client-governance/scripts/ai_client_governance.py scan-corrections ...`，
  该命令现在从 DB 读取，不再解析 `.md` 文件。
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
- 规则/脚本/manifest/README 改动后，必须运行或记录
  `python .ai-client/ai-client-governance/scripts/ai_client_governance.py runtime manifest-report --check-manifest`
  或等价 focused gate，证明 runtime registry、manifest 和 README 没有继续手工漂移。
- 规则/脚本强制执行能力变更后，收口前必须运行：
  `python .ai-client/ai-client-governance/scripts/ai_client_governance.py selftest --root <target-project>`。
  涉及 DB/doc-index 运行态路径、命令错误、shell-adapter、telemetry 或 tool-flow 的改动还必须覆盖
  `state-db-defaults-to-host-from-worktree-cwd`、`doc-index-defaults-to-host-from-worktree-cwd`、
  `command-error-taxonomy-and-compact-flow` 或等价 focused regression，证明任务 worktree cwd
  不会生成本地 `.ai-client/`，高风险 inline 命令会文件化或 fail closed，JSON 调用链默认 compact。
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
