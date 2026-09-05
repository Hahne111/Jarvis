"""Permission model (SECURITY.md §1, SPEC §17.1 PermissionDecision).

P0 Observe   automatic, logged        P4 Critical    strong approval (passkey/biometric)
P1 Safe      automatic                P5 Restricted  policy + trusted device + strong approval
P2 Reversible automatic + undo/log    P6 Forbidden   never
P3 Sensitive confirmation per context
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import IntEnum, StrEnum
from typing import Any

from core.events.envelope import DEFAULT_USER_ID


class RiskLevel(IntEnum):
    P0 = 0  # observe
    P1 = 1  # safe
    P2 = 2  # reversible
    P3 = 3  # sensitive
    P4 = 4  # critical
    P5 = 5  # restricted
    P6 = 6  # forbidden

    @property
    def label(self) -> str:
        return _RISK_LABELS[self]


_RISK_LABELS = {
    RiskLevel.P0: "observe",
    RiskLevel.P1: "safe",
    RiskLevel.P2: "reversible",
    RiskLevel.P3: "sensitive",
    RiskLevel.P4: "critical",
    RiskLevel.P5: "restricted",
    RiskLevel.P6: "forbidden",
}


class Decision(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


# Strictness order used to guarantee that policy can only get stricter (Development Law 9).
STRICTNESS = {Decision.ALLOW: 0, Decision.ASK: 1, Decision.DENY: 2}


class ApprovalStrength(IntEnum):
    """How strong an approval proof is. Voice is a comfort signal, never an authenticator."""

    NONE = 0
    VOICE = 1  # never sufficient on its own (SECURITY.md §2 rule 1)
    UI_CONFIRM = 2  # click/tap on an unlocked trusted device
    STRONG = 3  # passkey / biometric / hardware key


class ProofMethod(StrEnum):
    VOICE = "voice"
    UI_CONFIRM = "ui_confirm"
    PASSKEY = "passkey"
    BIOMETRIC = "biometric"
    HARDWARE_KEY = "hardware_key"


PROOF_STRENGTH: dict[ProofMethod, ApprovalStrength] = {
    ProofMethod.VOICE: ApprovalStrength.VOICE,
    ProofMethod.UI_CONFIRM: ApprovalStrength.UI_CONFIRM,
    ProofMethod.PASSKEY: ApprovalStrength.STRONG,
    ProofMethod.BIOMETRIC: ApprovalStrength.STRONG,
    ProofMethod.HARDWARE_KEY: ApprovalStrength.STRONG,
}


class PermissionError_(Exception):
    """Base class for permission failures (name avoids shadowing the builtin)."""


class PolicyViolation(PermissionError_):
    """Attempt to make the policy less strict, or an invalid policy."""


class ApprovalError(PermissionError_):
    """Approval rejected: unknown, expired, already decided, insufficient proof."""


def _now() -> datetime:
    return datetime.now(UTC)


def _new_id() -> str:
    return str(uuid.uuid4())


@dataclass(frozen=True)
class ApprovalProof:
    method: ProofMethod
    subject: str = DEFAULT_USER_ID  # who approved
    device_id: str | None = None  # on which device
    device_trusted: bool = False
    reference: str | None = None  # opaque id of the OS/passkey assertion, never a secret

    @property
    def strength(self) -> ApprovalStrength:
        return PROOF_STRENGTH[ProofMethod(self.method)]

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": ProofMethod(self.method).value,
            "subject": self.subject,
            "device_id": self.device_id,
            "device_trusted": self.device_trusted,
            "reference": self.reference,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ApprovalProof:
        return cls(
            method=ProofMethod(d["method"]),
            subject=d.get("subject", DEFAULT_USER_ID),
            device_id=d.get("device_id"),
            device_trusted=bool(d.get("device_trusted", False)),
            reference=d.get("reference"),
        )


@dataclass(frozen=True)
class PermissionRequest:
    """What an agent wants to do. Evaluated deterministically by the PermissionEngine."""

    action: str  # capability name, e.g. "computer.open_app"
    risk: RiskLevel
    actor: str  # agent / component asking
    correlation_id: str = field(default_factory=_new_id)  # usually the mission_id
    user_id: str = DEFAULT_USER_ID
    device_id: str | None = None
    device_trusted: bool = False
    context: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.action, str) or not self.action.strip():
            raise ValueError("action must be a non-empty string")
        if not isinstance(self.actor, str) or not self.actor.strip():
            raise ValueError("actor must be a non-empty string")
        object.__setattr__(self, "risk", RiskLevel(self.risk))

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "risk": int(self.risk),
            "actor": self.actor,
            "correlation_id": self.correlation_id,
            "user_id": self.user_id,
            "device_id": self.device_id,
            "device_trusted": self.device_trusted,
            "context": dict(self.context),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PermissionRequest:
        return cls(
            action=d["action"],
            risk=RiskLevel(int(d["risk"])),
            actor=d["actor"],
            correlation_id=d["correlation_id"],
            user_id=d.get("user_id", DEFAULT_USER_ID),
            device_id=d.get("device_id"),
            device_trusted=bool(d.get("device_trusted", False)),
            context=dict(d.get("context") or {}),
        )


@dataclass
class PermissionDecision:
    """SPEC §17.1: action, risk, rule, approval proof, expiration."""

    request: PermissionRequest
    decision: Decision
    rule: str  # id of the policy rule that produced the decision
    required_strength: ApprovalStrength = ApprovalStrength.NONE
    decision_id: str = field(default_factory=_new_id)
    approval_proof: ApprovalProof | None = None
    created_at: datetime = field(default_factory=_now)
    expires_at: datetime | None = None  # ASK: approval deadline; ALLOW: grant expiry
    used_at: datetime | None = None  # ALLOW grants are single-use once consumed by the gateway
    reason: str | None = None

    @property
    def action(self) -> str:
        return self.request.action

    @property
    def risk(self) -> RiskLevel:
        return self.request.risk

    def is_expired(self, now: datetime | None = None) -> bool:
        return self.expires_at is not None and (now or _now()) >= self.expires_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "request": self.request.to_dict(),
            "decision": self.decision.value,
            "rule": self.rule,
            "required_strength": int(self.required_strength),
            "approval_proof": self.approval_proof.to_dict() if self.approval_proof else None,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "used_at": self.used_at.isoformat() if self.used_at else None,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PermissionDecision:
        return cls(
            decision_id=d["decision_id"],
            request=PermissionRequest.from_dict(d["request"]),
            decision=Decision(d["decision"]),
            rule=d["rule"],
            required_strength=ApprovalStrength(int(d.get("required_strength", 0))),
            approval_proof=(
                ApprovalProof.from_dict(d["approval_proof"]) if d.get("approval_proof") else None
            ),
            created_at=datetime.fromisoformat(d["created_at"]),
            expires_at=datetime.fromisoformat(d["expires_at"]) if d.get("expires_at") else None,
            used_at=datetime.fromisoformat(d["used_at"]) if d.get("used_at") else None,
            reason=d.get("reason"),
        )
