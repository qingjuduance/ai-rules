#!/usr/bin/env python3
"""Spring-style component registry for the ai-client-governance client governance plugin."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ai_client_governance.common import cli_arguments as common_cli_args


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
            "runtime": "ai-client-governance-plugin",
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
        description="AI Client Governance, scripts, skills, lifecycle, gates, or adapters.",
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
            "ai-client-governance",
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
            "input.filter.user-message-preflight",
            "input-filter",
            "input",
            90,
            "Mandatory user-message join point that records input-filter facts before processing.",
            fail_policy="fail_closed",
            events=("user-message",),
            requires_facts=("raw_input",),
            produces_facts=("requirements", "triggers", "input_filter_report", "input_filter_preflight_event"),
            effect="state_write",
            dedupe_key="task_id:user-message",
            gate_label="ai_client_governance.py lifecycle input-filter + task-record gate --event preflight",
            gate_step="input-filter-preflight",
            condition="Run before planning, write-intent, resume, or final-output for every non-trivial user message.",
            performance_budget="single message parse plus SQLite gate; no repository scan",
            metadata={
                "join_point": "user-message",
                "advice": "around",
                "filter_chain": (
                    "classify-source",
                    "user-claim-validation",
                    "client-identity",
                    "agent-decision",
                    "data-confirmation",
                    "shell-proxy-usage",
                    "history-requirement-recovery",
                    "readonly-side-effect-policy",
                    "decompose-requirements",
                    "recordability-judgement",
                    "network-search-judgement",
                    "acceptance-extract",
                ),
                "required_event": "input-filter.preflight",
            },
        ),
        component(
            "client.runtime.host-capability-gateway",
            "processing-interceptor",
            "preflight",
            95,
            "Bridge host-client write/shell capabilities to lifecycle, task-record, and local command-proxy facts.",
            fail_policy="fail_closed",
            events=("user-message", "write-intent", "after-change", "final-output", "resume"),
            task_types=(
                "code-debug",
                "correction",
                "rules-script",
                "docs",
                "git",
                "frontend",
                "resume",
                "multi-agent",
            ),
            requires_facts=("task_id", "input_filter_preflight_event", "capability_gateway_facts"),
            produces_facts=(
                "lifecycle_input_filter_enforced",
                "prewrite_runtime_adapter_decision",
                "non_invasive_shell_command_proxy",
            ),
            effect="state_write",
            dedupe_key="task_id:event:host-capability-gateway",
            gate_label="ai_client_governance.py task-record gate requires event_type=capability-gateway.facts",
            gate_step="capability-gateway",
            condition=(
                "Run at host-client capability boundaries before repository writes or important shell commands. "
                "The default raw-shell strategy is local command-proxy activation, like a Python virtual environment; "
                "it must not modify user profiles, PATH, terminal settings, or global shell behavior."
            ),
            performance_budget="single SQLite fact check plus optional telemetry read; no profile writes",
            metadata={
                "required_event": "capability-gateway.facts",
                "lifecycle_boundary": "lifecycle input-filter + task-record gate --event preflight",
                "shell_boundary": "shell-adapter proxy-powershell no-profile command proxy",
                "non_invasive_policy": {
                    "profile_policy": "no_profile",
                    "profile_touched": False,
                    "user_shell_impact": "none",
                    "global_path_modified": False,
                },
                "profile_shim_policy": "explicit opt-in only; not a default enforcement path",
            },
        ),
        component(
            "input.filter.client-identity",
            "input-filter",
            "input",
            106,
            "Record the current AI client and model identity for audit correlation.",
            fail_policy="fail_closed",
            events=("user-message", "resume"),
            requires_facts=("task_id",),
            produces_facts=("client_type", "model_id", "client_identity_event"),
            mechanism_label="ai_client_governance.py lifecycle input-filter client-identity",
            gate_label="ai_client_governance.py task-record gate requires event_type=client-identity.analysis",
            gate_step="client-identity",
            effect="state_write",
            dedupe_key="task_id:user-message:client-identity",
            condition=(
                "Run before write-intent, final-output, or resume gates so audits can identify which "
                "client/model combinations skipped the standard flow."
            ),
            performance_budget="constant-time CLI/env/default identity capture; no repository scan",
            metadata={
                "join_point": "user-message",
                "aop_role": "around-advice",
                "required_event": "client-identity.analysis",
                "required_fields": ("client_type", "model_id"),
                "unknown_policy": "record explicit unknown values instead of omitting identity",
                "known_client_examples": (
                    "codex",
                    "claude-code",
                    "cursor",
                    "cline",
                    "windsurf",
                    "continue",
                    "trae",
                    "doubao",
                    "roo",
                    "aider",
                ),
                "shell_coverage_pair": "raw shell coverage is audited separately by shell-adapter diagnostics",
            },
        ),
        component(
            "input.filter.classify-source",
            "input-filter",
            "input",
            100,
            "Classify input source and trust boundary.",
            fail_policy="fail_closed",
            events=("user-message", "resume"),
            requires_facts=("raw_input",),
            produces_facts=("input_source", "trust_boundary"),
            condition="Run for every new user, web, file, tool, agent, or history input.",
        ),
        component(
            "input.filter.user-claim-validation",
            "input-filter",
            "input",
            105,
            "Classify user assertions separately from user goals before they steer execution.",
            fail_policy="fail_closed",
            events=("user-message", "resume"),
            requires_facts=("raw_input", "input_source"),
            produces_facts=("user_claims", "claim_trust_levels", "claim_verification_actions"),
            mechanism_label="ai_client_governance.py lifecycle input-filter user-claim-validation",
            gate_label="ai_client_governance.py task-record gate requires event_type=user-claim-validation.analysis",
            gate_step="user-claim-validation",
            effect="state_write",
            dedupe_key="task_id:user-message:user-claims",
            condition=(
                "Run before planning or write-intent when a user message contains facts, diagnoses, approvals, "
                "or execution claims that may be wrong, stale, or conflict with live state."
            ),
            performance_budget="local message segmentation and risk flags only; verification commands are planned separately",
            metadata={
                "join_point": "user-message",
                "aop_role": "around-advice",
                "required_event": "user-claim-validation.analysis",
            },
        ),
        component(
            "input.filter.agent-decision",
            "input-filter",
            "input",
            107,
            "Record whether to spawn, reuse, defer, or skip sub-agents before larger work proceeds.",
            fail_policy="fail_closed",
            events=("user-message", "resume"),
            requires_facts=("task_id", "task_type", "requirement_rows"),
            produces_facts=("agent_group_decision", "spawn_count", "no_spawn_reason", "context_pack_ref"),
            mechanism_label="ai_client_governance.py lifecycle input-filter agent-decision",
            gate_label="ai_client_governance.py task-record gate requires event_type=agent-decision.analysis",
            gate_step="agent-decision",
            effect="state_write",
            dedupe_key="task_id:user-message:agent-decision",
            condition=(
                "Run for medium/large, mutating, rules-script, correction, git/worktree, long-running, "
                "or user-requested multi-agent work. Not spawning agents still requires a reason and alternate validation."
            ),
            performance_budget="constant-size decision record; any history or code scan is a separate confirmation step",
            metadata={
                "required_event": "agent-decision.analysis",
                "required_fields": (
                    "agent_group_decision",
                    "spawn_count",
                    "no_spawn_reason",
                    "context_pack_ref",
                    "data_confirmation_evidence",
                ),
            },
        ),
        component(
            "input.filter.data-confirmation",
            "input-filter",
            "input",
            108,
            "Record confirmed data sources and unchecked boundaries before claims or history steer execution.",
            fail_policy="fail_closed",
            events=("user-message", "resume"),
            requires_facts=("task_id", "user_claims", "scope_kind"),
            produces_facts=("confirmation_sources", "checked_facts", "unverified_items"),
            mechanism_label="ai_client_governance.py lifecycle input-filter data-confirmation",
            gate_label="ai_client_governance.py task-record gate requires event_type=data-confirmation.analysis",
            gate_step="data-confirmation",
            effect="state_write",
            dedupe_key="task_id:user-message:data-confirmation",
            condition="Run before planning or write-intent for every medium/large or mutating task.",
            performance_budget="records confirmation facts only; expensive verification belongs to validation commands",
            metadata={
                "required_event": "data-confirmation.analysis",
                "required_fields": ("confirmation_sources", "checked_facts", "unverified_items"),
            },
        ),
        component(
            "input.filter.history-requirement-recovery",
            "input-filter",
            "input",
            109,
            "Recover repeated user process requirements from available history before correction or framework work.",
            fail_policy="fail_closed",
            events=("user-message", "resume"),
            requires_facts=("task_id", "raw_input", "history_sources"),
            produces_facts=("recovered_requirements", "history_record_refs"),
            mechanism_label="ai_client_governance.py lifecycle input-filter history-requirement-recovery",
            gate_label="ai_client_governance.py task-record gate requires event_type=history-requirement-recovery.analysis",
            gate_step="history-requirement-recovery",
            effect="state_write",
            dedupe_key="task_id:user-message:history-requirement-recovery",
            condition=(
                "Run for correction, rules-script, multi-agent, long-running, or messages such as "
                "'previously said', 'keeps forgetting', or 'find them all'."
            ),
            performance_budget="bounded scan of declared history sources; long files should use context-extract",
            metadata={
                "required_event": "history-requirement-recovery.analysis",
                "required_fields": ("history_sources", "recovered_requirements"),
            },
        ),
        component(
            "preflight.interceptor.shell-proxy-usage",
            "processing-interceptor",
            "preflight",
            209,
            "Record the PowerShell proxy plan or gated exception before important local commands run.",
            task_sizes=("medium", "large"),
            events=("user-message", "plan-output", "write-intent", "resume", "final-output"),
            requires_facts=("task_id", "command_candidates", "execution_environment"),
            produces_facts=("shell_proxy_usage", "raw_shell_gap_exception"),
            mechanism_label="ai_client_governance.py lifecycle input-filter shell-proxy-usage",
            gate_label="ai_client_governance.py task-record gate requires event_type=shell-proxy-usage.analysis",
            gate_step="shell-proxy-usage",
            fail_policy="fail_closed",
            effect="state_write",
            dedupe_key="task_id:event:shell-proxy-usage",
            condition=(
                "Run before important Windows PowerShell commands. Complex command strings should use "
                "shell-adapter proxy-powershell --powershell-command-file. The proxy is local/no-profile "
                "and must not alter user shell profiles, PATH, terminal settings, or global behavior."
            ),
            performance_budget="constant-size policy record; diagnostics are a separate readonly gate",
            metadata={
                "required_event": "shell-proxy-usage.analysis",
                "required_fields": ("policy", "planned_runner"),
                "final_required": (
                    "used_proxy=true with enforcement_mode=non-invasive-command-proxy, "
                    "profile_policy=no_profile, profile_touched=false, user_shell_impact=none, or exception_reason"
                ),
            },
        ),
        component(
            "preflight.interceptor.readonly-side-effect-policy",
            "processing-interceptor",
            "preflight",
            209,
            "Record whether a task is truly readonly and whether DB/status writes are allowed.",
            task_sizes=("medium", "large"),
            events=("user-message", "plan-output", "write-intent", "resume", "final-output"),
            requires_facts=("task_id", "command_candidates", "side_effects"),
            produces_facts=("readonly_contract", "db_write_allowed", "record_state_allowed"),
            mechanism_label="ai_client_governance.py lifecycle input-filter readonly-side-effect-policy",
            gate_label="ai_client_governance.py task-record gate requires event_type=readonly-side-effect-policy.analysis",
            gate_step="readonly-side-effect-policy",
            fail_policy="fail_closed",
            effect="state_write",
            dedupe_key="task_id:event:readonly-side-effect-policy",
            condition=(
                "Run before readonly audits or status commands. Commands such as --record-state or sync-check "
                "state writes are forbidden when readonly_contract=true."
            ),
            performance_budget="constant-size policy record; file/DB hashing is a separate validation command",
            metadata={
                "required_event": "readonly-side-effect-policy.analysis",
                "required_fields": (
                    "readonly_contract",
                    "db_write_allowed",
                    "record_state_allowed",
                    "side_effect_class",
                    "dry_run_supported",
                ),
            },
        ),
        component(
            "input.filter.decompose-requirements",
            "input-filter",
            "input",
            110,
            "Split user input into stable REQ rows.",
            fail_policy="fail_closed",
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
            fail_policy="fail_closed",
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
            fail_policy="fail_closed",
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
            fail_policy="fail_closed",
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
            fail_policy="fail_closed",
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
            "preflight.interceptor.structured-contract",
            "processing-interceptor",
            "preflight",
            205,
            "Describe typed task-record fields before execution.",
            task_sizes=("medium", "large"),
            events=("user-message", "plan-output", "write-intent", "resume"),
            requires_facts=("task_type", "event"),
            produces_facts=("structured_task_contract",),
            mechanism_label="ai_client_governance.py contract describe",
            effect="readonly",
            condition="Run before gated work so the AI sees required fields and enums before writing.",
            performance_budget="local JSON/text render; no repo scan",
        ),
        component(
            "preflight.gate.analysis-contract",
            "cross-cutting-gate",
            "preflight",
            205,
            "Require a clear task analysis contract before write-intent.",
            task_sizes=("medium", "large"),
            events=("plan-output", "write-intent", "resume", "final-output"),
            requires_facts=("task_id", "changed_paths", "acceptance_criteria"),
            produces_facts=("analysis_contract", "validation_budget"),
            mechanism_label="ai_client_governance.py completion-test --require-analysis",
            gate_label="ai_client_governance.py completion-test --require-analysis",
            gate_step="analysis-contract",
            fail_policy="fail_closed",
            effect="readonly",
            dedupe_key="task_id:event:changed_paths:analysis-contract",
            condition=(
                "Run before editing or expensive validation; if scope, non-goals, risks, or acceptance are unclear, "
                "stop before write-intent instead of compensating with broad tests at closeout."
            ),
            performance_budget="local path classification and constant-size analysis fields; no repository scan",
            metadata={
                "required_fields": ("analysis-summary", "analysis-scope", "non-goal", "risk", "acceptance"),
                "budget_policy": "required validation checks must fit the declared budget unless explicitly upgraded",
            },
        ),
        component(
            "preflight.interceptor.plan-approval-boundary",
            "processing-interceptor",
            "preflight",
            206,
            "Require explicit plan, approval, commit, and push boundaries before execution.",
            task_sizes=("medium", "large"),
            events=("user-message", "plan-output", "write-intent", "resume", "final-output"),
            requires_facts=("task_id", "approval_label", "task_type"),
            produces_facts=("plan_approval_boundary", "push_policy"),
            mechanism_label="ai_client_governance.py lifecycle input-filter plan-approval-boundary",
            gate_label="ai_client_governance.py task-record gate requires event_type=plan-approval-boundary.analysis",
            gate_step="plan-approval-boundary",
            fail_policy="fail_closed",
            effect="state_write",
            dedupe_key="task_id:event:approval_label:push_policy",
            condition=(
                "Run before diagnostics, repository writes, stage/commit, or closeout; commit approval never implies push approval."
            ),
            performance_budget="constant-time task-record payload check; no repository scan",
            metadata={
                "join_point": "plan-output",
                "aop_role": "around-advice",
                "required_event": "plan-approval-boundary.analysis",
                "push_policy": "push_requires_separate_approval",
            },
        ),
        component(
            "preflight.interceptor.command-compression",
            "processing-interceptor",
            "preflight",
            208,
            "Analyze whether newly generated local commands can be deduped, batched, or replaced by a local runner.",
            task_sizes=("medium", "large"),
            events=("user-message", "plan-output", "write-intent", "resume", "final-output"),
            requires_facts=("task_id", "task_type", "command_candidates"),
            produces_facts=("command_compression_analysis", "command_compression_event"),
            mechanism_label="ai_client_governance.py task-run plan",
            gate_label="ai_client_governance.py task-record gate requires event_type=command-compression.analysis",
            gate_step="command-compression",
            fail_policy="fail_closed",
            effect="readonly",
            dedupe_key="task_id:event:changed_paths:command_candidates",
            condition=(
                "Run before write-intent or final-output for every medium/large or mutating task; "
                "prefer one local task-run/gate-pool/telemetry pass over repeated model-mediated command selection."
            ),
            performance_budget="local command list normalization and grouping only; no repository scan unless explicit commands do it",
            metadata={
                "join_point": "write-intent",
                "required_event": "command-compression.analysis",
                "aop_role": "around-advice",
                "telemetry_policy": "write execution spans to aicg.db through task-run, gate-pool, shell-adapter, telemetry record, or the command adapter until host shell interception exists",
            },
        ),
        component(
            "preflight.interceptor.command-compression.mutating",
            "processing-interceptor",
            "preflight",
            208,
            "Analyze command compression for mutating tasks even when they are classified as small.",
            task_types=(
                "code-debug",
                "correction",
                "rules-script",
                "docs",
                "git",
                "frontend",
                "resume",
                "multi-agent",
            ),
            events=("user-message", "plan-output", "write-intent", "resume", "final-output"),
            requires_facts=("task_id", "task_type", "command_candidates"),
            produces_facts=("command_compression_analysis", "command_compression_event"),
            mechanism_label="ai_client_governance.py task-run plan",
            gate_label="ai_client_governance.py task-record gate requires event_type=command-compression.analysis",
            gate_step="command-compression",
            fail_policy="fail_closed",
            effect="readonly",
            dedupe_key="task_id:event:changed_paths:command_candidates",
            condition=(
                "Run before write-intent or final-output for every mutating task, including small edits; "
                "the medium/large node covers large non-mutating planning while this node closes the mutation gap."
            ),
            performance_budget="local command list normalization and grouping only; no repository scan unless explicit commands do it",
            metadata={
                "join_point": "write-intent",
                "required_event": "command-compression.analysis",
                "aop_role": "around-advice",
                "coverage": "mutating-task-fast-path",
                "telemetry_policy": "write execution spans to aicg.db through task-run, gate-pool, shell-adapter, telemetry record, or the command adapter until host shell interception exists",
            },
        ),
        component(
            "preflight.interceptor.scope-classification",
            "processing-interceptor",
            "preflight",
            208,
            "Classify each task as common governance, project specialization, native project, or mixed before write-intent.",
            task_sizes=("medium", "large"),
            events=("user-message", "plan-output", "write-intent", "resume", "final-output"),
            requires_facts=("changed_paths", "command_candidates"),
            produces_facts=("scope_kind", "scope_reason", "scope_paths"),
            mechanism_label="ai_client_governance.py lifecycle input-filter scope-classification",
            gate_label="ai_client_governance.py task-record gate checks scope-classification trigger",
            gate_step="scope-classification",
            fail_policy="fail_closed",
            effect="readonly",
            dedupe_key="task_id:event:changed_paths:command_candidates:scope",
            condition=(
                "Run whenever a task may touch rules, scripts, skills, records, or project files; "
                "common facts stay in ai-client-governance and project-specific facts stay in .ai-client/project or native project assets."
            ),
            performance_budget="local path and command-token classification only",
            metadata={
                "join_point": "user-message",
                "aop_role": "before-advice",
                "scope_kinds": (
                    "ai-client-governance-common",
                    "project-specialization",
                    "native-project-assets",
                    "mixed",
                    "unknown",
                ),
            },
        ),
        component(
            "preflight.interceptor.state-artifact-ownership",
            "processing-interceptor",
            "preflight",
            209,
            "Require script-generated state and artifacts to declare owner and cleanup commands.",
            task_types=("rules-script",),
            events=("write-intent", "after-change", "resume", "final-output"),
            requires_facts=("changed_paths", "script_generated_state"),
            produces_facts=("state_artifact_ownership", "cleanup_policy"),
            mechanism_label="ai_client_governance.py lifecycle input-filter state-artifact-ownership",
            gate_label="ai_client_governance.py task-record gate requires event_type=state-artifact-ownership.analysis",
            gate_step="state-artifact-ownership",
            fail_policy="fail_closed",
            effect="state_write",
            dedupe_key="task_id:event:state-artifact-ownership",
            condition=(
                "Run when scripts generate coord sessions, locks, lifecycle state, doc-index, pycache, or selftest artifacts; "
                "generated telemetry must be repaired by scripts, not hand-edited."
            ),
            performance_budget="constant-time manifest check plus optional owner-command validation",
            metadata={
                "join_point": "write-intent",
                "aop_role": "around-advice",
                "manual_edit_policy": "forbidden_without_break_glass",
            },
        ),
        component(
            "preflight.interceptor.patch-preflight",
            "processing-interceptor",
            "preflight",
            210,
            "Require stable anchors and small-step patches before editing long or hot files.",
            task_types=("rules-script", "docs", "correction"),
            events=("write-intent", "after-change", "resume"),
            requires_facts=("changed_paths", "patch_targets"),
            produces_facts=("patch_anchor_policy", "patch_apply_policy"),
            mechanism_label="context-extract + anchor uniqueness check",
            gate_label="ai_client_governance.py task-record gate requires event_type=patch-preflight.analysis",
            gate_step="patch-preflight",
            fail_policy="fail_closed",
            effect="readonly",
            dedupe_key="task_id:event:patch-targets",
            condition=(
                "Run before applying patches to long files, frequently edited governance files, or files touched by multiple worktrees."
            ),
            performance_budget="local rg/context-extract only; no broad repository scan",
            metadata={
                "join_point": "write-intent",
                "anchor_policy": "verify_unique_or_reextract",
                "apply_policy": "small_step_patch",
            },
        ),
        component(
            "preflight.interceptor.task-run-dag",
            "processing-interceptor",
            "preflight",
            209,
            "Execute compressed command groups through the local task-run DAG runner when commands are ready to run.",
            task_sizes=("medium", "large"),
            events=("write-intent", "after-change", "resume", "final-output"),
            requires_facts=("task_id", "command_compression_analysis", "command_candidates"),
            produces_facts=("task_run_report", "execution_telemetry", "cache_decision"),
            mechanism_label="ai_client_governance.py task-run run",
            gate_label="ai_client_governance.py task-run diagnose",
            gate_step="task-run-diagnostics",
            fail_policy="fail_closed",
            effect="state_write",
            dedupe_key="task_id:event:changed_paths:command_candidates:input_hashes",
            condition=(
                "Run after command-compression analysis when local commands are deterministic enough to execute; "
                "readonly and validation groups may parallelize/cache, stateful groups remain ordered and no-cache."
            ),
            performance_budget="single local DAG pass; cache only readonly/validation nodes with declared inputs; telemetry writes to aicg.db are mandatory by default",
            dependencies=("preflight.interceptor.command-compression",),
            metadata={
                "join_point": "write-intent",
                "aop_role": "around-advice",
                "runner": "task-run run",
                "cache_key_inputs": (
                    "runner_version",
                    "command",
                    "cwd",
                    "kind",
                    "task_types",
                    "changed_paths",
                    "declared_input_hashes",
                    "git_head",
                ),
                "uncacheable": "stateful/sequential commands and live worktree probes such as git status/git diff",
                "observability": "trace_json plus execution telemetry spans in aicg.db",
            },
        ),
        component(
            "preflight.gate.structured-task-record",
            "cross-cutting-gate",
            "preflight",
            212,
            "Require typed SQLite task records for gated work.",
            task_sizes=("medium", "large"),
            events=("user-message", "write-intent", "final-output", "resume"),
            requires_facts=("task_id", "structured_task_record"),
            produces_facts=("structured_task_record_gate_result",),
            gate_label="ai_client_governance.py task-record gate",
            gate_step="task-record",
            fail_policy="fail_closed",
            condition="Run when a structured task id exists; fail before processing or final output if input-filter facts or required rows are missing.",
            performance_budget="single SQLite read; no Markdown parsing",
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
            gate_label="ai_client_governance.py task-gate:user-input-and-requirements",
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
            gate_label="ai_client_governance.py task-gate",
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
            gate_label="ai_client_governance.py session-gate",
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
            "preflight.gate.worktree-creation-policy",
            "cross-cutting-gate",
            "preflight",
            245,
            "Require an explicit task-worktree creation method and sparse-checkout strategy before task writes.",
            events=("plan-output", "write-intent", "resume"),
            task_types=("code-debug", "correction", "rules-script", "docs", "git", "frontend", "resume", "multi-agent"),
            gate_label="ai_client_governance.py task-gate --only-worktree-creation-policy",
            gate_step="worktree-creation-policy",
            fail_policy="fail_closed",
            requires_facts=("task_tracking", "worktree_creation_method", "sparse_checkout_strategy"),
            produces_facts=("worktree_creation_policy_decision",),
            condition=(
                "Run before creating or reusing a task worktree for mutating work. "
                "Use worktree-task create by default; raw git worktree add is a break-glass path "
                "that must record the reason and source snapshot handling."
            ),
            performance_budget="One task tracking file read; no Git scan.",
            dedupe_key="worktree-creation-policy:task-tracking",
        ),
        component(
            "prewrite.gate.worktree-live-state",
            "cross-cutting-gate",
            "coordination",
            320,
            "Reconcile coord/session/queue records against Git live worktree state before writes, resume, merge cleanup, or final output.",
            events=("write-intent", "resume", "merge-cleanup", "final-output"),
            task_types=("code-debug", "correction", "rules-script", "docs", "git", "frontend", "resume", "multi-agent"),
            gate_label="ai_client_governance.py worktree-task reconcile",
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
            "prewrite.gate.approved-task-worktree",
            "cross-cutting-gate",
            "coordination",
            331,
            "Fail closed before mutating writes unless task-record proves explicit approval and task worktree evidence.",
            events=("write-intent",),
            task_types=("code-debug", "correction", "rules-script", "docs", "git", "frontend", "resume", "multi-agent"),
            gate_label="ai_client_governance.py task-record gate --event preflight",
            gate_step="task-record",
            fail_policy="fail_closed",
            requires_facts=("task_id", "approval_label", "approved_approval_row", "task_worktree"),
            produces_facts=("prewrite_runtime_adapter_decision",),
            effect="readonly",
            condition=(
                "Run at the host-client write boundary. Entry documents and model instructions are insufficient; "
                "Trae/Doubao/Codex-style writes must be backed by an approved active task and a task worktree row."
            ),
            dedupe_key="task_id:write-intent:approved-task-worktree",
            performance_budget="single SQLite task-record read; no repository scan",
            metadata={
                "adapter_role": "prewrite-approval-worktree",
                "blocked_gap": "client can otherwise write files or run shell commands after reading prose rules only",
            },
        ),
        component(
            "coordination.gate.host-submodule-closeout",
            "cross-cutting-gate",
            "coordination",
            340,
            "Verify host repository gitlink, worktree state, and task tracking after embedded ai-client-governance merges.",
            events=("merge-cleanup", "final-output"),
            task_types=("git", "rules-script"),
            gate_label="ai_client_governance.py worktree-task host-closeout",
            gate_step="host-submodule-closeout",
            fail_policy="fail_closed",
            requires_facts=("git_status", "worktree_reconcile_report", "task_tracking"),
            produces_facts=("host_submodule_closeout_report",),
            condition=(
                "Run when an ai-client-governance worktree merge or embedded submodule closeout is in scope; "
                "the node checks the host gitlink plus task state/tracking, not only the child repository."
            ),
            performance_budget="One host git status, one gitlink lookup, one state JSON read, and targeted task tracking reads.",
            dedupe_key="host-submodule-closeout:project-root:ai-client-governance",
        ),
        component(
            "coordination.interceptor.agent-context-reuse",
            "processing-interceptor",
            "coordination",
            295,
            "Decide whether to reuse an existing agent context, spawn a new agent, merge work, or close completed agents.",
            task_types=("multi-agent",),
            produces_facts=(
                "agent_context_reuse_decision",
                "reuse_key",
                "context_capsule",
                "context_ttl",
                "token_budget",
                "token_proxy_metrics",
            ),
            requires_facts=("task_tree", "write_scope", "agent_status", "context_budget", "contamination_boundary"),
            condition=(
                "Run before spawning delegated agents and before sending follow-up work to an existing agent; "
                "reuse only when task id, scope, retained facts, heartbeat freshness, and contamination checks pass."
            ),
            performance_budget="local status/brief/capsule inspection only; do not read full chat history by default",
            fail_policy="fail_closed",
            metadata={
                "allowed_decisions": ("reuse", "spawn", "merge", "close"),
                "agent_count_policy": "dynamic-by-task-tree-scope-locks-and-context-budget-not-fixed-small-cap",
                "required_brief_fields": (
                    "task_id",
                    "worktree_path",
                    "write_scope",
                    "forbidden_paths",
                    "validation_commands",
                    "return_capsule",
                    "context_reuse",
                    "reuse_key",
                    "retained_facts",
                    "skip_inputs",
                    "context_capsule",
                    "context_ttl",
                    "contamination_boundary",
                    "minimal_resume_inputs",
                    "token_budget",
                    "token_proxy_metrics",
                    "token_usage_source",
                ),
            },
        ),
        component(
            "coordination.interceptor.agent-brief",
            "processing-interceptor",
            "coordination",
            300,
            "Require Agent Brief before spawning delegated agents.",
            task_types=("multi-agent",),
            fail_policy="fail_closed",
            requires_facts=("task_id", "worktree_path", "write_scope", "forbidden_paths", "validation_commands", "return_capsule"),
            produces_facts=("agent_brief", "agent_dispatch_contract"),
            metadata={
                "required_fields": (
                    "task_id",
                    "worktree_path",
                    "write_scope",
                    "forbidden_paths",
                    "validation_commands",
                    "return_capsule",
                    "context_reuse",
                ),
                "dispatch_adapter": "worktree-task dispatch records agent_dispatch_brief when task_type includes multi-agent",
            },
        ),
        component(
            "coordination.interceptor.todo-task-record-adapter",
            "processing-interceptor",
            "coordination",
            315,
            "Expose client Todo lists as a derived view from durable task-queue/task-record state.",
            events=("status-output", "resume", "final-output"),
            produces_facts=("todo_projection",),
            mechanism_label="ai_client_governance.py task-queue todo",
            gate_step="task-record-queue-alignment",
            fail_policy="report_only",
            effect="readonly",
            condition=(
                "Use when updating Codex/Trae/client UI Todo items; Todo state must be regenerated from "
                "task-queue/task-record rather than treated as an independent fact source."
            ),
            performance_budget="single SQLite task-queue read; no repository scan",
            metadata={
                "source_policy": "derived_from_task_queue_not_fact_source",
                "durable_sources": ("task-queue", "task-record"),
            },
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
            "Run per-session embedded ai-client-governance sync state checks.",
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
            gate_label="ai_client_governance.py doc-index",
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
            gate_label="ai_client_governance.py doc-index",
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
            gate_label="ai_client_governance.py validate-doc",
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
            gate_label="ai_client_governance.py validate-doc",
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
            gate_label="ai_client_governance.py doc-index",
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
            gate_label="ai_client_governance.py doc-index",
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
            "validation.gate.security-policy",
            "cross-cutting-gate",
            "validation",
            615,
            "Assess changed text files through the unified local security policy gate.",
            events=("after-change", "completion-test", "final-output"),
            task_types=("code-debug", "correction", "rules-script", "docs", "git", "frontend", "resume", "multi-agent"),
            path_suffixes=TEXT_SUFFIXES,
            requires_changed_paths=True,
            gate_label="ai_client_governance.py policy assess",
            gate_step="security-policy",
            fail_policy="fail_closed",
            requires_facts=("changed_paths", "policy_assessment"),
            produces_facts=("security_policy_decision", "redaction_boundary", "command_policy_findings"),
            effect="readonly",
            condition=(
                "Run after content changes and before final output for prompt injection, sensitive text, "
                "command injection, supply-chain, and path-boundary risk. The plugin cannot intercept "
                "host-native shell internally, so the enforceable surface is governed file/command paths "
                "plus fail-closed diagnostics."
            ),
            dedupe_key="security-policy:changed-text-paths",
            performance_budget="Read changed text files once with deterministic regex policy; no network or model call.",
            metadata={
                "policy_backend": "local-rule-based-now; future OPA/declarative backend can replace this gate",
                "standards": ("OWASP LLM Top 10", "NIST AI RMF / GenAI Profile", "policy-as-code"),
            },
        ),
        component(
            "validation.gate.validate-doc",
            "cross-cutting-gate",
            "validation",
            620,
            "Validate changed Markdown document task evidence.",
            path_suffixes=(".md",),
            gate_label="ai_client_governance.py validate-doc",
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
            gate_label="ai_client_governance.py scan-corrections",
            gate_step="scan-corrections",
            fail_policy="fail_closed",
        ),
        component(
            "validation.gate.scan-corrections.path",
            "cross-cutting-gate",
            "validation",
            631,
            "Scan correction records when changed paths touch corrections.",
            path_prefixes=(".ai-client/project/records/corrections/", ".ai-client/corrections/"),
            gate_label="ai_client_governance.py scan-corrections",
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
            gate_label="ai_client_governance.py completion-test",
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
            "output.gate.discovered-issue-recording",
            "cross-cutting-gate",
            "output",
            735,
            "Fail final output unless newly discovered issues are recorded or explicitly marked no-action.",
            events=("final-output",),
            task_types=("correction", "rules-script", "docs", "git", "resume", "multi-agent", "long-running"),
            gate_label="ai_client_governance.py task-record gate --event final",
            gate_step="task-record",
            fail_policy="fail_closed",
            requires_facts=("task_id", "discovered_issue_recording_event"),
            produces_facts=("final_output_discovered_issue_decision",),
            effect="readonly",
            condition=(
                "Run before the final reply when analysis found problems, risks, follow-up tasks, or no-action decisions; "
                "facts must land in task-record, task-queue, framework-debt, correction, pending, or an explicit no-action row."
            ),
            dedupe_key="task_id:final-output:discovered-issue-recording",
            performance_budget="single SQLite task-record read; no repository scan",
            metadata={
                "adapter_role": "final-output-discovered-issue-recording",
                "allowed_destinations": (
                    "task-record",
                    "task-queue",
                    "framework-debt",
                    "correction",
                    "pending",
                    "no-action",
                ),
            },
        ),
        component(
            "final.gate.architecture-guard",
            "cross-cutting-gate",
            "final-gate",
            800,
            "Validate ai-client-governance architecture boundaries before closeout.",
            final_only=True,
            gate_label="ai_client_governance.py architecture-guard",
            gate_step="architecture-guard",
            fail_policy="fail_closed",
        ),
        component(
            "final.gate.file-ownership",
            "cross-cutting-gate",
            "final-gate",
            805,
            "Validate host-project .ai-client file ownership, .gitignore runtime block, and tracked live-state.",
            final_only=True,
            gate_label="ai_client_governance.py file-ownership audit",
            gate_step="file-ownership",
            fail_policy="fail_closed",
            requires_facts=("git_index", "gitignore_policy"),
            produces_facts=("file_ownership_audit",),
            condition="Run before closeout so generated DB/log/worktree artifacts cannot be committed by the host project.",
            dedupe_key="host-project:file-ownership",
            performance_budget="One git ls-files, one git status --ignored, and one .gitignore read.",
        ),
        component(
            "final.gate.task-gate",
            "cross-cutting-gate",
            "final-gate",
            810,
            "Validate task-type, user requirement, and output closeout evidence.",
            final_only=True,
            gate_label="ai_client_governance.py task-gate",
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
            gate_label="ai_client_governance.py session-gate",
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
            gate_label="ai_client_governance.py task-queue",
            gate_step="task-queue",
            fail_policy="fail_closed",
        ),
        component(
            "reporter.telemetry",
            "reporter",
            "report",
            895,
            "Report unified execution telemetry from aicg.db.",
            events=("status-output", "resume", "final-output"),
            final_only=True,
            mechanism_label="ai_client_governance.py telemetry report",
            gate_step="telemetry",
            fail_policy="report_only",
            produces_facts=("execution_telemetry_report",),
            effect="readonly",
            condition="Run when execution volume, duration, cache, duplicate, failure, or trace statistics are needed.",
            performance_budget="read indexed SQLite execution_spans/execution_events only; no command execution",
            metadata={
                "reports": (
                    "top operations",
                    "top subjects",
                    "duplicate subjects",
                    "span kind counts",
                    "subject type counts",
                    "failure rate",
                    "duration p50/p95/max",
                    "cache hit/miss counts",
                    "scope kind counts",
                    "adapter enforcement counts",
                ),
                "fact_source": ".ai-client/project/state/aicg.db",
            },
        ),
        component(
            "reporter.shell-adapter-diagnostics",
            "reporter",
            "report",
            900,
            "Report whether the host shell path is adapter-backed or still a raw-shell gap.",
            events=("status-output", "resume", "final-output"),
            final_only=True,
            mechanism_label="ai_client_governance.py shell-adapter diagnose",
            gate_step="shell-adapter-diagnostics",
            fail_policy="report_only",
            produces_facts=("shell_adapter_diagnostics",),
            effect="readonly",
            condition="Run when final or status output depends on execution telemetry coverage.",
            performance_budget="read local env and indexed SQLite telemetry events; profile marker is optional explicit opt-in evidence",
            metadata={
                "reports": (
                    "local env adapter activation",
                    "no-profile command proxy telemetry",
                    "optional explicit profile shim marker",
                    "adapter telemetry event count",
                    "fail-closed readiness",
                    "raw shell gap",
                ),
                "fact_source": ".ai-client/project/state/aicg.db plus optional PowerShell profile marker; command proxy must not write user profiles",
                "non_invasive_default": "no-profile command proxy, comparable to a project-local virtual environment activation",
            },
        ),
        component(
            "reporter.task-record-queue-alignment",
            "reporter",
            "report",
            905,
            "Report task-record and task-queue scope differences for recovery and monitoring.",
            events=("status-output", "resume", "final-output"),
            final_only=True,
            mechanism_label="ai_client_governance.py task-run diagnose",
            gate_step="task-record-queue-alignment",
            fail_policy="report_only",
            produces_facts=("task_record_queue_alignment",),
            effect="readonly",
            condition="Run before final output when structured task records and workflow queue state may diverge.",
            performance_budget="read one SQLite DB summary and queue state only",
            metadata={
                "reports": (
                    "task-record task counts",
                    "task-queue workflow counts",
                    "current task presence in both stores",
                    "task_record_minus_queue_total delta",
                ),
                "fact_source": ".ai-client/project/state/aicg.db",
            },
        ),
        component(
            "reporter.framework-debt",
            "cross-cutting-gate",
            "report",
            906,
            "Report open framework design debt before architecture passes.",
            events=("plan-output", "resume", "final-output"),
            produces_facts=("framework_debt_items",),
            mechanism_label="ai_client_governance.py framework-debt list",
            gate_step="framework-debt",
            fail_policy="report_only",
            effect="readonly",
            condition=(
                "Run when a design flaw is real but needs a framework-level change window, "
                "such as CLI parser ergonomics or repo-context-aware scope classification."
            ),
            performance_budget="single SQLite read; no repository scan",
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
        component(
            "reporter.task-run-diagnostics",
            "reporter",
            "report",
            920,
            "Report task-run cache, execution telemetry, and worktree coordination diagnostics.",
            events=("status-output", "resume", "final-output"),
            mechanism_label="ai_client_governance.py task-run diagnose",
            gate_step="task-run-diagnostics",
            fail_policy="report_only",
            produces_facts=("task_run_diagnostics",),
            effect="readonly",
            condition="Run when reporting current execution health, final closeout, or resumed task state.",
            performance_budget="read SQLite telemetry and coord state only; no command execution",
            metadata={
                "reports": (
                    "duplicate terminal commands",
                    "failed telemetry events",
                    "cache hit/miss counts",
                    "active locks",
                    "missing worktree sessions",
                    "shell adapter evidence and raw shell interception gap",
                    "common/project/native scope kind counts",
                ),
            },
        ),
        component(
            "preflight.interceptor.task-run-dag.mutating",
            "processing-interceptor",
            "preflight",
            209,
            "Execute compressed command groups for mutating tasks, including small edits, once commands are ready.",
            task_types=(
                "code-debug",
                "correction",
                "rules-script",
                "docs",
                "git",
                "frontend",
                "resume",
                "multi-agent",
            ),
            events=("write-intent", "after-change", "resume", "final-output"),
            requires_facts=("task_id", "command_compression_analysis", "command_candidates"),
            produces_facts=("task_run_report", "execution_telemetry", "cache_decision"),
            mechanism_label="ai_client_governance.py task-run run",
            gate_label="ai_client_governance.py task-run diagnose",
            gate_step="task-run-diagnostics",
            fail_policy="fail_closed",
            effect="state_write",
            dedupe_key="task_id:event:changed_paths:command_candidates:input_hashes",
            condition=(
                "Run after command-compression analysis for mutating tasks; readonly and validation groups may "
                "parallelize/cache, stateful groups remain ordered and no-cache."
            ),
            performance_budget="single local DAG pass; cache only readonly/validation nodes with declared inputs; telemetry writes to aicg.db are mandatory by default",
            dependencies=("preflight.interceptor.command-compression.mutating",),
            metadata={
                "join_point": "write-intent",
                "aop_role": "around-advice",
                "runner": "task-run run",
                "coverage": "mutating-task-fast-path",
            },
        ),
        component(
            "preflight.interceptor.raw-shell-coverage",
            "processing-interceptor",
            "preflight",
            211,
            "Fail closed when a mutating task claims raw host shell coverage without auto-intercept or command-proxy evidence.",
            task_types=(
                "code-debug",
                "correction",
                "rules-script",
                "docs",
                "git",
                "frontend",
                "resume",
                "multi-agent",
            ),
            events=("write-intent", "after-change", "resume", "final-output"),
            requires_facts=("task_id", "execution_telemetry", "raw_shell_coverage_decision"),
            produces_facts=("raw_shell_gap_status", "shell_adapter_auto_intercept", "shell_command_proxy", "telemetry_wrapped_commands"),
            mechanism_label="ai_client_governance.py task-run diagnose --require-raw-shell-coverage",
            gate_label="ai_client_governance.py task-run diagnose --require-raw-shell-coverage",
            gate_step="raw-shell-coverage",
            fail_policy="fail_closed",
            effect="readonly",
            dedupe_key="task_id:event:trace_id:raw-shell-coverage",
            condition=(
                "Run when important local commands must prove shell coverage. "
                "Task-run/tool telemetry can prove commands were wrapped, while local env activation "
                "or no-profile command-proxy telemetry closes the raw host shell gap for governed commands; "
                "profile shims are explicit opt-in only."
            ),
            performance_budget="read SQLite/JSONL telemetry and optional profile marker only; no command execution or profile writes",
            dependencies=("preflight.interceptor.task-run-dag.mutating",),
            metadata={
                "required_cli": "task-run diagnose --require-raw-shell-coverage",
                "shell_adapter_cli": "shell-adapter diagnose --require-command-proxy",
                "command_proxy_cli": "shell-adapter proxy-powershell",
                "wrapped_telemetry_policy": "task-run/tool telemetry counts as compensation evidence; shell-adapter command-proxy telemetry clears raw_shell_gap for governed commands without touching user profiles",
            },
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
    parser = argparse.ArgumentParser(description="Inspect the ai-client-governance client governance component registry.")
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

    manifest_report = subparsers.add_parser("manifest-report", help="Compare runtime registry facts with manifest.json.")
    common_cli_args.add_common_global_args(manifest_report, names=("root",))
    manifest_report.add_argument("--manifest", default="manifest.json")
    manifest_report.add_argument("--check-manifest", action="store_true", help="Exit non-zero when drift is found.")
    manifest_report.add_argument("--format", choices=("text", "json"), default="text")
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
        "AI Client Governance Components",
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
    lines = ["AI Client Governance Runtime Task Types", f"Task types: {len(items)}"]
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


EXPECTED_RUNTIME_COMMAND_KEYS = {
    "components",
    "contractDescribe",
    "taskRunPlan",
    "taskRunRun",
    "taskRunDiagnose",
    "policyAssess",
    "shellAdapterDiagnose",
    "shellAdapterProxyPowerShell",
    "agentCommRegister",
    "agentCommHeartbeat",
    "telemetryReport",
    "telemetryEffectiveness",
    "telemetryEffectivenessSnapshot",
    "clientFlowProbe",
    "taskQueueLifecycle",
    "taskQueueTransition",
    "lifecycleInputFilter",
    "lifecyclePreflight",
    "taskRecord",
    "gatePool",
    "completionTest",
    "worktreeReconcile",
    "worktreeCloseoutAll",
    "hostCloseout",
}


def manifest_path(root: Path, value: str) -> Path:
    path = Path(value or "manifest.json")
    return path if path.is_absolute() else root / path


def build_manifest_report(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.root).resolve()
    path = manifest_path(root, args.manifest)
    manifest = json.loads(path.read_text(encoding="utf-8-sig"))
    runtime = manifest.get("runtimeArchitecture") or {}
    kinds = set(runtime.get("componentKinds") or [])
    contract = set(runtime.get("componentContract") or [])
    commands = set((runtime.get("runtimeCommands") or {}).keys())
    actual_contract = set(ComponentDefinition.__dataclass_fields__)
    contract_required = actual_contract - {"metadata"}
    drifts: list[dict[str, Any]] = []

    def add_drift(kind: str, missing: set[str], extra: set[str]) -> None:
        if missing or extra:
            drifts.append({"kind": kind, "missing": sorted(missing), "extra": sorted(extra)})

    add_drift("componentKinds", COMPONENT_KINDS - kinds, kinds - COMPONENT_KINDS)
    add_drift("componentContract", contract_required - contract, contract - actual_contract)
    add_drift("runtimeCommands", EXPECTED_RUNTIME_COMMAND_KEYS - commands, commands - EXPECTED_RUNTIME_COMMAND_KEYS)
    return {
        "manifest": path.as_posix(),
        "status": "pass" if not drifts else "fail",
        "drift_count": len(drifts),
        "drifts": drifts,
        "actual": {
            "componentKinds": sorted(COMPONENT_KINDS),
            "componentContract": sorted(contract_required),
            "runtimeCommands": sorted(EXPECTED_RUNTIME_COMMAND_KEYS),
        },
        "manifest_values": {
            "componentKinds": sorted(kinds),
            "componentContract": sorted(contract),
            "runtimeCommands": sorted(commands),
        },
    }


def render_manifest_report(report: dict[str, Any]) -> str:
    lines = [
        "AI Client Governance Runtime Manifest Report",
        f"Manifest: {report['manifest']}",
        f"Status: {report['status']}",
        f"Drifts: {report['drift_count']}",
    ]
    for drift in report["drifts"]:
        lines.append(f"- {drift['kind']}: missing={drift['missing']} extra={drift['extra']}")
    if not report["drifts"]:
        lines.append("- no manifest drift")
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
    if args.command == "manifest-report":
        report = build_manifest_report(args)
        if args.format == "json":
            print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(render_manifest_report(report))
        return 1 if args.check_manifest and report["drift_count"] else 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
