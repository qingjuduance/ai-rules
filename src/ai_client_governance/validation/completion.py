#!/usr/bin/env python3
"""Plan completion tests from changed paths and task types.

The default profile is intentionally fast. Small governance code changes should
prove the changed surface with compile/encoding/focused checks and reserve the
full black-box selftest for release-like or high-risk passes. This keeps the
gate auditable without making every narrow fix pay the full-suite cost.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

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


def planned_checks(task_types: list[str], changed_paths: list[str], *, profile: str = "fast") -> list[PlannedCheck]:
    suffixes = path_suffixes(changed_paths)
    checks: list[PlannedCheck] = []

    def add(check: PlannedCheck) -> None:
        if check.id not in {item.id for item in checks}:
            checks.append(check)

    if ".py" in suffixes:
        add(PlannedCheck("py-compile", "python -m py_compile <changed-python-files>", "Python files changed."))
    if suffixes & TEXT_SUFFIXES:
        add(PlannedCheck("validate-encoding", "ai_client_governance.py validate-encoding --paths <changed-text-files>", "Text files changed."))
    if ".md" in suffixes or "docs" in task_types:
        add(PlannedCheck("validate-doc", "ai_client_governance.py validate-doc --paths <changed-markdown-files>", "Markdown or docs task in scope."))
        add(PlannedCheck("doc-index", "ai_client_governance.py doc-index check --changed-path <paths>", "Docs, references, README, or backlinks may be affected."))
    if "rules-script" in task_types or touches_runtime(changed_paths):
        add(PlannedCheck("runtime-components", "ai_client_governance.py runtime components --format json", "Runtime architecture changed."))
        add(PlannedCheck("gate-pool-dry-run", "ai_client_governance.py gate-pool --dry-run --final", "Registered gates should be visible and de-duplicated."))
        add(
            PlannedCheck(
                "focused-regression",
                "run focused checks for the changed commands/modules",
                "Fast profile requires changed-surface proof before escalating to the full suite.",
            )
        )
        add(
            PlannedCheck(
                "selftest",
                "ai_client_governance.py selftest --root <target-project>",
                "Full black-box suite for broad, release-like, or high-risk rules/script changes.",
                required=profile == "full",
            )
        )
    if "git" in task_types or any(path.startswith("src/ai_client_governance/worktree/") for path in changed_paths):
        add(PlannedCheck("worktree-reconcile", "ai_client_governance.py worktree-task reconcile --strict", "Git/worktree coordination changed or is in scope."))
        add(PlannedCheck("worktree-status", "ai_client_governance.py worktree-task status --record-state", "Live worktree state must be recorded in SQLite."))
        add(
            PlannedCheck(
                "host-closeout",
                "ai_client_governance.py worktree-task host-closeout --repo ai-client-governance --require-task-tracking --require-clean-host",
                "Embedded ai-client-governance merges must close out host gitlink, task state, and task tracking.",
            )
        )
    if "resume" in task_types:
        add(PlannedCheck("resume-pdf-export", "export affected resume PDF and inspect layout", "Resume delivery files changed."))
    if "frontend" in task_types:
        add(PlannedCheck("browser-check", "open local app and verify with browser screenshot/interaction", "Frontend behavior changed."))
    if not checks:
        add(PlannedCheck("acceptance-review", "review REQ rows against final answer", "No file-specific tests were inferred.", required=False))
    return checks


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
    parser.add_argument(
        "--profile",
        choices=("fast", "full"),
        default="fast",
        help="Validation profile. fast plans focused changed-surface checks; full requires the full selftest.",
    )
    parser.add_argument("--require-evidence", action="store_true", help="Fail if required planned checks are not mentioned in task tracking.")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(args.root).resolve()
    task_types = normalize_values(args.task_type)
    changed_paths = normalize_values(args.changed_path)
    checks = planned_checks(task_types, changed_paths, profile=args.profile)
    tracking = Path(args.task_tracking) if args.task_tracking else None
    if tracking and not tracking.is_absolute():
        tracking = root / tracking
    structured_text = structured_evidence_text(root, args.task_id, args.db)
    hits = evidence_hits(tracking, structured_text, checks)
    missing = [check.id for check in checks if check.required and args.require_evidence and not hits.get(check.id)]

    payload = {
        "task_types": task_types,
        "task_id": args.task_id or "",
        "profile": args.profile,
        "changed_paths": changed_paths,
        "planned_checks": [asdict(check) | {"evidence_found": hits.get(check.id, False)} for check in checks],
        "missing_evidence": missing,
    }
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print("Completion test plan:")
        for check in checks:
            marker = "evidence=yes" if hits.get(check.id) else "evidence=no"
            required = "required" if check.required else "optional"
            print(f"- {check.id} [{required}, {marker}]: {check.command}")
            print(f"  reason: {check.reason}")
        if missing:
            print("Missing required evidence:")
            for item in missing:
                print(f"- {item}")
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
