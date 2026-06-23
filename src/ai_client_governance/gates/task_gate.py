#!/usr/bin/env python3
"""Read-only gate for task-type specific AI Client Governance evidence.

The session gate checks whether work can close at all. This script checks
whether the selected task type recorded the evidence that makes closure
credible: network sources for rule/tool design, logs for debug work,
correction writeback for user complaints, and validation for script/rule work.
It never writes files.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

from ai_client_governance.common.paths import (
    CORRECTIONS_DIR,
    CORRECTIONS_INDEX,
    PENDING_TASKS_DIR,
)
from ai_client_governance.records import task_record as structured_task_record
from ai_client_governance.records import corrections as correction_records


TASK_ALIASES = {
    "code-debug": {
        "code",
        "debug",
        "mod",
        "代码",
        "调试",
        "故障排查",
        "日志",
    },
    "correction": {
        "correction",
        "corrections",
        "纠错",
        "修正",
        "用户投诉",
        "用户纠错",
    },
    "rules-script": {
        "rules",
        "rule",
        "script",
        "skill",
        "规则",
        "脚本",
        "门禁",
        "skill",
    },
    "docs": {
        "docs",
        "doc",
        "document",
        "文档",
        "重构",
        "新文档",
    },
    "git": {
        "git",
        "commit",
        "push",
        "提交",
        "推送",
    },
    "frontend": {
        "frontend",
        "ui",
        "browser",
        "前端",
        "页面",
        "浏览器",
    },
    "resume": {
        "resume",
        "pdf",
        "简历",
        "导出",
    },
    "multi-agent": {
        "multi-agent",
        "agent",
        "sub-agent",
        "delegated agent",
        "委派 Agent",
        "智能体",
    },
    "long-running": {
        "long-running",
        "pending",
        "恢复",
        "长任务",
        "未完成",
    },
}

URL_RE = re.compile(r"https?://[^\s)>\]]+")
REQ_ID_RE = re.compile(r"\b(?:REQ|UR)-[A-Za-z0-9][A-Za-z0-9_-]*\b")
CORRECTION_PATH_RE = re.compile(
    r"\.ai-client/project/records/corrections/[^\s`|,)]+?\.md"
)
CORRECTION_ID_RE = re.compile(r"\bCORR-[A-Za-z0-9_-]+\b")

SCRIPT_CAPABILITY_SIGNALS = [
    "脚本不支持",
    "脚本功能不支持",
    "功能不支持",
    "参数无法表达",
    "输出缺少",
    "登记失败",
    "手工绕过",
    "绕过机制",
    "脚本能力",
    "门禁脚本误判",
]

CONTEXT_COMPRESSION_SIGNALS = [
    "连续纠错",
    "追加新要求",
    "新增要求",
    "改变判断标准",
    "计划变长",
    "上下文压缩",
    "压缩快照",
]

OPEN_REQUIREMENT_STATUS_SIGNALS = [
    "待处理",
    "未处理",
    "未执行",
    "未开始",
    "遗漏",
    "未覆盖",
]

ALLOWED_OPEN_REQUIREMENT_REASONS = [
    "阻塞",
    "暂缓",
    "用户改为暂缓",
    "不处理原因",
    "后续计划",
    "等待用户",
    "非本轮范围",
]

PLACEHOLDER_CELLS = {"", "待补", "待补。", "TBD", "TODO", "N/A", "NA", "-"}

TRIGGER_LOG_REQUIRED_GROUPS = [
    ("trigger id", ["TRG-", "触发 ID", "触发ID"]),
    ("trigger source", ["触发来源", "用户要求", "批准", "门禁", "规则", "脚本"]),
    ("matched requirement or rule", ["命中的要求", "命中规则", "要求或规则", "AGENTS", "门禁"]),
    ("priority or highest-level judgement", ["优先级", "最高等级", "最高要求", "P0", "高"]),
    ("applicability judgement", ["适用范围", "适用场景", "适用", "范围"]),
    ("scope expansion judgement", ["是否扩大", "扩大范围", "范围扩大", "未扩大"]),
    ("reason", ["判断原因", "原因", "因为", "依据"]),
    ("required action", ["必须动作", "动作", "必须执行"]),
    ("executed steps", ["已执行步骤", "执行步骤", "步骤", "已执行"]),
    ("quantitative evidence", ["量化证据", "量化", "统计", "次数", "行数", "覆盖率"]),
    ("status", ["状态", "已完成", "已验证", "阻塞"]),
    ("trace", ["trace_id", "trace", "TRACE"]),
]

OUTPUT_INFO_REQUIRED_GROUPS = [
    ("output id", ["OUT-", "输出 ID", "输出ID"]),
    ("output type", ["输出类型", "计划", "状态", "最终回复", "文档说明", "脚本报告", "错误", "阻塞", "仓库状态"]),
    ("applicability scope", ["适用范围", "覆盖", "适用"]),
    ("exclusions", ["排除范围", "非本轮范围", "不适用", "不覆盖"]),
    ("objects", ["涉及对象", "仓库", "路径", "文件", "脚本", "文档", "项目", "分支", "remote"]),
    ("fact source", ["事实源", "证据", "git status", "验证", "telemetry", "审计记录", "trace", "report"]),
    ("completed items", ["已完成", "完成项"]),
    ("unfinished items", ["未完成", "剩余", "无未完成"]),
    ("unverified items", ["未验证", "未验", "无未验证"]),
    ("blocked items", ["阻塞", "无阻塞"]),
    ("user confirmation", ["用户需确认", "需确认", "批准", "无"]),
    ("final coverage", ["最终输出", "覆盖口径", "最终回复", "输出覆盖"]),
]

WORKTREE_MERGE_CLOSEOUT_SIGNALS = [
    "未合并",
    "已合并",
    "未自动合并",
    "不自动合并",
    "不会自动合并",
    "合并未执行",
    "合并已完成",
    "等待合并",
    "无需合并",
]

WORKTREE_COMMIT_CLOSEOUT_SIGNALS = [
    "未提交",
    "已提交",
    "未 commit",
    "已 commit",
    "未自动提交",
    "不自动提交",
    "不会自动提交",
    "commit 未执行",
    "commit 已完成",
    "未 stage",
    "已 stage",
    "未暂存",
    "已暂存",
]

WORKTREE_PUSH_CLOSEOUT_SIGNALS = [
    "未 push",
    "已 push",
    "未推送",
    "已推送",
    "未自动 push",
    "不自动 push",
    "不会自动 push",
    "push 未执行",
    "push 已完成",
    "无需 push",
    "无需推送",
]

WORKTREE_NEXT_ACTION_SIGNALS = [
    "下一步",
    "next action",
    "Next action",
    "后续动作",
    "下一阶段",
]


@dataclass
class Finding:
    level: str
    message: str
    file: str | None = None


@dataclass
class Report:
    root: str
    task_tracking: str | None
    task_types: list[str]
    errors: list[Finding]
    warnings: list[Finding]
    notes: list[Finding]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check task-type specific evidence in an AI Client Governance task tracking file."
    )
    parser.add_argument("--root", default=".", help="Repository root.")
    parser.add_argument("--task-tracking", help="Task tracking file to check.")
    parser.add_argument(
        "--task-types",
        nargs="*",
        default=None,
        help="Task types to require. If omitted, parse them from ## 任务类型门禁.",
    )
    parser.add_argument(
        "--require-task-types",
        action="store_true",
        help="Fail when no task type can be determined.",
    )
    parser.add_argument(
        "--only-worktree-creation-policy",
        action="store_true",
        help="Only validate worktree creation method and sparse-checkout policy evidence.",
    )
    parser.add_argument(
        "--fail-on-warning",
        action="store_true",
        help="Exit non-zero when warnings are found.",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format.",
    )
    parser.add_argument(
        "--task-id",
        help="Structured task id to validate from the SQLite task-record database.",
    )
    parser.add_argument(
        "--db",
        help="Structured task-record SQLite path. Default: <ai-client-project>/state/aicg.db.",
    )
    parser.add_argument(
        "--structured-event",
        choices=("preflight", "final"),
        default="final",
        help="Structured task-record gate event.",
    )
    return parser.parse_args()


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def rel_path(path: Path, root: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def section_text(text: str, heading: str) -> str:
    pattern = re.compile(
        rf"^##\s+{re.escape(heading)}\s*$([\s\S]*?)(?=^##\s+|\Z)",
        re.MULTILINE,
    )
    match = pattern.search(text)
    return match.group(1) if match else ""


def normalize_task_type(value: str) -> str | None:
    lowered = value.strip().lower()
    for canonical, aliases in TASK_ALIASES.items():
        if lowered == canonical:
            return canonical
        if lowered in {alias.lower() for alias in aliases}:
            return canonical
    return None


def parse_task_types(text: str, explicit: list[str] | None) -> list[str]:
    found: list[str] = []
    candidates: list[str] = []
    if explicit:
        candidates.extend(explicit)
    gate = section_text(text, "任务类型门禁")
    if gate:
        candidates.extend(re.split(r"[\s,，、/|:：]+", gate))

    for candidate in candidates:
        normalized = normalize_task_type(candidate)
        if normalized and normalized not in found:
            found.append(normalized)
    return found


def infer_task_types(text: str) -> list[str]:
    inferred: list[str] = []
    log_section = section_text(text, "日志与可观测性记录")
    code_debug_explicitly_not_applicable = contains_any(
        log_section,
        ["code-debug` 不适用", "code-debug 不适用", "不是代码运行"],
    )
    if not code_debug_explicitly_not_applicable and contains_any(
        text,
        [
            "UE4SS.log",
            "BGUHasBuffByID",
            "BGUAddBuff",
            "TriggerEffectToTarget",
            "hasBuffAfter",
            "watched runtime event",
            "main.lua",
            "OwnedBuffConfig.lua",
            "Lua 静态",
            "运行日志",
        ],
    ):
        inferred.append("code-debug")
    if contains_any(
        text,
        [
            CORRECTIONS_DIR.as_posix() + "/",
            ".ai-client/corrections/",
            "用户纠错",
            "修正文档",
        ],
    ):
        inferred.append("correction")
    if contains_any(
        text,
        [
            "ai_client_governance.py task-gate",
            "ai_client_governance.py session-gate",
            "门禁脚本",
            "通用规则",
            "规则/脚本",
            "AGENTS.md",
        ],
    ):
        inferred.append("rules-script")
    if contains_any(text, ["validate-doc", ".references", "Definition of Done"]):
        inferred.append("docs")
    if contains_any(
        text,
        [
            PENDING_TASKS_DIR.as_posix(),
            ".ai-client/pending-tasks",
            "active pending",
            "恢复现场",
        ],
    ):
        inferred.append("long-running")
    return inferred


def contains_any(text: str, patterns: list[str]) -> bool:
    lowered = text.lower()
    return any(pattern.lower() in lowered for pattern in patterns)


def has_network_search_trigger(text: str) -> bool:
    return contains_any(
        text,
        [
            "联网",
            "搜索",
            "查资料",
            "查文档",
            "查官网",
            "查询",
            "查找",
            "核对",
            "最新",
            "资料",
            "URL",
            "引用",
            "官方",
            "权威",
        ],
    )


def req_ids(text: str) -> set[str]:
    return set(REQ_ID_RE.findall(text))


def split_markdown_row(line: str) -> list[str]:
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return []
    return [cell.strip() for cell in stripped.strip("|").split("|")]


def is_markdown_separator(cells: list[str]) -> bool:
    if not cells:
        return False
    return all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in cells)


def markdown_tables(section: str) -> list[tuple[list[str], list[list[str]]]]:
    tables: list[tuple[list[str], list[list[str]]]] = []
    lines = section.splitlines()
    index = 0
    while index < len(lines):
        header = split_markdown_row(lines[index])
        if not header or index + 1 >= len(lines):
            index += 1
            continue
        separator = split_markdown_row(lines[index + 1])
        if not is_markdown_separator(separator):
            index += 1
            continue
        rows: list[list[str]] = []
        index += 2
        while index < len(lines):
            row = split_markdown_row(lines[index])
            if not row:
                break
            if len(row) < len(header):
                row.extend([""] * (len(header) - len(row)))
            rows.append(row[: len(header)])
            index += 1
        tables.append((header, rows))
    return tables


def header_index(headers: list[str], patterns: list[str]) -> int | None:
    for index, header in enumerate(headers):
        if contains_any(header, patterns):
            return index
    return None


def table_with_columns(
    section: str,
    required_columns: list[tuple[str, list[str]]],
) -> tuple[list[str], list[list[str]], dict[str, int]] | None:
    for headers, rows in markdown_tables(section):
        indexes: dict[str, int] = {}
        for label, patterns in required_columns:
            matched = header_index(headers, patterns)
            if matched is None:
                break
            indexes[label] = matched
        else:
            return headers, rows, indexes
    return None


def section_text_any(text: str, headings: list[str]) -> tuple[str, str]:
    for heading in headings:
        section = section_text(text, heading)
        if section.strip():
            return section, heading
    return "", ""


def meaningful_identity(value: str) -> bool:
    text = value.strip()
    if not text:
        return False
    return not contains_any(
        text,
        ["待定", "未知", "unknown", "n/a", "na", "未记录", "不明", "无客户端"],
    )


def review_result_passed(result: str, retest: str = "") -> bool:
    combined = f"{result} {retest}"
    has_pass = contains_any(combined, ["通过", "pass", "passed", "approved", "复测通过", "最终通过"])
    if not has_pass:
        return False
    has_fail = contains_any(combined, ["不通过", "未通过", "fail", "failed", "blocked", "阻塞"])
    if has_fail and not contains_any(combined, ["整改后通过", "复测通过", "最终通过", "已修复并通过"]):
        return False
    return True


def review_items_closed(cell: str) -> bool:
    text = cell.strip()
    if not text:
        return False
    if contains_any(text, ["无未处理", "无遗漏", "none", "no unresolved", "已处理", "已关闭", "已记录 follow-up"]):
        return True
    if contains_any(text, ["未处理", "未解决", "遗漏", "blocking", "阻塞"]) and not contains_any(
        text,
        ["无未处理", "已处理", "已关闭", "已记录", "follow-up"],
    ):
        return False
    return contains_any(text, ["无", "none", "已记录", "follow-up", "后续任务", "整改完成"])


def requirement_gate_ids(text: str) -> set[str]:
    section = section_text(text, "用户要求追踪门禁")
    table = user_requirement_table(section)
    if not table:
        return set()
    _, rows, indexes = table
    id_index = indexes["id"]
    ids: set[str] = set()
    for row in rows:
        ids.update(req_ids(row[id_index]))
    return ids


def user_requirement_table(section: str) -> tuple[list[str], list[list[str]], dict[str, int]] | None:
    return table_with_columns(
        section,
        [
            ("id", ["ID", "要求 ID", "要求ID", "REQ"]),
            ("requirement", ["用户要求", "原话", "要求内容", "最新指令"]),
            ("status", ["状态", "当前状态"]),
            ("action", ["处理动作", "动作"]),
            ("implementation", ["实现证据", "落点", "文件", "脚本"]),
            ("validation", ["验证证据", "验证", "门禁", "命令"]),
            ("final", ["最终回复", "收口", "回复覆盖", "覆盖口径"]),
        ],
    )


def has_section(text: str, heading: str) -> bool:
    return bool(section_text(text, heading).strip())


def matching_sections(text: str, heading_keywords: list[str]) -> str:
    parts: list[str] = []
    pattern = re.compile(r"^##\s+(.+?)\s*$([\s\S]*?)(?=^##\s+|\Z)", re.MULTILINE)
    for match in pattern.finditer(text):
        heading = match.group(1)
        if contains_any(heading, heading_keywords):
            parts.append(match.group(2))
    return "\n".join(parts)


def has_network_evidence(text: str) -> bool:
    section = section_text(text, "联网核对记录")
    if not section.strip():
        return False
    if URL_RE.search(section):
        return True
    return contains_any(
        section,
        ["不适用", "无需联网", "无法联网", "未找到权威资料", "风险边界"],
    )


def add(items: list[Finding], level: str, message: str, file: str | None = None) -> None:
    items.append(Finding(level=level, message=message, file=file))


def validate_network(text: str, errors: list[Finding], tracking: str) -> None:
    if not has_network_evidence(text):
        add(
            errors,
            "error",
            "Rules/scripts/design work must record network sources or an explicit non-applicable reason.",
            tracking,
        )


def validate_code_debug(text: str, errors: list[Finding], tracking: str) -> None:
    section = section_text(text, "日志与可观测性记录")
    if not section.strip():
        section = matching_sections(text, ["日志", "可观测", "归因", "验证记录"])
    if not section.strip():
        add(errors, "error", "code-debug requires ## 日志与可观测性记录.", tracking)
        return

    required_groups = [
        ("log source or diagnostic command", ["日志来源", "日志路径", "UE4SS.log", "stdout", "stderr", "复现命令"]),
        ("key log summary", ["关键日志", "日志证据", "已确认", "错误码", "Lua error", "hasBuffAfter"]),
        ("validation pattern or next diagnostic", ["pattern", "验证用日志", "hasBuffAfter", "watched runtime event", "loaded", "待验证"]),
    ]
    for label, patterns in required_groups:
        if not contains_any(section, patterns):
            add(errors, "error", f"code-debug log evidence lacks {label}.", tracking)


def has_nonempty_severity(text: str) -> bool:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if "严重程度" not in line:
            continue
        suffix = re.split(r"[:：]", line, maxsplit=1)
        if len(suffix) > 1 and suffix[1].strip():
            return True
        for next_line in lines[index + 1 : index + 4]:
            stripped = next_line.strip()
            if not stripped:
                continue
            if stripped.startswith("## "):
                return False
            return True
    return False


def validate_correction_records(
    text: str,
    root: Path,
    errors: list[Finding],
    tracking: str,
) -> list[tuple[str, str]]:
    normalized = text.replace("\\", "/")
    refs = sorted(set(CORRECTION_PATH_RE.findall(normalized)))
    record_refs = [
        ref
        for ref in refs
        if Path(ref).name not in {"README.md", "index.md"}
    ]
    if not record_refs:
        add(errors, "error", "correction task must name independent correction record files.", tracking)
        return []

    index_path = root / CORRECTIONS_INDEX
    index_text = index_path.read_text(encoding="utf-8") if index_path.exists() else ""
    if not index_text:
        add(errors, "error", "corrections index.md is missing or empty.", CORRECTIONS_INDEX.as_posix())

    records: list[tuple[str, str]] = []
    for ref in record_refs:
        record_path = root / ref
        if not record_path.exists():
            add(errors, "error", "referenced correction record does not exist.", ref)
            continue
        record_text = record_path.read_text(encoding="utf-8")
        records.append((ref, record_text))
        if Path(ref).name not in index_text:
            add(errors, "error", "referenced correction record is not listed in index.md.", ref)
    return records


def correction_db_rows(
    text: str,
    root: Path,
    db_override: str | None,
) -> list[dict[str, str]]:
    ids = sorted(set(CORRECTION_ID_RE.findall(text)))
    if not ids:
        return []
    db = correction_records.db_path(root, db_override)
    if not db.exists():
        return []
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        table = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='corrections'"
        ).fetchone()
        if table is None:
            return []
        placeholders = ", ".join("?" for _ in ids)
        rows = con.execute(
            "SELECT correction_id, severity, status, impact, upgrade_judgment "
            f"FROM corrections WHERE correction_id IN ({placeholders})",
            ids,
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        con.close()


def validate_correction_db_records(
    rows: list[dict[str, str]],
    errors: list[Finding],
    notes: list[Finding],
    tracking: str,
) -> bool:
    if not rows:
        return False
    missing: list[str] = []
    for row in rows:
        correction_id = str(row.get("correction_id") or "")
        if not str(row.get("severity") or "").strip():
            missing.append(f"{correction_id}:severity")
        if not str(row.get("impact") or "").strip():
            missing.append(f"{correction_id}:impact")
        if not str(row.get("upgrade_judgment") or "").strip():
            missing.append(f"{correction_id}:upgrade_judgment")
    if missing:
        add(
            errors,
            "error",
            "DB correction records must contain severity, impact, and upgrade_judgment: "
            + ", ".join(missing),
            tracking,
        )
    else:
        add(
            notes,
            "note",
            "Correction evidence resolved from SQLite corrections table; Markdown correction files/index are not required.",
            tracking,
        )
    return True


def validate_correction_severity_and_impact(
    text: str,
    records: list[tuple[str, str]],
    errors: list[Finding],
    tracking: str,
) -> None:
    if not records:
        return

    if not any(has_nonempty_severity(record_text) for _, record_text in records):
        add(
            errors,
            "error",
            "correction records must contain a non-empty severity field.",
            tracking,
        )

    combined_records = "\n".join(record_text for _, record_text in records)
    combined = f"{text}\n{combined_records}"
    if not contains_any(combined, ["影响面审计", "影响面扫描", "受影响", "影响判断"]):
        add(
            errors,
            "error",
            "correction task must record impact audit or affected-scope analysis.",
            tracking,
        )

    if contains_any(combined, ["暂不升级"]) and not contains_any(
        combined,
        ["不表示问题轻微", "不代表问题轻微", "不表示不严重", "已有防线", "后续观察"],
    ):
        add(
            errors,
            "error",
            "`暂不升级` must explain that it is not a severity downgrade and record existing defenses or observation.",
            tracking,
        )


def validate_python_cache_boundary(text: str, errors: list[Finding], tracking: str) -> None:
    if contains_any(
        text,
        [
            "py_compile",
            "ai_client_governance.py",
            "task-gate",
            "session-gate",
            "Python 脚本",
        ],
    ) and not contains_any(text, ["PYTHONPYCACHEPREFIX", "python-pycache", "pycache_prefix"]):
        add(
            errors,
            "error",
            "Python script validation must record pycache redirection to .ai-client/project/cache.",
            tracking,
        )


def validate_applicability_gate(text: str, errors: list[Finding], tracking: str) -> None:
    section = section_text(text, "适用范围门禁")
    if not section.strip():
        add(
            errors,
            "error",
            "rules-script design work must record ## 适用范围门禁.",
            tracking,
        )
        return

    required_groups = [
        ("intended scope", ["适用范围", "适用场景", "触发场景", "覆盖对象"]),
        ("exclusions", ["排除范围", "不适用", "丢弃", "不处理"]),
        ("practicality", ["实用性", "可操作", "人工步骤", "成本"]),
        ("efficiency", ["效率", "提速", "耗时", "读取文件数", "脚本化检查项数"]),
        ("extensibility", ["扩展性", "可扩展", "可演进", "后续升级", "树形", "trace"]),
        ("quantitative source", ["量化", "指标", "统计口径", "事实源", "telemetry", "审计记录"]),
    ]
    for label, patterns in required_groups:
        if not contains_any(section, patterns):
            add(errors, "error", f"applicability gate lacks {label}.", tracking)


def validate_runtime_effectiveness_gate(text: str, errors: list[Finding], tracking: str) -> None:
    if not contains_any(text, ["runtime", "runtime component", "运行时组件", "gate-pool", "ComponentDefinition"]):
        return
    required_groups = [
        ("runtime component visibility", ["runtime components", "组件", "节点可见"]),
        ("gate-pool plan or execution", ["gate-pool", "门禁池", "dry-run"]),
        ("completion test planning", ["completion-test", "完成测试", "测试计划"]),
        ("dedupe or performance evidence", ["dedupe", "去重", "性能", "耗时", "只运行一次"]),
    ]
    for label, patterns in required_groups:
        if not contains_any(text, patterns):
            add(errors, "error", f"runtime architecture work must record {label}.", tracking)


def validate_worktree_live_state_gate(text: str, errors: list[Finding], tracking: str) -> None:
    if not contains_any(text, ["coord", "worktree-coord", "live-state", "live state", "session", "lock", "integration queue"]):
        return
    if not contains_any(text, ["worktree-task reconcile", "git worktree list", "live-state", "live state"]):
        add(errors, "error", "worktree/coord work must record Git live-state reconciliation evidence.", tracking)


def validate_worktree_evidence(text: str, errors: list[Finding], tracking: str) -> None:
    section = section_text(text, "Worktree 证据") or section_text(text, "当前 Git 状态")
    haystack = section if section.strip() else text
    required_groups = [
        ("git worktree command or label", ["git worktree", "worktree"]),
        ("fixed worktree root", [".ai-client/project/.worktree", ".ai-client\\project\\.worktree"]),
        ("branch evidence", ["分支", "branch"]),
        ("base commit evidence", ["基准提交", "base commit", "rev-parse"]),
        ("status evidence", ["git status", "工作区", "status --short"]),
    ]
    for label, patterns in required_groups:
        if not contains_any(haystack, patterns):
            add(errors, "error", f"modifying task must record {label}.", tracking)


def validate_worktree_creation_policy(text: str, errors: list[Finding], tracking: str) -> None:
    section = section_text(text, "Worktree 证据") or section_text(text, "当前 Git 状态")
    if not section.strip():
        add(errors, "error", "worktree creation policy requires ## Worktree 证据.", tracking)
        return

    required_groups = [
        ("worktree creation method", ["worktree-task create", "创建方式", "创建命令"]),
        ("sparse checkout strategy", ["sparse", "稀疏", "--include-source-projects", "--exclude-path"]),
        ("source snapshot handling", [".source-projects", "source-projects", "源码目录", "源码快照", "不适用"]),
    ]
    for label, patterns in required_groups:
        if not contains_any(section, patterns):
            add(errors, "error", f"worktree creation policy lacks {label}.", tracking)

    if contains_any(section, ["git worktree add"]) and not contains_any(section, ["worktree-task create"]):
        if not contains_any(section, ["break-glass", "例外", "手工", "原因", "理由", "为什么"]):
            add(
                errors,
                "error",
                "raw git worktree add must record a break-glass reason and sparse strategy.",
                tracking,
            )


def validate_worktree_completion_record(text: str, errors: list[Finding], tracking: str) -> None:
    section = section_text(text, "Worktree 完成记录")
    if not section.strip():
        add(errors, "error", "modifying task must record ## Worktree 完成记录.", tracking)
        return

    required_groups = [
        ("worktree completion status", ["worktree", "完成", "状态"]),
        ("merge boundary", ["合并", "未合并", "不自动合并"]),
        ("stage or commit boundary", ["stage", "暂存", "commit", "提交", "未提交", "未暂存"]),
        ("push boundary", ["push", "推送", "未 push", "未推送"]),
        ("next user action", ["下一步", "用户需确认", "等待用户", "确认"]),
    ]
    for label, patterns in required_groups:
        if not contains_any(section, patterns):
            add(errors, "error", f"worktree completion record lacks {label}.", tracking)


def validate_input_decomposition_gate(text: str, errors: list[Finding], tracking: str) -> None:
    section = section_text(text, "用户输入拆解门禁")
    if not section.strip():
        add(errors, "error", "task tracking must record ## 用户输入拆解门禁.", tracking)
        return

    metadata_groups = [
        ("raw input or latest instruction", ["原始输入", "最新指令", "用户输入", "用户原话"]),
        ("requirement count", ["任务数", "要求数", "拆分数量", "数量"]),
    ]
    for label, patterns in metadata_groups:
        if not contains_any(section, patterns):
            add(errors, "error", f"input decomposition gate lacks {label}.", tracking)

    table = table_with_columns(
        section,
        [
            ("req", ["REQ ID", "要求 ID", "要求ID", "ID"]),
            ("requirement", ["用户要求", "要求摘要", "最新指令", "内容"]),
            ("record", ["记录判定", "落盘判定", "是否记录"]),
            ("network", ["联网/搜索判定", "联网判定", "搜索判定", "网络判定"]),
            (
                "validation",
                ["Agent/验证判定", "多 Agent 判定", "Agent 判定", "验证判定", "黑盒判定"],
            ),
            ("acceptance", ["验收", "完成口径", "最终回复", "覆盖口径"]),
        ],
    )
    if not table:
        add(
            errors,
            "error",
            "input decomposition gate must use a per-REQ table with recording, network/search, validation, and acceptance judgement columns.",
            tracking,
        )
        return

    _, rows, indexes = table
    if not rows:
        add(errors, "error", "input decomposition gate per-REQ table has no requirement rows.", tracking)
        return

    input_req_ids: set[str] = set()
    for row in rows:
        row_ids = req_ids(row[indexes["req"]])
        if not row_ids:
            add(errors, "error", "input decomposition row lacks REQ id.", tracking)
            continue
        input_req_ids.update(row_ids)
        requirement = row[indexes["requirement"]].strip()
        record = row[indexes["record"]].strip()
        network = row[indexes["network"]].strip()
        validation = row[indexes["validation"]].strip()
        acceptance = row[indexes["acceptance"]].strip()
        row_label = ", ".join(sorted(row_ids))
        if not requirement:
            add(errors, "error", f"{row_label} input decomposition row lacks user requirement text.", tracking)
        if not contains_any(record, ["必须记录", "需记录", "写入", "落盘", "不记录", "无需记录", "不落盘"]):
            add(errors, "error", f"{row_label} input decomposition row lacks explicit recording judgement.", tracking)
        if not contains_any(network, ["触发", "不触发", "需要", "无需", "联网", "搜索", "URL", "资料", "引用", "证据"]):
            add(errors, "error", f"{row_label} input decomposition row lacks explicit network/search judgement.", tracking)
        if not contains_any(validation, ["触发", "不触发", "需要", "无需", "验证", "测试", "selftest", "task-gate", "Agent", "黑盒"]):
            add(errors, "error", f"{row_label} input decomposition row lacks explicit subagent/validation judgement.", tracking)
        if not contains_any(acceptance, ["验收", "最终回复", "覆盖", "完成口径", "说明"]):
            add(errors, "error", f"{row_label} input decomposition row lacks final acceptance coverage.", tracking)

        if has_network_search_trigger(requirement):
            if contains_any(network, ["不触发", "无需联网", "无需搜索", "不需要联网", "不需要搜索"]):
                add(
                    errors,
                    "error",
                    f"{row_label} has network/search trigger words but judgement says non-applicable.",
                    tracking,
                )
            if not contains_any(network, ["触发", "需要", "联网", "搜索", "URL", "资料", "引用", "证据", "核对"]):
                add(
                    errors,
                    "error",
                    f"{row_label} has network/search trigger words but lacks source/evidence judgement.",
                    tracking,
                )

    tracked_req_ids = requirement_gate_ids(text)
    if tracked_req_ids:
        missing = sorted(tracked_req_ids - input_req_ids)
        if missing:
            add(
                errors,
                "error",
                f"input decomposition gate missing REQ coverage: {', '.join(missing)}.",
                tracking,
            )


def validate_multi_agent_acceptance_matrix(text: str, errors: list[Finding], tracking: str) -> set[str]:
    deprecated_sections = [
        "子 AI 验收矩阵",
        "子智能体验收矩阵",
        "多智能体验收矩阵",
    ]
    for deprecated in deprecated_sections:
        if section_text(text, deprecated).strip():
            add(
                errors,
                "error",
                f"deprecated multi-agent section ## {deprecated} is not accepted; use ## 多 Agent 验收矩阵.",
                tracking,
            )

    section, _ = section_text_any(
        text,
        [
            "多 Agent 验收矩阵",
        ],
    )
    if not section.strip():
        add(errors, "error", "multi-agent task must record ## 多 Agent 验收矩阵.", tracking)
        return set()

    table = table_with_columns(
        section,
        [
            ("agent", ["执行 Agent", "executor agent"]),
            ("client", ["执行客户端", "执行 client_type", "executor client"]),
            ("req", ["覆盖 REQ", "REQ", "要求"]),
            ("gates", ["覆盖门禁", "门禁", "gates"]),
            ("coverage", ["全面覆盖", "覆盖判定", "覆盖率"]),
            ("failure", ["失败路径", "失败用例", "failure"]),
            ("success", ["成功路径", "成功用例", "success"]),
            ("finding", ["发现问题", "修复复测", "复测", "remediation"]),
        ],
    )
    if not table:
        add(
            errors,
            "error",
            "multi-agent acceptance matrix must use a structured table with agent, client_type, REQ, gate, coverage, failure, success, and remediation columns.",
            tracking,
        )
        return set()

    _, rows, indexes = table
    if not rows:
        add(errors, "error", "multi-agent acceptance matrix has no agent rows.", tracking)
        return set()

    covered_req_ids: set[str] = set()
    covered_agents: set[str] = set()
    for row in rows:
        agent = row[indexes["agent"]].strip()
        client = row[indexes["client"]].strip()
        row_req_ids = req_ids(row[indexes["req"]])
        gates = row[indexes["gates"]].strip()
        coverage = row[indexes["coverage"]].strip()
        failure = row[indexes["failure"]].strip()
        success = row[indexes["success"]].strip()
        finding = row[indexes["finding"]].strip()
        row_label = agent or "multi-agent acceptance row"
        if not meaningful_identity(agent):
            add(errors, "error", "multi-agent acceptance matrix row lacks agent name.", tracking)
        else:
            covered_agents.add(agent)
        if not meaningful_identity(client):
            add(errors, "error", f"{row_label} multi-agent acceptance row lacks executor client_type.", tracking)
        if not row_req_ids:
            add(errors, "error", f"{row_label} acceptance row lacks covered REQ ids.", tracking)
        covered_req_ids.update(row_req_ids)
        required_cells = [
            ("covered gates", gates, ["门禁", "gate", "task-gate", "session-gate", "输出门禁", "输入拆解"]),
            ("full coverage judgement", coverage, ["全面", "全部", "覆盖矩阵", "覆盖率", "无遗漏"]),
            ("failure path", failure, ["失败路径", "失败用例", "exit 1", "应失败", "缺失", "失败"]),
            ("success path", success, ["成功路径", "成功用例", "exit 0", "应通过", "补齐", "通过"]),
            ("finding or remediation", finding, ["发现问题", "修复", "复测", "问题已修", "无问题"]),
        ]
        for label, cell, patterns in required_cells:
            if not contains_any(cell, patterns):
                add(errors, "error", f"{row_label} multi-agent acceptance row lacks {label}.", tracking)

    input_table = table_with_columns(
        section_text(text, "用户输入拆解门禁"),
        [
            ("req", ["REQ ID", "要求 ID", "要求ID", "ID"]),
            (
                "validation",
                [
                    "Agent/验证判定",
                    "多 Agent 判定",
                    "验证判定",
                    "黑盒判定",
                ],
            ),
        ],
    )
    if input_table:
        _, input_rows, input_indexes = input_table
        required_matrix_ids: set[str] = set()
        for input_row in input_rows:
            validation = input_row[input_indexes["validation"]]
            explicit_non_agent = contains_any(
                validation,
                ["不触发 Agent", "无需 Agent", "无 Agent"],
            )
            explicit_agent = contains_any(
                validation,
                ["multi-agent", "多 Agent", "多 Agent 验收矩阵"],
            ) or (contains_any(validation, ["Agent"]) and not explicit_non_agent)
            if explicit_agent:
                required_matrix_ids.update(req_ids(input_row[input_indexes["req"]]))
        missing = sorted(required_matrix_ids - covered_req_ids)
        if missing:
            add(
                errors,
                "error",
                f"multi-agent acceptance matrix missing triggered REQ coverage: {', '.join(missing)}.",
                tracking,
            )
    return covered_agents


def validate_multi_agent_review_result(
    text: str,
    errors: list[Finding],
    tracking: str,
    expected_executors: set[str],
) -> None:
    section, _ = section_text_any(
        text,
        [
            "多 Agent 审批结论",
        ],
    )
    if not section.strip():
        add(errors, "error", "multi-agent task must record ## 多 Agent 审批结论.", tracking)
        return

    table = table_with_columns(
        section,
        [
            ("executor_agent", ["执行 Agent", "executor agent"]),
            ("executor_client", ["执行客户端", "executor client", "执行 client_type"]),
            ("reviewed_task", ["执行任务", "reviewed task", "task id", "leaf id", "任务 ID"]),
            ("reviewer_agent", ["复核 Agent", "reviewer agent"]),
            ("reviewer_client", ["复核客户端", "reviewer client", "复核 client_type"]),
            ("result", ["复核结果", "审批结论", "pass/fail", "结论", "result"]),
            ("lifecycle", ["生命周期事实", "lifecycle"]),
            ("commit", ["提交状态", "commit", "merge", "push"]),
            ("unhandled", ["未处理项", "unhandled"]),
            ("quality", ["处理质量不足", "质量不足", "low-quality", "low quality"]),
            ("evidence", ["证据", "evidence", "依据"]),
            ("remediation", ["整改建议", "remediation", "修复建议"]),
            ("retest", ["复测", "retest"]),
        ],
    )
    if not table:
        add(
            errors,
            "error",
            (
                "multi-agent review result must use a structured table with executor/reviewer agents, "
                "client_type, reviewed task, pass/fail result, lifecycle, commit, unhandled, quality, "
                "evidence, remediation, and retest columns."
            ),
            tracking,
        )
        return

    _, rows, indexes = table
    if not rows:
        add(errors, "error", "multi-agent review result has no review rows.", tracking)
        return

    reviewed_executors: set[str] = set()
    for row in rows:
        executor = row[indexes["executor_agent"]].strip()
        executor_client = row[indexes["executor_client"]].strip()
        reviewed_task = row[indexes["reviewed_task"]].strip()
        reviewer = row[indexes["reviewer_agent"]].strip()
        reviewer_client = row[indexes["reviewer_client"]].strip()
        result = row[indexes["result"]].strip()
        lifecycle = row[indexes["lifecycle"]].strip()
        commit = row[indexes["commit"]].strip()
        unhandled = row[indexes["unhandled"]].strip()
        quality = row[indexes["quality"]].strip()
        evidence = row[indexes["evidence"]].strip()
        remediation = row[indexes["remediation"]].strip()
        retest = row[indexes["retest"]].strip()
        row_label = reviewed_task or executor or "multi-agent review row"

        if not meaningful_identity(executor):
            add(errors, "error", "multi-agent review row lacks executor agent.", tracking)
        else:
            reviewed_executors.add(executor)
        if not meaningful_identity(executor_client):
            add(errors, "error", f"{row_label} review row lacks executor client_type.", tracking)
        if not meaningful_identity(reviewer):
            add(errors, "error", f"{row_label} review row lacks reviewer agent.", tracking)
        if not meaningful_identity(reviewer_client):
            add(errors, "error", f"{row_label} review row lacks reviewer client_type.", tracking)
        if executor and reviewer and executor.lower() == reviewer.lower() and executor_client.lower() == reviewer_client.lower():
            add(errors, "error", f"{row_label} reviewer must be independent from executor.", tracking)
        if not meaningful_identity(reviewed_task):
            add(errors, "error", f"{row_label} review row lacks reviewed task id or leaf id.", tracking)
        if not review_result_passed(result, retest):
            add(errors, "error", f"{row_label} review result must be a final pass with retest evidence.", tracking)
        for label, cell, patterns in [
            ("task queue lifecycle fact", lifecycle, ["task queue", "task-queue", "任务队列"]),
            ("task record fact", lifecycle, ["task record", "task-record", "任务记录"]),
            ("worktree live state", lifecycle, ["worktree", "live state", "dirty", "clean", "工作区"]),
            ("validation/final gate fact", lifecycle, ["validation", "验证", "final gate", "最终门禁"]),
            ("telemetry/raw shell fact", lifecycle, ["telemetry", "raw shell", "shell", "遥测"]),
            ("commit/merge/push status", commit, ["commit", "提交", "merge", "合并", "push", "推送"]),
        ]:
            if not contains_any(cell, patterns):
                add(errors, "error", f"{row_label} review row lacks {label}.", tracking)
        if not review_items_closed(unhandled):
            add(errors, "error", f"{row_label} review row has unresolved unhandled items.", tracking)
        if not review_items_closed(quality):
            add(errors, "error", f"{row_label} review row has unresolved low-quality items.", tracking)
        if not meaningful_identity(evidence):
            add(errors, "error", f"{row_label} review row lacks evidence.", tracking)
        if not contains_any(remediation, ["整改", "修复", "退回", "无需整改", "无整改", "remediation"]):
            add(errors, "error", f"{row_label} review row lacks remediation guidance.", tracking)
        if not contains_any(retest, ["复测", "retest"]) or not review_result_passed(retest):
            add(errors, "error", f"{row_label} review row lacks passing retest result.", tracking)

    missing = sorted(expected_executors - reviewed_executors)
    if missing:
        add(
            errors,
            "error",
            f"multi-agent review result missing executor coverage: {', '.join(missing)}.",
            tracking,
        )


def validate_user_requirement_gate(text: str, errors: list[Finding], tracking: str) -> None:
    section = section_text(text, "用户要求追踪门禁")
    if not section.strip():
        add(errors, "error", "task tracking must record ## 用户要求追踪门禁.", tracking)
        return

    table = user_requirement_table(section)
    if not table:
        add(
            errors,
            "error",
            "user requirement gate must use a structured Markdown table with ID, requirement, status, action, implementation, validation, and final coverage columns.",
            tracking,
        )
        return

    _, rows, indexes = table
    if not rows:
        add(errors, "error", "user requirement gate has no REQ-/UR- rows.", tracking)
        return

    seen_ids: set[str] = set()
    for row in rows:
        row_ids = req_ids(row[indexes["id"]])
        if not row_ids:
            add(errors, "error", "user requirement gate row lacks REQ-/UR- id.", tracking)
            continue
        seen_ids.update(row_ids)
        row_label = ", ".join(sorted(row_ids))
        required_cells = [
            ("user requirement", row[indexes["requirement"]]),
            ("status", row[indexes["status"]]),
            ("implementation action", row[indexes["action"]]),
            ("implementation evidence", row[indexes["implementation"]]),
            ("validation evidence", row[indexes["validation"]]),
            ("final response coverage", row[indexes["final"]]),
        ]
        for label, cell in required_cells:
            if not cell.strip():
                add(errors, "error", f"{row_label} user requirement gate row lacks {label}.", tracking)
    if not seen_ids:
        add(errors, "error", "user requirement gate must contain at least one REQ-/UR- row.", tracking)

    if contains_any(section, OPEN_REQUIREMENT_STATUS_SIGNALS) and not contains_any(
        section, ALLOWED_OPEN_REQUIREMENT_REASONS
    ):
        add(
            errors,
            "error",
            "open or missed requirement rows must record a blocking/deferred reason.",
            tracking,
        )

    if "批准：" in text and "批准：" not in section:
        add(errors, "error", "approval labels must be mirrored in ## 用户要求追踪门禁.", tracking)


def trigger_log_rows(section: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or "---" in stripped or "TRG-" not in stripped:
            continue
        rows.append([cell.strip() for cell in stripped.strip("|").split("|")])
    return rows


def validate_requirement_trigger_log(text: str, errors: list[Finding], tracking: str) -> None:
    section = section_text(text, "要求触发日志")
    if not section.strip():
        add(errors, "error", "task tracking must record ## 要求触发日志.", tracking)
        return

    for label, patterns in TRIGGER_LOG_REQUIRED_GROUPS:
        if not contains_any(section, patterns):
            add(errors, "error", f"requirement trigger log lacks {label}.", tracking)

    rows = trigger_log_rows(section)
    if not rows:
        add(errors, "error", "requirement trigger log must contain at least one TRG- row.", tracking)
        return

    for row in rows:
        row_text = " ".join(row)
        trigger_id = next((cell for cell in row if "TRG-" in cell), "TRG-unknown")
        if len(row) < 12:
            add(
                errors,
                "error",
                f"{trigger_id} must fill all trigger log columns, including scope, quantification, status, and trace.",
                tracking,
            )
        if any(cell.strip() in PLACEHOLDER_CELLS for cell in row):
            add(errors, "error", f"{trigger_id} contains blank or placeholder trigger-log cells.", tracking)
        if not contains_any(row_text, ["最高", "优先级", "P0", "高"]):
            add(errors, "error", f"{trigger_id} lacks highest-priority judgement.", tracking)
        if not contains_any(row_text, ["适用", "范围"]):
            add(errors, "error", f"{trigger_id} lacks applicability/scope judgement.", tracking)
        if not contains_any(row_text, ["扩大", "未扩大"]):
            add(errors, "error", f"{trigger_id} lacks scope-expansion judgement.", tracking)
        if not contains_any(row_text, ["量化", "统计", "次", "条", "行", "覆盖率", "%", ">=", "<="]):
            add(errors, "error", f"{trigger_id} lacks quantitative evidence.", tracking)
        if not contains_any(row_text, ["trace", "TRACE"]):
            add(errors, "error", f"{trigger_id} lacks trace_id evidence.", tracking)


def output_info_rows(section: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or "---" in stripped or "OUT-" not in stripped:
            continue
        rows.append([cell.strip() for cell in stripped.strip("|").split("|")])
    return rows


def row_text(row: list[str]) -> str:
    return " ".join(row)


def worktree_output_rows(rows: list[list[str]]) -> list[list[str]]:
    return [
        row
        for row in rows
        if contains_any(
            row_text(row),
            [
                "worktree",
                "Worktree",
                ".ai-client/project/.worktree",
                ".ai-client\\project\\.worktree",
                "Worktree 完成记录",
            ],
        )
    ]


def output_closeout_cells(rows: list[list[str]]) -> str:
    parts: list[str] = []
    for row in rows:
        if len(row) > 10:
            parts.append(row[10])
        if len(row) > 11:
            parts.append(row[11])
    return " ".join(parts)


def validate_output_information_gate(text: str, errors: list[Finding], tracking: str) -> None:
    section = section_text(text, "输出信息门禁")
    if not section.strip():
        add(errors, "error", "task tracking must record ## 输出信息门禁.", tracking)
        return

    for label, patterns in OUTPUT_INFO_REQUIRED_GROUPS:
        if not contains_any(section, patterns):
            add(errors, "error", f"output information gate lacks {label}.", tracking)

    rows = output_info_rows(section)
    if not rows:
        add(errors, "error", "output information gate must contain at least one OUT- row.", tracking)
        return

    required_output_types = ["计划", "状态", "最终回复", "脚本报告", "错误", "仓库状态"]
    covered_types = " ".join(" ".join(row) for row in rows)
    for output_type in required_output_types:
        if output_type not in covered_types:
            add(errors, "error", f"output information gate does not cover output type: {output_type}.", tracking)

    for row in rows:
        current_row_text = row_text(row)
        output_id = next((cell for cell in row if "OUT-" in cell), "OUT-unknown")
        if len(row) < 13:
            add(
                errors,
                "error",
                f"{output_id} must fill all output gate columns, including scope, evidence, unfinished and final coverage.",
                tracking,
            )
        if any(cell.strip() in PLACEHOLDER_CELLS for cell in row):
            add(errors, "error", f"{output_id} contains blank or placeholder output-gate cells.", tracking)
        if not contains_any(current_row_text, ["适用范围", "覆盖", "适用"]):
            add(errors, "error", f"{output_id} lacks applicability scope.", tracking)
        if not contains_any(current_row_text, ["排除", "非本轮", "不适用", "不覆盖"]):
            add(errors, "error", f"{output_id} lacks exclusions or non-scope boundary.", tracking)
        if not contains_any(current_row_text, ["事实源", "证据", "git status", "验证", "telemetry", "审计记录", "trace", "report"]):
            add(errors, "error", f"{output_id} lacks fact source or evidence.", tracking)
        if not contains_any(current_row_text, ["未完成", "剩余", "无未完成"]):
            add(errors, "error", f"{output_id} lacks unfinished-item statement.", tracking)
        if not contains_any(current_row_text, ["未验证", "无未验证"]):
            add(errors, "error", f"{output_id} lacks unverified-item statement.", tracking)
        if not contains_any(current_row_text, ["阻塞", "无阻塞"]):
            add(errors, "error", f"{output_id} lacks blocked-item statement.", tracking)
        if not contains_any(current_row_text, ["最终输出", "最终回复", "覆盖口径"]):
            add(errors, "error", f"{output_id} lacks final output coverage.", tracking)

    if contains_any(text, ["worktree", ".ai-client/project/.worktree", ".ai-client\\project\\.worktree"]):
        worktree_rows = worktree_output_rows(rows)
        if not worktree_rows:
            add(errors, "error", "output information gate lacks worktree output object.", tracking)
            return
        closeout_text = output_closeout_cells(worktree_rows)
        required_worktree_closeout_groups = [
            ("explicit merge closeout status", WORKTREE_MERGE_CLOSEOUT_SIGNALS),
            ("explicit commit or stage closeout status", WORKTREE_COMMIT_CLOSEOUT_SIGNALS),
            ("explicit push closeout status", WORKTREE_PUSH_CLOSEOUT_SIGNALS),
            ("next user confirmation", WORKTREE_NEXT_ACTION_SIGNALS),
        ]
        for label, patterns in required_worktree_closeout_groups:
            if not contains_any(closeout_text, patterns):
                add(errors, "error", f"output information gate lacks {label}.", tracking)


def validate_script_capability_gate(text: str, errors: list[Finding], tracking: str) -> None:
    if not contains_any(text, SCRIPT_CAPABILITY_SIGNALS):
        return
    section = section_text(text, "脚本能力适配门禁")
    if not section.strip():
        add(
            errors,
            "error",
            "script capability signals require ## 脚本能力适配门禁.",
            tracking,
        )
        return

    required_groups = [
        ("current objective", ["当前目标", "目标"]),
        ("script gap", ["脚本缺口", "不支持", "参数", "输出", "失败"]),
        ("bypass risk", ["绕过", "风险", "产物", "运行态", "锁", "telemetry", "审计记录"]),
        ("quality/applicability", ["质量目标", "适用范围", "实用性", "效率"]),
        ("decision", ["修脚本", "记录阻塞", "受控入口", "决策"]),
        ("validation", ["验证", "最小真实用例", "py_compile", "--help", "task gate"]),
    ]
    for label, patterns in required_groups:
        if not contains_any(section, patterns):
            add(errors, "error", f"script capability gate lacks {label}.", tracking)


def validate_context_compression_snapshot(text: str, errors: list[Finding], tracking: str) -> None:
    if not contains_any(text, CONTEXT_COMPRESSION_SIGNALS):
        return
    section = section_text(text, "上下文压缩快照")
    if not section.strip():
        add(
            errors,
            "error",
            "context compression signals require ## 上下文压缩快照.",
            tracking,
        )
        return

    required_groups = [
        ("trigger", ["触发原因", "连续纠错", "新要求"]),
        ("latest user requirement", ["最新用户要求", "用户要求"]),
        ("confirmed facts", ["已确认事实", "事实"]),
        ("approved plan", ["已批准计划", "批准"]),
        ("risk boundary", ["风险边界", "禁止误动"]),
        ("return action", ["返回", "主任务", "下一步"]),
        ("restore list", ["最小恢复读取清单", "恢复读取"]),
    ]
    for label, patterns in required_groups:
        if not contains_any(section, patterns):
            add(errors, "error", f"context compression snapshot lacks {label}.", tracking)


def validate_task_type(
    task_type: str,
    text: str,
    root: Path,
    errors: list[Finding],
    warnings: list[Finding],
    notes: list[Finding],
    tracking: str,
    db_override: str | None = None,
) -> None:
    if task_type in {
        "correction",
        "rules-script",
        "docs",
        "git",
        "frontend",
        "resume",
        "multi-agent",
        "long-running",
    }:
        validate_worktree_evidence(text, errors, tracking)
        validate_worktree_creation_policy(text, errors, tracking)
        validate_worktree_completion_record(text, errors, tracking)

    if task_type == "code-debug":
        validate_code_debug(text, errors, tracking)
    elif task_type == "correction":
        db_rows = correction_db_rows(text, root, db_override)
        if validate_correction_db_records(db_rows, errors, notes, tracking):
            if not contains_any(text, ["是否需要升级", "已提炼进要求", "规则沉淀判断"]):
                add(
                    notes,
                    "note",
                    "Correction upgrade/rule decision came from SQLite correction records.",
                    tracking,
                )
        else:
            if not contains_any(
                text,
                [CORRECTIONS_DIR.as_posix(), ".ai-client/corrections", "correction", "修正文档"],
            ):
                add(errors, "error", "correction task must mention correction records.", tracking)
            if "index.md" not in text:
                add(errors, "error", "correction task must mention index.md writeback.", tracking)
            if not contains_any(text, ["是否需要升级", "已提炼进要求", "规则沉淀判断"]):
                add(errors, "error", "correction task must record upgrade/rule decision.", tracking)
            records = validate_correction_records(text, root, errors, tracking)
            validate_correction_severity_and_impact(text, records, errors, tracking)
    elif task_type == "rules-script":
        validate_network(text, errors, tracking)
        if not contains_any(text, ["批准标签", "批准：", "approval"]):
            add(errors, "error", "rules-script task must record approval label.", tracking)
        if not has_section(text, "验证记录"):
            add(errors, "error", "rules-script task must record ## 验证记录.", tracking)
        validate_applicability_gate(text, errors, tracking)
        validate_script_capability_gate(text, errors, tracking)
        validate_context_compression_snapshot(text, errors, tracking)
        validate_python_cache_boundary(text, errors, tracking)
        validate_runtime_effectiveness_gate(text, errors, tracking)
        validate_worktree_live_state_gate(text, errors, tracking)
        if contains_any(text, ["scripts/", "scripts\\"]) and not contains_any(
            text,
            ["py_compile", "--help", "语法解析", "最小真实用例"],
        ):
            add(
                warnings,
                "warning",
                "script changes should record compile/help or minimum real-use validation.",
                tracking,
            )
    elif task_type == "docs":
        if not contains_any(text, ["影响面扫描", "Definition of Done", "validate-doc"]):
            add(errors, "error", "docs task requires impact scan, DoD, and doc gate evidence.", tracking)
    elif task_type == "git":
        if not contains_any(text, ["git status", "工作区", "push", "推送边界"]):
            add(errors, "error", "git task requires status and push boundary evidence.", tracking)
        validate_worktree_live_state_gate(text, errors, tracking)
    elif task_type == "frontend":
        if not contains_any(text, ["browser", "screenshot", "截图", "Playwright", "localhost"]):
            add(warnings, "warning", "frontend task should record browser/screenshot verification.", tracking)
    elif task_type == "resume":
        if not contains_any(text, ["PDF", "导出", "页数", "留白"]):
            add(errors, "error", "resume task requires PDF export/layout evidence.", tracking)
    elif task_type == "multi-agent":
        if not contains_any(text, ["agent", "智能体", "current-status", "brief"]):
            add(errors, "error", "multi-agent task requires agent status/brief evidence.", tracking)
        covered_agents = validate_multi_agent_acceptance_matrix(text, errors, tracking)
        validate_multi_agent_review_result(text, errors, tracking, covered_agents)
    elif task_type == "long-running":
        if not contains_any(text, ["pending", "恢复现场", "下一步"]):
            add(errors, "error", "long-running task requires pending/recovery evidence.", tracking)
    else:
        add(notes, "note", f"No task-type rule implemented for {task_type}.", tracking)


def build_report(
    root: Path,
    task_tracking_arg: str | None,
    explicit_task_types: list[str] | None,
    require_task_types: bool,
    only_worktree_creation_policy: bool = False,
    db_override: str | None = None,
) -> Report:
    errors: list[Finding] = []
    warnings: list[Finding] = []
    notes: list[Finding] = []

    if not task_tracking_arg:
        add(errors, "error", "Provide --task-tracking for task-type gate validation.")
        return Report(str(root.resolve()), None, [], errors, warnings, notes)

    task_tracking = Path(task_tracking_arg)
    if not task_tracking.is_absolute():
        task_tracking = root / task_tracking
    tracking_rel = rel_path(task_tracking, root)

    if not task_tracking.exists():
        add(errors, "error", "Task tracking file does not exist.", tracking_rel)
        return Report(str(root.resolve()), tracking_rel, [], errors, warnings, notes)

    text = read_text(task_tracking)
    if only_worktree_creation_policy:
        validate_worktree_creation_policy(text, errors, tracking_rel)
        return Report(str(root.resolve()), tracking_rel, [], errors, warnings, notes)

    validate_input_decomposition_gate(text, errors, tracking_rel)
    validate_user_requirement_gate(text, errors, tracking_rel)
    validate_requirement_trigger_log(text, errors, tracking_rel)
    validate_output_information_gate(text, errors, tracking_rel)
    task_types = parse_task_types(text, explicit_task_types)
    inferred_task_types = infer_task_types(text)
    for task_type in inferred_task_types:
        if task_type not in task_types:
            task_types.append(task_type)
    if require_task_types and not task_types:
        add(errors, "error", "No task type selected in ## 任务类型门禁 or --task-types.", tracking_rel)

    for task_type in task_types:
        validate_task_type(task_type, text, root, errors, warnings, notes, tracking_rel, db_override)

    if inferred_task_types:
        add(notes, "note", f"Inferred task types: {', '.join(inferred_task_types)}.", tracking_rel)
    if task_types:
        add(notes, "note", f"Checked task types: {', '.join(task_types)}.", tracking_rel)

    return Report(
        root=str(root.resolve()),
        task_tracking=tracking_rel,
        task_types=task_types,
        errors=errors,
        warnings=warnings,
        notes=notes,
    )


def format_findings(title: str, items: list[Finding]) -> list[str]:
    lines = [f"{title}: {len(items)}"]
    if not items:
        lines.append("  none")
        return lines
    for item in items:
        location = f" [{item.file}]" if item.file else ""
        lines.append(f"  - {item.message}{location}")
    return lines


def format_text(report: Report) -> str:
    lines = [
        "AI Client Governance Task Gate Report",
        f"Root: {report.root}",
        f"Task tracking: {report.task_tracking or 'none'}",
        f"Task types: {', '.join(report.task_types) if report.task_types else 'none'}",
        "",
    ]
    lines.extend(format_findings("Errors", report.errors))
    lines.append("")
    lines.extend(format_findings("Warnings", report.warnings))
    lines.append("")
    lines.extend(format_findings("Notes", report.notes))
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    root = Path(args.root).resolve()
    if args.task_id:
        db = structured_task_record.db_path(root, args.db)
        if not db.exists():
            print(f"task-record error: structured DB does not exist: {db}", file=sys.stderr)
            return 2
        con = structured_task_record.connect(db, create=False)
        structured_report = structured_task_record.validate_task(
            con=con,
            db=db,
            task_id=args.task_id,
            event=args.structured_event,
            explicit_task_types=args.task_types or [],
        )
        if args.format == "json":
            print(json.dumps(asdict(structured_report), ensure_ascii=False, indent=2))
        else:
            print(structured_task_record.format_gate_report(structured_report))
        if structured_report.errors:
            return 1
        if args.fail_on_warning and structured_report.warnings:
            return 1
        return 0

    report = build_report(
        root=root,
        task_tracking_arg=args.task_tracking,
        explicit_task_types=args.task_types,
        require_task_types=args.require_task_types,
        only_worktree_creation_policy=args.only_worktree_creation_policy,
        db_override=args.db,
    )

    if args.format == "json":
        print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
    else:
        print(format_text(report))

    if report.errors:
        return 1
    if args.fail_on_warning and report.warnings:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
