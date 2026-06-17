#!/usr/bin/env python3
"""Plan deterministic local task execution before model-mediated steps."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


COMMAND_COMPRESSION_EVENT = "command-compression.analysis"

READONLY_PREFIXES = (
    "git status",
    "git ls-files",
    "git diff --name-only",
    "git diff --stat",
    "git rev-parse",
    "git worktree list",
    "rg ",
    "get-content",
    "select-string",
    "test-path",
)

VALIDATION_MARKERS = (
    " task-record gate ",
    " validate-doc",
    " validate-encoding",
    " doc-index",
    " architecture-guard",
    " selftest",
    " compileall",
    " completion-test",
    " git diff --check",
)

STATEFUL_MARKERS = (
    " apply ",
    " git add ",
    " git commit ",
    " git merge ",
    " git push ",
    " git rm ",
    " git mv ",
    " worktree-task create ",
    " worktree-task merge ",
    " worktree-task remove ",
    " worktree-task cleanup-branch ",
    " worktree-coord lock acquire ",
    " worktree-coord lock release ",
    " worktree-coord session close ",
    " task-record apply ",
)


@dataclass(frozen=True)
class PlannedCommand:
    id: str
    command: str
    normalized: str
    cwd: str
    kind: str
    reason: str


@dataclass(frozen=True)
class SkippedDuplicate:
    command: str
    cwd: str
    first_id: str
    duplicate_index: int


@dataclass(frozen=True)
class CommandGroup:
    id: str
    kind: str
    strategy: str
    can_parallel: bool
    commands: list[PlannedCommand]


@dataclass(frozen=True)
class CommandCompressionPlan:
    task_id: str
    event: str
    task_types: list[str]
    changed_paths: list[str]
    generated_at: str
    command_count_before: int
    command_count_after: int
    skipped_duplicate_count: int
    parallel_group_count: int
    stateful_group_count: int
    selected_pattern: str
    ledger_policy: str
    model_http_reduction: str
    groups: list[CommandGroup]
    skipped_duplicates: list[SkippedDuplicate]
    event_record: dict[str, Any]


def utc_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def normalize_command(command: str) -> str:
    return re.sub(r"\s+", " ", command.strip())


def padded_lower(command: str) -> str:
    return f" {normalize_command(command).lower()} "


def classify_command(command: str) -> tuple[str, str]:
    lowered = padded_lower(command)
    stripped = lowered.strip()
    if any(marker in lowered for marker in STATEFUL_MARKERS):
        return "stateful", "changes repository, coordination, or persistent task state"
    if any(marker in lowered for marker in VALIDATION_MARKERS):
        return "validation", "validation or gate command can be batched after writes settle"
    if any(stripped.startswith(prefix) for prefix in READONLY_PREFIXES):
        return "readonly", "read-only inspection can run in one local batch"
    if " tool-invocations run " in lowered:
        return "ledger-wrapped", "already routed through the local command ledger"
    return "sequential", "unknown side effects, keep ordering conservative"


def default_commands(args: argparse.Namespace) -> list[str]:
    commands: list[str] = []
    if args.task_id:
        commands.append(
            "python .ai-client/ai-client-governance/scripts/ai_client_governance.py "
            f"task-record gate --task-id {args.task_id} --event preflight"
        )
    commands.append("git status --short --branch")
    commands.append("git diff --check")

    changed = [path.replace("\\", "/") for path in args.changed_path]
    docs_changed = any(path.endswith(".md") or path in {"AGENTS.md", "README.md"} for path in changed)
    source_changed = any(path.endswith(".py") or path.startswith("src/") or path.startswith("scripts/") for path in changed)
    if "docs" in args.task_type or docs_changed:
        commands.append("python .ai-client/ai-client-governance/scripts/ai_client_governance.py validate-doc --root .")
        commands.append("python .ai-client/ai-client-governance/scripts/ai_client_governance.py doc-index check --root .")
    if "rules-script" in args.task_type or source_changed:
        commands.append("python -m compileall src scripts")
        commands.append("python scripts/ai_client_governance.py selftest --root .")
    return commands


def dedupe_commands(commands: list[str], cwd: str) -> tuple[list[PlannedCommand], list[SkippedDuplicate]]:
    seen: dict[tuple[str, str], PlannedCommand] = {}
    planned: list[PlannedCommand] = []
    skipped: list[SkippedDuplicate] = []
    for index, command in enumerate(commands, start=1):
        normalized = normalize_command(command)
        if not normalized:
            continue
        key = (cwd, normalized)
        if key in seen:
            skipped.append(
                SkippedDuplicate(
                    command=command,
                    cwd=cwd,
                    first_id=seen[key].id,
                    duplicate_index=index,
                )
            )
            continue
        kind, reason = classify_command(normalized)
        item = PlannedCommand(
            id=f"cmd-{len(planned) + 1:02d}",
            command=command,
            normalized=normalized,
            cwd=cwd,
            kind=kind,
            reason=reason,
        )
        seen[key] = item
        planned.append(item)
    return planned, skipped


def group_commands(commands: list[PlannedCommand]) -> list[CommandGroup]:
    groups: list[CommandGroup] = []
    current: list[PlannedCommand] = []
    current_kind = ""

    def flush() -> None:
        nonlocal current, current_kind
        if not current:
            return
        can_parallel = current_kind in {"readonly", "validation"}
        strategy = "parallel-batch" if can_parallel and len(current) > 1 else "single-step"
        if current_kind in {"stateful", "sequential", "ledger-wrapped"}:
            strategy = "ordered-step"
        groups.append(
            CommandGroup(
                id=f"group-{len(groups) + 1:02d}",
                kind=current_kind,
                strategy=strategy,
                can_parallel=can_parallel,
                commands=current,
            )
        )
        current = []
        current_kind = ""

    for command in commands:
        if command.kind in {"stateful", "sequential", "ledger-wrapped"}:
            flush()
            current = [command]
            current_kind = command.kind
            flush()
            continue
        if current and current_kind != command.kind:
            flush()
        current.append(command)
        current_kind = command.kind
    flush()
    return groups


def build_plan(args: argparse.Namespace) -> CommandCompressionPlan:
    root = Path(args.root).resolve()
    cwd = str(Path(args.cwd).resolve()) if args.cwd else str(root)
    commands = args.command or default_commands(args)
    planned, skipped = dedupe_commands(commands, cwd)
    groups = group_commands(planned)
    parallel_groups = [group for group in groups if group.can_parallel and len(group.commands) > 1]
    stateful_groups = [group for group in groups if group.kind in {"stateful", "sequential", "ledger-wrapped"}]
    payload: dict[str, Any] = {
        "task_id": args.task_id,
        "join_point": args.event,
        "task_types": args.task_type,
        "changed_paths": args.changed_path,
        "decision": "Use a local deterministic task-run plan before asking the model to reason across more command steps.",
        "selected_pattern": "local-command-compression",
        "command_count_before": len([command for command in commands if normalize_command(command)]),
        "command_count_after": len(groups),
        "skipped_duplicate_count": len(skipped),
        "parallel_group_count": len(parallel_groups),
        "stateful_group_count": len(stateful_groups),
        "ledger_policy": "Wrap important shell commands with tool-invocations run until host shell calls are automatically intercepted.",
        "model_http_reduction": "One local planner output replaces repeated model turns for deterministic command selection, dedupe, and grouping.",
        "groups": [
            {
                "id": group.id,
                "kind": group.kind,
                "strategy": group.strategy,
                "can_parallel": group.can_parallel,
                "commands": [command.normalized for command in group.commands],
            }
            for group in groups
        ],
        "skipped_duplicates": [asdict(item) for item in skipped],
        "generated_at": utc_now(),
    }
    event_record = {
        "event_id": f"EVT-{safe_id(args.task_id or 'TASK')}-COMMAND-COMPRESSION",
        "event_type": COMMAND_COMPRESSION_EVENT,
        "payload": payload,
    }
    return CommandCompressionPlan(
        task_id=args.task_id,
        event=args.event,
        task_types=args.task_type,
        changed_paths=args.changed_path,
        generated_at=payload["generated_at"],
        command_count_before=payload["command_count_before"],
        command_count_after=len(groups),
        skipped_duplicate_count=len(skipped),
        parallel_group_count=len(parallel_groups),
        stateful_group_count=len(stateful_groups),
        selected_pattern=payload["selected_pattern"],
        ledger_policy=payload["ledger_policy"],
        model_http_reduction=payload["model_http_reduction"],
        groups=groups,
        skipped_duplicates=skipped,
        event_record=event_record,
    )


def safe_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", value.strip()).strip("-").upper()
    return cleaned or "TASK"


def format_text(plan: CommandCompressionPlan) -> str:
    lines = [
        "AI Client Governance Task Run Plan",
        f"Task: {plan.task_id or '<none>'}",
        f"Event: {plan.event}",
        f"Commands before: {plan.command_count_before}",
        f"Command groups after compression: {plan.command_count_after}",
        f"Skipped duplicates: {plan.skipped_duplicate_count}",
        f"Parallel groups: {plan.parallel_group_count}",
        f"Stateful/ordered groups: {plan.stateful_group_count}",
        "",
        f"Selected pattern: {plan.selected_pattern}",
        f"Ledger policy: {plan.ledger_policy}",
        f"Model HTTP reduction: {plan.model_http_reduction}",
        "",
        "Groups:",
    ]
    for group in plan.groups:
        lines.append(f"  - {group.id} {group.kind} {group.strategy}")
        for command in group.commands:
            lines.append(f"    {command.id}: {command.normalized}")
    if plan.skipped_duplicates:
        lines.append("")
        lines.append("Skipped duplicates:")
        for item in plan.skipped_duplicates:
            lines.append(f"  - duplicate #{item.duplicate_index} of {item.first_id}: {item.command}")
    lines.append("")
    lines.append(f"Task-record event: {COMMAND_COMPRESSION_EVENT}")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plan local task execution with command compression.")
    parser.add_argument("--root", default=".", help="Host project root. Default: current directory.")
    sub = parser.add_subparsers(dest="command_name", required=True)

    plan = sub.add_parser("plan", help="Build a deterministic command compression plan.")
    plan.add_argument("--task-id", default="", help="Structured task id.")
    plan.add_argument("--task-type", action="append", default=[], help="Task type; repeatable.")
    plan.add_argument(
        "--event",
        default="write-intent",
        choices=("user-message", "plan-output", "write-intent", "after-change", "final-output", "resume"),
        help="Lifecycle join point for the plan.",
    )
    plan.add_argument("--changed-path", action="append", default=[], help="Changed or expected path; repeatable.")
    plan.add_argument("--command", action="append", default=[], help="Candidate command to dedupe/group; repeatable.")
    plan.add_argument("--cwd", default="", help="Working directory for candidate commands. Defaults to --root.")
    plan.add_argument("--format", choices=("text", "json"), default="text")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command_name == "plan":
        result = build_plan(args)
        if args.format == "json":
            print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        else:
            print(format_text(result))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
