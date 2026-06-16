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

from ai_rules.worktree.coord import current_branch, current_head, detect_repo, git_text, safe_id


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
    """Find the host project root that owns .codex/project."""
    if explicit:
        root = Path(explicit).expanduser().resolve()
        if not (root / ".codex" / "project").exists():
            raise SystemExit(f"project root lacks .codex/project: {root}")
        return root
    current = cwd.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".codex" / "project").exists():
            return candidate
    repo_root, _ = detect_repo(cwd)
    if (repo_root / ".codex" / "project").exists():
        return repo_root
    raise SystemExit("Cannot find host project root. Pass --project-root.")


def source_repo_for(project_root: Path, repo: str) -> Path:
    """Resolve the source Git repository for a task worktree."""
    if repo == "ai-rules":
        source_repo = project_root / ".codex" / "ai-rules"
    elif repo == "self":
        source_repo = project_root
    else:
        raise SystemExit(f"Error: --repo must be 'ai-rules' or 'self', got '{repo}'")
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


def clean_branch_name(value: str) -> str:
    """Normalize a porcelain branch ref into a short branch name."""
    return value.removeprefix("refs/heads/")


def task_worktree_path(project_root: Path, task_slug: str) -> Path:
    """Return the fixed path for a task worktree."""
    return project_root / ".codex" / "project" / ".worktree" / task_slug


def ai_rules_script() -> Path:
    """Return the ai_rules.py entry for this source tree."""
    return Path(__file__).resolve().parents[3] / "scripts" / "ai_rules.py"


def ref_exists(cwd: Path, ref: str) -> bool:
    """Return whether a Git ref can be resolved."""
    return git_run(["rev-parse", "--verify", ref], cwd, check=False).returncode == 0


