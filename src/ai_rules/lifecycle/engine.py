#!/usr/bin/env python3
"""AI rules lifecycle router for task classification, preflight, and final gates.

This script turns prose workflow rules into a small executable lifecycle for the
human/AI coordination layer: input -> classification -> preflight -> execution
evidence -> finalize. It is conservative by design. It writes only optional
lifecycle state JSON under .codex/project/lifecycle/ and delegates heavy checks
through the unified ai_rules.py CLI.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ai_rules.common.paths import PYTHON_PYCACHE_DIR, ai_rules_entrypoint


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SCHEMA_VERSION = 1
DEFAULT_TRACE_PREFIX = "trace-lifecycle"
STATE_DIR = Path(".codex") / "project" / "lifecycle"

TEXT_EXTENSIONS = {
    ".css",
    ".html",
    ".js",
    ".json",
    ".md",
    ".ps1",
    ".py",
    ".toml",
    ".ts",
    ".yaml",
    ".yml",
}

TASK_KEYWORDS: dict[str, list[str]] = {
    "code-debug": [
        "bug",
        "debug",
        "error",
        "exception",
        "log",
        "traceback",
        "报错",
        "调试",
        "日志",
        "异常",
    ],
    "correction": ["correction", "漏", "错", "纠错", "修正", "没按", "遗漏"],
    "rules-script": [
        "AGENTS",
        "CLAUDE.md",
        "GEMINI.md",
        "CONVENTIONS.md",
        "copilot-instructions",
        ".cursor/rules",
        ".clinerules",
        ".windsurf/rules",
        ".continue/rules",
        ".roo/rules",
        "adapter",
        "SKILL",
        "ai-rules",
        "gate",
        "hook",
        "lifecycle",
        "pipeline",
        "script",
        "state machine",
        "workflow",
        "callback",
        "wrapper",
        "生命周期",
        "流水线",
        "状态机",
        "中间层",
        "回调",
        "包装器",
        "规则",
        "脚本",
        "门禁",
    ],
    "docs": [
        "README",
        "docs",
        "markdown",
        "reference",
        "文档",
        "索引",
        "引用",
        "链接",
    ],
    "git": ["commit", "push", "stage", "stash", "git", "提交", "推送", "暂存"],
    "frontend": ["browser", "frontend", "localhost", "playwright", "ui", "页面", "浏览器"],
    "resume": ["PDF", "resumes/", "resumes\\", "简历", "导出"],
    "multi-agent": ["agent", "sub-agent", "主从", "子 AI", "子AI", "智能体"],
    "long-running": ["pending", "恢复", "继续", "长任务", "未完成", "定时", "周期"],
}

MUTATING_TYPES = {
    "code-debug",
    "correction",
    "rules-script",
    "docs",
    "git",
    "frontend",
    "resume",
    "multi-agent",
    "long-running",
}


@dataclass
class Finding:
    level: str
    message: str
    source: str = "ai_rules.py lifecycle"


@dataclass
class InputRecord:
    source: str
    trust: str
    needs_citation: bool
    summary: str
    derived_from: list[str] = field(default_factory=list)


@dataclass
class Classification:
    task_types: list[str]
    task_size: str
    task_size_reasons: list[str]
    requires_tracking: bool
    requires_approval: bool
    required_hooks: list[str]
    required_gates: list[str]
    changed_paths: list[str]


@dataclass
class LifecycleReport:
    schema_version: int
    trace_id: str
    phase: str
    state: str
    updated_at: str
    input: InputRecord
    classification: Classification
    errors: list[Finding] = field(default_factory=list)
    warnings: list[Finding] = field(default_factory=list)
    notes: list[Finding] = field(default_factory=list)
    gate_commands: list[list[str]] = field(default_factory=list)
    state_file: str | None = None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def read_text_file(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def rel_path(path: Path, root: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(root.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def normalize_paths(values: list[str] | None) -> list[str]:
    result: list[str] = []
    for value in values or []:
        for part in value.split(","):
            stripped = part.strip()
            if stripped and stripped not in result:
                result.append(stripped.replace("\\", "/"))
    return result


def message_from_args(args: argparse.Namespace, root: Path) -> str:
    parts: list[str] = []
    if getattr(args, "message", None):
        parts.append(args.message)
    if getattr(args, "message_file", None):
        path = Path(args.message_file)
        if not path.is_absolute():
            path = root / path
        if path.exists():
            parts.append(read_text_file(path))
    return "\n".join(parts).strip()


def summarize(text: str, limit: int = 240) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."


def trust_for_source(source: str) -> tuple[str, bool]:
    if source == "web":
        return "external-web", True
    if source == "file":
        return "repository-file", False
    if source == "tool":
        return "tool-output", False
    if source == "agent":
        return "delegated-agent-output", False
    if source == "history":
        return "session-history", False
    return "user-instruction", False


def contains_keyword(text: str, keyword: str) -> bool:
    if re.search(r"[\u4e00-\u9fff]", keyword):
        return keyword in text
    return re.search(rf"\b{re.escape(keyword.lower())}\b", text.lower()) is not None


def infer_task_types(text: str, changed_paths: list[str], explicit: list[str]) -> list[str]:
    found: list[str] = []
    for task_type in explicit:
        normalized = task_type.strip()
        if normalized and normalized not in found:
            found.append(normalized)

    path_blob = " ".join(changed_paths)
    haystack = f"{text}\n{path_blob}"
    for task_type, keywords in TASK_KEYWORDS.items():
        if any(contains_keyword(haystack, keyword) for keyword in keywords):
            if task_type not in found:
                found.append(task_type)

    suffixes = {Path(path).suffix.lower() for path in changed_paths}
    if ".md" in suffixes and "docs" not in found:
        found.append("docs")
    if ".py" in suffixes and "rules-script" not in found:
        found.append("rules-script")
    adapter_path_signals = (
        "AGENTS.md",
        "CLAUDE.md",
        "GEMINI.md",
        "CONVENTIONS.md",
        ".github/copilot-instructions.md",
        ".github/instructions/",
        ".cursor/rules/",
        ".clinerules/",
        ".windsurf/rules/",
        ".continue/rules/",
        ".roo/rules/",
    )
    if any(path.startswith(".codex/ai-rules") or path.endswith(adapter_path_signals) for path in changed_paths):
        if "rules-script" not in found:
            found.append("rules-script")
    return found


def estimate_task_size(
    text: str,
    task_types: list[str],
    changed_paths: list[str],
    input_source: str,
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    md_count = sum(1 for path in changed_paths if Path(path).suffix.lower() == ".md")
    py_count = sum(1 for path in changed_paths if Path(path).suffix.lower() == ".py")

    if len(task_types) >= 3:
        reasons.append(f"task types >= 3 ({len(task_types)})")
    if len(changed_paths) > 3:
        reasons.append(f"changed paths > 3 ({len(changed_paths)})")
    if {"rules-script", "docs"}.issubset(task_types):
        reasons.append("rules-script and docs gates both apply")
    if any(path.startswith(".codex/ai-rules") for path in changed_paths):
        reasons.append("embedded ai-rules repository is in scope")
    if any(keyword in text for keyword in ["全部", "批量", "状态机", "流水线", "主从", "生命周期"]):
        reasons.append("request contains broad workflow architecture signals")
    if md_count >= 3 or py_count >= 2:
        reasons.append("multiple markdown or python files are in scope")

    if reasons:
        return "large", reasons

    medium_reasons: list[str] = []
    if task_types and any(task_type in MUTATING_TYPES for task_type in task_types):
        medium_reasons.append("mutating or gated task type is present")
    if changed_paths:
        medium_reasons.append(f"changed paths present ({len(changed_paths)})")
    if input_source == "web":
        medium_reasons.append("external web input needs citation boundary")
    if len(text) > 600:
        medium_reasons.append("input text is long enough to need routing")

    if medium_reasons:
        return "medium", medium_reasons
    return "small", ["read-only or low-blast-radius input"]


def required_hooks_and_gates(
    task_types: list[str],
    task_size: str,
    changed_paths: list[str],
    input_source: str,
) -> tuple[list[str], list[str], bool, bool]:
    hooks: list[str] = [
        "input.filter.classify-source",
        "input.filter.decompose-requirements",
        "input.filter.recordability-judgement",
        "input.filter.network-search-judgement",
        "output.interceptor.answer-quality",
        "output.interceptor.user-satisfaction",
        "output.interceptor.finalize-closeout",
    ]
    gates: list[str] = []

    if input_source == "web":
        hooks.append("input.filter.citation-boundary")
    if task_size in {"medium", "large"}:
        hooks.extend(["preflight.interceptor.task-tracking", "preflight.interceptor.task-size"])
        gates.append("ai_rules.py task-gate:user-input-and-requirements")
    if changed_paths:
        hooks.append("post-change.interceptor.diff-check")
        gates.append("encoding-or-whitespace-check")
    if any(Path(path).suffix.lower() == ".py" for path in changed_paths):
        gates.append("py_compile")
    if "rules-script" in task_types:
        hooks.extend(["preflight.interceptor.approval", "preflight.interceptor.external-practice-check"])
        gates.extend(["ai_rules.py task-gate", "ai_rules.py session-gate"])
    if "docs" in task_types or any(Path(path).suffix.lower() == ".md" for path in changed_paths):
        hooks.extend([
            "post-change.interceptor.doc-index-bubble",
            "post-change.interceptor.reference-check",
            "output.interceptor.document-sync",
        ])
        gates.extend(["ai_rules.py doc-index", "ai_rules.py validate-doc"])
    if "git" in task_types:
        hooks.append("preflight.interceptor.git-boundary")
        gates.append("git status")
    if "multi-agent" in task_types:
        hooks.extend(["coordination.interceptor.agent-brief", "coordination.interceptor.agent-acceptance-matrix"])
        gates.append("ai_rules.py task-gate:multi-agent-acceptance-matrix")
    if "resume" in task_types:
        hooks.append("post-change.interceptor.resume-export")
        gates.append("PDF export/layout check")
    if "long-running" in task_types:
        hooks.extend([
            "session.interceptor.pending-recovery",
            "periodic.filter.sync-check",
            "periodic.interceptor.state-audit",
        ])
        gates.append("ai_rules.py session-gate")

    requires_tracking = task_size in {"medium", "large"} or any(
        task_type in {"rules-script", "docs", "git", "resume", "correction", "long-running"}
        for task_type in task_types
    )
    requires_approval = bool(changed_paths) or any(
        task_type in {"rules-script", "docs", "git", "resume", "correction", "multi-agent"}
        for task_type in task_types
    )

    return unique(hooks), unique(gates), requires_tracking, requires_approval


def unique(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def build_classification(args: argparse.Namespace, root: Path) -> tuple[InputRecord, Classification]:
    message = message_from_args(args, root)
    changed_paths = normalize_paths(getattr(args, "changed_path", None))
    explicit_types = normalize_paths(getattr(args, "task_type", None))
    trust, needs_citation = trust_for_source(args.input_source)
    input_record = InputRecord(
        source=args.input_source,
        trust=trust,
        needs_citation=needs_citation,
        summary=summarize(message),
        derived_from=normalize_paths(getattr(args, "derived_from", None)),
    )
    task_types = infer_task_types(message, changed_paths, explicit_types)
    task_size, reasons = estimate_task_size(message, task_types, changed_paths, args.input_source)
    hooks, gates, requires_tracking, requires_approval = required_hooks_and_gates(
        task_types, task_size, changed_paths, args.input_source
    )
    return input_record, Classification(
        task_types=task_types,
        task_size=task_size,
        task_size_reasons=reasons,
        requires_tracking=requires_tracking,
        requires_approval=requires_approval,
        required_hooks=hooks,
        required_gates=gates,
        changed_paths=changed_paths,
    )


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", default=".", help="Repository root. Default: current directory.")
    parser.add_argument(
        "--input-source",
        choices=("user", "web", "file", "tool", "agent", "history"),
        default="user",
        help="Input source being routed through the lifecycle.",
    )
    parser.add_argument("--message", default="", help="Input message or short task description.")
    parser.add_argument("--message-file", help="Read input text from a UTF-8 file.")
    parser.add_argument("--derived-from", action="append", default=[], help="Source URL/path/trace this input came from.")
    parser.add_argument("--changed-path", action="append", default=[], help="Path changed or expected to change.")
    parser.add_argument("--task-type", action="append", default=[], help="Explicit task type override/addition.")
    parser.add_argument("--task-tracking", help="Task tracking file for gated work.")
    parser.add_argument("--approved-label", help="Approval label, for example 批准：计划-生命周期状态机门禁.")
    parser.add_argument("--trace-id", help="Trace id. Default: generated or inferred from state file.")
    parser.add_argument("--state-file", help="Lifecycle state JSON path. Default: .codex/project/lifecycle/<trace>.json.")
    parser.add_argument("--format", choices=("text", "json"), default="text", help="Output format.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Route AI maintenance tasks through an executable lifecycle.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    classify = subparsers.add_parser("classify", help="Classify input and print lifecycle routing.")
    add_common_args(classify)

    preflight = subparsers.add_parser("preflight", help="Validate pre-execution lifecycle requirements.")
    add_common_args(preflight)

    finalize = subparsers.add_parser("finalize", help="Run or plan final lifecycle gates.")
    add_common_args(finalize)
    finalize.add_argument("--run-gates", action="store_true", help="Run ai_rules.py gate-pool and doc-index checks.")
    finalize.add_argument("--dry-run", action="store_true", help="Print final gate commands without running them.")

    status = subparsers.add_parser("status", help="Read a lifecycle state file.")
    status.add_argument("--root", default=".", help="Repository root. Default: current directory.")
    status.add_argument("--state-file", required=True, help="Lifecycle state JSON path.")
    status.add_argument("--format", choices=("text", "json"), default="text", help="Output format.")
    return parser.parse_args()


def default_trace_id() -> str:
    return f"{DEFAULT_TRACE_PREFIX}-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"


def state_path_for(args: argparse.Namespace, root: Path, trace_id: str) -> Path:
    if getattr(args, "state_file", None):
        path = Path(args.state_file)
        return path if path.is_absolute() else root / path
    return root / STATE_DIR / f"{trace_id}.json"


def validate_tracking(
    root: Path,
    args: argparse.Namespace,
    classification: Classification,
    errors: list[Finding],
    warnings: list[Finding],
) -> None:
    tracking_arg = getattr(args, "task_tracking", None)
    if classification.requires_tracking and not tracking_arg:
        errors.append(Finding("error", "task tracking is required for this lifecycle route."))
        return
    if not tracking_arg:
        return
    tracking = Path(tracking_arg)
    if not tracking.is_absolute():
        tracking = root / tracking
    if not tracking.exists():
        errors.append(Finding("error", f"task tracking file does not exist: {rel_path(tracking, root)}"))
        return
    text = read_text_file(tracking)
    required_sections = ["用户要求追踪门禁", "要求触发日志", "任务类型门禁"]
    if "rules-script" in classification.task_types:
        required_sections.extend(["联网核对记录", "脚本能力适配门禁"])
    if "docs" in classification.task_types:
        required_sections.extend(["影响面扫描", "Definition of Done"])
    for heading in required_sections:
        if not re.search(rf"^##\s+{re.escape(heading)}\s*$", text, re.MULTILINE):
            errors.append(Finding("error", f"task tracking lacks section: {heading}"))
    label = getattr(args, "approved_label", None)
    if classification.requires_approval:
        if not label:
            warnings.append(Finding("warning", "approval is required but --approved-label was not provided."))
        elif label not in text:
            warnings.append(Finding("warning", f"approval label is not mirrored in tracking: {label}"))


def build_report(args: argparse.Namespace, phase: str, state: str) -> LifecycleReport:
    root = Path(args.root).resolve()
    trace_id = args.trace_id or default_trace_id()
    input_record, classification = build_classification(args, root)
    errors: list[Finding] = []
    warnings: list[Finding] = []
    notes: list[Finding] = []

    if phase in {"preflight", "finalize"}:
        validate_tracking(root, args, classification, errors, warnings)
    if input_record.needs_citation and not input_record.derived_from:
        warnings.append(Finding("warning", "web input should record --derived-from source URL(s)."))
    if classification.task_size == "large":
        notes.append(Finding("note", "large task: use tracking, explicit gates, and avoid broad unplanned edits."))
    elif classification.task_size == "small":
        notes.append(Finding("note", "small task: lightweight answer path is acceptable unless files change."))

    report = LifecycleReport(
        schema_version=SCHEMA_VERSION,
        trace_id=trace_id,
        phase=phase,
        state=state,
        updated_at=utc_now(),
        input=input_record,
        classification=classification,
        errors=errors,
        warnings=warnings,
        notes=notes,
    )
    return report


def final_gate_commands(args: argparse.Namespace, report: LifecycleReport) -> list[list[str]]:
    root = Path(args.root).resolve()
    py = sys.executable
    entrypoint = ai_rules_entrypoint()
    commands: list[list[str]] = []
    changed_paths = report.classification.changed_paths
    if "docs" in report.classification.task_types or any(Path(path).suffix.lower() == ".md" for path in changed_paths):
        commands.append(
            [
                py,
                str(entrypoint),
                "doc-index",
                "check",
                "--root",
                str(root),
                "--rebuild",
                *sum((["--changed-path", path] for path in changed_paths), []),
                "--format",
                "text",
            ]
        )
    if args.task_tracking:
        gate_command = [
            py,
            str(entrypoint),
            "gate-pool",
            "--root",
            str(root),
            "--task-tracking",
            args.task_tracking,
            "--trace-id",
            report.trace_id,
            "--final",
        ]
        for task_type in report.classification.task_types:
            gate_command.extend(["--task-type", task_type])
        for path in changed_paths:
            gate_command.extend(["--changed-path", path])
        commands.append(gate_command)
    return commands


def save_state(report: LifecycleReport, args: argparse.Namespace) -> None:
    root = Path(args.root).resolve()
    path = state_path_for(args, root, report.trace_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = asdict(report)
    data["state_file"] = rel_path(path, root)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report.state_file = rel_path(path, root)


def run_command(command: list[str], root: Path) -> tuple[int, str]:
    env = os.environ.copy()
    env["PYTHONPYCACHEPREFIX"] = str(root / PYTHON_PYCACHE_DIR)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    proc = subprocess.run(
        command,
        cwd=root,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return proc.returncode, proc.stdout


def run_final_gates(args: argparse.Namespace, report: LifecycleReport) -> None:
    root = Path(args.root).resolve()
    commands = final_gate_commands(args, report)
    report.gate_commands = commands
    if args.dry_run or not args.run_gates:
        return
    for command in commands:
        code, output = run_command(command, root)
        if code != 0:
            report.errors.append(
                Finding(
                    "error",
                    f"final gate failed with exit {code}: {' '.join(command)}\n{output.strip()}",
                )
            )
        else:
            report.notes.append(
                Finding("note", f"final gate passed: {' '.join(command)}\n{summarize(output, 360)}")
            )


def format_json(report: LifecycleReport) -> str:
    return json.dumps(asdict(report), ensure_ascii=False, indent=2, sort_keys=True)


def format_text(report: LifecycleReport) -> str:
    c = report.classification
    lines = [
        f"Lifecycle phase: {report.phase}",
        f"State: {report.state}",
        f"Trace: {report.trace_id}",
        f"Input: {report.input.source} ({report.input.trust})",
        f"Needs citation: {str(report.input.needs_citation).lower()}",
        f"Task types: {', '.join(c.task_types) if c.task_types else 'none'}",
        f"Task size: {c.task_size}",
        f"Task size reasons: {'; '.join(c.task_size_reasons)}",
        f"Requires tracking: {str(c.requires_tracking).lower()}",
        f"Requires approval: {str(c.requires_approval).lower()}",
        f"Registered mechanisms: {', '.join(c.required_hooks)}",
        f"Registered gates: {', '.join(c.required_gates) if c.required_gates else 'none'}",
    ]
    if c.changed_paths:
        lines.append(f"Changed paths: {', '.join(c.changed_paths)}")
    if report.state_file:
        lines.append(f"State file: {report.state_file}")
    if report.gate_commands:
        lines.append("Gate commands:")
        for command in report.gate_commands:
            lines.append(f"  - {' '.join(command)}")
    for title, findings in (("Errors", report.errors), ("Warnings", report.warnings), ("Notes", report.notes)):
        lines.append(f"{title}: {len(findings)}")
        for finding in findings:
            lines.append(f"  - {finding.message}")
    return "\n".join(lines)


def read_status(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    path = Path(args.state_file)
    if not path.is_absolute():
        path = root / path
    data: Any = json.loads(path.read_text(encoding="utf-8"))
    if args.format == "json":
        print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"Lifecycle state: {data.get('state')}")
        print(f"Phase: {data.get('phase')}")
        print(f"Trace: {data.get('trace_id')}")
        classification = data.get("classification", {})
        print(f"Task size: {classification.get('task_size')}")
        print(f"Task types: {', '.join(classification.get('task_types', []))}")
    return 0


def main() -> int:
    args = parse_args()
    if args.command == "status":
        return read_status(args)

    phase = args.command
    state_by_phase = {
        "classify": "classified",
        "preflight": "preflight_checked",
        "finalize": "finalizing",
    }
    report = build_report(args, phase=phase, state=state_by_phase[phase])
    if args.command == "finalize":
        run_final_gates(args, report)
        if not report.errors:
            report.state = "finalized" if args.run_gates else "final_gate_planned"
    save_state(report, args)
    print(format_json(report) if args.format == "json" else format_text(report))
    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

