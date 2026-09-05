"""PermissionEngine: deterministic gate in front of every side effect (SPEC §5.1, SECURITY.md).

Flow:  evaluate(request) -> ALLOW (time-limited grant) | ASK (pending approval) | DENY
       approve(decision_id, proof) -> ALLOW grant, only with sufficient, unexpired proof
       deny(decision_id)           -> DENY
       is_granted(decision_id)     -> True only for an unexpired ALLOW (used by the gateway)

Every decision is an event on the bus (``permission.allowed|ask|approved|denied|consumed``),
correlated with the request (normally the mission). Pending approvals and grants are rebuilt
from the log after a restart; nothing here is secret - proofs carry references, never credentials.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from core.events.bus import EventBus
from core.events.envelope import Event
from core.permissions.model import (
    ApprovalError,
    ApprovalProof,
    ApprovalStrength,
    Decision,
    PermissionDecision,
    PermissionRequest,
    RiskLevel,
)
from core.permissions.policy import Policy

SOURCE = "permission-engine"


class PermissionEngine:
    def __init__(
        self,
        bus: EventBus,
        policy: Policy | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._bus = bus
        self._policy = policy or Policy()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._decisions: dict[str, PermissionDecision] = {}

    @property
    def policy(self) -> Policy:
        return self._policy

    def tighten(self, **changes: Any) -> Policy:
        """Replace the policy with a stricter one. Loosening raises PolicyViolation."""
        self._policy = self._policy.stricter(**changes)
        return self._policy

    # -- queries -------------------------------------------------------------------------------

    def get(self, decision_id: str) -> PermissionDecision:
        try:
            return self._decisions[decision_id]
        except KeyError:
            raise ApprovalError(f"unknown decision {decision_id}") from None

    def pending(self) -> list[PermissionDecision]:
        now = self._clock()
        return [
            d
            for d in self._decisions.values()
            if d.decision is Decision.ASK and not d.is_expired(now)
        ]

    def is_granted(self, decision_id: str) -> bool:
        d = self._decisions.get(decision_id)
        return (
            d is not None
            and d.decision is Decision.ALLOW
            and d.used_at is None
            and not d.is_expired(self._clock())
        )

    # -- commands ------------------------------------------------------------------------------

    async def evaluate(self, request: PermissionRequest) -> PermissionDecision:
        verdict = self._policy.evaluate(request)
        now = self._clock()
        decision = PermissionDecision(
            request=request,
            decision=verdict.decision,
            rule=verdict.rule,
            required_strength=verdict.required_strength,
            created_at=now,
            reason=verdict.reason,
        )
        if verdict.decision is Decision.ALLOW:
            decision.expires_at = now + timedelta(seconds=self._policy.grant_ttl_s)
            event_type = "permission.allowed"
        elif verdict.decision is Decision.ASK:
            ttl = self._policy.ask_ttl_s[verdict.required_strength]
            decision.expires_at = now + timedelta(seconds=ttl)
            event_type = "permission.ask"
        else:
            event_type = "permission.denied"
        await self._record(decision, event_type)
        return decision

    async def approve(self, decision_id: str, proof: ApprovalProof) -> PermissionDecision:
        d = self.get(decision_id)
        now = self._clock()
        if d.decision is not Decision.ASK:
            raise ApprovalError(f"decision {decision_id} is {d.decision.value}, not pending")
        if d.is_expired(now):
            await self._expire(d, now)
            raise ApprovalError(f"approval window for {decision_id} has expired")
        if proof.strength < d.required_strength:
            raise ApprovalError(
                f"proof {proof.method.value} ({proof.strength.name}) is weaker than required "
                f"{d.required_strength.name}"
            )
        if proof.strength is ApprovalStrength.VOICE:
            raise ApprovalError("voice is never an authenticator")  # defence in depth
        if d.request.risk >= RiskLevel.P4 and not proof.device_trusted:
            raise ApprovalError("P4+/P5 approvals require an unlocked trusted device")
        d.decision = Decision.ALLOW
        d.approval_proof = proof
        d.rule = f"{d.rule}+approved:{proof.method.value}"
        d.expires_at = now + timedelta(seconds=self._policy.grant_ttl_s)
        await self._record(d, "permission.approved")
        return d

    async def deny(self, decision_id: str, reason: str | None = None) -> PermissionDecision:
        d = self.get(decision_id)
        if d.decision is not Decision.ASK:
            raise ApprovalError(f"decision {decision_id} is {d.decision.value}, not pending")
        d.decision = Decision.DENY
        d.rule = f"{d.rule}+denied"
        d.reason = reason
        d.expires_at = None
        await self._record(d, "permission.denied")
        return d

    async def consume(self, decision_id: str) -> PermissionDecision:
        """Mark an ALLOW grant as used (single-use). Only the Execution Gateway calls this."""
        if not self.is_granted(decision_id):
            raise ApprovalError(f"decision {decision_id} is not an active grant")
        d = self.get(decision_id)
        d.used_at = self._clock()
        await self._record(d, "permission.consumed")
        return d

    async def expire_stale(self) -> int:
        """Turn timed-out ASKs into DENY (audit trail). Returns how many expired."""
        now = self._clock()
        stale = [
            d for d in self._decisions.values() if d.decision is Decision.ASK and d.is_expired(now)
        ]
        for d in stale:
            await self._expire(d, now)
        return len(stale)

    # -- recovery ------------------------------------------------------------------------------

    def rebuild_from_log(self) -> int:
        self._decisions.clear()
        for _, event in self._bus.replay(type_prefix="permission"):
            self._apply(event)
        return len(self._decisions)

    def _apply(self, event: Event) -> None:
        payload = event.payload.get("decision")
        if not payload:
            return
        d = PermissionDecision.from_dict(payload)
        self._decisions[d.decision_id] = d  # last event for a decision_id wins (append order)

    # -- internals -----------------------------------------------------------------------------

    async def _expire(self, d: PermissionDecision, now: datetime) -> None:
        d.decision = Decision.DENY
        d.rule = f"{d.rule}+expired"
        d.reason = "approval window expired"
        d.expires_at = None
        await self._record(d, "permission.denied")

    async def _record(self, d: PermissionDecision, event_type: str) -> None:
        self._decisions[d.decision_id] = d
        await self._bus.publish(
            Event.new(
                event_type,
                SOURCE,
                {"decision": d.to_dict()},
                correlation_id=d.request.correlation_id,
                user_id=d.request.user_id,
                device_id=d.request.device_id,
            )
        )
