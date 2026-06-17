#!/usr/bin/env python3
"""Manage task-level Git worktrees with fixed conventions.

This module provides standardized commands for creating, inspecting, and
cleaning up isolated task worktrees. It wraps git worktree with opinionated
defaults and integrates with worktree-coord session management.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ai_client_governance.worktree.coord import GuardedState, StateStore, current_branch, current_head, detect_repo, git_text, safe_id

DEFAULT_SELF_EXCLUDES = (".source-projects",)
ACTIVE_COORD_STATUSES = {"active", "integrating", "waiting"}
CLOSEOUT_REPO_ORDER = ("ai-client-governance", "self")


def git_run(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    """Run a git command."""
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=check,
    )


def git_run_input(
    args: list[str],
    cwd: Path,
    input_text: str,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """Run a git command with UTF-8 stdin."""
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=check,
    )


def run_command(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    """Run a non-Git command with UTF-8 output."""
    return subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=check,
    )


def git_common_dir(cwd: Path) -> Path:
    """Get the Git common directory."""
    result = git_text(["rev-parse", "--git-common-dir"], cwd)
    return (cwd / result).resolve()


def now_iso() -> str:
    """Return a local ISO timestamp for audit state files."""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def display_path(path: Path, project_root: Path) -> str:
    """Return a project-relative path when possible."""
    resolved = path.resolve()
    try:
        return resolved.relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def find_project_root(cwd: Path, explicit: str | None = None) -> Path:
    """Find the host project root that owns .ai-client/project."""
    if explicit:
        root = Path(explicit).expanduser().resolve()
        if not (root / ".ai-client" / "project").exists():
            raise SystemExit(f"project root lacks .ai-client/project: {root}")
        return root
    current = cwd.resolve()
    parts = current.parts
    for index in range(len(parts) - 2):
        if parts[index : index + 3] == (".ai-client", "project", ".worktree"):
            host = Path(*parts[:index])
            if (host / ".ai-client" / "project").exists():
                return host
    for candidate in (current, *current.parents):
        if (candidate / ".ai-client" / "project").exists():
            return candidate
    repo_root, _ = detect_repo(cwd)
    if (repo_root / ".ai-client" / "project").exists():
        return repo_root
    raise SystemExit("Cannot find host project root. Pass --project-root.")


def source_repo_for(project_root: Path, repo: str) -> Path:
    """Resolve the source Git repository for a task worktree."""
    if repo == "ai-client-governance":
        source_repo = project_root / ".ai-client" / "ai-client-governance"
    elif repo == "self":
        source_repo = project_root
    else:
        raise SystemExit(f"Error: --repo must be 'ai-client-governance' or 'self', got '{repo}'")
    if not (source_repo / ".git").exists():
        raise SystemExit(f"Git repository not found: {source_repo}")
    return source_repo.resolve()


def generate_task_slug(title: str) -> str:
    """Generate a filesystem-safe task slug."""
    slug = title.lower().replace(" ", "-")
    slug = "".join(c for c in slug if c.isalnum() or c == "-")
    return slug[:40] or "task"


def parse_worktree_list(cwd: Path) -> list[dict[str, str]]:
    """Parse git worktree list --porcelain output."""
    result = git_run(["worktree", "list", "--porcelain"], cwd, check=True)
    worktrees: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            if current:
                worktrees.append(current)
                current = {}
            continue
        if " " in line:
            key, _, value = line.partition(" ")
            current[key.lower()] = value
    if current:
        worktrees.append(current)
    return worktrees


def find_worktree_by_branch(worktrees: list[dict[str, str]], branch: str) -> dict[str, str] | None:
    """Find a worktree by branch name."""
    for wt in worktrees:
        if wt.get("branch", "").endswith(f"/{branch}"):
            return wt
    return None


def find_worktree_by_path(worktrees: list[dict[str, str]], path: Path) -> dict[str, str] | None:
    """Find a worktree by absolute path."""
    resolved = path.resolve()
    for wt in worktrees:
        if Path(wt.get("worktree", "")).resolve() == resolved:
            return wt
    return None


def clean_branch_name(value: str) -> str:
    """Normalize a porcelain branch ref into a short branch name."""
    return value.removeprefix("refs/heads/")


def task_worktree_path(project_root: Path, task_slug: str) -> Path:
    """Return the fixed path for a task worktree."""
    return project_root / ".ai-client" / "project" / ".worktree" / task_slug


def normalize_repo_path(value: str) -> str:
    """Normalize and validate a repository-relative path."""
    raw = value.replace("\\", "/").strip()
    if raw.startswith("/") or raw.startswith("//") or (len(raw) >= 2 and raw[1] == ":") or Path(value).is_absolute():
        raise ValueError(f"exclude path must be repository-relative: {value}")
    normalized = raw.strip("/")
    if not normalized or normalized == ".":
        raise ValueError("exclude path cannot be empty")
    if normalized.startswith("../") or "/../" in normalized or normalized == "..":
        raise ValueError(f"exclude path must stay inside the repository: {value}")
    if normalized == ".git" or normalized.startswith(".git/"):
        raise ValueError(f"exclude path cannot target Git metadata: {value}")
    return normalized


def sparse_excludes_for_create(args: argparse.Namespace, source_repo: Path) -> list[str]:
    """Return repository-relative paths excluded from a new worktree."""
    raw_paths: list[str] = []
    if args.repo == "self" and not args.include_source_projects:
        for path in DEFAULT_SELF_EXCLUDES:
            if (source_repo / path).exists():
                raw_paths.append(path)
    raw_paths.extend(args.exclude_path or [])

    excludes: list[str] = []
    seen: set[str] = set()
    for raw in raw_paths:
        normalized = normalize_repo_path(raw)
        if normalized not in seen:
            excludes.append(normalized)
            seen.add(normalized)
    return excludes


def sparse_patterns_for_excludes(excludes: list[str]) -> str:
    """Build non-cone sparse-checkout patterns that include all except excludes."""
    lines = ["/*"]
    for path in excludes:
        lines.append(f"!/{path}")
        lines.append(f"!/{path}/")
        lines.append(f"!/{path}/**")
    return "\n".join(lines) + "\n"


def ai_client_governance_script() -> Path:
    """Return the ai_client_governance.py entry for this source tree."""
    return Path(__file__).resolve().parents[3] / "scripts" / "ai_client_governance.py"


def ref_exists(cwd: Path, ref: str) -> bool:
    """Return whether a Git ref can be resolved."""
    return git_run(["rev-parse", "--verify", ref], cwd, check=False).returncode == 0


def merged_to_target(cwd: Path, branch: str, target_ref: str) -> bool | None:
    """Return whether branch is an ancestor of target_ref."""
    if not branch or not target_ref or not ref_exists(cwd, target_ref):
        return None
    result = git_run(["merge-base", "--is-ancestor", branch, target_ref], cwd, check=False)
    return result.returncode == 0


def merge_base(cwd: Path, left: str, right: str = "HEAD") -> str:
    """Return merge-base for two refs, or an empty string if unavailable."""
    return git_text(["merge-base", left, right], cwd, allow_fail=True)


def changed_files_since(cwd: Path, base_ref: str, head_ref: str = "HEAD") -> list[str]:
    """Return changed file names between base and head."""
    if not base_ref:
        return []
    result = git_run(["diff", "--name-only", f"{base_ref}..{head_ref}"], cwd, check=False)
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def task_worktree_context(project_root: Path, repo: str, task_slug: str) -> tuple[Path, Path, dict[str, str], str]:
    """Resolve common task worktree context."""
    source_repo = source_repo_for(project_root, repo)
    worktree_path = task_worktree_path(project_root, task_slug)
    if not worktree_path.exists():
        raise SystemExit(f"Error: worktree not found: {worktree_path}")
    wt = find_worktree_by_path(parse_worktree_list(source_repo), worktree_path)
    if not wt:
        raise SystemExit(f"Error: worktree not registered: {worktree_path}")
    branch = clean_branch_name(wt.get("branch", ""))
    if not branch:
        raise SystemExit(f"Error: worktree branch not found: {worktree_path}")
    return source_repo, worktree_path, wt, branch


def status_path_from_line(line: str) -> str:
    """Extract the path portion from one git status --short line."""
    value = line[3:].strip()
    if " -> " in value:
        value = value.split(" -> ", 1)[1].strip()
    return value.strip('"').replace("\\", "/")


def ignored_status_paths(cwd: Path, ignored_abs_paths: set[Path] | None) -> set[str]:
    """Return ignored paths relative to cwd's repository root."""
    if not ignored_abs_paths:
        return set()
    root_text = git_text(["rev-parse", "--show-toplevel"], cwd, allow_fail=True)
    if not root_text:
        return set()
    root = Path(root_text).resolve()
    ignored: set[str] = set()
    for item in ignored_abs_paths:
        try:
            ignored.add(item.resolve().relative_to(root).as_posix())
        except ValueError:
            continue
    return ignored


def short_status(path: Path, ignored_abs_paths: set[Path] | None = None) -> str:
    """Return git status --short for a worktree."""
    output = git_run(["status", "--short"], path, check=False).stdout.rstrip()
    ignored = ignored_status_paths(path, ignored_abs_paths)
    if not output or not ignored:
        return output
    lines = [line for line in output.splitlines() if status_path_from_line(line) not in ignored]
    return "\n".join(lines).rstrip()


def last_commit_message(path: Path) -> str:
    """Return the latest commit subject for a worktree."""
    return git_text(["log", "-1", "--pretty=%s"], path, allow_fail=True)


def host_gitlink_record(project_root: Path, submodule_path: str) -> dict[str, str]:
    """Return the host index gitlink record for an embedded repository."""
    result = git_run(["ls-files", "--stage", "--", submodule_path], project_root, check=False)
    if result.returncode != 0:
        return {"error": result.stderr.strip() or result.stdout.strip()}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 4:
            return {
                "mode": parts[0],
                "head_full_at_index": parts[1],
                "stage": parts[2],
                "path": parts[3],
            }
    return {}


def status_for_paths(project_root: Path, paths: list[str]) -> list[str]:
    """Return git status lines for selected host paths."""
    if not paths:
        return []
    result = git_run(["status", "--short", "--", *paths], project_root, check=False)
    return [line for line in result.stdout.splitlines() if line.strip()]


def resolve_project_paths(project_root: Path, values: list[str] | None) -> list[Path]:
    """Resolve user-provided paths against the host project root."""
    resolved: list[Path] = []
    for value in values or []:
        path = Path(value)
        if not path.is_absolute():
            path = project_root / path
        path = path.resolve()
        if path not in resolved:
            resolved.append(path)
    return resolved


