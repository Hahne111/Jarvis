"""Relevance Engine (SPEC §14.1, Phase 11 step 69): "JARVIS darf nicht nerven".

Every event gets urgency, relevance, confidence, an interruption cost and a channel:

    now            critical / needs the owner right away (push + HUD)
    opportunistic  important; delivered when the owner is reachable and not disturbed
    brief          informative; collected for the daily brief
    silent         irrelevant for the owner's attention (still in the log, never pushed)

Deterministic rules, no model. Context (home state, privacy mode, time of day) raises the
interruption cost; only genuinely critical events (urgency >= 0.9) break through it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from core.events.envelope import Event

CHANNELS = ("now", "opportunistic", "brief", "silent")


@dataclass(frozen=True)
class Assessment:
    channel: str
    urgency: float
    relevance: float
    confidence: float
    interruption_cost: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "urgency": round(self.urgency, 2),
            "relevance": round(self.relevance, 2),
            "confidence": round(self.confidence, 2),
            "interruption_cost": round(self.interruption_cost, 2),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Context:
    home_state: str = "home"  # home/away/sleep/work/movie/guests/night/vacation
    privacy_mode: str = "normal"  # normal/private/guest
    hour: int = 12  # local hour 0..23
    home_country: str | None = None  # ISO-2 the owner lives in (news relevance)

    @property
    def interruption_cost(self) -> float:
        cost = 0.2
        if self.home_state in ("sleep", "night"):
            cost = 0.9
        elif self.home_state in ("movie", "guests", "work"):
            cost = 0.6
        elif self.home_state in ("away", "vacation"):
            cost = 0.4
        if self.privacy_mode == "guest":
            cost = max(cost, 0.8)
        if self.hour < 7 or self.hour >= 23:
            cost = max(cost, 0.8)
        return cost


class RelevanceEngine:
    def __init__(self, context: Callable[[], Context] | None = None) -> None:
        self._context = context or (lambda: Context(hour=datetime.now(UTC).hour))

    def context(self) -> Context:
        return self._context()

    def assess(self, ev: Event, ctx: Context | None = None) -> Assessment:
        ctx = ctx or self.context()
        cost = ctx.interruption_cost
        urgency, relevance, confidence, reason = self._score(ev, ctx)
        if urgency >= 0.9:
            channel = "now"
        elif relevance < 0.2:
            channel = "silent"
        elif urgency >= 0.6:
            channel = "opportunistic" if cost < 0.7 else "brief"
        elif relevance >= 0.5:
            channel = "brief"
        else:
            channel = "silent"
        return Assessment(channel, urgency, relevance, confidence, cost, reason)

    @staticmethod
    def _score(ev: Event, ctx: Context) -> tuple[float, float, float, str]:
        t, p = ev.type, ev.payload or {}
        if t == "gateway.halted":
            return 1.0, 1.0, 1.0, "kill switch"
        if t == "permission.ask":
            risk = int(p.get("decision", {}).get("request", {}).get("risk", 3))
            return (0.95 if risk >= 4 else 0.9), 1.0, 1.0, f"approval needed (P{risk})"
        if t == "device.revoked" or t == "device.auth.failed":
            return (0.9 if t == "device.revoked" else 0.6), 0.9, 1.0, "device security"
        if t == "mission.watchdog":
            return 0.7, 0.9, 1.0, "mission stuck"
        if t == "mission.failed":
            return 0.6, 0.8, 1.0, "mission failed"
        if t == "mission.completed":
            return 0.2, 0.6, 1.0, "mission completed"
        if t == "home.device.changed":
            domain = p.get("domain")
            if domain in ("lock", "alarm_control_panel") or p.get("service") in ("open_cover",):
                return 0.8, 0.9, 1.0, "security device changed"
            return 0.1, 0.3, 1.0, "comfort device changed"
        if t == "home.state.changed":
            return 0.2, 0.5, 1.0, "home state"
        if t == "news.event.created" or t == "news.event.updated":
            conf = float(p.get("confidence", 0.5))
            topics = set(p.get("topics") or [])
            local = bool(ctx.home_country) and p.get("country") == ctx.home_country
            urgency = 0.6 if ("security" in topics and p.get("breaking") and local) else 0.3
            relevance = (
                0.7 if local else (0.5 if topics & {"security", "climate", "health"} else 0.35)
            )
            if p.get("forecast"):
                relevance *= 0.6
            return urgency, relevance, conf, "news" + (" (local)" if local else "")
        if t == "brief.ready":
            return 0.5, 0.8, 1.0, "daily brief"
        if t == "automation.suggested" or t == "habit.detected":
            return 0.3, 0.7, float(p.get("confidence", 0.7)), "suggestion"
        if t == "power.wake.sent":
            return 0.3, 0.6, 1.0, "wake sent"
        if t.startswith(
            (
                "telemetry.",
                "presence.",
                "capability.",
                "verification.",
                "agent.run.step",
                "workspace.run.output",
            )
        ):
            return 0.0, 0.05, 1.0, "operational noise"
        return 0.0, 0.1, 1.0, "not owner-relevant"

    @staticmethod
    def pushable(a: Assessment) -> bool:
        """What may reach the phone: critical always; important only when it won't disturb."""
        return a.channel == "now" or (a.channel == "opportunistic" and a.interruption_cost < 0.5)
