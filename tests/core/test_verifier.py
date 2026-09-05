"""Tests for core.verifier (Commit 008): tool-called != goal-achieved, retry policy, events."""

from __future__ import annotations

import asyncio

import pytest
from core.capabilities import (
    CapabilityManifest,
    CapabilityRegistry,
    ExecutionGateway,
    InvocationStatus,
    mocks,
    register_mocks,
)
from core.events import EventBus, SQLEventStore
from core.permissions import PermissionEngine, Policy, RiskLevel
from core.verifier import (
    Outcome,
    RetryPolicy,
    VerificationService,
    VerifiedExecutor,
    VerifierNotFound,
    VerifierRegistry,
    register_mock_verifiers,
)


def run(coro):
    return asyncio.run(coro)


def make(policy: Policy | None = None):
    bus = EventBus(SQLEventStore.in_memory())
    caps = register_mocks(CapabilityRegistry())
    verifiers = register_mock_verifiers(VerifierRegistry())
    gateway = ExecutionGateway(caps, PermissionEngine(bus, policy), bus)
    service = VerificationService(verifiers, caps, bus)
    executor = VerifiedExecutor(gateway, service, caps)
    return executor, gateway, service, caps, verifiers, bus


def types(bus: EventBus, cid: str) -> list[str]:
    return [e.type for _, e in bus.replay(correlation_id=cid)]


KW = dict(actor="agent", device_trusted=True, device_id="desk")


def test_registry_basics():
    reg = VerifierRegistry()
    reg.register("a.b", lambda inv: Outcome.ACHIEVED)
    assert "a.b" in reg and reg.names() == ["a.b"]
    with pytest.raises(ValueError):
        reg.register("a.b", lambda inv: Outcome.ACHIEVED)
    with pytest.raises(ValueError):
        reg.register(" ", lambda inv: Outcome.ACHIEVED)
    with pytest.raises(TypeError):
        reg.register("c.d", "nope")  # type: ignore[arg-type]
    with pytest.raises(VerifierNotFound):
        reg.get("zzz")


def test_mock_open_url_is_verified_end_to_end():
    executor, _, _, _, _, bus = make()
    res = run(executor.run("mock.open_url", {"url": "https://ok.org"}, correlation_id="v1", **KW))
    assert res.ok and res.attempts == 1
    assert res.verification.passed and res.verification.verifier == "mock.url_recorded"
    assert res.verification.evidence == {"url": "https://ok.org", "recorded": True}
    assert types(bus, "v1")[-2:] == ["capability.succeeded", "verification.passed"]


def test_tool_called_is_not_goal_achieved():
    executor, _, _, caps, verifiers, bus = make()
    # A capability that reports success but never changes the world.
    caps.register(
        CapabilityManifest(
            name="t.lie", version="1", risk=RiskLevel.P1, side_effects=True, verifier="t.check"
        ),
        lambda args: {"done": True},
    )
    verifiers.register("t.check", lambda inv: (Outcome.NOT_ACHIEVED, {"observed": None}))
    res = run(executor.run("t.lie", correlation_id="v2", **KW))
    assert res.invocation.status is InvocationStatus.SUCCEEDED  # the tool did return
    assert res.verification.outcome is Outcome.NOT_ACHIEVED
    assert not res.ok  # ...but nobody may call this success (Development Law 8)
    assert res.attempts == 1  # manifest.retries == 0 -> no blind retry
    assert types(bus, "v2")[-2:] == ["capability.succeeded", "verification.failed"]


def test_retry_until_verified_within_manifest_budget():
    executor, _, _, caps, verifiers, bus = make()
    world = {"calls": 0, "state": False}

    def act(args):
        world["calls"] += 1
        if world["calls"] >= 2:
            world["state"] = True  # only the second attempt really changes something
        return {"ok": True}

    caps.register(
        CapabilityManifest(
            name="t.flaky_world",
            version="1",
            risk=RiskLevel.P1,
            side_effects=True,
            verifier="t.state",
            retries=2,
        ),
        act,
    )
    verifiers.register(
        "t.state",
        lambda inv: (Outcome.ACHIEVED if world["state"] else Outcome.NOT_ACHIEVED, dict(world)),
    )
    res = run(executor.run("t.flaky_world", correlation_id="v3", **KW))
    assert res.ok and res.attempts == 2 and world["calls"] == 2
    t = types(bus, "v3")
    assert t.count("verification.failed") == 1 and t.count("verification.passed") == 1
    assert t.count("permission.allowed") == 2  # every attempt is a fresh, single-use grant


def test_budget_exhausted_reports_not_achieved():
    executor, _, _, caps, verifiers, _ = make()
    caps.register(
        CapabilityManifest(
            name="t.never",
            version="1",
            risk=RiskLevel.P1,
            side_effects=True,
            verifier="t.no",
            retries=1,
        ),
        lambda args: {"ok": True},
    )
    verifiers.register("t.no", lambda inv: False)
    res = run(executor.run("t.never", correlation_id="v4", **KW))
    assert not res.ok and res.attempts == 2 and res.verification.outcome is Outcome.NOT_ACHIEVED


