"""Latency telemetry (PERFORMANCE.md §3/§5, Phase 5 step 35): local p50/p95 per measurement point.

Samples are events (``telemetry.latency``) so the HUD, logs and later performance history all see
the same numbers. Nothing here is user content.
"""

from __future__ import annotations

import math
import time
from collections import defaultdict
from collections.abc import Callable

from core.events.bus import EventBus
from core.events.envelope import Event, Sensitivity

SOURCE = "voice-telemetry"
BUDGETS_MS = {  # SPEC §9.2 targets (goals, not guarantees)
    "wake_ack": 250,
    "barge_in_stop": 150,
    "local_dispatch": 300,
    "first_audio": 1000,
}


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, math.ceil(pct / 100 * len(ordered)) - 1))  # nearest rank
    return ordered[k]


class LatencyTelemetry:
    def __init__(self, bus: EventBus, *, clock: Callable[[], float] | None = None) -> None:
        self._bus = bus
        self._clock = clock or time.monotonic
        self._samples: dict[str, list[float]] = defaultdict(list)
        self._marks: dict[str, float] = {}

    def mark(self, name: str) -> None:
        """Remember 'now' under ``name`` (e.g. 'wake_detected')."""
        self._marks[name] = self._clock()

    def since(self, name: str) -> float | None:
        start = self._marks.get(name)
        return None if start is None else (self._clock() - start) * 1000

    async def record(self, point: str, ms: float, *, correlation_id: str | None = None) -> float:
        self._samples[point].append(ms)
        await self._bus.publish(
            Event.new(
                "telemetry.latency",
                SOURCE,
                {
                    "point": point,
                    "ms": round(ms, 1),
                    "budget_ms": BUDGETS_MS.get(point),
                    "within_budget": (BUDGETS_MS.get(point) is None or ms <= BUDGETS_MS[point]),
                },
                correlation_id=correlation_id or "voice",
                sensitivity=Sensitivity.PUBLIC,
            )
        )
        return ms

    async def record_since(
        self, point: str, mark: str, *, correlation_id: str | None = None
    ) -> float | None:
        ms = self.since(mark)
        if ms is None:
            return None
        return await self.record(point, ms, correlation_id=correlation_id)

    def summary(self) -> dict[str, dict[str, float | int | None]]:
        return {
            point: {
                "count": len(vals),
                "p50_ms": round(percentile(vals, 50), 1),
                "p95_ms": round(percentile(vals, 95), 1),
                "max_ms": round(max(vals), 1),
                "budget_ms": BUDGETS_MS.get(point),
            }
            for point, vals in sorted(self._samples.items())
        }


def summary_from_log(bus: EventBus) -> dict[str, dict[str, float | int | None]]:
    """Rebuild the summary from persisted events (e.g. after a restart or for the HUD)."""
    samples: dict[str, list[float]] = defaultdict(list)
    for _, ev in bus.replay(type_prefix="telemetry.latency"):
        samples[ev.payload["point"]].append(float(ev.payload["ms"]))
    return {
        point: {
            "count": len(vals),
            "p50_ms": round(percentile(vals, 50), 1),
            "p95_ms": round(percentile(vals, 95), 1),
            "max_ms": round(max(vals), 1),
            "budget_ms": BUDGETS_MS.get(point),
        }
        for point, vals in sorted(samples.items())
    }
