#!/usr/bin/env python3
"""Render AI rules tool invocation flow from the local JSONL ledger.

The script is read-only. It turns .codex/project/logs/tool-invocations/*.jsonl
records into a time-sequence flow today, and it can use optional parent fields
when the ledger grows trace/tree support later.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from ai_rules.common.paths import TOOL_INVOCATIONS_DIR

DEFAULT_LEDGER_DIR = TOOL_INVOCATIONS_DIR
SUCCESS_STATUSES = {"success", "succeeded", "passed", "ok"}
FAILURE_STATUSES = {"failed", "error", "invalid"}
FINAL_GATE_NAMES = {
    "ai_rules.py session-gate",
    "ai_rules.py task-gate",
    "ai_rules.py validate-doc",
    "ai_rules.py validate-encoding",
    "ai_rules.py scan-corrections",
}


@dataclass(frozen=True)
class Invocation:
    invocation_id: str
    name: str
    status: str
    timestamp: str
    command: str
    task_tracking: str
    task_types: list[str]
    phase: str
    final_gate: bool
    exit_code: int | None
    summary: str
    parent_invocation_id: str
    trace_id: str
    task_node_id: str
    parent_task_node_id: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class FlowIssue:
    level: str
    message: str
    invocation_id: str = ""


@dataclass(frozen=True)
class FlowReport:
    root: str
    ledger_files: list[str]
    invocations: list[Invocation]
    issues: list[FlowIssue]
    summary: dict[str, Any]
    flow_mode: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render a flow graph from AI rules tool invocation JSONL ledgers."
    )
    parser.add_argument("--root", default=".", help="Repository root.")
    parser.add_argument(
        "--ledger-dir",
        help="Ledger directory. Defaults to .codex/project/logs/tool-invocations under root.",
    )
    parser.add_argument("--task-tracking", help="Filter by task tracking substring.")
    parser.add_argument("--trace-id", help="Filter by trace id.")
    parser.add_argument("--since", help="Only include invocations after this date/time.")
    parser.add_argument("--until", help="Only include invocations before this date/time.")
    parser.add_argument("--top", type=int, default=80, help="Maximum invocations to render.")
    parser.add_argument(
        "--format",
        choices=("text", "markdown", "json", "mermaid"),
        default="markdown",
        help="Output format. Default: markdown.",
    )
    parser.add_argument(
        "--require-final-gate",
        action="store_true",
        help="Exit non-zero when no final-gate invocation exists.",
    )
    parser.add_argument(
        "--require-task-session-order",
        action="store_true",
        help="Exit non-zero when a successful session gate appears without a prior successful task gate.",
    )
    parser.add_argument(
        "--require-report",
        action="store_true",
        help="Exit non-zero when no invocation records an ai_rules.py tool-invocations report.",
    )
    parser.add_argument(
        "--require-trace",
        action="store_true",
        help="Exit non-zero when matched invocations lack trace_id fields.",
    )
    parser.add_argument(
        "--fail-on-warning",
        action="store_true",
        help="Exit non-zero when warnings are present.",
    )
    return parser.parse_args()


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", normalized):
        normalized = normalized + "T00:00:00"
    result = datetime.fromisoformat(normalized)
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result


def event_time(event: dict[str, Any]) -> datetime:
    return parse_dt(str(event.get("timestamp", ""))) or datetime.min.replace(tzinfo=timezone.utc)


def ledger_dir(root: Path, ledger_dir_arg: str | None) -> Path:
    if ledger_dir_arg:
        path = Path(ledger_dir_arg)
        return path if path.is_absolute() else root / path
    return root / DEFAULT_LEDGER_DIR


def display_path(path: Path, root: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(root.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def iter_ledger_files(root: Path, ledger_dir_arg: str | None) -> list[Path]:
    directory = ledger_dir(root, ledger_dir_arg)
    if not directory.exists():
        return []
    return sorted(directory.glob("*.jsonl"))


def read_events(root: Path, ledger_dir_arg: str | None) -> tuple[list[dict[str, Any]], list[str]]:
    events: list[dict[str, Any]] = []
    files = iter_ledger_files(root, ledger_dir_arg)
    for path in files:
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                events.append(json.loads(stripped))
            except json.JSONDecodeError as exc:
                events.append(
                    {
                        "invocation_id": f"invalid:{display_path(path, root)}:{line_no}",
                        "timestamp": "",
                        "name": "invalid-json",
                        "status": "invalid",
                        "command": "",
                        "exit_code": None,
                        "summary": f"{display_path(path, root)}:{line_no}: {exc}",
                    }
                )
    return events, [display_path(path, root) for path in files]


def collapse_invocations(events: list[dict[str, Any]]) -> list[Invocation]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for index, event in enumerate(events):
        invocation_id = str(event.get("invocation_id") or f"missing:{index}")
        grouped[invocation_id].append(event)

    invocations: list[Invocation] = []
    for invocation_id, items in grouped.items():
        items = sorted(items, key=event_time)
        final_items = [item for item in items if str(item.get("status")) != "started"]
        chosen = final_items[-1] if final_items else items[-1]
        name = str(chosen.get("name") or "unknown")
        final_gate = bool(chosen.get("final_gate")) or name in FINAL_GATE_NAMES
        invocations.append(
            Invocation(
                invocation_id=invocation_id,
                name=name,
                status=str(chosen.get("status") or "unknown"),
                timestamp=str(chosen.get("timestamp") or ""),
                command=str(chosen.get("command") or ""),
                task_tracking=str(chosen.get("task_tracking") or ""),
                task_types=list(chosen.get("task_types") or []),
                phase=str(chosen.get("phase") or ""),
                final_gate=final_gate,
                exit_code=chosen.get("exit_code"),
                summary=str(chosen.get("summary") or ""),
                parent_invocation_id=str(chosen.get("parent_invocation_id") or ""),
                trace_id=str(chosen.get("trace_id") or ""),
                task_node_id=str(chosen.get("task_node_id") or ""),
                parent_task_node_id=str(chosen.get("parent_task_node_id") or ""),
                raw=chosen,
            )
        )
    return sorted(invocations, key=lambda item: parse_dt(item.timestamp) or datetime.min.replace(tzinfo=timezone.utc))


def filter_invocations(
    invocations: list[Invocation],
    task_tracking: str | None,
    trace_id: str | None,
    since: datetime | None,
    until: datetime | None,
) -> list[Invocation]:
    result: list[Invocation] = []
    normalized_tracking = task_tracking.replace("\\", "/") if task_tracking else None
    for item in invocations:
        timestamp = parse_dt(item.timestamp) or datetime.min.replace(tzinfo=timezone.utc)
        if since and timestamp < since:
            continue
        if until and timestamp > until:
            continue
        if normalized_tracking:
            item_tracking = item.task_tracking.replace("\\", "/")
            if normalized_tracking not in item_tracking:
                continue
        if trace_id and item.trace_id != trace_id:
            continue
        result.append(item)
    return result


def is_success(item: Invocation) -> bool:
    return item.status.lower() in SUCCESS_STATUSES or item.exit_code == 0


def is_failure(item: Invocation) -> bool:
    return item.status.lower() in FAILURE_STATUSES or (
        item.exit_code is not None and item.exit_code != 0
    )


def records_report(item: Invocation) -> bool:
    command = item.command.lower()
    haystack = f"{item.name} {command}"
    return bool("ai_rules.py" in haystack and "tool-invocations" in haystack and re.search(
        r"(^|\s)report(\s|$)",
        command,
    ))


def issue(level: str, message: str, invocation_id: str = "") -> FlowIssue:
    return FlowIssue(level=level, message=message, invocation_id=invocation_id)


def analyze_flow(
    invocations: list[Invocation],
    require_final_gate: bool,
    require_task_session_order: bool,
    require_report: bool,
    require_trace: bool,
) -> list[FlowIssue]:
    issues: list[FlowIssue] = []
    if not invocations:
        issues.append(issue("error", "No invocations matched the selected filters."))
        return issues

    final_gate_count = sum(1 for item in invocations if item.final_gate)
    if final_gate_count == 0:
        level = "error" if require_final_gate else "warning"
        issues.append(issue(level, "No final-gate invocation found."))

    report_indexes = [index for index, item in enumerate(invocations) if records_report(item)]
    report_count = len(report_indexes)
    if report_count == 0:
        level = "error" if require_report else "warning"
        issues.append(issue(level, "No recorded ai_rules.py tool-invocations report invocation found."))

    trace_missing = [item for item in invocations if not item.trace_id]
    if require_trace and trace_missing:
        issues.append(
            issue(
                "error",
                f"{len(trace_missing)} invocation(s) lack trace_id; gate-pool runs must record one trace.",
                trace_missing[0].invocation_id,
            )
        )

    last_success_by_name: dict[str, int] = {}
    for index, item in enumerate(invocations):
        if is_success(item):
            last_success_by_name[item.name] = index
    for index, item in enumerate(invocations):
        if not is_failure(item):
            continue
        later_success = any(
            later.name == item.name and is_success(later)
            for later in invocations[index + 1 :]
        )
        if not later_success:
            issues.append(
                issue(
                    "warning",
                    f"Failed invocation has no later successful retry for {item.name}.",
                    item.invocation_id,
                )
            )

    successful_task_gate_indexes = [
        index
        for index, item in enumerate(invocations)
        if item.name == "ai_rules.py task-gate" and is_success(item)
    ]
    successful_session_gate_indexes = [
        index
        for index, item in enumerate(invocations)
        if item.name == "ai_rules.py session-gate" and is_success(item)
    ]
    if successful_session_gate_indexes:
        first_session = successful_session_gate_indexes[0]
        has_prior_task_gate = any(index < first_session for index in successful_task_gate_indexes)
        if not has_prior_task_gate:
            level = "error" if require_task_session_order else "warning"
            issues.append(
                issue(
                    level,
                    "Successful session gate appears without a prior successful task gate.",
                    invocations[first_session].invocation_id,
                )
            )

    final_gate_indexes = [
        index
        for index, item in enumerate(invocations)
        if item.final_gate and is_success(item)
    ]
    if final_gate_indexes and report_indexes and max(report_indexes) < max(final_gate_indexes):
        issues.append(
            issue(
                "warning",
                "Latest invocation report appears before the latest successful final gate.",
                invocations[max(report_indexes)].invocation_id,
            )
        )

    return issues


def flow_mode(invocations: list[Invocation]) -> str:
    has_parent = any(item.parent_invocation_id for item in invocations)
    has_task_parent = any(item.parent_task_node_id for item in invocations)
    if has_parent or has_task_parent:
        return "tree"
    return "sequence"


def build_edges(invocations: list[Invocation], mode: str) -> list[tuple[int, int, str]]:
    id_to_index = {item.invocation_id: index for index, item in enumerate(invocations)}
    task_node_to_index = {
        item.task_node_id: index
        for index, item in enumerate(invocations)
        if item.task_node_id
    }
    edges: list[tuple[int, int, str]] = []
    if mode == "tree":
        for index, item in enumerate(invocations):
            parent_index = None
            if item.parent_invocation_id:
                parent_index = id_to_index.get(item.parent_invocation_id)
            if parent_index is None and item.parent_task_node_id:
                parent_index = task_node_to_index.get(item.parent_task_node_id)
            if parent_index is not None:
                edges.append((parent_index, index, "parent"))
        if edges:
            return edges

    for index in range(len(invocations) - 1):
        edges.append((index, index + 1, "next"))
    return edges


def mermaid_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', "'").replace("\n", " ")


def node_label(item: Invocation, index: int) -> str:
    timestamp = item.timestamp.split("+", 1)[0].replace("T", " ")
    flags: list[str] = []
    if item.final_gate:
        flags.append("final")
    if is_failure(item):
        flags.append("failed")
    elif is_success(item):
        flags.append("ok")
    if item.phase:
        flags.append(item.phase)
    flag_text = f" [{' / '.join(flags)}]" if flags else ""
    return f"{index + 1}. {item.name}{flag_text}\\n{timestamp}"


def render_mermaid(invocations: list[Invocation], mode: str, top: int) -> str:
    selected = invocations[-top:] if len(invocations) > top else invocations
    edges = build_edges(selected, mode)
    lines = ["flowchart TD"]
    for index, item in enumerate(selected):
        shape_open, shape_close = ("[[", "]]") if item.final_gate else ("[", "]")
        lines.append(f'  n{index}{shape_open}"{mermaid_escape(node_label(item, index))}"{shape_close}')
    for source, target, label in edges:
        connector = "-->" if label == "next" else f"-- {label} -->"
        lines.append(f"  n{source} {connector} n{target}")
    for index, item in enumerate(selected):
        if is_failure(item):
            lines.append(f"  class n{index} failed")
        elif item.final_gate:
            lines.append(f"  class n{index} gate")
    lines.extend(
        [
            "  classDef failed fill:#ffe0e0,stroke:#b42318,color:#111",
            "  classDef gate fill:#e0f2fe,stroke:#0369a1,color:#111",
        ]
    )
    return "\n".join(lines)


def build_summary(invocations: list[Invocation], issues: list[FlowIssue], mode: str) -> dict[str, Any]:
    by_name = Counter(item.name for item in invocations)
    failures = Counter(item.name for item in invocations if is_failure(item))
    return {
        "flow_mode": mode,
        "invocation_count": len(invocations),
        "final_gate_count": sum(1 for item in invocations if item.final_gate),
        "failure_count": sum(1 for item in invocations if is_failure(item)),
        "report_count": sum(1 for item in invocations if records_report(item)),
        "has_trace_fields": any(item.trace_id or item.parent_invocation_id for item in invocations),
        "has_task_tree_fields": any(item.task_node_id or item.parent_task_node_id for item in invocations),
        "top_tools": [
            {"name": name, "count": count, "failures": failures.get(name, 0)}
            for name, count in by_name.most_common(10)
        ],
        "issue_counts": dict(Counter(item.level for item in issues)),
    }


def build_report(args: argparse.Namespace) -> FlowReport:
    root = Path(args.root).resolve()
    events, ledger_files = read_events(root, args.ledger_dir)
    invocations = collapse_invocations(events)
    invocations = filter_invocations(
        invocations,
        args.task_tracking,
        args.trace_id,
        parse_dt(args.since),
        parse_dt(args.until),
    )
    mode = flow_mode(invocations)
    issues = analyze_flow(
        invocations,
        args.require_final_gate,
        args.require_task_session_order,
        args.require_report,
        args.require_trace,
    )
    return FlowReport(
        root=root.as_posix(),
        ledger_files=ledger_files,
        invocations=invocations,
        issues=issues,
        summary=build_summary(invocations, issues, mode),
        flow_mode=mode,
    )


def render_issue_lines(issues: list[FlowIssue]) -> list[str]:
    lines = ["Issues:"]
    if not issues:
        lines.append("  none")
        return lines
    for item in issues:
        suffix = f" ({item.invocation_id})" if item.invocation_id else ""
        lines.append(f"  - {item.level}: {item.message}{suffix}")
    return lines


def render_text(report: FlowReport, top: int) -> str:
    lines = [
        "AI Rules Tool Flow Report",
        f"Root: {report.root}",
        f"Flow mode: {report.flow_mode}",
        f"Invocations: {len(report.invocations)}",
        f"Final gates: {report.summary['final_gate_count']}",
        f"Failures: {report.summary['failure_count']}",
        "",
        "Top tools:",
    ]
    for row in report.summary["top_tools"]:
        lines.append(f"  {row['name']}: count={row['count']} failures={row['failures']}")
    lines.append("")
    lines.extend(render_issue_lines(report.issues))
    lines.extend(["", "Flow:"])
    for index, item in enumerate(report.invocations[-top:]):
        lines.append(
            f"  {index + 1}. {item.timestamp} {item.name} "
            f"status={item.status} exit={item.exit_code} phase={item.phase or 'none'}"
        )
    return "\n".join(lines)


def render_markdown(report: FlowReport, top: int) -> str:
    mermaid = render_mermaid(report.invocations, report.flow_mode, top)
    lines = [
        "# AI Rules Tool Flow Report",
        "",
        f"- Root: `{report.root}`",
        f"- Flow mode: `{report.flow_mode}`",
        f"- Invocations: {len(report.invocations)}",
        f"- Final gates: {report.summary['final_gate_count']}",
        f"- Failures: {report.summary['failure_count']}",
        f"- Has trace fields: {str(report.summary['has_trace_fields']).lower()}",
        f"- Has task tree fields: {str(report.summary['has_task_tree_fields']).lower()}",
        "",
        "## Flow",
        "",
        "```mermaid",
        mermaid,
        "```",
        "",
        "## Issues",
        "",
    ]
    if not report.issues:
        lines.append("- None")
    else:
        for item in report.issues:
            suffix = f" `{item.invocation_id}`" if item.invocation_id else ""
            lines.append(f"- `{item.level}`: {item.message}{suffix}")
    lines.extend(
        [
            "",
            "## Invocations",
            "",
            "| # | Time | Tool | Status | Exit | Phase | Final |",
            "|---:|---|---|---|---:|---|---|",
        ]
    )
    selected = report.invocations[-top:] if len(report.invocations) > top else report.invocations
    for index, item in enumerate(selected, 1):
        exit_code = "" if item.exit_code is None else str(item.exit_code)
        lines.append(
            f"| {index} | {item.timestamp} | `{item.name}` | {item.status} | "
            f"{exit_code} | {item.phase or ''} | {str(item.final_gate).lower()} |"
        )
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    report = build_report(args)
    if args.format == "json":
        print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
    elif args.format == "mermaid":
        print(render_mermaid(report.invocations, report.flow_mode, args.top))
    elif args.format == "text":
        print(render_text(report, args.top))
    else:
        print(render_markdown(report, args.top))

    has_error = any(item.level == "error" for item in report.issues)
    has_warning = any(item.level == "warning" for item in report.issues)
    if has_error:
        return 1
    if args.fail_on_warning and has_warning:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

