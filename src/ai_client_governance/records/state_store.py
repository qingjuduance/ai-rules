#!/usr/bin/env python3
"""SQLite-backed governance state store.

Live governance facts belong in ``aicg.db``. Commands may print reports, but
they should not keep JSON state files as a second machine-readable source.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ai_client_governance.common.paths import STRUCTURED_DB_PATH


STATE_SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def db_path(root: Path, override: str | None = None) -> Path:
    if override:
        path = Path(override)
        return path if path.is_absolute() else root / path
    return root / STRUCTURED_DB_PATH


def connect(path: Path, *, create: bool = True) -> sqlite3.Connection:
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    elif not path.exists():
        raise ValueError(f"governance state DB does not exist: {path}")
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    init_db(con)
    return con


def init_db(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS governance_state (
            state_type TEXT NOT NULL,
            state_key TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            source_command TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (state_type, state_key)
        );

        CREATE TABLE IF NOT EXISTS governance_state_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            state_type TEXT NOT NULL,
            state_key TEXT NOT NULL,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            source_command TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_governance_state_type
            ON governance_state(state_type, updated_at);

        CREATE INDEX IF NOT EXISTS idx_governance_state_events_type
            ON governance_state_events(state_type, state_key, created_at);
        """
    )
    con.execute(
        "INSERT INTO meta(key, value) VALUES('governance_state_schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(STATE_SCHEMA_VERSION),),
    )
    con.commit()


def encode_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def decode_payload(value: str) -> dict[str, Any]:
    payload = json.loads(value or "{}")
    if not isinstance(payload, dict):
        raise ValueError("governance state payload must be a JSON object")
    return payload


def upsert_state(
    con: sqlite3.Connection,
    *,
    state_type: str,
    state_key: str,
    payload: dict[str, Any],
    source_command: str = "",
    summary: str = "",
    event_type: str = "state.upsert",
) -> None:
    now = utc_now()
    payload_json = encode_payload(payload)
    with con:
        con.execute(
            """
            INSERT INTO governance_state(
                state_type, state_key, payload_json, source_command, summary, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(state_type, state_key) DO UPDATE SET
                payload_json=excluded.payload_json,
                source_command=excluded.source_command,
                summary=excluded.summary,
                updated_at=excluded.updated_at
            """,
            (state_type, state_key, payload_json, source_command, summary, now, now),
        )
        con.execute(
            """
            INSERT INTO governance_state_events(
                state_type, state_key, event_type, payload_json, source_command, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (state_type, state_key, event_type, payload_json, source_command, now),
        )


def read_state(con: sqlite3.Connection, *, state_type: str, state_key: str) -> dict[str, Any] | None:
    row = con.execute(
        """
        SELECT state_type, state_key, payload_json, source_command, summary, created_at, updated_at
        FROM governance_state
        WHERE state_type = ? AND state_key = ?
        """,
        (state_type, state_key),
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["payload"] = decode_payload(result.pop("payload_json"))
    return result


def list_states(con: sqlite3.Connection, *, state_type: str | None = None) -> list[dict[str, Any]]:
    if state_type:
        rows = con.execute(
            """
            SELECT state_type, state_key, payload_json, source_command, summary, created_at, updated_at
            FROM governance_state
            WHERE state_type = ?
            ORDER BY updated_at DESC, state_key
            """,
            (state_type,),
        ).fetchall()
    else:
        rows = con.execute(
            """
            SELECT state_type, state_key, payload_json, source_command, summary, created_at, updated_at
            FROM governance_state
            ORDER BY state_type, updated_at DESC, state_key
            """
        ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["payload"] = decode_payload(item.pop("payload_json"))
        result.append(item)
    return result
