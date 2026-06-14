# AI Rules

## 快速索引

| 项目 | 说明 |
|---|---|
| 用途 | 保存可跨电脑、跨项目复用的 Codex/AI 协作规则。 |
| 通用规则源 | `AGENTS.md` |
| 安装位置 | 目标项目 `.codex/rules/common/AGENTS.md` |
| 项目规则位置 | 目标项目 `.codex/rules/project/AGENTS.md` |
| 通用 skills | `.codex/skills/` |
| 安装脚本 | `install-ai-rules.ps1` |
| 会话检查 | `check-ai-rules-sync.ps1` |
| 同步脚本 | `sync-ai-rules.ps1` |
| 文档门禁 | `scripts/validate_doc_task.py` |
| 编码门禁 | `scripts/validate_encoding.py` |
| 质量目标门禁 | `AGENTS.md` 的通用质量目标与新增能力门禁 |
| 日志门禁 | `AGENTS.md` 的日志优先与可观测性门禁 |
| 归因门禁 | `AGENTS.md` 的端到端问题归因门禁 |
| 自迭代门禁 | `AGENTS.md` 的门禁 warning 自迭代记录 |

本仓库只保存通用 AI 协作规则、通用脚本和安装同步工具，不保存某个项目的
特殊文档体系、简历、学习正文、源码快照、路线材料或会话运行状态。

## 使用方式

在目标项目中安装规则：

```powershell
powershell -ExecutionPolicy Bypass -File <ai-rules-path>\install-ai-rules.ps1 -TargetProjectPath <target-project-path>
```

安装会把本仓库 `AGENTS.md` 安装到目标项目
`.codex/rules/common/AGENTS.md`，并写入项目根 `AGENTS.md` 薄入口。
薄入口负责依次读取 common 规则和 project 规则。

项目特有规则归目标项目维护，默认位置是
`.codex/rules/project/AGENTS.md`。如果该文件不存在，安装脚本只创建一个
最小占位入口；如果已经存在，安装脚本不会覆盖、合并或重排它的正文。

若目标路径已有同名托管文件或目录且内容不同，脚本会先备份到
`.codex/ai-rules-backups/`，再刷新 common 规则、通用 scripts、通用 skills
和会话检查脚本。

安装后，每次开启新的 Codex 会话时，先执行目标项目根目录下的：

```powershell
powershell -ExecutionPolicy Bypass -File .\check-ai-rules-sync.ps1
```

检查脚本会读取 `.codex/ai-rules-config.json`，找到这份规则仓库；如果距离上次
成功同步已经超过 24 小时，就执行一次拉取、合并和推送。未超过 24 小时时只输出
跳过说明。

## 同步行为

`sync-ai-rules.ps1` 在规则仓库中执行：

1. 检查 Git 仓库，不存在时初始化。
2. 如果有本地规则变更，先提交到本地 Git。
3. 如果配置了 `origin`，执行 `git pull --no-rebase --no-edit origin <branch>`。
4. 合并没有冲突后执行 `git push -u origin <branch>`。
5. 把本次同步、推送、commit 和结果写入 `.ai-rules-sync/state.json`。

如果发生合并冲突，脚本会停止并保留现场，不会自动覆盖任何一方的规则。

## 重构与新文档门禁

重构目录、新建正式 Markdown、同步规则或批量处理文档时，收口前运行只读门禁：

```powershell
python scripts\validate_doc_task.py `
  --root . `
  --task-tracking .codex\task-tracking\<file>.md `
  --mode new-doc `
  --require-task-tracking
```

脚本会检查本次触及 Markdown 是否存在常见漏项，例如 task tracking 必需小节、
本机绝对路径、正式入口链接 `questions/`、缺少 `.references/`、README 可能需要
同步、DoD 或影响面扫描未记录。脚本只报告问题，不自动修改 README、引用记录、
pending、corrections 或 Git 状态。

## 编码门禁

在 Windows、PowerShell、Python 和 Git 混用场景中处理中文路径或中文内容时，
收口前运行只读编码门禁：

```powershell
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
python scripts\validate_encoding.py `
  --root . `
  --paths AGENTS.md README.md .\scripts `
  --windows-powershell51 `
  --smoke `
  --strict
