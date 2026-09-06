"""AgentRun record (SPEC §17.1: provider, model, effort, tools, cost, start/end, outcome)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from core.models.provider import Message, Usage


class RunOutcome(StrEnum):
    COMPLETED = "completed"
    AWAITING_APPROVAL = "awaiting_approval"
    BUDGET_EXCEEDED = "budget_exceeded"
    REFUSED = "refused"
    HALTED = "halted"
    FAILED = "failed"
    NOT_VERIFIED = "not_verified"  # the model said done, but no verified green run backs it


@dataclass
class AgentRun:
    mission_id: str
    provider: str
    model: str
    effort: str
    tools: list[str]
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    role: str = "coordinator"
    parent_run_id: str | None = None
    depth: int = 0
    steps: int = 0
    tool_calls: int = 0
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    ended_at: datetime | None = None
    outcome: RunOutcome | None = None
    final_text: str = ""
    error: str | None = None
    pending_decision_id: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome is RunOutcome.COMPLETED

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "mission_id": self.mission_id,
            "role": self.role,
            "parent_run_id": self.parent_run_id,
            "depth": self.depth,
            "provider": self.provider,
            "model": self.model,
            "effort": self.effort,
            "tools": list(self.tools),
            "steps": self.steps,
            "tool_calls": self.tool_calls,
            "usage": self.usage.to_dict(),
            "cost_usd": round(self.cost_usd, 6),
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "outcome": self.outcome.value if self.outcome else None,
            "final_text": self.final_text,
            "error": self.error,
            "pending_decision_id": self.pending_decision_id,
        }


def messages_to_dicts(messages: list[Message]) -> list[dict[str, Any]]:
    return [
        {"role": m.role, "content": m.content, "tool_call_id": m.tool_call_id, "name": m.name}
        for m in messages
    ]


def messages_from_dicts(rows: list[dict[str, Any]]) -> list[Message]:
    return [
        Message(r["role"], r["content"], tool_call_id=r.get("tool_call_id"), name=r.get("name"))
        for r in rows
    ]
