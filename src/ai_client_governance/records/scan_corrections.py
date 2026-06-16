#!/usr/bin/env python3
"""Read-only report for .codex/project/records/corrections records.

The script scans independent correction records and the derived index, then
reports status counts, error-type groups, upgrade candidates, observation
items, and index/record inconsistencies. It never writes files.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from ai_client_governance.common.paths import CORRECTIONS_DIR

CORRECTION_DIR = CORRECTIONS_DIR
INDEX_FILE = "index.md"
README_FILE = "README.md"


@dataclass
class CorrectionRecord:
    file: str
    title: str
    status: str
    error_type: str
    needs_upgrade: str
    tracking: str


@dataclass
class IndexRecord:
    file: str
    status: str
    error_type: str
    needs_upgrade: str
    tracking: str


@dataclass
class ScanReport:
    corrections_dir: str
    record_count: int
    index_count: int
    status_counts: dict[str, int]
    error_type_counts: dict[str, int]
    upgrade_counts: dict[str, int]
    missing_in_index: list[str]
    missing_record_files: list[str]
    status_mismatches: list[dict[str, str]]
    error_type_mismatches: list[dict[str, str]]
    upgrade_mismatches: list[dict[str, str]]
    candidate_upgrades: list[str]
    observation_items: list[str]


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def normalize_cell(value: str) -> str:
    value = value.strip()
    value = value.replace("`", "")
    value = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", value)
    return value.strip().rstrip("。.;；").strip()


def normalize_upgrade(value: str) -> str:
    value = normalize_cell(value)
    if value.startswith("是"):
        return "是"
    if value.startswith("否"):
        return "否"
    if "不升级" in value:
        return "否"
    if "升级" in value:
        return "是"
    return value or "未知"


def normalize_status(value: str) -> str:
    return normalize_cell(value) or "未知"


def section_text(text: str, heading: str) -> str:
    pattern = re.compile(
        rf"^##\s+{re.escape(heading)}\s*$([\s\S]*?)(?=^##\s+|\Z)",
        re.MULTILINE,
    )
    match = pattern.search(text)
    return match.group(1) if match else ""


def first_bullet_value(section: str, label: str | None = None) -> str:
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("-"):
            continue
        body = stripped.lstrip("-").strip()
        if label is None:
            return normalize_cell(body)
        if body.startswith(label):
            return normalize_cell(body.split("：", 1)[-1])
    return "未知"


def parse_record(path: Path, root: Path) -> CorrectionRecord:
    text = read_text(path)
    title_match = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
    title = normalize_cell(title_match.group(1)) if title_match else path.stem

    error_section = section_text(text, "错误类型")
    status_section = section_text(text, "当前状态")
    upgrade_section = section_text(text, "是否需要升级到规则/脚本/adapter") or section_text(
        text, "是否需要升级到 AGENTS.md"
    )
    tracking_section = section_text(text, "关联 task tracking")

    error_type = first_bullet_value(error_section, "类型")
    status = normalize_status(first_bullet_value(status_section, "状态"))
    needs_upgrade = normalize_upgrade(first_bullet_value(upgrade_section))
    tracking = first_bullet_value(tracking_section)

    return CorrectionRecord(
        file=path.relative_to(root).as_posix(),
        title=title,
        status=status,
        error_type=error_type,
        needs_upgrade=needs_upgrade,
        tracking=tracking,
    )


def parse_index(index_path: Path, root: Path) -> list[IndexRecord]:
    if not index_path.exists():
        return []

    records: list[IndexRecord] = []
    for line in read_text(index_path).splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        if stripped.startswith("|---") or "文件 | 状态" in stripped:
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 5:
            continue
        file_cell, status, error_type, needs_upgrade, tracking = cells[:5]
        file_match = re.search(r"\(([^)]+)\)", file_cell)
        if not file_match:
            continue
        file_name = file_match.group(1)
        if not file_name.endswith(".md"):
            continue
        records.append(
            IndexRecord(
                file=(index_path.parent / file_name).relative_to(root).as_posix(),
                status=normalize_status(status),
                error_type=normalize_cell(error_type) or "未知",
                needs_upgrade=normalize_upgrade(needs_upgrade),
                tracking=normalize_cell(tracking),
            )
        )
    return records


def iter_record_files(corrections_dir: Path) -> Iterable[Path]:
    for path in sorted(corrections_dir.glob("*.md")):
        if path.name in {INDEX_FILE, README_FILE}:
            continue
        yield path


def build_report(root: Path) -> ScanReport:
    corrections_dir = root / CORRECTION_DIR
    index_path = corrections_dir / INDEX_FILE

    records = [parse_record(path, root) for path in iter_record_files(corrections_dir)]
    index_records = parse_index(index_path, root)

    record_by_file = {record.file: record for record in records}
    index_by_file = {record.file: record for record in index_records}

    missing_in_index = sorted(set(record_by_file) - set(index_by_file))
    missing_record_files = sorted(set(index_by_file) - set(record_by_file))

    status_mismatches: list[dict[str, str]] = []
    error_type_mismatches: list[dict[str, str]] = []
    upgrade_mismatches: list[dict[str, str]] = []

    for file_name in sorted(set(record_by_file) & set(index_by_file)):
        record = record_by_file[file_name]
        indexed = index_by_file[file_name]
        if record.status != indexed.status:
            status_mismatches.append(
                {
                    "file": file_name,
                    "record": record.status,
                    "index": indexed.status,
                }
            )
        if record.error_type != indexed.error_type:
            error_type_mismatches.append(
                {
                    "file": file_name,
                    "record": record.error_type,
                    "index": indexed.error_type,
                }
            )
        if record.needs_upgrade != indexed.needs_upgrade:
            upgrade_mismatches.append(
                {
                    "file": file_name,
                    "record": record.needs_upgrade,
                    "index": indexed.needs_upgrade,
                }
            )

    status_counts = Counter(record.status for record in records)
    error_type_counts = Counter(record.error_type for record in records)
    upgrade_counts = Counter(record.needs_upgrade for record in records)

    candidate_upgrades = sorted(
        record.file
        for record in records
        if record.needs_upgrade == "是" and record.status not in {"已提炼进要求", "已废弃"}
    )
    observation_items = sorted(
        record.file
        for record in records
        if record.status in {"待记录", "待提炼", "暂不升级"}
    )

    return ScanReport(
        corrections_dir=CORRECTION_DIR.as_posix(),
        record_count=len(records),
        index_count=len(index_records),
        status_counts=dict(sorted(status_counts.items())),
        error_type_counts=dict(sorted(error_type_counts.items())),
        upgrade_counts=dict(sorted(upgrade_counts.items())),
        missing_in_index=missing_in_index,
        missing_record_files=missing_record_files,
        status_mismatches=status_mismatches,
        error_type_mismatches=error_type_mismatches,
        upgrade_mismatches=upgrade_mismatches,
        candidate_upgrades=candidate_upgrades,
        observation_items=observation_items,
    )


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


def mismatch_list(items: list[dict[str, str]]) -> str:
    if not items:
        return "- 无"
    lines = []
    for item in items:
        lines.append(
            f"- {item['file']}: record={item['record']} index={item['index']}"
        )
    return "\n".join(lines)


def format_markdown(report: ScanReport) -> str:
    return "\n\n".join(
        [
            "# Corrections Scan Report",
            f"- Corrections dir: `{report.corrections_dir}`",
            f"- Independent records: {report.record_count}",
            f"- Index rows: {report.index_count}",
            "## Status Counts\n\n" + dict_table(report.status_counts),
            "## Error Type Counts\n\n" + dict_table(report.error_type_counts),
            "## Upgrade Counts\n\n" + dict_table(report.upgrade_counts),
            "## Missing In Index\n\n" + bullet_list(report.missing_in_index),
            "## Index Rows Missing Record Files\n\n"
            + bullet_list(report.missing_record_files),
            "## Status Mismatches\n\n" + mismatch_list(report.status_mismatches),
            "## Error Type Mismatches\n\n"
            + mismatch_list(report.error_type_mismatches),
            "## Upgrade Mismatches\n\n" + mismatch_list(report.upgrade_mismatches),
            "## Candidate Upgrades\n\n" + bullet_list(report.candidate_upgrades),
            "## Observation Items\n\n" + bullet_list(report.observation_items),
        ]
    )


def format_text(report: ScanReport) -> str:
    lines = [
        "Corrections Scan Report",
        f"Corrections dir: {report.corrections_dir}",
        f"Independent records: {report.record_count}",
        f"Index rows: {report.index_count}",
        "",
        "Status counts:",
    ]
    lines.extend(f"  {key}: {value}" for key, value in report.status_counts.items())
    lines.append("")
    lines.append("Error type counts:")
    lines.extend(f"  {key}: {value}" for key, value in report.error_type_counts.items())
    lines.append("")
    lines.append("Upgrade counts:")
    lines.extend(f"  {key}: {value}" for key, value in report.upgrade_counts.items())

    checks = [
        ("Missing in index", report.missing_in_index),
        ("Index rows missing record files", report.missing_record_files),
        ("Status mismatches", report.status_mismatches),
        ("Error type mismatches", report.error_type_mismatches),
        ("Upgrade mismatches", report.upgrade_mismatches),
        ("Candidate upgrades", report.candidate_upgrades),
        ("Observation items", report.observation_items),
    ]
    for title, items in checks:
        lines.append("")
        lines.append(f"{title}:")
        if not items:
            lines.append("  none")
            continue
        for item in items:
            if isinstance(item, dict):
                lines.append(
                    f"  {item['file']}: record={item['record']} index={item['index']}"
                )
            else:
                lines.append(f"  {item}")
    return "\n".join(lines)


def has_inconsistencies(report: ScanReport) -> bool:
    return any(
        [
            report.missing_in_index,
            report.missing_record_files,
            report.status_mismatches,
            report.error_type_mismatches,
            report.upgrade_mismatches,
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scan .codex/project/records/corrections and print a read-only report."
    )
    parser.add_argument(
        "--root",
        default=".",
        help="Repository root. Defaults to current working directory.",
    )
    parser.add_argument(
        "--format",
        choices=("text", "markdown", "json"),
        default="text",
        help="Output format.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit with code 1 when index/record inconsistencies are found.",
    )
    args = parser.parse_args()

    root = Path(args.root).resolve()
    report = build_report(root)

    if args.format == "json":
        print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
    elif args.format == "markdown":
        print(format_markdown(report))
    else:
        print(format_text(report))

    if args.strict and has_inconsistencies(report):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