def test_unknown_outcomes_never_count_as_success_and_are_not_retried():
    executor, _, _, caps, verifiers, bus = make()
    caps.register(
        CapabilityManifest(
            name="t.boomv",
            version="1",
            risk=RiskLevel.P1,
            side_effects=True,
            verifier="t.raise",
            retries=3,
        ),
        lambda args: {"ok": True},
    )
    caps.register(
        CapabilityManifest(
            name="t.slowv",
            version="1",
            risk=RiskLevel.P1,
            side_effects=True,
            verifier="t.slow",
            timeout_ms=20,
            retries=3,
        ),
        lambda args: {"ok": True},
    )
    caps.register(
        CapabilityManifest(
            name="t.missing",
            version="1",
            risk=RiskLevel.P1,
            side_effects=True,
            verifier="t.nobody",
            retries=3,
        ),
        lambda args: {"ok": True},
    )

    def raise_(inv):
        raise RuntimeError("verifier broke")

    async def slow(inv):
        await asyncio.sleep(1)
        return Outcome.ACHIEVED

    verifiers.register("t.raise", raise_)
    verifiers.register("t.slow", slow)

    for name, reason in (
        ("t.boomv", "verifier raised"),
        ("t.slowv", "timed out"),
        ("t.missing", "not registered"),
    ):
        res = run(executor.run(name, correlation_id="v5", **KW))
        assert res.invocation.ok and not res.ok
        assert res.verification.outcome is Outcome.UNKNOWN and reason in (
            res.verification.reason or ""
        )
        assert res.attempts == 1
    assert types(bus, "v5").count("verification.unknown") == 3


def test_skipped_when_nothing_to_verify_or_nothing_ran():
    executor, _, _, _, _, bus = make(Policy(forbidden_actions=frozenset({"mock.clock"})))
    echo = run(executor.run("mock.echo", {"text": "x"}, correlation_id="v6", **KW))
    assert echo.ok and echo.verification.outcome is Outcome.SKIPPED
    assert echo.verification.reason == "no side effects declared"
    denied = run(executor.run("mock.clock", correlation_id="v6", **KW))
    assert not denied.ok and denied.invocation.status is InvocationStatus.DENIED
    assert denied.verification.outcome is Outcome.SKIPPED and denied.attempts == 1
    assert types(bus, "v6").count("verification.skipped") == 2


def test_tool_failures_are_retried_by_the_gateway_only_and_awaiting_approval_is_not_retried():
    executor, _, _, caps, _, _ = make(Policy(overrides={RiskLevel.P1: "ask"}))
    calls = {"n": 0}

    def flaky(args):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("first")
        return {"ok": True}

    caps.register(
        CapabilityManifest(name="t.flaky", version="1", risk=RiskLevel.P0, retries=1), flaky
    )
    res = run(executor.run("t.flaky", correlation_id="v7", **KW))
    assert res.ok and res.attempts == 1  # one verified attempt...
    assert res.invocation.attempts == 2 and calls["n"] == 2  # ...the gateway retried inside it

    waiting = run(
        executor.run("mock.open_url", {"url": "https://a.org"}, correlation_id="v7", **KW)
    )
    assert waiting.invocation.status is InvocationStatus.AWAITING_APPROVAL
    assert waiting.attempts == 1 and waiting.verification.outcome is Outcome.SKIPPED


def test_retry_policy_table():
    from core.verifier.model import Verification

    p = RetryPolicy()
    inv_ok = type("I", (), {"status": InvocationStatus.SUCCEEDED})()
    inv_failed = type("I", (), {"status": InvocationStatus.FAILED})()
    inv_denied = type("I", (), {"status": InvocationStatus.DENIED})()
    v = lambda o: Verification("c", "i", o)  # noqa: E731
    assert p.should_retry(inv_ok, v(Outcome.NOT_ACHIEVED), 1, 1)
    assert not p.should_retry(inv_ok, v(Outcome.NOT_ACHIEVED), 2, 1)
    assert not p.should_retry(inv_ok, v(Outcome.UNKNOWN), 1, 5)
    assert not p.should_retry(inv_ok, v(Outcome.ACHIEVED), 1, 5)
    assert not p.should_retry(inv_failed, v(Outcome.SKIPPED), 1, 1)  # gateway already retried
    assert not p.should_retry(inv_denied, v(Outcome.SKIPPED), 1, 5)
    assert (
        RetryPolicy(max_extra_attempts=0).should_retry(inv_ok, v(Outcome.NOT_ACHIEVED), 1, 5)
        is False
    )


def test_mock_url_verifier_direct():
    mocks.OPENED_URLS.clear()
    from core.capabilities.gateway import Invocation
    from core.verifier.mocks import url_recorded

    inv = Invocation("mock.open_url", {"url": "https://z.org"}, "a", "c")
    assert url_recorded(inv)[0] is Outcome.NOT_ACHIEVED
    mocks.OPENED_URLS.append("https://z.org")
    assert url_recorded(inv) == (Outcome.ACHIEVED, {"url": "https://z.org", "recorded": True})
