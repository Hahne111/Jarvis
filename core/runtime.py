"""CoreRuntime: wires the Core 0.1 modules into one process (ADR-0002 modular monolith).

EventBus(SQLEventStore) -> MissionEngine, PermissionEngine, CapabilityRegistry(+mocks),
ExecutionGateway, VerifierRegistry(+mocks), VerificationService, VerifiedExecutor, IntentRouter,
ModelRouter + IntelligenceProviders -> AgentCoordinator

Provider selection (env JARVIS_PROVIDER): "claude" (default; needs the anthropic SDK installed),
"mock" (offline scripted provider for tests/dev), "none" (agent path reports BLOCKED).
Home capabilities (env JARVIS_HOME): "off" (default), "fake" (in-memory home), "homeassistant"
(REST; JARVIS_HA_URL + JARVIS_HA_TOKEN from the environment only).
Desktop capabilities (env JARVIS_DESKTOP): "off" (default, headless), "fake" (in-memory model),
"prototype" (real OS via the unchanged jarvis/tools functions).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from adapters.desktop import DesktopBackend, FakeDesktop, PrototypeDesktop, register_desktop
from adapters.home import (
    FakeHome,
    FakeNetwork,
    HomeAssistantBackend,
    HomeBackend,
    HomeService,
    WolService,
    default_wol_targets,
    register_home,
    register_wol,
    targets_from_env,
)
from adapters.workspace import WorkspaceManager, register_workspace

from core import __version__
from core.agents import AgentCoordinator
from core.capabilities import CapabilityRegistry, ExecutionGateway, register_mocks
from core.devices import DeviceAuthenticator, DeviceRegistry
from core.events import EventBus, SQLEventStore
from core.intents.router import IntentRouter
from core.memory import HashingEmbedder, MemoryStore, MemoryWriter
from core.memory.capabilities import register_memory_capabilities, register_memory_verifiers
from core.missions import MissionEngine, MissionRepository
from core.models import (
    ClaudeProvider,
    IntelligenceProvider,
    MockProvider,
    ModelRouter,
    ModelSpec,
    Tier,
)
from core.news import FakeNewsSource, NewsPipeline, NewsStore, register_news, sources_from_env
from core.notify import PushService, PushTransport, push_transport_from_env
from core.permissions import PermissionEngine, Policy
from core.presence import PresenceService
from core.proactive import (
    BriefBuilder,
    Context,
    HabitDetector,
    PrivacyService,
    RelevanceEngine,
    SuggestionStore,
    register_brief,
    register_privacy,
)
from core.scheduler import JobStore, Scheduler
from core.skills import SkillRegistry, make_sandbox_runner, register_skill_capabilities
from core.verifier import (
    VerificationService,
    VerifiedExecutor,
    VerifierRegistry,
    register_mock_verifiers,
)

DEFAULT_DB_URL = "sqlite:///jarvis/data/core.db"
DEFAULT_WORKSPACE_ROOT = "jarvis/data/workspaces"
DEFAULT_SKILLS_ROOT = "jarvis/data/skills"


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
    memory: MemoryStore
    memory_writer: MemoryWriter
    presence: PresenceService
    workspaces: WorkspaceManager
    home: HomeService | None
    wol: WolService | None
    devices: DeviceRegistry
    auth: DeviceAuthenticator
    notify: PushService | None
    news: NewsPipeline | None
    scheduler: Scheduler
    relevance: RelevanceEngine
    habits: HabitDetector
    privacy: PrivacyService
    brief: BriefBuilder
    skills: SkillRegistry
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
        desktop: DesktopBackend | str | None = None,
        workspace_root: str | None = None,
        skills_root: str | None = None,
        home: HomeBackend | str | None = None,
        wol: WolService | None = None,
        push: PushTransport | str | None = None,
        news: NewsPipeline | str | None = None,
    ) -> CoreRuntime:
        url = db_url or os.environ.get("JARVIS_CORE_DB_URL", DEFAULT_DB_URL)
        if url.startswith("sqlite:///") and not url.endswith(":memory:"):
            os.makedirs(os.path.dirname(url.removeprefix("sqlite:///")) or ".", exist_ok=True)
        store = SQLEventStore(url)
        bus = EventBus(store)
        missions = MissionEngine(bus, MissionRepository(engine=store.engine))
        permissions = PermissionEngine(bus, policy)
        memory = MemoryStore(engine=store.engine, embedder=HashingEmbedder())
        memory_writer = MemoryWriter(memory, bus)
        capabilities = register_memory_capabilities(
            register_mocks(CapabilityRegistry()), memory, memory_writer
        )
        gateway = ExecutionGateway(capabilities, permissions, bus)
        verifiers = register_memory_verifiers(register_mock_verifiers(VerifierRegistry()), memory)
        desktop_backend = _desktop_backend(
            desktop if desktop is not None else os.environ.get("JARVIS_DESKTOP")
        )
        if desktop_backend is not None:
            register_desktop(capabilities, verifiers, desktop_backend)
        workspaces = WorkspaceManager(
            workspace_root or os.environ.get("JARVIS_WORKSPACE_ROOT", DEFAULT_WORKSPACE_ROOT)
        )
        register_workspace(capabilities, verifiers, bus, workspaces)
        home_backend = _home_backend(home if home is not None else os.environ.get("JARVIS_HOME"))
        home_service = HomeService(home_backend, bus) if home_backend is not None else None
        if home_service is not None:
            register_home(capabilities, verifiers, home_service)
        wol_service = wol if wol is not None else _wol_service(home, bus)
        if wol_service is not None:
            register_wol(capabilities, verifiers, wol_service)
        news_pipeline = news if isinstance(news, NewsPipeline) else _news_pipeline(news, bus, store)
        if news_pipeline is not None:
            register_news(capabilities, news_pipeline)
        privacy = PrivacyService(bus, memory_writer)
        register_privacy(capabilities, verifiers, privacy)
        verification = VerificationService(verifiers, capabilities, bus)
        executor = VerifiedExecutor(gateway, verification, capabilities)
        router = router if router is not None else ModelRouter()
        if providers is None:
            providers, router = _default_providers(
                provider or os.environ.get("JARVIS_PROVIDER"), router
            )
        presence = PresenceService(bus)
        devices = DeviceRegistry(store.engine)
        auth = DeviceAuthenticator(devices)
        transport = (
            push
            if (push is not None and not isinstance(push, str))
            else push_transport_from_env(push)
        )
        notify = PushService(bus, transport) if transport is not None else None
        habits = HabitDetector(bus, SuggestionStore(store.engine))
        coordinator = AgentCoordinator(
            bus=bus,
            executor=executor,
            capabilities=capabilities,
            router=router,
            providers=providers,
            permissions=permissions,
            memory=memory,
            workspaces=workspaces,
        )
        runtime = cls(
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
            memory=memory,
            memory_writer=memory_writer,
            presence=presence,
            workspaces=workspaces,
            home=home_service,
            wol=wol_service,
            devices=devices,
            auth=auth,
            notify=notify,
            news=news_pipeline,
            scheduler=None,  # type: ignore[arg-type]
            relevance=None,  # type: ignore[arg-type]
            habits=habits,
            privacy=privacy,
            brief=None,  # type: ignore[arg-type]
            skills=None,  # type: ignore[arg-type]
            db_url=url,
        )
        runtime._skills_root = skills_root or os.environ.get(
            "JARVIS_SKILLS_ROOT", DEFAULT_SKILLS_ROOT
        )
        runtime._wire_proactive()
        return runtime

    def _wire_proactive(self) -> None:
        """Pieces that need the whole runtime: brief, relevance context, scheduler."""
        from core.api.commands import run_text_command

        self.brief = BriefBuilder(self)
        register_brief(self.capabilities, self.brief, self.bus)

        def context() -> Context:
            from datetime import datetime

            return Context(
                home_state=self.home.states.current.value if self.home else "home",
                privacy_mode=self.privacy.mode,
                hour=datetime.now().hour,
                home_country=os.environ.get("JARVIS_HOME_COUNTRY") or None,
            )

        self.relevance = RelevanceEngine(context)
        if self.notify is not None:
            self.notify.gate = self._push_gate
        self.scheduler = Scheduler(
            self.bus,
            JobStore(self.store.engine),
            self.missions,
            self.capabilities,
            run_command=lambda text, **kw: run_text_command(self, text, **kw),
            run_capability=self.executor.run,
        )
        self.scheduler.ensure_system_jobs()
        self.skills = SkillRegistry(
            self._skills_root,
            self.capabilities,
            self.verifiers,
            self.bus,
            call=self.executor.run,
            run_tests=make_sandbox_runner(self.workspaces),
        )
        register_skill_capabilities(self.capabilities, self.verifiers, self.skills)
        self.skills.restore()

    def _push_gate(self, ev: Any) -> bool:
        """Relevance Engine decides what may reach the phone; privacy narrows it further."""
        a = self.relevance.assess(ev)
        if self.privacy.state.only_critical_push:
            return a.channel == "now" and a.urgency >= 0.9
        return self.relevance.pushable(a)

    def recover(self) -> dict[str, int]:
        """After a restart: rebuild permission state from the log; missions load from snapshots."""
        return {
            "permissions": self.permissions.rebuild_from_log(),
            "missions": len(self.missions.list()),
            "events": self.store.count(),
            "session_memory_dropped": self._drop_session_memory(),
            "presence_devices": len(self.presence.rebuild()["devices"]),
            "home_state": self._rebuild_home(),
            "privacy": self.privacy.rebuild(),
        }

    def _rebuild_home(self) -> str | None:
        if self.home is None:
            return None
        self.home.rebuild()
        return self.home.states.current.value

    def _drop_session_memory(self) -> int:
        import asyncio

        return asyncio.run(self.memory_writer.drop_session_memory())

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
            "memory_items": self.memory.count(),
            "presence": self.presence.snapshot(),
            "home": self.home.states.current.value if self.home else None,
            "devices": self.devices.count(),
            "push": self.notify.transport.name if self.notify else None,
            "news": self.news.store.count() if self.news else None,
            "privacy": self.privacy.mode,
            "skills": len(self.skills.list()),
            "scheduler": {
                "running": self.scheduler.snapshot()["running"],
                "jobs": len(self.scheduler.store.list()),
            },
            "capabilities": self.capabilities.health(),
        }


def _desktop_backend(choice: DesktopBackend | str | None) -> DesktopBackend | None:
    if choice is None or (isinstance(choice, str) and choice.lower() in ("", "off", "0", "false")):
        return None
    if not isinstance(choice, str):
        return choice
    if choice.lower() == "fake":
        return FakeDesktop()
    if choice.lower() == "prototype":
        return PrototypeDesktop()
    raise ValueError(f"unknown JARVIS_DESKTOP {choice!r} (off | fake | prototype)")


def _wol_service(home_choice: HomeBackend | str | None, bus: EventBus) -> WolService | None:
    """WOL targets from JARVIS_WOL_TARGETS (real network); JARVIS_HOME=fake gets a demo target
    on a fake network so the HUD/tests can exercise power.wake without touching the LAN."""
    targets = targets_from_env()
    if targets:
        return WolService(targets, bus)
    choice = home_choice if home_choice is not None else os.environ.get("JARVIS_HOME")
    if isinstance(choice, str) and choice.lower() == "fake":
        demo = default_wol_targets()
        net = FakeNetwork(mac_to_host={t.mac.lower(): t.host for t in demo})
        return WolService(demo, bus, net, verify_timeout_s=2.0)
    return None


def _news_pipeline(choice: str | None, bus: EventBus, store: SQLEventStore) -> NewsPipeline | None:
    """JARVIS_NEWS: off (default) | fake (demo stories) | rss (JARVIS_NEWS_FEEDS)."""
    c = (choice if choice is not None else os.environ.get("JARVIS_NEWS") or "off").lower()
    if c in ("", "off", "0", "false"):
        return None
    if c == "fake":
        from core.news.demo import demo_items

        return NewsPipeline(bus, NewsStore(store.engine), [FakeNewsSource("demo", demo_items())])
    if c == "rss":
        return NewsPipeline(bus, NewsStore(store.engine), list(sources_from_env()))
    raise ValueError(f"unknown JARVIS_NEWS {choice!r} (off | fake | rss)")


def _home_backend(choice: HomeBackend | str | None) -> HomeBackend | None:
    if choice is None or (isinstance(choice, str) and choice.lower() in ("", "off", "0", "false")):
        return None
    if not isinstance(choice, str):
        return choice
    if choice.lower() == "fake":
        return FakeHome()
    if choice.lower() in ("homeassistant", "ha"):
        return HomeAssistantBackend()
    raise ValueError(f"unknown JARVIS_HOME {choice!r} (off | fake | homeassistant)")


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
