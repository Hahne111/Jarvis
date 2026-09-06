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
    GET  /devices, POST /devices/enroll/start, POST /devices/enroll, POST /devices/{id}/revoke|trust
    POST /missions/{id}/handover {to_device_id}   move responsibility desktop <-> mobile
    GET  /notifications                             push deliveries (notify.sent|failed)
    Signed requests (X-Jarvis-Device/Timestamp/Nonce/Signature) bind device_trusted to the
    registry; unsigned callers are trusted only from loopback (core/devices/auth.py, ADR-0004)
    GET  /presence                     derived presence per device (docs/HUD_EVENTS.md)
    GET  /debug                        minimal debug dashboard (static HTML)
    GET  /hud/                         web-first HUD shell (apps/desktop/web, ADR-0003)
    GET  /workspace/{mission}/files|file|diff|preview/{path}   read-only coding-mode views
    PUT  /workspace/{mission}/file                             editor save via workspace.write
    GET  /memory?q&type&project        "What JARVIS Knows" (SPEC §8.4), /memory/{id}
    POST /memory/{id}/correct|forget|pin|unpin|temporary, /memory/forget_since, /memory/policy

The UI only ever sees events that are already persisted (SECURITY.md §3), and the API never
executes anything itself - every side effect goes through the ExecutionGateway.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
from pathlib import Path
from typing import Annotated, Any

from adapters.home import HomeUnavailable
from adapters.workspace import WorkspaceError
from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from core.api.commands import (
    execute_call,
    run_text_command,
    safe_transition,
    settle_agent_run,
    spoken_summary,
)
from core.capabilities import InvocationStatus
from core.devices import AuthError, Caller, DeviceType, EnrollmentError, valid_public_key
from core.events.envelope import Event, Priority
from core.memory import MemoryPolicyError, MemoryType
from core.missions import InvalidTransition, MissionNotFound, MissionStatus
from core.permissions import ApprovalError, ApprovalProof, PolicyViolation, ProofMethod
from core.runtime import CoreRuntime

_STATIC = Path(__file__).parent / "static"
_PREVIEW_TYPES = {
    ".html": "text/html",
    ".htm": "text/html",
    ".css": "text/css",
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".txt": "text/plain",
    ".md": "text/plain",
}
_HUD = Path(__file__).resolve().parents[2] / "apps" / "desktop" / "web"
mimetypes.add_type("application/manifest+json", ".webmanifest")


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


class SatelliteIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    satellite_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    user_id: str = "local-owner"
    device_trusted: bool = False  # strict default; the owner enrolls satellites explicitly


class EnrollStartIn(BaseModel):
    name_hint: str = Field(default="", max_length=120)
    type: DeviceType = DeviceType.MOBILE
    trusted: bool = True


class EnrollIn(BaseModel):
    code: str = Field(min_length=4, max_length=32)
    name: str = Field(min_length=1, max_length=120)
    public_key: str = Field(min_length=40, max_length=64)


class RevokeIn(BaseModel):
    reason: str | None = None


class TrustIn(BaseModel):
    trusted: bool


class HandoverIn(BaseModel):
    to_device_id: str = Field(min_length=1, max_length=120)
    note: str | None = Field(default=None, max_length=200)


class WorkspaceWriteIn(BaseModel):
    path: str = Field(min_length=1, max_length=512)
    content: str = Field(max_length=2_000_000)
    device_id: str | None = None
    device_trusted: bool = False
    user_id: str = "local-owner"


async def resolve_caller(request: Request) -> Caller:
    """Who is calling: a signed device (the registry decides trust) or the loopback owner."""
    runtime: CoreRuntime = request.app.state.runtime
    body = await request.body()
    host = request.client.host if request.client else None
    try:
        return runtime.auth.resolve(
            dict(request.headers), request.method, request.url.path, body, host
        )
    except AuthError as exc:
        await runtime.bus.publish(
            Event.new(
                "device.auth.failed",
                "core-api",
                {"device_id": exc.device_id, "reason": exc.reason, "path": request.url.path},
                correlation_id="devices",
                device_id=exc.device_id,
            )
        )
        raise HTTPException(401, f"device authentication failed: {exc.reason}") from None


CallerDep = Annotated[Caller, Depends(resolve_caller)]


