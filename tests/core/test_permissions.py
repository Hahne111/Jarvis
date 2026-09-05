"""Tests for core.permissions (Commit 006): P0-P6 policy, approvals, expiry, no bypass, replay."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from core.events import EventBus, SQLEventStore
from core.permissions import (
    ApprovalError,
    ApprovalProof,
    ApprovalStrength,
    Decision,
    PermissionEngine,
    PermissionRequest,
    Policy,
    PolicyViolation,
    ProofMethod,
    RiskLevel,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def make(policy: Policy | None = None, url: str | None = None):
    bus = EventBus(SQLEventStore(url) if url else SQLEventStore.in_memory())
    clock = FakeClock()
    return PermissionEngine(bus, policy, clock=clock), bus, clock


def req(risk: RiskLevel, action: str = "x.y", **kw) -> PermissionRequest:
    return PermissionRequest(action=action, risk=risk, actor="test-agent", **kw)


def run(coro):
    return asyncio.run(coro)


STRONG = ApprovalProof(ProofMethod.PASSKEY, device_id="phone", device_trusted=True, reference="a1")
CONFIRM = ApprovalProof(ProofMethod.UI_CONFIRM, device_id="desk", device_trusted=True)
VOICE = ApprovalProof(ProofMethod.VOICE, device_trusted=True)


# ---------------------------------------------------------------- default policy table


@pytest.mark.parametrize(
    ("risk", "expected", "strength"),
    [
        (RiskLevel.P0, Decision.ALLOW, ApprovalStrength.NONE),
        (RiskLevel.P1, Decision.ALLOW, ApprovalStrength.NONE),
        (RiskLevel.P2, Decision.ALLOW, ApprovalStrength.NONE),
        (RiskLevel.P3, Decision.ASK, ApprovalStrength.UI_CONFIRM),
        (RiskLevel.P4, Decision.ASK, ApprovalStrength.STRONG),
        (RiskLevel.P6, Decision.DENY, ApprovalStrength.NONE),
    ],
)
def test_default_decisions_per_risk_level(risk, expected, strength):
    engine, bus, _ = make()
    d = run(engine.evaluate(req(risk)))
    assert d.decision is expected
    assert d.required_strength is strength
    assert d.rule == ("forbidden" if risk is RiskLevel.P6 else f"default:{risk.name}")
    event_name = {Decision.ALLOW: "allowed", Decision.ASK: "ask", Decision.DENY: "denied"}
    assert bus.replay()[-1][1].type == f"permission.{event_name[expected]}"


def test_p5_is_device_bound():
    engine, _, _ = make()
    assert run(engine.evaluate(req(RiskLevel.P5))).decision is Decision.DENY
    d = run(engine.evaluate(req(RiskLevel.P5, device_trusted=True, device_id="phone")))
    assert d.decision is Decision.ASK and d.required_strength is ApprovalStrength.STRONG


def test_forbidden_action_is_denied_regardless_of_risk_and_proof():
    engine, _, _ = make(Policy(forbidden_actions=frozenset({"system.disable_kill_switch"})))
    d = run(engine.evaluate(req(RiskLevel.P1, "system.disable_kill_switch")))
    assert d.decision is Decision.DENY and d.rule == "forbidden"
    with pytest.raises(ApprovalError):
        run(engine.approve(d.decision_id, STRONG))
    assert not engine.is_granted(d.decision_id)


# ---------------------------------------------------------------- policy can only get stricter


def test_policy_overrides_may_only_tighten():
    Policy(overrides={RiskLevel.P2: Decision.ASK})  # stricter: ok
    Policy(overrides={RiskLevel.P1: Decision.DENY})  # stricter: ok
    with pytest.raises(PolicyViolation):
        Policy(overrides={RiskLevel.P3: Decision.ALLOW})
    with pytest.raises(PolicyViolation):
        Policy(overrides={RiskLevel.P6: Decision.ASK})
    with pytest.raises(PolicyViolation):
        Policy(grant_ttl_s=3600)
    with pytest.raises(PolicyViolation):
        Policy(ask_ttl_s={ApprovalStrength.STRONG: 10_000})


def test_policy_accepts_plain_strings_from_config():
    engine, _, _ = make(
        Policy(overrides={"P1": "ask", "2": "ask"}, forbidden_actions=["x.forbidden"])  # type: ignore[arg-type]
    )
    assert engine.policy.overrides == {RiskLevel.P1: Decision.ASK, RiskLevel.P2: Decision.ASK}
    d = run(engine.evaluate(req(RiskLevel.P1)))
    assert d.decision is Decision.ASK and d.required_strength is ApprovalStrength.UI_CONFIRM
    assert d.expires_at is not None
    assert run(engine.evaluate(req(RiskLevel.P1, "x.forbidden"))).decision is Decision.DENY


def test_engine_tighten_applies_and_rejects_loosening():
    engine, _, _ = make()
    engine.tighten(overrides={RiskLevel.P2: Decision.ASK}, ask_actions={"web.fetch"})
    d = run(engine.evaluate(req(RiskLevel.P2)))
    assert d.decision is Decision.ASK and d.rule == "override:P2"
    assert d.required_strength is ApprovalStrength.UI_CONFIRM
    d2 = run(engine.evaluate(req(RiskLevel.P1, "web.fetch")))
    assert d2.decision is Decision.ASK and d2.rule == "ask_action"
    with pytest.raises(PolicyViolation):
        engine.tighten(overrides={RiskLevel.P2: Decision.ALLOW})  # back to default = loosening
    engine.tighten(grant_ttl_s=30)
    with pytest.raises(PolicyViolation):
        engine.tighten(grant_ttl_s=45)  # longer than current 30s
    with pytest.raises(PolicyViolation):
        engine.tighten(ask_ttl_s={ApprovalStrength.STRONG: 121})
    with pytest.raises(PolicyViolation):
        engine.tighten(bogus=1)
    assert engine.policy.grant_ttl_s == 30
    assert engine.policy.overrides[RiskLevel.P2] is Decision.ASK  # unchanged after failures


# ---------------------------------------------------------------- approvals


def test_ask_then_approve_with_sufficient_proof_grants_temporarily():
    engine, bus, clock = make()
    d = run(engine.evaluate(req(RiskLevel.P4, "system.install", correlation_id="m1")))
    assert d.decision is Decision.ASK
    assert [x.decision_id for x in engine.pending()] == [d.decision_id]
    assert not engine.is_granted(d.decision_id)

    granted = run(engine.approve(d.decision_id, STRONG))
    assert granted.decision is Decision.ALLOW
    assert granted.approval_proof == STRONG
    assert granted.rule == "default:P4+approved:passkey"
    assert engine.is_granted(d.decision_id)
    assert engine.pending() == []

    clock.advance(engine.policy.grant_ttl_s + 1)
    assert not engine.is_granted(d.decision_id)  # temporary capability expired

    types = [e.type for _, e in bus.replay(correlation_id="m1")]
    assert types == ["permission.ask", "permission.approved"]
    assert (
        bus.replay(correlation_id="m1")[-1][1].payload["decision"]["approval_proof"]["method"]
        == "passkey"
    )


def test_weak_proofs_are_rejected():
    engine, _, _ = make()
    p4 = run(engine.evaluate(req(RiskLevel.P4)))
    with pytest.raises(ApprovalError):
        run(engine.approve(p4.decision_id, CONFIRM))  # UI confirm < STRONG
    with pytest.raises(ApprovalError):
        run(engine.approve(p4.decision_id, VOICE))
    untrusted = ApprovalProof(ProofMethod.BIOMETRIC, device_trusted=False)
    with pytest.raises(ApprovalError):
        run(engine.approve(p4.decision_id, untrusted))  # P4+ needs a trusted device
    assert engine.get(p4.decision_id).decision is Decision.ASK  # still pending, nothing leaked

    p3 = run(engine.evaluate(req(RiskLevel.P3)))
    with pytest.raises(ApprovalError):
        run(engine.approve(p3.decision_id, VOICE))  # voice is never an authenticator
    assert run(engine.approve(p3.decision_id, CONFIRM)).decision is Decision.ALLOW


def test_expired_ask_cannot_be_approved_and_becomes_a_denied_audit_record():
    engine, bus, clock = make()
    d = run(engine.evaluate(req(RiskLevel.P3, correlation_id="m2")))
    clock.advance(engine.policy.ask_ttl_s[ApprovalStrength.UI_CONFIRM] + 1)
    assert engine.pending() == []
    with pytest.raises(ApprovalError):
        run(engine.approve(d.decision_id, CONFIRM))
    final = engine.get(d.decision_id)
    assert final.decision is Decision.DENY and final.rule.endswith("+expired")
    assert [e.type for _, e in bus.replay(correlation_id="m2")] == [
        "permission.ask",
        "permission.denied",
    ]


def test_deny_is_final_and_decisions_cannot_be_reused():
    engine, _, _ = make()
    d = run(engine.evaluate(req(RiskLevel.P3)))
    run(engine.deny(d.decision_id, reason="owner said no"))
    assert engine.get(d.decision_id).decision is Decision.DENY
    with pytest.raises(ApprovalError):
        run(engine.approve(d.decision_id, STRONG))
    with pytest.raises(ApprovalError):
        run(engine.deny(d.decision_id))
    allowed = run(engine.evaluate(req(RiskLevel.P1)))
    with pytest.raises(ApprovalError):
        run(engine.approve(allowed.decision_id, STRONG))  # not pending
    with pytest.raises(ApprovalError):
        engine.get("ghost")
    assert not engine.is_granted("ghost")


def test_expire_stale_sweeps_only_timed_out_asks():
    engine, _, clock = make()
    a = run(engine.evaluate(req(RiskLevel.P3)))
    clock.advance(200)
    b = run(engine.evaluate(req(RiskLevel.P3)))
    clock.advance(150)  # a: 350s old (>300 ttl), b: 150s old
    assert run(engine.expire_stale()) == 1
    assert engine.get(a.decision_id).decision is Decision.DENY
    assert engine.get(b.decision_id).decision is Decision.ASK


def test_request_validation():
    with pytest.raises(ValueError):
        PermissionRequest(action="", risk=RiskLevel.P1, actor="a")
    with pytest.raises(ValueError):
        PermissionRequest(action="x.y", risk=RiskLevel.P1, actor=" ")
    with pytest.raises(ValueError):
        PermissionRequest(action="x.y", risk=9, actor="a")  # type: ignore[arg-type]


# ---------------------------------------------------------------- restart


def test_pending_and_grants_are_rebuilt_from_the_event_log(tmp_path):
    url = f"sqlite:///{tmp_path / 'core.db'}"
    engine, _, clock = make(url=url)
    pending = run(engine.evaluate(req(RiskLevel.P4, "deploy.prod", correlation_id="m3")))
    granted = run(engine.evaluate(req(RiskLevel.P3, "mail.send", correlation_id="m3")))
    run(engine.approve(granted.decision_id, CONFIRM))
    denied = run(engine.evaluate(req(RiskLevel.P6, "vault.export", correlation_id="m3")))

    restarted, _, clock2 = make(url=url)
    clock2.now = clock.now
    assert restarted.rebuild_from_log() == 3
    assert [d.decision_id for d in restarted.pending()] == [pending.decision_id]
    assert restarted.is_granted(granted.decision_id)
    assert restarted.get(denied.decision_id).decision is Decision.DENY
    # The rebuilt pending approval can still be completed - with the same rules.
    with pytest.raises(ApprovalError):
        run(restarted.approve(pending.decision_id, CONFIRM))
    assert run(restarted.approve(pending.decision_id, STRONG)).decision is Decision.ALLOW
