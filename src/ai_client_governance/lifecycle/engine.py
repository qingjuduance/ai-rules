#!/usr/bin/env python3
"""AI Client Governance lifecycle router for task classification, preflight, and final gates.

This script turns prose workflow rules into a small executable lifecycle for the
human/AI coordination layer: input -> classification -> preflight -> execution
evidence -> finalize. It is conservative by design: optional lifecycle state is
recorded in aicg.db, and heavy checks are delegated through the unified
ai_client_governance.py CLI.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ai_client_governance.common.paths import PYTHON_PYCACHE_DIR, ai_client_governance_entrypoint
from ai_client_governance.records import state_store
from ai_client_governance.records import task_record as structured_task_record
from ai_client_governance.runtime import AgentExecutionContext, default_registry, requires_approval_for, requires_tracking_for
from ai_client_governance.runtime.registry import MUTATING_TASK_TYPES, NODE_EVENTS, TASK_TYPE_KEYWORDS
from ai_client_governance.runtime.scope import classify_scope
from ai_client_governance.validation import completion


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SCHEMA_VERSION = 1
DEFAULT_TRACE_PREFIX = "trace-lifecycle"

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

TASK_KEYWORDS = TASK_TYPE_KEYWORDS
MUTATING_TYPES = MUTATING_TASK_TYPES
NETWORK_DECISION_KEYWORDS = (
    "联网",
    "搜索",
    "查",
    "核对",
    "最新",
    "资料",
    "引用",
    "url",
    "http://",
    "https://",
    "search",
    "browse",
    "web",
    "latest",
    "today",
    "yesterday",
    "cite",
    "citation",
    "source",
)


@dataclass
class Finding:
    level: str
    message: str
    source: str = "ai_client_governance.py lifecycle"


@dataclass
class InputRecord:
    source: str
    trust: str
    needs_citation: bool
    summary: str
    derived_from: list[str] = field(default_factory=list)


@dataclass
class ExecutionIdentity:
    client_type: str
    client_version: str
    model_id: str
    model_provider: str
    identity_source: str


@dataclass
class Classification:
    task_types: list[str]
    task_size: str
    task_size_reasons: list[str]
    runtime_event: str
    requires_tracking: bool
    requires_approval: bool
    required_hooks: list[str]
    required_gates: list[str]
    changed_paths: list[str]
    scope_kind: str
    scope_reason: str
    scope_paths: list[str]


@dataclass
class LifecycleReport:
    schema_version: int
    trace_id: str
    phase: str
    state: str
    updated_at: str
    input: InputRecord
    execution_identity: ExecutionIdentity
    classification: Classification
    errors: list[Finding] = field(default_factory=list)
    warnings: list[Finding] = field(default_factory=list)
    notes: list[Finding] = field(default_factory=list)
    gate_commands: list[list[str]] = field(default_factory=list)
    state_db: str | None = None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def read_text_file(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def rel_path(path: Path, root: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(root.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def normalize_paths(values: list[str] | None) -> list[str]:
    result: list[str] = []
    for value in values or []:
        for part in value.split(","):
            stripped = part.strip()
            if stripped and stripped not in result:
                result.append(stripped.replace("\\", "/"))
    return result


def first_nonempty(*values: str | None) -> str:
    for value in values:
        text = (value or "").strip()
        if text:
            return text
    return ""


def execution_identity_from_args(args: argparse.Namespace) -> ExecutionIdentity:
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
    source_bits: list[str] = []
    if any(
        getattr(args, name, None)
        for name in ("client_type", "client_version", "model", "model_provider")
    ):
        source_bits.append("cli")
    if any(
        os.environ.get(name)
        for name in (
            "AICG_CLIENT_TYPE",
            "AI_CLIENT_TYPE",
            "CODEX_CLIENT_TYPE",
            "AICG_MODEL",
            "AI_MODEL",
            "MODEL_NAME",
            "CODEX_MODEL",
        )
    ):
        source_bits.append("env")
    if not source_bits:
        source_bits.append("default-unknown")
    return ExecutionIdentity(
        client_type=client_type or "unknown",
        client_version=client_version or "unknown",
        model_id=model_id or "unknown",
        model_provider=model_provider or "unknown",
        identity_source="+".join(source_bits),
    )


def safe_id_part(value: str, fallback: str) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").upper()
    return (text or fallback)[:48]


def message_from_args(args: argparse.Namespace, root: Path) -> str:
    parts: list[str] = []
    if getattr(args, "message", None):
        parts.append(args.message)
    if getattr(args, "message_file", None):
        path = Path(args.message_file)
        if not path.is_absolute():
            path = root / path
        if path.exists():
            parts.append(read_text_file(path))
    return "\n".join(parts).strip()


def summarize(text: str, limit: int = 240) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."


def split_requirement_segments(text: str) -> list[str]:
    stripped = text.strip()
    if not stripped:
        return []
    candidates: list[str] = []
    for line in stripped.splitlines():
        item = re.sub(r"^\s*(?:[-*+]|\d+[.)]|[A-Za-z][.)])\s+", "", line).strip()
        if item:
            candidates.append(item)
    if len(candidates) <= 1:
        parts = [part.strip() for part in re.split(r"(?:[;；]|。(?=\s|$))", stripped) if part.strip()]
        if len(parts) > 1:
            candidates = parts
    return candidates or [stripped]


def network_decision_for(text: str, input_source: str, task_types: list[str]) -> str:
    lowered = text.lower()
    if input_source == "web":
        return "External web input: record source URL/path separately from user instructions."
    if any(keyword in lowered or keyword in text for keyword in NETWORK_DECISION_KEYWORDS):
        return "Network/search evidence required or an explicit no-network rationale must be recorded."
    if "rules-script" in task_types:
        return "Rules/script governance change: record external-practice decision before execution."
    return "No network required unless later facts depend on current or external sources."


def validation_decision_for(task_types: list[str], changed_paths: list[str]) -> str:
    if "rules-script" in task_types:
        return "Run focused CLI validation plus ai_client_governance.py selftest before closeout."
    if changed_paths:
        return "Run task-specific checks for changed paths and record validation evidence."
    return "Verify final response covers each requirement."


def claim_risk_flags(text: str, task_types: list[str]) -> list[str]:
    flags = ["source_is_user"]
    assertion_markers = ("应该", "必须", "不应该", "不能", "可以", "bug", "问题", "会生成", "不会", "直接")
    mutable_markers = ("push", "commit", "删除", "提交", "推送", "telemetry", "审计记录", "状态", "脚本", "selftest", "worktree")
    stale_markers = ("现在", "当前", "之前", "刚刚", "目前", "latest", "recent")
    if any(marker in text for marker in assertion_markers):
        flags.append("may_be_wrong")
    if any(marker.lower() in text.lower() for marker in stale_markers):
        flags.append("context_or_time_dependent")
    if any(marker.lower() in text.lower() for marker in mutable_markers) or set(task_types).intersection({"git", "rules-script"}):
        flags.append("affects_execution_or_repository_state")
    return unique(flags)


def verification_action_for_claim(text: str, task_types: list[str]) -> str:
    lowered = text.lower()
    if any(marker in lowered or marker in text for marker in ("git", "push", "commit", "worktree", "状态", "telemetry", "审计记录", "脚本", "selftest")):
        return "verify-local-live-state-or-script-contract-before-execution"
    if "rules-script" in task_types:
        return "verify-rules-and-external-practice-before-adoption"
    return "record-claim-and-verify-if-it-steers-execution"


def build_user_claims(segments: list[str], requirement_ids: list[str], task_types: list[str]) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    for index, segment in enumerate(segments, start=1):
        claim_id = f"CLAIM-{index:02d}"
        requirement_id = requirement_ids[index - 1] if index - 1 < len(requirement_ids) else ""
        flags = claim_risk_flags(segment, task_types)
        claims.append(
            {
                "claim_id": claim_id,
                "requirement_id": requirement_id,
                "claim_summary": summarize(segment, 180),
                "source": "user",
                "trust_level": "user-assertion-needs-verification" if len(flags) > 1 else "user-instruction",
                "risk_flags": flags,
                "verification_action": verification_action_for_claim(segment, task_types),
            }
        )
    return claims


def build_input_filter_task_record(args: argparse.Namespace, report: LifecycleReport) -> dict[str, Any]:
    message = message_from_args(args, Path(args.root).resolve())
    task_id = args.task_id or f"TASK-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"
    id_base = safe_id_part(task_id, "INPUT-FILTER")
    requirements = []
    requirement_ids: list[str] = []
    segments = split_requirement_segments(message)
    for index, segment in enumerate(segments, start=1):
        req_id = f"REQ-{id_base}-{index:02d}"
        requirement_ids.append(req_id)
        requirements.append(
            {
                "requirement_id": req_id,
                "summary": summarize(segment, 180),
                "record_decision": "Record in structured task record before execution.",
                "network_decision": network_decision_for(segment, report.input.source, report.classification.task_types),
                "validation_decision": validation_decision_for(
                    report.classification.task_types,
                    report.classification.changed_paths,
                ),
                "acceptance": "Final response explicitly covers this requirement.",
                "status": "open",
                "action": "Route through mandatory input-filter preflight before later processing.",
                "implementation_evidence": "Generated by lifecycle input-filter.",
                "validation_evidence": "Pending task-record gate and task-specific checks.",
                "final_coverage": "Mention completion, validation, and any blocked or deferred work.",
            }
        )

    claims = build_user_claims(segments, requirement_ids, report.classification.task_types)
    matched = ", ".join(requirement_ids) if requirement_ids else "none"
    task_types = report.classification.task_types
    task_type_set = set(task_types)
    identity = report.execution_identity
    approval_status = "approved" if args.approved_label else ("not_required" if not report.classification.requires_approval else "missing")
    execution_policy = "approved-local-only-no-push" if approval_status == "approved" else (
        "execute-no-approval-required" if approval_status == "not_required" else "block-before-write"
    )
    task = {
        "task_id": task_id,
        "title": args.title or summarize(message, 80) or "input-filter preflight task",
        "status": "active",
        "task_types": task_types,
        "task_size": report.classification.task_size,
        "summary": summarize(message, 480),
        "approval_label": args.approved_label or "",
        "trace_id": report.trace_id,
    }
    approvals = []
    if args.approved_label:
        approvals.append(
            {
                "approval_id": f"APR-{id_base}-01",
                "label": args.approved_label,
                "status": "approved",
                "summary": "Approval label supplied to lifecycle input-filter.",
            }
        )

    outputs = []
    for output_type in structured_task_record.OUTPUT_TYPES:
        outputs.append(
            {
                "output_id": f"OUT-{id_base}-{output_type}",
                "output_type": output_type,
                "applicability_scope": f"{output_type} boundary for input-filter generated task record.",
                "exclusions": "Does not claim implementation or validation before those steps run.",
                "objects": "user input, requirement rows, trigger rows, and lifecycle gates",
                "fact_source": "ai_client_governance.py lifecycle input-filter",
                "completed": "Input-filter facts generated.",
                "unfinished": "Implementation and final validation pending.",
                "unverified": "Task-specific checks pending.",
                "blocked": "None recorded at input-filter time.",
                "user_confirmation": "Use supplied approval label if mutation requires approval; otherwise none.",
                "final_coverage": "Final output must cover completed, unfinished, unverified, blocked, and Git/worktree status.",
                "trace_id": report.trace_id,
            }
        )

    payload: dict[str, Any] = {
        "task": task,
        "approvals": approvals,
        "requirements": requirements,
        "triggers": [
            {
                "trigger_id": f"TRG-{id_base}-USER-MESSAGE",
                "trigger_type": "user-message",
                "source": "latest user message",
                "matched_requirement": matched,
                "priority": "high",
                "applicability_scope": "user-message join point",
                "scope_expansion": "not expanded",
                "reason": "Every non-trivial user message must be decomposed before processing.",
                "required_action": "Run input-filter preflight and record REQ decisions.",
                "executed_steps": "Generated requirement, decision, trigger, output, and event rows.",
                "quantitative_evidence": f"{len(requirements)} requirement row(s)",
                "status": "done",
                "trace_id": report.trace_id,
            },
            {
                "trigger_id": f"TRG-{id_base}-INPUT-FILTER",
                "trigger_type": "input-filter",
                "source": "ai_client_governance.py lifecycle input-filter",
                "matched_requirement": matched,
                "priority": "high",
                "applicability_scope": "mandatory preflight filter chain",
                "scope_expansion": "not expanded",
                "reason": "Input analysis is a fail-closed governance join point.",
                "required_action": "Persist input-filter facts before write-intent, final-output, or resume gates.",
                "executed_steps": "Rendered structured task-record payload.",
                "quantitative_evidence": f"event={structured_task_record.INPUT_FILTER_PREFLIGHT_EVENT}",
                "status": "done",
                "trace_id": report.trace_id,
            },
            {
                "trigger_id": f"TRG-{id_base}-CLIENT-IDENTITY",
                "trigger_type": "client-identity",
                "source": "ai_client_governance.py lifecycle input-filter",
                "matched_requirement": matched,
                "priority": "high",
                "applicability_scope": "user-message execution context",
                "scope_expansion": "not expanded",
                "reason": "Model and client identity must be recorded so later audits can identify which runtimes skipped standard flow.",
                "required_action": "Persist client-identity.analysis with client_type and model_id before write-intent, final-output, or resume gates.",
                "executed_steps": "Captured client/model identity from CLI arguments, environment variables, or explicit unknown fallback.",
                "quantitative_evidence": f"client_type={identity.client_type}; model_id={identity.model_id}",
                "status": "done",
                "trace_id": report.trace_id,
            },
            {
                "trigger_id": f"TRG-{id_base}-COMMAND-COMPRESSION",
                "trigger_type": "command-compression",
                "source": "ai_client_governance.py lifecycle input-filter",
                "matched_requirement": matched,
                "priority": "high",
                "applicability_scope": "mandatory pre-command AOP join point",
                "scope_expansion": "not expanded",
                "reason": "Medium, large, or mutating tasks must analyze whether local commands can be deduped, batched, or run through a local runner before model-mediated step-by-step execution.",
                "required_action": "Persist command-compression.analysis before write-intent, final-output, or resume gates.",
                "executed_steps": "Rendered command compression preflight event.",
                "quantitative_evidence": f"event={structured_task_record.COMMAND_COMPRESSION_EVENT}",
                "status": "done",
                "trace_id": report.trace_id,
            },
            {
                "trigger_id": f"TRG-{id_base}-SCOPE-CLASSIFICATION",
                "trigger_type": "scope-classification",
                "source": "ai_client_governance.py lifecycle input-filter",
                "matched_requirement": matched,
                "priority": "high",
                "applicability_scope": report.classification.scope_kind,
                "scope_expansion": "not expanded",
                "reason": report.classification.scope_reason,
                "required_action": "Route common governance changes to ai-client-governance and project-specific changes to .ai-client/project or native project assets.",
                "executed_steps": "Classified changed paths and message path references before write-intent.",
                "quantitative_evidence": f"scope_paths={len(report.classification.scope_paths)}",
                "status": "done",
                "trace_id": report.trace_id,
            },
            {
                "trigger_id": f"TRG-{id_base}-PLAN-APPROVAL-BOUNDARY",
                "trigger_type": "plan-approval-boundary",
                "source": "ai_client_governance.py lifecycle input-filter",
                "matched_requirement": matched,
                "priority": "high",
                "applicability_scope": "plan-output/write-intent approval boundary",
                "scope_expansion": "not expanded",
                "reason": "User questions, approvals, local commits, and push are separate execution boundaries.",
                "required_action": "Persist plan approval boundary before write-intent or final-output gates.",
                "executed_steps": "Rendered plan-approval-boundary.analysis event.",
                "quantitative_evidence": f"approval_status={approval_status}; push_policy=push_requires_separate_approval",
                "status": "done",
                "trace_id": report.trace_id,
            },
            {
                "trigger_id": f"TRG-{id_base}-USER-CLAIM-VALIDATION",
                "trigger_type": "user-claim-validation",
                "source": "ai_client_governance.py lifecycle input-filter",
                "matched_requirement": matched,
                "priority": "high",
                "applicability_scope": "user-message claim trust boundary",
                "scope_expansion": "not expanded",
                "reason": "User requirements are goals, but user assertions may be wrong, stale, or conflict with live state.",
                "required_action": "Persist user-claim-validation.analysis before letting user assertions steer execution.",
                "executed_steps": "Rendered claim trust, risk, and verification rows.",
                "quantitative_evidence": f"{len(claims)} claim row(s)",
                "status": "done",
                "trace_id": report.trace_id,
            },
        ],
        "outputs": outputs,
        "events": [
            {
                "event_id": f"EVT-{id_base}-INPUT-FILTER",
                "event_type": structured_task_record.INPUT_FILTER_PREFLIGHT_EVENT,
                "payload": {
                    "join_point": "user-message",
                    "input_source": report.input.source,
                    "requirement_count": len(requirements),
                    "requirements": requirement_ids,
                    "task_types": task_types,
                    "task_size": report.classification.task_size,
                    "scope_kind": report.classification.scope_kind,
                    "scope_reason": report.classification.scope_reason,
                    "scope_paths": report.classification.scope_paths,
                    "filter_chain": [
                        "classify-source",
                        "user-claim-validation",
                        "client-identity",
                        "classify-common-project-scope",
                        "agent-decision",
                        "data-confirmation",
                        "shell-proxy-usage",
                        "history-requirement-recovery",
                        "readonly-side-effect-policy",
                        "decompose-requirements",
                        "recordability-judgement",
                        "network-search-judgement",
                        "acceptance-extract",
                    ],
                    "fail_policy": "fail_closed",
                },
            },
            {
                "event_id": f"EVT-{id_base}-AGENT-DECISION",
                "event_type": structured_task_record.AGENT_DECISION_EVENT,
                "payload": {
                    "join_point": "user-message",
                    "task_id": task_id,
                    "agent_group_decision": "deferred" if "multi-agent" in task_types else "not_spawned",
                    "spawn_count": 0,
                    "no_spawn_reason": (
                        "Input-filter classified this as multi-agent; dispatch evidence must be appended before final closeout."
                        if "multi-agent" in task_type_set
                        else "Input-filter selected a single-controller path; this reason must be updated if the user explicitly requires agents or the task is split."
                    ),
                    "context_pack_ref": "task-record:" + task_id,
                    "data_confirmation_evidence": f"{len(claims)} claim row(s); {len(requirements)} requirement row(s)",
                    "alternative_validation": (
                        "Pending subagent dispatch; controller must record spawned/reused/merged evidence before final gate."
                        if "multi-agent" in task_type_set
                        else "Single-controller validation path selected by lifecycle input-filter."
                    ),
                    "residual_risk": (
                        "Final multi-agent gate remains blocked until spawned/reused/merged evidence is appended."
                        if "multi-agent" in task_type_set
                        else "Subagent coverage was not used; controller owns review and validation coverage."
                    ),
                    "evaluation_scope": report.classification.scope_kind,
                    "history_scan_policy": "required for correction/rules-script/multi-agent/long-running tasks",
                    "fail_policy": "fail_closed",
                },
            },
            {
                "event_id": f"EVT-{id_base}-DATA-CONFIRMATION",
                "event_type": structured_task_record.DATA_CONFIRMATION_EVENT,
                "payload": {
                    "join_point": "user-message",
                    "confirmation_sources": ["user-message", "lifecycle-classification"],
                    "checked_facts": [
                        {
                            "fact": "requirements parsed from current user input",
                            "evidence": requirement_ids,
                        },
                        {
                            "fact": "scope classified before write-intent",
                            "evidence": report.classification.scope_kind,
                        },
                    ],
                    "unverified_items": [
                        "external historical records require explicit scan before final output"
                    ]
                    if task_type_set & {"correction", "rules-script", "multi-agent", "long-running"}
                    else [],
                    "fail_policy": "fail_closed",
                },
            },
            {
                "event_id": f"EVT-{id_base}-SHELL-PROXY-USAGE",
                "event_type": structured_task_record.SHELL_PROXY_USAGE_EVENT,
                "payload": {
                    "join_point": "write-intent",
                    "policy": (
                        "important Windows PowerShell commands must use the non-invasive "
                        "shell-adapter proxy-powershell command proxy or record a gated exception"
                    ),
                    "planned_runner": "shell-adapter proxy-powershell --powershell-command-file",
                    "enforcement_mode": "non-invasive-command-proxy",
                    "profile_policy": "no_profile",
                    "profile_touched": False,
                    "user_shell_impact": "none",
                    "used_proxy": "pending",
                    "telemetry_evidence": "",
                    "exception_reason": "",
                    "diagnostic_command": "shell-adapter diagnose --require-raw-shell-coverage",
                    "fail_policy": "fail_closed",
                },
            },
            {
                "event_id": f"EVT-{id_base}-HISTORY-REQUIREMENT-RECOVERY",
                "event_type": structured_task_record.HISTORY_REQUIREMENT_RECOVERY_EVENT,
                "payload": {
                    "join_point": "user-message",
                    "history_sources": ["current-user-message"],
                    "recovered_requirements": [requirement["summary"] for requirement in requirements],
                    "not_adopted_requirements": [],
                    "no_history_source_reason": "",
                    "no_action_reason": "",
                    "required_for_task_types": ["correction", "rules-script", "multi-agent", "long-running"],
                    "fail_policy": "fail_closed",
                },
            },
            {
                "event_id": f"EVT-{id_base}-READONLY-SIDE-EFFECT",
                "event_type": structured_task_record.READONLY_SIDE_EFFECT_EVENT,
                "payload": {
                    "join_point": "user-message",
                    "readonly_contract": False,
                    "db_write_allowed": True,
                    "record_state_allowed": True,
                    "side_effect_class": "stateful-preflight-record",
                    "dry_run_supported": False,
                    "forbidden_when_readonly": ["--record-state", "sync-check state write", "telemetry write"],
                    "fail_policy": "fail_closed",
                },
            },
            {
                "event_id": f"EVT-{id_base}-CLIENT-IDENTITY",
                "event_type": structured_task_record.CLIENT_IDENTITY_EVENT,
                "payload": {
                    "join_point": "user-message",
                    "client_type": identity.client_type,
                    "client_version": identity.client_version,
                    "model_id": identity.model_id,
                    "model_provider": identity.model_provider,
                    "identity_source": identity.identity_source,
                    "audit_goal": "Correlate task-record and telemetry outcomes by AI client and model to find runtimes that do not follow the standard flow.",
                    "fail_policy": "fail_closed_if_missing_event",
                },
            },
            {
                "event_id": f"EVT-{id_base}-COMMAND-COMPRESSION",
                "event_type": structured_task_record.COMMAND_COMPRESSION_EVENT,
                "payload": {
                    "join_point": "user-message",
                    "task_id": task_id,
                    "requirements": requirement_ids,
                    "task_types": task_types,
                    "task_size": report.classification.task_size,
                    "changed_paths": report.classification.changed_paths,
                    "scope_kind": report.classification.scope_kind,
                    "scope_reason": report.classification.scope_reason,
                    "scope_paths": report.classification.scope_paths,
                    "decision": "Analyze new local command candidates for dedupe, batching, cache eligibility, task-run DAG execution, gate-pool use, and telemetry wrapping before write-intent.",
                    "selected_pattern": "lifecycle-preflight-command-compression",
                    "groups": [
                        {
                            "group_id": "preflight-readonly",
                            "execution": "parallel-ok",
                            "cache": "readonly-only",
                            "commands": ["contract describe", "runtime components", "context-extract"],
                        },
                        {
                            "group_id": "stateful-write",
                            "execution": "ordered",
                            "cache": "disabled",
                            "commands": ["task-record apply", "worktree-task closeout-all", "git commit"],
                        },
                    ],
                    "recommended_command": "python .ai-client/ai-client-governance/scripts/ai_client_governance.py task-run plan --task-id <task-id> --event write-intent",
                    "recommended_runner": "python .ai-client/ai-client-governance/scripts/ai_client_governance.py task-run run --task-id <task-id> --event write-intent",
                    "recommended_diagnostics": "python .ai-client/ai-client-governance/scripts/ai_client_governance.py task-run diagnose --format json",
                    "fail_policy": "fail_closed",
                },
            },
            {
                "event_id": f"EVT-{id_base}-PLAN-APPROVAL-BOUNDARY",
                "event_type": structured_task_record.PLAN_APPROVAL_BOUNDARY_EVENT,
                "payload": {
                    "join_point": "plan-output",
                    "requires_approval": report.classification.requires_approval,
                    "approval_label": args.approved_label or "",
                    "approval_status": approval_status,
                    "execution_policy": execution_policy,
                    "push_policy": "push_requires_separate_approval",
                    "commit_policy": "local_commit_allowed_when_approved",
                    "fail_policy": "fail_closed",
                },
            },
            {
                "event_id": f"EVT-{id_base}-USER-CLAIM-VALIDATION",
                "event_type": structured_task_record.USER_CLAIM_VALIDATION_EVENT,
                "payload": {
                    "join_point": "user-message",
                    "claims": claims,
                    "execution_policy": "verify-first" if any(len(claim.get("risk_flags", [])) > 1 for claim in claims) else "execute-with-recorded-claims",
                    "fail_policy": "fail_closed",
                },
            },
            {
                "event_id": f"EVT-{id_base}-STATE-ARTIFACT-OWNERSHIP",
                "event_type": structured_task_record.STATE_ARTIFACT_OWNERSHIP_EVENT,
                "payload": {
                    "join_point": "write-intent",
                    "owner_policy": "script-generated state must be repaired by its owner command or by fixing that command first",
                    "generated_state_classes": [
                        "coord-session",
                        "worktree-lock",
                        "lifecycle-state",
                        "doc-index",
                        "python-pycache",
                        "selftest-artifact",
                    ],
                    "manual_edit_policy": "forbidden_without_break_glass",
                    "cleanup_policy": "use owner cleanup/reconcile/finalize commands; do not hand-edit runtime telemetry",
                    "fail_policy": "fail_closed",
                },
            },
            {
                "event_id": f"EVT-{id_base}-PATCH-PREFLIGHT",
                "event_type": structured_task_record.PATCH_PREFLIGHT_EVENT,
                "payload": {
                    "join_point": "write-intent",
                    "anchor_policy": "verify_unique_or_reextract",
                    "apply_policy": "small_step_patch",
                    "fallback_policy": "use structured parser or narrower context when anchors are unstable",
                    "fail_policy": "fail_closed",
                },
            },
            {
                "event_id": f"EVT-{id_base}-ANALYSIS-CONTRACT",
                "event_type": structured_task_record.ANALYSIS_CONTRACT_EVENT,
                "payload": {
                    "join_point": "preflight",
                    "analysis_summary": args.analysis_summary
                    or "Lifecycle input-filter generated a preflight analysis contract.",
                    "scope": args.analysis_scope
                    or ", ".join(report.classification.scope_paths)
                    or report.classification.scope_kind,
                    "non_goals": args.non_goal
                    or "No implementation is claimed by input-filter generation.",
                    "risks": args.risk
                    or "Generated preflight facts must be validated before write-intent.",
                    "acceptance": args.acceptance
                    or "task-record preflight gate passes after required facts are applied.",
                    "validation_budget": {
                        "profile": args.completion_profile,
                        "budget_seconds": args.budget_seconds,
                        "allow_expensive": args.allow_expensive,
                    },
                    "fail_policy": "fail_closed",
                },
            },
            {
                "event_id": f"EVT-{id_base}-CAPABILITY-GATEWAY",
                "event_type": structured_task_record.CAPABILITY_GATEWAY_FACTS_EVENT,
                "payload": {
                    "join_point": "host-capability-boundary",
                    "capability_fact_kind": "registration",
                    "control_layer": "plugin",
                    "enforcement_level": "audit_only",
                    "hard_enforcement_available": False,
                    "registration_event": True,
                    "invocation_telemetry_required": True,
                    "residual_risk": (
                        "This event records the plugin entrypoint capability boundary. It does not prove "
                        "host-native shell or tool calls outside governed wrappers were intercepted."
                    ),
                    "lifecycle_input_filter_enforced": True,
                    "prewrite_runtime_adapter": "task-record gate --event preflight",
                    "runtime_adapter_components": [
                        "client.runtime.host-capability-gateway",
                        "input.filter.user-message-preflight",
                        "prewrite.gate.approved-task-worktree",
                        "preflight.interceptor.raw-shell-coverage",
                    ],
                    "shell_enforcement_mode": "non-invasive-command-proxy",
                    "shell_control_layer": "plugin-command-wrapper",
                    "shell_enforcement_scope": "governed_commands_only",
                    "shell_command_proxy": "shell-adapter proxy-powershell",
                    "raw_host_shell_interception": False,
                    "profile_policy": "no_profile",
                    "profile_touched": False,
                    "user_shell_impact": "none",
                    "raw_shell_gap_policy": (
                        "Fail closed for governed commands unless no-profile command proxy, local env "
                        "activation, or an explicit gated exception is recorded; host-native raw shell "
                        "prevention requires host-client integration."
                    ),
                    "client_runtime_scope": report.classification.scope_kind,
                    "fail_policy": "fail_closed",
                },
            },
        ],
    }
    return payload


def write_input_filter_payload(root: Path, payload: dict[str, Any], output: str) -> str:
    path = Path(output)
    if not path.is_absolute():
        path = root / path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return rel_path(path, root)


def apply_input_filter_payload(root: Path, args: argparse.Namespace, payload: dict[str, Any]) -> dict[str, Any]:
    db = structured_task_record.db_path(root, args.db)
    con = structured_task_record.connect(db, create=True)
    structured_task_record.init_db(con)
    task_id = structured_task_record.apply_payload(con, payload, replace=args.replace)
    return {"db": str(db), "task_id": task_id, "applied": True}


def trust_for_source(source: str) -> tuple[str, bool]:
    if source == "web":
        return "external-web", True
    if source == "file":
        return "repository-file", False
    if source == "tool":
        return "tool-output", False
    if source == "agent":
        return "delegated-agent-output", False
    if source == "history":
        return "session-history", False
    return "user-instruction", False


def contains_keyword(text: str, keyword: str) -> bool:
    if re.search(r"[\u4e00-\u9fff]", keyword):
        return keyword in text
    return re.search(rf"\b{re.escape(keyword.lower())}\b", text.lower()) is not None


def infer_task_types(text: str, changed_paths: list[str], explicit: list[str]) -> list[str]:
    found: list[str] = []
    for task_type in explicit:
        normalized = task_type.strip()
        if normalized and normalized not in found:
            found.append(normalized)

    path_blob = " ".join(changed_paths)
    haystack = f"{text}\n{path_blob}"
    for task_type, keywords in TASK_KEYWORDS.items():
        if any(contains_keyword(haystack, keyword) for keyword in keywords):
            if task_type not in found:
                found.append(task_type)

    suffixes = {Path(path).suffix.lower() for path in changed_paths}
    if ".md" in suffixes and "docs" not in found:
        found.append("docs")
    if ".py" in suffixes and "rules-script" not in found:
        found.append("rules-script")
    adapter_path_signals = (
        "AGENTS.md",
        "CLAUDE.md",
        "GEMINI.md",
        "CONVENTIONS.md",
        ".github/copilot-instructions.md",
        ".github/instructions/",
        ".cursor/rules/",
        ".clinerules/",
        ".windsurf/rules/",
        ".continue/rules/",
        ".roo/rules/",
        ".trae/rules/",
    )
    if any(path.startswith(".ai-client/ai-client-governance") or path.endswith(adapter_path_signals) for path in changed_paths):
        if "rules-script" not in found:
            found.append("rules-script")
    return found


def estimate_task_size(
    text: str,
    task_types: list[str],
    changed_paths: list[str],
    input_source: str,
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    md_count = sum(1 for path in changed_paths if Path(path).suffix.lower() == ".md")
    py_count = sum(1 for path in changed_paths if Path(path).suffix.lower() == ".py")

    if len(task_types) >= 3:
        reasons.append(f"task types >= 3 ({len(task_types)})")
    if len(changed_paths) > 3:
        reasons.append(f"changed paths > 3 ({len(changed_paths)})")
    if {"rules-script", "docs"}.issubset(task_types):
        reasons.append("rules-script and docs gates both apply")
    if any(path.startswith(".ai-client/ai-client-governance") for path in changed_paths):
        reasons.append("embedded ai-client-governance repository is in scope")
    if any(keyword in text for keyword in ["全部", "批量", "状态机", "流水线", "主从", "生命周期"]):
        reasons.append("request contains broad workflow architecture signals")
    if md_count >= 3 or py_count >= 2:
        reasons.append("multiple markdown or python files are in scope")

    if reasons:
        return "large", reasons

    medium_reasons: list[str] = []
    if task_types and any(task_type in MUTATING_TYPES for task_type in task_types):
        medium_reasons.append("mutating or gated task type is present")
    if changed_paths:
        medium_reasons.append(f"changed paths present ({len(changed_paths)})")
    if input_source == "web":
        medium_reasons.append("external web input needs citation boundary")
    if len(text) > 600:
        medium_reasons.append("input text is long enough to need routing")

    if medium_reasons:
        return "medium", medium_reasons
    return "small", ["read-only or low-blast-radius input"]


def required_hooks_and_gates(
    task_types: list[str],
    task_size: str,
    changed_paths: list[str],
    input_source: str,
    event: str,
) -> tuple[list[str], list[str], bool, bool]:
    context = AgentExecutionContext(
        input_source=input_source,
        task_types=tuple(task_types),
        task_size=task_size,
        changed_paths=tuple(changed_paths),
        final=False,
        event=event,
    )
    registry = default_registry()
    hooks = registry.mechanism_labels_for_context(context)
    gates = registry.gate_labels_for_context(context)
    requires_tracking = requires_tracking_for(task_types, task_size)
    requires_approval = requires_approval_for(task_types, changed_paths=changed_paths)
    return unique(hooks), unique(gates), requires_tracking, requires_approval


def unique(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def build_classification(args: argparse.Namespace, root: Path) -> tuple[InputRecord, Classification]:
    message = message_from_args(args, root)
    changed_paths = normalize_paths(getattr(args, "changed_path", None))
    explicit_types = normalize_paths(getattr(args, "task_type", None))
    trust, needs_citation = trust_for_source(args.input_source)
    input_record = InputRecord(
        source=args.input_source,
        trust=trust,
        needs_citation=needs_citation,
        summary=summarize(message),
        derived_from=normalize_paths(getattr(args, "derived_from", None)),
    )
    task_types = infer_task_types(message, changed_paths, explicit_types)
    task_size, reasons = estimate_task_size(message, task_types, changed_paths, args.input_source)
    event = lifecycle_event(args)
    scope = classify_scope(root=root, paths=changed_paths, command=message)
    hooks, gates, requires_tracking, requires_approval = required_hooks_and_gates(
        task_types, task_size, changed_paths, args.input_source, event
    )
    return input_record, Classification(
        task_types=task_types,
        task_size=task_size,
        task_size_reasons=reasons,
        runtime_event=event,
        requires_tracking=requires_tracking,
        requires_approval=requires_approval,
        required_hooks=hooks,
        required_gates=gates,
        changed_paths=changed_paths,
        scope_kind=scope.scope_kind,
        scope_reason=scope.scope_reason,
        scope_paths=scope.paths,
    )


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", default=".", help="Repository root. Default: current directory.")
    parser.add_argument(
        "--input-source",
        choices=("user", "web", "file", "tool", "agent", "history"),
        default="user",
        help="Input source being routed through the lifecycle.",
    )
    parser.add_argument("--message", default="", help="Input message or short task description.")
    parser.add_argument("--message-file", help="Read input text from a UTF-8 file.")
    parser.add_argument("--derived-from", action="append", default=[], help="Source URL/path/trace this input came from.")
    parser.add_argument("--client-type", default="", help="AI client/runtime name, e.g. codex, claude-code, trae, cursor.")
    parser.add_argument("--client-version", default="", help="AI client/runtime version, if available.")
    parser.add_argument("--model", default="", help="Current model identifier, if available.")
    parser.add_argument("--model-provider", default="", help="Current model provider, if available.")
    parser.add_argument("--changed-path", action="append", default=[], help="Path changed or expected to change.")
    parser.add_argument("--task-type", action="append", default=[], help="Explicit task type override/addition.")
    parser.add_argument("--task-tracking", help="Task tracking file for gated work.")
    parser.add_argument("--task-id", help="Structured task-record id for gated work.")
    parser.add_argument("--db", help="Structured task-record SQLite path.")
    parser.add_argument("--approved-label", help="Approval label, for example 批准：计划-生命周期状态机门禁.")
    parser.add_argument("--trace-id", help="Trace id. Default: generated.")
    parser.add_argument("--completion-profile", choices=("fast", "full"), default="fast", help="Validation profile used by analysis-contract preflight.")
    parser.add_argument("--budget-seconds", type=int, help="Maximum required validation seconds allowed before write-intent.")
    parser.add_argument("--allow-expensive", action="store_true", help="Allow required validation checks to exceed the declared budget.")
    parser.add_argument("--analysis-summary", default="", help="One-sentence task understanding before write-intent.")
    parser.add_argument("--analysis-scope", action="append", default=[], help="Explicit analysis scope before write-intent.")
    parser.add_argument("--non-goal", action="append", default=[], help="Explicit non-goal or excluded scope.")
    parser.add_argument("--risk", action="append", default=[], help="Known risk or uncertainty before execution.")
    parser.add_argument("--acceptance", action="append", default=[], help="User-visible acceptance criterion.")
    parser.add_argument(
        "--record-state",
        action="store_true",
        help="Persist lifecycle state in .ai-client/project/state/aicg.db.",
    )
    parser.add_argument("--event", choices=sorted(NODE_EVENTS), help="Runtime event boundary. Default is inferred from lifecycle command.")
    parser.add_argument("--format", choices=("text", "json"), default="text", help="Output format.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Route AI maintenance tasks through an executable lifecycle.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    input_filter = subparsers.add_parser("input-filter", help="Run mandatory user-message input filter and emit task-record facts.")
    add_common_args(input_filter)
    input_filter.add_argument("--title", help="Task title for generated task-record payload.")
    input_filter.add_argument("--task-record-json", help="Write generated task-record JSON to this path.")
    input_filter.add_argument("--apply-task-record", action="store_true", help="Apply generated task-record payload to SQLite.")
    input_filter.add_argument("--replace", action="store_true", help="Replace an existing task when applying generated payload.")

    classify = subparsers.add_parser("classify", help="Classify input and print lifecycle routing.")
    add_common_args(classify)

    preflight = subparsers.add_parser("preflight", help="Validate pre-execution lifecycle requirements.")
    add_common_args(preflight)

    finalize = subparsers.add_parser("finalize", help="Run or plan final lifecycle gates.")
    add_common_args(finalize)
    finalize.add_argument("--run-gates", action="store_true", help="Run ai_client_governance.py gate-pool and doc-index checks.")
    finalize.add_argument("--dry-run", action="store_true", help="Print final gate commands without running them.")

    status = subparsers.add_parser("status", help="Read lifecycle state from SQLite.")
    status.add_argument("--root", default=".", help="Repository root. Default: current directory.")
    status.add_argument("--trace-id", required=True, help="Trace id to read.")
    status.add_argument("--db", help="SQLite database path. Default: <ai-client-project>/state/aicg.db.")
    status.add_argument("--format", choices=("text", "json"), default="text", help="Output format.")
    return parser.parse_args()


def default_trace_id() -> str:
    return f"{DEFAULT_TRACE_PREFIX}-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"


def lifecycle_event(args: argparse.Namespace) -> str:
    """Infer the runtime event boundary for this lifecycle command."""
    if getattr(args, "event", None):
        return args.event
    command = getattr(args, "command", "")
    if command == "input-filter":
        return "user-message"
    if command == "classify":
        return "user-message"
    if command == "preflight":
        return "write-intent" if getattr(args, "changed_path", None) or getattr(args, "task_type", None) else "plan-output"
    if command == "finalize":
        return "final-output"
    return ""


def validate_tracking(
    root: Path,
    args: argparse.Namespace,
    classification: Classification,
    errors: list[Finding],
    warnings: list[Finding],
) -> None:
    tracking_arg = getattr(args, "task_tracking", None)
    if classification.requires_tracking and not tracking_arg and not getattr(args, "task_id", None):
        errors.append(Finding("error", "task tracking is required for this lifecycle route."))
        return
    if not tracking_arg:
        return
    tracking = Path(tracking_arg)
    if not tracking.is_absolute():
        tracking = root / tracking
    if not tracking.exists():
        errors.append(Finding("error", f"task tracking file does not exist: {rel_path(tracking, root)}"))
        return
    text = read_text_file(tracking)
    required_sections = ["用户要求追踪门禁", "要求触发日志", "任务类型门禁"]
    if "rules-script" in classification.task_types:
        required_sections.extend(["联网核对记录", "脚本能力适配门禁"])
    if "docs" in classification.task_types:
        required_sections.extend(["影响面扫描", "Definition of Done"])
    for heading in required_sections:
        if not re.search(rf"^##\s+{re.escape(heading)}\s*$", text, re.MULTILINE):
            errors.append(Finding("error", f"task tracking lacks section: {heading}"))
    label = getattr(args, "approved_label", None)
    if classification.requires_approval:
        if not label:
            warnings.append(Finding("warning", "approval is required but --approved-label was not provided."))
        elif label not in text:
            warnings.append(Finding("warning", f"approval label is not mirrored in tracking: {label}"))


def validate_structured_record(
    root: Path,
    args: argparse.Namespace,
    classification: Classification,
    phase: str,
    errors: list[Finding],
    warnings: list[Finding],
    notes: list[Finding],
) -> None:
    task_id = getattr(args, "task_id", None)
    if classification.requires_tracking and not task_id:
        errors.append(
            Finding(
                "error",
                "structured task id is required for this lifecycle route; run lifecycle input-filter and task-record apply first.",
            )
        )
        return
    if not task_id:
        return
    db = structured_task_record.db_path(root, getattr(args, "db", None))
    if not db.exists():
        errors.append(Finding("error", f"structured task-record DB does not exist: {rel_path(db, root)}"))
        return
    con = structured_task_record.connect(db, create=False)
    event = "final" if phase == "finalize" else "preflight"
    report = structured_task_record.validate_task(con, db, task_id, event, classification.task_types)
    for item in report.errors:
        location = f" [{item.table}{':' + item.row_id if item.row_id else ''}]" if item.table else ""
        errors.append(Finding("error", f"task-record {event} gate: {item.message}{location}"))
    for item in report.warnings:
        location = f" [{item.table}{':' + item.row_id if item.row_id else ''}]" if item.table else ""
        warnings.append(Finding("warning", f"task-record {event} gate: {item.message}{location}"))
    if not report.errors:
        notes.append(Finding("note", f"task-record {event} gate passed for {task_id}"))


def validate_analysis_contract(
    root: Path,
    args: argparse.Namespace,
    classification: Classification,
    errors: list[Finding],
    notes: list[Finding],
) -> None:
    """Fail preflight when analysis scope, risk, acceptance, or budget is unclear."""
    context = AgentExecutionContext(
        input_source=getattr(args, "input_source", "user"),
        task_types=tuple(classification.task_types),
        task_size=classification.task_size,
        changed_paths=tuple(classification.changed_paths),
        final=False,
        event=classification.runtime_event,
    )
    if "analysis-contract" not in default_registry().gate_step_ids_for_context(context):
        return
    contract_args = argparse.Namespace(
        analysis_summary=getattr(args, "analysis_summary", ""),
        analysis_scope=getattr(args, "analysis_scope", []),
        non_goal=getattr(args, "non_goal", []),
        risk=getattr(args, "risk", []),
        acceptance=getattr(args, "acceptance", []),
    )
    contract = completion.build_analysis_contract(contract_args, classification.changed_paths)
    checks = completion.planned_checks(
        classification.task_types,
        classification.changed_paths,
        profile=getattr(args, "completion_profile", "fast"),
    )
    budget = completion.build_validation_budget(
        checks,
        profile=getattr(args, "completion_profile", "fast"),
        budget_seconds=getattr(args, "budget_seconds", None),
        allow_expensive=bool(getattr(args, "allow_expensive", False)),
    )
    if contract.missing_fields:
        errors.append(
            Finding(
                "error",
                "analysis contract is incomplete before write-intent: " + ", ".join(contract.missing_fields),
                "ai_client_governance.py lifecycle",
            )
        )
    if budget.blocked_by_budget:
        errors.append(
            Finding(
                "error",
                "validation budget is exceeded before write-intent: "
                f"required={budget.estimated_required_seconds}s budget={budget.budget_seconds}s",
                "ai_client_governance.py lifecycle",
            )
        )
    if not contract.missing_fields and not budget.blocked_by_budget:
        notes.append(
            Finding(
                "note",
                f"analysis contract passed; validation budget {budget.estimated_required_seconds}s/{budget.budget_seconds}s",
            )
        )


def build_report(args: argparse.Namespace, phase: str, state: str) -> LifecycleReport:
    root = Path(args.root).resolve()
    trace_id = args.trace_id or default_trace_id()
    input_record, classification = build_classification(args, root)
    execution_identity = execution_identity_from_args(args)
    errors: list[Finding] = []
    warnings: list[Finding] = []
    notes: list[Finding] = []

    if phase in {"preflight", "finalize"}:
        validate_tracking(root, args, classification, errors, warnings)
        validate_structured_record(root, args, classification, phase, errors, warnings, notes)
    if phase == "preflight":
        validate_analysis_contract(root, args, classification, errors, notes)
    if input_record.needs_citation and not input_record.derived_from:
        warnings.append(Finding("warning", "web input should record --derived-from source URL(s)."))
    if classification.task_size == "large":
        notes.append(Finding("note", "large task: use tracking, explicit gates, and avoid broad unplanned edits."))
    elif classification.task_size == "small":
        notes.append(Finding("note", "small task: lightweight answer path is acceptable unless files change."))

    report = LifecycleReport(
        schema_version=SCHEMA_VERSION,
        trace_id=trace_id,
        phase=phase,
        state=state,
        updated_at=utc_now(),
        input=input_record,
        execution_identity=execution_identity,
        classification=classification,
        errors=errors,
        warnings=warnings,
        notes=notes,
    )
    return report


def final_gate_commands(args: argparse.Namespace, report: LifecycleReport) -> list[list[str]]:
    root = Path(args.root).resolve()
    py = sys.executable
    entrypoint = ai_client_governance_entrypoint()
    commands: list[list[str]] = []
    changed_paths = report.classification.changed_paths
    final_context = AgentExecutionContext(
        input_source="tool",
        task_types=tuple(report.classification.task_types),
        task_size=report.classification.task_size,
        changed_paths=tuple(changed_paths),
        final=True,
        event="final-output",
    )
    gate_steps = set(default_registry().gate_step_ids_for_context(final_context))
    if "doc-index" in gate_steps and not args.task_tracking:
        commands.append(
            [
                py,
                str(entrypoint),
                "doc-index",
                "check",
                "--root",
                str(root),
                "--rebuild",
                *sum((["--changed-path", path] for path in changed_paths), []),
                "--format",
                "text",
            ]
        )
    if args.task_id:
        gate_command = [
            py,
            str(entrypoint),
            "gate-pool",
            "--root",
            str(root),
            "--task-id",
            args.task_id,
            "--trace-id",
            report.trace_id,
            "--final",
            "--event",
            "final-output",
        ]
        if args.db:
            gate_command.extend(["--db", args.db])
        for task_type in report.classification.task_types:
            gate_command.extend(["--task-type", task_type])
        for path in changed_paths:
            gate_command.extend(["--changed-path", path])
        commands.append(gate_command)
    elif args.task_tracking:
        gate_command = [
            py,
            str(entrypoint),
            "gate-pool",
            "--root",
            str(root),
            "--task-tracking",
            args.task_tracking,
            "--trace-id",
            report.trace_id,
            "--final",
            "--event",
            "final-output",
        ]
        for task_type in report.classification.task_types:
            gate_command.extend(["--task-type", task_type])
        for path in changed_paths:
            gate_command.extend(["--changed-path", path])
        commands.append(gate_command)
    return commands


def save_state(report: LifecycleReport, args: argparse.Namespace) -> None:
    root = Path(args.root).resolve()
    db = state_store.db_path(root, getattr(args, "db", None))
    con = state_store.connect(db)
    data = asdict(report)
    data["state_db"] = rel_path(db, root)
    state_store.upsert_state(
        con,
        state_type="lifecycle",
        state_key=report.trace_id,
        payload=data,
        source_command="ai_client_governance.py lifecycle",
        summary=f"lifecycle {report.phase} {report.state}",
        event_type="lifecycle.state_recorded",
    )
    report.state_db = rel_path(db, root)


def should_save_state(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "record_state", False))


def run_command(command: list[str], root: Path) -> tuple[int, str]:
    env = os.environ.copy()
    env["PYTHONPYCACHEPREFIX"] = str(root / PYTHON_PYCACHE_DIR)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    proc = subprocess.run(
        command,
        cwd=root,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return proc.returncode, proc.stdout


def run_final_gates(args: argparse.Namespace, report: LifecycleReport) -> None:
    root = Path(args.root).resolve()
    commands = final_gate_commands(args, report)
    report.gate_commands = commands
    if args.dry_run or not args.run_gates:
        return
    for command in commands:
        code, output = run_command(command, root)
        if code != 0:
            report.errors.append(
                Finding(
                    "error",
                    f"final gate failed with exit {code}: {' '.join(command)}\n{output.strip()}",
                )
            )
        else:
            report.notes.append(
                Finding("note", f"final gate passed: {' '.join(command)}\n{summarize(output, 360)}")
            )


def format_json(report: LifecycleReport) -> str:
    return json.dumps(asdict(report), ensure_ascii=False, indent=2, sort_keys=True)


def format_text(report: LifecycleReport) -> str:
    c = report.classification
    lines = [
        f"Lifecycle phase: {report.phase}",
        f"State: {report.state}",
        f"Trace: {report.trace_id}",
        f"Input: {report.input.source} ({report.input.trust})",
        f"Client/model: {report.execution_identity.client_type} / {report.execution_identity.model_id}",
        f"Needs citation: {str(report.input.needs_citation).lower()}",
        f"Task types: {', '.join(c.task_types) if c.task_types else 'none'}",
        f"Task size: {c.task_size}",
        f"Task size reasons: {'; '.join(c.task_size_reasons)}",
        f"Runtime event: {c.runtime_event or 'any'}",
        f"Requires tracking: {str(c.requires_tracking).lower()}",
        f"Requires approval: {str(c.requires_approval).lower()}",
        f"Registered mechanisms: {', '.join(c.required_hooks)}",
        f"Registered gates: {', '.join(c.required_gates) if c.required_gates else 'none'}",
    ]
    if c.changed_paths:
        lines.append(f"Changed paths: {', '.join(c.changed_paths)}")
    if report.state_db:
        lines.append(f"State DB: {report.state_db}")
    if report.gate_commands:
        lines.append("Gate commands:")
        for command in report.gate_commands:
            lines.append(f"  - {' '.join(command)}")
    for title, findings in (("Errors", report.errors), ("Warnings", report.warnings), ("Notes", report.notes)):
        lines.append(f"{title}: {len(findings)}")
        for finding in findings:
            lines.append(f"  - {finding.message}")
    return "\n".join(lines)


def read_status(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    db = state_store.db_path(root, args.db)
    con = state_store.connect(db, create=False)
    row = state_store.read_state(con, state_type="lifecycle", state_key=args.trace_id)
    if row is None:
        print(f"lifecycle state not found: {args.trace_id}", file=sys.stderr)
        return 1
    data: Any = row["payload"]
    if args.format == "json":
        print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"Lifecycle state: {data.get('state')}")
        print(f"Phase: {data.get('phase')}")
        print(f"Trace: {data.get('trace_id')}")
        identity = data.get("execution_identity", {})
        print(f"Client/model: {identity.get('client_type', 'unknown')} / {identity.get('model_id', 'unknown')}")
        classification = data.get("classification", {})
        print(f"Task size: {classification.get('task_size')}")
        print(f"Task types: {', '.join(classification.get('task_types', []))}")
    return 0


def run_input_filter(args: argparse.Namespace) -> int:
    report = build_report(args, phase="input-filter", state="input_filter_checked")
    root = Path(args.root).resolve()
    if not message_from_args(args, root):
        report.errors.append(Finding("error", "input-filter requires --message or --message-file."))
    payload = build_input_filter_task_record(args, report)
    result: dict[str, Any] = {
        "input_filter": asdict(report),
        "task_record": payload,
        "task_record_json": None,
        "applied_task_record": None,
    }
    if args.task_record_json:
        result["task_record_json"] = write_input_filter_payload(root, payload, args.task_record_json)
    if args.apply_task_record and not report.errors:
        try:
            result["applied_task_record"] = apply_input_filter_payload(root, args, payload)
        except (OSError, sqlite3.Error, ValueError) as exc:
            report.errors.append(Finding("error", f"failed to apply generated task-record payload: {exc}"))
            result["input_filter"] = asdict(report)
    if should_save_state(args):
        save_state(report, args)
        result["input_filter"] = asdict(report)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) if args.format == "json" else format_text(report))
    return 1 if report.errors else 0


def main() -> int:
    args = parse_args()
    if args.command == "status":
        return read_status(args)
    if args.command == "input-filter":
        return run_input_filter(args)

    phase = args.command
    state_by_phase = {
        "classify": "classified",
        "preflight": "preflight_checked",
        "finalize": "finalizing",
    }
    report = build_report(args, phase=phase, state=state_by_phase[phase])
    if args.command == "finalize":
        run_final_gates(args, report)
        if not report.errors:
            report.state = "finalized" if args.run_gates else "final_gate_planned"
    if should_save_state(args):
        save_state(report, args)
    print(format_json(report) if args.format == "json" else format_text(report))
    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
