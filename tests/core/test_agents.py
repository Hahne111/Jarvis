"""Tests for core.agents (Phase 3): provider -> allowlist -> gateway loop, budgets, approvals."""

from __future__ import annotations

import asyncio
import json

import pytest
from core.agents import RunOutcome
from core.api import create_app
from core.capabilities import mocks
from core.events import Sensitivity
from core.models import (
    AgentBudget,
    Message,
    MockProvider,
    ModelRouter,
    ModelSpec,
    ProviderResult,
    Tier,
    ToolCallProposal,
    Usage,
)
from core.permissions import Decision, Policy, RiskLevel
from core.runtime import CoreRuntime
from fastapi.testclient import TestClient

PROOF_CONFIRM = {"method": "ui_confirm", "device_id": "desk", "device_trusted": True}


def MOCK_ROUTER() -> ModelRouter:
    return ModelRouter([ModelSpec("mock-model", "mock", Tier.FRONTIER, supports_effort=False)])


def run(coro):
    return asyncio.run(coro)


def build(tmp_path, script, *, policy=None, name="c.db"):
    provider = MockProvider(script)
    rt = CoreRuntime.build(
        f"sqlite:///{tmp_path / name}",
        policy=policy,
        providers={"mock": provider},
        router=MOCK_ROUTER(),
    )
    return rt, provider


def call(name, args, call_id):
    return ToolCallProposal(name, args, call_id=call_id)


def types(rt, mid):
    return [e.type for _, e in rt.bus.replay(correlation_id=mid)]


KW = dict(
    allowlist={"mock.echo", "mock.clock", "mock.open_url"}, device_trusted=True, device_id="desk"
)


# ---------------------------------------------------------------- coordinator directly


def test_plain_answer_completes_without_tools(tmp_path):
    rt, provider = build(tmp_path, [ProviderResult("Done.", usage=Usage(100, 10), cost_usd=0.001)])
    m = run(rt.missions.create("say hi"))
    r = run(rt.coordinator.run(m.mission_id, "say hi", **KW))
    assert r.ok and r.final_text == "Done." and r.steps == 1 and r.tool_calls == 0
    assert r.usage.input_tokens == 100 and r.cost_usd == 0.001 and r.model == "mock-model"
    assert provider.calls[0]["tools"] == [
        "mock.clock",
        "mock.echo",
        "mock.open_url",
        "agent.delegate",
    ]
    assert "JARVIS Core" in provider.calls[0]["system"]
    assert types(rt, m.mission_id)[-3:] == [
        "agent.run.started",
        "agent.run.step",
        "agent.run.finished",
    ]


def test_tool_loop_runs_through_gateway_and_feeds_results_back(tmp_path):
    rt, provider = build(
        tmp_path,
        [
            ProviderResult(
                "Let me check.",
                tool_calls=(call("mock.echo", {"text": "ping"}, "c1"),),
                stop_reason="tool_use",
            ),
            ProviderResult("It said ping."),
        ],
    )
    m = run(rt.missions.create("echo ping"))
    r = run(rt.coordinator.run(m.mission_id, "echo ping", **KW))
    assert r.ok and r.steps == 2 and r.tool_calls == 1 and r.final_text == "It said ping."
    msgs = provider.calls[1]["messages"]
    assert [x.role for x in msgs] == ["user", "assistant", "tool"]
    tool_msg = json.loads(msgs[2].content)
    assert tool_msg["status"] == "succeeded" and tool_msg["result"] == {"text": "ping"}
    assert tool_msg["verified"] == "skipped" and msgs[2].tool_call_id == "c1"
    t = types(rt, m.mission_id)
    assert "agent.tool.proposed" in t and "capability.succeeded" in t and "permission.allowed" in t
    assert t.index("capability.succeeded") < t.index("agent.run.finished")


