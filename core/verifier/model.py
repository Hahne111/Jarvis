"""Verification outcomes (SPEC §5.1 "Verifier", Development Law 8).

An Invocation that *succeeded* only means the tool returned. Whether the real goal was reached is
the Verifier's call - and only a passed verification may be reported as success.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class Outcome(StrEnum):
    ACHIEVED = "achieved"  # goal reached according to independent evidence
    NOT_ACHIEVED = "not_achieved"  # tool ran, goal not reached -> retry candidate
    UNKNOWN = "unknown"  # verifier missing/failed/timed out -> never counts as success
    SKIPPED = "skipped"  # nothing to verify (no side effects) or nothing ran


EVENT_FOR_OUTCOME = {
    Outcome.ACHIEVED: "verification.passed",
    Outcome.NOT_ACHIEVED: "verification.failed",
    Outcome.UNKNOWN: "verification.unknown",
    Outcome.SKIPPED: "verification.skipped",
}


@dataclass(frozen=True)
class Verification:
    capability: str
    invocation_id: str
    outcome: Outcome
    verifier: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None
    verification_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def passed(self) -> bool:
        return self.outcome is Outcome.ACHIEVED

    def to_dict(self) -> dict[str, Any]:
        return {
            "verification_id": self.verification_id,
            "capability": self.capability,
            "invocation_id": self.invocation_id,
            "verifier": self.verifier,
            "outcome": self.outcome.value,
            "evidence": self.evidence,
            "reason": self.reason,
            "checked_at": self.checked_at.isoformat(),
        }
