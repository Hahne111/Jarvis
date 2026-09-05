"""Home State Machine (SPEC §11.1, Phase 8 step 55).

Home / Away / Sleep / Work / Movie / Guests / Night / Vacation. Each state defines allowed
proactive notifications, lighting/climate defaults, privacy rules, speaker routing and device
policies. The current state is derived from persisted ``home.state.changed`` events (rebuilt on
start), never guessed by a UI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class HomeState(StrEnum):
    HOME = "home"
    AWAY = "away"
    SLEEP = "sleep"
    WORK = "work"
    MOVIE = "movie"
    GUESTS = "guests"
    NIGHT = "night"
    VACATION = "vacation"


@dataclass(frozen=True)
class HomeStatePolicy:
    state: HomeState
    notifications: frozenset[str]  # allowed proactive notification classes
    lighting: str  # default scene/level hint for the lighting defaults
    climate_c: float  # default target temperature
    microphones_muted: bool  # privacy rule: no wake-word/listening in this state
    speaker_routing: str  # where JARVIS speaks
    device_policy: frozenset[str]  # domains an agent may control without asking again

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "notifications": sorted(self.notifications),
            "lighting": self.lighting,
            "climate_c": self.climate_c,
            "microphones_muted": self.microphones_muted,
            "speaker_routing": self.speaker_routing,
            "device_policy": sorted(self.device_policy),
        }


ALL = frozenset({"critical", "security", "reminder", "brief", "suggestion", "news"})
CRITICAL = frozenset({"critical", "security"})
QUIET = frozenset({"critical", "security", "reminder"})
COMMON = frozenset({"light", "switch", "scene", "climate", "cover"})

DEFAULT_POLICIES: dict[HomeState, HomeStatePolicy] = {
    HomeState.HOME: HomeStatePolicy(HomeState.HOME, ALL, "day", 21.0, False, "room", COMMON),
    HomeState.AWAY: HomeStatePolicy(
        HomeState.AWAY, CRITICAL, "off", 17.0, True, "mobile", frozenset({"light", "switch"})
    ),
    HomeState.SLEEP: HomeStatePolicy(
        HomeState.SLEEP, CRITICAL, "off", 18.0, False, "none", frozenset({"light"})
    ),
    HomeState.WORK: HomeStatePolicy(HomeState.WORK, QUIET, "focus", 21.0, False, "desk", COMMON),
    HomeState.MOVIE: HomeStatePolicy(
        HomeState.MOVIE, CRITICAL, "dim", 21.0, False, "tv", frozenset({"light", "cover"})
    ),
    HomeState.GUESTS: HomeStatePolicy(
        HomeState.GUESTS, QUIET, "warm", 21.5, True, "none", frozenset({"light", "scene"})
    ),
    HomeState.NIGHT: HomeStatePolicy(
        HomeState.NIGHT, CRITICAL, "night", 19.0, False, "room", frozenset({"light"})
    ),
    HomeState.VACATION: HomeStatePolicy(
        HomeState.VACATION, CRITICAL, "presence_sim", 16.0, True, "mobile", frozenset()
    ),
}


@dataclass
class HomeStateMachine:
    """Any state may follow any other (the owner decides); the machine records and exposes it."""

    policies: dict[HomeState, HomeStatePolicy] = field(
        default_factory=lambda: dict(DEFAULT_POLICIES)
    )
    current: HomeState = HomeState.HOME
    changed_at: str | None = None

    def policy(self, state: HomeState | None = None) -> HomeStatePolicy:
        return self.policies[state or self.current]

    def set(self, state: HomeState | str, at: str | None = None) -> tuple[HomeState, HomeState]:
        new = HomeState(str(state).lower())
        old = self.current
        self.current = new
        self.changed_at = at
        return old, new

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.current.value,
            "changed_at": self.changed_at,
            "policy": self.policy().to_dict(),
            "states": [s.value for s in HomeState],
        }
