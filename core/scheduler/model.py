"""Scheduled jobs = background missions with a clock (SPEC §14.2, Phase 11 step 72).

A job is durable (table ``jobs``) and either runs a text command through the normal
command path (``kind="command"`` -> intent router -> gateway -> real mission) or one P0
capability directly (``kind="capability"``, used by predictive preloading). Schedules are
simple on purpose: ``every_s`` intervals or a daily ``at`` (HH:MM, local time) with optional
weekdays. Every job has a run budget; nothing here bypasses permissions - a P3 action started
by the scheduler still waits for the owner.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any


@dataclass
class Job:
    name: str
    kind: str  # "command" | "capability"
    text: str | None = None  # kind=command
    capability: str | None = None  # kind=capability
    args: dict[str, Any] = field(default_factory=dict)
    every_s: int | None = None  # interval schedule
    at: str | None = None  # "HH:MM" daily schedule (UTC unless tz offset configured)
    weekdays: tuple[int, ...] | None = None  # 0=Mon .. 6=Sun; None = every day
    enabled: bool = True
    source: str = "owner"  # owner | suggestion | system | preload
    created_by: str = "local"
    max_runs: int | None = None
    budget_s: int = 900  # watchdog budget for the mission a job starts
    job_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    last_status: str | None = None
    last_mission_id: str | None = None
    runs: int = 0

    def __post_init__(self) -> None:
        if self.kind not in ("command", "capability"):
            raise ValueError("kind must be command or capability")
        if self.kind == "command" and not self.text:
            raise ValueError("command jobs need text")
        if self.kind == "capability" and not self.capability:
            raise ValueError("capability jobs need a capability")
        if not self.every_s and not self.at:
            raise ValueError("a job needs every_s or at")
        if self.every_s is not None and self.every_s < 60:
            raise ValueError("every_s must be at least 60 seconds")
        if self.at is not None:
            hh, mm = self.at.split(":")
            if not (0 <= int(hh) < 24 and 0 <= int(mm) < 60):
                raise ValueError("at must be HH:MM")
        if self.next_run_at is None:
            self.next_run_at = self.compute_next(self.created_at)

    def compute_next(self, after: datetime) -> datetime:
        if self.every_s:
            return after + timedelta(seconds=self.every_s)
        hh, mm = (int(x) for x in str(self.at).split(":"))
        candidate = after.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if candidate <= after:
            candidate += timedelta(days=1)
        for _ in range(8):
            if self.weekdays is None or candidate.weekday() in self.weekdays:
                return candidate
            candidate += timedelta(days=1)
        return candidate

    def exhausted(self) -> bool:
        return self.max_runs is not None and self.runs >= self.max_runs

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "name": self.name,
            "kind": self.kind,
            "text": self.text,
            "capability": self.capability,
            "args": dict(self.args),
            "every_s": self.every_s,
            "at": self.at,
            "weekdays": list(self.weekdays) if self.weekdays is not None else None,
            "enabled": self.enabled,
            "source": self.source,
            "created_by": self.created_by,
            "max_runs": self.max_runs,
            "budget_s": self.budget_s,
            "created_at": self.created_at.isoformat(),
            "next_run_at": self.next_run_at.isoformat() if self.next_run_at else None,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "last_status": self.last_status,
            "last_mission_id": self.last_mission_id,
            "runs": self.runs,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Job:
        return cls(
            name=d["name"],
            kind=d["kind"],
            text=d.get("text"),
            capability=d.get("capability"),
            args=dict(d.get("args") or {}),
            every_s=d.get("every_s"),
            at=d.get("at"),
            weekdays=tuple(d["weekdays"]) if d.get("weekdays") is not None else None,
            enabled=bool(d.get("enabled", True)),
            source=d.get("source", "owner"),
            created_by=d.get("created_by", "local"),
            max_runs=d.get("max_runs"),
            budget_s=int(d.get("budget_s", 900)),
            job_id=d["job_id"],
            created_at=datetime.fromisoformat(d["created_at"]),
            next_run_at=datetime.fromisoformat(d["next_run_at"]) if d.get("next_run_at") else None,
            last_run_at=datetime.fromisoformat(d["last_run_at"]) if d.get("last_run_at") else None,
            last_status=d.get("last_status"),
            last_mission_id=d.get("last_mission_id"),
            runs=int(d.get("runs", 0)),
        )
