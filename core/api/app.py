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

from core.capabilities.gateway import InvocationStatus
from core.events.envelope import Event
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
            # Deep Path: needs the Agent Runtime (Phase 3). Be honest about it.
            await runtime.missions.transition(
                mid, MissionStatus.BLOCKED, reason="agent runtime not available yet (Phase 3)"
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
        if pending is None:
            return {"decision": decision.to_dict(), "resumed": False}
        pending["kw"]["decision_id"] = decision_id
        result = await _execute(runtime, pending["mission_id"], pending)
        return {"decision": decision.to_dict(), "resumed": True, **result}

    @app.post("/approvals/{decision_id}/deny")
    async def deny(decision_id: str, body: DenyIn) -> dict[str, Any]:
        try:
            decision = await runtime.permissions.deny(decision_id, body.reason)
        except ApprovalError as exc:
            raise HTTPException(409, str(exc)) from None
        pending = runtime.pending_commands.pop(decision_id, None)
        if pending is not None:
            await _safe_transition(
                runtime, pending["mission_id"], MissionStatus.CANCELED, body.reason or "denied"
            )
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

    # -- debug UI ------------------------------------------------------------------------------

    @app.get("/debug", response_class=HTMLResponse)
    def debug() -> str:
        return (_STATIC / "debug.html").read_text(encoding="utf-8")

    return app


# -- helpers ---------------------------------------------------------------------------------------


def _row(seq: int, ev: Event) -> dict[str, Any]:
    return {"seq": seq, **ev.to_dict()}


async def _safe_transition(runtime: CoreRuntime, mid: str, to: MissionStatus, reason: str) -> None:
    try:
        await runtime.missions.transition(mid, to, reason=reason)
    except (InvalidTransition, MissionNotFound):
        pass  # mission already terminal or gone; the event log still has the full story


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
