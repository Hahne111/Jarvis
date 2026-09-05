"""MemoryStore: typed memory rows on SQLAlchemy Core (same engine as the event log).

Retrieval = lexical token overlap (always) + cosine similarity (when an Embedder is configured),
weighted by confidence and recency. pgvector can replace the in-Python cosine later without
changing callers.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    Column,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    delete,
    func,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from core.memory.embedding import Embedder, cosine, tokenize
from core.memory.model import MemoryItem, MemoryType, Retention

SEMANTIC_MIN = 0.5

metadata = MetaData()

memory_table = Table(
    "memory_items",
    metadata,
    Column("memory_id", String(36), primary_key=True),
    Column("type", String(16), nullable=False, index=True),
    Column("subject", String(200), nullable=False, index=True),
    Column("predicate", String(200), nullable=False, index=True),
    Column("project_scope", String(200), nullable=True, index=True),
    Column("owner", String(200), nullable=False),
    Column("confidence", Float, nullable=False),
    Column("source", String(32), nullable=False),
    Column("observations", Integer, nullable=False),
    Column("sensitivity", String(16), nullable=False),
    Column("retention", String(16), nullable=False),
    Column("created_at", String(40), nullable=False),
    Column("last_confirmed_at", String(40), nullable=False, index=True),
    Column("expires_at", String(40), nullable=True),
    Column("pinned", Boolean, nullable=False, default=False),
    Column("superseded_by", String(36), nullable=True, index=True),
    Column("doc", Text, nullable=False),  # full MemoryItem.to_dict()
    Column("text_index", Text, nullable=False),  # lowercased searchable text
    Column("embedding", Text, nullable=True),  # JSON list[float] when an embedder is configured
)


class MemoryStore:
    def __init__(
        self,
        url: str | None = None,
        *,
        engine: Engine | None = None,
        embedder: Embedder | None = None,
    ) -> None:
        if engine is not None:
            self._engine = engine
        elif url is None or url in ("sqlite://", "sqlite:///:memory:"):
            self._engine = create_engine(
                "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
            )
        else:
            self._engine = create_engine(url)
        metadata.create_all(self._engine)
        self._embedder = embedder

    @property
    def engine(self) -> Engine:
        return self._engine

    # -- write -----------------------------------------------------------------------------------

    def save(self, item: MemoryItem) -> None:
        row = {
            "memory_id": item.memory_id,
            "type": item.type.value,
            "subject": item.subject,
            "predicate": item.predicate,
            "project_scope": item.project_scope,
            "owner": item.owner,
            "confidence": float(item.confidence),
            "source": item.source.value,
            "observations": item.observations,
            "sensitivity": item.sensitivity.value,
            "retention": item.retention.value,
            "created_at": item.created_at.isoformat(),
            "last_confirmed_at": item.last_confirmed_at.isoformat(),
            "expires_at": item.expires_at.isoformat() if item.expires_at else None,
            "pinned": item.pinned,
            "superseded_by": item.superseded_by,
            "doc": json.dumps(item.to_dict(), sort_keys=True),
            "text_index": item.searchable_text(),
            "embedding": (
                json.dumps(self._embedder.embed([item.searchable_text()])[0])
                if self._embedder is not None
                else None
            ),
        }
        with self._engine.begin() as conn:
            exists = conn.execute(
                select(memory_table.c.memory_id).where(memory_table.c.memory_id == item.memory_id)
            ).scalar_one_or_none()
            if exists is None:
                conn.execute(memory_table.insert().values(**row))
            else:
                conn.execute(
                    memory_table.update()
                    .where(memory_table.c.memory_id == item.memory_id)
                    .values(**row)
                )

    def delete(self, memory_id: str) -> bool:
        with self._engine.begin() as conn:
            res = conn.execute(delete(memory_table).where(memory_table.c.memory_id == memory_id))
        return bool(res.rowcount)

    def delete_many(self, memory_ids: list[str]) -> int:
        if not memory_ids:
            return 0
        with self._engine.begin() as conn:
            res = conn.execute(delete(memory_table).where(memory_table.c.memory_id.in_(memory_ids)))
        return int(res.rowcount or 0)

    # -- read ------------------------------------------------------------------------------------

    def get(self, memory_id: str) -> MemoryItem | None:
        with self._engine.connect() as conn:
            raw = conn.execute(
                select(memory_table.c.doc).where(memory_table.c.memory_id == memory_id)
            ).scalar_one_or_none()
        return MemoryItem.from_dict(json.loads(raw)) if raw else None

    def count(self, *, active_only: bool = True) -> int:
        stmt = select(func.count()).select_from(memory_table)
        if active_only:
            stmt = stmt.where(memory_table.c.superseded_by.is_(None))
        with self._engine.connect() as conn:
            return int(conn.execute(stmt).scalar_one())

    def find(
        self,
        subject: str,
        predicate: str,
        *,
        project_scope: str | None = None,
        include_superseded: bool = False,
    ) -> list[MemoryItem]:
        stmt = select(memory_table.c.doc).where(
            memory_table.c.subject == subject, memory_table.c.predicate == predicate
        )
        if project_scope is None:
            stmt = stmt.where(memory_table.c.project_scope.is_(None))
        else:
            stmt = stmt.where(memory_table.c.project_scope == project_scope)
        if not include_superseded:
            stmt = stmt.where(memory_table.c.superseded_by.is_(None))
        stmt = stmt.order_by(memory_table.c.created_at)
        with self._engine.connect() as conn:
            return [MemoryItem.from_dict(json.loads(r)) for r in conn.execute(stmt).scalars()]

    def list(
        self,
        *,
        type: MemoryType | str | None = None,
        project_scope: str | None = None,
        include_superseded: bool = False,
        include_expired: bool = False,
        now: datetime | None = None,
    ) -> list[MemoryItem]:
        stmt = select(memory_table.c.doc).order_by(memory_table.c.last_confirmed_at.desc())
        if type is not None:
            stmt = stmt.where(memory_table.c.type == MemoryType(type).value)
        if project_scope is not None:
            stmt = stmt.where(memory_table.c.project_scope == project_scope)
        if not include_superseded:
            stmt = stmt.where(memory_table.c.superseded_by.is_(None))
        with self._engine.connect() as conn:
            items = [MemoryItem.from_dict(json.loads(r)) for r in conn.execute(stmt).scalars()]
        if not include_expired:
            now = now or datetime.now(UTC)
            items = [i for i in items if not i.is_expired(now)]
        return items

    def search(
        self,
        query: str,
        *,
        type: MemoryType | str | None = None,
        project_scope: str | None = None,
        limit: int = 10,
        now: datetime | None = None,
        min_score: float = 0.05,
    ) -> list[tuple[float, MemoryItem]]:
        """Rank active, unexpired items by lexical overlap (+ cosine if embedded), confidence
        and recency. Project-scoped queries also see unscoped memories."""
        q_tokens = set(tokenize(query))
        if not q_tokens:
            return []
        now = now or datetime.now(UTC)
        q_vec = self._embedder.embed([query])[0] if self._embedder is not None else None
        stmt = select(
            memory_table.c.doc, memory_table.c.text_index, memory_table.c.embedding
        ).where(memory_table.c.superseded_by.is_(None))
        if type is not None:
            stmt = stmt.where(memory_table.c.type == MemoryType(type).value)
        if project_scope is not None:
            stmt = stmt.where(
                (memory_table.c.project_scope == project_scope)
                | memory_table.c.project_scope.is_(None)
            )
        scored: list[tuple[float, MemoryItem]] = []
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        for doc, text_index, emb in rows:
            item = MemoryItem.from_dict(json.loads(doc))
            if item.is_expired(now):
                continue
            tokens = set(tokenize(text_index))
            lexical = len(q_tokens & tokens) / len(q_tokens)
            semantic = cosine(q_vec, json.loads(emb)) if (q_vec is not None and emb) else 0.0
            if semantic < SEMANTIC_MIN:  # below this, hashed/embedded similarity is noise
                semantic = 0.0
            relevance = max(lexical, semantic)
            if relevance <= 0:
                continue
            age_days = max(0.0, (now - item.last_confirmed_at).total_seconds() / 86400)
            recency = 1.0 / (1.0 + age_days / 30.0)  # half weight after a month
            score = relevance * (0.6 + 0.4 * float(item.confidence)) * (0.7 + 0.3 * recency)
            if item.pinned:
                score *= 1.1
            if score >= min_score:
                scored.append((round(score, 4), item))
        scored.sort(key=lambda s: (-s[0], s[1].created_at))
        return scored[:limit]

    # -- maintenance -----------------------------------------------------------------------------

    def expired(self, now: datetime | None = None) -> list[MemoryItem]:
        now = now or datetime.now(UTC)
        return [
            i
            for i in self.list(include_expired=True, now=now)
            if i.is_expired(now) and not i.pinned
        ]

    def session_items(self) -> list[MemoryItem]:
        return [i for i in self.list(include_expired=True) if i.retention is Retention.SESSION]

    def created_or_confirmed_since(self, since: datetime) -> list[MemoryItem]:
        return [
            i
            for i in self.list(include_superseded=True, include_expired=True)
            if i.created_at >= since or i.last_confirmed_at >= since
        ]
