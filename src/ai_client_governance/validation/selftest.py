"""Black-box self-tests for ai-client-governance enforcement behavior."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from ai_client_governance.common.paths import PYTHON_PYCACHE_DIR, TMP_DIR, ai_client_governance_entrypoint


@dataclass
class CommandResult:
    command: list[str]
    exit_code: int
    stdout: str
    stderr: str


@dataclass
class TestResult:
    name: str
    passed: bool
    summary: str
    commands: list[CommandResult]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run ai-client-governance black-box self-tests.")
    parser.add_argument("--root", default=".", help="Target project root. Default: current directory.")
    parser.add_argument("--keep", action="store_true", help="Keep temporary self-test files.")
    parser.add_argument("--format", choices=("text", "json"), default="text", help="Output format.")
    return parser.parse_args()


def run_command(command: list[str], cwd: Path, env_root: Path) -> CommandResult:
    completed = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            **os.environ,
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONPYCACHEPREFIX": str(env_root / PYTHON_PYCACHE_DIR),
        },
    )
    return CommandResult(
        command=command,
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def output_gate_rows(worktree_variant: str = "complete") -> str:
    rows = []
    output_types = ["计划", "状态", "最终回复", "脚本报告", "错误", "仓库状态"]
    for index, output_type in enumerate(output_types, 1):
        rows.append(
            "| OUT-SELFTEST-{index:02d} | {output_type} | 适用范围：黑盒 selftest 覆盖 | "
            "排除范围：不覆盖业务文件 | 涉及对象：临时 tracking 和 queue | "
            "事实源/证据：selftest 命令 | 已完成：测试输入生成和 worktree 完成记录 | 无未完成 | "
            "无未验证 | 无阻塞 | 用户需确认：无 | 最终输出/覆盖口径：报告通过或失败 | "
            "trace-selftest |".format(index=index, output_type=output_type)
        )
    if worktree_variant == "generic":
        rows.append(
            "| OUT-SELFTEST-WORKTREE | 仓库状态 | 适用范围：worktree completion 输出 | "
            "排除范围：不覆盖真实合并 | 涉及对象：selftest worktree | 事实源/证据：Worktree 完成记录 | "
            "已完成：记录 worktree closeout | 无未完成 | 无未验证 | 无阻塞 | "
            "用户需确认：无 | 最终输出/覆盖口径：报告 worktree、合并、提交、push | "
            "trace-selftest |"
        )
    else:
        rows.append(
            "| OUT-SELFTEST-WORKTREE | 仓库状态 | 适用范围：worktree completion 输出 | "
            "排除范围：不覆盖真实合并 | 涉及对象：selftest worktree | 事实源/证据：Worktree 完成记录 | "
            "已完成：记录不自动合并、不提交、不 push | 无未完成 | 无未验证 | 无阻塞 | "
            "用户需确认：下一步无需合并 | 最终输出/覆盖口径：报告 worktree 未合并、未提交、未 push | "
            "trace-selftest |"
        )
    return "\n".join(rows)


def input_gate_section_text(variant: str = "complete") -> str:
    metadata = """## 用户输入拆解门禁

