#!/usr/bin/env python3
"""Structured task records backed by SQLite.

Markdown task tracking is useful as a human report, but gates need typed data.
This module stores the task contract in SQLite with explicit enums, required
fields, and foreign keys so invalid records fail before gate execution.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ai_client_governance.common import cli_arguments as common_cli_args
from ai_client_governance.common.paths import STRUCTURED_DB_PATH
from ai_client_governance.runtime.scope import COMMON_SCOPE, MIXED_SCOPE, NATIVE_SCOPE, PROJECT_SCOPE, UNKNOWN_SCOPE


SCHEMA_VERSION = 1

TASK_STATUSES = ("candidate", "awaiting_approval", "ready", "active", "verifying", "done", "blocked", "cancelled")
REQUIREMENT_STATUSES = ("open", "in_progress", "done", "blocked", "deferred", "cancelled")
OUTPUT_TYPES = ("plan", "status", "final", "script", "error", "git_worktree")
VALIDATION_RESULTS = ("pass", "fail", "warn", "skipped")
APPROVAL_STATUSES = ("requested", "approved", "rejected")
WORKTREE_REPOS = ("self", "ai-client-governance", "other")
WORKTREE_CREATION_METHODS = ("worktree-task", "break-glass", "external")
WORKTREE_STATUSES = ("active", "done", "blocked", "removed")
WORKTREE_MERGE_STATUSES = ("not_merged", "merged", "not_required")
WORKTREE_COMMIT_STATUSES = ("not_committed", "committed", "not_required")
WORKTREE_PUSH_STATUSES = ("not_pushed", "pushed", "not_required")
GATE_RESULTS = ("pass", "fail", "warn", "skipped")

BASE_OUTPUT_TYPES = {"plan", "status", "final", "script", "error", "git_worktree"}
MUTATING_TASK_TYPES = {"correction", "rules-script", "docs", "git", "frontend", "resume", "multi-agent", "long-running"}
KNOWN_TASK_TYPES = MUTATING_TASK_TYPES | {"code-debug"}
INPUT_FILTER_PREFLIGHT_EVENT = "input-filter.preflight"
INPUT_FILTER_TRIGGER_TYPES = {"input-filter", "user-message"}
INPUT_FILTER_REQUIREMENT_FIELDS = (
    "summary",
    "record_decision",
    "network_decision",
    "validation_decision",
    "acceptance",
)
COMMAND_COMPRESSION_EVENT = "command-compression.analysis"
SCOPE_CLASSIFICATION_TRIGGER = "scope-classification"
PLAN_APPROVAL_BOUNDARY_EVENT = "plan-approval-boundary.analysis"
USER_CLAIM_VALIDATION_EVENT = "user-claim-validation.analysis"
STATE_ARTIFACT_OWNERSHIP_EVENT = "state-artifact-ownership.analysis"
PATCH_PREFLIGHT_EVENT = "patch-preflight.analysis"
SCOPE_KINDS = {COMMON_SCOPE, PROJECT_SCOPE, NATIVE_SCOPE, MIXED_SCOPE, UNKNOWN_SCOPE}


@dataclass
class Finding:
    level: str
    message: str
    table: str | None = None
    row_id: str | None = None


@dataclass
class GateReport:
    db: str
    task_id: str
    errors: list[Finding]
    warnings: list[Finding]
    notes: list[Finding]


def utc_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage structured AI Client Governance task records.")
    # Register common globals at both levels so either option order survives.
    common_cli_args.add_common_global_args(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Create or migrate the SQLite database.")
    common_cli_args.add_common_global_args(init, suppress_default=True)

    apply = sub.add_parser("apply", help="Validate and apply one structured task JSON document.")
    common_cli_args.add_common_global_args(apply, suppress_default=True)
    apply.add_argument("--json", dest="json_path", required=True, help="Input JSON file.")
    apply.add_argument("--replace", action="store_true", help="Replace an existing task with the same id.")

    gate = sub.add_parser("gate", help="Validate a task from the structured database.")
    common_cli_args.add_common_global_args(gate, suppress_default=True)
    gate.add_argument("--task-id", required=True)
    gate.add_argument("--event", choices=("preflight", "final"), default="final")
    gate.add_argument("--task-type", action="append", default=[], help="Required task type override/addition.")
    gate.add_argument("--fail-on-warning", action="store_true")

    export = sub.add_parser("export-md", help="Render a human-readable Markdown report from the database.")
    common_cli_args.add_common_global_args(export, suppress_default=True)
    export.add_argument("--task-id", required=True)
    export.add_argument("--output", help="Output Markdown path. Defaults to stdout.")

    status = sub.add_parser("status", help="Print database summary.")
    common_cli_args.add_common_global_args(status, suppress_default=True)
    status.add_argument("--task-id", help="Optional task id.")
    return parser.parse_args()


def db_path(root: Path, override: str | None) -> Path:
    if override:
        path = Path(override)
        return path if path.is_absolute() else root / path
    return root / STRUCTURED_DB_PATH


def connect(path: Path, *, create: bool = True) -> sqlite3.Connection:
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    elif not path.exists():
        raise ValueError(f"structured DB does not exist: {path}")
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    init_db(con)
    return con


def empty_summary(path: Path, task_id: str | None = None) -> dict[str, Any]:
    if task_id:
        return {"db": str(path), "task_id": task_id, "exists": False}
    return {
        "db": str(path),
        "exists": False,
        "schema_version": SCHEMA_VERSION,
        "task_count": 0,
        "tasks": [],
    }


def quote_enum(values: tuple[str, ...]) -> str:
    return ", ".join(repr(value) for value in values)


def init_db(con: sqlite3.Connection) -> None:
    con.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            title TEXT NOT NULL CHECK (length(trim(title)) > 0),
            status TEXT NOT NULL CHECK (status IN ({quote_enum(TASK_STATUSES)})),
            task_size TEXT NOT NULL DEFAULT 'medium',
            task_types_json TEXT NOT NULL DEFAULT '[]',
            summary TEXT NOT NULL DEFAULT '',
            approval_label TEXT NOT NULL DEFAULT '',
            trace_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS approvals (
            approval_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
            label TEXT NOT NULL CHECK (length(trim(label)) > 0),
            status TEXT NOT NULL CHECK (status IN ({quote_enum(APPROVAL_STATUSES)})),
            summary TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS requirements (
            requirement_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
            summary TEXT NOT NULL CHECK (length(trim(summary)) > 0),
            record_decision TEXT NOT NULL CHECK (length(trim(record_decision)) > 0),
            network_decision TEXT NOT NULL CHECK (length(trim(network_decision)) > 0),
            validation_decision TEXT NOT NULL CHECK (length(trim(validation_decision)) > 0),
            acceptance TEXT NOT NULL CHECK (length(trim(acceptance)) > 0),
            status TEXT NOT NULL CHECK (status IN ({quote_enum(REQUIREMENT_STATUSES)})),
            action TEXT NOT NULL CHECK (length(trim(action)) > 0),
            implementation_evidence TEXT NOT NULL CHECK (length(trim(implementation_evidence)) > 0),
            validation_evidence TEXT NOT NULL CHECK (length(trim(validation_evidence)) > 0),
            final_coverage TEXT NOT NULL CHECK (length(trim(final_coverage)) > 0)
        );

        CREATE TABLE IF NOT EXISTS triggers (
            trigger_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
            trigger_type TEXT NOT NULL CHECK (length(trim(trigger_type)) > 0),
            source TEXT NOT NULL CHECK (length(trim(source)) > 0),
            matched_requirement TEXT NOT NULL CHECK (length(trim(matched_requirement)) > 0),
            priority TEXT NOT NULL CHECK (length(trim(priority)) > 0),
            applicability_scope TEXT NOT NULL CHECK (length(trim(applicability_scope)) > 0),
            scope_expansion TEXT NOT NULL CHECK (length(trim(scope_expansion)) > 0),
            reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
            required_action TEXT NOT NULL CHECK (length(trim(required_action)) > 0),
            executed_steps TEXT NOT NULL CHECK (length(trim(executed_steps)) > 0),
            quantitative_evidence TEXT NOT NULL CHECK (length(trim(quantitative_evidence)) > 0),
            status TEXT NOT NULL CHECK (length(trim(status)) > 0),
            trace_id TEXT NOT NULL CHECK (length(trim(trace_id)) > 0)
        );

        CREATE TABLE IF NOT EXISTS outputs (
            output_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
            output_type TEXT NOT NULL CHECK (output_type IN ({quote_enum(OUTPUT_TYPES)})),
            applicability_scope TEXT NOT NULL CHECK (length(trim(applicability_scope)) > 0),
            exclusions TEXT NOT NULL CHECK (length(trim(exclusions)) > 0),
            objects TEXT NOT NULL CHECK (length(trim(objects)) > 0),
            fact_source TEXT NOT NULL CHECK (length(trim(fact_source)) > 0),
            completed TEXT NOT NULL CHECK (length(trim(completed)) > 0),
            unfinished TEXT NOT NULL CHECK (length(trim(unfinished)) > 0),
            unverified TEXT NOT NULL CHECK (length(trim(unverified)) > 0),
            blocked TEXT NOT NULL CHECK (length(trim(blocked)) > 0),
            user_confirmation TEXT NOT NULL CHECK (length(trim(user_confirmation)) > 0),
            final_coverage TEXT NOT NULL CHECK (length(trim(final_coverage)) > 0),
            trace_id TEXT NOT NULL CHECK (length(trim(trace_id)) > 0)
        );

        CREATE TABLE IF NOT EXISTS worktrees (
            worktree_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
            repo TEXT NOT NULL CHECK (repo IN ({quote_enum(WORKTREE_REPOS)})),
            source_repo TEXT NOT NULL CHECK (length(trim(source_repo)) > 0),
            path TEXT NOT NULL CHECK (length(trim(path)) > 0),
            branch TEXT NOT NULL CHECK (length(trim(branch)) > 0),
            base_commit TEXT NOT NULL CHECK (length(trim(base_commit)) > 0),
            creation_method TEXT NOT NULL CHECK (creation_method IN ({quote_enum(WORKTREE_CREATION_METHODS)})),
            sparse_policy TEXT NOT NULL CHECK (length(trim(sparse_policy)) > 0),
            source_handling TEXT NOT NULL CHECK (length(trim(source_handling)) > 0),
            status TEXT NOT NULL CHECK (status IN ({quote_enum(WORKTREE_STATUSES)})),
            merged_status TEXT NOT NULL CHECK (merged_status IN ({quote_enum(WORKTREE_MERGE_STATUSES)})),
            commit_status TEXT NOT NULL CHECK (commit_status IN ({quote_enum(WORKTREE_COMMIT_STATUSES)})),
            push_status TEXT NOT NULL CHECK (push_status IN ({quote_enum(WORKTREE_PUSH_STATUSES)})),
            next_action TEXT NOT NULL CHECK (length(trim(next_action)) > 0)
        );

        CREATE TABLE IF NOT EXISTS validations (
            validation_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
            command TEXT NOT NULL CHECK (length(trim(command)) > 0),
            cwd TEXT NOT NULL CHECK (length(trim(cwd)) > 0),
            result TEXT NOT NULL CHECK (result IN ({quote_enum(VALIDATION_RESULTS)})),
            summary TEXT NOT NULL CHECK (length(trim(summary)) > 0),
            evidence TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS gate_runs (
            gate_run_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
            gate_name TEXT NOT NULL CHECK (length(trim(gate_name)) > 0),
            result TEXT NOT NULL CHECK (result IN ({quote_enum(GATE_RESULTS)})),
            errors_json TEXT NOT NULL DEFAULT '[]',
            warnings_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS events (
            event_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
            event_type TEXT NOT NULL CHECK (length(trim(event_type)) > 0),
            payload_json TEXT NOT NULL DEFAULT '{{}}',
            created_at TEXT NOT NULL
        );
        """
    )
    con.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
    con.commit()


