# AI Rules

## 快速索引

| 项目 | 说明 |
|---|---|
| 仓库定位 | 可嵌入到任意项目的、脱离具体模型和客户端的 AI 执行流程框架。 |
| 推荐嵌入位置 | 目标项目 `.codex/ai-rules/` |
| 通用规则事实源 | `.codex/ai-rules/AGENTS.md`，文件名兼容 AGENTS 生态但内容是 agent-neutral 框架。 |
| 项目规则入口 | 目标项目 `.codex/project/rules/project/AGENTS.md` |
| 入口适配器 | `AGENTS.md`、`CLAUDE.md`、`GEMINI.md`、Copilot/Cursor/Cline/Windsurf/Continue/Roo/Aider 等工具原生规则入口。 |
| 机器清单 | `manifest.json` |
| 嵌入脚本 | `install-ai-rules.ps1`，一键嵌入、生成目标项目配置，并按缺失补齐常见入口 adapter。 |
| 会话检查 | `check-ai-rules-sync.ps1` |
| 统一 CLI | `python .codex/ai-rules/scripts/ai_rules.py --list` |
| Python 包 | `.codex/ai-rules/src/ai_rules/` |
| 修改工作区 | 所有修改型任务先创建 `.codex/project/.worktree/<task-slug>/` 任务 worktree。 |
| Token 用量统计 | 嵌入后 `.codex/ai-rules/.codex/skills/codex-token-usage/`，这是 Codex 本地日志的可选 skill，不代表框架只服务 Codex。 |
| 推荐嵌入方式 | Git submodule |
| 默认同步策略 | 每次会话检查；最多 24 小时 fetch 一次；不自动 pull/push。 |
| 回写方式 | 进入 `.codex/ai-rules/` 后使用普通 Git 命令提交和推送。 |

这个仓库不是某个项目的 `.codex/rules/common/` 文件夹备份，而是一套完整的
通用 AI 执行流程框架。目标项目应把本仓库作为一个独立 Git 仓库嵌入进来，
Git 项目默认用 submodule 记录精确规则版本，让通用规则、入口 adapter、
通用 skills、门禁脚本、README 和 manifest 保持完整上下文。

`AGENTS.md` 仍然保留，因为 Codex、Devin、Cursor 等生态已经支持它，也因为
本仓库历史上用它作为通用规则事实源。但新的设计原则是：**文件名是适配器，
流程框架才是事实源**。Claude Code 可以通过 `CLAUDE.md` 进入，Gemini CLI
可以通过 `GEMINI.md` 进入，Copilot、Cursor、Cline、Windsurf、Continue、
Roo Code、Aider 等工具也可以通过各自原生入口指向同一套规则。

## 当前优势

- **完整 Git 边界**：通用规则有自己的 commit、branch、remote 和 history；
  Git submodule 让父项目只记录一个 gitlink commit，项目内修改通用规则时，
  可以直接在 `.codex/ai-rules/` 用 Git 回写。
- **脱离模型和客户端**：核心生命周期、审批、worktree、联网核对、task tracking、
  Git 边界和收口门禁不依赖 Codex、Claude、Gemini、Copilot 或任何单一 IDE。
  各工具原生规则文件只作为薄 adapter，统一指向 `.codex/ai-rules/` 和
  `.codex/project/` 的事实源。
- **多入口 adapter 目录**：安装脚本可以在缺失时生成 `AGENTS.md`、`CLAUDE.md`、
  `GEMINI.md`、`.github/copilot-instructions.md`、`.cursor/rules/ai-rules.mdc`、
  `.clinerules/ai-rules.md`、`.windsurf/rules/ai-rules.md`、
  `.continue/rules/ai-rules.md`、`.roo/rules/ai-rules.md` 和 `CONVENTIONS.md`。
  已存在的项目原生入口默认保留，不静默覆盖。
- **只支持完整嵌入模型**：目标项目把本仓库整体放在 `.codex/ai-rules/`，
  不再生成或维护 common 规则、scripts、skills 的分散复制副本。
