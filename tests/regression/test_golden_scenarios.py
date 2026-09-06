"""Regression: SPEC §24.1 golden scenarios for 1.0, end to end against the fake adapters.

Voice fast path · remote PC wake → verified → mobile notification · coding mission with verifier ·
world news → globe → evidence · home scene verified · risky command → strong proof → audit ·
internet down → local basics · core restart → mission recovery · memory correction visible ·
kill switch stops everything. Every scenario is judged on persisted events, never on UI state.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from core.api import create_app
from core.capabilities import InvocationStatus
from core.notify import FakePush
from core.permissions import ApprovalProof, ProofMethod
from core.runtime import CoreRuntime
from core.verifier import Outcome
from fastapi.testclient import TestClient

CONFIRM = ApprovalProof(ProofMethod.UI_CONFIRM, device_id="desk", device_trusted=True)
PASSKEY = ApprovalProof(ProofMethod.PASSKEY, device_id="phone", device_trusted=True, reference="r")
VOICE = ApprovalProof(ProofMethod.VOICE, device_id="desk", device_trusted=True)
PROOF_STRONG = {"method": "passkey", "device_id": "phone", "device_trusted": True, "reference": "x"}


def run(coro):
    return asyncio.run(coro)


def kw(mission="g1"):
    return dict(actor="owner", correlation_id=mission, device_trusted=True, device_id="desk")


@pytest.fixture
def push():
    return FakePush()


@pytest.fixture
def url(tmp_path):
    return f"sqlite:///{tmp_path / 'golden.db'}"


@pytest.fixture
def rt(url, tmp_path, push):
    return CoreRuntime.build(
        url,
        provider="none",
        home="fake",
        news="fake",
        push=push,
        workspace_root=str(tmp_path / "ws"),
        skills_root=str(tmp_path / "skills"),
    )


@pytest.fixture
def client(rt):
    return TestClient(create_app(rt))


def cmd(client, text, **extra):
    r = client.post(
        "/commands", json={"text": text, "device_id": "desk", "device_trusted": True, **extra}
    )
    assert r.status_code == 200, r.text
    return r.json()


def types(client, mission):
    return [e["type"] for e in client.get("/events", params={"correlation_id": mission}).json()]


# 1 ------------------------------------------------------------ voice fast path
def test_voice_fast_path_completes_without_a_model(client):
    out = cmd(client, "what time is it")
    assert out["route"] == "capability" and out["status"] == "completed"
    t = types(client, out["mission_id"])
    assert "permission.allowed" in t and "capability.succeeded" in t
    assert t[-1] == "mission.completed"
    assert "agent.run.started" not in t  # no model involved


# 2 ------------------------------------------------- remote wake -> verified -> notification
def test_remote_pc_wake_is_verified_and_notifies_the_owner(rt, client, push):
    out = cmd(client, "wake desktop")
    assert out["status"] == "waiting_for_approval"
    assert push.sent[-1].title == "Approval needed" and "power.wake" in push.sent[-1].body
    r = client.post(
        f"/approvals/{out['decision_id']}/approve",
        json={"method": "ui_confirm", "device_id": "desk", "device_trusted": True},
    )
    assert r.status_code == 200 and r.json()["status"] == "completed"
    t = types(client, out["mission_id"])
    assert "permission.approved" in t and "verification.passed" in t
    assert "power.wake.sent" in t  # a magic packet left through the gate, not around it


# 3 ---------------------------------------------------------- coding mission with verifier
def test_coding_mission_is_only_done_after_a_verified_green_run(rt):
    m = "code-1"
    w = run(
        rt.executor.run(
            "workspace.write",
            {"path": "test_ok.py", "content": "def test_ok():\n    assert 1 + 1 == 2\n"},
            **kw(m),
        )
    )
    assert w.ok and w.verification.outcome is Outcome.ACHIEVED
    waiting = run(
        rt.executor.run(
            "workspace.run", {"command": "pytest", "args": ["-q", "test_ok.py"]}, **kw(m)
        )
    )
    assert waiting.invocation.status is InvocationStatus.AWAITING_APPROVAL
    run(rt.permissions.approve(waiting.invocation.decision_id, CONFIRM))
    done = run(
        rt.executor.run(
            "workspace.run",
            {"command": "pytest", "args": ["-q", "test_ok.py"]},
            decision_id=waiting.invocation.decision_id,
            **kw(m),
        )
    )
    assert done.ok and done.invocation.result["exit_code"] == 0
    assert done.verification.outcome is Outcome.ACHIEVED
    ev = [e.type for _, e in rt.bus.replay(correlation_id=m)]
    assert "workspace.file.changed" in ev and "workspace.run.finished" in ev


# 4 ------------------------------------------------------------ news -> globe -> evidence
def test_world_news_reaches_globe_with_evidence(client):
    r = client.post("/news/refresh").json()
    assert r["created"] > 0 and r["errors"] == {}
    events = client.get("/news").json()["events"]
    assert events
    top = max(events, key=lambda e: e["confidence"])
    assert top["source_count"] >= 2 and top["lat"] is not None and top["lon"] is not None
    detail = client.get(f"/news/{top['event_id']}").json()
    assert detail["sources"] and all(s["url"] for s in detail["sources"])  # evidence, not opinion
    countries = client.get("/news/countries").json()["countries"]
    assert any(c["count"] > 0 for c in countries)


# 5 ------------------------------------------------------------ home scene verified
def test_home_scene_is_verified_by_read_back(rt, client):
    out = cmd(client, "szene movie")
    assert out["status"] == "completed"
    assert "verification.passed" in types(client, out["mission_id"])
    rt.home.backend.ignoring.add("light.kitchen")
    failed = cmd(client, "turn on the kitchen light")
    assert failed["status"] == "failed"
    assert "verification.failed" in types(client, failed["mission_id"])


# 6 ------------------------------------------------ risky command -> strong proof -> audit
def test_risky_command_needs_strong_proof_and_leaves_an_audit_trail(rt):
    m = "risk-1"
    unlock = run(
        rt.executor.run("home.lock.set", {"target": "front door", "locked": False}, **kw(m))
    )
    assert unlock.invocation.status is InvocationStatus.AWAITING_APPROVAL
    did = unlock.invocation.decision_id
    for weak in (VOICE, CONFIRM):
        with pytest.raises(Exception, match=r"strong|proof|insufficient"):
            run(rt.permissions.approve(did, weak))
    assert rt.home.backend.entities["lock.front_door"].state == "locked"
    run(rt.permissions.approve(did, PASSKEY))
    done = run(
        rt.executor.run(
            "home.lock.set", {"target": "front door", "locked": False}, decision_id=did, **kw(m)
        )
    )
    assert done.ok and rt.home.backend.entities["lock.front_door"].state == "unlocked"
    audit = [e for _, e in rt.bus.replay(correlation_id=m) if e.type.startswith("permission.")]
    assert [e.type for e in audit][:1] == ["permission.ask"]
    approved = next(e for e in audit if e.type == "permission.approved")
    assert "passkey" in json.dumps(approved.payload)
    assert "permission.denied" not in [e.type for e in audit]  # weak proofs: rejected, no deny
    # undo = the reverse command through the very same gate, again with strong proof
    relock = run(
        rt.executor.run("home.lock.set", {"target": "front door", "locked": True}, **kw(m))
    )
    assert relock.invocation.status is InvocationStatus.AWAITING_APPROVAL


# 7 ------------------------------------------------------------ internet down -> local basics
def test_local_basics_work_without_any_network(tmp_path):
    rt = CoreRuntime.build(
        f"sqlite:///{tmp_path / 'off.db'}", provider="none", home="fake", news="off"
    )
    client = TestClient(create_app(rt))
    assert cmd(client, "echo offline")["status"] == "completed"
    assert cmd(client, "turn on the kitchen light")["status"] == "completed"
    assert client.get("/news").json()["enabled"] is False
    h = client.get("/health").json()
    assert h["status"] == "ok" and h["agent_ready"] is False


# 8 ------------------------------------------------------------ restart -> mission recovery
def test_core_restart_recovers_missions_and_pending_approvals(url, rt, client):
    done = cmd(client, "echo before restart")
    waiting = cmd(client, "wake desktop")
    rt2 = CoreRuntime.build(url, provider="none", home="fake")
    stats = rt2.recover()
    assert stats["missions"] >= 2 and stats["permissions"] >= 1
    c2 = TestClient(create_app(rt2))
    assert c2.get(f"/missions/{done['mission_id']}").json()["status"] == "completed"
    assert c2.get(f"/missions/{waiting['mission_id']}").json()["status"] == "waiting_for_approval"
    assert [d["decision_id"] for d in c2.get("/approvals").json()] == [waiting["decision_id"]]


# 9 ------------------------------------------------------------ memory correction visible
def test_memory_correction_is_visible_and_never_leaks_values_into_events(rt, client):
    item = run(
        rt.memory_writer.remember("preference", "owner", "coffee", "black", correlation_id="mem-1")
    ).item
    r = client.post(f"/memory/{item.memory_id}/correct", json={"value": "with milk"})
    assert r.status_code == 200 and r.json()["memory"]["value"] == "with milk"
    known = client.get("/memory", params={"q": "coffee"}).json()
    assert known and known[0]["value"] == "with milk"
    assert known[0]["supersedes"] and known[0]["memory_id"] != item.memory_id
    ev = [e for _, e in rt.bus.replay(type_prefix="memory")]
    assert {e.type for e in ev} >= {"memory.written", "memory.corrected"}
    dump = json.dumps([e.to_dict() for e in ev], default=str)
    assert "with milk" not in dump and '"black"' not in dump


# 10 ----------------------------------------------------------- kill switch stops everything
def test_kill_switch_stops_everything_and_needs_strong_proof_to_resume(rt, client, push):
    assert cmd(client, "Jarvis, stop") == {"route": "stop", "halted": True}
    assert client.get("/health").json()["halted"] is True
    assert push.sent[-1].priority == "urgent"
    assert cmd(client, "turn on the kitchen light")["status"] == "halted"
    assert cmd(client, "echo nothing")["status"] == "halted"
    direct = run(rt.executor.run("home.light.set", {"target": "kitchen", "on": True}, **kw("k1")))
    assert direct.invocation.status is InvocationStatus.HALTED
    assert (
        client.post("/resume", json={"method": "ui_confirm", "device_trusted": True}).status_code
        == 403
    )
    assert client.post("/resume", json=PROOF_STRONG).status_code == 200
    assert cmd(client, "echo back")["status"] == "completed"
