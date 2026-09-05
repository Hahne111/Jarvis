"""Memory model (SPEC §8.1-8.2, §17.1 MemoryItem).

    memory_id, type, subject, predicate, value, confidence, source, observations,
    created_at, last_confirmed_at, sensitivity, retention, project_scope

Corrections never overwrite: a new item supersedes the old one (both keep their ids), so the
"why does JARVIS think that?" question always has an answer. Forgetting deletes the row; the
audit event carries metadata only, never the value.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from core.events.envelope import DEFAULT_USER_ID, Sensitivity


class MemoryType(StrEnum):
    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROJECT = "project"
    PREFERENCE = "preference"
    HABIT = "habit"
    RELATIONSHIP = "relationship"
    PROCEDURAL = "procedural"
    VISUAL = "visual"


class MemorySource(StrEnum):
    EXPLICIT_STATEMENT = "explicit_statement"
    OBSERVATION = "observation"
    CORRECTION = "correction"


class Retention(StrEnum):
    DURABLE = "durable"
    TEMPORARY = "temporary"  # needs expires_at
    SESSION = "session"  # dropped at the next core start


DEFAULT_CONFIDENCE = {
    MemorySource.EXPLICIT_STATEMENT: 0.9,
    MemorySource.CORRECTION: 0.95,
    MemorySource.OBSERVATION: 0.5,
}


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class MemoryItem:
    type: MemoryType
    subject: str
    predicate: str
    value: Any  # JSON-serialisable
    source: MemorySource
    confidence: float | None = None
    memory_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    observations: int = 1
    created_at: datetime = field(default_factory=_now)
    last_confirmed_at: datetime = field(default_factory=_now)
    sensitivity: Sensitivity = Sensitivity.PRIVATE
    retention: Retention = Retention.DURABLE
    expires_at: datetime | None = None
    project_scope: str | None = None
    owner: str = DEFAULT_USER_ID
    pinned: bool = False
    supersedes: str | None = None
    superseded_by: str | None = None

    def __post_init__(self) -> None:
        self.type = MemoryType(self.type)
        self.source = MemorySource(self.source)
        self.sensitivity = Sensitivity(self.sensitivity)
        self.retention = Retention(self.retention)
        for name in ("subject", "predicate"):
            v = getattr(self, name)
            if not isinstance(v, str) or not v.strip():
                raise ValueError(f"{name} must be a non-empty string")
        try:
            json.dumps(self.value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"value must be JSON-serialisable: {exc}") from exc
        if self.confidence is None:
            self.confidence = DEFAULT_CONFIDENCE[self.source]
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("confidence must be within 0..1")
        if self.observations < 1:
            raise ValueError("observations must be >= 1")
        if self.retention is Retention.TEMPORARY and self.expires_at is None:
            raise ValueError("temporary memories need expires_at")
        if self.retention is not Retention.TEMPORARY and self.expires_at is not None:
            raise ValueError("only temporary memories have expires_at")
        for ts in (self.created_at, self.last_confirmed_at, self.expires_at):
            if ts is not None and ts.tzinfo is None:
                raise ValueError("timestamps must be timezone-aware")

    # -- state helpers -------------------------------------------------------------------------

    @property
    def active(self) -> bool:
        return self.superseded_by is None

    def is_expired(self, now: datetime | None = None) -> bool:
        return self.expires_at is not None and (now or _now()) >= self.expires_at

    def searchable_text(self) -> str:
        value = (
            self.value if isinstance(self.value, str) else json.dumps(self.value, sort_keys=True)
        )
        return f"{self.subject} {self.predicate} {value}".lower()

    def reinforce(self, now: datetime | None = None, step: float = 0.2) -> None:
        """Confirmed again: more observations, confidence moves 20% of the way to 0.99."""
        self.observations += 1
        self.confidence = min(0.99, float(self.confidence) + (0.99 - float(self.confidence)) * step)
        self.last_confirmed_at = now or _now()

    def make_temporary(self, ttl_s: int, now: datetime | None = None) -> None:
        if ttl_s <= 0:
            raise ValueError("ttl_s must be positive")
        self.retention = Retention.TEMPORARY
        self.expires_at = (now or _now()) + timedelta(seconds=ttl_s)

    # -- serialisation -------------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "type": self.type.value,
            "subject": self.subject,
            "predicate": self.predicate,
            "value": self.value,
            "confidence": round(float(self.confidence), 4),
            "source": self.source.value,
            "observations": self.observations,
            "created_at": self.created_at.isoformat(),
            "last_confirmed_at": self.last_confirmed_at.isoformat(),
            "sensitivity": self.sensitivity.value,
            "retention": self.retention.value,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "project_scope": self.project_scope,
            "owner": self.owner,
            "pinned": self.pinned,
            "supersedes": self.supersedes,
            "superseded_by": self.superseded_by,
        }

    def audit_dict(self) -> dict[str, Any]:
        """Metadata for events: everything except the value (SECURITY.md: forgetting must work)."""
        d = self.to_dict()
        d.pop("value")
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> MemoryItem:
        return cls(
            memory_id=d["memory_id"],
            type=MemoryType(d["type"]),
            subject=d["subject"],
            predicate=d["predicate"],
            value=d["value"],
            confidence=float(d["confidence"]),
            source=MemorySource(d["source"]),
            observations=int(d.get("observations", 1)),
            created_at=datetime.fromisoformat(d["created_at"]),
            last_confirmed_at=datetime.fromisoformat(d["last_confirmed_at"]),
            sensitivity=Sensitivity(d.get("sensitivity", "private")),
            retention=Retention(d.get("retention", "durable")),
            expires_at=datetime.fromisoformat(d["expires_at"]) if d.get("expires_at") else None,
            project_scope=d.get("project_scope"),
            owner=d.get("owner", DEFAULT_USER_ID),
            pinned=bool(d.get("pinned", False)),
            supersedes=d.get("supersedes"),
            superseded_by=d.get("superseded_by"),
        )
