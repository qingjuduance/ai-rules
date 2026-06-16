#!/usr/bin/env python3
"""Plan and run a small pool of AI rules maintenance gates.

The runner is intentionally conservative: it dispatches through the unified
``ai_rules.py`` CLI, records every child command through the tool-invocation
subcommand, and then asks tool-flow to verify the trace. It does not edit
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

from ai_rules.common.paths import PYTHON_PYCACHE_DIR, ai_rules_entrypoint, is_correction_path


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


@dataclass(frozen=True)
class GateStep:
    name: str
    phase: str
    command: list[str]
    final_gate: bool = False
    reason: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run AI rules gates as one traced pool.")
    parser.add_argument("--root", default=".", help="Repository root. Default: current directory.")
    parser.add_argument("--task-tracking", required=True, help="Task tracking file.")
    parser.add_argument("--task-type", action="append", default=[], help="Task type for task/session gates.")
    parser.add_argument("--changed-path", action="append", default=[], help="Path changed by this task.")
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


def build_steps(root: Path, args: argparse.Namespace) -> list[GateStep]:
    entrypoint = ai_rules_entrypoint()
    py = sys.executable
    changed_paths = normalize_paths(args.changed_path)
    existing_changed_paths = [
        path for path in changed_paths if (root / path).exists()
    ]
    markdown_paths = [path for path in existing_changed_paths if Path(path).suffix.lower() == ".md"]
    text_paths = [path for path in existing_changed_paths if Path(path).suffix.lower() in TEXT_EXTENSIONS]
    python_paths = [path for path in existing_changed_paths if Path(path).suffix.lower() == ".py"]
    task_types = list(args.task_type or [])

    steps: list[GateStep] = []
    if python_paths:
        steps.append(
            GateStep(
                name="py-compile",
                phase="validation",
                command=[py, "-m", "py_compile", *python_paths],
                reason="Python files changed.",
            )
        )
    if text_paths:
        steps.append(
            GateStep(
                name="ai_rules.py validate-encoding",
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
    if markdown_paths:
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
                name="ai_rules.py validate-doc",
                phase="validation",
                command=cli_command(py, entrypoint, "validate-doc", *validate_doc_args),
                final_gate=args.final,
                reason="Markdown files changed.",
            )
        )
    if "correction" in task_types or any(is_correction_path(path) for path in changed_paths):
        steps.append(
            GateStep(
                name="ai_rules.py scan-corrections",
                phase="validation",
                command=cli_command(py, entrypoint, "scan-corrections"),
                final_gate=args.final,
                reason="Correction records are in scope.",
            )
        )
    if changed_paths:
        steps.append(
            GateStep(
                name="git-diff-check",
                phase="validation",
                command=["git", "diff", "--check", "--", *changed_paths],
                reason="Changed paths should not introduce whitespace errors.",
            )
        )
    if args.final:
        steps.append(
            GateStep(
                name="ai_rules.py architecture-guard",
                phase="final-gate",
                command=cli_command(
                    py,
                    entrypoint,
                    "architecture-guard",
                    "--root",
                    str(root),
                    "--strict",
                ),
                final_gate=True,
                reason="Final .codex architecture boundary gate.",
            )
        )
        steps.append(
            GateStep(
                name="ai_rules.py task-gate",
                phase="final-gate",
                command=cli_command(
                    py,
                    entrypoint,
                    "task-gate",
                    "--task-tracking",
                    args.task_tracking,
                    "--require-task-types",
                    *task_type_args(task_types),
                ),
                final_gate=True,
                reason="Final task-type evidence gate.",
            )
        )
        steps.append(
            GateStep(
                name="ai_rules.py session-gate",
                phase="final-gate",
                command=cli_command(
                    py,
                    entrypoint,
                    "session-gate",
                    "--task-tracking",
                    args.task_tracking,
                    "--require-task-tracking",
                    "--require-task-gate",
                    *task_type_args(task_types),
                ),
                final_gate=True,
                reason="Final session closure gate.",
            )
        )
        steps.append(
            GateStep(
                name="ai_rules.py task-queue",
                phase="final-gate",
                command=cli_command(
                    py,
                    entrypoint,
                    "task-queue",
                    "--root",
                    str(root),
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

    trace_id = args.trace_id or ""
    if trace_id:
        steps.append(
            GateStep(
                name="ai_rules.py tool-invocations",
                phase="report",
                command=cli_command(
                    py,
                    entrypoint,
                    "tool-invocations",
                    "report",
                    "--trace-id",
                    trace_id,
                    "--top",
                    str(args.top),
                ),
                reason="Summarize traced gate invocations.",
            )
        )
        steps.append(
            GateStep(
                name="ai_rules.py tool-flow",
                phase="report",
                command=cli_command(
                    py,
                    entrypoint,
                    "tool-flow",
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
        "AI Rules Gate Pool Plan",
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
    task_tracking: str,
    task_types: list[str],
    summary: str,
    exit_code: int | None = None,
) -> int:
    command = [
        sys.executable,
        str(entrypoint),
        "tool-invocations",
        "record",
        "--name",
        "ai_rules.py gate-pool",
        "--status",
        status,
        "--task-tracking",
        task_tracking,
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
        "ai_rules.py gate-pool run",
    ]
    for task_type in task_types:
        command.extend(["--task-type", task_type])
    if exit_code is not None:
        command.extend(["--exit-code", str(exit_code)])
    env = os.environ.copy()
    env["PYTHONPYCACHEPREFIX"] = str(root / PYTHON_PYCACHE_DIR)
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
    task_tracking: str,
    task_types: list[str],
    attempt: int,
) -> int:
    wrapper = [
        sys.executable,
        str(entrypoint),
        "tool-invocations",
        "run",
        "--name",
        step.name,
        "--task-tracking",
        task_tracking,
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
        "--summary",
        step.reason or step.name,
    ]
    for task_type in task_types:
        wrapper.extend(["--task-type", task_type])
    if step.final_gate:
        wrapper.append("--final-gate")
    wrapper.append("--")
    wrapper.extend(step.command)
    env = os.environ.copy()
    env["PYTHONPYCACHEPREFIX"] = str(root / PYTHON_PYCACHE_DIR)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return subprocess.run(wrapper, cwd=root, env=env).returncode


def main() -> int:
    args = parse_args()
    root = Path(args.root).resolve()
    user_trace_id = args.trace_id or ""
    trace_id = user_trace_id or f"trace-{uuid.uuid4()}"
    args.trace_id = trace_id
    args.task_queue_trace_id = user_trace_id
    entrypoint = ai_rules_entrypoint()
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