- **三层资产边界清楚**：优先级固定为目标项目原生资产 >
  `.codex/project/` 项目特化层 > `.codex/ai-rules/` 通用层。本仓库只维护跨项目
  通用协作规则；项目业务、文档体系、简历、源码快照和本地交付规则留在目标项目。
- **每次会话都能发现不一致**：检查脚本每次运行都会检查 embedded repo 是否
  missing、dirty、ahead、behind 或 diverged；一旦不一致，会持续提示到同步完成。
- **24 小时只限制 fetch**：为了减少远端请求，默认最多 24 小时 fetch 一次；
  但本地 dirty/ahead/behind/diverged 状态每次会话都会检查并提示。
- **脚本能力和上下文可恢复**：脚本不支持当前目标时先走能力适配门禁，
  连续纠错或新要求叠加时把压缩快照写入 task tracking，避免手工绕过和恢复遗漏。
- **用户要求可验收**：每条用户要求登记为 `REQ-*`，绑定处理状态、实现证据、
  验证证据和最终回复覆盖；`ai_rules.py task-gate` 会检查缺失项。
- **框架化门禁模型**：`ai-rules` 是人和 AI 之间的执行框架。输入过滤器拆用户要求，
  处理拦截器管审批/worktree/联网核对，输出拦截器管最终回复和 worktree 完成提示，
  横切门禁管编码、Git、文档、账本和 trace flow。新增可确定约束时注册组件，
  不再只追加口头规则。
- **门禁池和调用链路**：`ai_rules.py gate-pool` 可以按同一 `trace_id` 编排多个
  门禁，`ai_rules.py tool-invocations` 和 `ai_rules.py tool-flow` 记录并验证调用链路。
- **本地 token 用量统计**：`codex-token-usage` skill 读取本机 Codex session
  JSONL 的 `token_count` 事件，输出总量、净用量、缓存命中率、峰值日和最忙周。
- **适合 GitHub 展示和复用**：README、manifest、规则事实源、adapter 目录和脚本
  共同说明如何嵌入、如何维护边界、如何同步和如何把改动回写给上游。

每次准备把本仓库上传或推送到 GitHub 前，都要回看本 README 的“当前优势”和
`manifest.json`，确认它们真实反映当前仓库能力。新增门禁、skill、脚本、
入口 adapter 或嵌入策略后，README 需要同步更新，方便别人一眼看懂这个仓库的价值。

## 多工具入口适配器

`ai-rules` 把主流 AI 编码工具的规则文件分成三类：

- **核心事实源**：`.codex/ai-rules/AGENTS.md` 和
  `.codex/project/rules/project/AGENTS.md`。它们保存真正的流程、边界和项目特化规则。
- **项目原生入口**：工具会自动读取或用户已经维护的规则文件，例如根 `AGENTS.md`、
  `CLAUDE.md`、`GEMINI.md`、`.github/copilot-instructions.md` 或
  `.cursor/rules/*.mdc`。
- **adapter 薄入口**：安装脚本在缺失时生成的短文件，只写读取顺序、同步检查、
  编码和边界；不复制大段通用规则。

当前内置 adapter 目录：

| 工具/生态 | 入口 adapter | 处理策略 |
|---|---|---|
| Codex / AGENTS 生态 / Devin / Cursor 兼容入口 | `AGENTS.md` | 默认生成薄入口；已存在时保留，除非显式 `-ForceRootEntry`。 |
| Claude Code | `CLAUDE.md` | 默认生成薄入口；如果工具支持导入，应优先导入 `AGENTS.md` 或通用事实源。 |
| Gemini CLI | `GEMINI.md` | 默认生成薄入口；可通过 `@` 导入方式复用通用事实源。 |
| GitHub Copilot | `.github/copilot-instructions.md`、`.github/instructions/ai-rules.instructions.md` | 默认只在缺失时生成，避免覆盖团队已有 Copilot 指令。 |
| Cursor | `.cursor/rules/ai-rules.mdc` | 使用 `alwaysApply: true` 的项目规则 adapter，已存在则保留。 |
| Cline | `.clinerules/ai-rules.md` | Workspace rules adapter，已存在则保留。 |
| Windsurf | `.windsurf/rules/ai-rules.md` | Project rules adapter，已存在则保留。 |
| Continue | `.continue/rules/ai-rules.md` | Project-specific rules adapter，已存在则保留。 |
| Roo Code | `.roo/rules/ai-rules.md` | Project rules adapter，已存在则保留。 |
| Aider | `CONVENTIONS.md` | Aider convention adapter；已存在时保留，因为它常承载项目真实约定。 |

