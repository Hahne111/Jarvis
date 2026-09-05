"""MemoryWriter: the only writer of memory (SPEC §8.3 learning loop, §8.4 controls).

Privacy filter -> upsert by (subject, predicate, project) -> reinforce / supersede / conflict ->
audit event. Events carry metadata only (never the value), so "forget" really forgets.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from core.events.bus import EventBus
from core.events.envelope import DEFAULT_USER_ID, Event, Sensitivity
from core.memory.model import (
    DEFAULT_CONFIDENCE,
    MemoryItem,
    MemorySource,
    MemoryType,
    Retention,
)
from core.memory.store import MemoryStore

SOURCE = "memory-writer"


class MemoryPolicyError(PermissionError):
    pass


@dataclass
class MemoryPolicy:
    """Owner-controlled learning rules (SPEC §8.4 Privacy Mode, Don't Learn This)."""

    learn_from_observation: bool = True  # Privacy Mode off -> True
    conversation_memory: bool = True  # False: nothing is written at all
    allow_secret: bool = False  # secret items need owner_approved=True per call
    dont_learn: set[tuple[str, str]] = field(default_factory=set)  # (subject, predicate)
    max_temporary_s: int = 30 * 24 * 3600

    def blocks(self, item_source: MemorySource, subject: str, predicate: str) -> str | None:
        if not self.conversation_memory:
            return "conversation memory disabled"
        if item_source is MemorySource.OBSERVATION and not self.learn_from_observation:
            return "privacy mode: not learning from observation"
        if (subject, predicate) in self.dont_learn or (subject, "*") in self.dont_learn:
            return "owner marked this as don't-learn"
        return None


@dataclass(frozen=True)
class WriteResult:
    action: str  # written | reinforced | superseded | conflict | skipped
    item: MemoryItem | None
    previous: MemoryItem | None = None
    reason: str | None = None


class MemoryWriter:
    def __init__(
        self,
        store: MemoryStore,
        bus: EventBus,
        policy: MemoryPolicy | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._bus = bus
        self.policy = policy or MemoryPolicy()
        self._clock = clock or (lambda: datetime.now(UTC))

    # -- learn -----------------------------------------------------------------------------------

    async def remember(
        self,
        type: MemoryType | str,
        subject: str,
        predicate: str,
        value: Any,
        *,
        source: MemorySource | str = MemorySource.EXPLICIT_STATEMENT,
        confidence: float | None = None,
        sensitivity: Sensitivity | str = Sensitivity.PRIVATE,
        retention: Retention | str = Retention.DURABLE,
        ttl_s: int | None = None,
        project_scope: str | None = None,
        owner: str = DEFAULT_USER_ID,
        owner_approved: bool = False,
        correlation_id: str | None = None,
    ) -> WriteResult:
        source = MemorySource(source)
        sensitivity = Sensitivity(sensitivity)
        retention = Retention(retention)
        blocked = self.policy.blocks(source, subject, predicate)
        if blocked:
            return WriteResult("skipped", None, reason=blocked)
        if sensitivity is Sensitivity.SECRET and not (owner_approved or self.policy.allow_secret):
            raise MemoryPolicyError("secret memories require explicit owner approval")
        now = self._clock()
        expires_at = None
        if ttl_s is not None:
            retention = Retention.TEMPORARY
        if retention is Retention.TEMPORARY:
            ttl = min(int(ttl_s or self.policy.max_temporary_s), self.policy.max_temporary_s)
            expires_at = now + timedelta(seconds=ttl)
        item = MemoryItem(
            type=type,
            subject=subject,
            predicate=predicate,
            value=value,
            source=source,
            confidence=confidence if confidence is not None else DEFAULT_CONFIDENCE[source],
            created_at=now,
            last_confirmed_at=now,
            sensitivity=sensitivity,
            retention=retention,
            expires_at=expires_at,
            project_scope=project_scope,
            owner=owner,
        )
        existing = self._store.find(subject, predicate, project_scope=project_scope)
        current = existing[-1] if existing else None
        if current is None:
            self._store.save(item)
            await self._emit("memory.written", item, correlation_id)
            return WriteResult("written", item)
        if _same_value(current.value, value):
            current.reinforce(now)
            if current.sensitivity is not Sensitivity.SECRET:
                current.sensitivity = max(current.sensitivity, sensitivity, key=_sens_rank)
            self._store.save(current)
            await self._emit("memory.reinforced", current, correlation_id)
            return WriteResult("reinforced", current)
        # Different value: a correction or a stronger claim supersedes; a weaker one is a conflict.
        if source is MemorySource.CORRECTION or float(item.confidence) >= float(current.confidence):
            return await self._supersede(current, item, correlation_id)
        await self._bus.publish(
            Event.new(
                "memory.conflict",
                SOURCE,
                {"existing": current.audit_dict(), "candidate": item.audit_dict()},
                correlation_id=correlation_id or item.memory_id,
                user_id=owner,
                sensitivity=Sensitivity.PRIVATE,
            )
        )
        return WriteResult("conflict", None, previous=current, reason="weaker than existing memory")

    async def correct(
        self,
        memory_id: str,
        value: Any,
        *,
        owner: str = DEFAULT_USER_ID,
        correlation_id: str | None = None,
    ) -> WriteResult:
        """Owner correction: new version with source=correction supersedes the old one."""
        current = self._require(memory_id)
        if not current.active:
            raise ValueError(f"memory {memory_id} is already superseded by {current.superseded_by}")
        now = self._clock()
        item = MemoryItem(
            type=current.type,
            subject=current.subject,
            predicate=current.predicate,
            value=value,
            source=MemorySource.CORRECTION,
            created_at=now,
            last_confirmed_at=now,
            sensitivity=current.sensitivity,
            retention=current.retention,
            expires_at=current.expires_at,
            project_scope=current.project_scope,
            owner=owner,
            pinned=current.pinned,
        )
        return await self._supersede(current, item, correlation_id)

    # -- owner controls (SPEC §8.4) ---------------------------------------------------------------

    async def forget(
        self, memory_id: str, *, reason: str = "owner request", correlation_id: str | None = None
    ) -> bool:
        item = self._store.get(memory_id)
        if item is None:
            return False
        self._store.delete(memory_id)
        await self._emit("memory.forgotten", item, correlation_id, extra={"reason": reason})
        return True

    async def forget_since(
        self, since: datetime, *, reason: str = "owner request", correlation_id: str | None = None
    ) -> int:
        """'Jarvis, forget the last 30 minutes': delete everything learned since ``since``.

        Pinned items survive. Items merely *reinforced* in the window are rolled back one
        observation instead of deleted (they were known before).
        """
        deleted: list[str] = []
        rolled_back: list[str] = []
        for item in self._store.created_or_confirmed_since(since):
            if item.pinned:
                continue
            if item.created_at >= since:
                self._store.delete(item.memory_id)
                deleted.append(item.memory_id)
            elif item.observations > 1:
                item.observations -= 1
                item.last_confirmed_at = since - timedelta(seconds=1)
                self._store.save(item)
                rolled_back.append(item.memory_id)
        await self._bus.publish(
            Event.new(
                "memory.forgotten_window",
                SOURCE,
                {
                    "since": since.isoformat(),
                    "deleted": deleted,
                    "rolled_back": rolled_back,
                    "reason": reason,
                },
                correlation_id=correlation_id or "memory",
            )
        )
        return len(deleted)

    async def pin(
        self, memory_id: str, pinned: bool = True, *, correlation_id: str | None = None
    ) -> MemoryItem:
        item = self._require(memory_id)
        item.pinned = pinned
        self._store.save(item)
        await self._emit("memory.pinned" if pinned else "memory.unpinned", item, correlation_id)
        return item

    async def make_temporary(
        self, memory_id: str, ttl_s: int, *, correlation_id: str | None = None
    ) -> MemoryItem:
        item = self._require(memory_id)
        item.make_temporary(min(ttl_s, self.policy.max_temporary_s), self._clock())
        self._store.save(item)
        await self._emit("memory.made_temporary", item, correlation_id)
        return item

    async def dont_learn(
        self, subject: str, predicate: str = "*", *, correlation_id: str | None = None
    ) -> None:
        self.policy.dont_learn.add((subject, predicate))
        await self._bus.publish(
            Event.new(
                "memory.dont_learn",
                SOURCE,
                {"subject": subject, "predicate": predicate},
                correlation_id=correlation_id or "memory",
            )
        )

    async def purge_expired(self, *, correlation_id: str | None = None) -> int:
        now = self._clock()
        expired = self._store.expired(now)
        for item in expired:
            self._store.delete(item.memory_id)
            await self._emit("memory.expired", item, correlation_id)
        return len(expired)

    async def drop_session_memory(self) -> int:
        items = self._store.session_items()
        self._store.delete_many([i.memory_id for i in items])
        return len(items)

    # -- internals -------------------------------------------------------------------------------

    def _require(self, memory_id: str) -> MemoryItem:
        item = self._store.get(memory_id)
        if item is None:
            raise KeyError(memory_id)
        return item

    async def _supersede(
        self, old: MemoryItem, new: MemoryItem, correlation_id: str | None
    ) -> WriteResult:
        new.supersedes = old.memory_id
        new.pinned = new.pinned or old.pinned
        self._store.save(new)
        old.superseded_by = new.memory_id
        self._store.save(old)
        event_type = (
            "memory.corrected" if new.source is MemorySource.CORRECTION else "memory.updated"
        )
        await self._emit(event_type, new, correlation_id, extra={"supersedes": old.audit_dict()})
        return WriteResult("superseded", new, previous=old)

    async def _emit(
        self,
        event_type: str,
        item: MemoryItem,
        correlation_id: str | None,
        *,
        extra: dict[str, Any] | None = None,
    ) -> None:
        payload = {"memory": item.audit_dict(), **(extra or {})}
        assert "value" not in payload["memory"]  # noqa: S101 - invariant, cheap, worth failing loudly
        await self._bus.publish(
            Event.new(
                event_type,
                SOURCE,
                payload,
                correlation_id=correlation_id or item.memory_id,
                user_id=item.owner,
                sensitivity=Sensitivity.PRIVATE,
            )
        )


def _same_value(a: Any, b: Any) -> bool:
    if isinstance(a, str) and isinstance(b, str):
        return a.strip().casefold() == b.strip().casefold()
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def _sens_rank(s: Sensitivity) -> int:
    return {Sensitivity.PUBLIC: 0, Sensitivity.PRIVATE: 1, Sensitivity.SECRET: 2}[s]
