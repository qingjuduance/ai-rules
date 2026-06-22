#!/usr/bin/env python3
"""Unified execution telemetry stored in the project SQLite state DB."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from ai_client_governance.common.time_utils import now_iso as utc_now, parse_dt
from pathlib import Path
from typing import Any
from uuid import uuid4

from ai_client_governance.common import cli_arguments as common_cli_args
from ai_client_governance.common.paths import host_project_root
from ai_client_governance.records import state_store


SCHEMA_VERSION = 2
SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API[_-]?KEY|ACCESS[_-]?KEY)[A-Z0-9_]*)=([^\s]+)"
)
SENSITIVE_OPTION_EQ = re.compile(
    r"(?i)(--?(?:token|secret|password|passwd|api[-_]?key|access[-_]?key|credential|auth))(=)([^\s]+)"
)
SENSITIVE_OPTION_SPACE = re.compile(
    r"(?i)(--?(?:token|secret|password|passwd|api[-_]?key|access[-_]?key|credential|auth))(\s+)([^\s]+)"
)
SENSITIVE_QUERY_PARAM = re.compile(
    r"(?i)([?&](?:token|secret|password|passwd|api[-_]?key|access[-_]?key|credential|auth)=)([^&#\s]+)"
)
SENSITIVE_KEY = re.compile(
    r"(?i)(token|secret|password|passwd|api[-_]?key|access[-_]?key|credential|auth|authorization)"
)
TRACE_ID_HEX = re.compile(r"^[0-9a-f]{32}$")
SPAN_ID_HEX = re.compile(r"^[0-9a-f]{16}$")
TRACEPARENT_RE = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")


@dataclass(frozen=True)
class TelemetrySpan:
    span_id: str
    trace_id: str
    parent_span_id: str
    task_id: str
    task_tracking: str
    name: str
    span_kind: str
    subject_type: str
    subject_redacted: str
    subject_hash: str
    cwd: str
    scope_kind: str
    scope_reason: str
    scope_paths: list[str]
    phase: str
    event_type: str
    status: str
    exit_code: int | None
    started_at: str
    ended_at: str
    duration_ms: int | None
    cached: bool
    cache_key: str
    cache_reason: str
    adapter_enforcement: str
    final_gate: bool
    task_types: list[str]
    attempt: int | None
    source: str
    summary: str
    attributes: dict[str, Any]


@dataclass(frozen=True)
class TraceContext:
    trace_id: str
    span_id: str
    parent_span_id: str = ""
    trace_flags: str = "01"
    tracestate: str = ""

    @property
    def traceparent(self) -> str:
        return f"00-{self.trace_id}-{self.span_id}-{self.trace_flags}"


def new_trace_id() -> str:
    return uuid4().hex


def new_span_id() -> str:
    return uuid4().hex[:16]


def parse_traceparent(value: str) -> TraceContext | None:
    match = TRACEPARENT_RE.fullmatch(str(value or "").strip().lower())
    if not match:
        return None
    return TraceContext(trace_id=match.group(1), span_id=match.group(2), trace_flags=match.group(3))


def new_trace_context(
    *,
    trace_id: str = "",
    parent_span_id: str = "",
    span_id: str = "",
    tracestate: str = "",
) -> TraceContext:
    normalized_trace, _trace_derived = normalized_trace_id(trace_id or new_trace_id())
    normalized_span, _span_derived = normalized_span_id(span_id or new_span_id())
    parent, _parent_derived = normalized_span_id(parent_span_id) if parent_span_id else ("", False)
    return TraceContext(
        trace_id=normalized_trace,
        span_id=normalized_span,
        parent_span_id=parent,
        tracestate=tracestate,
    )


def trace_context_from_env(env: dict[str, str] | None = None) -> TraceContext:
    current = env if env is not None else os.environ
    parsed = parse_traceparent(current.get("TRACEPARENT", ""))
    if parsed:
        return TraceContext(
            trace_id=parsed.trace_id,
            span_id=parsed.span_id,
            trace_flags=parsed.trace_flags,
            tracestate=current.get("TRACESTATE", ""),
        )
    return new_trace_context(trace_id=current.get("CODEX_TRACE_ID", ""))


def env_for_child(
    *,
    trace_id: str = "",
    parent_span_id: str = "",
    tracestate: str = "",
) -> dict[str, str]:
    context = new_trace_context(trace_id=trace_id, parent_span_id=parent_span_id, tracestate=tracestate)
    child = {
        "TRACEPARENT": context.traceparent,
        "CODEX_TRACE_ID": trace_id or context.trace_id,
        "CODEX_PARENT_INVOCATION_ID": parent_span_id or context.span_id,
    }
    if context.tracestate:
        child["TRACESTATE"] = context.tracestate
    return child


def db_path(root: Path, override: str | None = None) -> Path:
    if override:
        return state_store.db_path(root, override)
    return state_store.db_path(host_project_root(root), None)


def connect(root: Path, override: str | None = None) -> sqlite3.Connection:
    path = db_path(root, override)
    con = state_store.connect(path)
    init_db(con)
    return con


def init_db(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        DROP VIEW IF EXISTS command_metrics_by_name;
        DROP TABLE IF EXISTS command_events;
        DROP TABLE IF EXISTS command_spans;
        """
    )
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS execution_spans (
            span_id TEXT PRIMARY KEY,
            trace_id TEXT NOT NULL DEFAULT '',
            parent_span_id TEXT NOT NULL DEFAULT '',
            task_id TEXT NOT NULL DEFAULT '',
            task_tracking TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL DEFAULT '',
            span_kind TEXT NOT NULL DEFAULT '',
            subject_type TEXT NOT NULL DEFAULT '',
            subject_redacted TEXT NOT NULL DEFAULT '',
            subject_hash TEXT NOT NULL DEFAULT '',
            cwd TEXT NOT NULL DEFAULT '',
            scope_kind TEXT NOT NULL DEFAULT '',
            scope_reason TEXT NOT NULL DEFAULT '',
            scope_paths_json TEXT NOT NULL DEFAULT '[]',
            phase TEXT NOT NULL DEFAULT '',
            event_type TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT '',
            exit_code INTEGER,
            started_at TEXT NOT NULL DEFAULT '',
            ended_at TEXT NOT NULL DEFAULT '',
            duration_ms INTEGER,
            cached INTEGER NOT NULL DEFAULT 0,
            cache_key TEXT NOT NULL DEFAULT '',
            cache_reason TEXT NOT NULL DEFAULT '',
            adapter_enforcement TEXT NOT NULL DEFAULT '',
            final_gate INTEGER NOT NULL DEFAULT 0,
            task_types_json TEXT NOT NULL DEFAULT '[]',
            attempt INTEGER,
            source TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL DEFAULT '',
            attributes_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS execution_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            span_id TEXT NOT NULL,
            trace_id TEXT NOT NULL DEFAULT '',
            event_name TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT '',
            timestamp TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL DEFAULT '{}',
            source_command TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY(span_id) REFERENCES execution_spans(span_id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_execution_spans_trace
            ON execution_spans(trace_id, started_at, ended_at);
        CREATE INDEX IF NOT EXISTS idx_execution_spans_task
            ON execution_spans(task_id, started_at, ended_at);
        CREATE INDEX IF NOT EXISTS idx_execution_spans_kind_name
            ON execution_spans(span_kind, name, started_at, ended_at);
        CREATE INDEX IF NOT EXISTS idx_execution_spans_subject_hash
            ON execution_spans(subject_hash, started_at, ended_at);
        CREATE INDEX IF NOT EXISTS idx_execution_events_trace
            ON execution_events(trace_id, timestamp);

        CREATE VIEW IF NOT EXISTS execution_metrics_by_name AS
            SELECT
                span_kind,
                name,
                count(*) AS span_count,
                sum(CASE WHEN status = 'failed' OR coalesce(exit_code, 0) != 0 THEN 1 ELSE 0 END) AS failed_count,
                sum(CASE WHEN cached = 1 THEN 1 ELSE 0 END) AS cache_hit_count,
                avg(duration_ms) AS avg_duration_ms,
                max(duration_ms) AS max_duration_ms
            FROM execution_spans
            GROUP BY span_kind, name;
        """
    )
    con.execute(
        "INSERT INTO meta(key, value) VALUES('execution_telemetry_schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )
    con.commit()


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def decode_json(value: str, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except json.JSONDecodeError:
        return fallback


def redact_sensitive_text(value: str) -> str:
    redacted = SENSITIVE_ASSIGNMENT.sub(r"\1=<redacted>", value or "")
    redacted = SENSITIVE_OPTION_EQ.sub(r"\1\2<redacted>", redacted)
    redacted = SENSITIVE_OPTION_SPACE.sub(r"\1\2<redacted>", redacted)
    return SENSITIVE_QUERY_PARAM.sub(r"\1<redacted>", redacted)


def redact_subject(subject: str) -> str:
    redacted = redact_sensitive_text(subject or "")
    return re.sub(r"\s+", " ", redacted).strip()


def sanitize_value(value: Any, key: str = "") -> Any:
    if SENSITIVE_KEY.search(key or ""):
        if value in (None, ""):
            return value
        return "<redacted>"
    if isinstance(value, dict):
        return {str(item_key): sanitize_value(item_value, str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [sanitize_value(item, key) for item in value]
    if isinstance(value, tuple):
        return [sanitize_value(item, key) for item in value]
    if isinstance(value, str):
        return redact_sensitive_text(value)
    return value


def sanitize_mapping(value: dict[str, Any]) -> dict[str, Any]:
    return {str(key): sanitize_value(item, str(key)) for key, item in value.items()}


def subject_hash(subject: str) -> str:
    normalized = re.sub(r"\s+", " ", (subject or "").strip())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _command_contains_unstable_inline_python(command: str) -> bool:
    for match in re.finditer(r"(?i)(^|\s)(?P<exe>\"[^\"]+\"|'[^']+'|\S+)\s+-c(?=\s|$)", command or ""):
        exe = match.group("exe").strip("\"'")
        basename = exe.replace("\\", "/").rsplit("/", 1)[-1].lower()
        if basename in {"python", "python.exe", "python3", "python3.exe", "py", "py.exe"}:
            return True
    return False


def analyze_powershell_inline_command(command: str, *, command_file_used: bool = False) -> dict[str, Any]:
    """Return a small risk hint for command strings that are often misparsed inline."""
    if command_file_used:
        return {}
    text = command or ""
    lowered = text.lower()
    reasons: list[str] = []
    if _command_contains_unstable_inline_python(text) and not command_file_used:
        reasons.append("python-c-inline")
    if ("--payload-json" in lowered or "--metadata" in lowered) and any(char in text for char in "{}[]"):
        reasons.append("inline-json")
    if "`" in text:
        reasons.append("powershell-backtick")
    if ("'" in text and '"' in text) or text.count('"') >= 4:
        reasons.append("mixed-quotes")
    if re.search(r"\$[A-Za-z_][\w:]*\s*=", text) and (";" in text or "\n" in text):
        reasons.append("powershell-variable-assignment")
    if text.count(";") >= 3:
        reasons.append("many-statements")
    if not reasons:
        return {}
    return {
        "risk": "inline-command-quoting",
        "reasons": sorted(set(reasons)),
        "recommended_runner": "shell-adapter proxy-powershell --powershell-command-file",
        "preventive_rule": "Put complex PowerShell, python -c, JSON, mixed quotes, and multi-statement logic in a UTF-8 command file.",
    }


def classify_command_error(
    command: str,
    *,
    exit_code: int | None = None,
    parser_or_shell: str = "",
    command_file_used: bool = False,
    stderr_tail: str = "",
) -> dict[str, Any]:
    """Classify failed command spans into stable, reportable buckets."""
    if exit_code in (None, 0):
        return {}
    text = command or ""
    lowered = text.lower()
    parser = parser_or_shell or "unknown"
    category = "unclassified_command_failure"
    root_cause = "Command exited non-zero and no more specific failure pattern was recognized."
    corrected_command = "Inspect the failed command, classify the root cause, then retry once with a corrected command."
    preventive_rule = "Record command-error.analysis with failure_category, corrected_command, retry_count, and dedupe_key before another retry."
    retry_policy = "classify_before_retry"
    suggested_runner = "task-run plan/run or shell-adapter proxy-powershell"
    requires_command_file = False

    if _command_contains_unstable_inline_python(text):
        category = "python_c_inline_quoting"
        root_cause = "Inline python -c code is fragile through PowerShell and proxy quoting layers."
        corrected_command = "Move the Python snippet into a temporary .py file or execute it through a UTF-8 PowerShell command file."
        preventive_rule = "Do not pass quote-heavy python -c through inline PowerShell; use a file-backed command template."
        retry_policy = "rewrite_with_file_before_retry"
        suggested_runner = "shell-adapter proxy-powershell --powershell-command-file"
        requires_command_file = True
    elif ("--payload-json" in lowered or "--metadata" in lowered) and any(char in text for char in "{}[]"):
        category = "inline_json_quoting"
        root_cause = "Inline JSON is likely to be altered by shell argument parsing."
        corrected_command = "Write JSON to a UTF-8 file and pass the command's --payload-file, --metadata-file, or equivalent file option."
        preventive_rule = "Do not pass strict JSON as an inline PowerShell argument when a file option exists."
        retry_policy = "rewrite_with_file_before_retry"
        suggested_runner = "command-specific file argument"
        requires_command_file = True
    elif analyze_powershell_inline_command(text, command_file_used=command_file_used):
        category = "powershell_inline_complex_command"
        root_cause = "Complex inline PowerShell has variables, quotes, backticks, or many statements that are easy to misparse."
        corrected_command = "Put the full PowerShell body in a UTF-8 .ps1 command file and pass --powershell-command-file."
        preventive_rule = "Use command files for multi-statement PowerShell, variables plus separators, mixed quotes, and backticks."
        retry_policy = "rewrite_with_command_file_before_retry"
        suggested_runner = "shell-adapter proxy-powershell --powershell-command-file"
        requires_command_file = True
    elif re.search(
        r"(?i)(^|\s)(ai_client_governance\.py|scripts[\\/]ai_client_governance\.py|\.ai-client[\\/].+ai_client_governance\.py)",
        text,
    ) and exit_code == 2:
        category = "argparse_usage_error"
        root_cause = "Governance CLI rejected the argument order or option shape."
        corrected_command = "Run the subcommand --help, then place shared global options where that subparser accepts them."
        preventive_rule = "For nested governance commands, verify --help once and avoid moving global options after nested subcommands unless documented."
        retry_policy = "help_then_retry_once"
        suggested_runner = "governance CLI with verified option order"
    elif re.match(r"(?i)^\s*git(\.exe)?\s+", text):
        category = "git_command_failed"
        root_cause = "Git returned a non-zero exit code; this may be dirty state, missing ref, merge conflict, or branch safety policy."
        corrected_command = "Inspect git status, branch/ref existence, and merge state before retrying the specific git operation."
        preventive_rule = "Do not repeat failed Git writes blindly; record dirty/ref/conflict facts first."
        retry_policy = "inspect_git_state_before_retry"
        suggested_runner = "worktree-task or task-run with git state precheck"
    elif re.match(r"(?i)^\s*(cd|push-location|set-location)\b", text):
        category = "working_directory_command_failed"
        root_cause = "Directory change command failed or was used where tool cwd should be set explicitly."
        corrected_command = "Set the command working directory through the runner cwd argument instead of chaining cd."
        preventive_rule = "Prefer runner cwd/workdir over inline cd/Push-Location for single-command tool calls."
        retry_policy = "set_runner_cwd_before_retry"
        suggested_runner = "runner cwd/workdir"

    if stderr_tail:
        lowered_stderr = stderr_tail.lower()
        if "not recognized as the name of a cmdlet" in lowered_stderr:
            category = "powershell_command_not_found"
            root_cause = "PowerShell treated a token as a command, often because quoting stripped string delimiters."
            corrected_command = "Rebuild the command with a command file or quote-free argument list before retrying."
            retry_policy = "rewrite_before_retry"
            suggested_runner = "shell-adapter proxy-powershell --powershell-command-file"
            requires_command_file = True

    normalized_command = re.sub(r"\s+", " ", text).strip()
    dedupe_material = f"{category}\n{normalized_command}"
    return {
        "failure_category": category,
        "parser_or_shell": parser,
        "exit_code": exit_code,
        "root_cause": root_cause,
        "corrected_command": corrected_command,
        "retry_policy": retry_policy,
        "dedupe_key": subject_hash(dedupe_material)[:20],
        "preventive_rule": preventive_rule,
        "suggested_runner": suggested_runner,
        "requires_command_file": requires_command_file,
        "stderr_tail_available": bool(stderr_tail),
    }


def subject_from_event(event: dict[str, Any]) -> tuple[str, str]:
    if event.get("subject") not in (None, ""):
        return str(event.get("subject") or ""), str(event.get("subject_type") or "subject")
    if event.get("command") not in (None, ""):
        return str(event.get("command") or ""), "command"
    if event.get("url") not in (None, ""):
        return str(event.get("url") or ""), str(event.get("subject_type") or "http_url")
    if event.get("endpoint") not in (None, ""):
        return str(event.get("endpoint") or ""), str(event.get("subject_type") or "endpoint")
    return str(event.get("name") or ""), str(event.get("subject_type") or "operation")


def as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if value in (None, ""):
        return []
    return [str(value)]


def sanitized_event(event: dict[str, Any]) -> dict[str, Any]:
    payload = sanitize_mapping(dict(event))
    subject, subject_type = subject_from_event(event)
    if "command" in payload:
        payload["command"] = redact_subject(str(payload.get("command") or ""))
    if "subject" in payload:
        payload["subject"] = redact_subject(str(payload.get("subject") or ""))
    if "url" in payload:
        payload["url"] = redact_subject(str(payload.get("url") or ""))
    if "endpoint" in payload:
        payload["endpoint"] = redact_subject(str(payload.get("endpoint") or ""))
    payload["subject_type"] = subject_type
    payload["subject_hash"] = subject_hash(subject)
    return payload


def event_to_span(event: dict[str, Any]) -> TelemetrySpan:
    span_id = str(event.get("invocation_id") or event.get("span_id") or "")
    if not span_id:
        raise ValueError("telemetry event requires invocation_id or span_id")
    subject, subject_type = subject_from_event(event)
    redacted = redact_subject(subject)
    span_kind = str(
        event.get("span_kind")
        or ("command" if event.get("command") not in (None, "") else "")
        or event.get("event_type")
        or event.get("phase")
        or "operation"
    )
    attrs = {
        key: value
        for key, value in event.items()
        if key
        not in {
            "command",
            "invocation_id",
            "span_id",
            "trace_id",
            "parent_invocation_id",
            "parent_span_id",
            "task_id",
            "task_tracking",
            "name",
            "span_kind",
            "subject",
            "subject_type",
            "url",
            "endpoint",
            "status",
            "exit_code",
            "started_at",
            "ended_at",
            "duration_ms",
            "timestamp",
            "schema_version",
            "cwd",
            "source",
            "summary",
            "task_types",
            "phase",
            "event_type",
            "attempt",
            "final_gate",
            "cached",
            "cache_key",
            "cache_reason",
            "scope_kind",
            "scope_reason",
            "scope_paths",
            "adapter_enforcement",
            "task_node_id",
            "parent_task_node_id",
            "attributes",
        }
    }
    extension_attrs = event.get("attributes")
    if isinstance(extension_attrs, dict):
        attrs.update(extension_attrs)
    elif extension_attrs not in (None, ""):
        attrs["attributes"] = extension_attrs
    attrs = sanitize_mapping(attrs)
    return TelemetrySpan(
        span_id=span_id,
        trace_id=str(event.get("trace_id") or span_id),
        parent_span_id=str(event.get("parent_invocation_id") or event.get("parent_span_id") or ""),
        task_id=str(event.get("task_id") or ""),
        task_tracking=str(event.get("task_tracking") or ""),
        name=str(event.get("name") or "unknown"),
        span_kind=span_kind,
        subject_type=subject_type,
        subject_redacted=redacted,
        subject_hash=subject_hash(subject),
        cwd=str(event.get("cwd") or ""),
        scope_kind=str(event.get("scope_kind") or ""),
        scope_reason=str(event.get("scope_reason") or ""),
        scope_paths=as_list(event.get("scope_paths")),
        phase=str(event.get("phase") or ""),
        event_type=str(event.get("event_type") or ""),
        status=str(event.get("status") or ""),
        exit_code=as_int(event.get("exit_code")),
        started_at=str(event.get("started_at") or event.get("timestamp") or ""),
        ended_at=str(event.get("ended_at") or ""),
        duration_ms=as_int(event.get("duration_ms")),
        cached=bool(event.get("cached")),
        cache_key=str(event.get("cache_key") or ""),
        cache_reason=str(event.get("cache_reason") or ""),
        adapter_enforcement=str(event.get("adapter_enforcement") or ""),
        final_gate=bool(event.get("final_gate")),
        task_types=as_list(event.get("task_types")),
        attempt=as_int(event.get("attempt")),
        source=str(event.get("source") or ""),
        summary=str(event.get("summary") or ""),
        attributes=attrs,
    )


def append_event(
    root: Path,
    event: dict[str, Any],
    *,
    db: str | None = None,
    source_command: str = "ai_client_governance.py telemetry",
) -> Path:
    path = db_path(root, db)
    con = connect(root, db)
    span = event_to_span(event)
    payload = sanitized_event(event)
    timestamp = str(event.get("timestamp") or event.get("ended_at") or event.get("started_at") or utc_now())
    now = utc_now()
    terminal = span.status != "started"
    with con:
        con.execute(
            """
            INSERT INTO execution_spans(
                span_id, trace_id, parent_span_id, task_id, task_tracking, name, span_kind,
                subject_type, subject_redacted, subject_hash, cwd, scope_kind, scope_reason, scope_paths_json,
                phase, event_type, status, exit_code, started_at, ended_at, duration_ms,
                cached, cache_key, cache_reason, adapter_enforcement, final_gate,
                task_types_json, attempt, source, summary, attributes_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(span_id) DO UPDATE SET
                trace_id=excluded.trace_id,
                parent_span_id=excluded.parent_span_id,
                task_id=excluded.task_id,
                task_tracking=excluded.task_tracking,
                name=excluded.name,
                span_kind=excluded.span_kind,
                subject_type=excluded.subject_type,
                subject_redacted=excluded.subject_redacted,
                subject_hash=excluded.subject_hash,
                cwd=excluded.cwd,
                scope_kind=excluded.scope_kind,
                scope_reason=excluded.scope_reason,
                scope_paths_json=excluded.scope_paths_json,
                phase=excluded.phase,
                event_type=excluded.event_type,
                status=CASE WHEN ? THEN excluded.status ELSE execution_spans.status END,
                exit_code=CASE WHEN ? THEN excluded.exit_code ELSE execution_spans.exit_code END,
                started_at=CASE
                    WHEN execution_spans.started_at = '' THEN excluded.started_at
                    WHEN excluded.started_at = '' THEN execution_spans.started_at
                    ELSE execution_spans.started_at
                END,
                ended_at=CASE WHEN excluded.ended_at != '' THEN excluded.ended_at ELSE execution_spans.ended_at END,
                duration_ms=coalesce(excluded.duration_ms, execution_spans.duration_ms),
                cached=excluded.cached,
                cache_key=excluded.cache_key,
                cache_reason=excluded.cache_reason,
                adapter_enforcement=excluded.adapter_enforcement,
                final_gate=excluded.final_gate,
                task_types_json=excluded.task_types_json,
                attempt=coalesce(excluded.attempt, execution_spans.attempt),
                source=excluded.source,
                summary=excluded.summary,
                attributes_json=excluded.attributes_json,
                updated_at=excluded.updated_at
            """,
            (
                span.span_id,
                span.trace_id,
                span.parent_span_id,
                span.task_id,
                span.task_tracking,
                span.name,
                span.span_kind,
                span.subject_type,
                span.subject_redacted,
                span.subject_hash,
                span.cwd,
                span.scope_kind,
                span.scope_reason,
                encode(span.scope_paths),
                span.phase,
                span.event_type,
                span.status,
                span.exit_code,
                span.started_at,
                span.ended_at,
                span.duration_ms,
                int(span.cached),
                span.cache_key,
                span.cache_reason,
                span.adapter_enforcement,
                int(span.final_gate),
                encode(span.task_types),
                span.attempt,
                span.source,
                span.summary,
                encode(span.attributes),
                now,
                now,
                terminal,
                terminal,
            ),
        )
        con.execute(
            """
            INSERT INTO execution_events(span_id, trace_id, event_name, status, timestamp, payload_json, source_command, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                span.span_id,
                span.trace_id,
                span.event_type or span.phase or span.span_kind,
                span.status,
                timestamp,
                encode(payload),
                source_command,
                now,
            ),
        )
    return path


def read_events(
    root: Path,
    *,
    db: str | None = None,
    task_id: str = "",
    trace_id: str = "",
    since: str = "",
    until: str = "",
) -> list[dict[str, Any]]:
    con = connect(root, db)
    clauses: list[str] = []
    params: list[Any] = []
    if task_id:
        clauses.append("s.task_id = ?")
        params.append(task_id)
    if trace_id:
        clauses.append("e.trace_id = ?")
        params.append(trace_id)
    if since:
        clauses.append("e.timestamp >= ?")
        params.append(since)
    if until:
        clauses.append("e.timestamp <= ?")
        params.append(until)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    rows = con.execute(
        f"""
        SELECT e.payload_json
        FROM execution_events e
        JOIN execution_spans s ON s.span_id = e.span_id
        {where}
        ORDER BY e.timestamp, e.event_id
        """,
        params,
    ).fetchall()
    return [decode_json(row["payload_json"], {}) for row in rows]


def span_rows(
    root: Path,
    *,
    db: str | None = None,
    task_id: str = "",
    trace_id: str = "",
    since: str = "",
    until: str = "",
) -> list[dict[str, Any]]:
    con = connect(root, db)
    clauses: list[str] = []
    params: list[Any] = []
    if task_id:
        clauses.append("task_id = ?")
        params.append(task_id)
    if trace_id:
        clauses.append("trace_id = ?")
        params.append(trace_id)
    if since:
        clauses.append("coalesce(ended_at, started_at) >= ?")
        params.append(since)
    if until:
        clauses.append("coalesce(ended_at, started_at) <= ?")
        params.append(until)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    rows = con.execute(
        f"""
        SELECT *
        FROM execution_spans
        {where}
        ORDER BY coalesce(ended_at, started_at), span_id
        """,
        params,
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["scope_paths"] = decode_json(item.pop("scope_paths_json"), [])
        item["task_types"] = decode_json(item.pop("task_types_json"), [])
        item["attributes"] = decode_json(item.pop("attributes_json"), {})
        item["cached"] = bool(item["cached"])
        item["final_gate"] = bool(item["final_gate"])
        result.append(item)
    return result


def percentile(values: list[int], pct: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * pct)))
    return ordered[index]


def duration_stats(values: list[int]) -> dict[str, int | float | None]:
    return {
        "sum": sum(values),
        "avg": round(sum(values) / len(values), 2) if values else None,
        "p50": percentile(values, 0.50),
        "p95": percentile(values, 0.95),
        "max": max(values) if values else None,
    }


def command_error_from_span(span: dict[str, Any]) -> dict[str, Any]:
    attributes = span.get("attributes")
    attrs = attributes if isinstance(attributes, dict) else {}
    existing = attrs.get("command_error")
    if isinstance(existing, dict):
        return existing
    if span.get("status") == "failed" or span.get("exit_code") not in (None, 0):
        return classify_command_error(
            str(span.get("subject_redacted") or ""),
            exit_code=as_int(span.get("exit_code")),
            parser_or_shell=str(span.get("adapter_enforcement") or span.get("event_type") or "unknown"),
        )
    return {}


def inline_warning_from_span(span: dict[str, Any]) -> dict[str, Any]:
    attributes = span.get("attributes")
    attrs = attributes if isinstance(attributes, dict) else {}
    shell_adapter = attrs.get("shell_adapter")
    shell = shell_adapter if isinstance(shell_adapter, dict) else {}
    warning = shell.get("inline_command_warning")
    return warning if isinstance(warning, dict) else {}


def summarize_command_errors(
    command_errors: list[dict[str, Any]],
    *,
    failure_count: int,
    top: int = 10,
) -> dict[str, Any]:
    failure_category_counts = Counter(
        str(item.get("failure_category") or "unclassified_command_failure") for item in command_errors
    )
    unclassified_count = int(failure_category_counts.get("unclassified_command_failure", 0))
    unclassified_count += max(0, failure_count - len(command_errors))
    classified_count = max(
        0,
        len(command_errors) - int(failure_category_counts.get("unclassified_command_failure", 0)),
    )
    return {
        "classified_failure_count": classified_count,
        "unclassified_failure_count": unclassified_count,
        "command_file_required_count": len([item for item in command_errors if item.get("requires_command_file")]),
        "failure_categories": dict(failure_category_counts),
        "top_dedupe_keys": [
            {"dedupe_key": key, "count": count}
            for key, count in Counter(
                str(item.get("dedupe_key") or "") for item in command_errors if item.get("dedupe_key")
            ).most_common(top)
        ],
    }


def compact_span(span: dict[str, Any]) -> dict[str, Any]:
    row = {
        "name": span.get("name") or "",
        "phase": span.get("phase") or "",
        "event_type": span.get("event_type") or "",
        "duration_ms": span.get("duration_ms"),
        "status": span.get("status") or "",
        "exit_code": span.get("exit_code"),
        "cached": bool(span.get("cached")),
        "summary": span.get("summary") or "",
        "subject": span.get("subject_redacted") or "",
    }
    command_error = command_error_from_span(span)
    if command_error:
        row["command_error"] = {
            "failure_category": command_error.get("failure_category", ""),
            "dedupe_key": command_error.get("dedupe_key", ""),
            "retry_policy": command_error.get("retry_policy", ""),
            "requires_command_file": bool(command_error.get("requires_command_file")),
        }
    inline_warning = inline_warning_from_span(span)
    if inline_warning:
        row["inline_command_warning"] = inline_warning
    return row


def is_validation_span(span: dict[str, Any]) -> bool:
    phase = str(span.get("phase") or "")
    event_type = str(span.get("event_type") or "")
    name = str(span.get("name") or "").lower()
    return (
        phase in {"validation", "completion", "final-gate"}
        or event_type in {"validation", "gate", "gate-pool"}
        or "validation" in name
        or "completion-test" in name
    )


def valid_trace_id(value: str) -> bool:
    text = str(value or "").lower()
    return bool(TRACE_ID_HEX.fullmatch(text)) and text != "0" * 32


def valid_span_id(value: str) -> bool:
    text = str(value or "").lower()
    return bool(SPAN_ID_HEX.fullmatch(text)) and text != "0" * 16


def normalized_trace_id(value: str) -> tuple[str, bool]:
    text = str(value or "").lower()
    if valid_trace_id(text):
        return text, False
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:32] if text else "0" * 32
    if digest == "0" * 32:
        digest = "1" + digest[1:]
    return digest, True