def auto_task_tracking_paths(project_root: Path, task_slug: str | None) -> list[Path]:
    """Find likely task tracking files for a task slug."""
    if not task_slug:
        return []
    tracking_root = project_root / ".ai-client" / "project" / "records" / "task-tracking"
    if not tracking_root.exists():
        return []
    matches = sorted(tracking_root.glob(f"*{task_slug}*.md"))
    return [path.resolve() for path in matches if path.is_file()]


def state_ai_client_governance_head(state_path: Path) -> str:
    """Read the ai-client-governance main HEAD recorded in worktrees.json."""
    if not state_path.exists():
        return ""
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return ""
    ai_client_governance = data.get("ai-client-governance")
    if not isinstance(ai_client_governance, dict):
        return ""
    return str(ai_client_governance.get("main_head_full_at_snapshot", ""))


def ai_client_governance_sync_state_path(project_root: Path) -> Path:
    """Return the sync-check state file path in the host project."""
    return project_root / ".ai-client" / "project" / "state" / "ai-client-governance-state.json"


def project_relative_status_path(project_root: Path, path: Path) -> str:
    """Return a Git status path relative to the host project root."""
    try:
        return path.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def build_host_closeout_report(
    project_root: Path,
    *,
    repo: str,
    task_slug: str | None = None,
    task_tracking: list[str] | None = None,
    require_task_tracking: bool = False,
    require_clean_host: bool = False,
) -> dict[str, Any]:
    """Check host-side submodule gitlink, state, and task tracking closeout."""
    issues: list[str] = []
    if repo != "ai-client-governance":
        issues.append("host closeout currently applies only to the embedded ai-client-governance repository")

    source_repo = source_repo_for(project_root, repo)
    embedded_path = ".ai-client/ai-client-governance"
    embedded_head = current_head(source_repo)
    gitlink = host_gitlink_record(project_root, embedded_path)
    gitlink_head = gitlink.get("head_full_at_index", "")
    if gitlink.get("mode") != "160000":
        issues.append(f"host index does not track {embedded_path} as a gitlink submodule")
    elif gitlink_head != embedded_head:
        issues.append(
            f"host gitlink {embedded_path} points to {gitlink_head[:7] or '<missing>'}, "
            f"but embedded ai-client-governance HEAD is {embedded_head[:7]}"
        )

    state_path = project_root / ".ai-client" / "project" / "state" / "worktrees.json"
    state_head = state_ai_client_governance_head(state_path)
    if not state_path.exists():
        issues.append("host worktree state file is missing: .ai-client/project/state/worktrees.json")
    elif state_head != embedded_head:
        issues.append(
            "host worktree state is not refreshed for embedded ai-client-governance HEAD "
            f"(state={state_head[:7] or '<missing>'}, head={embedded_head[:7]})"
        )

    tracking_paths = resolve_project_paths(project_root, task_tracking)
    if not tracking_paths:
        tracking_paths = auto_task_tracking_paths(project_root, task_slug)
    tracking_reports: list[dict[str, Any]] = []
    if require_task_tracking and not tracking_paths:
        issues.append("no task tracking file was provided or found for host closeout")
    for path in tracking_paths:
        report = {
            "path": project_relative_status_path(project_root, path),
            "exists": path.exists(),
            "mentions_task_slug": False,
            "mentions_ai_client_governance_head": False,
        }
        if not path.exists():
            issues.append(f"task tracking file is missing: {report['path']}")
        else:
            text = path.read_text(encoding="utf-8", errors="replace")
            report["mentions_task_slug"] = bool(task_slug and task_slug in text)
            report["mentions_ai_client_governance_head"] = embedded_head in text or embedded_head[:7] in text
            if require_task_tracking and not report["mentions_ai_client_governance_head"]:
                issues.append(
                    f"task tracking file does not mention current embedded ai-client-governance HEAD {embedded_head[:7]}: "
                    f"{report['path']}"
                )
        tracking_reports.append(report)

    relevant_paths = [
        embedded_path,
        ".ai-client/project/state/worktrees.json",
        *[project_relative_status_path(project_root, path) for path in tracking_paths],
    ]
    relevant_status = status_for_paths(project_root, relevant_paths)
    host_status = short_status(project_root)
    if require_clean_host and host_status:
        issues.append("host repository is not clean; commit gitlink, state, and task tracking closeout first")

    return {
        "repo": repo,
        "embedded_path": embedded_path,
        "embedded_head_full": embedded_head,
        "embedded_head": embedded_head[:7],
        "host_gitlink": gitlink,
        "state_file": project_relative_status_path(project_root, state_path),
        "state_ai_client_governance_head_full": state_head,
        "state_ai_client_governance_head": state_head[:7] if state_head else "",
        "task_slug": task_slug or "",
        "task_tracking": tracking_reports,
        "relevant_host_status": relevant_status,
        "host_status_short": host_status.splitlines() if host_status else [],
        "issues": issues,
        "next_actions": [
            "run worktree-task status --write-state after merging ai-client-governance worktrees",
            "commit the host .ai-client/ai-client-governance gitlink, .ai-client/project/state/worktrees.json, and task tracking record",
            "rerun worktree-task host-closeout --require-clean-host before final reply",
        ],
    }


def build_worktree_record(
    wt: dict[str, str],
    *,
    repo_name: str,
    source_repo: Path,
    project_root: Path,
    target_ref: str,
    ignored_abs_paths: set[Path] | None = None,
) -> dict[str, Any]:
    """Build one auditable worktree status record."""
    path = Path(wt["worktree"]).resolve()
    branch = clean_branch_name(wt.get("branch", ""))
    status_text = short_status(path, ignored_abs_paths)
    merged = merged_to_target(source_repo, branch, target_ref)
    head = wt.get("head", "")
    return {
        "repo": repo_name,
        "task_slug": path.name,
        "path": display_path(path, project_root),
        "absolute_path": str(path),
        "branch": branch,
        "head_at_snapshot": head[:7],
        "head_full_at_snapshot": head,
        "last_commit_message": last_commit_message(path),
        "status": "dirty" if status_text else "clean",
        "dirty": bool(status_text),
        "status_short": status_text.splitlines(),
        "locked": "locked" in wt,
        "lock_reason": wt.get("locked", ""),
        "target_ref": target_ref,
        "merged_to_target": merged,
    }


def build_status_snapshot(
    project_root: Path,
    target_ref: str,
    ignored_abs_paths: set[Path] | None = None,
) -> dict[str, Any]:
    """Build a machine-readable status snapshot for all task worktrees."""
    repos = [
        ("self", project_root),
        ("ai-client-governance", project_root / ".ai-client" / "ai-client-governance"),
    ]
    worktree_base = project_root / ".ai-client" / "project" / ".worktree"
    snapshot: dict[str, Any] = {
        "schema_version": 4,
        "last_updated": now_iso(),
        "project_root": str(project_root.resolve()),
        "core_principle": "一切流程化 + 可审计",
        "snapshot_semantics": {
            "kind": "committed_audit_snapshot",
            "live_state_command": "python .ai-client/ai-client-governance/scripts/ai_client_governance.py worktree-task status --write-state",
            "head_fields": "HEAD fields are observed at snapshot generation time. Committing this state file can advance the main repository HEAD, so rerun the live_state_command for live truth.",
            "status_fields": "Status is calculated with the output state file ignored to avoid self-dirty snapshots.",
        },
        "audit_policy": {
            "worktree_state_source": ".ai-client/project/state/worktrees.json",
            "script_entry": "python .ai-client/ai-client-governance/scripts/ai_client_governance.py worktree-task status --write-state",
            "require_commit_before_merge": True,
            "require_state_snapshot_before_closeout": True,
            "require_live_status_rerun_before_final_reply": True,
            "queue_before_merge_command": "python .ai-client/ai-client-governance/scripts/ai_client_governance.py worktree-task queue",
            "merge_command": "python .ai-client/ai-client-governance/scripts/ai_client_governance.py worktree-task merge --execute",
            "worktree_cleanup_command": "python .ai-client/ai-client-governance/scripts/ai_client_governance.py worktree-task remove --execute",
            "pre_finalize_gate_command": "python .ai-client/ai-client-governance/scripts/ai_client_governance.py worktree-task finalize",
            "full_cleanup_finalize_gate_command": (
                "python .ai-client/ai-client-governance/scripts/ai_client_governance.py worktree-task finalize "
                "--require-merged --require-no-task-worktrees"
            ),
            "branch_cleanup_command": "python .ai-client/ai-client-governance/scripts/ai_client_governance.py worktree-task cleanup-branch --execute",
            "host_closeout_command": (
                "python .ai-client/ai-client-governance/scripts/ai_client_governance.py worktree-task host-closeout "
                "--repo ai-client-governance --require-task-tracking --require-clean-host"
            ),
            "embedded_ai_client_governance_merge_closeout": (
                "After merging an ai-client-governance task worktree, the host repository must commit "
                ".ai-client/ai-client-governance gitlink, .ai-client/project/state/worktrees.json, and related task tracking."
            ),
        },
    }

    for repo_name, repo_path in repos:
        if not (repo_path / ".git").exists():
            continue
        main_status = short_status(repo_path, ignored_abs_paths)
        main_head = current_head(repo_path)
        repo_record: dict[str, Any] = {
            "main_worktree": display_path(repo_path, project_root),
            "main_branch": current_branch(repo_path),
            "main_head_at_snapshot": main_head[:7],
            "main_head_full_at_snapshot": main_head,
            "main_status": "dirty" if main_status else "clean",
            "main_status_short": main_status.splitlines(),
            "target_ref": target_ref,
            "task_worktrees": [],
        }
        for wt in parse_worktree_list(repo_path):
            path = Path(wt.get("worktree", "")).resolve()
            if path == repo_path.resolve():
                continue
            try:
                path.relative_to(worktree_base.resolve())
            except ValueError:
                continue
            repo_record["task_worktrees"].append(
                build_worktree_record(
                    wt,
                    repo_name=repo_name,
                    source_repo=repo_path,
                    project_root=project_root,
                    target_ref=target_ref,
                    ignored_abs_paths=ignored_abs_paths,
                )
            )
        snapshot[repo_name] = repo_record
    return snapshot


def closeout_repo_order(selected: list[str] | None) -> list[str]:
    """Return closeout repositories in dependency order."""
    values = selected or list(CLOSEOUT_REPO_ORDER)
    deduped = list(dict.fromkeys(values))
    return [repo for repo in CLOSEOUT_REPO_ORDER if repo in deduped]


