#!/usr/bin/env python3
"""Manage a local file-based communication bus for AI sub-agents.

The bus is intentionally simple: UTF-8 JSON files hold group and agent state,
and JSONL files hold append-only messages. It does not replace tool-specific
spawn/send/wait features; it adds durable inbox/outbox records, acknowledgements,
heartbeats, artifact indexes, and token usage provenance.
"""

from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ai_client_governance.common.paths import AGENT_COMM_DIR

BASE_DIR = AGENT_COMM_DIR
GROUPS_DIR = BASE_DIR / "groups"
LOCKS_FILE = BASE_DIR / "locks.json"
SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def now_datetime() -> datetime:
    return datetime.now(timezone.utc).astimezone()


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
        raise SystemExit(f"metadata must be valid JSON: {exc}") from exc


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    ensure_dir(path.parent)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def read_locks() -> list[dict[str, Any]]:
    data = read_json(LOCKS_FILE, [])
    if not isinstance(data, list):
        raise SystemExit(f"{LOCKS_FILE} must contain a JSON array")
    return data


def write_locks(locks: list[dict[str, Any]]) -> None:
    write_json(LOCKS_FILE, locks)


def normalize_scope(scope: str) -> str:
    normalized = scope.replace("\\", "/").strip().strip("/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized or "."


def validate_path_segment(value: str, label: str) -> str:
    text = str(value)
    if not text or text != text.strip():
        raise SystemExit(f"{label} must not be empty or padded with whitespace")
    if text in {".", ".."}:
        raise SystemExit(f"{label} must not be '.' or '..'")
    if any(marker in text for marker in ("/", "\\", "\x00")):
        raise SystemExit(f"{label} must not contain path separators")
    if ":" in text or Path(text).is_absolute():
        raise SystemExit(f"{label} must be a plain path segment")
    return text


def scopes_overlap(left: str, right: str) -> bool:
    left_norm = normalize_scope(left)
    right_norm = normalize_scope(right)
    return (
        left_norm == right_norm
        or left_norm.startswith(f"{right_norm}/")
        or right_norm.startswith(f"{left_norm}/")
    )


def lock_is_active(lock: dict[str, Any], at_time: datetime | None = None) -> bool:
    if lock.get("status") != "active":
        return False
    expires_at = parse_time(str(lock.get("lease_expires_at", "")))
    if expires_at is None:
        return True
    return expires_at > (at_time or now_datetime())


def active_locks(group_id: str | None = None) -> list[dict[str, Any]]:
    locks = [lock for lock in read_locks() if lock_is_active(lock)]
    if group_id:
        locks = [lock for lock in locks if lock.get("group_id") == group_id]
    return locks


def find_lock_conflicts(
    scope: str,
    group_id: str,
    locks: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    candidates = [
        lock
        for lock in (locks if locks is not None else read_locks())
        if lock_is_active(lock)
    ]
    normalized = normalize_scope(scope)
    return [
        lock
        for lock in candidates
        if lock.get("group_id") != group_id
        and scopes_overlap(str(lock.get("scope", "")), normalized)
    ]


def append_jsonl(path: Path, data: dict[str, Any]) -> None:
    ensure_dir(path.parent)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(data, ensure_ascii=False, sort_keys=True) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path}:{line_number}: invalid JSONL row: {exc}") from exc
        if not isinstance(row, dict):
            raise SystemExit(f"{path}:{line_number}: JSONL row must be an object")
        rows.append(row)
    return rows


def group_dir(group_id: str) -> Path:
    return GROUPS_DIR / validate_path_segment(group_id, "group id")


def agent_dir(group_id: str, agent_id: str) -> Path:
    return group_dir(group_id) / "agents" / validate_path_segment(agent_id, "agent id")


def group_file(group_id: str) -> Path:
    return group_dir(group_id) / "group.json"


def agents_file(group_id: str) -> Path:
    return group_dir(group_id) / "agents.json"


def load_group(group_id: str) -> dict[str, Any]:
    path = group_file(group_id)
    if not path.exists():
        raise SystemExit(f"group not found: {group_id}; run init first")
    data = read_json(path, {})
    if not isinstance(data, dict):
        raise SystemExit(f"{path} must contain a JSON object")
    return data


def load_agents(group_id: str) -> list[dict[str, Any]]:
    data = read_json(agents_file(group_id), [])
    if not isinstance(data, list):
        raise SystemExit(f"{agents_file(group_id)} must contain a JSON array")
    return data


def save_agents(group_id: str, agents: list[dict[str, Any]]) -> None:
    write_json(agents_file(group_id), agents)


def touch_bus_files(group_id: str) -> None:
    root = group_dir(group_id)
    for relative in ("main-outbox.jsonl", "main-inbox.jsonl", "broadcasts.jsonl"):
        path = root / relative
        ensure_dir(path.parent)
        if not path.exists():
            path.write_text("", encoding="utf-8")
    ensure_dir(root / "agents")


def build_message(
    group_id: str,
    sender: str,
    recipient: str,
    body: str,
    subject: str,
    message_type: str,
    requires_ack: bool,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "message_id": f"msg-{uuid.uuid4().hex[:12]}",
        "group_id": group_id,
        "type": message_type,
        "subject": subject,
        "from": sender,
        "to": recipient,
        "body": body,
        "requires_ack": requires_ack,
        "created_at": utc_now(),
        "metadata": metadata,
    }


def append_to_sender_outbox(group_id: str, sender: str, message: dict[str, Any]) -> None:
    if sender in ("main", "controller", "总控"):
        append_jsonl(group_dir(group_id) / "main-outbox.jsonl", message)
    else:
        append_jsonl(agent_dir(group_id, sender) / "outbox.jsonl", message)


def append_to_recipient_inbox(group_id: str, recipient: str, message: dict[str, Any]) -> None:
    if recipient in ("main", "controller", "总控"):
        append_jsonl(group_dir(group_id) / "main-inbox.jsonl", message)
    elif recipient != "*":
        append_jsonl(agent_dir(group_id, recipient) / "inbox.jsonl", message)


def command_init(args: argparse.Namespace) -> int:
    group_id = args.group
    root = group_dir(group_id)
    ensure_dir(root)
    touch_bus_files(group_id)
    if not LOCKS_FILE.exists():
        write_locks([])
    data = read_json(group_file(group_id), {})
    if not isinstance(data, dict):
        raise SystemExit(f"{group_file(group_id)} must contain a JSON object")
    data.update(
        {
            "schema_version": SCHEMA_VERSION,
            "group_id": group_id,
            "group_title": args.title,
            "status": args.status,
            "task_tracking_file": args.tracking,
            "pending_file": args.pending,
            "approval_label": args.approval,
            "created_at": data.get("created_at") or utc_now(),
            "updated_at": utc_now(),
            "notes": args.notes,
            "write_scopes": [normalize_scope(scope) for scope in (args.write_scope or [])],
        }
    )
    write_json(group_file(group_id), data)
    if not agents_file(group_id).exists():
        save_agents(group_id, [])
    print(f"initialized {root}")
    return 0


def command_register(args: argparse.Namespace) -> int:
    load_group(args.group)
    touch_bus_files(args.group)
    agents = load_agents(args.group)
    existing = next((agent for agent in agents if agent.get("agent_id") == args.agent), None)
    record = {
        "agent_id": args.agent,
        "nickname": args.nickname,
        "role": args.role,
        "brief": args.brief,
        "status": args.status,
        "registered_at": utc_now(),
        "updated_at": utc_now(),
        "token_usage_source": args.token_usage_source,
        "reuse_key": args.reuse_key,
        "context_reuse": args.context_reuse,
        "context_capsule": args.context_capsule,
        "context_budget": args.context_budget,
        "context_ttl": args.context_ttl,
        "contamination_boundary": args.contamination_boundary,
        "minimal_resume_inputs": args.minimal_resume_input or [],
        "token_proxy_metrics": args.token_proxy_metric or [],
        "retained_facts": args.retained_fact or [],
        "skip_inputs": args.skip_input or [],
    }
    if existing:
        existing.update(record)
    else:
        agents.append(record)
    save_agents(args.group, agents)
    root = agent_dir(args.group, args.agent)
    ensure_dir(root)
    for name in ("inbox.jsonl", "outbox.jsonl", "artifacts.jsonl"):
        path = root / name
        if not path.exists():
            path.write_text("", encoding="utf-8")
    heartbeat = root / "heartbeat.json"
    if not heartbeat.exists():
        write_json(
            heartbeat,
            {
                "schema_version": SCHEMA_VERSION,
                "group_id": args.group,
                "agent_id": args.agent,
                "status": args.status,
                "updated_at": utc_now(),
                "note": "registered",
            },
        )
    print(f"registered {args.agent} in {args.group}")
    return 0


def command_send(args: argparse.Namespace) -> int:
    load_group(args.group)
    metadata = parse_json(args.metadata, {})
    message = build_message(
        args.group,
        args.sender,
        args.to,
        args.body,
        args.subject,
        args.type,
        args.requires_ack,
        metadata,
    )
    append_to_sender_outbox(args.group, args.sender, message)
    append_to_recipient_inbox(args.group, args.to, message)
    print(message["message_id"])
    return 0


def command_broadcast(args: argparse.Namespace) -> int:
    load_group(args.group)
    metadata = parse_json(args.metadata, {})
    message = build_message(
        args.group,
        args.sender,
        "*",
        args.body,
        args.subject,
        args.type,
        args.requires_ack,
        metadata,
    )
    append_jsonl(group_dir(args.group) / "broadcasts.jsonl", message)
    append_to_sender_outbox(args.group, args.sender, message)
    if args.copy_to_agents:
        for agent in load_agents(args.group):
            agent_id = str(agent.get("agent_id", ""))
            if agent_id:
                copied = dict(message)
                copied["to"] = agent_id
                append_to_recipient_inbox(args.group, agent_id, copied)
    print(message["message_id"])
    return 0


def command_read(args: argparse.Namespace) -> int:
    load_group(args.group)
    if args.box == "main-inbox":
        path = group_dir(args.group) / "main-inbox.jsonl"
    elif args.box == "main-outbox":
        path = group_dir(args.group) / "main-outbox.jsonl"
    elif args.box == "broadcasts":
        path = group_dir(args.group) / "broadcasts.jsonl"
    elif args.agent:
        path = agent_dir(args.group, args.agent) / f"{args.box}.jsonl"
    else:
        raise SystemExit("--agent is required for agent inbox/outbox/artifacts")
    rows = read_jsonl(path)
    if args.limit:
        rows = rows[-args.limit :]
    if args.format == "json":
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        for row in rows:
            print(
                "{created_at} {message_id} {from}->{to} [{type}] {subject}".format(
                    created_at=row.get("created_at", ""),
                    message_id=row.get("message_id", ""),
                    **{
                        "from": row.get("from", ""),
                        "to": row.get("to", ""),
                        "type": row.get("type", ""),
                        "subject": row.get("subject", ""),
                    },
                )
            )
            if args.show_body:
                print(f"  {row.get('body', '')}")
    return 0


def command_ack(args: argparse.Namespace) -> int:
    load_group(args.group)
    ack = {
        "schema_version": SCHEMA_VERSION,
        "message_id": f"ack-{uuid.uuid4().hex[:12]}",
        "group_id": args.group,
        "type": "ack",
        "ack_message_id": args.message,
        "from": args.sender,
        "to": args.to,
        "body": args.note,
        "created_at": utc_now(),
        "metadata": parse_json(args.metadata, {}),
    }
    append_to_sender_outbox(args.group, args.sender, ack)
    append_to_recipient_inbox(args.group, args.to, ack)
    print(ack["message_id"])
    return 0


def command_heartbeat(args: argparse.Namespace) -> int:
    load_group(args.group)
    root = agent_dir(args.group, args.agent)
    ensure_dir(root)
    heartbeat = {
        "schema_version": SCHEMA_VERSION,
        "group_id": args.group,
        "agent_id": args.agent,
        "status": args.status,
        "updated_at": utc_now(),
        "note": args.note,
        "current_files": args.file or [],
        "token_usage_source": args.token_usage_source,
        "reuse_key": args.reuse_key,
        "context_reuse": args.context_reuse,
        "context_capsule": args.context_capsule,
        "context_budget": args.context_budget,
        "context_ttl": args.context_ttl,
        "contamination_boundary": args.contamination_boundary,
        "minimal_resume_inputs": args.minimal_resume_input or [],
        "token_proxy_metrics": args.token_proxy_metric or [],
        "metadata": parse_json(args.metadata, {}),
    }
    write_json(root / "heartbeat.json", heartbeat)
    print(f"heartbeat {args.agent}: {args.status}")
    return 0


def command_artifact(args: argparse.Namespace) -> int:
    load_group(args.group)
    artifact = {
        "schema_version": SCHEMA_VERSION,
        "artifact_id": f"art-{uuid.uuid4().hex[:12]}",
        "group_id": args.group,
        "agent_id": args.agent,
        "path": args.path,
        "kind": args.kind,
        "summary": args.summary,
        "created_at": utc_now(),
        "metadata": parse_json(args.metadata, {}),
    }
    append_jsonl(agent_dir(args.group, args.agent) / "artifacts.jsonl", artifact)
    print(artifact["artifact_id"])
    return 0


def command_lock_acquire(args: argparse.Namespace) -> int:
    load_group(args.group)
    locks = read_locks()
    scope = normalize_scope(args.scope)
    conflicts = find_lock_conflicts(scope, args.group, locks)
    if conflicts and not args.force:
        print("lock conflict:")
        for lock in conflicts:
            print(
                "- {lock_id} group={group_id} owner={owner} scope={scope} "
                "lease_expires_at={lease_expires_at}".format(**lock)
            )
        return 3
    acquired_at = now_datetime()
    lease_expires_at = acquired_at + timedelta(minutes=max(args.lease_minutes, 1))
    record = {
        "schema_version": SCHEMA_VERSION,
        "lock_id": f"lock-{uuid.uuid4().hex[:12]}",
        "group_id": args.group,
        "owner": args.owner,
        "scope": scope,
        "status": "active",
        "acquired_at": acquired_at.isoformat(timespec="seconds"),
        "lease_expires_at": lease_expires_at.isoformat(timespec="seconds"),
        "released_at": None,
        "note": args.note,
        "force": bool(args.force),
        "conflicts_at_acquire": [lock.get("lock_id") for lock in conflicts],
    }
    locks.append(record)
    write_locks(locks)
    print(record["lock_id"])
    return 0


def command_lock_release(args: argparse.Namespace) -> int:
    load_group(args.group)
    locks = read_locks()
    released: list[str] = []
    scope = normalize_scope(args.scope) if args.scope else None
    for lock in locks:
        if lock.get("group_id") != args.group or lock.get("status") != "active":
            continue
        if args.lock_id and lock.get("lock_id") != args.lock_id:
            continue
        if scope and normalize_scope(str(lock.get("scope", ""))) != scope:
            continue
        if args.owner and lock.get("owner") != args.owner:
            continue
        lock["status"] = "released"
        lock["released_at"] = utc_now()
        lock["release_note"] = args.note
        released.append(str(lock.get("lock_id")))
    write_locks(locks)
    if not released:
        print("no matching active lock")
        return 1
    print("\n".join(released))
    return 0


def lock_conflict_pairs(locks: list[dict[str, Any]] | None = None) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    active = locks if locks is not None else active_locks()
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for index, left in enumerate(active):
        for right in active[index + 1 :]:
            if left.get("group_id") == right.get("group_id"):
                continue
            if scopes_overlap(str(left.get("scope", "")), str(right.get("scope", ""))):
                pairs.append((left, right))
    return pairs


def command_lock_status(args: argparse.Namespace) -> int:
    locks = read_locks()
    if args.group:
        locks = [lock for lock in locks if lock.get("group_id") == args.group]
    if args.active_only:
        locks = [lock for lock in locks if lock_is_active(lock)]
    if args.format == "json":
        print(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "locks": locks,
                    "conflict_pairs": [
                        {
                            "left": left.get("lock_id"),
                            "right": right.get("lock_id"),
                            "left_scope": left.get("scope"),
                            "right_scope": right.get("scope"),
                        }
                        for left, right in lock_conflict_pairs(active_locks())
                    ],
                    "reported_at": utc_now(),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if not locks:
        print("locks: none")
        return 0
    for lock in locks:
        active = "active" if lock_is_active(lock) else str(lock.get("status", "unknown"))
        print(
            "{lock_id} group={group_id} owner={owner} scope={scope} "
            "status={status} lease_expires_at={lease_expires_at}".format(
                lock_id=lock.get("lock_id", ""),
                group_id=lock.get("group_id", ""),
                owner=lock.get("owner", ""),
                scope=lock.get("scope", ""),
                status=active,
                lease_expires_at=lock.get("lease_expires_at", ""),
            )
        )
    return 0


def command_lock(args: argparse.Namespace) -> int:
    if args.lock_command == "acquire":
        return command_lock_acquire(args)
    if args.lock_command == "release":
        return command_lock_release(args)
    if args.lock_command == "status":
        return command_lock_status(args)
    raise SystemExit("unknown lock command")


def collect_messages(group_id: str) -> list[dict[str, Any]]:
    root = group_dir(group_id)
    rows: list[dict[str, Any]] = []
    for path in (root / "main-outbox.jsonl", root / "main-inbox.jsonl", root / "broadcasts.jsonl"):
        rows.extend(read_jsonl(path))
    for agent in load_agents(group_id):
        agent_id = str(agent.get("agent_id", ""))
        if not agent_id:
            continue
        rows.extend(read_jsonl(agent_dir(group_id, agent_id) / "inbox.jsonl"))
        rows.extend(read_jsonl(agent_dir(group_id, agent_id) / "outbox.jsonl"))
    return rows


def build_report(group_id: str) -> dict[str, Any]:
    group = load_group(group_id)
    agents = load_agents(group_id)
    messages = collect_messages(group_id)
    acked = {
        str(row.get("ack_message_id"))
        for row in messages
        if row.get("type") == "ack" and row.get("ack_message_id")
    }
    sent_requiring_ack = {
        str(row.get("message_id")): row
        for row in messages
        if row.get("requires_ack") and row.get("message_id")
    }
    heartbeats: list[dict[str, Any]] = []
    artifacts = 0
    for agent in agents:
        agent_id = str(agent.get("agent_id", ""))
        if not agent_id:
            continue
        heartbeat = read_json(agent_dir(group_id, agent_id) / "heartbeat.json", {})
        if heartbeat:
            heartbeats.append(heartbeat)
        artifacts += len(read_jsonl(agent_dir(group_id, agent_id) / "artifacts.jsonl"))
    group_locks = active_locks(group_id)
    conflicts = [
        {
            "left": left.get("lock_id"),
            "right": right.get("lock_id"),
            "left_group": left.get("group_id"),
            "right_group": right.get("group_id"),
            "left_scope": left.get("scope"),
            "right_scope": right.get("scope"),
        }
        for left, right in lock_conflict_pairs(active_locks())
        if left.get("group_id") == group_id or right.get("group_id") == group_id
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "group": group,
        "agent_count": len(agents),
        "agents": agents,
        "message_count": len(messages),
        "ack_required_count": len(sent_requiring_ack),
        "acked_count": len(sent_requiring_ack.keys() & acked),
        "unacked_messages": sorted(set(sent_requiring_ack) - acked),
        "heartbeat_count": len(heartbeats),
        "heartbeats": heartbeats,
        "artifact_count": artifacts,
        "active_lock_count": len(group_locks),
        "active_locks": group_locks,
        "lock_conflicts": conflicts,
        "reported_at": utc_now(),
    }


def command_report(args: argparse.Namespace) -> int:
    report = build_report(args.group)
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    group = report["group"]
    print(f"通信组: {group.get('group_id')} {group.get('group_title')}")
    print(
        "agents={agent_count} messages={message_count} ack={acked_count}/{ack_required_count} "
        "heartbeats={heartbeat_count} artifacts={artifact_count} "
        "active_locks={active_lock_count}".format(**report)
    )
    if report["unacked_messages"]:
        print("未确认消息:")
        for message_id in report["unacked_messages"]:
            print(f"- {message_id}")
    else:
        print("未确认消息: 无")
    if report["lock_conflicts"]:
        print("锁冲突:")
        for conflict in report["lock_conflicts"]:
            print(
                "- {left}({left_group}:{left_scope}) <-> "
                "{right}({right_group}:{right_scope})".format(**conflict)
            )
    else:
        print("锁冲突: 无")
    return 0


def command_validate(args: argparse.Namespace) -> int:
    root = group_dir(args.group)
    errors: list[str] = []
    warnings: list[str] = []
    if not root.exists():
        errors.append(f"group directory not found: {root}")
    for relative in ("group.json", "agents.json", "main-outbox.jsonl", "main-inbox.jsonl", "broadcasts.jsonl"):
        if not (root / relative).exists():
            errors.append(f"missing {relative}")
    try:
        report = build_report(args.group)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - validation report.
        errors.append(str(exc))
        report = {}
    mailbox_files = [
        root / "main-outbox.jsonl",
        root / "main-inbox.jsonl",
        root / "broadcasts.jsonl",
    ]
    try:
        agents = load_agents(args.group)
    except Exception:  # noqa: BLE001 - reported by other validation checks.
        agents = []
    for agent in agents:
        agent_id = str(agent.get("agent_id", ""))
        if not agent_id:
            continue
        mailbox_files.append(agent_dir(args.group, agent_id) / "inbox.jsonl")
        mailbox_files.append(agent_dir(args.group, agent_id) / "outbox.jsonl")
        mailbox_files.append(agent_dir(args.group, agent_id) / "artifacts.jsonl")
    for path in mailbox_files:
        rows = read_jsonl(path)
        seen: set[str] = set()
        for row in rows:
            key = str(row.get("message_id") or row.get("artifact_id") or "")
            if not key:
                continue
            if key in seen:
                warnings.append(f"duplicate id in {path}: {key}")
            seen.add(key)
    if report:
        for message_id in report.get("unacked_messages", []):
            warnings.append(f"message awaiting ack: {message_id}")
        for agent in report.get("agents", []):
            agent_id = str(agent.get("agent_id", ""))
            if agent_id and not (agent_dir(args.group, agent_id) / "heartbeat.json").exists():
                warnings.append(f"missing heartbeat for {agent_id}")
        active_statuses = {"running", "in_progress", "执行中", "运行中"}
        stale_after = timedelta(minutes=max(args.heartbeat_stale_minutes, 1))
        for heartbeat in report.get("heartbeats", []):
            status = str(heartbeat.get("status", ""))
            updated_at = parse_time(str(heartbeat.get("updated_at", "")))
            if status in active_statuses and updated_at is not None:
                if now_datetime() - updated_at > stale_after:
                    warnings.append(
                        "stale heartbeat for {agent}: {updated_at}".format(
                            agent=heartbeat.get("agent_id", ""),
                            updated_at=heartbeat.get("updated_at", ""),
                        )
                    )
        for conflict in report.get("lock_conflicts", []):
            errors.append(
                "lock conflict: {left}({left_group}:{left_scope}) <-> "
                "{right}({right_group}:{right_scope})".format(**conflict)
            )
        group = report.get("group", {})
        declared_scopes = [normalize_scope(scope) for scope in group.get("write_scopes", [])]
        for lock in report.get("active_locks", []):
            lock_scope = normalize_scope(str(lock.get("scope", "")))
            if declared_scopes and not any(scopes_overlap(lock_scope, scope) for scope in declared_scopes):
                warnings.append(
                    "lock scope outside declared write_scopes: "
                    f"{lock.get('lock_id')} scope={lock_scope}"
                )
    if errors:
        print("agent comm validation failed:")
        for error in errors:
            print(f"- {error}")
        return 2
    print("agent comm validation passed")
    if warnings:
        print("warnings:")
        for warning in warnings:
            print(f"- {warning}")
    return 0


def add_common_message_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("group", help="Communication group id.")
    parser.add_argument("--from", dest="sender", default="main", help="Sender id.")
    parser.add_argument("--subject", default="", help="Short subject.")
    parser.add_argument("--body", required=True, help="Message body.")
    parser.add_argument("--type", default="message", help="Message type.")
    parser.add_argument("--requires-ack", action="store_true", help="Require an ack event.")
    parser.add_argument("--metadata", help="JSON object with extra metadata.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Manage .ai-client/project/agents/comm local sub-agent communication files."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Initialize a communication group.")
    init.add_argument("group", help="Communication group id.")
    init.add_argument("--title", required=True, help="Group title.")
    init.add_argument("--tracking", default="", help="Task tracking file.")
    init.add_argument("--pending", default="", help="Pending file.")
    init.add_argument("--approval", default="", help="Approval label.")
    init.add_argument("--status", default="initialized", help="Group status.")
    init.add_argument("--notes", default="", help="Free-form notes.")
    init.add_argument("--write-scope", action="append", help="Declared writable path scope.")
    init.set_defaults(func=command_init)

    register = sub.add_parser("register", help="Register an agent mailbox.")
    register.add_argument("group", help="Communication group id.")
    register.add_argument("agent", help="Agent id.")
    register.add_argument("--nickname", default="", help="Agent nickname.")
    register.add_argument("--role", default="", help="Agent role.")
    register.add_argument("--brief", default="", help="Agent brief path.")
    register.add_argument("--status", default="registered", help="Agent status.")
    register.add_argument("--token-usage-source", default="unavailable", help="Token usage source.")
    register.add_argument("--reuse-key", default="", help="Stable key used to decide whether this agent context can be reused.")
    register.add_argument(
        "--context-reuse",
        choices=("new", "reuse", "spawn", "merge", "close", "forbidden", "unknown"),
        default="unknown",
        help="Context reuse decision recorded for this agent.",
    )
    register.add_argument("--context-capsule", default="", help="Path to a reusable context capsule or summary artifact.")
    register.add_argument("--context-budget", default="", help="Token or proxy budget for this agent context.")
    register.add_argument("--context-ttl", default="", help="Freshness window for reusing this agent context.")
    register.add_argument("--contamination-boundary", default="", help="Reason this context is safe or unsafe to reuse across scopes.")
    register.add_argument("--minimal-resume-input", action="append", help="Minimal input needed to resume this agent without full history.")
    register.add_argument("--token-proxy-metric", action="append", help="Proxy metric for token cost, such as brief lines or estimated read lines.")
    register.add_argument("--retained-fact", action="append", help="Fact retained in this agent context.")
    register.add_argument("--skip-input", action="append", help="Input already covered by a capsule and safe to skip.")
    register.set_defaults(func=command_register)

    send = sub.add_parser("send", help="Send a message to one recipient.")
    add_common_message_args(send)
    send.add_argument("--to", required=True, help="Recipient id.")
    send.set_defaults(func=command_send)

    broadcast = sub.add_parser("broadcast", help="Append a broadcast message.")
    add_common_message_args(broadcast)
    broadcast.add_argument("--copy-to-agents", action="store_true", help="Copy broadcast into agent inboxes.")
    broadcast.set_defaults(func=command_broadcast)

    read = sub.add_parser("read", help="Read a mailbox.")
    read.add_argument("group", help="Communication group id.")
    read.add_argument(
        "box",
        choices=("main-inbox", "main-outbox", "broadcasts", "inbox", "outbox", "artifacts"),
        help="Mailbox to read.",
    )
    read.add_argument("--agent", help="Agent id for agent inbox/outbox/artifacts.")
    read.add_argument("--limit", type=int, default=20, help="Last N messages. 0 means all.")
    read.add_argument("--format", choices=("text", "json"), default="text", help="Output format.")
    read.add_argument("--show-body", action="store_true", help="Print message bodies in text output.")
    read.set_defaults(func=command_read)

    ack = sub.add_parser("ack", help="Record an acknowledgement.")
    ack.add_argument("group", help="Communication group id.")
    ack.add_argument("message", help="Message id being acknowledged.")
    ack.add_argument("--from", dest="sender", required=True, help="Sender id.")
    ack.add_argument("--to", default="main", help="Ack recipient.")
    ack.add_argument("--note", default="", help="Ack note.")
    ack.add_argument("--metadata", help="JSON object with extra metadata.")
    ack.set_defaults(func=command_ack)

    heartbeat = sub.add_parser("heartbeat", help="Write an agent heartbeat.")
    heartbeat.add_argument("group", help="Communication group id.")
    heartbeat.add_argument("agent", help="Agent id.")
    heartbeat.add_argument("--status", default="running", help="Agent status.")
    heartbeat.add_argument("--note", default="", help="Heartbeat note.")
    heartbeat.add_argument("--file", action="append", help="Current file path.")
    heartbeat.add_argument("--token-usage-source", default="unavailable", help="Token usage source.")
    heartbeat.add_argument("--reuse-key", default="", help="Stable key used to decide whether this agent context can be reused.")
    heartbeat.add_argument(
        "--context-reuse",
        choices=("new", "reuse", "spawn", "merge", "close", "forbidden", "unknown"),
        default="unknown",
        help="Context reuse decision recorded for this heartbeat.",
    )
    heartbeat.add_argument("--context-capsule", default="", help="Path to a reusable context capsule or summary artifact.")
    heartbeat.add_argument("--context-budget", default="", help="Token or proxy budget for this agent context.")
    heartbeat.add_argument("--context-ttl", default="", help="Freshness window for reusing this agent context.")
    heartbeat.add_argument("--contamination-boundary", default="", help="Reason this context is safe or unsafe to reuse across scopes.")
    heartbeat.add_argument("--minimal-resume-input", action="append", help="Minimal input needed to resume this agent without full history.")
    heartbeat.add_argument("--token-proxy-metric", action="append", help="Proxy metric for token cost, such as brief lines or estimated read lines.")
    heartbeat.add_argument("--metadata", help="JSON object with extra metadata.")
    heartbeat.set_defaults(func=command_heartbeat)

    artifact = sub.add_parser("artifact", help="Record an artifact path.")
    artifact.add_argument("group", help="Communication group id.")
    artifact.add_argument("agent", help="Agent id.")
    artifact.add_argument("--path", required=True, help="Artifact path.")
    artifact.add_argument("--kind", default="file", help="Artifact kind.")
    artifact.add_argument("--summary", default="", help="Artifact summary.")
    artifact.add_argument("--metadata", help="JSON object with extra metadata.")
    artifact.set_defaults(func=command_artifact)

    lock = sub.add_parser("lock", help="Manage write-scope locks.")
    lock_sub = lock.add_subparsers(dest="lock_command", required=True)

    lock_acquire = lock_sub.add_parser("acquire", help="Acquire a write-scope lock.")
    lock_acquire.add_argument("group", help="Communication group id.")
    lock_acquire.add_argument("--owner", required=True, help="Owner agent or controller id.")
    lock_acquire.add_argument("--scope", required=True, help="Path or scope to lock.")
    lock_acquire.add_argument("--lease-minutes", type=int, default=60, help="Lease duration in minutes.")
    lock_acquire.add_argument("--note", default="", help="Lock note.")
    lock_acquire.add_argument("--force", action="store_true", help="Record lock even when conflicts exist.")

    lock_release = lock_sub.add_parser("release", help="Release active locks.")
    lock_release.add_argument("group", help="Communication group id.")
    lock_release.add_argument("--lock-id", help="Specific lock id.")
    lock_release.add_argument("--owner", help="Owner agent or controller id.")
    lock_release.add_argument("--scope", help="Path or scope to release.")
    lock_release.add_argument("--note", default="", help="Release note.")

    lock_status = lock_sub.add_parser("status", help="Show locks.")
    lock_status.add_argument("--group", help="Only show one group.")
    lock_status.add_argument("--active-only", action="store_true", help="Only show active non-expired locks.")
    lock_status.add_argument("--format", choices=("text", "json"), default="text", help="Output format.")
    lock.set_defaults(func=command_lock)

    report = sub.add_parser("report", help="Print group communication summary.")
    report.add_argument("group", help="Communication group id.")
    report.add_argument("--format", choices=("text", "json"), default="text", help="Output format.")
    report.set_defaults(func=command_report)

    validate = sub.add_parser("validate", help="Validate a communication group.")
    validate.add_argument("group", help="Communication group id.")
    validate.add_argument(
        "--heartbeat-stale-minutes",
        type=int,
        default=60,
        help="Warn when running heartbeats are older than this many minutes.",
    )
    validate.set_defaults(func=command_validate)

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