def normalized_span_id(value: str) -> tuple[str, bool]:
    text = str(value or "").lower()
    if valid_span_id(text):
        return text, False
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16] if text else "0" * 16
    if digest == "0" * 16:
        digest = "1" + digest[1:]
    return digest, True


def traceparent_for_span(span: dict[str, Any]) -> dict[str, Any]:
    trace_id, trace_derived = normalized_trace_id(str(span.get("trace_id") or ""))
    span_id, span_derived = normalized_span_id(str(span.get("span_id") or ""))
    return {
        "traceparent": f"00-{trace_id}-{span_id}-01",
        "derived": trace_derived or span_derived,
        "trace_id_derived": trace_derived,
        "span_id_derived": span_derived,
    }


def trace_depth(span_id: str, parents: dict[str, str]) -> int:
    depth = 0
    seen: set[str] = set()
    current = span_id
    while parents.get(current):
        parent = parents[current]
        if parent in seen:
            return depth
        seen.add(parent)
        depth += 1
        current = parent
    return depth


def build_trace_context_summary(spans: list[dict[str, Any]], top: int) -> dict[str, Any]:
    """Map local spans to OpenTelemetry/W3C Trace Context vocabulary.

    The SQLite schema already has ``trace_id``, ``span_id`` and
    ``parent_span_id`` plus arbitrary attributes. Reporting the standard shape
    here gives gates/exporters a stable contract without a risky schema
    migration. Non-W3C ids stay visible as non-standard; sample traceparents are
    marked ``derived`` when generated from local ids.
    """
    by_trace: dict[str, list[dict[str, Any]]] = {}
    for span in spans:
        by_trace.setdefault(str(span.get("trace_id") or ""), []).append(span)
    parents = {str(span.get("span_id") or ""): str(span.get("parent_span_id") or "") for span in spans}
    roots = [span for span in spans if not span.get("parent_span_id")]
    children = [span for span in spans if span.get("parent_span_id")]
    known_ids = {str(span.get("span_id") or "") for span in spans}
    orphans = [
        span
        for span in children
        if str(span.get("parent_span_id") or "") not in known_ids
    ]
    attrs = [span.get("attributes") for span in spans if isinstance(span.get("attributes"), dict)]
    traceparent_values = [
        str(attr.get("traceparent") or "")
        for attr in attrs
        if attr.get("traceparent") not in (None, "")
    ]
    tracestate_values = [
        str(attr.get("tracestate") or "")
        for attr in attrs
        if attr.get("tracestate") not in (None, "")
    ]
    sample_spans = spans[: max(0, top)]
    return {
        "standard": {
            "trace_model": "OpenTelemetry trace/span context",
            "propagation": "W3C traceparent/tracestate",
            "schema_strategy": "report-layer mapping; SQLite schema unchanged",
        },
        "trace_count": len(by_trace),
        "root_span_count": len(roots),
        "child_span_count": len(children),
        "orphan_span_count": len(orphans),
        "max_depth": max([trace_depth(str(span.get("span_id") or ""), parents) for span in spans] or [0]),
        "w3c_valid_trace_id_count": len([span for span in spans if valid_trace_id(str(span.get("trace_id") or ""))]),
        "w3c_valid_span_id_count": len([span for span in spans if valid_span_id(str(span.get("span_id") or ""))]),
        "traceparent_attribute_count": len(traceparent_values),
        "valid_traceparent_attribute_count": len([item for item in traceparent_values if TRACEPARENT_RE.fullmatch(item)]),
        "tracestate_attribute_count": len(tracestate_values),
        "largest_traces": [
            {"trace_id": trace_id, "span_count": len(items)}
            for trace_id, items in sorted(by_trace.items(), key=lambda item: len(item[1]), reverse=True)[:top]
        ],
        "sample_traceparents": [
            {
                "span_id": span.get("span_id") or "",
                **traceparent_for_span(span),
            }
            for span in sample_spans
        ],
    }