| 项 | 内容 |
|---|---|
| 原始输入/最新指令 | selftest 内置输入：验证 worktree evidence 和 task-id 精确命中。 |
| 任务数/要求数 | 2 个要求：REQ-SELFTEST-01 覆盖 worktree；REQ-SELFTEST-02 覆盖 task queue。 |
"""
    if variant == "prose":
        return (
            metadata
            + "\n这里用散文写是否记录、搜索判定和验证判定，但没有逐 REQ 表；应被门禁拒绝。\n"
        )

    headers = [
        "REQ ID",
        "用户要求摘要",
        "记录判定",
        "联网/搜索判定",
        "子 AI/验证判定",
        "验收/最终回复覆盖口径",
    ]
    rows = [
        [
            "REQ-SELFTEST-01",
            "验证修改型任务必须记录 worktree evidence。",
            "必须记录到用户要求追踪、触发日志和验证记录。",
            "不触发联网搜索；无需联网。",
            "不触发子 AI；触发 selftest、task-gate、失败路径和成功路径验证。",
            "最终回复覆盖 worktree evidence 的强制失败或通过。",
        ],
        [
            "REQ-SELFTEST-02",
            "验证 task queue 按显式 task-id 完成任务。",
            "必须记录到用户要求追踪、触发日志和验证记录。",
            "不触发联网搜索；无需联网。",
            "不触发子 AI；触发 selftest、task-gate、失败路径和成功路径验证。",
            "最终回复覆盖旧任务 cancelled、新任务 completed。",
        ],
    ]
    if variant == "missing-record":
        remove_index = headers.index("记录判定")
    elif variant == "missing-network":
        remove_index = headers.index("联网/搜索判定")
    elif variant == "missing-validation":
        remove_index = headers.index("子 AI/验证判定")
    else:
        remove_index = -1
    if remove_index >= 0:
        headers.pop(remove_index)
        rows = [row[:remove_index] + row[remove_index + 1 :] for row in rows]
    if variant == "missing-req":
        rows = rows[:1]

    lines = [
        metadata,
        "| " + " | ".join(headers) + " |",
        "|" + "|".join(["---"] * len(headers)) + "|",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(lines) + "\n"


def tracking_text(
    *,
    include_status: bool,
    include_input_gate: bool = True,
    input_gate_variant: str = "complete",
    user_requirement_variant: str = "complete",
    include_worktree_completion: bool = True,
    worktree_output_variant: str = "complete",
) -> str:
    status_row = (
        "| 当前 git status --short | 量化统计：1 条 status 证据，示例 ` M AGENTS.md`。 |"
        if include_status
        else ""
    )
    input_gate_section = input_gate_section_text(input_gate_variant) if include_input_gate else ""
    worktree_completion_section = (
        """## Worktree 完成记录

| 项 | 状态 |
|---|---|
| worktree 是否完成 | selftest 临时 worktree 证据生成已完成。 |
| 是否合并回源仓库 | 未合并；selftest 不执行真实合并。 |
| 是否 stage/commit | 未 stage、未 commit；selftest 只写临时目录。 |
| 是否 push | 未 push；selftest 不触碰远端。 |
| 下一步/用户需确认 | 无需用户确认；通过/失败由 selftest exit code 表示。 |
"""
        if include_worktree_completion
        else ""
    )
    user_requirement_section = """## 用户要求追踪门禁

| ID | 用户要求 | 关联批准 | 当前状态 | 处理动作 | 实现证据 | 验证证据 | 最终回复覆盖口径 |
|---|---|---|---|---|---|---|---|
| REQ-SELFTEST-01 | 验证修改型任务必须记录 worktree evidence。 | `批准：selftest` | 已完成 | 生成临时 tracking 并运行 task-gate。 | Worktree 证据节。 | task-gate 退出码。 | 报告强制失败或通过。 |
| REQ-SELFTEST-02 | 验证 task queue 按显式 task-id 完成任务。 | `批准：selftest` | 已完成 | 生成临时 queue 并运行 complete --task-id。 | 临时 task-queue.json。 | validate 退出码和任务状态。 | 报告旧任务 cancelled、新任务 completed。 |
"""
    if user_requirement_variant == "prose":
        user_requirement_section = """## 用户要求追踪门禁

REQ-SELFTEST-01 已完成，REQ-SELFTEST-02 已完成；这里故意不用标准 Markdown 表，必须失败。
"""

    return f"""# selftest worktree enforcement

{user_requirement_section}

{input_gate_section}

## 要求触发日志

| 触发 ID | 触发来源 | 命中的要求或规则 | 优先级/最高判断 | 适用范围 | 是否扩大范围 | 判断原因 | 必须动作 | 已执行步骤 | 量化证据 | 状态 | trace_id |
|---|---|---|---|---|---|---|---|---|---|---|---|
| TRG-SELFTEST-01 | selftest | git worktree 证据门禁 | 高优先级 | 适用范围：修改型任务 | 未扩大 | 因为缺证据必须失败 | 必须执行 task-gate | 已执行 1 次 CLI | 量化统计：1 次 task-gate | 已完成 | trace-selftest |

