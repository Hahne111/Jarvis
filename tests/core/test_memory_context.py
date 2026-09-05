"""Tests for Phase 4 steps 26-28: context builder, memory capabilities, What-JARVIS-Knows API."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from core.api import create_app
from core.capabilities import InvocationStatus
from core.events import Sensitivity
from core.memory import HashingEmbedder, MemoryStore
from core.memory.context import HEADER, ContextBuilder
from core.models import MockProvider, ModelRouter, ModelSpec, ProviderResult, Tier, ToolCallProposal
from core.permissions import ApprovalProof, ProofMethod
from core.runtime import CoreRuntime
from fastapi.testclient import TestClient


def run(coro):
    return asyncio.run(coro)


def mock_router(local: bool = False) -> ModelRouter:
    return ModelRouter(
        [
            ModelSpec(
                "mock-model",
                "mock",
                Tier.LOCAL if local else Tier.FRONTIER,
                local=local,
                supports_effort=False,
            )
        ]
    )


def build(tmp_path, script=None, *, local=False, name="c.db"):
    provider = MockProvider(script)
    rt = CoreRuntime.build(
        f"sqlite:///{tmp_path / name}", providers={"mock": provider}, router=mock_router(local)
    )
    return rt, provider


CONFIRM = ApprovalProof(ProofMethod.UI_CONFIRM, device_id="desk", device_trusted=True)
KW = dict(actor="agent", correlation_id="m1", device_trusted=True, device_id="desk")


# ---------------------------------------------------------------- context builder


def test_context_builder_selects_relevant_items_and_hides_secrets_from_cloud():
    store = MemoryStore(embedder=HashingEmbedder())
    from core.memory import MemoryItem

    items = [
        MemoryItem("preference", "owner", "preferred_editor", "VS Code", "explicit_statement"),
        MemoryItem(
            "project",
            "project:atlas",
            "database",
            "PostgreSQL",
            "explicit_statement",
            project_scope="atlas",
        ),
        MemoryItem(
            "semantic",
            "owner",
            "wifi_password",
            "hunter2",
            "explicit_statement",
            sensitivity=Sensitivity.SECRET,
        ),
        MemoryItem("habit", "owner", "coffee_time", "07:00", "observation"),
    ]
    for i in items:
        store.save(i)
    cb = ContextBuilder(store)
    block = cb.build(
        "which editor should I open for the atlas database work?", project_scope="atlas"
    )
    assert block.text.startswith(HEADER)
    assert "preferred_editor: VS Code" in block.text and "database: PostgreSQL" in block.text
    assert "[preference, 0.90, explicit_statement]" in block.text
    assert "hunter2" not in block.text and len(block.memory_ids) == 2
    # secret allowed only for a local model
    secret_cloud = cb.build("what is the wifi password")
    secret_local = cb.build("what is the wifi password", cloud=False)
    assert secret_cloud.empty and "hunter2" in secret_local.text
    # budgets
    assert cb.build("editor database coffee", max_items=1).memory_ids.__len__() == 1
    assert cb.build("editor database coffee", max_chars=len(HEADER) + 10).empty
    assert cb.build("nothing matches zzz").empty


def test_coordinator_injects_context_and_records_which_memories_were_used(tmp_path):
    rt, provider = build(tmp_path, [ProviderResult("Opening VS Code.")])
    fact = run(rt.memory_writer.remember("preference", "owner", "preferred_editor", "VS Code")).item
    run(
        rt.memory_writer.remember(
            "semantic", "owner", "vault_pin", "0000", sensitivity="secret", owner_approved=True
        )
    )
    m = run(rt.missions.create("open my editor"))
    r = run(
        rt.coordinator.run(
            m.mission_id, "open my preferred editor please", allowlist=set(), device_trusted=True
        )
    )
    assert r.ok
    system = provider.calls[0]["system"]
    assert HEADER in system and "VS Code" in system and "0000" not in system
    used = [
        e for _, e in rt.bus.replay(correlation_id=m.mission_id) if e.type == "memory.context_used"
    ]
    assert (
        len(used) == 1
        and used[0].payload["memory_ids"] == [fact.memory_id]
        and used[0].payload["cloud"] is True
    )

    rt_local, provider_local = build(tmp_path, [ProviderResult("ok")], local=True, name="l.db")
    run(
        rt_local.memory_writer.remember(
            "semantic", "owner", "vault_pin", "0000", sensitivity="secret", owner_approved=True
        )
    )
    m2 = run(rt_local.missions.create("x"))
    run(rt_local.coordinator.run(m2.mission_id, "what is my vault pin", allowlist=set()))
    assert "0000" in provider_local.calls[0]["system"]  # local model may see secrets


def test_correction_changes_what_the_agent_is_told(tmp_path):
    rt, provider = build(tmp_path, lambda msgs: ProviderResult("noted"))
    client = TestClient(create_app(rt))
    old = run(rt.memory_writer.remember("preference", "owner", "preferred_editor", "Vim")).item
    m = run(rt.missions.create("editor?"))
    run(rt.coordinator.run(m.mission_id, "open my preferred editor", allowlist=set()))
    assert "preferred_editor: Vim" in provider.calls[-1]["system"]
    r = client.post(f"/memory/{old.memory_id}/correct", json={"value": "VS Code"})
    assert r.status_code == 200 and r.json()["memory"]["source"] == "correction"
    run(rt.coordinator.run(m.mission_id, "open my preferred editor", allowlist=set()))
    assert (
        "preferred_editor: VS Code" in provider.calls[-1]["system"]
        and "Vim" not in provider.calls[-1]["system"]
    )


# ---------------------------------------------------------------- memory capabilities via gateway


def test_memory_capabilities_run_through_gateway_and_verifier(tmp_path):
    rt, _ = build(tmp_path)
    assert {"memory.recall", "memory.remember", "memory.correct", "memory.forget"} <= set(
        rt.capabilities.names()
    )
    res = run(
        rt.executor.run(
            "memory.remember",
            {"type": "preference", "subject": "owner", "predicate": "tea", "value": "green"},
            **KW,
        )
    )
    assert res.ok and res.verification.outcome.value == "achieved"
    mid = res.invocation.result["memory_id"]
    assert rt.memory.get(mid).value == "green"

    bad = run(
        rt.executor.run(
            "memory.remember",
            {"type": "working", "subject": "s", "predicate": "p", "value": "v"},
            **KW,
        )
    )
    assert (
        bad.invocation.status is InvocationStatus.FAILED and "type must be" in bad.invocation.error
    )

    recall = run(rt.executor.run("memory.recall", {"query": "tea"}, **KW))
    assert recall.ok and recall.invocation.result["items"][0]["value"] == "green"
    run(
        rt.memory_writer.remember(
            "semantic", "owner", "pin", "9999", sensitivity="secret", owner_approved=True
        )
    )
    assert (
        run(rt.executor.run("memory.recall", {"query": "pin"}, **KW)).invocation.result["count"]
        == 0
    )

    # correct and forget are P3 -> wait for the owner
    waiting = run(rt.executor.run("memory.correct", {"memory_id": mid, "value": "black"}, **KW))
    assert waiting.invocation.status is InvocationStatus.AWAITING_APPROVAL
    run(rt.permissions.approve(waiting.invocation.decision_id, CONFIRM))
    done = run(
        rt.executor.run(
            "memory.correct",
            {"memory_id": mid, "value": "black"},
            decision_id=waiting.invocation.decision_id,
            **KW,
        )
    )
    assert done.ok and rt.memory.find("owner", "tea")[0].value == "black"
    new_id = done.invocation.result["memory_id"]
    w2 = run(rt.executor.run("memory.forget", {"memory_id": new_id}, **KW))
    run(rt.permissions.approve(w2.invocation.decision_id, CONFIRM))
    gone = run(
        rt.executor.run(
            "memory.forget", {"memory_id": new_id}, decision_id=w2.invocation.decision_id, **KW
        )
    )
    assert gone.ok and rt.memory.get(new_id) is None


def test_agent_can_store_an_explicit_statement_via_tool(tmp_path):
    script = [
        ProviderResult(
            "",
            tool_calls=(
                ToolCallProposal(
                    "memory.remember",
                    {
                        "type": "preference",
                        "subject": "owner",
                        "predicate": "greeting",
                        "value": "Sir",
                    },
                    call_id="r1",
                ),
            ),
            stop_reason="tool_use",
        ),
        ProviderResult("Noted, Sir."),
    ]
    rt, _ = build(tmp_path, script)
    m = run(rt.missions.create("call me sir"))
    r = run(
        rt.coordinator.run(
            m.mission_id,
            "from now on call me Sir",
            allowlist=set(rt.capabilities.names()),
            device_trusted=True,
        )
    )
    assert r.ok and rt.memory.find("owner", "greeting")[0].value == "Sir"
    types = [e.type for _, e in rt.bus.replay(correlation_id=m.mission_id)]
    assert "memory.written" in types and "verification.passed" in types


# ---------------------------------------------------------------- What JARVIS Knows API


def test_memory_api_list_search_actions_policy_and_forget_since(tmp_path):
    rt, _ = build(tmp_path)
    client = TestClient(create_app(rt))
    a = run(rt.memory_writer.remember("preference", "owner", "editor", "VS Code")).item
    b = run(
        rt.memory_writer.remember(
            "project", "project:atlas", "database", "PostgreSQL", project_scope="atlas"
        )
    ).item
    assert {x["memory_id"] for x in client.get("/memory").json()} == {a.memory_id, b.memory_id}
    assert [x["memory_id"] for x in client.get("/memory", params={"type": "project"}).json()] == [
        b.memory_id
    ]
    hits = client.get("/memory", params={"q": "which database", "project": "atlas"}).json()
    assert hits[0]["memory_id"] == b.memory_id and "score" in hits[0]
    assert client.get(f"/memory/{a.memory_id}").json()["value"] == "VS Code"
    assert client.get("/memory/ghost").status_code == 404

    assert client.post(f"/memory/{a.memory_id}/pin").json()["pinned"] is True
    assert client.post(f"/memory/{a.memory_id}/unpin").json()["pinned"] is False
    tmp = client.post(f"/memory/{a.memory_id}/temporary", json={"ttl_s": 60}).json()
    assert tmp["retention"] == "temporary" and tmp["expires_at"]
    assert client.post("/memory/ghost/correct", json={"value": "x"}).status_code == 404
    assert client.post(f"/memory/{b.memory_id}/correct", json={"value": ""}).status_code == 422
    fixed = client.post(f"/memory/{b.memory_id}/correct", json={"value": "MySQL"}).json()
    assert fixed["action"] == "superseded"
    assert client.post(f"/memory/{b.memory_id}/correct", json={"value": "again"}).status_code == 409
    assert client.post(f"/memory/{a.memory_id}/forget").json() == {"forgotten": a.memory_id}
    assert client.post(f"/memory/{a.memory_id}/forget").status_code == 404

    pol = client.get("/memory/policy").json()
    assert pol["learn_from_observation"] is True and pol["dont_learn"] == []
    pol2 = client.post("/memory/policy", json={"learn_from_observation": False}).json()
    assert pol2["learn_from_observation"] is False
    assert (
        run(rt.memory_writer.remember("habit", "owner", "x", "y", source="observation")).action
        == "skipped"
    )
    assert client.post(
        "/memory/dont_learn", json={"subject": "owner", "predicate": "location"}
    ).json()["dont_learn"] == [["owner", "location"]]
    assert [e.type for _, e in rt.bus.replay(type_prefix="memory.policy_changed")] == [
        "memory.policy_changed"
    ]

    recent = run(rt.memory_writer.remember("episodic", "session", "said", "oops")).item
    out = client.post("/memory/forget_since", json={"minutes": 30}).json()
    assert out["deleted"] >= 1 and rt.memory.get(recent.memory_id) is None
    since = datetime.fromisoformat(out["since"])
    assert datetime.now(UTC) - since < timedelta(minutes=31)
    assert client.post("/memory/forget_since", json={"minutes": 0}).status_code == 422
    assert client.get("/health").json()["memory_items"] == rt.memory.count()
    assert "WHAT JARVIS KNOWS" in client.get("/debug").text