def compact_metrics(spans: list[dict[str, Any]]) -> dict[str, Any]:
    terminal = [span for span in spans if span.get("status") != "started"]
    durations = [int(span["duration_ms"]) for span in terminal if span.get("duration_ms") is not None]
    validation = [
        int(span["duration_ms"])
        for span in terminal
        if span.get("duration_ms") is not None and is_validation_span(span)
    ]
    failures = [
        span
        for span in terminal
        if span.get("status") == "failed" or (span.get("exit_code") not in (None, 0))
    ]
    command_errors = [command_error_from_span(span) for span in failures]
    command_errors = [item for item in command_errors if item]
    command_error_summary = summarize_command_errors(command_errors, failure_count=len(failures), top=10)
    inline_warnings = [inline_warning_from_span(span) for span in terminal]
    inline_warnings = [item for item in inline_warnings if item]
    subjects = Counter(str(span.get("subject_redacted") or "") for span in terminal if span.get("subject_redacted"))
    return {
        "span_count": len(spans),
        "terminal_span_count": len(terminal),
        "failed_count": len(failures),
        "failure_rate": (len(failures) / len(terminal)) if terminal else 0,
        "command_error": {
            **command_error_summary,
            "inline_command_warning_count": len(inline_warnings),
            "inline_command_warning_reasons": dict(
                Counter(reason for warning in inline_warnings for reason in warning.get("reasons", []))
            ),
        },
        "duration_ms": duration_stats(durations),
        "validation_duration_ms": duration_stats(validation),
        "cache": {
            "hits": len([span for span in terminal if span.get("cached")]),
            "misses": len([span for span in terminal if span.get("cache_key") and not span.get("cached")]),
        },
        "duplicate_subject_count": len([count for count in subjects.values() if count > 1]),
        "command_count": len([span for span in terminal if span.get("span_kind") == "command"]),
        "gate_pool_count": len([span for span in terminal if span.get("event_type") == "gate-pool" or span.get("name") == "gate-pool"]),
        "completion_test_count": len([span for span in terminal if "completion-test" in str(span.get("name") or "")]),
        "final_gate_count": len([span for span in terminal if span.get("final_gate")]),
    }


