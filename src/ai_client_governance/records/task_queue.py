#!/usr/bin/env python3
"""Manage the project-local AI task workflow.

The task state stored in ``aicg.db`` is deliberately closer to a small workflow engine than a
plain queue. User input first becomes a candidate or approval-waiting task.
Only an approved, ready task can be started. This prevents the common failure
mode where a new user message is treated as active work before scope and
approval have been resolved.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import uuid
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ai_client_governance.common import cli_arguments as common_cli_args
from ai_client_governance.records import state_store


QUEUE_STATE_TYPE = "task-queue"
QUEUE_STATE_KEY = "default"
SCHEMA_VERSION = 2

INTAKE_STATUSES = {"candidate", "awaiting_approval"}
READY_STATUSES = {"ready"}
RUNNING_STATUSES = {"active", "waiting_user", "waiting_tool", "waiting_agent", "verifying"}
BLOCKED_STATUSES = {"blocked"}
OPEN_STATUSES = INTAKE_STATUSES | READY_STATUSES | RUNNING_STATUSES | BLOCKED_STATUSES
CLOSED_STATUSES = {"completed", "cancelled", "rejected"}
ALL_STATUSES = OPEN_STATUSES | CLOSED_STATUSES
MUTATING_COMMANDS = {
    "enqueue",
    "request-approval",
    "approve",
    "start-next",
    "wait",
    "resume",
    "complete",
    "block",
    "cancel",
    "reject",
    "heartbeat",
}

WAIT_STATUS_BY_KIND = {
    "user": "waiting_user",
    "tool": "waiting_tool",
    "agent": "waiting_agent",
    "verification": "verifying",
}

ALLOWED_TRANSITIONS = {
    "candidate": {"awaiting_approval", "ready", "blocked", "cancelled", "rejected"},
    "awaiting_approval": {"ready", "blocked", "cancelled", "rejected"},
    "ready": {"active", "blocked", "cancelled"},
    "active": {"waiting_user", "waiting_tool", "waiting_agent", "verifying", "blocked", "completed", "cancelled"},
    "waiting_user": {"active", "blocked", "cancelled"},
    "waiting_tool": {"active", "blocked", "cancelled"},
    "waiting_agent": {"active", "blocked", "cancelled"},
    "verifying": {"active", "blocked", "completed", "cancelled"},
    "blocked": {"candidate", "awaiting_approval", "ready", "cancelled"},
}


@dataclass
class Finding:
    level: str
    message: str


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage AI task workflow state.")
    # Register common globals at both levels so either option order survives.
    common_cli_args.add_common_global_args(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    enqueue = sub.add_parser("enqueue", help="Create or refresh a candidate task.")
    common_cli_args.add_common_global_args(enqueue, suppress_default=True)
    enqueue.add_argument("--title", required=True)
    enqueue.add_argument("--message", required=True)
    enqueue.add_argument("--source", default="user")
    enqueue.add_argument("--task-tracking", required=True)
    enqueue.add_argument("--approval-label", default="")
    enqueue.add_argument("--trace-id", default="")
    enqueue.add_argument("--status", choices=("candidate", "awaiting_approval", "ready"), default="candidate")
    enqueue.add_argument("--task-id", help="Explicit task id. Default: generated.")
    enqueue.add_argument("--parent-task-id", default="")
    enqueue.add_argument("--context", action="append", default=[], help="Context/restoration path.")

    request_approval = sub.add_parser("request-approval", help="Move a candidate task to awaiting_approval.")
    common_cli_args.add_common_global_args(request_approval, suppress_default=True)
    request_approval.add_argument("--task-id", required=True)
    request_approval.add_argument("--approval-label", required=True)
    request_approval.add_argument("--summary", default="")

    approve = sub.add_parser("approve", help="Mark an approval-waiting task as ready.")
    common_cli_args.add_common_global_args(approve, suppress_default=True)
    approve.add_argument("--task-id", required=True)
    approve.add_argument("--approval-label", required=True)
    approve.add_argument("--summary", default="")

    start = sub.add_parser("start-next", help="Mark the next ready task active.")
    common_cli_args.add_common_global_args(start, suppress_default=True)
    start.add_argument("--task-id", help="Specific ready task id. Default: first ready task.")

    wait = sub.add_parser("wait", help="Move an active task into a waiting state.")
    common_cli_args.add_common_global_args(wait, suppress_default=True)
    wait.add_argument("--task-id", required=True)
    wait.add_argument("--kind", choices=sorted(WAIT_STATUS_BY_KIND), required=True)
    wait.add_argument("--reason", required=True)

    resume = sub.add_parser("resume", help="Resume a waiting or verifying task back to active.")
    common_cli_args.add_common_global_args(resume, suppress_default=True)
    resume.add_argument("--task-id", required=True)
    resume.add_argument("--summary", default="")

    complete = sub.add_parser("complete", help="Mark a task completed.")
    common_cli_args.add_common_global_args(complete, suppress_default=True)
    complete.add_argument("--task-id")
    complete.add_argument("--trace-id")
    complete.add_argument("--task-tracking")
    complete.add_argument("--summary", default="")

    block = sub.add_parser("block", help="Mark a task blocked.")
    common_cli_args.add_common_global_args(block, suppress_default=True)
    block.add_argument("--task-id", required=True)
    block.add_argument("--reason", required=True)

    cancel = sub.add_parser("cancel", help="Cancel an open task.")
    common_cli_args.add_common_global_args(cancel, suppress_default=True)
    cancel.add_argument("--task-id", required=True)
    cancel.add_argument("--reason", required=True)

    reject = sub.add_parser("reject", help="Reject an approval-waiting task.")
    common_cli_args.add_common_global_args(reject, suppress_default=True)
    reject.add_argument("--task-id", required=True)
    reject.add_argument("--reason", required=True)

    status = sub.add_parser("status", help="Print queue status.")
    common_cli_args.add_common_global_args(status, suppress_default=True)

    lifecycle = sub.add_parser("lifecycle", help="Print a unified queue/task-record lifecycle view.")
    common_cli_args.add_common_global_args(lifecycle, suppress_default=True)
    lifecycle.add_argument("--task-id", help="Only report one task id.")

    heartbeat = sub.add_parser("heartbeat", help="Print a periodic queue heartbeat.")
    common_cli_args.add_common_global_args(heartbeat, suppress_default=True)

    validate = sub.add_parser("validate", help="Validate queue invariants.")
    common_cli_args.add_common_global_args(validate, suppress_default=True)
    validate.add_argument("--current-task-tracking")
    validate.add_argument("--trace-id")
    validate.add_argument("--require-current", action="store_true")
    validate.add_argument("--strict-fifo", action="store_true")
    validate.add_argument("--fail-on-warning", action="store_true")

    return parser.parse_args()


def queue_db_path(root: Path, override: str | None) -> Path:
    return state_store.db_path(root, override)


def default_state() -> dict[str, Any]:
    timestamp = now_iso()
    return {
        "schema_version": SCHEMA_VERSION,
        "created_at": timestamp,
        "updated_at": timestamp,
        "policy": {
            "strict_fifo": True,
            "max_active_tasks": 1,
            "context_required": True,
            "approval_required_for_active": True,
        },
        "events": [],
        "tasks": [],
    }


def load_state(path: Path) -> dict[str, Any]:
    con = state_store.connect(path)
    row = state_store.read_state(con, state_type=QUEUE_STATE_TYPE, state_key=QUEUE_STATE_KEY)
    if row is None:
        return default_state()
    data = row["payload"]
    data.setdefault("created_at", now_iso())
    data.setdefault("updated_at", now_iso())
    data.setdefault("policy", {})
    data.setdefault("events", [])
    data.setdefault("tasks", [])
    if int(data.get("schema_version", 1)) < SCHEMA_VERSION:
        migrate_schema_v1_state(data)
    data["schema_version"] = SCHEMA_VERSION
    data["policy"].setdefault("strict_fifo", True)
    data["policy"].setdefault("max_active_tasks", 1)
    data["policy"].setdefault("context_required", True)
    data["policy"].setdefault("approval_required_for_active", True)
    return data


def load_state_readonly(path: Path) -> dict[str, Any]:
    if not path.exists():
        return default_state()
    return load_state(path)


def migrate_schema_v1_state(state: dict[str, Any]) -> None:
    timestamp = now_iso()
    for task in state.get("tasks", []):
        original = task.get("status", "")
        approval = str(task.get("approval_label", ""))
        if original == "queued":
            task["status"] = "awaiting_approval" if approval.startswith("待批准") else "ready"
            task["started_at"] = ""
        elif original == "active" and approval.startswith("待批准"):
            task["status"] = "awaiting_approval"
            task["started_at"] = ""
        elif original == "active":
            task["status"] = "active"
        task["updated_at"] = timestamp
        add_history(
            task,
            "schema_v1_state_migrated",
            task.get("status", ""),
            f"schema 1 status {original!r} migrated to schema 2 workflow state",
        )
        record_state_event(
            state,
            task,
            "schema_v1_state_migrated",
            original,
            task.get("status", ""),
            "schema 1 queue state migrated",
        )


def save_state(path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = now_iso()
    con = state_store.connect(path)
    state_store.upsert_state(
        con,
        state_type=QUEUE_STATE_TYPE,
        state_key=QUEUE_STATE_KEY,
        payload=state,
        source_command="ai_client_governance.py task-queue",
        summary="task queue workflow state",
        event_type="task_queue.state_saved",
    )


@contextmanager
def queue_lock(path: Path):
    # SQLite is the queue's live state coordinator; keep this context so the
    # command dispatch remains simple while avoiding JSON lock sidecars.
    yield


def message_hash(message: str) -> str:
    return hashlib.sha256(message.encode("utf-8")).hexdigest()


def is_approval_label(value: str) -> bool:
    text = value.strip()
    return bool(text) and not text.startswith("待批准")


def make_task(args: argparse.Namespace) -> dict[str, Any]:
    timestamp = now_iso()
    task_id = args.task_id or f"TQ-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"
    if args.status == "ready" and not is_approval_label(args.approval_label):
        raise ValueError("ready tasks require an explicit approval label")
    task = {
        "id": task_id,
        "title": args.title,
        "source": args.source,
        "status": args.status,
        "created_at": timestamp,
        "updated_at": timestamp,
        "started_at": "",
        "completed_at": "",
        "task_tracking": args.task_tracking,
        "approval_label": args.approval_label,
        "trace_id": args.trace_id,
        "parent_task_id": args.parent_task_id,
        "context": {
            "message_sha256": message_hash(args.message),
            "message_excerpt": args.message[:500],
            "restore_reading_list": list(args.context or []),
        },
        "history": [],
    }
    add_history(task, "candidate_created", args.status, "task candidate recorded")
    return task


def find_task(
    tasks: list[dict[str, Any]],
    *,
    task_id: str | None = None,
    trace_id: str | None = None,
    task_tracking: str | None = None,
) -> dict[str, Any] | None:
    if task_id:
        return next((task for task in tasks if task.get("id") == task_id), None)

    matches = tasks
    if trace_id:
        matches = [task for task in matches if task.get("trace_id") == trace_id]
    if task_tracking:
        matches = [
            task
            for task in matches
            if task_tracking_matches(task.get("task_tracking", ""), task_tracking)
        ]
    if not trace_id and not task_tracking:
        return None
    if not matches:
        return None

    status_priority = {
        "active": 0,
        "verifying": 1,
        "waiting_user": 2,
        "waiting_tool": 2,
        "waiting_agent": 2,
        "ready": 3,
        "candidate": 4,
        "awaiting_approval": 4,
        "blocked": 5,
        "completed": 6,
        "cancelled": 7,
        "rejected": 8,
    }
    return sorted(matches, key=lambda task: status_priority.get(task.get("status", ""), 99))[0]


def normalize(value: str | Path) -> str:
    return str(value).replace("\\", "/").lstrip("./")


def normalize_tracking_path(value: str | Path) -> str:
    text = str(value).replace("\\", "/").rstrip("/")
    while text.startswith("./"):
        text = text[2:]
    return text


def task_tracking_matches(stored: str, requested: str | Path) -> bool:
    stored_normalized = normalize_tracking_path(stored)
    requested_normalized = normalize_tracking_path(requested)
    if normalize(stored_normalized) == normalize(requested_normalized):
        return True
    return requested_normalized.endswith(f"/{stored_normalized}") or stored_normalized.endswith(
        f"/{requested_normalized}"
    )


def tasks_with_status(state: dict[str, Any], statuses: set[str]) -> list[dict[str, Any]]:
    return [task for task in state.get("tasks", []) if task.get("status") in statuses]


def open_tasks(state: dict[str, Any]) -> list[dict[str, Any]]:
    return tasks_with_status(state, OPEN_STATUSES)


def active_tasks(state: dict[str, Any]) -> list[dict[str, Any]]:
    return tasks_with_status(state, {"active"})


def ready_tasks(state: dict[str, Any]) -> list[dict[str, Any]]:
    return tasks_with_status(state, READY_STATUSES)


def runnable_or_running_tasks(state: dict[str, Any]) -> list[dict[str, Any]]:
    return tasks_with_status(state, READY_STATUSES | RUNNING_STATUSES)


def add_history(task: dict[str, Any], event: str, status: str, summary: str = "", from_status: str = "") -> None:
    task.setdefault("history", []).append(
        {
            "at": now_iso(),
            "event": event,
            "from_status": from_status,
            "status": status,
            "summary": summary,
        }
    )


def record_state_event(
    state: dict[str, Any],
    task: dict[str, Any],
    event: str,
    from_status: str,
    to_status: str,
    summary: str = "",
) -> None:
    state.setdefault("events", []).append(
        {
            "at": now_iso(),
            "task_id": task.get("id", ""),
            "event": event,
            "from_status": from_status,
            "to_status": to_status,
            "summary": summary,
        }
    )


def transition_task(state: dict[str, Any], task: dict[str, Any], to_status: str, event: str, summary: str = "") -> None:
    from_status = task.get("status", "")
    if to_status not in ALLOWED_TRANSITIONS.get(from_status, set()):
        raise ValueError(f"illegal transition for {task.get('id')}: {from_status} -> {to_status}")
    task["status"] = to_status
    task["updated_at"] = now_iso()
    if to_status == "active":
        task["started_at"] = now_iso()
    if to_status == "completed":
        task["completed_at"] = now_iso()
    add_history(task, event, to_status, summary, from_status=from_status)
    record_state_event(state, task, event, from_status, to_status, summary)


def queue_summary(state: dict[str, Any]) -> dict[str, Any]:
    tasks = state.get("tasks", [])
    counts = {status: 0 for status in sorted(ALL_STATUSES)}
    for task in tasks:
        status = task.get("status", "unknown")
        counts[status] = counts.get(status, 0) + 1
    active = active_tasks(state)
    ready = ready_tasks(state)
    waiting = tasks_with_status(state, {"waiting_user", "waiting_tool", "waiting_agent", "verifying"})
    awaiting_approval = tasks_with_status(state, {"awaiting_approval"})
    candidates = tasks_with_status(state, {"candidate"})
    blocked = tasks_with_status(state, {"blocked"})
    return {
        "total": len(tasks),
        "counts": counts,
        "active": active,
        "ready": ready,
        "waiting": waiting,
        "awaiting_approval": awaiting_approval,
        "candidates": candidates,
        "blocked": blocked,
        "all_tasks": tasks,
        "next_task": ready[0] if ready else None,
        "updated_at": state.get("updated_at", ""),
    }


def normalize_lifecycle_status(source: str, status: str) -> tuple[str, str]:
    raw = status or "unknown"
    if source == "task-queue" and raw == "completed":
        return "done", ""
    if raw in {"waiting_user", "waiting_tool", "waiting_agent"}:
        return "active", raw
    if raw in {
        "candidate",
        "awaiting_approval",
        "ready",
        "active",
        "verifying",
        "done",
        "blocked",
        "cancelled",
        "rejected",
    }:
        return raw, ""
    return "unknown", raw


def read_task_record_rows(path: Path, task_id: str | None = None) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    uri = f"file:{path.as_posix()}?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True)
        con.row_factory = sqlite3.Row
        table = con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'tasks'"
        ).fetchone()
        if table is None:
            return []
        params: list[Any] = []
        where = ""
        if task_id:
            where = "WHERE task_id = ?"
            params.append(task_id)
        rows = con.execute(
            f"SELECT task_id, title, status, trace_id, task_types_json, updated_at FROM tasks {where} ORDER BY updated_at DESC",
            params,
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error:
        return []
    finally:
        try:
            con.close()  # type: ignore[name-defined]
        except Exception:
            pass


def lifecycle_entry(
    *,
    task_id: str,
    queue_task: dict[str, Any] | None,
    record_task: dict[str, Any] | None,
) -> dict[str, Any]:
    warnings: list[str] = []
    queue_status = str(queue_task.get("status") or "") if queue_task else ""
    record_status = str(record_task.get("status") or "") if record_task else ""
    queue_normalized, queue_substatus = normalize_lifecycle_status("task-queue", queue_status)
    record_normalized, record_substatus = normalize_lifecycle_status("task-record", record_status)
    lifecycle_status = record_normalized if record_task else queue_normalized
    if queue_task and record_task and queue_normalized != record_normalized:
        warnings.append("status_drift")
        lifecycle_status = "drift"
    if queue_task is None:
        warnings.append("missing_in_queue")
    if record_task is None:
        warnings.append("missing_in_task_record")
    queue_trace = str(queue_task.get("trace_id") or "") if queue_task else ""
    record_trace = str(record_task.get("trace_id") or "") if record_task else ""
    if queue_trace and record_trace and queue_trace != record_trace:
        warnings.append("trace_id_drift")
    return {
        "task_id": task_id,
        "lifecycle_status": lifecycle_status,
        "warnings": warnings,
        "task_queue": {
            "exists": queue_task is not None,
            "task_id": queue_task.get("id", "") if queue_task else "",
            "raw_status": queue_status,
            "normalized_status": queue_normalized if queue_task else "",
            "substatus": queue_substatus,
            "trace_id": queue_trace,
            "title": queue_task.get("title", "") if queue_task else "",
            "updated_at": queue_task.get("updated_at", "") if queue_task else "",
        },
        "task_record": {
            "exists": record_task is not None,
            "task_id": record_task.get("task_id", "") if record_task else "",
            "raw_status": record_status,
            "normalized_status": record_normalized if record_task else "",
            "substatus": record_substatus,
            "trace_id": record_trace,
            "title": record_task.get("title", "") if record_task else "",
            "updated_at": record_task.get("updated_at", "") if record_task else "",
        },
    }


def lifecycle_summary(root: Path, path: Path, task_id: str | None = None) -> dict[str, Any]:
    queue_state = load_state_readonly(path)
    queue_tasks = {
        str(task.get("id") or ""): task
        for task in queue_state.get("tasks", [])
        if not task_id or str(task.get("id") or "") == task_id
    }
    record_rows = {
        str(row.get("task_id") or ""): row
        for row in read_task_record_rows(path, task_id)
    }
    entries: list[dict[str, Any]] = []
    used_queue: set[str] = set()
    used_record: set[str] = set()
    for item_id in sorted(set(queue_tasks) & set(record_rows)):
        entries.append(lifecycle_entry(task_id=item_id, queue_task=queue_tasks[item_id], record_task=record_rows[item_id]))
        used_queue.add(item_id)
        used_record.add(item_id)

    remaining_records_by_trace = {
        str(row.get("trace_id") or ""): (record_id, row)
        for record_id, row in record_rows.items()
        if record_id not in used_record and row.get("trace_id")
    }
    for queue_id, queue_task in sorted(queue_tasks.items()):
        if queue_id in used_queue:
            continue
        trace_id = str(queue_task.get("trace_id") or "")
        match = remaining_records_by_trace.get(trace_id)
        if match:
            record_id, record_task = match
            entry = lifecycle_entry(task_id=queue_id, queue_task=queue_task, record_task=record_task)
            entry["warnings"].append("id_trace_drift")
            entries.append(entry)
            used_queue.add(queue_id)
            used_record.add(record_id)

    for queue_id, queue_task in sorted(queue_tasks.items()):
        if queue_id not in used_queue:
            entries.append(lifecycle_entry(task_id=queue_id, queue_task=queue_task, record_task=None))
    for record_id, record_task in sorted(record_rows.items()):
        if record_id not in used_record:
            entries.append(lifecycle_entry(task_id=record_id, queue_task=None, record_task=record_task))
    warnings = sorted({warning for entry in entries for warning in entry["warnings"]})
    status_counts = Counter(str(entry.get("lifecycle_status") or "unknown") for entry in entries)
    queue_total = len(queue_tasks)
    record_total = len(record_rows)
    return {
        "state_db": path.as_posix(),
        "root": root.as_posix(),
        "task_id": task_id or "",
        "schema_version": SCHEMA_VERSION,
        "entry_count": len(entries),
        "status_counts": dict(status_counts),
        "task_record_minus_queue_total": record_total - queue_total,
        "warnings": warnings,
        "entries": entries,
        "status_mapping": {
            "task_queue.completed": "done",
            "task_record.done": "done",
            "waiting_user|waiting_tool|waiting_agent": "active with substatus",
        },
    }


def render_summary(summary: dict[str, Any]) -> str:
    lines = [
        "AI Task Workflow",
        f"Total: {summary['total']}",
        "Counts: "
        + ", ".join(f"{key}={value}" for key, value in sorted(summary["counts"].items()) if value),
        "",
    ]
    if summary["active"]:
        lines.append("Active:")
        for task in summary["active"]:
            lines.append(f"  - {task.get('id')}: {task.get('title')} ({task.get('task_tracking')})")
    else:
        lines.append("Active: none")
    if summary["waiting"]:
        lines.append("Waiting:")
        for task in summary["waiting"]:
            lines.append(f"  - {task.get('id')}: {task.get('status')} {task.get('title')}")
    if summary["ready"]:
        lines.append("Ready:")
        for task in summary["ready"]:
            lines.append(f"  - {task.get('id')}: {task.get('title')}")
    if summary["awaiting_approval"]:
        lines.append("Awaiting approval:")
        for task in summary["awaiting_approval"]:
            lines.append(f"  - {task.get('id')}: {task.get('title')} [{task.get('approval_label')}]")
    if summary["candidates"]:
        lines.append("Candidates:")
        for task in summary["candidates"]:
            lines.append(f"  - {task.get('id')}: {task.get('title')}")
    if summary["next_task"]:
        task = summary["next_task"]
        lines.append(f"Next ready: {task.get('id')}: {task.get('title')}")
    else:
        lines.append("Next ready: none")
    if summary["blocked"]:
        lines.append("Blocked:")
        for task in summary["blocked"]:
            lines.append(f"  - {task.get('id')}: {task.get('title')}")
    return "\n".join(lines)


def render_lifecycle(summary: dict[str, Any]) -> str:
    lines = [
        "AI Task Unified Lifecycle",
        f"DB: {summary['state_db']}",
        f"Entries: {summary['entry_count']}",
        "Counts: "
        + ", ".join(f"{key}={value}" for key, value in sorted(summary["status_counts"].items()) if value),
        f"Task-record minus queue total: {summary['task_record_minus_queue_total']}",
        f"Warnings: {', '.join(summary['warnings']) if summary['warnings'] else 'none'}",
        "",
    ]
    for entry in summary["entries"]:
        queue = entry["task_queue"]
        record = entry["task_record"]
        lines.append(
            f"- {entry['task_id']}: lifecycle={entry['lifecycle_status']} "
            f"queue={queue['raw_status'] or '<missing>'} record={record['raw_status'] or '<missing>'}"
        )
        if entry["warnings"]:
            lines.append(f"  warnings={', '.join(entry['warnings'])}")
    if not summary["entries"]:
        lines.append("No lifecycle entries.")
    return "\n".join(lines)


def command_enqueue(args: argparse.Namespace, root: Path, path: Path) -> int:
    state = load_state(path)
    try:
        task = make_task(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    existing = find_task(state["tasks"], task_id=args.task_id) if args.task_id else None
    if existing is None and not args.task_id:
        existing = find_task(state["tasks"], task_tracking=args.task_tracking) or (
            find_task(state["tasks"], trace_id=args.trace_id) if args.trace_id else None
        )
    if existing:
        if existing.get("status") != args.status:
            try:
                transition_task(state, existing, args.status, "intake_status_refreshed", "task intake status refreshed")
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 1
        for key, value in task.items():
            if key not in {"id", "created_at", "history", "status"}:
                existing[key] = value
        existing["updated_at"] = now_iso()
        add_history(existing, "metadata_refreshed", existing["status"], "task metadata refreshed")
        record_state_event(state, existing, "metadata_refreshed", existing["status"], existing["status"], "task metadata refreshed")
        task = existing
    else:
        state["tasks"].append(task)
        record_state_event(state, task, "candidate_created", "", task["status"], "task candidate recorded")
    save_state(path, state)
    print(json.dumps({"task_id": task["id"], "status": task["status"], "state_db": path.as_posix()}, ensure_ascii=False))
    return 0


def command_request_approval(args: argparse.Namespace, root: Path, path: Path) -> int:
    state = load_state(path)
    task = find_task(state["tasks"], task_id=args.task_id)
    if not task:
        print("Task not found.", file=sys.stderr)
        return 1
    task["approval_label"] = args.approval_label
    try:
        transition_task(state, task, "awaiting_approval", "approval_requested", args.summary or args.approval_label)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    save_state(path, state)
    print(json.dumps({"task_id": task["id"], "status": task["status"]}, ensure_ascii=False))
    return 0


def command_approve(args: argparse.Namespace, root: Path, path: Path) -> int:
    state = load_state(path)
    task = find_task(state["tasks"], task_id=args.task_id)
    if not task:
        print("Task not found.", file=sys.stderr)
        return 1
    if not is_approval_label(args.approval_label):
        print("Approval label must be explicit and must not start with 待批准.", file=sys.stderr)
        return 1
    task["approval_label"] = args.approval_label
    try:
        transition_task(state, task, "ready", "approved", args.summary or args.approval_label)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    save_state(path, state)
    print(json.dumps({"task_id": task["id"], "status": task["status"]}, ensure_ascii=False))
    return 0


def command_start_next(args: argparse.Namespace, root: Path, path: Path) -> int:
    state = load_state(path)
    if active_tasks(state):
        print("An active task already exists; complete, wait, or block it before starting another.", file=sys.stderr)
        return 1
    task = find_task(state["tasks"], task_id=args.task_id) if args.task_id else (ready_tasks(state)[0] if ready_tasks(state) else None)
    if not task:
        print("No ready task found.", file=sys.stderr)
        return 1
    if task.get("status") != "ready":
        print(f"Task is not ready: {task.get('id')} status={task.get('status')}", file=sys.stderr)
        return 1
    if not is_approval_label(str(task.get("approval_label", ""))):
        print(f"Task lacks explicit approval: {task.get('id')}", file=sys.stderr)
        return 1
    try:
        transition_task(state, task, "active", "started", "task started after approval")
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    save_state(path, state)
    print(json.dumps({"task_id": task["id"], "status": "active"}, ensure_ascii=False))
    return 0


def command_wait(args: argparse.Namespace, root: Path, path: Path) -> int:
    state = load_state(path)
    task = find_task(state["tasks"], task_id=args.task_id)
    if not task:
        print("Task not found.", file=sys.stderr)
        return 1
    to_status = WAIT_STATUS_BY_KIND[args.kind]
    try:
        transition_task(state, task, to_status, "waiting", args.reason)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    save_state(path, state)
    print(json.dumps({"task_id": task["id"], "status": task["status"]}, ensure_ascii=False))
    return 0


def command_resume(args: argparse.Namespace, root: Path, path: Path) -> int:
    state = load_state(path)
    task = find_task(state["tasks"], task_id=args.task_id)
    if not task:
        print("Task not found.", file=sys.stderr)
        return 1
    try:
        transition_task(state, task, "active", "resumed", args.summary)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    save_state(path, state)
    print(json.dumps({"task_id": task["id"], "status": task["status"]}, ensure_ascii=False))
    return 0


def command_complete(args: argparse.Namespace, root: Path, path: Path) -> int:
    state = load_state(path)
    task = find_task(state["tasks"], task_id=args.task_id, trace_id=args.trace_id, task_tracking=args.task_tracking)
    if not task and not any([args.task_id, args.trace_id, args.task_tracking]) and len(active_tasks(state)) == 1:
        task = active_tasks(state)[0]
    if not task:
        print("Task not found.", file=sys.stderr)
        return 1
    try:
        transition_task(state, task, "completed", "completed", args.summary)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    save_state(path, state)
    print(json.dumps({"task_id": task["id"], "status": "completed"}, ensure_ascii=False))
    return 0


def command_block(args: argparse.Namespace, root: Path, path: Path) -> int:
    state = load_state(path)
    task = find_task(state["tasks"], task_id=args.task_id)
    if not task:
        print("Task not found.", file=sys.stderr)
        return 1
    task["blocked_reason"] = args.reason
    try:
        transition_task(state, task, "blocked", "blocked", args.reason)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    save_state(path, state)
    print(json.dumps({"task_id": task["id"], "status": "blocked"}, ensure_ascii=False))
    return 0


def command_cancel(args: argparse.Namespace, root: Path, path: Path) -> int:
    state = load_state(path)
    task = find_task(state["tasks"], task_id=args.task_id)
    if not task:
        print("Task not found.", file=sys.stderr)
        return 1
    task["cancel_reason"] = args.reason
    try:
        transition_task(state, task, "cancelled", "cancelled", args.reason)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    save_state(path, state)
    print(json.dumps({"task_id": task["id"], "status": "cancelled"}, ensure_ascii=False))
    return 0


def command_reject(args: argparse.Namespace, root: Path, path: Path) -> int:
    state = load_state(path)
    task = find_task(state["tasks"], task_id=args.task_id)
    if not task:
        print("Task not found.", file=sys.stderr)
        return 1
    task["reject_reason"] = args.reason
    try:
        transition_task(state, task, "rejected", "rejected", args.reason)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    save_state(path, state)
    print(json.dumps({"task_id": task["id"], "status": "rejected"}, ensure_ascii=False))
    return 0


def validate_state(state: dict[str, Any], args: argparse.Namespace) -> tuple[list[Finding], list[Finding], list[str]]:
    errors: list[Finding] = []
    warnings: list[Finding] = []
    notes: list[str] = []
    tasks = state.get("tasks", [])
    active = active_tasks(state)
    max_active = int(state.get("policy", {}).get("max_active_tasks", 1))
    if len(active) > max_active:
        errors.append(Finding("error", f"more active tasks than allowed: {len(active)} > {max_active}"))
    ids = [task.get("id", "") for task in tasks]
    if len(ids) != len(set(ids)):
        errors.append(Finding("error", "duplicate task ids found"))
    for task in tasks:
        status = task.get("status")
        if status not in ALL_STATUSES:
            errors.append(Finding("error", f"{task.get('id')} has invalid status {status}"))
        if not task.get("task_tracking"):
            errors.append(Finding("error", f"{task.get('id')} lacks task_tracking"))
        context = task.get("context") or {}
        if not context.get("message_sha256") or not context.get("message_excerpt"):
            errors.append(Finding("error", f"{task.get('id')} lacks preserved message context"))
        if status in RUNNING_STATUSES and not is_approval_label(str(task.get("approval_label", ""))):
            errors.append(Finding("error", f"{task.get('id')} is running without explicit approval"))
        if status in {"candidate", "awaiting_approval"} and task.get("started_at"):
            warnings.append(Finding("warning", f"{task.get('id')} intake task has historical started_at; verify migration history"))
        history = task.get("history") or []
        if (
            status in RUNNING_STATUSES
            and not is_approval_label(str(task.get("approval_label", "")))
            and not any(item.get("event") == "approved" for item in history)
        ):
            warnings.append(Finding("warning", f"{task.get('id')} is running without an approved history event"))
    if args.current_task_tracking or args.trace_id:
        task = find_task(tasks, trace_id=args.trace_id, task_tracking=args.current_task_tracking)
        if args.require_current and not task:
            errors.append(Finding("error", "current task tracking/trace is not present in task workflow"))
        elif task:
            notes.append(f"current task: {task.get('id')} status={task.get('status')}")
            if task.get("status") not in RUNNING_STATUSES | CLOSED_STATUSES:
                errors.append(Finding("error", f"current task is not running/completed: {task.get('status')}"))
    if args.strict_fifo and active:
        first_work = runnable_or_running_tasks(state)[0] if runnable_or_running_tasks(state) else None
        if first_work and first_work.get("id") != active[0].get("id"):
            errors.append(Finding("error", "active task is not the first ready/running task under strict FIFO"))
    return errors, warnings, notes


def command_validate(args: argparse.Namespace, root: Path, path: Path) -> int:
    state = load_state(path)
    errors, warnings, notes = validate_state(state, args)
    payload = {
        "state_db": path.as_posix(),
        "schema_version": state.get("schema_version"),
        "errors": [finding.message for finding in errors],
        "warnings": [finding.message for finding in warnings],
        "notes": notes,
        "summary": queue_summary(state),
    }
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print("AI Task Workflow Validate")
        print(f"Queue: {path.as_posix()}")
        print(f"Schema: {state.get('schema_version')}")
        print(f"Errors: {len(errors)}")
        for finding in errors:
            print(f"  - {finding.message}")
        print(f"Warnings: {len(warnings)}")
        for finding in warnings:
            print(f"  - {finding.message}")
        for note in notes:
            print(f"Note: {note}")
    return 1 if errors or (warnings and args.fail_on_warning) else 0


def command_status(args: argparse.Namespace, root: Path, path: Path) -> int:
    state = load_state(path)
    summary = queue_summary(state)
    if args.format == "json":
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(render_summary(summary))
    return 0


def command_lifecycle(args: argparse.Namespace, root: Path, path: Path) -> int:
    summary = lifecycle_summary(root, path, args.task_id)
    if args.format == "json":
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(render_lifecycle(summary))
    return 0


def command_heartbeat(args: argparse.Namespace, root: Path, path: Path) -> int:
    state = load_state(path)
    summary = queue_summary(state)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now_iso(),
        "state_db": path.as_posix(),
        "summary": summary,
        "message": "Read this heartbeat before new work; start only ready tasks with explicit approval.",
    }
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print("AI Task Workflow Heartbeat")
        print(f"Generated: {payload['generated_at']}")
        print(render_summary(summary))
    return 0


def dispatch_command(args: argparse.Namespace, root: Path, path: Path) -> int:
    if args.command == "enqueue":
        return command_enqueue(args, root, path)
    if args.command == "request-approval":
        return command_request_approval(args, root, path)
    if args.command == "approve":
        return command_approve(args, root, path)
    if args.command == "start-next":
        return command_start_next(args, root, path)
    if args.command == "wait":
        return command_wait(args, root, path)
    if args.command == "resume":
        return command_resume(args, root, path)
    if args.command == "complete":
        return command_complete(args, root, path)
    if args.command == "block":
        return command_block(args, root, path)
    if args.command == "cancel":
        return command_cancel(args, root, path)
    if args.command == "reject":
        return command_reject(args, root, path)
    if args.command == "status":
        return command_status(args, root, path)
    if args.command == "lifecycle":
        return command_lifecycle(args, root, path)
    if args.command == "heartbeat":
        return command_heartbeat(args, root, path)
    if args.command == "validate":
        return command_validate(args, root, path)
    return 2


def main() -> int:
    args = parse_args()
    root = Path(args.root).resolve()
    path = queue_db_path(root, args.db)
    if args.command in MUTATING_COMMANDS:
        try:
            with queue_lock(path):
                return dispatch_command(args, root, path)
        except TimeoutError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    return dispatch_command(args, root, path)


if __name__ == "__main__":
    raise SystemExit(main())
