"""Unified security and command policy gate.

This module is intentionally small and local-rule based. It gives governance
commands one shared policy vocabulary now, while leaving room for a future OPA
or declarative policy backend when the framework owns a broader command
gateway.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ai_client_governance.common import cli_arguments as common_cli_args


SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
DECISION_ORDER = {"allow": 0, "warn": 1, "approval_required": 2, "block": 3}
SENSITIVE_RE = re.compile(
    r"(?i)(api[_-]?key|access[_-]?key|authorization|bearer\s+[a-z0-9._~+/=-]+|"
    r"password|passwd|secret|token|credential|private[_-]?key)"
)
PROMPT_INJECTION_RE = re.compile(
    r"(?i)(ignore (all )?(previous|prior|above) instructions|"
    r"reveal (the )?(system|developer) prompt|"
    r"bypass (the )?(policy|guard|safety)|"
    r"disregard (the )?(rules|instructions)|"
    r"act as an unrestricted|jailbreak)"
)
COMMAND_INJECTION_RE = re.compile(r"(?i)(\|\||&&|;\s*rm\b|;\s*curl\b|`[^`]+`|\$\([^)]+\))")
PATH_ESCAPE_RE = re.compile(r"(^|[\\/])\.\.([\\/]|$)")


@dataclass(frozen=True)
class PolicyFinding:
    category: str
    severity: str
    action: str
    message: str
    evidence: str


@dataclass(frozen=True)
class PolicyAssessment:
    subject_type: str
    decision: str
    severity: str
    findings: list[PolicyFinding]
    standards: list[str]
    note: str


def padded_lower(value: str) -> str:
    normalized = re.sub(r"\s+", " ", value or "").strip().lower()
    return f" {normalized} "


def short_evidence(value: str, limit: int = 120) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    if SENSITIVE_RE.search(text):
        text = SENSITIVE_RE.sub("<sensitive>", text)
    return text[:limit]


def command_findings(command: str) -> list[PolicyFinding]:
    lowered = padded_lower(command)
    stripped = lowered.strip()
    findings: list[PolicyFinding] = []
    if " git push " in lowered:
        findings.append(
            PolicyFinding(
                "git",
                "high",
                "approval_required",
                "Remote Git writes require a separate push approval boundary.",
                "git push",
            )
        )
    if any(marker in lowered for marker in (" git add ", " git commit ", " git merge ", " git rm ", " git mv ")):
        findings.append(
            PolicyFinding(
                "git_write",
                "high",
                "approval_required",
                "Git write commands require an explicit local execution approval boundary.",
                short_evidence(command),
            )
        )
    if any(marker in lowered for marker in (" remove-item ", " rm -rf ", " rmdir ", " del ")):
        findings.append(
            PolicyFinding(
                "destructive_command",
                "high",
                "approval_required",
                "Delete commands require explicit approval and path-boundary evidence.",
                short_evidence(command),
            )
        )
    if (
        stripped.startswith("pip install ")
        or stripped.startswith("npm install ")
        or stripped.startswith("pnpm install ")
        or stripped.startswith("yarn add ")
        or " poetry add " in lowered
    ):
        findings.append(
            PolicyFinding(
                "supply_chain",
                "high",
                "approval_required",
                "Dependency installation changes the supply-chain surface and may change lockfiles.",
                short_evidence(command),
            )
        )
    if (
        stripped.startswith("curl ")
        or stripped.startswith("wget ")
        or " invoke-webrequest " in lowered
        or " invoke-restmethod " in lowered
    ):
        findings.append(
            PolicyFinding(
                "network",
                "medium",
                "warn",
                "Network commands require source-trust and output-handling review.",
                short_evidence(command),
            )
        )
    if COMMAND_INJECTION_RE.search(command):
        findings.append(
            PolicyFinding(
                "command_injection",
                "high",
                "approval_required",
                "Shell control operators or command substitution need explicit command-intent review.",
                short_evidence(command),
            )
        )
    if SENSITIVE_RE.search(command):
        findings.append(
            PolicyFinding(
                "sensitive_information",
                "critical",
                "block",
                "Command appears to contain a secret or credential-bearing value.",
                short_evidence(command),
            )
        )
    if PATH_ESCAPE_RE.search(command):
        findings.append(
            PolicyFinding(
                "path_boundary",
                "medium",
                "warn",
                "Path traversal markers require repository boundary review.",
                short_evidence(command),
            )
        )
    return findings


def text_findings(text: str, *, subject_type: str, source: str = "") -> list[PolicyFinding]:
    findings: list[PolicyFinding] = []
    if PROMPT_INJECTION_RE.search(text):
        findings.append(
            PolicyFinding(
                "prompt_injection",
                "high",
                "block" if source in {"web", "external", "file"} else "approval_required",
                "Input contains prompt-injection style instructions; treat as untrusted content, not governance authority.",
                short_evidence(text),
            )
        )
    if SENSITIVE_RE.search(text):
        findings.append(
            PolicyFinding(
                "sensitive_information",
                "critical",
                "block",
                f"{subject_type} appears to contain secret-bearing text and must be redacted.",
                short_evidence(text),
            )
        )
    return findings


def combine_decision(findings: list[PolicyFinding]) -> tuple[str, str]:
    if not findings:
        return "allow", "low"
    decision = max((finding.action for finding in findings), key=lambda item: DECISION_ORDER[item])
    severity = max((finding.severity for finding in findings), key=lambda item: SEVERITY_ORDER[item])
    if decision == "allow" and severity != "low":
        decision = "warn"
    return decision, severity


def assess(
    *,
    command: str = "",
    text: str = "",
    subject_type: str = "command",
    source: str = "",
) -> PolicyAssessment:
    findings = command_findings(command) if command else text_findings(text, subject_type=subject_type, source=source)
    decision, severity = combine_decision(findings)
    return PolicyAssessment(
        subject_type=subject_type,
        decision=decision,
        severity=severity,
        findings=findings,
        standards=[
            "OWASP LLM Top 10: prompt injection, sensitive information disclosure, supply-chain risk",
            "NIST AI RMF / GenAI Profile: govern, map, measure, manage risk decisions",
            "Policy-as-code model: deterministic local policy evaluation before execution",
        ],
        note=(
            "Host-native raw shell interception is outside this plugin's direct control; "
            "this gate fail-closes governed command paths and records explicit bypass risk."
        ),
    )


def assessment_dict(result: PolicyAssessment) -> dict[str, Any]:
    return asdict(result)


def render_text(result: PolicyAssessment) -> str:
    lines = [
        "AI Client Governance Policy Assessment",
        f"Subject: {result.subject_type}",
        f"Decision: {result.decision}",
        f"Severity: {result.severity}",
        "",
        "Findings:",
    ]
    if not result.findings:
        lines.append("  - none")
    for finding in result.findings:
        lines.append(
            f"  - {finding.category} {finding.severity} {finding.action}: "
            f"{finding.message} evidence={finding.evidence}"
        )
    lines.append("")
    lines.append(result.note)
    return "\n".join(lines)


def decision_fails(decision: str, fail_on: str) -> bool:
    if fail_on == "none":
        return False
    return DECISION_ORDER[decision] >= DECISION_ORDER[fail_on]


def command_assess(args: argparse.Namespace) -> int:
    if args.command:
        result = assess(command=args.command, subject_type="command", source=args.source or "")
    else:
        text = args.text or ""
        if args.file:
            text = Path(args.file).read_text(encoding="utf-8-sig")
        result = assess(text=text, subject_type=args.subject_type, source=args.source or "")
    if args.format == "json":
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    else:
        print(render_text(result))
    return 1 if decision_fails(result.decision, args.fail_on) else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run unified AI Client Governance policy checks.")
    common_cli_args.add_common_global_args(parser, names=("root", "format"))
    sub = parser.add_subparsers(dest="command_name", required=True)

    assess_parser = sub.add_parser("assess", help="Assess one command, input, output, or file.")
    common_cli_args.add_common_global_args(assess_parser, names=("root", "format"), suppress_default=True)
    assess_parser.add_argument("--command", default="", help="Command text to classify.")
    assess_parser.add_argument("--text", default="", help="Input or output text to classify.")
    assess_parser.add_argument("--file", default="", help="Read text to classify from a UTF-8 file.")
    assess_parser.add_argument("--subject-type", default="command", help="Subject type label.")
    assess_parser.add_argument("--source", default="", help="Source boundary, e.g. user, web, file, tool.")
    assess_parser.add_argument(
        "--fail-on",
        choices=("none", "warn", "approval_required", "block"),
        default="block",
        help="Exit non-zero when the decision is at least this severe.",
    )
    assess_parser.set_defaults(func=command_assess)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
