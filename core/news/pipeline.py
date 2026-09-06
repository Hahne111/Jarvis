"""News pipeline (SPEC §13.1): sources -> ingest+timestamp -> dedupe/cluster -> countries/topics
-> source quality + confidence -> summary -> geospatial event -> event bus.

Everything is deterministic and local (no model): titles are tokenised, near-duplicates cluster
by Jaccard similarity, confidence grows with the number of *independent* sources and their
quality, and a story from a single source that is younger than an hour is ``breaking`` (shown as
provisional in the HUD). Sources are adapters: ``FakeNewsSource`` for tests/demo and ``RssSource``
(RSS 2.0 / Atom over httpx with the standard-library XML parser, no new dependency).
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

from core.events.bus import EventBus
from core.events.envelope import Event
from core.news.geo import country, detect_countries, detect_topics
from core.news.model import NewsEvent, RawItem, SourceRef, looks_like_forecast, similarity, tokens
from core.news.store import NewsStore

SOURCE = "news"
CLUSTER_SIMILARITY = 0.3  # Jaccard over title+summary content words, same country
BREAKING_WINDOW = timedelta(minutes=60)
CLUSTER_WINDOW = timedelta(hours=48)


class NewsSourceAdapter(Protocol):
    name: str

    async def fetch(self) -> list[RawItem]: ...


@dataclass
class FakeNewsSource:
    name: str = "fake"
    items: list[RawItem] = field(default_factory=list)
    failing: bool = False

    async def fetch(self) -> list[RawItem]:
        if self.failing:
            raise RuntimeError("source unavailable")
        return list(self.items)


_TAG = re.compile(r"<[^>]+>")
_NS = {"atom": "http://www.w3.org/2005/Atom", "content": "http://purl.org/rss/1.0/modules/content/"}


def strip_html(text: str | None) -> str:
    return re.sub(r"\s+", " ", _TAG.sub(" ", text or "")).strip()


def parse_date(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    value = value.strip()
    for parser in (
        lambda v: datetime.fromisoformat(v.replace("Z", "+00:00")),
        parsedate_to_datetime,
    ):
        try:
            dt = parser(value)
        except (ValueError, TypeError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    return datetime.now(UTC)


def parse_feed(xml_text: str, source_name: str, quality: float) -> list[RawItem]:
    """RSS 2.0 or Atom -> RawItems. Malformed feeds yield [] instead of raising."""
    try:
        root = ET.fromstring(xml_text)  # noqa: S314 - stdlib parser, no external entities
    except ET.ParseError:
        return []
    items: list[RawItem] = []
    if root.tag.endswith("feed"):  # Atom
        for e in root.findall("atom:entry", _NS):
            link_el = e.find("atom:link", _NS)
            link = (link_el.get("href") if link_el is not None else "") or ""
            title = strip_html(e.findtext("atom:title", default="", namespaces=_NS))
            summary = strip_html(
                e.findtext("atom:summary", default="", namespaces=_NS)
                or e.findtext("atom:content", default="", namespaces=_NS)
            )
            when = e.findtext("atom:updated", default=None, namespaces=_NS) or e.findtext(
                "atom:published", default=None, namespaces=_NS
            )
            if title:
                items.append(
                    RawItem(title, summary[:600], link, parse_date(when), source_name, quality)
                )
        return items
    for it in root.iter("item"):  # RSS 2.0 (channel/item)
        title = strip_html(it.findtext("title", default=""))
        summary = strip_html(
            it.findtext("description", default="")
            or it.findtext("content:encoded", default="", namespaces=_NS)
        )
        link = (it.findtext("link", default="") or "").strip()
        when = it.findtext("pubDate", default=None)
        if title:
            items.append(
                RawItem(title, summary[:600], link, parse_date(when), source_name, quality)
            )
    return items


class RssSource:
    def __init__(
        self, name: str, url: str, quality: float = 0.6, *, timeout_s: float = 8.0
    ) -> None:
        self.name = name
        self.url = url
        self.quality = max(0.0, min(1.0, quality))
        self._timeout = timeout_s

    async def fetch(self) -> list[RawItem]:
        import httpx

        async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True) as client:
            r = await client.get(self.url, headers={"user-agent": "JARVIS-core/0.5 news"})
            r.raise_for_status()
        return parse_feed(r.text, self.name, self.quality)


def sources_from_env(spec: str | None = None) -> list[RssSource]:
    """``JARVIS_NEWS_FEEDS="tagesschau=https://www.tagesschau.de/xml/rss2/@0.8,bbc=https://feeds.bbci.co.uk/news/world/rss.xml@0.8"``"""
    raw = spec if spec is not None else os.environ.get("JARVIS_NEWS_FEEDS", "")
    out: list[RssSource] = []
    for part in [p.strip() for p in raw.split(",") if p.strip()]:
        name, _, rest = part.partition("=")
        url, _, q = rest.partition("@")
        if name and url:
            out.append(RssSource(name.strip(), url.strip(), float(q) if q else 0.6))
    return out


# ---------------------------------------------------------------------------- pipeline


class NewsPipeline:
    def __init__(
        self,
        bus: EventBus,
        store: NewsStore,
        sources: list[NewsSourceAdapter] | None = None,
        *,
        clock: Any = None,
    ) -> None:
        self.bus = bus
        self.store = store
        self.sources: list[NewsSourceAdapter] = list(sources or [])
        self._clock = clock or (lambda: datetime.now(UTC))

    # -- refresh -------------------------------------------------------------------------------

    async def refresh(self) -> dict[str, Any]:
        created = updated = duplicates = 0
        errors: dict[str, str] = {}
        for src in self.sources:
            try:
                items = await src.fetch()
            except Exception as exc:  # one dead feed must not stop the others
                errors[src.name] = f"{type(exc).__name__}: {str(exc)[:120]}"
                continue
            c, u, d = await self.ingest(items)
            created, updated, duplicates = created + c, updated + u, duplicates + d
        summary = {
            "sources": len(self.sources),
            "created": created,
            "updated": updated,
            "duplicates": duplicates,
            "errors": errors,
            "events_total": self.store.count(),
        }
        await self.bus.publish(Event.new("news.refreshed", SOURCE, summary, correlation_id="news"))
        return summary

    async def ingest(self, items: list[RawItem]) -> tuple[int, int, int]:
        created = updated = duplicates = 0
        now = self._clock()
        recent = self.store.recent(limit=2000, since=now - CLUSTER_WINDOW)
        cache = [(e, tokens(f"{e.title} {e.summary}")) for e in recent]
        for item in sorted(items, key=lambda i: i.published_at):
            if self.store.seen(item.key):
                duplicates += 1
                continue
            toks = tokens(f"{item.title} {item.summary}")
            isos = detect_countries(f"{item.title}. {item.summary}")
            match = None
            best = 0.0
            for ev, etoks in cache:
                if ev.country and isos and ev.country != isos[0]:
                    continue  # a different country is a different story
                s = similarity(toks, etoks)
                if s > best:
                    best, match = s, ev
            if match is not None and best >= CLUSTER_SIMILARITY:
                ev = self._merge(match, item, now)
                self.store.save(ev)
                self.store.mark_seen(item.key, ev.event_id, now)
                cache = [
                    (ev, tokens(f"{ev.title} {ev.summary}") | t)
                    if e.event_id == ev.event_id
                    else (e, t)
                    for e, t in cache
                ]
                updated += 1
                await self._emit("news.event.updated", ev)
            else:
                ev = self._create(item, now)
                self.store.save(ev)
                self.store.mark_seen(item.key, ev.event_id, now)
                cache.append((ev, toks))
                created += 1
                await self._emit("news.event.created", ev)
        return created, updated, duplicates

    # -- building events ---------------------------------------------------------------------

    def _create(self, item: RawItem, now: datetime) -> NewsEvent:
        text = f"{item.title}. {item.summary}"
        isos = detect_countries(text)
        c = country(isos[0]) if isos else None
        src = SourceRef(item.source_name, item.url, item.source_quality, item.published_at)
        breaking = now - item.published_at <= BREAKING_WINDOW
        return NewsEvent(
            title=item.title.strip(),
            summary=(item.summary or item.title).strip()[:400],
            country=c.iso if c else None,
            region=c.region if c else None,
            lat=c.lat if c else None,
            lon=c.lon if c else None,
            topics=detect_topics(text),
            sources=(src,),
            confidence=self.confidence((src,), now),
            published_at=item.published_at,
            breaking=breaking,
            forecast=looks_like_forecast(item.title),
            ingested_at=now,
            updated_at=now,
        )

    def _merge(self, ev: NewsEvent, item: RawItem, now: datetime) -> NewsEvent:
        src = SourceRef(item.source_name, item.url, item.source_quality, item.published_at)
        sources = (*(s for s in ev.sources if s.name != item.source_name), src)
        topics = tuple(dict.fromkeys((*ev.topics, *detect_topics(f"{item.title}. {item.summary}"))))
        if "general" in topics and len(topics) > 1:
            topics = tuple(t for t in topics if t != "general")
        isos = detect_countries(f"{item.title}. {item.summary}")
        c = country(ev.country) or (country(isos[0]) if isos else None)
        ev.sources = sources
        ev.topics = topics
        if c is not None and ev.country is None:
            ev.country, ev.region, ev.lat, ev.lon = c.iso, c.region, c.lat, c.lon
        if item.source_quality > max(s.quality for s in ev.sources[:-1] or (src,)) and item.summary:
            ev.summary = item.summary.strip()[:400]  # the best source writes the summary
        ev.published_at = min(ev.published_at, item.published_at)
        ev.confidence = self.confidence(sources, now)
        ev.breaking = (
            len({s.name for s in sources}) < 2 and now - ev.published_at <= BREAKING_WINDOW
        )
        ev.forecast = ev.forecast and looks_like_forecast(item.title)
        ev.updated_at = now
        ev.version += 1
        return ev

    @staticmethod
    def confidence(sources: tuple[SourceRef, ...], now: datetime) -> float:
        """0..1: best source quality, boosted by independent corroboration, capped for freshness."""
        if not sources:
            return 0.0
        names = {s.name for s in sources}
        best = max(s.quality for s in sources)
        corroboration = min(0.3, 0.12 * (len(names) - 1))
        base = min(1.0, 0.5 * best + 0.2 + corroboration)
        newest = max(s.published_at for s in sources)
        if len(names) == 1 and now - newest <= BREAKING_WINDOW:
            base = min(base, 0.6)  # a single fresh source is provisional by definition
        return round(base, 2)

    async def _emit(self, event_type: str, ev: NewsEvent) -> None:
        await self.bus.publish(
            Event.new(event_type, SOURCE, ev.to_dict(), correlation_id=f"news:{ev.cluster_id}")
        )

    # -- queries used by the API/capabilities ------------------------------------------------

    def top(
        self, *, country_iso: str | None = None, topic: str | None = None, limit: int = 10
    ) -> list[NewsEvent]:
        events = self.store.recent(limit=500, country=country_iso, topic=topic)
        events.sort(key=lambda e: (e.confidence, len(e.sources), e.published_at), reverse=True)
        return events[:limit]
