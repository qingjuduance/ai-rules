# AI 执行流程通用框架

本文件是 `ai-rules` 的通用执行框架事实源，同时也是兼容 `AGENTS.md` 生态的
入口 adapter。框架规则不能绑定某一个模型、客户端或文件名；`AGENTS.md`、
`CLAUDE.md`、`GEMINI.md`、Copilot instructions、Cursor/Cline/Windsurf/Continue
rules 等都只是把同一套执行流程暴露给不同 AI 工具的入口适配层。

本文件只保留跨项目不可绕过的协作边界、生命周期和入口适配原则。可由程序检查、
生成或汇总的细节，优先放入 `src/ai_rules/` 包、`scripts/ai_rules.py`
统一入口、通用 skill、manifest 或 README。

## 读取顺序、入口适配与规则分层

- 当前 AI 工具自动加载的项目原生规则入口必须先视为项目入口。常见入口包括
  `AGENTS.md`、`CLAUDE.md`、`GEMINI.md`、`.github/copilot-instructions.md`、
  `.github/instructions/*.instructions.md`、`.cursor/rules/*.mdc`、
  `.clinerules/`、`.windsurf/rules/*.md`、`.continue/rules/`、`.roo/rules/`
  和 `CONVENTIONS.md`；具体以目标工具官方文档和目标项目已有文件为准。
- `ai-rules` 的通用规则事实源仍是嵌入式 `.codex/ai-rules/AGENTS.md`；
  这里的 `AGENTS.md` 是兼容文件名，不代表框架只服务 Codex 或 AGENTS 生态。
- 项目特有规则默认入口是 `.codex/project/rules/project/AGENTS.md`；如果项目
  未来改用其它内部事实源，必须在根入口 adapter、manifest 和安装配置中同步记录。
- 各工具入口 adapter 应保持薄层：只声明读取顺序、编码、同步检查和边界，不复制
  大段通用规则；能导入时优先导入，不能导入时用明确路径指向同一事实源。
- `.codex/rules/common/`、根 `scripts/`、顶层 `.codex/skills/` 的旧复制模型
  不再作为通用规则 fallback。
- 资产优先级固定为：
  1. 目标项目原生资产。
  2. `.codex/project/` 项目特化层。
  3. `.codex/ai-rules/` 通用层。
- 通用规则不得保存项目业务、学习路线、简历规则、源码快照、本地交付细节或
  某个 AI 工具私有的交互偏好。
- 项目规则不能放宽通用安全边界、审批流程、Git 边界和恢复现场要求。
- Windows/PowerShell 读取中文规则文件时，设置本次进程 UTF-8 编码，并使用
  `Get-Content -Raw -Encoding UTF8`；长文件优先用
  `python .codex/ai-rules/scripts/ai_rules.py context-extract` 摘录。

## 核心原则：流程化与可审计

- 一切可重复、易遗漏、会影响仓库状态或会跨会话协作的流程，都必须优先脚本化、
  状态化和门禁化；不能只依赖单次对话记忆或人工口头约定。
- 每个修改型任务必须留下可复盘证据：任务输入拆解、审批、worktree、写锁、
  操作账本、验证、提交、合并、清理和下一步状态，都要能被后续 AI 会话读取。
- worktree 创建、状态同步、收口检查和安全清理优先使用
  `python .codex/ai-rules/scripts/ai_rules.py worktree-task ...`；当前 worktree
  总览写入 `.codex/project/state/worktrees.json`，并在 task tracking 中引用。
  该文件是可提交的审计快照，活体状态以重新运行 `worktree-task status --write-state`
  为准；其中 HEAD 字段必须使用 `*_at_snapshot` 语义，避免提交快照本身推进 HEAD
  后造成误判。
- 多会话、多线程或多 worktree 修改同一范围时，必须通过 `worktree-coord` 的
  session、lock 和 integration queue 记录冲突、整合者、冲突矩阵和验证结果。
- 用户新增流程要求时，先判断是否应升级为脚本能力、生命周期组件、门禁或状态文件；
  默认不要只把要求写成散文规则。

## 会话同步

- 新会话必须运行 `.codex/ai-rules/check-ai-rules-sync.ps1` 或等价 wrapper。
- 跨系统事实逻辑优先来自
  `python .codex/ai-rules/scripts/ai_rules.py sync-check`。
- 每次会话都检查 `.codex/ai-rules/` 是否存在、是否为 Git 仓库、是否 dirty、
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
- 队列事实源是 `.codex/project/state/task-queue.json`，入口是：
  `python .codex/ai-rules/scripts/ai_rules.py task-queue ...`。
- 一次只允许一个 active task；插入任务完成后必须返回原主任务或记录阻塞。

## 强制 Worktree

- 所有修改型任务不论大小，正式改文件、移动文件、格式化、导出、运行写仓库脚本
  或提交前，必须先创建任务级 `git worktree` 和独立分支。
- 只读定位、读取规则、同步检查、状态检查、计划输出不要求 worktree；一旦要落盘，
  先进入 worktree。