def rev_list_count(cwd: Path, rev_range: str) -> int | None:
    """Return git rev-list --count for a revision range."""
    result = git_run(["rev-list", "--count", rev_range], cwd, check=False)
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def status_lines_outside(status_text: str, allowed_paths: set[str]) -> list[str]:
    """Return status lines whose path is outside allowed host closeout paths."""
    outside: list[str] = []
    normalized_allowed = {path.strip("/").replace("\\", "/") for path in allowed_paths if path.strip("/")}
    for line in status_text.splitlines():
        path = status_path_from_line(line)
        if any(path == allowed or path.startswith(f"{allowed}/") for allowed in normalized_allowed):
            continue
        outside.append(line)
    return outside


def closeout_owned_host_paths(project_root: Path, task_tracking: list[str] | None = None) -> set[str]:
    """Return host paths that closeout-all is allowed to stage and commit."""
    paths = {
        ".ai-client/project/state/worktrees.json",
        project_relative_status_path(project_root, ai_client_governance_sync_state_path(project_root)),
    }
    if host_gitlink_record(project_root, ".ai-client/ai-client-governance").get("mode") == "160000":
        paths.add(".ai-client/ai-client-governance")
    for path in resolve_project_paths(project_root, task_tracking):
        paths.add(project_relative_status_path(project_root, path))
    return paths


def governance_script_for_project(project_root: Path) -> Path:
    """Return the embedded governance script, falling back to the current source tree."""
    embedded = project_root / ".ai-client" / "ai-client-governance" / "scripts" / "ai_client_governance.py"
    if embedded.exists():
        return embedded
    return ai_client_governance_script()


def upstream_for(cwd: Path) -> str:
    """Return the configured upstream ref for the current branch."""
    return git_text(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], cwd, allow_fail=True)


def command_text(command: list[str]) -> str:
    """Render a command for reports."""
    return " ".join(command)


def add_closeout_action(
    plan: dict[str, Any],
    *,
    action: str,
    repo: str = "",
    task_slug: str = "",
    status: str = "planned",
    command: list[str] | None = None,
    reason: str = "",
) -> None:
    """Append one closeout action to a plan or execution report."""
    plan["actions"].append(
        {
            "action": action,
            "repo": repo,
            "task_slug": task_slug,
            "status": status,
            "command": command_text(command) if command else "",
            "reason": reason,
        }
    )


def add_closeout_step(
    plan: dict[str, Any],
    *,
    action: str,
    repo: str = "",
    task_slug: str = "",
    status: str,
    command: list[str] | None = None,
    detail: str = "",
) -> None:
    """Append one executed closeout step."""
    plan.setdefault("execution", []).append(
        {
            "action": action,
            "repo": repo,
            "task_slug": task_slug,
            "status": status,
            "command": command_text(command) if command else "",
            "detail": detail,
        }
    )


def build_closeout_all_plan(args: argparse.Namespace) -> dict[str, Any]:
    """Build a conservative plan for merging and cleaning task worktrees."""
    project_root = find_project_root(Path.cwd(), args.project_root)
    selected_repos = closeout_repo_order(args.repo)
    snapshot = build_status_snapshot(project_root, args.target_ref)
    allowed_host_paths = closeout_owned_host_paths(project_root, args.task_tracking)
    plan: dict[str, Any] = {
        "schema_version": 1,
        "command": "worktree-task closeout-all",
        "mode": "execute" if args.execute else "plan",
        "project_root": str(project_root),
        "target_ref": args.target_ref,
        "selected_repos": selected_repos,
        "push_policy": "closeout-all never pushes; run a separate push command after explicit approval.",
        "tasks": [],
        "actions": [],
        "execution": [],
        "blockers": [],
        "warnings": [],
        "repositories": {},
        "host_state_needed": False,
        "common_merge_needed": False,
        "self_merge_needed": False,
    }
    if args.plan and args.execute:
        plan["blockers"].append("use either --plan or --execute, not both")

    for raw_path in args.task_tracking or []:
        path = Path(raw_path)
        if not path.is_absolute():
            path = project_root / path
        if not path.exists():
            plan["blockers"].append(f"task tracking path does not exist: {project_relative_status_path(project_root, path)}")

    for repo_name in selected_repos:
        try:
            source_repo = source_repo_for(project_root, repo_name)
        except SystemExit as exc:
            plan["blockers"].append(str(exc))
            continue
        repo_record = snapshot.get(repo_name) if isinstance(snapshot.get(repo_name), dict) else {}
        task_worktrees = sorted(
            list(repo_record.get("task_worktrees", [])) if isinstance(repo_record, dict) else [],
            key=lambda item: str(item.get("task_slug", "")),
        )
        repo_status = short_status(source_repo)
        repo_current_branch = current_branch(source_repo)
        target_exists = ref_exists(source_repo, args.target_ref)
        plan["repositories"][repo_name] = {
            "source_repo": str(source_repo),
            "current_branch": repo_current_branch,
            "main_status": "dirty" if repo_status else "clean",
            "status_short": repo_status.splitlines(),
            "target_exists": target_exists,
            "task_worktree_count": len(task_worktrees),
        }
        if task_worktrees and not target_exists:
            plan["blockers"].append(f"{repo_name}: target ref not found: {args.target_ref}")
        if task_worktrees and repo_current_branch != args.target_ref:
            plan["blockers"].append(
                f"{repo_name}: source repository must be checked out on {args.target_ref}; current branch is {repo_current_branch}"
            )
        if task_worktrees and repo_status:
            plan["blockers"].append(f"{repo_name}: source repository worktree is dirty")

        for wt in task_worktrees:
            branch = str(wt.get("branch", ""))
            task_slug = str(wt.get("task_slug", ""))
            merged = wt.get("merged_to_target")
            branch_exists = bool(branch and ref_exists(source_repo, f"refs/heads/{branch}"))
            unique_commits = (
                rev_list_count(source_repo, f"{args.target_ref}..{branch}") if branch and target_exists else None
            )
            task = {
                "repo": repo_name,
                "task_slug": task_slug,
                "branch": branch,
                "worktree_path": wt.get("absolute_path", ""),
                "dirty": bool(wt.get("dirty")),
                "locked": bool(wt.get("locked")),
                "lock_reason": wt.get("lock_reason", ""),
                "merged_to_target": merged,
                "unique_commits": unique_commits,
                "status": "blocked",
                "needs_merge": merged is False,
                "cleanup_only": merged is True,
            }
            if task["dirty"]:
                plan["blockers"].append(f"{repo_name}:{task_slug}: task worktree is dirty")
            if task["locked"]:
                plan["blockers"].append(f"{repo_name}:{task_slug}: task worktree is locked ({task['lock_reason']})")
            if not branch:
                plan["blockers"].append(f"{repo_name}:{task_slug}: task worktree has no branch")
            elif not branch_exists:
                plan["blockers"].append(f"{repo_name}:{task_slug}: branch not found: {branch}")
            if merged is None:
                plan["blockers"].append(f"{repo_name}:{task_slug}: cannot determine merge state against {args.target_ref}")
            if merged is False and unique_commits == 0:
                plan["blockers"].append(
                    f"{repo_name}:{task_slug}: branch is not reported merged but has no unique commits; inspect manually"
                )

            if not task["dirty"] and not task["locked"] and branch and branch_exists and merged in (False, True):
                task["status"] = "ready"
                plan["host_state_needed"] = True
                if merged is False:
                    add_closeout_action(
                        plan,
                        action="merge",
                        repo=repo_name,
                        task_slug=task_slug,
                        command=["git", "merge", "--no-ff", branch, "-m", f"merge: {task_slug}"],
                        reason=f"{unique_commits if unique_commits is not None else '?'} unique commit(s)",
                    )
                    if repo_name == "ai-client-governance":
                        plan["common_merge_needed"] = True
                    if repo_name == "self":
                        plan["self_merge_needed"] = True
                else:
                    add_closeout_action(
                        plan,
                        action="skip-merge",
                        repo=repo_name,
                        task_slug=task_slug,
                        status="skipped",
                        reason=f"already merged to {args.target_ref}",
                    )
                add_closeout_action(
                    plan,
                    action="remove-worktree",
                    repo=repo_name,
                    task_slug=task_slug,
                    command=["git", "worktree", "remove", str(wt.get("absolute_path", ""))],
                )
                add_closeout_action(
                    plan,
                    action="delete-branch",
                    repo=repo_name,
                    task_slug=task_slug,
                    command=["git", "branch", "-d", branch],
                )
            plan["tasks"].append(task)

    if not plan["tasks"]:
        plan["warnings"].append("no task worktrees found under .ai-client/project/.worktree")

    if plan["host_state_needed"]:
        if "self" not in plan["repositories"]:
            host_status = short_status(project_root)
            plan["repositories"]["self"] = {
                "source_repo": str(project_root),
                "current_branch": current_branch(project_root),
                "main_status": "dirty" if host_status else "clean",
                "status_short": host_status.splitlines(),
                "target_exists": ref_exists(project_root, args.target_ref),
                "task_worktree_count": 0,
            }
        if plan["repositories"]["self"]["current_branch"] != args.target_ref:
            plan["blockers"].append(
                "self: host repository must be checked out on "
                f"{args.target_ref}; current branch is {plan['repositories']['self']['current_branch']}"
            )
        host_status = short_status(project_root)
        outside = status_lines_outside(host_status, allowed_host_paths)
        if outside:
            plan["blockers"].append("host repository has dirty status outside closeout-owned paths: " + "; ".join(outside))
        add_closeout_action(
            plan,
            action="write-state",
            repo="self",
            command=["python", "ai_client_governance.py", "worktree-task", "status", "--write-state"],
            reason="refresh .ai-client/project/state/worktrees.json after merge cleanup",
        )
        add_closeout_action(
            plan,
            action="host-closeout-commit",
            repo="self",
            command=["git", "commit", "-m", "chore: record worktree closeout"],
            reason="commit host gitlink/state changes without staging unrelated files",
        )

    if plan["host_state_needed"]:
        add_closeout_action(
            plan,
            action="validate-cli-list",
            repo="ai-client-governance",
            command=[sys.executable, str(governance_script_for_project(project_root)), "--list"],
        )
        if (project_root / ".ai-client" / "ai-client-governance" / ".git").exists():
            add_closeout_action(
                plan,
                action="sync-check",
                repo="ai-client-governance",
                command=[
                    sys.executable,
                    str(governance_script_for_project(project_root)),
                    "sync-check",
                    "--target-project-path",
                    str(project_root),
                    "--no-fetch",
                ],
            )

    if args.execute:
        current_cwd = Path.cwd().resolve()
        for task in plan.get("tasks", []):
            if task.get("status") != "ready":
                continue
            worktree_path = Path(str(task.get("worktree_path", ""))).resolve()
            try:
                current_cwd.relative_to(worktree_path)
            except ValueError:
                continue
            plan["blockers"].append(
                "closeout-all --execute must be run outside task worktrees it will remove; "
                "run it from the host root or embedded source repository"
            )
            break
    return plan


