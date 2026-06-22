"""Correction record register backed by SQLite.

This module replaces the previous Markdown-only correction storage with a
structured ``corrections`` table in ``.ai-client/project/state/aicg.db``.
The old ``scan_corrections.py`` now reads from this table instead of from
individual ``.md`` files.  Use ``import-md`` to migrate legacy Markdown
records into the database (idempotent).
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

from ai_client_governance.common import cli_arguments as common_cli_args
from ai_client_governance.common.paths import CORRECTIONS_DIR, structured_db_path
from ai_client_governance.common.time_utils import now_iso

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STATUSES = ("open", "in_progress", "resolved", "deferred", "rejected", "extracted")
SEVERITIES = ("P0", "P1", "P2", "P3")
OPEN_STATUSES = {"open", "in_progress", "deferred"}

# ---------------------------------------------------------------------------
# DB helpers  (mirrors framework_debt.py)
# ---------------------------------------------------------------------------


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
        CREATE TABLE IF NOT EXISTS corrections (
            correction_id TEXT PRIMARY KEY,
            title TEXT NOT NULL CHECK (length(trim(title)) > 0),
            severity TEXT NOT NULL CHECK (severity IN ('P0', 'P1', 'P2', 'P3')),
            error_type TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL CHECK (status IN (
                'open', 'in_progress', 'resolved', 'deferred', 'rejected', 'extracted'
            )),
            related_task_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,

            -- Rich text fields
            problem TEXT NOT NULL DEFAULT '',
            root_cause TEXT NOT NULL DEFAULT '',
            violated_rule TEXT NOT NULL DEFAULT '',
            impact TEXT NOT NULL DEFAULT '',
            fix_action TEXT NOT NULL DEFAULT '',
            upgrade_judgment TEXT NOT NULL DEFAULT '',

            -- Source provenance
            source_md_path TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT 'cli'
        )
        """
    )
    con.commit()


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def clean(value: str | None, field: str, *, required: bool = True) -> str:
    text = (value or "").strip()
    if required and not text:
        raise ValueError(f"{field} is required")
    return text


# ---------------------------------------------------------------------------
# CRUD operations
# ---------------------------------------------------------------------------


def item_from_args(args: argparse.Namespace) -> dict[str, str]:
    now = now_iso()
    return {
        "correction_id": clean(args.correction_id, "correction-id"),
        "title": clean(args.title, "title"),
        "severity": args.severity,
        "error_type": clean(args.error_type, "error-type", required=False),
        "status": args.status,
        "related_task_id": clean(args.related_task_id, "related-task-id", required=False),
        "created_at": now,
        "updated_at": now,
        "problem": clean(args.problem, "problem", required=False),
        "root_cause": clean(args.root_cause, "root-cause", required=False),
        "violated_rule": clean(args.violated_rule, "violated-rule", required=False),
        "impact": clean(args.impact, "impact", required=False),
        "fix_action": clean(args.fix_action, "fix-action", required=False),
        "upgrade_judgment": clean(args.upgrade_judgment, "upgrade-judgment", required=False),
        "source_md_path": "",
        "source": clean(args.source, "source", required=False) or "cli",
    }


def upsert_item(con: sqlite3.Connection, item: dict[str, str], *, replace: bool) -> None:
    existing = con.execute(
        "SELECT created_at FROM corrections WHERE correction_id = ?",
        (item["correction_id"],),
    ).fetchone()
    if existing and not replace:
        raise ValueError(
            f"correction already exists: {item['correction_id']} (use --replace)"
        )
    if existing:
        item["created_at"] = str(existing["created_at"])
    con.execute(
        """
        INSERT INTO corrections (
            correction_id, title, severity, error_type, status,
            related_task_id, created_at, updated_at,
            problem, root_cause, violated_rule, impact,
            fix_action, upgrade_judgment, source_md_path, source
        ) VALUES (
            :correction_id, :title, :severity, :error_type, :status,
            :related_task_id, :created_at, :updated_at,
            :problem, :root_cause, :violated_rule, :impact,
            :fix_action, :upgrade_judgment, :source_md_path, :source
        )
        ON CONFLICT(correction_id) DO UPDATE SET
            title = excluded.title,
            severity = excluded.severity,
            error_type = excluded.error_type,
            status = excluded.status,
            related_task_id = excluded.related_task_id,
            updated_at = excluded.updated_at,
            problem = excluded.problem,
            root_cause = excluded.root_cause,
            violated_rule = excluded.violated_rule,
            impact = excluded.impact,
            fix_action = excluded.fix_action,
            upgrade_judgment = excluded.upgrade_judgment,
            source_md_path = excluded.source_md_path,
            source = excluded.source
        """,
        item,
    )
    con.commit()


