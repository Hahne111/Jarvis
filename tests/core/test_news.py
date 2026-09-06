"""Tests for core/news (Phase 10): geo/topics, dedupe+cluster, confidence, RSS, API, HUD assets."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from core.api import create_app
from core.news import (
    FakeNewsSource,
    NewsPipeline,
    NewsStore,
    RawItem,
    detect_countries,
    detect_topics,
    parse_feed,
    resolve_country,
    similarity,
    sources_from_env,
    tokens,
)
from core.news.demo import demo_items
from core.runtime import CoreRuntime
from fastapi.testclient import TestClient

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


def run(coro):
    return asyncio.run(coro)


def item(title, hours_ago=2.0, source="wire", quality=0.8, summary="", url=None):
    return RawItem(
        title,
        summary or title,
        url or f"https://example.invalid/{abs(hash(title))}",
        NOW - timedelta(hours=hours_ago),
        source,
        quality,
    )


@pytest.fixture
def rt(tmp_path):
    return CoreRuntime.build(f"sqlite:///{tmp_path / 'n.db'}", provider="none", news="fake")


# ---------------------------------------------------------------- geo / topics / tokens


def test_country_and_topic_detection_is_deterministic():
    assert detect_countries("Bundestag beschließt KI-Programm in Berlin") == ["DE"]
    assert detect_countries("Talks between Washington and Beijing on chips") == ["US", "CN"]
    assert detect_countries("Storm hits Rio Grande do Sul, Brazil") == ["BR"]
    assert detect_countries("Local bakery wins award") == []
    assert resolve_country("news from japan") == "JP" and resolve_country("nirgendwo") is None
    assert detect_topics("New AI model beats benchmark, says OpenAI") == ("ai",)
    assert detect_topics("Nvidia unveils new chip for data centres") == ("tech",)
    assert detect_topics("Central bank raises interest rate amid inflation") == ("economy",)
    assert detect_topics("Village fair opens") == ("general",)
    a, b = (
        tokens("Germany passes new AI research funding bill"),
        tokens("Bundestag passes AI research funding bill in Germany"),
    )
    assert similarity(a, b) >= 0.5 and similarity(a, tokens("Japan launches lunar satellite")) < 0.2


# ---------------------------------------------------------------- pipeline


def test_pipeline_clusters_duplicates_and_scores_confidence(tmp_path):
    rt = CoreRuntime.build(f"sqlite:///{tmp_path / 'p.db'}", provider="none")
    store = NewsStore(rt.store.engine)
    src = FakeNewsSource("wire")
    pipe = NewsPipeline(rt.bus, store, [src], clock=lambda: NOW)
    src.items = [
        item("Germany passes new AI research funding bill", 3, "wire-a", 0.85),
        item("Germany passes AI research funding bill after long debate", 2.5, "daily-b", 0.7),
        item("Japan launches new lunar observation satellite", 0.3, "wire-a", 0.85),
        item("Australia could face record heat this summer, forecasters say", 5, "blog-c", 0.4),
    ]
    r = run(pipe.refresh())
    assert (r["created"], r["updated"], r["duplicates"]) == (3, 1, 0) and r["errors"] == {}
    events = store.recent()
    assert len(events) == 3
    de = next(e for e in events if e.country == "DE")
    assert de.source_names == {"wire-a", "daily-b"} and de.version == 2 and not de.breaking
    assert de.confidence > 0.7 and de.region == "Europe" and de.lat and "ai" in de.topics
    jp = next(e for e in events if e.country == "JP")
    assert jp.breaking and jp.confidence <= 0.6 and "science" in jp.topics  # single fresh source
    au = next(e for e in events if e.country == "AU")
    assert au.forecast and not au.breaking and au.confidence < de.confidence
    # a second refresh with the same items is idempotent; a corroborating source lifts a story
    src.items.append(
        item("Japan launches lunar observation satellite from Tanegashima", 0.2, "daily-b", 0.7)
    )
    r2 = run(pipe.refresh())
    assert r2["duplicates"] == 4 and r2["updated"] == 1 and r2["created"] == 0
    jp2 = store.get(jp.event_id)
    assert not jp2.breaking and jp2.confidence > jp.confidence and jp2.version == 2
    types = [e.type for _, e in rt.bus.replay(type_prefix="news")]
    assert types.count("news.event.created") == 3 and types.count("news.event.updated") == 2
    assert types[-1] == "news.refreshed"
    assert store.by_country() == {"DE": 1, "JP": 1, "AU": 1}
    assert sorted(e.country for e in pipe.top(limit=2)) == ["DE", "JP"]  # both corroborated
    assert [e.country for e in pipe.top(topic="ai")] == ["DE"]
    assert store.recent(include_forecasts=False) and all(
        not e.forecast for e in store.recent(include_forecasts=False)
    )
    # a dead source does not stop the others
    dead = FakeNewsSource("dead", failing=True)
    pipe.sources.append(dead)
    r3 = run(pipe.refresh())
    assert "dead" in r3["errors"] and r3["sources"] == 2


def test_rss_and_atom_parsing():
    rss = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
      <item><title>Storm &amp; flood in Brazil</title><link>https://example.invalid/a</link>
        <description>&lt;p&gt;Heavy rain hit the south.&lt;/p&gt;</description>
        <pubDate>Sat, 05 Sep 2026 10:00:00 GMT</pubDate></item>
      <item><title>No date item</title><link>https://example.invalid/b</link></item>
      <item><description>no title -> skipped</description></item>
    </channel></rss>"""
    items = parse_feed(rss, "feed", 0.7)
    assert [i.title for i in items] == ["Storm & flood in Brazil", "No date item"]
    assert items[0].summary == "Heavy rain hit the south." and items[0].published_at.year == 2026
    assert items[0].published_at.tzinfo is not None and items[0].source_quality == 0.7
    atom = """<feed xmlns="http://www.w3.org/2005/Atom"><title>x</title>
      <entry><title>Japan launches satellite</title><link href="https://example.invalid/j"/>
        <updated>2026-09-06T08:00:00Z</updated><summary>JAXA rocket.</summary></entry></feed>"""
    a = parse_feed(atom, "atomfeed", 0.9)
    assert (
        len(a) == 1 and a[0].url == "https://example.invalid/j" and a[0].summary == "JAXA rocket."
    )
    assert parse_feed("<not xml", "x", 0.5) == []
    srcs = sources_from_env(
        "tagesschau=https://www.tagesschau.de/xml/rss2/@0.8, "
        "bbc=https://feeds.bbci.co.uk/news/world/rss.xml"
    )
    assert [(s.name, s.quality) for s in srcs] == [("tagesschau", 0.8), ("bbc", 0.6)]
    assert sources_from_env("") == []


