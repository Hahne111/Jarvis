"""Presence Service (SPEC §10.5, Phase 6 step 37): what JARVIS is doing, per device, right now.

Presence is *derived* deterministically from persisted events - never invented by the UI
(SECURITY.md §3). It is rebuilt from the log on start and republished as ``presence.changed``
whenever it changes, so HUD, mobile and voice satellites all show the same state.

    idle | listening | thinking | speaking | working | awaiting_approval | halted
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from core.events.bus import EventBus
from core.events.envelope import Event, Priority

SOURCE = "presence"
CORE_DEVICE = "core"


class PresenceState(StrEnum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    WORKING = "working"
    AWAITING_APPROVAL = "awaiting_approval"
    HALTED = "halted"


_RELEVANT = frozenset(
    {
        "gateway.halted",
        "gateway.resumed",
        "agent.run.started",
        "agent.run.paused",
        "agent.run.resumed",
        "agent.run.finished",
        "permission.ask",
        "permission.approved",
        "permission.denied",
    }
)

_VOICE = {
    "voice.wake_ack": PresenceState.LISTENING,
    "voice.listening": PresenceState.LISTENING,
    "voice.thinking": PresenceState.THINKING,
    "voice.speaking": PresenceState.SPEAKING,
    "voice.follow_up": PresenceState.LISTENING,
    "voice.idle": PresenceState.IDLE,
}


@dataclass
class DevicePresence:
    device_id: str
    state: PresenceState = PresenceState.IDLE
    since: datetime | None = None
    active_mission: str | None = None
    pending_approvals: set[str] = field(default_factory=set)
    running_runs: set[str] = field(default_factory=set)

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "state": self.state.value,
            "since": self.since.isoformat() if self.since else None,
            "active_mission": self.active_mission,
            "pending_approvals": sorted(self.pending_approvals),
            "running_runs": len(self.running_runs),
        }


class PresenceService:
    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self._devices: dict[str, DevicePresence] = {}
        self._halted = False
        self._sub = bus.subscribe("*", self._on_event)

    # -- queries -------------------------------------------------------------------------------

    @property
    def halted(self) -> bool:
        return self._halted

    def get(self, device_id: str | None = None) -> DevicePresence:
        return self._devices.get(device_id or CORE_DEVICE, DevicePresence(device_id or CORE_DEVICE))

    def snapshot(self) -> dict[str, Any]:
        return {
            "halted": self._halted,
            "devices": {d: p.to_dict() for d, p in sorted(self._devices.items())},
        }

    def rebuild(self) -> dict[str, Any]:
        """Recompute from the log without publishing (start-up / consistency check)."""
        self._devices.clear()
        self._halted = False
        for _, ev in self._bus.replay():
            self._apply(ev, publish=False)
        return self.snapshot()

    # -- derivation ------------------------------------------------------------------------------

    async def _on_event(self, ev: Event) -> None:
        change = self._apply(ev, publish=True)
        if change is not None:
            await self._publish(*change)

    def _apply(self, ev: Event, *, publish: bool) -> tuple[DevicePresence, PresenceState] | None:
        t = ev.type
        if t not in _RELEVANT and t not in _VOICE:
            return None
        dev = self._devices.setdefault(
            ev.device_id or CORE_DEVICE, DevicePresence(ev.device_id or CORE_DEVICE)
        )
        before = dev.state
        if t == "gateway.halted":
            self._halted = True
        elif t == "gateway.resumed":
            self._halted = False
        elif t in _VOICE:
            dev.state = _VOICE[t]
            if t == "voice.idle":
                dev.active_mission = None
        elif t == "agent.run.started":
            dev.running_runs.add(ev.payload["run"]["run_id"])
            dev.active_mission = ev.correlation_id
            dev.state = PresenceState.WORKING
        elif t == "agent.run.paused":
            pass  # a paused run is still this device's active work (awaiting the owner)
        elif t == "agent.run.resumed":
            dev.running_runs.add(ev.payload["run"]["run_id"])
            dev.state = PresenceState.WORKING
        elif t == "agent.run.finished":
            dev.running_runs.discard(ev.payload["run"]["run_id"])
            if not dev.running_runs and dev.state is PresenceState.WORKING:
                dev.state = PresenceState.IDLE
                dev.active_mission = None
        elif t == "permission.ask":
            dev.pending_approvals.add(ev.payload["decision"]["decision_id"])
            dev.state = PresenceState.AWAITING_APPROVAL
        elif t in ("permission.approved", "permission.denied"):
            dev.pending_approvals.discard(ev.payload["decision"]["decision_id"])
            if not dev.pending_approvals and dev.state is PresenceState.AWAITING_APPROVAL:
                dev.state = PresenceState.WORKING if dev.running_runs else PresenceState.IDLE
        else:
            return None
        effective = PresenceState.HALTED if self._halted else dev.state
        if t in ("gateway.halted", "gateway.resumed"):
            # Halt/resume affects every device; one summary event carries the global flag.
            for d in self._devices.values():
                d.since = ev.timestamp
            return dev, effective
        was = PresenceState.HALTED if self._halted else before
        if effective != was or dev.since is None:
            dev.since = ev.timestamp
            return dev, effective
        return None

    async def _publish(self, dev: DevicePresence, effective: PresenceState) -> None:
        await self._bus.publish(
            Event.new(
                "presence.changed",
                SOURCE,
                {**dev.to_dict(), "state": effective.value, "halted": self._halted},
                correlation_id=dev.active_mission or "presence",
                device_id=dev.device_id,
                priority=Priority.URGENT if effective is PresenceState.HALTED else Priority.NORMAL,
            )
        )
