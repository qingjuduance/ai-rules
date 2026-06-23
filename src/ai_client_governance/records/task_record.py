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
from ai_client_governance.common.time_utils import now_iso as utc_now
from pathlib import Path
from typing import Any

from ai_client_governance.common import cli_arguments as common_cli_args
from ai_client_governance.common.paths import structured_db_path
from ai_client_governance.runtime.scope import COMMON_SCOPE, MIXED_SCOPE, NATIVE_SCOPE, PROJECT_SCOPE, UNKNOWN_SCOPE


SCHEMA_VERSION = 2

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
DESIGN_ONLY_TASK_TYPE = "design-only"
MUTATING_TASK_TYPES = {
    "code-debug",
    "correction",
    "rules-script",
    "docs",
    "git",
    "frontend",
    "resume",
    "multi-agent",
    "long-running",
}
KNOWN_TASK_TYPES = set(MUTATING_TASK_TYPES) | {DESIGN_ONLY_TASK_TYPE}
INPUT_FILTER_PREFLIGHT_EVENT = "input-filter.preflight"
ANALYSIS_CONTRACT_EVENT = "analysis-contract.preflight"
CLIENT_IDENTITY_EVENT = "client-identity.analysis"
CAPABILITY_GATEWAY_FACTS_EVENT = "capability-gateway.facts"
DESIGN_PACKAGE_EVENT = "design-package.analysis"
DESIGN_PACKAGE_REQUIRED_FIELDS = (
    "problem",
    "goals",
    "non_goals",
    "architecture",
    "data_model",
    "policy_gate",
    "migration",
    "validation",
    "risks",
    "implementation_tasks",
    "reviewer_acceptance",
    "handoff_capsule",
)
DESIGN_PACKAGE_LIST_FIELDS = {
    "goals",
    "non_goals",
    "risks",
    "implementation_tasks",
    "reviewer_acceptance",
}
DESIGN_PACKAGE_OBJECT_FIELDS = {
    "architecture",
    "data_model",
    "policy_gate",
    "migration",
    "validation",
    "handoff_capsule",
}
DESIGN_PACKAGE_HANDOFF_REQUIRED_FIELDS = (
    "summary",
    "required_reading",
    "handoff_instructions",
)
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
DOC_IMPACT_EVENT = "doc-impact.analysis"
STATE_ARTIFACT_OWNERSHIP_EVENT = "state-artifact-ownership.analysis"
PATCH_PREFLIGHT_EVENT = "patch-preflight.analysis"
DISCOVERED_ISSUE_RECORDING_EVENT = "final-output.discovered-issues-recorded"
COMMAND_ERROR_EVENT = "command-error.analysis"
AGENT_DISPATCH_BRIEF_EVENT = "agent-dispatch-brief.analysis"
AGENT_REVIEW_RESULT_EVENT = "agent-review-result.analysis"
AGENT_DECISION_EVENT = "agent-decision.analysis"
DATA_CONFIRMATION_EVENT = "data-confirmation.analysis"
SHELL_PROXY_USAGE_EVENT = "shell-proxy-usage.analysis"
HISTORY_REQUIREMENT_RECOVERY_EVENT = "history-requirement-recovery.analysis"
READONLY_SIDE_EFFECT_EVENT = "readonly-side-effect-policy.analysis"
SCOPE_KINDS = {COMMON_SCOPE, PROJECT_SCOPE, NATIVE_SCOPE, MIXED_SCOPE, UNKNOWN_SCOPE}
COMMAND_ERROR_REQUIRED_FIELDS = (
    "failed_command",
    "exit_code",
    "phase",
    "parser_or_shell",
    "failure_category",
    "root_cause",
    "corrected_command",
    "retry_count",
    "dedupe_key",
    "preventive_rule",
    "telemetry_evidence",
    "state_impact",
    "framework_debt_decision",
)


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

    append_event = sub.add_parser("append-event", help="Append one structured event row to an existing task.")
    common_cli_args.add_common_global_args(append_event, suppress_default=True)
    append_event.add_argument("--task-id", required=True)
    append_event.add_argument("--event-type", required=True)
    append_event.add_argument("--event-id", default="")
    append_event.add_argument("--payload-json", default="{}", help="JSON object payload. Defaults to {}.")
    append_event.add_argument("--payload-file", help="UTF-8 JSON file payload. Overrides --payload-json.")

    design_package = sub.add_parser("design-package", help="Append a typed design-package.analysis event to an existing task.")
    common_cli_args.add_common_global_args(design_package, suppress_default=True)
    design_package.add_argument("--task-id", required=True)
    design_package.add_argument("--event-id", default="")
    design_package.add_argument("--payload-json", default="", help="Design package JSON object.")
    design_package.add_argument("--payload-file", help="UTF-8 JSON file payload. Overrides --payload-json.")

    append_validation = sub.add_parser("append-validation", help="Append one validation evidence row to an existing task.")
    common_cli_args.add_common_global_args(append_validation, suppress_default=True)
    append_validation.add_argument("--task-id", required=True)
    append_validation.add_argument("--validation-id", default="")
    append_validation.add_argument("--command", dest="validation_command", required=True)
    append_validation.add_argument("--cwd", required=True)
    append_validation.add_argument("--result", required=True, choices=VALIDATION_RESULTS)
    append_validation.add_argument("--summary", required=True)
    append_validation.add_argument("--evidence", default="")

    append_worktree = sub.add_parser("append-worktree", help="Append or replace one worktree evidence row for an existing task.")
    common_cli_args.add_common_global_args(append_worktree, suppress_default=True)
    append_worktree.add_argument("--task-id", required=True)
    append_worktree.add_argument("--worktree-id", default="")
    append_worktree.add_argument("--repo", required=True, choices=WORKTREE_REPOS)
    append_worktree.add_argument("--source-repo", required=True)
    append_worktree.add_argument("--path", required=True)
    append_worktree.add_argument("--branch", required=True)
    append_worktree.add_argument("--base-commit", required=True)
    append_worktree.add_argument("--creation-method", required=True, choices=WORKTREE_CREATION_METHODS)
    append_worktree.add_argument("--sparse-policy", required=True)
    append_worktree.add_argument("--source-handling", required=True)
    append_worktree.add_argument("--status", required=True, choices=WORKTREE_STATUSES)
    append_worktree.add_argument("--merged-status", required=True, choices=WORKTREE_MERGE_STATUSES)
    append_worktree.add_argument("--commit-status", required=True, choices=WORKTREE_COMMIT_STATUSES)
    append_worktree.add_argument("--push-status", required=True, choices=WORKTREE_PUSH_STATUSES)
    append_worktree.add_argument("--next-action", required=True)

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

    evidence_graph = sub.add_parser("evidence-graph", help="Audit all task-id linked evidence for one task.")
    common_cli_args.add_common_global_args(evidence_graph, suppress_default=True)
    evidence_graph.add_argument("--task-id", required=True)
    evidence_graph.add_argument("--include-telemetry", action="store_true", default=True)

    describe = sub.add_parser("describe", help="Print the task-record schema: tables, fields, enums, and a sample JSON payload.")
    common_cli_args.add_common_global_args(describe, suppress_default=True)
    describe.add_argument("--sample", action="store_true", help="Print a minimal valid sample payload for task-record apply.")
    return parser.parse_args()


def db_path(root: Path, override: str | None) -> Path:
    return structured_db_path(root, override)


