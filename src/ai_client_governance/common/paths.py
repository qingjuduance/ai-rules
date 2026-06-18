#!/usr/bin/env python3
"""Shared project-local paths for ai-client-governance maintenance scripts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


DEFAULT_AI_CLIENT_ROOT = Path(".ai-client")
CONFIG_FILE_NAME = "ai-client-governance-config.json"
COMMON_REPO_NAME = "ai-client-governance"


def _candidate_project_roots(start: Path) -> list[Path]:
    """Return likely host project roots from the current working directory."""
    roots: list[Path] = []
    current = start.resolve()
    for candidate in (current, *current.parents):
        if candidate not in roots:
            roots.append(candidate)
    return roots


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _expand_layout_value(value: str, values: dict[str, str]) -> str:
    expanded = value
    for key, replacement in values.items():
        expanded = expanded.replace("${" + key + "}", replacement)
    return expanded


def _configured_layout(start: Path | None = None) -> dict[str, str]:
    """Load the host path layout.

    The neutral workspace is `.ai-client/`. There is no `.codex` fallback:
    native client adapters should point here instead of preserving old layouts.
    """
    start = start or Path.cwd()
    for root in _candidate_project_roots(start):
        for config_path in (root / DEFAULT_AI_CLIENT_ROOT / CONFIG_FILE_NAME,):
            if not config_path.exists():
                continue
            data = _read_json(config_path)
            raw_layout = data.get("layout") if isinstance(data.get("layout"), dict) else {}
            values = {
                "aiClientRoot": str(raw_layout.get("aiClientRoot") or data.get("aiClientRoot") or DEFAULT_AI_CLIENT_ROOT),
                "commonRepoName": str(raw_layout.get("commonRepoName") or COMMON_REPO_NAME),
            }
            common_repo_path = str(
                raw_layout.get("commonRepoPath")
                or data.get("embeddedRepoPath")
                or "${aiClientRoot}/${commonRepoName}"
            )
            project_path = str(
                raw_layout.get("projectPath")
                or data.get("projectEntry", "${aiClientRoot}/project/rules/project/AGENTS.md")
            )
            if project_path.endswith("/rules/project/AGENTS.md") or project_path.endswith("\\rules\\project\\AGENTS.md"):
                project_path = str(Path(project_path).parent.parent.parent)
            values["commonRepoPath"] = _expand_layout_value(common_repo_path, values).replace("\\", "/")
            values["projectPath"] = _expand_layout_value(project_path, values).replace("\\", "/")
            values["configPath"] = config_path.relative_to(root).as_posix()
            return values

    for root in _candidate_project_roots(start):
        if (root / DEFAULT_AI_CLIENT_ROOT / "project").exists() or (
            root / DEFAULT_AI_CLIENT_ROOT / COMMON_REPO_NAME
        ).exists():
            return {
                "aiClientRoot": DEFAULT_AI_CLIENT_ROOT.as_posix(),
                "commonRepoName": COMMON_REPO_NAME,
                "commonRepoPath": (DEFAULT_AI_CLIENT_ROOT / COMMON_REPO_NAME).as_posix(),
                "projectPath": (DEFAULT_AI_CLIENT_ROOT / "project").as_posix(),
                "configPath": (DEFAULT_AI_CLIENT_ROOT / CONFIG_FILE_NAME).as_posix(),
            }

    return {
        "aiClientRoot": DEFAULT_AI_CLIENT_ROOT.as_posix(),
        "commonRepoName": COMMON_REPO_NAME,
        "commonRepoPath": (DEFAULT_AI_CLIENT_ROOT / COMMON_REPO_NAME).as_posix(),
        "projectPath": (DEFAULT_AI_CLIENT_ROOT / "project").as_posix(),
        "configPath": (DEFAULT_AI_CLIENT_ROOT / CONFIG_FILE_NAME).as_posix(),
    }


LAYOUT = _configured_layout()
AI_CLIENT_ROOT = Path(LAYOUT["aiClientRoot"])
COMMON_REPO_PATH = Path(LAYOUT["commonRepoPath"])
CONFIG_PATH = Path(LAYOUT["configPath"])
PROJECT_DIR = Path(LAYOUT["projectPath"])

PROJECT_RULES_DIR = PROJECT_DIR / "rules"
PROJECT_RULES_PROJECT_DIR = PROJECT_RULES_DIR / "project"
PROJECT_RULES_ENTRY = PROJECT_RULES_PROJECT_DIR / "AGENTS.md"
PROJECT_SKILLS_DIR = PROJECT_DIR / "skills"

RECORDS_DIR = PROJECT_DIR / "records"
TASK_TRACKING_DIR = RECORDS_DIR / "task-tracking"
PENDING_TASKS_DIR = RECORDS_DIR / "pending-tasks"
CORRECTIONS_DIR = RECORDS_DIR / "corrections"
PROJECT_STATUS_DIR = RECORDS_DIR / "project-status"

AGENTS_DIR = PROJECT_DIR / "agents"
AGENT_BRIEFS_DIR = AGENTS_DIR / "briefs"
AGENT_COMM_DIR = AGENTS_DIR / "comm"
AGENT_GROUPS_DIR = AGENTS_DIR / "groups"

LOGS_DIR = PROJECT_DIR / "logs"
TOOL_INVOCATIONS_DIR = LOGS_DIR / "tool-invocations"

STATE_DIR = PROJECT_DIR / "state"
STRUCTURED_DB_PATH = STATE_DIR / "aicg.db"

CACHE_DIR = PROJECT_DIR / "cache"
PYTHON_PYCACHE_DIR = CACHE_DIR / "python-pycache"
TMP_DIR = PROJECT_DIR / "tmp"

PENDING_INDEX = PENDING_TASKS_DIR / "index.md"
CORRECTIONS_INDEX = CORRECTIONS_DIR / "index.md"
AGENT_GROUP_STATUS = AGENT_GROUPS_DIR / "current-status.json"


def as_posix_prefix(path: Path) -> str:
    return path.as_posix().rstrip("/") + "/"


def normalized_rel(value: str | Path) -> str:
    return str(value).replace("\\", "/").lstrip("./")


def starts_with_any(value: str | Path, roots: tuple[Path, ...]) -> bool:
    rel = normalized_rel(value)
    return any(rel.startswith(as_posix_prefix(root)) for root in roots)


def is_task_tracking_path(value: str | Path) -> bool:
    return starts_with_any(value, (TASK_TRACKING_DIR,))


def is_pending_path(value: str | Path) -> bool:
    return starts_with_any(value, (PENDING_TASKS_DIR,))


def is_correction_path(value: str | Path) -> bool:
    return starts_with_any(value, (CORRECTIONS_DIR,))


def first_existing(root: Path, *relative_paths: Path) -> Path:
    for relative in relative_paths:
        candidate = root / relative
        if candidate.exists():
            return candidate
    return root / relative_paths[0]


def ai_client_governance_root() -> Path:
    """Return the embedded ai-client-governance repository root from inside the package."""
    return Path(__file__).resolve().parents[3]


def ai_client_governance_scripts_dir() -> Path:
    """Return the public script entry directory."""
    return ai_client_governance_root() / "scripts"


def ai_client_governance_entrypoint() -> Path:
    """Return the single public Python CLI entrypoint."""
    return ai_client_governance_scripts_dir() / "ai_client_governance.py"


def ai_client_governance_src_dir() -> Path:
    """Return the package source root."""
    return ai_client_governance_root() / "src"