这个清单是兼容层，不是优先级反转。目标项目已有的原生规则文件最高优先级；
`.codex/project/` 是项目特化层；`.codex/ai-rules/` 是通用层。adapter 只负责把不同
AI 工具带回同一套事实源，不能静默覆盖已有规则，也不能把项目业务规则写回通用仓库。

外部依据优先来自官方文档：OpenAI Codex `AGENTS.md`、Anthropic Claude Code
`CLAUDE.md`、Google Gemini CLI `GEMINI.md`、GitHub Copilot custom instructions、
Cursor rules、Cline rules、Continue rules、Devin rules 和 Aider conventions。
官方没有稳定声明或不同版本行为不一致时，README 与 manifest 只记录“adapter 候选”，
不把它当成强制自动加载事实。

## 给其它 AI 的使用约定

当用户在另一个项目里告诉你本仓库位置时，按这个顺序处理：

1. 读取本仓库 `README.md` 和 `manifest.json`，确认当前 schema、嵌入位置和同步策略。
2. 在目标项目中嵌入完整仓库，推荐路径为 `.codex/ai-rules/`；
   目标项目是 Git 仓库时默认使用 Git submodule。
3. 生成或检查当前 AI 工具对应的入口 adapter；默认只补缺，不覆盖项目已有
   `AGENTS.md`、`CLAUDE.md`、`GEMINI.md`、Copilot/Cursor/Cline 等原生规则。
4. 确认所有 adapter 都指向 `.codex/ai-rules/AGENTS.md` 和
   `.codex/project/rules/project/AGENTS.md`，且项目特有规则不写回本仓库。
5. 每次新会话先运行 `.codex/ai-rules/check-ai-rules-sync.ps1` 或目标项目根部
   等价 wrapper；若提示不一致，必须提醒用户，直到 Git 同步完成。

## 嵌入方式

### 推荐：Git submodule

目标项目已经是 Git 仓库，且希望父仓库记录 ai-rules 的精确版本时，使用 submodule：

```powershell
git submodule add <ai-rules-url> .codex/ai-rules
git submodule update --init --recursive
```

如果 `<ai-rules-url>` 是本机路径，尤其是相对路径或 `file://` URL，Git 可能默认
禁止 file transport。手工执行时使用：

```powershell
git -c protocol.file.allow=always submodule add <local-ai-rules-path> .codex/ai-rules
git submodule update --init --recursive
```

后续更新：

```powershell
git -C .codex/ai-rules fetch origin
git -C .codex/ai-rules pull --ff-only
git add .gitmodules .codex/ai-rules
git commit -m "chore: update embedded ai-rules"
```

Git 官方文档把 submodule 定义为“把一个 Git 仓库作为另一个 Git 仓库的子目录”，
这正是本仓库推荐模型：父项目记录所使用的规则版本，规则仓库保留独立历史。

### 备选：嵌套 clone

如果暂时不想让父仓库记录 submodule，也可以直接 clone：

```powershell
git clone <ai-rules-url-or-local-path> .codex/ai-rules
```

这种方式更轻，但父仓库不会自然记录嵌入规则版本。需要在目标项目 README、
`.codex/ai-rules-config.json` 或 task tracking 中记录当前 commit。

### 脚本辅助嵌入

本仓库提供一个一键安装入口。常见流程是先把本仓库 clone 到本机，然后从
本仓库目录运行安装脚本，并传入目标项目根路径：

```powershell
powershell -ExecutionPolicy Bypass -File <ai-rules-path>\install-ai-rules.ps1 `
  -TargetProjectPath <target-project-path> `
  -RemoteUrl <ai-rules-url>
```

如果省略 `-RemoteUrl`，脚本会优先读取当前 `ai-rules` 仓库的 `origin` URL；
如果没有 remote，才退回使用本地仓库路径作为来源。脚本默认使用当前
`ai-rules` 分支作为 submodule branch，也可以显式传入 `-Branch main`。

