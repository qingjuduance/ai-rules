#!/usr/bin/env python3
"""Audit Markdown rule sections for thin-entry and tooling migration.

The script is read-only. It reports which sections should probably remain as
brief boundary rules, move to a skill, move to a deterministic script, or stay
as project-specific rules. It does not edit rule entries, README files, skills,
tracking files, or Git state.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path


HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")

KEYWORDS: dict[str, tuple[str, ...]] = {
    "keep-boundary": (
        "approval",
        "boundary",
        "conflict",
        "git",
        "higher-priority",
        "safety",
        "不得",
        "禁止",
        "审批",
        "批准",
        "边界",
        "冲突",
        "读取顺序",
        "管理边界",
        "安全",
    ),
    "skill-candidate": (
        "agent",
        "brief",
        "decision",
        "judge",
        "plan",
        "skill",
        "workflow",
        "判断",
        "计划",
        "评估",
        "取舍",
        "协作",
        "拆分",
        "回答",
        "提炼",
        "沉淀",
    ),
    "script-candidate": (
        "check",
        "command",
        "gate",
        "json",
        "report",
        "script",
        "validate",
        "命令",
        "报告",
        "检查",
        "统计",
        "脚本",
        "账本",
        "验证",
        "门禁",
    ),
    "program-candidate": (
        "background",
        "daemon",
        "monitor",
        "queue",
        "service",
        "watch",
        "令牌桶",
        "后台",
        "持续",
        "监控",
        "队列",
    ),
    "project-rule": (
        "java",
        "qt",
        "resume",
        "简历",
        "学习",
        "路线",
        "项目特有",
        "源码快照",
        "文档架构",
    ),
}

PRIORITY = (
    "project-rule",
    "program-candidate",
    "script-candidate",
    "skill-candidate",
    "keep-boundary",
)

ACTION_TEXT = {
    "keep-boundary": "Keep as a short rule-entry/README boundary rule.",
    "skill-candidate": "Move procedural judgement to a skill or skill reference.",
    "script-candidate": "Move deterministic checks to a script, gate, or report.",
    "program-candidate": "Consider a queue/daemon only after continuous monitoring is proven necessary.",
    "project-rule": "Keep in project rules or a project-specific skill, not common rules.",
    "review": "Review manually before moving or deleting.",
}


@dataclass(frozen=True)
class Section:
    file: str
    heading: str
    level: int
    start_line: int
    end_line: int
    line_count: int
    char_count: int
    recommendation: str
    scores: dict[str, int]
    reasons: list[str]
    suggested_action: str


@dataclass(frozen=True)
class AuditReport:
    root: str
    files: list[str]
    section_count: int
    summary: dict[str, int]
    sections: list[Section]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit rule docs for thin-entry and tooling migration candidates."
    )
    parser.add_argument("--root", default=".", help="Repository root for relative paths.")
    parser.add_argument(
        "--paths",
        nargs="+",
        default=("AGENTS.md", "CLAUDE.md", "GEMINI.md", "CONVENTIONS.md", "README.md"),
        help="Markdown files or directories to audit.",
    )
    parser.add_argument(
        "--min-lines",
        type=int,
        default=1,
        help="Only show sections with at least this many lines. Default: 1.",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=80,
        help="Maximum sections to print in text/markdown output. Default: 80.",
    )
    parser.add_argument(
        "--format",
        choices=("text", "markdown", "json"),
        default="text",
        help="Output format. Default: text.",
    )
    return parser.parse_args()


def resolve_path(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def display_path(path: Path, root: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(root.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def iter_markdown_files(root: Path, raw_paths: list[str]) -> list[Path]:
    files: list[Path] = []
    for raw_path in raw_paths:
        path = resolve_path(root, raw_path)
        if path.is_file() and path.suffix.lower() == ".md":
            files.append(path)
        elif path.is_dir():
            files.extend(sorted(child for child in path.rglob("*.md") if child.is_file()))

    seen: set[Path] = set()
    result: list[Path] = []
    for path in files:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            result.append(path)
    return result


def section_ranges(lines: list[str]) -> list[tuple[int, int, int, str]]:
    headings: list[tuple[int, int, str]] = []
    for index, line in enumerate(lines):
        match = HEADING_RE.match(line)
        if match:
            headings.append((index, len(match.group(1)), match.group(2).strip()))

    if not headings:
        return [(0, max(len(lines) - 1, 0), 0, "(whole file)")]

    ranges: list[tuple[int, int, int, str]] = []
    for item_index, (start, level, heading) in enumerate(headings):
        end = headings[item_index + 1][0] - 1 if item_index + 1 < len(headings) else len(lines) - 1
        ranges.append((start, end, level, heading))
    return ranges


def count_keywords(text: str, words: tuple[str, ...]) -> int:
    lowered = text.lower()
    return sum(lowered.count(word.lower()) for word in words)


def score_section(file_display: str, heading: str, body: str) -> tuple[str, dict[str, int], list[str]]:
    text = f"{file_display}\n{heading}\n{body}"
    scores = {label: count_keywords(text, words) for label, words in KEYWORDS.items()}
    normalized_file = file_display.replace("\\", "/")
    project_path = ".codex/project/rules/project/" in normalized_file
    if project_path:
        scores["project-rule"] += 3
    if "README.md" in file_display and heading.lower() in {"usage", "使用方式", "托管范围"}:
        scores["keep-boundary"] += 1

    recommendation = "review"
    if project_path:
        recommendation = "project-rule"
    elif scores["program-candidate"] >= 2:
        recommendation = "program-candidate"
    elif scores["script-candidate"] >= 2 and scores["script-candidate"] >= scores["skill-candidate"]:
        recommendation = "script-candidate"
    elif scores["skill-candidate"] >= 2:
        recommendation = "skill-candidate"
    elif scores["keep-boundary"] >= 1:
        recommendation = "keep-boundary"

    reasons = [
        f"{label}={score}"
        for label, score in sorted(scores.items())
        if score > 0
    ]
    if not reasons:
        reasons = ["no keyword signal"]
    return recommendation, scores, reasons


def parse_sections(path: Path, root: Path) -> list[Section]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    file_display = display_path(path, root)
    sections: list[Section] = []
    for start, end, level, heading in section_ranges(lines):
        body_lines = lines[start : end + 1]
        body = "\n".join(body_lines)
        recommendation, scores, reasons = score_section(file_display, heading, body)
        sections.append(
            Section(
                file=file_display,
                heading=heading,
                level=level,
                start_line=start + 1,
                end_line=end + 1,
                line_count=max(end - start + 1, 0),
                char_count=len(body),
                recommendation=recommendation,
                scores=scores,
                reasons=reasons,
                suggested_action=ACTION_TEXT[recommendation],
            )
        )
    return sections


def build_report(args: argparse.Namespace) -> AuditReport:
    root = Path(args.root).resolve()
    files = iter_markdown_files(root, list(args.paths))
    sections: list[Section] = []
    for path in files:
        sections.extend(parse_sections(path, root))

    sections = [section for section in sections if section.line_count >= args.min_lines]
    sections.sort(key=lambda item: (-item.line_count, item.file, item.start_line))
    summary = dict(Counter(section.recommendation for section in sections))
    return AuditReport(
        root=root.as_posix(),
        files=[display_path(path, root) for path in files],
        section_count=len(sections),
        summary=summary,
        sections=sections,
    )


def render_text(report: AuditReport, top: int) -> str:
    lines = [
        "Rule Tooling Audit",
        f"Root: {report.root}",
        f"Files: {len(report.files)}",
        f"Sections: {report.section_count}",
        "",
        "Summary:",
    ]
    for label, count in sorted(report.summary.items()):
        lines.append(f"  {label}: {count}")
    lines.extend(["", "Sections:"])
    for section in report.sections[:top]:
        heading_marks = "#" * section.level if section.level else ""
        lines.append(
            f"- {section.file}:{section.start_line}-{section.end_line} "
            f"{heading_marks} {section.heading} "
            f"lines={section.line_count} recommendation={section.recommendation}"
        )
        lines.append(f"  action={section.suggested_action}")
        lines.append(f"  reasons={', '.join(section.reasons)}")
    return "\n".join(lines)


def render_markdown(report: AuditReport, top: int) -> str:
    lines = [
        "# Rule Tooling Audit",
        "",
        f"- Root: `{report.root}`",
        f"- Files: {len(report.files)}",
        f"- Sections: {report.section_count}",
        "",
        "## Summary",
        "",
        "| Recommendation | Count |",
        "|---|---:|",
    ]
    for label, count in sorted(report.summary.items()):
        lines.append(f"| `{label}` | {count} |")

    lines.extend(
        [
            "",
            "## Migration Matrix",
            "",
            "| File | Lines | Heading | Recommendation | Suggested action | Reasons |",
            "|---|---:|---|---|---|---|",
        ]
    )
    for section in report.sections[:top]:
        heading = section.heading.replace("|", "\\|")
        action = section.suggested_action.replace("|", "\\|")
        reasons = ", ".join(section.reasons).replace("|", "\\|")
        lines.append(
            f"| `{section.file}` | {section.start_line}-{section.end_line} "
            f"({section.line_count}) | {heading} | `{section.recommendation}` | "
            f"{action} | {reasons} |"
        )
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    report = build_report(args)
    if args.format == "json":
        print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
    elif args.format == "markdown":
        print(render_markdown(report, args.top))
    else:
        print(render_text(report, args.top))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

