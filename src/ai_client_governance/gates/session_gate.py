#!/usr/bin/env python3
"""Read-only gate for AI Client Governance session closure.

The gate checks whether an active pending task is still present and whether
the current task tracking file records an execution loop: main task,
interrupting task, return action, current status, and next step.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from ai_client_governance.common.paths import CORRECTIONS_INDEX, PENDING_INDEX, PYTHON_PYCACHE_DIR

LOOP_HEADING = "执行闭环门禁"
REQUIRED_LOOP_LABELS = ["主任务", "插入任务", "返回动作", "当前状态", "下一步"]
BRANCH_HEADING = "主任务分支状态门禁"
REQUIRED_BRANCH_LABELS = ["分支", "状态", "证据", "下一步"]
BRANCH_GATE_SIGNALS = ["多分支主任务", "主任务分支", "支线遗漏", "分支状态门禁", "并行分支"]
DONE_STATUSES = {"已完成", "已废弃", "废弃", "完成"}
BRANCH_DONE_PREFIXES = ("已完成", "完成", "已废弃", "废弃", "不适用", "已处理")
BRANCH_OPEN_MARKERS = ("待", "处理中", "未", "阻塞", "继续", "验证", "回归")
BRANCH_KEYWORD_STOPWORDS = {
    "拥有",
    "生效",
    "问题",
    "分支",
    "支线",
    "状态",
    "门禁",
    "运行时",
    "扫描",
    "候选",
    "配置",
    "确认",
    "验证",
    "效果",
    "过滤",
}


@dataclass
class PendingTask:
    task_id: str
    title: str
    title_target: str
    status: str
    tracking: str
    tracking_path: str
    next_step: str


@dataclass
class BranchRow:
    branch: str
    status: str
    evidence: str
    next_step: str


@dataclass
class Finding:
    level: str
    message: str
    file: str | None = None


@dataclass
class Report:
    root: str
    active_pending_count: int
    task_tracking: str | None
    checked_pending: list[PendingTask]
    errors: list[Finding]
    warnings: list[Finding]
    notes: list[Finding]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check AI Client Governance pending/tracking execution-loop gates."
    )
    parser.add_argument(
        "--root",
        default=".",
        help="Repository root. Defaults to current working directory.",
    )
    parser.add_argument(
        "--task-tracking",
        help="Current task tracking file to validate.",
    )
    parser.add_argument(
        "--require-task-tracking",
        action="store_true",
        help="Fail when active pending exists but no task tracking file is provided.",
    )
    parser.add_argument(
        "--fail-on-warning",
        action="store_true",
        help="Exit non-zero when warnings are found.",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format.",
    )
    parser.add_argument(
        "--task-types",
        nargs="*",
        default=None,
        help="Optional task types to validate with ai_client_governance.py task-gate.",
    )
    parser.add_argument(
        "--require-task-gate",
        action="store_true",
        help="Run task-type evidence gate and fail when required evidence is missing.",
    )
    parser.add_argument(
        "--allow-inserted-task-tracking",
        action="store_true",
        help="Allow a non-linked tracking file when it records the active pending task ID and return action.",
    )
    return parser.parse_args()


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def normalize_cell(value: str) -> str:
    value = value.strip().replace("`", "")
    value = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", value)
    return value.strip()


def rel_path(path: Path, root: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def section_text(text: str, heading: str) -> str:
    pattern = re.compile(
        rf"^##\s+{re.escape(heading)}\s*$([\s\S]*?)(?=^##\s+|\Z)",
        re.MULTILINE,
    )
    match = pattern.search(text)
    return match.group(1) if match else ""


def parse_markdown_link(value: str) -> tuple[str, str]:
    match = re.search(r"\[([^\]]+)\]\(([^)]+)\)", value)
    if not match:
        return normalize_cell(value), ""
    return normalize_cell(match.group(1)), match.group(2).strip()


def parse_active_pending(root: Path) -> list[PendingTask]:
    index_path = root / PENDING_INDEX
    if not index_path.exists():
        return []

    text = read_text(index_path)
    active_section = section_text(text, "当前活跃任务")
    tasks: list[PendingTask] = []
    base_dir = index_path.parent

    for line in active_section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        if stripped.startswith("|---") or "任务 ID" in stripped:
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 8:
            continue
        task_id, title_cell, status, _priority, _approval, tracking, _updated, next_step = cells[:8]
        title, title_target = parse_markdown_link(title_cell)
        status = normalize_cell(status)
        if status in DONE_STATUSES:
            continue
        tracking_target = normalize_cell(tracking)
        tracking_path = ""
        if tracking_target:
            tracking_path = rel_path((base_dir / tracking_target).resolve(), root)
        tasks.append(
            PendingTask(
                task_id=normalize_cell(task_id),
                title=title,
                title_target=title_target,
                status=status,
                tracking=tracking_target,
                tracking_path=tracking_path,
                next_step=normalize_cell(next_step),
            )
        )
    return tasks


def add(items: list[Finding], level: str, message: str, file: str | None = None) -> None:
    items.append(Finding(level=level, message=message, file=file))


def parse_index_status_counts(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}
    counts: dict[str, int] = {}
    for line in read_text(path).splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        if stripped.startswith("|---") or "文件 | 状态" in stripped:
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 2:
            continue
        if not re.search(r"\([^)]+\.md\)", cells[0]):
            continue
        status = normalize_cell(cells[1])
        counts[status] = counts.get(status, 0) + 1
    return counts


def branch_gate_required(text: str) -> bool:
    return any(signal in text for signal in BRANCH_GATE_SIGNALS)


def markdown_table_rows(section: str) -> list[str]:
    return [line.strip() for line in section.splitlines() if line.strip().startswith("|")]


def branch_status_is_open(status: str) -> bool:
    normalized = normalize_cell(status)
    if not normalized:
        return True
    if any(marker in normalized for marker in BRANCH_OPEN_MARKERS):
        return True
    return not any(normalized.startswith(prefix) for prefix in BRANCH_DONE_PREFIXES)


def branch_keywords(branch: str) -> list[str]:
    normalized = normalize_cell(branch)
    parts = re.split(r"[/、,，;；\s]+|和|与|及", normalized)
    keywords: list[str] = []
    for part in parts:
        token = part.strip()
        for stopword in BRANCH_KEYWORD_STOPWORDS:
            token = token.replace(stopword, "")
        token = token.strip()
        if len(token) >= 2 and token not in keywords:
            keywords.append(token)
    return keywords


def parse_branch_rows(section: str) -> list[BranchRow]:
    rows = markdown_table_rows(section)
    data_rows = [
        row
        for row in rows
        if not row.startswith("|---") and "分支" not in row.split("|")[1].strip()
    ]
    parsed: list[BranchRow] = []
    for row in data_rows:
        cells = [normalize_cell(cell) for cell in row.strip("|").split("|")]
        if len(cells) < len(REQUIRED_BRANCH_LABELS):
            continue
        parsed.append(
            BranchRow(
                branch=cells[0],
                status=cells[1],
                evidence=cells[2],
                next_step=cells[3],
            )
        )
    return parsed


def validate_branch_status_gate(
    tracking_text: str,
    tracking_rel: str,
    active_tasks: list[PendingTask],
    errors: list[Finding],
    notes: list[Finding],
) -> None:
    section = section_text(tracking_text, BRANCH_HEADING)
    if not section.strip():
        add(
            errors,
            "error",
            f"Tracking indicates a multi-branch main task but is missing '## {BRANCH_HEADING}'.",
            tracking_rel,
        )
        return

    rows = markdown_table_rows(section)
    header = next((row for row in rows if not row.startswith("|---")), "")
    for label in REQUIRED_BRANCH_LABELS:
        if label not in header:
            add(
                errors,
                "error",
                f"Branch status gate table is missing header: {label}.",
                tracking_rel,
            )

    data_rows = [
        row
        for row in rows
        if not row.startswith("|---") and "分支" not in row.split("|")[1].strip()
    ]
    if len(data_rows) < 2:
        add(
            errors,
            "error",
            "Branch status gate should record at least two branch rows.",
            tracking_rel,
        )
        return

    for row in data_rows:
        cells = [normalize_cell(cell) for cell in row.strip("|").split("|")]
        if len(cells) < len(REQUIRED_BRANCH_LABELS):
            add(errors, "error", "Branch status row has too few columns.", tracking_rel)
            continue
        for index, label in enumerate(REQUIRED_BRANCH_LABELS):
            if not cells[index]:
                add(
                    errors,
                    "error",
                    f"Branch status row has empty {label} cell.",
                    tracking_rel,
                )

    branch_rows = parse_branch_rows(section)
    linked_next_steps = "\n".join(
        task.next_step for task in active_tasks if task.tracking_path == tracking_rel
    )
    if linked_next_steps:
        for branch_row in branch_rows:
            if not branch_status_is_open(branch_row.status):
                continue
            keywords = branch_keywords(branch_row.branch)
            if keywords and not any(keyword in linked_next_steps for keyword in keywords):
                add(
                    errors,
                    "error",
                    "Active pending next step does not mention open branch "
                    f"'{branch_row.branch}' (expected one of: {', '.join(keywords)}).",
                    tracking_rel,
                )

    add(notes, "note", "Branch status gate is present.", tracking_rel)


def validate_tracking(
    root: Path,
    tracking_arg: str | None,
    active_tasks: list[PendingTask],
    errors: list[Finding],
    warnings: list[Finding],
    notes: list[Finding],
    allow_inserted_tracking: bool = False,
) -> str | None:
    if not tracking_arg:
        return None

    tracking_path = Path(tracking_arg)
    if not tracking_path.is_absolute():
        tracking_path = root / tracking_path
    if not tracking_path.exists():
        add(errors, "error", "Task tracking file does not exist.", str(tracking_path))
        return rel_path(tracking_path, root)

    tracking_rel = rel_path(tracking_path, root)
    tracking_text = read_text(tracking_path)
    loop_section = section_text(tracking_text, LOOP_HEADING)

    if active_tasks and not loop_section.strip():
        add(
            errors,
            "error",
            f"Task tracking is missing '## {LOOP_HEADING}'.",
            tracking_rel,
        )
    elif loop_section:
        for label in REQUIRED_LOOP_LABELS:
            if label not in loop_section:
                add(
                    errors,
                    "error",
                    f"Execution loop section is missing label: {label}.",
                    tracking_rel,
                )

    if active_tasks:
        task_ids = [task.task_id for task in active_tasks if task.task_id]
        if task_ids and not any(task_id in tracking_text for task_id in task_ids):
            add(
                errors,
                "error",
                "Tracking does not mention any active pending task ID.",
                tracking_rel,
            )
        linked_to_active = any(task.tracking_path == tracking_rel for task in active_tasks)
        inserted_tracking = (
            allow_inserted_tracking
            and loop_section
            and task_ids
            and any(task_id in tracking_text for task_id in task_ids)
        )
        if not linked_to_active and not inserted_tracking:
            add(
                warnings,
                "warning",
                "Provided task tracking is not the linked tracking of any active pending task.",
                tracking_rel,
            )
        elif inserted_tracking:
            add(
                notes,
                "note",
                "Provided task tracking is treated as an inserted-task tracking with active pending return action.",
                tracking_rel,
            )
        if (
            linked_to_active
            and (branch_gate_required(tracking_text) or BRANCH_HEADING in tracking_text)
        ):
            validate_branch_status_gate(
                tracking_text, tracking_rel, active_tasks, errors, notes
            )

    if "返回动作" in tracking_text and "下一步" in tracking_text:
        add(notes, "note", "Execution loop return markers are present.", tracking_rel)

    return tracking_rel


def validate_task_gate(
    root: Path,
    tracking_arg: str | None,
    task_types: list[str] | None,
    require_task_gate: bool,
    errors: list[Finding],
    warnings: list[Finding],
    notes: list[Finding],
) -> None:
    if not require_task_gate and not task_types:
        return
    if not tracking_arg:
        add(errors, "error", "Task gate requested but no --task-tracking was provided.")
        return

    pycache_supported = hasattr(sys, "pycache_prefix")
    previous_pycache_prefix = getattr(sys, "pycache_prefix", None)
    try:
        if pycache_supported:
            pycache_prefix = root / PYTHON_PYCACHE_DIR
            pycache_prefix.mkdir(parents=True, exist_ok=True)
            sys.pycache_prefix = str(pycache_prefix)
        else:
            add(
                warnings,
                "warning",
                "Python runtime does not expose sys.pycache_prefix; imported task gate may use default pycache.",
            )
        from ai_client_governance.gates.task_gate import build_report as build_task_gate_report
    except Exception as exc:  # pragma: no cover - defensive import report
        add(errors, "error", f"Unable to import ai_client_governance.gates.task_gate: {exc}")
        return
    finally:
        if pycache_supported:
            sys.pycache_prefix = previous_pycache_prefix

    task_report = build_task_gate_report(
        root=root,
        task_tracking_arg=tracking_arg,
        explicit_task_types=task_types,
        require_task_types=require_task_gate,
    )
    for item in task_report.errors:
        add(errors, "error", f"Task gate: {item.message}", item.file)
    for item in task_report.warnings:
        add(warnings, "warning", f"Task gate: {item.message}", item.file)
    for item in task_report.notes:
        add(notes, "note", f"Task gate: {item.message}", item.file)


def build_report(
    root: Path,
    tracking_arg: str | None,
    require_tracking: bool,
    allow_inserted_tracking: bool = False,
) -> Report:
    errors: list[Finding] = []
    warnings: list[Finding] = []
    notes: list[Finding] = []

    active_tasks = parse_active_pending(root)
    pending_index = root / PENDING_INDEX
    if not pending_index.exists():
        add(errors, "error", "Pending index is missing.", rel_path(pending_index, root))

    for task in active_tasks:
        if not task.title_target:
            add(errors, "error", f"Active pending task {task.task_id} has no task file link.")
        else:
            task_file = pending_index.parent / task.title_target
            if not task_file.exists():
                add(errors, "error", "Active pending task file is missing.", rel_path(task_file, root))
        if not task.tracking_path:
            add(errors, "error", f"Active pending task {task.task_id} has no linked tracking.")
        else:
            linked_tracking = root / task.tracking_path
            if not linked_tracking.exists():
                add(errors, "error", "Linked tracking file is missing.", task.tracking_path)
        if not task.next_step or task.next_step in {"无", "none", "None"}:
            add(errors, "error", f"Active pending task {task.task_id} has no next step.")

    if active_tasks and require_tracking and not tracking_arg:
        add(
            errors,
            "error",
            "Active pending exists; provide --task-tracking for final gate validation.",
        )

    tracking_rel = validate_tracking(
        root=root,
        tracking_arg=tracking_arg,
        active_tasks=active_tasks,
        errors=errors,
        warnings=warnings,
        notes=notes,
        allow_inserted_tracking=allow_inserted_tracking,
    )

    validate_task_gate(
        root=root,
        tracking_arg=tracking_arg,
        task_types=None,
        require_task_gate=False,
        errors=errors,
        warnings=warnings,
        notes=notes,
    )

    correction_counts = parse_index_status_counts(root / CORRECTIONS_INDEX)
    if correction_counts.get("待记录", 0) > 0:
        add(errors, "error", "Corrections index still has records in 待记录.", str(CORRECTIONS_INDEX))
    if correction_counts.get("待提炼", 0) > 0:
        add(
            notes,
            "note",
            f"Corrections index has {correction_counts['待提炼']} record(s) in 待提炼.",
            str(CORRECTIONS_INDEX),
        )

    if not active_tasks:
        add(notes, "note", "No active pending tasks found.", rel_path(pending_index, root))

    return Report(
        root=str(root.resolve()),
        active_pending_count=len(active_tasks),
        task_tracking=tracking_rel,
        checked_pending=active_tasks,
        errors=errors,
        warnings=warnings,
        notes=notes,
    )


def format_findings(title: str, items: list[Finding]) -> list[str]:
    lines = [f"{title}: {len(items)}"]
    if not items:
        lines.append("  none")
        return lines
    for item in items:
        location = f" [{item.file}]" if item.file else ""
        lines.append(f"  - {item.message}{location}")
    return lines


def format_text(report: Report) -> str:
    lines = [
        "AI Client Governance Session Gate Report",
        f"Root: {report.root}",
        f"Active pending tasks: {report.active_pending_count}",
        f"Task tracking: {report.task_tracking or 'none'}",
        "",
        "Active pending:",
    ]
    if not report.checked_pending:
        lines.append("  none")
    else:
        for task in report.checked_pending:
            lines.append(
                f"  - {task.task_id}: {task.status}; tracking={task.tracking_path}; next={task.next_step}"
            )
    lines.append("")
    lines.extend(format_findings("Errors", report.errors))
    lines.append("")
    lines.extend(format_findings("Warnings", report.warnings))
    lines.append("")
    lines.extend(format_findings("Notes", report.notes))
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    root = Path(args.root).resolve()
    report = build_report(
        root=root,
        tracking_arg=args.task_tracking,
        require_tracking=args.require_task_tracking,
        allow_inserted_tracking=args.allow_inserted_task_tracking,
    )
    validate_task_gate(
        root=root,
        tracking_arg=args.task_tracking,
        task_types=args.task_types,
        require_task_gate=args.require_task_gate,
        errors=report.errors,
        warnings=report.warnings,
        notes=report.notes,
    )

    if args.format == "json":
        print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
    else:
        print(format_text(report))

    if report.errors:
        return 1
    if args.fail_on_warning and report.warnings:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

