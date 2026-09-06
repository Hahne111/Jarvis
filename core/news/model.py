"""News event data model (SPEC §13, Phase 10 step 66).

A ``NewsEvent`` is one *story*: several raw items from different sources collapse into one event
(cluster). Every event carries where it happened (ISO-2 country, region, lat/lon for the globe),
what it is about (topics), how well it is supported (sources with quality, confidence) and
whether it is still provisional (``breaking``: one source, very fresh). Forecasts are never mixed
with facts: an item whose title reads like a prediction is tagged ``forecast``.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

_WS = re.compile(r"[^a-z0-9äöüß]+")
_STOP = frozenset(
    "the a an and or of to in on for with at by from is are was were be been as that this it its "
    "der die das und oder von zu im in am für mit bei aus ist sind war waren wird werden ein eine "
    "einer eines einem einen des dem den als auch nach über auf um so bis wie nicht new says said "
    "after before amid into over under update live breaking".split()
)
_FORECAST = re.compile(
    r"\b(could|may|might|expected to|forecast|prognose|könnte|dürfte|erwartet|prediction|"
    r"will likely|voraussichtlich|outlook)\b",
    re.I,
)


def tokens(text: str) -> frozenset[str]:
    """Lower-cased content words used for dedupe/clustering (order-free)."""
    out = set()
    for t in _WS.split((text or "").lower()):
        if len(t) <= 2 or t in _STOP:
            continue
        if len(t) > 4 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]  # crude plural stemming keeps "centres"/"centre" together
        out.add(t)
    return frozenset(out)


def similarity(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def stable_id(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class SourceRef:
    name: str
    url: str
    quality: float  # 0..1 editorial quality / reliability of the source
    published_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "url": self.url,
            "quality": round(self.quality, 2),
            "published_at": self.published_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SourceRef:
        return cls(
            d["name"], d["url"], float(d["quality"]), datetime.fromisoformat(d["published_at"])
        )


@dataclass(frozen=True)
class RawItem:
    """What a source adapter returns before the pipeline normalises it."""

    title: str
    summary: str
    url: str
    published_at: datetime
    source_name: str
    source_quality: float = 0.5

    @property
    def key(self) -> str:
        return stable_id(self.source_name, self.url or self.title)


@dataclass
class NewsEvent:
    title: str
    summary: str
    country: str | None  # ISO-2
    region: str | None
    lat: float | None
    lon: float | None
    topics: tuple[str, ...]
    sources: tuple[SourceRef, ...]
    confidence: float
    published_at: datetime
    breaking: bool = False
    forecast: bool = False
    cluster_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    ingested_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    version: int = 1

    @property
    def source_names(self) -> set[str]:
        return {s.name for s in self.sources}

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "cluster_id": self.cluster_id,
            "title": self.title,
            "summary": self.summary,
            "country": self.country,
            "region": self.region,
            "lat": self.lat,
            "lon": self.lon,
            "topics": list(self.topics),
            "sources": [s.to_dict() for s in self.sources],
            "source_count": len(self.sources),
            "confidence": round(self.confidence, 2),
            "breaking": self.breaking,
            "forecast": self.forecast,
            "published_at": self.published_at.isoformat(),
            "ingested_at": self.ingested_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> NewsEvent:
        return cls(
            title=d["title"],
            summary=d["summary"],
            country=d.get("country"),
            region=d.get("region"),
            lat=d.get("lat"),
            lon=d.get("lon"),
            topics=tuple(d.get("topics", [])),
            sources=tuple(SourceRef.from_dict(s) for s in d.get("sources", [])),
            confidence=float(d["confidence"]),
            published_at=datetime.fromisoformat(d["published_at"]),
            breaking=bool(d.get("breaking", False)),
            forecast=bool(d.get("forecast", False)),
            cluster_id=d["cluster_id"],
            event_id=d["event_id"],
            ingested_at=datetime.fromisoformat(d["ingested_at"]),
            updated_at=datetime.fromisoformat(d["updated_at"]),
            version=int(d.get("version", 1)),
        )


def looks_like_forecast(text: str) -> bool:
    return bool(_FORECAST.search(text or ""))
