#!/usr/bin/env python3
"""Unified ai-client-governance command entry."""

from __future__ import annotations

import sys
from collections.abc import Callable

from ai_client_governance.agents import comm as agent_comm
from ai_client_governance.agents import group_status
from ai_client_governance.audit import rule_tooling_audit
from ai_client_governance.docs import doc_index, validate_doc_task
from ai_client_governance.gates import architecture_guard, gate_pool, session_gate, task_gate
from ai_client_governance.io import context_extract
from ai_client_governance import templates
from ai_client_governance.lifecycle import engine as lifecycle
from ai_client_governance.records import scan_corrections, task_queue, tool_flow, tool_invocations
from ai_client_governance.runtime import registry as runtime_registry
from ai_client_governance.sync import check as sync_check
from ai_client_governance.validation import completion, encoding, selftest
from ai_client_governance.worktree import coord as worktree_coord
from ai_client_governance.worktree import task as worktree_task


COMMANDS: dict[str, tuple[str, Callable[[], int]]] = {
    "agent-comm": ("Manage agent communication bus.", agent_comm.main),
    "agent-groups": ("Report agent group status.", group_status.main),
    "architecture-guard": ("Check AI Client Governance architecture boundaries.", architecture_guard.main),
    "completion-test": ("Plan task completion tests from changed paths and task types.", completion.main),
    "context-extract": ("Extract safe slices from long context files.", context_extract.main),
    "doc-index": ("Build and check Markdown document reference indexes.", doc_index.main),
    "gate-pool": ("Run a traceable pool of gates.", gate_pool.main),
    "lifecycle": ("Run lifecycle preflight/finalize/status.", lifecycle.main),
    "rule-audit": ("Audit rule-entry and README sections for tooling migration.", rule_tooling_audit.main),
    "runtime": ("Inspect agent governance runtime components.", runtime_registry.main),
    "scan-corrections": ("Scan correction records.", scan_corrections.main),
    "session-gate": ("Validate session closeout evidence.", session_gate.main),
    "sync-check": ("Check embedded ai-client-governance synchronization state.", sync_check.main),
    "task-gate": ("Validate task-type evidence.", task_gate.main),
    "task-queue": ("Manage task workflow state.", task_queue.main),
    "templates": ("Render Markdown templates.", templates.main),
    "selftest": ("Run ai-client-governance black-box self-tests.", selftest.main),
    "tool-flow": ("Report invocation trace flow.", tool_flow.main),
    "tool-invocations": ("Record and report tool invocations.", tool_invocations.main),
    "validate-doc": ("Validate document task evidence.", validate_doc_task.main),
    "validate-encoding": ("Validate text encoding and source hygiene.", encoding.main),
    "worktree-coord": ("Coordinate sessions across git worktrees.", worktree_coord.main),
    "worktree-task": ("Create and close fixed task worktrees.", worktree_task.main),
}


def render_commands() -> str:
    lines = ["Available ai-client-governance commands:"]
    for name in sorted(COMMANDS):
        lines.append(f"  {name:<20} {COMMANDS[name][0]}")
    return "\n".join(lines)


def main() -> int:
    argv = sys.argv[1:]
    if not argv or argv[0] in {"-h", "--help", "--list"}:
        print(render_commands())
        return 0
    command, remainder = argv[0], argv[1:]
    if command not in COMMANDS:
        print(f"Unknown ai-client-governance command: {command}", file=sys.stderr)
        print(render_commands(), file=sys.stderr)
        return 2
    sys.argv = [f"ai_client_governance {command}", *remainder]
    return COMMANDS[command][1]()


if __name__ == "__main__":
    raise SystemExit(main())
