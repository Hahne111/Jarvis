"""MissionEngine: the only writer of mission/task state (SPEC §5.1 "Mission Engine").

Every change is (1) validated against the state machine, (2) published as an event on the
EventBus - which persists it before delivery - and (3) written to the mission snapshot table.
After a restart the snapshot is loaded directly; if it is missing or stale, the state can be
rebuilt deterministically from the event log (``rebuild_from_log``).

Event types (all correlated by mission_id):
    mission.created
    mission.<new_status>                e.g. mission.running, mission.completed
    mission.task.added
    mission.task.<new_state>            e.g. mission.task.running, mission.task.completed
    mission.checkpoint
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from core.events.bus import EventBus
from core.events.envelope import Event, Priority
from core.missions.model import (
    MISSION_TRANSITIONS,
    TASK_TRANSITIONS,
    InvalidTransition,
    Mission,
    MissionNotFound,
    MissionStatus,
    Task,
    TaskNotFound,
    TaskState,
)
from core.missions.repository import MissionRepository

SOURCE = "mission-engine"


class MissionEngine:
    def __init__(self, bus: EventBus, repo: MissionRepository) -> None:
        self._bus = bus
        self._repo = repo

    # -- queries -------------------------------------------------------------------------------

    def get(self, mission_id: str) -> Mission:
        mission = self._repo.get(mission_id)
        if mission is None:
            raise MissionNotFound(mission_id)
        return mission

    def list(self, status: MissionStatus | None = None) -> list[Mission]:
        return self._repo.list(status)

    # -- commands ------------------------------------------------------------------------------

    async def create(
        self,
        goal: str,
        *,
        priority: Priority | str = Priority.NORMAL,
        budget: dict[str, Any] | None = None,
        owner: str | None = None,
        device_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> Mission:
        kwargs: dict[str, Any] = {
            "priority": Priority(priority),
            "budget": dict(budget or {}),
            "device_id": device_id,
            "context": dict(context or {}),
        }
        if owner is not None:
            kwargs["owner"] = owner
        mission = Mission(goal=goal, **kwargs)
        await self._commit(mission, "mission.created", mission.to_dict())
        return mission

    async def transition(
        self,
        mission_id: str,
        to: MissionStatus | str,
        *,
        reason: str | None = None,
    ) -> Mission:
        mission = self.get(mission_id)
        target = MissionStatus(to)
        if target not in MISSION_TRANSITIONS[mission.status]:
            raise InvalidTransition(
                f"mission {mission_id}: {mission.status.value} -> {target.value} is not allowed"
            )
        payload = {"from": mission.status.value, "to": target.value, "reason": reason}
        mission.status = target
        await self._commit(mission, f"mission.{target.value}", payload)
        return mission

    async def checkpoint(self, mission_id: str, data: dict[str, Any]) -> Mission:
        mission = self.get(mission_id)
        if mission.is_terminal:
            raise InvalidTransition(f"mission {mission_id} is terminal ({mission.status.value})")
        entry = {"at": datetime.now(UTC).isoformat(), **data}
        mission.checkpoints.append(entry)
        await self._commit(mission, "mission.checkpoint", {"checkpoint": entry})
        return mission

    async def add_task(
        self,
        mission_id: str,
        title: str,
        *,
        dependencies: list[str] | None = None,
        assigned_agent: str | None = None,
    ) -> Task:
        mission = self.get(mission_id)
        if mission.is_terminal:
            raise InvalidTransition(f"mission {mission_id} is terminal ({mission.status.value})")
        if not isinstance(title, str) or not title.strip():
            raise ValueError("task title must be a non-empty string")
        deps = list(dependencies or [])
        known = {t.task_id for t in mission.tasks}
        missing = [d for d in deps if d not in known]
        if missing:
            raise TaskNotFound(f"unknown dependencies for mission {mission_id}: {missing}")
        task = Task(
            mission_id=mission_id, title=title, dependencies=deps, assigned_agent=assigned_agent
        )
        mission.tasks.append(task)
        await self._commit(mission, "mission.task.added", {"task": task.to_dict()})
        return task

    async def set_task_state(
        self,
        mission_id: str,
        task_id: str,
        to: TaskState | str,
        *,
        reason: str | None = None,
    ) -> Task:
        mission = self.get(mission_id)
        task = mission.task(task_id)
        target = TaskState(to)
        if target not in TASK_TRANSITIONS[task.state]:
            raise InvalidTransition(
                f"task {task_id}: {task.state.value} -> {target.value} is not allowed"
            )
        if target is TaskState.RUNNING:
            if mission.status is not MissionStatus.RUNNING:
                raise InvalidTransition(
                    f"task {task_id} cannot start while mission is {mission.status.value}"
                )
            unmet = [
                d for d in task.dependencies if mission.task(d).state is not TaskState.COMPLETED
            ]
            if unmet:
                raise InvalidTransition(f"task {task_id} has unmet dependencies: {unmet}")
        payload = {
            "task_id": task_id,
            "from": task.state.value,
            "to": target.value,
            "reason": reason,
        }
        if task.state in (TaskState.RUNNING, TaskState.FAILED) and target is TaskState.PENDING:
            task.retries += 1
            payload["retries"] = task.retries
        task.state = target
        await self._commit(mission, f"mission.task.{target.value}", payload, task=task)
        return task

    # -- recovery ------------------------------------------------------------------------------

    async def rebuild_from_log(self) -> int:
        """Rebuild all mission snapshots from the event log. Returns the number of missions."""
        missions: dict[str, Mission] = {}
        for _, event in self._bus.replay(type_prefix="mission"):
            self._apply(missions, event)
        self._repo.delete_all()
        for mission in missions.values():
            self._repo.save(mission)
        return len(missions)

    @staticmethod
    def _apply(missions: dict[str, Mission], event: Event) -> None:
        mid = event.correlation_id
        if event.type == "mission.created":
            missions[mid] = Mission.from_dict(event.payload)
            return
        mission = missions.get(mid)
        if mission is None:
            return  # event from an unknown mission (e.g. log truncated); skip deterministically
        parts = event.type.split(".")
        if parts[1] == "task":
            if parts[2] == "added":
                mission.tasks.append(Task.from_dict(event.payload["task"]))
            else:
                task = mission.task(event.payload["task_id"])
                task.state = TaskState(event.payload["to"])
                task.retries = int(event.payload.get("retries", task.retries))
                task.updated_at = event.timestamp
        elif parts[1] == "checkpoint":
            mission.checkpoints.append(dict(event.payload["checkpoint"]))
        else:
            mission.status = MissionStatus(event.payload["to"])
        mission.version += 1
        mission.updated_at = event.timestamp

    # -- internals -----------------------------------------------------------------------------

    async def _commit(
        self,
        mission: Mission,
        event_type: str,
        payload: dict[str, Any],
        *,
        task: Task | None = None,
    ) -> None:
        if event_type != "mission.created":
            mission.version += 1
        event = Event.new(
            event_type,
            SOURCE,
            payload,
            correlation_id=mission.mission_id,
            user_id=mission.owner,
            device_id=mission.device_id,
            priority=mission.priority,
        )
        await self._bus.publish(event)  # durable log entry first
        mission.updated_at = event.timestamp
        if task is not None:
            task.updated_at = event.timestamp  # same clock as the log, so rebuilds are identical
        self._repo.save(mission)
