#!/usr/bin/env python3
"""Coordinate AI maintenance sessions across Git worktrees.

The state is stored under the repository Git common directory, not inside a
worktree copy. That makes active sessions, write locks, and integration queue
items visible to all linked worktrees for the same repository.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
ACTIVE_STATUSES = {"active", "integrating", "waiting"}
QUEUE_STATUSES = {"pending", "integrating", "done", "blocked", "abandoned"}


@dataclass
class Finding:
    level: str
    message: str
    item: str | None = None


def utc_now_dt() -> datetime:
    return datetime.now(timezone.utc)


def utc_now() -> str:
    return utc_now_dt().isoformat(timespec="seconds")


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def parse_json(value: str | None, default: Any) -> Any:
    if value is None or value == "":
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise SystemExit(
            "metadata must be valid JSON. On Windows/PowerShell, prefer "
            "--metadata-file or repeated --metadata-kv key=value entries. "
            f"JSON parse error: {exc}"
        ) from exc


def parse_json_object(value: str | None, source: str) -> dict[str, Any]:
    parsed = parse_json(value, {})
    if not isinstance(parsed, dict):
        raise SystemExit(f"{source} must contain a JSON object")
    return parsed


def parse_metadata_value(value: str) -> Any:
    stripped = value.strip()
    if not stripped:
        return ""
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return value


def parse_metadata_kv(items: list[str]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(f"--metadata-kv requires key=value, got: {item}")
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise SystemExit(f"--metadata-kv key must not be empty, got: {item}")
        metadata[key] = parse_metadata_value(value)
    return metadata


def load_metadata(args: argparse.Namespace) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    metadata.update(parse_json_object(args.metadata, "--metadata"))
    if args.metadata_file:
        path = Path(args.metadata_file)
        try:
            file_text = path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise SystemExit(f"unable to read --metadata-file {path}: {exc}") from exc
        metadata.update(parse_json_object(file_text, f"--metadata-file {path}"))
    metadata.update(parse_metadata_kv(args.metadata_kv or []))
    return metadata


def git_text(args: list[str], cwd: Path, allow_fail: bool = False) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=not allow_fail,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        if allow_fail:
            return ""
        raise SystemExit(f"git {' '.join(args)} failed: {exc}") from exc
    if result.returncode != 0 and allow_fail:
        return ""
    return result.stdout.strip()


def detect_repo(cwd: Path) -> tuple[Path, Path]:
    root_text = git_text(["rev-parse", "--show-toplevel"], cwd)
    root = Path(root_text).resolve()
    common_text = git_text(
        ["rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd,
        allow_fail=True,
    )
    if common_text:
        common_dir = Path(common_text).resolve()
    else:
        fallback = git_text(["rev-parse", "--git-common-dir"], cwd)
        candidate = Path(fallback)
        common_dir = candidate.resolve() if candidate.is_absolute() else (cwd / candidate).resolve()
    return root, common_dir


def current_branch(cwd: Path) -> str:
    branch = git_text(["branch", "--show-current"], cwd, allow_fail=True)
    return branch or "DETACHED"


def current_head(cwd: Path) -> str:
    return git_text(["rev-parse", "HEAD"], cwd, allow_fail=True)


def upstream_ref(cwd: Path) -> str:
    return git_text(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], cwd, allow_fail=True)


def merge_base_with_upstream(cwd: Path) -> str:
    upstream = upstream_ref(cwd)
    if not upstream:
        return ""
    return git_text(["merge-base", "HEAD", upstream], cwd, allow_fail=True)


def safe_id(prefix: str) -> str:
    stamp = utc_now_dt().strftime("%Y%m%d%H%M%S")
    return f"{prefix}-{stamp}-{uuid.uuid4().hex[:8]}"


def normalize_scope(scope: str, repo_root: Path) -> str:
    raw = scope.replace("\\", "/").strip()
    if not raw:
        raise SystemExit("scope must not be empty")
    path = Path(raw)
    if path.is_absolute():
        resolved = path.resolve()
    else:
        resolved = (repo_root / raw).resolve()
    try:
        relative = resolved.relative_to(repo_root.resolve())
    except ValueError as exc:
        raise SystemExit(f"scope must stay inside repository root: {scope}") from exc
    text = relative.as_posix().strip("/")
    return text or "."


def scopes_overlap(left: str, right: str) -> bool:
    left_norm = left.strip("/") or "."
    right_norm = right.strip("/") or "."
    if left_norm == "." or right_norm == ".":
        return True
    return (
        left_norm == right_norm
        or left_norm.startswith(f"{right_norm}/")
        or right_norm.startswith(f"{left_norm}/")
    )


def lease_expires(minutes: int) -> str:
    return (utc_now_dt() + timedelta(minutes=minutes)).isoformat(timespec="seconds")


def is_active_record(item: dict[str, Any], at_time: datetime | None = None) -> bool:
    if item.get("status") not in ACTIVE_STATUSES and item.get("status") != "active":
        return False
    expires = parse_time(str(item.get("lease_expires_at", "")))
    if expires is None:
        return True
    return expires > (at_time or utc_now_dt())


def is_stale_record(item: dict[str, Any], at_time: datetime | None = None) -> bool:
    if item.get("status") not in ACTIVE_STATUSES and item.get("status") != "active":
        return False
    expires = parse_time(str(item.get("lease_expires_at", "")))
    return bool(expires and expires <= (at_time or utc_now_dt()))


class StateStore:
    def __init__(self, common_dir: Path) -> None:
        self.runtime_dir = common_dir / "codex-runtime" / "worktree-coord"
        self.state_file = self.runtime_dir / "state.json"
        self.events_file = self.runtime_dir / "events.jsonl"
        self.lock_file = self.runtime_dir / "state.lock"

    def ensure(self) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)

    def read(self) -> dict[str, Any]:
        self.ensure()
        if not self.state_file.exists():
            return {
                "schema_version": SCHEMA_VERSION,
                "created_at": utc_now(),
                "updated_at": utc_now(),
                "sessions": {},
                "locks": [],
                "queue": [],
            }
        data = json.loads(self.state_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise SystemExit(f"{self.state_file} must contain a JSON object")
        data.setdefault("schema_version", SCHEMA_VERSION)
        data.setdefault("sessions", {})
        data.setdefault("locks", [])
        data.setdefault("queue", [])
        return data

    def write(self, state: dict[str, Any]) -> None:
        self.ensure()
        state["updated_at"] = utc_now()
        temp = self.runtime_dir / f".state.{uuid.uuid4().hex}.tmp"
        temp.write_text(
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temp.replace(self.state_file)

    def append_event(self, event: dict[str, Any]) -> None:
        self.ensure()
        event = {"at": utc_now(), **event}
        with self.events_file.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")

    def acquire_guard(self, timeout_seconds: int = 10, stale_seconds: int = 120) -> None:
        self.ensure()
        deadline = time.monotonic() + timeout_seconds
        payload = json.dumps(
            {"pid": os.getpid(), "created_at": utc_now()},
            ensure_ascii=False,
        )
        while True:
            try:
                with self.lock_file.open("x", encoding="utf-8", newline="\n") as handle:
                    handle.write(payload)
                return
            except FileExistsError:
                if self._guard_is_stale(stale_seconds):
                    try:
                        self.lock_file.unlink()
                    except FileNotFoundError:
                        pass
                    continue
                if time.monotonic() >= deadline:
                    raise SystemExit(f"timed out waiting for coordination lock: {self.lock_file}")
                time.sleep(0.1)

    def _guard_is_stale(self, stale_seconds: int) -> bool:
        try:
            data = json.loads(self.lock_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        created = parse_time(str(data.get("created_at", "")))
        return bool(created and created < utc_now_dt() - timedelta(seconds=stale_seconds))

    def release_guard(self) -> None:
        try:
            self.lock_file.unlink()
        except FileNotFoundError:
            pass


class GuardedState:
    def __init__(self, store: StateStore) -> None:
        self.store = store

    def __enter__(self) -> StateStore:
        self.store.acquire_guard()
        return self.store

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.store.release_guard()


def active_sessions(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        session
        for session in state.get("sessions", {}).values()
        if isinstance(session, dict) and is_active_record(session)
    ]


def active_locks(state: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        lock
        for lock in state.get("locks", [])
        if isinstance(lock, dict) and is_active_record(lock)
    ]


def lock_conflicts(locks: list[dict[str, Any]] | None = None) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    items = locks or []
    conflicts: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for index, left in enumerate(items):
        for right in items[index + 1 :]:
            if left.get("session_id") == right.get("session_id"):
                continue
            if scopes_overlap(str(left.get("scope", "")), str(right.get("scope", ""))):
                conflicts.append((left, right))
    return conflicts


def queue_counts(state: dict[str, Any]) -> dict[str, int]:
    counter = Counter(
        str(item.get("status", "pending"))
        for item in state.get("queue", [])
        if isinstance(item, dict)
    )
    return {status: counter.get(status, 0) for status in sorted(QUEUE_STATUSES)}


def build_summary(state: dict[str, Any]) -> dict[str, Any]:
    sessions = list(state.get("sessions", {}).values())
    locks = list(state.get("locks", []))
    active_lock_items = active_locks(state)
    stale_sessions = [item for item in sessions if isinstance(item, dict) and is_stale_record(item)]
    stale_locks = [item for item in locks if isinstance(item, dict) and is_stale_record(item)]
    conflicts = lock_conflicts(active_lock_items)
    return {
        "schema_version": state.get("schema_version"),
        "updated_at": state.get("updated_at"),
        "sessions_total": len(sessions),
        "sessions_active": len(active_sessions(state)),
        "sessions_stale": len(stale_sessions),
        "locks_total": len(locks),
        "locks_active": len(active_lock_items),
        "locks_stale": len(stale_locks),
        "lock_conflicts": len(conflicts),
        "queue": queue_counts(state),
    }


def print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))


def print_summary_text(summary: dict[str, Any]) -> None:
    print("AI Rules Worktree Coordination")
    print(f"updated_at: {summary.get('updated_at')}")
    print(
        "sessions: "
        f"total={summary['sessions_total']} active={summary['sessions_active']} "
        f"stale={summary['sessions_stale']}"
    )
    print(
        "locks: "
        f"total={summary['locks_total']} active={summary['locks_active']} "
        f"stale={summary['locks_stale']} conflicts={summary['lock_conflicts']}"
    )
    queue = summary["queue"]
    print(
        "queue: "
        + " ".join(f"{key}={queue[key]}" for key in sorted(queue))
    )


def require_session(state: dict[str, Any], session_id: str) -> dict[str, Any]:
    session = state.get("sessions", {}).get(session_id)
    if not isinstance(session, dict):
        raise SystemExit(f"session not found: {session_id}")
    return session


def session_is_active(state: dict[str, Any], session_id: str) -> bool:
    return is_active_record(require_session(state, session_id))


def changed_files(cwd: Path, base: str, head: str) -> list[str]:
    if base and head:
        out = git_text(["diff", "--name-only", f"{base}...{head}"], cwd, allow_fail=True)
        if not out:
            out = git_text(["diff", "--name-only", base, head], cwd, allow_fail=True)
        if out:
            return sorted(set(line.strip() for line in out.splitlines() if line.strip()))
    status = git_text(["-c", "core.quotepath=false", "status", "--porcelain"], cwd, allow_fail=True)
    files: list[str] = []
    for line in status.splitlines():
        if not line:
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        if path:
            files.append(path.strip('"'))
    return sorted(set(files))


def command_status(args: argparse.Namespace, store: StateStore) -> int:
    state = store.read()
    summary = build_summary(state)
    if args.format == "json":
        data: dict[str, Any] = {"summary": summary}
        if not args.active_only:
            data["sessions"] = list(state.get("sessions", {}).values())
            data["locks"] = state.get("locks", [])
            data["queue_items"] = state.get("queue", [])
        else:
            data["sessions"] = active_sessions(state)
            data["locks"] = active_locks(state)
            data["queue_items"] = [
                item
                for item in state.get("queue", [])
                if isinstance(item, dict) and item.get("status") in {"pending", "integrating", "blocked"}
            ]
        print_json(data)
    else:
        print_summary_text(summary)
        if args.active_only:
            for session in active_sessions(state):
                print(
                    f"active session {session.get('session_id')}: "
                    f"branch={session.get('branch')} scopes={','.join(session.get('scopes', []))}"
                )
            for lock in active_locks(state):
                print(
                    f"active lock {lock.get('lock_id')}: "
                    f"session={lock.get('session_id')} scope={lock.get('scope')} "
                    f"expires={lock.get('lease_expires_at')}"
                )
    return 0


def command_session_register(args: argparse.Namespace, repo_root: Path, store: StateStore) -> int:
    session_id = args.session_id or safe_id("S")
    scopes = [normalize_scope(scope, repo_root) for scope in (args.scope or [])]
    metadata = load_metadata(args)
    with GuardedState(store) as guarded:
        state = guarded.read()
        sessions = state.setdefault("sessions", {})
        if session_id in sessions and not args.force:
            raise SystemExit(f"session already exists: {session_id}; pass --force to update")
        session = {
            "session_id": session_id,
            "title": args.title,
            "task": args.task,
            "task_tracking": args.task_tracking,
            "agent_group": args.agent_group,
            "status": args.status,
            "worktree": str(Path.cwd().resolve()),
            "repo_root": str(repo_root),
            "branch": current_branch(Path.cwd()),
            "head": current_head(Path.cwd()),
            "scopes": scopes,
            "metadata": metadata,
            "created_at": sessions.get(session_id, {}).get("created_at", utc_now()),
            "updated_at": utc_now(),
            "lease_expires_at": lease_expires(args.lease_minutes),
        }
        sessions[session_id] = session
        guarded.append_event({"event": "session.register", "session_id": session_id, "scopes": scopes})
        guarded.write(state)
    if args.format == "json":
        print_json({"session_id": session_id, "scopes": scopes})
    else:
        print(f"registered session {session_id}")
        print(f"scopes: {', '.join(scopes) if scopes else '(none)'}")
    return 0


def command_session_heartbeat(args: argparse.Namespace, repo_root: Path, store: StateStore) -> int:
    scopes = [normalize_scope(scope, repo_root) for scope in (args.scope or [])]
    with GuardedState(store) as guarded:
        state = guarded.read()
        session = require_session(state, args.session_id)
        if scopes:
            session["scopes"] = scopes
        if args.status:
            session["status"] = args.status
        session["branch"] = current_branch(Path.cwd())
        session["head"] = current_head(Path.cwd())
        session["updated_at"] = utc_now()
        session["lease_expires_at"] = lease_expires(args.lease_minutes)
        guarded.append_event({"event": "session.heartbeat", "session_id": args.session_id})
        guarded.write(state)
    print(f"heartbeat session {args.session_id}")
    return 0


def command_session_close(args: argparse.Namespace, store: StateStore) -> int:
    released = 0
    with GuardedState(store) as guarded:
        state = guarded.read()
        session = require_session(state, args.session_id)
        session["status"] = "closed"
        session["updated_at"] = utc_now()
        if not args.keep_locks:
            for lock in state.get("locks", []):
                if lock.get("session_id") == args.session_id and lock.get("status") == "active":
                    lock["status"] = "released"
                    lock["updated_at"] = utc_now()
                    released += 1
        guarded.append_event(
            {"event": "session.close", "session_id": args.session_id, "released_locks": released}
        )
        guarded.write(state)
    print(f"closed session {args.session_id}; released_locks={released}")
    return 0


def command_lock_acquire(args: argparse.Namespace, repo_root: Path, store: StateStore) -> int:
    requested = [normalize_scope(scope, repo_root) for scope in args.scope]
    with GuardedState(store) as guarded:
        state = guarded.read()
        if not session_is_active(state, args.session_id):
            raise SystemExit(f"session is not active: {args.session_id}")
        existing = active_locks(state)
        conflicts = [
            lock
            for lock in existing
            if lock.get("session_id") != args.session_id
            and any(scopes_overlap(str(lock.get("scope", "")), scope) for scope in requested)
        ]
        if conflicts:
            if args.format == "json":
                print_json({"status": "conflict", "conflicts": conflicts})
            else:
                print("lock conflict:")
                for lock in conflicts:
                    print(
                        f"- lock={lock.get('lock_id')} session={lock.get('session_id')} "
                        f"scope={lock.get('scope')} expires={lock.get('lease_expires_at')}"
                    )
            return 2
        created: list[dict[str, Any]] = []
        for scope in requested:
            lock = {
                "lock_id": safe_id("L"),
                "session_id": args.session_id,
                "scope": scope,
                "mode": args.mode,
                "reason": args.reason,
                "status": "active",
                "created_at": utc_now(),
                "updated_at": utc_now(),
                "lease_expires_at": lease_expires(args.lease_minutes),
            }
            state.setdefault("locks", []).append(lock)
            created.append(lock)
        guarded.append_event(
            {
                "event": "lock.acquire",
                "session_id": args.session_id,
                "lock_ids": [lock["lock_id"] for lock in created],
                "scopes": requested,
            }
        )
        guarded.write(state)
    if args.format == "json":
        print_json({"status": "acquired", "locks": created})
    else:
        print(f"acquired {len(created)} lock(s)")
        for lock in created:
            print(f"- {lock['lock_id']} {lock['scope']} expires={lock['lease_expires_at']}")
    return 0


def command_lock_release(args: argparse.Namespace, repo_root: Path, store: StateStore) -> int:
    scopes = [normalize_scope(scope, repo_root) for scope in (args.scope or [])]
    if not args.all and not args.lock_id and not scopes:
        raise SystemExit("release requires --all, --lock-id, or --scope")
    released = 0
    with GuardedState(store) as guarded:
        state = guarded.read()
        for lock in state.get("locks", []):
            if lock.get("status") != "active":
                continue
            if args.session_id and lock.get("session_id") != args.session_id:
                continue
            match = args.all
            if args.lock_id and lock.get("lock_id") == args.lock_id:
                match = True
            if scopes and str(lock.get("scope", "")) in scopes:
                match = True
            if match:
                lock["status"] = "released"
                lock["updated_at"] = utc_now()
                released += 1
        guarded.append_event(
            {"event": "lock.release", "session_id": args.session_id, "released_locks": released}
        )
        guarded.write(state)
    print(f"released_locks={released}")
    return 0


def command_queue_add(args: argparse.Namespace, repo_root: Path, store: StateStore) -> int:
    branch = args.branch or current_branch(Path.cwd())
    head = args.head or current_head(Path.cwd())
    base = args.base or merge_base_with_upstream(Path.cwd())
    files = [normalize_scope(path, repo_root) for path in (args.file or [])]
    if not files:
        files = [normalize_scope(path, repo_root) for path in changed_files(Path.cwd(), base, head)]
    item_id = args.item_id or safe_id("Q")
    item = {
        "item_id": item_id,
        "session_id": args.session_id,
        "branch": branch,
        "base": base,
        "head": head,
        "status": "pending",
        "summary": args.summary,
        "task_tracking": args.task_tracking,
        "files": files,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "validation": [],
        "notes": [],
    }
    with GuardedState(store) as guarded:
        state = guarded.read()
        if any(existing.get("item_id") == item_id for existing in state.get("queue", [])):
            raise SystemExit(f"queue item already exists: {item_id}")
        state.setdefault("queue", []).append(item)
        guarded.append_event({"event": "queue.add", "item_id": item_id, "branch": branch, "files": files})
        guarded.write(state)
    if args.format == "json":
        print_json(item)
    else:
        print(f"queued {item_id} branch={branch} files={len(files)}")
    return 0


def command_queue_mark(args: argparse.Namespace, store: StateStore) -> int:
    with GuardedState(store) as guarded:
        state = guarded.read()
        queue = state.get("queue", [])
        item = next((entry for entry in queue if entry.get("item_id") == args.item_id), None)
        if not isinstance(item, dict):
            raise SystemExit(f"queue item not found: {args.item_id}")
        item["status"] = args.status
        item["updated_at"] = utc_now()
        if args.validation:
            item.setdefault("validation", []).append({"at": utc_now(), "value": args.validation})
        if args.note:
            item.setdefault("notes", []).append({"at": utc_now(), "value": args.note})
        guarded.append_event({"event": "queue.mark", "item_id": args.item_id, "status": args.status})
        guarded.write(state)
    print(f"marked {args.item_id} status={args.status}")
    return 0


def command_queue_list(args: argparse.Namespace, store: StateStore) -> int:
    state = store.read()
    items = [
        item
        for item in state.get("queue", [])
        if isinstance(item, dict) and (not args.status or item.get("status") == args.status)
    ]
    if args.format == "json":
        print_json({"queue_items": items, "counts": queue_counts(state)})
    else:
        print("integration queue")
        for item in items:
            print(
                f"- {item.get('item_id')} status={item.get('status')} "
                f"branch={item.get('branch')} files={len(item.get('files', []))}"
            )
        if not items:
            print("  none")
    return 0


def validate_state(state: dict[str, Any]) -> tuple[list[Finding], list[Finding], list[Finding]]:
    errors: list[Finding] = []
    warnings: list[Finding] = []
    notes: list[Finding] = []

    sessions = state.get("sessions", {})
    if not isinstance(sessions, dict):
        errors.append(Finding("error", "sessions must be an object"))
    locks = state.get("locks", [])
    if not isinstance(locks, list):
        errors.append(Finding("error", "locks must be an array"))
        locks = []
    queue = state.get("queue", [])
    if not isinstance(queue, list):
        errors.append(Finding("error", "queue must be an array"))
        queue = []

    active_lock_items = active_locks(state)
    for left, right in lock_conflicts(active_lock_items):
        errors.append(
            Finding(
                "error",
                "active locks overlap across sessions",
                f"{left.get('lock_id')} <-> {right.get('lock_id')}",
            )
        )

    for session_id, session in sessions.items():
        if not isinstance(session, dict):
            errors.append(Finding("error", "session entry must be an object", str(session_id)))
            continue
        if is_stale_record(session):
            warnings.append(Finding("warning", "session lease is stale", str(session_id)))
        if is_active_record(session) and not session.get("scopes"):
            warnings.append(Finding("warning", "active session has no declared scopes", str(session_id)))
        if is_active_record(session) and not session.get("worktree"):
            errors.append(Finding("error", "active session is missing worktree path", str(session_id)))

    for lock in locks:
        if not isinstance(lock, dict):
            errors.append(Finding("error", "lock entry must be an object"))
            continue
        if lock.get("status") == "active" and lock.get("session_id") not in sessions:
            errors.append(Finding("error", "active lock references missing session", str(lock.get("lock_id"))))
        if is_stale_record(lock):
            warnings.append(Finding("warning", "lock lease is stale", str(lock.get("lock_id"))))

    for item in queue:
        if not isinstance(item, dict):
            errors.append(Finding("error", "queue item must be an object"))
            continue
        item_id = str(item.get("item_id", ""))
        if item.get("status") not in QUEUE_STATUSES:
            errors.append(Finding("error", "queue item has invalid status", item_id))
        if item.get("status") in {"pending", "integrating"} and not item.get("branch"):
            errors.append(Finding("error", "open queue item is missing branch", item_id))
        if item.get("status") in {"pending", "integrating"} and not item.get("files"):
            warnings.append(Finding("warning", "open queue item has no file list", item_id))

    summary = build_summary(state)
    notes.append(Finding("note", f"summary={json.dumps(summary, ensure_ascii=False, sort_keys=True)}"))
    return errors, warnings, notes


def format_findings(title: str, findings: list[Finding]) -> list[str]:
    lines = [f"{title}: {len(findings)}"]
    if not findings:
        lines.append("  none")
        return lines
    for finding in findings:
        suffix = f" [{finding.item}]" if finding.item else ""
        lines.append(f"  - {finding.message}{suffix}")
    return lines


def command_validate(args: argparse.Namespace, store: StateStore) -> int:
    state = store.read()
    errors, warnings, notes = validate_state(state)
    if args.format == "json":
        print_json(
            {
                "summary": build_summary(state),
                "errors": [asdict(item) for item in errors],
                "warnings": [asdict(item) for item in warnings],
                "notes": [asdict(item) for item in notes],
            }
        )
    else:
        print("AI Rules Worktree Coordination Validation")
        for block in (
            format_findings("Errors", errors),
            format_findings("Warnings", warnings),
            format_findings("Notes", notes),
        ):
            print("\n".join(block))
            print()
    if errors:
        return 1
    if args.fail_on_warning and warnings:
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Coordinate AI maintenance sessions, locks, and integration queues across Git worktrees."
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    subparsers = parser.add_subparsers(dest="command", required=True)

    status = subparsers.add_parser("status", help="Show coordination summary.")
    status.add_argument("--active-only", action="store_true")

    validate = subparsers.add_parser("validate", help="Validate coordination state.")
    validate.add_argument("--fail-on-warning", action="store_true")

    session = subparsers.add_parser("session", help="Manage sessions.")
    session_sub = session.add_subparsers(dest="session_command", required=True)
    register = session_sub.add_parser("register", help="Register or update a session.")
    register.add_argument("--session-id")
    register.add_argument("--title", required=True)
    register.add_argument("--task", default="")
    register.add_argument("--task-tracking", default="")
    register.add_argument("--agent-group", default="")
    register.add_argument("--scope", action="append", default=[])
    register.add_argument("--status", default="active", choices=sorted(ACTIVE_STATUSES))
    register.add_argument("--lease-minutes", type=int, default=240)
    register.add_argument(
        "--metadata",
        help="Strict JSON object metadata. Use --metadata-file or --metadata-kv on PowerShell.",
    )
    register.add_argument(
        "--metadata-file",
        help="Read UTF-8 JSON object metadata from a file.",
    )
    register.add_argument(
        "--metadata-kv",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Add metadata one key at a time. VALUE is parsed as JSON when possible; repeatable.",
    )
    register.add_argument("--force", action="store_true")

    heartbeat = session_sub.add_parser("heartbeat", help="Refresh a session lease.")
    heartbeat.add_argument("--session-id", required=True)
    heartbeat.add_argument("--scope", action="append", default=[])
    heartbeat.add_argument("--status", choices=sorted(ACTIVE_STATUSES))
    heartbeat.add_argument("--lease-minutes", type=int, default=240)

    close = session_sub.add_parser("close", help="Close a session.")
    close.add_argument("--session-id", required=True)
    close.add_argument("--keep-locks", action="store_true")

    lock = subparsers.add_parser("lock", help="Manage write locks.")
    lock_sub = lock.add_subparsers(dest="lock_command", required=True)
    acquire = lock_sub.add_parser("acquire", help="Acquire write locks for scopes.")
    acquire.add_argument("--session-id", required=True)
    acquire.add_argument("--scope", action="append", required=True)
    acquire.add_argument("--mode", choices=("write", "integration"), default="write")
    acquire.add_argument("--reason", default="")
    acquire.add_argument("--lease-minutes", type=int, default=240)

    release = lock_sub.add_parser("release", help="Release locks.")
    release.add_argument("--session-id")
    release.add_argument("--lock-id")
    release.add_argument("--scope", action="append", default=[])
    release.add_argument("--all", action="store_true")

    queue = subparsers.add_parser("queue", help="Manage integration queue items.")
    queue_sub = queue.add_subparsers(dest="queue_command", required=True)
    add = queue_sub.add_parser("add", help="Add an integration queue item.")
    add.add_argument("--item-id")
    add.add_argument("--session-id", default="")
    add.add_argument("--branch")
    add.add_argument("--base")
    add.add_argument("--head")
    add.add_argument("--summary", required=True)
    add.add_argument("--task-tracking", default="")
    add.add_argument("--file", action="append", default=[])

    mark = queue_sub.add_parser("mark", help="Mark a queue item status.")
    mark.add_argument("--item-id", required=True)
    mark.add_argument("--status", required=True, choices=sorted(QUEUE_STATUSES))
    mark.add_argument("--validation")
    mark.add_argument("--note")

    queue_list = queue_sub.add_parser("list", help="List integration queue items.")
    queue_list.add_argument("--status", choices=sorted(QUEUE_STATUSES))
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    cwd = Path.cwd()
    repo_root, common_dir = detect_repo(cwd)
    store = StateStore(common_dir)

    if args.command == "status":
        return command_status(args, store)
    if args.command == "validate":
        return command_validate(args, store)
    if args.command == "session":
        if args.session_command == "register":
            return command_session_register(args, repo_root, store)
        if args.session_command == "heartbeat":
            return command_session_heartbeat(args, repo_root, store)
        if args.session_command == "close":
            return command_session_close(args, store)
    if args.command == "lock":
        if args.lock_command == "acquire":
            return command_lock_acquire(args, repo_root, store)
        if args.lock_command == "release":
            return command_lock_release(args, repo_root, store)
    if args.command == "queue":
        if args.queue_command == "add":
            return command_queue_add(args, repo_root, store)
        if args.queue_command == "mark":
            return command_queue_mark(args, store)
        if args.queue_command == "list":
            return command_queue_list(args, store)
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