def diff_number(before: int | float | None, after: int | float | None) -> dict[str, Any]:
    if before is None or after is None:
        return {"before": before, "after": after, "delta": None, "delta_percent": None}
    delta = after - before
    return {
        "before": before,
        "after": after,
        "delta": delta,
        "delta_percent": round((delta / before) * 100, 2) if before else None,
    }


def effectiveness_metric_diffs(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "duration_sum_ms": diff_number(baseline["duration_ms"]["sum"], candidate["duration_ms"]["sum"]),
        "duration_p95_ms": diff_number(baseline["duration_ms"]["p95"], candidate["duration_ms"]["p95"]),
        "validation_duration_sum_ms": diff_number(
            baseline["validation_duration_ms"]["sum"], candidate["validation_duration_ms"]["sum"]
        ),
        "span_count": diff_number(baseline["span_count"], candidate["span_count"]),
        "command_count": diff_number(baseline["command_count"], candidate["command_count"]),
        "failed_count": diff_number(baseline["failed_count"], candidate["failed_count"]),
        "failure_rate": diff_number(baseline["failure_rate"], candidate["failure_rate"]),
        "cache_hits": diff_number(baseline["cache"]["hits"], candidate["cache"]["hits"]),
        "cache_misses": diff_number(baseline["cache"]["misses"], candidate["cache"]["misses"]),
        "duplicate_subject_count": diff_number(
            baseline["duplicate_subject_count"], candidate["duplicate_subject_count"]
        ),
        "gate_pool_count": diff_number(baseline["gate_pool_count"], candidate["gate_pool_count"]),
        "completion_test_count": diff_number(
            baseline["completion_test_count"], candidate["completion_test_count"]
        ),
        "final_gate_count": diff_number(baseline["final_gate_count"], candidate["final_gate_count"]),
    }


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.root).resolve()
    spans = span_rows(
        root,
        db=args.db,
        task_id=args.task_id or "",
        trace_id=args.trace_id or "",
        since=args.since or "",
        until=args.until or "",
    )
    terminal = [span for span in spans if span.get("status") != "started"]
    durations = [int(span["duration_ms"]) for span in terminal if span.get("duration_ms") is not None]
    validation_spans = [span for span in terminal if span.get("duration_ms") is not None and is_validation_span(span)]
    phases = sorted({str(span.get("phase") or "unknown") for span in terminal})
    duration_by_phase = {
        phase: duration_stats(
            [
                int(span["duration_ms"])
                for span in terminal
                if str(span.get("phase") or "unknown") == phase and span.get("duration_ms") is not None
            ]
        )
        for phase in phases
    }
    failures = [
        span
        for span in terminal
        if span.get("status") == "failed" or (span.get("exit_code") not in (None, 0))
    ]
    command_errors = [command_error_from_span(span) for span in failures]
    command_errors = [item for item in command_errors if item]
    command_error_summary = summarize_command_errors(command_errors, failure_count=len(failures), top=args.top)
    inline_warnings = [inline_warning_from_span(span) for span in terminal]
    inline_warnings = [item for item in inline_warnings if item]
    identity_attrs = [span.get("attributes") for span in terminal if isinstance(span.get("attributes"), dict)]
    client_type_counts = Counter(str(attr.get("client_type") or "unknown") for attr in identity_attrs)
    model_counts = Counter(str(attr.get("model_id") or "unknown") for attr in identity_attrs)
    client_model_counts = Counter(
        f"{str(attr.get('client_type') or 'unknown')} / {str(attr.get('model_id') or 'unknown')}"
        for attr in identity_attrs
    )
    subject_counts = Counter(str(span.get("subject_redacted") or "") for span in terminal if span.get("subject_redacted"))
    duplicates = [
        {"subject": subject, "count": count}
        for subject, count in subject_counts.most_common()
        if count > 1
    ]
    return {
        "db": str(db_path(root, args.db)),
        "filters": {
            "task_id": args.task_id or "",
            "trace_id": args.trace_id or "",
            "since": args.since or "",
            "until": args.until or "",
        },
        "span_count": len(spans),
        "terminal_span_count": len(terminal),
        "failed_count": len(failures),
        "failure_rate": (len(failures) / len(terminal)) if terminal else 0,
        "command_error": {
            **command_error_summary,
            "inline_command_warning_count": len(inline_warnings),
            "inline_command_warning_reasons": dict(
                Counter(reason for warning in inline_warnings for reason in warning.get("reasons", []))
            ),
        },
        "duration_ms": duration_stats(durations),
        "duration_by_phase": duration_by_phase,
        "slowest_spans": [
            compact_span(span)
            for span in sorted(
                [span for span in terminal if span.get("duration_ms") is not None],
                key=lambda item: int(item.get("duration_ms") or 0),
                reverse=True,
            )[: args.top]
        ],
        "slowest_validation_spans": [
            compact_span(span)
            for span in sorted(validation_spans, key=lambda item: int(item.get("duration_ms") or 0), reverse=True)[: args.top]
        ],
        "cache": {
            "hits": len([span for span in terminal if span.get("cached")]),
            "misses": len([span for span in terminal if span.get("cache_key") and not span.get("cached")]),
        },
        "top_operations": [
            {"name": name, "count": count}
            for name, count in Counter(str(span.get("name") or "unknown") for span in terminal).most_common(args.top)
        ],
        "top_subjects": [
            {"subject": subject, "count": count}
            for subject, count in subject_counts.most_common(args.top)
        ],
        "span_kind_counts": dict(Counter(str(span.get("span_kind") or "unknown") for span in terminal)),
        "subject_type_counts": dict(Counter(str(span.get("subject_type") or "unknown") for span in terminal)),
        "duplicate_subjects": duplicates[: args.top],
        "status_counts": dict(Counter(str(span.get("status") or "unknown") for span in spans)),
        "scope_kind_counts": dict(Counter(str(span.get("scope_kind") or "unknown") for span in terminal)),
        "client_type_counts": dict(client_type_counts),
        "model_counts": dict(model_counts),
        "client_model_counts": dict(client_model_counts),
        "adapter_enforcement_counts": dict(
            Counter(str(span.get("adapter_enforcement") or "none") for span in terminal)
        ),
        "trace_context": build_trace_context_summary(spans, args.top),
        "latest_spans": [compact_span(span) for span in terminal[-args.top :]],
    }


