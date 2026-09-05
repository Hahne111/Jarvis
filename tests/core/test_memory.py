"""Tests for core.memory (Phase 4, steps 23-25): model, store/search, writer, privacy, forget."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from core.events import EventBus, Sensitivity, SQLEventStore
from core.memory import (
    HashingEmbedder,
    MemoryItem,
    MemoryPolicy,
    MemoryPolicyError,
    MemorySource,
    MemoryStore,
    MemoryType,
    MemoryWriter,
    Retention,
)
from core.memory.embedding import cosine, tokenize


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now += timedelta(**kw)


def run(coro):
    return asyncio.run(coro)


def make(policy: MemoryPolicy | None = None, embedder=None):
    bus = EventBus(SQLEventStore.in_memory())
    store = MemoryStore(engine=bus.store.engine, embedder=embedder)  # type: ignore[attr-defined]
    clock = Clock()
    return MemoryWriter(store, bus, policy, clock=clock), store, bus, clock


def events(bus: EventBus) -> list:
    return [e for _, e in bus.replay(type_prefix="memory")]


# ---------------------------------------------------------------- model


def test_item_defaults_and_validation():
    item = MemoryItem(
        MemoryType.PREFERENCE,
        "owner",
        "preferred_editor",
        "VS Code",
        MemorySource.EXPLICIT_STATEMENT,
    )
    assert item.confidence == 0.9 and item.observations == 1 and item.active
    d = item.to_dict()
    assert set(d) >= {
        "memory_id",
        "type",
        "subject",
        "predicate",
        "value",
        "confidence",
        "source",
        "observations",
        "created_at",
        "last_confirmed_at",
        "sensitivity",
        "retention",
        "project_scope",
    }
    assert MemoryItem.from_dict(d) == item
    assert "value" not in item.audit_dict() and item.audit_dict()["predicate"] == "preferred_editor"
    with pytest.raises(ValueError):
        MemoryItem("preference", " ", "p", "v", "observation")
    with pytest.raises(ValueError):
        MemoryItem("preference", "s", "p", object(), "observation")
    with pytest.raises(ValueError):
        MemoryItem("preference", "s", "p", "v", "observation", confidence=1.5)
    with pytest.raises(ValueError):
        MemoryItem("preference", "s", "p", "v", "observation", retention="temporary")
    with pytest.raises(ValueError):
        MemoryItem("preference", "s", "p", "v", "observation", expires_at=datetime.now(UTC))
    item.reinforce()
    assert item.observations == 2 and 0.9 < item.confidence < 0.99
    for _ in range(50):
        item.reinforce()
    assert item.confidence <= 0.99


# ---------------------------------------------------------------- embedder / store


def test_hashing_embedder_is_deterministic_and_meaningful():
    e = HashingEmbedder()
    a, b = e.embed(["postgres database for atlas", "atlas uses a postgres database"])
    c = e.embed(["kokoro tts voice"])[0]
    assert e.embed(["postgres database for atlas"])[0] == a
    assert len(a) == 64 and abs(sum(x * x for x in a) - 1.0) < 1e-9
    assert cosine(a, b) > 0.6 and cosine(a, b) > cosine(a, c)  # hashed dims may collide
    assert tokenize("Hallo, Welt! Größe 42") == ["hallo", "welt", "größe", "42"]
    assert cosine([], [1.0]) == 0.0


def test_store_find_list_search_and_scopes():
    store = MemoryStore(embedder=HashingEmbedder())
    a = MemoryItem(
        "semantic",
        "project:atlas",
        "database",
        "PostgreSQL",
        "explicit_statement",
        project_scope="atlas",
    )
    b = MemoryItem("preference", "owner", "preferred_editor", "VS Code", "explicit_statement")
    c = MemoryItem(
        "habit", "owner", "morning_routine", {"time": "07:00", "action": "coffee"}, "observation"
    )
    for i in (a, b, c):
        store.save(i)
    assert store.count() == 3 and store.get(b.memory_id) == b and store.get("nope") is None
    assert store.find("owner", "preferred_editor") == [b]
    assert store.find("project:atlas", "database") == []  # scoped item needs the scope
    assert store.find("project:atlas", "database", project_scope="atlas") == [a]
    assert [i.memory_id for i in store.list(type="preference")] == [b.memory_id]

    hits = store.search("which database does atlas use", project_scope="atlas")
    assert hits and hits[0][1] == a
    assert store.search("editor")[0][1] == b
    assert store.search("coffee")[0][1] == c  # dict values are searchable
    assert store.search("") == [] and store.search("zzzz qqqq") == []
    # project-scoped search still sees unscoped memories
    assert any(i == b for _, i in store.search("editor preference", project_scope="atlas"))


def test_store_excludes_superseded_and_expired_and_supports_windows():
    store = MemoryStore()
    now = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    old = MemoryItem(
        "semantic",
        "s",
        "p",
        "old",
        "observation",
        created_at=now - timedelta(days=2),
        last_confirmed_at=now - timedelta(days=2),
    )
    new = MemoryItem(
        "semantic",
        "s",
        "p",
        "new",
        "correction",
        supersedes=old.memory_id,
        created_at=now,
        last_confirmed_at=now,
    )
    old.superseded_by = new.memory_id
    temp = MemoryItem(
        "working",
        "s",
        "tmp",
        "x",
        "observation",
        retention="temporary",
        expires_at=now + timedelta(minutes=5),
        created_at=now,
        last_confirmed_at=now,
    )
    for i in (old, new, temp):
        store.save(i)
    assert store.find("s", "p") == [new]
    assert [i.memory_id for i in store.find("s", "p", include_superseded=True)] == [
        old.memory_id,
        new.memory_id,
    ]
    assert store.count() == 2 and store.count(active_only=False) == 3
    assert [i.memory_id for i in store.list(now=now)] == [new.memory_id, temp.memory_id]
    later = now + timedelta(minutes=6)
    assert [i.memory_id for i in store.list(now=later)] == [new.memory_id]
    assert [i.memory_id for i in store.expired(later)] == [temp.memory_id]
    assert store.search("new", now=later)[0][1] == new and store.search("old", now=later) == []
    assert {i.memory_id for i in store.created_or_confirmed_since(now - timedelta(hours=1))} == {
        new.memory_id,
        temp.memory_id,
    }
    assert store.delete(temp.memory_id) and not store.delete(temp.memory_id)
    assert (
        store.delete_many([old.memory_id, new.memory_id]) == 2
        and store.count(active_only=False) == 0
    )


# ---------------------------------------------------------------- writer: learning loop


def test_write_reinforce_and_confidence_growth():
    writer, store, bus, clock = make()
    r1 = run(
        writer.remember("preference", "owner", "preferred_editor", "VS Code", correlation_id="m1")
    )
    assert r1.action == "written" and r1.item.confidence == 0.9
    clock.advance(hours=1)
    r2 = run(
        writer.remember("preference", "owner", "preferred_editor", "vs code", source="observation")
    )
    assert r2.action == "reinforced" and r2.item.memory_id == r1.item.memory_id
    assert r2.item.observations == 2 and r2.item.confidence > 0.9
    assert r2.item.last_confirmed_at == clock.now
    assert store.count() == 1
    assert [e.type for e in events(bus)] == ["memory.written", "memory.reinforced"]
    assert events(bus)[0].correlation_id == "m1"


def test_stronger_claim_supersedes_weaker_claim_conflicts():
    writer, store, bus, _ = make()
    first = run(
        writer.remember(
            "semantic",
            "project:atlas",
            "database",
            "MySQL",
            source="observation",
            project_scope="atlas",
        )
    )
    weaker = run(
        writer.remember(
            "semantic",
            "project:atlas",
            "database",
            "SQLite",
            source="observation",
            confidence=0.3,
            project_scope="atlas",
        )
    )
    assert weaker.action == "conflict" and weaker.previous.memory_id == first.item.memory_id
    assert store.find("project:atlas", "database", project_scope="atlas")[0].value == "MySQL"
    stronger = run(
        writer.remember(
            "semantic", "project:atlas", "database", "PostgreSQL", project_scope="atlas"
        )
    )
    assert stronger.action == "superseded" and stronger.item.supersedes == first.item.memory_id
    assert store.get(first.item.memory_id).superseded_by == stronger.item.memory_id
    assert store.find("project:atlas", "database", project_scope="atlas") == [stronger.item]
    types = [e.type for e in events(bus)]
    assert types == ["memory.written", "memory.conflict", "memory.updated"]
    conflict = events(bus)[1]
    assert (
        conflict.payload["candidate"]["confidence"] == 0.3
        and "value" not in conflict.payload["existing"]
    )


def test_owner_correction_creates_new_version_with_source_correction():
    writer, store, bus, _ = make()
    orig = run(writer.remember("preference", "owner", "coffee", "black")).item
    run(writer.pin(orig.memory_id))
    fixed = run(writer.correct(orig.memory_id, "with milk", correlation_id="chat-7"))
    assert fixed.action == "superseded" and fixed.item.source is MemorySource.CORRECTION
    assert (
        fixed.item.confidence == 0.95
        and fixed.item.pinned
        and fixed.item.supersedes == orig.memory_id
    )
    assert store.find("owner", "coffee") == [fixed.item]
    with pytest.raises(ValueError):
        run(writer.correct(orig.memory_id, "again"))  # already superseded
    with pytest.raises(KeyError):
        run(writer.correct("ghost", "x"))
    ev = events(bus)[-1]
    assert ev.type == "memory.corrected" and ev.payload["supersedes"]["memory_id"] == orig.memory_id
    assert ev.correlation_id == "chat-7"


# ---------------------------------------------------------------- writer: privacy + retention


def test_privacy_filters_and_dont_learn():
    writer, store, bus, _ = make(MemoryPolicy(learn_from_observation=False))
    skipped = run(writer.remember("habit", "owner", "lunch", "12:30", source="observation"))
    assert skipped.action == "skipped" and "privacy" in skipped.reason and store.count() == 0
    assert run(writer.remember("preference", "owner", "lunch", "12:30")).action == "written"
    run(writer.dont_learn("owner", "location"))
    assert run(writer.remember("habit", "owner", "location", "home")).action == "skipped"
    run(writer.dont_learn("guest:anna"))
    assert run(writer.remember("preference", "guest:anna", "drink", "tea")).action == "skipped"
    with pytest.raises(MemoryPolicyError):
        run(writer.remember("semantic", "owner", "safe_code", "1234", sensitivity="secret"))
    ok = run(
        writer.remember(
            "semantic", "owner", "safe_code", "1234", sensitivity="secret", owner_approved=True
        )
    )
    assert ok.action == "written" and ok.item.sensitivity is Sensitivity.SECRET
    assert all("1234" not in str(e.payload) for e in events(bus))  # value never in events
    off = make(MemoryPolicy(conversation_memory=False))[0]
    assert run(off.remember("preference", "owner", "x", "y")).action == "skipped"
    assert [e.type for e in events(bus)].count("memory.dont_learn") == 2


def test_temporary_memories_expire_and_pins_survive():
    writer, store, bus, clock = make()
    tmp = run(
        writer.remember("working", "session", "current_file", "voice_router.py", ttl_s=600)
    ).item
    assert tmp.retention is Retention.TEMPORARY and tmp.expires_at == clock.now + timedelta(
        seconds=600
    )
    huge = run(
        writer.remember("working", "session", "note", "x", retention="temporary", ttl_s=10**9)
    ).item
    assert huge.expires_at == clock.now + timedelta(seconds=writer.policy.max_temporary_s)
    keep = run(writer.remember("preference", "owner", "editor", "VS Code")).item
    run(writer.make_temporary(keep.memory_id, 60))
    run(writer.pin(keep.memory_id))
    clock.advance(minutes=11)
    assert run(writer.purge_expired()) == 1
    assert store.get(tmp.memory_id) is None and store.get(keep.memory_id) is not None
    assert [e.type for e in events(bus)].count("memory.expired") == 1
    assert store.search("voice_router", now=clock.now) == []


def test_forget_and_forget_window_are_reproducible_and_leave_no_values_behind():
    writer, store, bus, clock = make()
    early = run(writer.remember("preference", "owner", "editor", "VS Code")).item
    clock.advance(hours=2)
    run(
        writer.remember("preference", "owner", "editor", "VS Code", source="observation")
    )  # reinforce inside window
    pinned = run(writer.remember("semantic", "owner", "birthday", "01-01")).item
    run(writer.pin(pinned.memory_id))
    clock.advance(minutes=10)
    recent = run(
        writer.remember(
            "episodic", "session", "said", "something embarrassing", source="observation"
        )
    ).item
    clock.advance(minutes=5)

    assert (
        run(
            writer.forget_since(
                clock.now - timedelta(minutes=30), reason="voice: forget the last 30 minutes"
            )
        )
        == 1
    )
    assert store.get(recent.memory_id) is None
    assert store.get(pinned.memory_id) is not None  # pinned survives
    rolled = store.get(early.memory_id)
    assert rolled is not None and rolled.observations == 1  # reinforcement in window rolled back
    window = events(bus)[-1]
    assert window.type == "memory.forgotten_window"
    assert window.payload["deleted"] == [recent.memory_id] and window.payload["rolled_back"] == [
        early.memory_id
    ]

    assert (
        run(writer.forget(early.memory_id)) is True and run(writer.forget(early.memory_id)) is False
    )
    assert store.get(early.memory_id) is None
    assert (
        events(bus)[-1].type == "memory.forgotten"
        and events(bus)[-1].payload["reason"] == "owner request"
    )
    assert all(
        "embarrassing" not in str(e.payload) and "VS Code" not in str(e.payload)
        for e in events(bus)
    )


def test_session_memory_is_dropped_on_restart(tmp_path):
    from core.runtime import CoreRuntime

    url = f"sqlite:///{tmp_path / 'm.db'}"
    rt = CoreRuntime.build(url, provider="none")
    run(rt.memory_writer.remember("working", "session", "scratch", "x", retention="session"))
    run(rt.memory_writer.remember("preference", "owner", "editor", "VS Code"))
    assert rt.memory.count() == 2 and rt.health()["memory_items"] == 2
    rt2 = CoreRuntime.build(url, provider="none")
    stats = rt2.recover()
    assert stats["session_memory_dropped"] == 1 and rt2.memory.count() == 1
    assert rt2.memory.search("editor")[0][1].value == "VS Code"
