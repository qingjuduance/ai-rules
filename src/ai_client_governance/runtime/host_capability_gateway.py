"""Non-invasive host capability gateway checks for CLI entrypoints.

The gateway is deliberately DB-only: it verifies task context and records
capability facts in the task-record database when a task row already exists.
It does not modify shell profiles, PATH, terminal startup, or global host state.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_client_governance.common.time_utils import now_iso
from ai_client_governance.records import state_store

CAPABILITY_GATEWAY_EVENT = "capability-gateway.facts"
TASK_ID_RE = re.compile(r"(TASK-[A-Za-z0-9][A-Za-z0-9_.-]*)")


@dataclass(frozen=True)
class GatewayResult:
    task_id: str
    task_exists: bool
    event_id: str
    db_path: Path


def project_db_path(project_root: Path, override: str | None = None) -> Path:
    return state_store.db_path(project_root, override)


def infer_task_id(*, task_id: str | None = None, task_tracking: str | Path | None = None) -> str:
    explicit = str(task_id or "").strip()
    if explicit:
        return explicit
    if task_tracking:
        candidates = [str(task_tracking), Path(str(task_tracking)).stem]
        for candidate in candidates:
            match = TASK_ID_RE.search(candidate)
            if match:
                return match.group(1)
    return ""


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    row = con.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
    return row is not None


def _task_exists(con: sqlite3.Connection, task_id: str) -> bool:
    if not _table_exists(con, "tasks"):
        return False
    row = con.execute("SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    return row is not None


def gateway_payload(*, command_name: str, join_point: str, task_id: str) -> dict[str, Any]:
    return {
        "join_point": join_point,
        "command_name": command_name,
        "task_id": task_id,
        "capability_fact_kind": "registration",
        "control_layer": "plugin",
        "enforcement_level": "audit_only",
        "hard_enforcement_available": False,
        "registration_event": True,
        "invocation_telemetry_required": True,
        "residual_risk": (
            "This event proves the governance entrypoint recorded a task-scoped capability "
            "boundary. It does not prove that host-native shell/tool calls outside the "
            "governed wrapper were intercepted."
        ),
        "lifecycle_input_filter_enforced": True,
        "prewrite_runtime_adapter": "approved-task-worktree-or-queue-task",
        "runtime_adapter_components": [
            "client.runtime.host-capability-gateway",
            "input.filter.user-message-preflight",
            "prewrite.gate.approved-task-worktree",
            "preflight.interceptor.raw-shell-coverage",
        ],
        "shell_enforcement_mode": "non-invasive-command-proxy",
        "shell_control_layer": "plugin-command-wrapper",
        "shell_enforcement_scope": "governed_commands_only",
        "raw_host_shell_interception": False,
        "profile_policy": "no_profile",
        "profile_touched": False,
        "user_shell_impact": "none",
        "global_path_modified": False,
        "db_only_check": True,
    }


def ensure_entrypoint_gateway(
    *,
    project_root: Path,
    command_name: str,
    join_point: str,
    task_id: str | None = None,
    task_tracking: str | Path | None = None,
    db: str | None = None,
    require_existing_task: bool = True,
    emit_event: bool = True,
) -> GatewayResult:
    resolved_task_id = infer_task_id(task_id=task_id, task_tracking=task_tracking)
    if not resolved_task_id:
        raise ValueError(
            f"{command_name} requires host capability task context: pass --task-id "
            "or a TASK-*.md --task-tracking path"
        )

    db_path = project_db_path(project_root, db)
    if not db_path.exists():
        if require_existing_task:
            raise ValueError(f"{command_name} requires task-record DB before this entrypoint: {db_path}")
        return GatewayResult(task_id=resolved_task_id, task_exists=False, event_id="", db_path=db_path)

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        exists = _task_exists(con, resolved_task_id)
        if require_existing_task and not exists:
            raise ValueError(f"{command_name} requires existing task-record row: {resolved_task_id}")
        event_id = ""
        if emit_event and exists:
            if not _table_exists(con, "events"):
                if require_existing_task:
                    raise ValueError(f"{command_name} requires task-record events table: {db_path}")
            else:
                event_id = f"EVT-{resolved_task_id}-CAPABILITY-ENTRYPOINT-{uuid.uuid4().hex[:8]}"
                with con:
                    con.execute(
                        """
                        INSERT INTO events(event_id, task_id, event_type, payload_json, created_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            event_id,
                            resolved_task_id,
                            CAPABILITY_GATEWAY_EVENT,
                            json.dumps(
                                gateway_payload(
                                    command_name=command_name,
                                    join_point=join_point,
                                    task_id=resolved_task_id,
                                ),
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                            now_iso(),
                        ),
                    )
        return GatewayResult(task_id=resolved_task_id, task_exists=exists, event_id=event_id, db_path=db_path)
    finally:
        con.close()
