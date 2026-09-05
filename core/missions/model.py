"""Mission and Task models plus the mission state machine (SPEC §5.3, §17.1).

    CREATED -> PLANNING -> WAITING_FOR_APPROVAL -> RUNNING -> VERIFYING -> COMPLETED
                                                -> PAUSED / BLOCKED / FAILED / CANCELED

Only ``MissionEngine`` mutates these objects; every transition emits an event and is persisted.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from core.events.envelope import DEFAULT_USER_ID, Priority


class MissionStatus(StrEnum):
    CREATED = "created"
    PLANNING = "planning"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    RUNNING = "running"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    PAUSED = "paused"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELED = "canceled"


TERMINAL_STATES: frozenset[MissionStatus] = frozenset(
    {MissionStatus.COMPLETED, MissionStatus.FAILED, MissionStatus.CANCELED}
)

# Deterministic transition table. Anything not listed is an InvalidTransition.
MISSION_TRANSITIONS: dict[MissionStatus, frozenset[MissionStatus]] = {
    MissionStatus.CREATED: frozenset({MissionStatus.PLANNING, MissionStatus.CANCELED}),
    MissionStatus.PLANNING: frozenset(
        {
            MissionStatus.WAITING_FOR_APPROVAL,
            MissionStatus.RUNNING,
            MissionStatus.BLOCKED,
            MissionStatus.FAILED,
            MissionStatus.CANCELED,
        }
    ),
    MissionStatus.WAITING_FOR_APPROVAL: frozenset(
        {MissionStatus.RUNNING, MissionStatus.PAUSED, MissionStatus.CANCELED}
    ),
    MissionStatus.RUNNING: frozenset(
        {
            MissionStatus.VERIFYING,
            MissionStatus.WAITING_FOR_APPROVAL,
            MissionStatus.PAUSED,
            MissionStatus.BLOCKED,
            MissionStatus.FAILED,
            MissionStatus.CANCELED,
        }
    ),
    MissionStatus.VERIFYING: frozenset(
        {
            MissionStatus.COMPLETED,
            MissionStatus.RUNNING,
            MissionStatus.FAILED,
            MissionStatus.CANCELED,
        }
    ),
    MissionStatus.PAUSED: frozenset({MissionStatus.RUNNING, MissionStatus.CANCELED}),
    MissionStatus.BLOCKED: frozenset(
        {
            MissionStatus.PLANNING,
            MissionStatus.RUNNING,
            MissionStatus.FAILED,
            MissionStatus.CANCELED,
        }
    ),
    MissionStatus.COMPLETED: frozenset(),
    MissionStatus.FAILED: frozenset(),
    MissionStatus.CANCELED: frozenset(),
}


class TaskState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


TASK_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.PENDING: frozenset({TaskState.RUNNING, TaskState.CANCELED}),
    TaskState.RUNNING: frozenset(
        {TaskState.COMPLETED, TaskState.FAILED, TaskState.PENDING, TaskState.CANCELED}
    ),
    TaskState.FAILED: frozenset({TaskState.PENDING, TaskState.CANCELED}),
    TaskState.COMPLETED: frozenset(),
    TaskState.CANCELED: frozenset(),
}


class InvalidTransition(ValueError):
    """The requested state change is not allowed by the state machine."""


class MissionNotFound(KeyError):
    pass


class TaskNotFound(KeyError):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


def _new_id() -> str:
    return str(uuid.uuid4())


@dataclass
class Task:
    """SPEC §17.1 Task: mission_id, dependencies, assigned_agent, state, retries."""

    mission_id: str
    title: str
    task_id: str = field(default_factory=_new_id)
    state: TaskState = TaskState.PENDING
    dependencies: list[str] = field(default_factory=list)
    assigned_agent: str | None = None
    retries: int = 0
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "mission_id": self.mission_id,
            "title": self.title,
            "state": self.state.value,
            "dependencies": list(self.dependencies),
            "assigned_agent": self.assigned_agent,
            "retries": self.retries,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Task:
        return cls(
            task_id=d["task_id"],
            mission_id=d["mission_id"],
            title=d["title"],
            state=TaskState(d["state"]),
            dependencies=list(d.get("dependencies") or []),
            assigned_agent=d.get("assigned_agent"),
            retries=int(d.get("retries", 0)),
            created_at=datetime.fromisoformat(d["created_at"]),
            updated_at=datetime.fromisoformat(d["updated_at"]),
        )


@dataclass
class Mission:
    """SPEC §17.1 Mission: goal, status, priority, budget, owner, context, checkpoints.

    ``mission_id`` doubles as the event ``correlation_id`` so the whole mission is one chain.
    """

    goal: str
    mission_id: str = field(default_factory=_new_id)
    status: MissionStatus = MissionStatus.CREATED
    priority: Priority = Priority.NORMAL
    budget: dict[str, Any] = field(default_factory=dict)
    owner: str = DEFAULT_USER_ID
    device_id: str | None = None
    context: dict[str, Any] = field(default_factory=dict)
    checkpoints: list[dict[str, Any]] = field(default_factory=list)
    tasks: list[Task] = field(default_factory=list)
    version: int = 0  # increments on every persisted change
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)

    def __post_init__(self) -> None:
        if not isinstance(self.goal, str) or not self.goal.strip():
            raise ValueError("mission goal must be a non-empty string")
        self.status = MissionStatus(self.status)
        self.priority = Priority(self.priority)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATES

    def can_transition(self, to: MissionStatus) -> bool:
        return MissionStatus(to) in MISSION_TRANSITIONS[self.status]

    def task(self, task_id: str) -> Task:
        for t in self.tasks:
            if t.task_id == task_id:
                return t
        raise TaskNotFound(task_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "goal": self.goal,
            "status": self.status.value,
            "priority": self.priority.value,
            "budget": dict(self.budget),
            "owner": self.owner,
            "device_id": self.device_id,
            "context": dict(self.context),
            "checkpoints": list(self.checkpoints),
            "tasks": [t.to_dict() for t in self.tasks],
            "version": self.version,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Mission:
        return cls(
            mission_id=d["mission_id"],
            goal=d["goal"],
            status=MissionStatus(d["status"]),
            priority=Priority(d.get("priority", Priority.NORMAL)),
            budget=dict(d.get("budget") or {}),
            owner=d.get("owner", DEFAULT_USER_ID),
            device_id=d.get("device_id"),
            context=dict(d.get("context") or {}),
            checkpoints=list(d.get("checkpoints") or []),
            tasks=[Task.from_dict(t) for t in d.get("tasks") or []],
            version=int(d.get("version", 0)),
            created_at=datetime.fromisoformat(d["created_at"]),
            updated_at=datetime.fromisoformat(d["updated_at"]),
        )
