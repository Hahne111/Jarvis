"""Typed Event Bus (SPEC §5.2, Phase 1 / Commit 004).

- ``Event``: the immutable envelope every state change is published as.
- ``EventStore`` / ``SQLEventStore``: durable, ordered, replayable persistence.
- ``EventBus``: in-process pub/sub that persists an event *before* delivering it.
"""

from core.events.bus import DeliveryReport, DuplicateEventError, EventBus, Subscription
from core.events.envelope import Event, Priority, Sensitivity
from core.events.store import EventStore, SQLEventStore

__all__ = [
    "DeliveryReport",
    "DuplicateEventError",
    "Event",
    "EventBus",
    "EventStore",
    "Priority",
    "SQLEventStore",
    "Sensitivity",
    "Subscription",
]
