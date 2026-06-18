#!/usr/bin/env python3
"""Describe structured input contracts before task execution."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from typing import Any

from ai_client_governance.records.task_record import (
    APPROVAL_STATUSES,
    KNOWN_TASK_TYPES,
    MUTATING_TASK_TYPES,
    OUTPUT_TYPES,
    PATCH_PREFLIGHT_EVENT,
    PLAN_APPROVAL_BOUNDARY_EVENT,
    REQUIREMENT_STATUSES,
    STATE_ARTIFACT_OWNERSHIP_EVENT,
    TASK_STATUSES,
    USER_CLAIM_VALIDATION_EVENT,
    VALIDATION_RESULTS,
    WORKTREE_COMMIT_STATUSES,
    WORKTREE_CREATION_METHODS,
    WORKTREE_MERGE_STATUSES,
    WORKTREE_PUSH_STATUSES,
    WORKTREE_REPOS,
    WORKTREE_STATUSES,
)


ALL_TASK_TYPES = sorted(KNOWN_TASK_TYPES)


@dataclass
class FieldSpec:
    path: str
    required: bool
    type: str
    allowed: list[str]
    reason: str


@dataclass
class Contract:
    schema_version: int
    event: str
    task_types: list[str]
    required_tables: list[str]
    fields: list[FieldSpec]
    gate_requirements: list[str]
    write_commands: list[str]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Describe typed AI Client Governance task-record contracts.")
    sub = parser.add_subparsers(dest="command", required=True)
    describe = sub.add_parser("describe", help="Print required structured fields before execution.")
    describe.add_argument("--task-type", action="append", default=[], choices=ALL_TASK_TYPES)
    describe.add_argument(
        "--event",
        choices=("user-message", "plan-output", "write-intent", "completion-test", "final-output", "resume"),
        default="write-intent",
    )
    describe.add_argument("--format", choices=("text", "json"), default="text")
    return parser.parse_args()


def unique(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def field(path: str, required: bool, field_type: str, reason: str, allowed: list[str] | None = None) -> FieldSpec:
    return FieldSpec(path=path, required=required, type=field_type, allowed=allowed or [], reason=reason)


def build_contract(task_types: list[str], event: str) -> Contract:
    normalized = unique(task_types)
    mutating = bool(set(normalized) & MUTATING_TASK_TYPES)
    final = event == "final-output"
    required_tables = ["tasks", "requirements", "triggers", "outputs", "events"]
    if mutating:
        required_tables.append("worktrees")
    if final or normalized:
        required_tables.append("validations")
    if "rules-script" in normalized:
        required_tables.append("approvals")

    fields = [
        field("task.task_id", True, "string", "Stable primary key used by gates and follow-up records."),
        field("task.title", True, "string", "Human-readable task title."),
        field("task.status", True, "enum", "Lifecycle state.", list(TASK_STATUSES)),
        field("task.task_size", False, "string", "Task size estimate; defaults to medium when omitted."),
        field("task.task_types", True, "array<enum>", "Gate routing.", ALL_TASK_TYPES),
        field("task.summary", False, "string", "Short task summary."),
        field("task.approval_label", "rules-script" in normalized, "string", "Required when file changes need explicit approval."),
        field("task.trace_id", False, "string", "Optional trace id shared by rows from the same run."),
        field("task.created_at", False, "datetime", "Creation time; defaults to now when omitted."),
        field("approvals[].approval_id", False, "string", "Stable approval row id; generated when omitted."),
        field("approvals[].label", "rules-script" in normalized, "string", "Approval label. Required for rules-script tasks."),
        field("approvals[].status", "rules-script" in normalized, "enum", "Approval decision.", list(APPROVAL_STATUSES)),
        field("approvals[].summary", False, "string", "Approval context or source message."),
        field("approvals[].created_at", False, "datetime", "Approval time; defaults to now when omitted."),
        field("requirements[].requirement_id", True, "string", "Stable REQ/UR id."),
        field("requirements[].summary", True, "string", "User requirement summary."),
        field("requirements[].record_decision", True, "string", "Where this requirement is recorded."),
        field("requirements[].network_decision", True, "string", "Search/citation decision before execution."),
        field("requirements[].validation_decision", True, "string", "Validation plan before execution."),
        field("requirements[].acceptance", True, "string", "Acceptance/final response coverage."),
        field("requirements[].status", True, "enum", "Requirement state.", list(REQUIREMENT_STATUSES)),
        field("requirements[].action", True, "string", "Implementation action."),
        field("requirements[].implementation_evidence", True, "string", "File, DB, command, or state evidence."),
        field("requirements[].validation_evidence", True, "string", "Validation evidence."),
        field("requirements[].final_coverage", True, "string", "What final answer must say."),
        field("triggers[].trigger_id", True, "string", "Stable trigger row id."),
        field("triggers[].trigger_type", True, "string", "Rule, user-request, or workflow trigger type."),
        field("triggers[].source", True, "string", "Source document, rule, command, or user message."),
        field("triggers[].matched_requirement", True, "string", "REQ/UR or rule matched by this trigger."),
        field("triggers[].priority", True, "string", "Highest-priority judgement."),
        field("triggers[].applicability_scope", True, "string", "Scope boundary."),
        field("triggers[].scope_expansion", True, "string", "Whether scope expanded."),
        field("triggers[].reason", True, "string", "Why the trigger applies."),
        field("triggers[].required_action", True, "string", "Action required by this trigger."),
        field("triggers[].executed_steps", True, "string", "Steps already executed for this trigger."),
        field("triggers[].quantitative_evidence", True, "string", "Count, row, path, or command evidence."),
        field("triggers[].status", True, "string", "Trigger handling status."),
        field("triggers[].trace_id", True, "string", "Trace id linking trigger evidence to execution."),
        field("outputs[].output_id", True, "string", "Stable output row id."),
        field("outputs[].output_type", True, "enum", "Output boundary covered by final gates.", OUTPUT_TYPES),
        field("outputs[].applicability_scope", True, "string", "What this output covers."),
        field("outputs[].exclusions", True, "string", "What this output does not cover."),
        field("outputs[].objects", True, "string", "Concrete files, rows, commands, or user-facing objects covered."),
        field("outputs[].fact_source", True, "string", "Where facts came from."),
        field("outputs[].completed", True, "string", "Completed items."),
        field("outputs[].unfinished", True, "string", "Unfinished items, or explicit none."),
        field("outputs[].unverified", True, "string", "Unverified items, or explicit none."),
        field("outputs[].blocked", True, "string", "Blocked items, or explicit none."),
        field("outputs[].user_confirmation", True, "string", "Needed user confirmation, or explicit none."),
        field("outputs[].final_coverage", True, "string", "Final response coverage."),
        field("outputs[].trace_id", True, "string", "Trace id linking output claims to execution."),
        field("events[].event_id", False, "string", "Stable event row id; generated when omitted."),
        field("events[].event_type", True, "string", "Lifecycle event type; input preflight must record input-filter.preflight."),
        field("events[].payload", False, "json", "Machine-readable event payload, including join point and filter-chain facts."),
        field("events[].created_at", False, "datetime", "Event time; defaults to now when omitted."),
        field(
            "events[event_type=command-compression.analysis].payload.decision",
            mutating or normalized != [],
            "string",
            "Pre-command analysis of whether generated local commands can be deduped, batched, cached, or routed through task-run.",
        ),
        field(
            "events[event_type=command-compression.analysis].payload.groups",
            mutating or normalized != [],
            "json",
            "Machine-readable command groups for task-run run, including parallel/cache/order boundaries.",
        ),
        field(
            f"events[event_type={PLAN_APPROVAL_BOUNDARY_EVENT}].payload.execution_policy",
            mutating or normalized != [],
            "string",
            "Plan, approval, local commit, and push boundary before execution.",
        ),
        field(
            f"events[event_type={PLAN_APPROVAL_BOUNDARY_EVENT}].payload.push_policy",
            mutating or normalized != [],
            "string",
            "Must be push_requires_separate_approval for gated work.",
        ),
        field(
            f"events[event_type={USER_CLAIM_VALIDATION_EVENT}].payload.claims",
            mutating or normalized != [],
            "json",
            "User assertions with trust_level, risk_flags, and verification_action.",
        ),
        field(
            f"events[event_type={USER_CLAIM_VALIDATION_EVENT}].payload.execution_policy",
            mutating or normalized != [],
            "string",
            "Whether user assertions require verify-first, clarification, blocking, or recorded execution.",
        ),
        field(
            f"events[event_type={STATE_ARTIFACT_OWNERSHIP_EVENT}].payload.manual_edit_policy",
            "rules-script" in normalized,
            "string",
            "Must be forbidden_without_break_glass for script-generated state.",
        ),
        field(
            f"events[event_type={PATCH_PREFLIGHT_EVENT}].payload.anchor_policy",
            bool(set(normalized) & {"rules-script", "docs", "correction"}),
            "string",
            "Must verify unique anchors or re-extract narrow context before patching.",
        ),
    ]
    if mutating:
        fields.extend(
            [
                field("worktrees[].worktree_id", False, "string", "Stable worktree row id; generated when omitted."),
                field("worktrees[].repo", True, "enum", "Source repository.", list(WORKTREE_REPOS)),
                field("worktrees[].source_repo", True, "string", "Repository path used to create the worktree."),
                field("worktrees[].path", True, "string", "Task worktree path."),
                field("worktrees[].branch", True, "string", "Task branch."),
                field("worktrees[].base_commit", True, "string", "Base commit."),
                field("worktrees[].creation_method", True, "enum", "Must normally be worktree-task.", list(WORKTREE_CREATION_METHODS)),
                field("worktrees[].sparse_policy", True, "string", "Sparse checkout/source snapshot policy."),
                field("worktrees[].source_handling", True, "string", "How source snapshots and break-glass exceptions are handled."),
                field("worktrees[].status", True, "enum", "Current worktree lifecycle status.", list(WORKTREE_STATUSES)),
                field("worktrees[].merged_status", True, "enum", "Merge boundary.", list(WORKTREE_MERGE_STATUSES)),
                field("worktrees[].commit_status", True, "enum", "Stage/commit boundary.", list(WORKTREE_COMMIT_STATUSES)),
                field("worktrees[].push_status", True, "enum", "Push boundary.", list(WORKTREE_PUSH_STATUSES)),
                field("worktrees[].next_action", True, "string", "Next user or merge action."),
            ]
        )
    if final or normalized:
        fields.extend(
            [
                field("validations[].validation_id", False, "string", "Stable validation row id; generated when omitted."),
                field("validations[].command", True, "string", "Exact validation command."),
                field("validations[].cwd", True, "string", "Validation working directory."),
                field("validations[].result", True, "enum", "Validation result.", list(VALIDATION_RESULTS)),
                field("validations[].summary", True, "string", "High-signal validation result."),
                field("validations[].evidence", False, "string", "Optional command output or artifact evidence."),
                field("validations[].created_at", False, "datetime", "Validation time; defaults to now when omitted."),
            ]
        )

    gate_requirements = [
        "Run lifecycle input-filter for the user-message join point and persist an events[] row with event_type=input-filter.preflight.",
        "Run completion-test --require-analysis before write-intent and record explicit scope, non-goals, risks, acceptance, and validation budget.",
        "Create or update the structured record before running final gates.",
        "Run task-record gate --task-id <id> before final answer.",
    ]
    if final:
        gate_requirements.append("Final output must include all output types: " + ", ".join(OUTPUT_TYPES) + ".")
    if mutating:
        gate_requirements.append("Worktree evidence must use creation_method=worktree-task unless break-glass is justified.")
        gate_requirements.append(
            "Mutating work must persist events.event_type=command-compression.analysis before write-intent or final gates."
        )
        gate_requirements.append(
            f"Mutating work must persist events.event_type={PLAN_APPROVAL_BOUNDARY_EVENT} with push_policy=push_requires_separate_approval."
        )
        gate_requirements.append(
            f"Mutating work must persist events.event_type={USER_CLAIM_VALIDATION_EVENT} before user assertions steer execution."
        )
        gate_requirements.append(
            "Important local commands should run through task-run run, gate-pool, shell-adapter, or the command adapter; non-command execution should use telemetry record so execution spans exist in aicg.db."
        )
    if "rules-script" in normalized:
        gate_requirements.append("rules-script tasks require task.approval_label plus an approved approvals[] row with the same label.")
        gate_requirements.append(
            f"rules-script tasks require events.event_type={STATE_ARTIFACT_OWNERSHIP_EVENT} for script-generated telemetry and artifacts."
        )
    if set(normalized) & {"rules-script", "docs", "correction"}:
        gate_requirements.append(
            f"Patchable governance/docs/correction tasks require events.event_type={PATCH_PREFLIGHT_EVENT}."
        )
    if "docs" in normalized:
        gate_requirements.append("Docs tasks should include validate-doc or doc-index validation rows.")
    if "resume" in normalized:
        gate_requirements.append("Resume tasks should include PDF/layout validation rows when Markdown/PDF is changed.")

    write_commands = [
        "python <AICG_REPO>/scripts/ai_client_governance.py completion-test --require-analysis --analysis-summary <summary> --analysis-scope <scope> --non-goal <non-goal> --risk <risk> --acceptance <acceptance> --budget-seconds <seconds>",
        "python <AICG_REPO>/scripts/ai_client_governance.py task-run plan --task-id <task-id> --event write-intent",
        "python <AICG_REPO>/scripts/ai_client_governance.py task-run run --task-id <task-id> --event write-intent --trace-json <trace.json>",
        "python <AICG_REPO>/scripts/ai_client_governance.py task-run diagnose --format json",
        "python <AICG_REPO>/scripts/ai_client_governance.py framework-debt add --item-id <id> --title <title> --problem <problem> --impact <impact> --desired-change <change> --framework-change-required <reason>",
        "python <AICG_REPO>/scripts/ai_client_governance.py task-record init",
        "python <AICG_REPO>/scripts/ai_client_governance.py task-record apply --json <task-record.json>",
        "python <AICG_REPO>/scripts/ai_client_governance.py task-record gate --task-id <task-id>",
    ]
    return Contract(
        schema_version=1,
        event=event,
        task_types=normalized,
        required_tables=required_tables,
        fields=fields,
        gate_requirements=gate_requirements,
        write_commands=write_commands,
    )


def format_text(contract: Contract) -> str:
    lines = [
        "AI Client Governance Structured Contract",
        f"Event: {contract.event}",
        f"Task types: {', '.join(contract.task_types) if contract.task_types else 'none'}",
        f"Required tables: {', '.join(contract.required_tables)}",
        "",
        "Required fields:",
    ]
    for item in contract.fields:
        marker = "required" if item.required else "optional"
        allowed = f" [{', '.join(item.allowed)}]" if item.allowed else ""
        lines.append(f"  - {item.path} ({marker}, {item.type}{allowed}): {item.reason}")
    lines.append("")
    lines.append("Gate requirements:")
    for item in contract.gate_requirements:
        lines.append(f"  - {item}")
    lines.append("")
    lines.append("Write commands:")
    for item in contract.write_commands:
        lines.append(f"  - {item}")
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    if args.command == "describe":
        contract = build_contract(args.task_type, args.event)
        if args.format == "json":
            print(json.dumps(asdict(contract), ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(format_text(contract))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
