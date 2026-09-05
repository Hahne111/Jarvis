"""Agent budgets (SPEC §14.2, §7.3 'Agent-Endlosschleife'): time, tokens, cost, tool calls."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from core.models.provider import Usage


class BudgetExceeded(RuntimeError):
    def __init__(self, dimension: str, limit: float, used: float) -> None:
        super().__init__(f"budget exceeded: {dimension} {used} > {limit}")
        self.dimension = dimension
        self.limit = limit
        self.used = used


@dataclass(frozen=True)
class AgentBudget:
    max_seconds: float | None = 300.0
    max_tokens: int | None = 200_000
    max_cost_usd: float | None = 2.0
    max_tool_calls: int | None = 25
    max_steps: int | None = 20

    def __post_init__(self) -> None:
        for name in ("max_seconds", "max_tokens", "max_cost_usd", "max_tool_calls", "max_steps"):
            v = getattr(self, name)
            if v is not None and v <= 0:
                raise ValueError(f"{name} must be positive or None")

    def to_dict(self) -> dict:
        return {
            "max_seconds": self.max_seconds,
            "max_tokens": self.max_tokens,
            "max_cost_usd": self.max_cost_usd,
            "max_tool_calls": self.max_tool_calls,
            "max_steps": self.max_steps,
        }


@dataclass
class BudgetTracker:
    budget: AgentBudget
    clock: Callable[[], float] = time.monotonic
    started_at: float = field(init=False)
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0
    tool_calls: int = 0
    steps: int = 0

    def __post_init__(self) -> None:
        self.started_at = self.clock()

    @property
    def elapsed_seconds(self) -> float:
        return self.clock() - self.started_at

    def charge(self, usage: Usage, cost_usd: float = 0.0) -> None:
        self.usage = self.usage + usage
        self.cost_usd += cost_usd
        self.check()

    def record_tool_call(self, n: int = 1) -> None:
        self.tool_calls += n
        self.check()

    def record_step(self) -> None:
        self.steps += 1
        self.check()

    def check(self) -> None:
        b = self.budget
        if b.max_seconds is not None and self.elapsed_seconds > b.max_seconds:
            raise BudgetExceeded("seconds", b.max_seconds, round(self.elapsed_seconds, 3))
        if b.max_tokens is not None and self.usage.total_tokens > b.max_tokens:
            raise BudgetExceeded("tokens", b.max_tokens, self.usage.total_tokens)
        if b.max_cost_usd is not None and self.cost_usd > b.max_cost_usd:
            raise BudgetExceeded("cost_usd", b.max_cost_usd, round(self.cost_usd, 6))
        if b.max_tool_calls is not None and self.tool_calls > b.max_tool_calls:
            raise BudgetExceeded("tool_calls", b.max_tool_calls, self.tool_calls)
        if b.max_steps is not None and self.steps > b.max_steps:
            raise BudgetExceeded("steps", b.max_steps, self.steps)

    def to_dict(self) -> dict:
        return {
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "usage": self.usage.to_dict(),
            "cost_usd": round(self.cost_usd, 6),
            "tool_calls": self.tool_calls,
            "steps": self.steps,
            "budget": self.budget.to_dict(),
        }