def print_closeout_all_report(plan: dict[str, Any], *, output_format: str) -> None:
    """Print a closeout-all plan or execution report."""
    if output_format == "json":
        print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
        return
    print("Worktree closeout-all:")
    print(f"  mode: {plan.get('mode')}")
    print(f"  project_root: {plan.get('project_root')}")
    print(f"  target_ref: {plan.get('target_ref')}")
    print(f"  repos: {', '.join(plan.get('selected_repos', [])) or '<none>'}")
    print(f"  task_worktrees: {len(plan.get('tasks', []))}")
    blockers = plan.get("blockers", [])
    warnings = plan.get("warnings", [])
    print(f"  blockers: {len(blockers)}")
    for blocker in blockers:
        print(f"  - {blocker}")
    print(f"  warnings: {len(warnings)}")
    for warning in warnings:
        print(f"  - {warning}")
    print("  actions:")
    actions = plan.get("actions", [])
    if not actions:
        print("  - none")
    for action in actions:
        label = f"{action.get('repo') or '-'}:{action.get('task_slug') or '-'}"
        print(f"  - [{action.get('status')}] {action.get('action')} {label}")
        if action.get("reason"):
            print(f"    reason: {action.get('reason')}")
        if action.get("command"):
            print(f"    command: {action.get('command')}")
    execution = plan.get("execution", [])
    if execution:
        print("  execution:")
        for step in execution:
            label = f"{step.get('repo') or '-'}:{step.get('task_slug') or '-'}"
            print(f"  - [{step.get('status')}] {step.get('action')} {label}")
            if step.get("detail"):
                print(f"    detail: {step.get('detail')}")


def run_closeout_process(
    plan: dict[str, Any],
    command: list[str],
    *,
    cwd: Path,
    action: str,
    repo: str = "",
    task_slug: str = "",
    check: bool = True,
) -> subprocess.CompletedProcess:
    """Run one closeout command and record its result."""
    completed = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    detail = (completed.stdout or completed.stderr).strip().splitlines()
    add_closeout_step(
        plan,
        action=action,
        repo=repo,
        task_slug=task_slug,
        status="done" if completed.returncode == 0 else "failed",
        command=command,
        detail=detail[0] if detail else "",
    )
    if check and completed.returncode != 0:
        raise subprocess.CalledProcessError(
            completed.returncode,
            command,
            output=completed.stdout,
            stderr=completed.stderr,
        )
    return completed


def execute_closeout_all(plan: dict[str, Any], args: argparse.Namespace) -> int:
    """Execute a previously validated closeout-all plan."""
    project_root = Path(str(plan["project_root"]))
    tasks = [task for task in plan.get("tasks", []) if task.get("status") == "ready"]
    touched_repos: set[str] = set()
    try:
        for repo_name in closeout_repo_order(args.repo):
            repo_tasks = [task for task in tasks if task.get("repo") == repo_name]
            if not repo_tasks:
                continue
            source_repo = Path(str(plan["repositories"][repo_name]["source_repo"]))
            for task in repo_tasks:
                task_slug = str(task["task_slug"])
                branch = str(task["branch"])
                if task.get("needs_merge"):
                    diff_range = f"{args.target_ref}..{branch}"
                    run_closeout_process(
                        plan,
                        ["git", "diff", "--check", diff_range],
                        cwd=source_repo,
                        action="pre-merge-diff-check",
                        repo=repo_name,
                        task_slug=task_slug,
                    )
                    merge_cmd = ["git", "merge", "--no-ff", branch, "-m", f"merge: {task_slug}"]
                    completed = run_closeout_process(
                        plan,
                        merge_cmd,
                        cwd=source_repo,
                        action="merge",
                        repo=repo_name,
                        task_slug=task_slug,
                        check=False,
                    )
                    if completed.returncode != 0:
                        abort = subprocess.run(
                            ["git", "merge", "--abort"],
                            cwd=source_repo,
                            capture_output=True,
                            text=True,
                            encoding="utf-8",
                            errors="replace",
                        )
                        add_closeout_step(
                            plan,
                            action="merge-abort",
                            repo=repo_name,
                            task_slug=task_slug,
                            status="done" if abort.returncode == 0 else "failed",
                            command=["git", "merge", "--abort"],
                            detail=(abort.stderr or abort.stdout).strip(),
                        )
                        return completed.returncode
                    touched_repos.add(repo_name)
                run_closeout_process(
                    plan,
                    ["git", "worktree", "remove", str(task["worktree_path"])],
                    cwd=source_repo,
                    action="remove-worktree",
                    repo=repo_name,
                    task_slug=task_slug,
                )
                closed_sessions, released_locks = close_coord_sessions_for_worktree(
                    source_repo,
                    Path(str(task["worktree_path"])),
                )
                add_closeout_step(
                    plan,
                    action="close-coord-session",
                    repo=repo_name,
                    task_slug=task_slug,
                    status="done" if closed_sessions else "skipped",
                    command=["worktree-task", "closeout-all", "close-coord-session"],
                    detail=(
                        f"closed_sessions={','.join(closed_sessions)} released_locks={released_locks}"
                        if closed_sessions
                        else "no active coord session matched removed worktree"
                    ),
                )
                git_run(["worktree", "prune"], source_repo, check=False)
                if ref_exists(source_repo, f"refs/heads/{branch}"):
                    if merged_to_target(source_repo, branch, args.target_ref) is not True:
                        add_closeout_step(
                            plan,
                            action="delete-branch",
                            repo=repo_name,
                            task_slug=task_slug,
                            status="failed",
                            command=["git", "branch", "-d", branch],
                            detail=f"branch is not merged to {args.target_ref}",
                        )
                        return 1
                    run_closeout_process(
                        plan,
                        ["git", "branch", "-d", branch],
                        cwd=source_repo,
                        action="delete-branch",
                        repo=repo_name,
                        task_slug=task_slug,
                    )

        for repo_name in sorted(touched_repos):
            source_repo = Path(str(plan["repositories"][repo_name]["source_repo"]))
            run_closeout_process(
                plan,
                ["git", "diff", "--check"],
                cwd=source_repo,
                action="post-merge-diff-check",
                repo=repo_name,
            )

        if plan.get("host_state_needed"):
            script = governance_script_for_project(project_root)
            run_closeout_process(
                plan,
                [sys.executable, str(script), "--list"],
                cwd=project_root,
                action="validate-cli-list",
                repo="ai-client-governance",
            )
            if (project_root / ".ai-client" / "ai-client-governance" / ".git").exists():
                run_closeout_process(
                    plan,
                    [
                        sys.executable,
                        str(script),
                        "sync-check",
                        "--target-project-path",
                        str(project_root),
                        "--no-fetch",
                    ],
                    cwd=project_root,
                    action="sync-check",
                    repo="ai-client-governance",
                )

            state_path = status_state_path(project_root, "")
            snapshot = build_status_snapshot(project_root, args.target_ref, {state_path.resolve()})
            snapshot["state_file"] = display_path(state_path, project_root)
            write_status_snapshot(state_path, snapshot)
            add_closeout_step(
                plan,
                action="write-state",
                repo="self",
                status="done",
                command=[sys.executable, str(script), "worktree-task", "status", "--write-state"],
                detail=display_path(state_path, project_root),
            )

            stage_paths = sorted(closeout_owned_host_paths(project_root, args.task_tracking))
            existing_stage_paths = [path for path in stage_paths if (project_root / path).exists()]
            run_closeout_process(
                plan,
                ["git", "add", "--", *existing_stage_paths],
                cwd=project_root,
                action="stage-host-closeout",
                repo="self",
            )
            run_closeout_process(
                plan,
                ["git", "diff", "--cached", "--check"],
                cwd=project_root,
                action="host-staged-diff-check",
                repo="self",
            )
            cached = git_run(["diff", "--cached", "--quiet", "--exit-code"], project_root, check=False)
            if cached.returncode == 1:
                run_closeout_process(
                    plan,
                    ["git", "commit", "-m", "chore: record worktree closeout"],
                    cwd=project_root,
                    action="host-closeout-commit",
                    repo="self",
                )
                touched_repos.add("self")
            else:
                add_closeout_step(
                    plan,
                    action="host-closeout-commit",
                    repo="self",
                    status="skipped",
                    command=["git", "commit", "-m", "chore: record worktree closeout"],
                    detail="no staged host closeout changes",
                )
            run_closeout_process(
                plan,
                ["git", "diff", "--check"],
                cwd=project_root,
                action="host-post-closeout-diff-check",
                repo="self",
            )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.output or "").strip()
        if detail:
            plan["blockers"].append(detail)
        else:
            plan["blockers"].append(f"command failed: {command_text([str(part) for part in exc.cmd])}")
        return exc.returncode or 1
    return 0


