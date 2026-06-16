"""Agent governance runtime primitives for ai-client-governance."""

from ai_client_governance.runtime.registry import (
    AgentExecutionContext,
    ComponentDefinition,
    ComponentRegistry,
    TaskTypeDefinition,
    default_registry,
    requires_approval_for,
    requires_tracking_for,
)

__all__ = [
    "AgentExecutionContext",
    "ComponentDefinition",
    "ComponentRegistry",
    "TaskTypeDefinition",
    "default_registry",
    "requires_approval_for",
    "requires_tracking_for",
]