def test_allowlist_rejects_unknown_tools_without_executing(tmp_path):
    rt, provider = build(
        tmp_path,
        [
            ProviderResult(
                "",
                tool_calls=(
                    call("shell.exec", {"cmd": "rm -rf /"}, "x1"),
                    call("mock.echo", {"text": "ok"}, "x2"),
                ),
                stop_reason="tool_use",
            ),
            ProviderResult("Fine."),
        ],
    )
    m = run(rt.missions.create("x"))
    r = run(rt.coordinator.run(m.mission_id, "x", allowlist={"mock.echo"}, device_trusted=True))
    assert r.ok and r.tool_calls == 1 and r.tools == ["mock.echo"]
    msgs = provider.calls[1]["messages"]
    assert json.loads(msgs[1].content) == {"status": "rejected", "reason": "not allowed"}
    assert msgs[1].tool_call_id == "x1"
    t = types(rt, m.mission_id)
    assert t.count("agent.tool.rejected") == 1 and t.count("capability.invoked") == 1
    rejected = next(
        e for _, e in rt.bus.replay(correlation_id=m.mission_id) if e.type == "agent.tool.rejected"
    )
    assert rejected.payload["call"]["name"] == "shell.exec" and rejected.priority.value == "urgent"


def test_budget_stops_an_endless_tool_loop(tmp_path):
    rt, _ = build(
        tmp_path,
        lambda msgs: ProviderResult(
            "", tool_calls=(call("mock.clock", {}, "k"),), stop_reason="tool_use"
        ),
    )
    m = run(rt.missions.create("loop"))
    r = run(rt.coordinator.run(m.mission_id, "loop", budget=AgentBudget(max_steps=3), **KW))
    assert r.outcome is RunOutcome.BUDGET_EXCEEDED and r.steps == 3 and "steps" in (r.error or "")
    assert types(rt, m.mission_id).count("agent.run.budget_exceeded") == 1
    r2 = run(rt.coordinator.run(m.mission_id, "loop", budget=AgentBudget(max_tool_calls=2), **KW))
    assert r2.outcome is RunOutcome.BUDGET_EXCEEDED and r2.tool_calls == 2


def test_refusal_provider_error_and_missing_provider(tmp_path):
    rt, _ = build(
        tmp_path,
        [ProviderResult("", refused=True, refusal_category="cyber", stop_reason="refusal")],
    )
    m = run(rt.missions.create("x"))
    r = run(rt.coordinator.run(m.mission_id, "x", **KW))
    assert r.outcome is RunOutcome.REFUSED and "cyber" in r.error

    rt2 = CoreRuntime.build(f"sqlite:///{tmp_path / 'n.db'}", providers={}, router=MOCK_ROUTER())
    assert not rt2.coordinator.can_run()
    m2 = run(rt2.missions.create("y"))
    r2 = run(rt2.coordinator.run(m2.mission_id, "y", **KW))
    assert r2.outcome is RunOutcome.FAILED and "unavailable" in r2.error

    secret_only = CoreRuntime.build(
        f"sqlite:///{tmp_path / 's.db'}", providers={"mock": MockProvider()}, router=MOCK_ROUTER()
    )
    assert not secret_only.coordinator.can_run(
        sensitivity=Sensitivity.SECRET
    )  # no local model registered


def test_kill_switch_halts_the_run(tmp_path):
    rt, _ = build(
        tmp_path,
        [
            ProviderResult(
                "", tool_calls=(call("mock.echo", {"text": "x"}, "h"),), stop_reason="tool_use"
            )
        ],
    )
    run(rt.gateway.halt("stop"))
    m = run(rt.missions.create("x"))
    r = run(rt.coordinator.run(m.mission_id, "x", **KW))
    assert r.outcome is RunOutcome.HALTED


# ---------------------------------------------------------------- via API, resume across restart


def test_agent_command_via_api_and_health(tmp_path):
    rt, _ = build(tmp_path, [ProviderResult("The answer is 42.")])
    client = TestClient(create_app(rt))
    h = client.get("/health").json()
    assert h["agent_ready"] is True and h["providers"] == {"mock": True}
    out = client.post("/commands", json={"text": "what is the answer?"}).json()
    assert (
        out["route"] == "agent"
        and out["status"] == "completed"
        and out["result"] == "The answer is 42."
    )
    assert out["run"]["outcome"] == "completed"
    assert client.get(f"/missions/{out['mission_id']}").json()["status"] == "completed"


