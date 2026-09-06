"""News capabilities (P0, read/ingest only - no world side effects, no verifier needed).

news.refresh  pull the configured sources through the pipeline (bounded, evented)
news.top      top events, optionally by country/topic - what the voice/globe answer is built from
"""

from __future__ import annotations

from typing import Any

from core.capabilities.manifest import CapabilityManifest
from core.capabilities.registry import CapabilityRegistry
from core.news.geo import COUNTRIES, resolve_country
from core.news.pipeline import NewsPipeline
from core.permissions.model import RiskLevel

NEWS_MANIFESTS: tuple[CapabilityManifest, ...] = (
    CapabilityManifest(
        name="news.refresh",
        version="1.0",
        risk=RiskLevel.P0,
        inputs={},
        timeout_ms=60_000,
        description="Fetch the configured news sources and update the world event model.",
    ),
    CapabilityManifest(
        name="news.top",
        version="1.0",
        risk=RiskLevel.P0,
        inputs={"country": "string?", "topic": "string?", "limit": "integer?"},
        description="Top world events (optionally for a country name/ISO code and a topic).",
    ),
)


def register_news(registry: CapabilityRegistry, pipeline: NewsPipeline) -> CapabilityRegistry:
    async def refresh(args: dict[str, Any]) -> dict[str, Any]:
        return await pipeline.refresh()

    async def top(args: dict[str, Any]) -> dict[str, Any]:
        raw_country = args.get("country")
        iso = None
        if raw_country:
            iso = (
                raw_country.upper()
                if raw_country.upper() in COUNTRIES
                else resolve_country(raw_country)
            )
        limit = max(1, min(25, int(args.get("limit") or 8)))
        events = pipeline.top(country_iso=iso, topic=args.get("topic"), limit=limit)
        spoken = "; ".join(f"{e.title}{' - provisional' if e.breaking else ''}" for e in events[:5])
        return {
            "country": iso,
            "topic": args.get("topic"),
            "count": len(events),
            "events": [e.to_dict() for e in events],
            "spoken": spoken or "No news events yet - run news.refresh.",
        }

    registry.register(NEWS_MANIFESTS[0], refresh)
    registry.register(NEWS_MANIFESTS[1], top)
    return registry
