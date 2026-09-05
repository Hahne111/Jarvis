"""Permission Engine (SECURITY.md §1-2, Phase 2 / Commit 006)."""

from core.permissions.engine import PermissionEngine
from core.permissions.model import (
    ApprovalError,
    ApprovalProof,
    ApprovalStrength,
    Decision,
    PermissionDecision,
    PermissionRequest,
    PolicyViolation,
    ProofMethod,
    RiskLevel,
)
from core.permissions.policy import DEFAULT_RULES, Policy, Verdict

__all__ = [
    "DEFAULT_RULES",
    "ApprovalError",
    "ApprovalProof",
    "ApprovalStrength",
    "Decision",
    "PermissionDecision",
    "PermissionEngine",
    "PermissionRequest",
    "Policy",
    "PolicyViolation",
    "ProofMethod",
    "RiskLevel",
    "Verdict",
]
