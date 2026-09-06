"""Regression: performance budgets from docs/PERFORMANCE.md §3 (SPEC §29 "Fluidity budgets").

Budgets are generous multiples of the targets so CI runners do not flake, but a real regression
(an accidental network call, a synchronous sleep, a busy loop on the hot path) still fails.
"""

from __future__ import annotations

import statistics
import time
from pathlib import Path

import pytest
from core.api import create_app
from core.runtime import CoreRuntime
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[2]
HUD = REPO / "apps" / "desktop" / "web"


def p95(samples: list[float]) -> float:
    s = sorted(samples)
    return s[min(len(s) - 1, round(0.95 * (len(s) - 1)))]


@pytest.fixture
def rt(tmp_path):
    return CoreRuntime.build(f"sqlite:///{tmp_path / 'perf.db'}", provider="none", home="fake")


def test_fast_path_dispatch_stays_under_300ms(rt):
    """PERFORMANCE §3: local safe action dispatch < 300 ms (intent → gateway → verifier → reply)."""
    client = TestClient(create_app(rt))
    client.post("/commands", json={"text": "echo warm up"})  # first call pays imports/DDL
    samples = []
    for i in range(25):
        t0 = time.perf_counter()
        r = client.post("/commands", json={"text": f"echo sample {i}"})
        samples.append(time.perf_counter() - t0)
        assert r.json()["status"] == "completed"
    assert p95(samples) < 0.3, f"p95 {p95(samples):.3f}s, median {statistics.median(samples):.3f}s"


def test_intent_classification_is_deterministic_and_fast(rt):
    """PERFORMANCE §3: fast intent classification typically < 100 ms - ours is regex-only."""
    texts = [
        "echo hi",
        "what time is it",
        "turn on the kitchen light",
        "Jarvis, stop",
        "gute nacht",
    ]
    t0 = time.perf_counter()
    for _ in range(200):
        for t in texts:
            rt.intents.route(t)
    per_call = (time.perf_counter() - t0) / (200 * len(texts))
    assert per_call < 0.001, f"{per_call * 1000:.2f} ms per classification"


def test_event_publish_p95_under_20ms(rt):
    client = TestClient(create_app(rt))
    out = client.post("/commands", json={"text": "echo events"}).json()
    events = client.get("/events", params={"correlation_id": out["mission_id"]}).json()
    assert len(events) >= 5
    # the whole verified mission, i.e. every publish in it, fits comfortably in the budget
    t0 = time.perf_counter()
    for i in range(10):
        client.post("/commands", json={"text": f"echo burst {i}"})
    per_event = (time.perf_counter() - t0) / (10 * len(events))
    assert per_event < 0.02, f"{per_event * 1000:.2f} ms per persisted event"


def test_mission_recovery_after_restart_is_fast(tmp_path):
    url = f"sqlite:///{tmp_path / 'rec.db'}"
    rt = CoreRuntime.build(url, provider="none")
    client = TestClient(create_app(rt))
    for i in range(40):
        client.post("/commands", json={"text": f"echo mission {i}"})
    t0 = time.perf_counter()
    rt2 = CoreRuntime.build(url, provider="none")
    stats = rt2.recover()
    took = time.perf_counter() - t0
    assert stats["missions"] == 40
    assert took < 3.0, f"recovery of 40 missions took {took:.2f}s"
    assert TestClient(create_app(rt2)).get("/health").json()["status"] == "ok"


def test_hud_shell_stays_small_and_offline():
    """The shell is served from the Core without a build step and must never load from a CDN."""
    sizes = {
        p.name: p.stat().st_size for p in HUD.iterdir() if p.suffix in (".js", ".css", ".html")
    }
    total = sum(sizes.values())
    assert total < 300_000, sizes
    text = "\n".join(p.read_text(encoding="utf-8") for p in HUD.iterdir() if p.is_file())
    for forbidden in ("cdn.jsdelivr", "unpkg.com", "cdnjs.cloudflare", "googleapis.com"):
        assert forbidden not in text, forbidden
    assert "importmap" not in text or "http" not in text.split("importmap", 1)[1][:400]


def test_health_endpoint_is_cheap(rt):
    client = TestClient(create_app(rt))
    client.get("/health")
    t0 = time.perf_counter()
    for _ in range(50):
        assert client.get("/health").status_code == 200
    assert (time.perf_counter() - t0) / 50 < 0.05