安装前可先预演：

```powershell
powershell -ExecutionPolicy Bypass -File <ai-rules-path>\install-ai-rules.ps1 `
  -TargetProjectPath <target-project-path> `
  -PlanOnly
```

或者使用 PowerShell 原生预演：

```powershell
powershell -ExecutionPolicy Bypass -File <ai-rules-path>\install-ai-rules.ps1 `
  -TargetProjectPath <target-project-path> `
  -WhatIf
```

脚本默认完成：

- 把完整 `ai-rules` 仓库嵌入到 `.codex/ai-rules/`，Git 项目默认用 submodule。
- 目标项目根 `AGENTS.md` 缺失时写入薄入口；已存在时默认保留。
- 常见 AI 工具入口 adapter 缺失时写入薄入口；已存在时默认保留，可通过
  `-SkipAgentAdapters` 跳过或 `-ForceAgentAdapters` 备份后重写。
- 仅在缺失时创建 `.codex/project/rules/project/AGENTS.md` 项目规则占位。
- 写 `.codex/ai-rules-config.json`，记录 source URL、submodule path、branch
  同步策略和 adapter catalog。
- 安装后运行 `.codex/ai-rules/check-ai-rules-sync.ps1 -NoFetch` 做本地一致性检查。

已有文件和目录的处理策略：

- `AGENTS.md` 已存在：默认保留，不覆盖；它属于目标项目已有入口。
  如果确实要用生成薄入口替换，必须显式传 `-ForceRootEntry`，脚本会按备份策略处理。
- `CLAUDE.md`、`GEMINI.md`、`.github/copilot-instructions.md`、
  `.github/instructions/*.instructions.md`、`.cursor/rules/*.mdc`、`.clinerules/`、
  `.windsurf/rules/`、`.continue/rules/`、`.roo/rules/`、`CONVENTIONS.md`
  等工具原生入口已存在：默认保留，不覆盖；如果确实要统一重写 adapter，必须显式传
  `-ForceAgentAdapters`。
- `.codex/ai-rules-config.json` 已存在：默认备份后重写，保持配置事实源最新。
- `.codex/project/rules/project/AGENTS.md` 已存在：默认保留，不覆盖。
- 原生 `.codex/skills/` 或其它项目已有 skill：默认保留、只读索引和报告冲突；
  除非用户明确要求修改原生 skill，否则安装脚本和 ai-rules 维护任务都不能改它。
- `.codex/project/skills/` 是项目特化层：用于放置根据 ai-rules 流程、纠错和当前项目
  经验生成的 skill，不用于承接通用 skill 的复制副本。
- skill 同名时按“原生项目 skill > `.codex/project` skill > `ai-rules` skill”选择，
  后续由架构守卫或人工 review 报告冲突。
- `.codex/ai-rules/` 已存在且是已注册 submodule：不重复添加，只继续配置检查。
- `.codex/ai-rules/` 已存在但不是 Git 仓库：停止并提示用户手工处理。
- `.codex/ai-rules/` 是 Git 仓库但未注册为 submodule：默认停止；确认要接管时，
  重新运行并传 `-AdoptExistingGitRepo`。
- 目标项目不是 Git 仓库且使用默认 submodule 模式：停止；可先 `git init`，
  或显式传 `-Mode clone`。

脚本不会自动 commit 或 push。`git submodule add` 会修改父项目的 `.gitmodules`
和 gitlink，安装后仍要人工 review：

```powershell
git status
git diff --cached
git add .gitmodules .codex/ai-rules AGENTS.md `
  CLAUDE.md GEMINI.md CONVENTIONS.md .github .cursor .clinerules `
  .windsurf .continue .roo .codex/ai-rules-config.json `
  .codex/project/rules/project/AGENTS.md