def create_app(runtime: CoreRuntime) -> FastAPI:
    app = FastAPI(title="JARVIS Core", version=runtime.version, docs_url="/docs")
    app.state.runtime = runtime

    def proof_for(body: ProofIn, who: Caller) -> ApprovalProof:
        """Bind the proof to what the Core can verify about the caller (SECURITY.md §2 rule 1)."""
        strong = body.method in (
            ProofMethod.PASSKEY,
            ProofMethod.BIOMETRIC,
            ProofMethod.HARDWARE_KEY,
        )
        if strong and not who.may_prove_strongly:
            raise HTTPException(
                403, "a strong proof needs a signed, trusted device or the local owner"
            )
        if body.method is ProofMethod.UI_CONFIRM and not (who.local or who.trusted):
            raise HTTPException(403, "ui_confirm needs an unlocked trusted device")
        proof = body.to_proof()
        return ApprovalProof(
            method=proof.method,
            subject=proof.subject,
            device_id=who.device_id(proof.device_id),
            device_trusted=who.effective_trust(proof.device_trusted),
            reference=proof.reference,
        )

    def owner_only(who: Caller) -> None:
        if not (who.local or (who.signed and who.trusted)):
            raise HTTPException(403, "only the local owner or a trusted device may do this")

    # -- health / events -----------------------------------------------------------------------

    @app.get("/health")
    def health() -> dict[str, Any]:
        return runtime.health()

    @app.get("/presence")
    def presence() -> dict[str, Any]:
        return runtime.presence.snapshot()

    @app.get("/home")
    async def home() -> dict[str, Any]:
        """Rooms, devices and the home state (read-only; actions go through the gate)."""
        if runtime.home is None:
            return {"enabled": False}
        try:
            online = await runtime.home.sync()
        except HomeUnavailable:
            online = False
        return {"enabled": True, "online": online, **runtime.home.snapshot()}

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
    async def commands(body: CommandIn, who: CallerDep) -> dict[str, Any]:
        return await run_text_command(
            runtime,
            body.text,
            user_id=body.user_id,
            device_id=who.device_id(body.device_id),
            device_trusted=who.effective_trust(body.device_trusted),
            source="core-api",
        )

    @app.post("/satellite/command")
    async def satellite_command(body: SatelliteIn, who: CallerDep) -> dict[str, Any]:
        """Voice satellite (e.g. Home Assistant Assist 'Hey Jarvis'): final transcript in,
        short spoken answer out. The satellite is a device like any other: voice can never
        satisfy a P3+ approval, and it is untrusted unless the owner enrolled it."""
        dev = who.device_id(f"satellite:{body.satellite_id}")
        trusted = who.effective_trust(body.device_trusted)
        ev = dict(correlation_id="voice", user_id=body.user_id, device_id=dev)
        await runtime.bus.publish(
            Event.new("voice.transcript", "satellite", {"text": body.text, "final": True}, **ev)
        )
        await runtime.bus.publish(Event.new("voice.thinking", "satellite", {}, **ev))
        result = await run_text_command(
            runtime,
            body.text,
            user_id=body.user_id,
            device_id=dev,
            device_trusted=trusted,
            source="satellite",
        )
        speech = spoken_summary(result)
        await runtime.bus.publish(Event.new("voice.speaking", "satellite", {"text": speech}, **ev))
        await runtime.bus.publish(Event.new("voice.idle", "satellite", {}, **ev))
        return {**result, "speech": speech, "device_id": dev}

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

    @app.post("/missions/{mission_id}/handover")
    async def mission_handover(mission_id: str, body: HandoverIn, who: CallerDep) -> dict[str, Any]:
        """Desktop <-> mobile handover (SPEC §10): the mission stays one object in the Core;
        only the responsible device changes (visible in presence and the mission checkpoints)."""
        owner_only(who)
        try:
            m = runtime.missions.get(mission_id)
        except MissionNotFound:
            raise HTTPException(404, "mission not found") from None
        target = runtime.devices.get(body.to_device_id)
        if target is not None and target.revoked:
            raise HTTPException(409, "target device is revoked")
        to_device = target.device_id if target else body.to_device_id
        from_device = who.device_id(m.device_id)
        payload = {
            "from_device": from_device,
            "to_device": to_device,
            "status": m.status.value,
            "note": body.note,
            "by": who.device_id("local"),
        }
        try:
            await runtime.missions.checkpoint(mission_id, {"handover": payload})
        except InvalidTransition as exc:
            raise HTTPException(409, str(exc)) from None
        await runtime.bus.publish(
            Event.new(
                "mission.handover",
                "core-api",
                payload,
                correlation_id=mission_id,
                user_id=m.owner,
                device_id=to_device,
                priority=Priority.URGENT
                if m.status is MissionStatus.WAITING_FOR_APPROVAL
                else Priority.NORMAL,
            )
        )
        return {"mission": runtime.missions.get(mission_id).to_dict(), "handover": payload}

    @app.get("/notifications")
    def notifications(limit: int = Query(50, ge=1, le=500)) -> list[dict[str, Any]]:
        rows = runtime.store.replay(type_prefix="notify", limit=5000)
        return [_row(seq, ev) for seq, ev in rows][-limit:]

    # -- approvals -----------------------------------------------------------------------------

    @app.get("/approvals")
    def approvals() -> list[dict[str, Any]]:
        return [d.to_dict() for d in runtime.permissions.pending()]

    @app.post("/approvals/{decision_id}/approve")
    async def approve(decision_id: str, body: ProofIn, who: CallerDep) -> dict[str, Any]:
        try:
            decision = await runtime.permissions.approve(decision_id, proof_for(body, who))
        except ApprovalError as exc:
            raise HTTPException(409, str(exc)) from None
        pending = runtime.pending_commands.pop(decision_id, None)
        if pending is not None:
            pending["kw"]["decision_id"] = decision_id
            result = await execute_call(runtime, pending["mission_id"], pending)
            return {"decision": decision.to_dict(), "resumed": True, **result}
        run = await runtime.coordinator.resume(decision_id)  # paused agent run in the event log?
        if run is not None:
            if runtime.missions.get(run.mission_id).status is MissionStatus.WAITING_FOR_APPROVAL:
                await runtime.missions.transition(
                    run.mission_id, MissionStatus.RUNNING, reason="approved"
                )
            result = await settle_agent_run(runtime, run)
            return {"decision": decision.to_dict(), "resumed": True, **result}
        return {"decision": decision.to_dict(), "resumed": False}

    @app.post("/approvals/{decision_id}/deny")
    async def deny(decision_id: str, body: DenyIn, who: CallerDep) -> dict[str, Any]:
        owner_only(who)  # deny is final for the mission: not for anonymous remote callers
        try:
            decision = await runtime.permissions.deny(decision_id, body.reason)
        except ApprovalError as exc:
            raise HTTPException(409, str(exc)) from None
        pending = runtime.pending_commands.pop(decision_id, None)
        mission_id = pending["mission_id"] if pending else decision.request.correlation_id
        await safe_transition(runtime, mission_id, MissionStatus.CANCELED, body.reason or "denied")
        return {"decision": decision.to_dict()}

    # -- kill switch ---------------------------------------------------------------------------

    @app.post("/kill")
    async def kill() -> dict[str, Any]:
        await runtime.gateway.halt("kill switch via API")
        return {"halted": True}

    @app.post("/resume")
    async def resume(body: ProofIn, who: CallerDep) -> dict[str, Any]:
        try:
            await runtime.gateway.resume(proof_for(body, who))
        except (ApprovalError, PolicyViolation) as exc:
            raise HTTPException(403, str(exc)) from None
        return {"halted": False}

    # -- devices: enrollment, trust, revocation (Phase 9, SPEC §10) ---------------------------

    async def _device_event(event_type: str, payload: dict[str, Any], device_id: str) -> None:
        await runtime.bus.publish(
            Event.new(
                event_type, "core-api", payload, correlation_id="devices", device_id=device_id
            )
        )

    @app.get("/devices")
    def devices_list(who: CallerDep) -> dict[str, Any]:
        return {
            "caller": who.to_dict(),
            "devices": [d.to_dict() for d in runtime.devices.list()],
            "pending_enrollments": [
                e.to_dict(with_code=False) for e in runtime.devices.pending_enrollments()
            ],
        }

    @app.post("/devices/enroll/start")
    async def devices_enroll_start(body: EnrollStartIn, who: CallerDep) -> dict[str, Any]:
        owner_only(who)
        e = runtime.devices.start_enrollment(
            name_hint=body.name_hint,
            type=body.type,
            trusted=body.trusted,
            created_by=who.device_id("local") or "local",
        )
        await _device_event(
            "device.enrollment.started", e.to_dict(with_code=False), e.enrollment_id
        )
        return e.to_dict(with_code=True)  # the code is shown once, here, never in an event

    @app.post("/devices/enroll")
    async def devices_enroll(body: EnrollIn) -> dict[str, Any]:
        if not valid_public_key(body.public_key):
            raise HTTPException(400, "public_key must be a base64 raw Ed25519 public key")
        try:
            device = runtime.devices.complete_enrollment(
                body.code, name=body.name, public_key=body.public_key
            )
        except EnrollmentError as exc:
            await _device_event("device.enrollment.failed", {"reason": str(exc)}, "unknown")
            raise HTTPException(403, str(exc)) from None
        await _device_event("device.enrolled", device.to_dict(), device.device_id)
        return device.to_dict()

    @app.post("/devices/{device_id}/revoke")
    async def devices_revoke(device_id: str, body: RevokeIn, who: CallerDep) -> dict[str, Any]:
        owner_only(who)
        try:
            device = runtime.devices.revoke(device_id, body.reason)
        except KeyError:
            raise HTTPException(404, "device not found") from None
        await _device_event(
            "device.revoked", {**device.to_dict(), "by": who.device_id("local")}, device_id
        )
        return device.to_dict()

    @app.post("/devices/{device_id}/trust")
    async def devices_trust(device_id: str, body: TrustIn, who: CallerDep) -> dict[str, Any]:
        owner_only(who)
        current = runtime.devices.get(device_id)
        if current is None:
            raise HTTPException(404, "device not found")
        if current.revoked:
            raise HTTPException(409, "a revoked device cannot be trusted again; enroll anew")
        device = runtime.devices.set_trusted(device_id, body.trusted)
        await _device_event(
            "device.trust.changed", {**device.to_dict(), "by": who.device_id("local")}, device_id
        )
        return device.to_dict()

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

    # -- workspace views for the HUD coding mode (read-only; writes/runs go through the gate) ---

    @app.get("/workspace/{mission_id}/files")
    def workspace_files(mission_id: str) -> dict[str, Any]:
        try:
            files = runtime.workspaces.list(mission_id)
        except WorkspaceError as exc:
            raise HTTPException(400, str(exc)) from None
        return {"mission_id": mission_id, "files": files}

    @app.get("/workspace/{mission_id}/file")
    def workspace_file(mission_id: str, path: str) -> dict[str, Any]:
        try:
            content = runtime.workspaces.read(mission_id, path)
        except WorkspaceError as exc:
            raise HTTPException(404 if "no such" in str(exc) else 400, str(exc)) from None
        return {"path": path, "content": content}

    @app.put("/workspace/{mission_id}/file")
    async def workspace_write(
        mission_id: str, body: WorkspaceWriteIn, who: CallerDep
    ) -> dict[str, Any]:
        """Editor save: goes through the gate like any other write (P2, verified, evented)."""
        try:
            runtime.missions.get(mission_id)
        except MissionNotFound:
            raise HTTPException(404, "mission not found") from None
        res = await runtime.executor.run(
            "workspace.write",
            {"path": body.path, "content": body.content},
            actor=f"owner:{who.device_id(body.device_id) or 'api'}",
            correlation_id=mission_id,
            user_id=body.user_id,
            device_id=who.device_id(body.device_id),
            device_trusted=who.effective_trust(body.device_trusted),
        )
        inv, ver = res.invocation, res.verification
        payload = {"path": body.path, "invocation": inv.to_dict(), "verification": ver.to_dict()}
        if inv.status is InvocationStatus.AWAITING_APPROVAL:
            return {**payload, "status": "waiting_for_approval", "decision_id": inv.decision_id}
        if inv.status is InvocationStatus.FAILED and inv.error and "traversal" in inv.error:
            raise HTTPException(400, inv.error)
        if res.ok:
            return {**payload, "status": "completed", "result": inv.result}
        return {**payload, "status": inv.status.value, "error": inv.error or ver.reason}

    @app.get("/workspace/{mission_id}/diff")
    def workspace_diff(mission_id: str, path: str) -> dict[str, Any]:
        try:
            return {"path": path, "diff": runtime.workspaces.diff(mission_id, path)}
        except WorkspaceError as exc:
            raise HTTPException(404 if "no such" in str(exc) else 400, str(exc)) from None

    @app.get("/workspace/{mission_id}/preview/{path:path}")
    def workspace_preview(mission_id: str, path: str) -> Response:
        """Serve a workspace file for the preview iframe (sandboxed CSP, no caching)."""
        try:
            target = runtime.workspaces.resolve(mission_id, path or "index.html", must_exist=True)
        except WorkspaceError as exc:
            raise HTTPException(404, str(exc)) from None
        if target.is_dir():
            target = target / "index.html"
            if not target.is_file():
                raise HTTPException(404, "no index.html")
        media = _PREVIEW_TYPES.get(target.suffix.lower(), "application/octet-stream")
        headers = {
            "Content-Security-Policy": (
                "sandbox allow-scripts; default-src 'self' 'unsafe-inline' data: blob:"
            ),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        }
        return Response(content=target.read_bytes(), media_type=media, headers=headers)

    # -- debug UI ------------------------------------------------------------------------------

    @app.get("/debug", response_class=HTMLResponse)
    def debug() -> str:
        return (_STATIC / "debug.html").read_text(encoding="utf-8")

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/hud/")

    if _HUD.is_dir():
        app.mount("/hud", StaticFiles(directory=str(_HUD), html=True), name="hud")

    return app


# -- helpers ---------------------------------------------------------------------------------------


def _row(seq: int, ev: Event) -> dict[str, Any]:
    return {"seq": seq, **ev.to_dict()}


async def _memory_action(runtime: CoreRuntime, memory_id: str, *, pinned: bool) -> dict[str, Any]:
    try:
        return (await runtime.memory_writer.pin(memory_id, pinned)).to_dict()
    except KeyError:
        raise HTTPException(404, "memory not found") from None
