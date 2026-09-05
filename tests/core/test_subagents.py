"""Tests for subagent orchestration (SPEC §6.3): roles, isolation, parallelism, shared budget."""

from __future__ import annotations

import asyncio
import json

from core.agents import DELEGATE_TOOL, ROLES, AgentCoordinator, RunOutcome
from core.capabilities import CapabilityRegistry, register_mocks
from core.models import (
    AgentBudget,
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


def run(coro):
    return asyncio.run(coro)


def mock_router() -> ModelRouter:
    return ModelRouter([ModelSpec("mock-model", "mock", Tier.FRONTIER, supports_effort=False)])


def call(name, args, call_id):
    return ToolCallProposal(name, args, call_id=call_id)


def delegate(role, goal, call_id):
    return call(DELEGATE_TOOL, {"role": role, "goal": goal}, call_id)


def build(tmp_path, script, *, policy=None, **coord_kw):
    provider = MockProvider(script)
    rt = CoreRuntime.build(
        f"sqlite:///{tmp_path / 'c.db'}",
        policy=policy,
        providers={"mock": provider},
        router=mock_router(),
    )
    if coord_kw:
        rt.coordinator = AgentCoordinator(
            bus=rt.bus,
            executor=rt.executor,
            capabilities=rt.capabilities,
            router=rt.router,
            providers={"mock": provider},
            permissions=rt.permissions,
            **coord_kw,
        )
    return rt, provider


def types(rt, mid):
    return [e.type for _, e in rt.bus.replay(correlation_id=mid)]


KW = dict(
    allowlist={"mock.echo", "mock.clock", "mock.open_url"}, device_trusted=True, device_id="desk"
)


# ---------------------------------------------------------------- roles


def test_roles_restrict_tools_by_risk():
    caps = register_mocks(CapabilityRegistry())
    allow = frozenset({"mock.echo", "mock.clock", "mock.open_url"})
    assert ROLES["research"].filter_allowlist(allow, caps) == {"mock.echo", "mock.clock"}
    assert ROLES["verification"].filter_allowlist(allow, caps) == {"mock.echo", "mock.clock"}
    assert ROLES["security"].filter_allowlist(allow, caps) == {"mock.echo", "mock.clock"}
    assert ROLES["test"].filter_allowlist(allow, caps) == allow  # open_url is P1 <= P2
    assert ROLES["implementation"].filter_allowlist(allow, caps) == allow
    assert ROLES["research"].filter_allowlist(frozenset({"ghost"}), caps) == frozenset()
    assert set(ROLES) == {"research", "implementation", "test", "verification", "security"}


# ---------------------------------------------------------------- delegation


def scripted_by_goal(table):
    """Provider script keyed by the first user message: parent and sub-runs get their own lines."""
    state = {}

    def fn(messages):
        goal = messages[0].content
        i = state.get(goal, 0)
        state[goal] = i + 1
        return table[goal][i]

    return fn


def test_delegation_runs_isolated_subrun_and_returns_result_to_parent(tmp_path):
    table = {
        "find the time": [
            ProviderResult(
                "",
                tool_calls=(delegate("research", "what time is it?", "d1"),),
                stop_reason="tool_use",
            ),
            ProviderResult("It is now."),
        ],
        "what time is it?": [
            ProviderResult(
                "",
                tool_calls=(call("mock.clock", {}, "c1"),),
                stop_reason="tool_use",
                usage=Usage(5, 5),
            ),
            ProviderResult("The clock says now.", usage=Usage(5, 5)),
        ],
    }
    rt, provider = build(tmp_path, scripted_by_goal(table))
    m = run(rt.missions.create("find the time"))
    r = run(rt.coordinator.run(m.mission_id, "find the time", **KW))
    assert r.ok and r.final_text == "It is now." and r.depth == 0 and r.role == "coordinator"

    sub_calls = [c for c in provider.calls if c["messages"][0].content == "what time is it?"]
    assert len(sub_calls) == 2
    assert sub_calls[0]["tools"] == ["mock.clock", "mock.echo"]  # research: P0 only, no delegate
    assert "research subagent" in sub_calls[0]["system"] and "JARVIS Core" in sub_calls[0]["system"]
    assert [x.role for x in sub_calls[1]["messages"]] == ["user", "tool"]  # isolated history

    parent_second = [c for c in provider.calls if c["messages"][0].content == "find the time"][1]
    tool_msg = json.loads(parent_second["messages"][-1].content)
    assert tool_msg["role"] == "research" and tool_msg["outcome"] == "completed"
    assert (
        tool_msg["result"] == "The clock says now."
        and parent_second["messages"][-1].tool_call_id == "d1"
    )

    t = types(rt, m.mission_id)
    assert t.count("agent.subrun.started") == 1 and t.count("agent.subrun.finished") == 1
    assert t.count("agent.run.finished") == 1 and "capability.succeeded" in t
    sub_started = next(
        e for _, e in rt.bus.replay(correlation_id=m.mission_id) if e.type == "agent.subrun.started"
    )
    assert (
        sub_started.payload["run"]["parent_run_id"] == r.run_id
        and sub_started.payload["run"]["depth"] == 1
    )
    assert r.tool_calls == 0 and sub_started.payload["run"]["tool_calls"] == 0
    sub_finished = next(
        e
        for _, e in rt.bus.replay(correlation_id=m.mission_id)
        if e.type == "agent.subrun.finished"
    )
    assert (
        sub_finished.payload["run"]["tool_calls"] == 1 and sub_finished.payload["run"]["steps"] == 2
    )


def test_parallel_delegations_keep_call_order_and_share_one_budget(tmp_path):
    table = {
        "compare": [
            ProviderResult(
                "",
                tool_calls=(delegate("research", "A?", "dA"), delegate("verification", "B?", "dB")),
                stop_reason="tool_use",
            ),
            ProviderResult("A and B compared."),
        ],
        "A?": [ProviderResult("answer A", usage=Usage(10, 10), cost_usd=0.01)],
        "B?": [ProviderResult("answer B", usage=Usage(10, 10), cost_usd=0.01)],
    }
    rt, _ = build(tmp_path, scripted_by_goal(table))
    m = run(rt.missions.create("compare"))
    r = run(rt.coordinator.run(m.mission_id, "compare", budget=AgentBudget(max_steps=10), **KW))
    assert r.ok
    finished = [
        e for _, e in rt.bus.replay(correlation_id=m.mission_id) if e.type == "agent.run.finished"
    ][-1]
    assert finished.payload["run"]["steps"] == 2
    # Parent messages: user, tool(dA), tool(dB), assistant
    t = types(rt, m.mission_id)
    assert t.count("agent.subrun.started") == 2 and t.count("agent.subrun.finished") == 2

    # Shared budget: 3 steps -> parent step 1 + two sub-steps use it up; parent cannot go on.
    rt2, _ = build(tmp_path, scripted_by_goal(table))
    m2 = run(rt2.missions.create("compare"))
    r2 = run(rt2.coordinator.run(m2.mission_id, "compare", budget=AgentBudget(max_steps=3), **KW))
    assert r2.outcome is RunOutcome.BUDGET_EXCEEDED
    assert "agent.run.budget_exceeded" in types(rt2, m2.mission_id)


def test_unknown_role_and_too_many_delegations_are_rejected_without_subruns(tmp_path):
    table = {
        "x": [
            ProviderResult(
                "",
                tool_calls=(
                    delegate("wizard", "abracadabra", "d0"),
                    delegate("research", "", "d1"),
                    delegate("research", "one", "d2"),
                    delegate("research", "two", "d3"),
                ),
                stop_reason="tool_use",
            ),
            ProviderResult("ok"),
        ],
        "one": [ProviderResult("1")],
        "two": [ProviderResult("2")],
    }
    rt, provider = build(tmp_path, scripted_by_goal(table), max_subagents_per_step=1)
    m = run(rt.missions.create("x"))
    r = run(rt.coordinator.run(m.mission_id, "x", **KW))
    assert r.ok
    msgs = [c for c in provider.calls if c["messages"][0].content == "x"][1]["messages"]
    by_id = {x.tool_call_id: json.loads(x.content) for x in msgs if x.role == "tool"}
    assert by_id["d0"]["status"] == "rejected" and "wizard" in by_id["d0"]["reason"]
    for cid in ("d1", "d2", "d3"):  # beyond max_subagents_per_step=1 (d0 took the only slot)
        assert by_id[cid] == {"status": "rejected", "reason": "at most 1 subagents per step"}
    assert types(rt, m.mission_id).count("agent.subrun.started") == 0


def test_subagent_cannot_wait_for_approval_and_escalates(tmp_path):
    policy = Policy(overrides={RiskLevel.P1: Decision.ASK})
    table = {
        "open a.org": [
            ProviderResult(
                "",
                tool_calls=(delegate("implementation", "open https://a.org now", "d1"),),
                stop_reason="tool_use",
            ),
            ProviderResult(
                "",
                tool_calls=(call("mock.open_url", {"url": "https://a.org"}, "o1"),),
                stop_reason="tool_use",
            ),
        ],
        "open https://a.org now": [
            ProviderResult(
                "",
                tool_calls=(call("mock.open_url", {"url": "https://a.org"}, "s1"),),
                stop_reason="tool_use",
            ),
            ProviderResult("I could not: needs owner approval."),
        ],
    }
    rt, provider = build(tmp_path, scripted_by_goal(table), policy=policy)
    m = run(rt.missions.create("open a.org"))
    r = run(rt.coordinator.run(m.mission_id, "open a.org", **KW))
    # Sub-run: ask was denied deterministically and escalated; parent then asked itself -> pauses.
    sub_second = [
        c for c in provider.calls if c["messages"][0].content == "open https://a.org now"
    ][1]
    esc = json.loads(sub_second["messages"][-1].content)
    assert esc["status"] == "needs_owner_approval" and esc["capability"] == "mock.open_url"
    assert r.outcome is RunOutcome.AWAITING_APPROVAL and r.pending_decision_id
    pending = rt.permissions.pending()
    assert [d.decision_id for d in pending] == [
        r.pending_decision_id
    ]  # only the parent's ask is open
    denied = [d for d in rt.permissions._decisions.values() if d.decision is Decision.DENY]
    assert any("subagent cannot wait" in (d.reason or "") for d in denied)


def test_delegation_can_be_disabled(tmp_path):
    rt, provider = build(tmp_path, [ProviderResult("hi")], allow_delegation=False)
    m = run(rt.missions.create("x"))
    assert run(rt.coordinator.run(m.mission_id, "x", **KW)).ok
    assert DELEGATE_TOOL not in provider.calls[0]["tools"]
