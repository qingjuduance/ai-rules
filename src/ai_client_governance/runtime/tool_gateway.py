#!/usr/bin/env python3
"""Schema-first agent tool gateway catalog.

This module is a registry and validation surface first. It does not claim that
the host client must route tool calls through it; that requires a host-client
integration. The plugin can publish stable schemas, validate JSON arguments
when invoked, and keep the human CLI as a secondary surface.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from typing import Any


SIDE_EFFECT_VALUES = (
    "readonly",
    "state_write",
    "repo_write",
    "git_write",
    "network",
    "command",
    "human_interrupt",
)
CONTROL_LAYER_VALUES = (
    "plugin",
    "plugin-command-wrapper",
    "host-client",
    "model-api",
)
ENFORCEMENT_LEVEL_VALUES = (
    "schema_validated_when_called",
    "governed_invocation_only",
    "host_client_required",
    "model_api_required",
)
REQUIRED_TOOL_FIELDS = (
    "name",
    "description",
    "command",
    "side_effect",
    "parallel_safe",
    "control_layer",
    "enforcement_level",
    "parameters_schema",
    "output_schema",
    "compact_output_policy",
)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    command: str
    side_effect: str
    parallel_safe: bool
    control_layer: str
    enforcement_level: str
    parameters_schema: dict[str, Any]
    output_schema: dict[str, Any]
    compact_output_policy: str


def object_schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": required or [],
    }


def gateway_tool_schema() -> dict[str, Any]:
    return object_schema(
        {
            "name": {"type": "string"},
            "description": {"type": "string"},
            "command": {"type": "string"},
            "side_effect": {"type": "string", "enum": list(SIDE_EFFECT_VALUES)},
            "parallel_safe": {"type": "boolean"},
            "control_layer": {"type": "string", "enum": list(CONTROL_LAYER_VALUES)},
            "enforcement_level": {"type": "string", "enum": list(ENFORCEMENT_LEVEL_VALUES)},
            "parameters_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "compact_output_policy": {
                "type": "string",
                "description": "Required compact-output rule for default CLI/model-facing output.",
            },
        },
        required=list(REQUIRED_TOOL_FIELDS),
    )


TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="task_queue_lifecycle",
        description="Return queue/task-record alignment for one task or the whole project.",
        command="task-queue lifecycle",
        side_effect="readonly",
        parallel_safe=True,
        control_layer="plugin",
        enforcement_level="schema_validated_when_called",
        parameters_schema=object_schema(
            {
                "task_id": {"type": "string", "description": "Optional task id filter."},
                "fail_on_drift": {"type": "boolean", "default": False},
                "format": {"type": "string", "enum": ["json", "text"], "default": "json"},
            }
        ),
        output_schema=object_schema(
            {
                "status_counts": {"type": "object"},
                "warnings": {"type": "array", "items": {"type": "string"}},
                "blocking_warnings": {"type": "array", "items": {"type": "string"}},
            }
        ),
        compact_output_policy="Default to counts, warnings, and matching task rows; link full JSON as artifact when large.",
    ),
    ToolSpec(
        name="framework_debt_report",
        description="Surface open framework debt by severity before write or closeout.",
        command="framework-debt report",
        side_effect="readonly",
        parallel_safe=True,
        control_layer="plugin",
        enforcement_level="schema_validated_when_called",
        parameters_schema=object_schema(
            {
                "min_severity": {"type": "string", "enum": ["P0", "P1", "P2", "P3"], "default": "P1"},
                "category": {"type": "string"},
                "format": {"type": "string", "enum": ["json", "text"], "default": "json"},
            }
        ),
        output_schema=object_schema(
            {
                "open_count": {"type": "integer"},
                "important_count": {"type": "integer"},
                "items": {"type": "array"},
                "decision": {"type": "string"},
            }
        ),
        compact_output_policy="Return severity counts plus P0/P1 titles first; keep long problem text behind drill-down.",
    ),
    ToolSpec(
        name="corrections_report",
        description="Surface open corrections and P0/P1 process failures.",
        command="corrections report",
        side_effect="readonly",
        parallel_safe=True,
        control_layer="plugin",
        enforcement_level="schema_validated_when_called",
        parameters_schema=object_schema(
            {
                "include_closed": {"type": "boolean", "default": False},
                "format": {"type": "string", "enum": ["json", "text"], "default": "json"},
            }
        ),
        output_schema=object_schema(
            {
                "open_count": {"type": "integer"},
                "by_severity": {"type": "object"},
                "has_p0": {"type": "boolean"},
                "items": {"type": "array"},
            }
        ),
        compact_output_policy="Return IDs, severities, titles, and fix actions; omit long root-cause prose by default.",
    ),
    ToolSpec(
        name="runtime_capability_report",
        description="Report plugin, host-client, and model/API capability boundaries.",
        command="runtime capability-report",
        side_effect="readonly",
        parallel_safe=True,
        control_layer="plugin",
        enforcement_level="schema_validated_when_called",
        parameters_schema=object_schema(
            {
                "format": {"type": "string", "enum": ["json", "text"], "default": "json"},
                "capability": {"type": "string", "description": "Optional capability id filter."},
            }
        ),
        output_schema=object_schema(
            {
                "plugin_enforceable": {"type": "array"},
                "plugin_auditable": {"type": "array"},
                "host_client_required": {"type": "array"},
                "model_api_required": {"type": "array"},
            }
        ),
        compact_output_policy="Return capability ids and control layers; include long notes only for the selected capability.",
    ),
    ToolSpec(
        name="shell_adapter_proxy_powershell",
        description="Run a PowerShell command through the non-invasive no-profile command proxy.",
        command="shell-adapter proxy-powershell",
        side_effect="command",
        parallel_safe=False,
        control_layer="plugin-command-wrapper",
        enforcement_level="governed_invocation_only",
        parameters_schema=object_schema(
            {
                "task_id": {"type": "string"},
                "command": {"type": "string"},
                "cwd": {"type": "string"},
                "fail_on_inline_risk": {"type": "boolean", "default": False},
            },
            required=["command"],
        ),
        output_schema=object_schema(
            {
                "exit_code": {"type": "integer"},
                "telemetry_span_id": {"type": "string"},
                "command_error": {"type": "object"},
            }
        ),
        compact_output_policy="Return exit code, classified error, and artifact paths; never dump large stdout by default.",
    ),
)


def validate_specs(specs: list[ToolSpec]) -> list[str]:
    warnings: list[str] = []
    for spec in specs:
        if spec.side_effect not in SIDE_EFFECT_VALUES:
            warnings.append(f"{spec.name}: invalid side_effect={spec.side_effect}")
        if spec.control_layer not in CONTROL_LAYER_VALUES:
            warnings.append(f"{spec.name}: invalid control_layer={spec.control_layer}")
        if spec.enforcement_level not in ENFORCEMENT_LEVEL_VALUES:
            warnings.append(f"{spec.name}: invalid enforcement_level={spec.enforcement_level}")
        if not spec.compact_output_policy.strip():
            warnings.append(f"{spec.name}: compact_output_policy is required")
    return warnings


def filtered_specs(name: str = "") -> list[ToolSpec]:
    if not name:
        return list(TOOL_SPECS)
    return [spec for spec in TOOL_SPECS if spec.name == name]


def build_report(name: str = "") -> dict[str, Any]:
    specs = filtered_specs(name)
    validation_warnings = validate_specs(specs)
    return {
        "schema_version": 1,
        "schema_kind": "ai-client-governance-tool-gateway",
        "gateway_status": "plugin_registry_only",
        "host_client_integration_required": True,
        "tool_schema": gateway_tool_schema(),
        "field_values": {
            "side_effect": list(SIDE_EFFECT_VALUES),
            "parallel_safe": "boolean; true only for readonly/stateless calls that can run concurrently",
            "control_layer": list(CONTROL_LAYER_VALUES),
            "enforcement_level": list(ENFORCEMENT_LEVEL_VALUES),
            "compact_output_policy": "required non-empty string on every tool spec",
        },
        "validation_status": "pass" if not validation_warnings else "fail",
        "validation_warnings": validation_warnings,
        "warning": (
            "Schemas are plugin-enforceable when this gateway is called. They do not force "
            "the host agent loop to dispatch through the gateway until the host client integrates it."
        ),
        "tools": [asdict(spec) for spec in specs],
    }


def render_text(report: dict[str, Any]) -> str:
    lines = [
        "AI Client Governance Agent Tool Gateway",
        f"Status: {report['gateway_status']}",
        f"Host integration required: {report['host_client_integration_required']}",
        f"Tools: {len(report['tools'])}",
        f"Warning: {report['warning']}",
    ]
    for item in report["tools"]:
        lines.append(
            f"- {item['name']}: {item['command']} [{item['side_effect']}; "
            f"parallel_safe={item['parallel_safe']}; {item['enforcement_level']}]"
        )
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect schema-first agent tool gateway specs.")
    parser.add_argument("--tool", help="Only show one tool by name.")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = build_report(args.tool or "")
    if args.tool and not report["tools"]:
        print(f"unknown tool gateway spec: {args.tool}")
        return 1
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_text(report))
    return 0 if report["validation_status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
