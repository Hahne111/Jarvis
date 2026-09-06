"""World Intelligence Globe data side (SPEC §13, Phase 10): news events, pipeline, sources."""

from core.news.capabilities import NEWS_MANIFESTS, register_news
from core.news.geo import COUNTRIES, Country, detect_countries, detect_topics, resolve_country
from core.news.model import NewsEvent, RawItem, SourceRef, similarity, tokens
from core.news.pipeline import (
    FakeNewsSource,
    NewsPipeline,
    NewsSourceAdapter,
    RssSource,
    parse_feed,
    sources_from_env,
)
from core.news.store import NewsStore

__all__ = [
    "COUNTRIES",
    "NEWS_MANIFESTS",
    "Country",
    "FakeNewsSource",
    "NewsEvent",
    "NewsPipeline",
    "NewsSourceAdapter",
    "NewsStore",
    "RawItem",
    "RssSource",
    "SourceRef",
    "detect_countries",
    "detect_topics",
    "parse_feed",
    "register_news",
    "resolve_country",
    "similarity",
    "sources_from_env",
    "tokens",
]
