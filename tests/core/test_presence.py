"""Tests for core.presence (Phase 6 step 37): derived per-device presence, halt overlay, rebuild."""

from __future__ import annotations

import asyncio

from core.api import create_app
from core.models import MockProvider, ModelRouter, ModelSpec, ProviderResult, Tier, ToolCallProposal
from core.permissions import ApprovalProof, Decision, Policy, ProofMethod, RiskLevel
from core.presence import PresenceState
from core.runtime import CoreRuntime
from fastapi.testclient import TestClient
from voice import VoiceBridge
from voice.fakes import FakeSTT, FakeTTS, FakeWake


def run(coro):
    return asyncio.run(coro)


def states(rt, device):
    return [
        e.payload["state"]
        for _, e in rt.bus.replay(type_prefix="presence")
        if e.payload["device_id"] == device
    ]


def test_voice_turn_drives_presence_and_halt_overlays_everything(tmp_path):
    rt = CoreRuntime.build(f"sqlite:///{tmp_path / 'p.db'}", provider="none")
    bridge = VoiceBridge(
        rt,
        wake=FakeWake(),
        stt=FakeSTT([["echo hi"]]),
        tts=FakeTTS(),
        device_id="desk",
        follow_up=False,
    )
    run(bridge.handle_wake())
    assert states(rt, "desk") == ["listening", "thinking", "speaking", "idle"]
    assert (
        rt.presence.get("desk").state is PresenceState.IDLE
        and rt.presence.get("nope").state is PresenceState.IDLE
    )

    run(rt.gateway.halt("test"))
    assert rt.presence.halted and rt.presence.snapshot()["halted"] is True
    last = rt.bus.replay(type_prefix="presence")[-1][1]
    assert last.payload["state"] == "halted" and last.priority.value == "urgent"
    run(rt.gateway.resume(ApprovalProof(ProofMethod.PASSKEY, device_trusted=True)))
    assert not rt.presence.halted
    assert rt.bus.replay(type_prefix="presence")[-1][1].payload["state"] == "idle"


def test_agent_run_and_approval_presence(tmp_path):
    script = [
        ProviderResult(
            "",
            tool_calls=(ToolCallProposal("mock.open_url", {"url": "https://a.org"}, call_id="c"),),
            stop_reason="tool_use",
        ),
        ProviderResult("Opened."),
    ]
    rt = CoreRuntime.build(
        f"sqlite:///{tmp_path / 'a.db'}",
        policy=Policy(overrides={RiskLevel.P1: Decision.ASK}),
        providers={"mock": MockProvider(script)},
        router=ModelRouter([ModelSpec("mock-model", "mock", Tier.FRONTIER, supports_effort=False)]),
    )
    client = TestClient(create_app(rt))
    out = client.post(
        "/commands",
        json={"text": "open a.org please", "device_id": "phone", "device_trusted": True},
    ).json()
    assert out["status"] == "waiting_for_approval"
    p = client.get("/presence").json()["devices"]["phone"]
    assert (
        p["state"] == "awaiting_approval"
        and p["active_mission"] == out["mission_id"]
        and p["pending_approvals"]
    )
    client.post(
        f"/approvals/{out['decision_id']}/approve",
        json={"method": "ui_confirm", "device_id": "phone", "device_trusted": True},
    )
    assert states(rt, "phone") == ["working", "awaiting_approval", "working", "idle"]
    assert client.get("/health").json()["presence"]["devices"]["phone"]["state"] == "idle"


def test_presence_rebuild_matches_live_state(tmp_path):
    url = f"sqlite:///{tmp_path / 'r.db'}"
    rt = CoreRuntime.build(url, provider="none")
    bridge = VoiceBridge(
        rt,
        wake=FakeWake(),
        stt=FakeSTT([["echo one. two. three."]]),
        tts=FakeTTS(0.5),
        device_id="desk",
        follow_up=False,
    )

    async def scenario():
        task = asyncio.create_task(bridge.handle_wake())
        await asyncio.sleep(0.1)
        assert rt.presence.get("desk").state is PresenceState.SPEAKING
        await bridge.barge_in(reason="test")
        await task

    run(scenario())
    live = rt.presence.snapshot()
    rt2 = CoreRuntime.build(url, provider="none")
    stats = rt2.recover()
    assert stats["presence_devices"] == 1
    rebuilt = rt2.presence.snapshot()
    assert rebuilt["devices"]["desk"]["state"] == live["devices"]["desk"]["state"] == "idle"
    assert rebuilt["halted"] is False
    # rebuild does not publish new presence events
    assert len([1 for _, e in rt2.bus.replay(type_prefix="presence")]) == len(
        [1 for _, e in rt.bus.replay(type_prefix="presence")]
    )
