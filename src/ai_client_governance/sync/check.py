#!/usr/bin/env python3
"""Check an embedded ai-client-governance Git repository.

This is the cross-platform fact source for session sync checks. Platform
wrappers may call it, but the timing and Git-state decisions live here.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ai_client_governance.common.paths import AI_CLIENT_GOVERNANCE_STATE_PATH


STATE_SCHEMA_VERSION = 4


@dataclass(frozen=True)
class GitResult:
    exit_code: int
    text: str


@dataclass
class SyncReport:
    status: str
    target_project_path: str
    embedded_repo_path: str
    repo_label: str
    state_path: str
    remote_name: str
    remote_present: bool = False
    fetch_due: bool = False
    fetched: bool = False
    dirty: bool = False
    branch: str = ""
    upstream: str = ""
    ahead: int = 0
    behind: int = 0
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    next_actions: list[str] = field(default_factory=list)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check embedded ai-client-governance Git sync state.")
    parser.add_argument("--target-project-path", default=".", help="Target project path. Default: current directory.")
    parser.add_argument("--embedded-repo-path", help="Embedded ai-client-governance repo path.")
    parser.add_argument("--config-path", help="ai-client-governance config path.")
    parser.add_argument("--fetch-interval-hours", type=int, default=24, help="Fetch interval. Default: 24.")
    parser.add_argument("--remote-name", default="origin", help="Remote name. Default: origin.")
    parser.add_argument("--force-fetch", action="store_true", help="Fetch even if interval has not elapsed.")
    parser.add_argument("--no-fetch", action="store_true", help="Do not fetch.")
    parser.add_argument("--fail-on-warning", action="store_true", help="Exit 1 when warnings exist.")
    parser.add_argument("--format", choices=("text", "json"), default="text", help="Output format. Default: text.")
    return parser.parse_args()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return now_utc().isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def resolve_path(base: Path, value: str | None, default: str) -> Path:
    raw = value or default
    path = Path(raw)
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def display_path(project: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(project.resolve()).as_posix()
    except ValueError:
        return str(path)


def run_git(repo: Path, args: list[str]) -> GitResult:
    process = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return GitResult(exit_code=process.returncode, text=process.stdout.strip())


def fetch_due(state: dict[str, Any] | None, hours: int, force_fetch: bool, no_fetch: bool) -> bool:
    if force_fetch:
        return True
    if no_fetch:
        return False
    if not state or not state.get("last_fetch_at"):
        return True
    raw = str(state["last_fetch_at"]).replace("Z", "+00:00")
    try:
        last_fetch = datetime.fromisoformat(raw)
    except ValueError:
        return True
    if last_fetch.tzinfo is None:
        last_fetch = last_fetch.replace(tzinfo=timezone.utc)
    return (now_utc() - last_fetch.astimezone(timezone.utc)).total_seconds() >= hours * 3600


def write_state(path: Path, previous: dict[str, Any] | None, fetched: bool, status: str) -> None:
    now = iso_now()
    data = {
        "schema_version": STATE_SCHEMA_VERSION,
        "last_checked_at": now,
        "last_fetch_at": now if fetched else (previous or {}).get("last_fetch_at"),
        "last_status": status,
    }
    write_json(path, data)


def add_warning(report: SyncReport, message: str, next_action: str | None = None) -> None:
    report.warnings.append(message)
    if next_action:
        report.next_actions.append(next_action)


def check_sync(args: argparse.Namespace) -> SyncReport:
    project = Path(args.target_project_path).resolve()
    config_path = resolve_path(project, args.config_path, ".ai-client/ai-client-governance-config.json")
    config = read_json(config_path) or {}
    embedded_default = str(config.get("embeddedRepoPath") or ".ai-client/ai-client-governance")
    embedded_repo = resolve_path(project, args.embedded_repo_path, embedded_default)
    state_path = project / AI_CLIENT_GOVERNANCE_STATE_PATH
    state = read_json(state_path)
    repo_label = display_path(project, embedded_repo)
    report = SyncReport(
        status="ok",
        target_project_path=str(project),
        embedded_repo_path=str(embedded_repo),
        repo_label=repo_label,
        state_path=str(state_path),
        remote_name=args.remote_name,
    )

    if not embedded_repo.exists():
        add_warning(
            report,
            f"Embedded ai-client-governance repo is missing at {repo_label}. Embed it before rule work.",
            "Embed the configured ai-client-governance repository before rule work.",
        )
        report.status = "missing"
        write_state(state_path, state, fetched=False, status=report.status)
        return report

    inside = run_git(embedded_repo, ["rev-parse", "--is-inside-work-tree"])
    if inside.exit_code != 0 or inside.text != "true":
        add_warning(report, f"{repo_label} exists but is not a Git work tree.", "Review the embedded ai-client-governance path.")
        report.status = "not-git"
        write_state(state_path, state, fetched=False, status=report.status)
        return report

    remote = run_git(embedded_repo, ["remote", "get-url", args.remote_name])
    report.remote_present = remote.exit_code == 0 and bool(remote.text.strip())
    report.fetch_due = report.remote_present and fetch_due(
        state,
        args.fetch_interval_hours,
        args.force_fetch,
        args.no_fetch,
    )
    if report.remote_present and report.fetch_due:
        fetched = run_git(embedded_repo, ["fetch", args.remote_name])
        if fetched.exit_code == 0:
            report.fetched = True
            report.notes.append(f"Fetched {args.remote_name} for {repo_label}.")
        else:
            add_warning(report, f"Could not fetch {args.remote_name} for {repo_label}. {fetched.text}")
    elif report.remote_present:
        report.notes.append(f"Fetch skipped: last fetch is within {args.fetch_interval_hours} hours.")
    else:
        add_warning(
            report,
            f"{repo_label} has no remote named {args.remote_name}; update checks can only inspect local state.",
            "Configure an ai-client-governance remote or compare manually.",
        )

    status = run_git(embedded_repo, ["status", "--porcelain"])
    report.dirty = bool(status.text.strip())
    if report.dirty:
        add_warning(
            report,
            f"{repo_label} has local uncommitted changes. Commit, stash, or discard intentionally before syncing.",
            "Review and intentionally commit, stash, or discard local ai-client-governance changes.",
        )

    branch = run_git(embedded_repo, ["branch", "--show-current"])
    report.branch = branch.text.strip()
    if not report.branch:
        add_warning(
            report,
            f"{repo_label} is in detached HEAD state; record the intended ai-client-governance version explicitly.",
            "Record or switch to the intended ai-client-governance branch.",
        )
    else:
        upstream = run_git(embedded_repo, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
        upstream_name = upstream.text.strip() if upstream.exit_code == 0 else ""
        if not upstream_name and report.remote_present:
            candidate = f"{args.remote_name}/{report.branch}"
            verify = run_git(embedded_repo, ["rev-parse", "--verify", candidate])
            if verify.exit_code == 0:
                upstream_name = candidate
        report.upstream = upstream_name
        if not upstream_name:
            add_warning(
                report,
                f"{repo_label} branch '{report.branch}' has no upstream; compare or push manually.",
                "Set an upstream or compare manually.",
            )
        else:
            counts = run_git(embedded_repo, ["rev-list", "--left-right", "--count", f"HEAD...{upstream_name}"])
            if counts.exit_code == 0:
                parts = counts.text.split()
                if len(parts) >= 2:
                    report.ahead = int(parts[0])
                    report.behind = int(parts[1])
                if report.ahead > 0 and report.behind > 0:
                    add_warning(
                        report,
                        f"{repo_label} diverged from {upstream_name} (ahead {report.ahead}, behind {report.behind}). Resolve manually.",
                        "Resolve ai-client-governance divergence manually.",
                    )
                elif report.ahead > 0:
                    add_warning(
                        report,
                        f"{repo_label} is ahead of {upstream_name} by {report.ahead} commit(s). Push from .ai-client/ai-client-governance when approved.",
                        "Push from .ai-client/ai-client-governance after approval.",
                    )
                elif report.behind > 0:
                    add_warning(
                        report,
                        f"{repo_label} is behind {upstream_name} by {report.behind} commit(s). Run git pull --ff-only inside .ai-client/ai-client-governance.",
                        "Run git pull --ff-only inside .ai-client/ai-client-governance after review.",
                    )
                else:
                    report.notes.append(f"{repo_label} is aligned with {upstream_name}.")
            else:
                add_warning(report, f"Could not compare {repo_label} with {upstream_name}. {counts.text}")

    report.status = "ok" if not report.warnings else "warning"
    write_state(state_path, state, fetched=report.fetched, status=report.status)
    return report


def render_text(report: SyncReport) -> str:
    lines = [f"AI Client Governance sync check: {report.status}"]
    lines.extend(f"- {note}" for note in report.notes)
    lines.extend(f"WARNING: {warning}" for warning in report.warnings)
    if report.next_actions:
        lines.append("Next actions:")
        lines.extend(f"- {action}" for action in report.next_actions)
    if report.warnings:
        lines.append("Warnings repeat every session until the embedded ai-client-governance repository is synchronized.")
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    report = check_sync(args)
    if args.format == "json":
        print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
    else:
        print(render_text(report))
    if args.fail_on_warning and report.warnings:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
