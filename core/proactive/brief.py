"""Daily Brief (SPEC §14.1 "informativ -> Daily Brief", Phase 11 step 73).

Composes what accumulated in the ``brief`` channel into one short, spoken-friendly text: open
approvals, missions since the last brief, home/privacy state, top news (facts before forecasts,
provisional marked), pending suggestions, next scheduled jobs. Pure read of persisted state; the
result is itself an event (``brief.ready``) so HUD, voice and push all get the same brief.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from core.capabilities.manifest import CapabilityManifest
from core.capabilities.registry import CapabilityRegistry
from core.events.envelope import Event
from core.permissions.model import RiskLevel

SOURCE = "brief"

BRIEF_MANIFEST = CapabilityManifest(
    name="brief.generate",
    version="1.0",
    risk=RiskLevel.P0,
    inputs={"reason": "string?"},
    description="Compose the daily brief from missions, approvals, home, news and suggestions.",
)


class BriefBuilder:
    def __init__(self, runtime: Any, *, clock: Callable[[], datetime] | None = None) -> None:
        self._rt = runtime
        self._clock = clock or (lambda: datetime.now(UTC))

    def last_brief_at(self) -> datetime | None:
        rows = self._rt.bus.replay(type_prefix="brief.ready")
        return rows[-1][1].timestamp if rows else None

    def build(self, reason: str = "manual") -> dict[str, Any]:
        rt = self._rt
        now = self._clock()
        since = self.last_brief_at() or (now - timedelta(hours=24))
        sections: list[dict[str, Any]] = []
        lines: list[str] = []

        approvals = rt.permissions.pending()
        if approvals:
            items = [f"{d.request.action} (P{int(d.request.risk)})" for d in approvals]
            sections.append({"title": "Waiting for you", "items": items})
            plural = "s" if len(items) != 1 else ""
            lines.append(f"{len(items)} approval{plural} waiting: {', '.join(items[:3])}.")

        missions = [m for m in rt.missions.list() if m.updated_at >= since]
        done = [m for m in missions if m.status.value == "completed"]
        failed = [m for m in missions if m.status.value in ("failed", "paused", "blocked")]
        if missions:
            sections.append(
                {
                    "title": "Missions",
                    "items": [f"{m.status.value}: {m.goal[:60]}" for m in missions[-8:]],
                }
            )
            lines.append(
                f"{len(done)} mission{'s' if len(done) != 1 else ''} completed"
                + (f", {len(failed)} need attention" if failed else "")
                + "."
            )

        if rt.home is not None:
            st = rt.home.states.current.value
            sections.append({"title": "Home", "items": [f"state {st}"]})
            lines.append(f"Home is in {st} mode.")
        privacy = getattr(rt, "privacy", None)
        if privacy is not None and privacy.mode != "normal":
            lines.append(f"Privacy mode is {privacy.mode}: JARVIS is not learning right now.")

        if rt.news is not None:
            top = [e for e in rt.news.top(limit=6) if not e.forecast][:3]
            if top:
                sections.append(
                    {
                        "title": "World",
                        "items": [
                            f"{e.title}{' (provisional)' if e.breaking else ''} - "
                            f"{e.country or 'global'}, confidence {int(e.confidence * 100)}%"
                            for e in top
                        ],
                    }
                )
                lines.append("World: " + "; ".join(e.title for e in top) + ".")

        suggestions = getattr(rt, "habits", None)
        if suggestions is not None:
            pending = suggestions.store.list(status="pending")
            if pending:
                sections.append({"title": "Suggestions", "items": [s.title for s in pending[:5]]})
                plural = "s" if len(pending) != 1 else ""
                lines.append(f"{len(pending)} routine suggestion{plural} to review.")

        scheduler = getattr(rt, "scheduler", None)
        if scheduler is not None:
            nxt = [j for j in scheduler.store.list(enabled_only=True) if j.next_run_at][:5]
            if nxt:
                sections.append(
                    {
                        "title": "Next",
                        "items": [
                            f"{j.next_run_at.strftime('%H:%M')} {j.name}"
                            for j in sorted(nxt, key=lambda j: j.next_run_at)
                        ],
                    }
                )

        if not lines:
            lines.append("Nothing needs your attention. All quiet.")
        text = " ".join(lines)
        return {
            "generated_at": now.isoformat(),
            "since": since.isoformat(),
            "reason": reason,
            "text": text,
            "sections": sections,
        }


def register_brief(
    registry: CapabilityRegistry, builder: BriefBuilder, bus: Any
) -> CapabilityRegistry:
    async def generate(args: dict[str, Any]) -> dict[str, Any]:
        brief = builder.build(reason=str(args.get("reason") or "manual"))
        await bus.publish(Event.new("brief.ready", SOURCE, brief, correlation_id="brief"))
        return brief

    registry.register(BRIEF_MANIFEST, generate)
    return registry