## 输出信息门禁

| 输出 ID | 输出类型 | 适用范围 | 排除范围 | 涉及对象 | 事实源/证据 | 已完成 | 未完成 | 未验证 | 阻塞 | 用户需确认 | 最终输出/覆盖口径 | trace_id |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
{output_gate_rows(worktree_output_variant)}

## 子 AI 验收矩阵

| 子 AI | 覆盖 REQ | 覆盖门禁 | 全面覆盖判定 | 失败路径 | 成功路径 | 发现问题/修复复测 |
|---|---|---|---|---|---|---|
| selftest-local | REQ-SELFTEST-01、REQ-SELFTEST-02 | 输入拆解门禁、输出门禁、git worktree 门禁、task queue 门禁 | 全面覆盖本 selftest 范围，无遗漏 | 缺输入拆解、缺 status、泛化 worktree 输出均应失败 exit 1 | 补齐输入、status 和严格 closeout 后应通过 exit 0 | 无子 AI；本地黑盒发现即修复并复测 |

## 任务类型门禁

| 任务类型 | 必选门禁 | 证据位置 | 状态 |
|---|---|---|---|
| `git` | worktree、分支、基准提交、推送边界。 | Worktree 证据。 | 已完成 |

## Worktree 证据

| 项目 | 证据 |
|---|---|
| git worktree 命令 | `git worktree add <path> -b codex/selftest` |
| 固定 worktree 根 | `.codex/project/.worktree/selftest` |
| branch 分支 | `codex/selftest` |
| base commit 基准提交 | `abc1234` |
{status_row}

{worktree_completion_section}

## 当前 Git 边界

- 推送边界：selftest 不 push。
- PYTHONPYCACHEPREFIX：`.codex/project/cache/python-pycache`。

## 联网核对记录

| 来源 | 用途 | 结论 |
|---|---|---|
| 不适用 | selftest 只验证本地门禁强制行为。 | 无需联网；风险边界为不评价外部成熟做法。 |

## 适用范围门禁

| 维度 | 结论 |
|---|---|
| 适用范围/覆盖对象 | 覆盖对象是 worktree evidence 的黑盒强制测试。 |
| 适用场景 | 适用场景为修改型任务的 task-gate 验证。 |
| 排除范围/不适用 | 不适用业务文档、真实提交、远端 push 和外部联网核对。 |
| 实用性/可操作 | 一条命令生成临时 tracking 并检查失败/成功路径，人工步骤少。 |
| 成本/效率 | 量化指标为 2 次 task-gate 和 1 个临时目录，耗时低。 |
| 扩展性/兼容 | 后续可扩展更多黑盒路径，不保留旧 wrapper 兼容。 |
| 量化指标/统计口径/事实源 | 事实源是 selftest 命令退出码、stdout 和临时 tracking。 |

## 验证记录

