"""Push notifications (SPEC §10 Mobile Agent, Phase 9 step 60).

The PushService subscribes to the bus and forwards the few events the owner must see while away:
an approval that waits for them, the kill switch, a failed mission, a revoked device. Delivery
goes through a pluggable transport:

* ``FakePush``    - records messages (tests, ``JARVIS_PUSH=fake``)
* ``WebhookPush`` - HTTP POST to ``JARVIS_PUSH_URL`` (ntfy topic, Home Assistant webhook, ...);
                    an optional bearer token comes only from ``JARVIS_PUSH_TOKEN``

Every delivery attempt is itself an event (``notify.sent`` / ``notify.failed``) with title,
body and channel - never with secrets or memory values. Phase 11's Relevance Engine later
decides *which* informational events reach a channel; the critical/urgent set here stays.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from core.events.bus import EventBus
from core.events.envelope import Event, Priority

SOURCE = "notify"


@dataclass(frozen=True)
class PushMessage:
    title: str
    body: str
    priority: str = "default"  # default | high | urgent (ntfy-compatible)
    tags: tuple[str, ...] = ()
    correlation_id: str = "notify"
    click: str | None = None  # deep link for the mobile HUD (e.g. /hud/#approvals)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "body": self.body,
            "priority": self.priority,
            "tags": list(self.tags),
            "correlation_id": self.correlation_id,
            "click": self.click,
        }


class PushTransport(Protocol):
    name: str

    async def send(self, message: PushMessage) -> None: ...


@dataclass
class FakePush:
    name: str = "fake"
    sent: list[PushMessage] = field(default_factory=list)
    failing: bool = False

    async def send(self, message: PushMessage) -> None:
        if self.failing:
            raise RuntimeError("push transport down")
        self.sent.append(message)


class WebhookPush:
    """POST {title, body, priority, tags, click} as JSON. Works with ntfy (JSON publish) and any
    webhook receiver (Home Assistant `webhook` trigger -> notify.mobile_app)."""

    name = "webhook"

    def __init__(self, url: str | None = None, token: str | None = None, timeout_s: float = 5.0):
        self._url = url or os.environ.get("JARVIS_PUSH_URL") or ""
        self._token = token or os.environ.get("JARVIS_PUSH_TOKEN") or ""
        self._timeout = timeout_s
        if not self._url:
            raise ValueError("JARVIS_PUSH_URL is required for JARVIS_PUSH=webhook")

    def __repr__(self) -> str:
        return f"WebhookPush(url={self._url!r}, token={'set' if self._token else 'unset'})"

    async def send(self, message: PushMessage) -> None:
        import httpx

        headers = {"content-type": "application/json"}
        if self._token:
            headers["authorization"] = f"Bearer {self._token}"
        payload = {k: v for k, v in message.to_dict().items() if k != "correlation_id"}
        if "ntfy" in self._url:  # ntfy JSON publish wants the topic in the body
            payload["topic"] = self._url.rstrip("/").rsplit("/", 1)[-1]
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.post(self._url, json=payload, headers=headers)
            r.raise_for_status()


class PushService:
    """Turns the owner-relevant subset of events into push messages."""

    def __init__(self, bus: EventBus, transport: PushTransport) -> None:
        self._bus = bus
        self.transport = transport
        self._sub = bus.subscribe("*", self._on_event)
        self.gate: Callable[[Event], bool] | None = None  # Relevance Engine hook (Phase 11)
        self._tasks: set[asyncio.Task[None]] = set()

    def close(self) -> None:
        self._bus.unsubscribe(self._sub)

    # -- mapping -------------------------------------------------------------------------------

    @staticmethod
    def message_for(ev: Event) -> PushMessage | None:
        p = ev.payload or {}
        t = ev.type
        if t == "permission.ask":
            req = p["decision"]["request"]
            strength = ["", "voice", "a tap", "a passkey"][int(p["decision"]["required_strength"])]
            return PushMessage(
                "Approval needed",
                f"{req['action']} (P{req['risk']}) needs {strength or 'your ok'}.",
                "high",
                ("lock",),
                ev.correlation_id,
                "/hud/#approvals",
            )
        if t == "gateway.halted":
            return PushMessage(
                "JARVIS stopped",
                f"Kill switch: {p.get('reason', 'halted')}. Resume with a passkey.",
                "urgent",
                ("octagonal_sign",),
                ev.correlation_id,
                "/hud/",
            )
        if t == "mission.failed":
            return PushMessage(
                "Mission failed",
                str(p.get("reason") or p.get("goal") or ev.correlation_id)[:180],
                "default",
                ("x",),
                ev.correlation_id,
                "/hud/#missions",
            )
        if t == "device.revoked":
            return PushMessage(
                "Device revoked",
                f"{p.get('name', 'a device')} can no longer talk to JARVIS.",
                "high",
                ("no_entry",),
                "devices",
                "/hud/#devices",
            )
        return None

    # -- delivery ------------------------------------------------------------------------------

    async def _on_event(self, ev: Event) -> None:
        if ev.type.startswith("notify."):
            return
        msg = self.message_for(ev)
        if msg is None:
            return
        if self.gate is not None and not self.gate(ev):
            await self._bus.publish(
                Event.new(
                    "notify.suppressed",
                    SOURCE,
                    {**msg.to_dict(), "channel": self.transport.name},
                    correlation_id=msg.correlation_id,
                )
            )
            return
        await self.deliver(msg)

    async def deliver(self, msg: PushMessage) -> bool:
        try:
            await self.transport.send(msg)
        except Exception as exc:  # the transport is outside our control; never crash the bus
            await self._bus.publish(
                Event.new(
                    "notify.failed",
                    SOURCE,
                    {**msg.to_dict(), "channel": self.transport.name, "error": str(exc)[:200]},
                    correlation_id=msg.correlation_id,
                )
            )
            return False
        await self._bus.publish(
            Event.new(
                "notify.sent",
                SOURCE,
                {**msg.to_dict(), "channel": self.transport.name},
                correlation_id=msg.correlation_id,
                priority=Priority.URGENT if msg.priority == "urgent" else Priority.NORMAL,
            )
        )
        return True


def push_transport_from_env(choice: str | None) -> PushTransport | None:
    c = (choice or os.environ.get("JARVIS_PUSH") or "off").lower()
    if c in ("", "off", "0", "false"):
        return None
    if c == "fake":
        return FakePush()
    if c == "webhook":
        return WebhookPush()
    raise ValueError(f"unknown JARVIS_PUSH {choice!r} (off | fake | webhook)")
