#!/usr/bin/env python3
"""Build and check a Markdown document reference index.

The index is a project-local evidence file. It complements hand-maintained
.references records by making backlinks, broken links, anchors, and code-path
mentions queryable after code or documentation changes. Scoped checks prefer a
directory-bubbling model: changed path -> local directory metadata -> parent
README files and AI rule adapters. Cross-directory backlinks are reported as
fallback risks.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

from ai_client_governance.common.paths import host_project_root


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SCHEMA_VERSION = 1
DEFAULT_OUTPUT = Path(".ai-client") / "project" / "doc-index" / "graph.json"
DEFAULT_TARGETS = [
    "README.md",
    "AGENTS.md",
    "CLAUDE.md",
    "GEMINI.md",
    "CONVENTIONS.md",
    ".github",
    ".cursor/rules",
    ".clinerules",
    ".windsurf/rules",
    ".continue/rules",
    ".roo/rules",
    "docs",
    ".ai-client/project/rules/project",
    ".ai-client/ai-client-governance/AGENTS.md",
]


def resolve_index_path(root: Path, value: str | None = None) -> Path:
    """Resolve doc-index artifacts under the host project unless explicitly overridden."""
    raw_value = value or os.environ.get("AICG_DOC_INDEX_OUTPUT")
    if raw_value:
        path = Path(raw_value)
        return path if path.is_absolute() else root / path
    return host_project_root(root) / DEFAULT_OUTPUT


EXCLUDED_DIR_NAMES = {
    ".git",
    ".idea",
    ".source-projects",
    ".uploads",
    ".trae",
    "__pycache__",
    "node_modules",
}
EXCLUDED_AI_CLIENT_DIRS = {
    "agent-comm",
    "agent-groups",
    "cache",
    "doc-index",
    "lifecycle",
    "project",
    "task-tracking",
    "tool-invocations",
}
LINK_RE = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)]+)\)")
REFERENCE_LINK_RE = re.compile(r"^\s*\[([^\]]+)\]:\s*(\S+)")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
CODE_REF_RE = re.compile(
    r"`([^`\n]+\.(?:py|md|java|xml|yml|yaml|json|properties|cpp|cc|cxx|h|hpp|go|rs|ts|js|css|html|ps1))`"
)
EXTERNAL_RE = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)
MARKDOWN_LIKE_SUFFIXES = {".md", ".mdc"}


@dataclass(frozen=True)
class Heading:
    line: int
    level: int
    text: str
    anchor: str


@dataclass(frozen=True)
class Link:
    line: int
    text: str
    target: str
    kind: str
    target_path: str | None = None
    target_anchor: str | None = None
    target_exists: bool | None = None
    target_anchor_exists: bool | None = None


@dataclass(frozen=True)
class CodeRef:
    line: int
    text: str
    target_path: str | None
    target_exists: bool | None


@dataclass(frozen=True)
class FileNode:
    path: str
    sha256: str
    title: str | None
    headings: list[Heading]
    links: list[Link]
    code_refs: list[CodeRef]


@dataclass
class DocGraph:
    schema_version: int
    generated_at: str
    root: str
    targets: list[str]
    summary: dict[str, int]
    files: list[FileNode]
    backlinks: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class Finding:
    level: str
    rule: str
    file: str
    line: int
    detail: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build or check a Markdown document index.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="Build the project doc-index graph.")
    add_common_args(build)
    build.add_argument("--output", help="Index output path.")

    check = subparsers.add_parser("check", help="Check links and backlinks from the document index.")
    add_common_args(check)
    check.add_argument("--index", help="Index JSON path.")
    check.add_argument("--output", help="Index output path when --rebuild is used.")
    check.add_argument("--rebuild", action="store_true", help="Rebuild the index before checking.")
    check.add_argument("--changed-path", action="append", default=[], help="Changed path used for scoped affected-doc checks.")
    check.add_argument("--strict", action="store_true", help="Exit non-zero for scoped broken links or anchors.")
    return parser.parse_args()


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", default=".", help="Repository root. Default: current directory.")
    parser.add_argument(
        "--paths",
        nargs="*",
        default=list(DEFAULT_TARGETS),
        help="Files or directories to scan. Defaults to README.md, AI rule adapters, docs, project rules, and common ai-client-governance.",
    )
    parser.add_argument("--format", choices=("text", "json"), default="text", help="Output format.")


def rel_path(path: Path, root: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(root.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def is_under(path: Path, ancestor: Path) -> bool:
    try:
        path.resolve().relative_to(ancestor.resolve())
        return True
    except ValueError:
        return False


def is_excluded(path: Path, root: Path) -> bool:
    rel_parts = path.resolve().relative_to(root.resolve()).parts
    if any(part in EXCLUDED_DIR_NAMES for part in rel_parts):
        return True
    if len(rel_parts) >= 2 and rel_parts[0] == ".ai-client" and rel_parts[1] in EXCLUDED_AI_CLIENT_DIRS:
        return True
    return False


def iter_markdown_files(root: Path, targets: list[str]) -> list[Path]:
    files: set[Path] = set()
    for target in targets:
        path = Path(target)
        if not path.is_absolute():
            path = root / path
        if not path.exists():
            continue
        if path.is_file() and path.suffix.lower() in MARKDOWN_LIKE_SUFFIXES:
            if not is_excluded(path, root):
                files.add(path.resolve())
            continue
        if path.is_dir():
            for pattern in ("*.md", "*.mdc"):
                for child in path.rglob(pattern):
                    if not is_excluded(child, root):
                        files.add(child.resolve())
    return sorted(files)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def strip_wrapping_angle(value: str) -> str:
    value = value.strip()
    if value.startswith("<") and value.endswith(">"):
        return value[1:-1].strip()
    return value


def split_target(value: str) -> tuple[str, str | None]:
    normalized = strip_wrapping_angle(unquote(value.strip()))
    normalized = normalized.split("?", 1)[0]
    if "#" not in normalized:
        return normalized, None
    path_part, anchor = normalized.split("#", 1)
    return path_part, anchor or None


def is_external(target: str) -> bool:
    lower = target.lower()
    return bool(EXTERNAL_RE.match(lower)) and not lower.startswith("file:")


def is_local_absolute(target: str) -> bool:
    return bool(
        re.match(r"^[a-zA-Z]:[\\/]", target)
        or target.startswith("/")
        or target.lower().startswith("file:")
    )


def clean_heading_text(text: str) -> str:
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    return text.strip()


def github_like_anchor(text: str) -> str:
    text = clean_heading_text(text).lower()
    chars: list[str] = []
    previous_dash = False
    for ch in text:
        if ch.isalnum() or "\u4e00" <= ch <= "\u9fff":
            chars.append(ch)
            previous_dash = False
        elif ch.isspace() or ch in "-_":
            if not previous_dash and chars:
                chars.append("-")
                previous_dash = True
    return "".join(chars).strip("-")


def resolve_local_target(source: Path, target: str, root: Path) -> tuple[Path | None, str | None, str | None]:
    path_part, anchor = split_target(target)
    if is_external(path_part) or is_local_absolute(path_part):
        return None, None, anchor
    if not path_part:
        return source.resolve(), rel_path(source, root), anchor
    candidate = (source.parent / path_part).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None, None, anchor
    return candidate, rel_path(candidate, root), anchor


def line_number_for_offset(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def parse_headings(lines: list[str]) -> list[Heading]:
    headings: list[Heading] = []
    seen: dict[str, int] = {}
    for index, line in enumerate(lines, start=1):
        match = HEADING_RE.match(line)
        if not match:
            continue
        level = len(match.group(1))
        text = clean_heading_text(match.group(2))
        base = github_like_anchor(text)
        count = seen.get(base, 0)
        seen[base] = count + 1
        anchor = base if count == 0 else f"{base}-{count}"
        headings.append(Heading(index, level, text, anchor))
    return headings


def fenced_code_lines(lines: list[str]) -> set[int]:
    fenced: set[int] = set()
    in_fence = False
    fence_marker: str | None = None
    for index, line in enumerate(lines, start=1):
        stripped = line.strip()
        starts_fence = stripped.startswith("```") or stripped.startswith("~~~")
        if starts_fence:
            marker = stripped[:3]
            fenced.add(index)
            if not in_fence:
                in_fence = True
                fence_marker = marker
            elif fence_marker == marker:
                in_fence = False
                fence_marker = None
            continue
        if in_fence:
            fenced.add(index)
    return fenced


def parse_file(path: Path, root: Path, anchor_map: dict[str, set[str]]) -> FileNode:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    rel = rel_path(path, root)
    headings = parse_headings(lines)
    title = headings[0].text if headings else None
    anchors = anchor_map.get(rel, set())
    fenced_lines = fenced_code_lines(lines)

    links: list[Link] = []
    for match in LINK_RE.finditer(text):
        label = match.group(1).strip()
        raw_target = match.group(2).strip()
        line = line_number_for_offset(text, match.start())
        if line in fenced_lines:
            continue
        path_part, anchor = split_target(raw_target)
        if is_external(path_part):
            links.append(Link(line, label, raw_target, "external", target_anchor=anchor))
            continue
        candidate, target_rel, target_anchor = resolve_local_target(path, raw_target, root)
        target_exists = candidate.exists() if candidate else None
        target_anchor_exists = None
        if candidate and target_exists and target_anchor:
            target_anchor_exists = target_anchor in anchor_map.get(target_rel or rel_path(candidate, root), set())
        links.append(
            Link(
                line=line,
                text=label,
                target=raw_target,
                kind="local",
                target_path=target_rel,
                target_anchor=target_anchor,
                target_exists=target_exists,
                target_anchor_exists=target_anchor_exists,
            )
        )

    for index, line in enumerate(lines, start=1):
        if index in fenced_lines:
            continue
        ref_match = REFERENCE_LINK_RE.match(line)
        if not ref_match:
            continue
        raw_target = ref_match.group(2).strip()
        path_part, anchor = split_target(raw_target)
        if is_external(path_part):
            links.append(Link(index, ref_match.group(1), raw_target, "external", target_anchor=anchor))
            continue
        candidate, target_rel, target_anchor = resolve_local_target(path, raw_target, root)
        target_exists = candidate.exists() if candidate else None
        target_anchor_exists = None
        if candidate and target_exists and target_anchor:
            target_anchor_exists = target_anchor in anchor_map.get(target_rel or rel_path(candidate, root), set())
        links.append(
            Link(
                line=index,
                text=ref_match.group(1),
                target=raw_target,
                kind="reference",
                target_path=target_rel,
                target_anchor=target_anchor,
                target_exists=target_exists,
                target_anchor_exists=target_anchor_exists,
            )
        )

    code_refs: list[CodeRef] = []
    for match in CODE_REF_RE.finditer(text):
        value = match.group(1).strip()
        line = line_number_for_offset(text, match.start())
        if line in fenced_lines:
            continue
        candidate = (path.parent / value).resolve()
        target_rel: str | None = None
        exists: bool | None = None
        if is_under(candidate, root):
            target_rel = rel_path(candidate, root)
            exists = candidate.exists()
        code_refs.append(CodeRef(line=line, text=value, target_path=target_rel, target_exists=exists))

    return FileNode(
        path=rel,
        sha256=sha256_text(text),
        title=title,
        headings=headings,
        links=links,
        code_refs=code_refs,
    )


def build_graph(root: Path, targets: list[str]) -> DocGraph:
    files = iter_markdown_files(root, targets)
    anchor_map: dict[str, set[str]] = {}
    for path in files:
        rel = rel_path(path, root)
        lines = path.read_text(encoding="utf-8").splitlines()
        anchor_map[rel] = {heading.anchor for heading in parse_headings(lines)}

    nodes = [parse_file(path, root, anchor_map) for path in files]
    backlinks: dict[str, list[str]] = {}
    local_links = 0
    external_links = 0
    broken_links = 0
    missing_anchors = 0
    code_refs = 0
    missing_code_refs = 0
    for node in nodes:
        for link in node.links:
            if link.kind == "external":
                external_links += 1
                continue
            local_links += 1
            if link.target_path:
                backlinks.setdefault(link.target_path, []).append(f"{node.path}:{link.line}")
            if link.target_exists is False:
                broken_links += 1
            if link.target_anchor_exists is False:
                missing_anchors += 1
        for code_ref in node.code_refs:
            code_refs += 1
            if code_ref.target_exists is False:
                missing_code_refs += 1

    return DocGraph(
        schema_version=SCHEMA_VERSION,
        generated_at=utc_now(),
        root=str(root.resolve()),
        targets=targets,
        summary={
            "files": len(nodes),
            "headings": sum(len(node.headings) for node in nodes),
            "local_links": local_links,
            "external_links": external_links,
            "broken_local_links": broken_links,
            "missing_anchors": missing_anchors,
            "code_refs": code_refs,
            "missing_code_refs": missing_code_refs,
        },
        files=nodes,
        backlinks={key: sorted(value) for key, value in sorted(backlinks.items())},
    )


def write_graph(graph: DocGraph, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(asdict(graph), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_graph(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def existing_rel(path: Path, root: Path) -> str | None:
    if path.exists() and is_under(path, root):
        return rel_path(path, root)
    return None


def changed_path_directory(root: Path, changed_path: str) -> Path:
    path = Path(changed_path)
    if not path.is_absolute():
        path = root / path
    if path.exists() and path.is_dir():
        return path.resolve()
    if path.suffix:
        return path.resolve().parent
    return path.resolve()


def bubble_targets_for(root: Path, changed_path: str) -> list[str]:
    targets: list[str] = []

    def add(path: Path) -> None:
        rel = existing_rel(path, root)
        if rel and rel not in targets:
            targets.append(rel)

    changed = Path(changed_path)
    if not changed.is_absolute():
        changed = root / changed
    add(changed)

    if changed.suffix.lower() == ".md":
        add(changed.parent / ".references" / changed.name)

    current = changed_path_directory(root, changed_path)
    try:
        current.relative_to(root)
    except ValueError:
        return targets

    while True:
        add(current / "README.md")
        add(current / "AGENTS.md")
        add(current / "CLAUDE.md")
        add(current / "GEMINI.md")
        add(current / "CONVENTIONS.md")
        add(current / ".references" / "README.md")
        add(current / ".references" / "AGENTS.md")
        if current == root:
            break
        current = current.parent
    return targets


def parse_ref_source(ref: str) -> str:
    return ref.rsplit(":", 1)[0]


def graph_findings(
    graph: dict, root: Path, changed_paths: list[str]
) -> tuple[list[Finding], list[Finding], dict[str, list[str]], dict[str, list[str]], dict[str, list[str]]]:
    changed = {path.replace("\\", "/") for path in changed_paths}
    if not changed:
        changed = set()
    broken: list[Finding] = []
    anchors: list[Finding] = []
    affected: dict[str, list[str]] = {}
    bubble: dict[str, list[str]] = {}
    cross_directory_refs: dict[str, list[str]] = {}
    backlinks = graph.get("backlinks", {})
    for path in changed:
        bubble[path] = bubble_targets_for(root, path)
        if path in backlinks:
            affected[path] = list(backlinks[path])
            bubble_sources = set(bubble[path])
            cross_refs = [
                ref for ref in affected[path] if parse_ref_source(ref) not in bubble_sources
            ]
            if cross_refs:
                cross_directory_refs[path] = cross_refs

    for node in graph.get("files", []):
        source = node["path"]
        source_is_changed = source in changed
        for link in node.get("links", []):
            target_path = link.get("target_path")
            target_is_changed = target_path in changed if target_path else False
            scoped = not changed or source_is_changed or target_is_changed
            if not scoped:
                continue
            if link.get("target_exists") is False:
                broken.append(
                    Finding(
                        "error",
                        "broken-local-link",
                        source,
                        int(link.get("line") or 0),
                        f"{link.get('target')} -> {target_path or 'outside-root'}",
                    )
                )
            if link.get("target_anchor_exists") is False:
                anchors.append(
                    Finding(
                        "warning",
                        "missing-anchor",
                        source,
                        int(link.get("line") or 0),
                        f"{link.get('target')} anchor {link.get('target_anchor')}",
                    )
                )
    return broken, anchors, affected, bubble, cross_directory_refs


def normalize_changed_paths(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values or []:
        for part in value.split(","):
            stripped = part.strip().replace("\\", "/")
            if stripped and stripped not in result:
                result.append(stripped)
    return result


def format_build_text(graph: DocGraph, output: Path) -> str:
    summary = graph.summary
    return "\n".join(
        [
            f"Doc index written: {output.as_posix()}",
            f"Files: {summary['files']}",
            f"Headings: {summary['headings']}",
            f"Local links: {summary['local_links']}",
            f"External links: {summary['external_links']}",
            f"Broken local links: {summary['broken_local_links']}",
            f"Missing anchors: {summary['missing_anchors']}",
            f"Code refs: {summary['code_refs']}",
            f"Missing code refs: {summary['missing_code_refs']}",
        ]
    )


def format_check_text(
    graph: dict,
    broken: list[Finding],
    anchors: list[Finding],
    affected: dict[str, list[str]],
    bubble: dict[str, list[str]],
    cross_directory_refs: dict[str, list[str]],
) -> str:
    summary = graph.get("summary", {})
    lines = [
        "Doc index check",
        f"Files: {summary.get('files', 0)}",
        f"Global broken local links: {summary.get('broken_local_links', 0)}",
        f"Global missing anchors: {summary.get('missing_anchors', 0)}",
        f"Scoped broken local links: {len(broken)}",
        f"Scoped missing anchors: {len(anchors)}",
        f"Bubble changed paths: {len(bubble)}",
        f"Cross-directory ref paths: {len(cross_directory_refs)}",
        f"Affected changed paths: {len(affected)}",
    ]
    for target, refs in sorted(bubble.items()):
        preview = ", ".join(refs[:10])
        extra = "" if len(refs) <= 10 else f" ... (+{len(refs) - 10})"
        lines.append(f"  - bubble {target}: {preview or 'none'}{extra}")
    for target, refs in sorted(affected.items()):
        preview = ", ".join(refs[:8])
        extra = "" if len(refs) <= 8 else f" ... (+{len(refs) - 8})"
        lines.append(f"  - {target}: {preview}{extra}")
    if cross_directory_refs:
        lines.append("Cross-directory refs:")
        for target, refs in sorted(cross_directory_refs.items()):
            preview = ", ".join(refs[:8])
            extra = "" if len(refs) <= 8 else f" ... (+{len(refs) - 8})"
            lines.append(f"  - {target}: {preview}{extra}")
    if broken:
        lines.append("Broken links:")
        for item in broken[:20]:
            lines.append(f"  - {item.file}:{item.line} {item.detail}")
    if anchors:
        lines.append("Missing anchors:")
        for item in anchors[:20]:
            lines.append(f"  - {item.file}:{item.line} {item.detail}")
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    root = Path(args.root).resolve()
    if args.command == "build":
        graph = build_graph(root, args.paths)
        output = resolve_index_path(root, args.output)
        write_graph(graph, output)
        if args.format == "json":
            print(json.dumps({"output": rel_path(output, root), "summary": graph.summary}, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(format_build_text(graph, Path(rel_path(output, root))))
        return 0

    index = resolve_index_path(root, args.index)
    if args.rebuild or not index.exists():
        graph_obj = build_graph(root, args.paths)
        output = resolve_index_path(root, args.output)
        write_graph(graph_obj, output)
        graph = json.loads(json.dumps(asdict(graph_obj), ensure_ascii=False))
        index = output
    else:
        graph = load_graph(index)

    changed_paths = normalize_changed_paths(args.changed_path)
    broken, anchors, affected, bubble, cross_directory_refs = graph_findings(graph, root, changed_paths)
    payload = {
        "index": rel_path(index, root),
        "summary": graph.get("summary", {}),
        "changed_paths": changed_paths,
        "scoped_broken_links": [asdict(item) for item in broken],
        "scoped_missing_anchors": [asdict(item) for item in anchors],
        "affected": affected,
        "bubble": bubble,
        "cross_directory_refs": cross_directory_refs,
    }
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_check_text(graph, broken, anchors, affected, bubble, cross_directory_refs))
    return 1 if args.strict and (broken or anchors) else 0


if __name__ == "__main__":
    raise SystemExit(main())
