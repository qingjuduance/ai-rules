#!/usr/bin/env python3
"""Plan and run deterministic local task commands before model-mediated steps."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from ai_client_governance.records import task_queue, telemetry
from ai_client_governance.runtime.scope import classify_scope


COMMAND_COMPRESSION_EVENT = "command-compression.analysis"
RUNNER_VERSION = "task-run-dag-v1"
STDIO_LIMIT = 4000
TELEMETRY_EVENT_SCHEMA_VERSION = 2
DEFAULT_JSONL_ARTIFACT_DIR = Path(".ai-client") / "project" / "logs" / "tool-invocations"

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


def is_governance_repo_root(root: Path) -> bool:
    return (
        (root / "scripts" / "ai_client_governance.py").exists()
        and (root / "src" / "ai_client_governance").exists()
        and (root / "manifest.json").exists()
    )


def package_governance_root() -> Path:
    return Path(__file__).resolve().parents[3]


def resolve_governance_script(root: Path) -> Path | None:
    candidates = [
        root / ".ai-client" / "ai-client-governance",
        root if is_governance_repo_root(root) else None,
        package_governance_root(),
    ]
    for candidate in candidates:
        if candidate is None:
            continue
        script = candidate / "scripts" / "ai_client_governance.py"
        if script.exists() and is_governance_repo_root(candidate):
            return script
    return None


def quote_arg(value: str | Path) -> str:
    text = str(value)
    if not text:
        return '""'
    if re.search(r'[\s"&|<>^()]', text):
        return '"' + text.replace('"', '\\"') + '"'
    return text


def command_path(path: Path, cwd: Path) -> str:
    try:
        display = path.resolve().relative_to(cwd.resolve())
    except ValueError:
        display = path.resolve()
    return quote_arg(str(display).replace("\\", "/"))


def governance_command(root: Path, *parts: str) -> str:
    script = resolve_governance_script(root)
    if script is None:
        placeholder = "<missing-ai-client-governance-script>"
        return " ".join(["python", placeholder, *parts])
    return " ".join(["python", command_path(script, root), *parts])


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
    telemetry_policy: str
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
    telemetry_path: str = ""


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


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def event_time(event: dict[str, Any]) -> datetime | None:
    for key in ("timestamp", "ended_at", "started_at"):
        parsed = parse_dt(str(event.get(key) or ""))
        if parsed:
            return parsed
    return None


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
    if " shell-adapter run " in lowered:
        return "telemetry-wrapped", "already routed through the shell adapter telemetry"
    if " tool-invocations run " in lowered:
        return "telemetry-wrapped", "already routed through local execution telemetry"
    return "sequential", "unknown side effects, keep ordering conservative"


def default_commands(args: argparse.Namespace) -> list[str]:
    root = Path(args.root).resolve()
    gov = lambda *parts: governance_command(root, *parts)
    commands: list[str] = []
    if args.task_id:
        commands.append(gov("task-record", "gate", "--task-id", quote_arg(args.task_id), "--event", "preflight"))
    commands.append("git status --short --branch")
    commands.append("git diff --check")

    changed = [path.replace("\\", "/") for path in args.changed_path]
    docs_changed = any(path.endswith(".md") or path in {"AGENTS.md", "README.md"} for path in changed)
    source_changed = any(path.endswith(".py") or path.startswith("src/") or path.startswith("scripts/") for path in changed)
    if "docs" in args.task_type or docs_changed:
        commands.append(gov("validate-doc", "--root", "."))
        commands.append(gov("doc-index", "check", "--root", "."))
    if "rules-script" in args.task_type or source_changed:
        commands.append("python -m compileall src scripts")
        commands.append(gov("selftest", "--root", "."))
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
        if current_kind in {"stateful", "sequential", "telemetry-wrapped"}:
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
        if command.kind in {"stateful", "sequential", "telemetry-wrapped"}:
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
    scope = classify_scope(root=root, paths=args.changed_path, command="\n".join(commands), cwd=cwd).to_dict()
    parallel_groups = [group for group in groups if group.can_parallel and len(group.commands) > 1]
    stateful_groups = [group for group in groups if group.kind in {"stateful", "sequential", "telemetry-wrapped"}]
    payload: dict[str, Any] = {
        "task_id": args.task_id,
        "join_point": args.event,
        "task_types": args.task_type,
        "changed_paths": args.changed_path,
        "scope": scope,
        "decision": "Use a local deterministic task-run plan before asking the model to reason across more command steps.",
        "selected_pattern": "local-command-compression",
        "command_count_before": len([command for command in commands if normalize_command(command)]),
        "command_count_after": len(groups),
        "skipped_duplicate_count": len(skipped),
        "parallel_group_count": len(parallel_groups),
        "stateful_group_count": len(stateful_groups),
        "telemetry_policy": "Write execution spans to aicg.db through task-run, gate-pool, shell-adapter, telemetry record, or the command adapter; use JSONL only as an explicit isolated artifact.",
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
        telemetry_policy=payload["telemetry_policy"],
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
    return ExecutionResult(**raw)


def write_cache(path: Path, result: ExecutionResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(result), ensure_ascii=False, indent=2), encoding="utf-8")


def jsonl_artifact_directory(root: Path, value: str | None) -> Path:
    if value:
        path = Path(value)
        return path if path.is_absolute() else root / path
    return host_project_root(root) / DEFAULT_JSONL_ARTIFACT_DIR


def append_telemetry_event(root: Path, jsonl_artifact_dir: str | None, event: dict[str, Any], db: str | None = None) -> Path:
    if not jsonl_artifact_dir:
        return telemetry.append_event(
            host_project_root(root),
            event,
            db=db,
            source_command="ai_client_governance.py task-run",
        )
    directory = jsonl_artifact_directory(root, jsonl_artifact_dir)
    now = datetime.now().astimezone()
    path = directory / f"{now.year:04d}-{now.month:02d}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(telemetry.sanitized_event(event), ensure_ascii=False, sort_keys=True) + "\n")
    return path


def telemetry_event(
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
    scope = classify_scope(root=Path(result.cwd), paths=[], command=result.command, cwd=result.cwd)
    return {
        "schema_version": TELEMETRY_EVENT_SCHEMA_VERSION,
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
        "scope_kind": scope.scope_kind,
        "scope_reason": scope.scope_reason,
        "scope_paths": scope.paths,
        "adapter_enforcement": os.environ.get("AICG_EXECUTION_TELEMETRY_ENFORCEMENT", "task-run-telemetry"),
    }


def record_telemetry_status(
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
    event = telemetry_event(
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
    return append_telemetry_event(root, getattr(args, "jsonl_artifact_dir", None), event, getattr(args, "db", None))


def with_final_telemetry(
    root: Path,
    args: argparse.Namespace,
    result: ExecutionResult,
    *,
    trace_id: str,
    invocation_id: str,
) -> ExecutionResult:
    if getattr(args, "no_telemetry", False):
        return result
    try:
        path = record_telemetry_status(
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
        stderr = tail_text((result.stderr_tail + "\n" if result.stderr_tail else "") + f"telemetry write failed: {exc}")
        return replace(
            result,
            status="failed",
            exit_code=result.exit_code if result.exit_code != 0 else 126,
            stderr_tail=stderr,
        )
    return replace(result, telemetry_path=str(path))


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
    if not getattr(args, "no_telemetry", False):
        try:
            record_telemetry_status(
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
                stderr_tail=f"telemetry write failed before command: {exc}",
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
            return with_final_telemetry(root, args, result, trace_id=trace_id, invocation_id=invocation_id)
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
    return with_final_telemetry(root, args, result, trace_id=trace_id, invocation_id=invocation_id)


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
        jsonl_artifact_dir=args.jsonl_artifact_dir,
        db=args.db,
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


def jsonl_artifact_files(root: Path, jsonl_artifact_dir: str | None = None) -> list[Path]:
    directory = jsonl_artifact_directory(root, jsonl_artifact_dir)
    return sorted(directory.glob("*.jsonl")) if directory.exists() else []


def read_telemetry_events(root: Path, jsonl_artifact_dir: str | None = None, db: str | None = None) -> list[dict[str, Any]]:
    if not jsonl_artifact_dir:
        return telemetry.read_events(host_project_root(root), db=db)
    events: list[dict[str, Any]] = []
    for path in jsonl_artifact_files(root, jsonl_artifact_dir):
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


def filter_telemetry_events(
    events: list[dict[str, Any]],
    *,
    task_id: str = "",
    trace_id: str = "",
    since: str = "",
    until: str = "",
) -> list[dict[str, Any]]:
    since_dt = parse_dt(since)
    until_dt = parse_dt(until)
    filtered: list[dict[str, Any]] = []
    for event in events:
        if task_id and str(event.get("task_id") or "") != task_id:
            continue
        if trace_id and str(event.get("trace_id") or "") != trace_id:
            continue
        timestamp = event_time(event)
        if since_dt and (timestamp is None or timestamp < since_dt):
            continue
        if until_dt and (timestamp is None or timestamp > until_dt):
            continue
        filtered.append(event)
    return filtered


SHELL_ADAPTER_MARKER_BEGIN = "# >>> ai-client-governance shell-adapter >>>"
SHELL_ADAPTER_MARKER_END = "# <<< ai-client-governance shell-adapter <<<"


def is_shell_adapter_event(event: dict[str, Any]) -> bool:
    return (
        event.get("event_type") == "shell-adapter"
        or event.get("source") == "ai_client_governance.py shell-adapter"
        or bool(event.get("shell_adapter"))
    )


def is_task_run_tool_event(event: dict[str, Any]) -> bool:
    source = str(event.get("source") or "")
    event_type = str(event.get("event_type") or "")
    telemetry_sources = {
        "ai_client_governance.py task-run",
        "ai_client_governance.py tool-invocations",
        "ai_client_governance.py gate-pool",
        "ai_client_governance.py gate-pool run",
        "ai_client_governance.py telemetry",
        "ai_client_governance.py telemetry record",
    }
    return source in telemetry_sources or event_type in {"task-run", "gate-pool", "telemetry"}


def profile_has_shell_adapter(profile_path: str) -> bool:
    if not profile_path:
        return False
    profile = Path(profile_path).expanduser()
    if not profile.exists():
        return False
    try:
        text = profile.read_text(encoding="utf-8")
    except OSError:
        return False
    return SHELL_ADAPTER_MARKER_BEGIN in text and SHELL_ADAPTER_MARKER_END in text


def coord_summary(coord_root: Path) -> dict[str, Any]:
    script = resolve_governance_script(coord_root)
    if script is None:
        return {
            "available": False,
            "reason": (
                "missing embedded .ai-client/ai-client-governance/scripts/ai_client_governance.py "
                "or ai-client-governance repository-local scripts/ai_client_governance.py"
            ),
        }
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
    jsonl_artifact_dir: str | None = None,
    db: str | None = None,
    profile_path: str = "",
    task_id: str = "",
    trace_id: str = "",
    since: str = "",
    until: str = "",
) -> dict[str, Any]:
    all_events = read_telemetry_events(root, jsonl_artifact_dir, db)
    events = filter_telemetry_events(all_events, task_id=task_id, trace_id=trace_id, since=since, until=until)
    terminal_events = [event for event in events if event.get("status") != "started"]
    executed_events = [event for event in terminal_events if not event.get("cached")]
    commands = [str(event.get("command", "")).strip() for event in executed_events if event.get("command")]
    duplicates = [
        {"command": command, "count": count}
        for command, count in Counter(commands).most_common()
        if command and count > 1
    ]
    shell_adapter_events = [event for event in terminal_events if is_shell_adapter_event(event)]
    task_run_tool_events = [event for event in terminal_events if is_task_run_tool_event(event)]
    env_intercept_installed = bool(os.environ.get("AICG_SHELL_ADAPTER"))
    profile_intercept_installed = profile_has_shell_adapter(profile_path)
    adapter_installed = env_intercept_installed or profile_intercept_installed
    scope_kind_counts = Counter(str(event.get("scope_kind") or "unknown") for event in terminal_events)
    shell_adapter_modes = Counter(str(event.get("adapter_enforcement") or "unknown") for event in shell_adapter_events)
    task_run_tool_modes = Counter(str(event.get("adapter_enforcement") or "unknown") for event in task_run_tool_events)
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
        "telemetry": {
            "source": str(jsonl_artifact_directory(root, jsonl_artifact_dir)) if jsonl_artifact_dir else str(telemetry.db_path(host_project_root(root), db)),
            "filters": {
                "task_id": task_id,
                "trace_id": trace_id,
                "since": since,
                "until": until,
                "total_event_count": len(all_events),
            },
            "event_count": len(events),
            "duplicate_commands": duplicates[:10],
            "failures": failures[-10:],
            "scope_kind_counts": dict(sorted(scope_kind_counts.items())),
            "shell_adapter_auto_intercept": {
                "installed": adapter_installed,
                "env_installed": env_intercept_installed,
                "profile_installed": profile_intercept_installed,
                "profile_path": profile_path,
                "fail_closed_ready": adapter_installed,
            },
            "shell_adapter_telemetry": {
                "event_count": len(shell_adapter_events),
                "enforcement_modes": dict(sorted(shell_adapter_modes.items())),
                "latest_event": shell_adapter_events[-1] if shell_adapter_events else {},
            },
            "task_run_tool_telemetry": {
                "event_count": len(task_run_tool_events),
                "enforcement_modes": dict(sorted(task_run_tool_modes.items())),
                "latest_event": task_run_tool_events[-1] if task_run_tool_events else {},
            },
            "adapter": {
                "installed": adapter_installed,
                "event_count": len(shell_adapter_events),
                "enforcement_modes": dict(sorted(shell_adapter_modes.items())),
                "fail_closed_ready": adapter_installed,
                "latest_event": shell_adapter_events[-1] if shell_adapter_events else {},
            },
            "raw_shell_auto_intercepted": adapter_installed,
            "raw_shell_gap": ""
            if adapter_installed
            else (
                "The host client shell is not automatically intercepted by ai-client-governance; "
                "important commands should run through shell-adapter, task-run, gate-pool, or the command adapter."
            ),
        },
        "records": record_alignment_summary(root, db, task_id=task_id),
        "coordination": coord_summary(coord_root),
        "run": {
            "result_count": len(run_results),
            "failed_count": len([item for item in run_results if item.exit_code != 0]),
            "cache_hits": len([item for item in run_results if item.cached]),
            "cache_misses": len([item for item in run_results if item.cache_key and not item.cached]),
        },
    }


def table_exists(con: sqlite3.Connection, table: str) -> bool:
    row = con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
    return row is not None


def record_alignment_summary(root: Path, db: str | None = None, *, task_id: str = "") -> dict[str, Any]:
    path = telemetry.db_path(host_project_root(root), db)
    payload: dict[str, Any] = {
        "db": str(path),
        "task_record": {"available": False, "task_count": 0, "status_counts": {}},
        "task_queue": {"available": False, "total": 0, "counts": {}},
        "alignment": {
            "task_record_minus_queue_total": 0,
            "current_task_in_task_record": False,
            "current_task_in_queue": False,
            "notes": [],
        },
    }
    if not path.exists():
        payload["alignment"]["notes"].append("structured DB is missing")
        return payload
    try:
        con = sqlite3.connect(path)
        con.row_factory = sqlite3.Row
        if table_exists(con, "tasks"):
            rows = con.execute("SELECT status, count(*) AS count FROM tasks GROUP BY status").fetchall()
            status_counts = {str(row["status"]): int(row["count"]) for row in rows}
            record_count = sum(status_counts.values())
            payload["task_record"] = {
                "available": True,
                "task_count": record_count,
                "status_counts": status_counts,
            }
            if task_id:
                current = con.execute("SELECT status FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
                payload["alignment"]["current_task_in_task_record"] = current is not None
                if current:
                    payload["task_record"]["current_task_status"] = str(current["status"])
        else:
            payload["alignment"]["notes"].append("task-record table is missing")
    except sqlite3.Error as exc:
        payload["task_record"]["error"] = str(exc)
        payload["alignment"]["notes"].append("task-record summary failed")
    try:
        queue_state = task_queue.load_state(path)
        queue_summary = task_queue.queue_summary(queue_state)
        payload["task_queue"] = {
            "available": True,
            "total": int(queue_summary.get("total", 0)),
            "counts": queue_summary.get("counts", {}),
        }
        if task_id:
            payload["alignment"]["current_task_in_queue"] = any(
                item.get("id") == task_id for item in queue_summary.get("all_tasks", [])
            )
    except Exception as exc:  # defensive: diagnostics should report, not hide, queue drift.
        payload["task_queue"]["error"] = str(exc)
        payload["alignment"]["notes"].append("task-queue summary failed")
    record_count = int(payload["task_record"].get("task_count") or 0)
    queue_total = int(payload["task_queue"].get("total") or 0)
    payload["alignment"]["task_record_minus_queue_total"] = record_count - queue_total
    if record_count != queue_total:
        payload["alignment"]["notes"].append(
            "task-record and task-queue have different scopes; use this delta as a recovery/monitoring signal"
        )
    return payload


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
        f"Telemetry policy: {plan.telemetry_policy}",
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
        f"Telemetry events: {report.diagnostics.get('telemetry', {}).get('event_count', 0)}",
        "",
        "Results:",
    ]
    for item in report.results:
        cache = " cache" if item.cached else ""
        telemetry_ref = f" telemetry={item.telemetry_path}" if item.telemetry_path else ""
        lines.append(f"  - {item.node_id} {item.status}{cache} exit={item.exit_code}{telemetry_ref} {item.command}")
    return "\n".join(lines)


def format_diagnostics_text(diagnostics: dict[str, Any]) -> str:
    telemetry_report = diagnostics.get("telemetry", {})
    filters = telemetry_report.get("filters", {})
    auto_intercept = telemetry_report.get("shell_adapter_auto_intercept", {})
    shell_adapter_telemetry = telemetry_report.get("shell_adapter_telemetry", {})
    task_run_tool_telemetry = telemetry_report.get("task_run_tool_telemetry", {})
    coord = diagnostics.get("coordination", {})
    run = diagnostics.get("run", {})
    requirements = diagnostics.get("requirements", {})
    lines = [
        "AI Client Governance Task Run Diagnostics",
        (
            "Telemetry filters: "
            f"task_id={filters.get('task_id') or '<all>'} "
            f"trace_id={filters.get('trace_id') or '<all>'} "
            f"since={filters.get('since') or '<none>'} "
            f"until={filters.get('until') or '<none>'}"
        ),
        f"Telemetry source: {telemetry_report.get('source', '')}",
        f"Telemetry events: {telemetry_report.get('event_count', 0)}",
        f"Telemetry events total: {filters.get('total_event_count', telemetry_report.get('event_count', 0))}",
        f"Telemetry duplicate commands: {len(telemetry_report.get('duplicate_commands', []))}",
        f"Telemetry failures: {len(telemetry_report.get('failures', []))}",
        f"Raw shell auto intercepted: {telemetry_report.get('raw_shell_auto_intercepted')}",
        f"Shell adapter auto intercept env: {auto_intercept.get('env_installed')}",
        f"Shell adapter auto intercept profile: {auto_intercept.get('profile_installed')}",
        f"Shell adapter telemetry events: {shell_adapter_telemetry.get('event_count', 0)}",
        f"Task-run/tool telemetry events: {task_run_tool_telemetry.get('event_count', 0)}",
        f"Scope kinds: {json.dumps(telemetry_report.get('scope_kind_counts', {}), ensure_ascii=False, sort_keys=True)}",
        f"Coord available: {coord.get('available')}",
        f"Active locks: {coord.get('locks_active', 0)}",
        f"Active sessions: {coord.get('sessions_active', 0)}",
        f"Missing-worktree sessions: {len(coord.get('missing_worktree_sessions', []))}",
        f"Run cache hits: {run.get('cache_hits', 0)}",
        f"Run cache misses: {run.get('cache_misses', 0)}",
    ]
    if requirements:
        lines.append(f"Requirement failures: {', '.join(requirements.get('failed', [])) or '<none>'}")
    return "\n".join(lines)


def diagnose_requirement_failures(args: argparse.Namespace, diagnostics: dict[str, Any]) -> list[str]:
    telemetry_report = diagnostics.get("telemetry", {})
    auto_intercept = telemetry_report.get("shell_adapter_auto_intercept", {})
    shell_adapter_telemetry = telemetry_report.get("shell_adapter_telemetry", {})
    task_run_tool_telemetry = telemetry_report.get("task_run_tool_telemetry", {})
    failures: list[str] = []
    if getattr(args, "require_raw_shell_intercept", False) and not auto_intercept.get("installed"):
        failures.append("raw-shell-auto-intercept")
    if getattr(args, "require_shell_adapter_telemetry", False) and not shell_adapter_telemetry.get("event_count"):
        failures.append("shell-adapter-telemetry")
    if getattr(args, "require_task_run_tool_telemetry", False) and not task_run_tool_telemetry.get("event_count"):
        failures.append("task-run-tool-telemetry")
    return failures


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
    run.add_argument("--jsonl-artifact-dir", help="Explicit JSONL artifact directory for isolated tests or exports.")
    run.add_argument("--db", help="SQLite telemetry DB. Default: <root>/.ai-client/project/state/aicg.db.")
    run.add_argument("--task-tracking", default="", help="Optional historical task tracking path.")
    run.add_argument("--no-telemetry", action="store_true", help="Do not write execution telemetry events. Use only for isolated tests.")
    run.add_argument("--format", choices=("text", "json"), default="text")

    diagnose = sub.add_parser("diagnose", help="Report execution telemetry, cache, and coordination diagnostics.")
    diagnose.add_argument("--coord-root", help="Repository root whose worktree-coord state should be diagnosed.")
    diagnose.add_argument("--jsonl-artifact-dir", help="Explicit JSONL artifact directory for isolated tests or exports.")
    diagnose.add_argument("--db", help="SQLite telemetry DB. Default: <root>/.ai-client/project/state/aicg.db.")
    diagnose.add_argument("--profile-path", default="", help="PowerShell profile file to inspect for shell-adapter auto-intercept markers.")
    diagnose.add_argument("--task-id", default="", help="Only diagnose telemetry events for one structured task id.")
    diagnose.add_argument("--trace-id", default="", help="Only diagnose telemetry events for one trace id.")
    diagnose.add_argument("--since", default="", help="Only include telemetry events at or after this ISO timestamp.")
    diagnose.add_argument("--until", default="", help="Only include telemetry events at or before this ISO timestamp.")
    diagnose.add_argument(
        "--require-raw-shell-intercept",
        "--require-raw-shell-coverage",
        dest="require_raw_shell_intercept",
        action="store_true",
        help="Exit non-zero unless shell-adapter env/profile auto-intercept is installed.",
    )
    diagnose.add_argument("--require-shell-adapter-telemetry", action="store_true", help="Exit non-zero unless shell-adapter telemetry evidence exists.")
    diagnose.add_argument("--require-task-run-tool-telemetry", action="store_true", help="Exit non-zero unless task-run or tool-invocations telemetry evidence exists.")
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
        diagnostics = build_diagnostics(
            root=root,
            coord_root=coord_root,
            jsonl_artifact_dir=args.jsonl_artifact_dir,
            db=args.db,
            profile_path=args.profile_path,
            task_id=args.task_id,
            trace_id=args.trace_id,
            since=args.since,
            until=args.until,
        )
        requirement_failures = diagnose_requirement_failures(args, diagnostics)
        diagnostics["requirements"] = {
            "failed": requirement_failures,
            "passed": not requirement_failures,
        }
        if args.format == "json":
            print(json.dumps(diagnostics, ensure_ascii=False, indent=2))
        else:
            print(format_diagnostics_text(diagnostics))
        return 1 if requirement_failures else 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
