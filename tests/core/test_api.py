"""Tests for core.api + core.runtime (Commit 009): Phase 1 DoD via the local API/WebSocket."""

from __future__ import annotations

import json

import pytest
from core.api import create_app
from core.permissions import Decision, Policy, RiskLevel
from core.runtime import CoreRuntime
from fastapi.testclient import TestClient

PROOF_STRONG = {"method": "passkey", "device_id": "phone", "device_trusted": True, "reference": "x"}
PROOF_CONFIRM = {"method": "ui_confirm", "device_id": "desk", "device_trusted": True}


@pytest.fixture
def runtime(tmp_path) -> CoreRuntime:
    return CoreRuntime.build(f"sqlite:///{tmp_path / 'core.db'}")


@pytest.fixture
def client(runtime) -> TestClient:
    return TestClient(create_app(runtime))


def cmd(client: TestClient, text: str, **kw):
    r = client.post(
        "/commands", json={"text": text, "device_id": "desk", "device_trusted": True, **kw}
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_health_and_redaction(runtime, client):
    h = client.get("/health").json()
    assert h["status"] == "ok" and h["version"] == runtime.version and h["halted"] is False
    assert {"mock.clock", "mock.echo", "mock.open_url", "memory.recall"} <= set(h["capabilities"])
    from core.runtime import _redact

    assert (
        _redact("postgresql://jarvis:pw@127.0.0.1:5432/jarvis")
        == "postgresql://***@127.0.0.1:5432/jarvis"
    )
    assert _redact("sqlite:///x.db") == "sqlite:///x.db"


def test_text_command_reaches_core_and_completes_a_verified_mission(client):
    out = cmd(client, "echo hello core")
    assert out["route"] == "capability" and out["status"] == "completed"
    assert out["result"] == {"text": "hello core"}
    m = client.get(f"/missions/{out['mission_id']}").json()
    assert m["status"] == "completed" and m["goal"] == "echo hello core"
    types = [
        e["type"]
        for e in client.get("/events", params={"correlation_id": out["mission_id"]}).json()
    ]
    assert types == [
        "mission.created",
        "command.received",
        "mission.planning",
        "mission.running",
        "permission.allowed",
        "permission.consumed",
        "capability.invoked",
        "capability.succeeded",
        "verification.skipped",
        "mission.verifying",
        "mission.completed",
    ]
    assert client.get("/missions/nope").status_code == 404


def test_unrouted_command_is_blocked_honestly_not_faked(client):
    out = cmd(client, "write me a space game")
    assert out["route"] == "agent" and out["status"] == "blocked"
    m = client.get(f"/missions/{out['mission_id']}").json()
    assert m["status"] == "blocked"
    assert (
        client.get("/missions", params={"status": "blocked"}).json()[0]["mission_id"]
        == out["mission_id"]
    )


def test_untrusted_device_cannot_open_urls(client):
    out = cmd(client, "open https://example.org", device_trusted=False)
    assert out["status"] == "failed" and out["invocation"]["status"] == "denied"
    assert out["invocation"]["rule"] == "requires:device.trusted"
    assert client.get(f"/missions/{out['mission_id']}").json()["status"] == "failed"


def test_approval_workflow_end_to_end(tmp_path):
    runtime = CoreRuntime.build(
        f"sqlite:///{tmp_path / 'c.db'}", policy=Policy(overrides={RiskLevel.P1: Decision.ASK})
    )
    client = TestClient(create_app(runtime))
    out = cmd(client, "open https://example.org")
    assert out["status"] == "waiting_for_approval" and out["decision_id"]
    mid, did = out["mission_id"], out["decision_id"]
    assert client.get(f"/missions/{mid}").json()["status"] == "waiting_for_approval"
    assert [d["decision_id"] for d in client.get("/approvals").json()] == [did]

    # Voice is never enough.
    r = client.post(f"/approvals/{did}/approve", json={"method": "voice", "device_trusted": True})
    assert r.status_code == 409
    r = client.post(f"/approvals/{did}/approve", json=PROOF_CONFIRM)
    assert r.status_code == 200
    body = r.json()
    assert body["resumed"] is True and body["status"] == "completed"
    assert body["result"] == {"opened": "https://example.org", "mock": True}
    assert body["verification"]["outcome"] == "achieved"
    assert client.get(f"/missions/{mid}").json()["status"] == "completed"
    assert client.get("/approvals").json() == []
    # Second approval of the same decision is refused.
    assert client.post(f"/approvals/{did}/approve", json=PROOF_CONFIRM).status_code == 409

    # Deny path cancels the mission.
    out2 = cmd(client, "open https://b.org")
    r = client.post(f"/approvals/{out2['decision_id']}/deny", json={"reason": "no"})
    assert r.status_code == 200 and r.json()["decision"]["decision"] == "deny"
    assert client.get(f"/missions/{out2['mission_id']}").json()["status"] == "canceled"
    assert client.post("/approvals/ghost/deny", json={}).status_code == 409


def test_kill_switch_via_api_and_stop_command(client):
    assert cmd(client, "Jarvis, stop everything") == {"route": "stop", "halted": True}
    assert client.get("/health").json()["halted"] is True
    out = cmd(client, "echo x")
    assert out["status"] == "halted" and out["invocation"]["status"] == "halted"
    assert client.get(f"/missions/{out['mission_id']}").json()["status"] == "paused"
    assert client.post("/resume", json=PROOF_CONFIRM).status_code == 403
    assert client.post("/resume", json=PROOF_STRONG).status_code == 200
    assert client.get("/health").json()["halted"] is False
    assert client.post("/kill").json() == {"halted": True}
    assert client.get("/health").json()["status"] == "halted"


def test_websocket_replays_then_streams_live_without_gaps(client):
    first = cmd(client, "echo one")
    seqs_before = [e["seq"] for e in client.get("/events").json()]
    with client.websocket_connect("/ws/events?after_seq=0") as ws:
        replayed = [json.loads(ws.receive_text()) for _ in seqs_before]
        assert [e["seq"] for e in replayed] == seqs_before
        assert replayed[-1]["correlation_id"] == first["mission_id"]
        second = cmd(client, "clock")
        live = []
        while not live or live[-1]["type"] != "mission.completed":
            live.append(json.loads(ws.receive_text()))
        assert live[0]["seq"] == seqs_before[-1] + 1  # no gap, no duplicate
        assert all(e["correlation_id"] == second["mission_id"] for e in live)
        assert [e["seq"] for e in live] == list(range(live[0]["seq"], live[0]["seq"] + len(live)))
    # partial replay
    with client.websocket_connect(f"/ws/events?after_seq={seqs_before[-1]}") as ws:
        assert json.loads(ws.receive_text())["seq"] == seqs_before[-1] + 1


def test_debug_page_and_validation(client):
    r = client.get("/debug")
    assert r.status_code == 200 and "J.A.R.V.I.S. CORE" in r.text and "/ws/events" in r.text
    assert client.post("/commands", json={"text": ""}).status_code == 422
    assert client.get("/events", params={"limit": 0}).status_code == 422


def test_runtime_recovers_after_restart(tmp_path):
    url = f"sqlite:///{tmp_path / 'r.db'}"
    rt = CoreRuntime.build(url, policy=Policy(overrides={RiskLevel.P1: Decision.ASK}))
    client = TestClient(create_app(rt))
    waiting = cmd(client, "open https://x.org")
    done = cmd(client, "echo done")

    rt2 = CoreRuntime.build(url, policy=Policy(overrides={RiskLevel.P1: Decision.ASK}))
    stats = rt2.recover()
    assert stats["missions"] == 2 and stats["permissions"] >= 2
    client2 = TestClient(create_app(rt2))
    assert client2.get(f"/missions/{done['mission_id']}").json()["status"] == "completed"
    assert (
        client2.get(f"/missions/{waiting['mission_id']}").json()["status"] == "waiting_for_approval"
    )
    assert [d["decision_id"] for d in client2.get("/approvals").json()] == [waiting["decision_id"]]
    # The pending approval survives, but the in-memory command binding does not (documented):
    r = client2.post(f"/approvals/{waiting['decision_id']}/approve", json=PROOF_CONFIRM)
    assert r.status_code == 200 and r.json()["resumed"] is False
