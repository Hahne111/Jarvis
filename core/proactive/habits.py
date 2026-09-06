"""Habit Detector + Automation Suggestions (SPEC §14/§15, Phase 11 steps 70/71/74).

The detector reads only the persisted ``command.received`` log: the same fast-path intent
(capability + args) repeated on >= 3 different days inside the same ~2 h window becomes a
*suggestion* ("turn on the kitchen light around 07:10 on weekdays?"). A suggestion is never
activated by JARVIS (SPEC §15 autonomy table: routines with external actions only after the
owner's ok). Accepting creates a scheduler job; dismissing silences the key for 30 days.

Predictive preloading (step 74) is the harmless twin: for a detected habit whose preparation is
a P0 capability (news.top -> news.refresh, workspace.* -> workspace.list), the detector
proposes a P0 preload job ten minutes earlier; these are created automatically because they can
only read/prepare - the owner sees them in the schedule and can delete them.
"""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Column, MetaData, String, Table, Text, select
from sqlalchemy.engine import Engine

from core.events.bus import EventBus
from core.events.envelope import Event

SOURCE = "proactive"
MIN_DAYS = 3
WINDOW_MINUTES = 60  # +/- around the median minute of day
DISMISS_DAYS = 30
PRELOAD_FOR: dict[str, tuple[str, dict[str, Any]]] = {
    "news.top": ("news.refresh", {}),
    "workspace.list": ("workspace.list", {}),
    "home.list_devices": ("home.list_devices", {}),
}

metadata = MetaData()
suggestions_table = Table(
    "suggestions",
    metadata,
    Column("suggestion_id", String(16), primary_key=True),
    Column("key", String(200), nullable=False, index=True),
    Column("status", String(16), nullable=False, index=True),
    Column("updated_at", String(40), nullable=False),
    Column("doc", Text, nullable=False),
)


@dataclass
class Suggestion:
    key: str  # capability + canonical args
    title: str
    command: str  # the text command that would be scheduled
    at: str  # HH:MM
    weekdays: tuple[int, ...] | None
    evidence: dict[str, Any]
    confidence: float
    kind: str = "routine"  # routine | preload
    status: str = "pending"  # pending | accepted | dismissed
    suggestion_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    job_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "suggestion_id": self.suggestion_id,
            "key": self.key,
            "kind": self.kind,
            "title": self.title,
            "command": self.command,
            "at": self.at,
            "weekdays": list(self.weekdays) if self.weekdays is not None else None,
            "evidence": self.evidence,
            "confidence": round(self.confidence, 2),
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "job_id": self.job_id,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Suggestion:
        return cls(
            key=d["key"],
            title=d["title"],
            command=d["command"],
            at=d["at"],
            weekdays=tuple(d["weekdays"]) if d.get("weekdays") is not None else None,
            evidence=dict(d.get("evidence") or {}),
            confidence=float(d.get("confidence", 0.7)),
            kind=d.get("kind", "routine"),
            status=d.get("status", "pending"),
            suggestion_id=d["suggestion_id"],
            created_at=datetime.fromisoformat(d["created_at"]),
            updated_at=datetime.fromisoformat(d["updated_at"]),
            job_id=d.get("job_id"),
        )


class SuggestionStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        metadata.create_all(engine)

    def save(self, s: Suggestion) -> Suggestion:
        row = {
            "suggestion_id": s.suggestion_id,
            "key": s.key,
            "status": s.status,
            "updated_at": s.updated_at.isoformat(),
            "doc": json.dumps(s.to_dict(), separators=(",", ":"), sort_keys=True),
        }
        with self._engine.begin() as conn:
            exists = conn.execute(
                select(suggestions_table.c.suggestion_id).where(
                    suggestions_table.c.suggestion_id == s.suggestion_id
                )
            ).first()
            if exists:
                conn.execute(
                    suggestions_table.update()
                    .where(suggestions_table.c.suggestion_id == s.suggestion_id)
                    .values(**row)
                )
            else:
                conn.execute(suggestions_table.insert().values(**row))
        return s

    def get(self, suggestion_id: str) -> Suggestion | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(suggestions_table.c.doc).where(
                    suggestions_table.c.suggestion_id == suggestion_id
                )
            ).first()
        return Suggestion.from_dict(json.loads(row.doc)) if row else None

    def list(self, status: str | None = None) -> list[Suggestion]:
        stmt = select(suggestions_table.c.doc).order_by(suggestions_table.c.updated_at.desc())
        if status:
            stmt = stmt.where(suggestions_table.c.status == status)
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return [Suggestion.from_dict(json.loads(r.doc)) for r in rows]

    def by_key(self, key: str) -> Suggestion | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(suggestions_table.c.doc)
                .where(suggestions_table.c.key == key)
                .order_by(suggestions_table.c.updated_at.desc())
            ).first()
        return Suggestion.from_dict(json.loads(row.doc)) if row else None