def require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def require_list(value: Any, label: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    return value


def clean_text(value: Any, label: str, *, required: bool = True) -> str:
    text = "" if value is None else str(value).strip()
    if required and not text:
        raise ValueError(f"{label} is required")
    return text


def enum_text(value: Any, label: str, allowed: tuple[str, ...]) -> str:
    text = clean_text(value, label)
    if text not in allowed:
        raise ValueError(f"{label} must be one of {', '.join(allowed)}; got {text!r}")
    return text


def task_types_from(value: Any) -> list[str]:
    values = require_list(value, "task.task_types")
    if not values:
        raise ValueError("task.task_types must contain at least one item")
    result: list[str] = []
    for item in values:
        text = clean_text(item, "task.task_types[]")
        if text not in KNOWN_TASK_TYPES:
            raise ValueError(f"unknown task type: {text}")
        if text not in result:
            result.append(text)
    return result


def task_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    task = require_mapping(payload.get("task"), "task")
    now = utc_now()
    task_id = clean_text(task.get("task_id") or task.get("id"), "task.task_id")
    task_types = task_types_from(task.get("task_types", []))
    return {
        "task_id": task_id,
        "title": clean_text(task.get("title"), "task.title"),
        "status": enum_text(task.get("status", "active"), "task.status", TASK_STATUSES),
        "task_size": clean_text(task.get("task_size", "medium"), "task.task_size"),
        "task_types_json": json.dumps(task_types, ensure_ascii=False),
        "summary": clean_text(task.get("summary", ""), "task.summary", required=False),
        "approval_label": clean_text(task.get("approval_label", ""), "task.approval_label", required=False),
        "trace_id": clean_text(task.get("trace_id", ""), "task.trace_id", required=False),
        "created_at": clean_text(task.get("created_at", now), "task.created_at"),
        "updated_at": now,
    }


def rows_from_payload(task_id: str, payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    now = utc_now()
    rows: dict[str, list[dict[str, Any]]] = {
        "approvals": [],
        "requirements": [],
        "triggers": [],
        "outputs": [],
        "worktrees": [],
        "validations": [],
        "events": [],
    }

    for index, item in enumerate(require_list(payload.get("approvals"), "approvals"), start=1):
        approval = require_mapping(item, f"approvals[{index}]")
        rows["approvals"].append(
            {
                "approval_id": clean_text(approval.get("approval_id") or f"APR-{task_id}-{index:02d}", "approval_id"),
                "task_id": task_id,
                "label": clean_text(approval.get("label"), "approvals[].label"),
                "status": enum_text(approval.get("status", "approved"), "approvals[].status", APPROVAL_STATUSES),
                "summary": clean_text(approval.get("summary", ""), "approvals[].summary", required=False),
                "created_at": clean_text(approval.get("created_at", now), "approvals[].created_at"),
            }
        )

    for index, item in enumerate(require_list(payload.get("requirements"), "requirements"), start=1):
        req = require_mapping(item, f"requirements[{index}]")
        rows["requirements"].append(
            {
                "requirement_id": clean_text(req.get("requirement_id") or req.get("id"), "requirements[].requirement_id"),
                "task_id": task_id,
                "summary": clean_text(req.get("summary"), "requirements[].summary"),
                "record_decision": clean_text(req.get("record_decision"), "requirements[].record_decision"),
                "network_decision": clean_text(req.get("network_decision"), "requirements[].network_decision"),
                "validation_decision": clean_text(req.get("validation_decision"), "requirements[].validation_decision"),
                "acceptance": clean_text(req.get("acceptance"), "requirements[].acceptance"),
                "status": enum_text(req.get("status", "open"), "requirements[].status", REQUIREMENT_STATUSES),
                "action": clean_text(req.get("action"), "requirements[].action"),
                "implementation_evidence": clean_text(req.get("implementation_evidence"), "requirements[].implementation_evidence"),
                "validation_evidence": clean_text(req.get("validation_evidence"), "requirements[].validation_evidence"),
                "final_coverage": clean_text(req.get("final_coverage"), "requirements[].final_coverage"),
            }
        )

    for index, item in enumerate(require_list(payload.get("triggers"), "triggers"), start=1):
        trigger = require_mapping(item, f"triggers[{index}]")
        rows["triggers"].append(
            {
                "trigger_id": clean_text(trigger.get("trigger_id") or trigger.get("id"), "triggers[].trigger_id"),
                "task_id": task_id,
                "trigger_type": clean_text(trigger.get("trigger_type") or trigger.get("type"), "triggers[].trigger_type"),
                "source": clean_text(trigger.get("source"), "triggers[].source"),
                "matched_requirement": clean_text(trigger.get("matched_requirement"), "triggers[].matched_requirement"),
                "priority": clean_text(trigger.get("priority"), "triggers[].priority"),
                "applicability_scope": clean_text(trigger.get("applicability_scope"), "triggers[].applicability_scope"),
                "scope_expansion": clean_text(trigger.get("scope_expansion"), "triggers[].scope_expansion"),
                "reason": clean_text(trigger.get("reason"), "triggers[].reason"),
                "required_action": clean_text(trigger.get("required_action"), "triggers[].required_action"),
                "executed_steps": clean_text(trigger.get("executed_steps"), "triggers[].executed_steps"),
                "quantitative_evidence": clean_text(trigger.get("quantitative_evidence"), "triggers[].quantitative_evidence"),
                "status": clean_text(trigger.get("status"), "triggers[].status"),
                "trace_id": clean_text(trigger.get("trace_id"), "triggers[].trace_id"),
            }
        )

    for index, item in enumerate(require_list(payload.get("outputs"), "outputs"), start=1):
        output = require_mapping(item, f"outputs[{index}]")
        rows["outputs"].append(
            {
                "output_id": clean_text(output.get("output_id") or output.get("id"), "outputs[].output_id"),
                "task_id": task_id,
                "output_type": enum_text(output.get("output_type") or output.get("type"), "outputs[].output_type", OUTPUT_TYPES),
                "applicability_scope": clean_text(output.get("applicability_scope"), "outputs[].applicability_scope"),
                "exclusions": clean_text(output.get("exclusions"), "outputs[].exclusions"),
                "objects": clean_text(output.get("objects"), "outputs[].objects"),
                "fact_source": clean_text(output.get("fact_source"), "outputs[].fact_source"),
                "completed": clean_text(output.get("completed"), "outputs[].completed"),
                "unfinished": clean_text(output.get("unfinished"), "outputs[].unfinished"),
                "unverified": clean_text(output.get("unverified"), "outputs[].unverified"),
                "blocked": clean_text(output.get("blocked"), "outputs[].blocked"),
                "user_confirmation": clean_text(output.get("user_confirmation"), "outputs[].user_confirmation"),
                "final_coverage": clean_text(output.get("final_coverage"), "outputs[].final_coverage"),
                "trace_id": clean_text(output.get("trace_id"), "outputs[].trace_id"),
            }
        )

    for index, item in enumerate(require_list(payload.get("worktrees"), "worktrees"), start=1):
        wt = require_mapping(item, f"worktrees[{index}]")
        rows["worktrees"].append(
            {
                "worktree_id": clean_text(wt.get("worktree_id") or f"WT-{task_id}-{index:02d}", "worktrees[].worktree_id"),
                "task_id": task_id,
                "repo": enum_text(wt.get("repo"), "worktrees[].repo", WORKTREE_REPOS),
                "source_repo": clean_text(wt.get("source_repo"), "worktrees[].source_repo"),
                "path": clean_text(wt.get("path"), "worktrees[].path"),
                "branch": clean_text(wt.get("branch"), "worktrees[].branch"),
                "base_commit": clean_text(wt.get("base_commit"), "worktrees[].base_commit"),
                "creation_method": enum_text(wt.get("creation_method"), "worktrees[].creation_method", WORKTREE_CREATION_METHODS),
                "sparse_policy": clean_text(wt.get("sparse_policy"), "worktrees[].sparse_policy"),
                "source_handling": clean_text(wt.get("source_handling"), "worktrees[].source_handling"),
                "status": enum_text(wt.get("status"), "worktrees[].status", WORKTREE_STATUSES),
                "merged_status": enum_text(wt.get("merged_status"), "worktrees[].merged_status", WORKTREE_MERGE_STATUSES),
                "commit_status": enum_text(wt.get("commit_status"), "worktrees[].commit_status", WORKTREE_COMMIT_STATUSES),
                "push_status": enum_text(wt.get("push_status"), "worktrees[].push_status", WORKTREE_PUSH_STATUSES),
                "next_action": clean_text(wt.get("next_action"), "worktrees[].next_action"),
            }
        )

    for index, item in enumerate(require_list(payload.get("validations"), "validations"), start=1):
        validation = require_mapping(item, f"validations[{index}]")
        rows["validations"].append(
            {
                "validation_id": clean_text(validation.get("validation_id") or f"VAL-{task_id}-{index:02d}", "validations[].validation_id"),
                "task_id": task_id,
                "command": clean_text(validation.get("command"), "validations[].command"),
                "cwd": clean_text(validation.get("cwd"), "validations[].cwd"),
                "result": enum_text(validation.get("result"), "validations[].result", VALIDATION_RESULTS),
                "summary": clean_text(validation.get("summary"), "validations[].summary"),
                "evidence": clean_text(validation.get("evidence", ""), "validations[].evidence", required=False),
                "created_at": clean_text(validation.get("created_at", now), "validations[].created_at"),
            }
        )

    for index, item in enumerate(require_list(payload.get("events"), "events"), start=1):
        event = require_mapping(item, f"events[{index}]")
        payload_value = event.get("payload", {})
        rows["events"].append(
            {
                "event_id": clean_text(event.get("event_id") or f"EVT-{task_id}-{index:02d}", "events[].event_id"),
                "task_id": task_id,
                "event_type": clean_text(event.get("event_type") or event.get("type"), "events[].event_type"),
                "payload_json": json.dumps(payload_value, ensure_ascii=False, sort_keys=True),
                "created_at": clean_text(event.get("created_at", now), "events[].created_at"),
            }
        )
    return rows


def load_payload(path: Path) -> dict[str, Any]:
    return require_mapping(json.loads(path.read_text(encoding="utf-8-sig")), "payload")


def validate_payload_shape(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    task = task_from_payload(payload)
    rows = rows_from_payload(task["task_id"], payload)
    if not rows["requirements"]:
        raise ValueError("requirements must contain at least one row")
    if not rows["triggers"]:
        raise ValueError("triggers must contain at least one row")
    if not rows["outputs"]:
        raise ValueError("outputs must contain at least one row")
    task_types = set(json.loads(task["task_types_json"]))
    if "rules-script" in task_types:
        if not task["approval_label"]:
            raise ValueError("rules-script tasks require task.approval_label")
        if not rows["approvals"]:
            raise ValueError("rules-script tasks require at least one approval row")
        if not any(row["status"] == "approved" and row["label"] == task["approval_label"] for row in rows["approvals"]):
            raise ValueError("rules-script tasks require an approved approvals[] row matching task.approval_label")
    return task, rows


def insert_rows(con: sqlite3.Connection, table: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    columns = list(rows[0])
    placeholders = ", ".join("?" for _ in columns)
    names = ", ".join(columns)
    sql = f"INSERT INTO {table} ({names}) VALUES ({placeholders})"
    con.executemany(sql, [[row[column] for column in columns] for row in rows])


def apply_payload(con: sqlite3.Connection, payload: dict[str, Any], replace: bool) -> str:
    task, rows = validate_payload_shape(payload)
    task_id = task["task_id"]
    with con:
        if replace:
            con.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
        elif con.execute("SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)).fetchone():
            raise ValueError(f"task already exists: {task_id}; use --replace")

        insert_rows(con, "tasks", [task])
        for table in ("approvals", "requirements", "triggers", "outputs", "worktrees", "validations", "events"):
            insert_rows(con, table, rows[table])
    return task_id


def row_count(con: sqlite3.Connection, table: str, task_id: str) -> int:
    return int(con.execute(f"SELECT count(*) FROM {table} WHERE task_id = ?", (task_id,)).fetchone()[0])


def rows(con: sqlite3.Connection, table: str, task_id: str) -> list[sqlite3.Row]:
    return list(con.execute(f"SELECT * FROM {table} WHERE task_id = ? ORDER BY 1", (task_id,)))


def task_row(con: sqlite3.Connection, task_id: str) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()


def add(items: list[Finding], level: str, message: str, table: str | None = None, row_id: str | None = None) -> None:
    items.append(Finding(level=level, message=message, table=table, row_id=row_id))


def task_types_for(task: sqlite3.Row, explicit: list[str]) -> set[str]:
    found = set(json.loads(task["task_types_json"] or "[]"))
    for item in explicit:
        text = item.strip()
        if text:
            found.add(text)
    return found


def has_text(row: sqlite3.Row, column: str) -> bool:
    return bool(str(row[column] or "").strip())


def validate_input_filter_preflight(con: sqlite3.Connection, task_id: str, errors: list[Finding], notes: list[Finding]) -> None:
    """Require user-message input analysis facts before execution or final output."""
    requirements = rows(con, "requirements", task_id)
    triggers = rows(con, "triggers", task_id)
    events = rows(con, "events", task_id)

    for requirement in requirements:
        missing = [field for field in INPUT_FILTER_REQUIREMENT_FIELDS if not has_text(requirement, field)]
        if missing:
            add(
                errors,
                "error",
                f"requirement lacks input-filter decision fields: {', '.join(missing)}",
                "requirements",
                requirement["requirement_id"],
            )

    input_triggers = [
        trigger
        for trigger in triggers
        if str(trigger["trigger_type"] or "").strip() in INPUT_FILTER_TRIGGER_TYPES
    ]
    if not input_triggers:
        add(
            errors,
            "error",
            "input-filter preflight requires a trigger row with trigger_type=user-message or input-filter",
            "triggers",
        )

    if not any(str(event["event_type"] or "").strip() == INPUT_FILTER_PREFLIGHT_EVENT for event in events):
        add(
            errors,
            "error",
            f"input-filter preflight requires an events row with event_type={INPUT_FILTER_PREFLIGHT_EVENT}",
            "events",
        )
    else:
        add(notes, "note", f"input-filter preflight facts present: {INPUT_FILTER_PREFLIGHT_EVENT}", "events")


def validate_command_compression_preflight(
    con: sqlite3.Connection,
    task: sqlite3.Row,
    task_types: set[str],
    errors: list[Finding],
    notes: list[Finding],
) -> None:
    """Require command compression analysis before command-heavy or mutating work."""
    task_size = str(task["task_size"] or "").strip().lower()
    if not (task_types & MUTATING_TASK_TYPES or task_size in {"medium", "large"}):
        return

    matching = [
        event
        for event in rows(con, "events", task["task_id"])
        if str(event["event_type"] or "").strip() == COMMAND_COMPRESSION_EVENT
    ]
    if not matching:
        add(
            errors,
            "error",
            f"command compression preflight requires an events row with event_type={COMMAND_COMPRESSION_EVENT}",
            "events",
        )
        return

    usable = False
    invalid_event_ids: list[str] = []
    for event in matching:
        try:
            payload = json.loads(event["payload_json"] or "{}")
        except json.JSONDecodeError:
            invalid_event_ids.append(event["event_id"])
            continue
        groups = payload.get("groups") if isinstance(payload, dict) else None
        has_groups = isinstance(groups, list) and bool(groups)
        if isinstance(payload, dict) and (payload.get("decision") or payload.get("selected_pattern")) and has_groups:
            usable = True
            break
        invalid_event_ids.append(event["event_id"])
    if not usable:
        add(
            errors,
            "error",
            "command compression analysis must record decision or selected_pattern plus non-empty groups payload",
            "events",
            ", ".join(invalid_event_ids),
        )
    else:
        add(notes, "note", f"command compression analysis facts present: {COMMAND_COMPRESSION_EVENT}", "events")


def validate_scope_classification_preflight(
    con: sqlite3.Connection,
    task: sqlite3.Row,
    task_types: set[str],
    errors: list[Finding],
    notes: list[Finding],
) -> None:
    """Require common/project/native scope facts before gated work."""
    task_size = str(task["task_size"] or "").strip().lower()
    if not (task_types & MUTATING_TASK_TYPES or task_size in {"medium", "large"}):
        return

    scope_triggers = [
        trigger
        for trigger in rows(con, "triggers", task["task_id"])
        if str(trigger["trigger_type"] or "").strip() == SCOPE_CLASSIFICATION_TRIGGER
    ]
    if not scope_triggers:
        add(
            errors,
            "error",
            "scope classification requires a trigger row with trigger_type=scope-classification",
            "triggers",
        )

    usable_payload = False
    invalid_event_ids: list[str] = []
    for event in rows(con, "events", task["task_id"]):
        try:
            payload = json.loads(event["payload_json"] or "{}")
        except json.JSONDecodeError:
            invalid_event_ids.append(event["event_id"])
            continue
        if not isinstance(payload, dict):
            continue
        scope_kind = str(payload.get("scope_kind") or "").strip()
        if not scope_kind:
            continue
        if scope_kind not in SCOPE_KINDS:
            invalid_event_ids.append(event["event_id"])
            continue
        usable_payload = True
        break
    if not usable_payload:
        add(
            errors,
            "error",
            "scope classification requires an event payload with a valid scope_kind",
            "events",
            ", ".join(invalid_event_ids),
        )
    else:
        add(notes, "note", "scope classification facts present", "events")


def event_payloads(con: sqlite3.Connection, task_id: str, event_type: str) -> list[tuple[str, dict[str, Any]]]:
    payloads: list[tuple[str, dict[str, Any]]] = []
    for event in rows(con, "events", task_id):
        if str(event["event_type"] or "").strip() != event_type:
            continue
        try:
            payload = json.loads(event["payload_json"] or "{}")
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            payloads.append((event["event_id"], payload))
    return payloads


def validate_plan_approval_boundary(
    con: sqlite3.Connection,
    task: sqlite3.Row,
    task_types: set[str],
    errors: list[Finding],
    notes: list[Finding],
) -> None:
    """Require an explicit plan/approval/push boundary before gated execution."""
    task_size = str(task["task_size"] or "").strip().lower()
    if not (task_types & MUTATING_TASK_TYPES or task_size in {"medium", "large"}):
        return

    payloads = event_payloads(con, task["task_id"], PLAN_APPROVAL_BOUNDARY_EVENT)
    if not payloads:
        add(
            errors,
            "error",
            f"plan approval boundary requires an events row with event_type={PLAN_APPROVAL_BOUNDARY_EVENT}",
            "events",
        )
        return

    valid = False
    for _event_id, payload in payloads:
        if not payload.get("execution_policy"):
            continue
        if payload.get("push_policy") != "push_requires_separate_approval":
            continue
        if task_types & MUTATING_TASK_TYPES and payload.get("approval_status") not in {"approved", "not_required"}:
            continue
        valid = True
        break
    if not valid:
        add(
            errors,
            "error",
            "plan approval boundary must record execution_policy, approval_status, and push_policy=push_requires_separate_approval",
            "events",
        )
    else:
        add(notes, "note", f"plan approval boundary facts present: {PLAN_APPROVAL_BOUNDARY_EVENT}", "events")


def validate_user_claim_validation(
    con: sqlite3.Connection,
    task: sqlite3.Row,
    task_types: set[str],
    errors: list[Finding],
    notes: list[Finding],
) -> None:
    """Require user assertions to be classified before they steer execution."""
    task_size = str(task["task_size"] or "").strip().lower()
    if not (task_types & MUTATING_TASK_TYPES or task_size in {"medium", "large"}):
        return

    payloads = event_payloads(con, task["task_id"], USER_CLAIM_VALIDATION_EVENT)
    if not payloads:
        add(
            errors,
            "error",
            f"user claim validation requires an events row with event_type={USER_CLAIM_VALIDATION_EVENT}",
            "events",
        )
        return

    valid = False
    for _event_id, payload in payloads:
        claims = payload.get("claims")
        if not isinstance(claims, list):
            continue
        if payload.get("execution_policy") not in {"verify-first", "execute-with-recorded-claims", "ask-clarification", "block"}:
            continue
        missing_claim_fields = False
        for claim in claims:
            if not isinstance(claim, dict):
                missing_claim_fields = True
                break
            required = ("claim_id", "claim_summary", "source", "trust_level", "risk_flags", "verification_action")
            if any(not claim.get(field) for field in required):
                missing_claim_fields = True
                break
        if not missing_claim_fields:
            valid = True
            break
    if not valid:
        add(
            errors,
            "error",
            "user claim validation must record claims with trust_level, risk_flags, verification_action, and execution_policy",
            "events",
        )
    else:
        add(notes, "note", f"user claim validation facts present: {USER_CLAIM_VALIDATION_EVENT}", "events")


def validate_state_artifact_ownership(
    con: sqlite3.Connection,
    task: sqlite3.Row,
    task_types: set[str],
    errors: list[Finding],
    notes: list[Finding],
) -> None:
    """Require script-generated state to declare its owner and cleanup path."""
    if "rules-script" not in task_types:
        return
    payloads = event_payloads(con, task["task_id"], STATE_ARTIFACT_OWNERSHIP_EVENT)
    if not payloads:
        add(
            errors,
            "error",
            f"script-generated state ownership requires event_type={STATE_ARTIFACT_OWNERSHIP_EVENT}",
            "events",
        )
        return
    valid = any(
        isinstance(payload.get("generated_state_classes"), list)
        and payload.get("manual_edit_policy") == "forbidden_without_break_glass"
        and payload.get("owner_policy")
        for _event_id, payload in payloads
    )
    if not valid:
        add(
            errors,
            "error",
            "script-generated state ownership must record owner_policy, generated_state_classes, and manual_edit_policy=forbidden_without_break_glass",
            "events",
        )
    else:
        add(notes, "note", f"script-generated state ownership facts present: {STATE_ARTIFACT_OWNERSHIP_EVENT}", "events")


def validate_patch_preflight(
    con: sqlite3.Connection,
    task: sqlite3.Row,
    task_types: set[str],
    errors: list[Finding],
    notes: list[Finding],
) -> None:
    """Require a patch strategy before editing long or governance files."""
    if not (task_types & {"rules-script", "docs", "correction"}):
        return
    payloads = event_payloads(con, task["task_id"], PATCH_PREFLIGHT_EVENT)
    if not payloads:
        add(errors, "error", f"patch preflight requires event_type={PATCH_PREFLIGHT_EVENT}", "events")
        return
    valid = any(
        payload.get("anchor_policy") == "verify_unique_or_reextract"
        and payload.get("apply_policy") == "small_step_patch"
        for _event_id, payload in payloads
    )
    if not valid:
        add(
            errors,
            "error",
            "patch preflight must record anchor_policy=verify_unique_or_reextract and apply_policy=small_step_patch",
            "events",
        )
    else:
        add(notes, "note", f"patch preflight facts present: {PATCH_PREFLIGHT_EVENT}", "events")


def validate_task(con: sqlite3.Connection, db: Path, task_id: str, event: str, explicit_task_types: list[str]) -> GateReport:
    errors: list[Finding] = []
    warnings: list[Finding] = []
    notes: list[Finding] = []
    task = task_row(con, task_id)
    if task is None:
        add(errors, "error", "task does not exist", "tasks", task_id)
        return GateReport(str(db), task_id, errors, warnings, notes)

    task_types = task_types_for(task, explicit_task_types)
    unknown = sorted(task_types - KNOWN_TASK_TYPES)
    if unknown:
        add(errors, "error", f"unknown task types: {', '.join(unknown)}", "tasks", task_id)

    requirement_count = row_count(con, "requirements", task_id)
    trigger_count = row_count(con, "triggers", task_id)
    output_count = row_count(con, "outputs", task_id)
    if requirement_count == 0:
        add(errors, "error", "at least one requirement row is required", "requirements")
    if trigger_count == 0:
        add(errors, "error", "at least one trigger row is required", "triggers")
    if output_count == 0:
        add(errors, "error", "at least one output row is required", "outputs")

    validate_input_filter_preflight(con, task_id, errors, notes)
    validate_command_compression_preflight(con, task, task_types, errors, notes)
    validate_scope_classification_preflight(con, task, task_types, errors, notes)
    validate_plan_approval_boundary(con, task, task_types, errors, notes)
    validate_user_claim_validation(con, task, task_types, errors, notes)
    validate_state_artifact_ownership(con, task, task_types, errors, notes)
    validate_patch_preflight(con, task, task_types, errors, notes)

    if event == "final":
        output_types = {row["output_type"] for row in rows(con, "outputs", task_id)}
        missing_outputs = sorted(BASE_OUTPUT_TYPES - output_types)
        if missing_outputs:
            add(errors, "error", f"missing final output types: {', '.join(missing_outputs)}", "outputs")

        unfinished = [
            row["requirement_id"]
            for row in rows(con, "requirements", task_id)
            if row["status"] not in {"done", "blocked", "deferred", "cancelled"}
        ]
        if unfinished:
            add(errors, "error", f"requirements are not closed: {', '.join(unfinished)}", "requirements")

    if task_types & MUTATING_TASK_TYPES:
        worktrees = rows(con, "worktrees", task_id)
        if not worktrees and event == "final":
            add(errors, "error", "mutating tasks require worktree evidence", "worktrees")
        elif not worktrees:
            add(
                notes,
                "note",
                "mutating task has no worktree evidence yet; prewrite/final gates must require it before repository writes",
                "worktrees",
            )
        for worktree in worktrees:
            if worktree["creation_method"] != "worktree-task":
                if "break" not in worktree["source_handling"].lower() and "批准" not in worktree["source_handling"]:
                    add(
                        errors,
                        "error",
                        "non worktree-task creation requires break-glass source handling evidence",
                        "worktrees",
                        worktree["worktree_id"],
                    )
    validations = rows(con, "validations", task_id)
    if event == "final" and not validations:
        add(errors, "error", "final gates require at least one validation row", "validations")
    if validations and not any(row["result"] == "pass" for row in validations):
        add(warnings, "warning", "no passing validation row recorded", "validations")

    if "rules-script" in task_types:
        if not task["approval_label"]:
            add(errors, "error", "rules-script tasks require approval_label", "tasks", task_id)
        approvals = rows(con, "approvals", task_id)
        if not approvals:
            add(errors, "error", "rules-script tasks require approval evidence", "approvals")
        elif not any(row["status"] == "approved" and row["label"] == task["approval_label"] for row in approvals):
            add(errors, "error", "rules-script tasks require an approved approval row matching approval_label", "approvals")
    if "docs" in task_types and not any(
        "validate-doc" in row["command"] or "doc-index" in row["command"] for row in validations
    ):
        add(warnings, "warning", "docs task has no validate-doc/doc-index validation row", "validations")
    if "resume" in task_types and not any("PDF" in row["summary"] or "pdf" in row["command"].lower() for row in validations):
        add(warnings, "warning", "resume task has no PDF/layout validation row", "validations")

    add(notes, "note", f"structured task record checked: {task_id}", "tasks", task_id)
    return GateReport(str(db), task_id, errors, warnings, notes)


def format_findings(title: str, findings: list[Finding]) -> list[str]:
    lines = [f"{title}: {len(findings)}"]
    if not findings:
        lines.append("  none")
        return lines
    for item in findings:
        location = ""
        if item.table:
            location = f" [{item.table}{':' + item.row_id if item.row_id else ''}]"
        lines.append(f"  - {item.message}{location}")
    return lines


def format_gate_report(report: GateReport) -> str:
    lines = [
        "AI Client Governance Structured Task Gate Report",
        f"DB: {report.db}",
        f"Task: {report.task_id}",
        "",
    ]
    lines.extend(format_findings("Errors", report.errors))
    lines.append("")
    lines.extend(format_findings("Warnings", report.warnings))
    lines.append("")
    lines.extend(format_findings("Notes", report.notes))
    return "\n".join(lines)


def task_summary(con: sqlite3.Connection, task_id: str | None = None) -> dict[str, Any]:
    if task_id:
        task = task_row(con, task_id)
        if task is None:
            return {"task_id": task_id, "exists": False}
        return {
            "task_id": task_id,
            "exists": True,
            "task": dict(task),
            "counts": {
                table: row_count(con, table, task_id)
                for table in ("requirements", "triggers", "outputs", "worktrees", "validations", "events")
            },
        }
    return {
        "schema_version": con.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()["value"],
        "task_count": con.execute("SELECT count(*) FROM tasks").fetchone()[0],
        "tasks": [dict(row) for row in con.execute("SELECT task_id, title, status, task_types_json FROM tasks ORDER BY updated_at DESC")],
    }


def render_markdown(con: sqlite3.Connection, task_id: str) -> str:
    task = task_row(con, task_id)
    if task is None:
        raise ValueError(f"task does not exist: {task_id}")
    lines = [
        f"# {task['title']}",
        "",
        "## Structured Task",
        "",
        f"- Task ID: `{task['task_id']}`",
        f"- Status: `{task['status']}`",
        f"- Task types: `{', '.join(json.loads(task['task_types_json'] or '[]'))}`",
        f"- Approval label: `{task['approval_label'] or 'none'}`",
        f"- Trace: `{task['trace_id'] or 'none'}`",
        "",
        "## Requirements",
        "",
        "| ID | Status | Summary | Implementation | Validation | Final Coverage |",
        "|---|---|---|---|---|---|",
    ]
    for row in rows(con, "requirements", task_id):
        lines.append(
            f"| {row['requirement_id']} | {row['status']} | {row['summary']} | "
            f"{row['implementation_evidence']} | {row['validation_evidence']} | {row['final_coverage']} |"
        )
    lines.extend(["", "## Outputs", "", "| ID | Type | Completed | Unfinished | Unverified | Blocked |", "|---|---|---|---|---|---|"])
    for row in rows(con, "outputs", task_id):
        lines.append(
            f"| {row['output_id']} | {row['output_type']} | {row['completed']} | {row['unfinished']} | "
            f"{row['unverified']} | {row['blocked']} |"
        )
    lines.extend(["", "## Validations", "", "| ID | Result | Command | Summary |", "|---|---|---|---|"])
    for row in rows(con, "validations", task_id):
        lines.append(f"| {row['validation_id']} | {row['result']} | `{row['command']}` | {row['summary']} |")
    lines.append("")
    return "\n".join(lines)


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def main() -> int:
    args = parse_args()
    root = Path(args.root).resolve()
    path = db_path(root, args.db)

    try:
        if args.command == "status":
            if not path.exists():
                summary = empty_summary(path, args.task_id)
            else:
                con = connect(path, create=False)
                summary = task_summary(con, args.task_id)
            print_json(summary) if args.format == "json" else print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 0

        create_db = args.command in {"init", "apply"}
        con = connect(path, create=create_db)
        if create_db:
            init_db(con)

        if args.command == "init":
            result = {"db": str(path), "schema_version": SCHEMA_VERSION}
            print_json(result) if args.format == "json" else print(f"Initialized structured DB: {path}")
            return 0
        if args.command == "apply":
            payload_path = Path(args.json_path)
            if not payload_path.is_absolute():
                payload_path = root / payload_path
            task_id = apply_payload(con, load_payload(payload_path), replace=args.replace)
            result = {"db": str(path), "task_id": task_id, "applied": True}
            print_json(result) if args.format == "json" else print(f"Applied structured task record: {task_id}")
            return 0
        if args.command == "gate":
            report = validate_task(con, path, args.task_id, args.event, args.task_type)
            if args.format == "json":
                print_json(asdict(report))
            else:
                print(format_gate_report(report))
            if report.errors:
                return 1
            if args.fail_on_warning and report.warnings:
                return 1
            gate_id = f"GATE-{args.task_id}-{uuid.uuid4().hex[:8]}"
            with con:
                con.execute(
                    "INSERT INTO gate_runs(gate_run_id, task_id, gate_name, result, errors_json, warnings_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        gate_id,
                        args.task_id,
                        "task-record gate",
                        "fail" if report.errors else "warn" if report.warnings else "pass",
                        json.dumps([asdict(item) for item in report.errors], ensure_ascii=False),
                        json.dumps([asdict(item) for item in report.warnings], ensure_ascii=False),
                        utc_now(),
                    ),
                )
            return 0
        if args.command == "export-md":
            text = render_markdown(con, args.task_id)
            if args.output:
                output = Path(args.output)
                if not output.is_absolute():
                    output = root / output
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(text, encoding="utf-8")
                print(f"Exported structured report: {output}")
            else:
                print(text)
            return 0
    except (OSError, sqlite3.Error, ValueError, json.JSONDecodeError) as exc:
        print(f"task-record error: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