def window_filters(prefix: str, args: argparse.Namespace) -> dict[str, str]:
    return {
        "trace_id": getattr(args, f"{prefix}_trace_id") or "",
        "since": getattr(args, f"{prefix}_since") or "",
        "until": getattr(args, f"{prefix}_until") or "",
    }


def build_effectiveness_report(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.root).resolve()
    baseline_filters = window_filters("baseline", args)
    candidate_filters = window_filters("candidate", args)
    baseline_spans = span_rows(
        root,
        db=args.db,
        task_id=args.baseline_task_id or args.task_id or "",
        trace_id=baseline_filters["trace_id"],
        since=baseline_filters["since"],
        until=baseline_filters["until"],
    )
    candidate_spans = span_rows(
        root,
        db=args.db,
        task_id=args.candidate_task_id or args.task_id or "",
        trace_id=candidate_filters["trace_id"],
        since=candidate_filters["since"],
        until=candidate_filters["until"],
    )
    baseline = compact_metrics(baseline_spans)
    candidate = compact_metrics(candidate_spans)
    return {
        "db": str(db_path(root, args.db)),
        "task_id": args.task_id or "",
        "baseline": {
            "filters": {
                "task_id": args.baseline_task_id or args.task_id or "",
                **baseline_filters,
            },
            "metrics": baseline,
        },
        "candidate": {
            "filters": {
                "task_id": args.candidate_task_id or args.task_id or "",
                **candidate_filters,
            },
            "metrics": candidate,
        },
        "diff": effectiveness_metric_diffs(baseline, candidate),
        "trace_context": {
            "baseline": build_trace_context_summary(baseline_spans, args.top),
            "candidate": build_trace_context_summary(candidate_spans, args.top),
        },
    }