git commit -m "chore: embed ai-rules"
```

如果目标项目不是 Git 仓库，或明确不希望父仓库记录 ai-rules 提交，才传
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
│       └── ai-rules.instructions.md
├── .cursor/
│   └── rules/
│       └── ai-rules.mdc
├── .clinerules/
│   └── ai-rules.md
├── .windsurf/
│   └── rules/
│       └── ai-rules.md
├── .continue/
│   └── rules/
│       └── ai-rules.md
├── .roo/
│   └── rules/
│       └── ai-rules.md
└── .codex/
    ├── ai-rules/
    │   ├── AGENTS.md
    │   ├── README.md
    │   ├── manifest.json
    │   ├── scripts/
    │   │   └── ai_rules.py
    │   ├── src/
    │   │   └── ai_rules/
    │   └── .codex/skills/
    ├── ai-rules-config.json
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
- `.codex/ai-rules/`：本仓库的完整 Git 工作树，是通用规则事实源。
- `.codex/project/rules/project/`：目标项目维护的本地规则，不写回本仓库。
- schema 3 不提供 common 规则、scripts 或 skills 的分散复制目标；需要通用内容时，
  直接读取 `.codex/ai-rules/` 内的仓库文件。

## 脚本包结构与统一入口

`ai-rules` 的实现代码采用类似 Spring Boot 根包的组织方式：

```text
.codex/ai-rules/
├── src/ai_rules/
│   ├── cli.py
│   ├── agents/
│   ├── audit/
│   ├── common/
│   ├── docs/
│   ├── gates/
│   ├── lifecycle/
│   ├── records/
│   ├── sync/
│   ├── validation/
│   └── worktree/
└── scripts/
    └── ai_rules.py
```

- `src/ai_rules/` 是真实实现，按功能域分层。
- `scripts/ai_rules.py` 是唯一公开 Python 入口，使用子命令分发到各模块。
- 不再生成 `scripts/codex_*.py`、`scripts/validate_*.py`、`scripts/agent_*.py`
  这类平铺旧入口；当前范围内发现旧入口时直接迁移或移除。
- 新增能力优先放进 `src/ai_rules/<domain>/`，并通过统一 CLI 暴露。

常用命令：

```powershell
python .codex\ai-rules\scripts\ai_rules.py --list
python .codex\ai-rules\scripts\ai_rules.py task-gate --help
python .codex\ai-rules\scripts\ai_rules.py templates task-tracking
```

## 每次会话检查

在目标项目根目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\.codex\ai-rules\check-ai-rules-sync.ps1 `
  -TargetProjectPath .
