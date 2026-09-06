"""Scheduler loop + watchdog (SPEC §14.2, PERFORMANCE §4, Phase 11 step 72).

``tick()`` is the unit of work (tests call it directly; ``run_forever`` calls it every minute):

1. run every due job - a ``command`` job goes through the normal command path and therefore
   creates a real mission with the usual permissions; a ``capability`` job may only call P0
   capabilities (predictive preloading) and is refused otherwise;
2. watchdog: missions that sit in planning/running/verifying longer than their budget are
   paused (running/verifying) or failed (planning) with a ``mission.watchdog`` event - never
   silently lost;
3. everything is persisted, so a restart resumes from the job table and the mission store.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from core.capabilities.registry import CapabilityRegistry
from core.events.bus import EventBus
from core.events.envelope import Event
from core.missions.engine import MissionEngine
from core.missions.model import InvalidTransition, MissionStatus
from core.permissions.model import RiskLevel
from core.scheduler.model import Job
from core.scheduler.store import JobStore

SOURCE = "scheduler"
DEFAULT_MISSION_BUDGET_S = 15 * 60
ACTIVE = (MissionStatus.PLANNING, MissionStatus.RUNNING, MissionStatus.VERIFYING)

RunCommand = Callable[..., Awaitable[dict[str, Any]]]
RunCapability = Callable[..., Awaitable[Any]]


class Scheduler:
    def __init__(
        self,
        bus: EventBus,
        store: JobStore,
        missions: MissionEngine,
        capabilities: CapabilityRegistry,
        *,
        run_command: RunCommand,
        run_capability: RunCapability,
        clock: Callable[[], datetime] | None = None,
        interval_s: float = 60.0,
    ) -> None:
        self.bus = bus
        self.store = store
        self._missions = missions
        self._caps = capabilities
        self._run_command = run_command
        self._run_capability = run_capability
        self._clock = clock or (lambda: datetime.now(UTC))
        self.interval_s = interval_s
        self._task: asyncio.Task[None] | None = None
        self.ticks = 0

    # -- jobs ------------------------------------------------------------------------------------

    def add(self, job: Job) -> Job:
        if job.kind == "capability":
            if job.capability not in self._caps:
                raise ValueError(f"unknown capability {job.capability!r}")
            risk = self._caps.get(job.capability).manifest.risk
            if risk is not RiskLevel.P0:
                raise ValueError("capability jobs may only run P0 capabilities (use a command job)")
        return self.store.save(job)

    def ensure_system_jobs(self) -> list[Job]:
        """Idempotent defaults: the daily brief at 07:30 (owner can disable it)."""
        created = []
        if self.store.by_name("daily brief") is None and "brief.generate" in self._caps:
            created.append(
                self.add(
                    Job(
                        name="daily brief",
                        kind="capability",
                        capability="brief.generate",
                        args={"reason": "scheduled"},
                        at="07:30",
                        source="system",
                        created_by="system",
                    )
                )
            )
        return created

    # -- tick --------------------------------------------------------------------------------------

    async def tick(self) -> dict[str, Any]:
        now = self._clock()
        self.ticks += 1
        ran = []
        for job in self.store.due(now):
            ran.append(await self.run_job(job, now))
        watched = await self.watchdog(now)
        return {"ran": ran, "watchdog": watched, "at": now.isoformat()}

    async def run_job(self, job: Job, now: datetime | None = None) -> dict[str, Any]:
        now = now or self._clock()
        await self._emit("job.started", job, {})
        status, mission_id, error = "completed", None, None
        try:
            if job.kind == "command":
                result = await self._run_command(
                    job.text,
                    user_id="local-owner",
                    device_id=f"scheduler:{job.job_id}",
                    device_trusted=False,
                    source="scheduler",
                )
                status = str(result.get("status") or ("halted" if result.get("halted") else "ok"))
                mission_id = result.get("mission_id")
                error = result.get("error")
            else:
                res = await self._run_capability(
                    job.capability,
                    dict(job.args),
                    actor=f"scheduler:{job.job_id}",
                    correlation_id=f"job:{job.job_id}",
                    device_id=f"scheduler:{job.job_id}",
                    device_trusted=False,
                )
                status = "completed" if res.invocation.ok else res.invocation.status.value
                error = res.invocation.error
        except Exception as exc:  # a job must never take the loop down
            status, error = "error", f"{type(exc).__name__}: {str(exc)[:160]}"
        job.runs += 1
        job.last_run_at = now
        job.last_status = status
        job.last_mission_id = mission_id
        job.next_run_at = None if job.exhausted() else job.compute_next(now)
        if job.exhausted():
            job.enabled = False
        self.store.save(job)
        summary = {"status": status, "mission_id": mission_id, "error": error}
        await self._emit("job.finished", job, summary)
        return {"job_id": job.job_id, "name": job.name, **summary}

    async def watchdog(self, now: datetime) -> list[dict[str, Any]]:
        out = []
        for m in self._missions.list():
            if m.status not in ACTIVE:
                continue
            budget = int(m.budget.get("max_seconds") or DEFAULT_MISSION_BUDGET_S)
            age = (now - m.updated_at).total_seconds()
            if age <= budget:
                continue
            target = (
                MissionStatus.FAILED if m.status is MissionStatus.PLANNING else MissionStatus.PAUSED
            )
            reason = f"watchdog: no progress for {int(age)} s (budget {budget} s)"
            await self.bus.publish(
                Event.new(
                    "mission.watchdog",
                    SOURCE,
                    {
                        "status": m.status.value,
                        "age_s": int(age),
                        "budget_s": budget,
                        "to": target.value,
                    },
                    correlation_id=m.mission_id,
                    user_id=m.owner,
                    device_id=m.device_id,
                    priority="urgent",
                )
            )
            with contextlib.suppress(InvalidTransition):
                await self._missions.transition(m.mission_id, target, reason=reason)
            out.append({"mission_id": m.mission_id, "from": m.status.value, "to": target.value})
        return out

    # -- loop --------------------------------------------------------------------------------------

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run_forever(), name="jarvis-scheduler")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def run_forever(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as exc:  # keep ticking; the failure is visible as an event
                with contextlib.suppress(Exception):
                    await self.bus.publish(
                        Event.new(
                            "scheduler.error",
                            SOURCE,
                            {"error": str(exc)[:200]},
                            correlation_id="scheduler",
                        )
                    )
            await asyncio.sleep(self.interval_s)

    def snapshot(self) -> dict[str, Any]:
        jobs = self.store.list()
        return {
            "running": self._task is not None and not self._task.done(),
            "ticks": self.ticks,
            "jobs": [j.to_dict() for j in jobs],
            "next": min((j.next_run_at for j in jobs if j.enabled and j.next_run_at), default=None),
        }

    async def _emit(self, event_type: str, job: Job, extra: dict[str, Any]) -> None:
        await self.bus.publish(
            Event.new(
                event_type,
                SOURCE,
                {"job": job.to_dict(), **extra},
                correlation_id=f"job:{job.job_id}",
            )
        )


def next_minute(now: datetime) -> datetime:
    return (now + timedelta(minutes=1)).replace(second=0, microsecond=0)
