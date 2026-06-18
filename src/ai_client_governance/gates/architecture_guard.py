#!/usr/bin/env python3
"""Validate the project-local AI Client Governance architecture boundaries."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path

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
NATIVE_PROJECT_SKILLS_DIR = Path("skills")
AI_CLIENT_GOVERNANCE_SKILLS_DIR = AI_CLIENT_ROOT / "ai-client-governance" / "skills"

REQUIRED_PROJECT_PATHS = [
    PROJECT_DIR / "records",
    PROJECT_DIR / "agents",
    PROJECT_DIR / "logs",
    PROJECT_DIR / "state",
    PROJECT_DIR / "tools",
    PROJECT_RULES_ENTRY,
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
    ai_client = root / AI_CLIENT_ROOT
    if not ai_client.exists():
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

    if (root / "scripts").exists():
        errors.append(Finding("error", "root scripts directory must not exist", "scripts"))
    if (root / ".codex" / "cache").exists():
        errors.append(Finding("error", "top-level .codex/cache must not exist; use .ai-client/project/cache", ".codex/cache"))

    for path in REQUIRED_PROJECT_PATHS:
        if not (root / path).exists():
            errors.append(Finding("error", f"required project path is missing: {path.as_posix()}", path.as_posix()))

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

    return {
        "root": root.as_posix(),
        "required_ai_client_top": sorted(REQUIRED_AI_CLIENT_TOP),
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
    }


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
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_text(report))
    has_errors = bool(report["errors"])
    has_warnings = bool(report["warnings"])
    return 1 if has_errors or (args.strict and has_warnings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
