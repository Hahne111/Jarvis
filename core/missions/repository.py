"""Durable mission snapshots (SQLAlchemy Core; shares the engine with SQLEventStore).

The event log is the source of truth; this table is the queryable current state and is
rebuildable from the log via ``MissionEngine.rebuild_from_log``.
"""

from __future__ import annotations

import json

from sqlalchemy import Column, Integer, MetaData, String, Table, Text, create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from core.missions.model import Mission, MissionStatus

metadata = MetaData()

missions_table = Table(
    "missions",
    metadata,
    Column("mission_id", String(36), primary_key=True),
    Column("status", String(32), nullable=False, index=True),
    Column("priority", String(16), nullable=False),
    Column("owner", String(200), nullable=False),
    Column("version", Integer, nullable=False),
    Column("updated_at", String(40), nullable=False),
    Column("doc", Text, nullable=False),  # full Mission.to_dict() incl. tasks
)


class MissionRepository:
    def __init__(self, url: str | None = None, *, engine: Engine | None = None) -> None:
        if engine is not None:
            self._engine = engine
        elif url is None or url in ("sqlite://", "sqlite:///:memory:"):
            self._engine = create_engine(
                "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
            )
        else:
            self._engine = create_engine(url)
        metadata.create_all(self._engine)

    @property
    def engine(self) -> Engine:
        return self._engine

    def save(self, mission: Mission) -> None:
        row = {
            "mission_id": mission.mission_id,
            "status": mission.status.value,
            "priority": mission.priority.value,
            "owner": mission.owner,
            "version": mission.version,
            "updated_at": mission.updated_at.isoformat(),
            "doc": json.dumps(mission.to_dict(), separators=(",", ":"), sort_keys=True),
        }
        with self._engine.begin() as conn:
            existing = conn.execute(
                select(missions_table.c.mission_id).where(
                    missions_table.c.mission_id == mission.mission_id
                )
            ).scalar_one_or_none()
            if existing is None:
                conn.execute(missions_table.insert().values(**row))
            else:
                conn.execute(
                    missions_table.update()
                    .where(missions_table.c.mission_id == mission.mission_id)
                    .values(**row)
                )

    def get(self, mission_id: str) -> Mission | None:
        stmt = select(missions_table.c.doc).where(missions_table.c.mission_id == mission_id)
        with self._engine.connect() as conn:
            raw = conn.execute(stmt).scalar_one_or_none()
        return Mission.from_dict(json.loads(raw)) if raw is not None else None

    def list(self, status: MissionStatus | None = None) -> list[Mission]:
        stmt = select(missions_table.c.doc).order_by(missions_table.c.updated_at)
        if status is not None:
            stmt = stmt.where(missions_table.c.status == MissionStatus(status).value)
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).scalars().all()
        return [Mission.from_dict(json.loads(raw)) for raw in rows]

    def delete_all(self) -> None:
        with self._engine.begin() as conn:
            conn.execute(missions_table.delete())
