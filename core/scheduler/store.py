"""Durable job table (shares the Core engine)."""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import Column, Integer, MetaData, String, Table, Text, delete, select
from sqlalchemy.engine import Engine

from core.scheduler.model import Job

metadata = MetaData()

jobs_table = Table(
    "jobs",
    metadata,
    Column("job_id", String(16), primary_key=True),
    Column("name", String(120), nullable=False),
    Column("enabled", Integer, nullable=False),
    Column("source", String(16), nullable=False),
    Column("next_run_at", String(40), nullable=True, index=True),
    Column("doc", Text, nullable=False),
)


class JobStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        metadata.create_all(engine)

    def save(self, job: Job) -> Job:
        row = {
            "job_id": job.job_id,
            "name": job.name,
            "enabled": 1 if job.enabled else 0,
            "source": job.source,
            "next_run_at": job.next_run_at.isoformat() if job.next_run_at else None,
            "doc": json.dumps(job.to_dict(), separators=(",", ":"), sort_keys=True),
        }
        with self._engine.begin() as conn:
            exists = conn.execute(
                select(jobs_table.c.job_id).where(jobs_table.c.job_id == job.job_id)
            ).first()
            if exists:
                conn.execute(
                    jobs_table.update().where(jobs_table.c.job_id == job.job_id).values(**row)
                )
            else:
                conn.execute(jobs_table.insert().values(**row))
        return job

    def get(self, job_id: str) -> Job | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(jobs_table.c.doc).where(jobs_table.c.job_id == job_id)
            ).first()
        return Job.from_dict(json.loads(row.doc)) if row else None

    def by_name(self, name: str) -> Job | None:
        with self._engine.connect() as conn:
            row = conn.execute(select(jobs_table.c.doc).where(jobs_table.c.name == name)).first()
        return Job.from_dict(json.loads(row.doc)) if row else None

    def list(self, *, enabled_only: bool = False) -> list[Job]:
        stmt = select(jobs_table.c.doc).order_by(jobs_table.c.next_run_at)
        if enabled_only:
            stmt = stmt.where(jobs_table.c.enabled == 1)
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return [Job.from_dict(json.loads(r.doc)) for r in rows]

    def due(self, now: datetime) -> list[Job]:
        return [
            j
            for j in self.list(enabled_only=True)
            if j.next_run_at is not None and j.next_run_at <= now and not j.exhausted()
        ]

    def delete(self, job_id: str) -> bool:
        with self._engine.begin() as conn:
            res = conn.execute(delete(jobs_table).where(jobs_table.c.job_id == job_id))
        return bool(res.rowcount)
