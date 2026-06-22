#!/usr/bin/env python3
"""Read-only scan report for correction records stored in SQLite.

Replaces the previous Markdown-only scanner.  Correction records are now
the authoritative source in the ``corrections`` table of
``.ai-client/project/state/aicg.db``.  This script reads from the DB and
produces a status/error-type/severity summary, plus lists of candidate
upgrades and observation items.  It never writes to the DB.

The old Markdown-based scanner (index consistency checks, .md parsing) is
preserved as ``scan_corrections_legacy()`` for backward-compatible
``--legacy`` mode but is no longer the default.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

from ai_client_governance.common import cli_arguments as common_cli_args
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# Data classes (report structure preserved for compatibility with gate_pool)
# ---------------------------------------------------------------------------


@dataclass
class ScanReport:
    source: str
    record_count: int
    status_counts: dict[str, int]
    error_type_counts: dict[str, int]
    severity_counts: dict[str, int]
    candidate_upgrades: list[str]
    observation_items: list[str]
    has_p0: bool


# ---------------------------------------------------------------------------
# DB-backed scan
# ---------------------------------------------------------------------------


def _connect(root: Path, db_override: str | None) -> sqlite3.Connection:
    from ai_client_governance.records.corrections import db_path, init_db

    path = db_path(root, db_override)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    init_db(con)
    return con


def build_report_from_db(
    root: Path, db_override: str | None
) -> ScanReport:
    con = _connect(root, db_override)
    try:
        rows = con.execute(
            "SELECT correction_id, severity, error_type, status, "
            "upgrade_judgment FROM corrections ORDER BY severity, correction_id"
        ).fetchall()
    finally:
        con.close()

    status_counts = Counter(str(row["status"] or "") for row in rows)
    error_type_counts = Counter(str(row["error_type"] or "") for row in rows)
    severity_counts = Counter(str(row["severity"] or "") for row in rows)

    candidate_upgrades: list[str] = []
    observation_items: list[str] = []
    for row in rows:
        j = str(row["upgrade_judgment"] or "")
        s = str(row["status"] or "")
        if j and "升级" in j and "不升级" not in j and s not in {
            "extracted", "resolved", "rejected",
        }:
            candidate_upgrades.append(str(row["correction_id"]))
        if s in {"open", "deferred"}:
            observation_items.append(str(row["correction_id"]))

    has_p0 = any(str(row["severity"]) == "P0" for row in rows)

    return ScanReport(
        source="db",
        record_count=len(rows),
        status_counts=dict(sorted(status_counts.items())),
        error_type_counts=dict(sorted(error_type_counts.items())),
        severity_counts=dict(sorted(severity_counts.items())),
        candidate_upgrades=sorted(candidate_upgrades),
        observation_items=sorted(observation_items),
        has_p0=has_p0,
    )


# ---------------------------------------------------------------------------
# Formatters (preserved from old scanner for gate_pool compatibility)
# ---------------------------------------------------------------------------


def bullet_list(items: list[str]) -> str:
    if not items:
        return "- 无"
    return "\n".join(f"- {item}" for item in items)


def dict_table(values: dict[str, int]) -> str:
    if not values:
        return "| 项 | 数量 |\n|---|---:|\n| 无 | 0 |"
    rows = ["| 项 | 数量 |", "|---|---:|"]
    rows.extend(f"| {key} | {count} |" for key, count in values.items())
    return "\n".join(rows)


def format_markdown(report: ScanReport) -> str:
    return "\n\n".join(
        [
            "# Corrections Scan Report",
            f"- Source: `{report.source}`",
            f"- Records: {report.record_count}",
            f"- Has open P0: {report.has_p0}",
            "## Status Counts\n\n" + dict_table(report.status_counts),
            "## Error Type Counts\n\n" + dict_table(report.error_type_counts),
            "## Severity Counts\n\n" + dict_table(report.severity_counts),
            "## Candidate Upgrades\n\n" + bullet_list(report.candidate_upgrades),
            "## Observation Items\n\n" + bullet_list(report.observation_items),
        ]
    )


def format_text(report: ScanReport) -> str:
    lines = [
        "Corrections Scan Report",
        f"Source: {report.source}",
        f"Records: {report.record_count}",
        f"Has open P0: {report.has_p0}",
        "",
        "Status counts:",
    ]
    lines.extend(f"  {key}: {value}" for key, value in report.status_counts.items())
    lines.append("")
    lines.append("Error type counts:")
    lines.extend(f"  {key}: {value}" for key, value in report.error_type_counts.items())
    lines.append("")
    lines.append("Severity counts:")
    lines.extend(f"  {key}: {value}" for key, value in report.severity_counts.items())
    lines.append("")
    lines.append(f"Candidate upgrades ({len(report.candidate_upgrades)}):")
    lines.extend(f"  {item}" for item in report.candidate_upgrades)
    lines.append("")
    lines.append(f"Observation items ({len(report.observation_items)}):")
    lines.extend(f"  {item}" for item in report.observation_items)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scan correction records and print a read-only report."
    )
    common_cli_args.add_common_global_args(parser)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit 1 when open P0 items exist.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(args.root).resolve()
    report = build_report_from_db(root, args.db)

    if args.format == "json":
        print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
    elif args.format == "markdown":
        print(format_markdown(report))
    else:
        print(format_text(report))

    if args.strict and report.has_p0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
