"""Model Router (SPEC §5.1, §6.1, PERFORMANCE.md §2): Fast / Smart / Deep path.

Deterministic: picks a model and effort from path, privacy, latency and cost limits.
Prices are USD per million tokens (Anthropic first-party rates, cached 2026-06).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from core.events.envelope import Sensitivity
from core.models.provider import Usage

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


class Path(StrEnum):
    FAST = "fast"  # local rules / tiny model, milliseconds
    SMART = "smart"  # ambiguous command, small reasoning, sub-second
    DEEP = "deep"  # research, coding, complex missions


class Tier(StrEnum):
    FRONTIER = "frontier"
    STANDARD = "standard"
    SMALL = "small"
    LOCAL = "local"


@dataclass(frozen=True)
class ModelSpec:
    id: str
    provider: str  # "claude" | "ollama" | ...
    tier: Tier
    input_usd_per_mtok: float = 0.0
    output_usd_per_mtok: float = 0.0
    local: bool = False
    supports_effort: bool = True

    def cost(self, usage: Usage) -> float:
        return (
            usage.input_tokens * self.input_usd_per_mtok
            + usage.output_tokens * self.output_usd_per_mtok
        ) / 1_000_000


DEFAULT_MODELS: tuple[ModelSpec, ...] = (
    ModelSpec("claude-opus-5", "claude", Tier.FRONTIER, 5.0, 25.0),
    ModelSpec("claude-sonnet-5", "claude", Tier.STANDARD, 2.0, 10.0),
    ModelSpec("claude-haiku-4-5", "claude", Tier.SMALL, 1.0, 5.0),
)


class NoEligibleModel(LookupError):
    pass


@dataclass(frozen=True)
class RoutingRequest:
    path: Path
    sensitivity: Sensitivity = Sensitivity.PRIVATE
    max_cost_usd: float | None = None  # per call, estimated on expected tokens
    expected_input_tokens: int = 4_000
    expected_output_tokens: int = 2_000
    offline: bool = False
    prefer_local: bool = False


@dataclass(frozen=True)
class RoutingDecision:
    model: ModelSpec
    effort: str
    path: Path
    reason: str

    def to_dict(self) -> dict:
        return {
            "model": self.model.id,
            "provider": self.model.provider,
            "effort": self.effort,
            "path": self.path.value,
            "reason": self.reason,
        }


@dataclass
class ModelRouter:
    models: list[ModelSpec] = field(default_factory=lambda: list(DEFAULT_MODELS))

    def add(self, spec: ModelSpec) -> None:
        if any(m.id == spec.id for m in self.models):
            raise ValueError(f"model {spec.id!r} already registered")
        self.models.append(spec)

    def choose(self, req: RoutingRequest) -> RoutingDecision:
        candidates = list(self.models)
        reasons: list[str] = []
        # Privacy: secret content never leaves the house; offline: only local models work.
        if req.sensitivity is Sensitivity.SECRET or req.offline:
            candidates = [m for m in candidates if m.local]
            reasons.append(
                "local-only"
                + (" (secret)" if req.sensitivity is Sensitivity.SECRET else " (offline)")
            )
        if req.prefer_local and any(m.local for m in candidates):
            candidates = [m for m in candidates if m.local]
            reasons.append("prefer-local")
        if req.max_cost_usd is not None:
            est = Usage(req.expected_input_tokens, req.expected_output_tokens)
            candidates = [m for m in candidates if m.cost(est) <= req.max_cost_usd]
            reasons.append(f"cost<=${req.max_cost_usd}")
        if not candidates:
            raise NoEligibleModel(f"no model satisfies {req} ({', '.join(reasons)})")

        order = {
            Path.DEEP: [Tier.FRONTIER, Tier.STANDARD, Tier.LOCAL, Tier.SMALL],
            Path.SMART: [Tier.STANDARD, Tier.SMALL, Tier.LOCAL, Tier.FRONTIER],
            Path.FAST: [Tier.LOCAL, Tier.SMALL, Tier.STANDARD, Tier.FRONTIER],
        }[req.path]
        effort = {Path.DEEP: "xhigh", Path.SMART: "medium", Path.FAST: "low"}[req.path]
        for tier in order:
            for m in candidates:
                if m.tier is tier:
                    reasons.append(f"{req.path.value}->{tier.value}")
                    return RoutingDecision(
                        m, effort if m.supports_effort else "high", req.path, ", ".join(reasons)
                    )
        raise NoEligibleModel("unreachable")  # pragma: no cover
