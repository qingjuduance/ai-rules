#!/usr/bin/env python3
"""Safely extract bounded context from long repository text files.

The script is read-only. It prints file metadata, Markdown heading indexes,
keyword snippets, and explicit line ranges without dumping entire long files to
the terminal.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


@dataclass(frozen=True)
class FileSummary:
    path: str
    line_count: int
    byte_count: int
    sha256: str


@dataclass(frozen=True)
class ExtractLine:
    line: int
    text: str


@dataclass(frozen=True)
class ExtractItem:
    kind: str
    path: str
    label: str
    start_line: int
    end_line: int
    lines: list[ExtractLine]


@dataclass(frozen=True)
class ExtractReport:
    root: str
    max_lines: int
    emitted_line_count: int
    truncated: bool
    files: list[FileSummary]
    items: list[ExtractItem]
    omitted_item_count: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read long text files through bounded summaries, headings, matches, and ranges.",
    )
    parser.add_argument("paths", nargs="+", help="Files to inspect.")
    parser.add_argument("--root", default=".", help="Repository root for relative paths. Default: current directory.")
    parser.add_argument("--encoding", default="utf-8", help="Text encoding. Default: utf-8.")
    parser.add_argument("--errors", default="replace", help="Decode error handling. Default: replace.")
    parser.add_argument("--headings", action="store_true", help="Emit Markdown heading index.")
    parser.add_argument(
        "--match",
        action="append",
        default=[],
        help="Literal or regex pattern to find. Can be repeated.",
    )
    parser.add_argument("--regex", action="store_true", help="Treat --match values as regular expressions.")
    parser.add_argument("--case-sensitive", action="store_true", help="Use case-sensitive matching.")
    parser.add_argument(
        "--range",
        dest="ranges",
        action="append",
        default=[],
        help="Emit 1-based inclusive line range, e.g. 10:30 or 10-30. Can be repeated.",
    )
    parser.add_argument("--context", type=int, default=2, help="Context lines around matches. Default: 2.")
    parser.add_argument("--max-lines", type=int, default=120, help="Maximum extracted text lines to emit. Default: 120.")
    parser.add_argument(
        "--format",
        choices=("text", "markdown", "json"),
        default="text",
        help="Output format. Default: text.",
    )
    return parser.parse_args()


def resolve_path(root: Path, value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def display_path(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def read_file(path: Path, encoding: str, errors: str) -> tuple[bytes, list[str]]:
    data = path.read_bytes()
    text = data.decode(encoding, errors=errors)
    return data, text.splitlines()


def parse_range(value: str, line_count: int) -> tuple[int, int]:
    match = re.fullmatch(r"\s*(\d+)\s*(?::|-)\s*(\d+)\s*", value)
    if not match:
        raise ValueError(f"Invalid range '{value}'. Use START:END.")
    start = int(match.group(1))
    end = int(match.group(2))
    if start < 1 or end < 1 or start > end:
        raise ValueError(f"Invalid range '{value}'. Expected 1 <= START <= END.")
    return max(1, start), min(line_count, end)


def make_lines(lines: list[str], start: int, end: int) -> list[ExtractLine]:
    return [ExtractLine(line=index, text=lines[index - 1]) for index in range(start, end + 1)]


def heading_items(root: Path, path: Path, lines: list[str]) -> list[ExtractItem]:
    file_display = display_path(root, path)
    result: list[ExtractItem] = []
    for index, text in enumerate(lines, start=1):
        match = HEADING_RE.match(text)
        if not match:
            continue
        level = len(match.group(1))
        label = f"h{level} {match.group(2).strip()}"
        result.append(
            ExtractItem(
                kind="heading",
                path=file_display,
                label=label,
                start_line=index,
                end_line=index,
                lines=[ExtractLine(line=index, text=text)],
            )
        )
    return result


def compile_patterns(values: Iterable[str], regex: bool, case_sensitive: bool) -> list[re.Pattern[str]]:
    flags = 0 if case_sensitive else re.IGNORECASE
    patterns: list[re.Pattern[str]] = []
    for value in values:
        pattern = value if regex else re.escape(value)
        patterns.append(re.compile(pattern, flags))
    return patterns


def match_items(
    root: Path,
    path: Path,
    lines: list[str],
    patterns: list[re.Pattern[str]],
    context: int,
) -> list[ExtractItem]:
    file_display = display_path(root, path)
    result: list[ExtractItem] = []
    for index, text in enumerate(lines, start=1):
        for pattern in patterns:
            if not pattern.search(text):
                continue
            start = max(1, index - context)
            end = min(len(lines), index + context)
            result.append(
                ExtractItem(
                    kind="match",
                    path=file_display,
                    label=pattern.pattern,
                    start_line=start,
                    end_line=end,
                    lines=make_lines(lines, start, end),
                )
            )
            break
    return result


def range_items(root: Path, path: Path, lines: list[str], raw_ranges: list[str]) -> list[ExtractItem]:
    file_display = display_path(root, path)
    result: list[ExtractItem] = []
    for raw_range in raw_ranges:
        start, end = parse_range(raw_range, len(lines))
        result.append(
            ExtractItem(
                kind="range",
                path=file_display,
                label=raw_range,
                start_line=start,
                end_line=end,
                lines=make_lines(lines, start, end),
            )
        )
    return result


def limit_items(items: list[ExtractItem], max_lines: int) -> tuple[list[ExtractItem], int, bool]:
    emitted = 0
    limited: list[ExtractItem] = []
    omitted = 0
    truncated = False
    for item in items:
        line_count = len(item.lines)
        if emitted + line_count > max_lines:
            truncated = True
            omitted += 1
            continue
        limited.append(item)
        emitted += line_count
    return limited, emitted, truncated or omitted > 0


def build_report(args: argparse.Namespace) -> ExtractReport:
    root = Path(args.root).resolve()
    summaries: list[FileSummary] = []
    items: list[ExtractItem] = []
    patterns = compile_patterns(args.match, args.regex, args.case_sensitive)
    default_headings = not args.match and not args.ranges and not args.headings

    for raw_path in args.paths:
        path = resolve_path(root, raw_path)
        data, lines = read_file(path, args.encoding, args.errors)
        file_display = display_path(root, path)
        summaries.append(
            FileSummary(
                path=file_display,
                line_count=len(lines),
                byte_count=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
            )
        )
        if args.headings or default_headings:
            items.extend(heading_items(root, path, lines))
        if patterns:
            items.extend(match_items(root, path, lines, patterns, max(0, args.context)))
        if args.ranges:
            items.extend(range_items(root, path, lines, args.ranges))

    limited, emitted, truncated = limit_items(items, max(0, args.max_lines))
    return ExtractReport(
        root=str(root),
        max_lines=max(0, args.max_lines),
        emitted_line_count=emitted,
        truncated=truncated,
        files=summaries,
        items=limited,
        omitted_item_count=len(items) - len(limited),
    )


def render_text(report: ExtractReport) -> str:
    output = [
        f"Context extract: files={len(report.files)} emitted_lines={report.emitted_line_count} "
        f"truncated={str(report.truncated).lower()} omitted_items={report.omitted_item_count}",
    ]
    for summary in report.files:
        output.append(
            f"- {summary.path}: lines={summary.line_count} bytes={summary.byte_count} sha256={summary.sha256}"
        )
    for item in report.items:
        output.append(f"\n[{item.kind}] {item.path}:{item.start_line}-{item.end_line} {item.label}")
        for line in item.lines:
            output.append(f"{line.line}: {line.text}")
    return "\n".join(output)


def render_markdown(report: ExtractReport) -> str:
    output = [
        "# Context Extract",
        "",
        f"- Files: {len(report.files)}",
        f"- Emitted lines: {report.emitted_line_count}",
        f"- Truncated: `{str(report.truncated).lower()}`",
        f"- Omitted items: {report.omitted_item_count}",
        "",
        "| File | Lines | Bytes | SHA256 |",
        "|---|---:|---:|---|",
    ]
    for summary in report.files:
        output.append(f"| `{summary.path}` | {summary.line_count} | {summary.byte_count} | `{summary.sha256}` |")
    for item in report.items:
        output.extend(["", f"## {item.kind}: `{item.path}` {item.start_line}-{item.end_line}", "", "```text"])
        output.extend(f"{line.line}: {line.text}" for line in item.lines)
        output.append("```")
    return "\n".join(output)


def main() -> int:
    args = parse_args()
    report = build_report(args)
    if args.format == "json":
        print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
    elif args.format == "markdown":
        print(render_markdown(report))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

