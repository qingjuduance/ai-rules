#!/usr/bin/env python3
"""Shell adapter entry point for fail-closed execution telemetry enforcement."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ai_client_governance.records import tool_invocations
from ai_client_governance.runtime.scope import classify_scope


ADAPTER_MARKER_BEGIN = "# >>> ai-client-governance shell-adapter >>>"
ADAPTER_MARKER_END = "# <<< ai-client-governance shell-adapter <<<"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def command_to_string(command: list[str]) -> str:
    return " ".join(command).strip()


def adapter_event(
    *,
    args: argparse.Namespace,
    invocation_id: str,
    command_text: str,
    status: str,
    started_at: str,
    ended_at: str | None = None,
    exit_code: int | None = None,
    duration_ms: int | None = None,
    summary: str = "",
) -> dict[str, Any]:
    root = Path(args.root).resolve()
    cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd().resolve()
    scope = classify_scope(root=root, paths=args.scope_path or [], command=command_text, cwd=cwd)
    event = tool_invocations.make_event(
        invocation_id=invocation_id,
        name=args.name or tool_invocations.infer_name(command_text, "shell-adapter"),
        command=command_text,
        status=status,
        task_tracking=args.task_tracking or "",
        task_types=args.task_type or [],
        phase=args.phase or "shell-adapter",
        final_gate=False,
        exit_code=exit_code,
        started_at=started_at,
        ended_at=ended_at,
        duration_ms=duration_ms,
        summary=summary,
        trace_id=args.trace_id or os.environ.get("CODEX_TRACE_ID", invocation_id),
        parent_invocation_id=os.environ.get("CODEX_PARENT_INVOCATION_ID", ""),
        task_node_id=args.task_node_id or os.environ.get("CODEX_TASK_NODE_ID", ""),
        parent_task_node_id=os.environ.get("CODEX_PARENT_TASK_NODE_ID", ""),
        event_type="shell-adapter",
        task_id=args.task_id or "",
        scope_kind=args.scope_kind or scope.scope_kind,
        scope_reason=args.scope_reason or scope.scope_reason,
        scope_paths=scope.paths,
        adapter_enforcement=args.adapter_enforcement or "shell-adapter",
        shell_adapter={
            "adapter": "ai_client_governance.py shell-adapter",
            "mode": "run",
            "cwd": str(cwd),
            "fail_policy": "fail_closed",
        },
    )
    event["source"] = "ai_client_governance.py shell-adapter"
    return event


def command_vector(args: argparse.Namespace) -> tuple[list[str], str]:
    if args.powershell_command:
        shell = args.powershell_exe or ("pwsh" if os.name != "nt" else "powershell")
        command = [shell, "-NoProfile", "-Command", args.powershell_command]
        return command, args.powershell_command
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    return command, command_to_string(command)


def run_command(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd().resolve()
    command, command_text = command_vector(args)
    if not command:
        print("shell-adapter run requires a command after -- or --powershell-command", file=sys.stderr)
        return 2

    invocation_id = str(uuid.uuid4())
    started_at = now_iso()
    start_event = adapter_event(
        args=args,
        invocation_id=invocation_id,
        command_text=command_text,
        status="started",
        started_at=started_at,
        summary=args.summary or "shell adapter command started",
    )
    tool_invocations.append_event(root, args.jsonl_artifact_dir, start_event, args.db)

    child_env = os.environ.copy()
    child_env["AICG_SHELL_ADAPTER"] = "shell-adapter"
    child_env["AICG_EXECUTION_TELEMETRY_ENFORCEMENT"] = args.adapter_enforcement or "shell-adapter"
    child_env["CODEX_TRACE_ID"] = args.trace_id or child_env.get("CODEX_TRACE_ID", invocation_id)
    child_env["CODEX_PARENT_INVOCATION_ID"] = invocation_id
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env["PYTHONUTF8"] = "1"
    start = time.monotonic()
    completed = subprocess.run(command, cwd=cwd, env=child_env)
    ended_at = now_iso()
    duration_ms = int((time.monotonic() - start) * 1000)
    status = "succeeded" if completed.returncode == 0 else "failed"
    end_event = adapter_event(
        args=args,
        invocation_id=invocation_id,
        command_text=command_text,
        status=status,
        started_at=started_at,
        ended_at=ended_at,
        exit_code=completed.returncode,
        duration_ms=duration_ms,
        summary=args.summary or f"shell adapter command {status}",
    )
    path = tool_invocations.append_event(root, args.jsonl_artifact_dir, end_event, args.db)
    if args.format == "json":
        print(
            json.dumps(
                {
                    "status": status,
                    "exit_code": completed.returncode,
                    "telemetry_path": str(path),
                    "invocation_id": invocation_id,
                    "scope_kind": end_event.get("scope_kind"),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(f"shell-adapter {status} exit={completed.returncode} telemetry={path}")
    return completed.returncode


def profile_snippet(args: argparse.Namespace) -> int:
    root = str(Path(args.root).resolve()).replace("\\", "/")
    script = str(Path(args.script_path).resolve()).replace("\\", "/") if args.script_path else "ai_client_governance.py"
    snippet = f"""{ADAPTER_MARKER_BEGIN}
