"""Track framework-level design debt in the project SQLite state.

This register is for issues that are real but should not be patched piecemeal
inside a narrow task. Recording them as typed rows lets the next architecture
pass batch related fixes, such as command parser ergonomics, without relying on
chat memory or scattered comments.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from ai_client_governance.common.time_utils import now_iso
from pathlib import Path
from typing import Any

from ai_client_governance.common import cli_arguments as common_cli_args
from ai_client_governance.common.paths import structured_db_path


STATUSES = ("open", "planned", "in_progress", "resolved", "deferred", "rejected")
SEVERITIES = ("P0", "P1", "P2", "P3")
SEVERITY_RANK = {severity: index for index, severity in enumerate(SEVERITIES)}
OPEN_STATUSES = {"open", "planned", "in_progress", "deferred"}


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def db_path(root: Path, override: str | None) -> Path:
    return structured_db_path(root, override)


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    init_db(con)
    return con


def init_db(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS framework_debt (
            item_id TEXT PRIMARY KEY,
            title TEXT NOT NULL CHECK (length(trim(title)) > 0),
            category TEXT NOT NULL CHECK (length(trim(category)) > 0),
            severity TEXT NOT NULL CHECK (severity IN ('P0', 'P1', 'P2', 'P3')),
            status TEXT NOT NULL CHECK (status IN ('open', 'planned', 'in_progress', 'resolved', 'deferred', 'rejected')),
            problem TEXT NOT NULL CHECK (length(trim(problem)) > 0),
            impact TEXT NOT NULL CHECK (length(trim(impact)) > 0),
            desired_change TEXT NOT NULL CHECK (length(trim(desired_change)) > 0),
            framework_change_required TEXT NOT NULL CHECK (length(trim(framework_change_required)) > 0),
            workaround TEXT NOT NULL DEFAULT '',
            next_trigger TEXT NOT NULL CHECK (length(trim(next_trigger)) > 0),
            related_task_id TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL CHECK (length(trim(source)) > 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    con.commit()


def clean(value: str | None, field: str, *, required: bool = True) -> str:
    text = (value or "").strip()
    if required and not text:
        raise ValueError(f"{field} is required")
    return text


def item_from_args(args: argparse.Namespace) -> dict[str, str]:
    now = now_iso()
    return {
        "item_id": clean(args.item_id, "item-id"),
        "title": clean(args.title, "title"),
        "category": clean(args.category, "category"),
        "severity": args.severity,
        "status": args.status,
        "problem": clean(args.problem, "problem"),
        "impact": clean(args.impact, "impact"),
        "desired_change": clean(args.desired_change, "desired-change"),
        "framework_change_required": clean(args.framework_change_required, "framework-change-required"),
        "workaround": clean(args.workaround, "workaround", required=False),
        "next_trigger": clean(args.next_trigger, "next-trigger"),
        "related_task_id": clean(args.related_task_id, "related-task-id", required=False),
        "source": clean(args.source, "source"),
        "created_at": now,
        "updated_at": now,
    }


def upsert_item(con: sqlite3.Connection, item: dict[str, str], *, replace: bool) -> None:
    existing = con.execute("SELECT created_at FROM framework_debt WHERE item_id = ?", (item["item_id"],)).fetchone()
    if existing and not replace:
        raise ValueError(f"framework debt item already exists: {item['item_id']} (use --replace)")
    if existing:
        item["created_at"] = str(existing["created_at"])
    con.execute(
        """
        INSERT INTO framework_debt (
            item_id, title, category, severity, status, problem, impact,
            desired_change, framework_change_required, workaround, next_trigger,
            related_task_id, source, created_at, updated_at
        ) VALUES (
            :item_id, :title, :category, :severity, :status, :problem, :impact,
            :desired_change, :framework_change_required, :workaround, :next_trigger,
            :related_task_id, :source, :created_at, :updated_at
        )
        ON CONFLICT(item_id) DO UPDATE SET
            title = excluded.title,
            category = excluded.category,
            severity = excluded.severity,
            status = excluded.status,
            problem = excluded.problem,
            impact = excluded.impact,
            desired_change = excluded.desired_change,
            framework_change_required = excluded.framework_change_required,
            workaround = excluded.workaround,
            next_trigger = excluded.next_trigger,
            related_task_id = excluded.related_task_id,
            source = excluded.source,
            updated_at = excluded.updated_at
        """,
        item,
    )
    con.commit()


def list_items(con: sqlite3.Connection, *, statuses: list[str], category: str, include_closed: bool) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[str] = []
    if statuses:
        clauses.append("status IN (" + ", ".join("?" for _ in statuses) + ")")
        params.extend(statuses)
    elif not include_closed:
        clauses.append("status NOT IN ('resolved', 'rejected')")
    if category:
        clauses.append("category = ?")
        params.append(category)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    query = (
        "SELECT item_id, title, category, severity, status, problem, impact, desired_change, "
        "framework_change_required, workaround, next_trigger, related_task_id, source, created_at, updated_at "
        f"FROM framework_debt{where} ORDER BY severity, updated_at DESC, item_id"
    )
    return [dict(row) for row in con.execute(query, params)]


def min_severity_items(items: list[dict[str, Any]], min_severity: str) -> list[dict[str, Any]]:
    cutoff = SEVERITY_RANK[min_severity]
    return [item for item in items if SEVERITY_RANK.get(str(item["severity"]), 99) <= cutoff]


def count_by(items: list[dict[str, Any]], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        key = str(item.get(field) or "")
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def infer_categories(task_types: list[str], changed_paths: list[str]) -> list[str]:
    categories: list[str] = []
    mapping = {
        "rules-script": ("runtime", "lifecycle", "validation", "framework-debt", "policy"),
        "docs": ("docs", "framework-debt"),
        "git": ("worktree", "policy", "lifecycle"),
        "multi-agent": ("multi-agent",),
        "correction": ("correction", "lifecycle"),
    }
    for task_type in task_types:
        for category in mapping.get(task_type, ()):
            if category not in categories:
                categories.append(category)
    path_blob = "\n".join(path.replace("\\", "/") for path in changed_paths)
    path_mapping = {
        "src/ai_client_governance/validation/": "validation",
        "src/ai_client_governance/lifecycle/": "lifecycle",
        "src/ai_client_governance/runtime/": "runtime",
        "src/ai_client_governance/gates/": "policy",
        "src/ai_client_governance/worktree/": "worktree",
        "src/ai_client_governance/records/framework_debt.py": "framework-debt",
        "README.md": "docs",
        "AGENTS.md": "docs",
    }
    for marker, category in path_mapping.items():
        if marker in path_blob and category not in categories:
            categories.append(category)
    return categories


def build_report(
    items: list[dict[str, Any]],
    *,
    min_severity: str,
    max_items: int,
    task_types: list[str],
    changed_paths: list[str],
) -> dict[str, Any]:
    open_items = [item for item in items if item["status"] in OPEN_STATUSES]
    important = min_severity_items(open_items, min_severity)
    relevant_categories = infer_categories(task_types, changed_paths)
    relevant = [
        item
        for item in important
        if item["severity"] in {"P0", "P1"} or not relevant_categories or item["category"] in relevant_categories
    ]
    return {
        "count": len(items),
        "open_count": len(open_items),
        "important_count": len(important),
        "min_severity": min_severity,
        "by_severity": count_by(open_items, "severity"),
        "by_category": count_by(open_items, "category"),
        "relevant_categories": relevant_categories,
        "task_types": task_types,
        "changed_paths": changed_paths,
        "items": relevant[:max_items],
        "has_p0": any(item["severity"] == "P0" for item in open_items),
        "decision": (
            "surface-before-write-or-closeout: open P0/P1 framework debt is visible"
            if relevant
            else "no-important-open-framework-debt-for-current-scope"
        ),
    }


def render_report_text(report: dict[str, Any]) -> str:
    lines = [
        "Framework debt report:",
        f"- open: {report['open_count']} important({report['min_severity']}+): {report['important_count']}",
        f"- by severity: {json.dumps(report['by_severity'], ensure_ascii=False, sort_keys=True)}",
        f"- by category: {json.dumps(report['by_category'], ensure_ascii=False, sort_keys=True)}",
        f"- decision: {report['decision']}",
    ]
    if report["relevant_categories"]:
        lines.append(f"- relevant categories: {', '.join(report['relevant_categories'])}")
    if not report["items"]:
        lines.append("- no matching important open items")
        return "\n".join(lines)
    lines.append("Important open items:")
    for item in report["items"]:
        lines.append(f"- {item['item_id']} [{item['severity']}/{item['status']}] {item['title']}")
        lines.append(f"  impact: {item['impact']}")
        lines.append(f"  next: {item['next_trigger']}")
    return "\n".join(lines)


def render_text(items: list[dict[str, Any]]) -> str:
    lines = ["Framework debt register:"]
    if not items:
        lines.append("- no matching items")
        return "\n".join(lines)
    for item in items:
        lines.append(f"- {item['item_id']} [{item['severity']}/{item['status']}] {item['title']}")
        lines.append(f"  category: {item['category']}")
        lines.append(f"  impact: {item['impact']}")
        lines.append(f"  next trigger: {item['next_trigger']}")
    return "\n".join(lines)


def render_markdown(items: list[dict[str, Any]]) -> str:
    lines = [
        "# Framework Debt Register",
        "",
        "| ID | Severity | Status | Category | Title | Impact | Next Trigger |",
        "|---|---|---|---|---|---|---|",
    ]
    for item in items:
        lines.append(
            f"| {item['item_id']} | {item['severity']} | {item['status']} | {item['category']} | "
            f"{item['title']} | {item['impact']} | {item['next_trigger']} |"
        )
    lines.append("")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Track framework-level design debt.")
    common_cli_args.add_common_global_args(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Create the framework debt table.")
    common_cli_args.add_common_global_args(init, suppress_default=True)

    add = sub.add_parser("add", help="Add or update one framework debt item.")
    common_cli_args.add_common_global_args(add, suppress_default=True)
    add.add_argument("--item-id", required=True)
    add.add_argument("--title", required=True)
    add.add_argument("--category", default="framework")
    add.add_argument("--severity", choices=SEVERITIES, default="P2")
    add.add_argument("--status", choices=STATUSES, default="open")
    add.add_argument("--problem", required=True)
    add.add_argument("--impact", required=True)
    add.add_argument("--desired-change", required=True)
    add.add_argument("--framework-change-required", required=True)
    add.add_argument("--workaround", default="")
    add.add_argument("--next-trigger", default="next framework architecture pass")
    add.add_argument("--related-task-id", default="")
    add.add_argument("--source", default="user-feedback")
    add.add_argument("--replace", action="store_true")

    list_cmd = sub.add_parser("list", help="List framework debt items.")
    common_cli_args.add_common_global_args(list_cmd, suppress_default=True)
    list_cmd.add_argument("--status", action="append", choices=STATUSES, default=[])
    list_cmd.add_argument("--category", default="")
    list_cmd.add_argument("--include-closed", action="store_true")

    report = sub.add_parser("report", help="Summarize important open framework debt for planning or closeout.")
    common_cli_args.add_common_global_args(report, suppress_default=True)
    report.add_argument("--status", action="append", choices=STATUSES, default=[])
    report.add_argument("--category", default="")
    report.add_argument("--include-closed", action="store_true")
    report.add_argument("--min-severity", choices=SEVERITIES, default="P1")
    report.add_argument("--max-items", type=int, default=12)
    report.add_argument("--task-type", action="append", default=[], help="Task type used to infer relevant debt categories.")
    report.add_argument("--changed-path", action="append", default=[], help="Changed path used to infer relevant debt categories.")
    report.add_argument(
        "--fail-on-open-p0",
        action="store_true",
        help="Exit 1 when any open P0 item exists. Use only for release-style gates.",
    )

    export = sub.add_parser("export-md", help="Export matching debt items as Markdown.")
    common_cli_args.add_common_global_args(export, suppress_default=True)
    export.add_argument("--status", action="append", choices=STATUSES, default=[])
    export.add_argument("--category", default="")
    export.add_argument("--include-closed", action="store_true")
    export.add_argument("--output", help="Output Markdown path. Defaults to stdout.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(args.root).resolve()
    path = db_path(root, args.db)
    try:
        con = connect(path)
        if args.command == "init":
            payload = {"db": str(path), "initialized": True}
            print(json.dumps(payload, ensure_ascii=False, indent=2) if args.format == "json" else f"initialized: {path}")
            return 0
        if args.command == "add":
            item = item_from_args(args)
            upsert_item(con, item, replace=args.replace)
            payload = {"db": str(path), "item_id": item["item_id"], "status": item["status"]}
            print(json.dumps(payload, ensure_ascii=False, indent=2) if args.format == "json" else f"recorded: {item['item_id']}")
            return 0
        if args.command == "list":
            items = list_items(con, statuses=args.status, category=args.category, include_closed=args.include_closed)
            payload = {"db": str(path), "count": len(items), "items": items}
            print(json.dumps(payload, ensure_ascii=False, indent=2) if args.format == "json" else render_text(items))
            return 0
        if args.command == "report":
            items = list_items(con, statuses=args.status, category=args.category, include_closed=args.include_closed)
            report = build_report(
                items,
                min_severity=args.min_severity,
                max_items=max(1, args.max_items),
                task_types=args.task_type,
                changed_paths=args.changed_path,
            )
            payload = {"db": str(path), **report}
            print(json.dumps(payload, ensure_ascii=False, indent=2) if args.format == "json" else render_report_text(payload))
            return 1 if args.fail_on_open_p0 and payload["has_p0"] else 0
        if args.command == "export-md":
            items = list_items(con, statuses=args.status, category=args.category, include_closed=args.include_closed)
            text = render_markdown(items)
            if args.output:
                output = Path(args.output)
                if not output.is_absolute():
                    output = root / output
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(text, encoding="utf-8", newline="\n")
                print(json.dumps({"output": str(output), "count": len(items)}, ensure_ascii=False, indent=2) if args.format == "json" else str(output))
            else:
                print(text)
            return 0
    except (sqlite3.Error, ValueError) as exc:
        print(f"framework-debt error: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