| 命令 | 工作目录 | 结果 | 摘要 |
|---|---|---|---|
| `ai_client_governance.py task-gate` | selftest | 由 selftest 验证 | 缺 status 应失败，补齐 status 应通过。 |
"""


def replace_section(text: str, heading: str, replacement: str) -> str:
    pattern = re.compile(
        rf"^##\s+{re.escape(heading)}\s*$[\s\S]*?(?=^##\s+|\Z)",
        re.MULTILINE,
    )
    next_text, count = pattern.subn(lambda _: replacement.rstrip() + "\n\n", text, count=1)
    if count != 1:
        raise ValueError(f"section not found: {heading}")
    return next_text


def task_gate_command(root: Path, tracking: Path, *task_types: str) -> list[str]:
    selected_task_types = list(task_types or ("git",))
    return [
        sys.executable,
        str(ai_client_governance_entrypoint()),
        "task-gate",
        "--root",
        str(root),
        "--task-types",
        *selected_task_types,
        "--require-task-types",
        "--task-tracking",
        str(tracking),
    ]


def test_worktree_gate(root: Path, run_dir: Path) -> TestResult:
    bad_tracking = run_dir / "missing-worktree-status.md"
    good_tracking = run_dir / "complete-worktree-status.md"
    bad_tracking.write_text(tracking_text(include_status=False), encoding="utf-8", newline="\n")
    good_tracking.write_text(tracking_text(include_status=True), encoding="utf-8", newline="\n")

    commands: list[CommandResult] = []
    bad = run_command(task_gate_command(root, bad_tracking), cwd=root, env_root=root)
    commands.append(bad)
    good = run_command(task_gate_command(root, good_tracking), cwd=root, env_root=root)
    commands.append(good)

    bad_failed_for_status = bad.exit_code != 0 and "status evidence" in (bad.stdout + bad.stderr)
    good_passed = good.exit_code == 0
    return TestResult(
        name="worktree-evidence-required",
        passed=bad_failed_for_status and good_passed,
        summary=(
            "missing status evidence failed and complete evidence passed"
            if bad_failed_for_status and good_passed
            else "worktree evidence enforcement did not match expected behavior"
        ),
        commands=commands,
    )


def test_input_and_output_closeout_gate(root: Path, run_dir: Path) -> TestResult:
    missing_input = run_dir / "missing-input-decomposition.md"
    prose_user_requirements = run_dir / "prose-user-requirements.md"
    missing_record_classification = run_dir / "missing-record-classification.md"
    missing_input_classification = run_dir / "missing-input-classification.md"
    missing_validation_classification = run_dir / "missing-validation-classification.md"
    prose_input_classification = run_dir / "prose-input-classification.md"
    missing_req_coverage = run_dir / "missing-input-req-coverage.md"
    generic_output = run_dir / "generic-worktree-output.md"
    complete = run_dir / "strict-closeout-complete.md"
    missing_input.write_text(
        tracking_text(include_status=True, include_input_gate=False),
        encoding="utf-8",
        newline="\n",
    )
    prose_user_requirements.write_text(
        tracking_text(include_status=True, user_requirement_variant="prose"),
        encoding="utf-8",
        newline="\n",
    )
    missing_input_classification.write_text(
        tracking_text(include_status=True, input_gate_variant="missing-network"),
        encoding="utf-8",
        newline="\n",
    )
    missing_record_classification.write_text(
        tracking_text(include_status=True, input_gate_variant="missing-record"),
        encoding="utf-8",
        newline="\n",
    )
    missing_validation_classification.write_text(
        tracking_text(include_status=True, input_gate_variant="missing-validation"),
        encoding="utf-8",
        newline="\n",
    )
    prose_input_classification.write_text(
        tracking_text(include_status=True, input_gate_variant="prose"),
        encoding="utf-8",
        newline="\n",
    )
    missing_req_coverage.write_text(
        tracking_text(include_status=True, input_gate_variant="missing-req"),
        encoding="utf-8",
        newline="\n",
    )
    generic_output.write_text(
        tracking_text(include_status=True, worktree_output_variant="generic"),
        encoding="utf-8",
        newline="\n",
    )
    complete.write_text(tracking_text(include_status=True), encoding="utf-8", newline="\n")

    commands = [
        run_command(task_gate_command(root, missing_input), cwd=root, env_root=root),
        run_command(task_gate_command(root, prose_user_requirements), cwd=root, env_root=root),
        run_command(task_gate_command(root, missing_record_classification), cwd=root, env_root=root),
        run_command(task_gate_command(root, missing_input_classification), cwd=root, env_root=root),
        run_command(task_gate_command(root, missing_validation_classification), cwd=root, env_root=root),
        run_command(task_gate_command(root, prose_input_classification), cwd=root, env_root=root),
        run_command(task_gate_command(root, missing_req_coverage), cwd=root, env_root=root),
        run_command(task_gate_command(root, generic_output), cwd=root, env_root=root),
        run_command(task_gate_command(root, complete), cwd=root, env_root=root),
    ]
    missing_input_failed = commands[0].exit_code != 0 and "用户输入拆解门禁" in (
        commands[0].stdout + commands[0].stderr
    )
    prose_user_requirements_failed = commands[1].exit_code != 0 and "structured Markdown table" in (
        commands[1].stdout + commands[1].stderr
    )
    missing_record_failed = commands[2].exit_code != 0 and "per-REQ table" in (
        commands[2].stdout + commands[2].stderr
    )
    missing_classification_failed = commands[3].exit_code != 0 and "per-REQ table" in (
        commands[3].stdout + commands[3].stderr
    )
    missing_validation_failed = commands[4].exit_code != 0 and "per-REQ table" in (
        commands[4].stdout + commands[4].stderr
    )
    prose_input_failed = commands[5].exit_code != 0 and "per-REQ table" in (
        commands[5].stdout + commands[5].stderr
    )
    missing_req_failed = commands[6].exit_code != 0 and "missing REQ coverage" in (
        commands[6].stdout + commands[6].stderr
    )
    generic_output_failed = commands[7].exit_code != 0 and "explicit merge closeout status" in (
        commands[7].stdout + commands[7].stderr
    )
    complete_passed = commands[8].exit_code == 0
    passed = (
        missing_input_failed
        and prose_user_requirements_failed
        and missing_record_failed
        and missing_classification_failed
        and missing_validation_failed
        and prose_input_failed
        and missing_req_failed
        and generic_output_failed
        and complete_passed
    )
    return TestResult(
        name="input-and-worktree-output-closeout-required",
        passed=passed,
        summary=(
            "missing input gate, prose user requirements, record/network/subagent columns, prose input, missing REQ coverage, and generic worktree output failed; strict closeout passed"
            if passed
            else "input or worktree output closeout enforcement did not match expected behavior"
        ),
        commands=commands,
    )


def test_multi_agent_acceptance_matrix_gate(root: Path, run_dir: Path) -> TestResult:
    complete_text = tracking_text(include_status=True)
    missing_matrix = run_dir / "missing-multi-agent-matrix.md"
    incomplete_matrix = run_dir / "incomplete-multi-agent-matrix.md"
    prose_matrix = run_dir / "prose-multi-agent-matrix.md"
    complete = run_dir / "complete-multi-agent-matrix.md"

    incomplete_section = """## 子 AI 验收矩阵

