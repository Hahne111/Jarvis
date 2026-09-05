"""FastAPI app for the Core process.

Endpoints
    GET  /health                       runtime + capability health
    GET  /events?after_seq&correlation_id&type_prefix&limit   replay from the durable store
    WS   /ws/events?after_seq=N        replay-then-live without gaps (reads only persisted events)
    POST /commands {text, device_id?, device_trusted?}         text command -> intent -> mission
    GET  /missions, /missions/{id}
    GET  /approvals                    pending approvals
    POST /approvals/{decision_id}/approve {method, device_id, device_trusted, reference?}
    POST /approvals/{decision_id}/deny {reason?}
    POST /kill, POST /resume {method, device_id, device_trusted}
    GET  /debug                        minimal debug dashboard (static HTML)
    GET  /memory?q&type&project        "What JARVIS Knows" (SPEC §8.4), /memory/{id}
    POST /memory/{id}/correct|forget|pin|unpin|temporary, /memory/forget_since, /memory/policy

The UI only ever sees events that are already persisted (SECURITY.md §3), and the API never
executes anything itself - every side effect goes through the ExecutionGateway.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from core.agents import AgentRun, RunOutcome
from core.capabilities.gateway import InvocationStatus
from core.events.envelope import Event
from core.memory import MemoryPolicyError, MemoryType
from core.missions import InvalidTransition, MissionNotFound, MissionStatus
from core.permissions import ApprovalError, ApprovalProof, PolicyViolation, ProofMethod
from core.runtime import CoreRuntime

_STATIC = Path(__file__).parent / "static"


class CommandIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    device_id: str | None = None
    device_trusted: bool = False
    user_id: str = "local-owner"


class ProofIn(BaseModel):
    method: ProofMethod
    device_id: str | None = None
    device_trusted: bool = False
    reference: str | None = None
    subject: str = "local-owner"

    def to_proof(self) -> ApprovalProof:
        return ApprovalProof(
            method=self.method,
            subject=self.subject,
            device_id=self.device_id,
            device_trusted=self.device_trusted,
            reference=self.reference,
        )


class DenyIn(BaseModel):
    reason: str | None = None


class CorrectIn(BaseModel):
    value: str = Field(min_length=1, max_length=4000)


class TemporaryIn(BaseModel):
    ttl_s: int = Field(gt=0)


class ForgetSinceIn(BaseModel):
    minutes: int = Field(gt=0, le=60 * 24 * 30)
    reason: str | None = None


class PolicyIn(BaseModel):
    learn_from_observation: bool | None = None
    conversation_memory: bool | None = None


class DontLearnIn(BaseModel):
    subject: str = Field(min_length=1)
    predicate: str = "*"


def create_app(runtime: CoreRuntime) -> FastAPI:
    app = FastAPI(title="JARVIS Core", version=runtime.version, docs_url="/docs")
    app.state.runtime = runtime

    # -- health / events -----------------------------------------------------------------------

    @app.get("/health")
    def health() -> dict[str, Any]:
        return runtime.health()

    @app.get("/events")
    def events(
        after_seq: int = Query(0, ge=0),
        correlation_id: str | None = None,
        type_prefix: str | None = None,
        limit: int = Query(200, ge=1, le=2000),
    ) -> list[dict[str, Any]]:
        rows = runtime.store.replay(
            after_seq=after_seq, correlation_id=correlation_id, type_prefix=type_prefix, limit=limit
        )
        return [_row(seq, ev) for seq, ev in rows]

    @app.websocket("/ws/events")
    async def ws_events(websocket: WebSocket, after_seq: int = Query(0, ge=0)) -> None:
        await websocket.accept()
        wakeup: asyncio.Queue[str] = asyncio.Queue()
        sub = runtime.bus.subscribe("*", lambda ev: wakeup.put_nowait(ev.event_id))
        last = after_seq
        try:
            while True:
                # Only persisted rows are streamed; the bus is just the wake-up signal.
                for seq, ev in runtime.store.replay(after_seq=last):
                    await websocket.send_text(json.dumps(_row(seq, ev)))
                    last = seq
                try:
                    await asyncio.wait_for(wakeup.get(), timeout=1.0)
                except TimeoutError:
                    pass
        except WebSocketDisconnect:
            pass
        finally:
            runtime.bus.unsubscribe(sub)

    # -- commands ------------------------------------------------------------------------------

    @app.post("/commands")
    async def commands(body: CommandIn) -> dict[str, Any]:
        intent = runtime.intents.route(body.text)
        if intent.kind == "stop":
            await runtime.gateway.halt(f"stop command: {body.text.strip()}")
            return {"route": "stop", "halted": True}

        mission = await runtime.missions.create(
            body.text.strip(),
            device_id=body.device_id,
            owner=body.user_id,
            context={"intent": intent.to_dict(), "device_trusted": body.device_trusted},
        )
        mid = mission.mission_id
        await runtime.bus.publish(
            Event.new(
                "command.received",
                "core-api",
                {"text": body.text, "intent": intent.to_dict(), "mission_id": mid},
                correlation_id=mid,
                user_id=body.user_id,
                device_id=body.device_id,
            )
        )
        await runtime.missions.transition(mid, MissionStatus.PLANNING)

        if intent.kind != "capability":
            # Deep Path: hand the goal to the Agent Coordinator, or say that none is configured.
            if runtime.coordinator.can_run():
                await runtime.missions.transition(mid, MissionStatus.RUNNING)
                run = await runtime.coordinator.run(
                    mid,
                    body.text.strip(),
                    allowlist=set(runtime.capabilities.names()),
                    user_id=body.user_id,
                    device_id=body.device_id,
                    device_trusted=body.device_trusted,
                )
                return await _settle_agent_run(runtime, run)
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
                "user_id": body.user_id,
                "device_id": body.device_id,
                "device_trusted": body.device_trusted,
            },
        }
        return await _execute(runtime, mid, call)

    # -- missions ------------------------------------------------------------------------------

    @app.get("/missions")
    def missions(status: MissionStatus | None = None) -> list[dict[str, Any]]:
        return [m.to_dict() for m in runtime.missions.list(status)]

    @app.get("/missions/{mission_id}")
    def mission(mission_id: str) -> dict[str, Any]:
        try:
            return runtime.missions.get(mission_id).to_dict()
        except MissionNotFound:
            raise HTTPException(404, "mission not found") from None

    # -- approvals -----------------------------------------------------------------------------

    @app.get("/approvals")
    def approvals() -> list[dict[str, Any]]:
        return [d.to_dict() for d in runtime.permissions.pending()]

    @app.post("/approvals/{decision_id}/approve")
    async def approve(decision_id: str, body: ProofIn) -> dict[str, Any]:
        try:
            decision = await runtime.permissions.approve(decision_id, body.to_proof())
        except ApprovalError as exc:
            raise HTTPException(409, str(exc)) from None
        pending = runtime.pending_commands.pop(decision_id, None)
        if pending is not None:
            pending["kw"]["decision_id"] = decision_id
            result = await _execute(runtime, pending["mission_id"], pending)
            return {"decision": decision.to_dict(), "resumed": True, **result}
        run = await runtime.coordinator.resume(decision_id)  # paused agent run in the event log?
        if run is not None:
            if runtime.missions.get(run.mission_id).status is MissionStatus.WAITING_FOR_APPROVAL:
                await runtime.missions.transition(
                    run.mission_id, MissionStatus.RUNNING, reason="approved"
                )
            result = await _settle_agent_run(runtime, run)
            return {"decision": decision.to_dict(), "resumed": True, **result}
        return {"decision": decision.to_dict(), "resumed": False}

    @app.post("/approvals/{decision_id}/deny")
    async def deny(decision_id: str, body: DenyIn) -> dict[str, Any]:
        try:
            decision = await runtime.permissions.deny(decision_id, body.reason)
        except ApprovalError as exc:
            raise HTTPException(409, str(exc)) from None
        pending = runtime.pending_commands.pop(decision_id, None)
        mission_id = pending["mission_id"] if pending else decision.request.correlation_id
        await _safe_transition(runtime, mission_id, MissionStatus.CANCELED, body.reason or "denied")
        return {"decision": decision.to_dict()}

    # -- kill switch ---------------------------------------------------------------------------

    @app.post("/kill")
    async def kill() -> dict[str, Any]:
        await runtime.gateway.halt("kill switch via API")
        return {"halted": True}

    @app.post("/resume")
    async def resume(body: ProofIn) -> dict[str, Any]:
        try:
            await runtime.gateway.resume(body.to_proof())
        except (ApprovalError, PolicyViolation) as exc:
            raise HTTPException(403, str(exc)) from None
        return {"halted": False}

    # -- memory: "What JARVIS Knows" (SPEC §8.4) -----------------------------------------------

    @app.get("/memory")
    def memory_list(
        q: str | None = None,
        type: MemoryType | None = None,
        project: str | None = None,
        limit: int = Query(50, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        if q:
            return [
                {**item.to_dict(), "score": score}
                for score, item in runtime.memory.search(
                    q, type=type, project_scope=project, limit=limit
                )
            ]
        return [i.to_dict() for i in runtime.memory.list(type=type, project_scope=project)[:limit]]

    @app.get("/memory/policy")
    def memory_policy() -> dict[str, Any]:
        p = runtime.memory_writer.policy
        return {
            "learn_from_observation": p.learn_from_observation,
            "conversation_memory": p.conversation_memory,
            "dont_learn": sorted(p.dont_learn),
            "max_temporary_s": p.max_temporary_s,
        }

    @app.post("/memory/policy")
    async def memory_policy_set(body: PolicyIn) -> dict[str, Any]:
        p = runtime.memory_writer.policy
        if body.learn_from_observation is not None:
            p.learn_from_observation = body.learn_from_observation
        if body.conversation_memory is not None:
            p.conversation_memory = body.conversation_memory
        await runtime.bus.publish(
            Event.new("memory.policy_changed", "core-api", memory_policy(), correlation_id="memory")
        )
        return memory_policy()

    @app.post("/memory/dont_learn")
    async def memory_dont_learn(body: DontLearnIn) -> dict[str, Any]:
        await runtime.memory_writer.dont_learn(body.subject, body.predicate)
        return memory_policy()

    @app.post("/memory/forget_since")
    async def memory_forget_since(body: ForgetSinceIn) -> dict[str, Any]:
        from datetime import UTC, datetime, timedelta

        since = datetime.now(UTC) - timedelta(minutes=body.minutes)
        n = await runtime.memory_writer.forget_since(since, reason=body.reason or "owner request")
        return {"deleted": n, "since": since.isoformat()}

    @app.get("/memory/{memory_id}")
    def memory_get(memory_id: str) -> dict[str, Any]:
        item = runtime.memory.get(memory_id)
        if item is None:
            raise HTTPException(404, "memory not found")
        return item.to_dict()

    @app.post("/memory/{memory_id}/correct")
    async def memory_correct(memory_id: str, body: CorrectIn) -> dict[str, Any]:
        try:
            result = await runtime.memory_writer.correct(memory_id, body.value)
        except KeyError:
            raise HTTPException(404, "memory not found") from None
        except (ValueError, MemoryPolicyError) as exc:
            raise HTTPException(409, str(exc)) from None
        return {"action": result.action, "memory": result.item.to_dict() if result.item else None}

    @app.post("/memory/{memory_id}/forget")
    async def memory_forget(memory_id: str) -> dict[str, Any]:
        if not await runtime.memory_writer.forget(memory_id):
            raise HTTPException(404, "memory not found")
        return {"forgotten": memory_id}

    @app.post("/memory/{memory_id}/pin")
    async def memory_pin(memory_id: str) -> dict[str, Any]:
        return await _memory_action(runtime, memory_id, pinned=True)

    @app.post("/memory/{memory_id}/unpin")
    async def memory_unpin(memory_id: str) -> dict[str, Any]:
        return await _memory_action(runtime, memory_id, pinned=False)

    @app.post("/memory/{memory_id}/temporary")
    async def memory_temporary(memory_id: str, body: TemporaryIn) -> dict[str, Any]:
        try:
            return (await runtime.memory_writer.make_temporary(memory_id, body.ttl_s)).to_dict()
        except KeyError:
            raise HTTPException(404, "memory not found") from None

    # -- debug UI ------------------------------------------------------------------------------

    @app.get("/debug", response_class=HTMLResponse)
    def debug() -> str:
        return (_STATIC / "debug.html").read_text(encoding="utf-8")

    return app


# -- helpers ---------------------------------------------------------------------------------------


def _row(seq: int, ev: Event) -> dict[str, Any]:
    return {"seq": seq, **ev.to_dict()}


async def _memory_action(runtime: CoreRuntime, memory_id: str, *, pinned: bool) -> dict[str, Any]:
    try:
        return (await runtime.memory_writer.pin(memory_id, pinned)).to_dict()
    except KeyError:
        raise HTTPException(404, "memory not found") from None


async def _safe_transition(runtime: CoreRuntime, mid: str, to: MissionStatus, reason: str) -> None:
    try:
        await runtime.missions.transition(mid, to, reason=reason)
    except (InvalidTransition, MissionNotFound):
        pass  # mission already terminal or gone; the event log still has the full story


async def _settle_agent_run(runtime: CoreRuntime, run: AgentRun) -> dict[str, Any]:
    """Map an AgentRun outcome onto the mission state machine and build the API response."""
    mid = run.mission_id
    payload: dict[str, Any] = {"route": "agent", "mission_id": mid, "run": run.to_dict()}
    if run.outcome is RunOutcome.AWAITING_APPROVAL:
        await _safe_transition(
            runtime, mid, MissionStatus.WAITING_FOR_APPROVAL, "approval required"
        )
        return {**payload, "status": "waiting_for_approval", "decision_id": run.pending_decision_id}
    if run.outcome is RunOutcome.HALTED:
        await _safe_transition(runtime, mid, MissionStatus.PAUSED, "kill switch")
        return {**payload, "status": "halted"}
    if run.outcome is RunOutcome.COMPLETED:
        await _safe_transition(runtime, mid, MissionStatus.VERIFYING, "agent finished")
        await _safe_transition(runtime, mid, MissionStatus.COMPLETED, "agent run completed")
        return {**payload, "status": "completed", "result": run.final_text}
    await _safe_transition(runtime, mid, MissionStatus.FAILED, f"{run.outcome.value}: {run.error}")
    return {**payload, "status": "failed", "error": run.error}


async def _execute(runtime: CoreRuntime, mid: str, call: dict[str, Any]) -> dict[str, Any]:
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
        await _safe_transition(
            runtime, mid, MissionStatus.WAITING_FOR_APPROVAL, "approval required"
        )
        return {**payload, "status": "waiting_for_approval", "decision_id": inv.decision_id}
    if inv.status is InvocationStatus.HALTED:
        await _safe_transition(runtime, mid, MissionStatus.PAUSED, "kill switch")
        return {**payload, "status": "halted"}
    await _safe_transition(runtime, mid, MissionStatus.VERIFYING, "tool finished")
    if result.ok:
        await _safe_transition(runtime, mid, MissionStatus.COMPLETED, "verified")
        return {**payload, "status": "completed", "result": inv.result}
    await _safe_transition(
        runtime, mid, MissionStatus.FAILED, f"{inv.status.value}/{ver.outcome.value}: {inv.error}"
    )
    return {**payload, "status": "failed", "error": inv.error or ver.reason}