```

跨系统入口优先使用统一 CLI：

```powershell
python .\.codex\ai-rules\scripts\ai_rules.py sync-check --target-project-path .
```

检查脚本只读检查嵌入仓库状态，默认行为如下：

- 每次会话都检查 `.codex/ai-rules` 是否存在、是否为 Git 仓库、是否有 dirty 改动。
- 如果上次 fetch 已超过 24 小时，执行一次 `git fetch`；未超过 24 小时则跳过 fetch。
- 无论是否 fetch，都会比较本地 HEAD 与 upstream，发现 ahead、behind 或 diverged
  就提示用户。
- 不自动 `git pull`，因为 pull 可能修改规则工作树。
- 不自动 `git push`，因为 push 需要用户明确确认远端边界。
- warning 会每次出现，直到用户在 `.codex/ai-rules/` 中完成同步。
- 支持 JSON 输出，供门禁或其它脚本复用：
  `python .\.codex\ai-rules\scripts\ai_rules.py sync-check --target-project-path . --format json`。

## 长文件安全读取

通用规则、项目规则、长 task tracking 或长日志不要直接整段输出到终端。
优先用 Python 摘录脚本读取标题索引、关键词命中段或明确行号范围：

```powershell
python .\.codex\ai-rules\scripts\ai_rules.py context-extract .codex\ai-rules\AGENTS.md --headings
python .\.codex\ai-rules\scripts\ai_rules.py context-extract .codex\ai-rules\AGENTS.md --match 同步 --context 2
python .\.codex\ai-rules\scripts\ai_rules.py context-extract .codex\ai-rules\AGENTS.md --range 49:64 --format markdown
```

脚本输出行数、字节数、SHA256、实际摘录行数和 `truncated` 标记；它只读文件，
不修改规则、tracking 或 Git 状态。

常见处理命令：

```powershell
git -C .codex/ai-rules status
git -C .codex/ai-rules pull --ff-only
git -C .codex/ai-rules push
```

如果出现 diverged 或冲突，停止自动处理，保留现场，让用户决定 merge、rebase
或拆分提交。

## 写回通用规则

修改通用规则时，直接在嵌入仓库中工作：

```powershell
cd .codex/ai-rules
git status
git add AGENTS.md README.md manifest.json scripts
git commit -m "docs: update common AI rules"
git push origin main
```

不要把目标项目 `.codex/project/rules/project/`、`.codex/project/skills/`、
`.codex/project/records/task-tracking/`、`.codex/project/records/pending-tasks/`、
`.codex/project/records/corrections/`、`.codex/project/logs/tool-invocations/`
或业务文档写回本仓库。

## 强制 Worktree 工作区

核心原则是“一切流程化 + 可审计”：重复步骤必须沉淀到脚本，跨会话状态必须写入
机器可读文件，最终结果必须能通过 task tracking、Git 提交和 worktree 状态快照复盘。

所有修改型任务不论任务量大小，都必须先创建任务级 `git worktree` 和独立分支，再改文件、
运行会写入仓库的脚本、格式化、导出或提交。只读定位、读取规则、同步检查、状态检查和
计划输出可以在原工作区完成；一旦要落盘修改，先进入 worktree。

目标路径固定为宿主项目 `.codex/project/.worktree/<task-slug>/`。即使修改的是嵌入式
`.codex/ai-rules/` 仓库，也从 ai-rules 仓库执行 `git worktree add`，把新工作区放到宿主
项目的 `.codex/project/.worktree/` 下。

优先使用固定脚本入口：

```powershell
python .codex\ai-rules\scripts\ai_rules.py worktree-task create `
  --title "worktree fixed task script" `
  --repo ai-rules `
  --task-slug worktree-task-script `
  --scope src/ai_rules/worktree `
  --scope src/ai_rules/cli.py `
  --task-tracking .codex/project/records/task-tracking/2026-06-16-worktree固定脚本.md `
  --register-session

python .codex\ai-rules\scripts\ai_rules.py worktree-task status --write-state
python .codex\ai-rules\scripts\ai_rules.py worktree-task status --format json
python .codex\ai-rules\scripts\ai_rules.py worktree-task close --repo ai-rules --task-slug worktree-task-script
python .codex\ai-rules\scripts\ai_rules.py worktree-task remove --repo ai-rules --task-slug worktree-task-script
```

`status --write-state` 会生成或更新 `.codex/project/state/worktrees.json`，记录 self 和
ai-rules 两个仓库下每个任务 worktree 的路径、分支、`head_at_snapshot`、dirty 状态和
是否已合并到目标分支。这个文件是可提交的审计快照，不是免运行的实时数据库；提交快照
本身会推进主仓库 HEAD，所以 HEAD 字段必须按 `*_at_snapshot` 理解。后续 AI 会话必须
优先读取这个快照，再重新运行同一个脚本或结合 `git worktree list --porcelain` 核对真实
Git 状态，最终回复前也必须做一次 live status 校验。

`remove` 默认只输出 dry-run 计划；只有显式传 `--execute` 才会调用
`git worktree remove`，且默认拒绝移除 dirty worktree。固定脚本会把路径限制在宿主
项目 `.codex/project/.worktree/<task-slug>/`，创建时可自动登记
`worktree-coord` session 和写锁。

手工 fallback 示例：

```powershell
New-Item -ItemType Directory -Force .codex\project\.worktree | Out-Null
git -C .codex\ai-rules worktree add `
  .codex\project\.worktree\ai-rules-<task-slug> `
  -b codex/ai-rules-<task-slug>
```

task tracking 或最终验证记录必须写清源仓库路径、worktree 路径、分支名、基准提交和
`git status` 摘要。`.codex/project/.worktree/` 是本地隔离工作区，不应被 stage 或提交；
如果目标项目还没有忽略该路径，应在收口前补上忽略规则或明确记录不提交该目录。

## 并行 Worktree 协调

`ai_rules.py worktree-coord` 用来协调同一 Git 仓库的多个 worktree、会话和
智能体组。它把运行态写入 `git rev-parse --git-common-dir` 下的
`codex-runtime/worktree-coord/`，不进入提交。

