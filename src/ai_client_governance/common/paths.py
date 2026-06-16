#!/usr/bin/env python3
"""Shared project-local paths for ai-client-governance maintenance scripts."""

from __future__ import annotations

from pathlib import Path


PROJECT_DIR = Path(".codex") / "project"

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
AI_CLIENT_GOVERNANCE_STATE_PATH = STATE_DIR / "ai-client-governance-state.json"

CACHE_DIR = PROJECT_DIR / "cache"
PYTHON_PYCACHE_DIR = CACHE_DIR / "python-pycache"
TMP_DIR = PROJECT_DIR / "tmp"

LEGACY_TASK_TRACKING_DIR = Path(".codex") / "task-tracking"
LEGACY_RULES_DIR = Path(".codex") / "rules"
LEGACY_PROJECT_RULES_DIR = LEGACY_RULES_DIR / "project"
LEGACY_PROJECT_RULES_ENTRY = LEGACY_PROJECT_RULES_DIR / "AGENTS.md"
LEGACY_SKILLS_DIR = Path(".codex") / "skills"
LEGACY_PENDING_TASKS_DIR = Path(".codex") / "pending-tasks"
LEGACY_CORRECTIONS_DIR = Path(".codex") / "corrections"
LEGACY_PROJECT_STATUS_DIR = Path(".codex") / "project-status"
LEGACY_AGENT_BRIEFS_DIR = Path(".codex") / "agent-briefs"
LEGACY_AGENT_COMM_DIR = Path(".codex") / "agent-comm"
LEGACY_AGENT_GROUPS_DIR = Path(".codex") / "agent-groups"
LEGACY_TOOL_INVOCATIONS_DIR = Path(".codex") / "tool-invocations"
LEGACY_AI_CLIENT_GOVERNANCE_STATE_PATH = Path(".codex") / "ai-client-governance-state.json"
LEGACY_CACHE_DIR = Path(".codex") / "cache"
LEGACY_TMP_DIR = Path(".codex") / "tmp"

PENDING_INDEX = PENDING_TASKS_DIR / "index.md"
CORRECTIONS_INDEX = CORRECTIONS_DIR / "index.md"
AGENT_GROUP_STATUS = AGENT_GROUPS_DIR / "current-status.json"

LEGACY_PENDING_INDEX = LEGACY_PENDING_TASKS_DIR / "index.md"
LEGACY_CORRECTIONS_INDEX = LEGACY_CORRECTIONS_DIR / "index.md"
LEGACY_AGENT_GROUP_STATUS = LEGACY_AGENT_GROUPS_DIR / "current-status.json"


def as_posix_prefix(path: Path) -> str:
    return path.as_posix().rstrip("/") + "/"


def normalized_rel(value: str | Path) -> str:
    return str(value).replace("\\", "/").lstrip("./")


def starts_with_any(value: str | Path, roots: tuple[Path, ...]) -> bool:
    rel = normalized_rel(value)
    return any(rel.startswith(as_posix_prefix(root)) for root in roots)


def is_task_tracking_path(value: str | Path) -> bool:
    return starts_with_any(value, (TASK_TRACKING_DIR, LEGACY_TASK_TRACKING_DIR))


def is_pending_path(value: str | Path) -> bool:
    return starts_with_any(value, (PENDING_TASKS_DIR, LEGACY_PENDING_TASKS_DIR))


def is_correction_path(value: str | Path) -> bool:
    return starts_with_any(value, (CORRECTIONS_DIR, LEGACY_CORRECTIONS_DIR))


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

