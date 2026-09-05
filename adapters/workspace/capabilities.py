"""Workspace capabilities (SPEC §12, Phase 7 steps 44-46) - the coding agent's only file access.

    workspace.list   P0
    workspace.read   P0
    workspace.write  P2  reversible (version copy)   verifier workspace.file_matches
    workspace.diff   P0
    workspace.run    P3  approval; allowlisted command, timeout, CPU limit, scrubbed env
                                                     verifier workspace.run_exit_code
The workspace is always the current mission's (correlation id) - a call can never pick another.
Writes emit ``workspace.file.changed`` (with the diff) and runs stream ``workspace.run.output``
so the HUD's editor/terminal panels render real events.
"""

from __future__ import annotations

from typing import Any

from core.capabilities.gateway import Invocation, current_actor, current_correlation_id
from core.capabilities.manifest import CapabilityInputError, CapabilityManifest
from core.capabilities.registry import CapabilityRegistry
from core.events.bus import EventBus
from core.events.envelope import Event
from core.permissions.model import RiskLevel
from core.verifier.model import Outcome
from core.verifier.service import VerifierRegistry

from adapters.workspace.manager import WorkspaceError, WorkspaceManager

SOURCE = "workspace"

WORKSPACE_MANIFESTS: tuple[CapabilityManifest, ...] = (
    CapabilityManifest(
        name="workspace.list",
        version="1.0",
        risk=RiskLevel.P0,
        inputs={"path": "string?"},
        description="List files in the mission workspace (relative paths).",
    ),
    CapabilityManifest(
        name="workspace.read",
        version="1.0",
        risk=RiskLevel.P0,
        inputs={"path": "string"},
        description="Read a text file from the mission workspace.",
    ),
    CapabilityManifest(
        name="workspace.write",
        version="1.0",
        risk=RiskLevel.P2,
        inputs={"path": "string", "content": "string"},
        side_effects=True,
        reversible=True,
        verifier="workspace.file_matches",
        description="Create or overwrite a text file in the workspace (previous version kept).",
    ),
    CapabilityManifest(
        name="workspace.diff",
        version="1.0",
        risk=RiskLevel.P0,
        inputs={"path": "string"},
        description="Unified diff between the last saved version and the current file.",
    ),
    CapabilityManifest(
        name="workspace.run",
        version="1.0",
        risk=RiskLevel.P3,
        inputs={"command": "string", "args": "array?", "timeout_s": "integer?"},
        side_effects=True,
        reversible=False,
        verifier="workspace.run_exit_code",
        timeout_ms=150_000,
        description=(
            "Run an allowlisted command (python, pytest, node, npm, git, ...) inside the mission "
            "workspace with a timeout. Needs the owner's confirmation."
        ),
    ),
)


def _workspace_id() -> str:
    cid = current_correlation_id.get()
    if not cid:
        raise CapabilityInputError("workspace calls need a mission (no correlation id)")
    return cid


def register_workspace(
    registry: CapabilityRegistry,
    verifiers: VerifierRegistry,
    bus: EventBus,
    manager: WorkspaceManager,
) -> CapabilityRegistry:
    async def emit(event_type: str, payload: dict[str, Any], wsid: str) -> None:
        await bus.publish(Event.new(event_type, SOURCE, payload, correlation_id=wsid))

    def guard(fn):
        async def wrapped(args: dict[str, Any]) -> dict[str, Any]:
            try:
                return await fn(args)
            except WorkspaceError as exc:
                raise CapabilityInputError(str(exc)) from exc

        return wrapped

    @guard
    async def list_files(args: dict[str, Any]) -> dict[str, Any]:
        files = manager.list(_workspace_id(), args.get("path") or ".")
        return {"files": files, "count": len(files)}

    @guard
    async def read(args: dict[str, Any]) -> dict[str, Any]:
        content = manager.read(_workspace_id(), args["path"])
        return {"path": args["path"], "content": content, "sha256": manager.sha256(content)}

    @guard
    async def write(args: dict[str, Any]) -> dict[str, Any]:
        wsid = _workspace_id()
        result = manager.write(wsid, args["path"], args["content"])
        await emit(
            "workspace.file.changed",
            {
                **{k: v for k, v in result.items() if k != "diff"},
                "diff": result["diff"][:20_000],
                "actor": current_actor.get(),
            },
            wsid,
        )
        return {k: v for k, v in result.items() if k != "diff"} | {
            "diff_lines": result["diff"].count("\n")
        }

    @guard
    async def diff(args: dict[str, Any]) -> dict[str, Any]:
        d = manager.diff(_workspace_id(), args["path"])
        return {"path": args["path"], "diff": d[:50_000], "changed": bool(d)}

    @guard
    async def run(args: dict[str, Any]) -> dict[str, Any]:
        wsid = _workspace_id()
        raw_args = args.get("args") or []
        if not isinstance(raw_args, list) or not all(
            isinstance(a, str | int | float) for a in raw_args
        ):
            raise CapabilityInputError("args must be a list of strings")
        seq = {"n": 0}

        async def on_output(stream: str, text: str) -> None:
            seq["n"] += 1
            await emit(
                "workspace.run.output",
                {"stream": stream, "chunk": text, "n": seq["n"], "command": args["command"]},
                wsid,
            )

        await emit(
            "workspace.run.started",
            {"command": args["command"], "args": [str(a) for a in raw_args]},
            wsid,
        )
        result = await manager.run(
            wsid,
            args["command"],
            [str(a) for a in raw_args],
            timeout_s=args.get("timeout_s"),
            on_output=on_output,
        )
        await emit("workspace.run.finished", result.to_dict() | {"chunks": seq["n"]}, wsid)
        return result.to_dict()

    handlers = {
        "workspace.list": list_files,
        "workspace.read": read,
        "workspace.write": write,
        "workspace.diff": diff,
        "workspace.run": run,
    }
    for m in WORKSPACE_MANIFESTS:
        registry.register(m, handlers[m.name])

    def file_matches(inv: Invocation) -> tuple[Outcome, dict[str, Any]]:
        actual = manager.file_sha256(inv.correlation_id, inv.args["path"])
        expected = manager.sha256(inv.args["content"])
        return (Outcome.ACHIEVED if actual == expected else Outcome.NOT_ACHIEVED), {
            "expected": expected[:12],
            "actual": (actual or "")[:12],
        }

    def run_exit_code(inv: Invocation) -> tuple[Outcome, dict[str, Any]]:
        r = inv.result if isinstance(inv.result, dict) else {}
        code = r.get("exit_code")
        if r.get("timed_out"):
            return Outcome.NOT_ACHIEVED, {"timed_out": True}
        return (Outcome.ACHIEVED if code == 0 else Outcome.NOT_ACHIEVED), {"exit_code": code}

    verifiers.register("workspace.file_matches", file_matches)
    verifiers.register("workspace.run_exit_code", run_exit_code)
    return registry