- 任务 worktree 默认放在宿主项目 `.codex/project/.worktree/<task-slug>/`。
- 修改嵌入式 `.codex/ai-rules/` 时，从 ai-rules 仓库执行 `git worktree add`，
  目标路径仍放到宿主项目 `.codex/project/.worktree/<task-slug>/`。
- task tracking 必须记录源仓库、worktree 路径、分支、基准提交和 `git status`。
- 如果工具限制、路径冲突、分支冲突、权限或 Git 状态异常导致无法创建 worktree，
  必须停止修改并向用户确认处理方式，不能退回主工作区直接改。
- 跨 worktree 的 session、锁和 integration queue 使用
  `python .codex/ai-rules/scripts/ai_rules.py worktree-coord ...`。

## ai-rules 框架与生命周期组件

- `ai-rules` 是人和 AI 之间的执行框架，不只是规则文档。新增可确定的约束时，
  优先注册为输入过滤器、处理拦截器、输出拦截器、横切门禁或报告组件，而不是只追加散文。
- 输入过滤器负责拆分用户输入、识别要求数量、绑定逐 REQ 行和任务类型，并判断每条
  要求是否必须落盘、是否触发联网/搜索、是否触发子 AI 或黑盒验证。
- 处理拦截器负责审批、worktree、联网核对、task tracking、脚本能力适配和状态机。
- 输出拦截器负责最终回复覆盖、worktree 完成状态、未合并/未提交/未 push 边界和下一步提示。
- 横切门禁负责编码、文档引用、Git 边界、脚本账本、trace flow 和 correction 扫描。
- 非纯只读小问答，优先运行生命周期 preflight：
  `python .codex/ai-rules/scripts/ai_rules.py lifecycle preflight ...`。
- 收口前优先运行 lifecycle finalize，并根据任务类型触发已注册门禁：
  `python .codex/ai-rules/scripts/ai_rules.py lifecycle finalize ...`。
- 生命周期把输入来源区分为 `user`、`web`、`file`、`tool`、`agent`、`history`。
- 联网输入必须记录 URL 或资料路径；不能把外部资料和用户指令混作同一事实。
- 脚本判断与人工判断不一致时，在 task tracking 记录采用、修正或阻塞原因。

## Task Tracking 与恢复现场

- 中/大任务、修改型任务、规则/脚本/skill、文档、Git、简历、long-running、
  multi-agent 或 correction 任务必须有 task tracking。
- task tracking 放在 `.codex/project/records/task-tracking/`。
- pending 恢复入口放在 `.codex/project/records/pending-tasks/`。
- correction 记录放在 `.codex/project/records/corrections/`。
- 运行态、日志和账本放在 `.codex/project/state/`、`.codex/project/logs/`
  和 `.codex/project/tmp/`，不写回通用仓库。
- task tracking 至少记录：用户输入拆解、用户要求、触发日志、任务类型、worktree 证据、
  Worktree 完成记录、影响面、操作账本、验证记录、DoD、Git 状态和恢复现场。
- 多分支任务必须记录 `## 主任务分支状态门禁`，覆盖每个分支的状态、证据和下一步。
- 模板由程序输出：
  `python .codex/ai-rules/scripts/ai_rules.py templates task-tracking`。

## 任务类型门禁

- 每轮执行和收口前必须选择实际命中的任务类型：
  `code-debug`、`correction`、`rules-script`、`docs`、`git`、`frontend`、
  `resume`、`multi-agent`、`long-running`。
- 任务类型不是标签装饰；选中后必须在 task tracking 写证据。
- `rules-script` 默认要求联网核对外部成熟做法；不能只口头声称参考过。
- 任务门禁入口：
  `python .codex/ai-rules/scripts/ai_rules.py task-gate ...`。
- session 收口门禁入口：
  `python .codex/ai-rules/scripts/ai_rules.py session-gate ...`。
- 门禁脚本只读检查，不得自动修改规则入口 adapter、README、pending、
  corrections 或 Git。
- 门禁失败时，先修证据、修脚本能力或记录阻塞，不得带失败门禁发送完成态回复。

## 用户要求、触发日志与输出门禁

- 收到用户输入后，先记录 `## 用户输入拆解门禁`：原始输入或最新指令、拆出的
  任务数/要求数，以及逐 REQ 表。逐 REQ 表必须至少包含 `REQ ID`、用户要求摘要、
  记录判定、联网/搜索判定、子 AI/验证判定和验收/最终回复覆盖口径。
- `task-gate` 必须按表格行解析输入拆解门禁，并与 `## 用户要求追踪门禁` 的 REQ
  行交叉校验；散文式“记录/搜索/验证”关键词不能代替逐 REQ 判定。
- 用户输入中出现“联网、搜索、查、核对、最新、资料、URL、引用”等要求时，必须在
  输入拆解和触发日志中登记，后续验证记录要说明已联网、无需联网或阻塞原因。
- 用户新增要求、纠正要求、批准计划、改变优先级或询问是否实现时，必须登记到
  `## 用户要求追踪门禁`。