常用命令：

```powershell
python scripts\ai_rules.py worktree-coord status --active-only
python scripts\ai_rules.py worktree-coord session register --title "task" --scope "docs" --metadata-kv phase=planning
python scripts\ai_rules.py worktree-coord lock acquire --session-id <id> --scope "docs"
python scripts\ai_rules.py worktree-coord queue add --session-id <id> --summary "integration item"
python scripts\ai_rules.py worktree-coord validate
```

脚本只登记 session、写锁和 integration queue，不自动 merge、rebase、commit
或 push。最终冲突仍由整合者在独立 worktree 中判断和验证。

在 Windows/PowerShell 中传递结构化 metadata 时，优先使用可重复的
`--metadata-kv key=value`，或把 JSON 对象保存为 UTF-8 文件后使用
`--metadata-file <path>`；不要依赖命令行内联 JSON 字符串，因为引号可能被
原生命令参数传递层剥离。`--metadata` 仍保留给能稳定传入严格 JSON 的环境。

示例：

```powershell
python scripts\ai_rules.py worktree-coord session register `
  --title "gate runner" `
  --scope "scripts" `
  --metadata-kv phase=implementation `
  --metadata-kv requested_by=user

$metadata = @{ phase = "planning"; requested_by = "user" } | ConvertTo-Json -Compress
$metadata | Set-Content -Encoding UTF8 .codex\project\tmp\worktree-metadata.json
python scripts\ai_rules.py worktree-coord session register `
  --title "gate runner" `
  --scope "scripts" `
  --metadata-file .codex\project\tmp\worktree-metadata.json
```

## 门禁池与调用链路

`ai_rules.py gate-pool` 用来把固定门禁编排成一次可追踪运行。它不会自动
修改规则、tracking、corrections、pending 或 Git 状态；每个子门禁都通过
`ai_rules.py tool-invocations` 写入 `.codex/project/logs/tool-invocations/*.jsonl`，并共享同一个
`trace_id`。

从框架角度看，门禁池类似一条可注册的执行链：

- 输入过滤器：先把用户原始输入拆成任务数和逐 REQ 表，逐行记录用户要求摘要、
  记录判定、联网/搜索判定、子 AI/验证判定和验收口径。
- 处理拦截器：在改文件前检查审批、worktree、任务类型、联网核对和脚本能力。
- 输出拦截器：最终回复前检查完成项、未完成项、未验证项、阻塞项、active pending、
  Git/worktree 状态、是否合并、是否提交、是否 push 和下一步用户确认。
- 横切门禁：编码、文档引用、correction 扫描、工具调用账本和 trace flow。

新增门禁时优先注册到 `task-gate`、`session-gate`、`gate-pool` 或 `lifecycle` 的
相应位置，让它自动进入最终收口链路。

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
python scripts\ai_rules.py gate-pool `
  --task-tracking .codex\project\records\task-tracking\example.md `
  --task-type correction `
  --task-type rules-script `
  --changed-path .codex\ai-rules\AGENTS.md `
  --changed-path .codex\ai-rules\src\ai_rules\gates\task_gate.py `
  --final
```

只查看计划：

```powershell
python scripts\ai_rules.py gate-pool --task-tracking .codex\project\records\task-tracking\example.md --dry-run
```

链路报告可单独运行：

```powershell
python scripts\ai_rules.py tool-flow --trace-id <trace-id> --require-final-gate --require-report --require-trace
```

## 输出门禁与 Worktree 收口

修改型任务通常不会自动合并 worktree，也不会自动 stage、commit 或 push。
因此最终回复前，task tracking 必须有 `## Worktree 完成记录` 和
`## 输出信息门禁` 中的 worktree/Git 行，至少写清：

- worktree 路径、分支、基准提交和当前 `git status`。
- worktree 任务是否完成。
- 是否已经合并回源仓库；未合并时写明等待用户确认。
- 是否 stage/commit；未提交时写明没有本地 commit。
- 是否 push；未推送时写明远端未变。
- 用户下一步要决定的是合并、提交、推送、继续验证还是放弃。

`ai_rules.py task-gate` 会把这些当作输出拦截器检查，缺少时最终门禁失败。

## Codex Token 用量统计

`.codex/ai-rules/.codex/skills/codex-token-usage/` 提供本地只读统计能力，用于回答
“统计最近 30 天 Codex token 用量”“统计某个自然月的缓存命中率和净用量”
这类问题。它默认读取当前用户 `~/.codex/sessions/**/rollout-*.jsonl`，
只汇总 `token_count` 事件中的 `last_token_usage`，避免累加会话内累计字段
`total_token_usage` 导致重复计算。

常用命令：

```powershell
python .codex\ai-rules\.codex\skills\codex-token-usage\scripts\codex_token_usage.py --days 30 --timezone Asia/Shanghai
python .codex\ai-rules\.codex\skills\codex-token-usage\scripts\codex_token_usage.py --month 2026-04 --format json
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
- 目标项目 `.codex/project/rules/project/`。
- 目标项目 task tracking、pending、corrections、tool invocation 账本。
- 外部项目状态、日志、源码快照、构建产物和本地临时验证目录。

