"""Deterministic policy evaluation. No model, no prompt - a table and a few rules.

Development Law 9: the policy can only become stricter without explicit owner approval, so
``Policy`` refuses any override that would loosen a default.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.permissions.model import (
    STRICTNESS,
    ApprovalStrength,
    Decision,
    PermissionRequest,
    PolicyViolation,
    RiskLevel,
)

# Default decision and required approval strength per risk level (SECURITY.md §1).
DEFAULT_RULES: dict[RiskLevel, tuple[Decision, ApprovalStrength]] = {
    RiskLevel.P0: (Decision.ALLOW, ApprovalStrength.NONE),
    RiskLevel.P1: (Decision.ALLOW, ApprovalStrength.NONE),
    RiskLevel.P2: (Decision.ALLOW, ApprovalStrength.NONE),
    RiskLevel.P3: (Decision.ASK, ApprovalStrength.UI_CONFIRM),
    RiskLevel.P4: (Decision.ASK, ApprovalStrength.STRONG),
    RiskLevel.P5: (Decision.ASK, ApprovalStrength.STRONG),
    RiskLevel.P6: (Decision.DENY, ApprovalStrength.NONE),
}

# How long an ASK waits for an answer, and how long a granted approval stays valid.
DEFAULT_ASK_TTL_S: dict[ApprovalStrength, int] = {
    ApprovalStrength.UI_CONFIRM: 300,
    ApprovalStrength.STRONG: 120,
}
DEFAULT_GRANT_TTL_S = 60  # temporary capability: rights expire (SECURITY.md §2 rule 3)


@dataclass(frozen=True)
class Verdict:
    decision: Decision
    rule: str
    required_strength: ApprovalStrength = ApprovalStrength.NONE
    reason: str | None = None


@dataclass(frozen=True)
class Policy:
    """Immutable policy. Build once; a stricter variant is a new object (``stricter``)."""

    overrides: dict[RiskLevel, Decision] = field(default_factory=dict)
    forbidden_actions: frozenset[str] = frozenset()
    ask_actions: frozenset[str] = frozenset()  # force ASK (>= UI_CONFIRM) regardless of risk
    grant_ttl_s: int = DEFAULT_GRANT_TTL_S
    ask_ttl_s: dict[ApprovalStrength, int] = field(default_factory=lambda: dict(DEFAULT_ASK_TTL_S))

    def __post_init__(self) -> None:
        # Normalise so plain strings/ints from config behave exactly like the enums.
        normalised = {RiskLevel(r): Decision(d) for r, d in self.overrides.items()}
        object.__setattr__(self, "overrides", normalised)
        object.__setattr__(self, "forbidden_actions", frozenset(self.forbidden_actions))
        object.__setattr__(self, "ask_actions", frozenset(self.ask_actions))
        object.__setattr__(
            self, "ask_ttl_s", {ApprovalStrength(k): int(v) for k, v in self.ask_ttl_s.items()}
        )
        for risk, decision in self.overrides.items():
            base = DEFAULT_RULES[risk][0]
            if STRICTNESS[decision] < STRICTNESS[base]:
                raise PolicyViolation(
                    f"override for {risk.name} would loosen policy "
                    f"({base.value} -> {decision.value})"
                )
        if self.grant_ttl_s <= 0 or self.grant_ttl_s > DEFAULT_GRANT_TTL_S:
            raise PolicyViolation(f"grant_ttl_s must be in 1..{DEFAULT_GRANT_TTL_S}")
        for strength, ttl in self.ask_ttl_s.items():
            if ttl <= 0 or ttl > DEFAULT_ASK_TTL_S[ApprovalStrength(strength)]:
                raise PolicyViolation("ask_ttl_s may only be shortened, never extended")

    def stricter(self, **changes: object) -> Policy:
        """Return a policy that is at least as strict as ``self`` in every dimension."""
        new_overrides: dict = changes.get("overrides", {})  # type: ignore[assignment]
        for risk, decision in new_overrides.items():
            current = self.overrides.get(RiskLevel(risk), DEFAULT_RULES[RiskLevel(risk)][0])
            if STRICTNESS[Decision(decision)] < STRICTNESS[current]:
                raise PolicyViolation(
                    f"override for {RiskLevel(risk).name} would loosen policy "
                    f"({current.value} -> {Decision(decision).value})"
                )
        if int(changes.get("grant_ttl_s", self.grant_ttl_s)) > self.grant_ttl_s:  # type: ignore[call-overload]
            raise PolicyViolation("grant_ttl_s may only be shortened")
        for strength, ttl in dict(changes.get("ask_ttl_s", {})).items():  # type: ignore[call-overload]
            if int(ttl) > self.ask_ttl_s[ApprovalStrength(strength)]:
                raise PolicyViolation("ask_ttl_s may only be shortened")
        merged = {
            "overrides": {**self.overrides, **changes.pop("overrides", {})},  # type: ignore[dict-item]
            "forbidden_actions": self.forbidden_actions
            | frozenset(changes.pop("forbidden_actions", ())),  # type: ignore[arg-type]
            "ask_actions": self.ask_actions | frozenset(changes.pop("ask_actions", ())),  # type: ignore[arg-type]
            "grant_ttl_s": changes.pop("grant_ttl_s", self.grant_ttl_s),
            "ask_ttl_s": {**self.ask_ttl_s, **changes.pop("ask_ttl_s", {})},  # type: ignore[dict-item]
        }
        if changes:
            raise PolicyViolation(f"unknown policy fields: {sorted(changes)}")
        return Policy(**merged)  # type: ignore[arg-type]

    def evaluate(self, request: PermissionRequest) -> Verdict:
        risk = request.risk
        if request.action in self.forbidden_actions or risk is RiskLevel.P6:
            return Verdict(Decision.DENY, "forbidden", reason="action is P6/forbidden")
        decision, strength = DEFAULT_RULES[risk]
        rule = f"default:{risk.name}"
        if risk in self.overrides:
            decision = self.overrides[risk]
            rule = f"override:{risk.name}"
            if decision is Decision.ASK and strength is ApprovalStrength.NONE:
                strength = ApprovalStrength.UI_CONFIRM
        if request.action in self.ask_actions and decision is Decision.ALLOW:
            decision, rule = Decision.ASK, "ask_action"
            strength = max(strength, ApprovalStrength.UI_CONFIRM)
        if decision is Decision.DENY:
            return Verdict(Decision.DENY, rule)
        if risk is RiskLevel.P5 and not request.device_trusted:
            # Restricted actions are device-bound: without a trusted device there is nothing to ask.
            return Verdict(Decision.DENY, "restricted_requires_trusted_device")
        if decision is Decision.ALLOW:
            return Verdict(Decision.ALLOW, rule)
        return Verdict(Decision.ASK, rule, required_strength=strength)
