"""Text command pipeline shared by the HTTP API and the voice bridge (Phase 1 DoD, Phase 5).

    text -> IntentRouter -> stop | capability (Fast Path via VerifiedExecutor) | agent (Coordinator)
Every command is a Mission; every step is an event. Nothing here executes anything directly.
"""

from __future__ import annotations

from typing import Any

from core.agents import AgentRun, RunOutcome
from core.capabilities.gateway import InvocationStatus
from core.events.envelope import DEFAULT_USER_ID, Event
from core.missions import InvalidTransition, MissionNotFound, MissionStatus
from core.runtime import CoreRuntime


async def run_text_command(
    runtime: CoreRuntime,
    text: str,
    *,
    user_id: str = DEFAULT_USER_ID,
    device_id: str | None = None,
    device_trusted: bool = False,
    source: str = "core-api",
) -> dict[str, Any]:
    intent = runtime.intents.route(text)
    if intent.kind == "stop":
        await runtime.gateway.halt(f"stop command: {text.strip()}")
        return {"route": "stop", "halted": True}

    mission = await runtime.missions.create(
        text.strip(),
        device_id=device_id,
        owner=user_id,
        context={"intent": intent.to_dict(), "device_trusted": device_trusted},
    )
    mid = mission.mission_id
    await runtime.bus.publish(
        Event.new(
            "command.received",
            source,
            {"text": text, "intent": intent.to_dict(), "mission_id": mid},
            correlation_id=mid,
            user_id=user_id,
            device_id=device_id,
        )
    )
    await runtime.missions.transition(mid, MissionStatus.PLANNING)

    if intent.kind != "capability":
        # Deep Path: hand the goal to the Agent Coordinator, or say that none is configured.
        if runtime.coordinator.can_run():
            await runtime.missions.transition(mid, MissionStatus.RUNNING)
            run = await runtime.coordinator.run(
                mid,
                text.strip(),
                allowlist=set(runtime.capabilities.names()),
                user_id=user_id,
                device_id=device_id,
                device_trusted=device_trusted,
            )
            return await settle_agent_run(runtime, run)
        await runtime.missions.transition(
            mid, MissionStatus.BLOCKED, reason="no intelligence provider configured"
        )
        return {
            "route": "agent",
            "mission_id": mid,
            "status": "blocked",
            "intent": intent.to_dict(),
        }

    await runtime.missions.transition(mid, MissionStatus.RUNNING)
    call = {
        "capability": intent.capability,
        "args": intent.args,
        "kw": {
            "actor": "intent-router",
            "correlation_id": mid,
            "user_id": user_id,
            "device_id": device_id,
            "device_trusted": device_trusted,
        },
    }
    return await execute_call(runtime, mid, call)


async def safe_transition(runtime: CoreRuntime, mid: str, to: MissionStatus, reason: str) -> None:
    try:
        await runtime.missions.transition(mid, to, reason=reason)
    except (InvalidTransition, MissionNotFound):
        pass  # mission already terminal or gone; the event log still has the full story


async def settle_agent_run(runtime: CoreRuntime, run: AgentRun) -> dict[str, Any]:
    """Map an AgentRun outcome onto the mission state machine and build the API response."""
    mid = run.mission_id
    payload: dict[str, Any] = {"route": "agent", "mission_id": mid, "run": run.to_dict()}
    if run.outcome is RunOutcome.AWAITING_APPROVAL:
        await safe_transition(runtime, mid, MissionStatus.WAITING_FOR_APPROVAL, "approval required")
        return {**payload, "status": "waiting_for_approval", "decision_id": run.pending_decision_id}
    if run.outcome is RunOutcome.HALTED:
        await safe_transition(runtime, mid, MissionStatus.PAUSED, "kill switch")
        return {**payload, "status": "halted"}
    if run.outcome is RunOutcome.COMPLETED:
        await safe_transition(runtime, mid, MissionStatus.VERIFYING, "agent finished")
        await safe_transition(runtime, mid, MissionStatus.COMPLETED, "agent run completed")
        return {**payload, "status": "completed", "result": run.final_text}
    await safe_transition(runtime, mid, MissionStatus.FAILED, f"{run.outcome.value}: {run.error}")
    return {**payload, "status": "failed", "error": run.error}


async def execute_call(runtime: CoreRuntime, mid: str, call: dict[str, Any]) -> dict[str, Any]:
    """Run one capability call for a mission via the verified executor; settle the mission."""
    kw = dict(call["kw"])
    kw["correlation_id"] = mid
    if runtime.missions.get(mid).status is MissionStatus.WAITING_FOR_APPROVAL:
        await runtime.missions.transition(mid, MissionStatus.RUNNING, reason="approved")
    result = await runtime.executor.run(call["capability"], call["args"], **kw)
    inv, ver = result.invocation, result.verification
    payload: dict[str, Any] = {
        "route": "capability",
        "mission_id": mid,
        "invocation": inv.to_dict(),
        "verification": ver.to_dict(),
        "attempts": result.attempts,
    }
    if inv.status is InvocationStatus.AWAITING_APPROVAL:
        runtime.pending_commands[inv.decision_id] = {**call, "mission_id": mid}  # type: ignore[index]
        await safe_transition(runtime, mid, MissionStatus.WAITING_FOR_APPROVAL, "approval required")
        return {**payload, "status": "waiting_for_approval", "decision_id": inv.decision_id}
    if inv.status is InvocationStatus.HALTED:
        await safe_transition(runtime, mid, MissionStatus.PAUSED, "kill switch")
        return {**payload, "status": "halted"}
    await safe_transition(runtime, mid, MissionStatus.VERIFYING, "tool finished")
    if result.ok:
        await safe_transition(runtime, mid, MissionStatus.COMPLETED, "verified")
        return {**payload, "status": "completed", "result": inv.result}
    await safe_transition(
        runtime, mid, MissionStatus.FAILED, f"{inv.status.value}/{ver.outcome.value}: {inv.error}"
    )
    return {**payload, "status": "failed", "error": inv.error or ver.reason}
