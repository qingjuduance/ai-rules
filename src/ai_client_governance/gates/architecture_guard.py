#!/usr/bin/env python3
"""Validate the project-local AI Client Governance architecture boundaries."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from ai_client_governance.audit import file_ownership
from ai_client_governance.common.paths import AI_CLIENT_ROOT, PROJECT_DIR, PROJECT_RULES_ENTRY, PROJECT_SKILLS_DIR


REQUIRED_AI_CLIENT_TOP = {"ai-client-governance", "ai-client-governance-config.json", "project"}
FORBIDDEN_CODEX_TOP = {
    "ai-client-governance",
    "ai-client-governance-config.json",
    "project",
    "rules",
    "skills",
    "cache",
    "tmp",
    "task-tracking",
    "pending-tasks",
    "corrections",
    "project-status",
    "agent-briefs",
    "agent-comm",
    "agent-groups",
    "tool-invocations",
}
LEGACY_FALLBACK_PATTERNS = (
    ".codex/rules/common",
    ".codex/project",
    ".codex/ai-client-governance",
    ".codex/skills",
    "--queue-file",
    "fallbackmode",
    "fallbackmethod",
    "legacy markdown fallback",
    "legacy json fallback",
    "json fallback",
    "markdown fallback",
    "fallback reader",
    "fallback writer",
    "compatibility reader",
    "compatibility writer",
    "backward-compatible fallback",
    "default fallback",
    "fallback path",
)
LEGACY_FALLBACK_ALLOWLIST = (
    "history",
    "historical",
    "audit",
    "export",
    "forbidden",
    "not a fallback",
    "not supported",
    "not-generated-by-default",
    "not as fallback",
    "no fallback",
    "no .codex fallback",
    "must not",
    "do not",
    "older",
    "old layout",
    "old .codex governance layout must be removed",
    "不是",
    "不作为",
    "不能",
    "不得",
    "不要",
    "不再",
    "不再作为",
    "不再用",
    "不支持",
    "不兼容",
    "已删除",
    "旧布局",
    "残留",
    "迁移或删除",
)
NATIVE_PROJECT_SKILLS_DIR = Path("skills")
AI_CLIENT_GOVERNANCE_SKILLS_DIR = AI_CLIENT_ROOT / "ai-client-governance" / "skills"

REQUIRED_PROJECT_PATHS = [
    PROJECT_DIR / "records",
    PROJECT_DIR / "agents",
    PROJECT_DIR / "tools",
    PROJECT_RULES_ENTRY,
]
GENERATED_PROJECT_PATHS = [
    PROJECT_DIR / "cache",
    PROJECT_DIR / "tmp",
    PROJECT_DIR / "logs",
    PROJECT_DIR / "state",
    PROJECT_DIR / ".worktree",
    PROJECT_DIR / "doc-index",
    PROJECT_DIR / "lifecycle",
    PROJECT_DIR / "agents" / "comm" / "groups",
    PROJECT_DIR / "agents" / "groups",
]
PROJECT_ROOT_AGENTS = Path("AGENTS.md")
ADAPTER_REQUIRED_MARKERS = (
    ".ai-client/ai-client-governance/AGENTS.md",
    ".ai-client/project/rules/project/AGENTS.md",
)
NATIVE_RULE_ADAPTERS = [
    Path("AGENTS.md"),
    Path("CLAUDE.md"),
    Path("GEMINI.md"),
    Path("CONVENTIONS.md"),
    Path(".github") / "copilot-instructions.md",
    Path(".github") / "instructions" / "ai-client-governance.instructions.md",
    Path(".cursor") / "rules" / "ai-client-governance.mdc",
    Path(".clinerules") / "ai-client-governance.md",
    Path(".windsurf") / "rules" / "ai-client-governance.md",
    Path(".continue") / "rules" / "ai-client-governance.md",
    Path(".roo") / "rules" / "ai-client-governance.md",
    Path(".trae") / "rules" / "ai-client-governance.md",
]


@dataclass
class Finding:
    level: str
    message: str
    path: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check AI Client Governance architecture boundaries.")
    parser.add_argument("--root", default=".", help="Repository root. Default: current directory.")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero on warnings.")
    parser.add_argument("--allow-config-file", action="store_true", default=True)
    parser.add_argument(
        "--check-no-legacy-fallback",
        action="store_true",
        help="Fail on unsupported legacy governance fallback references outside explicit history/export wording.",
    )
    return parser.parse_args()


def rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def build_report(root: Path) -> dict[str, object]:
    errors: list[Finding] = []
    warnings: list[Finding] = []
    notes: list[str] = []
    governance_repo_root = (root / "manifest.json").exists() and (root / "src" / "ai_client_governance").exists()
    ai_client = root / AI_CLIENT_ROOT
    if governance_repo_root:
        ai_client_top_entries = []
        notes.append("root kind: ai-client-governance repository")
    elif not ai_client.exists():
        errors.append(Finding("error", ".ai-client directory is missing", AI_CLIENT_ROOT.as_posix()))
        ai_client_top_entries: list[str] = []
    else:
        ai_client_top_entries = sorted(item.name for item in ai_client.iterdir())
        for name in sorted(REQUIRED_AI_CLIENT_TOP):
            if not (ai_client / name).exists():
                errors.append(
                    Finding(
                        "error",
                        f"required .ai-client top-level entry is missing: {name}",
                        f".ai-client/{name}",
                    )
                )

    codex = root / ".codex"
    codex_top_entries = sorted(item.name for item in codex.iterdir()) if codex.exists() else []
    if codex.exists():
        for name in codex_top_entries:
            level = "error" if name in FORBIDDEN_CODEX_TOP else "warning"
            finding = Finding(
                level,
                f"old .codex governance layout must be removed, found top-level entry: {name}",
                f".codex/{name}",
            )
            (errors if level == "error" else warnings).append(finding)

    if (root / "scripts").exists() and not governance_repo_root:
        errors.append(Finding("error", "root scripts directory must not exist", "scripts"))
    if (root / ".codex" / "cache").exists():
        errors.append(Finding("error", "top-level .codex/cache must not exist; use .ai-client/project/cache", ".codex/cache"))

    for path in REQUIRED_PROJECT_PATHS:
        if not governance_repo_root and not (root / path).exists():
            errors.append(Finding("error", f"required project path is missing: {path.as_posix()}", path.as_posix()))
    generated_present = [path.as_posix() for path in GENERATED_PROJECT_PATHS if (root / path).exists()]
    generated_absent = [path.as_posix() for path in GENERATED_PROJECT_PATHS if not (root / path).exists()]
    notes.append(
        f"generated runtime paths: present={len(generated_present)}, absent-generated-on-demand={len(generated_absent)}"
    )

    root_agents = root / PROJECT_ROOT_AGENTS
    if not root_agents.exists():
        errors.append(Finding("error", "project root AGENTS.md adapter is missing", PROJECT_ROOT_AGENTS.as_posix()))
    else:
        text = root_agents.read_text(encoding="utf-8", errors="replace")
        notes.append(f"root AGENTS adapter: {rel(root_agents, root)}")
        for marker in ADAPTER_REQUIRED_MARKERS:
            if marker not in text:
                warnings.append(
                    Finding(
                        "warning",
                        f"project root AGENTS.md adapter lacks required read-order marker: {marker}",
                        PROJECT_ROOT_AGENTS.as_posix(),
                    )
                )

    existing_adapters: list[str] = []
    for adapter in NATIVE_RULE_ADAPTERS:
        adapter_path = root / adapter
        if not adapter_path.exists() or adapter == PROJECT_ROOT_AGENTS:
            continue
        existing_adapters.append(adapter.as_posix())
        text = adapter_path.read_text(encoding="utf-8", errors="replace")
        for marker in ADAPTER_REQUIRED_MARKERS:
            if marker not in text:
                warnings.append(
                    Finding(
                        "warning",
                        f"native AI rule adapter lacks required read-order marker: {marker}",
                        adapter.as_posix(),
                    )
                )

    project_rules = root / PROJECT_RULES_ENTRY
    if project_rules.exists():
        notes.append(f"project rules entry: {rel(project_rules, root)}")
    native_project_skills = root / NATIVE_PROJECT_SKILLS_DIR
    project_skills = root / PROJECT_SKILLS_DIR
    ai_client_governance_skills = root / AI_CLIENT_GOVERNANCE_SKILLS_DIR
    if native_project_skills.exists():
        native_skill_names = sorted(item.name for item in native_project_skills.iterdir() if item.is_dir())
        notes.append(f"native project skills: {len(native_skill_names)}")
    else:
        native_skill_names = []
        notes.append("native project skills: 0 (directory absent)")
    if project_skills.exists():
        project_skill_names = sorted(item.name for item in project_skills.iterdir() if item.is_dir())
        skill_count = len(project_skill_names)
        notes.append(f"project skills: {skill_count}")
    else:
        project_skill_names = []
        notes.append("project skills: 0 (directory absent)")
    if ai_client_governance_skills.exists():
        ai_client_governance_skill_names = sorted(item.name for item in ai_client_governance_skills.iterdir() if item.is_dir())
        notes.append(f"ai-client-governance skills: {len(ai_client_governance_skill_names)}")
    else:
        ai_client_governance_skill_names = []

    native_project_duplicates = sorted(set(native_skill_names) & set(project_skill_names))
    native_ai_client_governance_duplicates = sorted(set(native_skill_names) & set(ai_client_governance_skill_names))
    project_ai_client_governance_duplicates = sorted(set(project_skill_names) & set(ai_client_governance_skill_names))

    for name in native_project_duplicates:
        warnings.append(
            Finding(
                "warning",
                "native project skill shadows .ai-client/project specialization; native asset has highest priority and requires explicit approval to modify",
                f"{NATIVE_PROJECT_SKILLS_DIR.as_posix()}/{name}",
            )
        )
    for name in native_ai_client_governance_duplicates:
        warnings.append(
            Finding(
                "warning",
                "native project skill shadows ai-client-governance skill; native asset has highest priority and must not be overwritten",
                f"{NATIVE_PROJECT_SKILLS_DIR.as_posix()}/{name}",
            )
        )
    for name in project_ai_client_governance_duplicates:
        warnings.append(
            Finding(
                "warning",
                ".ai-client/project skill shadows ai-client-governance skill; project specialization wins after native assets and conflict must be reviewed",
                f"{PROJECT_SKILLS_DIR.as_posix()}/{name}",
            )
        )

    for base, names, label in [
        (native_project_skills, native_skill_names, "native project"),
        (project_skills, project_skill_names, "project"),
        (ai_client_governance_skills, ai_client_governance_skill_names, "ai-client-governance"),
    ]:
        for name in names:
            if not (base / name / "SKILL.md").exists():
                warnings.append(
                    Finding(
                        "warning",
                        f"{label} skill lacks SKILL.md: {name}",
                        rel(base / name, root),
                    )
                )

    file_report = {"tracked_total": 0, "ignored_untracked_count": 0, "gitignore": {"status": "not-applicable"}}
    if not governance_repo_root:
        file_report = file_ownership.build_report(root)
    for item in file_report.get("errors", []):
        if isinstance(item, dict):
            errors.append(
                Finding(
                    "error",
                    "file ownership audit: " + str(item.get("message", "")),
                    str(item.get("path", "")),
                )
            )
    for item in file_report.get("warnings", []):
        if isinstance(item, dict):
            warnings.append(
                Finding(
                    "warning",
                    "file ownership audit: " + str(item.get("message", "")),
                    str(item.get("path", "")),
                )
            )
    notes.append(
        "file ownership: "
        f"tracked={file_report.get('tracked_total', 0)}, "
        f"ignored={file_report.get('ignored_untracked_count', 0)}, "
        f"gitignore={file_report.get('gitignore', {}).get('status', 'unknown')}"
    )

    return {
        "root": root.as_posix(),
        "required_ai_client_top": sorted(REQUIRED_AI_CLIENT_TOP),
        "required_project_paths": [path.as_posix() for path in REQUIRED_PROJECT_PATHS],
        "generated_project_paths": {
            "present": generated_present,
            "absent_generated_on_demand": generated_absent,
        },
        "ai_client_top": ai_client_top_entries,
        "forbidden_codex_top": sorted(FORBIDDEN_CODEX_TOP),
        "codex_top": codex_top_entries,
        "errors": [asdict(item) for item in errors],
        "warnings": [asdict(item) for item in warnings],
        "notes": notes,
        "priority_order": ["native-project-assets", "project-specialization", "ai-client-governance-common"],
        "native_rule_adapters": [path.as_posix() for path in NATIVE_RULE_ADAPTERS],
        "existing_native_rule_adapters": existing_adapters,
        "native_project_skill_names": native_skill_names,
        "project_skill_names": project_skill_names,
        "ai_client_governance_skill_names": ai_client_governance_skill_names,
        "duplicate_skill_names": sorted(set(native_project_duplicates + native_ai_client_governance_duplicates + project_ai_client_governance_duplicates)),
        "duplicate_skill_breakdown": {
            "native_project": native_project_duplicates,
            "native_ai_client_governance": native_ai_client_governance_duplicates,
            "project_ai_client_governance": project_ai_client_governance_duplicates,
        },
        "file_ownership_audit": file_report,
    }


def check_no_legacy_fallback(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    scan_roots = [
        root / "AGENTS.md",
        root / "README.md",
        root / "manifest.json",
        root / "src",
        root / "skills",
    ]
    embedded = root / AI_CLIENT_ROOT / "ai-client-governance"
    if embedded.exists():
        scan_roots.extend(
            [
                embedded / "AGENTS.md",
                embedded / "README.md",
                embedded / "manifest.json",
                embedded / "src",
                embedded / "skills",
            ]
        )
    files: list[Path] = []
    for item in scan_roots:
        if item.is_file():
            files.append(item)
        elif item.is_dir():
            files.extend(
                path
                for path in item.rglob("*")
                if path.is_file() and path.suffix.lower() in {".py", ".md", ".json", ".yaml", ".yml"}
            )
    for path in sorted(set(files)):
        current_rel = rel(path, root)
        if current_rel.endswith("src/ai_client_governance/validation/selftest.py"):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        in_pattern_definition = False
        lines = text.splitlines()
        for line_no, line in enumerate(lines, start=1):
            if current_rel.endswith("src/ai_client_governance/gates/architecture_guard.py"):
                if "LEGACY_FALLBACK_PATTERNS" in line:
                    in_pattern_definition = True
                if in_pattern_definition:
                    if line.strip() == ")":
                        in_pattern_definition = False
                    continue
            lowered = line.lower()
            if not any(pattern in lowered for pattern in LEGACY_FALLBACK_PATTERNS):
                continue
            context = "\n".join(lines[max(0, line_no - 3) : min(len(lines), line_no + 2)]).lower()
            if any(marker in context for marker in LEGACY_FALLBACK_ALLOWLIST):
                continue
            findings.append(
                Finding(
                    "error",
                    "unsupported legacy/fallback reference needs cleanup or explicit history/export allowlist",
                    f"{rel(path, root)}:{line_no}",
                )
            )
    return findings


def render_text(report: dict[str, object]) -> str:
    errors = report["errors"]
    warnings = report["warnings"]
    lines = [
        "AI Client Governance Architecture Guard",
        f"Root: {report['root']}",
        "Priority: " + " > ".join(report["priority_order"]),
        "Native rule adapters: " + ", ".join(report["existing_native_rule_adapters"]),
        "Required .ai-client top: " + ", ".join(report["required_ai_client_top"]),
        "Actual .ai-client top: " + ", ".join(report["ai_client_top"]),
        "Forbidden .codex top present: " + (", ".join(report["codex_top"]) or "none"),
        f"Errors: {len(errors)}",
    ]
    for item in errors:
        lines.append(f"  - {item['message']} [{item.get('path', '')}]")
    lines.append(f"Warnings: {len(warnings)}")
    for item in warnings:
        lines.append(f"  - {item['message']} [{item.get('path', '')}]")
    for note in report["notes"]:
        lines.append(f"Note: {note}")
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    root = Path(args.root).resolve()
    report = build_report(root)
    if args.check_no_legacy_fallback:
        legacy_findings = check_no_legacy_fallback(root)
        report["no_legacy_fallback"] = {
            "status": "pass" if not legacy_findings else "fail",
            "findings": [asdict(item) for item in legacy_findings],
        }
        report["errors"].extend(asdict(item) for item in legacy_findings)
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_text(report))
    has_errors = bool(report["errors"])
    has_warnings = bool(report["warnings"])
    return 1 if has_errors or (args.strict and has_warnings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