## Schema 3 边界

schema 3 只描述完整嵌入模型：`.codex/ai-rules/` 是通用规则、README、
manifest、scripts 和 skills 的唯一通用事实源。安装脚本只生成薄入口 adapter、
`.codex/ai-rules-config.json` 和缺失时的项目规则占位，不再声明或写出旧 common
副本、scripts 副本或 skills 副本目标。adapter 文件包括但不限于 `AGENTS.md`、
`CLAUDE.md`、`GEMINI.md`、Copilot instructions、Cursor rules、Cline rules、
Windsurf rules、Continue rules、Roo rules 和 Aider `CONVENTIONS.md`。

迁移旧项目时，先把完整仓库嵌入 `.codex/ai-rules/`，再让目标项目已有入口或新生成
adapter 指向 `.codex/ai-rules/AGENTS.md` 和
`.codex/project/rules/project/AGENTS.md`。旧的 common 副本、脚本副本或 skill 副本
不参与新模型的读取、安装和同步。

如果目标项目已经有 `AGENTS.md`、`CLAUDE.md`、`GEMINI.md`、Copilot/Cursor/Cline
等原生规则或本地 skills，先把它们当作原生项目事实源管理。`ai-rules` 只能补缺、
索引和报告冲突，不能静默覆盖；根据纠错或项目流程生成的项目特化 skill 统一维护在
`.codex/project/skills/`，通用 skill 保持在 `.codex/ai-rules/.codex/skills/`。

## 验证建议

修改本仓库后，至少运行：

```powershell
$env:PYTHONUTF8 = "1"
$env:PYTHONPYCACHEPREFIX = ".codex\project\cache\python-pycache"
$pyFiles = Get-ChildItem -Recurse -Filter *.py .codex\ai-rules\scripts, .codex\ai-rules\src
python -m py_compile ($pyFiles | ForEach-Object { $_.FullName })
python .codex\ai-rules\scripts\ai_rules.py --list
python .codex\ai-rules\scripts\ai_rules.py validate-encoding `
  --paths .codex\ai-rules\AGENTS.md `
  .codex\ai-rules\README.md `
  .codex\ai-rules\manifest.json `
  AGENTS.md CLAUDE.md GEMINI.md CONVENTIONS.md `
  .github .cursor .clinerules .windsurf .continue .roo `
  .codex\ai-rules\scripts `
  .codex\ai-rules\src `
  --require-paths
python .codex\ai-rules\scripts\ai_rules.py session-gate --help
python .codex\ai-rules\scripts\ai_rules.py task-gate --help
python .codex\ai-rules\scripts\ai_rules.py tool-flow --help
python .codex\ai-rules\scripts\ai_rules.py gate-pool --help
python .codex\ai-rules\scripts\ai_rules.py worktree-coord --help
python .codex\ai-rules\scripts\ai_rules.py selftest --root .
```

修改 PowerShell 脚本后，还要用 PowerShell Parser 做语法检查，并在临时目录跑最小
真实用例：嵌入仓库、写 config、运行每次会话检查、验证 warning/OK 输出符合预期。

