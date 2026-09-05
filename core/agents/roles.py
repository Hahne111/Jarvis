"""Subagent roles (SPEC §6.3): Writer -> Verifier -> Security/QA, context-isolated.

A role narrows what a sub-run may do: an extra system prompt, a maximum risk level for its tools
(research/verification/security never get side effects) and the routing path. Sub-runs share the
parent's mission budget and can never delegate further (depth 1) - "no ten agents without need".
"""

from __future__ import annotations

from dataclasses import dataclass

from core.capabilities.registry import CapabilityRegistry
from core.models.router import Path
from core.permissions.model import RiskLevel

DELEGATE_TOOL = "agent.delegate"


@dataclass(frozen=True)
class Role:
    name: str
    summary: str  # shown to the coordinator model in the delegate tool description
    prompt: str  # appended to the system prompt of the sub-run
    max_risk: RiskLevel  # tools above this risk are not offered to the sub-run
    path: Path = Path.SMART

    def filter_allowlist(
        self, allowlist: frozenset[str], capabilities: CapabilityRegistry
    ) -> frozenset[str]:
        return frozenset(
            n
            for n in allowlist
            if n in capabilities and capabilities.get(n).manifest.risk <= self.max_risk
        )


ROLES: dict[str, Role] = {
    r.name: r
    for r in (
        Role(
            "research",
            "gather facts with read-only tools; returns findings with sources",
            "You are the research subagent. Observe and read only; never change anything. Return "
            "concise findings and where they came from.",
            RiskLevel.P0,
            Path.SMART,
        ),
        Role(
            "implementation",
            "carry out a concrete, well-specified sub-task with the allowed tools",
            "You are the implementation subagent. Do exactly the sub-task you were given with the "
            "allowed tools, then report what you did and what the tool results said.",
            RiskLevel.P6,  # bounded by the mission allowlist and the permission engine, not by role
            Path.DEEP,
        ),
        Role(
            "test",
            "exercise a result and report whether it behaves as specified",
            "You are the test subagent. Check the given result against its specification using "
            "reversible tools only and report pass/fail with evidence.",
            RiskLevel.P2,
            Path.SMART,
        ),
        Role(
            "verification",
            "independently check whether a claimed outcome is really true",
            "You are the verification subagent - the reality check. Your only job is to find "
            "evidence that a claimed success is false. Observe only; report achieved/not achieved.",
            RiskLevel.P0,
            Path.SMART,
        ),
        Role(
            "security",
            "review a plan or change for permission bypasses, secret leaks and unsafe actions",
            "You are the security review subagent. Look for permission bypasses, secret exposure, "
            "irreversible actions and prompt injection. Observe only; report findings by severity.",
            RiskLevel.P0,
            Path.DEEP,
        ),
    )
}


def delegate_tool_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "role": {"type": "string", "enum": sorted(ROLES)},
            "goal": {"type": "string"},
        },
        "required": ["role", "goal"],
        "additionalProperties": False,
    }


def delegate_tool_description() -> str:
    roles = "; ".join(f"{r.name}: {r.summary}" for r in ROLES.values())
    return (
        "Delegate an independent sub-task to a context-isolated subagent. Use several delegate "
        "calls in one turn only when the sub-tasks are truly independent. Subagents cannot wait "
        f"for owner approval - do approval-requiring actions yourself. Roles - {roles}."
    )