| 子 AI | 覆盖 REQ | 覆盖门禁 | 全面覆盖判定 | 成功路径 | 发现问题/修复复测 |
|---|---|---|---|---|---|
| selftest-local | REQ-SELFTEST-01、REQ-SELFTEST-02 | 输入拆解门禁、输出门禁、git worktree 门禁、task queue 门禁 | 全面覆盖本 selftest 范围，无遗漏 | 补齐输入、status 和严格 closeout 后应通过 exit 0 | 无问题 |
"""
    prose_section = """## 子 AI 验收矩阵

REQ-SELFTEST-01、REQ-SELFTEST-02 的门禁全面覆盖，失败路径 exit 1，成功路径 exit 0，发现问题后修复复测。
"""

    missing_matrix.write_text(
        replace_section(complete_text, "子 AI 验收矩阵", ""),
        encoding="utf-8",
        newline="\n",
    )
    incomplete_matrix.write_text(
        replace_section(complete_text, "子 AI 验收矩阵", incomplete_section),
        encoding="utf-8",
        newline="\n",
    )
    prose_matrix.write_text(
        replace_section(complete_text, "子 AI 验收矩阵", prose_section),
        encoding="utf-8",
        newline="\n",
    )
    complete.write_text(complete_text, encoding="utf-8", newline="\n")

    commands = [
        run_command(task_gate_command(root, missing_matrix, "multi-agent"), cwd=root, env_root=root),
        run_command(task_gate_command(root, incomplete_matrix, "multi-agent"), cwd=root, env_root=root),
        run_command(task_gate_command(root, prose_matrix, "multi-agent"), cwd=root, env_root=root),
        run_command(task_gate_command(root, complete, "multi-agent"), cwd=root, env_root=root),
    ]
    missing_failed = commands[0].exit_code != 0 and "子 AI 验收矩阵" in (
        commands[0].stdout + commands[0].stderr
    )
    incomplete_failed = commands[1].exit_code != 0 and "structured table" in (
        commands[1].stdout + commands[1].stderr
    )
    prose_failed = commands[2].exit_code != 0 and "structured table" in (
        commands[2].stdout + commands[2].stderr
    )
    complete_passed = commands[3].exit_code == 0
    passed = missing_failed and incomplete_failed and prose_failed and complete_passed
    return TestResult(
        name="multi-agent-acceptance-matrix-required",
        passed=passed,
        summary=(
            "missing, incomplete, and prose-only multi-agent matrices failed; complete matrix passed"
            if passed
            else "multi-agent acceptance matrix enforcement did not match expected behavior"
        ),
        commands=commands,
    )


def queue_command(root: Path, queue_file: Path, *args: str) -> list[str]:
    return [
        sys.executable,
        str(ai_client_governance_entrypoint()),
        "task-queue",
        "--root",
        str(root),
        "--queue-file",
        str(queue_file),
        *args,
    ]


def test_task_queue_task_id_priority(root: Path, run_dir: Path) -> TestResult:
    queue_file = run_dir / "task-queue.json"
    trace_id = "trace-selftest-task-id"
    tracking = ".codex/project/records/task-tracking/selftest-task-id.md"
    commands_to_run = [
        queue_command(
            root,
            queue_file,
            "enqueue",
            "--task-id",
            "TQ-selftest-old",
            "--title",
            "old task",
            "--message",
            "old same trace",
            "--task-tracking",
            tracking,
            "--approval-label",
            "批准：selftest",
            "--trace-id",
            trace_id,
            "--status",
            "ready",
        ),
        queue_command(root, queue_file, "start-next", "--task-id", "TQ-selftest-old"),
        queue_command(root, queue_file, "cancel", "--task-id", "TQ-selftest-old", "--reason", "selftest old"),
        queue_command(
            root,
            queue_file,
            "enqueue",
            "--task-id",
            "TQ-selftest-new",
            "--title",
            "new task",
            "--message",
            "new same trace",
            "--task-tracking",
            tracking,
            "--approval-label",
            "批准：selftest",
            "--trace-id",
            trace_id,
            "--status",
            "ready",
        ),
        queue_command(root, queue_file, "start-next", "--task-id", "TQ-selftest-new"),
        queue_command(
            root,
            queue_file,
            "complete",
            "--task-id",
            "TQ-selftest-new",
            "--trace-id",
            trace_id,
            "--task-tracking",
            tracking,
            "--summary",
            "selftest complete exact task id",
        ),
        queue_command(root, queue_file, "validate", "--trace-id", trace_id, "--current-task-tracking", tracking),
    ]

    commands = [run_command(command, cwd=root, env_root=root) for command in commands_to_run]
    state = json.loads(queue_file.read_text(encoding="utf-8"))
    statuses = {task["id"]: task["status"] for task in state.get("tasks", [])}
    passed = all(command.exit_code == 0 for command in commands) and statuses.get("TQ-selftest-old") == "cancelled" and statuses.get("TQ-selftest-new") == "completed"
    return TestResult(
        name="task-queue-task-id-priority",
        passed=passed,
        summary=(
            "complete --task-id completed the requested task without touching cancelled same-trace task"
            if passed
            else f"unexpected queue statuses: {statuses}"
        ),
        commands=commands,
    )


def test_gate_pool_validate_doc_tracking_context(root: Path, run_dir: Path) -> TestResult:
    tracking = run_dir / "gate-pool-doc-context.md"
    trace_id = f"trace-selftest-gate-pool-doc-context-{run_dir.name}"
    rel_tracking = tracking.relative_to(root).as_posix()
    tracking_body = replace_section(
        tracking_text(include_status=True),
        "验证记录",
        f"""## 验证记录

