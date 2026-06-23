#!/usr/bin/env python3
"""Expose repository-local Codex skills through the current project.

The skill sources remain in `.ai-client/project/skills` and
`.ai-client/ai-client-governance/skills`. Some Codex surfaces only discover
project-local skills from a root `skills/` directory at session startup, so this
command creates local links or copies in the current project instead of
installing anything into the user's global CODEX_HOME.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from ai_client_governance.common.paths import COMMON_REPO_PATH, PROJECT_SKILLS_DIR, host_project_root


SOURCE_PRIORITY = ("project", "common")
WINDOWS_JUNCTION = "junction"
COPY_MODE = "copy"
SYMLINK_MODE = "symlink"


@dataclass(frozen=True)
class SkillSource:
    name: str
    source_kind: str
    path: str
    active: bool
    installed: bool
    shadowed_by: str = ""
    description: str = ""


@dataclass(frozen=True)
class InstallResult:
    name: str
    source_kind: str
    source: str
    destination: str
    status: str
    action: str
    message: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Discover and expose repository-local Codex skills.")
    sub = parser.add_subparsers(dest="command", required=True)

    list_cmd = sub.add_parser("list", help="List .ai-client project and ai-client-governance skill sources.")
    add_common_args(list_cmd)

    install = sub.add_parser(
        "install-local",
        help="Expose active .ai-client skills through the current project's root skills/ directory.",
    )
    add_common_args(install)
    install.add_argument("--skill", action="append", default=[], help="Install only this skill name. Repeatable.")
    install.add_argument(
        "--mode",
        choices=(WINDOWS_JUNCTION, SYMLINK_MODE, COPY_MODE),
        default=WINDOWS_JUNCTION if os.name == "nt" else SYMLINK_MODE,
        help="Installation method. Default: junction on Windows, symlink elsewhere.",
    )
    install.add_argument("--execute", action="store_true", help="Actually create local links/copies. Default is dry-run.")
    return parser.parse_args()


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", default=".", help="Project or embedded governance root. Default: current directory.")
    parser.add_argument("--dest", help="Local skills destination. Default: <project-root>/skills.")
    parser.add_argument("--format", choices=("text", "json"), default="text")


def resolve_dest(project_root: Path, value: str | None) -> Path:
    if not value:
        return (project_root / "skills").resolve()
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = project_root / path
    return path.resolve()


def display_path(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def skill_source_dirs(project_root: Path) -> list[tuple[str, Path]]:
    return [
        ("project", project_root / PROJECT_SKILLS_DIR),
        ("common", project_root / COMMON_REPO_PATH / "skills"),
    ]


def parse_frontmatter(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return {}
    if not text.startswith("---"):
        return {}
    match = re.match(r"^---\r?\n(?P<body>.*?)\r?\n---", text, flags=re.DOTALL)
    if not match:
        return {}
    result: dict[str, str] = {}
    key = ""
    lines: list[str] = []
    for raw_line in match.group("body").splitlines():
        if raw_line.startswith((" ", "\t")) and key:
            lines.append(raw_line.strip())
            continue
        if key:
            result[key] = " ".join(item for item in lines if item).strip().strip("'\"")
        if ":" not in raw_line:
            key = ""
            lines = []
            continue
        key, value = raw_line.split(":", 1)
        key = key.strip()
        value = value.strip()
        lines = [] if value in {">", "|", ">-", "|-"} else [value]
    if key:
        result[key] = " ".join(item for item in lines if item).strip().strip("'\"")
    return result


def discover_skills(project_root: Path, dest: Path) -> list[SkillSource]:
    candidates: list[tuple[str, str, Path, str]] = []
    priority = {kind: index for index, kind in enumerate(SOURCE_PRIORITY)}
    for source_kind, directory in skill_source_dirs(project_root):
        if not directory.exists():
            continue
        for item in sorted(directory.iterdir(), key=lambda value: value.name.lower()):
            skill_file = item / "SKILL.md"
            if not item.is_dir() or not skill_file.exists():
                continue
            meta = parse_frontmatter(skill_file)
            name = str(meta.get("name") or item.name).strip()
            description = str(meta.get("description") or "").strip()
            if name:
                candidates.append((source_kind, name, item.resolve(), description))

    candidates.sort(key=lambda row: (priority.get(row[0], 999), row[1].lower()))
    selected: dict[str, str] = {}
    result: list[SkillSource] = []
    for source_kind, name, path, description in candidates:
        installed = (dest / name / "SKILL.md").exists()
        if name in selected:
            result.append(
                SkillSource(
                    name=name,
                    source_kind=source_kind,
                    path=display_path(project_root, path),
                    active=False,
                    installed=installed,
                    shadowed_by=selected[name],
                    description=description,
                )
            )
            continue
        selected[name] = source_kind
        result.append(
            SkillSource(
                name=name,
                source_kind=source_kind,
                path=display_path(project_root, path),
                active=True,
                installed=installed,
                description=description,
            )
        )
    return result


def active_skill_paths(project_root: Path, dest: Path, requested: set[str]) -> tuple[list[tuple[SkillSource, Path]], list[str]]:
    skills = discover_skills(project_root, dest)
    active = [item for item in skills if item.active]
    known = {item.name for item in skills}
    missing = sorted(requested - known)
    if requested:
        active = [item for item in active if item.name in requested]
    pairs: list[tuple[SkillSource, Path]] = []
    for item in active:
        source = Path(item.path)
        if not source.is_absolute():
            source = project_root / source
        pairs.append((item, source.resolve()))
    return pairs, missing


def ensure_inside_project(project_root: Path, dest: Path) -> None:
    resolved_root = project_root.resolve()
    resolved_dest = dest.resolve()
    if resolved_dest != resolved_root / "skills" and resolved_root not in resolved_dest.parents:
        raise ValueError(f"Local skill destination must stay inside the current project: {dest}")


def create_junction(source: Path, dest: Path) -> str:
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(dest), str(source)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stdout.strip() or "mklink /J failed")
    return completed.stdout.strip()


def install_one(item: SkillSource, source: Path, dest_root: Path, *, mode: str, execute: bool) -> InstallResult:
    dest = dest_root / item.name
    if dest.exists() or dest.is_symlink():
        return InstallResult(
            name=item.name,
            source_kind=item.source_kind,
            source=str(source),
            destination=str(dest),
            status="exists",
            action="skip",
            message="destination already exists; remove or rename it intentionally before reinstalling",
        )
    if not execute:
        return InstallResult(
            name=item.name,
            source_kind=item.source_kind,
            source=str(source),
            destination=str(dest),
            status="planned",
            action=mode,
        )
    dest_root.mkdir(parents=True, exist_ok=True)
    action = mode
    if mode == COPY_MODE:
        shutil.copytree(source, dest)
    elif mode == SYMLINK_MODE:
        dest.symlink_to(source, target_is_directory=True)
    elif mode == WINDOWS_JUNCTION:
        if os.name == "nt":
            create_junction(source, dest)
        else:
            dest.symlink_to(source, target_is_directory=True)
            action = SYMLINK_MODE
    else:
        raise ValueError(f"Unsupported install mode: {mode}")
    return InstallResult(
        name=item.name,
        source_kind=item.source_kind,
        source=str(source),
        destination=str(dest),
        status="installed",
        action=action,
    )


def render_list(skills: list[SkillSource], dest: Path) -> str:
    lines = [
        "AI Client Governance local skill sources",
        f"Local skills dir: {dest}",
        f"Skills: {len(skills)} active={len([item for item in skills if item.active])}",
    ]
    for item in skills:
        state = "active" if item.active else f"shadowed by {item.shadowed_by}"
        installed = "installed" if item.installed else "not-installed"
        description = f" - {item.description}" if item.description else ""
        lines.append(f"- {item.name}: {item.source_kind} {state} {installed} ({item.path}){description}")
    return "\n".join(lines)


def render_install(results: list[InstallResult], *, execute: bool, dest: Path) -> str:
    installed = len([item for item in results if item.status == "installed"])
    planned = len([item for item in results if item.status == "planned"])
    existing = len([item for item in results if item.status == "exists"])
    lines = [
        "AI Client Governance local skill install",
        f"Local skills dir: {dest}",
        f"Mode: {'execute' if execute else 'dry-run'}",
        f"Results: installed={installed} planned={planned} existing={existing} total={len(results)}",
    ]
    for item in results:
        lines.append(f"- {item.name}: {item.status} via {item.action} -> {item.destination}")
        if item.message:
            lines.append(f"  {item.message}")
    if execute and installed:
        lines.append("Restart Codex from this project to pick up newly exposed local skills.")
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    requested_root = Path(args.root).resolve()
    project_root = host_project_root(requested_root).resolve()
    dest = resolve_dest(project_root, args.dest)
    ensure_inside_project(project_root, dest)

    if args.command == "list":
        skills = discover_skills(project_root, dest)
        payload = {
            "project_root": str(project_root),
            "local_skills_dir": str(dest),
            "skills": [asdict(item) for item in skills],
        }
        if args.format == "json":
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(render_list(skills, dest))
        return 0

    if args.command == "install-local":
        requested = set(args.skill or [])
        pairs, missing = active_skill_paths(project_root, dest, requested)
        results = [install_one(item, source, dest, mode=args.mode, execute=args.execute) for item, source in pairs]
        payload = {
            "project_root": str(project_root),
            "local_skills_dir": str(dest),
            "execute": bool(args.execute),
            "mode": args.mode,
            "missing_requested_skills": missing,
            "results": [asdict(item) for item in results],
        }
        if args.format == "json":
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            if missing:
                print("Missing requested skills: " + ", ".join(missing))
            print(render_install(results, execute=args.execute, dest=dest))
        return 1 if missing else 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
