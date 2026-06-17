#!/usr/bin/env python3
"""Plan and run deterministic local task commands before model-mediated steps."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


COMMAND_COMPRESSION_EVENT = "command-compression.analysis"
RUNNER_VERSION = "task-run-dag-v1"
STDIO_LIMIT = 4000
LEDGER_SCHEMA_VERSION = 2
DEFAULT_LEDGER_DIR = Path(".ai-client") / "project" / "logs" / "tool-invocations"

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

UNCACHEABLE_PREFIXES = (
    "git status",
    "git diff",
    "git worktree list",
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


@dataclass(frozen=True)
class ExecutionResult:
    node_id: str
    group_id: str
    command: str
    cwd: str
    kind: str
    status: str
    exit_code: int
    duration_ms: int
    cached: bool
    cache_key: str
    cache_reason: str
    stdout_tail: str
    stderr_tail: str
    started_at: str
    ended_at: str
    ledger_path: str = ""


@dataclass(frozen=True)
class TaskRunSummary:
    task_id: str
    trace_id: str
    status: str
    command_count: int
    executed_count: int
    cache_hits: int
    cache_misses: int
    skipped_duplicate_count: int
    failed_count: int
    duration_ms: int


@dataclass(frozen=True)
class TaskRunReport:
    schema_version: int
    runner_version: str
    plan: CommandCompressionPlan
    summary: TaskRunSummary
    results: list[ExecutionResult]
    diagnostics: dict[str, Any]


def utc_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def now_ms() -> int:
    return int(time.time() * 1000)


def tail_text(value: str) -> str:
    return value[-STDIO_LIMIT:] if len(value) > STDIO_LIMIT else value


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


def git_head(cwd: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def input_fingerprints(root: Path, paths: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in paths:
        if not raw:
            continue
        path = Path(raw)
        candidate = path if path.is_absolute() else root / path
        if not candidate.exists() or not candidate.is_file():
            result[str(raw)] = "missing"
            continue
        digest = hashlib.sha256()
        digest.update(candidate.read_bytes())
        result[str(raw)] = digest.hexdigest()
    return result


def cache_directory(root: Path, value: str | None) -> Path:
    if value:
        path = Path(value)
        return path if path.is_absolute() else root / path
    return host_project_root(root) / ".ai-client" / "project" / "cache" / "task-run"


def cache_key_for(
    *,
    command: PlannedCommand,
    args: argparse.Namespace,
    root: Path,
    input_hashes: dict[str, str],
) -> str:
    payload = {
        "runner_version": RUNNER_VERSION,
        "command": command.normalized,
        "cwd": command.cwd,
        "kind": command.kind,
        "task_types": args.task_type,
        "changed_paths": args.changed_path,
        "inputs": input_hashes,
        "head": git_head(Path(command.cwd)),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def command_cacheable(command: PlannedCommand) -> tuple[bool, str]:
    normalized = command.normalized.lower()
    if command.kind not in {"readonly", "validation"}:
        return False, f"{command.kind} nodes are ordered/no-cache"
    if any(normalized.startswith(prefix) for prefix in UNCACHEABLE_PREFIXES):
        return False, "command depends on live worktree state"
    return True, "declared readonly/validation node"


def read_cache(path: Path) -> ExecutionResult | None:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(raw, dict):
        raw.setdefault("ledger_path", "")
    return ExecutionResult(**raw)


def write_cache(path: Path, result: ExecutionResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(result), ensure_ascii=False, indent=2), encoding="utf-8")


def ledger_directory(root: Path, value: str | None) -> Path:
    if value:
        path = Path(value)
        return path if path.is_absolute() else root / path
    return host_project_root(root) / DEFAULT_LEDGER_DIR


def append_ledger_event(root: Path, ledger_dir: str | None, event: dict[str, Any]) -> Path:
    directory = ledger_directory(root, ledger_dir)
    now = datetime.now().astimezone()
    path = directory / f"{now.year:04d}-{now.month:02d}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    return path


def ledger_event(
    *,
    result: ExecutionResult,
    status: str,
    trace_id: str,
    invocation_id: str,
    task_types: list[str],
    task_tracking: str,
    task_id: str,
    summary: str,
    exit_code: int | None,
    ended_at: str | None = None,
) -> dict[str, Any]:
    timestamp = ended_at or result.started_at
    return {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "invocation_id": invocation_id,
        "timestamp": timestamp,
        "name": "ai_client_governance.py task-run",
        "command": result.command,
        "status": status,
        "exit_code": exit_code,
        "started_at": result.started_at,
        "ended_at": ended_at,
        "duration_ms": result.duration_ms if ended_at else None,
        "task_tracking": task_tracking,
        "task_types": task_types,
        "task_id": task_id,
        "phase": "task-run",
        "final_gate": False,
        "summary": summary,
        "trace_id": trace_id,
        "parent_invocation_id": "",
        "task_node_id": result.node_id,
        "parent_task_node_id": result.group_id,
        "event_type": "task-run",
        "attempt": None,
        "cwd": result.cwd,
        "source": "ai_client_governance.py task-run",
        "cache_key": result.cache_key,
        "cache_reason": result.cache_reason,
        "cached": result.cached,
    }


def record_ledger_status(
    root: Path,
    args: argparse.Namespace,
    result: ExecutionResult,
    *,
    trace_id: str,
    invocation_id: str,
    status: str,
    exit_code: int | None,
    summary: str,
    ended_at: str | None = None,
) -> Path:
    event = ledger_event(
        result=result,
        status=status,
        trace_id=trace_id,
        invocation_id=invocation_id,
        task_types=args.task_type or [],
        task_tracking=getattr(args, "task_tracking", "") or "",
        task_id=args.task_id or "",
        summary=summary,
        exit_code=exit_code,
        ended_at=ended_at,
    )
    return append_ledger_event(root, getattr(args, "ledger_dir", None), event)


def with_final_ledger(
    root: Path,
    args: argparse.Namespace,
    result: ExecutionResult,
    *,
    trace_id: str,
    invocation_id: str,
) -> ExecutionResult:
    if getattr(args, "no_ledger", False):
        return result
    try:
        path = record_ledger_status(
            root,
            args,
            result,
            trace_id=trace_id,
            invocation_id=invocation_id,
            status=result.status,
            exit_code=result.exit_code,
            summary=f"{result.status} {result.kind} node {result.node_id}",
            ended_at=result.ended_at,
        )
    except OSError as exc:
        stderr = tail_text((result.stderr_tail + "\n" if result.stderr_tail else "") + f"ledger write failed: {exc}")
        return replace(
            result,
            status="failed",
            exit_code=result.exit_code if result.exit_code != 0 else 126,
            stderr_tail=stderr,
        )
    return replace(result, ledger_path=str(path))


def run_one_command(
    command: PlannedCommand,
    group_id: str,
    args: argparse.Namespace,
    root: Path,
    input_hashes: dict[str, str],
    trace_id: str,
) -> ExecutionResult:
    started_at = utc_now()
    start = now_ms()
    invocation_id = str(uuid4())
    cache_key = ""
    cache_reason = "cache disabled"
    cache_path: Path | None = None
    cacheable, reason = command_cacheable(command)
    start_result = ExecutionResult(
        node_id=command.id,
        group_id=group_id,
        command=command.normalized,
        cwd=command.cwd,
        kind=command.kind,
        status="started",
        exit_code=0,
        duration_ms=0,
        cached=False,
        cache_key="",
        cache_reason=cache_reason,
        stdout_tail="",
        stderr_tail="",
        started_at=started_at,
        ended_at="",
    )
    if not getattr(args, "no_ledger", False):
        try:
            record_ledger_status(
                root,
                args,
                start_result,
                trace_id=trace_id,
                invocation_id=invocation_id,
                status="started",
                exit_code=None,
                summary=f"started {command.kind} node {command.id}",
            )
        except OSError as exc:
            return replace(
                start_result,
                status="failed",
                exit_code=126,
                stderr_tail=f"ledger write failed before command: {exc}",
                ended_at=utc_now(),
            )
    if args.cache and cacheable:
        cache_key = cache_key_for(command=command, args=args, root=root, input_hashes=input_hashes)
        cache_path = cache_directory(root, args.cache_dir) / f"{cache_key}.json"
        cached = read_cache(cache_path)
        if cached and cached.exit_code == 0:
            result = ExecutionResult(
                node_id=command.id,
                group_id=group_id,
                command=command.normalized,
                cwd=command.cwd,
                kind=command.kind,
                status="cache-hit",
                exit_code=cached.exit_code,
                duration_ms=0,
                cached=True,
                cache_key=cache_key,
                cache_reason=reason,
                stdout_tail=cached.stdout_tail,
                stderr_tail=cached.stderr_tail,
                started_at=started_at,
                ended_at=utc_now(),
            )
            return with_final_ledger(root, args, result, trace_id=trace_id, invocation_id=invocation_id)
        cache_reason = reason
    elif args.cache:
        cache_reason = reason

    try:
        completed = subprocess.run(
            command.normalized,
            cwd=command.cwd,
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=args.timeout_seconds or None,
            env={
                **os.environ,
                "CODEX_TRACE_ID": trace_id,
                "CODEX_PARENT_INVOCATION_ID": invocation_id,
                "CODEX_TASK_NODE_ID": command.id,
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUTF8": "1",
            },
        )
        exit_code = completed.returncode
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
    except subprocess.TimeoutExpired as exc:
        exit_code = 124
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        stderr = (stderr + "\n" if stderr else "") + f"timeout after {args.timeout_seconds}s"
    except OSError as exc:
        exit_code = 127
        stdout = ""
        stderr = str(exc)

    result = ExecutionResult(
        node_id=command.id,
        group_id=group_id,
        command=command.normalized,
        cwd=command.cwd,
        kind=command.kind,
        status="succeeded" if exit_code == 0 else "failed",
        exit_code=exit_code,
        duration_ms=max(0, now_ms() - start),
        cached=False,
        cache_key=cache_key,
        cache_reason=cache_reason,
        stdout_tail=tail_text(stdout),
        stderr_tail=tail_text(stderr),
        started_at=started_at,
        ended_at=utc_now(),
    )
    if args.cache and cache_path and result.exit_code == 0:
        write_cache(cache_path, result)
    return with_final_ledger(root, args, result, trace_id=trace_id, invocation_id=invocation_id)


def execute_plan(plan: CommandCompressionPlan, args: argparse.Namespace) -> TaskRunReport:
    root = Path(args.root).resolve()
    trace_id = args.trace_id or f"trace-task-run-{datetime.now().strftime('%Y%m%d')}-{safe_id(args.task_id or 'run').lower()}"
    start = now_ms()
    input_paths = args.input_path or args.changed_path
    input_hashes = input_fingerprints(root, input_paths)
    results: list[ExecutionResult] = []

    for group in plan.groups:
        if args.parallel and group.can_parallel and len(group.commands) > 1:
            with ThreadPoolExecutor(max_workers=min(len(group.commands), max(1, args.max_workers))) as executor:
                futures = {
                    executor.submit(run_one_command, command, group.id, args, root, input_hashes, trace_id): command
                    for command in group.commands
                }
                group_results = [future.result() for future in as_completed(futures)]
            group_results.sort(key=lambda item: item.node_id)
            results.extend(group_results)
        else:
            for command in group.commands:
                result = run_one_command(command, group.id, args, root, input_hashes, trace_id)
                results.append(result)
                if args.fail_fast and result.exit_code != 0:
                    break
        if args.fail_fast and any(result.exit_code != 0 for result in results if result.group_id == group.id):
            break

    failed = [item for item in results if item.exit_code != 0]
    cache_hits = [item for item in results if item.cached]
    cache_misses = [item for item in results if args.cache and not item.cached and item.cache_key]
    summary = TaskRunSummary(
        task_id=args.task_id,
        trace_id=trace_id,
        status="failed" if failed else "succeeded",
        command_count=sum(len(group.commands) for group in plan.groups),
        executed_count=len(results) - len(cache_hits),
        cache_hits=len(cache_hits),
        cache_misses=len(cache_misses),
        skipped_duplicate_count=plan.skipped_duplicate_count,
        failed_count=len(failed),
        duration_ms=max(0, now_ms() - start),
    )
    diagnostics = build_diagnostics(
        root=root,
        coord_root=Path(args.coord_root).resolve() if args.coord_root else root,
        results=results,
        ledger_dir=args.ledger_dir,
    )
    report = TaskRunReport(
        schema_version=1,
        runner_version=RUNNER_VERSION,
        plan=plan,
        summary=summary,
        results=results,
        diagnostics=diagnostics,
    )
    if args.trace_json:
        trace_path = Path(args.trace_json)
        if not trace_path.is_absolute():
            trace_path = root / trace_path
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        trace_path.write_text(json.dumps(asdict(report), ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def ledger_files(root: Path, ledger_dir: str | None = None) -> list[Path]:
    directory = ledger_directory(root, ledger_dir)
    return sorted(directory.glob("*.jsonl")) if directory.exists() else []


def read_ledger_events(root: Path, ledger_dir: str | None = None) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for path in ledger_files(root, ledger_dir):
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    event = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    events.append(event)
    return events


def coord_summary(coord_root: Path) -> dict[str, Any]:
    script = coord_root / "scripts" / "ai_client_governance.py"
    if not script.exists():
        return {"available": False, "reason": f"missing {script}"}
    try:
        completed = subprocess.run(
            ["python", str(script), "worktree-coord", "--format", "json", "status"],
            cwd=coord_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "reason": str(exc)}
    if completed.returncode != 0:
        return {"available": False, "reason": completed.stderr.strip() or completed.stdout.strip()}
    try:
        state = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {"available": False, "reason": "invalid JSON from worktree-coord"}
    active_locks = [item for item in state.get("locks", []) if item.get("status") == "active"]
    active_sessions = [item for item in state.get("sessions", []) if item.get("status") == "active"]
    missing_worktrees = [
        item
        for item in active_sessions
        if item.get("worktree") and not Path(str(item.get("worktree"))).exists()
    ]
    return {
        "available": True,
        "locks_active": len(active_locks),
        "sessions_active": len(active_sessions),
        "active_lock_scopes": [item.get("scope") for item in active_locks],
        "missing_worktree_sessions": [item.get("session_id") for item in missing_worktrees],
        "summary": state.get("summary", {}),
    }


def build_diagnostics(
    root: Path,
    coord_root: Path,
    results: list[ExecutionResult] | None = None,
    ledger_dir: str | None = None,
) -> dict[str, Any]:
    events = read_ledger_events(root, ledger_dir)
    terminal_events = [event for event in events if event.get("status") != "started"]
    executed_events = [event for event in terminal_events if not event.get("cached")]
    commands = [str(event.get("command", "")).strip() for event in executed_events if event.get("command")]
    duplicates = [
        {"command": command, "count": count}
        for command, count in Counter(commands).most_common()
        if command and count > 1
    ]
    failures = [
        {
            "timestamp": event.get("timestamp", ""),
            "name": event.get("name", ""),
            "command": event.get("command", ""),
            "exit_code": event.get("exit_code"),
        }
        for event in terminal_events
        if event.get("status") == "failed" or (event.get("exit_code") not in (None, 0))
    ]
    run_results = results or []
    return {
        "ledger": {
            "directory": str(ledger_directory(root, ledger_dir)),
            "event_count": len(events),
            "duplicate_commands": duplicates[:10],
            "failures": failures[-10:],
            "raw_shell_auto_intercepted": False,
            "raw_shell_gap": (
                "The host client shell is not automatically intercepted by ai-client-governance; "
                "important commands should run through task-run, gate-pool, or tool-invocations."
            ),
        },
        "coordination": coord_summary(coord_root),
        "run": {
            "result_count": len(run_results),
            "failed_count": len([item for item in run_results if item.exit_code != 0]),
            "cache_hits": len([item for item in run_results if item.cached]),
            "cache_misses": len([item for item in run_results if item.cache_key and not item.cached]),
        },
    }


def format_plan_text(plan: CommandCompressionPlan) -> str:
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


def format_run_text(report: TaskRunReport) -> str:
    lines = [
        "AI Client Governance Task Run Report",
        f"Task: {report.summary.task_id or '<none>'}",
        f"Trace: {report.summary.trace_id}",
        f"Status: {report.summary.status}",
        f"Commands: {report.summary.command_count}",
        f"Executed: {report.summary.executed_count}",
        f"Cache hits: {report.summary.cache_hits}",
        f"Cache misses: {report.summary.cache_misses}",
        f"Failures: {report.summary.failed_count}",
        f"Ledger events: {report.diagnostics.get('ledger', {}).get('event_count', 0)}",
        "",
        "Results:",
    ]
    for item in report.results:
        cache = " cache" if item.cached else ""
        ledger = f" ledger={item.ledger_path}" if item.ledger_path else ""
        lines.append(f"  - {item.node_id} {item.status}{cache} exit={item.exit_code}{ledger} {item.command}")
    return "\n".join(lines)


def format_diagnostics_text(diagnostics: dict[str, Any]) -> str:
    ledger = diagnostics.get("ledger", {})
    coord = diagnostics.get("coordination", {})
    run = diagnostics.get("run", {})
    lines = [
        "AI Client Governance Task Run Diagnostics",
        f"Ledger events: {ledger.get('event_count', 0)}",
        f"Ledger duplicate commands: {len(ledger.get('duplicate_commands', []))}",
        f"Ledger failures: {len(ledger.get('failures', []))}",
        f"Raw shell auto intercepted: {ledger.get('raw_shell_auto_intercepted')}",
        f"Coord available: {coord.get('available')}",
        f"Active locks: {coord.get('locks_active', 0)}",
        f"Active sessions: {coord.get('sessions_active', 0)}",
        f"Missing-worktree sessions: {len(coord.get('missing_worktree_sessions', []))}",
        f"Run cache hits: {run.get('cache_hits', 0)}",
        f"Run cache misses: {run.get('cache_misses', 0)}",
    ]
    return "\n".join(lines)


def add_plan_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--task-id", default="", help="Structured task id.")
    parser.add_argument("--task-type", action="append", default=[], help="Task type; repeatable.")
    parser.add_argument(
        "--event",
        default="write-intent",
        choices=("user-message", "plan-output", "write-intent", "after-change", "final-output", "resume"),
        help="Lifecycle join point for the plan.",
    )
    parser.add_argument("--changed-path", action="append", default=[], help="Changed or expected path; repeatable.")
    parser.add_argument("--command", action="append", default=[], help="Candidate command to dedupe/group; repeatable.")
    parser.add_argument("--cwd", default="", help="Working directory for candidate commands. Defaults to --root.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plan and run local task execution with command compression.")
    parser.add_argument("--root", default=".", help="Host project root. Default: current directory.")
    sub = parser.add_subparsers(dest="command_name", required=True)

    plan = sub.add_parser("plan", help="Build a deterministic command compression plan.")
    add_plan_args(plan)
    plan.add_argument("--format", choices=("text", "json"), default="text")

    run = sub.add_parser("run", help="Execute a compressed local command DAG.")
    add_plan_args(run)
    run.add_argument("--input-path", action="append", default=[], help="Declared input path for cache fingerprinting.")
    run.add_argument("--cache", action="store_true", help="Enable safe opt-in cache for readonly/validation nodes.")
    run.add_argument("--cache-dir", help="Cache directory. Default: .ai-client/project/cache/task-run.")
    run.add_argument("--parallel", action="store_true", help="Run readonly/validation groups in parallel.")
    run.add_argument("--max-workers", type=int, default=4, help="Maximum parallel workers.")
    run.add_argument("--fail-fast", action="store_true", help="Stop after the first failed group.")
    run.add_argument("--timeout-seconds", type=int, default=0, help="Per-command timeout in seconds.")
    run.add_argument("--trace-id", default="", help="Trace id for report output.")
    run.add_argument("--trace-json", help="Write full run report JSON to this path.")
    run.add_argument("--coord-root", help="Repository root whose worktree-coord state should be diagnosed.")
    run.add_argument("--ledger-dir", help="Tool invocation ledger directory. Default: host .ai-client/project/logs/tool-invocations.")
    run.add_argument("--task-tracking", default="", help="Optional historical task tracking path for ledger compatibility.")
    run.add_argument("--no-ledger", action="store_true", help="Do not write command ledger events. Use only for isolated tests.")
    run.add_argument("--format", choices=("text", "json"), default="text")

    diagnose = sub.add_parser("diagnose", help="Report command ledger, cache, and coordination diagnostics.")
    diagnose.add_argument("--coord-root", help="Repository root whose worktree-coord state should be diagnosed.")
    diagnose.add_argument("--ledger-dir", help="Tool invocation ledger directory. Default: host .ai-client/project/logs/tool-invocations.")
    diagnose.add_argument("--format", choices=("text", "json"), default="text")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command_name == "plan":
        result = build_plan(args)
        if args.format == "json":
            print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        else:
            print(format_plan_text(result))
        return 0
    if args.command_name == "run":
        plan = build_plan(args)
        report = execute_plan(plan, args)
        if args.format == "json":
            print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
        else:
            print(format_run_text(report))
        return 1 if report.summary.failed_count else 0
    if args.command_name == "diagnose":
        root = Path(args.root).resolve()
        coord_root = Path(args.coord_root).resolve() if args.coord_root else root
        diagnostics = build_diagnostics(root=root, coord_root=coord_root, ledger_dir=args.ledger_dir)
        if args.format == "json":
            print(json.dumps(diagnostics, ensure_ascii=False, indent=2))
        else:
            print(format_diagnostics_text(diagnostics))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
