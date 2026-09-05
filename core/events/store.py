"""Durable, ordered, replayable event persistence (SPEC §5.3: every transition is persisted;
Phase 1 exit criterion: events are correlated and replayable).

``SQLEventStore`` uses SQLAlchemy Core so the same code runs on SQLite (tests, offline/safe-boot)
and PostgreSQL (Home Core). Rows are append-only; ``seq`` is the total order used for replay.
"""

from __future__ import annotations

import abc
from collections.abc import Iterator

from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    func,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

from core.events.envelope import Event

metadata = MetaData()

events_table = Table(
    "events",
    metadata,
    Column("seq", Integer, primary_key=True, autoincrement=True),
    Column("event_id", String(36), nullable=False, unique=True),
    Column("type", String(200), nullable=False, index=True),
    Column("timestamp", String(40), nullable=False),
    Column("source", String(200), nullable=False),
    Column("correlation_id", String(36), nullable=False, index=True),
    Column("user_id", String(200), nullable=False),
    Column("device_id", String(200), nullable=True),
    Column("sensitivity", String(16), nullable=False),
    Column("priority", String(16), nullable=False),
    Column("payload", Text, nullable=False),
    Column("ttl", Integer, nullable=True),
)


class EventStore(abc.ABC):
    """Append-only event log."""

    @abc.abstractmethod
    def append(self, event: Event) -> bool:
        """Persist ``event``. Returns False if an event with the same event_id already exists."""

    @abc.abstractmethod
    def get(self, event_id: str) -> Event | None: ...

    @abc.abstractmethod
    def replay(
        self,
        *,
        after_seq: int = 0,
        correlation_id: str | None = None,
        type_prefix: str | None = None,
        limit: int | None = None,
    ) -> list[tuple[int, Event]]:
        """Return ``(seq, event)`` pairs in append order, optionally filtered."""

    @abc.abstractmethod
    def count(self) -> int: ...

    @abc.abstractmethod
    def last_seq(self) -> int: ...

    def iter_events(self, **filters: object) -> Iterator[Event]:
        for _, event in self.replay(**filters):  # type: ignore[arg-type]
            yield event


class SQLEventStore(EventStore):
    """SQLAlchemy-backed store. ``url`` is any SQLAlchemy URL (sqlite:///path.db, postgresql://...)."""

    def __init__(self, url: str, *, engine: Engine | None = None) -> None:
        if engine is not None:
            self._engine = engine
        elif url == "sqlite://" or url == "sqlite:///:memory:":
            # One shared in-memory database for the lifetime of this store.
            self._engine = create_engine(
                "sqlite://",
                poolclass=StaticPool,
                connect_args={"check_same_thread": False},
            )
        else:
            self._engine = create_engine(url)
        metadata.create_all(self._engine)

    @classmethod
    def in_memory(cls) -> SQLEventStore:
        return cls("sqlite://")

    @property
    def engine(self) -> Engine:
        return self._engine

    def append(self, event: Event) -> bool:
        row = event.to_dict()
        row["payload"] = event.to_json()  # full envelope as JSON keeps the row self-describing
        try:
            with self._engine.begin() as conn:
                conn.execute(events_table.insert().values(**row))
        except IntegrityError:
            return False
        return True

    def get(self, event_id: str) -> Event | None:
        stmt = select(events_table.c.payload).where(events_table.c.event_id == event_id)
        with self._engine.connect() as conn:
            raw = conn.execute(stmt).scalar_one_or_none()
        return Event.from_json(raw) if raw is not None else None

    def replay(
        self,
        *,
        after_seq: int = 0,
        correlation_id: str | None = None,
        type_prefix: str | None = None,
        limit: int | None = None,
    ) -> list[tuple[int, Event]]:
        stmt = (
            select(events_table.c.seq, events_table.c.payload)
            .where(events_table.c.seq > after_seq)
            .order_by(events_table.c.seq)
        )
        if correlation_id is not None:
            stmt = stmt.where(events_table.c.correlation_id == correlation_id)
        if type_prefix is not None:
            stmt = stmt.where(
                (events_table.c.type == type_prefix)
                | events_table.c.type.like(type_prefix.replace("%", r"\%") + ".%", escape="\\")
            )
        if limit is not None:
            stmt = stmt.limit(limit)
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return [(seq, Event.from_json(raw)) for seq, raw in rows]

    def count(self) -> int:
        with self._engine.connect() as conn:
            return int(conn.execute(select(func.count()).select_from(events_table)).scalar_one())

    def last_seq(self) -> int:
        with self._engine.connect() as conn:
            value = conn.execute(select(func.max(events_table.c.seq))).scalar_one()
        return int(value or 0)