def parse_coord_time(value: str | None) -> datetime | None:
    """Parse a coord timestamp."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def coord_record_is_active(item: dict[str, Any]) -> bool:
    """Return whether a coord session or lock is active at this instant."""
    if item.get("status") not in ACTIVE_COORD_STATUSES:
        return False
    expires = parse_coord_time(str(item.get("lease_expires_at", "")))
    return expires is None or expires > datetime.now(timezone.utc)


def read_coord_state(repo_path: Path) -> dict[str, Any]:
    """Read worktree-coord state for a Git repository."""
    state_path = git_common_dir(repo_path) / "ai-client-runtime" / "worktree-coord" / "state.json"
    if not state_path.exists():
        return {"sessions": {}, "locks": [], "queue": [], "state_file": str(state_path)}
    data = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"coord state must contain a JSON object: {state_path}")
    data.setdefault("sessions", {})
    data.setdefault("locks", [])
    data.setdefault("queue", [])
    data["state_file"] = str(state_path)
    return data


def live_worktree_maps(repo_path: Path) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    """Return live worktrees indexed by path and branch."""
    by_path: dict[str, dict[str, str]] = {}
    branch_by_path: dict[str, str] = {}
    for wt in parse_worktree_list(repo_path):
        raw_path = wt.get("worktree", "")
        if not raw_path:
            continue
        path = str(Path(raw_path).resolve())
        branch = clean_branch_name(wt.get("branch", ""))
        by_path[path] = wt
        branch_by_path[path] = branch
    return by_path, branch_by_path


def task_worktree_paths(snapshot: dict[str, Any], repo_name: str) -> set[str]:
    """Return task worktree absolute paths from a status snapshot."""
    repo_record = snapshot.get(repo_name)
    if not isinstance(repo_record, dict):
        return set()
    return {
        str(Path(str(item.get("absolute_path", ""))).resolve())
        for item in repo_record.get("task_worktrees", [])
        if isinstance(item, dict) and item.get("absolute_path")
    }


def reconcile_repo_live_state(
    *,
    repo_name: str,
    repo_path: Path,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Compare coord metadata with Git live worktree state for one repository."""
    coord_state = read_coord_state(repo_path)
    live_by_path, branch_by_path = live_worktree_maps(repo_path)
    task_paths = task_worktree_paths(snapshot, repo_name)
    active_sessions = [
        session
        for session in coord_state.get("sessions", {}).values()
        if isinstance(session, dict) and coord_record_is_active(session)
    ]
    active_session_ids = {str(session.get("session_id", "")) for session in active_sessions}
    active_locks = [
        lock
        for lock in coord_state.get("locks", [])
        if isinstance(lock, dict) and coord_record_is_active(lock)
    ]
    queue_open = [
        item
        for item in coord_state.get("queue", [])
        if isinstance(item, dict) and item.get("status") in {"pending", "integrating", "blocked"}
    ]
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []

    for session in active_sessions:
        session_id = str(session.get("session_id", ""))
        raw_path = str(session.get("worktree", ""))
        if not raw_path:
            errors.append({"code": "active_session_missing_worktree_path", "item": session_id})
            continue
        path = str(Path(raw_path).resolve())
        live = live_by_path.get(path)
        if live is None:
            errors.append({"code": "active_session_worktree_missing_from_git_live_state", "item": session_id, "path": path})
            continue
        expected_branch = str(session.get("branch", ""))
        actual_branch = branch_by_path.get(path, "")
        if expected_branch and actual_branch and expected_branch != actual_branch:
            errors.append(
                {
                    "code": "active_session_branch_mismatch",
                    "item": session_id,
                    "expected": expected_branch,
                    "actual": actual_branch,
                    "path": path,
                }
            )

    session_paths = {
        str(Path(str(session.get("worktree", ""))).resolve())
        for session in active_sessions
        if session.get("worktree")
    }
    for path in sorted(task_paths):
        if path not in session_paths:
            warnings.append({"code": "task_worktree_without_active_coord_session", "path": path})

    for lock in active_locks:
        session_id = str(lock.get("session_id", ""))
        if session_id and session_id not in active_session_ids:
            warnings.append({"code": "active_lock_without_active_session", "item": str(lock.get("lock_id", "")), "session_id": session_id})

    live_branches = set(branch_by_path.values())
    for item in queue_open:
        branch = str(item.get("branch", ""))
        if branch and branch not in live_branches and not ref_exists(repo_path, branch):
            warnings.append({"code": "queue_item_branch_not_found", "item": str(item.get("item_id", "")), "branch": branch})

    return {
        "repo": repo_name,
        "repo_path": str(repo_path),
        "coord_state_file": coord_state.get("state_file", ""),
        "live_worktree_count": len(live_by_path),
        "task_worktree_count": len(task_paths),
        "active_session_count": len(active_sessions),
        "active_lock_count": len(active_locks),
        "open_queue_count": len(queue_open),
        "errors": errors,
        "warnings": warnings,
    }


def mark_missing_sessions_stale(repo_path: Path, report: dict[str, Any]) -> list[str]:
    """Mark sessions whose worktree disappeared from Git live state."""
    missing_session_ids = {
        str(issue.get("item", ""))
        for issue in report.get("errors", [])
        if issue.get("code") == "active_session_worktree_missing_from_git_live_state" and issue.get("item")
    }
    if not missing_session_ids:
        return []
    store = StateStore(git_common_dir(repo_path))
    repaired: list[str] = []
    with GuardedState(store) as guarded:
        state = guarded.read()
        sessions = state.get("sessions", {})
        for session_id in sorted(missing_session_ids):
            session = sessions.get(session_id)
            if not isinstance(session, dict):
                continue
            session["status"] = "stale_or_missing_worktree"
            session["updated_at"] = now_iso()
            session["reconcile_note"] = "Git live state no longer contains the recorded worktree path."
            for lock in state.get("locks", []):
                if isinstance(lock, dict) and lock.get("session_id") == session_id and lock.get("status") == "active":
                    lock["status"] = "released_missing_worktree"
                    lock["updated_at"] = now_iso()
            repaired.append(session_id)
        if repaired:
            guarded.write(state)
            for session_id in repaired:
                guarded.append_event(
                    {
                        "event": "session.mark_missing_worktree_stale",
                        "session_id": session_id,
                        "source": "worktree-task reconcile --mark-missing-stale",
                    }
                )
    return repaired


def close_coord_sessions_for_worktree(repo_path: Path, worktree_path: Path) -> tuple[list[str], int]:
    """Close active coord sessions owned by a worktree removed by closeout-all."""
    removed_path = str(worktree_path.resolve()).casefold()
    store = StateStore(git_common_dir(repo_path))
    closed: list[str] = []
    released = 0
    released_by_session: dict[str, int] = {}
    with GuardedState(store) as guarded:
        state = guarded.read()
        sessions = state.get("sessions", {})
        for session_id, session in sorted(sessions.items()):
            if not isinstance(session, dict) or not coord_record_is_active(session):
                continue
            raw_worktree = str(session.get("worktree", ""))
            if not raw_worktree:
                continue
            session_worktree = str(Path(raw_worktree).resolve()).casefold()
            if session_worktree != removed_path:
                continue
            session["status"] = "closed_by_closeout"
            session["updated_at"] = now_iso()
            session["closeout_note"] = "closeout-all removed the recorded task worktree and closed this session."
            session_released = 0
            for lock in state.get("locks", []):
                if isinstance(lock, dict) and lock.get("session_id") == session_id and lock.get("status") == "active":
                    lock["status"] = "released_by_closeout"
                    lock["updated_at"] = now_iso()
                    released += 1
                    session_released += 1
            closed.append(str(session_id))
            released_by_session[str(session_id)] = session_released
        if closed:
            guarded.write(state)
            for session_id in closed:
                guarded.append_event(
                    {
                        "event": "session.close_by_closeout",
                        "session_id": session_id,
                        "released_locks": released_by_session.get(session_id, 0),
                        "worktree": str(worktree_path.resolve()),
                    }
                )
    return closed, released


def status_state_path(project_root: Path, output: str | None) -> Path:
    """Resolve the status snapshot output path."""
    path = Path(output).expanduser() if output else project_root / ".ai-client" / "project" / "state" / "worktrees.json"
    if not path.is_absolute():
        path = project_root / path
    return path


