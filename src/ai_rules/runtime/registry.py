#!/usr/bin/env python3
"""Spring-style component registry for the ai-rules client governance plugin."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


COMPONENT_KINDS = {
    "input-filter",
    "processing-interceptor",
    "output-interceptor",
    "cross-cutting-gate",
    "reporter",
}

FAIL_POLICIES = {"fail_closed", "warn_only", "requires_approval", "report_only"}
NODE_EFFECTS = {"readonly", "state_write", "repo_write", "git_write", "network", "human_interrupt"}
NODE_EVENTS = {
    "user-message",
    "plan-output",
    "status-output",
    "write-intent",
    "after-change",
    "completion-test",
    "final-output",
    "resume",
    "merge-cleanup",
    "session-start",
    "state-audit",
}

PHASE_ORDER = {
    "input": 100,
    "preflight": 200,
    "coordination": 300,
    "session": 400,
    "post-change": 500,
    "validation": 600,
    "completion": 650,
    "output": 700,
    "final-gate": 800,
    "report": 900,
}


@dataclass(frozen=True)
class AgentExecutionContext:
    input_source: str = "user"
    task_types: tuple[str, ...] = ()
    task_size: str = "small"
    changed_paths: tuple[str, ...] = ()
    final: bool = False
    event: str = ""


@dataclass(frozen=True)
class TaskTypeDefinition:
    id: str
    description: str
    mutating: bool
    requires_tracking: bool
    requires_approval: bool
    keywords: tuple[str, ...] = ()


@dataclass(frozen=True)
class ComponentDefinition:
    id: str
    kind: str
    phase: str
    order: int
    description: str
    fail_policy: str = "warn_only"
    task_types: tuple[str, ...] = ()
    task_sizes: tuple[str, ...] = ()
    input_sources: tuple[str, ...] = ()
    path_suffixes: tuple[str, ...] = ()
    path_prefixes: tuple[str, ...] = ()
    requires_changed_paths: bool = False
    final_only: bool = False
    events: tuple[str, ...] = ()
    condition: str = ""
    requires_facts: tuple[str, ...] = ()
    produces_facts: tuple[str, ...] = ()
    effect: str = "readonly"
    dedupe_key: str = ""
    performance_budget: str = ""
    dependencies: tuple[str, ...] = ()
    mechanism_label: str = ""
    gate_label: str = ""
    gate_step: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def matches(self, context: AgentExecutionContext) -> bool:
        if self.final_only and not context.final:
            return False
        if self.events and context.event and context.event not in self.events:
            return False
        if self.task_types and not set(self.task_types).intersection(context.task_types):
            return False
        if self.task_sizes and context.task_size not in self.task_sizes:
            return False
        if self.input_sources and context.input_source not in self.input_sources:
            return False
        if self.requires_changed_paths and not context.changed_paths:
            return False
        if self.path_suffixes and not any(
            Path(path).suffix.lower() in self.path_suffixes for path in context.changed_paths
        ):
            return False
        if self.path_prefixes and not any(
            normalized(path).startswith(normalized(prefix))
            for path in context.changed_paths
            for prefix in self.path_prefixes
        ):
            return False
        return True


class ComponentRegistry:
    def __init__(
        self,
        components: list[ComponentDefinition],
        task_types: dict[str, TaskTypeDefinition],
    ) -> None:
        self.components = sorted(
            components,
            key=lambda item: (PHASE_ORDER.get(item.phase, 999), item.order, item.id),
        )
        self.task_types = task_types
        self._validate()

    def _validate(self) -> None:
        ids: set[str] = set()
        for component in self.components:
            if component.id in ids:
                raise ValueError(f"duplicate runtime component id: {component.id}")
            ids.add(component.id)
            if component.kind not in COMPONENT_KINDS:
                raise ValueError(f"{component.id} has invalid kind: {component.kind}")
            if component.fail_policy not in FAIL_POLICIES:
                raise ValueError(f"{component.id} has invalid fail_policy: {component.fail_policy}")
            if component.effect not in NODE_EFFECTS:
                raise ValueError(f"{component.id} has invalid effect: {component.effect}")
            unknown_events = set(component.events) - NODE_EVENTS
            if unknown_events:
                raise ValueError(f"{component.id} references unknown events: {sorted(unknown_events)}")
            unknown = set(component.task_types) - set(self.task_types)
            if unknown:
                raise ValueError(f"{component.id} references unknown task types: {sorted(unknown)}")

    def matching_components(
        self,
        context: AgentExecutionContext,
        *,
        kind: str | None = None,
    ) -> list[ComponentDefinition]:
        items = [component for component in self.components if component.matches(context)]
        if kind:
            items = [component for component in items if component.kind == kind]
        return items

    def mechanism_labels_for_context(self, context: AgentExecutionContext) -> list[str]:
        return unique(
            component.mechanism_label or component.id
            for component in self.matching_components(context)
            if component.kind != "cross-cutting-gate"
        )

    def gate_labels_for_context(self, context: AgentExecutionContext) -> list[str]:
        return unique(
            component.gate_label or component.id
            for component in self.matching_components(context)
            if component.gate_label
        ) + unique(
            component.id
            for component in self.matching_components(context, kind="cross-cutting-gate")
            if not component.gate_label
        )

    def gate_step_ids_for_context(self, context: AgentExecutionContext) -> list[str]:
        return unique(
            component.gate_step
            for component in self.matching_components(context, kind="cross-cutting-gate")
            if component.gate_step
        )

    def as_dict(self, context: AgentExecutionContext | None = None) -> dict[str, Any]:
        components = self.components if context is None else self.matching_components(context)
        return {
            "schema_version": 1,
            "runtime": "ai-rules-client-governance-plugin",
            "task_types": [asdict(item) for item in self.task_types.values()],
            "components": [asdict(item) for item in components],
        }


def normalized(value: str | Path) -> str:
    return str(value).replace("\\", "/").lstrip("./")


def unique(values: Any) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


TASK_TYPE_DEFINITIONS: dict[str, TaskTypeDefinition] = {
    "code-debug": TaskTypeDefinition(
        id="code-debug",
        description="Code debugging, logs, exceptions, and behavior fixes.",
        mutating=True,
        requires_tracking=False,
        requires_approval=False,
        keywords=("bug", "debug", "error", "exception", "log", "traceback", "报错", "调试", "日志", "异常"),
    ),
    "correction": TaskTypeDefinition(
        id="correction",
        description="User correction, missed requirement, or process failure.",
        mutating=True,
        requires_tracking=True,
        requires_approval=True,
        keywords=("correction", "漏", "错", "纠错", "修正", "没按", "遗漏"),
    ),
    "rules-script": TaskTypeDefinition(
        id="rules-script",
        description="AI rules, scripts, skills, lifecycle, gates, or adapters.",
        mutating=True,
        requires_tracking=True,
        requires_approval=True,
        keywords=(
            "AGENTS",
            "CLAUDE.md",
            "GEMINI.md",
            "CONVENTIONS.md",
            "copilot-instructions",
            ".cursor/rules",
            ".clinerules",
            ".windsurf/rules",
            ".continue/rules",
            ".roo/rules",
            "adapter",
            "SKILL",
            "ai-rules",
            "gate",
            "hook",
            "lifecycle",
            "pipeline",
            "script",
            "state machine",
            "workflow",
            "callback",
            "wrapper",
            "生命周期",
            "流水线",
            "状态机",
            "中间层",
            "回调",
            "包装器",
            "规则",
            "脚本",
            "门禁",
        ),
    ),
    "docs": TaskTypeDefinition(
        id="docs",
        description="Markdown documents, README, references, indexes, or links.",
        mutating=True,
        requires_tracking=True,
        requires_approval=True,
        keywords=("README", "docs", "markdown", "reference", "文档", "索引", "引用", "链接"),
    ),
    "git": TaskTypeDefinition(
        id="git",
        description="Git staging, commits, pushes, worktrees, and branch state.",
        mutating=True,
        requires_tracking=True,
        requires_approval=True,
        keywords=("commit", "push", "stage", "stash", "git", "提交", "推送", "暂存"),
    ),
    "frontend": TaskTypeDefinition(
        id="frontend",
        description="Frontend, browser, Playwright, local UI, and localhost work.",
        mutating=True,
        requires_tracking=False,
        requires_approval=False,
        keywords=("browser", "frontend", "localhost", "playwright", "ui", "页面", "浏览器"),
    ),
    "resume": TaskTypeDefinition(
        id="resume",
        description="Resume markdown, PDF export, layout, and delivery files.",
        mutating=True,
        requires_tracking=True,
        requires_approval=True,
        keywords=("PDF", "resumes/", "resumes\\", "简历", "导出"),
    ),
    "multi-agent": TaskTypeDefinition(
        id="multi-agent",
        description="Sub-agent, multi-agent, delegation, and acceptance matrix work.",
        mutating=True,
        requires_tracking=True,
        requires_approval=True,
        keywords=("multi-agent", "sub-agent", "delegated agent", "主从", "子 AI", "子AI", "多智能体"),
    ),
    "long-running": TaskTypeDefinition(
        id="long-running",
        description="Pending tasks, recovery, periodic checks, and long-running work.",
        mutating=True,
        requires_tracking=True,
        requires_approval=False,
        keywords=("pending", "恢复", "继续", "长任务", "未完成", "定时", "周期"),
    ),
}

TASK_TYPE_KEYWORDS: dict[str, list[str]] = {
    key: list(value.keywords) for key, value in TASK_TYPE_DEFINITIONS.items()
}

MUTATING_TASK_TYPES = {
    key for key, value in TASK_TYPE_DEFINITIONS.items() if value.mutating
}

TEXT_SUFFIXES = (".css", ".html", ".js", ".json", ".md", ".ps1", ".py", ".toml", ".ts", ".yaml", ".yml")
DOC_IMPACT_SUFFIXES = (".json", ".md", ".ps1", ".py", ".toml", ".yaml", ".yml")


def component(
    id: str,
    kind: str,
    phase: str,
    order: int,
    description: str,
    *,
    fail_policy: str = "warn_only",
    task_types: tuple[str, ...] = (),
    task_sizes: tuple[str, ...] = (),
    input_sources: tuple[str, ...] = (),
    path_suffixes: tuple[str, ...] = (),
    path_prefixes: tuple[str, ...] = (),
    requires_changed_paths: bool = False,
    final_only: bool = False,
    events: tuple[str, ...] = (),
    condition: str = "",
    requires_facts: tuple[str, ...] = (),
    produces_facts: tuple[str, ...] = (),
    effect: str = "readonly",
    dedupe_key: str = "",
    performance_budget: str = "",
    dependencies: tuple[str, ...] = (),
    mechanism_label: str = "",
    gate_label: str = "",
    gate_step: str = "",
    metadata: dict[str, Any] | None = None,
) -> ComponentDefinition:
    return ComponentDefinition(
        id=id,
        kind=kind,
        phase=phase,
        order=order,
        description=description,
        fail_policy=fail_policy,
        task_types=task_types,
        task_sizes=task_sizes,
        input_sources=input_sources,
        path_suffixes=path_suffixes,
        path_prefixes=path_prefixes,
        requires_changed_paths=requires_changed_paths,
        final_only=final_only,
        events=events,
        condition=condition,
        requires_facts=requires_facts,
        produces_facts=produces_facts,
        effect=effect,
        dedupe_key=dedupe_key,
        performance_budget=performance_budget,
        dependencies=dependencies,
        mechanism_label=mechanism_label,
        gate_label=gate_label,
        gate_step=gate_step,
        metadata=metadata or {},
    )


def default_components() -> list[ComponentDefinition]:
    return [
        component(
            "input.filter.classify-source",
            "input-filter",
            "input",
            100,
            "Classify input source and trust boundary.",
            events=("user-message", "resume"),
            requires_facts=("raw_input",),
            produces_facts=("input_source", "trust_boundary"),
            condition="Run for every new user, web, file, tool, agent, or history input.",
        ),
        component(
            "input.filter.decompose-requirements",
            "input-filter",
            "input",
            110,
            "Split user input into stable REQ rows.",
            events=("user-message",),
            requires_facts=("raw_input",),
            produces_facts=("requirement_rows",),
            condition="Run when the input contains a user goal, correction, approval, or new constraint.",
        ),
        component(
            "input.filter.recordability-judgement",
            "input-filter",
            "input",
            120,
            "Decide whether each requirement must be recorded.",
            events=("user-message",),
            requires_facts=("requirement_rows",),
            produces_facts=("recordability_decisions",),
            condition="Run after requirement decomposition and before task tracking decisions.",
        ),
        component(
            "input.filter.network-search-judgement",
            "input-filter",
            "input",
            130,
            "Decide whether external search or URL evidence is required.",
            events=("user-message",),
            requires_facts=("requirement_rows",),
            produces_facts=("search_required_decisions",),
            condition="Run when user input or task type may depend on current or external facts.",
        ),
        component(
            "input.filter.acceptance-extract",
            "input-filter",
            "input",
            135,
            "Extract user-visible acceptance criteria before execution.",
            events=("user-message", "plan-output"),
            requires_facts=("requirement_rows",),
            produces_facts=("acceptance_criteria",),
            condition="Run when the user asks for implementation, plan, redesign, correction, or completion.",
        ),
        component(
            "input.filter.skill-router",
            "input-filter",
            "input",
            145,
            "Select candidate skills as capability plugins without bypassing governance gates.",
            events=("user-message",),
            requires_facts=("raw_input", "requirement_rows"),
            produces_facts=("candidate_skills",),
            condition="Run when repository, project, or global skills may match the user's request.",
        ),
        component(
            "input.filter.citation-boundary",
            "input-filter",
            "input",
            140,
            "Keep web facts separate from user instructions.",
            input_sources=("web",),
            fail_policy="fail_closed",
        ),
        component(
            "preflight.interceptor.task-tracking",
            "processing-interceptor",
            "preflight",
            200,
            "Require task tracking for medium, large, and gated work.",
            task_sizes=("medium", "large"),
            fail_policy="fail_closed",
        ),
        component(
            "preflight.interceptor.task-size",
            "processing-interceptor",
            "preflight",
            210,
            "Record task size and blast-radius reasons.",
            task_sizes=("medium", "large"),
        ),
        component(
            "preflight.gate.task-gate.requirements",
            "cross-cutting-gate",
            "preflight",
            215,
            "Require structured input decomposition and user requirement evidence.",
            task_sizes=("medium", "large"),
            gate_label="ai_rules.py task-gate:user-input-and-requirements",
            fail_policy="fail_closed",
        ),
        component(
            "preflight.interceptor.approval",
            "processing-interceptor",
            "preflight",
            220,
            "Require explicit approval before mutating rules/scripts/docs/git-sensitive scopes.",
            task_types=("rules-script", "docs", "git", "resume", "correction", "multi-agent"),
            fail_policy="requires_approval",
            events=("write-intent", "plan-output"),
            requires_facts=("task_type", "changed_paths_or_write_intent", "approval_label"),
            produces_facts=("approval_state",),
            effect="human_interrupt",
            condition="Block repo/state/Git writes unless an explicit labeled approval is present.",
        ),
        component(
            "preflight.gate.task-gate.rules-script",
            "cross-cutting-gate",
            "preflight",
            225,
            "Require task-gate evidence for rules and script architecture changes.",
            task_types=("rules-script",),
            gate_label="ai_rules.py task-gate",
            gate_step="task-gate",
            fail_policy="fail_closed",
        ),
        component(
            "preflight.gate.session-gate.rules-script",
            "cross-cutting-gate",
            "preflight",
            226,
            "Require session-gate evidence for rules and script architecture changes.",
            task_types=("rules-script",),
            gate_label="ai_rules.py session-gate",
            gate_step="session-gate",
            fail_policy="fail_closed",
        ),
        component(
            "preflight.interceptor.external-practice-check",
            "processing-interceptor",
            "preflight",
            230,
            "Record external mature-practice checks for rules and script architecture changes.",
            task_types=("rules-script",),
            fail_policy="fail_closed",
            effect="network",
            condition="Run before rules/governance architecture changes unless authoritative local evidence is sufficient and recorded.",
        ),
        component(
            "preflight.interceptor.git-boundary",
            "processing-interceptor",
            "preflight",
            240,
            "Enforce stage, commit, push, worktree, and dirty-tree boundaries.",
            task_types=("git",),
            fail_policy="fail_closed",
            events=("plan-output", "write-intent", "final-output"),
            requires_facts=("git_status", "approval_state"),
            produces_facts=("git_boundary_decision",),
            condition="Run for Git stage, commit, push, worktree, dirty tree, or branch-state operations.",
        ),
        component(
            "prewrite.gate.worktree-live-state",
            "cross-cutting-gate",
            "coordination",
            320,
            "Reconcile coord/session/queue records against Git live worktree state before writes, resume, merge cleanup, or final output.",
            events=("write-intent", "resume", "merge-cleanup", "final-output"),
            task_types=("code-debug", "correction", "rules-script", "docs", "git", "frontend", "resume", "multi-agent"),
            gate_label="ai_rules.py worktree-task reconcile",
            gate_step="worktree-live-state",
            fail_policy="fail_closed",
            requires_facts=("coord_state", "git_worktree_list"),
            produces_facts=("worktree_reconcile_report",),
            condition="Run when a task may write, recover, merge, clean up, or claim final worktree state.",
            performance_budget="One git worktree list per repository and one coord state read per repository.",
            dedupe_key="worktree-live-state:project-root",
        ),
        component(
            "prewrite.interceptor.worktree-isolation",
            "processing-interceptor",
            "coordination",
            330,
            "Require task-level worktree isolation before repository writes.",
            events=("write-intent",),
            task_types=("code-debug", "correction", "rules-script", "docs", "git", "frontend", "resume", "multi-agent"),
            fail_policy="fail_closed",
            requires_facts=("approval_state", "worktree_reconcile_report"),
            produces_facts=("worktree_isolation_decision",),
            condition="Run after live-state reconciliation and before the first file or Git write.",
        ),
        component(
            "coordination.gate.host-submodule-closeout",
            "cross-cutting-gate",
            "coordination",
            340,
            "Verify host repository gitlink, worktree state, and task tracking after embedded ai-rules merges.",
            events=("merge-cleanup", "final-output"),
            task_types=("git", "rules-script"),
            gate_label="ai_rules.py worktree-task host-closeout",
            gate_step="host-submodule-closeout",
            fail_policy="fail_closed",
            requires_facts=("git_status", "worktree_reconcile_report", "task_tracking"),
            produces_facts=("host_submodule_closeout_report",),
            condition=(
                "Run when an ai-rules worktree merge or embedded submodule closeout is in scope; "
                "the node checks the host gitlink plus task state/tracking, not only the child repository."
            ),
            performance_budget="One host git status, one gitlink lookup, one state JSON read, and targeted task tracking reads.",
            dedupe_key="host-submodule-closeout:project-root:ai-rules",
        ),
        component(
            "coordination.interceptor.agent-brief",
            "processing-interceptor",
            "coordination",
            300,
            "Require Agent Brief before spawning delegated agents.",
            task_types=("multi-agent",),
            fail_policy="fail_closed",
        ),
        component(
            "coordination.interceptor.agent-acceptance-matrix",
            "processing-interceptor",
            "coordination",
            310,
            "Require structured multi-agent acceptance matrix evidence.",
            task_types=("multi-agent",),
            fail_policy="fail_closed",
        ),
        component(
            "session.interceptor.pending-recovery",
            "processing-interceptor",
            "session",
            400,
            "Preserve pending-task recovery state for long-running work.",
            task_types=("long-running",),
            fail_policy="fail_closed",
        ),
        component(
            "state.filter.sync-check",
            "processing-interceptor",
            "session",
            410,
            "Run per-session embedded ai-rules sync state checks.",
            task_types=("long-running",),
            fail_policy="warn_only",
            events=("session-start", "status-output"),
            requires_facts=("embedded_repo_state",),
            produces_facts=("sync_check_report",),
            condition="Run at session start or status output; this is not a timed scheduler.",
            metadata={"previous_id": "periodic.filter.sync-check"},
        ),
        component(
            "state.interceptor.session-audit",
            "processing-interceptor",
            "session",
            420,
            "Audit long-running state before closeout, resume, or status output.",
            task_types=("long-running",),
            fail_policy="fail_closed",
            events=("resume", "status-output", "final-output"),
            requires_facts=("task_queue_state", "pending_records", "coord_state"),
            produces_facts=("session_state_audit",),
            condition="Run when recovering or reporting long-running state; this is not a timed scheduler.",
            metadata={"previous_id": "periodic.interceptor.state-audit"},
        ),
        component(
            "post-change.interceptor.diff-check",
            "processing-interceptor",
            "post-change",
            500,
            "Check changed paths for diff and whitespace hazards.",
            requires_changed_paths=True,
            gate_label="encoding-or-whitespace-check",
            gate_step="git-diff-check",
            fail_policy="fail_closed",
        ),
        component(
            "post-change.interceptor.doc-index-bubble",
            "processing-interceptor",
            "post-change",
            510,
            "Bubble document-index checks through README and reference scopes.",
            task_types=("docs",),
            gate_label="ai_rules.py doc-index",
            gate_step="doc-index",
            fail_policy="fail_closed",
        ),
        component(
            "post-change.interceptor.document-impact-sync",
            "processing-interceptor",
            "post-change",
            511,
            "After rules, scripts, skills, manifest, or docs change, require affected-document and reference sync judgement.",
            task_types=("rules-script", "docs"),
            path_suffixes=DOC_IMPACT_SUFFIXES,
            requires_changed_paths=True,
            gate_label="ai_rules.py doc-index",
            gate_step="doc-index",
            fail_policy="fail_closed",
            metadata={
                "dedupe_key": "doc-index:changed-paths",
                "performance_budget": "aggregate changed paths and run one indexed reference check per gate-pool invocation",
                "requires_tracking_evidence": "record affected docs, updated docs/references, or no-impact rationale",
            },
        ),
        component(
            "post-change.interceptor.reference-check",
            "processing-interceptor",
            "post-change",
            520,
            "Check Markdown references and cycle-risk boundaries.",
            task_types=("docs",),
            gate_label="ai_rules.py validate-doc",
            gate_step="validate-doc",
            fail_policy="fail_closed",
        ),
        component(
            "post-change.interceptor.reference-check.markdown-path",
            "processing-interceptor",
            "post-change",
            521,
            "Check Markdown references when changed paths contain Markdown.",
            path_suffixes=(".md",),
            gate_label="ai_rules.py validate-doc",
            gate_step="validate-doc",
            fail_policy="fail_closed",
        ),
        component(
            "post-change.gate.doc-index.markdown-path",
            "cross-cutting-gate",
            "post-change",
            522,
            "Run document reference index checks when changed paths contain Markdown.",
            path_suffixes=(".md",),
            gate_label="ai_rules.py doc-index",
            gate_step="doc-index",
            fail_policy="fail_closed",
        ),
        component(
            "post-change.gate.document-impact-index",
            "cross-cutting-gate",
            "post-change",
            523,
            "Run one scoped document impact index for changed rules, scripts, manifests, and docs.",
            task_types=("rules-script", "docs"),
            path_suffixes=DOC_IMPACT_SUFFIXES,
            requires_changed_paths=True,
            gate_label="ai_rules.py doc-index",
            gate_step="doc-index",
            fail_policy="fail_closed",
            metadata={
                "dedupe_key": "doc-index:changed-paths",
                "observability": "visible in runtime components and gate-pool dry-run",
            },
        ),
        component(
            "post-change.interceptor.resume-export",
            "processing-interceptor",
            "post-change",
            530,
            "Export and visually check affected resume PDFs.",
            task_types=("resume",),
            gate_label="PDF export/layout check",
            fail_policy="fail_closed",
        ),
        component(
            "validation.gate.py-compile",
            "cross-cutting-gate",
            "validation",
            600,
            "Compile changed Python files.",
            path_suffixes=(".py",),
            gate_label="py_compile",
            gate_step="py-compile",
            fail_policy="fail_closed",
        ),
        component(
            "validation.gate.encoding",
            "cross-cutting-gate",
            "validation",
            610,
            "Validate UTF-8 and text source hygiene for changed text files.",
            path_suffixes=TEXT_SUFFIXES,
            gate_label="encoding-or-whitespace-check",
            gate_step="validate-encoding",
            fail_policy="fail_closed",
        ),
        component(
            "validation.gate.validate-doc",
            "cross-cutting-gate",
            "validation",
            620,
            "Validate changed Markdown document task evidence.",
            path_suffixes=(".md",),
            gate_label="ai_rules.py validate-doc",
            gate_step="validate-doc",
            fail_policy="fail_closed",
        ),
        component(
            "validation.gate.scan-corrections",
            "cross-cutting-gate",
            "validation",
            630,
            "Scan correction records when correction work is in scope.",
            task_types=("correction",),
            gate_label="ai_rules.py scan-corrections",
            gate_step="scan-corrections",
            fail_policy="fail_closed",
        ),
        component(
            "validation.gate.scan-corrections.path",
            "cross-cutting-gate",
            "validation",
            631,
            "Scan correction records when changed paths touch corrections.",
            path_prefixes=(".codex/project/records/corrections/", ".codex/corrections/"),
            gate_label="ai_rules.py scan-corrections",
            gate_step="scan-corrections",
            fail_policy="fail_closed",
        ),
        component(
            "validation.gate.git-diff-check",
            "cross-cutting-gate",
            "validation",
            640,
            "Run git diff whitespace validation for changed paths.",
            requires_changed_paths=True,
            gate_label="git diff --check",
            gate_step="git-diff-check",
            fail_policy="fail_closed",
        ),
        component(
            "completion.gate.test-plan",
            "cross-cutting-gate",
            "completion",
            650,
            "Plan task completion tests from changed paths, task types, and acceptance criteria.",
            events=("completion-test", "final-output"),
            requires_changed_paths=True,
            gate_label="ai_rules.py completion-test",
            gate_step="completion-test-plan",
            fail_policy="fail_closed",
            requires_facts=("changed_paths", "task_types", "acceptance_criteria"),
            produces_facts=("completion_test_plan",),
            condition="Run after changes and before final answer when implementation or docs claim completion.",
            dedupe_key="completion-test-plan:changed-paths:task-types",
            performance_budget="Read-only path classification plus optional task tracking evidence scan.",
        ),
        component(
            "output.gate.git-state-audit",
            "cross-cutting-gate",
            "output",
            695,
            "Audit Git/worktree state at plan, status, and final output boundaries without pushing.",
            events=("plan-output", "status-output", "final-output"),
            gate_label="git status --short --branch",
            gate_step="git-state-audit",
            fail_policy="report_only",
            requires_facts=("git_status", "worktree_reconcile_report"),
            produces_facts=("git_state_audit",),
            condition="Run before user-visible plans, status reports, and final conclusions.",
            performance_budget="One git status --short --branch per relevant repository.",
        ),
        component(
            "output.interceptor.answer-quality",
            "output-interceptor",
            "output",
            700,
            "Check final answer quality and user satisfaction coverage.",
            fail_policy="fail_closed",
        ),
        component(
            "output.interceptor.user-satisfaction",
            "output-interceptor",
            "output",
            710,
            "Ensure final response covers user-visible acceptance criteria.",
            fail_policy="fail_closed",
        ),
        component(
            "output.interceptor.document-sync",
            "output-interceptor",
            "output",
            720,
            "Ensure document/index sync status is reflected in final output.",
            task_types=("docs",),
            fail_policy="fail_closed",
        ),
        component(
            "output.interceptor.runtime-effectiveness",
            "output-interceptor",
            "output",
            725,
            "Report whether registered governance nodes were visible, executed, de-duplicated, and efficient enough.",
            task_types=("rules-script", "docs"),
            fail_policy="fail_closed",
            metadata={
                "evidence": "runtime components registry, gate-pool plan, tool-flow trace, and task tracking validation notes",
            },
        ),
        component(
            "output.interceptor.finalize-closeout",
            "output-interceptor",
            "output",
            730,
            "Report completed, unverified, blocked, active pending, and Git/worktree state.",
            fail_policy="fail_closed",
        ),
        component(
            "final.gate.architecture-guard",
            "cross-cutting-gate",
            "final-gate",
            800,
            "Validate ai-rules architecture boundaries before closeout.",
            final_only=True,
            gate_label="ai_rules.py architecture-guard",
            gate_step="architecture-guard",
            fail_policy="fail_closed",
        ),
        component(
            "final.gate.task-gate",
            "cross-cutting-gate",
            "final-gate",
            810,
            "Validate task-type, user requirement, and output closeout evidence.",
            final_only=True,
            gate_label="ai_rules.py task-gate",
            gate_step="task-gate",
            fail_policy="fail_closed",
        ),
        component(
            "final.gate.session-gate",
            "cross-cutting-gate",
            "final-gate",
            820,
            "Validate session closeout evidence.",
            final_only=True,
            gate_label="ai_rules.py session-gate",
            gate_step="session-gate",
            fail_policy="fail_closed",
        ),
        component(
            "final.gate.task-queue",
            "cross-cutting-gate",
            "final-gate",
            830,
            "Validate active task queue state before closeout.",
            final_only=True,
            gate_label="ai_rules.py task-queue",
            gate_step="task-queue",
            fail_policy="fail_closed",
        ),
        component(
            "reporter.tool-invocations",
            "reporter",
            "report",
            900,
            "Report traced tool and gate invocations.",
            final_only=True,
            gate_step="tool-invocations",
            fail_policy="report_only",
        ),
        component(
            "reporter.tool-flow",
            "reporter",
            "report",
            910,
            "Verify trace flow after final gates.",
            final_only=True,
            gate_step="tool-flow",
            fail_policy="report_only",
        ),
    ]


def default_registry() -> ComponentRegistry:
    return ComponentRegistry(default_components(), TASK_TYPE_DEFINITIONS)


def requires_tracking_for(task_types: list[str] | tuple[str, ...], task_size: str) -> bool:
    if task_size in {"medium", "large"}:
        return True
    return any(TASK_TYPE_DEFINITIONS.get(task_type, _DEFAULT_TASK_TYPE).requires_tracking for task_type in task_types)


def requires_approval_for(
    task_types: list[str] | tuple[str, ...],
    *,
    changed_paths: list[str] | tuple[str, ...],
) -> bool:
    if changed_paths:
        return True
    return any(TASK_TYPE_DEFINITIONS.get(task_type, _DEFAULT_TASK_TYPE).requires_approval for task_type in task_types)


_DEFAULT_TASK_TYPE = TaskTypeDefinition(
    id="unknown",
    description="Unknown task type.",
    mutating=False,
    requires_tracking=False,
    requires_approval=False,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect the ai-rules client governance component registry.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    components = subparsers.add_parser("components", help="List registered components, optionally filtered by context.")
    components.add_argument("--input-source", default="user")
    components.add_argument("--task-type", action="append", default=[])
    components.add_argument("--task-size", choices=("small", "medium", "large"), default="small")
    components.add_argument("--changed-path", action="append", default=[])
    components.add_argument("--final", action="store_true")
    components.add_argument("--event", choices=sorted(NODE_EVENTS), default="")
    components.add_argument("--kind", choices=sorted(COMPONENT_KINDS))
    components.add_argument("--format", choices=("text", "json"), default="text")

    task_types = subparsers.add_parser("task-types", help="List registered task types.")
    task_types.add_argument("--format", choices=("text", "json"), default="text")
    return parser.parse_args()


def context_from_args(args: argparse.Namespace) -> AgentExecutionContext:
    return AgentExecutionContext(
        input_source=args.input_source,
        task_types=tuple(args.task_type or []),
        task_size=args.task_size,
        changed_paths=tuple(normalize_cli_paths(args.changed_path)),
        final=bool(args.final),
        event=args.event,
    )


def normalize_cli_paths(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        for part in value.split(","):
            stripped = normalized(part.strip())
            if stripped and stripped not in result:
                result.append(stripped)
    return result


def render_components(registry: ComponentRegistry, context: AgentExecutionContext, kind: str | None, fmt: str) -> str:
    components = registry.matching_components(context, kind=kind)
    if fmt == "json":
        return json.dumps(
            {
                "context": asdict(context),
                "components": [asdict(component) for component in components],
                "mechanisms": registry.mechanism_labels_for_context(context),
                "gates": registry.gate_labels_for_context(context),
                "gate_steps": registry.gate_step_ids_for_context(context),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    lines = [
        "AI Rules Governance Components",
        f"Input source: {context.input_source}",
        f"Task types: {', '.join(context.task_types) if context.task_types else 'none'}",
        f"Task size: {context.task_size}",
        f"Changed paths: {', '.join(context.changed_paths) if context.changed_paths else 'none'}",
        f"Final: {str(context.final).lower()}",
        f"Event: {context.event or 'any'}",
        f"Components: {len(components)}",
    ]
    for item in components:
        labels = []
        if item.mechanism_label:
            labels.append(f"mechanism={item.mechanism_label}")
        if item.gate_label:
            labels.append(f"gate={item.gate_label}")
        if item.gate_step:
            labels.append(f"step={item.gate_step}")
        if item.events:
            labels.append(f"events={','.join(item.events)}")
        if item.effect != "readonly":
            labels.append(f"effect={item.effect}")
        if item.dedupe_key:
            labels.append(f"dedupe={item.dedupe_key}")
        suffix = f" ({'; '.join(labels)})" if labels else ""
        lines.append(f"- {item.id} [{item.kind} {item.phase} order={item.order} {item.fail_policy}]{suffix}")
    return "\n".join(lines)


def render_task_types(registry: ComponentRegistry, fmt: str) -> str:
    items = list(registry.task_types.values())
    if fmt == "json":
        return json.dumps([asdict(item) for item in items], ensure_ascii=False, indent=2, sort_keys=True)
    lines = ["AI Rules Runtime Task Types", f"Task types: {len(items)}"]
    for item in items:
        flags = []
        if item.mutating:
            flags.append("mutating")
        if item.requires_tracking:
            flags.append("tracking")
        if item.requires_approval:
            flags.append("approval")
        lines.append(f"- {item.id}: {item.description} ({', '.join(flags) if flags else 'read-only'})")
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    registry = default_registry()
    if args.command == "components":
        print(render_components(registry, context_from_args(args), args.kind, args.format))
        return 0
    if args.command == "task-types":
        print(render_task_types(registry, args.format))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
