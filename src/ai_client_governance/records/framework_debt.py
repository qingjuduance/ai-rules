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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ai_client_governance.common import cli_arguments as common_cli_args
from ai_client_governance.common.paths import STRUCTURED_DB_PATH


STATUSES = ("open", "planned", "in_progress", "resolved", "deferred", "rejected")
SEVERITIES = ("P0", "P1", "P2", "P3")


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def db_path(root: Path, override: str | None) -> Path:
    if override:
        path = Path(override)
        return path if path.is_absolute() else root / path
    return root / STRUCTURED_DB_PATH


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
