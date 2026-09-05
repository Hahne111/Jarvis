"""Tests for core.events (Commit 004): envelope validation, durable replayable store, pub/sub."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from datetime import datetime

import pytest
from core.events import (
    DuplicateEventError,
    Event,
    EventBus,
    Priority,
    Sensitivity,
    SQLEventStore,
)
from core.events.bus import matches

# ---------------------------------------------------------------- envelope


def test_event_defaults_match_spec_envelope():
    ev = Event.new("mission.task.started", "coding-agent", {"task": "t1"})
    d = ev.to_dict()
    assert set(d) == {
        "event_id",
        "type",
        "timestamp",
        "source",
        "correlation_id",
        "user_id",
        "device_id",
        "sensitivity",
        "priority",
        "payload",
        "ttl",
    }
    assert d["user_id"] == "local-owner"
    assert d["sensitivity"] == "private"
    assert d["priority"] == "normal"
    assert d["device_id"] is None
    assert d["ttl"] is None
    assert ev.timestamp.tzinfo is not None


@pytest.mark.parametrize("bad", ["Mission.Started", "mission", "mission..x", "", "a.b-c", "1a.b"])
def test_event_type_must_be_lowercase_dotted(bad):
    with pytest.raises(ValueError):
        Event.new(bad, "test")


def test_event_rejects_invalid_fields():
    with pytest.raises(ValueError):
        Event.new("a.b", "")
    with pytest.raises(ValueError):
        Event.new("a.b", "src", {"obj": object()})
    with pytest.raises(ValueError):
        Event(type="a.b", source="src", timestamp=datetime(2026, 1, 1))  # naive
    with pytest.raises(ValueError):
        Event.new("a.b", "src", sensitivity="top-secret")
    with pytest.raises(ValueError):
        Event.new("a.b", "src", priority="asap")
    with pytest.raises(ValueError):
        Event.new("a.b", "src", ttl=-1)
    with pytest.raises(ValueError):
        Event.new("a.b", "src", ttl=True)


def test_event_is_immutable_and_roundtrips_json():
    ev = Event.new(
        "home.light.changed",
        "home-assistant",
        {"entity": "light.desk", "on": True},
        device_id="desk-pc",
        sensitivity="secret",
        priority=Priority.URGENT,
        ttl=30,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        ev.type = "x.y"  # type: ignore[misc]
    back = Event.from_json(ev.to_json())
    assert back == ev
    assert back.sensitivity is Sensitivity.SECRET
    assert back.priority is Priority.URGENT
    assert json.loads(ev.to_json())["payload"] == {"entity": "light.desk", "on": True}


def test_follow_up_keeps_correlation_chain():
    first = Event.new("mission.created", "core", device_id="phone", sensitivity="secret")
    nxt = first.follow_up("mission.planning", "planner", {"steps": 3})
    assert nxt.correlation_id == first.correlation_id
    assert nxt.event_id != first.event_id
    assert nxt.device_id == "phone"
    assert nxt.sensitivity is Sensitivity.SECRET


# ---------------------------------------------------------------- store


def test_store_appends_in_order_and_replays(tmp_path):
    store = SQLEventStore(f"sqlite:///{tmp_path / 'events.db'}")
    a = Event.new("mission.created", "core")
    b = a.follow_up("mission.task.started", "agent")
    c = Event.new("home.light.changed", "ha")
    assert store.append(a) and store.append(b) and store.append(c)
    assert store.count() == 3
    assert [e.event_id for _, e in store.replay()] == [a.event_id, b.event_id, c.event_id]
    assert [s for s, _ in store.replay()] == [1, 2, 3]
    assert store.last_seq() == 3
    assert store.get(b.event_id) == b
    assert store.get("missing") is None


def test_store_filters_by_correlation_prefix_seq_and_limit():
    store = SQLEventStore.in_memory()
    a = Event.new("mission.created", "core")
    b = a.follow_up("mission.task.started", "agent")
    c = Event.new("missionary.x", "other")  # must NOT match prefix "mission"
    d = Event.new("home.light.changed", "ha")
    for e in (a, b, c, d):
        store.append(e)
    assert [e.event_id for _, e in store.replay(correlation_id=a.correlation_id)] == [
        a.event_id,
        b.event_id,
    ]
    assert [e.type for _, e in store.replay(type_prefix="mission")] == [
        "mission.created",
        "mission.task.started",
    ]
    assert [e.type for _, e in store.replay(type_prefix="mission.task")] == ["mission.task.started"]
    assert [s for s, _ in store.replay(after_seq=2)] == [3, 4]
    assert len(store.replay(limit=1)) == 1


def test_store_is_idempotent_on_duplicate_event_id():
    store = SQLEventStore.in_memory()
    ev = Event.new("a.b", "src")
    assert store.append(ev) is True
    assert store.append(ev) is False
    assert store.count() == 1


def test_store_survives_reopen(tmp_path):
    url = f"sqlite:///{tmp_path / 'durable.db'}"
    ev = Event.new("mission.created", "core", {"goal": "persist"})
    SQLEventStore(url).append(ev)
    reopened = SQLEventStore(url)  # simulates a process restart
    assert reopened.count() == 1
    assert reopened.replay()[0][1] == ev


# ---------------------------------------------------------------- bus


@pytest.mark.parametrize(
    ("pattern", "event_type", "expected"),
    [
        ("*", "anything.here", True),
        ("mission.*", "mission.task.started", True),
        ("mission.*", "mission", True),
        ("mission.*", "missionary.x", False),
        ("mission.task.started", "mission.task.started", True),
        ("mission.task.started", "mission.task.done", False),
    ],
)
def test_pattern_matching(pattern, event_type, expected):
    assert matches(pattern, event_type) is expected


def test_bus_persists_before_delivery_and_supports_sync_and_async_handlers():
    bus = EventBus()
    seen: list[str] = []

    def sync_handler(ev: Event):
        # Durable first: the event is already in the store when a subscriber sees it.
        assert bus.store.get(ev.event_id) == ev
        seen.append("sync:" + ev.type)

    async def async_handler(ev: Event):
        await asyncio.sleep(0)
        seen.append("async:" + ev.type)

    bus.subscribe("mission.*", sync_handler)
    bus.subscribe("*", async_handler)
    bus.subscribe("home.*", lambda ev: seen.append("home"))

    ev, report = asyncio.run(bus.emit("mission.task.started", "agent", {"n": 1}))
    assert report.ok and report.delivered == 2 and report.failed == 0 and report.seq == 1
    assert seen == ["sync:mission.task.started", "async:mission.task.started"]
    assert bus.store.count() == 1 and bus.replay()[0][1] == ev


def test_bus_isolates_failing_handlers(caplog):
    bus = EventBus()
    calls: list[str] = []

    def boom(ev):
        raise RuntimeError("handler exploded")

    bus.subscribe("*", boom)
    bus.subscribe("*", lambda ev: calls.append(ev.type))
    ev = Event.new("a.b", "src")
    report = asyncio.run(bus.publish(ev))
    assert report.delivered == 1 and report.failed == 1 and not report.ok
    assert calls == ["a.b"]
    assert bus.store.count() == 1  # persisted even though a handler failed
    assert "handler exploded" in caplog.text or "event handler failed" in caplog.text


def test_bus_rejects_duplicate_publish_and_non_events():
    bus = EventBus()
    ev = Event.new("a.b", "src")
    asyncio.run(bus.publish(ev))
    with pytest.raises(DuplicateEventError):
        asyncio.run(bus.publish(ev))
    with pytest.raises(TypeError):
        asyncio.run(bus.publish({"type": "a.b"}))  # type: ignore[arg-type]
    assert bus.store.count() == 1


def test_bus_unsubscribe_and_replay_to_rebuild_state(tmp_path):
    url = f"sqlite:///{tmp_path / 'bus.db'}"
    bus = EventBus(SQLEventStore(url))
    sub = bus.subscribe("mission.*", lambda ev: None)
    assert bus.unsubscribe(sub) is True
    assert bus.unsubscribe(sub) is False
    assert bus.subscribers("mission.created") == []

    m = Event.new("mission.created", "core", {"goal": "g"})
    asyncio.run(bus.publish(m))
    asyncio.run(bus.publish(m.follow_up("mission.task.started", "agent")))
    asyncio.run(bus.publish(Event.new("home.light.changed", "ha")))

    # "Restart": a fresh bus on the same database rebuilds state from the log.
    restarted = EventBus(SQLEventStore(url))
    state: list[str] = []
    n = asyncio.run(
        restarted.replay_to(lambda ev: state.append(ev.type), correlation_id=m.correlation_id)
    )
    assert n == 2
    assert state == ["mission.created", "mission.task.started"]
