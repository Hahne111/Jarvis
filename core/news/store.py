"""Durable news events (SQLAlchemy Core; shares the Core engine)."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from typing import Any

from sqlalchemy import Column, Float, Integer, MetaData, String, Table, Text, delete, select
from sqlalchemy.engine import Engine

from core.news.model import NewsEvent

metadata = MetaData()

news_table = Table(
    "news_events",
    metadata,
    Column("event_id", String(32), primary_key=True),
    Column("cluster_id", String(32), nullable=False, index=True),
    Column("country", String(2), nullable=True, index=True),
    Column("region", String(40), nullable=True),
    Column("published_at", String(40), nullable=False, index=True),
    Column("updated_at", String(40), nullable=False),
    Column("confidence", Float, nullable=False),
    Column("breaking", Integer, nullable=False),
    Column("doc", Text, nullable=False),
)

seen_table = Table(  # raw item keys already ingested (dedupe across refreshes)
    "news_seen",
    metadata,
    Column("item_key", String(32), primary_key=True),
    Column("event_id", String(32), nullable=False),
    Column("seen_at", String(40), nullable=False),
)


class NewsStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        metadata.create_all(engine)

    def save(self, ev: NewsEvent) -> None:
        row = {
            "event_id": ev.event_id,
            "cluster_id": ev.cluster_id,
            "country": ev.country,
            "region": ev.region,
            "published_at": ev.published_at.isoformat(),
            "updated_at": ev.updated_at.isoformat(),
            "confidence": ev.confidence,
            "breaking": 1 if ev.breaking else 0,
            "doc": json.dumps(ev.to_dict(), separators=(",", ":"), sort_keys=True),
        }
        with self._engine.begin() as conn:
            existing = conn.execute(
                select(news_table.c.event_id).where(news_table.c.event_id == ev.event_id)
            ).first()
            if existing:
                conn.execute(
                    news_table.update().where(news_table.c.event_id == ev.event_id).values(**row)
                )
            else:
                conn.execute(news_table.insert().values(**row))

    def get(self, event_id: str) -> NewsEvent | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(news_table.c.doc).where(news_table.c.event_id == event_id)
            ).first()
        return NewsEvent.from_dict(json.loads(row.doc)) if row else None

    def recent(
        self,
        *,
        limit: int = 500,
        country: str | None = None,
        topic: str | None = None,
        since: datetime | None = None,
        include_forecasts: bool = True,
    ) -> list[NewsEvent]:
        stmt = select(news_table.c.doc).order_by(news_table.c.published_at.desc())
        if country:
            stmt = stmt.where(news_table.c.country == country.upper())
        if since is not None:
            stmt = stmt.where(news_table.c.published_at >= since.isoformat())
        with self._engine.connect() as conn:
            rows = conn.execute(stmt.limit(max(limit * 4, 200))).all()
        events = [NewsEvent.from_dict(json.loads(r.doc)) for r in rows]
        if topic:
            events = [e for e in events if topic in e.topics]
        if not include_forecasts:
            events = [e for e in events if not e.forecast]
        return events[:limit]

    def by_country(self, since: datetime | None = None) -> dict[str, int]:
        stmt = select(news_table.c.country).where(news_table.c.country.is_not(None))
        if since is not None:
            stmt = stmt.where(news_table.c.published_at >= since.isoformat())
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return dict(Counter(r.country for r in rows))

    def count(self) -> int:
        with self._engine.connect() as conn:
            return len(conn.execute(select(news_table.c.event_id)).all())

    # -- dedupe bookkeeping --------------------------------------------------------------------

    def seen(self, item_key: str) -> str | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(seen_table.c.event_id).where(seen_table.c.item_key == item_key)
            ).first()
        return row.event_id if row else None

    def mark_seen(self, item_key: str, event_id: str, when: datetime) -> None:
        with self._engine.begin() as conn:
            conn.execute(delete(seen_table).where(seen_table.c.item_key == item_key))
            conn.execute(
                seen_table.insert().values(
                    item_key=item_key, event_id=event_id, seen_at=when.isoformat()
                )
            )

    def snapshot(self) -> dict[str, Any]:
        return {"events": self.count(), "countries": self.by_country()}