| 命令 | 工作目录 | 结果 | 摘要 |
|---|---|---|---|
| `{root}\\scripts\\ai_client_governance.py task-gate --root {root}` | selftest | 通过 | 这里故意保留绝对路径命令证据，必须依赖 task tracking 上下文通过 validate-doc。 |
""",
    )
    tracking_body += f"""
## 已处理文件

| 文件/目录 | 处理内容 | 归属 |
|---|---|---|
| `{rel_tracking}` | selftest gate-pool validate-doc tracking context 回归样本。 | selftest |

## 循环引用检查

- 检查范围：selftest 临时 task tracking。
- 检查方式：gate-pool validate-doc。
- 检查结果：无正式文档循环引用风险。

## 恢复现场

- 下一步：selftest 自动清理临时目录。
- 当前 Git 状态：不修改真实 Git 状态。
- 子 AI 状态：不适用。
- 禁止误动范围：不提交、不推送、不修改业务文档。

## 最终结论

- gate-pool validate-doc 必须携带 task tracking 上下文，否则此样本中的绝对路径命令证据会被普通文档规则误拦截。
"""
    tracking.write_text(tracking_body, encoding="utf-8", newline="\n")
    command = [
        sys.executable,
        str(ai_client_governance_entrypoint()),
        "gate-pool",
        "--root",
        str(root),
        "--task-tracking",
        str(tracking),
        "--changed-path",
        str(tracking),
        "--trace-id",
        trace_id,
    ]
    result = run_command(command, cwd=root, env_root=root)
    output = result.stdout + result.stderr
    passed = (
        result.exit_code == 0
        and "ai_client_governance.py validate-doc" in output
        and "status=failed" not in output
        and "Errors: 1" not in output
    )
    return TestResult(
        name="gate-pool-validate-doc-tracking-context",
        passed=passed,
        summary=(
            "gate-pool passed task tracking context into validate-doc for changed tracking files"
            if passed
            else "gate-pool validate-doc did not preserve task tracking context"
        ),
        commands=[result],
    )


def format_text(root: Path, run_dir: Path, results: list[TestResult]) -> str:
    lines = [
        "ai-client-governance Selftest Report",
        f"Root: {root}",
        f"Artifacts: {run_dir}",
        "",
    ]
    for result in results:
        lines.append(f"- {result.name}: {'PASS' if result.passed else 'FAIL'}")
        lines.append(f"  {result.summary}")
        for command in result.commands:
            lines.append(f"  command: {' '.join(command.command)}")
            lines.append(f"  exit: {command.exit_code}")
            if command.stdout.strip():
                lines.append(f"  stdout: {command.stdout.strip().splitlines()[0]}")
            if command.stderr.strip():
                lines.append(f"  stderr: {command.stderr.strip().splitlines()[0]}")
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    root = Path(args.root).resolve()
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = root / TMP_DIR / "ai-client-governance-selftest" / timestamp
    run_dir.mkdir(parents=True, exist_ok=True)

    results = [
        test_worktree_gate(root, run_dir),
        test_input_and_output_closeout_gate(root, run_dir),
        test_multi_agent_acceptance_matrix_gate(root, run_dir),
        test_task_queue_task_id_priority(root, run_dir),
        test_gate_pool_validate_doc_tracking_context(root, run_dir),
    ]
    passed = all(result.passed for result in results)

    if args.format == "json":
        print(
            json.dumps(
                {
                    "root": str(root),
                    "artifacts": str(run_dir),
                    "passed": passed,
                    "results": [asdict(result) for result in results],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(format_text(root, run_dir, results))

    if passed and not args.keep:
        resolved_tmp = (root / TMP_DIR).resolve()
        resolved_run = run_dir.resolve()
        if resolved_tmp in resolved_run.parents:
            shutil.rmtree(resolved_run)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
