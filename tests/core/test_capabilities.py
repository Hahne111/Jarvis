"""Tests for core.capabilities (Commit 007): manifests, registry, mocks, gateway, kill switch.

Phase 1 exit: a mock tool runs only after permission.  Phase 2 exit: a denied action cannot be
bypassed; a risky action waits for approval.
"""

from __future__ import annotations

import asyncio

import pytest
from core.capabilities import (
    CapabilityConflict,
    CapabilityInputError,
    CapabilityManifest,
    CapabilityNotFound,
    CapabilityRegistry,
    ExecutionGateway,
    InvocationStatus,
    mocks,
    register_mocks,
)
from core.events import EventBus, SQLEventStore
from core.permissions import (
    ApprovalError,
    ApprovalProof,
    Decision,
    PermissionEngine,
    Policy,
    ProofMethod,
    RiskLevel,
)

STRONG = ApprovalProof(ProofMethod.PASSKEY, device_id="phone", device_trusted=True, reference="r1")
CONFIRM = ApprovalProof(ProofMethod.UI_CONFIRM, device_id="desk", device_trusted=True)


def run(coro):
    return asyncio.run(coro)


def make(policy: Policy | None = None):
    bus = EventBus(SQLEventStore.in_memory())
    perms = PermissionEngine(bus, policy)
    registry = register_mocks(CapabilityRegistry())
    return ExecutionGateway(registry, perms, bus), registry, perms, bus


def types(bus: EventBus, correlation_id: str) -> list[str]:
    return [e.type for _, e in bus.replay(correlation_id=correlation_id)]


# ---------------------------------------------------------------- manifest


def test_manifest_from_spec_example_roundtrips():
    spec = {
        "name": "computer.open_app",
        "version": "1.0",
        "risk": "P1",
        "inputs": {"app_id": "string"},
        "requires": ["device.trusted"],
        "side_effects": True,
        "reversible": False,
        "verifier": "computer.process_running",
        "timeout_ms": 10000,
    }
    m = CapabilityManifest.from_dict(spec)
    assert m.risk is RiskLevel.P1 and m.requires == ("device.trusted",)
    assert CapabilityManifest.from_dict(m.to_dict()) == m


def test_manifest_validation_rules():
    with pytest.raises(ValueError):
        CapabilityManifest(name="OpenApp", version="1", risk=RiskLevel.P1)
    with pytest.raises(ValueError):
        CapabilityManifest(name="a.b", version="1", risk=RiskLevel.P1, requires=("magic",))
    with pytest.raises(ValueError):
        CapabilityManifest(name="a.b", version="1", risk=RiskLevel.P1, inputs={"x": "blob"})
    with pytest.raises(ValueError):  # side effects need a verifier (Law 4)
        CapabilityManifest(name="a.b", version="1", risk=RiskLevel.P1, side_effects=True)
    with pytest.raises(ValueError):  # side effects cannot be "observe"
        CapabilityManifest(
            name="a.b", version="1", risk=RiskLevel.P0, side_effects=True, verifier="v"
        )
    with pytest.raises(ValueError):
        CapabilityManifest(name="a.b", version="1", risk=RiskLevel.P1, timeout_ms=0)


def test_input_schema_validation():
    m = CapabilityManifest(
        name="a.b",
        version="1",
        risk=RiskLevel.P0,
        inputs={"text": "string", "count": "integer?", "flag": "boolean?", "ratio": "number?"},
    )
    assert m.validate_inputs({"text": "hi"}) == {"text": "hi"}
    assert m.validate_inputs({"text": "hi", "count": 2, "ratio": 0.5}) == {
        "text": "hi",
        "count": 2,
        "ratio": 0.5,
    }
    for bad in ({}, {"text": 1}, {"text": "hi", "count": True}, {"text": "hi", "extra": 1}, []):
        with pytest.raises(CapabilityInputError):
            m.validate_inputs(bad)  # type: ignore[arg-type]


# ---------------------------------------------------------------- registry + mocks


def test_registry_register_get_conflict_and_health():
    reg = register_mocks(CapabilityRegistry())
    assert reg.names() == ["mock.clock", "mock.echo", "mock.open_url"]
    assert "mock.echo" in reg and "nope" not in reg
    with pytest.raises(CapabilityConflict):
        reg.register(mocks.ECHO, mocks.echo)
    with pytest.raises(CapabilityNotFound):
        reg.get("nope")
    with pytest.raises(TypeError):
        reg.register(CapabilityManifest(name="x.y", version="1", risk=RiskLevel.P0), "nope")  # type: ignore[arg-type]
    assert reg.health()["mock.echo"]["status"] == "unknown"
    assert [m["name"] for m in reg.manifests()] == reg.names()
    assert reg.unregister("mock.clock") is True and reg.unregister("mock.clock") is False


