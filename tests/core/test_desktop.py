"""Tests for adapters/desktop (Phase 6): capabilities behind the gate, verifiers, fast path."""

from __future__ import annotations

import asyncio

import pytest
from adapters.desktop import DESKTOP_MANIFESTS, FakeDesktop, PrototypeDesktop
from core.capabilities import InvocationStatus
from core.permissions import ApprovalProof, ProofMethod
from core.runtime import CoreRuntime
from core.verifier import Outcome


def run(coro):
    return asyncio.run(coro)


CONFIRM = ApprovalProof(ProofMethod.UI_CONFIRM, device_id="desk", device_trusted=True)
KW = dict(actor="agent", correlation_id="m1", device_trusted=True, device_id="desk")


@pytest.fixture
def rt(tmp_path):
    return CoreRuntime.build(
        f"sqlite:///{tmp_path / 'd.db'}", provider="none", desktop=FakeDesktop()
    )


def backend(rt) -> FakeDesktop:
    return rt.capabilities.get("computer.open_app").handler.__closure__[0].cell_contents  # type: ignore[index]


def test_manifests_follow_the_security_rules():
    names = {m.name for m in DESKTOP_MANIFESTS}
    assert {
        "computer.open_app",
        "computer.type_text",
        "system.lock_screen",
        "system.set_volume",
    } <= names
    for m in DESKTOP_MANIFESTS:
        if m.side_effects:
            assert m.verifier and m.risk.value >= 1  # Law 4: side effects -> verifier, never P0
        if m.name.startswith("computer.") and m.name != "computer.list_windows":
            assert "device.trusted" in m.requires
    by = {m.name: m for m in DESKTOP_MANIFESTS}
    assert by["computer.type_text"].risk.name == "P3" and not by["computer.type_text"].reversible
    assert by["system.lock_screen"].reversible and by["computer.open_app"].risk.name == "P1"


def test_open_app_is_verified_and_a_lying_launcher_is_caught(tmp_path):
    fake = FakeDesktop(failing={"browser"})
    rt = CoreRuntime.build(f"sqlite:///{tmp_path / 'o.db'}", provider="none", desktop=fake)
    ok = run(rt.executor.run("computer.open_app", {"app": "vscode"}, **KW))
    assert ok.ok and ok.verification.outcome is Outcome.ACHIEVED and "vscode" in fake.running
    lie = run(rt.executor.run("computer.open_app", {"app": "browser"}, **KW))
    assert lie.invocation.status is InvocationStatus.SUCCEEDED  # the tool said "Tried to launch"
    assert lie.verification.outcome is Outcome.NOT_ACHIEVED and not lie.ok  # ...but nothing runs
    unknown = run(rt.executor.run("computer.open_app", {"app": "photoshop"}, **KW))
    assert (
        unknown.invocation.status is InvocationStatus.FAILED
        and "unknown app" in unknown.invocation.error
    )
    untrusted = run(
        rt.executor.run("computer.open_app", {"app": "vscode"}, actor="a", correlation_id="m2")
    )
    assert (
        untrusted.invocation.status is InvocationStatus.DENIED
        and untrusted.invocation.rule == "requires:device.trusted"
    )


def test_windows_focus_screenshot_volume_lock(rt):
    run(rt.executor.run("computer.open_app", {"app": "terminal"}, **KW))
    wins = run(rt.executor.run("computer.list_windows", {}, **KW))
    assert wins.ok and "Terminal - main" in wins.invocation.result["windows"]
    focus = run(rt.executor.run("computer.focus_window", {"title": "desktop"}, **KW))
    assert focus.ok and focus.verification.outcome is Outcome.ACHIEVED
    miss = run(rt.executor.run("computer.focus_window", {"title": "nothing"}, **KW))
    assert miss.invocation.ok and miss.verification.outcome is Outcome.NOT_ACHIEVED
    shot = run(rt.executor.run("computer.screenshot", {}, **KW))
    assert shot.ok and shot.invocation.result["path"].endswith(".png")
    vol = run(rt.executor.run("system.set_volume", {"level": 250}, **KW))
    assert (
        vol.ok
        and vol.invocation.result["level"] == 100
        and vol.verification.evidence["current"] == 100
    )
    info = run(rt.executor.run("system.info", {}, **KW))
    assert info.ok and "FakeDesktop" in info.invocation.result["info"]
    lock = run(rt.executor.run("system.lock_screen", {}, **KW))
    assert lock.ok and lock.verification.outcome is Outcome.ACHIEVED


