#!/usr/bin/env python3
"""Shell adapter entry point for fail-closed execution telemetry enforcement."""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
import tempfile
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ai_client_governance.records import telemetry, tool_invocations
from ai_client_governance.runtime.scope import classify_scope


ADAPTER_MARKER_BEGIN = "# >>> ai-client-governance shell-adapter >>>"
ADAPTER_MARKER_END = "# <<< ai-client-governance shell-adapter <<<"
POWERSHELL_PROXY_ENFORCEMENT = "powershell-command-proxy"
POWERSHELL_PROXY_ENV = "AICG_COMMAND_PROXY"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def command_to_string(command: list[str]) -> str:
    return " ".join(command).strip()


def powershell_executable(preferred: str | None = None) -> str:
    if preferred:
        return preferred
    return "powershell" if os.name == "nt" else "pwsh"


def is_command_proxy_event(event: dict[str, Any]) -> bool:
    shell_adapter = event.get("shell_adapter")
    shell_payload = shell_adapter if isinstance(shell_adapter, dict) else {}
    mode = str(shell_payload.get("mode") or "")
    enforcement = str(event.get("adapter_enforcement") or "")
    return (
        mode == POWERSHELL_PROXY_ENFORCEMENT
        or enforcement == POWERSHELL_PROXY_ENFORCEMENT
        or str(shell_payload.get("command_proxy") or "") == "true"
    )


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
            "mode": getattr(args, "adapter_mode", "run"),
            "cwd": str(cwd),
            "fail_policy": "fail_closed",
            **getattr(args, "shell_adapter_extra", {}),
        },
    )
    event["source"] = "ai_client_governance.py shell-adapter"
    return event


def command_vector(args: argparse.Namespace) -> tuple[list[str], str]:
    if args.powershell_command:
        shell = powershell_executable(args.powershell_exe)
        command = [shell, "-NoProfile", "-Command", args.powershell_command]
        return command, args.powershell_command
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    return command, command_to_string(command)


def powershell_proxy_script() -> str:
    return r"""
$ErrorActionPreference = "Continue"
try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
} catch {
}
$env:AICG_SHELL_ADAPTER = "powershell-command-proxy"
$env:AICG_EXECUTION_TELEMETRY_ENFORCEMENT = "powershell-command-proxy"
$env:AICG_COMMAND_PROXY = "powershell"
if (-not $env:AICG_PROXY_COMMAND_B64) {
    Write-Error "AICG_PROXY_COMMAND_B64 is required"
    exit 2
}
try {
    $bytes = [Convert]::FromBase64String($env:AICG_PROXY_COMMAND_B64)
    $command = [System.Text.Encoding]::UTF8.GetString($bytes)
    Invoke-Expression $command
    if ($null -ne $global:LASTEXITCODE) {
        exit $global:LASTEXITCODE
    }
    if (-not $?) {
        exit 1
    }
    exit 0
} catch {
    Write-Error $_
    exit 1
}
""".lstrip()


def powershell_proxy_command(args: argparse.Namespace, command_text: str) -> tuple[list[str], dict[str, str], dict[str, str]]:
    if os.name != "nt" and not args.allow_non_windows:
        raise RuntimeError("proxy-powershell is currently implemented for Windows PowerShell hosts only")
    shell = powershell_executable(args.powershell_exe)
    command_b64 = base64.b64encode(command_text.encode("utf-8")).decode("ascii")
    extra_env = {
        "AICG_PROXY_COMMAND_B64": command_b64,
        POWERSHELL_PROXY_ENV: "powershell",
        "AICG_SHELL_ADAPTER": POWERSHELL_PROXY_ENFORCEMENT,
        "AICG_EXECUTION_TELEMETRY_ENFORCEMENT": POWERSHELL_PROXY_ENFORCEMENT,
    }
    metadata = {
        "profile_policy": "no_profile",
        "profile_touched": "false",
        "platform": "windows" if os.name == "nt" else "non-windows-pwsh",
        "shell_executable": shell,
        "command_proxy": "true",
    }
    return [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass"], extra_env, metadata


def run_powershell_proxy(args: argparse.Namespace) -> int:
    raw_command = list(args.command)
    if raw_command and raw_command[0] == "--":
        raw_command = raw_command[1:]
    command = args.powershell_command or command_to_string(raw_command)
    if not command:
        print("shell-adapter proxy-powershell requires --powershell-command or a command after --", file=sys.stderr)
        return 2
    try:
        base_command, proxy_env, proxy_metadata = powershell_proxy_command(args, command)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix="aicg-shell-proxy-") as tmp:
        script_path = Path(tmp) / "Invoke-AicgCommandProxy.ps1"
        script_path.write_text(powershell_proxy_script(), encoding="utf-8", newline="\n")
        args.command = [*base_command, "-File", str(script_path)]
        args.powershell_command = ""
        args.adapter_enforcement = POWERSHELL_PROXY_ENFORCEMENT
        args.adapter_mode = POWERSHELL_PROXY_ENFORCEMENT
        args.command_text_override = command
        args.shell_adapter_extra = {
            **proxy_metadata,
            "temporary_script": str(script_path),
        }
        args.child_env_extra = proxy_env
        return run_command(args)


