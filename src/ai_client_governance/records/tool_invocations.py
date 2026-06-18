#!/usr/bin/env python3
"""Run or record command-adapter telemetry spans.

The default machine fact source is .ai-client/project/state/aicg.db. JSONL output
is only an explicit isolated artifact path for tests or one-off exports.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from ai_client_governance.common.paths import PYTHON_PYCACHE_DIR, TOOL_INVOCATIONS_DIR
from ai_client_governance.records import telemetry
from ai_client_governance.runtime.scope import classify_scope

DEFAULT_JSONL_ARTIFACT_DIR = TOOL_INVOCATIONS_DIR
PYCACHE_PREFIX_ENV = "AICG_PYTHONPYCACHEPREFIX"
EXECUTION_TELEMETRY_ENFORCEMENT_ENV = "AICG_EXECUTION_TELEMETRY_ENFORCEMENT"
SCHEMA_VERSION = 2
FINAL_GATE_NAMES = {
    "ai_client_governance.py session-gate",
    "ai_client_governance.py task-gate",
    "ai_client_governance.py validate-doc",
    "ai_client_governance.py validate-encoding",
    "ai_client_governance.py scan-corrections",
    "ai_client_governance.py architecture-guard",
    "ai_client_governance.py task-queue",
}


@dataclass
class Invocation:
    invocation_id: str
    name: str
    status: str
    timestamp: str
    command: str
    task_tracking: str
    trace_id: str
    exit_code: int | None
    final_gate: bool
    summary: str
    raw: dict[str, Any]


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", normalized):
        normalized = normalized + "T00:00:00"
    result = datetime.fromisoformat(normalized)
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result


def ensure_list(value: list[str] | None) -> list[str]:
    return value if value is not None else []


def env_default(value: str | None, env_name: str) -> str:
    return value or os.environ.get(env_name, "")


def env_default_int(value: int | None, env_name: str) -> int | None:
    if value is not None:
        return value
    raw = os.environ.get(env_name, "")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def command_to_string(command: list[str] | str | None) -> str:
    if command is None:
        return ""
    if isinstance(command, str):
        return command.strip()
    return " ".join(command).strip()


def infer_name(command: list[str] | str | None, fallback: str = "unknown") -> str:
    text = command_to_string(command)
    if not text:
        return fallback
    match = re.search(r"([A-Za-z0-9_.-]+\.(?:py|ps1|sh|bat|cmd|exe))", text)
    if match:
        return match.group(1)
    tokens = re.split(r"\s+", text.strip())
    for token in tokens:
        cleaned = token.strip("'\"")
        if cleaned in {"python", "python3", "py", "powershell", "pwsh", "cmd", "/c", "--"}:
            continue
        return Path(cleaned).name or fallback
    return fallback


def is_final_gate(name: str, explicit: bool) -> bool:
    return explicit or name in FINAL_GATE_NAMES


def jsonl_artifact_dir(root: Path, jsonl_artifact_dir_arg: str | None) -> Path:
    if jsonl_artifact_dir_arg:
        path = Path(jsonl_artifact_dir_arg)
        return path if path.is_absolute() else root / path
    return root / DEFAULT_JSONL_ARTIFACT_DIR


def pycache_prefix(root: Path) -> Path:
    configured = os.environ.get(PYCACHE_PREFIX_ENV, "")
    if configured:
        path = Path(configured)
        return path if path.is_absolute() else root / path
    return root / PYTHON_PYCACHE_DIR


def jsonl_artifact_file(root: Path, jsonl_artifact_dir_arg: str | None, timestamp: str) -> Path:
    dt = parse_dt(timestamp) or datetime.now().astimezone()
    directory = jsonl_artifact_dir(root, jsonl_artifact_dir_arg)
    return directory / f"{dt.year:04d}-{dt.month:02d}.jsonl"


def append_event(root: Path, jsonl_artifact_dir_arg: str | None, event: dict[str, Any], db_arg: str | None = None) -> Path:
    if not jsonl_artifact_dir_arg:
        return telemetry.append_event(
            root,
            event,
            db=db_arg,
            source_command="ai_client_governance.py tool-invocations",
        )
    path = jsonl_artifact_file(root, jsonl_artifact_dir_arg, str(event["timestamp"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(telemetry.sanitized_event(event), ensure_ascii=False, sort_keys=True))
        handle.write("\n")
    return path


def make_event(
    *,
    invocation_id: str,
    name: str,
    command: str,
    status: str,
    task_tracking: str,
    task_types: list[str],
    phase: str,
    final_gate: bool,
    exit_code: int | None = None,
    started_at: str | None = None,
    ended_at: str | None = None,
    duration_ms: int | None = None,
    summary: str = "",
    trace_id: str = "",
    parent_invocation_id: str = "",
    task_node_id: str = "",
    parent_task_node_id: str = "",
    event_type: str = "",
    attempt: int | None = None,
    task_id: str = "",
    scope_kind: str = "",
    scope_reason: str = "",
    scope_paths: list[str] | None = None,
    adapter_enforcement: str = "",
    shell_adapter: dict[str, Any] | None = None,
) -> dict[str, Any]:
    timestamp = ended_at or started_at or now_iso()
    trace_context = telemetry.new_trace_context(trace_id=trace_id, parent_span_id=parent_invocation_id)
    return {
        "schema_version": SCHEMA_VERSION,
        "invocation_id": invocation_id,
        "timestamp": timestamp,
        "name": name,
        "command": command,
        "status": status,
        "exit_code": exit_code,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_ms": duration_ms,
        "task_tracking": task_tracking,
        "task_types": task_types,
        "phase": phase,
        "final_gate": final_gate,
        "summary": summary,
        "trace_id": trace_id,
        "parent_invocation_id": parent_invocation_id,
        "task_node_id": task_node_id,
        "parent_task_node_id": parent_task_node_id,
        "event_type": event_type,
        "attempt": attempt,
        "task_id": task_id,
        "cwd": os.getcwd(),
        "source": "ai_client_governance.py tool-invocations",
        "scope_kind": scope_kind,
        "scope_reason": scope_reason,
        "scope_paths": scope_paths or [],
        "adapter_enforcement": adapter_enforcement,
        "shell_adapter": shell_adapter or {},
        "traceparent": trace_context.traceparent,
        "tracestate": trace_context.tracestate,
        "attributes": {
            "traceparent": trace_context.traceparent,
            **({"tracestate": trace_context.tracestate} if trace_context.tracestate else {}),
        },
    }


def iter_jsonl_artifact_files(root: Path, jsonl_artifact_dir_arg: str | None) -> Iterable[Path]:
    directory = jsonl_artifact_dir(root, jsonl_artifact_dir_arg)
    if not directory.exists():
        return []
    return sorted(directory.glob("*.jsonl"))


def read_events(root: Path, jsonl_artifact_dir_arg: str | None, db_arg: str | None = None) -> list[dict[str, Any]]:
    if not jsonl_artifact_dir_arg:
        return telemetry.read_events(root, db=db_arg)
    events: list[dict[str, Any]] = []
    for path in iter_jsonl_artifact_files(root, jsonl_artifact_dir_arg):
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                events.append(json.loads(stripped))
            except json.JSONDecodeError as exc:
                events.append(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "invocation_id": f"invalid:{path}:{line_no}",
                        "timestamp": "",
                        "name": "invalid-json",
                        "command": "",
                        "status": "invalid",
                        "exit_code": None,
                        "task_tracking": "",
                        "task_types": [],
                        "phase": "",
                        "final_gate": False,
                        "summary": f"{path}:{line_no}: {exc}",
                    }
                )
    return events


def event_time(event: dict[str, Any]) -> datetime:
    return parse_dt(str(event.get("timestamp", ""))) or datetime.min.replace(tzinfo=timezone.utc)


def in_window(event: dict[str, Any], since: datetime | None, until: datetime | None) -> bool:
    timestamp = event_time(event)
    if since and timestamp < since:
        return False
    if until and timestamp > until:
        return False
    return True


def collapse_invocations(events: list[dict[str, Any]]) -> list[Invocation]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        invocation_id = str(event.get("invocation_id") or uuid.uuid4())
        grouped[invocation_id].append(event)

    invocations: list[Invocation] = []
    for invocation_id, items in grouped.items():
        items = sorted(items, key=event_time)
        final_items = [item for item in items if str(item.get("status")) != "started"]
        chosen = final_items[-1] if final_items else items[-1]
        invocations.append(
            Invocation(
                invocation_id=invocation_id,
                name=str(chosen.get("name") or "unknown"),
                status=str(chosen.get("status") or "unknown"),
                timestamp=str(chosen.get("timestamp") or ""),
                command=str(chosen.get("command") or ""),
                task_tracking=str(chosen.get("task_tracking") or ""),
                trace_id=str(chosen.get("trace_id") or ""),
                exit_code=chosen.get("exit_code"),
                final_gate=bool(chosen.get("final_gate")),
                summary=str(chosen.get("summary") or ""),
                raw=chosen,
            )
        )
    return sorted(invocations, key=lambda item: parse_dt(item.timestamp) or datetime.min.replace(tzinfo=timezone.utc))


def filter_invocations(
    invocations: list[Invocation],
    task_tracking: str | None,
    trace_id: str | None,
    since: datetime | None,
    until: datetime | None,
) -> list[Invocation]:
    result = []
    normalized_tracking = task_tracking.replace("\\", "/") if task_tracking else None
    for item in invocations:
        if since or until:
            raw_time = parse_dt(item.timestamp) or datetime.min.replace(tzinfo=timezone.utc)
            if since and raw_time < since:
                continue
            if until and raw_time > until:
                continue
        if normalized_tracking:
            item_tracking = item.task_tracking.replace("\\", "/")
            if normalized_tracking not in item_tracking:
                continue
        if trace_id and item.trace_id != trace_id:
            continue
        result.append(item)
    return result


def format_text(invocations: list[Invocation], top: int) -> str:
    lines = ["AI Client Governance Command Adapter Report", f"Invocation attempts: {len(invocations)}"]
    if not invocations:
        lines.extend(["", "No invocation records found."])
        return "\n".join(lines)

    by_name = Counter(item.name for item in invocations)
    failures = Counter(item.name for item in invocations if item.status == "failed" or item.exit_code not in {None, 0})
    latest_by_name: dict[str, Invocation] = {}
    for item in invocations:
        latest_by_name[item.name] = item

    lines.append("")
    lines.append("Top operations:")
    for name, count in by_name.most_common(top):
        latest = latest_by_name[name]
        lines.append(
            f"  {name}: count={count} failures={failures.get(name, 0)} "
            f"latest={latest.timestamp} status={latest.status} exit={latest.exit_code}"
        )

    gate_invocations = [item for item in invocations if item.final_gate]
    lines.append("")
    lines.append("Final gate invocations:")
    if not gate_invocations:
        lines.append("  none")
    else:
        for item in gate_invocations[-top:]:
            lines.append(
                f"  {item.timestamp} {item.name} status={item.status} "
                f"exit={item.exit_code} tracking={item.task_tracking or 'none'}"
            )

    latest = invocations[-1]
    lines.extend(
        [
            "",
            "Latest invocation:",
            f"  {latest.timestamp} {latest.name} status={latest.status} exit={latest.exit_code}",
            f"  command={latest.command}",
        ]
    )
    return "\n".join(lines)


def format_markdown(invocations: list[Invocation], top: int) -> str:
    if not invocations:
        return "# AI Client Governance Command Adapter Report\n\n- Invocation attempts: 0\n"

    by_name = Counter(item.name for item in invocations)
    failures = Counter(item.name for item in invocations if item.status == "failed" or item.exit_code not in {None, 0})
    latest_by_name: dict[str, Invocation] = {}
    for item in invocations:
        latest_by_name[item.name] = item

    lines = [
        "# AI Client Governance Command Adapter Report",
        "",
        f"- Invocation attempts: {len(invocations)}",
        "",
        "| Tool | Count | Failures | Latest | Status | Exit |",
        "|---|---:|---:|---|---|---:|",
    ]
    for name, count in by_name.most_common(top):
        latest = latest_by_name[name]
        exit_code = "" if latest.exit_code is None else str(latest.exit_code)
        lines.append(
            f"| `{name}` | {count} | {failures.get(name, 0)} | {latest.timestamp} | "
            f"{latest.status} | {exit_code} |"
        )

    gate_invocations = [item for item in invocations if item.final_gate]
    lines.extend(["", "## Final Gate Invocations", ""])
    if not gate_invocations:
        lines.append("- None")
    else:
        for item in gate_invocations[-top:]:
            lines.append(
                f"- {item.timestamp}: `{item.name}` status={item.status}, "
                f"exit={item.exit_code}, tracking={item.task_tracking or 'none'}"
            )
    return "\n".join(lines)


def command_record(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    command = args.command or ""
    name = args.name or infer_name(command)
    timestamp = args.ended_at or args.started_at or now_iso()
    invocation_id = args.invocation_id or str(uuid.uuid4())
    trace_id = env_default(args.trace_id, "CODEX_TRACE_ID") or invocation_id
    scope = classify_scope(root=root, paths=args.scope_path or [], command=command, cwd=os.getcwd())
    scope_kind = args.scope_kind or scope.scope_kind
    scope_reason = args.scope_reason or scope.scope_reason
    event = make_event(
        invocation_id=invocation_id,
        name=name,
        command=command,
        status=args.status,
        task_tracking=args.task_tracking or "",
        task_types=ensure_list(args.task_type),
        phase=args.phase or "",
        final_gate=is_final_gate(name, args.final_gate),
        exit_code=args.exit_code,
        started_at=args.started_at,
        ended_at=args.ended_at or timestamp,
        duration_ms=args.duration_ms,
        summary=args.summary or "",
        trace_id=trace_id,
        parent_invocation_id=env_default(args.parent_invocation_id, "CODEX_PARENT_INVOCATION_ID"),
        task_node_id=env_default(args.task_node_id, "CODEX_TASK_NODE_ID"),
        parent_task_node_id=env_default(args.parent_task_node_id, "CODEX_PARENT_TASK_NODE_ID"),
        event_type=env_default(args.event_type, "CODEX_EVENT_TYPE"),
        attempt=env_default_int(args.attempt, "CODEX_ATTEMPT"),
        task_id=args.task_id or "",
        scope_kind=scope_kind,
        scope_reason=scope_reason,
        scope_paths=scope.paths,
        adapter_enforcement=args.adapter_enforcement or os.environ.get(EXECUTION_TELEMETRY_ENFORCEMENT_ENV, "tool-invocations"),
    )
    path = append_event(root, args.jsonl_artifact_dir, event, args.db)
    print(f"recorded {name} status={args.status} telemetry={path}")
    return 0


def command_run(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("run requires a command after --", file=sys.stderr)
        return 2

    name = args.name or infer_name(command)
    invocation_id = str(uuid.uuid4())
    trace_id = env_default(args.trace_id, "CODEX_TRACE_ID") or invocation_id
    parent_invocation_id = env_default(args.parent_invocation_id, "CODEX_PARENT_INVOCATION_ID")
    task_node_id = env_default(args.task_node_id, "CODEX_TASK_NODE_ID")
    parent_task_node_id = env_default(args.parent_task_node_id, "CODEX_PARENT_TASK_NODE_ID")
    event_type = env_default(args.event_type, "CODEX_EVENT_TYPE")
    attempt = env_default_int(args.attempt, "CODEX_ATTEMPT")
    started_at = now_iso()
    command_text = command_to_string(command)
    scope = classify_scope(root=root, paths=args.scope_path or [], command=command_text, cwd=args.cwd or os.getcwd())
    scope_kind = args.scope_kind or scope.scope_kind
    scope_reason = args.scope_reason or scope.scope_reason
    start_event = make_event(
        invocation_id=invocation_id,
        name=name,
        command=command_text,
        status="started",
        task_tracking=args.task_tracking or "",
        task_types=ensure_list(args.task_type),
        phase=args.phase or "",
        final_gate=is_final_gate(name, args.final_gate),
        started_at=started_at,
        summary=args.summary or "",
        trace_id=trace_id,
        parent_invocation_id=parent_invocation_id,
        task_node_id=task_node_id,
        parent_task_node_id=parent_task_node_id,
        event_type=event_type,
        attempt=attempt,
        task_id=args.task_id or "",
        scope_kind=scope_kind,
        scope_reason=scope_reason,
        scope_paths=scope.paths,
        adapter_enforcement=args.adapter_enforcement or os.environ.get(EXECUTION_TELEMETRY_ENFORCEMENT_ENV, "tool-invocations"),
    )
    append_event(root, args.jsonl_artifact_dir, start_event, args.db)

    start = time.monotonic()
    child_env = os.environ.copy()
    child_env.update(telemetry.env_for_child(trace_id=trace_id, parent_span_id=invocation_id))
    child_env["CODEX_TRACE_ID"] = trace_id
    child_env["CODEX_PARENT_INVOCATION_ID"] = invocation_id
    child_env["PYTHONPYCACHEPREFIX"] = str(pycache_prefix(root))
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env["PYTHONUTF8"] = "1"
    if task_node_id:
        child_env["CODEX_PARENT_TASK_NODE_ID"] = task_node_id
    completed = subprocess.run(command, cwd=args.cwd or None, env=child_env)
    ended_at = now_iso()
    duration_ms = int((time.monotonic() - start) * 1000)
    status = "succeeded" if completed.returncode == 0 else "failed"
    end_event = make_event(
        invocation_id=invocation_id,
        name=name,
        command=command_text,
        status=status,
        task_tracking=args.task_tracking or "",
        task_types=ensure_list(args.task_type),
        phase=args.phase or "",
        final_gate=is_final_gate(name, args.final_gate),
        exit_code=completed.returncode,
        started_at=started_at,
        ended_at=ended_at,
        duration_ms=duration_ms,
        summary=args.summary or "",
        trace_id=trace_id,
        parent_invocation_id=parent_invocation_id,
        task_node_id=task_node_id,
        parent_task_node_id=parent_task_node_id,
        event_type=event_type,
        attempt=attempt,
        task_id=args.task_id or "",
        scope_kind=scope_kind,
        scope_reason=scope_reason,
        scope_paths=scope.paths,
        adapter_enforcement=args.adapter_enforcement or os.environ.get(EXECUTION_TELEMETRY_ENFORCEMENT_ENV, "tool-invocations"),
    )
    path = append_event(root, args.jsonl_artifact_dir, end_event, args.db)
    print(f"recorded {name} status={status} exit={completed.returncode} telemetry={path}")
    return completed.returncode


def command_report(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    events = read_events(root, args.jsonl_artifact_dir, args.db)
    invocations = collapse_invocations(events)
    invocations = filter_invocations(
        invocations,
        args.task_tracking,
        args.trace_id,
        parse_dt(args.since),
        parse_dt(args.until),
    )

    if args.format == "json":
        payload = [item.raw for item in invocations]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif args.format == "markdown":
        print(format_markdown(invocations, args.top))
    else:
        print(format_text(invocations, args.top))

    if args.require_final_gate and not any(item.final_gate for item in invocations):
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run or record command-adapter execution telemetry spans."
    )
    parser.add_argument("--root", default=".", help="Repository root.")
    parser.add_argument(
        "--jsonl-artifact-dir",
        help="Explicit JSONL artifact directory for isolated tests or exports. Default records to aicg.db.",
    )
    parser.add_argument("--db", help="SQLite telemetry DB. Default: <root>/.ai-client/project/state/aicg.db.")
    subparsers = parser.add_subparsers(dest="command_name", required=True)

    record = subparsers.add_parser("record", help="Append one command-adapter span event.")
    record.add_argument("--name", help="Operation, tool, or script name.")
    record.add_argument("--command", help="Command string.")
    record.add_argument("--status", default="succeeded", help="Invocation status.")
    record.add_argument("--exit-code", type=int, help="Process exit code.")
    record.add_argument("--task-tracking", help="Related task tracking file.")
    record.add_argument("--task-id", help="Related structured task id.")
    record.add_argument("--task-type", action="append", help="Related task type.")
    record.add_argument("--scope-kind", help="Explicit governance scope kind.")
    record.add_argument("--scope-reason", help="Explicit governance scope reason.")
    record.add_argument("--scope-path", action="append", help="Path used for common/project/native scope classification.")
    record.add_argument("--adapter-enforcement", help="Telemetry enforcement adapter label.")
    record.add_argument("--phase", help="Task phase, e.g. planning or final-gate.")
    record.add_argument("--summary", help="Short result summary.")
    record.add_argument("--final-gate", action="store_true", help="Mark as final gate evidence.")
    record.add_argument("--trace-id", help="Trace id shared by related invocations.")
    record.add_argument("--parent-invocation-id", help="Parent invocation id for tree flow reports.")
    record.add_argument("--task-node-id", help="Task tree node id associated with this invocation.")
    record.add_argument("--parent-task-node-id", help="Parent task tree node id.")
    record.add_argument("--event-type", help="Event type, e.g. gate-pool, gate, report, validation.")
    record.add_argument("--attempt", type=int, help="Retry attempt number for this invocation.")
    record.add_argument("--started-at", help="ISO start timestamp.")
    record.add_argument("--ended-at", help="ISO end timestamp.")
    record.add_argument("--duration-ms", type=int, help="Duration in milliseconds.")
    record.add_argument("--invocation-id", help="Explicit invocation id.")
    record.set_defaults(func=command_record)

    run = subparsers.add_parser("run", help="Run a command and record its result as execution telemetry.")
    run.add_argument("--name", help="Operation, tool, or script name.")
    run.add_argument("--task-tracking", help="Related task tracking file.")
    run.add_argument("--task-id", help="Related structured task id.")
    run.add_argument("--task-type", action="append", help="Related task type.")
    run.add_argument("--scope-kind", help="Explicit governance scope kind.")
    run.add_argument("--scope-reason", help="Explicit governance scope reason.")
    run.add_argument("--scope-path", action="append", help="Path used for common/project/native scope classification.")
    run.add_argument("--adapter-enforcement", help="Telemetry enforcement adapter label.")
    run.add_argument("--phase", help="Task phase, e.g. planning or final-gate.")
    run.add_argument("--summary", help="Short result summary.")
    run.add_argument("--final-gate", action="store_true", help="Mark as final gate evidence.")
    run.add_argument("--trace-id", help="Trace id shared by related invocations.")
    run.add_argument("--parent-invocation-id", help="Parent invocation id for tree flow reports.")
    run.add_argument("--task-node-id", help="Task tree node id associated with this invocation.")
    run.add_argument("--parent-task-node-id", help="Parent task tree node id.")
    run.add_argument("--event-type", help="Event type, e.g. gate-pool, gate, report, validation.")
    run.add_argument("--attempt", type=int, help="Retry attempt number for this invocation.")
    run.add_argument("--cwd", help="Working directory for the command.")
    run.add_argument("command", nargs=argparse.REMAINDER)
    run.set_defaults(func=command_run)

    report = subparsers.add_parser("report", help="Summarize command-adapter records.")
    report.add_argument("--since", help="Only include events after this date/time.")
    report.add_argument("--until", help="Only include events before this date/time.")
    report.add_argument("--task-tracking", help="Filter by task tracking substring.")
    report.add_argument("--trace-id", help="Filter by trace id.")
    report.add_argument("--top", type=int, default=10, help="Number of rows to show.")
    report.add_argument(
        "--format",
        choices=("text", "markdown", "json"),
        default="text",
        help="Output format.",
    )
    report.add_argument(
        "--require-final-gate",
        action="store_true",
        help="Exit 1 when no final-gate invocation exists in the report window.",
    )
    report.set_defaults(func=command_report)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