```

脚本会检查目标文本文件是否可按 UTF-8 读取，并提示常见风险：PowerShell
`Get-Content`、`Set-Content`、`Out-File`、`Add-Content` 缺少 `-Encoding`，
Python 文本 I/O 缺少 `encoding="utf-8"`，以及 Windows PowerShell 5.1 读取
非 ASCII `.ps1` 时可能需要 BOM 或解析器验证。脚本只报告问题，不自动转码或重写文件。

## 通用质量目标门禁

新增或大幅修改功能、规则、脚本、自动化、流程、模板、文档机制、验证策略或协作方式时，
不能只完成眼前产物，还要检查它是否满足通用质量目标：自我迭代升级、系统化、流程化、
内容准确化、可量化、减少错误和复发、降低 token/context 输入量、减少无效上下文和提速。

task tracking 需要填写“通用质量目标记录”，用可验证的代理指标说明效果，例如
warning/error 数量变化、复发次数、验证命令通过数、人工步骤减少数、读取文件数、
`rg` 查询数、必读文件清单长度、brief 行数、脚本化检查项数、恢复读取清单长度、
未确认推断数量和剩余观察项。没有真实 token 统计时，只能说明降低上下文输入量，
不能声称精确节省 token。

## 日志优先与可观测性门禁

处理代码项目、Mod、插件、脚本、工具链、构建、运行或调试问题时，通用规则要求先找
日志和诊断输出，再决定是否改代码。适用日志包括 stdout/stderr、构建输出、测试输出、
崩溃转储、平台事件日志、游戏引擎日志、插件加载日志、外部命令输出、监控指标和 trace。

如果没有日志，必须在 task tracking 记录“日志缺失/不足”，优先开启 debug/verbose、
dry-run、最小复现或补最小诊断日志。Mod、插件和框架扩展优先使用宿主平台 logger；
日志要能带上事件名、组件入口、关联 ID、退出码、耗时、fallback、重试和脱敏后的参数摘要。
最终回复要说明依据的日志证据，以及未解决时下一步最该查看的具体日志或诊断命令。

## 端到端问题归因门禁

跨端数据、接口、插件、客户端、后端、脚本管道或多组件协作问题，不能把错误暴露端
直接当成根因端。客户端获取数据时报错时，要同时检查插件上传数据、请求 payload、
后端入参解析、业务处理、存储或缓存中间态、返回组装、客户端接收、客户端解析和输出处理。

task tracking 要填写端到端归因矩阵和字段生命周期记录。每段都要写证据、当前判断、
是否已排除和下一步验证。只有确认上游输入、中间态、返回组装和客户端解析的证据后，
才能决定修复点；下游兜底不能掩盖真正根因。

## 门禁 warning 自迭代

门禁脚本发现本轮新增 warning 或 error 时，不能只修到通过。必须在 task tracking
记录触发命令、warning 摘要、涉及文件、根因、修复前后数量、防复发动作和是否需要
correction、规则、提示词或脚本升级。

写 task tracking、correction、`.references` 或 README 前，先检查是否写了本机绝对路径、
本次 checked files 是否都进 tracking、新建 `.references` 是否进已处理文件、普通说明行
是否过长、敏感信息是否脱敏。目标是让同类 warning 复发次数下降，同时减少重复全文读取、
上下文输入和临场排查成本。

## 远程仓库

首次使用远程仓库时，在本目录执行：

```powershell
git remote add origin git@github.com:your-name/ai-rules.git
powershell -ExecutionPolicy Bypass -File .\sync-ai-rules.ps1
```

如果没有配置 remote，脚本只维护本地 Git 和同步状态，不会尝试推送。

## 规则分层

目标项目最终结构：

```text
AGENTS.md
.codex/
  rules/
    common/
      AGENTS.md
    project/
      AGENTS.md
```

- 根 `AGENTS.md` 只是入口，不承载完整规则正文。
- `.codex/rules/common/` 由本仓库同步维护。
- `.codex/rules/project/` 由目标项目维护，写项目特有规则。
- 通用规则知道 project 规则位置，并要求每次任务先读取 project 入口。
- 通用规则不得把 project 规则内容写回本仓库。

## 托管范围

纳入同步：

- `AGENTS.md`，安装目标为 `.codex/rules/common/AGENTS.md`
- `.codex/skills/agents-rule-maintainer/`
- `.codex/skills/self-correction-planner/`
- `scripts/agent_comm.py`
- `scripts/agent_group_status.py`
- `scripts/scan_corrections.py`
- `scripts/validate_doc_task.py`
- `scripts/validate_encoding.py`
- `check-ai-rules-sync.ps1`

不纳入同步：

- 目标项目 `.codex/rules/project/`
- 目标项目已有的项目特有 skills
- `.codex/task-tracking/`
- `.codex/pending-tasks/`
- `.codex/agent-comm/`
- `.codex/agent-groups/`
- 某个项目的文档架构、学习路线、简历规则、源码快照和会话运行日志。
