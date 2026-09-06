"""Regression: security invariants that must hold in every 1.0 build (SECURITY.md, SPEC §7/§29).

Each test pins one guarantee the Core makes regardless of provider, adapter or UI:
policy only tightens, P6 never executes, the kill switch stops side effects, secrets never reach
the event log, unsigned remote callers cannot approve, model tool-calls are filtered by allowlist,
and the skill reviewer rejects attempts to reach around the Core.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest
from core.api import create_app
from core.capabilities import CapabilityManifest, InvocationStatus
from core.models.provider import ToolCallProposal, filter_tool_calls
from core.permissions import (
    Decision,
    Policy,
    PolicyViolation,
    RiskLevel,
)
from core.runtime import CoreRuntime
from core.skills import SkillReviewer
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[2]
REMOTE = ("203.0.113.77", 40001)
KW = dict(actor="owner", correlation_id="sec-1", device_trusted=True, device_id="desk")
PROOF_STRONG = {"method": "passkey", "device_id": "phone", "device_trusted": True, "reference": "x"}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def rt(tmp_path):
    return CoreRuntime.build(
        f"sqlite:///{tmp_path / 'sec.db'}",
        provider="none",
        home="fake",
        workspace_root=str(tmp_path / "ws"),
        skills_root=str(tmp_path / "skills"),
    )


def all_events_json(rt) -> str:
    return json.dumps([e.to_dict() for _, e in rt.bus.replay()], default=str)


def test_policy_can_only_become_stricter(rt):
    with pytest.raises(PolicyViolation):
        Policy(overrides={RiskLevel.P3: Decision.ALLOW})
    with pytest.raises(PolicyViolation):
        Policy(overrides={RiskLevel.P6: Decision.ASK})
    rt.permissions.tighten(overrides={RiskLevel.P1: Decision.ASK})
    with pytest.raises(PolicyViolation):
        rt.permissions.tighten(overrides={RiskLevel.P1: Decision.ALLOW})
    assert rt.permissions.policy.overrides[RiskLevel.P1] is Decision.ASK


def test_p6_is_never_executed_and_cannot_be_approved(rt):
    calls: list[dict] = []

    async def handler(args):
        calls.append(args)
        return {"boom": True}

    rt.capabilities.register(
        CapabilityManifest(
            name="vault.export",
            version="1",
            risk=RiskLevel.P6,
            side_effects=True,
            verifier="mock.url_recorded",
        ),
        handler,
    )
    out = run(rt.executor.run("vault.export", {}, **KW))
    assert out.invocation.status is InvocationStatus.DENIED and calls == []
    # strong proof does not help: there is nothing to approve, the deny is final
    assert not [d for d in rt.permissions.pending() if d.request.action == "vault.export"]
    assert "vault.export" in rt.capabilities.names()
    again = run(
        rt.executor.run(
            "vault.export", {}, actor="agent", **{k: v for k, v in KW.items() if k != "actor"}
        )
    )
    assert again.invocation.status is InvocationStatus.DENIED and calls == []


def test_kill_switch_stops_side_effects_until_strong_resume(rt):
    client = TestClient(create_app(rt))
    before = rt.home.backend.entities["light.kitchen"].state
    assert client.post("/kill").json() == {"halted": True}
    r = client.post("/commands", json={"text": "turn on the kitchen light", "device_trusted": True})
    assert r.json()["status"] == "halted"
    assert rt.home.backend.entities["light.kitchen"].state == before
    direct = run(rt.executor.run("home.light.set", {"target": "kitchen", "on": True}, **KW))
    assert direct.invocation.status is InvocationStatus.HALTED
    assert rt.home.backend.entities["light.kitchen"].state == before
    # only strong proof resumes; a UI tap does not
    assert (
        client.post("/resume", json={"method": "ui_confirm", "device_trusted": True}).status_code
        == 403
    )
    assert client.post("/resume", json=PROOF_STRONG).status_code == 200
    ok = client.post(
        "/commands", json={"text": "turn on the kitchen light", "device_trusted": True}
    )
    assert ok.json()["status"] == "completed"


def test_secrets_never_reach_the_event_log_or_the_api(rt):
    secret = "hunter2-very-secret-token"  # noqa: S105 - test fixture value
    run(
        rt.memory_writer.remember(
            "preference",
            "owner",
            "wifi_password",
            secret,
            sensitivity="secret",
            owner_approved=True,
            correlation_id="sec-1",
        )
    )
    log = all_events_json(rt)
    assert "wifi_password" in log or "memory.remembered" in log
    assert secret not in log
    client = TestClient(create_app(rt))
    assert secret not in client.get("/events").text
    assert secret not in client.get("/health").text
    # the DB URL never leaks credentials
    from core.runtime import _redact

    assert "pw" not in _redact("postgresql://u:pw@h/db")


def test_unsigned_remote_callers_cannot_approve_kill_or_change_policy(rt):
    local = TestClient(create_app(rt))
    remote = TestClient(create_app(rt), client=REMOTE)
    waiting = local.post("/commands", json={"text": "wake desktop", "device_trusted": True}).json()
    assert waiting["status"] == "waiting_for_approval"
    did = waiting["decision_id"]
    assert (
        remote.post(f"/approvals/{did}/approve", json={"method": "ui_confirm"}).status_code == 403
    )
    assert remote.post(f"/approvals/{did}/approve", json=PROOF_STRONG).status_code == 403
    assert remote.post("/resume", json=PROOF_STRONG).status_code == 403
    # an unsigned remote caller is never a trusted device: a P0 command runs, a P3 one does not
    assert (
        remote.post("/commands", json={"text": "echo hi", "device_trusted": True}).json()["status"]
        == "completed"
    )
    risky = remote.post("/commands", json={"text": "wake desktop", "device_trusted": True}).json()
    assert risky["status"] != "completed" and risky["status"] != "waiting_for_approval"
    assert (
        local.get(f"/missions/{waiting['mission_id']}").json()["status"] == "waiting_for_approval"
    )


def test_model_tool_calls_are_filtered_by_allowlist():
    proposals = [
        ToolCallProposal(name="mock.echo", args={"text": "ok"}),
        ToolCallProposal(name="system.lock_screen", args={}),
        ToolCallProposal(name="workspace.run", args={"command": "rm"}),
    ]
    allowed, rejected = filter_tool_calls(proposals, {"mock.echo"})
    assert [p.name for p in allowed] == ["mock.echo"]
    assert {p.name for p in rejected} == {"system.lock_screen", "workspace.run"}
    assert filter_tool_calls(proposals, set()) == ([], proposals)


def test_skill_reviewer_rejects_bypass_attempts(tmp_path):
    example = REPO / "skills" / "examples" / "hello_world"
    reviewer = SkillReviewer()
    assert reviewer.review(example).ok
    for tag, inject in (
        ("os", "import os\n"),
        ("subprocess", "import subprocess\n"),
        ("core", "from core.permissions import Policy\n"),
        ("gateway", "from core.capabilities.gateway import ExecutionGateway\n"),
        ("eval", "x = eval('1')\n"),
        ("open", "f = open('/etc/passwd')\n"),
        ("dunder", "y = ().__class__.__subclasses__()\n"),
    ):
        dst = tmp_path / tag
        shutil.copytree(example, dst, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
        skill = dst / "skill.py"
        skill.write_text(inject + skill.read_text())
        report = reviewer.review(dst)
        assert not report.ok, (tag, report.to_dict())
        assert any(f.severity == "reject" for f in report.findings), tag