def write_status_snapshot(path: Path, snapshot: dict[str, Any]) -> Path:
    """Write the status snapshot atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{safe_id('tmp')}.tmp")
    temp.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temp.replace(path)
    return path


def print_status_text(snapshot: dict[str, Any], task_slug: str | None) -> None:
    """Print a human-readable worktree status report."""
    found_any = False
    for repo_name in ("self", "ai-client-governance"):
        repo_record = snapshot.get(repo_name)
        if not isinstance(repo_record, dict):
            continue
        task_worktrees = list(repo_record.get("task_worktrees", []))
        if task_slug:
            task_worktrees = [wt for wt in task_worktrees if wt.get("task_slug") == task_slug]
        if not task_worktrees:
            continue
        print(f"Task worktrees ({repo_name}):")
        found_any = True
        for wt in task_worktrees:
            merged = wt.get("merged_to_target")
            if merged is None:
                merged_label = "unknown"
            else:
                merged_label = "yes" if merged else "no"
            print(f"  {wt['task_slug']}:")
            print(f"    path: {wt['absolute_path']}")
            print(f"    branch: {wt['branch']}")
            print(f"    head_at_snapshot: {wt['head_at_snapshot']}")
            print(f"    dirty: {'yes' if wt['dirty'] else 'no'}")
            print(f"    merged_to_{wt['target_ref']}: {merged_label}")
        print()

    if not found_any:
        print("No task worktrees found.")


def command_create(args: argparse.Namespace) -> int:
    """Create a new task worktree."""
    project_root = find_project_root(Path.cwd(), args.project_root)
    source_repo = source_repo_for(project_root, args.repo)
    task_slug = args.task_slug or generate_task_slug(args.title)
    branch_name = f"codex/{task_slug}"
    worktree_path = task_worktree_path(project_root, task_slug)

    if worktree_path.exists():
        print(f"Error: worktree path already exists: {worktree_path}", file=sys.stderr)
        return 1

    # Check if branch already exists
    existing_worktrees = parse_worktree_list(source_repo)
    if find_worktree_by_branch(existing_worktrees, branch_name):
        print(f"Error: branch '{branch_name}' already has a worktree", file=sys.stderr)
        return 1

    check_result = git_run(["rev-parse", "--verify", f"refs/heads/{branch_name}"], source_repo, check=False)
    if check_result.returncode == 0:
        print(f"Error: branch '{branch_name}' already exists", file=sys.stderr)
        return 1

    try:
        sparse_excludes = sparse_excludes_for_create(args, source_repo)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        print(f"[dry-run] Would create worktree:")
        print(f"  source_repo: {source_repo}")
        print(f"  worktree_path: {worktree_path}")
        print(f"  branch: {branch_name}")
        print(f"  base: {args.base or 'HEAD'}")
        if sparse_excludes:
            print(f"  sparse_checkout_excludes: {', '.join(sparse_excludes)}")
        return 0

    # Create parent directory
    worktree_path.parent.mkdir(parents=True, exist_ok=True)

    # Create worktree
    git_args = ["worktree", "add", "-b", branch_name]
    if sparse_excludes:
        git_args.append("--no-checkout")
    if args.git_lock:
        git_args.extend(["--lock", "--reason", args.git_lock_reason or f"AI Client Governance task {task_slug}"])
    git_args.append(str(worktree_path))
    if args.base:
        git_args.append(args.base)

    result = git_run(git_args, source_repo, check=False)
    if result.returncode != 0:
        print(f"Error creating worktree:\n{result.stderr}", file=sys.stderr)
        return 1

    if sparse_excludes:
        patterns = sparse_patterns_for_excludes(sparse_excludes)
        sparse_result = git_run_input(
            ["sparse-checkout", "set", "--no-cone", "--stdin"],
            worktree_path,
            patterns,
            check=False,
        )
        if sparse_result.returncode != 0:
            print(f"Error configuring sparse checkout:\n{sparse_result.stderr}", file=sys.stderr)
            return 1
        checkout_result = git_run(["checkout", "-q"], worktree_path, check=False)
        if checkout_result.returncode != 0:
            print(f"Error checking out sparse worktree:\n{checkout_result.stderr}", file=sys.stderr)
            return 1

    if args.register_session:
        session_id = args.session_id or safe_id("S")
        register_cmd = [
            sys.executable,
            str(ai_client_governance_script()),
            "worktree-coord",
            "session",
            "register",
            "--session-id",
            session_id,
            "--title",
            args.title,
            "--task",
            task_slug,
            "--metadata-kv",
            f"repo={args.repo}",
            "--metadata-kv",
            f"task_slug={task_slug}",
            "--metadata-kv",
            "created_by=worktree-task",
        ]
        if args.task_tracking:
            register_cmd.extend(["--task-tracking", args.task_tracking])
        for scope in args.scope or []:
            register_cmd.extend(["--scope", scope])
        result = run_command(register_cmd, worktree_path, check=False)
        if result.returncode != 0:
            print(f"Warning: worktree created but session registration failed:\n{result.stderr}", file=sys.stderr)
            return result.returncode
        print(f"Registered session: {session_id}")

        if args.scope and not args.no_locks:
            lock_cmd = [
                sys.executable,
                str(ai_client_governance_script()),
                "worktree-coord",
                "lock",
                "acquire",
                "--session-id",
                session_id,
                "--reason",
                args.lock_reason or f"AI Client Governance task {task_slug}",
            ]
            for scope in args.scope:
                lock_cmd.extend(["--scope", scope])
            lock_result = run_command(lock_cmd, worktree_path, check=False)
            if lock_result.returncode != 0:
                print(f"Warning: session registered but lock acquisition failed:\n{lock_result.stderr or lock_result.stdout}", file=sys.stderr)
                return lock_result.returncode
            print(lock_result.stdout.strip())

    # Print evidence
    head_commit = current_head(worktree_path)
    head_log = git_text(["log", "-1", "--oneline"], worktree_path)

    print(f"\nCreated worktree:")
    print(f"  source_repo: {source_repo}")
    print(f"  worktree_path: {worktree_path}")
    print(f"  branch: {branch_name}")
    print(f"  base_commit: {head_commit[:7]} {head_log}")
    print(f"\nTask tracking evidence:")
    print(f"| 源仓库 | {source_repo} |")
    print(f"| worktree 路径 | {worktree_path} |")
    print(f"| 分支 | {branch_name} |")
    print(f"| 基准提交 | {head_commit[:7]} {head_log} |")
    print(f"| git status | clean (newly created) |")
    if sparse_excludes:
        print(f"| sparse checkout 排除 | {', '.join(sparse_excludes)} |")
    
    return 0


def command_status(args: argparse.Namespace) -> int:
    """Show status of task worktrees."""
    repo_root = find_project_root(Path.cwd(), args.project_root)
    output = status_state_path(repo_root, args.write_state) if args.write_state is not None else None
    ignored_paths = {output.resolve()} if output else set()
    snapshot = build_status_snapshot(repo_root, args.target_ref, ignored_paths)

    if output is not None:
        snapshot["state_file"] = display_path(output, repo_root)
        write_status_snapshot(output, snapshot)
        if args.format != "json":
            print(f"Wrote worktree state: {output}")

    if args.format == "json":
        print(json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print_status_text(snapshot, args.task_slug)

    return 0


def command_reconcile(args: argparse.Namespace) -> int:
    """Reconcile coord/session state against Git live worktree state."""
    repo_root = find_project_root(Path.cwd(), args.project_root)
    output = status_state_path(repo_root, args.write_state) if args.write_state is not None else None
    ignored_paths = {output.resolve()} if output else set()
    snapshot = build_status_snapshot(repo_root, args.target_ref, ignored_paths)
    if output is not None:
        snapshot["state_file"] = display_path(output, repo_root)
        write_status_snapshot(output, snapshot)

    repo_names = args.repo or ["self", "ai-client-governance"]
    reports: list[dict[str, Any]] = []
    for repo_name in repo_names:
        repo_path = source_repo_for(repo_root, repo_name)
        report = reconcile_repo_live_state(repo_name=repo_name, repo_path=repo_path, snapshot=snapshot)
        if args.mark_missing_stale:
            repaired = mark_missing_sessions_stale(repo_path, report)
            report["repaired_sessions"] = repaired
            if repaired:
                report = reconcile_repo_live_state(repo_name=repo_name, repo_path=repo_path, snapshot=snapshot)
                report["repaired_sessions"] = repaired
        reports.append(report)

    errors = [issue for report in reports for issue in report["errors"]]
    warnings = [issue for report in reports for issue in report["warnings"]]
    payload = {
        "schema_version": 1,
        "project_root": str(repo_root),
        "target_ref": args.target_ref,
        "reports": reports,
        "errors": errors,
        "warnings": warnings,
        "state_file": display_path(output, repo_root) if output else "",
    }

    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        if output is not None:
            print(f"Wrote worktree state: {output}")
        print("Worktree live-state reconcile:")
        for report in reports:
            print(
                f"  {report['repo']}: live={report['live_worktree_count']} "
                f"task={report['task_worktree_count']} sessions={report['active_session_count']} "
                f"locks={report['active_lock_count']} queue={report['open_queue_count']}"
            )
            if report.get("repaired_sessions"):
                print(f"    repaired_sessions: {', '.join(report['repaired_sessions'])}")
        print(f"  errors: {len(errors)}")
        for issue in errors:
            print(f"  - {issue}")
        print(f"  warnings: {len(warnings)}")
        for issue in warnings:
            print(f"  - {issue}")

    return 1 if errors or (args.strict and warnings) else 0


def command_close(args: argparse.Namespace) -> int:
    """Check worktree closeout status."""
    task_slug = args.task_slug
    repo_root = find_project_root(Path.cwd(), args.project_root)
    try:
        source_repo, worktree_path, wt, _branch = task_worktree_context(repo_root, args.repo, task_slug)
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 1

    head = wt.get("head", "")

    # Check status
    status_result = git_run(["status", "--short"], worktree_path, check=True)
    is_dirty = bool(status_result.stdout.strip())

    current_branch_name = current_branch(worktree_path)
    target_check = git_run(["rev-parse", "--verify", args.target_ref], source_repo, check=False)
    is_merged: bool | None = None
    if target_check.returncode == 0:
        merge_result = git_run(["merge-base", "--is-ancestor", "HEAD", args.target_ref], worktree_path, check=False)
        is_merged = merge_result.returncode == 0

    print(f"Worktree closeout status:")
    print(f"  path: {worktree_path}")
    print(f"  branch: {current_branch_name}")
    print(f"  head: {head[:7]}")
    print(f"  dirty: {'yes' if is_dirty else 'no'}")
    if is_merged is None:
        print(f"  merged_to_{args.target_ref}: unknown (target ref not found)")
    else:
        print(f"  merged_to_{args.target_ref}: {'yes' if is_merged else 'no (needs manual merge or push)'}")

    print(f"\n## Worktree 完成记录")
    print(f"| 项 | 状态 |")
    print(f"|---|---|")
    print(f"| worktree 是否完成 | {'已完成' if not is_dirty else '仍有未提交改动'} |")
    merged_label = "未知" if is_merged is None else ("已合并" if is_merged else "未合并")
    print(f"| 是否合并回源仓库 | {merged_label} |")
    print(f"| 是否 stage/commit | {'已提交' if not is_dirty else '未提交或有新改动'} |")
    print(f"| 是否 push | 脚本不自动判断远端 push；需结合 `git status --branch` 和远端分支确认 |")
    if not is_dirty and is_merged:
        next_step = "合并/收口任务应移除 worktree；如保留需记录原因"
    else:
        next_step = "需要用户确认是否合并/提交/push"
    print(f"| 下一步/用户需确认 | {next_step} |")

    if args.session_id:
        close_cmd = [
            sys.executable,
            str(ai_client_governance_script()),
            "worktree-coord",
            "session",
            "close",
            "--session-id",
            args.session_id,
        ]
        if args.keep_locks:
            close_cmd.append("--keep-locks")
        result = run_command(close_cmd, worktree_path, check=False)
        if result.returncode != 0:
            print(f"Warning: closeout printed but session close failed:\n{result.stderr}", file=sys.stderr)
            return result.returncode
        print(result.stdout.strip())

    if is_dirty:
        return 1
    return 0


def command_queue(args: argparse.Namespace) -> int:
    """Add a task worktree branch to the integration queue."""
    repo_root = find_project_root(Path.cwd(), args.project_root)
    try:
        _source_repo, worktree_path, _wt, branch = task_worktree_context(repo_root, args.repo, args.task_slug)
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 1

    base = merge_base(worktree_path, args.target_ref, "HEAD")
    head = current_head(worktree_path)
    files = args.file or changed_files_since(worktree_path, base, "HEAD")
    summary = args.summary or f"Merge {branch} into {args.target_ref}"
    queue_cmd = [
        sys.executable,
        str(ai_client_governance_script()),
        "worktree-coord",
        "queue",
        "add",
        "--branch",
        branch,
        "--base",
        base,
        "--head",
        head,
        "--summary",
        summary,
    ]
    if args.item_id:
        queue_cmd.extend(["--item-id", args.item_id])
    if args.session_id:
        queue_cmd.extend(["--session-id", args.session_id])
    if args.task_tracking:
        queue_cmd.extend(["--task-tracking", args.task_tracking])
    for file_name in files:
        queue_cmd.extend(["--file", file_name])

    result = run_command(queue_cmd, worktree_path, check=False)
    if result.returncode != 0:
        print(result.stderr or result.stdout, file=sys.stderr)
        return result.returncode
    print(result.stdout.strip())
    return 0


def command_merge(args: argparse.Namespace) -> int:
    """Merge a clean task worktree branch into its target branch."""
    repo_root = find_project_root(Path.cwd(), args.project_root)
    try:
        source_repo, worktree_path, _wt, branch = task_worktree_context(repo_root, args.repo, args.task_slug)
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 1

    worktree_status = short_status(worktree_path)
    if worktree_status:
        print("Error: task worktree is dirty; commit inside the task worktree before merging.", file=sys.stderr)
        print(worktree_status, file=sys.stderr)
        return 1

    source_status = short_status(source_repo)
    if source_status:
        print("Error: source repository worktree is dirty; merge requires a clean target worktree.", file=sys.stderr)
        print(source_status, file=sys.stderr)
        return 1

    current_target_branch = current_branch(source_repo)
    if current_target_branch != args.target_ref:
        print(
            f"Error: source repository must be checked out on {args.target_ref}; current branch is {current_target_branch}.",
            file=sys.stderr,
        )
        return 1

    merged = merged_to_target(source_repo, branch, args.target_ref)
    if merged is True:
        print(f"Branch already merged: {branch} -> {args.target_ref}")
        return 0
    if merged is None:
        print(f"Error: target ref not found: {args.target_ref}", file=sys.stderr)
        return 1

    message = args.message or f"merge: {args.task_slug}"
    merge_cmd = ["merge", "--no-ff", branch, "-m", message]
    if not args.execute:
        print("[dry-run] Would merge task worktree branch:")
        print(f"  source_repo: {source_repo}")
        print(f"  target_ref: {args.target_ref}")
        print(f"  branch: {branch}")
        print(f"  command: git {' '.join(merge_cmd)}")
        return 0

    result = git_run(merge_cmd, source_repo, check=False)
    if result.returncode != 0:
        print("Error: merge failed.", file=sys.stderr)
        if result.stdout:
            print(result.stdout, file=sys.stderr)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        return result.returncode

    print(result.stdout.strip())
    print(f"Merged {branch} into {args.target_ref}")
    print("Next cleanup after verifying the merge:")
    print(
        "  python .ai-client/ai-client-governance/scripts/ai_client_governance.py worktree-task remove "
        f"--repo {args.repo} --task-slug {args.task_slug} --execute"
    )
    print(
        "  python .ai-client/ai-client-governance/scripts/ai_client_governance.py worktree-task cleanup-branch "
        f"--repo {args.repo} --task-slug {args.task_slug} --execute"
    )
    if args.repo == "ai-client-governance":
        print("Host repository closeout is also required for embedded ai-client-governance merges:")
        print("  python .ai-client/ai-client-governance/scripts/ai_client_governance.py worktree-task status --write-state")
        host_cmd = (
            "  python .ai-client/ai-client-governance/scripts/ai_client_governance.py worktree-task host-closeout "
            f"--repo ai-client-governance --task-slug {args.task_slug} --require-task-tracking"
        )
        for tracking_path in args.task_tracking or []:
            host_cmd += f" --task-tracking {tracking_path}"
        print(host_cmd)
        print("  git add .ai-client/ai-client-governance .ai-client/project/state/worktrees.json <related task tracking>")

    if args.queue_item:
        mark_cmd = [
            sys.executable,
            str(ai_client_governance_script()),
            "worktree-coord",
            "queue",
            "mark",
            "--item-id",
            args.queue_item,
            "--status",
            "done",
            "--validation",
            f"merged {branch} into {args.target_ref}",
        ]
        mark_result = run_command(mark_cmd, worktree_path, check=False)
        if mark_result.returncode != 0:
            print(f"Warning: merge succeeded but queue mark failed:\n{mark_result.stderr or mark_result.stdout}", file=sys.stderr)
            return mark_result.returncode
        print(mark_result.stdout.strip())

    return 0


def command_finalize(args: argparse.Namespace) -> int:
    """Run a live worktree status gate before final output or closeout."""
    repo_root = find_project_root(Path.cwd(), args.project_root)
    output = status_state_path(repo_root, args.write_state) if args.write_state is not None else None
    ignored_paths = {output.resolve()} if output else set()
    snapshot = build_status_snapshot(repo_root, args.target_ref, ignored_paths)
    if output is not None:
        snapshot["state_file"] = display_path(output, repo_root)
        write_status_snapshot(output, snapshot)

    issues: list[str] = []
    total_task_worktrees = 0
    dirty_task_worktrees = 0
    unmerged_task_worktrees = 0
    for repo_name in ("self", "ai-client-governance"):
        repo_record = snapshot.get(repo_name)
        if not isinstance(repo_record, dict):
            continue
        for wt in repo_record.get("task_worktrees", []):
            total_task_worktrees += 1
            label = f"{repo_name}:{wt.get('task_slug')}"
            if wt.get("dirty"):
                dirty_task_worktrees += 1
                issues.append(f"{label} is dirty")
            if args.require_merged and wt.get("merged_to_target") is not True:
                unmerged_task_worktrees += 1
                issues.append(f"{label} is not merged to {wt.get('target_ref')}")
    if args.require_no_task_worktrees and total_task_worktrees:
        issues.append(f"task worktrees still exist: {total_task_worktrees}")
    host_closeout_report: dict[str, Any] | None = None
    if args.require_host_closeout:
        host_closeout_report = build_host_closeout_report(
            repo_root,
            repo="ai-client-governance",
            task_slug=args.host_task_slug,
            task_tracking=args.task_tracking,
            require_task_tracking=args.require_task_tracking,
            require_clean_host=args.require_clean_host,
        )
        for issue in host_closeout_report.get("issues", []):
            issues.append(f"host closeout: {issue}")
        snapshot["host_closeout"] = host_closeout_report

    if args.format == "json":
        print(json.dumps({"snapshot": snapshot, "issues": issues}, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        if output is not None:
            print(f"Wrote worktree state: {output}")
        print("Worktree finalize gate:")
        print(f"  task_worktrees: {total_task_worktrees}")
        print(f"  dirty_task_worktrees: {dirty_task_worktrees}")
        print(f"  unmerged_task_worktrees: {unmerged_task_worktrees}")
        if host_closeout_report is not None:
            print("  host_closeout:")
            print(f"    embedded_ai_client_governance_head: {host_closeout_report.get('embedded_head')}")
            print(f"    state_ai_client_governance_head: {host_closeout_report.get('state_ai_client_governance_head')}")
            print(f"    relevant_host_status: {len(host_closeout_report.get('relevant_host_status', []))}")
        if issues:
            print("  issues:")
            for issue in issues:
                print(f"  - {issue}")
        else:
            print("  issues: none")

    return 1 if issues else 0


def command_host_closeout(args: argparse.Namespace) -> int:
    """Verify host-side closeout after embedded ai-client-governance merges."""
    repo_root = find_project_root(Path.cwd(), args.project_root)
    report = build_host_closeout_report(
        repo_root,
        repo=args.repo,
        task_slug=args.task_slug,
        task_tracking=args.task_tracking,
        require_task_tracking=args.require_task_tracking,
        require_clean_host=args.require_clean_host,
    )
    issues = list(report.get("issues", []))
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        gitlink = report.get("host_gitlink", {})
        print("Host closeout gate:")
        print(f"  repo: {report.get('repo')}")
        print(f"  embedded_path: {report.get('embedded_path')}")
        print(f"  embedded_head: {report.get('embedded_head')}")
        print(f"  host_gitlink_head: {str(gitlink.get('head_full_at_index', ''))[:7]}")
        print(f"  state_ai_client_governance_head: {report.get('state_ai_client_governance_head')}")
        print(f"  task_tracking_files: {len(report.get('task_tracking', []))}")
        relevant_status = report.get("relevant_host_status", [])
        print(f"  relevant_host_status: {len(relevant_status)}")
        for line in relevant_status:
            print(f"    {line}")
        host_status = report.get("host_status_short", [])
        print(f"  host_status_entries: {len(host_status)}")
        if issues:
            print("  issues:")
            for issue in issues:
                print(f"  - {issue}")
        else:
            print("  issues: none")
    return 1 if issues else 0


def command_cleanup_branch(args: argparse.Namespace) -> int:
    """Delete a branch only after it has merged and has no mounted worktree."""
    repo_root = find_project_root(Path.cwd(), args.project_root)
    source_repo = source_repo_for(repo_root, args.repo)
    branch = args.branch or f"codex/{args.task_slug}"
    if not ref_exists(source_repo, f"refs/heads/{branch}"):
        print(f"Error: branch not found: {branch}", file=sys.stderr)
        return 1
    if find_worktree_by_branch(parse_worktree_list(source_repo), branch):
        print(f"Error: branch still has a worktree; remove the worktree before deleting branch: {branch}", file=sys.stderr)
        return 1
    merged = merged_to_target(source_repo, branch, args.target_ref)
    if merged is not True:
        print(f"Error: refusing to delete unmerged branch: {branch}", file=sys.stderr)
        return 1
    delete_cmd = ["branch", "-d", branch]
    if not args.execute:
        print("[dry-run] Would delete merged branch:")
        print(f"  source_repo: {source_repo}")
        print(f"  branch: {branch}")
        print(f"  target_ref: {args.target_ref}")
        print(f"  command: git {' '.join(delete_cmd)}")
        return 0
    result = git_run(delete_cmd, source_repo, check=False)
    if result.returncode != 0:
        print(result.stderr or result.stdout, file=sys.stderr)
        return result.returncode
    print(result.stdout.strip())
    return 0


def command_remove(args: argparse.Namespace) -> int:
    """Remove a task worktree."""
    task_slug = args.task_slug
    repo_root = find_project_root(Path.cwd(), args.project_root)
    worktree_path = task_worktree_path(repo_root, task_slug)

    if not worktree_path.exists():
        print(f"Error: worktree not found: {worktree_path}", file=sys.stderr)
        return 1

    source_repo = source_repo_for(repo_root, args.repo)

    # Check if clean
    status_result = git_run(["status", "--short"], worktree_path, check=False)
    is_dirty = bool(status_result.stdout.strip())

    if args.execute and is_dirty and not args.force:
        print(f"Error: worktree is dirty. Commit or stash changes, or use --force.", file=sys.stderr)
        print(status_result.stdout)
        return 1

    if not args.execute:
        print(f"[dry-run] Would remove worktree:")
        print(f"  path: {worktree_path}")
        print(f"  dirty: {'yes (would require --force)' if is_dirty else 'no'}")
        prune_result = git_run(["worktree", "prune", "--dry-run"], source_repo, check=False)
        if prune_result.stdout.strip():
            print("  prune_dry_run:")
            print(prune_result.stdout.strip())
        return 0

    # Remove worktree
    remove_args = ["worktree", "remove"]
    if args.force:
        remove_args.append("--force")
    remove_args.append(str(worktree_path))
    result = git_run(remove_args, source_repo, check=False)
    if result.returncode != 0:
        print(f"Error removing worktree:\n{result.stderr}", file=sys.stderr)
        return 1

    print(f"Removed worktree: {worktree_path}")

    # Prune
    git_run(["worktree", "prune"], source_repo, check=False)

    return 0


def command_closeout_all(args: argparse.Namespace) -> int:
    """Plan or execute a conservative closeout for all selected task worktrees."""
    plan = build_closeout_all_plan(args)
    if not args.execute:
        print_closeout_all_report(plan, output_format=args.format)
        return 1 if plan.get("blockers") else 0
    if plan.get("blockers"):
        print_closeout_all_report(plan, output_format=args.format)
        return 1
    result = execute_closeout_all(plan, args)
    print_closeout_all_report(plan, output_format=args.format)
    if result != 0:
        return result
    return 1 if plan.get("blockers") else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage task-level Git worktrees with fixed conventions."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    # create
    create = subparsers.add_parser("create", help="Create a new task worktree.")
    create.add_argument("--project-root", help="Host project root containing .ai-client/project.")
    create.add_argument("--title", required=True, help="Task title.")
    create.add_argument("--repo", choices=["self", "ai-client-governance"], required=True, help="Source repository.")
    create.add_argument("--task-slug", help="Override generated task slug.")
    create.add_argument("--base", help="Base commit/branch. Default: HEAD.")
    create.add_argument("--scope", action="append", help="Initial session scopes (repeatable).")
    create.add_argument("--register-session", action="store_true", help="Register worktree-coord session.")
    create.add_argument("--session-id", help="Explicit session id.")
    create.add_argument("--task-tracking", help="Task tracking file path.")
    create.add_argument("--no-locks", action="store_true", help="Do not acquire coord locks for scopes.")
    create.add_argument("--lock-reason", help="Reason recorded on coord locks.")
    create.add_argument("--git-lock", action="store_true", help="Create the git worktree locked.")
    create.add_argument("--git-lock-reason", help="Reason passed to git worktree add --lock.")
    create.add_argument(
        "--include-source-projects",
        action="store_true",
        help="For --repo self, include .source-projects instead of excluding it by default.",
    )
    create.add_argument(
        "--exclude-path",
        action="append",
        help="Repository-relative path to exclude from the new worktree with sparse-checkout. Repeatable.",
    )
    create.add_argument("--dry-run", action="store_true", help="Show what would be created.")
    
    # status
    status = subparsers.add_parser("status", help="Show task worktrees status.")
    status.add_argument("--project-root", help="Host project root containing .ai-client/project.")
    status.add_argument("--task-slug", "--task", dest="task_slug", help="Show one task slug.")
    status.add_argument("--target-ref", default="main", help="Target ref used for merged status. Default: main.")
    status.add_argument("--format", choices=["text", "json"], default="text", help="Output format.")
    status.add_argument(
        "--write-state",
        nargs="?",
        const="",
        help="Write full status snapshot. Default path: .ai-client/project/state/worktrees.json.",
    )

    reconcile = subparsers.add_parser("reconcile", help="Reconcile coord/session metadata against Git live worktree state.")
    reconcile.add_argument("--project-root", help="Host project root containing .ai-client/project.")
    reconcile.add_argument("--repo", action="append", choices=["self", "ai-client-governance"], help="Repository to reconcile. Default: both.")
    reconcile.add_argument("--target-ref", default="main", help="Target ref used for merged status. Default: main.")
    reconcile.add_argument("--strict", action="store_true", help="Fail on warnings as well as errors.")
    reconcile.add_argument(
        "--mark-missing-stale",
        action="store_true",
        help="Mark active coord sessions with missing Git worktrees as stale_or_missing_worktree and release their active locks.",
    )
    reconcile.add_argument("--format", choices=["text", "json"], default="text", help="Output format.")
    reconcile.add_argument(
        "--write-state",
        nargs="?",
        const="",
        help="Write full status snapshot. Default path: .ai-client/project/state/worktrees.json.",
    )

    closeout_all = subparsers.add_parser(
        "closeout-all",
        help="Plan or execute merge, host closeout, worktree removal, branch cleanup, and validation.",
    )
    closeout_all.add_argument("--project-root", help="Host project root containing .ai-client/project.")
    closeout_all.add_argument(
        "--repo",
        action="append",
        choices=["self", "ai-client-governance"],
        help="Repository to close out. Repeatable. Default: ai-client-governance first, then self.",
    )
    closeout_all.add_argument("--target-ref", default="main", help="Target branch currently checked out in source repos.")
    closeout_all.add_argument("--plan", action="store_true", help="Print a dry-run plan. This is the default without --execute.")
    closeout_all.add_argument("--execute", action="store_true", help="Actually merge, clean, and commit host closeout without pushing.")
    closeout_all.add_argument(
        "--task-tracking",
        action="append",
        help="Related host tracking/state path to stage in the host closeout commit. Repeatable.",
    )
    closeout_all.add_argument("--format", choices=["text", "json"], default="text", help="Output format.")

    # close
    close = subparsers.add_parser("close", help="Check worktree closeout status.")
    close.add_argument("--project-root", help="Host project root containing .ai-client/project.")
    close.add_argument("--task-slug", "--task", dest="task_slug", required=True, help="Task slug (worktree directory name).")
    close.add_argument("--repo", choices=["self", "ai-client-governance"], default="self", help="Source repository.")
    close.add_argument("--target-ref", default="main", help="Target ref for merge check.")
    close.add_argument("--session-id", help="Close this worktree-coord session after checks.")
    close.add_argument("--keep-locks", action="store_true", help="Keep locks when closing the session.")

    # queue
    queue = subparsers.add_parser("queue", help="Add a task worktree branch to the integration queue.")
    queue.add_argument("--project-root", help="Host project root containing .ai-client/project.")
    queue.add_argument("--task-slug", "--task", dest="task_slug", required=True, help="Task slug (worktree directory name).")
    queue.add_argument("--repo", choices=["self", "ai-client-governance"], default="self", help="Source repository.")
    queue.add_argument("--target-ref", default="main", help="Target ref used to compute changed files.")
    queue.add_argument("--item-id", help="Explicit integration queue item id.")
    queue.add_argument("--session-id", help="Owning worktree-coord session id.")
    queue.add_argument("--summary", help="Queue item summary.")
    queue.add_argument("--task-tracking", help="Task tracking file path.")
    queue.add_argument("--file", action="append", help="Changed file to record; default is diff from target merge-base.")

    # merge
    merge = subparsers.add_parser("merge", help="Merge a clean task worktree branch into its target branch.")
    merge.add_argument("--project-root", help="Host project root containing .ai-client/project.")
    merge.add_argument("--task-slug", "--task", dest="task_slug", required=True, help="Task slug (worktree directory name).")
    merge.add_argument("--repo", choices=["self", "ai-client-governance"], default="self", help="Source repository.")
    merge.add_argument("--target-ref", default="main", help="Target branch currently checked out in the source repo.")
    merge.add_argument("--message", help="Merge commit message. Default: merge: <task-slug>.")
    merge.add_argument("--queue-item", help="Mark this integration queue item done after a successful merge.")
    merge.add_argument("--task-tracking", action="append", help="Related host task tracking file for ai-client-governance host closeout.")
    merge.add_argument("--execute", action="store_true", help="Actually run git merge. Default is dry-run.")

    # finalize
    finalize = subparsers.add_parser("finalize", help="Run a live worktree status gate before final output.")
    finalize.add_argument("--project-root", help="Host project root containing .ai-client/project.")
    finalize.add_argument("--target-ref", default="main", help="Target ref used for merged status. Default: main.")
    finalize.add_argument("--require-merged", action="store_true", help="Fail when any task worktree is not merged to target.")
    finalize.add_argument("--require-no-task-worktrees", action="store_true", help="Fail when any task worktree still exists.")
    finalize.add_argument("--require-host-closeout", action="store_true", help="Fail unless embedded ai-client-governance host gitlink/state/tracking are closed out.")
    finalize.add_argument("--host-task-slug", help="Task slug used to find related task tracking for host closeout.")
    finalize.add_argument("--task-tracking", action="append", help="Related task tracking file for host closeout. Repeatable.")
    finalize.add_argument("--require-task-tracking", action="store_true", help="Require task tracking to mention the current embedded ai-client-governance HEAD.")
    finalize.add_argument("--require-clean-host", action="store_true", help="Fail if the host repository has any dirty status entries.")
    finalize.add_argument("--format", choices=["text", "json"], default="text", help="Output format.")
    finalize.add_argument(
        "--write-state",
        nargs="?",
        const="",
        help="Write full status snapshot. Default path: .ai-client/project/state/worktrees.json.",
    )

    host_closeout = subparsers.add_parser(
        "host-closeout",
        help="Verify host submodule gitlink, worktree state, and task tracking after embedded ai-client-governance merges.",
    )
    host_closeout.add_argument("--project-root", help="Host project root containing .ai-client/project.")
    host_closeout.add_argument("--repo", choices=["ai-client-governance"], default="ai-client-governance", help="Embedded repository to verify.")
    host_closeout.add_argument("--task-slug", "--task", dest="task_slug", help="Task slug used to find related task tracking.")
    host_closeout.add_argument("--task-tracking", action="append", help="Related task tracking file. Repeatable.")
    host_closeout.add_argument("--require-task-tracking", action="store_true", help="Require tracking to mention current embedded HEAD.")
    host_closeout.add_argument("--require-clean-host", action="store_true", help="Fail if the host repository has dirty status entries.")
    host_closeout.add_argument("--format", choices=["text", "json"], default="text", help="Output format.")

    # cleanup-branch
    cleanup_branch = subparsers.add_parser("cleanup-branch", help="Delete a merged task branch after its worktree is removed.")
    cleanup_branch.add_argument("--project-root", help="Host project root containing .ai-client/project.")
    cleanup_branch.add_argument("--task-slug", "--task", dest="task_slug", required=True, help="Task slug used as codex/<task-slug>.")
    cleanup_branch.add_argument("--repo", choices=["self", "ai-client-governance"], default="self", help="Source repository.")
    cleanup_branch.add_argument("--target-ref", default="main", help="Target ref that must contain the branch.")
    cleanup_branch.add_argument("--branch", help="Explicit branch name. Default: codex/<task-slug>.")
    cleanup_branch.add_argument("--execute", action="store_true", help="Actually delete the branch. Default is dry-run.")
    
    # remove
    remove = subparsers.add_parser("remove", help="Remove a task worktree.")
    remove.add_argument("--project-root", help="Host project root containing .ai-client/project.")
    remove.add_argument("--task-slug", "--task", dest="task_slug", required=True, help="Task slug (worktree directory name).")
    remove.add_argument("--repo", choices=["self", "ai-client-governance"], default="self", help="Source repository.")
    remove.add_argument("--force", action="store_true", help="Remove even if dirty.")
    remove.add_argument("--execute", action="store_true", help="Actually remove the worktree. Default is dry-run.")
    
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    
    if args.command == "create":
        return command_create(args)
    if args.command == "status":
        return command_status(args)
    if args.command == "reconcile":
        return command_reconcile(args)
    if args.command == "closeout-all":
        return command_closeout_all(args)
    if args.command == "close":
        return command_close(args)
    if args.command == "queue":
        return command_queue(args)
    if args.command == "merge":
        return command_merge(args)
    if args.command == "finalize":
        return command_finalize(args)
    if args.command == "host-closeout":
        return command_host_closeout(args)
    if args.command == "cleanup-branch":
        return command_cleanup_branch(args)
    if args.command == "remove":
        return command_remove(args)
    
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
