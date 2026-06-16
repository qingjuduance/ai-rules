#!/usr/bin/env python3
"""Read-only gate for document rebuild and new-document tasks.

The script checks changed or explicit Markdown files for common maintenance
misses: UTF-8 readability, absolute local paths, forbidden question links from
formal entries, missing sibling .references records, README review candidates,
and required task-tracking sections.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from ai_client_governance.common.paths import is_correction_path, is_pending_path, is_task_tracking_path


MANDATORY_TRACKING_HEADINGS = [
    "已处理文件",
    "验证记录",
    "循环引用检查",
    "恢复现场",
    "最终结论",
]

ABSOLUTE_PATH_RE = re.compile(
    r"(?i)(?:\b[A-Z]:[\\/][^\s)`]+|file://[^\s)`]+|vscode://[^\s)`]+)"
)
MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)]+)\)")
LONG_LINE_LIMIT = 120
EXTERNAL_EVIDENCE_MARKERS = [
    "外部运行",
    "真实路径",
    "真实全路径",
    "日志路径",
    "日志来源",
    "游戏目录",
    "游戏运行",
    "构建输出",
    "导出产物",
    "临时验证根",
    "ue4ss",
    "blackmythwukong",
    "steamapps",
    "mods",
]


@dataclass
class Finding:
    level: str
    file: str
    message: str
    line: int | None = None


@dataclass
class Report:
    root: str
    checked_files: list[str]
    task_tracking: str | None
    errors: list[Finding]
    warnings: list[Finding]
    notes: list[Finding]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate document task maintenance gates."
    )
    parser.add_argument(
        "--root",
        default=".",
        help="Repository root. Defaults to current directory.",
    )
    parser.add_argument(
        "--paths",
        nargs="*",
        default=None,
        help="Markdown files or directories to check. Defaults to git-changed Markdown files.",
    )
    parser.add_argument(
        "--task-tracking",
        help="Task tracking file that records this task.",
    )
    parser.add_argument(
        "--mode",
        choices=("generic", "new-doc", "refactor", "sync", "rules", "resume"),
        default="generic",
        help="Task mode used for mode-specific gate warnings.",
    )
    parser.add_argument(
        "--require-task-tracking",
        action="store_true",
        help="Fail when no task tracking file is provided.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero when errors are found.",
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
    return parser.parse_args()


def rel_path(path: Path, root: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(root.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def resolve_from_root(root: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return root / path


def run_git_changed(root: Path) -> list[Path]:
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                "core.quotepath=false",
                "status",
                "--porcelain",
                "--untracked-files=all",
            ],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.CalledProcessError):
        return []

    paths: list[Path] = []
    for raw_line in result.stdout.splitlines():
        if not raw_line:
            continue
        path_text = raw_line[3:].strip()
        if " -> " in path_text:
            path_text = path_text.split(" -> ", 1)[1].strip()
        path_text = path_text.strip('"')
        if path_text.lower().endswith(".md"):
            paths.append(root / path_text)
    return paths


def iter_markdown_from_paths(paths: Iterable[Path]) -> list[Path]:
    collected: list[Path] = []
    for path in paths:
        if path.is_dir():
            collected.extend(sorted(path.rglob("*.md")))
        elif path.suffix.lower() == ".md":
            collected.append(path)
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in collected:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(path)
    return unique


def is_local_link(target: str) -> bool:
    lowered = target.lower()
    return not (
        lowered.startswith("http://")
        or lowered.startswith("https://")
        or lowered.startswith("mailto:")
        or lowered.startswith("#")
    )


def is_reference_file(rel: str) -> bool:
    return "/.references/" in f"/{rel}"


def is_questions_file(rel: str) -> bool:
    return "/questions/" in f"/{rel}"


def is_tracking_or_correction(rel: str, explicit_tracking_rel: str | None = None) -> bool:
    return rel == explicit_tracking_rel or is_task_tracking_path(rel) or is_correction_path(rel)


def can_record_external_evidence_path(rel: str, explicit_tracking_rel: str | None = None) -> bool:
    return (
        rel == explicit_tracking_rel
        or is_task_tracking_path(rel)
        or is_pending_path(rel)
        or is_correction_path(rel)
    )


def is_external_evidence_path_line(
    rel: str,
    lines: list[str],
    index: int,
    explicit_tracking_rel: str | None = None,
) -> bool:
    if not can_record_external_evidence_path(rel, explicit_tracking_rel):
        return False

    start = max(0, index - 3)
    end = min(len(lines), index + 4)
    context = "\n".join(lines[start:end]).lower()
    return any(marker in context for marker in EXTERNAL_EVIDENCE_MARKERS)


def is_formal_entry(rel: str) -> bool:
    name = Path(rel).name.lower()
    if is_reference_file(rel) or is_questions_file(rel) or is_tracking_or_correction(rel):
        return False
    if name in {"readme.md", "roadmap.md"}:
        return True
    return rel.startswith("docs/")


def expected_reference_file(path: Path) -> Path:
    return path.parent / ".references" / path.name


def add_finding(
    errors: list[Finding],
    warnings: list[Finding],
    notes: list[Finding],
    level: str,
    file: str,
    message: str,
    line: int | None = None,
) -> None:
    finding = Finding(level=level, file=file, message=message, line=line)
    if level == "error":
        errors.append(finding)
    elif level == "warning":
        warnings.append(finding)
    else:
        notes.append(finding)


def check_markdown_file(
    path: Path,
    root: Path,
    selected: set[str],
    errors: list[Finding],
    warnings: list[Finding],
    notes: list[Finding],
    explicit_tracking_rel: str | None = None,
) -> None:
    rel = rel_path(path, root)
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        add_finding(errors, warnings, notes, "error", rel, f"not UTF-8: {exc}")
        return
    except FileNotFoundError:
        add_finding(errors, warnings, notes, "error", rel, "file does not exist")
        return

    local_links = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        line_no = index + 1
        external_evidence_path = is_external_evidence_path_line(rel, lines, index, explicit_tracking_rel)
        if ABSOLUTE_PATH_RE.search(line):
            if external_evidence_path:
                add_finding(
                    errors,
                    warnings,
                    notes,
                    "note",
                    rel,
                    "records external runtime evidence path",
                    line_no,
                )
            else:
                level = "warning" if is_tracking_or_correction(rel, explicit_tracking_rel) else "error"
                add_finding(
                    errors,
                    warnings,
                    notes,
                    level,
                    rel,
                    "contains absolute local path; prefer repository-relative paths",
                    line_no,
                )
        if (
            len(line) > LONG_LINE_LIMIT
            and not line.lstrip().startswith("|")
            and not external_evidence_path
        ):
            if "http://" not in line and "https://" not in line:
                add_finding(
                    errors,
                    warnings,
                    notes,
                    "warning",
                    rel,
                    f"line longer than {LONG_LINE_LIMIT} visible characters",
                    line_no,
                )
        for _label, target in MARKDOWN_LINK_RE.findall(line):
            if not is_local_link(target):
                continue
            local_links.append((line_no, target))
            normalized = target.replace("\\", "/")
            if is_formal_entry(rel) and "questions/" in normalized:
                add_finding(
                    errors,
                    warnings,
                    notes,
                    "error",
                    rel,
                    "formal entry links to questions/; use plain text instead",
                    line_no,
                )

    if local_links and not is_reference_file(rel):
        reference_file = expected_reference_file(path)
        if not reference_file.exists():
            add_finding(
                errors,
                warnings,
                notes,
                "error",
                rel,
                f"local Markdown links found but missing {rel_path(reference_file, root)}",
            )

    if (
        path.name.lower() != "readme.md"
        and not is_reference_file(rel)
        and not rel.startswith(".codex/")
    ):
        readme = path.parent / "README.md"
        if readme.exists() and rel_path(readme, root) not in selected:
            add_finding(
                errors,
                warnings,
                notes,
                "warning",
                rel,
                f"review whether {rel_path(readme, root)} needs an index update",
            )


def check_task_tracking(
    tracking_path: Path | None,
    root: Path,
    require: bool,
    checked_files: list[str],
    mode: str,
    errors: list[Finding],
    warnings: list[Finding],
    notes: list[Finding],
) -> str | None:
    if tracking_path is None:
        if require:
            add_finding(
                errors,
                warnings,
                notes,
                "error",
                "<task-tracking>",
                "task tracking file is required",
            )
        return None

    rel = rel_path(tracking_path, root)
    try:
        text = tracking_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        add_finding(errors, warnings, notes, "error", rel, f"not UTF-8: {exc}")
        return rel
    except FileNotFoundError:
        add_finding(errors, warnings, notes, "error", rel, "file does not exist")
        return rel

    for heading in MANDATORY_TRACKING_HEADINGS:
        if f"## {heading}" not in text and f"### {heading}" not in text:
            add_finding(
                errors,
                warnings,
                notes,
                "error",
                rel,
                f"missing task tracking section: {heading}",
            )
    normalized_text = text.replace("\\", "/")
    for checked in checked_files:
        if checked not in normalized_text:
            add_finding(
                errors,
                warnings,
                notes,
                "warning",
                rel,
                f"checked file is not mentioned in task tracking: {checked}",
            )
    if mode in {"new-doc", "refactor", "sync", "rules"}:
        required_phrases = ["影响面", "Definition of Done"]
        for phrase in required_phrases:
            if phrase not in text:
                add_finding(
                    errors,
                    warnings,
                    notes,
                    "warning",
                    rel,
                    f"mode {mode} should record {phrase}",
                )
    if mode == "refactor":
        for phrase in ["旧路径", "旧标题", "旧锚点"]:
            if phrase not in text:
                add_finding(
                    errors,
                    warnings,
                    notes,
                    "warning",
                    rel,
                    f"refactor mode should record search result for {phrase}",
                )
    if mode == "sync":
        for phrase in ["common", "project", "推送"]:
            if phrase not in normalized_text and phrase not in text:
                add_finding(
                    errors,
                    warnings,
                    notes,
                    "warning",
                    rel,
                    f"sync mode should record boundary/result for {phrase}",
                )
    return rel


def build_report(args: argparse.Namespace) -> Report:
    root = Path(args.root).resolve()
    raw_paths = args.paths
    if raw_paths is None:
        paths = run_git_changed(root)
    else:
        paths = [resolve_from_root(root, item) for item in raw_paths]

    markdown_files = [
        path
        for path in iter_markdown_from_paths(paths)
        if path.exists() and (root in path.resolve().parents or path.resolve() == root)
    ]
    selected = {rel_path(path, root) for path in markdown_files if path.exists()}

    errors: list[Finding] = []
    warnings: list[Finding] = []
    notes: list[Finding] = []

    if not markdown_files:
        add_finding(
            errors,
            warnings,
            notes,
            "note",
            "<paths>",
            "no Markdown files selected",
        )

    tracking_path = (
        resolve_from_root(root, args.task_tracking).resolve()
        if args.task_tracking
        else None
    )
    explicit_tracking_rel = rel_path(tracking_path, root) if tracking_path else None

    for path in markdown_files:
        check_markdown_file(path, root, selected, errors, warnings, notes, explicit_tracking_rel)

    tracking_rel = check_task_tracking(
        tracking_path,
        root,
        args.require_task_tracking,
        sorted(selected),
        args.mode,
        errors,
        warnings,
        notes,
    )

    return Report(
        root=root.as_posix(),
        checked_files=sorted(selected),
        task_tracking=tracking_rel,
        errors=errors,
        warnings=warnings,
        notes=notes,
    )


def render_findings(title: str, findings: list[Finding]) -> list[str]:
    lines = [f"{title}: {len(findings)}"]
    if not findings:
        lines.append("  none")
        return lines
    for item in findings:
        location = item.file if item.line is None else f"{item.file}:{item.line}"
        lines.append(f"  - {location}: {item.message}")
    return lines


def render_text(report: Report) -> str:
    lines = [
        "Document Task Gate Report",
        f"Root: {report.root}",
        f"Task tracking: {report.task_tracking or 'none'}",
        f"Checked Markdown files: {len(report.checked_files)}",
    ]
    for path in report.checked_files:
        lines.append(f"  - {path}")
    lines.append("")
    lines.extend(render_findings("Errors", report.errors))
    lines.append("")
    lines.extend(render_findings("Warnings", report.warnings))
    lines.append("")
    lines.extend(render_findings("Notes", report.notes))
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    report = build_report(args)
    if args.format == "json":
        print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
    else:
        print(render_text(report))

    if args.strict and report.errors:
        return 1
    if args.fail_on_warning and (report.errors or report.warnings):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