- 每条要求使用稳定 ID，记录状态、动作、实现证据、验证证据和最终回复覆盖口径。
- 同一轮多个要求不能只处理最近一条；最终回复前逐条回看。
- 触发用户要求、任务类型、安全要求、脚本能力适配、上下文压缩、脚本账本或最终门禁时，
  必须在 `## 要求触发日志` 记录 TRG 行。
- 最终回复前必须用 `## 输出信息门禁` 或等价记录覆盖已完成项、未处理项、
  未验证项、阻塞项、active pending、Git/worktree 状态和下一步。
- 修改型任务的输出门禁必须提示 worktree 是否完成、是否已合并、是否 stage/commit、
  是否 push、一般不会自动合并时需要用户确认的下一步。

## 脚本与包结构

- `ai-rules` 的 Python 代码采用包结构：`src/ai_rules/` 是唯一实现层。
- `scripts/` 只保留一个公开入口：
  `python .codex/ai-rules/scripts/ai_rules.py <command> ...`。
- 新增能力不得再创建 `scripts/codex_*.py`、`scripts/validate_*.py`、
  `scripts/agent_*.py` 这类平铺旧入口；发现旧入口属于当前改动范围时直接移除或迁移。
- 通用脚本默认使用 Python 标准库实现；PowerShell/Bash 只作为 wrapper 或平台入口。
- 新增或修改脚本后，必须验证 `--help`、一个真实成功路径、必要的失败/警告路径，
  并运行 `python .codex/ai-rules/scripts/ai_rules.py selftest` 覆盖黑盒强制行为。
- 脚本能力不支持当前目标时，先记录 `## 脚本能力适配门禁`；不得手工改运行态、
  锁、账本或派生报告来伪造脚本能力。
- Python 运行产生的缓存必须重定向到 `.codex/project/cache/python-pycache`。
- 文本文件必须用 UTF-8；JSON 不应带 UTF-8 BOM。
- 编码验证入口：
  `python .codex/ai-rules/scripts/ai_rules.py validate-encoding ...`。

## 文档、引用与交付

- 新文档、重构、README/索引/引用维护必须做影响面扫描、循环引用检查和 DoD。
- 文档引用图入口：
  `python .codex/ai-rules/scripts/ai_rules.py doc-index ...`。
- 文档任务验证入口：
  `python .codex/ai-rules/scripts/ai_rules.py validate-doc ...`。
- 仓库内部 Markdown 链接使用相对路径，不写本机绝对路径。
- 外部运行目标、日志、构建输出和临时验证根等复现证据必须记录真实路径。

## Git 边界

- 用户只要求 commit 时默认只本地 commit，不 push。
- 只有用户明确说 push、推送或提交并推送时，才允许 `git push`。
- stage/commit/push 前仍要遵守审批和 worktree 规则。
- 工作区有无关脏改动时，只 stage 本次任务相关文件。
- commit 后检查提交号、最新提交信息和剩余工作区状态。
- 多个 worktree 或子任务修改同一热点文件时，进入 integration queue，
  由单一整合者合并、记录冲突矩阵和验证结果。

## 子 AI 协作

- 大任务或用户明确要求多 AI 分工时，总控先拆任务树、写范围和验证边界。
- 创建子 AI 前必须准备 Agent Brief 或等价短输入包。
- Agent Brief 模板由程序输出：
  `python .codex/ai-rules/scripts/ai_rules.py templates agent-brief`。
- 文件型通信总线入口：
  `python .codex/ai-rules/scripts/ai_rules.py agent-comm ...`。
- 状态看板入口：
  `python .codex/ai-rules/scripts/ai_rules.py agent-groups ...`。
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
  `python .codex/ai-rules/scripts/ai_rules.py templates correction`。
- 扫描入口：
  `python .codex/ai-rules/scripts/ai_rules.py scan-corrections ...`。
- 不把一次性用户原话直接堆进规则入口 adapter；先沉淀 correction，再判断是否
  升级规则、脚本、skill、manifest 或 README。

## Skills

- 通用 skill 事实源在 `.codex/ai-rules/.codex/skills/`。
- 项目特化 skill 放 `.codex/project/skills/`。
- 目标项目原生 skill 优先级最高；同名冲突必须记录，不能静默覆盖。
- 修改任意 skill 后必须运行对应 skill 校验；带脚本的 skill 还要跑最小真实用例。

## 收口

- 收口前按任务类型运行 gate pool 或等价门禁：
  `python .codex/ai-rules/scripts/ai_rules.py gate-pool ...`。
- 规则/脚本强制执行能力变更后，收口前必须运行：
  `python .codex/ai-rules/scripts/ai_rules.py selftest --root <target-project>`。
- 工具调用账本入口：
  `python .codex/ai-rules/scripts/ai_rules.py tool-invocations ...`。
- 调用链路报告入口：
  `python .codex/ai-rules/scripts/ai_rules.py tool-flow ...`。
- 最终回复必须说明：本轮任务完成状态、原主任务状态、active pending、验证结果、
  Git/worktree 状态、worktree 是否完成、是否合并/提交/push、未处理项和下一步。
