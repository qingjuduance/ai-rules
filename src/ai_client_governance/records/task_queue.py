#!/usr/bin/env python3
"""Manage the project-local AI task workflow.

The task state file is deliberately closer to a small workflow engine than a
plain queue. User input first becomes a candidate or approval-waiting task.
Only an approved, ready task can be started. This prevents the common failure
mode where a new user message is treated as active work before scope and
approval have been resolved.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ai_client_governance.common.paths import STATE_DIR


QUEUE_PATH = STATE_DIR / "task-queue.json"
HEARTBEAT_PATH = STATE_DIR / "task-queue-heartbeat.json"
SCHEMA_VERSION = 2
LOCK_TIMEOUT_SECONDS = 10
LOCK_STALE_SECONDS = 300

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
    parser.add_argument("--root", default=".", help="Repository root. Default: current directory.")
    parser.add_argument("--queue-file", help="Override queue JSON path.")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    sub = parser.add_subparsers(dest="command", required=True)

    enqueue = sub.add_parser("enqueue", help="Create or refresh a candidate task.")
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
    request_approval.add_argument("--task-id", required=True)
    request_approval.add_argument("--approval-label", required=True)
    request_approval.add_argument("--summary", default="")

    approve = sub.add_parser("approve", help="Mark an approval-waiting task as ready.")
    approve.add_argument("--task-id", required=True)
    approve.add_argument("--approval-label", required=True)
    approve.add_argument("--summary", default="")

    start = sub.add_parser("start-next", help="Mark the next ready task active.")
    start.add_argument("--task-id", help="Specific ready task id. Default: first ready task.")

    wait = sub.add_parser("wait", help="Move an active task into a waiting state.")
    wait.add_argument("--task-id", required=True)
    wait.add_argument("--kind", choices=sorted(WAIT_STATUS_BY_KIND), required=True)
    wait.add_argument("--reason", required=True)

    resume = sub.add_parser("resume", help="Resume a waiting or verifying task back to active.")
    resume.add_argument("--task-id", required=True)
    resume.add_argument("--summary", default="")

    complete = sub.add_parser("complete", help="Mark a task completed.")
    complete.add_argument("--task-id")
    complete.add_argument("--trace-id")
    complete.add_argument("--task-tracking")
    complete.add_argument("--summary", default="")

    block = sub.add_parser("block", help="Mark a task blocked.")
    block.add_argument("--task-id", required=True)
    block.add_argument("--reason", required=True)

    cancel = sub.add_parser("cancel", help="Cancel an open task.")
    cancel.add_argument("--task-id", required=True)
    cancel.add_argument("--reason", required=True)

    reject = sub.add_parser("reject", help="Reject an approval-waiting task.")
    reject.add_argument("--task-id", required=True)
    reject.add_argument("--reason", required=True)

    sub.add_parser("status", help="Print queue status.")

    heartbeat = sub.add_parser("heartbeat", help="Print a periodic queue heartbeat.")
    heartbeat.add_argument("--write-snapshot", action="store_true")

    validate = sub.add_parser("validate", help="Validate queue invariants.")
    validate.add_argument("--current-task-tracking")
    validate.add_argument("--trace-id")
    validate.add_argument("--require-current", action="store_true")
    validate.add_argument("--strict-fifo", action="store_true")
    validate.add_argument("--fail-on-warning", action="store_true")

    return parser.parse_args()


def queue_path(root: Path, override: str | None) -> Path:
    if override:
        path = Path(override)
        return path if path.is_absolute() else root / path
    return root / QUEUE_PATH


def heartbeat_path(root: Path) -> Path:
    return root / HEARTBEAT_PATH


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
    if not path.exists():
        return default_state()
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("created_at", now_iso())
    data.setdefault("updated_at", now_iso())
    data.setdefault("policy", {})
    data.setdefault("events", [])
    data.setdefault("tasks", [])
    if int(data.get("schema_version", 1)) < SCHEMA_VERSION:
        migrate_legacy_state(data)
    data["schema_version"] = SCHEMA_VERSION
    data["policy"].setdefault("strict_fifo", True)
    data["policy"].setdefault("max_active_tasks", 1)
    data["policy"].setdefault("context_required", True)
    data["policy"].setdefault("approval_required_for_active", True)
    return data


def migrate_legacy_state(state: dict[str, Any]) -> None:
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
            "legacy_state_migrated",
            task.get("status", ""),
            f"schema 1 status {original!r} migrated to schema 2 workflow state",
        )
        record_state_event(
            state,
            task,
            "legacy_state_migrated",
            original,
            task.get("status", ""),
            "schema 1 queue state migrated",
        )


def save_state(path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = now_iso()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


@contextmanager
def queue_lock(path: Path):
    lock = path.with_name(f"{path.name}.lock")
    start = time.monotonic()
    handle = None
    while handle is None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = lock.open("x", encoding="utf-8")
            handle.write(f"pid={os.getpid()} acquired_at={now_iso()}\n")
            handle.flush()
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
                if age > LOCK_STALE_SECONDS:
                    lock.unlink()
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() - start > LOCK_TIMEOUT_SECONDS:
                raise TimeoutError(f"task queue lock timed out: {lock}")
            time.sleep(0.1)
    try:
        yield
    finally:
        if handle is not None:
            handle.close()
        try:
            lock.unlink()
        except FileNotFoundError:
            pass


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
        "next_task": ready[0] if ready else None,
        "updated_at": state.get("updated_at", ""),
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
    print(json.dumps({"task_id": task["id"], "status": task["status"], "queue_file": path.as_posix()}, ensure_ascii=False))
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
        "queue_file": path.as_posix(),
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


def command_heartbeat(args: argparse.Namespace, root: Path, path: Path) -> int:
    state = load_state(path)
    summary = queue_summary(state)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now_iso(),
        "queue_file": path.as_posix(),
        "summary": summary,
        "message": "Read this heartbeat before new work; start only ready tasks with explicit approval.",
    }
    if args.write_snapshot:
        output = heartbeat_path(root)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        payload["heartbeat_file"] = output.as_posix()
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print("AI Task Workflow Heartbeat")
        print(f"Generated: {payload['generated_at']}")
        print(render_summary(summary))
        if payload.get("heartbeat_file"):
            print(f"Heartbeat file: {payload['heartbeat_file']}")
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
    if args.command == "heartbeat":
        return command_heartbeat(args, root, path)
    if args.command == "validate":
        return command_validate(args, root, path)
    return 2


def main() -> int:
    args = parse_args()
    root = Path(args.root).resolve()
    path = queue_path(root, args.queue_file)
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