def merged_to_target(cwd: Path, branch: str, target_ref: str) -> bool | None:
    """Return whether branch is an ancestor of target_ref."""
    if not branch or not target_ref or not ref_exists(cwd, target_ref):
        return None
    result = git_run(["merge-base", "--is-ancestor", branch, target_ref], cwd, check=False)
    return result.returncode == 0


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
    return {
        "repo": repo_name,
        "task_slug": path.name,
        "path": display_path(path, project_root),
        "absolute_path": str(path),
        "branch": branch,
        "head": wt.get("head", "")[:7],
        "head_full": wt.get("head", ""),
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
        ("ai-rules", project_root / ".codex" / "ai-rules"),
    ]
    worktree_base = project_root / ".codex" / "project" / ".worktree"
    snapshot: dict[str, Any] = {
        "schema_version": 2,
        "last_updated": now_iso(),
        "project_root": str(project_root.resolve()),
        "core_principle": "一切流程化 + 可审计",
        "audit_policy": {
            "worktree_state_source": ".codex/project/state/worktrees.json",
            "script_entry": "python .codex/ai-rules/scripts/ai_rules.py worktree-task status --write-state",
            "require_commit_before_merge": True,
            "require_state_snapshot_before_closeout": True,
        },
    }

    for repo_name, repo_path in repos:
        if not (repo_path / ".git").exists():
            continue
        main_status = short_status(repo_path, ignored_abs_paths)
        repo_record: dict[str, Any] = {
            "main_worktree": display_path(repo_path, project_root),
            "main_branch": current_branch(repo_path),
            "main_head": current_head(repo_path)[:7],
            "main_head_full": current_head(repo_path),
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


def status_state_path(project_root: Path, output: str | None) -> Path:
    """Resolve the status snapshot output path."""
    path = Path(output).expanduser() if output else project_root / ".codex" / "project" / "state" / "worktrees.json"
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
    for repo_name in ("self", "ai-rules"):
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
            print(f"    head: {wt['head']}")
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

    if args.dry_run:
        print(f"[dry-run] Would create worktree:")
        print(f"  source_repo: {source_repo}")
        print(f"  worktree_path: {worktree_path}")
        print(f"  branch: {branch_name}")
        print(f"  base: {args.base or 'HEAD'}")
        return 0

    # Create parent directory
    worktree_path.parent.mkdir(parents=True, exist_ok=True)

    # Create worktree
    git_args = ["worktree", "add", "-b", branch_name]
    if args.git_lock:
        git_args.extend(["--lock", "--reason", args.git_lock_reason or f"Codex task {task_slug}"])
    git_args.append(str(worktree_path))
    if args.base:
        git_args.append(args.base)

    result = git_run(git_args, source_repo, check=False)
    if result.returncode != 0:
        print(f"Error creating worktree:\n{result.stderr}", file=sys.stderr)
        return 1

    if args.register_session:
        session_id = args.session_id or safe_id("S")
        register_cmd = [
            sys.executable,
            str(ai_rules_script()),
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
                str(ai_rules_script()),
                "worktree-coord",
                "lock",
                "acquire",
                "--session-id",
                session_id,
                "--reason",
                args.lock_reason or f"Codex task {task_slug}",
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


def command_close(args: argparse.Namespace) -> int:
    """Check worktree closeout status."""
    task_slug = args.task_slug
    repo_root = find_project_root(Path.cwd(), args.project_root)
    worktree_path = task_worktree_path(repo_root, task_slug)

    if not worktree_path.exists():
        print(f"Error: worktree not found: {worktree_path}", file=sys.stderr)
        return 1

    source_repo = source_repo_for(repo_root, args.repo)

    worktrees = parse_worktree_list(source_repo)
    wt = next((w for w in worktrees if Path(w["worktree"]).resolve() == worktree_path.resolve()), None)
    if not wt:
        print(f"Error: worktree not registered: {worktree_path}", file=sys.stderr)
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
    print(f"| 下一步/用户需确认 | {'可以按需要移除 worktree' if not is_dirty and is_merged else '需要用户确认是否合并/提交/push'} |")

    if args.session_id:
        close_cmd = [
            sys.executable,
            str(ai_rules_script()),
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage task-level Git worktrees with fixed conventions."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    # create
    create = subparsers.add_parser("create", help="Create a new task worktree.")
    create.add_argument("--project-root", help="Host project root containing .codex/project.")
    create.add_argument("--title", required=True, help="Task title.")
    create.add_argument("--repo", choices=["self", "ai-rules"], required=True, help="Source repository.")
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
    create.add_argument("--dry-run", action="store_true", help="Show what would be created.")
    
    # status
    status = subparsers.add_parser("status", help="Show task worktrees status.")
    status.add_argument("--project-root", help="Host project root containing .codex/project.")
    status.add_argument("--task-slug", "--task", dest="task_slug", help="Show one task slug.")
    status.add_argument("--target-ref", default="main", help="Target ref used for merged status. Default: main.")
    status.add_argument("--format", choices=["text", "json"], default="text", help="Output format.")
    status.add_argument(
        "--write-state",
        nargs="?",
        const="",
        help="Write full status snapshot. Default path: .codex/project/state/worktrees.json.",
    )
    
    # close
    close = subparsers.add_parser("close", help="Check worktree closeout status.")
    close.add_argument("--project-root", help="Host project root containing .codex/project.")
    close.add_argument("--task-slug", "--task", dest="task_slug", required=True, help="Task slug (worktree directory name).")
    close.add_argument("--repo", choices=["self", "ai-rules"], default="self", help="Source repository.")
    close.add_argument("--target-ref", default="main", help="Target ref for merge check.")
    close.add_argument("--session-id", help="Close this worktree-coord session after checks.")
    close.add_argument("--keep-locks", action="store_true", help="Keep locks when closing the session.")
    
    # remove
    remove = subparsers.add_parser("remove", help="Remove a task worktree.")
    remove.add_argument("--project-root", help="Host project root containing .codex/project.")
    remove.add_argument("--task-slug", "--task", dest="task_slug", required=True, help="Task slug (worktree directory name).")
    remove.add_argument("--repo", choices=["self", "ai-rules"], default="self", help="Source repository.")
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
    if args.command == "close":
        return command_close(args)
    if args.command == "remove":
        return command_remove(args)
    
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
