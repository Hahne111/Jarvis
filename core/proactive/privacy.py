"""Privacy / Guest modes (SPEC §8.4, §14, Phase 11 step 75).

    normal   learn, listen, notify as configured
    private  no learning from observation, satellites paused, only critical pushes
    guest    private + no conversation memory at all (nothing written while guests are here)

The mode is a persisted event (``privacy.changed``) rebuilt on start; it only ever *tightens*
what JARVIS does. Turning it off again is an owner action (P2 capability through the gate).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from core.capabilities.gateway import Invocation, current_actor
from core.capabilities.manifest import CapabilityInputError, CapabilityManifest
from core.capabilities.registry import CapabilityRegistry
from core.events.bus import EventBus
from core.events.envelope import Event
from core.memory.writer import MemoryWriter
from core.permissions.model import RiskLevel
from core.verifier.model import Outcome
from core.verifier.service import VerifierRegistry

SOURCE = "privacy"
MODES = ("normal", "private", "guest")

PRIVACY_MANIFESTS: tuple[CapabilityManifest, ...] = (
    CapabilityManifest(
        name="privacy.get",
        version="1.0",
        risk=RiskLevel.P0,
        inputs={},
        description="Current privacy mode (normal, private, guest) and what it suppresses.",
    ),
    CapabilityManifest(
        name="privacy.set",
        version="1.0",
        risk=RiskLevel.P2,
        inputs={"mode": "string"},
        side_effects=True,
        reversible=True,
        verifier="privacy.mode_is",
        description=(
            "Set the privacy mode: normal, private (no learning, satellites paused) or guest."
        ),
    ),
)


@dataclass
class PrivacyState:
    mode: str = "normal"
    changed_at: str | None = None
    baseline_learn: bool = True
    baseline_conversation: bool = True

    @property
    def learning_allowed(self) -> bool:
        return self.mode == "normal"

    @property
    def satellites_paused(self) -> bool:
        return self.mode != "normal"

    @property
    def only_critical_push(self) -> bool:
        return self.mode != "normal"

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "changed_at": self.changed_at,
            "learning": self.learning_allowed,
            "satellites_paused": self.satellites_paused,
            "only_critical_push": self.only_critical_push,
            "modes": list(MODES),
        }


class PrivacyService:
    def __init__(self, bus: EventBus, memory_writer: MemoryWriter) -> None:
        self.bus = bus
        self._writer = memory_writer
        self.state = PrivacyState(
            baseline_learn=memory_writer.policy.learn_from_observation,
            baseline_conversation=memory_writer.policy.conversation_memory,
        )

    @property
    def mode(self) -> str:
        return self.state.mode

    def apply(self, mode: str, at: str | None = None) -> tuple[str, str]:
        if mode not in MODES:
            raise CapabilityInputError(f"unknown privacy mode {mode!r}; one of {MODES}")
        old = self.state.mode
        pol = self._writer.policy
        if old == "normal" and mode != "normal":
            self.state.baseline_learn = pol.learn_from_observation
            self.state.baseline_conversation = pol.conversation_memory
        if mode == "normal":
            pol.learn_from_observation = self.state.baseline_learn
            pol.conversation_memory = self.state.baseline_conversation
        elif mode == "private":
            pol.learn_from_observation = False
            pol.conversation_memory = self.state.baseline_conversation
        else:  # guest
            pol.learn_from_observation = False
            pol.conversation_memory = False
        self.state.mode = mode
        self.state.changed_at = at
        return old, mode

    async def set(self, mode: str) -> dict[str, Any]:
        now = datetime.now(UTC).isoformat()
        old, new = self.apply(mode, at=now)
        await self.bus.publish(
            Event.new(
                "privacy.changed",
                SOURCE,
                {"from": old, "to": new, "actor": current_actor.get(), **self.state.to_dict()},
                correlation_id="privacy",
                priority="urgent" if new != "normal" else "normal",
            )
        )
        return {"from": old, "to": new, **self.state.to_dict()}

    def rebuild(self) -> str:
        rows = self.bus.replay(type_prefix="privacy.changed")
        if rows:
            last = rows[-1][1]
            self.apply(last.payload["to"], at=last.timestamp.isoformat())
        return self.state.mode


def register_privacy(
    registry: CapabilityRegistry, verifiers: VerifierRegistry, service: PrivacyService
) -> CapabilityRegistry:
    async def get(args: dict[str, Any]) -> dict[str, Any]:
        return service.state.to_dict()

    async def set_mode(args: dict[str, Any]) -> dict[str, Any]:
        return await service.set(str(args["mode"]).lower())

    registry.register(PRIVACY_MANIFESTS[0], get)
    registry.register(PRIVACY_MANIFESTS[1], set_mode)

    def mode_is(inv: Invocation) -> tuple[Outcome, dict[str, Any]]:
        wanted = str(inv.args.get("mode", "")).lower()
        return (Outcome.ACHIEVED if service.mode == wanted else Outcome.NOT_ACHIEVED), {
            "mode": service.mode
        }

    verifiers.register("privacy.mode_is", mode_is)
    return registry
