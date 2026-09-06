"""Execution Gateway (SPEC §5.1): the only path from a decision to a real capability call.

    invoke()  ->  halted?  ->  manifest/inputs/requirements  ->  PermissionEngine
              ->  ALLOW: run with timeout/retries, consume grant, record health, emit events
              ->  ASK:   return awaiting_approval (caller parks the mission, resumes later)
              ->  DENY:  never runs

Kill switch: ``halt()`` stops every invocation immediately (P0 included) and can only be released
with a strong approval proof from a trusted device (SECURITY.md §2 rule 7).
"""

from __future__ import annotations

import asyncio
import inspect
import uuid
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from core.capabilities.manifest import CapabilityInputError
from core.capabilities.registry import CapabilityRegistry
from core.events.bus import EventBus
from core.events.envelope import DEFAULT_USER_ID, Event, Priority
from core.permissions.engine import PermissionEngine
from core.permissions.model import (
    ApprovalError,
    ApprovalProof,
    ApprovalStrength,
    Decision,
    PermissionRequest,
)

SOURCE = "execution-gateway"

# Handlers can read the correlation id (mission) of the invocation that is running them.
current_correlation_id: ContextVar[str | None] = ContextVar("jarvis_correlation_id", default=None)
current_actor: ContextVar[str | None] = ContextVar("jarvis_actor", default=None)
current_user_id: ContextVar[str] = ContextVar("jarvis_user_id", default="local-owner")
current_device_id: ContextVar[str | None] = ContextVar("jarvis_device_id", default=None)


class InvocationStatus(StrEnum):
    SUCCEEDED = (
        "succeeded"  # the tool ran and returned - NOT "goal achieved" (that is the Verifier)
    )
    FAILED = "failed"
    TIMEOUT = "timeout"
    DENIED = "denied"
    AWAITING_APPROVAL = "awaiting_approval"
    HALTED = "halted"
    INVALID = "invalid"


class GatewayHalted(RuntimeError):
    pass


@dataclass
class Invocation:
    capability: str
    args: dict[str, Any]
    actor: str
    correlation_id: str
    status: InvocationStatus = InvocationStatus.INVALID
    invocation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    decision_id: str | None = None
    rule: str | None = None
    result: Any = None
    error: str | None = None
    attempts: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def ok(self) -> bool:
        return self.status is InvocationStatus.SUCCEEDED

    @property
    def duration_ms(self) -> int | None:
        if self.started_at and self.finished_at:
            return int((self.finished_at - self.started_at).total_seconds() * 1000)
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "invocation_id": self.invocation_id,
            "capability": self.capability,
            "args": self.args,
            "actor": self.actor,
            "correlation_id": self.correlation_id,
            "status": self.status.value,
            "decision_id": self.decision_id,
            "rule": self.rule,
            "result": self.result,
            "error": self.error,
            "attempts": self.attempts,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_ms": self.duration_ms,
        }


