#!/usr/bin/env python3
"""Plan completion tests from changed paths and task types.

The default profile is intentionally fast. Small governance code changes should
prove the changed surface with compile/encoding/focused checks and reserve the
full black-box selftest for release-like or high-risk passes. This keeps the
gate auditable without making every narrow fix pay the full-suite cost.

This command also carries the pre-write analysis contract and validation budget.
If the task is not understood clearly enough, or required checks exceed the
declared budget, the task should stop before editing instead of paying for a
late broad test sweep.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from ai_client_governance.records import telemetry
from ai_client_governance.records.task_record import connect, db_path, rows, task_row


TEXT_SUFFIXES = {".css", ".html", ".js", ".json", ".md", ".ps1", ".py", ".toml", ".ts", ".yaml", ".yml"}


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


@dataclass(frozen=True)
class PlannedCheck:
    id: str
    command: str
    reason: str
    required: bool = True
    estimated_seconds: int = 5
    cost: str = "cheap"
    actual_duration_ms: int | None = None


@dataclass(frozen=True)
class AnalysisContract:
    ready_for_write: bool
    missing_fields: list[str]
    summary: str
    scope: list[str]
    non_goals: list[str]
    risks: list[str]
    acceptance: list[str]
    decision: str


@dataclass(frozen=True)
class ValidationBudget:
    profile: str
    budget_seconds: int
    estimated_required_seconds: int
    estimated_optional_seconds: int
    expensive_required_checks: list[str]
    blocked_by_budget: bool
    decision: str


@dataclass(frozen=True)
class ValidationAttribution:
    planned_slowest_required: list[dict[str, object]]
    planned_slowest_optional: list[dict[str, object]]
    actual_slowest_validation_spans: list[dict[str, object]]
    budget_pressure: str
    likely_bottlenecks: list[str]
    recommendations: list[str]


PROFILE_BUDGET_SECONDS = {
    "fast": 90,
    "full": 600,
}


def normalize_values(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        for part in value.split(","):
            stripped = part.strip().replace("\\", "/")
            if stripped and stripped not in result:
                result.append(stripped)
    return result


def path_suffixes(paths: list[str]) -> set[str]:
    return {Path(path).suffix.lower() for path in paths}


def touches_runtime(paths: list[str]) -> bool:
    runtime_prefixes = (
        "src/ai_client_governance/runtime/",
        "src/ai_client_governance/lifecycle/",
        "src/ai_client_governance/gates/",
        "src/ai_client_governance/worktree/",
        "src/ai_client_governance/validation/",
    )
    return any(path.startswith(runtime_prefixes) for path in paths)


def explicit_values(values: list[str]) -> list[str]:
    return [value.strip() for value in values if value.strip()]


def build_analysis_contract(args: argparse.Namespace, changed_paths: list[str]) -> AnalysisContract:
    """Summarize whether the task is understood enough to enter write-intent.

    This is deliberately small and explicit. If analysis is fuzzy, the fix is
    to stop before editing, not to compensate with a broad test run at closeout.
    """
    summary = args.analysis_summary.strip()
    scope = explicit_values(args.analysis_scope) or changed_paths
    non_goals = explicit_values(args.non_goal)
    risks = explicit_values(args.risk)
    acceptance = explicit_values(args.acceptance)
    missing: list[str] = []
    if not summary:
        missing.append("analysis-summary")
    if not scope:
        missing.append("analysis-scope-or-changed-path")
    if not non_goals:
        missing.append("non-goal")
    if not risks:
        missing.append("risk")
    if not acceptance:
        missing.append("acceptance")
    ready = not missing
    decision = (
        "ready-for-write: analysis scope, non-goals, risks, and acceptance are explicit"
        if ready
        else "block-write-intent: analysis contract is incomplete"
    )
    return AnalysisContract(
        ready_for_write=ready,
        missing_fields=missing,
        summary=summary,
        scope=scope,
        non_goals=non_goals,
        risks=risks,
        acceptance=acceptance,
        decision=decision,
    )


def planned_checks(task_types: list[str], changed_paths: list[str], *, profile: str = "fast") -> list[PlannedCheck]:
    suffixes = path_suffixes(changed_paths)
    checks: list[PlannedCheck] = []

    def add(check: PlannedCheck) -> None:
        if check.id not in {item.id for item in checks}:
            checks.append(check)

    if ".py" in suffixes:
        add(PlannedCheck("py-compile", "python -m py_compile <changed-python-files>", "Python files changed.", estimated_seconds=5))
    if suffixes & TEXT_SUFFIXES:
        add(
            PlannedCheck(
                "validate-encoding",
                "ai_client_governance.py validate-encoding --paths <changed-text-files>",
                "Text files changed.",
                estimated_seconds=5,
            )
        )
    if ".md" in suffixes or "docs" in task_types:
        add(
            PlannedCheck(
                "validate-doc",
                "ai_client_governance.py validate-doc --paths <changed-markdown-files>",
                "Markdown or docs task in scope.",
                estimated_seconds=15,
            )
        )
        add(
            PlannedCheck(
                "doc-index",
                "ai_client_governance.py doc-index check --changed-path <paths>",
                "Docs, references, README, or backlinks may be affected.",
                estimated_seconds=15,
            )
        )
    if "rules-script" in task_types or touches_runtime(changed_paths):
        add(
            PlannedCheck(
                "runtime-components",
                "ai_client_governance.py runtime components --format json",
                "Runtime architecture changed.",
                estimated_seconds=5,
            )
        )
        add(
            PlannedCheck(
                "gate-pool-dry-run",
                "ai_client_governance.py gate-pool --dry-run --final",
                "Registered gates should be visible and de-duplicated.",
                estimated_seconds=8,
            )
        )
        add(
            PlannedCheck(
                "focused-regression",
                "run focused checks for the changed commands/modules",
                "Fast profile requires changed-surface proof before escalating to the full suite.",
                estimated_seconds=20,
            )
        )
        add(
            PlannedCheck(
                "selftest",
                "ai_client_governance.py selftest --root <target-project>",
                "Full black-box suite for broad, release-like, or high-risk rules/script changes.",
                required=profile == "full",
                estimated_seconds=240,
                cost="expensive",
            )
        )
    if "git" in task_types or any(path.startswith("src/ai_client_governance/worktree/") for path in changed_paths):
        add(
            PlannedCheck(
                "worktree-reconcile",
                "ai_client_governance.py worktree-task reconcile --strict",
                "Git/worktree coordination changed or is in scope.",
                estimated_seconds=8,
            )
        )
        add(
            PlannedCheck(
                "worktree-status",
                "ai_client_governance.py worktree-task status --record-state",
                "Live worktree state must be recorded in SQLite.",
                estimated_seconds=8,
            )
        )
        add(
            PlannedCheck(
                "host-closeout",
                "ai_client_governance.py worktree-task host-closeout --repo ai-client-governance --require-task-tracking --require-clean-host",
                "Embedded ai-client-governance merges must close out host gitlink, task state, and task tracking.",
                estimated_seconds=10,
            )
        )
    if "resume" in task_types:
        add(
            PlannedCheck(
                "resume-pdf-export",
                "export affected resume PDF and inspect layout",
                "Resume delivery files changed.",
                estimated_seconds=180,
                cost="expensive",
            )
        )
    if "frontend" in task_types:
        add(
            PlannedCheck(
                "browser-check",
                "open local app and verify with browser screenshot/interaction",
                "Frontend behavior changed.",
                estimated_seconds=90,
                cost="expensive",
            )
        )
    if not checks:
        add(
            PlannedCheck(
                "acceptance-review",
                "review REQ rows against final answer",
                "No file-specific tests were inferred.",
                required=False,
                estimated_seconds=2,
            )
        )
    return checks


def build_validation_budget(
    checks: list[PlannedCheck],
    *,
    profile: str,
    budget_seconds: int | None,
    allow_expensive: bool,
) -> ValidationBudget:
    budget = budget_seconds if budget_seconds is not None else PROFILE_BUDGET_SECONDS[profile]
    required = [check for check in checks if check.required]
    optional = [check for check in checks if not check.required]
    required_seconds = sum(check.estimated_seconds for check in required)
    optional_seconds = sum(check.estimated_seconds for check in optional)
    expensive_required = [check.id for check in required if check.cost == "expensive"]
    blocked = required_seconds > budget and not allow_expensive
    if blocked:
        decision = "block-validation: required checks exceed the declared budget; narrow scope or explicitly upgrade budget"
    elif expensive_required and profile == "fast" and not allow_expensive:
        decision = "warn-validation: expensive required checks are present in fast profile"
    else:
        decision = "run-focused-required-checks: required checks fit the declared budget"
    return ValidationBudget(
        profile=profile,
        budget_seconds=budget,
        estimated_required_seconds=required_seconds,
        estimated_optional_seconds=optional_seconds,
        expensive_required_checks=expensive_required,
        blocked_by_budget=blocked,
        decision=decision,
    )


def actual_validation_spans(root: Path, task_id: str | None, trace_id: str | None, db_override: str | None, top: int) -> list[dict[str, object]]:
    if not task_id and not trace_id:
        return []
    try:
        spans = telemetry.span_rows(root, db=db_override, task_id=task_id or "", trace_id=trace_id or "")
    except (sqlite3.Error, ValueError, OSError):
        return []
    validation_phases = {"validation", "completion", "final-gate"}
    validation_events = {"validation", "gate", "gate-pool"}
    candidates = [
        span
        for span in spans
        if span.get("duration_ms") is not None
        and (
            str(span.get("phase") or "") in validation_phases
            or str(span.get("event_type") or "") in validation_events
            or "validation" in str(span.get("name") or "").lower()
            or "completion-test" in str(span.get("name") or "").lower()
        )
    ]
    candidates.sort(key=lambda item: int(item.get("duration_ms") or 0), reverse=True)
    result: list[dict[str, object]] = []
    for span in candidates[:top]:
        result.append(
            {
                "name": span.get("name") or "",
                "phase": span.get("phase") or "",
                "event_type": span.get("event_type") or "",
                "duration_ms": span.get("duration_ms"),
                "status": span.get("status") or "",
                "exit_code": span.get("exit_code"),
                "cached": bool(span.get("cached")),
                "summary": span.get("summary") or "",
            }
        )
    return result


def planned_check_dict(check: PlannedCheck) -> dict[str, object]:
    return {
        "id": check.id,
        "command": check.command,
        "required": check.required,
        "estimated_seconds": check.estimated_seconds,
        "cost": check.cost,
        "reason": check.reason,
        "actual_duration_ms": check.actual_duration_ms,
    }


def build_validation_attribution(
    checks: list[PlannedCheck],
    budget: ValidationBudget,
    *,
    root: Path,
    task_id: str | None,
    trace_id: str | None,
    db_override: str | None,
    top: int,
) -> ValidationAttribution:
    required = sorted((check for check in checks if check.required), key=lambda item: item.estimated_seconds, reverse=True)
    optional = sorted((check for check in checks if not check.required), key=lambda item: item.estimated_seconds, reverse=True)
    actual = actual_validation_spans(root, task_id, trace_id, db_override, top)
    # Match actual spans to planned checks by id/subject keyword for duration attribution
    actual_by_name: dict[str, int] = {}
    for span in actual:
        name = str(span.get("name") or "").lower()
        subj = str(span.get("subject") or "").lower()
        duration = int(span.get("duration_ms") or 0)
        if name:
            actual_by_name[name] = max(actual_by_name.get(name, 0), duration)
        if subj:
            actual_by_name[subj] = max(actual_by_name.get(subj, 0), duration)
    def match_duration(check: PlannedCheck) -> int | None:
        check_id = check.id.lower()
        check_cmd = check.command.lower()
        for key, duration in actual_by_name.items():
            if check_id in key or check_id in check_cmd and key in check_cmd:
                return duration
            # Match common keywords: py-compile, validate-encoding, selftest, gate-pool
            for keyword in [check_id, check_id.replace("-", "")]:
                if keyword and keyword in key:
                    return duration
        return None
    enriched_checks: list[PlannedCheck] = []
    for check in checks:
        matched = match_duration(check)
        if matched is not None:
            enriched_checks.append(PlannedCheck(
                id=check.id, command=check.command, reason=check.reason,
                required=check.required, estimated_seconds=check.estimated_seconds,
                cost=check.cost, actual_duration_ms=matched,
            ))
        else:
            enriched_checks.append(check)
    required = sorted((c for c in enriched_checks if c.required), key=lambda item: item.estimated_seconds, reverse=True)
    optional = sorted((c for c in enriched_checks if not c.required), key=lambda item: item.estimated_seconds, reverse=True)
    pressure_ratio = budget.estimated_required_seconds / budget.budget_seconds if budget.budget_seconds else 0
    if budget.blocked_by_budget:
        pressure = "blocked"
    elif pressure_ratio >= 0.8:
        pressure = "high"
    elif pressure_ratio >= 0.5:
        pressure = "medium"
    else:
        pressure = "low"
    likely: list[str] = []
    if required:
        likely.append(f"largest-planned-required-check={required[0].id}:{required[0].estimated_seconds}s")
    if budget.expensive_required_checks:
        likely.append("expensive-required-checks=" + ",".join(budget.expensive_required_checks))
    if actual:
        likely.append(f"slowest-actual-validation={actual[0]['name']}:{actual[0]['duration_ms']}ms")
    if not actual:
        likely.append("actual-validation-telemetry-missing")
    recommendations: list[str] = []
    if budget.blocked_by_budget:
        recommendations.append("split scope, lower required checks, or explicitly upgrade the validation budget")
    if optional and optional[0].cost == "expensive":
        recommendations.append(f"keep {optional[0].id} optional unless the task is high-risk, release-like, or explicitly full-profile")
    if actual:
        recommendations.append("optimize or cache the slowest actual validation spans before adding more checks")
    else:
        recommendations.append("run validations through gate-pool/task-run/tool-invocations so duration_ms evidence exists")
    # Redundancy detection: check for duplicated subjects across validation spans
    redundant_hints: list[str] = []
    seen_subjects: dict[str, int] = {}
    for span in actual:
        subj = str(span.get("subject", span.get("name", "")) or "")
        if subj:
            seen_subjects[subj] = seen_subjects.get(subj, 0) + 1
    for subj, count in seen_subjects.items():
        if count > 1:
            redundant_hints.append(f"{subj} checked {count}x")
    if redundant_hints:
        recommendations.append("redundant checks: " + ", ".join(redundant_hints[:5]))
    return ValidationAttribution(
        planned_slowest_required=[planned_check_dict(check) for check in required[:top]],
        planned_slowest_optional=[planned_check_dict(check) for check in optional[:top]],
        actual_slowest_validation_spans=actual,
        budget_pressure=pressure,
        likely_bottlenecks=likely,
        recommendations=recommendations,
    )


def structured_evidence_text(root: Path, task_id: str | None, db_override: str | None) -> str:
    if not task_id:
        return ""
    path = db_path(root, db_override)
    if not path.exists():
        return ""
    try:
        con = connect(path)
        task = task_row(con, task_id)
        if task is None:
            return ""
        parts: list[str] = []
        parts.extend(str(value) for value in dict(task).values())
        for table in ("requirements", "triggers", "outputs", "worktrees", "validations"):
            for row in rows(con, table, task_id):
                parts.extend(str(value) for value in dict(row).values())
        return "\n".join(parts)
    except sqlite3.Error:
        return ""


def evidence_hits(task_tracking: Path | None, structured_text: str, checks: list[PlannedCheck]) -> dict[str, bool]:
    evidence = structured_text
    if not task_tracking or not task_tracking.exists():
        text = evidence
    else:
        text = task_tracking.read_text(encoding="utf-8") + "\n" + evidence
    return {check.id: check.id in text or check.command.split()[0] in text for check in checks}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plan completion tests for an ai-client-governance task.")
    parser.add_argument("--root", default=".", help="Repository root. Default: current directory.")
    parser.add_argument("--task-type", action="append", default=[], help="Task type in scope; repeatable.")
    parser.add_argument("--changed-path", action="append", default=[], help="Changed path; repeatable or comma-separated.")
    parser.add_argument("--task-tracking", help="Task tracking file used for optional evidence scan.")
    parser.add_argument("--task-id", help="Structured task id used for optional SQLite evidence scan.")
    parser.add_argument("--db", help="Structured task-record SQLite path.")
    parser.add_argument("--trace-id", help="Trace id used to load actual validation telemetry.")
    parser.add_argument(
        "--profile",
        choices=("fast", "full"),
        default="fast",
        help="Validation profile. fast plans focused changed-surface checks; full requires the full selftest.",
    )
    parser.add_argument("--budget-seconds", type=int, help="Maximum estimated seconds for required validation checks.")
    parser.add_argument("--allow-expensive", action="store_true", help="Allow required checks to exceed the declared budget.")
    parser.add_argument("--require-analysis", action="store_true", help="Fail unless an explicit analysis contract is complete.")
    parser.add_argument("--analysis-summary", default="", help="One-sentence understanding of the task before write-intent.")
    parser.add_argument("--analysis-scope", action="append", default=[], help="Explicit scope boundary; repeatable.")
    parser.add_argument("--non-goal", action="append", default=[], help="Explicit non-goal or excluded scope; repeatable.")
    parser.add_argument("--risk", action="append", default=[], help="Known risk or uncertainty before execution; repeatable.")
    parser.add_argument("--acceptance", action="append", default=[], help="User-visible acceptance criterion; repeatable.")
    parser.add_argument("--require-evidence", action="store_true", help="Fail if required planned checks are not mentioned in task tracking.")
    parser.add_argument("--attribution-top", type=int, default=5, help="Number of slow planned/actual validation items to explain.")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(args.root).resolve()
    task_types = normalize_values(args.task_type)
    changed_paths = normalize_values(args.changed_path)
    checks = planned_checks(task_types, changed_paths, profile=args.profile)
    analysis_contract = build_analysis_contract(args, changed_paths)
    validation_budget = build_validation_budget(
        checks,
        profile=args.profile,
        budget_seconds=args.budget_seconds,
        allow_expensive=args.allow_expensive,
    )
    validation_attribution = build_validation_attribution(
        checks,
        validation_budget,
        root=root,
        task_id=args.task_id,
        trace_id=args.trace_id,
        db_override=args.db,
        top=max(1, args.attribution_top),
    )
    tracking = Path(args.task_tracking) if args.task_tracking else None
    if tracking and not tracking.is_absolute():
        tracking = root / tracking
    structured_text = structured_evidence_text(root, args.task_id, args.db)
    hits = evidence_hits(tracking, structured_text, checks)
    missing = [check.id for check in checks if check.required and args.require_evidence and not hits.get(check.id)]
    analysis_missing = analysis_contract.missing_fields if args.require_analysis else []
    budget_errors = ["validation-budget"] if validation_budget.blocked_by_budget else []

    payload = {
        "task_types": task_types,
        "task_id": args.task_id or "",
        "profile": args.profile,
        "changed_paths": changed_paths,
        "analysis_contract": asdict(analysis_contract),
        "validation_budget": asdict(validation_budget),
        "validation_attribution": asdict(validation_attribution),
        "planned_checks": [asdict(check) | {"evidence_found": hits.get(check.id, False)} for check in checks],
        "missing_evidence": missing,
        "missing_analysis": analysis_missing,
        "budget_errors": budget_errors,
    }
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print("Analysis contract:")
        print(f"- decision: {analysis_contract.decision}")
        if analysis_contract.missing_fields:
            print(f"- missing: {', '.join(analysis_contract.missing_fields)}")
        print("Validation budget:")
        print(
            f"- required={validation_budget.estimated_required_seconds}s "
            f"optional={validation_budget.estimated_optional_seconds}s budget={validation_budget.budget_seconds}s"
        )
        print(f"- decision: {validation_budget.decision}")
        print("Validation attribution:")
        print(f"- budget pressure: {validation_attribution.budget_pressure}")
        for item in validation_attribution.likely_bottlenecks:
            print(f"- bottleneck: {item}")
        print("Slowest planned checks (by estimated_seconds):")
        for check in validation_attribution.planned_slowest_required[:3]:
            print(f"  - {check['id']}: ~{check['estimated_seconds']}s [{check['cost']}]")
        if validation_attribution.actual_slowest_validation_spans:
            print("Slowest actual validation spans (by duration_ms):")
            for span in validation_attribution.actual_slowest_validation_spans[:3]:
                print(f"  - {span.get('name', '?')}: {span.get('duration_ms', '?')}ms")
        for item in validation_attribution.recommendations:
            print(f"- recommendation: {item}")
        print("Completion test plan:")
        for check in checks:
            marker = "evidence=yes" if hits.get(check.id) else "evidence=no"
            required = "required" if check.required else "optional"
            print(
                f"- {check.id} [{required}, {marker}, {check.cost}, ~{check.estimated_seconds}s]: "
                f"{check.command}"
            )
            print(f"  reason: {check.reason}")
        if analysis_missing:
            print("Missing required analysis:")
            for item in analysis_missing:
                print(f"- {item}")
        if budget_errors:
            print("Validation budget errors:")
            for item in budget_errors:
                print(f"- {item}")
        if missing:
            print("Missing required evidence:")
            for item in missing:
                print(f"- {item}")
    return 1 if missing or analysis_missing or budget_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