def run_command(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd().resolve()
    command, command_text = command_vector(args)
    command_text = getattr(args, "command_text_override", "") or command_text
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
    child_env.update(telemetry.env_for_child(trace_id=args.trace_id or child_env.get("CODEX_TRACE_ID", ""), parent_span_id=invocation_id))
    child_env["CODEX_TRACE_ID"] = args.trace_id or child_env.get("CODEX_TRACE_ID", invocation_id)
    child_env["CODEX_PARENT_INVOCATION_ID"] = invocation_id
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env["PYTHONUTF8"] = "1"
    child_env.update(getattr(args, "child_env_extra", {}))
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
    command_proxy_events = [event for event in adapter_events if is_command_proxy_event(event)]
    command_proxy_env = bool(os.environ.get(POWERSHELL_PROXY_ENV))
    command_proxy_ready = command_proxy_env or bool(command_proxy_events)
    raw_shell_coverage_ready = auto_intercept_ready or command_proxy_ready
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
        "command_proxy": {
            "supported_platforms": ["windows-powershell"],
            "implemented_platforms": ["windows-powershell"],
            "env_active": command_proxy_env,
            "event_count": len(command_proxy_events),
            "no_profile_event_count": len(
                [
                    event
                    for event in command_proxy_events
                    if isinstance(event.get("shell_adapter"), dict)
                    and event["shell_adapter"].get("profile_policy") == "no_profile"
                ]
            ),
            "latest_event": command_proxy_events[-1] if command_proxy_events else {},
            "coverage_ready": command_proxy_ready,
        },
        "telemetry": {
            "event_count": len(adapter_events),
            "latest_event": adapter_events[-1] if adapter_events else {},
        },
        "raw_shell_coverage_ready": raw_shell_coverage_ready,
        "fail_closed_ready": raw_shell_coverage_ready,
    }
    requirement_failures: list[str] = []
    if args.require_auto_intercept and not auto_intercept_ready:
        requirement_failures.append("shell-adapter-auto-intercept")
    if (args.require_adapter or args.require_fail_closed or args.require_raw_shell_coverage) and not raw_shell_coverage_ready:
        requirement_failures.append("shell-adapter-raw-shell-coverage")
    if args.require_command_proxy and not command_proxy_ready:
        requirement_failures.append("shell-adapter-command-proxy")
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
        print(f"Command proxy events: {payload['command_proxy']['event_count']}")
        print(f"Raw shell coverage ready: {payload['raw_shell_coverage_ready']}")
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

    proxy = sub.add_parser("proxy-powershell", help="Run a command through an isolated PowerShell command proxy without touching user profiles.")
    proxy.add_argument("--name", help="Tool or command name.")
    proxy.add_argument("--task-id", help="Structured task id.")
    proxy.add_argument("--task-tracking", help="Related task tracking file.")
    proxy.add_argument("--task-type", action="append", help="Related task type.")
    proxy.add_argument("--phase", help="Task phase.")
    proxy.add_argument("--summary", help="Short result summary.")
    proxy.add_argument("--trace-id", help="Trace id.")
    proxy.add_argument("--task-node-id", help="Task tree node id associated with this invocation.")
    proxy.add_argument("--cwd", help="Working directory for the command.")
    proxy.add_argument("--scope-path", action="append", help="Path used for common/project/native scope classification.")
    proxy.add_argument("--scope-kind", help="Explicit governance scope kind.")
    proxy.add_argument("--scope-reason", help="Explicit governance scope reason.")
    proxy.add_argument("--powershell-command", help="PowerShell command string to run through the proxy.")
    proxy.add_argument("--powershell-exe", help="PowerShell executable. Defaults to Windows PowerShell on Windows, pwsh elsewhere.")
    proxy.add_argument("--allow-non-windows", action="store_true", help="Allow pwsh-based proxy execution on non-Windows hosts when available.")
    proxy.add_argument("--format", choices=("text", "json"), default="text")
    proxy.add_argument("command", nargs=argparse.REMAINDER)
    proxy.set_defaults(func=run_powershell_proxy)

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
    diag.add_argument("--require-adapter", action="store_true", help="Exit non-zero unless raw shell coverage exists through auto-intercept or command proxy evidence.")
    diag.add_argument("--require-fail-closed", action="store_true", help="Exit non-zero unless raw shell coverage exists through auto-intercept or command proxy evidence.")
    diag.add_argument("--require-auto-intercept", action="store_true", help="Exit non-zero unless adapter env/profile auto-intercept is installed.")
    diag.add_argument("--require-command-proxy", action="store_true", help="Exit non-zero unless command proxy evidence exists.")
    diag.add_argument("--require-raw-shell-coverage", action="store_true", help="Exit non-zero unless auto-intercept or command proxy evidence exists.")
    diag.add_argument("--require-telemetry", action="store_true", help="Exit non-zero unless shell-adapter telemetry evidence exists.")
    diag.add_argument("--format", choices=("text", "json"), default="text")
    diag.set_defaults(func=diagnose)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