def list_items(
    con: sqlite3.Connection,
    *,
    statuses: list[str] | None,
    include_closed: bool,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[str] = []
    if statuses:
        clauses.append("status IN (" + ", ".join("?" for _ in statuses) + ")")
        params.extend(statuses)
    elif not include_closed:
        clauses.append("status NOT IN ('resolved', 'rejected', 'extracted')")
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    query = (
        "SELECT correction_id, title, severity, error_type, status, "
        "related_task_id, created_at, updated_at, "
        "problem, root_cause, violated_rule, impact, fix_action, upgrade_judgment, "
        "source_md_path, source "
        f"FROM corrections{where} ORDER BY severity, updated_at DESC, correction_id"
    )
    return [dict(row) for row in con.execute(query, params)]


# ---------------------------------------------------------------------------
# Report (mirrors framework_debt.build_report pattern)
# ---------------------------------------------------------------------------


def build_report(items: list[dict[str, Any]]) -> dict[str, Any]:
    open_items = [item for item in items if item["status"] in OPEN_STATUSES]
    return {
        "count": len(items),
        "open_count": len(open_items),
        "by_severity": _count_by(open_items, "severity"),
        "by_status": _count_by(items, "status"),
        "by_error_type": _count_by(open_items, "error_type"),
        "has_p0": any(item["severity"] == "P0" for item in open_items),
        "items": items,
    }


def _count_by(items: list[dict[str, Any]], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        key = str(item.get(field) or "")
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def render_text(items: list[dict[str, Any]]) -> str:
    lines = ["Correction register:"]
    if not items:
        lines.append("- no matching items")
        return "\n".join(lines)
    for item in items:
        lines.append(
            f"- {item['correction_id']} [{item['severity']}/{item['status']}] {item['title']}"
        )
        lines.append(f"  error_type: {item['error_type']}")
        lines.append(f"  task: {item['related_task_id']}")
    return "\n".join(lines)


def render_markdown(items: list[dict[str, Any]]) -> str:
    lines = [
        "# Corrections Register",
        "",
        "| ID | Severity | Type | Status | Task | Title |",
        "|---|---|---|---|---|---|",
    ]
    for item in items:
        lines.append(
            f"| {item['correction_id']} | {item['severity']} "
            f"| {item['error_type']} | {item['status']} "
            f"| {item['related_task_id']} | {item['title']} |"
        )
    lines.append("")
    return "\n".join(lines)


def render_report_text(report: dict[str, Any]) -> str:
    lines = [
        "Correction report:",
        f"- total: {report['count']}, open: {report['open_count']}",
        f"- by severity: {json.dumps(report['by_severity'], ensure_ascii=False, sort_keys=True)}",
        f"- by status: {json.dumps(report['by_status'], ensure_ascii=False, sort_keys=True)}",
        f"- by error_type: {json.dumps(report['by_error_type'], ensure_ascii=False, sort_keys=True)}",
        f"- has P0: {report['has_p0']}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Markdown import (migrate legacy .md correction files)
# ---------------------------------------------------------------------------

_SECTION_PATTERN = re.compile(
    r"^##\s+(.+?)\s*$([\s\S]*?)(?=^##\s+|\Z)",
    re.MULTILINE,
)

_FRONTMATTER_FIELDS = {
    "严重度": "severity",
    "类型": "error_type",
    "状态": "status",
    "关联任务": "related_task_id",
    "创建时间": "created_at",
}

_SECTION_FIELD_MAP = {
    "问题": "problem",
    "根因": "root_cause",
    "违反规则": "violated_rule",
    "实际影响": "impact",
    "修正动作": "fix_action",
    "升级判定": "upgrade_judgment",
    # Legacy template sections
    "具体遗漏": "problem",
    "根因判断": "root_cause",
    "即时修复动作": "fix_action",
    "候选规则": "upgrade_judgment",
}


def _extract_frontmatter(text: str) -> dict[str, str]:
    """Extract key-value pairs from the leading table in a correction .md."""
    result: dict[str, str] = {}
    in_table = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("| 字段") or stripped.startswith("|---"):
            in_table = True
            continue
        if in_table and stripped.startswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if len(cells) >= 2:
                key, value = cells[0], cells[1]
                norm_key = _FRONTMATTER_FIELDS.get(key, key)
                result[norm_key] = value
        elif in_table:
            break
    return result


def _extract_sections(text: str) -> dict[str, str]:
    """Extract ## sections into a dict keyed by normalized field name."""
    result: dict[str, str] = {}
    for match in _SECTION_PATTERN.finditer(text):
        heading = match.group(1).strip()
        body = match.group(2).strip()
        field = _SECTION_FIELD_MAP.get(heading)
        if field:
            result[field] = body
    return result


def parse_correction_md(path: Path, root: Path) -> dict[str, str]:
    """Parse a legacy correction Markdown file into a DB row dict."""
    text = path.read_text(encoding="utf-8")
    title_match = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
    title = title_match.group(1).strip() if title_match else path.stem

    fm = _extract_frontmatter(text)
    sections = _extract_sections(text)

    now = now_iso()
    return {
        "correction_id": title.replace(" ", "-") if title else path.stem,
        "title": title,
        "severity": fm.get("severity", "P1"),
        "error_type": fm.get("error_type", ""),
        "status": fm.get("status", "open"),
        "related_task_id": fm.get("related_task_id", ""),
        "created_at": fm.get("created_at", now),
        "updated_at": now,
        "problem": sections.get("problem", ""),
        "root_cause": sections.get("root_cause", ""),
        "violated_rule": sections.get("violated_rule", ""),
        "impact": sections.get("impact", ""),
        "fix_action": sections.get("fix_action", ""),
        "upgrade_judgment": sections.get("upgrade_judgment", ""),
        "source_md_path": path.relative_to(root).as_posix(),
        "source": "import-md",
    }


def import_md(
    con: sqlite3.Connection,
    root: Path,
    corrections_dir: Path,
    *,
    replace: bool,
) -> list[dict[str, str]]:
    """Import all correction .md files from *corrections_dir* into the DB.

    Skips ``index.md`` and ``README.md``.  Idempotent: existing rows are
    skipped unless ``replace=True``.
    """
    imported: list[dict[str, str]] = []
    for path in sorted(corrections_dir.glob("*.md")):
        if path.name in {"index.md", "README.md"}:
            continue
        item = parse_correction_md(path, root)
        existing = con.execute(
            "SELECT correction_id FROM corrections WHERE correction_id = ?",
            (item["correction_id"],),
        ).fetchone()
        if existing and not replace:
            continue
        upsert_item(con, item, replace=True)
        imported.append(item)
    return imported


# ---------------------------------------------------------------------------
# CLI argument parser
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Manage correction records in SQLite."
    )
    common_cli_args.add_common_global_args(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    # init
    init_p = sub.add_parser("init", help="Create the corrections table.")
    common_cli_args.add_common_global_args(init_p, suppress_default=True)

    # add
    add_p = sub.add_parser("add", help="Add or update one correction record.")
    common_cli_args.add_common_global_args(add_p, suppress_default=True)
    add_p.add_argument("--correction-id", required=True)
    add_p.add_argument("--title", required=True)
    add_p.add_argument("--severity", choices=SEVERITIES, default="P1")
    add_p.add_argument("--error-type", default="")
    add_p.add_argument("--status", choices=STATUSES, default="open")
    add_p.add_argument("--related-task-id", default="")
    add_p.add_argument("--problem", default="")
    add_p.add_argument("--root-cause", default="")
    add_p.add_argument("--violated-rule", default="")
    add_p.add_argument("--impact", default="")
    add_p.add_argument("--fix-action", default="")
    add_p.add_argument("--upgrade-judgment", default="")
    add_p.add_argument("--source", default="cli")
    add_p.add_argument("--replace", action="store_true")

    # list
    list_p = sub.add_parser("list", help="List correction records.")
    common_cli_args.add_common_global_args(list_p, suppress_default=True)
    list_p.add_argument("--status", action="append", choices=STATUSES, default=[])
    list_p.add_argument("--include-closed", action="store_true")

    # report
    report_p = sub.add_parser(
        "report", help="Summarize open correction records."
    )
    common_cli_args.add_common_global_args(report_p, suppress_default=True)
    report_p.add_argument("--status", action="append", choices=STATUSES, default=[])
    report_p.add_argument("--include-closed", action="store_true")
    report_p.add_argument(
        "--fail-on-open-p0",
        action="store_true",
        help="Exit 1 when any open P0 item exists.",
    )

    # export-md
    export_p = sub.add_parser(
        "export-md", help="Export matching corrections as Markdown."
    )
    common_cli_args.add_common_global_args(export_p, suppress_default=True)
    export_p.add_argument("--status", action="append", choices=STATUSES, default=[])
    export_p.add_argument("--include-closed", action="store_true")
    export_p.add_argument("--output", help="Output file path. Defaults to stdout.")

    # import-md
    import_p = sub.add_parser(
        "import-md",
        help="Import legacy correction .md files into the DB (idempotent).",
    )
    common_cli_args.add_common_global_args(import_p, suppress_default=True)
    import_p.add_argument(
        "--corrections-dir",
        default="",
        help="Directory containing legacy .md files. "
        "Default: .ai-client/project/records/corrections/",
    )
    import_p.add_argument(
        "--replace",
        action="store_true",
        help="Overwrite existing DB rows with the same correction_id.",
    )

    return parser.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    args = parse_args()
    root = Path(args.root).resolve()
    path = db_path(root, args.db)
    try:
        con = connect(path)

        if args.command == "init":
            payload = {"db": str(path), "initialized": True}
            _out(args, payload, f"initialized: {path}")
            return 0

        if args.command == "add":
            item = item_from_args(args)
            upsert_item(con, item, replace=args.replace)
            payload = {
                "db": str(path),
                "correction_id": item["correction_id"],
                "status": item["status"],
            }
            _out(args, payload, f"recorded: {item['correction_id']}")
            return 0

        if args.command == "list":
            items = list_items(
                con, statuses=args.status, include_closed=args.include_closed
            )
            payload = {"db": str(path), "count": len(items), "items": items}
            _out(args, payload, render_text(items))
            return 0

        if args.command == "report":
            items = list_items(
                con, statuses=args.status, include_closed=args.include_closed
            )
            report = build_report(items)
            payload = {"db": str(path), **report}
            _out(args, payload, render_report_text(payload))
            return 1 if args.fail_on_open_p0 and payload["has_p0"] else 0

        if args.command == "export-md":
            items = list_items(
                con, statuses=args.status, include_closed=args.include_closed
            )
            text = render_markdown(items)
            if args.output:
                output = Path(args.output)
                if not output.is_absolute():
                    output = root / output
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(text, encoding="utf-8", newline="\n")
                _out(
                    args,
                    {"output": str(output), "count": len(items)},
                    str(output),
                )
            else:
                print(text)
            return 0

        if args.command == "import-md":
            cor_dir = Path(args.corrections_dir) if args.corrections_dir else root / CORRECTIONS_DIR
            imported = import_md(con, root, cor_dir, replace=args.replace)
            payload = {
                "db": str(path),
                "imported_count": len(imported),
                "imported": [
                    {"correction_id": i["correction_id"], "source_md_path": i["source_md_path"]}
                    for i in imported
                ],
            }
            _out(args, payload, f"imported {len(imported)} correction(s)")
            return 0

    except (sqlite3.Error, ValueError) as exc:
        print(f"corrections error: {exc}", file=sys.stderr)
        return 1
    return 2


def _out(args: argparse.Namespace, payload: dict, text: str) -> None:
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(text)


if __name__ == "__main__":
    raise SystemExit(main())
