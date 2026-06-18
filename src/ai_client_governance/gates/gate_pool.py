#!/usr/bin/env python3
"""Plan and run a small pool of AI Client Governance maintenance gates.

The runner is intentionally conservative: it dispatches through the unified
``ai_client_governance.py`` CLI, records every child command through unified
telemetry, and then asks tool-flow to verify the trace. It does not edit
tracking, rules, corrections, pending files, or Git state.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from ai_client_governance.common.paths import (
    PYTHON_PYCACHE_DIR,
    STRUCTURED_DB_PATH,
    ai_client_governance_entrypoint,
    is_correction_path,
)
from ai_client_governance.runtime import AgentExecutionContext, default_registry


TEXT_EXTENSIONS = {
    ".css",
    ".html",
    ".js",
    ".json",
    ".md",
    ".ps1",
    ".py",
    ".toml",
    ".ts",
    ".yaml",
    ".yml",
}

PLACEHOLDER_TASK_ID = "<task-id>"
PYCACHE_PREFIX_ENV = "AICG_PYTHONPYCACHEPREFIX"


@dataclass(frozen=True)
class GateStep:
    name: str
    phase: str
    command: list[str]
    final_gate: bool = False
    reason: str = ""


def configured_path(root: Path, env_name: str, fallback: Path) -> Path:
    configured = os.environ.get(env_name, "")
    if configured:
        path = Path(configured)
        return path if path.is_absolute() else root / path
    return root / fallback


def pycache_prefix(root: Path) -> Path:
    return configured_path(root, PYCACHE_PREFIX_ENV, PYTHON_PYCACHE_DIR)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run AI Client Governance gates as one traced pool.")
    parser.add_argument("--root", default=".", help="Repository root. Default: current directory.")
    parser.add_argument("--task-tracking", help="Task tracking file.")
    parser.add_argument("--task-id", help="Structured task id to validate from task-record SQLite.")
    parser.add_argument("--db", help="Structured task-record SQLite path.")
    parser.add_argument("--task-type", action="append", default=[], help="Task type for task/session gates.")
    parser.add_argument("--changed-path", action="append", default=[], help="Path changed by this task.")
    parser.add_argument("--completion-profile", choices=("fast", "full"), default="fast", help="completion-test validation profile.")
    parser.add_argument("--budget-seconds", type=int, help="Maximum estimated seconds for required validation checks.")
    parser.add_argument("--allow-expensive", action="store_true", help="Allow completion-test required checks to exceed budget.")
    parser.add_argument("--require-analysis", action="store_true", help="Require completion-test analysis contract.")
    parser.add_argument("--analysis-summary", default="", help="Task understanding passed to completion-test.")
    parser.add_argument("--analysis-scope", action="append", default=[], help="Explicit analysis scope passed to completion-test.")
    parser.add_argument("--non-goal", action="append", default=[], help="Non-goal passed to completion-test.")
    parser.add_argument("--risk", action="append", default=[], help="Risk boundary passed to completion-test.")
    parser.add_argument("--acceptance", action="append", default=[], help="Acceptance criterion passed to completion-test.")
    parser.add_argument(
        "--event",
        choices=(
            "user-message",
            "plan-output",
            "status-output",
            "write-intent",
            "after-change",
            "completion-test",
            "final-output",
            "resume",
            "merge-cleanup",
            "session-start",
            "state-audit",
        ),
        help="Runtime event boundary. Default: final-output when --final is set, otherwise after-change.",
    )
    parser.add_argument("--trace-id", help="Trace id to reuse. Default: generated.")
    parser.add_argument("--top", type=int, default=30, help="Rows to show in reports.")
    parser.add_argument("--final", action="store_true", help="Include final task/session gates.")
    parser.add_argument("--dry-run", action="store_true", help="Print the plan without running it.")
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Plan/report output format.",
    )
    return parser.parse_args()


def rel_or_abs(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def host_project_root(root: Path) -> Path:
    """Return the host project root when running inside .ai-client/project/.worktree."""
    resolved = root.resolve()
    parts = resolved.parts
    for index in range(len(parts) - 2):
        if parts[index : index + 3] == (".ai-client", "project", ".worktree"):
            host = Path(*parts[:index])
            if (host / ".ai-client" / "project").exists():
                return host
    return resolved


def cli_command(py: str, entrypoint: Path, command: str, *args: str) -> list[str]:
    return [py, str(entrypoint), command, *args]


def normalize_paths(paths: list[str]) -> list[str]:
    result: list[str] = []
    for value in paths:
        for part in value.split(","):
            stripped = part.strip()
            if stripped and stripped not in result:
                result.append(stripped)
    return result


def task_type_args(task_types: list[str]) -> list[str]:
    return ["--task-types", *task_types] if task_types else []


def registry_gate_steps(task_types: list[str], changed_paths: list[str], final: bool, event: str) -> set[str]:
    context = AgentExecutionContext(
        input_source="tool",
        task_types=tuple(task_types),
        task_size="medium" if changed_paths or task_types else "small",
        changed_paths=tuple(path.replace("\\", "/") for path in changed_paths),
        final=final,
        event=event,
    )
    return set(default_registry().gate_step_ids_for_context(context))


def structured_db_args(args: argparse.Namespace, root: Path) -> list[str]:
    if args.db:
        return ["--db", args.db]
    return ["--db", str(host_project_root(root) / STRUCTURED_DB_PATH)]


def fact_source_args(
    args: argparse.Namespace,
    root: Path,
    *,
    structured_event: str | None = None,
) -> list[str]:
    if args.task_tracking:
        return ["--task-tracking", args.task_tracking]
    task_id = args.task_id or (PLACEHOLDER_TASK_ID if args.dry_run else "")
    if not task_id:
        raise ValueError("gate-pool requires --task-tracking or --task-id")
    result = ["--task-id", task_id]
    result.extend(structured_db_args(args, root))
    if structured_event:
        result.extend(["--structured-event", structured_event])
    return result


def build_steps(root: Path, args: argparse.Namespace) -> list[GateStep]:
    entrypoint = ai_client_governance_entrypoint()
    py = sys.executable
    changed_paths = normalize_paths(args.changed_path)
    existing_changed_paths = [
        path for path in changed_paths if (root / path).exists()
    ]
    markdown_paths = [path for path in existing_changed_paths if Path(path).suffix.lower() == ".md"]
    text_paths = [path for path in existing_changed_paths if Path(path).suffix.lower() in TEXT_EXTENSIONS]
    python_paths = [path for path in existing_changed_paths if Path(path).suffix.lower() == ".py"]
    task_types = list(args.task_type or [])
    event = args.event or ("final-output" if args.final else "after-change")
    gate_steps = registry_gate_steps(task_types, changed_paths, args.final, event)

    steps: list[GateStep] = []
    if "worktree-creation-policy" in gate_steps:
        if args.task_tracking:
            worktree_policy_args = ["--task-tracking", args.task_tracking, "--only-worktree-creation-policy"]
        else:
            worktree_policy_args = fact_source_args(args, root, structured_event="preflight")
        steps.append(
            GateStep(
                name="ai_client_governance.py task-gate --only-worktree-creation-policy",
                phase="preflight",
                command=cli_command(
                    py,
                    entrypoint,
                    "task-gate",
                    *worktree_policy_args,
                ),
                final_gate=args.final,
                reason=(
                    "Task worktree creation must declare worktree-task create or a break-glass "
                    "raw git path plus sparse/source snapshot handling."
                ),
            )
        )
    if "worktree-live-state" in gate_steps:
        steps.append(
            GateStep(
                name="ai_client_governance.py worktree-task reconcile",
                phase="coordination",
                command=cli_command(
                    py,
                    entrypoint,
                    "worktree-task",
                    "reconcile",
                    "--strict",
                ),
                final_gate=args.final,
                reason="Coord/session state must match Git live worktree state at this runtime boundary.",
            )
        )
    if "host-submodule-closeout" in gate_steps:
        closeout_args = [
            "worktree-task",
            "host-closeout",
            "--project-root",
            str(host_project_root(root)),
            "--repo",
            "ai-client-governance",
        ]
        if args.task_tracking:
            closeout_args.extend(["--task-tracking", args.task_tracking])
            closeout_args.append("--require-task-tracking")
        if args.final:
            closeout_args.append("--require-clean-host")
        steps.append(
            GateStep(
                name="ai_client_governance.py worktree-task host-closeout",
                phase="coordination",
                command=cli_command(py, entrypoint, *closeout_args),
                final_gate=args.final,
                reason="Embedded ai-client-governance merges must update the host gitlink, task state, and task tracking.",
            )
        )
    if python_paths and "py-compile" in gate_steps:
        steps.append(
            GateStep(
                name="py-compile",
                phase="validation",
                command=[py, "-m", "py_compile", *python_paths],
                reason="Python files changed.",
            )
        )
    if text_paths and "validate-encoding" in gate_steps:
        steps.append(
            GateStep(
                name="ai_client_governance.py validate-encoding",
                phase="validation",
                command=cli_command(
                    py,
                    entrypoint,
                    "validate-encoding",
                    "--paths",
                    *text_paths,
                    "--require-paths",
                    "--strict",
                ),
                final_gate=args.final,
                reason="Text files changed.",
            )
        )
    if markdown_paths and "validate-doc" in gate_steps:
        validate_doc_args = [
            "--root",
            str(root),
            "--paths",
            *markdown_paths,
            "--strict",
        ]
        if args.task_tracking:
            validate_doc_args.extend(
                [
                    "--task-tracking",
                    args.task_tracking,
                    "--require-task-tracking",
                ]
            )
        steps.append(
            GateStep(
                name="ai_client_governance.py validate-doc",
                phase="validation",
                command=cli_command(py, entrypoint, "validate-doc", *validate_doc_args),
                final_gate=args.final,
                reason="Markdown files changed.",
            )
        )
    if changed_paths and "doc-index" in gate_steps:
        doc_index_args = [
            "check",
            "--root",
            str(root),
            "--rebuild",
            "--strict",
        ]
        for path in changed_paths:
            doc_index_args.extend(["--changed-path", path])
        steps.append(
            GateStep(
                name="ai_client_governance.py doc-index",
                phase="post-change",
                command=cli_command(py, entrypoint, "doc-index", *doc_index_args),
                final_gate=args.final,
                reason="Changed paths may affect docs, README, references, or backlinks; run once with all changed paths.",
            )
        )
    if "scan-corrections" in gate_steps and (
        "correction" in task_types or any(is_correction_path(path) for path in changed_paths)
    ):
        steps.append(
            GateStep(
                name="ai_client_governance.py scan-corrections",
                phase="validation",
                command=cli_command(py, entrypoint, "scan-corrections"),
                final_gate=args.final,
                reason="Correction records are in scope.",
            )
        )
    if changed_paths and "git-diff-check" in gate_steps:
        steps.append(
            GateStep(
                name="git-diff-check",
                phase="validation",
                command=["git", "diff", "--check", "--", *changed_paths],
                reason="Changed paths should not introduce whitespace errors.",
            )
        )
    if changed_paths and "completion-test-plan" in gate_steps:
        completion_args = [
            "--root",
            str(root),
        ]
        if args.task_tracking:
            completion_args.extend(["--task-tracking", args.task_tracking])
        if args.task_id:
            completion_args.extend(["--task-id", args.task_id])
        if args.db:
            completion_args.extend(["--db", args.db])
        completion_args.extend(["--profile", args.completion_profile])
        if args.budget_seconds is not None:
            completion_args.extend(["--budget-seconds", str(args.budget_seconds)])
        if args.allow_expensive:
            completion_args.append("--allow-expensive")
        if args.require_analysis:
            completion_args.append("--require-analysis")
        if args.analysis_summary:
            completion_args.extend(["--analysis-summary", args.analysis_summary])
        for value in args.analysis_scope:
            completion_args.extend(["--analysis-scope", value])
        for value in args.non_goal:
            completion_args.extend(["--non-goal", value])
        for value in args.risk:
            completion_args.extend(["--risk", value])
        for value in args.acceptance:
            completion_args.extend(["--acceptance", value])
        for task_type in task_types:
            completion_args.extend(["--task-type", task_type])
        for path in changed_paths:
            completion_args.extend(["--changed-path", path])
        steps.append(
            GateStep(
                name="ai_client_governance.py completion-test",
                phase="completion",
                command=cli_command(py, entrypoint, "completion-test", *completion_args),
                final_gate=args.final,
                reason="Plan completion tests from changed paths and task types.",
            )
        )
    if "git-state-audit" in gate_steps:
        steps.append(
            GateStep(
                name="git-state-audit",
                phase="output",
                command=["git", "-C", str(root), "status", "--short", "--branch"],
                final_gate=args.final,
                reason="Report Git state at the output boundary without pushing.",
            )
        )
    if args.final and "file-ownership" in gate_steps:
        ownership_root = host_project_root(root)
        steps.append(
            GateStep(
                name="ai_client_governance.py file-ownership audit",
                phase="final-gate",
                command=cli_command(
                    py,
                    entrypoint,
                    "file-ownership",
                    "audit",
                    "--root",
                    str(ownership_root),
                    "--strict",
                    "--record-state",
                ),
                final_gate=True,
                reason="Final host-project .ai-client file ownership, .gitignore, and tracked live-state gate.",
            )
        )
    if args.final and "architecture-guard" in gate_steps:
        architecture_root = host_project_root(root)
        steps.append(
            GateStep(
                name="ai_client_governance.py architecture-guard",
                phase="final-gate",
                command=cli_command(
                    py,
                    entrypoint,
                    "architecture-guard",
                    "--root",
                    str(architecture_root),
                    "--strict",
                ),
                final_gate=True,
                reason="Final host-project .ai-client architecture boundary gate.",
            )
        )
    if "task-record" in gate_steps and not args.task_tracking:
        task_record_args = structured_db_args(args, root)
        task_record_args.extend(["gate", "--task-id", args.task_id or PLACEHOLDER_TASK_ID])
        steps.append(
            GateStep(
                name="ai_client_governance.py task-record gate",
                phase="final-gate" if args.final else "preflight",
                command=cli_command(py, entrypoint, "task-record", *task_record_args),
                final_gate=args.final,
                reason="Structured SQLite task record is the task evidence source for new tasks.",
            )
        )
    if args.final and "task-gate" in gate_steps and (args.task_tracking or not args.task_id):
        task_gate_args = fact_source_args(args, root)
        steps.append(
            GateStep(
                name="ai_client_governance.py task-gate",
                phase="final-gate",
                command=cli_command(
                    py,
                    entrypoint,
                    "task-gate",
                    *task_gate_args,
                    "--require-task-types",
                    *task_type_args(task_types),
                ),
                final_gate=True,
                reason="Final task-type evidence gate.",
            )
        )
    if args.final and "session-gate" in gate_steps:
        session_root = host_project_root(root)
        session_gate_args = fact_source_args(args, root)
        steps.append(
            GateStep(
                name="ai_client_governance.py session-gate",
                phase="final-gate",
                command=cli_command(
                    py,
                    entrypoint,
                    "session-gate",
                    "--root",
                    str(session_root),
                    *session_gate_args,
                    "--require-task-gate",
                    *task_type_args(task_types),
                ),
                final_gate=True,
                reason="Final session closure gate.",
            )
        )
        if args.task_tracking:
            steps[-1].command.append("--require-task-tracking")
    if args.final and "task-queue" in gate_steps:
        if args.task_tracking:
            queue_root = host_project_root(root)
            steps.append(
                GateStep(
                    name="ai_client_governance.py task-queue",
                    phase="final-gate",
                    command=cli_command(
                        py,
                        entrypoint,
                        "task-queue",
                        "--root",
                        str(queue_root),
                        "validate",
                        "--current-task-tracking",
                        args.task_tracking,
                        "--trace-id",
                        getattr(args, "task_queue_trace_id", args.trace_id or ""),
                        "--require-current",
                        "--strict-fifo",
                    ),
                    final_gate=True,
                    reason="Final task queue state gate.",
                )
            )
        elif (args.task_id or args.dry_run) and "task-record" not in gate_steps:
            task_record_args = structured_db_args(args, root)
            task_record_args.extend(["gate", "--task-id", args.task_id or PLACEHOLDER_TASK_ID])
            steps.append(
                GateStep(
                    name="ai_client_governance.py task-record gate",
                    phase="final-gate",
                    command=cli_command(py, entrypoint, "task-record", *task_record_args),
                    final_gate=True,
                    reason="Structured task record replaces Markdown task-queue current tracking in task-id mode.",
                )
            )

    trace_id = args.trace_id or ""
    if trace_id:
        telemetry_root = host_project_root(root)
        steps.append(
            GateStep(
                name="ai_client_governance.py telemetry",
                phase="report",
                command=cli_command(
                    py,
                    entrypoint,
                    "telemetry",
                    "--root",
                    str(telemetry_root),
                    "report",
                    "--trace-id",
                    trace_id,
                    "--top",
                    str(args.top),
                ),
                reason="Summarize execution telemetry for the trace.",
            )
        )
        steps.append(
            GateStep(
                name="ai_client_governance.py tool-flow",
                phase="report",
                command=cli_command(
                    py,
                    entrypoint,
                    "tool-flow",
                    "--root",
                    str(telemetry_root),
                    "--trace-id",
                    trace_id,
                    "--top",
                    str(args.top),
                    "--format",
                    "text",
                    "--require-final-gate",
                    "--require-report",
                    "--require-trace",
                ),
                reason="Verify traced gate flow.",
            )
        )
    return steps


def render_plan(root: Path, steps: list[GateStep], trace_id: str, fmt: str) -> str:
    payload = {
        "root": root.as_posix(),
        "trace_id": trace_id,
        "step_count": len(steps),
        "steps": [asdict(step) for step in steps],
    }
    if fmt == "json":
        return json.dumps(payload, ensure_ascii=False, indent=2)
    lines = [
        "AI Client Governance Gate Pool Plan",
        f"Root: {root.as_posix()}",
        f"Trace: {trace_id}",
        f"Steps: {len(steps)}",
        "",
    ]
    for index, step in enumerate(steps, 1):
        final = " final" if step.final_gate else ""
        lines.append(f"{index}. {step.name} [{step.phase}{final}]")
        lines.append(f"   reason: {step.reason or 'n/a'}")
        lines.append(f"   command: {' '.join(step.command)}")
    return "\n".join(lines)


def run_record(
    root: Path,
    entrypoint: Path,
    *,
    invocation_id: str,
    trace_id: str,
    status: str,
    task_tracking: str | None,
    task_types: list[str],
    summary: str,
    exit_code: int | None = None,
) -> int:
    telemetry_root = host_project_root(root)
    command = [
        sys.executable,
        str(entrypoint),
        "tool-invocations",
        "--root",
        str(telemetry_root),
        "record",
        "--name",
        "ai_client_governance.py gate-pool",
        "--status",
        status,
        "--phase",
        "gate-pool",
        "--trace-id",
        trace_id,
        "--event-type",
        "gate-pool",
        "--invocation-id",
        invocation_id,
        "--summary",
        summary,
        "--command",
        "ai_client_governance.py gate-pool run",
    ]
    if task_tracking:
        command.extend(["--task-tracking", task_tracking])
    for task_type in task_types:
        command.extend(["--task-type", task_type])
    if exit_code is not None:
        command.extend(["--exit-code", str(exit_code)])
    env = os.environ.copy()
    env["PYTHONPYCACHEPREFIX"] = str(pycache_prefix(telemetry_root))
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return subprocess.run(command, cwd=root, env=env).returncode


def run_step(
    root: Path,
    entrypoint: Path,
    step: GateStep,
    *,
    trace_id: str,
    parent_id: str,
    task_tracking: str | None,
    task_types: list[str],
    attempt: int,
) -> int:
    telemetry_root = host_project_root(root)
    wrapper = [
        sys.executable,
        str(entrypoint),
        "tool-invocations",
        "--root",
        str(telemetry_root),
        "run",
        "--name",
        step.name,
        "--phase",
        step.phase,
        "--trace-id",
        trace_id,
        "--parent-invocation-id",
        parent_id,
        "--event-type",
        "gate",
        "--attempt",
        str(attempt),
        "--cwd",
        str(root),
        "--summary",
        step.reason or step.name,
    ]
    if task_tracking:
        wrapper.extend(["--task-tracking", task_tracking])
    for task_type in task_types:
        wrapper.extend(["--task-type", task_type])
    if step.final_gate:
        wrapper.append("--final-gate")
    wrapper.append("--")
    wrapper.extend(step.command)
    env = os.environ.copy()
    env["PYTHONPYCACHEPREFIX"] = str(pycache_prefix(telemetry_root))
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return subprocess.run(wrapper, cwd=root, env=env).returncode


def main() -> int:
    args = parse_args()
    if not args.task_tracking and not args.task_id and not args.dry_run:
        raise SystemExit("gate-pool requires --task-tracking or --task-id")
    root = Path(args.root).resolve()
    user_trace_id = args.trace_id or ""
    trace_id = user_trace_id or f"trace-{uuid.uuid4()}"
    args.trace_id = trace_id
    args.task_queue_trace_id = user_trace_id
    entrypoint = ai_client_governance_entrypoint()
    steps = build_steps(root, args)
    print(render_plan(root, steps, trace_id, args.format))
    if args.dry_run:
        return 0

    pool_id = f"gate-pool-{uuid.uuid4()}"
    task_types = list(args.task_type or [])
    run_record(
        root,
        entrypoint,
        invocation_id=pool_id,
        trace_id=trace_id,
        status="started",
        task_tracking=args.task_tracking,
        task_types=task_types,
        summary=f"starting {len(steps)} gate-pool steps",
    )
    exit_code = 0
    for index, step in enumerate(steps, 1):
        result = run_step(
            root,
            entrypoint,
            step,
            trace_id=trace_id,
            parent_id=pool_id,
            task_tracking=args.task_tracking,
            task_types=task_types,
            attempt=index,
        )
        if result != 0:
            exit_code = result
            break
    run_record(
        root,
        entrypoint,
        invocation_id=pool_id,
        trace_id=trace_id,
        status="succeeded" if exit_code == 0 else "failed",
        task_tracking=args.task_tracking,
        task_types=task_types,
        summary=f"finished gate-pool steps exit={exit_code}",
        exit_code=exit_code,
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
