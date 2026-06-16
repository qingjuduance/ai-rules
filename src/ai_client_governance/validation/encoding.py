#!/usr/bin/env python3
"""Read-only encoding gate for Windows, PowerShell, Python, and Git tasks."""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


TEXT_EXTENSIONS = {
    ".css",
    ".html",
    ".js",
    ".json",
    ".md",
    ".mjs",
    ".ps1",
    ".py",
    ".toml",
    ".ts",
    ".txt",
    ".yaml",
    ".yml",
}

BINARY_PARTS = {
    ".git",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
}

GET_CONTENT_RE = re.compile(r"\bGet-Content\b(?![^\r\n]*\s-Encoding\b)", re.I)
SET_CONTENT_RE = re.compile(r"\bSet-Content\b(?![^\r\n]*\s-Encoding\b)", re.I)
OUT_FILE_RE = re.compile(r"\bOut-File\b(?![^\r\n]*\s-Encoding\b)", re.I)
ADD_CONTENT_RE = re.compile(r"\bAdd-Content\b(?![^\r\n]*\s-Encoding\b)", re.I)
GIT_PATH_OUTPUT_RE = re.compile(
    r"(?:^|[;&|]\s*|\s)&?\s*git\s+(?:status|ls-files|diff|log|show)\b",
    re.I,
)


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
    errors: list[Finding]
    warnings: list[Finding]
    notes: list[Finding]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate UTF-8 and Windows command encoding gates."
    )
    parser.add_argument("--root", default=".", help="Repository root.")
    parser.add_argument(
        "--paths",
        nargs="*",
        default=None,
        help="Files or directories to check. Defaults to git-changed text files.",
    )
    parser.add_argument(
        "--require-paths",
        action="store_true",
        help="Fail when no text files are selected.",
    )
    parser.add_argument(
        "--windows-powershell51",
        action="store_true",
        help="Warn when non-ASCII .ps1 files lack a UTF-8 BOM.",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Run a UTF-8 roundtrip smoke test with a Unicode path.",
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
    return path.resolve().relative_to(root.resolve()).as_posix()


def resolve_from_root(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def is_ignored(path: Path) -> bool:
    return any(part in BINARY_PARTS for part in path.parts)


def is_text_candidate(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in TEXT_EXTENSIONS and not is_ignored(path)


def run_git_changed(root: Path) -> list[Path]:
    try:
        result = subprocess.run(
            ["git", "-c", "core.quotepath=false", "status", "--porcelain", "--untracked-files=all"],
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
        paths.append(root / path_text)
    return paths


def iter_files(paths: Iterable[Path]) -> list[Path]:
    collected: list[Path] = []
    for path in paths:
        if path.is_dir():
            collected.extend(sorted(item for item in path.rglob("*") if is_text_candidate(item)))
        elif is_text_candidate(path):
            collected.append(path)

    seen: set[Path] = set()
    unique: list[Path] = []
    for path in collected:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(path)
    return unique


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


def check_powershell_line(
    line: str,
    rel: str,
    line_no: int,
    errors: list[Finding],
    warnings: list[Finding],
    notes: list[Finding],
) -> None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return

    checks = [
        (GET_CONTENT_RE, "Get-Content without -Encoding; use -Raw -Encoding UTF8 for repo text"),
        (SET_CONTENT_RE, "Set-Content without -Encoding; use -Encoding UTF8 for repo text"),
        (OUT_FILE_RE, "Out-File without -Encoding; use -Encoding UTF8 for repo text"),
        (ADD_CONTENT_RE, "Add-Content without -Encoding; use -Encoding UTF8 for repo text"),
    ]
    for pattern, message in checks:
        if pattern.search(line):
            add_finding(errors, warnings, notes, "warning", rel, message, line_no)

    if "$OutputEncoding" in line and "UTF8Encoding" not in line and "UTF8" not in line:
        add_finding(
            errors,
            warnings,
            notes,
            "warning",
            rel,
            "$OutputEncoding is set without an explicit UTF-8 encoding",
            line_no,
        )

    if GIT_PATH_OUTPUT_RE.search(line):
        has_quote_path = "core.quotepath=false" in line
        has_nul_output = re.search(r"(?:^|\s)-z(?:\s|$)", line) is not None
        if not has_quote_path and not has_nul_output:
            add_finding(
                errors,
                warnings,
                notes,
                "warning",
                rel,
                "git path-output command without core.quotepath=false or -z",
                line_no,
            )

def keyword_value(node: ast.Call, name: str) -> ast.AST | None:
    for keyword in node.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def has_keyword(node: ast.Call, name: str) -> bool:
    return keyword_value(node, name) is not None


def literal_string(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def literal_true(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def call_full_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = call_full_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def call_line(node: ast.AST) -> int | None:
    return getattr(node, "lineno", None)


def call_mode(node: ast.Call) -> str:
    mode = literal_string(keyword_value(node, "mode"))
    if mode is not None:
        return mode
    if len(node.args) >= 2:
        mode = literal_string(node.args[1])
        if mode is not None:
            return mode
    return "r"


def check_python_call(
    node: ast.Call,
    rel: str,
    errors: list[Finding],
    warnings: list[Finding],
    notes: list[Finding],
) -> None:
    full_name = call_full_name(node.func)
    line_no = call_line(node)

    if full_name == "open" or full_name.endswith(".open"):
        mode = call_mode(node)
        if "b" not in mode and not has_keyword(node, "encoding"):
            add_finding(
                errors,
                warnings,
                notes,
                "warning",
                rel,
                "text file open without explicit encoding; use encoding=\"utf-8\"",
                line_no,
            )
        return

    if full_name.endswith(".read_text") or full_name.endswith(".write_text"):
        if not has_keyword(node, "encoding"):
            add_finding(
                errors,
                warnings,
                notes,
                "warning",
                rel,
                "Path text I/O without explicit encoding; use encoding=\"utf-8\"",
                line_no,
            )
        return

    if full_name in {"subprocess.run", "subprocess.Popen", "subprocess.check_output"}:
        uses_text = literal_true(keyword_value(node, "text")) or literal_true(
            keyword_value(node, "universal_newlines")
        )
        if uses_text and not has_keyword(node, "encoding"):
            add_finding(
                errors,
                warnings,
                notes,
                "warning",
                rel,
                "subprocess text output without explicit encoding; use encoding=\"utf-8\"",
                line_no,
            )


def check_python_ast(
    text: str,
    rel: str,
    errors: list[Finding],
    warnings: list[Finding],
    notes: list[Finding],
) -> None:
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        add_finding(
            errors,
            warnings,
            notes,
            "warning",
            rel,
            f"cannot parse Python for encoding checks: {exc.msg}",
            exc.lineno,
        )
        return

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            check_python_call(node, rel, errors, warnings, notes)


def check_file(
    path: Path,
    root: Path,
    args: argparse.Namespace,
    errors: list[Finding],
    warnings: list[Finding],
    notes: list[Finding],
) -> None:
    rel = rel_path(path, root)
    try:
        data = path.read_bytes()
    except OSError as exc:
        add_finding(errors, warnings, notes, "error", rel, f"cannot read file: {exc}")
        return

    has_bom = data.startswith(b"\xef\xbb\xbf")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        add_finding(errors, warnings, notes, "error", rel, f"not UTF-8: {exc}")
        return

    if "\ufffd" in text:
        add_finding(
            errors,
            warnings,
            notes,
            "warning",
            rel,
            "contains Unicode replacement character; verify source text was not already corrupted",
        )

    suffix = path.suffix.lower()
    has_non_ascii = any(ord(ch) > 127 for ch in text)
    if suffix == ".json":
        if has_bom:
            add_finding(
                errors,
                warnings,
                notes,
                "warning",
                rel,
                "JSON file has a UTF-8 BOM; avoid BOM for cross-system JSON",
            )
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            add_finding(
                errors,
                warnings,
                notes,
                "error",
                rel,
                f"invalid JSON after UTF-8 decoding: {exc.msg}",
                exc.lineno,
            )
    if suffix == ".ps1":
        if args.windows_powershell51 and has_non_ascii and not has_bom:
            add_finding(
                errors,
                warnings,
                notes,
                "warning",
                rel,
                "non-ASCII PowerShell script lacks UTF-8 BOM; Windows PowerShell 5.1 needs parser verification",
            )
        for line_no, line in enumerate(text.splitlines(), start=1):
            check_powershell_line(line, rel, line_no, errors, warnings, notes)
    elif suffix == ".py":
        check_python_ast(text, rel, errors, warnings, notes)


def run_smoke(
    errors: list[Finding],
    warnings: list[Finding],
    notes: list[Finding],
) -> None:
    sample = {"path": "中文路径", "text": "编码检查 UTF-8 smoke"}
    try:
        with tempfile.TemporaryDirectory(prefix="encoding-smoke-") as temp_dir:
            root = Path(temp_dir) / "中文目录"
            root.mkdir()
            target = root / "规则-编码.json"
            target.write_text(json.dumps(sample, ensure_ascii=False), encoding="utf-8")
            loaded = json.loads(target.read_text(encoding="utf-8"))
            if loaded != sample:
                add_finding(
                    errors,
                    warnings,
                    notes,
                    "error",
                    "<smoke>",
                    "UTF-8 roundtrip content mismatch",
                )
                return
            add_finding(
                errors,
                warnings,
                notes,
                "note",
                "<smoke>",
                "UTF-8 Unicode path roundtrip succeeded",
            )
    except Exception as exc:
        add_finding(errors, warnings, notes, "error", "<smoke>", f"smoke test failed: {exc}")


def build_report(args: argparse.Namespace) -> Report:
    root = Path(args.root).resolve()
    if args.paths is None:
        raw_paths = run_git_changed(root)
    else:
        raw_paths = [resolve_from_root(root, item) for item in args.paths]

    files = [
        path
        for path in iter_files(raw_paths)
        if root in path.resolve().parents or path.resolve() == root
    ]
    selected = sorted(rel_path(path, root) for path in files)

    errors: list[Finding] = []
    warnings: list[Finding] = []
    notes: list[Finding] = []

    if args.require_paths and not files:
        add_finding(errors, warnings, notes, "error", "<paths>", "no text files selected")
    elif not files:
        add_finding(errors, warnings, notes, "note", "<paths>", "no text files selected")

    for path in files:
        check_file(path, root, args, errors, warnings, notes)

    if args.smoke:
        run_smoke(errors, warnings, notes)

    return Report(
        root=root.as_posix(),
        checked_files=selected,
        errors=errors,
        warnings=warnings,
        notes=notes,
    )


def render_findings(title: str, findings: list[Finding]) -> list[str]:
    lines = [f"{title}: {len(findings)}"]
    if not findings:
        lines.append("  none")
        return lines
    for finding in findings:
        location = finding.file if finding.line is None else f"{finding.file}:{finding.line}"
        lines.append(f"  - {location}: {finding.message}")
    return lines


def render_text(report: Report) -> str:
    lines = [
        "Encoding Gate Report",
        f"Root: {report.root}",
        f"Checked text files: {len(report.checked_files)}",
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