def test_agent_pauses_for_approval_and_resumes_after_restart_from_the_log(tmp_path):
    policy = Policy(overrides={RiskLevel.P1: Decision.ASK})
    script = [
        ProviderResult(
            "Opening.",
            tool_calls=(call("mock.open_url", {"url": "https://a.org"}, "o1"),),
            stop_reason="tool_use",
        ),
        ProviderResult("Opened a.org."),
    ]
    rt, _ = build(tmp_path, script, policy=policy)
    client = TestClient(create_app(rt))
    out = client.post(
        "/commands",
        json={"text": "please open a.org for me", "device_id": "desk", "device_trusted": True},
    ).json()
    assert out["status"] == "waiting_for_approval" and out["decision_id"]
    mid, did = out["mission_id"], out["decision_id"]
    assert client.get(f"/missions/{mid}").json()["status"] == "waiting_for_approval"
    assert [
        e["type"]
        for e in client.get(
            "/events", params={"correlation_id": mid, "type_prefix": "agent.run"}
        ).json()
    ][-1] == "agent.run.paused"

    # Restart: new runtime, new provider instance (script continues at result #2), same database.
    rt2 = CoreRuntime.build(
        f"sqlite:///{tmp_path / 'c.db'}",
        policy=policy,
        providers={"mock": MockProvider(script[1:])},
        router=MOCK_ROUTER(),
    )
    rt2.recover()
    client2 = TestClient(create_app(rt2))
    assert [d["decision_id"] for d in client2.get("/approvals").json()] == [did]
    before = len(mocks.OPENED_URLS)
    r = client2.post(f"/approvals/{did}/approve", json=PROOF_CONFIRM)
    assert r.status_code == 200, r.text
    body = r.json()
    assert (
        body["resumed"] is True
        and body["status"] == "completed"
        and body["result"] == "Opened a.org."
    )
    assert len(mocks.OPENED_URLS) == before + 1 and mocks.OPENED_URLS[-1] == "https://a.org"
    assert client2.get(f"/missions/{mid}").json()["status"] == "completed"
    t = [e["type"] for e in client2.get("/events", params={"correlation_id": mid}).json()]
    assert "agent.run.resumed" in t and t.index("agent.run.resumed") < t.index(
        "capability.succeeded"
    ) < t.index("agent.run.finished")
    assert t.count("verification.passed") == 1
    # A second approval attempt is refused and the paused run is not replayed twice.
    assert client2.post(f"/approvals/{did}/approve", json=PROOF_CONFIRM).status_code == 409
    assert run(rt2.coordinator.resume(did)) is None


def test_denying_an_agent_approval_cancels_the_mission(tmp_path):
    policy = Policy(overrides={RiskLevel.P1: Decision.ASK})
    rt, _ = build(
        tmp_path,
        [
            ProviderResult(
                "",
                tool_calls=(call("mock.open_url", {"url": "https://b.org"}, "o2"),),
                stop_reason="tool_use",
            )
        ],
        policy=policy,
    )
    client = TestClient(create_app(rt))
    out = client.post(
        "/commands", json={"text": "open b.org please", "device_trusted": True}
    ).json()
    assert out["status"] == "waiting_for_approval"
    r = client.post(f"/approvals/{out['decision_id']}/deny", json={"reason": "nope"})
    assert r.status_code == 200
    assert client.get(f"/missions/{out['mission_id']}").json()["status"] == "canceled"


def test_default_provider_selection(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_PROVIDER", "none")
    rt = CoreRuntime.build(f"sqlite:///{tmp_path / 'p1.db'}")
    assert rt.providers == {} and not rt.coordinator.can_run()
    rt_mock = CoreRuntime.build(f"sqlite:///{tmp_path / 'p2.db'}", provider="mock")
    assert list(rt_mock.providers) == ["mock"] and rt_mock.coordinator.can_run()
    rt_claude = CoreRuntime.build(f"sqlite:///{tmp_path / 'p3.db'}", provider="claude")
    assert list(rt_claude.providers) == [
        "claude"
    ]  # availability depends on the SDK being installed
    with pytest.raises(ValueError):
        CoreRuntime.build(f"sqlite:///{tmp_path / 'p4.db'}", provider="gpt")
    out = TestClient(create_app(rt)).post("/commands", json={"text": "write a game"}).json()
    assert out["status"] == "blocked"
    assert rt.missions.get(out["mission_id"]).status.value == "blocked"


def test_message_roundtrip_helpers():
    from core.agents.run import messages_from_dicts, messages_to_dicts

    msgs = [Message("user", "a"), Message("tool", "b", tool_call_id="t", name="n")]
    assert messages_from_dicts(messages_to_dicts(msgs)) == msgs
