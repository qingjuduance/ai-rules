#!/usr/bin/env python3
"""Render reusable ai-client-governance Markdown templates."""

from __future__ import annotations

import argparse


TEMPLATES: dict[str, str] = {
    "task-tracking": """# YYYY-MM-DD-task-keyword

## 本次需求概述

-

## 会话链路

| 项目 | 内容 |
|---|---|
| 时间 |  |
| 批准标签 |  |
| trace |  |
| task queue |  |
| worktree |  |

## 用户输入拆解门禁

| 项 | 内容 |
|---|---|
| 原始输入/最新指令 |  |
| 任务数/要求数 |  |

| REQ ID | 用户要求摘要 | 记录判定 | 联网/搜索判定 | 子 AI/验证判定 | 验收/最终回复覆盖口径 |
|---|---|---|---|---|---|
| REQ- |  | 必须记录/无需记录，并说明落点。 | 触发/不触发联网搜索，并说明证据或不适用原因。 | 触发/不触发子 AI、自测、失败路径和成功路径。 | 最终回复如何覆盖该要求。 |

## 用户要求追踪门禁

| ID | 用户要求 | 关联批准 | 当前状态 | 处理动作 | 实现证据 | 验证证据 | 最终回复覆盖口径 |
|---|---|---|---|---|---|---|

## 要求触发日志

| ID | 类型 | 触发内容 | 必须动作 | 状态 | trace |
|---|---|---|---|---|---|

## 主任务分支状态门禁

| 分支 | 状态 | 证据 | 下一步 |
|---|---|---|---|

## 任务类型门禁

| 任务类型 | 必选门禁 | 证据位置 | 状态 |
|---|---|---|---|

## Worktree 证据

| 项目 | 证据 |
|---|---|
| 源仓库 |  |
| worktree 创建方式 |  |
| worktree 路径 |  |
| 分支 |  |
| 基准提交 |  |
| sparse checkout 策略 |  |
| 源码目录/快照处理 |  |
| git status |  |

## Worktree 完成记录

| 项 | 状态 |
|---|---|
| worktree 是否完成 |  |
| 是否合并回源仓库 |  |
| 是否 stage/commit |  |
| 是否 push |  |
| 下一步/用户需确认 |  |

## 输出信息门禁

| 输出 ID | 输出类型 | 适用范围 | 排除范围 | 涉及对象 | 事实源/证据 | 已完成 | 未完成 | 未验证 | 阻塞 | 用户需确认 | 最终输出/覆盖口径 | trace_id |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| OUT-PLAN | 计划 |  |  |  |  |  | 无未完成 | 无未验证 | 无阻塞 |  |  |  |
| OUT-STATUS | 状态 |  |  |  |  |  | 无未完成 | 无未验证 | 无阻塞 |  |  |  |
| OUT-FINAL | 最终回复 |  |  |  |  |  | 无未完成 | 无未验证 | 无阻塞 |  |  |  |
| OUT-SCRIPT | 脚本报告 |  |  |  |  |  | 无未完成 | 无未验证 | 无阻塞 |  |  |  |
| OUT-ERROR | 错误 |  |  |  |  |  | 无未完成 | 无未验证 | 无阻塞 |  |  |  |
| OUT-GIT-WORKTREE | 仓库状态 | 覆盖 worktree、合并、提交、push 和下一步 |  | worktree | git status / worktree 记录 |  | 无未完成 | 无未验证 | 无阻塞 | 下一步：确认是否合并、提交或 push | 最终回复必须明确说明未合并/未提交/未 push 或已合并/已提交/已 push |  |

## 子 AI 验收矩阵

| 子 AI | 覆盖 REQ | 覆盖门禁 | 全面覆盖判定 | 失败路径 | 成功路径 | 发现问题/修复复测 |
|---|---|---|---|---|---|---|

## 影响面扫描

| 范围 | 检查方式 | 结论 |
|---|---|---|

## 操作 Telemetry

| 时间 | 操作 | 结果 |
|---|---|---|

## 验证记录

| 命令 | 工作目录 | 结果 | 摘要 |
|---|---|---|---|

## Definition of Done

| 项 | 状态 | 证据 |
|---|---|---|

## 恢复现场

- 下一步：
- 最小恢复读取清单：
- 禁止误动范围：
""",
    "pending-task": """# 任务关键词

## 状态

- 状态：
- 最近更新：
- 对应 task tracking：

## 恢复入口

- 原始请求：
- 最新指令：
- 批准标签：
- 最近安全停止点：
- 下一步动作：

## 最小恢复读取清单

-

## 禁止误动范围

-

## 完成记录

- 完成时间：
- 验证结果：
- 剩余风险：
""",
    "correction": """# CORR-YYYYMMDD-KEYWORD

<!--
  Machine fact source: SQLite `corrections` table in .ai-client/project/state/aicg.db.
  This file is a human-readable copy produced by `corrections export-md` or
  ingested by `corrections import-md`. Use `corrections add` to create records;
  regenerate this file via `corrections export-md` after DB edits.
-->

| 字段 | 值 |
|------|-----|
| 严重度 | P1 |
| 类型 | 流程违规 |
| 状态 | open |
| 关联任务 | |
| 创建时间 | YYYY-MM-DD |

## 问题

-

## 根因

-

## 违反规则

>

## 实际影响

-

## 修正动作

-

## 升级判定

→ 规则/脚本/skill/manifest/README：是否升级，原因。
""",
    "agent-brief": """# Agent Brief

## task_scope

## allowed_files

## required_inputs

## confirmed_facts

## validation

## output_contract

## write_scope

## lock_policy

## context_reuse

## reuse_key

## retained_facts

## skip_inputs

## context_capsule

## context_ttl

## contamination_boundary

## minimal_resume_inputs

## token_budget

## token_proxy_metrics

## token_usage_source
""",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render ai-client-governance Markdown templates.")
    parser.add_argument("template", nargs="?", choices=sorted(TEMPLATES), help="Template name.")
    parser.add_argument("--list", action="store_true", help="List available templates.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.list or not args.template:
        for name in sorted(TEMPLATES):
            print(name)
        return 0
    print(TEMPLATES[args.template].rstrip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