def snapshot_key(args: argparse.Namespace) -> str:
    if args.snapshot_key:
        return args.snapshot_key
    if args.task_id:
        return f"task:{args.task_id}"
    if args.trace_id:
        return f"trace:{args.trace_id}"
    digest = hashlib.sha256(
        json.dumps(
            {"since": args.since or "", "until": args.until or "", "label": args.label or ""},
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:12]
    return f"window:{digest}"


def build_effectiveness_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.root).resolve()
    spans = span_rows(
        root,
        db=args.db,
        task_id=args.task_id or "",
        trace_id=args.trace_id or "",
        since=args.since or "",
        until=args.until or "",
    )
    payload = {
        "schema_version": 1,
        "snapshot_key": snapshot_key(args),
        "label": args.label or "",
        "generated_at": utc_now(),
        "db": str(db_path(root, args.db)),
        "filters": {
            "task_id": args.task_id or "",
            "trace_id": args.trace_id or "",
            "since": args.since or "",
            "until": args.until or "",
        },
        "metrics": compact_metrics(spans),
        "trace_context": build_trace_context_summary(spans, args.top),
    }
    con = state_store.connect(db_path(root, args.db))
    state_store.upsert_state(
        con,
        state_type="telemetry-effectiveness-snapshot",
        state_key=payload["snapshot_key"],
        payload=payload,
        source_command="ai_client_governance.py telemetry effectiveness snapshot",
        summary=args.label or "telemetry effectiveness snapshot",
        event_type="telemetry.effectiveness.snapshot",
    )
    return payload


def build_effectiveness_trend(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.root).resolve()
    con = state_store.connect(db_path(root, args.db))
    rows = state_store.list_states(con, state_type="telemetry-effectiveness-snapshot")
    snapshots = [row["payload"] for row in rows if isinstance(row.get("payload"), dict)]
    if args.task_id:
        snapshots = [
            item
            for item in snapshots
            if str(item.get("filters", {}).get("task_id") or "") == args.task_id
        ]
    if args.label:
        snapshots = [item for item in snapshots if str(item.get("label") or "") == args.label]
    snapshots.sort(key=lambda item: (str(item.get("generated_at") or ""), str(item.get("snapshot_key") or "")))
    deltas: list[dict[str, Any]] = []
    for before, after in zip(snapshots, snapshots[1:]):
        deltas.append(
            {
                "before": before.get("snapshot_key", ""),
                "after": after.get("snapshot_key", ""),
                "diff": effectiveness_metric_diffs(before.get("metrics", compact_metrics([])), after.get("metrics", compact_metrics([]))),
            }
        )
    return {
        "db": str(db_path(root, args.db)),
        "snapshot_count": len(snapshots),
        "snapshots": snapshots[-args.top :],
        "deltas": deltas[-args.top :],
    }


def format_text(report: dict[str, Any]) -> str:
    duration = report["duration_ms"]
    cache = report["cache"]
    command_error = report.get("command_error", {})
    lines = [
        "AI Client Governance Execution Telemetry Report",
        f"DB: {report['db']}",
        f"Spans: {report['span_count']} terminal={report['terminal_span_count']}",
        f"Failures: {report['failed_count']} rate={report['failure_rate']:.2%}",
        (
            "Duration ms: "
            f"sum={duration['sum']} avg={duration['avg']} p50={duration['p50']} "
            f"p95={duration['p95']} max={duration['max']}"
        ),
        f"Cache: hits={cache['hits']} misses={cache['misses']}",
        (
            "Command errors: "
            f"classified={command_error.get('classified_failure_count', 0)} "
            f"unclassified={command_error.get('unclassified_failure_count', 0)} "
            f"command_file_required={command_error.get('command_file_required_count', 0)} "
            f"inline_warnings={command_error.get('inline_command_warning_count', 0)}"
        ),
        "",
        "Top operations:",
    ]
    for row in report["top_operations"]:
        lines.append(f"  {row['name']}: count={row['count']}")
    lines.append("")
    lines.append("Failure categories:")
    categories = command_error.get("failure_categories", {})
    if categories:
        for category, count in sorted(categories.items(), key=lambda item: (-int(item[1]), item[0])):
            lines.append(f"  {category}: count={count}")
    else:
        lines.append("  none")
    warning_reasons = command_error.get("inline_command_warning_reasons", {})
    if warning_reasons:
        lines.append("Inline command warning reasons:")
        for reason, count in sorted(warning_reasons.items(), key=lambda item: (-int(item[1]), item[0])):
            lines.append(f"  {reason}: count={count}")
        lines.append("  recommended_runner=shell-adapter proxy-powershell --powershell-command-file")
    lines.append("")
    lines.append("Top subjects:")
    for row in report["top_subjects"]:
        lines.append(f"  count={row['count']} {row['subject']}")
    lines.append("")
    lines.append("Duration by phase:")
    for phase, stats in report["duration_by_phase"].items():
        lines.append(
            f"  {phase}: sum={stats['sum']} avg={stats['avg']} p50={stats['p50']} "
            f"p95={stats['p95']} max={stats['max']}"
        )
    lines.append("")
    lines.append("Slowest validation spans:")
    if report["slowest_validation_spans"]:
        for row in report["slowest_validation_spans"]:
            lines.append(
                f"  {row['duration_ms']}ms {row['name']} [{row['phase']}/{row['event_type']}] status={row['status']}"
            )
    else:
        lines.append("  none")
    lines.append("")
    lines.append("Duplicate subjects:")
    if report["duplicate_subjects"]:
        for row in report["duplicate_subjects"]:
            lines.append(f"  count={row['count']} {row['subject']}")
    else:
        lines.append("  none")
    lines.append("")
    lines.append(f"Span kinds: {json.dumps(report['span_kind_counts'], ensure_ascii=False, sort_keys=True)}")
    lines.append(f"Subject types: {json.dumps(report['subject_type_counts'], ensure_ascii=False, sort_keys=True)}")
    lines.append(f"Scope kinds: {json.dumps(report['scope_kind_counts'], ensure_ascii=False, sort_keys=True)}")
    lines.append(f"Client types: {json.dumps(report['client_type_counts'], ensure_ascii=False, sort_keys=True)}")
    lines.append(f"Models: {json.dumps(report['model_counts'], ensure_ascii=False, sort_keys=True)}")
    lines.append(f"Client/model: {json.dumps(report['client_model_counts'], ensure_ascii=False, sort_keys=True)}")
    lines.append(
        f"Adapter enforcement: {json.dumps(report['adapter_enforcement_counts'], ensure_ascii=False, sort_keys=True)}"
    )
    trace = report["trace_context"]
    lines.extend(
        [
            "",
            "Trace context:",
            (
                f"  traces={trace['trace_count']} roots={trace['root_span_count']} "
                f"children={trace['child_span_count']} orphans={trace['orphan_span_count']} "
                f"max_depth={trace['max_depth']}"
            ),
            (
                f"  w3c trace ids={trace['w3c_valid_trace_id_count']} "
                f"span ids={trace['w3c_valid_span_id_count']} "
                f"traceparent attrs={trace['valid_traceparent_attribute_count']}/{trace['traceparent_attribute_count']}"
            ),
        ]
    )
    return "\n".join(lines)


