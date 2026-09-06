"""Demo stories for ``JARVIS_NEWS=fake`` (HUD/globe without network). Fictional, clearly generic."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from core.news.model import RawItem


def demo_items(now: datetime | None = None) -> list[RawItem]:
    now = now or datetime.now(UTC)
    h = lambda hours: now - timedelta(hours=hours)  # noqa: E731
    src_a, src_b, src_c = ("wire-a", 0.85), ("daily-b", 0.7), ("blog-c", 0.4)
    return [
        RawItem(
            "Germany passes new AI research funding bill",
            "The Bundestag approved a multi-year programme for artificial intelligence "
            "research centres in Berlin and Munich.",
            "https://example.invalid/de-ai-1",
            h(3),
            *src_a,
        ),
        RawItem(
            "Bundestag approves multi-year AI research funding programme",
            "Billions for artificial intelligence research: the programme runs five years.",
            "https://example.invalid/de-ai-2",
            h(2.5),
            *src_b,
        ),
        RawItem(
            "Berlin approves AI funding programme for research centres",
            "Opposition criticises the pace of the artificial intelligence programme.",
            "https://example.invalid/de-ai-3",
            h(2),
            *src_c,
        ),
        RawItem(
            "Japan launches new lunar observation satellite",
            "JAXA's rocket lifted off from Tanegashima; the space probe will map the Moon's "
            "south pole.",
            "https://example.invalid/jp-space",
            h(6),
            *src_a,
        ),
        RawItem(
            "Brazil floods displace thousands in the south",
            "Heavy storms and flooding hit Rio Grande do Sul; the government declared an "
            "emergency.",
            "https://example.invalid/br-flood",
            h(9),
            *src_b,
        ),
        RawItem(
            "Kenya opens largest solar plant in East Africa",
            "The renewable energy facility near Nairobi adds 400 MW to the grid.",
            "https://example.invalid/ke-solar",
            h(14),
            *src_a,
        ),
        RawItem(
            "US central bank holds interest rate steady",
            "Markets in Washington and New York reacted calmly; inflation remains above target.",
            "https://example.invalid/us-rates",
            h(20),
            *src_a,
        ),
        RawItem(
            "Fed keeps rates unchanged, signals patience",
            "Stocks edged higher after the decision on interest rates.",
            "https://example.invalid/us-rates-2",
            h(19),
            *src_b,
        ),
        RawItem(
            "India's election commission announces vote schedule",
            "The parliament election will run in several phases across the country.",
            "https://example.invalid/in-election",
            h(26),
            *src_b,
        ),
        RawItem(
            "Australia could face record heat this summer, forecasters say",
            "The outlook points to an expected El Niño influence over the continent.",
            "https://example.invalid/au-heat",
            h(30),
            *src_c,
        ),
        RawItem(
            "Explosion reported near Kyiv power station",
            "Ukrainian officials said the attack damaged energy infrastructure; details are "
            "still emerging.",
            "https://example.invalid/ua-strike",
            h(0.4),
            *src_b,
        ),
        RawItem(
            "Champions League final set for Istanbul",
            "Turkish football fans prepare for the match in the city.",
            "https://example.invalid/tr-football",
            h(40),
            *src_c,
        ),
    ]
