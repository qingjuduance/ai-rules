#!/usr/bin/env python3
"""Print the current AI agent-group status board.

The script reads a structured status file maintained by the main thread.
It does not call tool-specific sub-agent APIs directly; the main thread must update
the status file when agents are spawned, paused, closed, or reused.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ai_rules.common.paths import AGENT_GROUP_STATUS

DEFAULT_STATUS_FILE = AGENT_GROUP_STATUS


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Show the current AI agent-group status board."
    )
    parser.add_argument(
        "--status-file",
        default=str(DEFAULT_STATUS_FILE),
        help="Path to the agent group status JSON file.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Print one status snapshot and exit.",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Keep printing snapshots at the selected interval.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="Watch interval in seconds. Default: 5.",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=0,
        help="Maximum watch iterations. 0 means run until interrupted.",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format. Default: text.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Include child node and file details in text output.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Include completed historical groups. Default shows active/open groups only.",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Warn when stored summary fields drift from computed status.",
    )
    parser.add_argument(
        "--stale-after",
        type=int,
        default=300,
        help="Seconds after updated_at before the snapshot is marked stale.",
    )
    return parser.parse_args()


def load_status(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"status file not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def parse_time(value: str) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def status_kind(value: str) -> str:
    text = (value or "").lower()
    if any(token in text for token in ("运行", "running", "in_progress")):
        return "running"
    if any(token in text for token in ("待启动", "pending", "todo")):
        return "pending"
    if any(token in text for token in ("完成", "closed", "done", "completed")):
        return "completed"
    if any(token in text for token in ("阻塞", "blocked")):
        return "blocked"
    if any(token in text for token in ("暂停", "paused")):
        return "paused"
    if any(token in text for token in ("验证", "verify")):
        return "verify"
    return "other"


def compute_summary(status: dict[str, Any], groups: list[Any] | None = None) -> dict[str, Any]:
    if groups is None:
        groups = status.get("groups", [])
    counts = {
        "total_groups": len(groups),
        "running_groups": 0,
        "pending_groups": 0,
        "completed_groups": 0,
        "blocked_groups": 0,
        "active_agents": 0,
        "residual_agents": 0,
    }
    for group in groups:
        kind = status_kind(str(group.get("status", "")))
        if kind == "running":
            counts["running_groups"] += 1
        elif kind == "pending":
            counts["pending_groups"] += 1
        elif kind == "completed":
            counts["completed_groups"] += 1
        elif kind == "blocked":
            counts["blocked_groups"] += 1
        counts["active_agents"] += len(group.get("active_agents", []) or [])
        counts["residual_agents"] += len(group.get("residual_agents", []) or [])
    return counts


def validation_warnings(status: dict[str, Any]) -> list[str]:
    stored = status.get("summary", {})
    computed = compute_summary(status)
    warnings: list[str] = []
    if not isinstance(stored, dict):
        return ["summary 不是对象，无法校验"]
    for key, stored_value in stored.items():
        if key in computed and stored_value != computed[key]:
            warnings.append(
                f"summary.{key}={stored_value}，实时计算为 {computed[key]}"
            )
    return warnings


def is_open_group(group: dict[str, Any]) -> bool:
    kind = status_kind(str(group.get("status", "")))
    if kind != "completed":
        return True
    if group.get("active_agents"):
        return True
    if group.get("unfinished_items"):
        return True
    return False


def visible_groups(status: dict[str, Any], show_all: bool) -> list[Any]:
    groups = status.get("groups", [])
    if show_all:
        return groups
    return [group for group in groups if isinstance(group, dict) and is_open_group(group)]


def stale_message(status: dict[str, Any], stale_after: int) -> str:
    updated_at = parse_time(str(status.get("updated_at", "")))
    if updated_at is None:
        return "updated_at 无法解析，状态可能过期"
    now = datetime.now(updated_at.tzinfo)
    age_seconds = int((now - updated_at).total_seconds())
    if age_seconds > stale_after:
        return f"状态可能过期：{age_seconds}s 未更新"
    return f"状态新鲜：{age_seconds}s 前更新"


def compact_list(values: list[Any], limit: int = 2) -> str:
    if not values:
        return "无"
    rendered: list[str] = []
    for value in values[:limit]:
        if isinstance(value, dict):
            label = value.get("nickname") or value.get("id") or value.get("role")
            rendered.append(str(label or value))
        else:
            rendered.append(str(value))
    if len(values) > limit:
        rendered.append(f"...+{len(values) - limit}")
    return "、".join(rendered)


def render_text(status: dict[str, Any], stale_after: int, verbose: bool, show_all: bool) -> str:
    groups = visible_groups(status, show_all)
    summary = compute_summary(status, groups)
    updated_at = status.get("updated_at", "unknown")
    view_name = "全部智能体组" if show_all else "活跃/未完成智能体组"
    lines = [
        "智能体组状态看板",
        f"视图: {view_name}",
        f"更新时间: {updated_at} ({stale_message(status, stale_after)})",
        (
            "总组数: {total_groups} | 运行中: {running_groups} | "
            "待启动: {pending_groups} | 已完成: {completed_groups} | "
            "活跃子AI: {active_agents} | 残留: {residual_agents}"
        ).format(**summary),
        "",
    ]

    if not groups and not show_all:
        lines.append("当前没有活跃或未完成的智能体组；历史完成组可用 --all 查看。")
        return "\n".join(lines).rstrip()

    for group in groups:
        lines.append(
            "- {group_id} {group_title}: {status} | 父节点: {parent} | "
            "叶子数: {leaf_count} | 活跃: {active} | 残留: {residual}".format(
                group_id=group.get("group_id", "unknown"),
                group_title=group.get("group_title", ""),
                status=group.get("status", "unknown"),
                parent=group.get("parent_node", "unknown"),
                leaf_count=group.get("leaf_count", 0),
                active=compact_list(group.get("active_agents", []) or []),
                residual=compact_list(group.get("residual_agents", []) or []),
            )
        )
        unfinished = group.get("unfinished_items", []) or []
        if unfinished:
            lines.append(f"  未完成: {compact_list(unfinished, limit=3)}")
        next_action = group.get("next_action")
        if next_action:
            lines.append(f"  下一步: {next_action}")
        if verbose:
            children = group.get("children", []) or []
            for child in children:
                agent = child.get("agent", {}) if isinstance(child, dict) else {}
                nickname = agent.get("nickname") or agent.get("id") or "无"
                lines.append(
                    "  * {node}: {status} | {type} | agent={agent} | {conclusion}".format(
                        node=child.get("node_id", "unknown"),
                        status=child.get("status", "unknown"),
                        type=child.get("type", "node"),
                        agent=nickname,
                        conclusion=child.get("conclusion", ""),
                    )
                )
        lines.append("")
    return "\n".join(lines).rstrip()


def emit(status: dict[str, Any], args: argparse.Namespace) -> None:
    groups = visible_groups(status, args.all)
    if args.format == "json":
        enriched = dict(status)
        if not args.all:
            enriched["groups"] = groups
        enriched["computed_summary"] = compute_summary(status, groups)
        enriched["all_groups_summary"] = compute_summary(status)
        enriched["view"] = "all" if args.all else "active"
        enriched["stale_status"] = stale_message(status, args.stale_after)
        if args.validate:
            enriched["validation_warnings"] = validation_warnings(status)
        print(json.dumps(enriched, ensure_ascii=False, indent=2))
        return
    print(render_text(status, args.stale_after, args.verbose, args.all))
    if args.validate:
        warnings = validation_warnings(status)
        if warnings:
            print("\n校验警告:")
            for warning in warnings:
                print(f"- {warning}")
        else:
            print("\n校验: summary 与实时计算一致")


def main() -> int:
    args = parse_args()
    status_path = Path(args.status_file)
    if not args.once and not args.watch:
        args.once = True

    iteration = 0
    while True:
        try:
            status = load_status(status_path)
            emit(status, args)
        except Exception as exc:  # noqa: BLE001 - command-line report tool.
            print(f"agent group status error: {exc}", file=sys.stderr)
            return 2

        iteration += 1
        if args.once or not args.watch:
            return 0
        if args.max_iterations and iteration >= args.max_iterations:
            return 0
        print("-" * 60)
        time.sleep(max(args.interval, 0.1))


if __name__ == "__main__":
    raise SystemExit(main())