def format_markdown(report: dict[str, Any]) -> str:
    duration = report["duration_ms"]
    cache = report["cache"]
    command_error = report.get("command_error", {})
    lines = [
        "# AI Client Governance Execution Telemetry Report",
        "",
        f"- DB: `{report['db']}`",
        f"- Spans: {report['span_count']} terminal={report['terminal_span_count']}",
        f"- Failures: {report['failed_count']} rate={report['failure_rate']:.2%}",
        (
            "- Duration ms: "
            f"sum={duration['sum']} avg={duration['avg']} p50={duration['p50']} "
            f"p95={duration['p95']} max={duration['max']}"
        ),
        f"- Cache: hits={cache['hits']} misses={cache['misses']}",
        (
            f"- Command errors: classified={command_error.get('classified_failure_count', 0)} "
            f"unclassified={command_error.get('unclassified_failure_count', 0)} "
            f"command_file_required={command_error.get('command_file_required_count', 0)} "
            f"inline_warnings={command_error.get('inline_command_warning_count', 0)}"
        ),
        "",
        "## Failure Categories",
        "",
        "| Category | Count |",
        "| --- | ---: |",
    ]
    categories = command_error.get("failure_categories", {})
    if categories:
        for category, count in sorted(categories.items(), key=lambda item: (-int(item[1]), item[0])):
            lines.append(f"| `{category}` | {count} |")
    else:
        lines.append("| `none` | 0 |")
    warning_reasons = command_error.get("inline_command_warning_reasons", {})
    if warning_reasons:
        lines.extend(["", "## Inline Command Warnings", "", "| Reason | Count |", "| --- | ---: |"])
        for reason, count in sorted(warning_reasons.items(), key=lambda item: (-int(item[1]), item[0])):
            lines.append(f"| `{reason}` | {count} |")
        lines.append("")
        lines.append("Recommended runner: `shell-adapter proxy-powershell --powershell-command-file`.")
    lines.extend(
        [
        "## Top Operations",
        "",
        "| Operation | Count |",
        "| --- | ---: |",
        ]
    )
    for row in report["top_operations"]:
        lines.append(f"| `{row['name']}` | {row['count']} |")
    lines.extend(["", "## Top Subjects", "", "| Count | Subject |", "| ---: | --- |"])
    for row in report["top_subjects"]:
        lines.append(f"| {row['count']} | `{row['subject']}` |")
    lines.extend(["", "## Duration By Phase", "", "| Phase | Sum ms | Avg ms | P50 | P95 | Max |", "| --- | ---: | ---: | ---: | ---: | ---: |"])
    for phase, stats in report["duration_by_phase"].items():
        lines.append(f"| {phase} | {stats['sum']} | {stats['avg']} | {stats['p50']} | {stats['p95']} | {stats['max']} |")
    lines.extend(["", "## Client And Model", "", "| Client / Model | Count |", "| --- | ---: |"])
    for key, count in sorted(report["client_model_counts"].items()):
        lines.append(f"| `{key}` | {count} |")
    lines.extend(["", "## Slowest Validation Spans", ""])
    if report["slowest_validation_spans"]:
        lines.extend(["| Duration ms | Name | Phase | Event | Status |", "| ---: | --- | --- | --- | --- |"])
        for row in report["slowest_validation_spans"]:
            lines.append(f"| {row['duration_ms']} | `{row['name']}` | {row['phase']} | {row['event_type']} | {row['status']} |")
    else:
        lines.append("None.")
    lines.extend(["", "## Duplicate Subjects", ""])
    if report["duplicate_subjects"]:
        lines.extend(["| Count | Subject |", "| ---: | --- |"])
        for row in report["duplicate_subjects"]:
            lines.append(f"| {row['count']} | `{row['subject']}` |")
    else:
        lines.append("None.")
    trace = report["trace_context"]
    lines.extend(
        [
            "",
            "## Trace Context",
            "",
            f"- Standard: {trace['standard']['trace_model']}; {trace['standard']['propagation']}",
            f"- Traces: {trace['trace_count']} roots={trace['root_span_count']} children={trace['child_span_count']} orphans={trace['orphan_span_count']}",
            f"- W3C valid ids: trace={trace['w3c_valid_trace_id_count']} span={trace['w3c_valid_span_id_count']}",
            f"- Traceparent attributes: {trace['valid_traceparent_attribute_count']}/{trace['traceparent_attribute_count']}",
        ]
    )
    return "\n".join(lines)


def format_effectiveness_text(report: dict[str, Any]) -> str:
    lines = [
        "AI Client Governance Effectiveness Report",
        f"DB: {report['db']}",
        f"Task: {report['task_id'] or '<any>'}",
        "",
        "Metric deltas:",
    ]
    for name, diff in report["diff"].items():
        lines.append(
            f"  {name}: before={diff['before']} after={diff['after']} "
            f"delta={diff['delta']} delta_percent={diff['delta_percent']}"
        )
    return "\n".join(lines)


def format_effectiveness_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# AI Client Governance Effectiveness Report",
        "",
        f"- DB: `{report['db']}`",
        f"- Task: `{report['task_id'] or '<any>'}`",
        "",
        "| Metric | Before | After | Delta | Delta % |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for name, diff in report["diff"].items():
        lines.append(
            f"| `{name}` | {diff['before']} | {diff['after']} | {diff['delta']} | {diff['delta_percent']} |"
        )
    return "\n".join(lines)


def format_snapshot_text(report: dict[str, Any]) -> str:
    metrics = report["metrics"]
    duration = metrics["duration_ms"]
    return "\n".join(
        [
            "AI Client Governance Effectiveness Snapshot",
            f"Key: {report['snapshot_key']}",
            f"Label: {report['label'] or '<none>'}",
            f"Spans: {metrics['span_count']} terminal={metrics['terminal_span_count']}",
            f"Failures: {metrics['failed_count']} rate={metrics['failure_rate']:.2%}",
            f"Duration ms: sum={duration['sum']} p95={duration['p95']} max={duration['max']}",
        ]
    )


def format_trend_text(report: dict[str, Any]) -> str:
    lines = [
        "AI Client Governance Effectiveness Trend",
        f"Snapshots: {report['snapshot_count']}",
        "",
        "Deltas:",
    ]
    if not report["deltas"]:
        lines.append("  none")
    for item in report["deltas"]:
        duration = item["diff"]["duration_sum_ms"]
        commands = item["diff"]["command_count"]
        lines.append(
            f"  {item['before']} -> {item['after']}: "
            f"duration_delta={duration['delta']} command_delta={commands['delta']}"
        )
    return "\n".join(lines)


def parse_attribute_kv(values: list[str] | None) -> dict[str, Any] | None:
    if not values:
        return None
    parsed: dict[str, Any] = {}
    for item in values:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"attribute must use key=value form: {item}")
        parsed[key.strip()] = value
    return parsed


def first_nonempty(*values: str | None) -> str:
    for value in values:
        text = (value or "").strip()
        if text:
            return text
    return ""


def execution_identity_attributes(args: argparse.Namespace) -> dict[str, str]:
    client_type = first_nonempty(
        getattr(args, "client_type", None),
        os.environ.get("AICG_CLIENT_TYPE"),
        os.environ.get("AI_CLIENT_TYPE"),
        os.environ.get("CODEX_CLIENT_TYPE"),
    )
    client_version = first_nonempty(
        getattr(args, "client_version", None),
        os.environ.get("AICG_CLIENT_VERSION"),
        os.environ.get("AI_CLIENT_VERSION"),
        os.environ.get("CODEX_CLIENT_VERSION"),
    )
    model_id = first_nonempty(
        getattr(args, "model", None),
        os.environ.get("AICG_MODEL"),
        os.environ.get("AI_MODEL"),
        os.environ.get("MODEL_NAME"),
        os.environ.get("CODEX_MODEL"),
    )
    model_provider = first_nonempty(
        getattr(args, "model_provider", None),
        os.environ.get("AICG_MODEL_PROVIDER"),
        os.environ.get("AI_MODEL_PROVIDER"),
        os.environ.get("MODEL_PROVIDER"),
    )
    attrs = {
        "client_type": client_type or "unknown",
        "model_id": model_id or "unknown",
    }
    if client_version:
        attrs["client_version"] = client_version
    if model_provider:
        attrs["model_provider"] = model_provider
    return attrs


