"""Black-box self-tests for ai-client-governance enforcement behavior."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from ai_client_governance.common.paths import (
    PYTHON_PYCACHE_DIR,
    TMP_DIR,
    TOOL_INVOCATIONS_DIR,
    ai_client_governance_entrypoint,
    ai_client_governance_root,
)
from ai_client_governance.records import state_store
from ai_client_governance.records import task_record as structured_task_record
from ai_client_governance.worktree.coord import StateStore as CoordStateStore


SELFTEST_ARTIFACT_ENV = "AICG_SELFTEST_ARTIFACT_ROOT"
STATE_DB_ENV = "AICG_STATE_DB"
PYCACHE_PREFIX_ENV = "AICG_PYTHONPYCACHEPREFIX"


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
    parser.add_argument("--cleanup-stale", action="store_true", help="Remove stale selftest-owned artifacts before running.")
    parser.add_argument("--format", choices=("text", "json"), default="text", help="Output format.")
    return parser.parse_args()


def run_command(command: list[str], cwd: Path, env_root: Path, unset_env: list[str] | None = None) -> CommandResult:
    artifact_root = Path(os.environ.get(SELFTEST_ARTIFACT_ENV, str(env_root))).resolve()
    env = {
        **os.environ,
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPYCACHEPREFIX": str(artifact_root / PYTHON_PYCACHE_DIR),
        PYCACHE_PREFIX_ENV: str(artifact_root / PYTHON_PYCACHE_DIR),
        STATE_DB_ENV: str(artifact_root / "state" / "aicg-selftest.db"),
        "AICG_DOC_INDEX_OUTPUT": str(artifact_root / "doc-index" / "graph.json"),
    }
    for name in unset_env or []:
        env.pop(name, None)
    completed = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    return CommandResult(
        command=command,
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def remove_tree(path: Path) -> None:
    """Remove a temporary tree, retrying read-only Git object files on Windows."""
    def onerror(func: object, failed_path: str, _exc_info: object) -> None:
        os.chmod(failed_path, 0o700)
        func(failed_path)  # type: ignore[operator]

    shutil.rmtree(path, onerror=onerror)


def write_text_lf(path: Path, value: str) -> None:
    """Write text fixtures with LF endings so git diff --check is platform-stable."""
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(value)


def snapshot_ai_client_paths(root: Path) -> set[str]:
    base = root / ".ai-client"
    if not base.exists():
        return set()
    return {path.relative_to(root).as_posix() for path in base.rglob("*")}


def allowed_selftest_artifact_paths(root: Path, run_dir: Path) -> set[str]:
    allowed: set[str] = set()
    for path in [root / ".ai-client", root / ".ai-client" / "project", root / TMP_DIR, root / TMP_DIR / "ai-client-governance-selftest"]:
        try:
            allowed.add(path.relative_to(root).as_posix())
        except ValueError:
            continue
    try:
        run_rel = run_dir.relative_to(root).as_posix()
    except ValueError:
        return allowed
    allowed.add(run_rel)
    return allowed


def unexpected_ai_client_artifacts(root: Path, run_dir: Path, before: set[str]) -> list[str]:
    after = snapshot_ai_client_paths(root)
    try:
        run_rel = run_dir.relative_to(root).as_posix()
    except ValueError:
        run_rel = ""
    allowed = allowed_selftest_artifact_paths(root, run_dir)
    unexpected: list[str] = []
    for path in sorted(after - before):
        if path in allowed:
            continue
        if run_rel and path.startswith(run_rel + "/"):
            continue
        unexpected.append(path)
    return unexpected


def cleanup_empty_selftest_parents(root: Path, run_dir: Path, before: set[str]) -> None:
    for path in [run_dir.parent, root / TMP_DIR, root / ".ai-client" / "project", root / ".ai-client"]:
        rel = path.relative_to(root).as_posix()
        if rel in before:
            continue
        try:
            path.rmdir()
        except OSError:
            pass


def cleanup_stale_selftest_artifacts(root: Path) -> None:
    for path in [
        root / TMP_DIR / "ai-client-governance-selftest",
        root / PYTHON_PYCACHE_DIR,
        root / TOOL_INVOCATIONS_DIR,
    ]:
        if path.exists():
            remove_tree(path)
    for path in [
        root / PYTHON_PYCACHE_DIR.parent,
        root / TOOL_INVOCATIONS_DIR.parent,
        root / TMP_DIR,
        root / ".ai-client" / "project",
        root / ".ai-client",
    ]:
        try:
            path.rmdir()
        except OSError:
            pass
    for base in [root / "scripts", root / "src"]:
        if not base.exists():
            continue
        for path in sorted(base.rglob("__pycache__"), reverse=True):
            if path.is_dir():
                remove_tree(path)


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
| REQ-SELFTEST-02 | 验证 task queue 按显式 task-id 完成任务。 | `批准：selftest` | 已完成 | 生成临时 DB queue 并运行 complete --task-id。 | 临时 aicg.db。 | validate 退出码和任务状态。 | 报告旧任务 cancelled、新任务 completed。 |
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
| worktree 创建方式 | `worktree-task create --repo self --task-slug selftest` |
| 固定 worktree 根 | `.ai-client/project/.worktree/selftest` |
| branch 分支 | `codex/selftest` |
| base commit 基准提交 | `abc1234` |
| sparse checkout 策略 | 默认 sparse checkout 排除 `.source-projects`，需要时显式 `--include-source-projects`。 |
| 源码目录/快照处理 | `.source-projects` 默认不复制；本 selftest 不需要源码快照。 |
{status_row}

{worktree_completion_section}

## 当前 Git 边界

- 推送边界：selftest 不 push。
- PYTHONPYCACHEPREFIX：`.ai-client/project/cache/python-pycache`。

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
| 扩展性/可演进 | 后续可扩展更多黑盒路径，旧 wrapper 直接删除。 |
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


def worktree_creation_policy_command(root: Path, tracking: Path) -> list[str]:
    return [
        sys.executable,
        str(ai_client_governance_entrypoint()),
        "task-gate",
        "--root",
        str(root),
        "--task-tracking",
        str(tracking),
        "--only-worktree-creation-policy",
    ]


def test_worktree_creation_policy_gate(root: Path, run_dir: Path) -> TestResult:
    raw_git_tracking = run_dir / "raw-git-worktree-policy.md"
    missing_sparse_tracking = run_dir / "missing-sparse-policy.md"
    good_tracking = run_dir / "complete-worktree-policy.md"

    good_text = tracking_text(include_status=True)
    raw_git_tracking.write_text(
        good_text.replace(
            "| worktree 创建方式 | `worktree-task create --repo self --task-slug selftest` |",
            "| git worktree 命令 | `git worktree add <path> -b codex/selftest` |",
        ),
        encoding="utf-8",
        newline="\n",
    )
    missing_sparse_tracking.write_text(
        good_text.replace(
            "| sparse checkout 策略 | 默认 sparse checkout 排除 `.source-projects`，需要时显式 `--include-source-projects`。 |\n",
            "",
        ),
        encoding="utf-8",
        newline="\n",
    )
    good_tracking.write_text(good_text, encoding="utf-8", newline="\n")

    commands = [
        run_command(worktree_creation_policy_command(root, raw_git_tracking), cwd=root, env_root=root),
        run_command(worktree_creation_policy_command(root, missing_sparse_tracking), cwd=root, env_root=root),
        run_command(worktree_creation_policy_command(root, good_tracking), cwd=root, env_root=root),
    ]
    raw_git_failed = commands[0].exit_code != 0 and "raw git worktree add" in (
        commands[0].stdout + commands[0].stderr
    )
    missing_sparse_failed = commands[1].exit_code != 0 and "sparse checkout strategy" in (
        commands[1].stdout + commands[1].stderr
    )
    good_passed = commands[2].exit_code == 0
    passed = raw_git_failed and missing_sparse_failed and good_passed
    return TestResult(
        name="worktree-creation-policy-required",
        passed=passed,
        summary=(
            "raw git without break-glass reason and missing sparse policy failed; complete policy passed"
            if passed
            else "worktree creation policy enforcement did not match expected behavior"
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


def queue_command(root: Path, db: Path, *args: str) -> list[str]:
    return [
        sys.executable,
        str(ai_client_governance_entrypoint()),
        "task-queue",
        "--root",
        str(root),
        "--db",
        str(db),
        *args,
    ]


def test_task_queue_task_id_priority(root: Path, run_dir: Path) -> TestResult:
    db = run_dir / "aicg.db"
    trace_id = "trace-selftest-task-id"
    tracking = ".ai-client/project/records/task-tracking/selftest-task-id.md"
    commands_to_run = [
        queue_command(
            root,
            db,
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
        queue_command(root, db, "start-next", "--task-id", "TQ-selftest-old"),
        queue_command(root, db, "cancel", "--task-id", "TQ-selftest-old", "--reason", "selftest old"),
        queue_command(
            root,
            db,
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
        queue_command(root, db, "start-next", "--task-id", "TQ-selftest-new"),
        queue_command(
            root,
            db,
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
        queue_command(root, db, "validate", "--trace-id", trace_id, "--current-task-tracking", tracking),
        queue_command(root, db, "--format", "json", "status"),
        [
            sys.executable,
            str(ai_client_governance_entrypoint()),
            "task-queue",
            "status",
            "--db",
            str(db),
            "--format",
            "json",
        ],
        [
            sys.executable,
            str(ai_client_governance_entrypoint()),
            "task-queue",
            "lifecycle",
            "--db",
            str(db),
            "--task-id",
            "TQ-selftest-new",
            "--format",
            "json",
        ],
    ]

    commands = [run_command(command, cwd=root, env_root=root) for command in commands_to_run]
    old_order_status = commands[-3]
    subcommand_order_status = commands[-2]
    lifecycle_status = commands[-1]
    summary = json.loads(subcommand_order_status.stdout)
    lifecycle: dict[str, object] = {}
    try:
        lifecycle = json.loads(lifecycle_status.stdout)
    except json.JSONDecodeError:
        pass
    tasks = []
    for group in ("active", "ready", "waiting", "awaiting_approval", "candidates", "blocked"):
        tasks.extend(summary.get(group, []))
    statuses = {task["id"]: task["status"] for task in tasks}
    statuses.update(
        {
            task["id"]: task["status"]
            for task in summary.get("all_tasks", [])
            if isinstance(task, dict) and task.get("id")
        }
    )
    passed = (
        all(command.exit_code == 0 for command in commands)
        and statuses.get("TQ-selftest-old") == "cancelled"
        and statuses.get("TQ-selftest-new") == "completed"
        and "\"completed\"" in old_order_status.stdout
        and "\"completed\"" in subcommand_order_status.stdout
        and lifecycle.get("status_counts", {}).get("done") == 1
        and "missing_in_task_record" in lifecycle.get("warnings", [])
    )
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


def test_task_lifecycle_unified_status(root: Path, run_dir: Path) -> TestResult:
    task_id = "TQ-LIFECYCLE-SELFTEST"
    db = run_dir / "task-lifecycle-selftest.db"
    trace_id = "trace-structured-selftest"
    tracking = ".ai-client/project/records/task-tracking/lifecycle-selftest.md"
    payload = structured_payload(task_id, include_worktree=False)
    payload_path = run_dir / "task-lifecycle-record.json"
    payload_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    commands = [
        run_command(
            queue_command(
                root,
                db,
                "enqueue",
                "--task-id",
                task_id,
                "--title",
                "lifecycle selftest",
                "--message",
                "lifecycle selftest",
                "--task-tracking",
                tracking,
                "--approval-label",
                "批准：selftest",
                "--trace-id",
                trace_id,
                "--status",
                "ready",
            ),
            cwd=root,
            env_root=root,
        ),
        run_command(queue_command(root, db, "start-next", "--task-id", task_id), cwd=root, env_root=root),
        run_command(queue_command(root, db, "complete", "--task-id", task_id, "--trace-id", trace_id), cwd=root, env_root=root),
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "task-record",
                "apply",
                "--db",
                str(db),
                "--json",
                str(payload_path),
                "--replace",
                "--format",
                "json",
            ],
            cwd=root,
            env_root=root,
        ),
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "task-queue",
                "lifecycle",
                "--db",
                str(db),
                "--task-id",
                task_id,
                "--format",
                "json",
            ],
            cwd=root,
            env_root=root,
        ),
    ]
    lifecycle: dict[str, object] = {}
    try:
        lifecycle = json.loads(commands[-1].stdout)
    except json.JSONDecodeError:
        pass
    entries = lifecycle.get("entries", []) if isinstance(lifecycle.get("entries"), list) else []
    first = entries[0] if entries and isinstance(entries[0], dict) else {}
    passed = (
        all(command.exit_code == 0 for command in commands)
        and lifecycle.get("status_counts", {}).get("done") == 1
        and lifecycle.get("warnings") == []
        and first.get("lifecycle_status") == "done"
        and first.get("task_queue", {}).get("raw_status") == "completed"
        and first.get("task_record", {}).get("raw_status") == "done"
    )
    return TestResult(
        name="task-lifecycle-unified-status",
        passed=passed,
        summary=(
            "task-queue lifecycle normalizes queue completed and task-record done without drift"
            if passed
            else "task lifecycle unified status regression failed"
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


def structured_payload(task_id: str, include_worktree: bool = True) -> dict[str, object]:
    outputs = []
    for output_type in ("plan", "status", "final", "script", "error", "git_worktree"):
        outputs.append(
            {
                "output_id": f"OUT-{task_id}-{output_type}",
                "output_type": output_type,
                "applicability_scope": "structured selftest scope",
                "exclusions": "does not cover business files",
                "objects": "temporary SQLite database",
                "fact_source": "selftest generated JSON",
                "completed": "structured record was generated",
                "unfinished": "none",
                "unverified": "none",
                "blocked": "none",
                "user_confirmation": "none",
                "final_coverage": "selftest reports pass or fail",
                "trace_id": "trace-structured-selftest",
            }
        )
    payload: dict[str, object] = {
        "task": {
            "task_id": task_id,
            "title": "structured task record selftest",
            "status": "done",
            "task_types": ["rules-script", "docs"],
            "task_size": "medium",
            "approval_label": "批准：selftest",
            "trace_id": "trace-structured-selftest",
        },
        "approvals": [
            {
                "approval_id": f"APR-{task_id}",
                "label": "批准：selftest",
                "status": "approved",
                "summary": "selftest approval",
            }
        ],
        "requirements": [
            {
                "requirement_id": f"REQ-{task_id}-01",
                "summary": "verify typed task records reject missing fields and pass complete records",
                "record_decision": "recorded in SQLite",
                "network_decision": "not applicable for local selftest",
                "validation_decision": "task-record gate and task-gate --task-id",
                "acceptance": "complete structured record passes gates",
                "status": "done",
                "action": "generated structured payload",
                "implementation_evidence": "temporary JSON and SQLite database",
                "validation_evidence": "task-record gate pass",
                "final_coverage": "selftest summary includes structured gate result",
            }
        ],
        "triggers": [
            {
                "trigger_id": f"TRG-{task_id}-01",
                "trigger_type": "user-message",
                "source": "selftest user message",
                "matched_requirement": f"REQ-{task_id}-01",
                "priority": "high",
                "applicability_scope": "typed task record gate",
                "scope_expansion": "not expanded",
                "reason": "Markdown reverse parsing is inefficient",
                "required_action": "write structured DB before gate",
                "executed_steps": "apply payload and run gates",
                "quantitative_evidence": "one invalid payload and one valid payload",
                "status": "done",
                "trace_id": "trace-structured-selftest",
            },
            {
                "trigger_id": f"TRG-{task_id}-SCOPE",
                "trigger_type": "scope-classification",
                "source": "selftest structured payload",
                "matched_requirement": f"REQ-{task_id}-01",
                "priority": "high",
                "applicability_scope": "ai-client-governance-common",
                "scope_expansion": "not expanded",
                "reason": "selftest payload represents common governance structured record behavior",
                "required_action": "record common/project/native scope before gated work",
                "executed_steps": "included scope-classification trigger and event payload",
                "quantitative_evidence": "scope_kind=ai-client-governance-common",
                "status": "done",
                "trace_id": "trace-structured-selftest",
            }
        ],
        "outputs": outputs,
        "events": [
            {
                "event_id": f"EVT-{task_id}-INPUT-FILTER",
                "event_type": "input-filter.preflight",
                "payload": {
                    "join_point": "user-message",
                    "requirement_count": 1,
                    "scope_kind": "ai-client-governance-common",
                    "scope_reason": "selftest common governance payload",
                    "scope_paths": ["src/ai_client_governance/records/task_record.py"],
                    "filter_chain": [
                        "classify-source",
                        "user-claim-validation",
                        "classify-common-project-scope",
                        "decompose-requirements",
                        "recordability-judgement",
                        "network-search-judgement",
                        "acceptance-extract",
                    ],
                    "fail_policy": "fail_closed",
                },
            },
            {
                "event_id": f"EVT-{task_id}-COMMAND-COMPRESSION",
                "event_type": "command-compression.analysis",
                "payload": {
                    "join_point": "write-intent",
                    "scope_kind": "ai-client-governance-common",
                    "scope_reason": "selftest common governance payload",
                    "scope_paths": ["src/ai_client_governance/records/task_record.py"],
                    "decision": "selftest records command compression before mutating task gates",
                    "selected_pattern": "local-command-compression",
                    "command_count_before": 2,
                    "command_count_after": 1,
                    "groups": [
                        {
                            "group_id": "selftest-readonly",
                            "execution": "parallel-ok",
                            "cache": "readonly-only",
                            "commands": ["contract describe", "task-record gate"],
                        }
                    ],
                },
            },
            {
                "event_id": f"EVT-{task_id}-PLAN-APPROVAL-BOUNDARY",
                "event_type": structured_task_record.PLAN_APPROVAL_BOUNDARY_EVENT,
                "payload": {
                    "join_point": "plan-output",
                    "requires_approval": True,
                    "approval_label": "批准：selftest",
                    "approval_status": "approved",
                    "execution_policy": "approved-local-only-no-push",
                    "push_policy": "push_requires_separate_approval",
                    "commit_policy": "local_commit_allowed_when_approved",
                    "fail_policy": "fail_closed",
                },
            },
            {
                "event_id": f"EVT-{task_id}-USER-CLAIM-VALIDATION",
                "event_type": structured_task_record.USER_CLAIM_VALIDATION_EVENT,
                "payload": {
                    "join_point": "user-message",
                    "execution_policy": "execute-with-recorded-claims",
                    "claims": [
                        {
                            "claim_id": "CLAIM-SELFTEST-01",
                            "requirement_id": f"REQ-{task_id}-01",
                            "claim_summary": "selftest payload asserts complete records should pass",
                            "source": "user",
                            "trust_level": "user-assertion-needs-verification",
                            "risk_flags": ["source_is_user", "affects_execution_or_repository_state"],
                            "verification_action": "verify-local-live-state-or-script-contract-before-execution",
                        }
                    ],
                    "fail_policy": "fail_closed",
                },
            },
            {
                "event_id": f"EVT-{task_id}-STATE-ARTIFACT-OWNERSHIP",
                "event_type": structured_task_record.STATE_ARTIFACT_OWNERSHIP_EVENT,
                "payload": {
                    "join_point": "write-intent",
                    "owner_policy": "selftest scripts own their generated state and cleanup",
                    "generated_state_classes": ["lifecycle-state", "doc-index", "python-pycache", "selftest-artifact"],
                    "manual_edit_policy": "forbidden_without_break_glass",
                    "cleanup_policy": "selftest cleans allowed artifacts and fails on unexpected artifacts",
                    "fail_policy": "fail_closed",
                },
            },
            {
                "event_id": f"EVT-{task_id}-PATCH-PREFLIGHT",
                "event_type": structured_task_record.PATCH_PREFLIGHT_EVENT,
                "payload": {
                    "join_point": "write-intent",
                    "anchor_policy": "verify_unique_or_reextract",
                    "apply_policy": "small_step_patch",
                    "fallback_policy": "use narrower context when anchors are unstable",
                    "fail_policy": "fail_closed",
                },
            }
        ],
        "validations": [
            {
                "validation_id": f"VAL-{task_id}-01",
                "command": "ai_client_governance.py validate-doc --selftest",
                "cwd": "selftest",
                "result": "pass",
                "summary": "validate-doc/doc-index style validation row present for docs task",
                "evidence": "selftest fixture",
            }
        ],
    }
    if include_worktree:
        payload["worktrees"] = [
            {
                "worktree_id": f"WT-{task_id}-01",
                "repo": "ai-client-governance",
                "source_repo": "selftest",
                "path": "selftest",
                "branch": "codex/selftest",
                "base_commit": "selftest",
                "creation_method": "worktree-task",
                "sparse_policy": "no source snapshots included",
                "source_handling": "worktree-task create is the default path",
                "status": "done",
                "merged_status": "not_required",
                "commit_status": "not_required",
                "push_status": "not_required",
                "next_action": "none",
            }
        ]
    return payload


def payload_without_event(task_id: str, event_type: str) -> dict[str, object]:
    payload = structured_payload(task_id)
    payload["events"] = [
        event
        for event in payload["events"]  # type: ignore[index]
        if isinstance(event, dict) and event.get("event_type") != event_type
    ]
    return payload


def test_structured_task_record_gate(root: Path, run_dir: Path) -> TestResult:
    task_id = "STRUCT-SELFTEST"
    db = run_dir / "structured-selftest.db"
    invalid_payload = structured_payload(task_id + "-BAD")
    invalid_payload["requirements"] = []
    missing_filter_payload = structured_payload(task_id + "-NOFILTER")
    missing_filter_payload["events"] = []
    missing_filter_payload["triggers"][0]["trigger_type"] = "selftest"
    no_worktree_payload = structured_payload(task_id + "-NOWORKTREE", include_worktree=False)
    invalid = run_dir / "structured-invalid.json"
    missing_filter = run_dir / "structured-missing-input-filter.json"
    no_worktree = run_dir / "structured-no-worktree.json"
    valid = run_dir / "structured-valid.json"
    invalid.write_text(json.dumps(invalid_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    missing_filter.write_text(json.dumps(missing_filter_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    no_worktree.write_text(json.dumps(no_worktree_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    valid.write_text(json.dumps(structured_payload(task_id), ensure_ascii=False, indent=2), encoding="utf-8")

    commands = [
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "contract",
                "describe",
                "--task-type",
                "rules-script",
                "--task-type",
                "docs",
                "--event",
                "write-intent",
            ],
            cwd=root,
            env_root=root,
        ),
        run_command(
            [sys.executable, str(ai_client_governance_entrypoint()), "task-record", "--db", str(db), "init"],
            cwd=root,
            env_root=root,
        ),
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "task-record",
                "--db",
                str(db),
                "apply",
                "--json",
                str(invalid),
            ],
            cwd=root,
            env_root=root,
        ),
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "task-record",
                "--db",
                str(db),
                "apply",
                "--json",
                str(no_worktree),
            ],
            cwd=root,
            env_root=root,
        ),
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "task-record",
                "--db",
                str(db),
                "gate",
                "--task-id",
                task_id + "-NOWORKTREE",
                "--event",
                "preflight",
            ],
            cwd=root,
            env_root=root,
        ),
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "task-record",
                "--db",
                str(db),
                "gate",
                "--task-id",
                task_id + "-NOWORKTREE",
            ],
            cwd=root,
            env_root=root,
        ),
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "task-record",
                "--db",
                str(db),
                "apply",
                "--json",
                str(missing_filter),
            ],
            cwd=root,
            env_root=root,
        ),
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "task-record",
                "--db",
                str(db),
                "gate",
                "--task-id",
                task_id + "-NOFILTER",
                "--event",
                "preflight",
            ],
            cwd=root,
            env_root=root,
        ),
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "task-record",
                "--db",
                str(db),
                "apply",
                "--json",
                str(valid),
            ],
            cwd=root,
            env_root=root,
        ),
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "task-record",
                "--db",
                str(db),
                "gate",
                "--task-id",
                task_id,
                "--event",
                "preflight",
            ],
            cwd=root,
            env_root=root,
        ),
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "task-record",
                "--db",
                str(db),
                "gate",
                "--task-id",
                task_id,
            ],
            cwd=root,
            env_root=root,
        ),
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "task-record",
                "status",
                "--db",
                str(db),
                "--format",
                "json",
                "--task-id",
                task_id,
            ],
            cwd=root,
            env_root=root,
        ),
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "task-gate",
                "--db",
                str(db),
                "--task-id",
                task_id,
            ],
            cwd=root,
            env_root=root,
        ),
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "gate-pool",
                "--db",
                str(db),
                "--task-id",
                task_id,
                "--task-type",
                "rules-script",
                "--task-type",
                "docs",
                "--final",
                "--dry-run",
            ],
            cwd=root,
            env_root=root,
        ),
    ]
    (
        contract_describe,
        init_db,
        invalid_apply,
        no_worktree_apply,
        no_worktree_preflight,
        no_worktree_final,
        missing_filter_apply,
        missing_filter_preflight,
        valid_apply,
        valid_preflight,
        valid_final,
        status_new_order,
        task_gate,
        gate_pool,
    ) = commands
    passed = (
        contract_describe.exit_code == 0
        and init_db.exit_code == 0
        and invalid_apply.exit_code != 0
        and no_worktree_apply.exit_code == 0
        and no_worktree_preflight.exit_code == 0
        and no_worktree_final.exit_code != 0
        and missing_filter_apply.exit_code == 0
        and missing_filter_preflight.exit_code != 0
        and valid_apply.exit_code == 0
        and valid_preflight.exit_code == 0
        and valid_final.exit_code == 0
        and status_new_order.exit_code == 0
        and task_gate.exit_code == 0
        and gate_pool.exit_code == 0
        and "requirements must contain at least one row" in (invalid_apply.stdout + invalid_apply.stderr)
        and "mutating task has no worktree evidence yet" in (no_worktree_preflight.stdout + no_worktree_preflight.stderr)
        and "mutating tasks require worktree evidence" in (no_worktree_final.stdout + no_worktree_final.stderr)
        and "input-filter preflight requires" in (missing_filter_preflight.stdout + missing_filter_preflight.stderr)
        and "\"exists\": true" in status_new_order.stdout
        and "--task-id" in gate_pool.stdout
    )
    return TestResult(
        name="structured-task-record-gate",
        passed=passed,
        summary=(
            "structured task records reject missing rows, fail closed without input-filter facts, and pass DB-backed gates"
            if passed
            else "structured task record gate regression failed"
        ),
        commands=commands,
    )


def test_preflight_boundary_hardening(root: Path, run_dir: Path) -> TestResult:
    db = run_dir / "preflight-boundary-hardening.db"
    valid_task = "PREFLIGHT-HARDENING-VALID"
    missing_plan_task = "PREFLIGHT-HARDENING-NOPLAN"
    missing_claim_task = "PREFLIGHT-HARDENING-NOCLAIM"
    valid = run_dir / "preflight-hardening-valid.json"
    missing_plan = run_dir / "preflight-hardening-missing-plan.json"
    missing_claim = run_dir / "preflight-hardening-missing-claim.json"
    valid.write_text(json.dumps(structured_payload(valid_task), ensure_ascii=False, indent=2), encoding="utf-8")
    missing_plan.write_text(
        json.dumps(payload_without_event(missing_plan_task, structured_task_record.PLAN_APPROVAL_BOUNDARY_EVENT), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    missing_claim.write_text(
        json.dumps(payload_without_event(missing_claim_task, structured_task_record.USER_CLAIM_VALIDATION_EVENT), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    commands = [
        run_command([sys.executable, str(ai_client_governance_entrypoint()), "task-record", "--db", str(db), "init"], cwd=root, env_root=root),
        run_command([sys.executable, str(ai_client_governance_entrypoint()), "task-record", "--db", str(db), "apply", "--json", str(missing_plan)], cwd=root, env_root=root),
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "task-record",
                "--db",
                str(db),
                "gate",
                "--task-id",
                missing_plan_task,
                "--event",
                "preflight",
            ],
            cwd=root,
            env_root=root,
        ),
        run_command([sys.executable, str(ai_client_governance_entrypoint()), "task-record", "--db", str(db), "apply", "--json", str(missing_claim)], cwd=root, env_root=root),
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "task-record",
                "--db",
                str(db),
                "gate",
                "--task-id",
                missing_claim_task,
                "--event",
                "preflight",
            ],
            cwd=root,
            env_root=root,
        ),
        run_command([sys.executable, str(ai_client_governance_entrypoint()), "task-record", "--db", str(db), "apply", "--json", str(valid)], cwd=root, env_root=root),
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "task-record",
                "--db",
                str(db),
                "gate",
                "--task-id",
                valid_task,
                "--event",
                "preflight",
            ],
            cwd=root,
            env_root=root,
        ),
    ]
    missing_plan_output = commands[2].stdout + commands[2].stderr
    missing_claim_output = commands[4].stdout + commands[4].stderr
    valid_output = commands[6].stdout + commands[6].stderr
    passed = (
        commands[0].exit_code == 0
        and commands[1].exit_code == 0
        and commands[2].exit_code != 0
        and commands[3].exit_code == 0
        and commands[4].exit_code != 0
        and commands[5].exit_code == 0
        and commands[6].exit_code == 0
        and "plan approval boundary requires" in missing_plan_output
        and "user claim validation requires" in missing_claim_output
        and "Errors: 0" in valid_output
    )
    return TestResult(
        name="preflight-boundary-hardening",
        passed=passed,
        summary=(
            "preflight fails closed without plan approval boundary or user claim validation facts"
            if passed
            else "preflight hardening gate regression failed"
        ),
        commands=commands,
    )


def test_tool_flow_accepts_task_record_gate(root: Path, run_dir: Path) -> TestResult:
    jsonl_artifact_dir = run_dir / "tool-flow-task-record-jsonl"
    jsonl_artifact_dir.mkdir(parents=True, exist_ok=True)
    trace_id = "trace-selftest-tool-flow-task-record"
    events = [
        {
            "invocation_id": "selftest-task-record-gate",
            "timestamp": "2026-06-17T12:00:00+00:00",
            "name": "ai_client_governance.py task-record gate",
            "status": "succeeded",
            "command": "ai_client_governance.py task-record gate --task-id SELFTEST",
            "exit_code": 0,
            "final_gate": True,
            "phase": "final-gate",
            "trace_id": trace_id,
        },
        {
            "invocation_id": "selftest-session-gate",
            "timestamp": "2026-06-17T12:00:01+00:00",
            "name": "ai_client_governance.py session-gate",
            "status": "succeeded",
            "command": "ai_client_governance.py session-gate --task-id SELFTEST",
            "exit_code": 0,
            "final_gate": True,
            "phase": "final-gate",
            "trace_id": trace_id,
        },
        {
            "invocation_id": "selftest-telemetry-report",
            "timestamp": "2026-06-17T12:00:02+00:00",
            "name": "ai_client_governance.py telemetry",
            "status": "succeeded",
            "command": "ai_client_governance.py telemetry report --trace-id " + trace_id,
            "exit_code": 0,
            "phase": "report",
            "trace_id": trace_id,
        },
    ]
    write_text_lf(
        jsonl_artifact_dir / "2026-06.jsonl",
        "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
    )
    commands = [
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "tool-flow",
                "--root",
                str(root),
                "--jsonl-artifact-dir",
                str(jsonl_artifact_dir),
                "--trace-id",
                trace_id,
                "--format",
                "text",
                "--require-task-session-order",
                "--fail-on-warning",
            ],
            cwd=root,
            env_root=root,
        )
    ]
    output = commands[0].stdout + commands[0].stderr
    passed = commands[0].exit_code == 0 and "Successful session gate appears" not in output
    return TestResult(
        name="tool-flow-accepts-task-record-gate",
        passed=passed,
        summary=(
            "tool-flow treats structured task-record gate as a prior task gate before session-gate"
            if passed
            else "tool-flow still reports a false task/session ordering issue for task-record gate"
        ),
        commands=commands,
    )


def test_lifecycle_input_filter_preflight(root: Path, run_dir: Path) -> TestResult:
    task_id = "INPUT-FILTER-SELFTEST"
    db = run_dir / "input-filter-selftest.db"
    generated = run_dir / "input-filter-task-record.json"
    message = "Check parsed user request rows."
    commands = [
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "runtime",
                "components",
                "--event",
                "user-message",
                "--kind",
                "input-filter",
                "--format",
                "json",
            ],
            cwd=root,
            env_root=root,
        ),
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "lifecycle",
                "input-filter",
                "--message",
                message,
                "--task-id",
                task_id,
                "--title",
                "input filter lifecycle selftest",
                "--task-type",
                "code-debug",
                "--db",
                str(db),
                "--task-record-json",
                str(generated),
                "--apply-task-record",
                "--format",
                "json",
            ],
            cwd=root,
            env_root=root,
        ),
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "task-record",
                "--db",
                str(db),
                "gate",
                "--task-id",
                task_id,
                "--event",
                "preflight",
            ],
            cwd=root,
            env_root=root,
        ),
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "lifecycle",
                "preflight",
                "--message",
                message,
                "--task-id",
                task_id,
                "--task-type",
                "code-debug",
                "--db",
                str(db),
                "--analysis-summary",
                "Verify lifecycle input-filter facts before preflight.",
                "--analysis-scope",
                "src/ai_client_governance/lifecycle/engine.py",
                "--non-goal",
                "Do not exercise repository writes in this selftest.",
                "--risk",
                "Analysis-contract gates must not hide missing input-filter facts.",
                "--acceptance",
                "Preflight passes when task-record facts and analysis contract are complete.",
                "--format",
                "json",
            ],
            cwd=root,
            env_root=root,
        ),
    ]
    component_output = commands[0].stdout + commands[0].stderr
    input_filter_output = commands[1].stdout + commands[1].stderr
    gate_output = commands[2].stdout + commands[2].stderr
    lifecycle_output = commands[3].stdout + commands[3].stderr
    passed = (
        all(command.exit_code == 0 for command in commands)
        and generated.exists()
        and "input.filter.user-message-preflight" in component_output
        and "input.filter.user-claim-validation" in component_output
        and "\"fail_policy\": \"fail_closed\"" in component_output
        and "\"event_type\": \"input-filter.preflight\"" in input_filter_output
        and "\"event_type\": \"command-compression.analysis\"" in input_filter_output
        and f"\"event_type\": \"{structured_task_record.PLAN_APPROVAL_BOUNDARY_EVENT}\"" in input_filter_output
        and f"\"event_type\": \"{structured_task_record.USER_CLAIM_VALIDATION_EVENT}\"" in input_filter_output
        and "\"state_db\"" in input_filter_output
        and "\"scope_kind\"" in input_filter_output
        and "scope-classification" in input_filter_output
        and "input-filter preflight facts present" in gate_output
        and "task-record preflight gate passed" in lifecycle_output
        and "analysis contract passed" in lifecycle_output
    )
    return TestResult(
        name="lifecycle-input-filter-preflight",
        passed=passed,
        summary=(
            "lifecycle input-filter emits structured facts and preflight gates pass when those facts exist"
            if passed
            else "lifecycle input-filter preflight regression failed"
        ),
        commands=commands,
    )


def test_task_run_command_compression_plan(root: Path, run_dir: Path) -> TestResult:
    host_project = run_dir / "task-run-host-project"
    embedded = host_project / ".ai-client" / "ai-client-governance"
    governance_root = ai_client_governance_root()
    (embedded / "scripts").mkdir(parents=True, exist_ok=True)
    (embedded / "src" / "ai_client_governance").mkdir(parents=True, exist_ok=True)
    (embedded / "scripts" / "ai_client_governance.py").write_text("# selftest embedded entry\n", encoding="utf-8")
    (embedded / "manifest.json").write_text('{"name":"ai-client-governance"}\n', encoding="utf-8")
    commands = [
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "runtime",
                "components",
                "--event",
                "write-intent",
                "--task-type",
                "rules-script",
                "--task-size",
                "medium",
                "--kind",
                "processing-interceptor",
                "--format",
                "json",
            ],
            cwd=root,
            env_root=root,
        ),
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "runtime",
                "components",
                "--event",
                "write-intent",
                "--task-type",
                "code-debug",
                "--task-size",
                "small",
                "--kind",
                "processing-interceptor",
                "--format",
                "json",
            ],
            cwd=root,
            env_root=root,
        ),
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "task-run",
                "plan",
                "--task-id",
                "TASK-RUN-SELFTEST",
                "--task-type",
                "rules-script",
                "--task-type",
                "docs",
                "--event",
                "write-intent",
                "--changed-path",
                "AGENTS.md",
                "--command",
                "git status --short --branch",
                "--command",
                "git status --short --branch",
                "--command",
                "python scripts/ai_client_governance.py validate-doc --root .",
                "--command",
                "git add AGENTS.md",
                "--format",
                "json",
            ],
            cwd=governance_root,
            env_root=root,
        ),
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "task-run",
                "--root",
                str(host_project),
                "plan",
                "--task-id",
                "TASK-RUN-HOST-SELFTEST",
                "--task-type",
                "rules-script",
                "--event",
                "write-intent",
                "--changed-path",
                "src/example.py",
                "--format",
                "json",
            ],
            cwd=root,
            env_root=root,
        ),
    ]
    component_output = commands[0].stdout + commands[0].stderr
    mutating_component_output = commands[1].stdout + commands[1].stderr
    plan_output = commands[2].stdout + commands[2].stderr
    host_plan_output = commands[3].stdout + commands[3].stderr
    passed = (
        all(command.exit_code == 0 for command in commands)
        and "preflight.interceptor.command-compression" in component_output
        and "preflight.interceptor.scope-classification" in component_output
        and "preflight.interceptor.command-compression.mutating" in mutating_component_output
        and "preflight.interceptor.task-run-dag.mutating" in mutating_component_output
        and "\"event_type\": \"command-compression.analysis\"" in plan_output
        and "\"scope_kind\": \"ai-client-governance-common\"" in plan_output
        and "\"skipped_duplicate_count\": 1" in plan_output
        and "\"approval_required_count\": 1" in plan_output
        and "\"capability\": \"git_write\"" in plan_output
        and "\"side_effect\": \"persistent\"" in plan_output
        and "local-command-compression" in plan_output
        and ".ai-client/ai-client-governance/scripts/ai_client_governance.py selftest" in host_plan_output
        and "python scripts/ai_client_governance.py selftest" not in host_plan_output
    )
    return TestResult(
        name="task-run-command-compression-plan",
        passed=passed,
        summary=(
            "task-run plan emits command-compression.analysis and dedupes repeated commands"
            if passed
            else "task-run command compression plan regression failed"
        ),
        commands=commands,
    )


def test_task_run_dag_cache_diagnostics(root: Path, run_dir: Path) -> TestResult:
    jsonl_artifact_dir = run_dir / "task-run-telemetry-jsonl"
    cache_dir = run_dir / "task-run-cache"
    validation_command = (
        f"{sys.executable} {ai_client_governance_entrypoint()} "
        "validate-encoding --root . --paths README.md --strict"
    )
    run_base = [
        sys.executable,
        str(ai_client_governance_entrypoint()),
        "task-run",
        "--root",
        str(root),
        "run",
        "--task-id",
        "TASK-RUN-DAG-SELFTEST",
        "--task-type",
        "rules-script",
        "--event",
        "write-intent",
        "--cache",
        "--cache-dir",
        str(cache_dir),
        "--input-path",
        "README.md",
        "--jsonl-artifact-dir",
        str(jsonl_artifact_dir),
        "--format",
        "json",
        "--command",
        validation_command,
    ]
    commands = [
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "runtime",
                "components",
                "--event",
                "write-intent",
                "--task-type",
                "rules-script",
                "--task-size",
                "medium",
                "--kind",
                "processing-interceptor",
                "--format",
                "json",
            ],
            cwd=root,
            env_root=root,
        ),
        run_command(run_base, cwd=root, env_root=root),
        run_command(run_base, cwd=root, env_root=root),
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "task-run",
                "--root",
                str(root),
                "diagnose",
                "--jsonl-artifact-dir",
                str(jsonl_artifact_dir),
                "--format",
                "json",
            ],
            cwd=root,
            env_root=root,
            unset_env=["AICG_SHELL_ADAPTER"],
        ),
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "task-run",
                "--root",
                str(root),
                "diagnose",
                "--jsonl-artifact-dir",
                str(jsonl_artifact_dir),
                "--task-id",
                "TASK-RUN-DAG-SELFTEST",
                "--format",
                "json",
            ],
            cwd=root,
            env_root=root,
            unset_env=["AICG_SHELL_ADAPTER"],
        ),
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "task-run",
                "--root",
                str(root),
                "diagnose",
                "--jsonl-artifact-dir",
                str(jsonl_artifact_dir),
                "--require-task-run-tool-telemetry",
                "--format",
                "json",
            ],
            cwd=root,
            env_root=root,
            unset_env=["AICG_SHELL_ADAPTER"],
        ),
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "task-run",
                "--root",
                str(root),
                "diagnose",
                "--jsonl-artifact-dir",
                str(jsonl_artifact_dir),
                "--require-raw-shell-intercept",
                "--format",
                "json",
            ],
            cwd=root,
            env_root=root,
            unset_env=["AICG_SHELL_ADAPTER"],
        ),
    ]
    component_output = commands[0].stdout + commands[0].stderr
    first_output = commands[1].stdout + commands[1].stderr
    second_output = commands[2].stdout + commands[2].stderr
    first: dict[str, object] = {}
    second: dict[str, object] = {}
    diagnose: dict[str, object] = {}
    filtered_diagnose: dict[str, object] = {}
    try:
        first = json.loads(commands[1].stdout)
        second = json.loads(commands[2].stdout)
        diagnose = json.loads(commands[3].stdout)
        filtered_diagnose = json.loads(commands[4].stdout)
    except json.JSONDecodeError:
        pass
    first_summary = first.get("summary", {}) if isinstance(first.get("summary"), dict) else {}
    second_summary = second.get("summary", {}) if isinstance(second.get("summary"), dict) else {}
    telemetry = diagnose.get("telemetry", {}) if isinstance(diagnose.get("telemetry"), dict) else {}
    records = diagnose.get("records", {}) if isinstance(diagnose.get("records"), dict) else {}
    alignment = records.get("alignment", {}) if isinstance(records.get("alignment"), dict) else {}
    filtered_telemetry = (
        filtered_diagnose.get("telemetry", {}) if isinstance(filtered_diagnose.get("telemetry"), dict) else {}
    )
    shell_adapter_telemetry = (
        telemetry.get("shell_adapter_telemetry", {}) if isinstance(telemetry.get("shell_adapter_telemetry"), dict) else {}
    )
    task_run_tool_telemetry = (
        telemetry.get("task_run_tool_telemetry", {})
        if isinstance(telemetry.get("task_run_tool_telemetry"), dict)
        else {}
    )
    raw_shell_auto_intercept = (
        telemetry.get("shell_adapter_auto_intercept", {})
        if isinstance(telemetry.get("shell_adapter_auto_intercept"), dict)
        else {}
    )
    latest_event = (
        task_run_tool_telemetry.get("latest_event", {})
        if isinstance(task_run_tool_telemetry.get("latest_event"), dict)
        else {}
    )
    filtered_filters = (
        filtered_telemetry.get("filters", {}) if isinstance(filtered_telemetry.get("filters"), dict) else {}
    )
    raw_shell_require_output = commands[6].stdout + commands[6].stderr
    passed = (
        all(command.exit_code == 0 for command in commands[:6])
        and commands[6].exit_code != 0
        and "preflight.interceptor.task-run-dag" in component_output
        and int(first_summary.get("cache_misses", 0)) == 1
        and int(second_summary.get("cache_hits", 0)) == 1
        and "\"status\": \"cache-hit\"" in second_output
        and "\"telemetry_path\"" in first_output
        and int(telemetry.get("event_count", 0)) >= 4
        and int(shell_adapter_telemetry.get("event_count", 0)) == 0
        and int(task_run_tool_telemetry.get("event_count", 0)) >= 2
        and not raw_shell_auto_intercept.get("installed")
        and telemetry.get("raw_shell_gap")
        and "raw-shell-auto-intercept" in raw_shell_require_output
        and "mixed" in telemetry.get("scope_kind_counts", {})
        and "ai-client-governance-common" in str(latest_event.get("scope_reason", ""))
        and not telemetry.get("duplicate_commands")
        and not telemetry.get("failures")
        and records.get("db")
        and "task_record_minus_queue_total" in alignment
        and filtered_filters.get("task_id") == "TASK-RUN-DAG-SELFTEST"
        and int(filtered_telemetry.get("event_count", 0)) >= 4
    )
    return TestResult(
        name="task-run-dag-cache-diagnostics",
        passed=passed,
        summary=(
            "task-run run writes task/tool telemetry, reuses safe cache, and diagnose keeps raw shell gap fail-closed"
            if passed
            else "task-run DAG/cache/diagnostics regression failed"
        ),
        commands=commands,
    )


def test_telemetry_trace_context_effectiveness(root: Path, run_dir: Path) -> TestResult:
    db = run_dir / "telemetry-trace-effectiveness.db"
    baseline_trace = "4bf92f3577b34da6a3ce929d0e0e4736"
    candidate_trace = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    commands = [
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "telemetry",
                "--db",
                str(db),
                "record",
                "--span-id",
                "00f067aa0ba902b7",
                "--trace-id",
                baseline_trace,
                "--name",
                "gate-pool",
                "--span-kind",
                "gate",
                "--phase",
                "validation",
                "--event-type",
                "gate-pool",
                "--status",
                "succeeded",
                "--duration-ms",
                "120",
                "--attribute-kv",
                f"traceparent=00-{baseline_trace}-00f067aa0ba902b7-01",
            ],
            cwd=root,
            env_root=root,
        ),
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "telemetry",
                "record",
                "--db",
                str(db),
                "--span-id",
                "b9c7c989f97918e1",
                "--trace-id",
                candidate_trace,
                "--name",
                "gate-pool",
                "--span-kind",
                "gate",
                "--phase",
                "validation",
                "--event-type",
                "gate-pool",
                "--status",
                "succeeded",
                "--duration-ms",
                "80",
                "--cached",
            ],
            cwd=root,
            env_root=root,
        ),
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "telemetry",
                "report",
                "--db",
                str(db),
                "--format",
                "json",
            ],
            cwd=root,
            env_root=root,
        ),
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "telemetry",
                "effectiveness",
                "--db",
                str(db),
                "--baseline-trace-id",
                baseline_trace,
                "--candidate-trace-id",
                candidate_trace,
                "--format",
                "json",
            ],
            cwd=root,
            env_root=root,
        ),
    ]
    report: dict[str, object] = {}
    effectiveness: dict[str, object] = {}
    try:
        report = json.loads(commands[2].stdout)
        effectiveness = json.loads(commands[3].stdout)
    except json.JSONDecodeError:
        pass
    trace_context = report.get("trace_context", {}) if isinstance(report.get("trace_context"), dict) else {}
    diff = effectiveness.get("diff", {}) if isinstance(effectiveness.get("diff"), dict) else {}
    duration_diff = diff.get("duration_sum_ms", {}) if isinstance(diff.get("duration_sum_ms"), dict) else {}
    passed = (
        all(command.exit_code == 0 for command in commands)
        and trace_context.get("trace_count") == 2
        and trace_context.get("valid_traceparent_attribute_count") == 1
        and duration_diff.get("delta") == -40
        and effectiveness.get("candidate", {}).get("metrics", {}).get("cache", {}).get("hits") == 1
    )
    return TestResult(
        name="telemetry-trace-context-effectiveness",
        passed=passed,
        summary=(
            "telemetry report exposes trace context and effectiveness compares before/after metrics"
            if passed
            else "telemetry trace context/effectiveness regression failed"
        ),
        commands=commands,
    )


def test_shell_adapter_scope_diagnostics(root: Path, run_dir: Path) -> TestResult:
    jsonl_artifact_dir = run_dir / "shell-adapter-telemetry-jsonl"
    profile_path = run_dir / "Microsoft.PowerShell_profile.ps1"
    commands = [
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "shell-adapter",
                "--root",
                str(root),
                "--jsonl-artifact-dir",
                str(jsonl_artifact_dir),
                "run",
                "--task-id",
                "SHELL-ADAPTER-SELFTEST",
                "--task-type",
                "rules-script",
                "--scope-path",
                ".ai-client/ai-client-governance/src/ai_client_governance/runtime/shell_adapter.py",
                "--format",
                "json",
                "--",
                sys.executable,
                "-c",
                "import sys; sys.exit(0)",
            ],
            cwd=root,
            env_root=root,
        ),
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "shell-adapter",
                "--root",
                str(root),
                "--jsonl-artifact-dir",
                str(jsonl_artifact_dir),
                "diagnose",
                "--task-id",
                "SHELL-ADAPTER-SELFTEST",
                "--format",
                "json",
            ],
            cwd=root,
            env_root=root,
            unset_env=["AICG_SHELL_ADAPTER"],
        ),
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "shell-adapter",
                "--root",
                str(root),
                "--jsonl-artifact-dir",
                str(jsonl_artifact_dir),
                "diagnose",
                "--task-id",
                "SHELL-ADAPTER-SELFTEST",
                "--require-telemetry",
                "--format",
                "json",
            ],
            cwd=root,
            env_root=root,
            unset_env=["AICG_SHELL_ADAPTER"],
        ),
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "shell-adapter",
                "--root",
                str(root),
                "--jsonl-artifact-dir",
                str(jsonl_artifact_dir),
                "diagnose",
                "--task-id",
                "SHELL-ADAPTER-SELFTEST",
                "--require-fail-closed",
                "--format",
                "json",
            ],
            cwd=root,
            env_root=root,
            unset_env=["AICG_SHELL_ADAPTER"],
        ),
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "shell-adapter",
                "--root",
                str(root),
                "install-powershell",
                "--profile-path",
                str(profile_path),
                "--script-path",
                str(ai_client_governance_entrypoint()),
            ],
            cwd=root,
            env_root=root,
        ),
    ]
    run_output = commands[0].stdout + commands[0].stderr
    diagnose: dict[str, object] = {}
    try:
        diagnose = json.loads(commands[1].stdout)
    except json.JSONDecodeError:
        pass
    scope_counts = diagnose.get("scope_kind_counts", {}) if isinstance(diagnose.get("scope_kind_counts"), dict) else {}
    auto_intercept = diagnose.get("auto_intercept", {}) if isinstance(diagnose.get("auto_intercept"), dict) else {}
    telemetry = diagnose.get("telemetry", {}) if isinstance(diagnose.get("telemetry"), dict) else {}
    fail_closed_output = commands[3].stdout + commands[3].stderr
    passed = (
        all(command.exit_code == 0 for command in [commands[0], commands[1], commands[2], commands[4]])
        and commands[3].exit_code != 0
        and "\"status\": \"succeeded\"" in run_output
        and int(diagnose.get("event_count", 0)) >= 1
        and int(telemetry.get("event_count", 0)) >= 1
        and not auto_intercept.get("installed")
        and not diagnose.get("fail_closed_ready")
        and "shell-adapter-auto-intercept" in fail_closed_output
        and "ai-client-governance-common" in scope_counts
        and "powershell-profile" in commands[4].stdout
        and "execute_required" in commands[4].stdout
    )
    return TestResult(
        name="shell-adapter-scope-diagnostics",
        passed=passed,
        summary=(
            "shell-adapter run writes scoped telemetry while fail-closed diagnose still requires auto-intercept"
            if passed
            else "shell-adapter scoped telemetry diagnostics regression failed"
        ),
        commands=commands,
    )


def test_file_ownership_audit(root: Path, run_dir: Path) -> TestResult:
    from ai_client_governance.worktree.task import closeout_owned_host_paths

    project = run_dir / "file-ownership-project"
    (project / ".ai-client" / "project" / "rules" / "project").mkdir(parents=True, exist_ok=True)
    (project / ".ai-client" / "project" / "state").mkdir(parents=True, exist_ok=True)
    write_text_lf(project / "README.md", "# file ownership selftest\n")
    write_text_lf(project / ".ai-client" / "ai-client-governance-config.json", "{}\n")
    write_text_lf(project / ".ai-client" / "project" / "rules" / "project" / "AGENTS.md", "# project rules\n")
    write_text_lf(project / ".ai-client" / "project" / "state" / "aicg.db", "not a tracked artifact\n")

    commands = [
        run_command(["git", "init", "-b", "main"], cwd=project, env_root=root),
        run_command(["git", "config", "user.email", "selftest@example.invalid"], cwd=project, env_root=root),
        run_command(["git", "config", "user.name", "ai-client-governance selftest"], cwd=project, env_root=root),
        run_command(["git", "add", "README.md", ".ai-client"], cwd=project, env_root=root),
        run_command(["git", "commit", "-m", "init file ownership selftest"], cwd=project, env_root=root),
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "file-ownership",
                "audit",
                "--root",
                str(project),
                "--strict",
            ],
            cwd=project,
            env_root=root,
        ),
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "file-ownership",
                "ensure-gitignore",
                "--root",
                str(project),
                "--execute",
            ],
            cwd=project,
            env_root=root,
        ),
        run_command(
            ["git", "rm", "--cached", ".ai-client/project/state/aicg.db"],
            cwd=project,
            env_root=root,
        ),
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "file-ownership",
                "audit",
                "--root",
                str(project),
                "--strict",
            ],
            cwd=project,
            env_root=root,
        ),
    ]
    bad_failed = commands[5].exit_code != 0 and "aicg.db" in (commands[5].stdout + commands[5].stderr)
    ensured = commands[6].exit_code == 0 and "Action:" in commands[6].stdout
    untracked = commands[7].exit_code == 0
    good_passed = commands[8].exit_code == 0 and "Gitignore managed block: ok" in commands[8].stdout
    closeout_paths = closeout_owned_host_paths(project)
    closeout_excludes_live_state = ".ai-client/project/state/aicg.db" not in closeout_paths
    passed = (
        all(command.exit_code == 0 for command in commands[:5])
        and bad_failed
        and ensured
        and untracked
        and good_passed
        and closeout_excludes_live_state
    )
    return TestResult(
        name="file-ownership-audit",
        passed=passed,
        summary=(
            "file-ownership audit rejects tracked live state, closeout excludes live DB, and passes after managed gitignore plus git rm --cached"
            if passed
            else "file-ownership audit or closeout live-state exclusion did not enforce tracked live-state and gitignore boundaries"
        ),
        commands=commands,
    )


def test_worktree_closeout_all_plan(root: Path, run_dir: Path) -> TestResult:
    project = run_dir / "closeout-all-project"
    (project / ".ai-client" / "project").mkdir(parents=True, exist_ok=True)
    (project / ".ai-client" / "project" / ".gitkeep").write_text("", encoding="utf-8")
    commands = [
        run_command(["git", "init", "-b", "main"], cwd=project, env_root=root),
        run_command(["git", "config", "user.email", "selftest@example.invalid"], cwd=project, env_root=root),
        run_command(["git", "config", "user.name", "ai-client-governance selftest"], cwd=project, env_root=root),
        run_command(["git", "add", ".ai-client/project/.gitkeep"], cwd=project, env_root=root),
        run_command(["git", "commit", "-m", "init selftest project"], cwd=project, env_root=root),
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "worktree-task",
                "closeout-all",
                "--help",
            ],
            cwd=root,
            env_root=root,
        ),
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "worktree-task",
                "closeout-all",
                "--project-root",
                str(project),
                "--repo",
                "self",
                "--plan",
                "--format",
                "json",
            ],
            cwd=project,
            env_root=root,
        ),
    ]
    payload: dict[str, object] = {}
    try:
        payload = json.loads(commands[-1].stdout)
    except json.JSONDecodeError:
        payload = {}
    passed = (
        all(command.exit_code == 0 for command in commands)
        and "--push" not in commands[5].stdout
        and payload.get("command") == "worktree-task closeout-all"
        and payload.get("mode") == "plan"
        and payload.get("selected_repos") == ["self"]
        and payload.get("blockers") == []
        and payload.get("actions") == []
    )
    return TestResult(
        name="worktree-closeout-all-plan",
        passed=passed,
        summary=(
            "closeout-all exposes help and produces a clean no-task dry-run plan"
            if passed
            else "closeout-all dry-run planning regression failed"
        ),
        commands=commands,
    )


def test_completion_test_profiles(root: Path, run_dir: Path) -> TestResult:
    commands = [
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "completion-test",
                "--task-type",
                "rules-script",
                "--changed-path",
                "src/ai_client_governance/records/task_queue.py",
                "--format",
                "json",
            ],
            cwd=root,
            env_root=root,
        ),
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "completion-test",
                "--profile",
                "full",
                "--task-type",
                "rules-script",
                "--changed-path",
                "src/ai_client_governance/records/task_queue.py",
                "--format",
                "json",
            ],
            cwd=root,
            env_root=root,
        ),
    ]
    try:
        fast = json.loads(commands[0].stdout)
        full = json.loads(commands[1].stdout)
    except json.JSONDecodeError:
        fast = {}
        full = {}

    def check_required(payload: dict[str, object], check_id: str) -> bool | None:
        for check in payload.get("planned_checks", []):
            if isinstance(check, dict) and check.get("id") == check_id:
                return bool(check.get("required"))
        return None

    passed = (
        all(command.exit_code == 0 for command in commands)
        and fast.get("profile") == "fast"
        and full.get("profile") == "full"
        and check_required(fast, "focused-regression") is True
        and check_required(fast, "selftest") is False
        and check_required(full, "selftest") is True
    )
    return TestResult(
        name="completion-test-validation-profiles",
        passed=passed,
        summary=(
            "completion-test fast profile requires focused checks while full profile requires selftest"
            if passed
            else "completion-test validation profiles did not plan expected checks"
        ),
        commands=commands,
    )


def test_completion_test_analysis_budget(root: Path, run_dir: Path) -> TestResult:
    base_args = [
        sys.executable,
        str(ai_client_governance_entrypoint()),
        "completion-test",
        "--task-type",
        "rules-script",
        "--changed-path",
        "src/ai_client_governance/validation/completion.py",
        "--format",
        "json",
    ]
    missing = run_command([*base_args, "--require-analysis"], cwd=root, env_root=root)
    complete = run_command(
        [
            *base_args,
            "--require-analysis",
            "--analysis-summary",
            "Add upfront analysis contract and validation budget.",
            "--analysis-scope",
            "src/ai_client_governance/validation/completion.py",
            "--non-goal",
            "Do not run full selftest for a narrow parser change.",
            "--risk",
            "Budget must fail closed when expensive checks are required.",
            "--acceptance",
            "JSON reports ready analysis and unblocked budget.",
            "--budget-seconds",
            "90",
        ],
        cwd=root,
        env_root=root,
    )
    over_budget = run_command(
        [
            *base_args,
            "--profile",
            "full",
            "--require-analysis",
            "--analysis-summary",
            "Add upfront analysis contract and validation budget.",
            "--analysis-scope",
            "src/ai_client_governance/validation/completion.py",
            "--non-goal",
            "Do not hide unclear analysis behind expensive validation.",
            "--risk",
            "Full selftest can exceed a narrow task budget.",
            "--acceptance",
            "Budget overflow exits non-zero.",
            "--budget-seconds",
            "60",
        ],
        cwd=root,
        env_root=root,
    )
    try:
        missing_payload = json.loads(missing.stdout)
        complete_payload = json.loads(complete.stdout)
        over_budget_payload = json.loads(over_budget.stdout)
    except json.JSONDecodeError:
        missing_payload = {}
        complete_payload = {}
        over_budget_payload = {}

    passed = (
        missing.exit_code == 1
        and complete.exit_code == 0
        and over_budget.exit_code == 1
        and "analysis-summary" in missing_payload.get("missing_analysis", [])
        and complete_payload.get("analysis_contract", {}).get("ready_for_write") is True
        and complete_payload.get("validation_budget", {}).get("blocked_by_budget") is False
        and complete_payload.get("validation_attribution", {}).get("budget_pressure") in {"low", "medium", "high"}
        and complete_payload.get("validation_attribution", {}).get("planned_slowest_required")
        and "validation-budget" in over_budget_payload.get("budget_errors", [])
    )
    return TestResult(
        name="completion-test-analysis-budget",
        passed=passed,
        summary=(
            "completion-test blocks unclear analysis and over-budget validation"
            if passed
            else "completion-test analysis/budget enforcement regressed"
        ),
        commands=[missing, complete, over_budget],
    )


def test_framework_debt_register(root: Path, run_dir: Path) -> TestResult:
    db = run_dir / "framework-debt.db"
    commands = [
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "framework-debt",
                "--db",
                str(db),
                "init",
                "--format",
                "json",
            ],
            cwd=root,
            env_root=root,
        ),
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "framework-debt",
                "--db",
                str(db),
                "add",
                "--item-id",
                "FD-SELFTEST-CLI-GLOBAL-ARGS",
                "--title",
                "CLI global args still need framework-wide parser migration",
                "--category",
                "cli",
                "--severity",
                "P1",
                "--problem",
                "Some subcommand CLIs still reject root options after nested subcommands.",
                "--impact",
                "Agents waste time and tokens retrying equivalent commands.",
                "--desired-change",
                "Adopt a uniform command parser pattern across all governance CLIs.",
                "--framework-change-required",
                "The fix crosses subparser setup in multiple command modules.",
                "--workaround",
                "Place common options before unsupported nested subcommands.",
                "--related-task-id",
                "TASK-SELFTEST",
                "--replace",
                "--format",
                "json",
            ],
            cwd=root,
            env_root=root,
        ),
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "framework-debt",
                "--db",
                str(db),
                "list",
                "--category",
                "cli",
                "--format",
                "json",
            ],
            cwd=root,
            env_root=root,
        ),
    ]
    try:
        payload = json.loads(commands[-1].stdout)
    except json.JSONDecodeError:
        payload = {}
    item = next(
        (
            row for row in payload.get("items", [])
            if isinstance(row, dict) and row.get("item_id") == "FD-SELFTEST-CLI-GLOBAL-ARGS"
        ),
        {},
    )
    passed = all(command.exit_code == 0 for command in commands) and item.get("severity") == "P1"
    return TestResult(
        name="framework-debt-register",
        passed=passed,
        summary=(
            "framework-debt records and lists architecture-level design issues"
            if passed
            else "framework-debt register did not persist the expected row"
        ),
        commands=commands,
    )


def test_framework_debt_report(root: Path, run_dir: Path) -> TestResult:
    db = run_dir / "framework-debt-report.db"
    commands = [
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "framework-debt",
                "--db",
                str(db),
                "add",
                "--item-id",
                "FD-SELFTEST-P0",
                "--title",
                "P0 surfacing smoke",
                "--category",
                "validation",
                "--severity",
                "P0",
                "--problem",
                "Important debt must surface automatically.",
                "--impact",
                "Agents can miss critical architecture issues.",
                "--desired-change",
                "Report important debt during planning and closeout.",
                "--framework-change-required",
                "The fix spans report, registry, and gate-pool.",
                "--next-trigger",
                "selftest",
                "--replace",
                "--format",
                "json",
            ],
            cwd=root,
            env_root=root,
        ),
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "framework-debt",
                "--db",
                str(db),
                "add",
                "--item-id",
                "FD-SELFTEST-P2",
                "--title",
                "P2 low priority smoke",
                "--category",
                "docs",
                "--severity",
                "P2",
                "--problem",
                "Lower priority debt should remain recorded.",
                "--impact",
                "It should not crowd out P0/P1 surfacing.",
                "--desired-change",
                "Keep it below the P1 report cutoff.",
                "--framework-change-required",
                "The fix depends on debt report filtering.",
                "--next-trigger",
                "selftest",
                "--replace",
                "--format",
                "json",
            ],
            cwd=root,
            env_root=root,
        ),
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "framework-debt",
                "--db",
                str(db),
                "report",
                "--min-severity",
                "P1",
                "--task-type",
                "rules-script",
                "--changed-path",
                "src/ai_client_governance/validation/completion.py",
                "--format",
                "json",
            ],
            cwd=root,
            env_root=root,
        ),
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "gate-pool",
                "--dry-run",
                "--task-id",
                "TASK-SELFTEST-DEBT",
                "--task-type",
                "rules-script",
                "--changed-path",
                "src/ai_client_governance/validation/completion.py",
                "--event",
                "final-output",
                "--final",
                "--format",
                "json",
            ],
            cwd=root,
            env_root=root,
        ),
    ]
    try:
        report_payload = json.loads(commands[2].stdout)
        gate_payload = json.loads(commands[3].stdout)
    except json.JSONDecodeError:
        report_payload = {}
        gate_payload = {}
    report_ids = [item.get("item_id") for item in report_payload.get("items", []) if isinstance(item, dict)]
    gate_commands = [" ".join(step.get("command", [])) for step in gate_payload.get("steps", []) if isinstance(step, dict)]
    passed = (
        all(command.exit_code == 0 for command in commands)
        and "FD-SELFTEST-P0" in report_ids
        and "FD-SELFTEST-P2" not in report_ids
        and any("framework-debt" in command and "report" in command for command in gate_commands)
    )
    return TestResult(
        name="framework-debt-report",
        passed=passed,
        summary=(
            "framework-debt report surfaces important debt and gate-pool plans it"
            if passed
            else "framework-debt report/gate-pool surfacing regressed"
        ),
        commands=commands,
    )


def test_lifecycle_analysis_contract_preflight(root: Path, run_dir: Path) -> TestResult:
    db = run_dir / "analysis-contract.db"
    task_id = "TASK-SELFTEST-ANALYSIS-CONTRACT"
    input_filter = run_command(
        [
            sys.executable,
            str(ai_client_governance_entrypoint()),
            "lifecycle",
            "input-filter",
            "--root",
            str(root),
            "--db",
            str(db),
            "--task-id",
            task_id,
            "--title",
            "Analysis contract selftest",
            "--message",
            "Implement a rules-script change with a preflight analysis contract.",
            "--task-type",
            "rules-script",
            "--changed-path",
            "src/ai_client_governance/validation/completion.py",
            "--approved-label",
            "批准：selftest-analysis-contract",
            "--apply-task-record",
            "--replace",
            "--format",
            "json",
        ],
        cwd=root,
        env_root=root,
    )
    missing = run_command(
        [
            sys.executable,
            str(ai_client_governance_entrypoint()),
            "lifecycle",
            "preflight",
            "--root",
            str(root),
            "--db",
            str(db),
            "--task-id",
            task_id,
            "--task-type",
            "rules-script",
            "--changed-path",
            "src/ai_client_governance/validation/completion.py",
            "--format",
            "json",
        ],
        cwd=root,
        env_root=root,
    )
    complete = run_command(
        [
            sys.executable,
            str(ai_client_governance_entrypoint()),
            "lifecycle",
            "preflight",
            "--root",
            str(root),
            "--db",
            str(db),
            "--task-id",
            task_id,
            "--task-type",
            "rules-script",
            "--changed-path",
            "src/ai_client_governance/validation/completion.py",
            "--analysis-summary",
            "Implement lifecycle analysis contract enforcement.",
            "--analysis-scope",
            "src/ai_client_governance/lifecycle/engine.py",
            "--non-goal",
            "Do not run full selftest in this preflight smoke.",
            "--risk",
            "Budget enforcement must fail before writes.",
            "--acceptance",
            "Preflight passes only when analysis fields are complete.",
            "--budget-seconds",
            "90",
            "--format",
            "json",
        ],
        cwd=root,
        env_root=root,
    )
    try:
        missing_payload = json.loads(missing.stdout)
        complete_payload = json.loads(complete.stdout)
    except json.JSONDecodeError:
        missing_payload = {}
        complete_payload = {}
    missing_messages = [item.get("message", "") for item in missing_payload.get("errors", []) if isinstance(item, dict)]
    complete_messages = [item.get("message", "") for item in complete_payload.get("notes", []) if isinstance(item, dict)]
    passed = (
        input_filter.exit_code == 0
        and missing.exit_code == 1
        and complete.exit_code == 0
        and any("analysis contract is incomplete" in message for message in missing_messages)
        and any("analysis contract passed" in message for message in complete_messages)
    )
    return TestResult(
        name="lifecycle-analysis-contract-preflight",
        passed=passed,
        summary=(
            "lifecycle preflight fails closed without analysis and passes with complete analysis"
            if passed
            else "lifecycle analysis contract preflight regressed"
        ),
        commands=[input_filter, missing, complete],
    )


def test_sync_check_records_db_state(root: Path, run_dir: Path) -> TestResult:
    project = run_dir / "sync-check-db-project"
    embedded = project / ".ai-client" / "ai-client-governance"
    legacy_state_path = project / ".ai-client" / "project" / "state" / "ai-client-governance-state.json"
    state_db = project / ".ai-client" / "project" / "state" / "aicg.db"
    embedded.mkdir(parents=True, exist_ok=True)
    write_text_lf(embedded / "AGENTS.md", "# governance selftest\n")
    commands = [
        run_command(["git", "init", "-b", "main"], cwd=embedded, env_root=root),
        run_command(["git", "config", "user.email", "selftest@example.invalid"], cwd=embedded, env_root=root),
        run_command(["git", "config", "user.name", "ai-client-governance selftest"], cwd=embedded, env_root=root),
        run_command(["git", "add", "AGENTS.md"], cwd=embedded, env_root=root),
        run_command(["git", "commit", "-m", "init governance selftest"], cwd=embedded, env_root=root),
        run_command(
            [
                sys.executable,
                str(ai_client_governance_entrypoint()),
                "sync-check",
                "--target-project-path",
                str(project),
                "--db",
                str(state_db),
                "--no-fetch",
                "--format",
                "json",
            ],
            cwd=project,
            env_root=root,
        ),
    ]
    row = None
    if state_db.exists():
        con = state_store.connect(state_db, create=False)
        row = state_store.read_state(con, state_type="sync-check", state_key="ai-client-governance")
    passed = (
        all(command.exit_code == 0 for command in commands)
        and row is not None
        and not legacy_state_path.exists()
    )
    return TestResult(
        name="sync-check-records-db-state",
        passed=passed,
        summary=(
            "sync-check records state in aicg.db without generating legacy JSON"
            if passed
            else "sync-check did not record DB state or generated legacy JSON"
        ),
        commands=commands,
    )


def test_worktree_closeout_all_closes_coord_session(root: Path, run_dir: Path) -> TestResult:
    sandbox = Path(tempfile.mkdtemp(prefix="aicg-closeout-"))
    project = sandbox / "p"
    governance = project / ".ai-client" / "ai-client-governance"
    worktree = project / ".ai-client" / "project" / ".worktree" / "closeout-session"
    session_id = "S-SELFTEST-CLOSEOUT"
    (project / ".ai-client" / "project").mkdir(parents=True, exist_ok=True)
    (governance / "scripts").mkdir(parents=True, exist_ok=True)
    worktree.parent.mkdir(parents=True, exist_ok=True)
    write_text_lf(project / "README.md", "# selftest\n")
    write_text_lf(project / ".gitignore", ".ai-client/project/.worktree/\n.ai-client/project/state/\n")
    write_text_lf(project / ".ai-client" / "project" / ".gitkeep", "")
    write_text_lf(governance / "AGENTS.md", "# governance selftest\n")
    write_text_lf(
        governance / "scripts" / "ai_client_governance.py",
        "import runpy\n"
        f"runpy.run_path(r'{ai_client_governance_entrypoint()}', run_name='__main__')\n",
    )
    commands = [
        run_command(["git", "init", "-b", "main"], cwd=project, env_root=root),
        run_command(["git", "config", "user.email", "selftest@example.invalid"], cwd=project, env_root=root),
        run_command(["git", "config", "user.name", "ai-client-governance selftest"], cwd=project, env_root=root),
        run_command(["git", "init", "-b", "main"], cwd=governance, env_root=root),
        run_command(["git", "config", "user.email", "selftest@example.invalid"], cwd=governance, env_root=root),
        run_command(["git", "config", "user.name", "ai-client-governance selftest"], cwd=governance, env_root=root),
        run_command(["git", "add", "AGENTS.md", "scripts/ai_client_governance.py"], cwd=governance, env_root=root),
        run_command(["git", "commit", "-m", "init governance selftest"], cwd=governance, env_root=root),
        run_command(
            ["git", "add", ".gitignore", "README.md", ".ai-client/project/.gitkeep", ".ai-client/ai-client-governance"],
            cwd=project,
            env_root=root,
        ),
        run_command(["git", "commit", "-m", "init closeout coord host"], cwd=project, env_root=root),
        run_command(["git", "worktree", "add", "-b", "codex/closeout-session", str(worktree)], cwd=governance, env_root=root),
    ]
    if not worktree.exists():
        return TestResult(
            name="worktree-closeout-all-closes-coord-session",
            passed=False,
            summary="test setup failed to create the task worktree",
            commands=commands,
        )
    write_text_lf(worktree / "AGENTS.md", "# governance selftest\n\ncoord closeout\n")
    commands.extend(
        [
            run_command(["git", "add", "AGENTS.md"], cwd=worktree, env_root=root),
            run_command(["git", "commit", "-m", "update from task worktree"], cwd=worktree, env_root=root),
            run_command(
                [
                    sys.executable,
                    str(ai_client_governance_entrypoint()),
                    "task-queue",
                    "enqueue",
                    "--root",
                    str(project),
                    "--task-id",
                    "TASK-SELFTEST-CLOSEOUT",
                    "--title",
                    "closeout queue selftest",
                    "--message",
                    "closeout queue selftest",
                    "--task-tracking",
                    ".ai-client/project/records/task-tracking/closeout-session.md",
                    "--approval-label",
                    "批准：selftest",
                    "--status",
                    "ready",
                ],
                cwd=project,
                env_root=root,
            ),
            run_command(
                [
                    sys.executable,
                    str(ai_client_governance_entrypoint()),
                    "task-queue",
                    "start-next",
                    "--root",
                    str(project),
                    "--task-id",
                    "TASK-SELFTEST-CLOSEOUT",
                ],
                cwd=project,
                env_root=root,
            ),
            run_command(
                [
                    sys.executable,
                    str(ai_client_governance_entrypoint()),
                    "worktree-coord",
                    "session",
                    "register",
                    "--session-id",
                    session_id,
                    "--title",
                    "closeout coord selftest",
                    "--task",
                    "closeout-session",
                    "--scope",
                    "AGENTS.md",
                ],
                cwd=worktree,
                env_root=root,
            ),
            run_command(
                [
                    sys.executable,
                    str(ai_client_governance_entrypoint()),
                    "worktree-coord",
                    "lock",
                    "acquire",
                    "--session-id",
                    session_id,
                    "--scope",
                    "AGENTS.md",
                    "--reason",
                    "selftest closeout lock",
                ],
                cwd=worktree,
                env_root=root,
            ),
            run_command(
                [
                    sys.executable,
                    str(ai_client_governance_entrypoint()),
                    "worktree-task",
                    "closeout-all",
                    "--project-root",
                    str(project),
                    "--repo",
                    "ai-client-governance",
                    "--execute",
                    "--complete-current-task",
                    "--complete-summary",
                    "selftest closeout queue complete",
                    "--format",
                    "json",
                ],
                cwd=project,
                env_root=root,
            ),
            run_command(
                [
                    sys.executable,
                    str(ai_client_governance_entrypoint()),
                    "worktree-task",
                    "reconcile",
                    "--project-root",
                    str(project),
                    "--repo",
                    "ai-client-governance",
                    "--strict",
                    "--format",
                    "json",
                ],
                cwd=project,
                env_root=root,
            ),
            run_command(
                [
                    sys.executable,
                    str(ai_client_governance_entrypoint()),
                    "task-queue",
                    "status",
                    "--root",
                    str(project),
                    "--format",
                    "json",
                ],
                cwd=project,
                env_root=root,
            ),
        ]
    )
    closeout_payload: dict[str, object] = {}
    reconcile_payload: dict[str, object] = {}
    queue_payload: dict[str, object] = {}
    try:
        closeout_payload = json.loads(commands[-3].stdout)
        reconcile_payload = json.loads(commands[-2].stdout)
        queue_payload = json.loads(commands[-1].stdout)
    except json.JSONDecodeError:
        pass
    coord_store = CoordStateStore(governance / ".git")
    coord_state = coord_store.read()
    legacy_state_path = governance / ".git" / "ai-client-runtime" / "worktree-coord" / "state.json"
    session = coord_state.get("sessions", {}).get(session_id, {})
    locks = coord_state.get("locks", [])
    execution = closeout_payload.get("execution", []) if isinstance(closeout_payload, dict) else []
    close_steps = [
        item for item in execution
        if isinstance(item, dict) and item.get("action") == "close-coord-session"
    ]
    queue_complete_steps = [
        item for item in execution
        if isinstance(item, dict) and item.get("action") == "complete-task-queue"
    ]
    queue_task = next(
        (
            item for item in queue_payload.get("all_tasks", [])
            if isinstance(item, dict) and item.get("id") == "TASK-SELFTEST-CLOSEOUT"
        ),
        {},
    )
    passed = (
        all(command.exit_code == 0 for command in commands)
        and bool(close_steps)
        and close_steps[-1].get("status") == "done"
        and bool(queue_complete_steps)
        and queue_complete_steps[-1].get("status") == "done"
        and isinstance(queue_task, dict)
        and queue_task.get("status") == "completed"
        and isinstance(session, dict)
        and session.get("status") == "closed_by_closeout"
        and coord_store.state_db.exists()
        and not legacy_state_path.exists()
        and all(
            not (isinstance(lock, dict) and lock.get("session_id") == session_id and lock.get("status") == "active")
            for lock in locks
        )
        and reconcile_payload.get("errors") == []
    )
    if passed and sandbox.exists():
        remove_tree(sandbox)
    return TestResult(
        name="worktree-closeout-all-closes-coord-session",
        passed=passed,
        summary=(
            "closeout-all closes coord sessions, releases locks, and completes the active queue task"
            if passed
            else "closeout-all left stale coord session/lock state or did not complete the queue task"
        ),
        commands=commands,
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
    if args.cleanup_stale:
        cleanup_stale_selftest_artifacts(root)
    before_ai_client = snapshot_ai_client_paths(root)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = root / TMP_DIR / "ai-client-governance-selftest" / timestamp
    run_dir.mkdir(parents=True, exist_ok=True)

    previous_artifact_root = os.environ.get(SELFTEST_ARTIFACT_ENV)
    previous_state_db = os.environ.get(STATE_DB_ENV)
    previous_pycache_prefix_env = os.environ.get(PYCACHE_PREFIX_ENV)
    previous_python_pycache = os.environ.get("PYTHONPYCACHEPREFIX")
    previous_sys_pycache_prefix = sys.pycache_prefix
    os.environ[SELFTEST_ARTIFACT_ENV] = str(run_dir)
    pycache_root = run_dir / PYTHON_PYCACHE_DIR
    os.environ[STATE_DB_ENV] = str(run_dir / "state" / "aicg-selftest.db")
    os.environ[PYCACHE_PREFIX_ENV] = str(pycache_root)
    os.environ["PYTHONPYCACHEPREFIX"] = str(pycache_root)
    sys.pycache_prefix = str(pycache_root)
    try:
        results = [
            test_worktree_gate(root, run_dir),
            test_worktree_creation_policy_gate(root, run_dir),
            test_input_and_output_closeout_gate(root, run_dir),
            test_multi_agent_acceptance_matrix_gate(root, run_dir),
            test_task_queue_task_id_priority(root, run_dir),
            test_task_lifecycle_unified_status(root, run_dir),
            test_gate_pool_validate_doc_tracking_context(root, run_dir),
            test_structured_task_record_gate(root, run_dir),
            test_preflight_boundary_hardening(root, run_dir),
            test_tool_flow_accepts_task_record_gate(root, run_dir),
            test_lifecycle_input_filter_preflight(root, run_dir),
            test_task_run_command_compression_plan(root, run_dir),
            test_task_run_dag_cache_diagnostics(root, run_dir),
            test_telemetry_trace_context_effectiveness(root, run_dir),
            test_shell_adapter_scope_diagnostics(root, run_dir),
            test_file_ownership_audit(root, run_dir),
            test_worktree_closeout_all_plan(root, run_dir),
            test_completion_test_profiles(root, run_dir),
            test_completion_test_analysis_budget(root, run_dir),
            test_framework_debt_register(root, run_dir),
            test_framework_debt_report(root, run_dir),
            test_lifecycle_analysis_contract_preflight(root, run_dir),
            test_sync_check_records_db_state(root, run_dir),
            test_worktree_closeout_all_closes_coord_session(root, run_dir),
        ]
    finally:
        if previous_artifact_root is None:
            os.environ.pop(SELFTEST_ARTIFACT_ENV, None)
        else:
            os.environ[SELFTEST_ARTIFACT_ENV] = previous_artifact_root
        if previous_state_db is None:
            os.environ.pop(STATE_DB_ENV, None)
        else:
            os.environ[STATE_DB_ENV] = previous_state_db
        if previous_pycache_prefix_env is None:
            os.environ.pop(PYCACHE_PREFIX_ENV, None)
        else:
            os.environ[PYCACHE_PREFIX_ENV] = previous_pycache_prefix_env
        if previous_python_pycache is None:
            os.environ.pop("PYTHONPYCACHEPREFIX", None)
        else:
            os.environ["PYTHONPYCACHEPREFIX"] = previous_python_pycache
        sys.pycache_prefix = previous_sys_pycache_prefix

    unexpected_artifacts = unexpected_ai_client_artifacts(root, run_dir, before_ai_client)
    results.append(
        TestResult(
            name="selftest-artifact-manifest",
            passed=not unexpected_artifacts,
            summary=(
                "selftest artifacts stayed within the declared run directory"
                if not unexpected_artifacts
                else "unexpected selftest artifacts: " + ", ".join(unexpected_artifacts[:8])
            ),
            commands=[],
        )
    )
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
            remove_tree(resolved_run)
            cleanup_empty_selftest_parents(root, run_dir, before_ai_client)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
