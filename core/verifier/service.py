"""VerifierRegistry, VerificationService and the retry policy (Phase 2 step 14, Commit 008)."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from core.capabilities.gateway import ExecutionGateway, Invocation, InvocationStatus
from core.capabilities.registry import CapabilityRegistry
from core.events.bus import EventBus
from core.events.envelope import Event
from core.verifier.model import EVENT_FOR_OUTCOME, Outcome, Verification

SOURCE = "verifier"

# A verifier receives the finished invocation and returns an Outcome, or (Outcome, evidence dict).
VerifierFn = Callable[[Invocation], Any | Awaitable[Any]]


class VerifierNotFound(KeyError):
    pass


class VerifierRegistry:
    def __init__(self) -> None:
        self._fns: dict[str, VerifierFn] = {}

    def register(self, name: str, fn: VerifierFn) -> None:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("verifier name must be a non-empty string")
        if not callable(fn):
            raise TypeError("verifier must be callable")
        if name in self._fns:
            raise ValueError(f"verifier {name!r} already registered")
        self._fns[name] = fn

    def get(self, name: str) -> VerifierFn:
        try:
            return self._fns[name]
        except KeyError:
            raise VerifierNotFound(name) from None

    def names(self) -> list[str]:
        return sorted(self._fns)

    def __contains__(self, name: object) -> bool:
        return name in self._fns


class VerificationService:
    def __init__(
        self,
        verifiers: VerifierRegistry,
        capabilities: CapabilityRegistry,
        bus: EventBus,
    ) -> None:
        self._verifiers = verifiers
        self._caps = capabilities
        self._bus = bus

    async def verify(self, invocation: Invocation) -> Verification:
        cap_name = invocation.capability
        if invocation.status is not InvocationStatus.SUCCEEDED:
            return await self._record(
                invocation,
                Outcome.SKIPPED,
                reason=f"invocation {invocation.status.value}, nothing to verify",
            )
        if cap_name not in self._caps:
            return await self._record(invocation, Outcome.UNKNOWN, reason="unknown capability")
        manifest = self._caps.get(cap_name).manifest
        if manifest.verifier is None:
            return await self._record(
                invocation, Outcome.SKIPPED, reason="no side effects declared"
            )
        if manifest.verifier not in self._verifiers:
            return await self._record(
                invocation,
                Outcome.UNKNOWN,
                verifier=manifest.verifier,
                reason=f"verifier {manifest.verifier!r} is not registered",
            )
        fn = self._verifiers.get(manifest.verifier)
        try:
            result = fn(invocation)
            if inspect.isawaitable(result):
                result = await asyncio.wait_for(result, timeout=manifest.timeout_ms / 1000)
        except TimeoutError:
            return await self._record(
                invocation, Outcome.UNKNOWN, verifier=manifest.verifier, reason="verifier timed out"
            )
        except Exception as exc:  # a broken verifier must never turn into a "success"
            return await self._record(
                invocation,
                Outcome.UNKNOWN,
                verifier=manifest.verifier,
                reason=f"verifier raised {type(exc).__name__}: {exc}",
            )
        outcome, evidence = _normalise(result)
        return await self._record(
            invocation, outcome, verifier=manifest.verifier, evidence=evidence
        )

    async def _record(
        self,
        invocation: Invocation,
        outcome: Outcome,
        *,
        verifier: str | None = None,
        evidence: dict[str, Any] | None = None,
        reason: str | None = None,
    ) -> Verification:
        v = Verification(
            capability=invocation.capability,
            invocation_id=invocation.invocation_id,
            outcome=outcome,
            verifier=verifier,
            evidence=dict(evidence or {}),
            reason=reason,
        )
        await self._bus.publish(
            Event.new(
                EVENT_FOR_OUTCOME[outcome],
                SOURCE,
                {"verification": v.to_dict()},
                correlation_id=invocation.correlation_id,
            )
        )
        return v


def _normalise(result: Any) -> tuple[Outcome, dict[str, Any]]:
    if isinstance(result, tuple) and len(result) == 2:
        outcome, evidence = result
        return Outcome(outcome), dict(evidence or {})
    if isinstance(result, bool):
        return (Outcome.ACHIEVED if result else Outcome.NOT_ACHIEVED), {}
    return Outcome(result), {}


@dataclass(frozen=True)
class RetryPolicy:
    """Retry only when the tool ran, the verifier says the goal was *not* reached, and the
    manifest allows it.

    UNKNOWN never triggers a retry (a blind retry of a side effect is worse than reporting it).
    Tool failures/timeouts are already retried inside the ExecutionGateway (same manifest
    budget), so the executor never doubles that; denied/awaiting/halted is never retried.
    """

    max_extra_attempts: int | None = None  # None -> use manifest.retries

    def should_retry(
        self,
        invocation: Invocation,
        verification: Verification,
        attempt: int,
        manifest_retries: int,
    ) -> bool:
        limit = self.max_extra_attempts if self.max_extra_attempts is not None else manifest_retries
        if attempt > limit:
            return False
        return (
            invocation.status is InvocationStatus.SUCCEEDED
            and verification.outcome is Outcome.NOT_ACHIEVED
        )


@dataclass(frozen=True)
class VerifiedResult:
    invocation: Invocation
    verification: Verification
    attempts: int

    @property
    def ok(self) -> bool:
        """True only when the tool ran AND the goal is verified (or there was nothing to verify)."""
        return (
            self.invocation.status is InvocationStatus.SUCCEEDED
            and self.verification.outcome
            in (
                Outcome.ACHIEVED,
                Outcome.SKIPPED,
            )
        )


class VerifiedExecutor:
    """invoke -> verify -> (retry per policy) -> VerifiedResult. The only honest 'done'."""

    def __init__(
        self,
        gateway: ExecutionGateway,
        verification: VerificationService,
        capabilities: CapabilityRegistry,
        policy: RetryPolicy | None = None,
    ) -> None:
        self._gateway = gateway
        self._verification = verification
        self._caps = capabilities
        self._policy = policy or RetryPolicy()

    async def run(
        self, capability: str, args: dict[str, Any] | None = None, **kw: Any
    ) -> VerifiedResult:
        manifest_retries = (
            self._caps.get(capability).manifest.retries if capability in self._caps else 0
        )
        attempt = 0
        while True:
            attempt += 1
            invocation = await self._gateway.invoke(capability, args, **kw)
            verification = await self._verification.verify(invocation)
            if not self._policy.should_retry(invocation, verification, attempt, manifest_retries):
                return VerifiedResult(invocation, verification, attempt)
            kw.pop("decision_id", None)  # a grant is single-use; a retry needs a fresh decision