def _hhmm(minute_of_day: int) -> str:
    return f"{minute_of_day // 60:02d}:{minute_of_day % 60:02d}"


class HabitDetector:
    def __init__(self, bus: EventBus, store: SuggestionStore, *, window_days: int = 14) -> None:
        self.bus = bus
        self.store = store
        self.window_days = window_days

    def observations(self, now: datetime) -> dict[str, list[tuple[datetime, str]]]:
        """key -> [(timestamp, text)] from fast-path commands the owner gave (not the scheduler)."""
        since = now - timedelta(days=self.window_days)
        out: dict[str, list[tuple[datetime, str]]] = defaultdict(list)
        for _, ev in self.bus.replay(type_prefix="command.received"):
            if ev.timestamp < since or ev.source == "scheduler":
                continue
            intent = (ev.payload or {}).get("intent") or {}
            if intent.get("kind") != "capability" or not intent.get("capability"):
                continue
            key = intent["capability"] + " " + json.dumps(intent.get("args") or {}, sort_keys=True)
            out[key].append((ev.timestamp, (ev.payload or {}).get("text") or ""))
        return out

    async def scan(self, now: datetime | None = None) -> list[Suggestion]:
        now = now or datetime.now(UTC)
        created: list[Suggestion] = []
        for key, obs in self.observations(now).items():
            minutes = sorted(t.hour * 60 + t.minute for t, _ in obs)
            days = {t.date() for t, _ in obs}
            if len(days) < MIN_DAYS:
                continue
            median = minutes[len(minutes) // 2]
            inside = [
                (t, txt) for t, txt in obs if abs(t.hour * 60 + t.minute - median) <= WINDOW_MINUTES
            ]
            if len({t.date() for t, _ in inside}) < MIN_DAYS:
                continue
            existing = self.store.by_key(key)
            if existing is not None and (
                existing.status in ("pending", "accepted")
                or (
                    existing.status == "dismissed"
                    and now - existing.updated_at < timedelta(days=DISMISS_DAYS)
                )
            ):
                continue
            weekday_only = all(t.weekday() < 5 for t, _ in inside)
            weekend_only = all(t.weekday() >= 5 for t, _ in inside)
            weekdays = (0, 1, 2, 3, 4) if weekday_only else (5, 6) if weekend_only else None
            text = inside[-1][1]
            capability = key.split(" ", 1)[0]
            confidence = min(0.95, 0.5 + 0.1 * len({t.date() for t, _ in inside}))
            s = Suggestion(
                key=key,
                title=f"Routine: '{text}' around {_hhmm(median)}"
                + (" on weekdays" if weekday_only else " on weekends" if weekend_only else ""),
                command=text,
                at=_hhmm(median),
                weekdays=weekdays,
                evidence={"days": len(days), "observations": len(inside), "capability": capability},
                confidence=confidence,
                created_at=now,
                updated_at=now,
            )
            self.store.save(s)
            created.append(s)
            await self.bus.publish(
                Event.new("habit.detected", SOURCE, s.to_dict(), correlation_id="proactive")
            )
            await self.bus.publish(
                Event.new("automation.suggested", SOURCE, s.to_dict(), correlation_id="proactive")
            )
        return created

    @staticmethod
    def preload_for(s: Suggestion) -> tuple[str, dict[str, Any], str] | None:
        """(capability, args, HH:MM ten minutes earlier) for a habit that has a P0 preparation."""
        capability = str(s.evidence.get("capability") or "")
        if capability not in PRELOAD_FOR:
            return None
        cap, args = PRELOAD_FOR[capability]
        hh, mm = (int(x) for x in s.at.split(":"))
        minute = (hh * 60 + mm - 10) % (24 * 60)
        return cap, dict(args), _hhmm(minute)
