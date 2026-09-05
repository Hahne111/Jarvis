"""CoreRuntime: wires the Core 0.1 modules into one process (ADR-0002 modular monolith).

EventBus(SQLEventStore) -> MissionEngine, PermissionEngine, CapabilityRegistry(+mocks),
ExecutionGateway, VerifierRegistry(+mocks), VerificationService, VerifiedExecutor, IntentRouter,
ModelRouter + IntelligenceProviders -> AgentCoordinator

Provider selection (env JARVIS_PROVIDER): "claude" (default; needs the anthropic SDK installed),
"mock" (offline scripted provider for tests/dev), "none" (agent path reports BLOCKED).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from core import __version__
from core.agents import AgentCoordinator
from core.capabilities import CapabilityRegistry, ExecutionGateway, register_mocks
from core.events import EventBus, SQLEventStore
from core.intents.router import IntentRouter
from core.missions import MissionEngine, MissionRepository
from core.models import (
    ClaudeProvider,
    IntelligenceProvider,
    MockProvider,
    ModelRouter,
    ModelSpec,
    Tier,
)
from core.permissions import PermissionEngine, Policy
from core.verifier import (
    VerificationService,
    VerifiedExecutor,
    VerifierRegistry,
    register_mock_verifiers,
)

DEFAULT_DB_URL = "sqlite:///jarvis/data/core.db"


@dataclass
class CoreRuntime:
    store: SQLEventStore
    bus: EventBus
    missions: MissionEngine
    permissions: PermissionEngine
    capabilities: CapabilityRegistry
    gateway: ExecutionGateway
    verifiers: VerifierRegistry
    verification: VerificationService
    executor: VerifiedExecutor
    intents: IntentRouter
    router: ModelRouter
    providers: dict[str, IntelligenceProvider]
    coordinator: AgentCoordinator
    db_url: str
    version: str = __version__
    # decision_id -> pending command (mission + call) waiting for approval
    pending_commands: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        db_url: str | None = None,
        policy: Policy | None = None,
        *,
        provider: str | None = None,
        providers: dict[str, IntelligenceProvider] | None = None,
        router: ModelRouter | None = None,
    ) -> CoreRuntime:
        url = db_url or os.environ.get("JARVIS_CORE_DB_URL", DEFAULT_DB_URL)
        if url.startswith("sqlite:///") and not url.endswith(":memory:"):
            os.makedirs(os.path.dirname(url.removeprefix("sqlite:///")) or ".", exist_ok=True)
        store = SQLEventStore(url)
        bus = EventBus(store)
        missions = MissionEngine(bus, MissionRepository(engine=store.engine))
        permissions = PermissionEngine(bus, policy)
        capabilities = register_mocks(CapabilityRegistry())
        gateway = ExecutionGateway(capabilities, permissions, bus)
        verifiers = register_mock_verifiers(VerifierRegistry())
        verification = VerificationService(verifiers, capabilities, bus)
        executor = VerifiedExecutor(gateway, verification, capabilities)
        router = router if router is not None else ModelRouter()
        if providers is None:
            providers, router = _default_providers(
                provider or os.environ.get("JARVIS_PROVIDER"), router
            )
        coordinator = AgentCoordinator(
            bus=bus,
            executor=executor,
            capabilities=capabilities,
            router=router,
            providers=providers,
            permissions=permissions,
        )
        return cls(
            store=store,
            bus=bus,
            missions=missions,
            permissions=permissions,
            capabilities=capabilities,
            gateway=gateway,
            verifiers=verifiers,
            verification=verification,
            executor=executor,
            intents=IntentRouter(capabilities),
            router=router,
            providers=providers,
            coordinator=coordinator,
            db_url=url,
        )

    def recover(self) -> dict[str, int]:
        """After a restart: rebuild permission state from the log; missions load from snapshots."""
        return {
            "permissions": self.permissions.rebuild_from_log(),
            "missions": len(self.missions.list()),
            "events": self.store.count(),
        }

    def health(self) -> dict[str, Any]:
        return {
            "status": "halted" if self.gateway.halted else "ok",
            "version": self.version,
            "db_url": _redact(self.db_url),
            "events": self.store.count(),
            "last_seq": self.store.last_seq(),
            "halted": self.gateway.halted,
            "pending_approvals": len(self.permissions.pending()),
            "providers": {n: p.available() for n, p in self.providers.items()},
            "agent_ready": self.coordinator.can_run(),
            "capabilities": self.capabilities.health(),
        }


def _default_providers(
    choice: str | None, router: ModelRouter
) -> tuple[dict[str, IntelligenceProvider], ModelRouter]:
    choice = (choice or "claude").lower()
    if choice == "none":
        return {}, router
    if choice == "mock":
        # Offline development: a scripted provider behind a "mock" model spec.
        router = ModelRouter(
            [ModelSpec("mock-model", "mock", Tier.FRONTIER, supports_effort=False)]
        )
        return {"mock": MockProvider()}, router
    if choice == "claude":
        return {"claude": ClaudeProvider()}, router
    raise ValueError(f"unknown JARVIS_PROVIDER {choice!r} (claude | mock | none)")


def _redact(url: str) -> str:
    """Never expose credentials from a DATABASE_URL in health output."""
    if "@" in url and "://" in url:
        scheme, rest = url.split("://", 1)
        return f"{scheme}://***@{rest.split('@', 1)[1]}"
    return url