class ExecutionGateway:
    def __init__(
        self,
        registry: CapabilityRegistry,
        permissions: PermissionEngine,
        bus: EventBus,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._registry = registry
        self._permissions = permissions
        self._bus = bus
        self._clock = clock or (lambda: datetime.now(UTC))
        self._halted: str | None = None

    # -- kill switch ---------------------------------------------------------------------------

    @property
    def halted(self) -> bool:
        return self._halted is not None

    async def halt(self, reason: str = "kill switch") -> None:
        """Stop everything. Idempotent; voice ("stop everything"), UI or hardware may call it."""
        if self._halted is None:
            self._halted = reason
            await self._emit("gateway.halted", {"reason": reason}, priority=Priority.CRITICAL)

    async def resume(self, proof: ApprovalProof) -> None:
        """Release the kill switch. Needs a strong proof from a trusted device, never an agent."""
        if proof.strength < ApprovalStrength.STRONG or not proof.device_trusted:
            raise ApprovalError("resuming requires a strong approval from a trusted device")
        if self._halted is not None:
            self._halted = None
            await self._emit(
                "gateway.resumed", {"proof": proof.to_dict()}, priority=Priority.URGENT
            )

    # -- invocation ----------------------------------------------------------------------------

    async def invoke(
        self,
        capability: str,
        args: dict[str, Any] | None = None,
        *,
        actor: str,
        correlation_id: str,
        user_id: str = DEFAULT_USER_ID,
        device_id: str | None = None,
        device_trusted: bool = False,
        decision_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> Invocation:
        inv = Invocation(
            capability, dict(args or {}), actor, correlation_id, decision_id=decision_id
        )
        env = {"user_id": user_id, "device_id": device_id}

        if self._halted is not None:
            return await self._finish(inv, InvocationStatus.HALTED, env, error=self._halted)

        if capability not in self._registry:
            return await self._finish(
                inv, InvocationStatus.INVALID, env, error="unknown capability"
            )
        cap = self._registry.get(capability)
        manifest = cap.manifest
        try:
            inv.args = manifest.validate_inputs(inv.args)
        except CapabilityInputError as exc:
            return await self._finish(inv, InvocationStatus.INVALID, env, error=str(exc))

        facts = {
            "device.trusted": bool(device_trusted),
            "network.online": bool((context or {}).get("network.online", False)),
            "owner.present": bool((context or {}).get("owner.present", False)),
        }
        unmet = [r for r in manifest.requires if not facts.get(r, False)]
        if unmet:
            inv.rule = "requires:" + ",".join(unmet)
            return await self._finish(inv, InvocationStatus.DENIED, env, error="unmet requirements")

        # -- permission -------------------------------------------------------------------------
        if decision_id is not None:
            # Resuming after an approval: the grant must be active, unused and for this exact call.
            try:
                decision = self._permissions.get(decision_id)
            except ApprovalError:
                inv.rule = "grant:unknown"
                return await self._finish(
                    inv, InvocationStatus.DENIED, env, error="unknown decision"
                )
            same_call = (
                decision.request.action == capability
                and decision.request.correlation_id == correlation_id
                and decision.request.context.get("args") == inv.args
            )
            if not same_call or not self._permissions.is_granted(decision_id):
                inv.rule = "grant:mismatch_or_inactive"
                return await self._finish(
                    inv, InvocationStatus.DENIED, env, error="grant not valid"
                )
        else:
            request = PermissionRequest(
                action=capability,
                risk=manifest.risk,
                actor=actor,
                correlation_id=correlation_id,
                user_id=user_id,
                device_id=device_id,
                device_trusted=device_trusted,
                context={"args": inv.args, "side_effects": manifest.side_effects},
            )
            decision = await self._permissions.evaluate(request)
            inv.decision_id = decision.decision_id
            inv.rule = decision.rule
            if decision.decision is Decision.DENY:
                return await self._finish(inv, InvocationStatus.DENIED, env, error=decision.reason)
            if decision.decision is Decision.ASK:
                return await self._finish(inv, InvocationStatus.AWAITING_APPROVAL, env)

        inv.rule = decision.rule
        await self._permissions.consume(decision.decision_id)  # single-use grant

        # -- execution --------------------------------------------------------------------------
        inv.started_at = self._clock()
        await self._emit(
            "capability.invoked",
            {"invocation": inv.to_dict(), "manifest_version": manifest.version},
            correlation_id=correlation_id,
            **env,
        )
        timeout_s = manifest.timeout_ms / 1000
        status = InvocationStatus.FAILED
        tok_c = current_correlation_id.set(correlation_id)
        tok_a = current_actor.set(actor)
        tok_u = current_user_id.set(user_id)
        tok_d = current_device_id.set(device_id)
        for attempt in range(1, manifest.retries + 2):
            inv.attempts = attempt
            try:
                result = cap.handler(inv.args)
                if inspect.isawaitable(result):
                    result = await asyncio.wait_for(result, timeout=timeout_s)
                inv.result, inv.error = result, None
                status = InvocationStatus.SUCCEEDED
                break
            except TimeoutError:
                inv.error = f"timeout after {manifest.timeout_ms} ms"
                status = InvocationStatus.TIMEOUT
            except Exception as exc:  # isolate tool failures; the gateway itself never crashes
                inv.error = f"{type(exc).__name__}: {exc}"
                status = InvocationStatus.FAILED
        current_correlation_id.reset(tok_c)
        current_actor.reset(tok_a)
        current_user_id.reset(tok_u)
        current_device_id.reset(tok_d)
        cap.health.record(status is InvocationStatus.SUCCEEDED, inv.error)
        return await self._finish(inv, status, env)

    # -- internals -----------------------------------------------------------------------------

    async def _finish(
        self,
        inv: Invocation,
        status: InvocationStatus,
        env: dict[str, Any],
        *,
        error: str | None = None,
    ) -> Invocation:
        inv.status = status
        if error is not None:
            inv.error = error
        inv.finished_at = self._clock()
        await self._emit(
            f"capability.{status.value}",
            {"invocation": inv.to_dict()},
            correlation_id=inv.correlation_id,
            **env,
        )
        return inv

    async def _emit(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        correlation_id: str | None = None,
        user_id: str = DEFAULT_USER_ID,
        device_id: str | None = None,
        priority: Priority = Priority.NORMAL,
    ) -> None:
        await self._bus.publish(
            Event.new(
                event_type,
                SOURCE,
                payload,
                correlation_id=correlation_id,
                user_id=user_id,
                device_id=device_id,
                priority=priority,
            )
        )
