"""Desktop capabilities (SPEC §10.2, §17.2) - every side effect has a risk level and a verifier.

computer.open_app      P1  requires device.trusted   verifier computer.process_running
computer.list_windows  P0
computer.focus_window  P2  reversible                verifier computer.window_focused
computer.screenshot    P0  (file stays local)
computer.type_text     P3  approval, irreversible    verifier computer.input_delivered
computer.press_key     P3  approval, irreversible    verifier computer.input_delivered
system.info            P0
system.set_volume      P1  reversible                verifier system.volume_is
system.lock_screen     P2  (owner unlocks)           verifier system.locked
"""

from __future__ import annotations

from typing import Any

from core.capabilities.gateway import Invocation
from core.capabilities.manifest import CapabilityManifest
from core.capabilities.registry import CapabilityRegistry
from core.permissions.model import RiskLevel
from core.verifier.model import Outcome
from core.verifier.service import VerifierRegistry

from adapters.desktop.backend import DesktopBackend, in_thread

TRUSTED = ("device.trusted",)

DESKTOP_MANIFESTS: tuple[CapabilityManifest, ...] = (
    CapabilityManifest(
        name="computer.open_app",
        version="1.0",
        risk=RiskLevel.P1,
        inputs={"app": "string"},
        requires=TRUSTED,
        side_effects=True,
        reversible=False,
        verifier="computer.process_running",
        timeout_ms=15_000,
        description="Open an application by name (e.g. vscode, browser, terminal).",
    ),
    CapabilityManifest(
        name="computer.list_windows",
        version="1.0",
        risk=RiskLevel.P0,
        inputs={},
        description="List the titles of open windows.",
    ),
    CapabilityManifest(
        name="computer.focus_window",
        version="1.0",
        risk=RiskLevel.P2,
        inputs={"title": "string"},
        requires=TRUSTED,
        side_effects=True,
        reversible=True,
        verifier="computer.window_focused",
        description="Bring the window whose title contains the text to the front.",
    ),
    CapabilityManifest(
        name="computer.screenshot",
        version="1.0",
        risk=RiskLevel.P0,
        inputs={"filename": "string?"},
        requires=TRUSTED,
        description="Save a screenshot locally and return its path (never leaves the device).",
    ),
    CapabilityManifest(
        name="computer.type_text",
        version="1.0",
        risk=RiskLevel.P3,
        inputs={"text": "string"},
        requires=TRUSTED,
        side_effects=True,
        reversible=False,
        verifier="computer.input_delivered",
        description="Type text into the focused window. Needs the owner's confirmation.",
    ),
    CapabilityManifest(
        name="computer.press_key",
        version="1.0",
        risk=RiskLevel.P3,
        inputs={"keys": "string"},
        requires=TRUSTED,
        side_effects=True,
        reversible=False,
        verifier="computer.input_delivered",
        description="Press a key or shortcut (e.g. 'ctrl+s'). Needs the owner's confirmation.",
    ),
    CapabilityManifest(
        name="system.info",
        version="1.0",
        risk=RiskLevel.P0,
        inputs={},
        description="CPU, memory and OS summary.",
    ),
    CapabilityManifest(
        name="system.set_volume",
        version="1.0",
        risk=RiskLevel.P1,
        inputs={"level": "integer"},
        requires=TRUSTED,
        side_effects=True,
        reversible=True,
        verifier="system.volume_is",
        description="Set the output volume (0-100).",
    ),
    CapabilityManifest(
        name="system.lock_screen",
        version="1.0",
        risk=RiskLevel.P2,
        inputs={},
        requires=TRUSTED,
        side_effects=True,
        reversible=True,
        verifier="system.locked",
        description="Lock the screen.",
    ),
)


def register_desktop(
    registry: CapabilityRegistry, verifiers: VerifierRegistry, backend: DesktopBackend
) -> CapabilityRegistry:
    async def open_app(args: dict[str, Any]) -> dict[str, Any]:
        return {"app": args["app"], "message": await in_thread(backend.open_app, args["app"])}

    async def list_windows(args: dict[str, Any]) -> dict[str, Any]:
        wins = await in_thread(backend.list_windows)
        return {"windows": wins, "count": len(wins)}

    async def focus_window(args: dict[str, Any]) -> dict[str, Any]:
        return {
            "title": args["title"],
            "message": await in_thread(backend.focus_window, args["title"]),
        }

    async def screenshot(args: dict[str, Any]) -> dict[str, Any]:
        return {"path": await in_thread(backend.screenshot, args.get("filename") or "")}

    async def type_text(args: dict[str, Any]) -> dict[str, Any]:
        return {"message": await in_thread(backend.type_text, args["text"])}

    async def press_key(args: dict[str, Any]) -> dict[str, Any]:
        return {"message": await in_thread(backend.press_key, args["keys"])}

    async def system_info(args: dict[str, Any]) -> dict[str, Any]:
        return {"info": await in_thread(backend.system_info)}

    async def set_volume(args: dict[str, Any]) -> dict[str, Any]:
        level = max(0, min(100, int(args["level"])))
        return {"level": level, "message": await in_thread(backend.set_volume, level)}

    async def lock_screen(args: dict[str, Any]) -> dict[str, Any]:
        return {"message": await in_thread(backend.lock_screen)}

    handlers = {
        "computer.open_app": open_app,
        "computer.list_windows": list_windows,
        "computer.focus_window": focus_window,
        "computer.screenshot": screenshot,
        "computer.type_text": type_text,
        "computer.press_key": press_key,
        "system.info": system_info,
        "system.set_volume": set_volume,
        "system.lock_screen": lock_screen,
    }
    for manifest in DESKTOP_MANIFESTS:
        registry.register(manifest, handlers[manifest.name])

    # -- verifiers: independent checks against the backend; None -> UNKNOWN -------------------

    def _tri(value: bool | None, evidence: dict[str, Any]) -> tuple[Outcome, dict[str, Any]]:
        if value is None:
            return Outcome.UNKNOWN, {**evidence, "reason": "backend has no independent signal"}
        return (Outcome.ACHIEVED if value else Outcome.NOT_ACHIEVED), evidence

    def process_running(inv: Invocation) -> tuple[Outcome, dict[str, Any]]:
        app = inv.args["app"]
        return _tri(backend.process_running(app), {"app": app})

    def window_focused(inv: Invocation) -> tuple[Outcome, dict[str, Any]]:
        focused = backend.focused_window()
        if focused is None:
            return _tri(None, {"title": inv.args["title"]})
        return _tri(inv.args["title"].lower() in focused.lower(), {"focused": focused})

    def input_delivered(inv: Invocation) -> tuple[Outcome, dict[str, Any]]:
        last = backend.last_input()
        if last is None:
            return _tri(None, {})
        kind, value = last
        expected = inv.args.get("text") if kind == "text" else inv.args.get("keys")
        return _tri(value == expected, {"kind": kind})

    def volume_is(inv: Invocation) -> tuple[Outcome, dict[str, Any]]:
        current = backend.get_volume()
        wanted = max(0, min(100, int(inv.args["level"])))
        return _tri(
            None if current is None else current == wanted, {"current": current, "wanted": wanted}
        )

    def locked(inv: Invocation) -> tuple[Outcome, dict[str, Any]]:
        return _tri(backend.is_locked(), {})

    verifiers.register("computer.process_running", process_running)
    verifiers.register("computer.window_focused", window_focused)
    verifiers.register("computer.input_delivered", input_delivered)
    verifiers.register("system.volume_is", volume_is)
    verifiers.register("system.locked", locked)
    return registry