$env:AICG_SHELL_ADAPTER = "powershell-profile"
$env:AICG_EXECUTION_TELEMETRY_ENFORCEMENT = "shell-adapter"
function Invoke-AicgShellCommand {{
    param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Command)
    if (-not $Command -or $Command.Count -eq 0) {{ return }}
    & python "{script}" shell-adapter run --root "{root}" --cwd (Get-Location).Path -- @Command
}}
Set-Alias aicgsh Invoke-AicgShellCommand
{ADAPTER_MARKER_END}
"""
    print(snippet)
    return 0


def install_powershell(args: argparse.Namespace) -> int:
    profile = Path(args.profile_path).expanduser()
    snippet_args = argparse.Namespace(root=args.root, script_path=args.script_path)
    root = str(Path(snippet_args.root).resolve()).replace("\\", "/")
    script = str(Path(snippet_args.script_path).resolve()).replace("\\", "/") if snippet_args.script_path else "ai_client_governance.py"
    snippet = f"""{ADAPTER_MARKER_BEGIN}
$env:AICG_SHELL_ADAPTER = "powershell-profile"
$env:AICG_EXECUTION_TELEMETRY_ENFORCEMENT = "shell-adapter"
function Invoke-AicgShellCommand {{
    param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Command)
    if (-not $Command -or $Command.Count -eq 0) {{ return }}
    & python "{script}" shell-adapter run --root "{root}" --cwd (Get-Location).Path -- @Command
}}
Set-Alias aicgsh Invoke-AicgShellCommand
{ADAPTER_MARKER_END}
"""
    existing = profile.read_text(encoding="utf-8") if profile.exists() else ""
    before, sep, rest = existing.partition(ADAPTER_MARKER_BEGIN)
    if sep:
        _old, end_sep, after = rest.partition(ADAPTER_MARKER_END)
        existing = before.rstrip() + "\n" + (after.lstrip() if end_sep else "")
    if not args.execute:
        print(
            json.dumps(
                {
                    "would_write": str(profile),
                    "execute_required": True,
                    "marker_present": bool(sep),
                    "snippet": snippet,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text(existing.rstrip() + "\n\n" + snippet, encoding="utf-8", newline="\n")
    print(f"installed shell-adapter profile shim: {profile}")
    return 0


def diagnose(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    events = tool_invocations.read_events(root, args.jsonl_artifact_dir, args.db)
    terminal = [event for event in events if event.get("status") != "started"]
    if args.task_id:
        terminal = [event for event in terminal if str(event.get("task_id") or "") == args.task_id]
    adapter_events = [
        event
        for event in terminal
        if event.get("event_type") == "shell-adapter"
        or event.get("source") == "ai_client_governance.py shell-adapter"
        or bool(event.get("shell_adapter"))
    ]
    profile_text = ""
    if args.profile_path:
        profile = Path(args.profile_path).expanduser()
        profile_text = profile.read_text(encoding="utf-8") if profile.exists() else ""
    profile_installed = ADAPTER_MARKER_BEGIN in profile_text and ADAPTER_MARKER_END in profile_text
    env_installed = bool(os.environ.get("AICG_SHELL_ADAPTER"))
    auto_intercept_ready = env_installed or profile_installed
    scope_counts = Counter(str(event.get("scope_kind") or "unknown") for event in adapter_events)
    payload = {
        "adapter": "ai_client_governance.py shell-adapter",
        "installed": auto_intercept_ready,
        "env_installed": env_installed,
        "profile_installed": profile_installed,
        "event_count": len(adapter_events),
        "scope_kind_counts": dict(sorted(scope_counts.items())),
        "latest_event": adapter_events[-1] if adapter_events else {},
        "auto_intercept": {
            "installed": auto_intercept_ready,
            "env_installed": env_installed,
            "profile_installed": profile_installed,
        },
        "telemetry": {
            "event_count": len(adapter_events),
            "latest_event": adapter_events[-1] if adapter_events else {},
        },
        "fail_closed_ready": auto_intercept_ready,
    }
    requirement_failures: list[str] = []
    if (
        args.require_adapter
        or args.require_fail_closed
        or args.require_auto_intercept
    ) and not payload["fail_closed_ready"]:
        requirement_failures.append("shell-adapter-auto-intercept")
    if args.require_telemetry and not adapter_events:
        requirement_failures.append("shell-adapter-telemetry")
    payload["requirements"] = {
        "failed": requirement_failures,
        "passed": not requirement_failures,
    }
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print("AI Client Governance Shell Adapter Diagnostics")
        print(f"Installed: {payload['installed']}")
        print(f"Adapter events: {payload['event_count']}")
        print(f"Fail-closed ready: {payload['fail_closed_ready']}")
        print(f"Scope kinds: {json.dumps(payload['scope_kind_counts'], ensure_ascii=False, sort_keys=True)}")
        print(f"Requirement failures: {', '.join(requirement_failures) or '<none>'}")
    if requirement_failures:
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run local shell commands through an AI Client Governance telemetry adapter.")
    parser.add_argument("--root", default=".", help="Host project root. Default: current directory.")
    parser.add_argument("--jsonl-artifact-dir", help="Explicit JSONL artifact directory for isolated tests or exports.")
    parser.add_argument("--db", help="SQLite telemetry DB. Default: <root>/.ai-client/project/state/aicg.db.")
    sub = parser.add_subparsers(dest="command_name", required=True)

    run = sub.add_parser("run", help="Run a command through the shell adapter and write telemetry events.")
    run.add_argument("--name", help="Tool or command name.")
    run.add_argument("--task-id", help="Structured task id.")
    run.add_argument("--task-tracking", help="Related task tracking file.")
    run.add_argument("--task-type", action="append", help="Related task type.")
    run.add_argument("--phase", help="Task phase.")
    run.add_argument("--summary", help="Short result summary.")
    run.add_argument("--trace-id", help="Trace id.")
    run.add_argument("--task-node-id", help="Task tree node id associated with this invocation.")
    run.add_argument("--cwd", help="Working directory for the command.")
    run.add_argument("--scope-path", action="append", help="Path used for common/project/native scope classification.")
    run.add_argument("--scope-kind", help="Explicit governance scope kind.")
    run.add_argument("--scope-reason", help="Explicit governance scope reason.")
    run.add_argument("--adapter-enforcement", help="Telemetry enforcement label. Default: shell-adapter.")
    run.add_argument("--powershell-command", help="Run a PowerShell command string through powershell -NoProfile -Command.")
    run.add_argument("--powershell-exe", help="PowerShell executable for --powershell-command.")
    run.add_argument("--format", choices=("text", "json"), default="text")
    run.add_argument("command", nargs=argparse.REMAINDER)
    run.set_defaults(func=run_command)

    snippet = sub.add_parser("profile-snippet", help="Print a PowerShell profile shim for the adapter.")
    snippet.add_argument("--script-path", help="Path to ai_client_governance.py for the shim.")
    snippet.set_defaults(func=profile_snippet)

    install = sub.add_parser("install-powershell", help="Install or refresh the PowerShell profile shim.")
    install.add_argument("--profile-path", required=True, help="PowerShell profile file to update.")
    install.add_argument("--script-path", help="Path to ai_client_governance.py for the shim.")
    install.add_argument("--execute", action="store_true", help="Actually write the profile file. Without this, print a plan.")
    install.set_defaults(func=install_powershell)

    diag = sub.add_parser("diagnose", help="Report shell adapter installation and telemetry evidence.")
    diag.add_argument("--task-id", help="Only include adapter events for one structured task id.")
    diag.add_argument("--profile-path", help="PowerShell profile file to inspect for the adapter marker.")
    diag.add_argument("--require-adapter", action="store_true", help="Exit non-zero unless adapter env/profile auto-intercept is installed.")
    diag.add_argument("--require-fail-closed", action="store_true", help="Exit non-zero unless adapter env/profile auto-intercept is installed.")
    diag.add_argument("--require-auto-intercept", action="store_true", help="Exit non-zero unless adapter env/profile auto-intercept is installed.")
    diag.add_argument("--require-telemetry", action="store_true", help="Exit non-zero unless shell-adapter telemetry evidence exists.")
    diag.add_argument("--format", choices=("text", "json"), default="text")
    diag.set_defaults(func=diagnose)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
