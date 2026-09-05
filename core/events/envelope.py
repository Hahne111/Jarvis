"""Core Event Envelope (SPEC §5.2).

Event {
  event_id, type, timestamp, source, correlation_id, user_id,
  device_id?, sensitivity, priority, payload, ttl?
}
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

# "mission.task.started": lowercase dotted segments, at least two segments.
_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
DEFAULT_USER_ID = "local-owner"


class Sensitivity(StrEnum):
    PUBLIC = "public"
    PRIVATE = "private"
    SECRET = "secret"  # noqa: S105 - classification level, not a credential


class Priority(StrEnum):
    BACKGROUND = "background"
    NORMAL = "normal"
    URGENT = "urgent"
    CRITICAL = "critical"


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid.uuid4())


@dataclass(frozen=True)
class Event:
    """Immutable, JSON-serialisable event envelope.

    Use ``Event.new(...)`` for the common case; the constructor validates every field so an
    invalid event can never reach the store or a subscriber.
    """

    type: str
    source: str
    payload: dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=new_id)
    timestamp: datetime = field(default_factory=utc_now)
    correlation_id: str = field(default_factory=new_id)
    user_id: str = DEFAULT_USER_ID
    device_id: str | None = None
    sensitivity: Sensitivity = Sensitivity.PRIVATE
    priority: Priority = Priority.NORMAL
    ttl: int | None = None  # seconds

    def __post_init__(self) -> None:
        if not isinstance(self.type, str) or not _TYPE_PATTERN.match(self.type):
            raise ValueError(
                f"invalid event type {self.type!r}: expected lowercase dotted segments "
                "like 'mission.task.started'"
            )
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("event source must be a non-empty string")
        if not isinstance(self.payload, dict):
            raise ValueError("payload must be a dict")
        try:
            json.dumps(self.payload)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"payload must be JSON-serialisable: {exc}") from exc
        if not isinstance(self.timestamp, datetime) or self.timestamp.tzinfo is None:
            raise ValueError("timestamp must be a timezone-aware datetime")
        for name in ("event_id", "correlation_id", "user_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be a non-empty string")
        if self.device_id is not None and not isinstance(self.device_id, str):
            raise ValueError("device_id must be a string or None")
        # Accept plain strings for the enums (e.g. from JSON) but normalise to the enum.
        object.__setattr__(self, "sensitivity", Sensitivity(self.sensitivity))
        object.__setattr__(self, "priority", Priority(self.priority))
        if self.ttl is not None and (isinstance(self.ttl, bool) or not isinstance(self.ttl, int)):
            raise ValueError("ttl must be an int (seconds) or None")
        if self.ttl is not None and self.ttl < 0:
            raise ValueError("ttl must be >= 0")

    @classmethod
    def new(
        cls,
        type: str,
        source: str,
        payload: dict[str, Any] | None = None,
        *,
        correlation_id: str | None = None,
        user_id: str = DEFAULT_USER_ID,
        device_id: str | None = None,
        sensitivity: Sensitivity | str = Sensitivity.PRIVATE,
        priority: Priority | str = Priority.NORMAL,
        ttl: int | None = None,
    ) -> Event:
        return cls(
            type=type,
            source=source,
            payload=dict(payload or {}),
            correlation_id=correlation_id or new_id(),
            user_id=user_id,
            device_id=device_id,
            sensitivity=Sensitivity(sensitivity),
            priority=Priority(priority),
            ttl=ttl,
        )

    def follow_up(
        self,
        type: str,
        source: str,
        payload: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Event:
        """Create a new event in the same correlation chain (same correlation_id/user/device)."""
        kwargs.setdefault("user_id", self.user_id)
        kwargs.setdefault("device_id", self.device_id)
        kwargs.setdefault("sensitivity", self.sensitivity)
        return Event.new(type, source, payload, correlation_id=self.correlation_id, **kwargs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "type": self.type,
            "timestamp": self.timestamp.isoformat(),
            "source": self.source,
            "correlation_id": self.correlation_id,
            "user_id": self.user_id,
            "device_id": self.device_id,
            "sensitivity": self.sensitivity.value,
            "priority": self.priority.value,
            "payload": self.payload,
            "ttl": self.ttl,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Event:
        ts = data["timestamp"]
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts)
        return cls(
            event_id=data["event_id"],
            type=data["type"],
            timestamp=ts,
            source=data["source"],
            correlation_id=data["correlation_id"],
            user_id=data.get("user_id", DEFAULT_USER_ID),
            device_id=data.get("device_id"),
            sensitivity=Sensitivity(data.get("sensitivity", Sensitivity.PRIVATE)),
            priority=Priority(data.get("priority", Priority.NORMAL)),
            payload=dict(data.get("payload") or {}),
            ttl=data.get("ttl"),
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> Event:
        return cls.from_dict(json.loads(raw))