def test_keyboard_input_needs_approval_and_is_verified(rt):
    waiting = run(rt.executor.run("computer.type_text", {"text": "hello"}, **KW))
    assert waiting.invocation.status is InvocationStatus.AWAITING_APPROVAL
    run(rt.permissions.approve(waiting.invocation.decision_id, CONFIRM))
    done = run(
        rt.executor.run(
            "computer.type_text",
            {"text": "hello"},
            decision_id=waiting.invocation.decision_id,
            **KW,
        )
    )
    assert done.ok and done.verification.outcome is Outcome.ACHIEVED
    w2 = run(rt.executor.run("computer.press_key", {"keys": "ctrl+s"}, **KW))
    run(rt.permissions.approve(w2.invocation.decision_id, CONFIRM))
    key = run(
        rt.executor.run(
            "computer.press_key", {"keys": "ctrl+s"}, decision_id=w2.invocation.decision_id, **KW
        )
    )
    assert key.ok and key.verification.evidence == {"kind": "key"}
    # locked screen: the tool reports it, the verifier does not see the input -> not achieved
    run(rt.executor.run("system.lock_screen", {}, **KW))
    w3 = run(rt.executor.run("computer.type_text", {"text": "secret?"}, **KW))
    run(rt.permissions.approve(w3.invocation.decision_id, CONFIRM))
    blocked = run(
        rt.executor.run(
            "computer.type_text", {"text": "secret?"}, decision_id=w3.invocation.decision_id, **KW
        )
    )
    assert blocked.invocation.ok and blocked.verification.outcome is Outcome.NOT_ACHIEVED


def test_fast_path_routes_desktop_commands_without_a_provider(rt):
    from core.api import create_app
    from fastapi.testclient import TestClient

    client = TestClient(create_app(rt))

    def cmd(text):
        return client.post(
            "/commands", json={"text": text, "device_id": "desk", "device_trusted": True}
        ).json()

    assert cmd("open vscode")["status"] == "completed"
    assert cmd("Lautstärke auf 30")["result"]["level"] == 30
    assert cmd("show open windows")["result"]["count"] >= 2
    assert cmd("lock the screen")["status"] == "completed"
    typed = cmd("type hello")  # no rule -> agent path -> blocked (no provider), never a keystroke
    assert typed["status"] == "blocked"
    assert rt.intents.route("open https://x.org").capability == "mock.open_url"  # URL rule wins


def test_desktop_selection_and_unknown_signal_stays_unknown(tmp_path):
    off = CoreRuntime.build(f"sqlite:///{tmp_path / 'off.db'}", provider="none")
    assert "computer.open_app" not in off.capabilities
    fake = CoreRuntime.build(f"sqlite:///{tmp_path / 'f.db'}", provider="none", desktop="fake")
    assert "computer.open_app" in fake.capabilities
    with pytest.raises(ValueError):
        CoreRuntime.build(f"sqlite:///{tmp_path / 'x.db'}", provider="none", desktop="hologram")

    class NoSignal(FakeDesktop):
        def process_running(self, name):  # a backend without an independent signal
            return None

    ns = CoreRuntime.build(f"sqlite:///{tmp_path / 'n.db'}", provider="none", desktop=NoSignal())
    res = run(ns.executor.run("computer.open_app", {"app": "vscode"}, **KW))
    assert res.invocation.ok and res.verification.outcome is Outcome.UNKNOWN and not res.ok

    proto = PrototypeDesktop()  # importable headless; real calls are not made in CI
    assert (
        proto.focused_window() is None and proto.last_input() is None and proto.is_locked() is None
    )