def test_mock_handlers():
    assert mocks.echo({"text": "hi"}) == {"text": "hi"}
    assert "now" in mocks.clock({})
    with pytest.raises(CapabilityInputError):
        mocks.open_url({"url": "file:///etc/passwd"})
    mocks.OPENED_URLS.clear()
    assert mocks.open_url({"url": "https://example.org"})["mock"] is True
    assert mocks.OPENED_URLS == ["https://example.org"]


# ---------------------------------------------------------------- gateway: permission path


def test_p0_mock_runs_after_automatic_permission_and_emits_ordered_events():
    gw, reg, perms, bus = make()
    inv = run(gw.invoke("mock.echo", {"text": "hi"}, actor="agent", correlation_id="m1"))
    assert inv.ok and inv.result == {"text": "hi"} and inv.attempts == 1
    assert inv.rule == "default:P0" and inv.decision_id
    assert perms.get(inv.decision_id).used_at is not None  # grant consumed
    assert types(bus, "m1") == [
        "permission.allowed",
        "permission.consumed",
        "capability.invoked",
        "capability.succeeded",
    ]
    assert reg.health()["mock.echo"] == {
        **reg.health()["mock.echo"],
        "status": "ok",
        "invocations": 1,
        "failures": 0,
    }


def test_requirements_block_before_permission_is_even_asked():
    gw, _, _, bus = make()
    inv = run(gw.invoke("mock.open_url", {"url": "https://x.org"}, actor="a", correlation_id="m2"))
    assert inv.status is InvocationStatus.DENIED and inv.rule == "requires:device.trusted"
    assert types(bus, "m2") == ["capability.denied"]
    ok = run(
        gw.invoke(
            "mock.open_url",
            {"url": "https://x.org"},
            actor="a",
            correlation_id="m2",
            device_trusted=True,
            device_id="desk",
        )
    )
    assert ok.ok and ok.result["opened"] == "https://x.org"


def test_risky_action_waits_for_approval_and_runs_only_with_the_matching_grant():
    gw, _, perms, bus = make(Policy(overrides={RiskLevel.P1: Decision.ASK}))
    calls = len(mocks.OPENED_URLS)
    kw = dict(actor="agent", correlation_id="m3", device_trusted=True, device_id="desk")
    inv = run(gw.invoke("mock.open_url", {"url": "https://a.org"}, **kw))
    assert inv.status is InvocationStatus.AWAITING_APPROVAL
    assert len(mocks.OPENED_URLS) == calls  # nothing executed
    assert [d.decision_id for d in perms.pending()] == [inv.decision_id]

    # Wrong args / other capability with the same decision -> denied, still not executed.
    run(perms.approve(inv.decision_id, CONFIRM))
    other = run(
        gw.invoke("mock.open_url", {"url": "https://evil.org"}, decision_id=inv.decision_id, **kw)
    )
    assert other.status is InvocationStatus.DENIED and other.rule == "grant:mismatch_or_inactive"
    echo = run(gw.invoke("mock.echo", {"text": "x"}, decision_id=inv.decision_id, **kw))
    assert echo.status is InvocationStatus.DENIED
    assert len(mocks.OPENED_URLS) == calls

    # Exact same call with the grant -> runs once; the grant is single-use.
    done = run(
        gw.invoke("mock.open_url", {"url": "https://a.org"}, decision_id=inv.decision_id, **kw)
    )
    assert done.ok and mocks.OPENED_URLS[-1] == "https://a.org"
    again = run(
        gw.invoke("mock.open_url", {"url": "https://a.org"}, decision_id=inv.decision_id, **kw)
    )
    assert again.status is InvocationStatus.DENIED
    assert len(mocks.OPENED_URLS) == calls + 1
    assert types(bus, "m3")[:2] == ["permission.ask", "capability.awaiting_approval"]
    assert "capability.succeeded" in types(bus, "m3")