def command_record(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    try:
        attributes = parse_attribute_kv(args.attribute_kv)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    env_context = trace_context_from_env()
    parsed_traceparent = parse_traceparent(args.traceparent or "")
    base_context = parsed_traceparent or env_context
    trace_id = args.trace_id or base_context.trace_id
    span_id = args.span_id or new_span_id()
    trace_context = new_trace_context(
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=args.parent_span_id or base_context.span_id,
        tracestate=args.tracestate or base_context.tracestate,
    )
    timestamp = args.timestamp or args.ended_at or args.started_at or utc_now()
    event = {
        "span_id": span_id,
        "trace_id": trace_id,
        "parent_span_id": args.parent_span_id or trace_context.parent_span_id,
        "task_id": args.task_id or "",
        "task_tracking": args.task_tracking or "",
        "name": args.name or args.span_kind or "operation",
        "span_kind": args.span_kind or "operation",
        "subject": args.subject or "",
        "subject_type": args.subject_type or "operation",
        "command": args.command or "",
        "url": args.url or "",
        "endpoint": args.endpoint or "",
        "status": args.status,
        "exit_code": args.exit_code,
        "started_at": args.started_at or timestamp,
        "ended_at": args.ended_at or (timestamp if args.status != "started" else ""),
        "duration_ms": args.duration_ms,
        "timestamp": timestamp,
        "cwd": args.cwd or str(Path.cwd().resolve()),
        "source": args.source or "ai_client_governance.py telemetry",
        "summary": args.summary or "",
        "task_types": as_list(args.task_type),
        "phase": args.phase or "",
        "event_type": args.event_type or "telemetry.record",
        "attempt": args.attempt,
        "final_gate": bool(args.final_gate),
        "cached": bool(args.cached),
        "cache_key": args.cache_key or "",
        "cache_reason": args.cache_reason or "",
        "scope_kind": args.scope_kind or "",
        "scope_reason": args.scope_reason or "",
        "scope_paths": as_list(args.scope_path),
        "adapter_enforcement": args.adapter_enforcement or "",
        "traceparent": trace_context.traceparent,
        "tracestate": trace_context.tracestate,
    }
    merged_attributes = execution_identity_attributes(args)
    merged_attributes.update(attributes or {})
    merged_attributes.setdefault("traceparent", trace_context.traceparent)
    if trace_context.tracestate:
        merged_attributes.setdefault("tracestate", trace_context.tracestate)
    event["attributes"] = merged_attributes
    path = append_event(root, event, db=args.db, source_command="ai_client_governance.py telemetry record")
    print(f"recorded telemetry span={span_id} kind={event['span_kind']} status={args.status} db={path}")
    return 0


def command_init(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    connect(root, args.db)
    print(f"telemetry initialized: {db_path(root, args.db)}")
    return 0


def command_report(args: argparse.Namespace) -> int:
    report = build_report(args)
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif args.format == "markdown":
        print(format_markdown(report))
    else:
        print(format_text(report))
    return 0


def command_effectiveness(args: argparse.Namespace) -> int:
    action = args.effectiveness_action or "compare"
    if action == "snapshot":
        report = build_effectiveness_snapshot(args)
        if args.format == "json":
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(format_snapshot_text(report))
        return 0
    if action == "trend":
        report = build_effectiveness_trend(args)
        if args.format == "json":
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(format_trend_text(report))
        return 0
    report = build_effectiveness_report(args)
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif args.format == "markdown":
        print(format_effectiveness_markdown(report))
    else:
        print(format_effectiveness_text(report))
    return 0


def telemetry_events(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    events = read_events(
        root,
        db=args.db,
        task_id=args.task_id or "",
        trace_id=args.trace_id or "",
        since=args.since or "",
        until=args.until or "",
    )
    print(json.dumps(events[-args.top :], ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Record and analyze unified execution telemetry in aicg.db.")
    common_cli_args.add_common_global_args(parser, names=("root", "db"))
    sub = parser.add_subparsers(dest="command_name", required=True)

    init = sub.add_parser("init", help="Create or migrate telemetry tables.")
    common_cli_args.add_common_global_args(init, names=("root", "db"), suppress_default=True)
    init.set_defaults(func=command_init)

    record = sub.add_parser("record", help="Record one execution telemetry span/event.")
    common_cli_args.add_common_global_args(record, names=("root", "db"), suppress_default=True)
    record.add_argument("--span-id", default="", help="Explicit span id. Default: generated UUID.")
    record.add_argument("--trace-id", default="", help="Trace id shared by related spans. Default: span id.")
    record.add_argument("--parent-span-id", default="", help="Parent span id.")
    record.add_argument("--traceparent", default="", help="W3C traceparent header to continue.")
    record.add_argument("--tracestate", default="", help="W3C tracestate header to continue.")
    record.add_argument("--task-id", default="", help="Structured task id.")
    record.add_argument("--task-tracking", default="", help="Human-readable task tracking reference, if any.")
    record.add_argument("--task-type", action="append", help="Related task type.")
    record.add_argument("--client-type", default="", help="AI client/runtime name, e.g. codex, claude-code, trae, cursor.")
    record.add_argument("--client-version", default="", help="AI client/runtime version, if available.")
    record.add_argument("--model", default="", help="Current model identifier, if available.")
    record.add_argument("--model-provider", default="", help="Current model provider, if available.")
    record.add_argument("--name", default="", help="Operation name.")
    record.add_argument("--span-kind", default="operation", help="Execution kind, e.g. command, model_http, sub_agent.")
    record.add_argument("--subject", default="", help="Primary execution subject.")
    record.add_argument("--subject-type", default="operation", help="Subject kind, e.g. command, http_url, model.")
    record.add_argument("--command", default="", help="Command subject for command spans.")
    record.add_argument("--url", default="", help="HTTP URL subject for network/model spans.")
    record.add_argument("--endpoint", default="", help="Endpoint subject for external service spans.")
    record.add_argument("--status", default="succeeded", help="Span status.")
    record.add_argument("--exit-code", type=int, help="Process exit code for command-like spans.")
    record.add_argument("--started-at", default="", help="ISO start timestamp.")
    record.add_argument("--ended-at", default="", help="ISO end timestamp.")
    record.add_argument("--timestamp", default="", help="Event timestamp.")
    record.add_argument("--duration-ms", type=int, help="Duration in milliseconds.")
    record.add_argument("--cwd", default="", help="Working directory for this span.")
    record.add_argument("--source", default="", help="Telemetry producer.")
    record.add_argument("--summary", default="", help="Short result summary.")
    record.add_argument("--phase", default="", help="Lifecycle phase.")
    record.add_argument("--event-type", default="", help="Telemetry event type.")
    record.add_argument("--attempt", type=int, help="Retry attempt.")
    record.add_argument("--final-gate", action="store_true", help="Mark as final gate evidence.")
    record.add_argument("--cached", action="store_true", help="Mark as a cache hit.")
    record.add_argument("--cache-key", default="", help="Cache key.")
    record.add_argument("--cache-reason", default="", help="Cache reason.")
    record.add_argument("--scope-kind", default="", help="Governance scope kind.")
    record.add_argument("--scope-reason", default="", help="Governance scope reason.")
    record.add_argument("--scope-path", action="append", help="Path associated with scope classification.")
    record.add_argument("--adapter-enforcement", default="", help="Enforcement adapter label.")
    record.add_argument("--attribute-kv", action="append", help="Extension attribute in key=value form.")
    record.set_defaults(func=command_record)

    report = sub.add_parser("report", help="Summarize execution telemetry.")
    common_cli_args.add_common_global_args(report, names=("root", "db"), suppress_default=True)
    report.add_argument("--task-id", default="", help="Only include spans for one structured task id.")
    report.add_argument("--trace-id", default="", help="Only include spans for one trace id.")
    report.add_argument("--since", default="", help="Only include spans at or after this ISO timestamp.")
    report.add_argument("--until", default="", help="Only include spans at or before this ISO timestamp.")
    report.add_argument("--top", type=int, default=10, help="Number of top rows to show.")
    report.add_argument("--format", choices=("text", "markdown", "json"), default="text")
    report.set_defaults(func=command_report)

    effectiveness = sub.add_parser("effectiveness", help="Compare, snapshot, or trend telemetry effectiveness.")
    common_cli_args.add_common_global_args(effectiveness, names=("root", "db"), suppress_default=True)
    effectiveness.add_argument(
        "effectiveness_action",
        nargs="?",
        choices=("compare", "snapshot", "trend"),
        default="compare",
        help="Default compare keeps the historical before/after behavior.",
    )
    effectiveness.add_argument("--task-id", default="", help="Shared task id for both sides.")
    effectiveness.add_argument("--trace-id", default="", help="Trace id for snapshot filtering.")
    effectiveness.add_argument("--since", default="", help="Snapshot/trend window start ISO timestamp.")
    effectiveness.add_argument("--until", default="", help="Snapshot/trend window end ISO timestamp.")
    effectiveness.add_argument("--snapshot-key", default="", help="Snapshot key. Default derives from task/trace/window.")
    effectiveness.add_argument("--label", default="", help="Human label for snapshot/trend filtering.")
    effectiveness.add_argument("--baseline-task-id", default="", help="Task id for the baseline side.")
    effectiveness.add_argument("--candidate-task-id", default="", help="Task id for the candidate side.")
    effectiveness.add_argument("--baseline-trace-id", default="", help="Trace id for the baseline side.")
    effectiveness.add_argument("--candidate-trace-id", default="", help="Trace id for the candidate side.")
    effectiveness.add_argument("--baseline-since", default="", help="Baseline window start ISO timestamp.")
    effectiveness.add_argument("--baseline-until", default="", help="Baseline window end ISO timestamp.")
    effectiveness.add_argument("--candidate-since", default="", help="Candidate window start ISO timestamp.")
    effectiveness.add_argument("--candidate-until", default="", help="Candidate window end ISO timestamp.")
    effectiveness.add_argument("--top", type=int, default=10, help="Number of trace-context rows to include.")
    effectiveness.add_argument("--format", choices=("text", "markdown", "json"), default="text")
    effectiveness.set_defaults(func=command_effectiveness)

    events = sub.add_parser("events", help="Print raw normalized telemetry events as JSON.")
    common_cli_args.add_common_global_args(events, names=("root", "db"), suppress_default=True)
    events.add_argument("--task-id", default="", help="Only include events for one structured task id.")
    events.add_argument("--trace-id", default="", help="Only include events for one trace id.")
    events.add_argument("--since", default="", help="Only include events at or after this ISO timestamp.")
    events.add_argument("--until", default="", help="Only include events at or before this ISO timestamp.")
    events.add_argument("--top", type=int, default=50, help="Number of latest events to show.")
    events.set_defaults(func=telemetry_events)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
