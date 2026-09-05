"""In-process, asyncio-friendly Event Bus (SPEC §5.1 "Event Bus", ADR-0002).

Contract:
- ``publish`` persists the event **before** any subscriber sees it (durable first, then deliver),
  so the UI/HUD can only ever render persisted events (SECURITY.md §3, "UI täuscht Status vor").
- Subscribers are matched by type pattern: exact ``"mission.task.started"``, prefix
  ``"mission.*"`` (also matches ``"mission"`` itself) or ``"*"``.
- Handlers may be sync or async; they are awaited sequentially in subscription order so delivery
  is deterministic. A failing handler never prevents delivery to the others.
- Publishing the same event twice raises ``DuplicateEventError`` (a bug, not a retry path).
"""

from __future__ import annotations

import inspect
import itertools
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from core.events.envelope import Event
from core.events.store import EventStore, SQLEventStore

log = logging.getLogger("jarvis.core.events")

Handler = Callable[[Event], Any | Awaitable[Any]]


class DuplicateEventError(ValueError):
    """The event_id was already persisted; events are immutable and published exactly once."""


@dataclass(frozen=True)
class Subscription:
    id: int
    pattern: str
    handler: Handler


@dataclass(frozen=True)
class DeliveryReport:
    event_id: str
    seq: int
    delivered: int
    failed: int

    @property
    def ok(self) -> bool:
        return self.failed == 0


def matches(pattern: str, event_type: str) -> bool:
    if pattern == "*":
        return True
    if pattern.endswith(".*"):
        prefix = pattern[:-2]
        return event_type == prefix or event_type.startswith(prefix + ".")
    return pattern == event_type


class EventBus:
    def __init__(self, store: EventStore | None = None) -> None:
        self._store = store if store is not None else SQLEventStore.in_memory()
        self._subs: list[Subscription] = []
        self._ids = itertools.count(1)

    @property
    def store(self) -> EventStore:
        return self._store

    # -- subscriptions -------------------------------------------------------------------------

    def subscribe(self, pattern: str, handler: Handler) -> Subscription:
        if not pattern or not isinstance(pattern, str):
            raise ValueError("pattern must be a non-empty string")
        if not callable(handler):
            raise TypeError("handler must be callable")
        sub = Subscription(next(self._ids), pattern, handler)
        self._subs.append(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> bool:
        before = len(self._subs)
        self._subs = [s for s in self._subs if s.id != sub.id]
        return len(self._subs) != before

    def subscribers(self, event_type: str) -> list[Subscription]:
        return [s for s in self._subs if matches(s.pattern, event_type)]

    # -- publishing ----------------------------------------------------------------------------

    async def publish(self, event: Event) -> DeliveryReport:
        if not isinstance(event, Event):
            raise TypeError("publish() expects an Event")
        if not self._store.append(event):
            raise DuplicateEventError(
                f"event {event.event_id} ({event.type}) was already published"
            )
        seq = self._store.last_seq()
        delivered = failed = 0
        for sub in self.subscribers(event.type):
            try:
                result = sub.handler(event)
                if inspect.isawaitable(result):
                    await result
                delivered += 1
            except Exception:  # isolate subscriber failures, never lose delivery
                failed += 1
                log.exception(
                    "event handler failed: pattern=%s type=%s event_id=%s",
                    sub.pattern,
                    event.type,
                    event.event_id,
                )
        return DeliveryReport(event.event_id, seq, delivered, failed)

    async def emit(self, type: str, source: str, payload: dict[str, Any] | None = None, **kw: Any):
        """Convenience: build with ``Event.new`` and publish. Returns ``(event, report)``."""
        event = Event.new(type, source, payload, **kw)
        return event, await self.publish(event)

    # -- replay --------------------------------------------------------------------------------

    def replay(self, **filters: Any) -> list[tuple[int, Event]]:
        return self._store.replay(**filters)

    async def replay_to(self, handler: Handler, **filters: Any) -> int:
        """Feed persisted events (in order) to ``handler``; used to rebuild state after restart."""
        n = 0
        for _, event in self._store.replay(**filters):
            result = handler(event)
            if inspect.isawaitable(result):
                await result
            n += 1
        return n
