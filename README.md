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

本仓库只保存通用 AI 协作规则、通用脚本和安装同步工具，不保存某个项目的
特殊文档体系、简历、学习正文、源码快照、路线材料或会话运行状态。

## 使用方式

在目标项目中安装规则：

```powershell
powershell -ExecutionPolicy Bypass -File D:\root\file\resume\ai-rules\install-ai-rules.ps1 -TargetProjectPath D:\path\to\project
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
- `check-ai-rules-sync.ps1`

不纳入同步：

- 目标项目 `.codex/rules/project/`
- 目标项目已有的项目特有 skills
- `.codex/task-tracking/`
- `.codex/pending-tasks/`
- `.codex/agent-comm/`
- `.codex/agent-groups/`
- 某个项目的文档架构、学习路线、简历规则、源码快照和会话运行日志。