# ---------------------------------------------------------------- runtime / api / hud


def test_news_api_capabilities_and_intents(rt):
    client = TestClient(create_app(rt))
    assert client.get("/news").json()["count"] == 0
    r = client.post("/news/refresh").json()
    assert r["created"] >= 8 and r["errors"] == {}
    news = client.get("/news").json()
    assert news["enabled"] and news["count"] == r["created"]
    de = client.get("/news", params={"country": "de"}).json()["events"]
    assert len(de) == 1 and de[0]["source_count"] == 3 and de[0]["confidence"] >= 0.8
    assert de[0]["topics"][:1] == ["ai"] and not de[0]["breaking"]
    ai = client.get("/news", params={"topic": "ai"}).json()["events"]
    assert ai and all("ai" in e["topics"] for e in ai)
    countries = client.get("/news/countries").json()["countries"]
    by = {c["iso"]: c for c in countries}
    assert (
        by["DE"]["count"] == 1
        and by["JP"]["lat"]
        and "aliases" in by["DE"]
        and by["DE"]["aliases"] is None
    )
    assert client.get(f"/news/{de[0]['event_id']}").json()["title"] == de[0]["title"]
    assert client.get("/news/nope").status_code == 404
    ua = client.get("/news", params={"country": "UA"}).json()["events"][0]
    assert ua["breaking"] and "security" in ua["topics"]
    assert client.get("/health").json()["news"] == r["created"]
    # capability through the gate + fast-path intents (offline, no model)
    top = run(
        rt.executor.run(
            "news.top", {"country": "germany", "limit": 3}, actor="owner", correlation_id="n1"
        )
    )
    assert (
        top.ok
        and top.invocation.result["country"] == "DE"
        and "provisional" not in top.invocation.result["spoken"]
    )
    for text, args in (
        ("news", {}),
        ("Was ist heute weltweit wichtig?", {}),
        ("news about ai", {"topic": "ai"}),
        ("nachrichten aus japan", {"country": "japan"}),
        ("show me the news from germany", {"country": "germany"}),
    ):
        i = rt.intents.route(text)
        assert (i.capability, i.args) == ("news.top", args), text
    out = client.post("/commands", json={"text": "news about ai"}).json()
    assert out["status"] == "completed" and out["result"]["count"] >= 1
    # telemetry endpoint persists HUD timings
    t = client.post("/telemetry", json={"point": "globe_frame", "ms": 9.5, "samples": 300}).json()
    assert t == {"ok": True, "within_budget": True}
    assert client.post("/telemetry", json={"point": "bogus", "ms": 1}).status_code == 422
    ev = [e for _, e in rt.bus.replay(type_prefix="telemetry.latency")][-1]
    assert (
        ev.payload["point"] == "globe_frame"
        and ev.payload["budget_ms"] == 16.7
        and ev.source == "hud"
    )


def test_news_disabled_and_demo_items(tmp_path):
    rt = CoreRuntime.build(f"sqlite:///{tmp_path / 'off.db'}", provider="none")
    client = TestClient(create_app(rt))
    assert rt.news is None and "news.top" not in rt.capabilities
    assert client.get("/news").json() == {"enabled": False, "events": [], "count": 0}
    assert client.post("/news/refresh").status_code == 409
    assert rt.intents.route("news").kind == "agent"
    items = demo_items(NOW)
    assert len(items) >= 10 and len({i.key for i in items}) == len(items)


def test_hud_ships_the_globe(rt):
    client = TestClient(create_app(rt))
    html = client.get("/hud/").text
    assert 'id="globeCanvas"' in html and 'id="modes"' in html and 'id="evidenceRail"' in html
    js = client.get("/hud/globe.js").text
    assert (
        "export class Globe" in js
        and "TIERS" in js
        and "globe_frame" in js
        and "https://" not in js
    )
    hud = client.get("/hud/hud.js").text
    assert 'import { Globe } from "/hud/globe.js"' in hud and "hud_mode_switch" in hud