def connect(path: Path, *, create: bool = True) -> sqlite3.Connection:
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    elif not path.exists():
        raise ValueError(f"structured DB does not exist: {path}")
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    init_db(con)
    _ensure_time_columns(con)
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
            final_coverage TEXT NOT NULL CHECK (length(trim(final_coverage)) > 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
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
            trace_id TEXT NOT NULL CHECK (length(trim(trace_id)) > 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
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
            trace_id TEXT NOT NULL CHECK (length(trim(trace_id)) > 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
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
            next_action TEXT NOT NULL CHECK (length(trim(next_action)) > 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
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


def _ensure_time_columns(con: sqlite3.Connection) -> None:
    """补齐旧数据库的时间字段 (v1 -> v2 迁移)。

    新数据库由 ``init_db`` 直接生成带列的 schema；历史 v1 数据库
    可能缺少 requirements/triggers/outputs/worktrees 表的
    ``created_at`` / ``updated_at``，本函数以幂等方式补齐。
    """
    current_version = 0
    try:
        con.execute("SELECT value FROM meta WHERE key = 'schema_version'")
        row = con.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        if row is not None:
            try:
                current_version = int(row[0])
            except (TypeError, ValueError):
                current_version = 0
    except sqlite3.OperationalError:
        current_version = 0

    if current_version >= 2:
        return

    now = utc_now()
    migrations = (
        ("requirements", ("created_at", "updated_at")),
        ("triggers", ("created_at", "updated_at")),
        ("outputs", ("created_at", "updated_at")),
        ("worktrees", ("created_at", "updated_at")),
    )
    for table, columns in migrations:
        for column in columns:
            try:
                con.execute(
                    f"SELECT {column} FROM {table} LIMIT 1"
                )
            except sqlite3.OperationalError:
                # ALTER TABLE DEFAULT 不支持 ? 占位符，用 SQLite 字面量
                escaped_now = now.replace("'", "''")
                con.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} TEXT NOT NULL DEFAULT '{escaped_now}'"
                )
    con.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
    con.commit()


# ======================================================================
# Schema descriptor: table columns, enums, required fields
# ======================================================================

# (table, enum_name, enum_tuple) — 用于 describe 输出
_ENUM_REGISTRY: list[tuple[str, str, tuple[str, ...]]] = [
    ("tasks", "status", TASK_STATUSES),
    ("tasks", "task_types", tuple(sorted(KNOWN_TASK_TYPES))),
    ("requirements", "status", REQUIREMENT_STATUSES),
    ("outputs", "output_type", OUTPUT_TYPES),
    ("validations", "result", VALIDATION_RESULTS),
    ("approvals", "status", APPROVAL_STATUSES),
    ("worktrees", "repo", WORKTREE_REPOS),
    ("worktrees", "creation_method", WORKTREE_CREATION_METHODS),
    ("worktrees", "status", WORKTREE_STATUSES),
    ("worktrees", "merged_status", WORKTREE_MERGE_STATUSES),
    ("worktrees", "commit_status", WORKTREE_COMMIT_STATUSES),
    ("worktrees", "push_status", WORKTREE_PUSH_STATUSES),
    ("gate_runs", "result", GATE_RESULTS),
]

# Top-level JSON payload tables, in required-then-optional order.
# (table, required, [columns...])
# 列取自 rows_from_payload + schema SQL。
_PAYLOAD_TABLE_LAYOUT: dict[str, list[tuple[str, str, str]]] = {
    "task": [
        ("task_id", "string", "Primary key. Required. Stable identifier referenced by gates, triggers, outputs, etc."),
        ("title", "string", "Human-readable task title. Required."),
        ("status", "enum<TASK_STATUSES>", "Lifecycle state. Required."),
        ("task_size", "string", "small|medium|large. Defaults to medium."),
        ("task_types", "array<string>", "Gate routing. Subset of code-debug|correction|design-only|docs|frontend|git|long-running|multi-agent|resume|rules-script. Required."),
        ("summary", "string", "Short task summary. Optional."),
        ("approval_label", "string", "Required when file changes need explicit approval."),
        ("trace_id", "string", "Optional trace id shared across rows from the same run."),
        ("created_at", "datetime", "ISO 8601 timestamp. Defaults to now()."),
        ("updated_at", "datetime", "ISO 8601 timestamp. Defaults to now()."),
    ],
    "approvals": [
        ("approval_id", "string", "Stable approval row id; generated when omitted."),
        ("label", "string", "Approval label. Required for rules-script tasks."),
        ("status", "enum<APPROVAL_STATUSES>", "requested|approved|rejected. Required."),
        ("summary", "string", "Optional summary of what was approved / rejected."),
        ("created_at", "datetime", "ISO 8601 timestamp. Defaults to now()."),
    ],
    "requirements": [
        ("requirement_id", "string", "Stable id; generated when omitted."),
        ("summary", "string", "Human-readable requirement summary. Required."),
        ("record_decision", "string", "include|omit|defer. Required."),
        ("network_decision", "string", "not-required|required|deferred. Required."),
        ("validation_decision", "string", "syntax-only|gate-only|full|skipped. Required."),
        ("acceptance", "string", "Success criteria for this requirement. Required."),
        ("status", "enum<REQUIREMENT_STATUSES>", "open|in_progress|done|blocked|deferred|cancelled. Required."),
        ("action", "string", "What we will do to satisfy this requirement. Required."),
        ("implementation_evidence", "string", "Files/commands/evidence produced for this requirement. Required."),
        ("validation_evidence", "string", "Evidence of validation. Required."),
        ("final_coverage", "string", "Coverage notes for this requirement. Required."),
        ("created_at", "datetime", "ISO 8601 timestamp. Defaults to now()."),
        ("updated_at", "datetime", "ISO 8601 timestamp. Defaults to now()."),
    ],
    "triggers": [
        ("trigger_id", "string", "Stable id; generated when omitted."),
        ("trigger_type", "string", "input-filter|user-message|command-compression|scope-classification|gate|etc. Required."),
        ("source", "string", "Where this trigger originated. Required."),
        ("matched_requirement", "string", "Requirement id(s) this trigger matches. Required."),
        ("priority", "string", "high|medium|low. Required."),
        ("applicability_scope", "string", "Scope this trigger applies to. Required."),
        ("scope_expansion", "string", "Scope expansion notes. Required."),
        ("reason", "string", "Why this trigger applies. Required."),
        ("required_action", "string", "Action required by the trigger. Required."),
        ("executed_steps", "string", "Steps executed. Required."),
        ("quantitative_evidence", "string", "Numbers: commands, files, timings. Required."),
        ("status", "string", "fired|pending|blocked. Required."),
        ("trace_id", "string", "Trace id shared across related rows. Required."),
        ("created_at", "datetime", "ISO 8601 timestamp. Defaults to now()."),
        ("updated_at", "datetime", "ISO 8601 timestamp. Defaults to now()."),
    ],
    "outputs": [
        ("output_id", "string", "Stable id; generated when omitted."),
        ("output_type", "enum<OUTPUT_TYPES>", "plan|status|final|script|error|git_worktree. Required."),
        ("applicability_scope", "string", "Scope covered. Required."),
        ("exclusions", "string", "What is excluded. Required even if empty string."),
        ("objects", "string", "What objects/files changed. Required."),
        ("fact_source", "string", "Source of facts. Required."),
        ("completed", "string", "Completed items summary. Required."),
        ("unfinished", "string", "Unfinished items. Required even if empty string."),
        ("unverified", "string", "Unverified items. Required even if empty string."),
        ("blocked", "string", "Blocked items. Required even if empty string."),
        ("user_confirmation", "string", "Confirmation requested from user. Required even if empty string."),
        ("final_coverage", "string", "Final coverage summary. Required."),
        ("trace_id", "string", "Trace id. Required even if empty string."),
        ("created_at", "datetime", "ISO 8601 timestamp. Defaults to now()."),
        ("updated_at", "datetime", "ISO 8601 timestamp. Defaults to now()."),
    ],
    "worktrees": [
        ("worktree_id", "string", "Stable id; generated when omitted."),
        ("repo", "enum<WORKTREE_REPOS>", "self|ai-client-governance|other. Required."),
        ("source_repo", "string", "Source repository path. Required."),
        ("path", "string", "Worktree path (relative to project root). Required."),
        ("branch", "string", "Task branch name. Required."),
        ("base_commit", "string", "Base commit hash or reference. Required."),
        ("creation_method", "enum<WORKTREE_CREATION_METHODS>", "worktree-task|break-glass|external. Required."),
        ("sparse_policy", "string", "Sparse checkout policy. Required even if 'none'."),
        ("source_handling", "string", "How source code is handled in the worktree. Required."),
        ("status", "enum<WORKTREE_STATUSES>", "active|done|blocked|removed. Required."),
        ("merged_status", "enum<WORKTREE_MERGE_STATUSES>", "not_merged|merged|not_required. Required."),
        ("commit_status", "enum<WORKTREE_COMMIT_STATUSES>", "not_committed|committed|not_required. Required."),
        ("push_status", "enum<WORKTREE_PUSH_STATUSES>", "not_pushed|pushed|not_required. Required."),
        ("next_action", "string", "Next action for this worktree. Required even if empty string."),
        ("created_at", "datetime", "ISO 8601 timestamp. Defaults to now()."),
        ("updated_at", "datetime", "ISO 8601 timestamp. Defaults to now()."),
    ],
    "validations": [
        ("validation_id", "string", "Stable id; generated when omitted."),
        ("command", "string", "Command executed. Required."),
        ("cwd", "string", "Working directory of the command. Required."),
        ("result", "enum<VALIDATION_RESULTS>", "pass|fail|warn|skipped. Required."),
        ("summary", "string", "Summary of what was validated. Required."),
        ("evidence", "string", "Optional evidence text."),
        ("created_at", "datetime", "ISO 8601 timestamp. Defaults to now()."),
    ],
    "events": [
        ("event_id", "string", "Stable id; generated when omitted."),
        ("event_type", "string", "Dot-separated event type: design-package.analysis, client-identity.analysis, plan-approval-boundary.analysis, command-compression.analysis, scope-classification.analysis, user-claim-validation.analysis, state-artifact-ownership.analysis, patch-preflight.analysis, etc. Required."),
        ("payload", "object", "Free-form JSON object. Required even if empty object {}. Written into the DB as JSON text."),
        ("created_at", "datetime", "ISO 8601 timestamp. Defaults to now()."),
    ],
}


def build_schema_descriptor() -> dict[str, Any]:
    """Return a dict describing the full task-record schema.

    Enums are listed explicitly so callers don't need to read Python source.
    """
    enums: dict[str, list[str]] = {
        "TASK_STATUSES": list(TASK_STATUSES),
        "REQUIREMENT_STATUSES": list(REQUIREMENT_STATUSES),
        "OUTPUT_TYPES": list(OUTPUT_TYPES),
        "VALIDATION_RESULTS": list(VALIDATION_RESULTS),
        "APPROVAL_STATUSES": list(APPROVAL_STATUSES),
        "WORKTREE_REPOS": list(WORKTREE_REPOS),
        "WORKTREE_CREATION_METHODS": list(WORKTREE_CREATION_METHODS),
        "WORKTREE_STATUSES": list(WORKTREE_STATUSES),
        "WORKTREE_MERGE_STATUSES": list(WORKTREE_MERGE_STATUSES),
        "WORKTREE_COMMIT_STATUSES": list(WORKTREE_COMMIT_STATUSES),
        "WORKTREE_PUSH_STATUSES": list(WORKTREE_PUSH_STATUSES),
        "GATE_RESULTS": list(GATE_RESULTS),
        "KNOWN_TASK_TYPES": list(sorted(KNOWN_TASK_TYPES)),
    }

    tables: dict[str, list[dict[str, str]]] = {}
    for table_name, columns in _PAYLOAD_TABLE_LAYOUT.items():
        tables[table_name] = [
            {"name": name, "type": col_type, "description": desc}
            for name, col_type, desc in columns
        ]

    enum_usage: list[dict[str, str]] = []
    for table, field, values in _ENUM_REGISTRY:
        enum_usage.append({"table": table, "field": field, "enum": values[0].upper() + "S"})

    return {
        "schema_version": SCHEMA_VERSION,
        "tables": tables,
        "enums": enums,
        "enum_usage": enum_usage,
    }


def build_sample_payload() -> dict[str, Any]:
    """Return a minimal valid payload usable with `task-record apply`."""
    now = utc_now()
    return {
        "task": {
            "task_id": "TASK-20260619-SAMPLE",
            "title": "Sample structured task record",
            "status": "active",
            "task_size": "small",
            "task_types": ["rules-script"],
            "summary": "Demonstrates a minimal valid payload.",
            "approval_label": "sample-approval",
            "trace_id": "sample-trace-001",
            "created_at": now,
            "updated_at": now,
        },
        "approvals": [
            {
                "approval_id": "APV-001",
                "label": "sample-approval",
                "status": "approved",
                "summary": "Approved for schema demonstration purposes only.",
                "created_at": now,
            }
        ],
        "requirements": [
            {
                "requirement_id": "REQ-001",
                "summary": "Sample requirement summary.",
                "record_decision": "include",
                "network_decision": "not-required",
                "validation_decision": "syntax-only",
                "acceptance": "Schema validation succeeds; apply succeeds.",
                "status": "open",
                "action": "Run task-record apply and verify no errors.",
                "implementation_evidence": "task_record.py rows_from_payload + apply_payload.",
                "validation_evidence": "n/a for sample.",
                "final_coverage": "All rows written successfully.",
                "created_at": now,
                "updated_at": now,
            }
        ],
        "triggers": [
            {
                "trigger_id": "TRG-001",
                "trigger_type": "input-filter",
                "source": "sample",
                "matched_requirement": "REQ-001",
                "priority": "medium",
                "applicability_scope": "demonstration",
                "scope_expansion": "none",
                "reason": "Demonstration payload.",
                "required_action": "run task-record apply",
                "executed_steps": "task-record apply --json sample.json",
                "quantitative_evidence": "1 task, 1 requirement row",
                "status": "fired",
                "trace_id": "sample-trace-001",
                "created_at": now,
                "updated_at": now,
            }
        ],
        "outputs": [
            {
                "output_id": "OUT-001",
                "output_type": "final",
                "applicability_scope": "demonstration",
                "exclusions": "none",
                "objects": "schema description",
                "fact_source": "task_record.py _PAYLOAD_TABLE_LAYOUT",
                "completed": "sample payload built",
                "unfinished": "none",
                "unverified": "none",
                "blocked": "none",
                "user_confirmation": "none",
                "final_coverage": "sample payload covers all required tables",
                "trace_id": "sample-trace-001",
                "created_at": now,
                "updated_at": now,
            }
        ],
        "worktrees": [
            {
                "worktree_id": "WT-001",
                "repo": "ai-client-governance",
                "source_repo": ".ai-client/ai-client-governance",
                "path": ".ai-client/project/.worktree/20260619-sample",
                "branch": "task/20260619-sample",
                "base_commit": "HEAD",
                "creation_method": "worktree-task",
                "sparse_policy": "none",
                "source_handling": "in-worktree",
                "status": "active",
                "merged_status": "not_merged",
                "commit_status": "not_committed",
                "push_status": "not_pushed",
                "next_action": "verify sample then discard",
                "created_at": now,
                "updated_at": now,
            }
        ],
        "validations": [
            {
                "validation_id": "VAL-001",
                "command": "python -c \"import ast; ast.parse(open('src/ai_client_governance/records/task_record.py').read())\"",
                "cwd": ".ai-client/ai-client-governance",
                "result": "skipped",
                "summary": "Sample validation placeholder.",
                "evidence": "",
                "created_at": now,
            }
        ],
        "events": [
            {
                "event_id": "EVT-001",
                "event_type": "sample-event.analysis",
                "payload": {"note": "Sample event payload."},
                "created_at": now,
            }
        ],
    }


def _compact_table_fields() -> dict[str, list[str]]:
    return {
        "task": ["task_id", "title", "status", "task_types", "created_at"],
        "approvals": ["approval_id", "label", "status", "created_at"],
        "requirements": ["requirement_id", "summary", "record_decision", "status", "action"],
        "triggers": ["trigger_id", "trigger_type", "source", "matched_requirement", "priority"],
        "outputs": ["output_id", "output_type", "applicability_scope", "completed", "dirty"],
        "worktrees": ["worktree_id", "repo", "path", "branch", "status", "merged_status"],
        "validations": ["validation_id", "result", "command", "summary"],
        "events": ["event_id", "event_type", "payload", "created_at"],
    }


def format_compact_text_descriptor() -> str:
    """Simplified text output: ~30 lines, human and AI readable."""
    lines: list[str] = []
    lines.append("task-record schema")
    lines.append("commands: init | apply | design-package | describe [--sample] | gate | status | export-md")
    lines.append("")
    lines.append("tables (key fields):")
    for table, fields in _compact_table_fields().items():
        lines.append(f"  {table}: {', '.join(fields)}")
    lines.append("")
    lines.append("worktree enums:")
    lines.append("  repo: self, ai-client-governance")
    lines.append("  creation_method: worktree-task, break-glass, external")
    lines.append("  status: active, done, blocked, removed")
    lines.append("  merged_status: not_merged, merged, not_required")
    lines.append("  commit_status: not_committed, committed, not_required")
    lines.append("  push_status: not_pushed, pushed, not_required")
    lines.append("")
    lines.append("other enums:")
    lines.append("  task status: candidate, awaiting_approval, ready, active, verifying, done, blocked, cancelled")
    lines.append("  validation result: pass, fail, warn, skipped")
    lines.append("  output_type: plan, status, final, script, error, git_worktree")
    lines.append("  task_types includes design-only for DB-backed design handoff/review packages")
    lines.append("")
    lines.append("notes:")
    lines.append("  - created_at / updated_at auto-filled on write")
    lines.append("  - design-package command writes event_type=design-package.analysis")
    lines.append("  - --sample for a minimal apply JSON payload")
    lines.append("  - --format json for the full schema descriptor")
    return "\n".join(lines)


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
                "created_at": clean_text(req.get("created_at", now), "requirements[].created_at"),
                "updated_at": clean_text(req.get("updated_at", now), "requirements[].updated_at"),
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
                "created_at": clean_text(trigger.get("created_at", now), "triggers[].created_at"),
                "updated_at": clean_text(trigger.get("updated_at", now), "triggers[].updated_at"),
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
                "created_at": clean_text(output.get("created_at", now), "outputs[].created_at"),
                "updated_at": clean_text(output.get("updated_at", now), "outputs[].updated_at"),
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
                "created_at": clean_text(wt.get("created_at", now), "worktrees[].created_at"),
                "updated_at": clean_text(wt.get("updated_at", now), "worktrees[].updated_at"),
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


def load_json_object(value: str, label: str) -> dict[str, Any]:
    parsed = json.loads(value)
    return require_mapping(parsed, label)


def load_json_object_from_args(root: Path, payload_json: str, payload_file: str | None) -> dict[str, Any]:
    if payload_file:
        path = Path(payload_file)
        if not path.is_absolute():
            path = root / path
        return load_json_object(path.read_text(encoding="utf-8-sig"), "payload-file")
    if not payload_json:
        raise ValueError("payload-json or payload-file is required")
    return load_json_object(payload_json, "payload-json")


def _value_has_content(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return any(_value_has_content(item) for item in value)
    if isinstance(value, dict):
        return any(_value_has_content(item) for item in value.values())
    return value is not None


def design_package_errors(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for field in DESIGN_PACKAGE_REQUIRED_FIELDS:
        if field not in payload or not _value_has_content(payload.get(field)):
            errors.append(f"missing_or_empty:{field}")

    for field in sorted(DESIGN_PACKAGE_LIST_FIELDS):
        value = payload.get(field)
        if not isinstance(value, list) or not value:
            errors.append(f"must_be_non_empty_list:{field}")
            continue
        for index, item in enumerate(value):
            if not _value_has_content(item):
                errors.append(f"empty_list_item:{field}[{index}]")

    for field in sorted(DESIGN_PACKAGE_OBJECT_FIELDS):
        value = payload.get(field)
        if not isinstance(value, dict) or not _value_has_content(value):
            errors.append(f"must_be_non_empty_object:{field}")

    handoff = payload.get("handoff_capsule")
    if isinstance(handoff, dict):
        for field in DESIGN_PACKAGE_HANDOFF_REQUIRED_FIELDS:
            if not _value_has_content(handoff.get(field)):
                errors.append(f"handoff_capsule_missing:{field}")
    return errors


def require_design_package_payload(payload: dict[str, Any]) -> dict[str, Any]:
    errors = design_package_errors(payload)
    if errors:
        raise ValueError("invalid design package payload: " + "; ".join(errors))
    return payload


def append_event_row(
    con: sqlite3.Connection,
    *,
    task_id: str,
    event_type: str,
    payload: dict[str, Any],
    event_id: str = "",
) -> str:
    if task_row(con, task_id) is None:
        raise ValueError(f"task does not exist: {task_id}")
    now = utc_now()
    row = {
        "event_id": clean_text(event_id or f"EVT-{task_id}-{uuid.uuid4().hex[:8]}", "events[].event_id"),
        "task_id": task_id,
        "event_type": clean_text(event_type, "events[].event_type"),
        "payload_json": json.dumps(payload, ensure_ascii=False, sort_keys=True),
        "created_at": now,
    }
    with con:
        insert_rows(con, "events", [row])
    return row["event_id"]


def append_validation_row(con: sqlite3.Connection, args: argparse.Namespace) -> str:
    task_id = args.task_id
    if task_row(con, task_id) is None:
        raise ValueError(f"task does not exist: {task_id}")
    row = {
        "validation_id": clean_text(
            args.validation_id or f"VAL-{task_id}-{uuid.uuid4().hex[:8]}",
            "validations[].validation_id",
        ),
        "task_id": task_id,
        "command": clean_text(args.validation_command, "validations[].command"),
        "cwd": clean_text(args.cwd, "validations[].cwd"),
        "result": enum_text(args.result, "validations[].result", VALIDATION_RESULTS),
        "summary": clean_text(args.summary, "validations[].summary"),
        "evidence": clean_text(args.evidence, "validations[].evidence", required=False),
        "created_at": utc_now(),
    }
    with con:
        con.execute("DELETE FROM validations WHERE validation_id = ?", (row["validation_id"],))
        insert_rows(con, "validations", [row])
    return row["validation_id"]


def append_worktree_row(con: sqlite3.Connection, args: argparse.Namespace) -> str:
    task_id = args.task_id
    if task_row(con, task_id) is None:
        raise ValueError(f"task does not exist: {task_id}")
    now = utc_now()
    row = {
        "worktree_id": clean_text(args.worktree_id or f"WT-{task_id}-{uuid.uuid4().hex[:8]}", "worktrees[].worktree_id"),
        "task_id": task_id,
        "repo": enum_text(args.repo, "worktrees[].repo", WORKTREE_REPOS),
        "source_repo": clean_text(args.source_repo, "worktrees[].source_repo"),
        "path": clean_text(args.path, "worktrees[].path"),
        "branch": clean_text(args.branch, "worktrees[].branch"),
        "base_commit": clean_text(args.base_commit, "worktrees[].base_commit"),
        "creation_method": enum_text(args.creation_method, "worktrees[].creation_method", WORKTREE_CREATION_METHODS),
        "sparse_policy": clean_text(args.sparse_policy, "worktrees[].sparse_policy"),
        "source_handling": clean_text(args.source_handling, "worktrees[].source_handling"),
        "status": enum_text(args.status, "worktrees[].status", WORKTREE_STATUSES),
        "merged_status": enum_text(args.merged_status, "worktrees[].merged_status", WORKTREE_MERGE_STATUSES),
        "commit_status": enum_text(args.commit_status, "worktrees[].commit_status", WORKTREE_COMMIT_STATUSES),
        "push_status": enum_text(args.push_status, "worktrees[].push_status", WORKTREE_PUSH_STATUSES),
        "next_action": clean_text(args.next_action, "worktrees[].next_action"),
        "created_at": now,
        "updated_at": now,
    }
    with con:
        con.execute("DELETE FROM worktrees WHERE worktree_id = ?", (row["worktree_id"],))
        insert_rows(con, "worktrees", [row])
    return row["worktree_id"]


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


def validate_input_filter_preflight(
    con: sqlite3.Connection,
    task_id: str,
    errors: list[Finding],
    warnings: list[Finding],
    notes: list[Finding],
) -> None:
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

    identity_payloads = []
    for event in events:
        if str(event["event_type"] or "").strip() != CLIENT_IDENTITY_EVENT:
            continue
        try:
            identity_payloads.append(json.loads(event["payload_json"] or "{}"))
        except json.JSONDecodeError:
            identity_payloads.append({})
    if not identity_payloads:
        add(
            errors,
            "error",
            f"input-filter preflight requires an events row with event_type={CLIENT_IDENTITY_EVENT}",
            "events",
        )
        return
    latest_identity = identity_payloads[-1]
    missing_identity = [
        field
        for field in ("client_type", "model_id")
        if not str(latest_identity.get(field) or "").strip()
    ]
    if missing_identity:
        add(
            errors,
            "error",
            f"client/model identity event lacks required field(s): {', '.join(missing_identity)}",
            "events",
        )
        return
    unknown_identity = [
        field
        for field in ("client_type", "model_id")
        if str(latest_identity.get(field) or "").strip().lower() == "unknown"
    ]
    if unknown_identity:
        add(
            warnings,
            "warning",
            "client/model identity is unknown for: " + ", ".join(unknown_identity),
            "events",
        )
    add(
        notes,
        "note",
        (
            "client/model identity facts present: "
            f"{latest_identity.get('client_type')} / {latest_identity.get('model_id')}"
        ),
        "events",
    )


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


def table_exists(con: sqlite3.Connection, table: str) -> bool:
    row = con.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def json_row_count(con: sqlite3.Connection, table: str, task_id: str) -> dict[str, Any]:
    if not table_exists(con, table):
        return {"table": table, "present": False, "count": 0}
    if table in {"corrections", "framework_debt"}:
        column = "related_task_id"
    else:
        column = "task_id"
    try:
        count = con.execute(f"SELECT count(*) FROM {table} WHERE {column} = ?", (task_id,)).fetchone()[0]
    except sqlite3.Error as exc:
        return {"table": table, "present": True, "count": 0, "error": str(exc)}
    return {"table": table, "present": True, "count": int(count)}


def telemetry_command_error_summary(con: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    if not table_exists(con, "execution_spans"):
        return {
            "table": "execution_spans",
            "present": False,
            "failed_span_count": 0,
            "unclassified_failure_count": 0,
        }
    failed_rows = con.execute(
        """
        SELECT span_id, attributes_json
        FROM execution_spans
        WHERE task_id = ?
          AND (status = 'failed' OR coalesce(exit_code, 0) != 0)
        """,
        (task_id,),
    ).fetchall()
    unclassified = 0
    categories: dict[str, int] = {}
    for row in failed_rows:
        try:
            attrs = json.loads(row["attributes_json"] or "{}")
        except json.JSONDecodeError:
            attrs = {}
        command_error = attrs.get("command_error") if isinstance(attrs, dict) else {}
        if not isinstance(command_error, dict):
            command_error = {}
        category = str(command_error.get("failure_category") or "unclassified_command_failure")
        categories[category] = categories.get(category, 0) + 1
        if category == "unclassified_command_failure":
            unclassified += 1
    return {
        "table": "execution_spans",
        "present": True,
        "failed_span_count": len(failed_rows),
        "unclassified_failure_count": unclassified,
        "failure_categories": categories,
    }


def build_evidence_graph(con: sqlite3.Connection, task_id: str, include_telemetry: bool = True) -> dict[str, Any]:
    task = task_row(con, task_id)
    linked_tables = [
        "tasks",
        "approvals",
        "requirements",
        "triggers",
        "outputs",
        "worktrees",
        "validations",
        "events",
        "gate_runs",
    ]
    linked_counts = {table: json_row_count(con, table, task_id) for table in linked_tables}
    optional_counts = {
        "corrections": json_row_count(con, "corrections", task_id),
        "framework_debt": json_row_count(con, "framework_debt", task_id),
    }
    telemetry = telemetry_command_error_summary(con, task_id) if include_telemetry else {"skipped": True}
    required_minimums = {
        "tasks": 1,
        "requirements": 1,
        "triggers": 1,
        "outputs": 1,
        "events": 1,
    }
    gaps: list[str] = []
    if task is None:
        gaps.append("tasks:missing")
    for table, minimum in required_minimums.items():
        if int(linked_counts.get(table, {}).get("count", 0)) < minimum:
            gaps.append(f"{table}:count<{minimum}")
    if include_telemetry and telemetry.get("present") and int(telemetry.get("unclassified_failure_count", 0)) > 0:
        gaps.append("telemetry:unclassified_command_failure")
    return {
        "schema_version": 1,
        "task_id": task_id,
        "status": "pass" if not gaps else "fail",
        "task_present": task is not None,
        "linked_counts": linked_counts,
        "optional_task_scoped_counts": optional_counts,
        "telemetry": telemetry,
        "gaps": gaps,
        "correlation_spine": {
            "root": "task_id",
            "telemetry": "execution_spans.task_id plus trace_id/span_id",
            "corrections": "corrections.related_task_id",
            "framework_debt": "framework_debt.related_task_id",
        },
    }


def render_evidence_graph(report: dict[str, Any]) -> str:
    lines = [
        "Task Evidence Graph",
        f"Task: {report['task_id']}",
        f"Status: {report['status']}",
        "Linked rows:",
    ]
    for table, item in report["linked_counts"].items():
        lines.append(f"- {table}: {item.get('count', 0)} ({'present' if item.get('present') else 'missing table'})")
    lines.append("Optional task-scoped rows:")
    for table, item in report["optional_task_scoped_counts"].items():
        lines.append(f"- {table}: {item.get('count', 0)} ({'present' if item.get('present') else 'missing table'})")
    telemetry = report.get("telemetry", {})
    if isinstance(telemetry, dict):
        lines.append(
            "Telemetry command errors: "
            f"failed={telemetry.get('failed_span_count', 0)} "
            f"unclassified={telemetry.get('unclassified_failure_count', 0)}"
        )
    if report["gaps"]:
        lines.append("Gaps: " + ", ".join(report["gaps"]))
    else:
        lines.append("Gaps: none")
    return "\n".join(lines)


def command_error_payload_errors(payload: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    for field in COMMAND_ERROR_REQUIRED_FIELDS:
        if field == "retry_count":
            if payload.get(field) is None:
                issues.append(field)
            continue
        if not payload_nonempty(payload.get(field)):
            issues.append(field)
    if str(payload.get("failure_category") or "").strip() == "unclassified_command_failure":
        issues.append("failure_category.unclassified_command_failure")
    return issues


def validate_command_error_analysis(
    con: sqlite3.Connection,
    task: sqlite3.Row,
    task_types: set[str],
    event: str,
    errors: list[Finding],
    notes: list[Finding],
) -> None:
    """Fail final gates when command failures are not classified and resolved."""
    if event != "final":
        return
    task_size = str(task["task_size"] or "").strip().lower()
    if not (task_types & MUTATING_TASK_TYPES or task_size in {"medium", "large"}):
        return

    payloads = event_payloads(con, task["task_id"], COMMAND_ERROR_EVENT)
    invalid: list[str] = []
    for event_id, payload in payloads:
        payload_errors = command_error_payload_errors(payload)
        if payload_errors:
            invalid.append(f"{event_id}:{','.join(payload_errors)}")

    telemetry_summary = telemetry_command_error_summary(con, task["task_id"])
    failed_spans = int(telemetry_summary.get("failed_span_count", 0) or 0)
    unclassified_spans = int(telemetry_summary.get("unclassified_failure_count", 0) or 0)
    if invalid:
        add(
            errors,
            "error",
            "command-error.analysis events must classify failures and include required remediation fields",
            "events",
            "; ".join(invalid),
        )
    elif payloads:
        add(notes, "note", f"command-error analysis facts present: {COMMAND_ERROR_EVENT}", "events")

    if failed_spans and not payloads:
        add(
            errors,
            "error",
            "telemetry records failed command spans but no command-error.analysis event was recorded",
            "events",
            f"failed_spans={failed_spans}",
        )
    if unclassified_spans:
        add(
            errors,
            "error",
            "telemetry contains unclassified command failures; classify them before final closeout",
            "execution_spans",
            f"unclassified={unclassified_spans}",
        )


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


def validate_prewrite_runtime_adapter(
    con: sqlite3.Connection,
    task: sqlite3.Row,
    task_types: set[str],
    event: str,
    errors: list[Finding],
    notes: list[Finding],
) -> None:
    """Require approved active task + task worktree before mutating writes."""
    if not (task_types & MUTATING_TASK_TYPES):
        return
    if event != "preflight":
        return

    if str(task["status"] or "").strip() not in {"active", "verifying", "done"}:
        add(
            errors,
            "error",
            "prewrite runtime adapter requires task status active/verifying/done before repository writes",
            "tasks",
            task["task_id"],
        )

    worktrees = rows(con, "worktrees", task["task_id"])
    if not worktrees:
        add(
            errors,
            "error",
            "prewrite runtime adapter requires task worktree evidence before repository writes",
            "worktrees",
        )
        return

    usable = [
        worktree
        for worktree in worktrees
        if worktree["creation_method"] == "worktree-task"
        and worktree["status"] in {"active", "done"}
        and worktree["commit_status"] in {"not_committed", "committed", "not_required"}
        and worktree["push_status"] in {"not_pushed", "not_required"}
    ]
    if not usable:
        add(
            errors,
            "error",
            "prewrite runtime adapter requires an active/done worktree-task row with local-only push boundary",
            "worktrees",
        )
    else:
        add(notes, "note", "prewrite runtime adapter facts present: active task + task worktree", "worktrees")


def validate_agent_decision(
    con: sqlite3.Connection,
    task: sqlite3.Row,
    task_types: set[str],
    event: str,
    errors: list[Finding],
    notes: list[Finding],
) -> None:
    """Require an explicit agent-group decision before larger mutating work."""
    allowed_decisions = {"spawned", "not_spawned", "deferred", "reused", "merged"}
    task_size = str(task["task_size"] or "").strip().lower()
    if not (task_types & MUTATING_TASK_TYPES or task_size in {"medium", "large"}):
        return
    payloads = event_payloads(con, task["task_id"], AGENT_DECISION_EVENT)
    if not payloads:
        add(errors, "error", f"agent decision requires event_type={AGENT_DECISION_EVENT}", "events")
        return

    valid = False
    invalid_event_ids: list[str] = []
    for event_id, payload in payloads:
        decision = str(payload.get("agent_group_decision") or "").strip()
        try:
            spawn_count = int(payload.get("spawn_count", 0))
        except (TypeError, ValueError):
            spawn_count = -1
        no_spawn_reason = str(payload.get("no_spawn_reason") or "").strip()
        context_pack_ref = str(payload.get("context_pack_ref") or "").strip()
        confirmation = str(payload.get("data_confirmation_evidence") or "").strip()
        alternative_validation = str(payload.get("alternative_validation") or "").strip()
        residual_risk = str(payload.get("residual_risk") or "").strip()
        if decision not in allowed_decisions or spawn_count < 0 or not context_pack_ref or not confirmation:
            invalid_event_ids.append(event_id)
            continue
        if event == "final" and "multi-agent" in task_types and decision not in {"spawned", "reused", "merged"}:
            invalid_event_ids.append(event_id)
            continue
        if decision == "spawned" and spawn_count < 1:
            invalid_event_ids.append(event_id)
            continue
        if spawn_count == 0 and (not no_spawn_reason or not alternative_validation or not residual_risk):
            invalid_event_ids.append(event_id)
            continue
        valid = True
        break
    if not valid:
        add(
            errors,
            "error",
            (
                "agent decision must record agent_group_decision, spawn_count/no_spawn_reason, "
                "context_pack_ref, data_confirmation_evidence, and alternative_validation/residual_risk when spawn_count=0; "
                "final multi-agent gates require spawned/reused/merged evidence"
            ),
            "events",
            ", ".join(invalid_event_ids),
        )
    else:
        add(notes, "note", f"agent decision facts present: {AGENT_DECISION_EVENT}", "events")


def validate_data_confirmation(
    con: sqlite3.Connection,
    task: sqlite3.Row,
    task_types: set[str],
    errors: list[Finding],
    notes: list[Finding],
) -> None:
    """Require checked data sources before user claims or history steer execution."""
    task_size = str(task["task_size"] or "").strip().lower()
    if not (task_types & MUTATING_TASK_TYPES or task_size in {"medium", "large"}):
        return
    payloads = event_payloads(con, task["task_id"], DATA_CONFIRMATION_EVENT)
    if not payloads:
        add(errors, "error", f"data confirmation requires event_type={DATA_CONFIRMATION_EVENT}", "events")
        return
    valid = False
    invalid_event_ids: list[str] = []
    for event_id, payload in payloads:
        sources = payload.get("confirmation_sources")
        checks = payload.get("checked_facts")
        if not isinstance(sources, list) or not sources:
            invalid_event_ids.append(event_id)
            continue
        if not isinstance(checks, list) or not checks:
            invalid_event_ids.append(event_id)
            continue
        if "unverified_items" not in payload:
            invalid_event_ids.append(event_id)
            continue
        valid = True
        break
    if not valid:
        add(
            errors,
            "error",
            "data confirmation must record confirmation_sources, checked_facts, and unverified_items",
            "events",
            ", ".join(invalid_event_ids),
        )
    else:
        add(notes, "note", f"data confirmation facts present: {DATA_CONFIRMATION_EVENT}", "events")


def validate_shell_proxy_usage(
    con: sqlite3.Connection,
    task: sqlite3.Row,
    task_types: set[str],
    event: str,
    errors: list[Finding],
    notes: list[Finding],
) -> None:
    """Require PowerShell proxy policy before command-heavy work and evidence at final."""
    task_size = str(task["task_size"] or "").strip().lower()
    if not (task_types & MUTATING_TASK_TYPES or task_size in {"medium", "large"}):
        return
    payloads = event_payloads(con, task["task_id"], SHELL_PROXY_USAGE_EVENT)
    if not payloads:
        add(errors, "error", f"shell proxy usage requires event_type={SHELL_PROXY_USAGE_EVENT}", "events")
        return
    valid = False
    invalid_event_ids: list[str] = []
    for event_id, payload in payloads:
        policy = str(payload.get("policy") or "").strip()
        planned_runner = str(payload.get("planned_runner") or "").strip()
        exception_reason = str(payload.get("exception_reason") or "").strip()
        used_proxy = payload.get("used_proxy")
        telemetry_evidence = str(payload.get("telemetry_evidence") or payload.get("proxy_invocation_id") or "").strip()
        enforcement_mode = str(payload.get("enforcement_mode") or payload.get("shell_enforcement_mode") or "").strip()
        profile_policy = str(payload.get("profile_policy") or "").strip()
        profile_touched = payload.get("profile_touched")
        user_shell_impact = str(payload.get("user_shell_impact") or "").strip()
        if not policy or not planned_runner:
            invalid_event_ids.append(event_id)
            continue
        if profile_touched is True or (profile_policy and profile_policy != "no_profile"):
            invalid_event_ids.append(event_id)
            continue
        if event == "final":
            if used_proxy is True:
                if not telemetry_evidence:
                    invalid_event_ids.append(event_id)
                    continue
                if enforcement_mode != "non-invasive-command-proxy":
                    invalid_event_ids.append(event_id)
                    continue
                if profile_policy != "no_profile" or profile_touched is not False or user_shell_impact != "none":
                    invalid_event_ids.append(event_id)
                    continue
            elif not exception_reason:
                invalid_event_ids.append(event_id)
                continue
        valid = True
        break
    if not valid:
        suffix = (
            "; final gate also requires used_proxy=true with telemetry_evidence/proxy_invocation_id, "
            "enforcement_mode=non-invasive-command-proxy, profile_policy=no_profile, "
            "profile_touched=false, user_shell_impact=none, or exception_reason"
            if event == "final"
            else ""
        )
        add(
            errors,
            "error",
            f"shell proxy usage must record policy and planned_runner{suffix}",
            "events",
            ", ".join(invalid_event_ids),
        )
    else:
        add(notes, "note", f"shell proxy usage facts present: {SHELL_PROXY_USAGE_EVENT}", "events")


def validate_capability_gateway_event(
    con: sqlite3.Connection,
    task: sqlite3.Row,
    errors: list[Finding],
    notes: list[Finding],
) -> None:
    payloads = event_payloads(con, task["task_id"], CAPABILITY_GATEWAY_FACTS_EVENT)
    if not payloads:
        add(errors, "error", f"capability gateway requires event_type={CAPABILITY_GATEWAY_FACTS_EVENT}", "events")
        return

    required_components = {
        "client.runtime.host-capability-gateway",
        "input.filter.user-message-preflight",
        "prewrite.gate.approved-task-worktree",
        "preflight.interceptor.raw-shell-coverage",
    }
    valid = False
    invalid_event_ids: list[str] = []
    for event_id, payload in payloads:
        components = payload.get("runtime_adapter_components")
        component_set = set(str(item) for item in components) if isinstance(components, list) else set()
        if str(payload.get("capability_fact_kind") or "").strip() != "registration":
            invalid_event_ids.append(event_id)
            continue
        if str(payload.get("control_layer") or "").strip() != "plugin":
            invalid_event_ids.append(event_id)
            continue
        if str(payload.get("enforcement_level") or "").strip() != "audit_only":
            invalid_event_ids.append(event_id)
            continue
        if payload.get("hard_enforcement_available") is not False:
            invalid_event_ids.append(event_id)
            continue
        if payload.get("registration_event") is not True:
            invalid_event_ids.append(event_id)
            continue
        if payload.get("invocation_telemetry_required") is not True:
            invalid_event_ids.append(event_id)
            continue
        if str(payload.get("shell_control_layer") or "").strip() != "plugin-command-wrapper":
            invalid_event_ids.append(event_id)
            continue
        if str(payload.get("shell_enforcement_scope") or "").strip() != "governed_commands_only":
            invalid_event_ids.append(event_id)
            continue
        if payload.get("raw_host_shell_interception") is not False:
            invalid_event_ids.append(event_id)
            continue
        if not str(payload.get("residual_risk") or "").strip():
            invalid_event_ids.append(event_id)
            continue
        if payload.get("lifecycle_input_filter_enforced") is not True:
            invalid_event_ids.append(event_id)
            continue
        if not str(payload.get("prewrite_runtime_adapter") or "").strip():
            invalid_event_ids.append(event_id)
            continue
        if not required_components.issubset(component_set):
            invalid_event_ids.append(event_id)
            continue
        if str(payload.get("shell_enforcement_mode") or "").strip() != "non-invasive-command-proxy":
            invalid_event_ids.append(event_id)
            continue
        if str(payload.get("profile_policy") or "").strip() != "no_profile":
            invalid_event_ids.append(event_id)
            continue
        if payload.get("profile_touched") is not False:
            invalid_event_ids.append(event_id)
            continue
        if str(payload.get("user_shell_impact") or "").strip() != "none":
            invalid_event_ids.append(event_id)
            continue
        valid = True
        break
    if not valid:
        add(
            errors,
            "error",
            (
                "capability-gateway facts must record plugin registration/audit boundary fields "
                "(capability_fact_kind=registration, control_layer=plugin, enforcement_level=audit_only, "
                "hard_enforcement_available=false, registration_event=true, "
                "invocation_telemetry_required=true, shell_control_layer=plugin-command-wrapper, "
                "shell_enforcement_scope=governed_commands_only, raw_host_shell_interception=false, "
                "residual_risk), lifecycle_input_filter_enforced=true, prewrite runtime adapter, "
                "required runtime_adapter_components, "
                "shell_enforcement_mode=non-invasive-command-proxy, profile_policy=no_profile, "
                "profile_touched=false, and user_shell_impact=none"
            ),
            "events",
            ", ".join(invalid_event_ids),
        )
    else:
        add(notes, "note", f"capability gateway facts present: {CAPABILITY_GATEWAY_FACTS_EVENT}", "events")


def validate_analysis_contract_preflight(
    con: sqlite3.Connection,
    task: sqlite3.Row,
    task_types: set[str],
    event: str,
    errors: list[Finding],
    notes: list[Finding],
) -> None:
    """Require analysis contract facts before write-intent for modifying or medium/large tasks."""
    task_size = str(task["task_size"] or "").strip().lower()
    if not (task_types & MUTATING_TASK_TYPES or task_size in {"medium", "large"}):
        return
    payloads = event_payloads(con, task["task_id"], ANALYSIS_CONTRACT_EVENT)
    if not payloads:
        add(errors, "error", f"analysis contract requires event_type={ANALYSIS_CONTRACT_EVENT}", "events")
        return
    required_fields = {"analysis_summary", "scope", "non_goals", "risks", "acceptance"}
    valid = False
    missing_event_ids: list[str] = []
    for event_id, payload in payloads:
        present = {k for k in required_fields if str(payload.get(k, "")).strip()}
        missing = required_fields - present
        if missing:
            missing_event_ids.append(f"{event_id}:missing={','.join(sorted(missing))}")
            continue
        contract_fields = {k: str(payload.get(k, "")).strip() for k in required_fields}
        if any(not v for v in contract_fields.values()):
            missing_event_ids.append(f"{event_id}:empty_values")
            continue
        valid = True
        break
    if not valid:
        add(
            errors,
            "error",
            f"analysis contract must include summary, scope, non-goals, risks, and acceptance",
            "events",
            ", ".join(missing_event_ids) if missing_event_ids else "no payloads",
        )
    else:
        add(notes, "note", f"analysis contract facts present: {ANALYSIS_CONTRACT_EVENT}", "events")


def validate_history_requirement_recovery(
    con: sqlite3.Connection,
    task: sqlite3.Row,
    task_types: set[str],
    errors: list[Finding],
    notes: list[Finding],
) -> None:
    """Require repeated user requirements to be recovered or explicitly scoped out."""
    if not (task_types & {"correction", "rules-script", "multi-agent", "long-running"}):
        return
    payloads = event_payloads(con, task["task_id"], HISTORY_REQUIREMENT_RECOVERY_EVENT)
    if not payloads:
        add(errors, "error", f"history requirement recovery requires event_type={HISTORY_REQUIREMENT_RECOVERY_EVENT}", "events")
        return
    valid = False
    invalid_event_ids: list[str] = []
    for event_id, payload in payloads:
        sources = payload.get("history_sources")
        recovered = payload.get("recovered_requirements")
        not_adopted = payload.get("not_adopted_requirements")
        no_history_reason = str(payload.get("no_history_source_reason") or "").strip()
        no_action_reason = str(payload.get("no_action_reason") or "").strip()
        if isinstance(sources, list) and sources and isinstance(recovered, list) and recovered and isinstance(not_adopted, list):
            valid = True
            break
        if no_history_reason and no_action_reason:
            valid = True
            break
        invalid_event_ids.append(event_id)
    if not valid:
        add(
            errors,
            "error",
            (
                "history requirement recovery must record history_sources plus non-empty recovered_requirements "
                "and not_adopted_requirements, or no_history_source_reason plus no_action_reason"
            ),
            "events",
            ", ".join(invalid_event_ids),
        )
    else:
        add(notes, "note", f"history requirement recovery facts present: {HISTORY_REQUIREMENT_RECOVERY_EVENT}", "events")


def validate_readonly_side_effect_policy(
    con: sqlite3.Connection,
    task: sqlite3.Row,
    task_types: set[str],
    errors: list[Finding],
    notes: list[Finding],
) -> None:
    """Require explicit readonly and DB-write side-effect boundaries."""
    task_size = str(task["task_size"] or "").strip().lower()
    if not (task_types & MUTATING_TASK_TYPES or task_size in {"medium", "large"}):
        return
    payloads = event_payloads(con, task["task_id"], READONLY_SIDE_EFFECT_EVENT)
    if not payloads:
        add(errors, "error", f"readonly side-effect policy requires event_type={READONLY_SIDE_EFFECT_EVENT}", "events")
        return
    valid = False
    invalid_event_ids: list[str] = []
    for event_id, payload in payloads:
        required = (
            "readonly_contract",
            "db_write_allowed",
            "record_state_allowed",
            "side_effect_class",
            "dry_run_supported",
        )
        if any(field not in payload for field in required):
            invalid_event_ids.append(event_id)
            continue
        if payload.get("readonly_contract") is True:
            if payload.get("db_write_allowed") is True or payload.get("record_state_allowed") is True:
                invalid_event_ids.append(event_id)
                continue
        valid = True
        break
    if not valid:
        add(
            errors,
            "error",
            (
                "readonly side-effect policy must record readonly_contract, db_write_allowed, "
                "record_state_allowed, side_effect_class, and dry_run_supported; readonly tasks cannot allow DB writes"
            ),
            "events",
            ", ".join(invalid_event_ids),
        )
    else:
        add(notes, "note", f"readonly side-effect policy facts present: {READONLY_SIDE_EFFECT_EVENT}", "events")


def validate_discovered_issue_recording(
    con: sqlite3.Connection,
    task: sqlite3.Row,
    task_types: set[str],
    event: str,
    errors: list[Finding],
    notes: list[Finding],
) -> None:
    """Require final-output to record newly discovered issues or explicit no-action."""
    if event != "final":
        return
    task_size = str(task["task_size"] or "").strip().lower()
    if not (task_types & MUTATING_TASK_TYPES or task_size in {"medium", "large"}):
        return

    payloads = event_payloads(con, task["task_id"], DISCOVERED_ISSUE_RECORDING_EVENT)
    if not payloads:
        add(
            errors,
            "error",
            f"final output requires event_type={DISCOVERED_ISSUE_RECORDING_EVENT}",
            "events",
        )
        return

    valid_destinations = {
        "task-record",
        "task-queue",
        "framework-debt",
        "correction",
        "pending",
        "no-action",
    }
    valid = False
    invalid_event_ids: list[str] = []
    for event_id, payload in payloads:
        issues = payload.get("issues")
        if not isinstance(issues, list):
            invalid_event_ids.append(event_id)
            continue
        issue_rows = issues or [{"destination": payload.get("no_issue_policy", "no-action")}]
        missing = False
        for issue in issue_rows:
            if not isinstance(issue, dict):
                missing = True
                break
            destination = str(issue.get("destination") or "").strip()
            if destination not in valid_destinations:
                missing = True
                break
            if destination != "no-action" and not str(issue.get("record_ref") or "").strip():
                missing = True
                break
        if not missing:
            valid = True
            break
        invalid_event_ids.append(event_id)
    if not valid:
        add(
            errors,
            "error",
            "discovered issue recording must list issues with destination and record_ref, or explicit no-action",
            "events",
            ", ".join(invalid_event_ids),
        )
    else:
        add(notes, "note", f"discovered issue recording facts present: {DISCOVERED_ISSUE_RECORDING_EVENT}", "events")


def validate_agent_dispatch_brief(
    con: sqlite3.Connection,
    task: sqlite3.Row,
    task_types: set[str],
    errors: list[Finding],
    notes: list[Finding],
) -> None:
    """Require structured Agent Brief facts for multi-agent dispatch."""
    if "multi-agent" not in task_types:
        return
    payloads = event_payloads(con, task["task_id"], AGENT_DISPATCH_BRIEF_EVENT)
    if not payloads:
        add(errors, "error", f"multi-agent dispatch requires event_type={AGENT_DISPATCH_BRIEF_EVENT}", "events")
        return
    required = ("task_id", "worktree_path", "write_scope", "forbidden_paths", "validation_commands", "return_capsule")
    valid = False
    invalid_event_ids: list[str] = []
    for event_id, payload in payloads:
        missing = [field for field in required if not payload.get(field)]
        if missing:
            invalid_event_ids.append(event_id)
            continue
        if not isinstance(payload.get("write_scope"), list) or not isinstance(payload.get("forbidden_paths"), list):
            invalid_event_ids.append(event_id)
            continue
        if not isinstance(payload.get("validation_commands"), list):
            invalid_event_ids.append(event_id)
            continue
        valid = True
        break
    if not valid:
        add(
            errors,
            "error",
            "agent dispatch brief must include task_id, worktree_path, write_scope, forbidden_paths, validation_commands, and return_capsule",
            "events",
            ", ".join(invalid_event_ids),
        )
    else:
        add(notes, "note", f"agent dispatch brief facts present: {AGENT_DISPATCH_BRIEF_EVENT}", "events")


def payload_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def payload_contains(value: Any, patterns: tuple[str, ...]) -> bool:
    lowered = payload_text(value).lower()
    return any(pattern.lower() in lowered for pattern in patterns)


def payload_nonempty(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return bool(value)
    return value is not None


def review_result_is_pass(value: Any) -> bool:
    text = payload_text(value).lower()
    if not any(pattern in text for pattern in ("pass", "passed", "approved", "通过", "复测通过")):
        return False
    if any(pattern in text for pattern in ("fail", "failed", "blocked", "不通过", "未通过", "阻塞")) and not any(
        pattern in text for pattern in ("retest pass", "retest passed", "复测通过", "整改后通过", "最终通过")
    ):
        return False
    return True


def review_items_resolved(value: Any) -> bool:
    if value in (None, "", [], ()):
        return True
    if isinstance(value, list):
        for item in value:
            if not isinstance(item, dict):
                return False
            status = str(item.get("status") or "").strip().lower()
            if status not in {"resolved", "closed", "follow_up", "follow-up", "no_action", "none"}:
                return False
        return True
    text = payload_text(value).lower()
    if any(pattern in text for pattern in ("unresolved", "blocking", "未处理", "未解决", "阻塞")) and not any(
        pattern in text for pattern in ("resolved", "closed", "follow", "已处理", "已关闭", "已记录")
    ):
        return False
    return any(pattern in text for pattern in ("none", "no unresolved", "resolved", "closed", "无", "已处理", "已关闭"))


def agent_review_errors(review: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    for field in ("executor_agent", "executor_client_type", "reviewer_agent", "reviewer_client_type"):
        if not str(review.get(field) or "").strip():
            issues.append(field)
    executor = str(review.get("executor_agent") or "").strip().lower()
    reviewer = str(review.get("reviewer_agent") or "").strip().lower()
    executor_client = str(review.get("executor_client_type") or "").strip().lower()
    reviewer_client = str(review.get("reviewer_client_type") or "").strip().lower()
    if executor and reviewer and executor == reviewer and executor_client == reviewer_client:
        issues.append("independent_reviewer")
    if not str(review.get("reviewed_task_id") or review.get("reviewed_leaf_id") or "").strip():
        issues.append("reviewed_task_or_leaf_id")
    if not review_result_is_pass(review.get("decision")):
        issues.append("decision")

    lifecycle = review.get("lifecycle_fact_check")
    if not isinstance(lifecycle, dict):
        issues.append("lifecycle_fact_check")
    else:
        required_lifecycle = (
            "task_queue_lifecycle",
            "task_record_status",
            "requirements_triggers_outputs_worktrees_validations_events",
            "final_gate",
            "worktree_live_state",
            "branch_head_dirty_status",
            "validation_results",
            "telemetry_raw_shell_gap",
            "active_pending_state",
        )
        missing = [key for key in required_lifecycle if not payload_nonempty(lifecycle.get(key))]
        issues.extend(f"lifecycle_fact_check.{key}" for key in missing)
        if payload_contains(lifecycle, ("stale", "dirty-unexplained", "missing", "blocked", "fail", "不通过", "阻塞")) and not payload_contains(
            lifecycle,
            ("clean", "passed", "pass", "none", "no gap", "通过"),
        ):
            issues.append("lifecycle_fact_check.blocking_state")

    commit_status = review.get("commit_status_check")
    if not isinstance(commit_status, dict):
        issues.append("commit_status_check")
    else:
        for key in ("commit", "merge", "push"):
            if not payload_nonempty(commit_status.get(key)):
                issues.append(f"commit_status_check.{key}")

    if not review_items_resolved(review.get("unhandled_items")):
        issues.append("unhandled_items")
    if not review_items_resolved(review.get("low_quality_items")):
        issues.append("low_quality_items")
    if not payload_nonempty(review.get("evidence")):
        issues.append("evidence")
    if not payload_nonempty(review.get("remediation_guidance")):
        issues.append("remediation_guidance")
    if not payload_nonempty(review.get("retest_plan")):
        issues.append("retest_plan")
    if not review_result_is_pass(review.get("retest_result")):
        issues.append("retest_result")
    return issues


def validate_agent_review_result(
    con: sqlite3.Connection,
    task: sqlite3.Row,
    task_types: set[str],
    event: str,
    errors: list[Finding],
    notes: list[Finding],
) -> None:
    """Require final cross-client Agent review results for multi-agent work."""
    if event != "final" or "multi-agent" not in task_types:
        return
    payloads = event_payloads(con, task["task_id"], AGENT_REVIEW_RESULT_EVENT)
    if not payloads:
        add(errors, "error", f"multi-agent final gate requires event_type={AGENT_REVIEW_RESULT_EVENT}", "events")
        return

    valid = False
    invalid_event_ids: list[str] = []
    for event_id, payload in payloads:
        reviews = payload.get("reviews")
        if not isinstance(reviews, list) or not reviews:
            invalid_event_ids.append(f"{event_id}:reviews")
            continue
        event_errors: list[str] = []
        for index, review in enumerate(reviews):
            if not isinstance(review, dict):
                event_errors.append(f"reviews[{index}]")
                continue
            review_issues = agent_review_errors(review)
            event_errors.extend(f"reviews[{index}].{issue}" for issue in review_issues)
        if event_errors:
            invalid_event_ids.append(f"{event_id}:{','.join(event_errors)}")
            continue
        valid = True
        break

    if not valid:
        add(
            errors,
            "error",
            (
                "agent review result must use reviews[] with executor_agent, executor_client_type, reviewer_agent, "
                "reviewer_client_type, reviewed_task_id or reviewed_leaf_id, pass decision, lifecycle_fact_check, "
                "commit_status_check, resolved unhandled/low_quality items, evidence, remediation_guidance, "
                "retest_plan, and passing retest_result"
            ),
            "events",
            ", ".join(invalid_event_ids),
        )
    else:
        add(notes, "note", f"agent review result facts present: {AGENT_REVIEW_RESULT_EVENT}", "events")


def validate_design_package(
    con: sqlite3.Connection,
    task: sqlite3.Row,
    task_types: set[str],
    errors: list[Finding],
    notes: list[Finding],
) -> None:
    """Validate DB-backed design handoff/review packages.

    A design-only task must have a typed design package. Any task that records
    this event must use the same complete payload so downstream executor and
    reviewer agents can rely on stable keys instead of prose summaries.
    """
    payloads = event_payloads(con, task["task_id"], DESIGN_PACKAGE_EVENT)
    if not payloads:
        if DESIGN_ONLY_TASK_TYPE in task_types:
            add(errors, "error", f"design-only tasks require event_type={DESIGN_PACKAGE_EVENT}", "events")
        return

    valid = False
    invalid_event_ids: list[str] = []
    for event_id, payload in payloads:
        payload_errors = design_package_errors(payload)
        if payload_errors:
            invalid_event_ids.append(f"{event_id}:{','.join(payload_errors)}")
            continue
        valid = True
        break
    if not valid:
        add(
            errors,
            "error",
            (
                "design package event is incomplete; required fields are "
                + ", ".join(DESIGN_PACKAGE_REQUIRED_FIELDS)
            ),
            "events",
            "; ".join(invalid_event_ids),
        )
    else:
        add(notes, "note", f"design package facts present: {DESIGN_PACKAGE_EVENT}", "events")


def validate_capability_gateway_facts(
    con: sqlite3.Connection,
    task: sqlite3.Row,
    task_types: set[str],
    event: str,
    db: Path,
    errors: list[Finding],
    notes: list[Finding],
) -> None:
    """Final gate must verify governed shell invocation evidence, not prose claims.

    The capability-gateway event is a plugin registration/audit fact. It cannot
    prove host-native raw shell prevention. Final closeout therefore checks the
    shell-proxy-usage analysis event for governed-command telemetry or an
    explicit exception.
    """
    task_size = str(task["task_size"] or "").strip().lower()
    if not (task_types & MUTATING_TASK_TYPES or task_size in {"medium", "large"}):
        return
    # Only enforce at final gate
    if event != "final":
        return
    # Check shell-proxy-usage event has telemetry evidence
    payloads = event_payloads(con, task["task_id"], SHELL_PROXY_USAGE_EVENT)
    if not payloads:
        add(errors, "error", f"final gate requires governed shell invocation event_type={SHELL_PROXY_USAGE_EVENT}", "events")
        return
    has_proxy_evidence = False
    for _event_id, payload in payloads:
        used_proxy = payload.get("used_proxy")
        telemetry_evidence = str(payload.get("telemetry_evidence") or payload.get("proxy_invocation_id") or "").strip()
        exception_reason = str(payload.get("exception_reason") or "").strip()
        if used_proxy is True and telemetry_evidence:
            has_proxy_evidence = True
            break
        if used_proxy is not True and exception_reason:
            # Exception recorded — allow but note the gap
            has_proxy_evidence = True
            break
    if has_proxy_evidence:
        add(notes, "note", f"governed shell invocation evidence present: {SHELL_PROXY_USAGE_EVENT}", "events")
    else:
        add(
            errors,
            "error",
            "final gate requires governed shell invocation evidence: used_proxy=true with telemetry_evidence, or exception_reason",
            "events",
        )
    validate_capability_gateway_event(con, task, errors, notes)


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

    validate_input_filter_preflight(con, task_id, errors, warnings, notes)
    validate_command_compression_preflight(con, task, task_types, errors, notes)
    validate_scope_classification_preflight(con, task, task_types, errors, notes)
    validate_plan_approval_boundary(con, task, task_types, errors, notes)
    validate_user_claim_validation(con, task, task_types, errors, notes)
    validate_state_artifact_ownership(con, task, task_types, errors, notes)
    validate_patch_preflight(con, task, task_types, errors, notes)
    validate_prewrite_runtime_adapter(con, task, task_types, event, errors, notes)
    validate_agent_decision(con, task, task_types, event, errors, notes)
    validate_data_confirmation(con, task, task_types, errors, notes)
    validate_shell_proxy_usage(con, task, task_types, event, errors, notes)
    validate_history_requirement_recovery(con, task, task_types, errors, notes)
    validate_readonly_side_effect_policy(con, task, task_types, errors, notes)
    validate_discovered_issue_recording(con, task, task_types, event, errors, notes)
    validate_agent_dispatch_brief(con, task, task_types, errors, notes)
    validate_agent_review_result(con, task, task_types, event, errors, notes)
    validate_design_package(con, task, task_types, errors, notes)
    validate_analysis_contract_preflight(con, task, task_types, event, errors, notes)
    validate_capability_gateway_facts(con, task, task_types, event, db, errors, notes)
    validate_command_error_analysis(con, task, task_types, event, errors, notes)

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
            add(errors, "error", "mutating tasks require worktree evidence before write-intent", "worktrees")
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
    if "docs" in task_types:
        has_doc_validation = any(
            "validate-doc" in row["command"] or "doc-index" in row["command"] for row in validations
        )
        has_doc_impact = any(row["event_type"] == DOC_IMPACT_EVENT for row in rows(con, "events", task_id))
        if not has_doc_validation:
            add(warnings, "warning", "docs task has no validate-doc/doc-index validation row", "validations")
        if event == "final" and not has_doc_impact:
            add(warnings, "warning", "docs task has no doc-impact.analysis event from changed-path bubbling", "events")
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
        if args.command == "describe":
            if args.sample:
                payload = build_sample_payload()
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            elif args.format == "json":
                descriptor = build_schema_descriptor()
                print(json.dumps(descriptor, ensure_ascii=False, indent=2))
            else:
                print(format_compact_text_descriptor())
            return 0

        if args.command == "status":
            if not path.exists():
                summary = empty_summary(path, args.task_id)
            else:
                con = connect(path, create=False)
                summary = task_summary(con, args.task_id)
            print_json(summary) if args.format == "json" else print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 0
        if args.command == "evidence-graph":
            if not path.exists():
                report = {
                    "schema_version": 1,
                    "task_id": args.task_id,
                    "status": "fail",
                    "task_present": False,
                    "linked_counts": {},
                    "optional_task_scoped_counts": {},
                    "telemetry": {},
                    "gaps": ["db:missing"],
                    "correlation_spine": {"root": "task_id"},
                }
            else:
                con = connect(path, create=False)
                report = build_evidence_graph(con, args.task_id, include_telemetry=args.include_telemetry)
            print_json(report) if args.format == "json" else print(render_evidence_graph(report))
            return 0 if report.get("status") == "pass" else 1

        create_db = args.command in {"init", "apply"}
        con = connect(path, create=create_db)
        if create_db:
            init_db(con)
        else:
            _ensure_time_columns(con)

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
        if args.command == "append-event":
            payload = load_json_object_from_args(root, args.payload_json, args.payload_file)
            event_id = append_event_row(
                con,
                task_id=args.task_id,
                event_type=args.event_type,
                payload=payload,
                event_id=args.event_id,
            )
            result = {"db": str(path), "task_id": args.task_id, "event_id": event_id, "appended": True}
            print_json(result) if args.format == "json" else print(f"Appended task event: {event_id}")
            return 0
        if args.command == "design-package":
            payload = require_design_package_payload(
                load_json_object_from_args(root, args.payload_json, args.payload_file)
            )
            event_id = append_event_row(
                con,
                task_id=args.task_id,
                event_type=DESIGN_PACKAGE_EVENT,
                payload=payload,
                event_id=args.event_id,
            )
            result = {"db": str(path), "task_id": args.task_id, "event_id": event_id, "appended": True}
            print_json(result) if args.format == "json" else print(f"Appended design package event: {event_id}")
            return 0
        if args.command == "append-validation":
            validation_id = append_validation_row(con, args)
            result = {"db": str(path), "task_id": args.task_id, "validation_id": validation_id, "appended": True}
            print_json(result) if args.format == "json" else print(f"Appended validation evidence: {validation_id}")
            return 0
        if args.command == "append-worktree":
            worktree_id = append_worktree_row(con, args)
            result = {"db": str(path), "task_id": args.task_id, "worktree_id": worktree_id, "appended": True}
            print_json(result) if args.format == "json" else print(f"Appended worktree evidence: {worktree_id}")
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