def test_denied_action_cannot_be_bypassed():
    gw, reg, perms, bus = make(Policy(forbidden_actions=frozenset({"mock.echo"})))
    inv = run(gw.invoke("mock.echo", {"text": "x"}, actor="agent", correlation_id="m4"))
    assert inv.status is InvocationStatus.DENIED and inv.rule == "forbidden"
    with pytest.raises(ApprovalError):  # a denied decision can never be approved...
        run(perms.approve(inv.decision_id, STRONG))
    retry = run(
        gw.invoke(
            "mock.echo",
            {"text": "x"},
            decision_id=inv.decision_id,
            actor="agent",
            correlation_id="m4",
        )
    )
    assert retry.status is InvocationStatus.DENIED  # ...nor used as a grant
    ghost = run(
        gw.invoke(
            "mock.echo", {"text": "x"}, decision_id="ghost", actor="agent", correlation_id="m4"
        )
    )
    assert ghost.status is InvocationStatus.DENIED and ghost.rule == "grant:unknown"
    assert "capability.invoked" not in types(bus, "m4")
    assert reg.health()["mock.echo"]["invocations"] == 0


def test_invalid_inputs_and_unknown_capabilities_never_reach_permission_or_tool():
    gw, reg, _, bus = make()
    bad = run(gw.invoke("mock.echo", {"text": 1}, actor="a", correlation_id="m5"))
    assert bad.status is InvocationStatus.INVALID and "text" in (bad.error or "")
    unknown = run(gw.invoke("mock.nope", {}, actor="a", correlation_id="m5"))
    assert unknown.status is InvocationStatus.INVALID
    assert types(bus, "m5") == ["capability.invalid", "capability.invalid"]
    assert reg.health()["mock.echo"]["invocations"] == 0


# ---------------------------------------------------------------- gateway: execution semantics


def test_failure_timeout_retry_and_health():
    gw, reg, _, bus = make()
    state = {"calls": 0}

    def flaky(args):
        state["calls"] += 1
        if state["calls"] == 1:
            raise RuntimeError("first try fails")
        return {"ok": True}

    async def slow(args):
        await asyncio.sleep(1)

    def boom(args):
        raise ValueError("always")

    reg.register(
        CapabilityManifest(name="t.flaky", version="1", risk=RiskLevel.P0, retries=1), flaky
    )
    reg.register(
        CapabilityManifest(name="t.slow", version="1", risk=RiskLevel.P0, timeout_ms=20), slow
    )
    reg.register(CapabilityManifest(name="t.boom", version="1", risk=RiskLevel.P0), boom)

    ok = run(gw.invoke("t.flaky", actor="a", correlation_id="m6"))
    assert ok.ok and ok.attempts == 2 and ok.result == {"ok": True}

    slow_inv = run(gw.invoke("t.slow", actor="a", correlation_id="m6"))
    assert slow_inv.status is InvocationStatus.TIMEOUT and "timeout" in slow_inv.error
    assert "capability.timeout" in types(bus, "m6")

    for _ in range(3):
        failed = run(gw.invoke("t.boom", actor="a", correlation_id="m6"))
        assert failed.status is InvocationStatus.FAILED and "ValueError: always" == failed.error
    health = reg.health()
    assert health["t.boom"]["status"] == "failing" and health["t.boom"]["consecutive_failures"] == 3
    assert health["t.flaky"]["status"] == "ok" and health["t.slow"]["failures"] == 1
    assert ok.duration_ms is not None and ok.duration_ms >= 0


# ---------------------------------------------------------------- kill switch


def test_kill_switch_stops_everything_and_needs_strong_proof_to_resume():
    gw, _, _, bus = make()
    run(gw.halt("Jarvis, stop everything"))
    assert gw.halted
    inv = run(gw.invoke("mock.echo", {"text": "x"}, actor="a", correlation_id="m7"))
    assert inv.status is InvocationStatus.HALTED and inv.error == "Jarvis, stop everything"
    with pytest.raises(ApprovalError):
        run(gw.resume(CONFIRM))  # UI confirm is not strong enough
    with pytest.raises(ApprovalError):
        run(gw.resume(ApprovalProof(ProofMethod.PASSKEY, device_trusted=False)))
    assert gw.halted
    run(gw.resume(STRONG))
    assert not gw.halted
    assert run(gw.invoke("mock.echo", {"text": "x"}, actor="a", correlation_id="m7")).ok
    all_types = [e.type for _, e in bus.replay()]
    assert all_types[0] == "gateway.halted" and "gateway.resumed" in all_types
    halted_event = bus.replay()[0][1]
    assert halted_event.priority.value == "critical"
