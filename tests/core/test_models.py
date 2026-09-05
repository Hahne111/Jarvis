"""Tests for core.models (Commit 010): provider interface, router, budgets, Claude adapter shape."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from core.capabilities import mocks
from core.events.envelope import Sensitivity
from core.models import (
    AgentBudget,
    BudgetExceeded,
    BudgetTracker,
    ClaudeProvider,
    Message,
    MockProvider,
    ModelRouter,
    ModelSpec,
    NoEligibleModel,
    Path,
    ProviderError,
    ProviderResult,
    RoutingRequest,
    Tier,
    ToolCallProposal,
    ToolSpec,
    Usage,
    filter_tool_calls,
)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- provider-neutral types


def test_tool_spec_from_manifest_is_strict_json_schema():
    spec = ToolSpec.from_manifest(mocks.OPEN_URL)
    assert spec.name == "mock.open_url"
    assert spec.input_schema == {
        "type": "object",
        "properties": {"url": {"type": "string"}},
        "additionalProperties": False,
        "required": ["url"],
    }
    assert "P1" in spec.description and "side_effects=True" in spec.description
    assert "required" not in ToolSpec.from_manifest(mocks.CLOCK).input_schema


def test_message_validation_and_usage_math():
    with pytest.raises(ValueError):
        Message("system", "x")
    with pytest.raises(ValueError):
        Message("tool", "result")  # needs tool_call_id
    assert (Usage(1, 2, 3, 4) + Usage(10, 20, 30, 40)).to_dict() == {
        "input_tokens": 11,
        "output_tokens": 22,
        "cache_read_tokens": 33,
        "cache_write_tokens": 44,
    }
    assert Usage(5, 6).total_tokens == 11


def test_tool_call_allowlist_is_zero_trust():
    props = [ToolCallProposal("mock.echo", {}), ToolCallProposal("shell.exec", {"cmd": "rm"})]
    allowed, rejected = filter_tool_calls(props, {"mock.echo"})
    assert [p.name for p in allowed] == ["mock.echo"]
    assert [p.name for p in rejected] == ["shell.exec"]
    assert filter_tool_calls(props, set()) == ([], props)


# ---------------------------------------------------------------- mock provider


def test_mock_provider_default_scripted_and_exhausted():
    p = MockProvider()
    r = run(p.complete([Message("user", "hi")], model="m"))
    assert r.text == "[mock] hi" and r.model == "m" and p.calls[0]["effort"] == "high"

    scripted = MockProvider([ProviderResult("one"), ProviderResult("two")])
    assert run(scripted.complete([Message("user", "a")])).text == "one"
    assert run(scripted.complete([Message("user", "b")])).text == "two"
    with pytest.raises(ProviderError):
        run(scripted.complete([Message("user", "c")]))

    fn = MockProvider(lambda msgs: ProviderResult(text=msgs[-1].content.upper()))
    assert run(fn.complete([Message("user", "loud")])).text == "LOUD"
    assert fn.available()


# ---------------------------------------------------------------- router


def test_router_paths_pick_tier_and_effort():
    r = ModelRouter()
    deep = r.choose(RoutingRequest(Path.DEEP))
    assert deep.model.id == "claude-opus-5" and deep.effort == "xhigh"
    smart = r.choose(RoutingRequest(Path.SMART))
    assert smart.model.id == "claude-sonnet-5" and smart.effort == "medium"
    fast = r.choose(RoutingRequest(Path.FAST))
    assert fast.model.id == "claude-haiku-4-5" and fast.effort == "low"
    assert deep.to_dict()["path"] == "deep"


def test_router_privacy_offline_and_cost_constraints():
    r = ModelRouter()
    with pytest.raises(NoEligibleModel):  # secret data may not leave the house
        r.choose(RoutingRequest(Path.DEEP, sensitivity=Sensitivity.SECRET))
    with pytest.raises(NoEligibleModel):
        r.choose(RoutingRequest(Path.FAST, offline=True))
    r.add(ModelSpec("ollama:qwen3:8b", "ollama", Tier.LOCAL, local=True, supports_effort=False))
    with pytest.raises(ValueError):
        r.add(ModelSpec("ollama:qwen3:8b", "ollama", Tier.LOCAL, local=True))
    secret = r.choose(RoutingRequest(Path.DEEP, sensitivity=Sensitivity.SECRET))
    assert secret.model.local and secret.effort == "high" and "local-only (secret)" in secret.reason
    assert r.choose(RoutingRequest(Path.FAST)).model.id == "ollama:qwen3:8b"  # local first on FAST
    cheap = r.choose(
        RoutingRequest(
            Path.DEEP, max_cost_usd=0.02, expected_input_tokens=4000, expected_output_tokens=2000
        )
    )
    # opus: 4k*5 + 2k*25 = 0.07 > 0.02; sonnet: 0.008+0.02=0.028 > 0.02; haiku: 0.004+0.01=0.014 ok
    assert cheap.model.id == "ollama:qwen3:8b" or cheap.model.id == "claude-haiku-4-5"
    no_local = ModelRouter()
    assert (
        no_local.choose(RoutingRequest(Path.DEEP, max_cost_usd=0.02)).model.id == "claude-haiku-4-5"
    )
    assert ModelSpec("x", "p", Tier.SMALL, 1.0, 5.0).cost(Usage(1_000_000, 1_000_000)) == 6.0


# ---------------------------------------------------------------- budgets


def test_budget_tracker_enforces_every_dimension():
    clock = {"t": 0.0}
    tr = BudgetTracker(
        AgentBudget(
            max_seconds=10, max_tokens=100, max_cost_usd=1.0, max_tool_calls=2, max_steps=3
        ),
        clock=lambda: clock["t"],
    )
    tr.charge(Usage(40, 40), 0.5)
    tr.record_tool_call()
    tr.record_step()
    assert tr.to_dict()["usage"]["input_tokens"] == 40 and tr.cost_usd == 0.5
    with pytest.raises(BudgetExceeded) as e:
        tr.charge(Usage(30, 0))
    assert e.value.dimension == "tokens" and e.value.used == 110
    with pytest.raises(BudgetExceeded):
        tr.record_tool_call(5)
    clock["t"] = 11
    with pytest.raises(BudgetExceeded) as e2:
        tr.check()
    assert e2.value.dimension == "seconds"
    with pytest.raises(ValueError):
        AgentBudget(max_steps=0)
    unlimited = BudgetTracker(AgentBudget(None, None, None, None, None))
    unlimited.charge(Usage(10**9, 10**9), 10**6)  # no limits, no exception


# ---------------------------------------------------------------- claude adapter (no network)


class FakeMessagesAPI:
    def __init__(self, response, *, fail: Exception | None = None):
        self.response = response
        self.fail = fail
        self.kwargs = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        if self.fail:
            raise self.fail
        return self.response


def fake_client(response, *, fail=None):
    api = FakeMessagesAPI(response, fail=fail)
    return SimpleNamespace(beta=SimpleNamespace(messages=api), messages=api), api


def test_claude_request_shape_and_response_parsing():
    response = SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text="Opening it. "),
            SimpleNamespace(
                type="tool_use", id="toolu_1", name="mock.open_url", input={"url": "https://a.org"}
            ),
            SimpleNamespace(type="text", text="Done."),
        ],
        usage=SimpleNamespace(
            input_tokens=1000,
            output_tokens=200,
            cache_read_input_tokens=800,
            cache_creation_input_tokens=0,
        ),
        model="claude-opus-5",
        stop_reason="tool_use",
        stop_details=None,
    )
    client, api = fake_client(response)
    provider = ClaudeProvider(client=client)
    assert provider.available()
    result = run(
        provider.complete(
            [
                Message("user", "open a.org"),
                Message("assistant", "ok"),
                Message("tool", "opened", tool_call_id="toolu_0"),
            ],
            system="You are JARVIS.",
            tools=[ToolSpec.from_manifest(mocks.OPEN_URL)],
            effort="xhigh",
            max_tokens=4096,
        )
    )
    req = api.kwargs
    assert req["model"] == "claude-opus-5" and req["max_tokens"] == 4096
    assert req["thinking"] == {"type": "adaptive", "display": "omitted"}
    assert req["output_config"] == {"effort": "xhigh"}
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert req["tools"][0]["name"] == "mock.open_url" and req["tools"][0]["strict"] is True
    assert req["betas"] == ["server-side-fallback-2026-07-01"] and req["fallbacks"] == "default"
    assert req["messages"][2] == {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "toolu_0", "content": "opened"}],
    }
    assert "budget_tokens" not in str(req)

    assert result.text == "Opening it. Done."
    assert [t.to_dict() for t in result.tool_calls] == [
        {"call_id": "toolu_1", "name": "mock.open_url", "args": {"url": "https://a.org"}}
    ]
    assert result.usage.to_dict() == {
        "input_tokens": 1000,
        "output_tokens": 200,
        "cache_read_tokens": 800,
        "cache_write_tokens": 0,
    }
    assert result.stop_reason == "tool_use" and not result.refused
    assert result.cost_usd == pytest.approx((1000 * 5 + 200 * 25) / 1_000_000)


def test_claude_refusal_errors_and_validation():
    refusal = SimpleNamespace(
        content=[],
        usage=SimpleNamespace(input_tokens=1, output_tokens=0),
        model="claude-opus-5",
        stop_reason="refusal",
        stop_details=SimpleNamespace(category="cyber", explanation="no"),
    )
    client, api = fake_client(refusal)
    provider = ClaudeProvider(client=client, refusal_fallbacks=False)
    r = run(provider.complete([Message("user", "x")]))
    assert r.refused and r.refusal_category == "cyber" and r.text == ""
    assert "betas" not in api.kwargs and "fallbacks" not in api.kwargs

    failing, _ = fake_client(None, fail=RuntimeError("boom sk-ant-should-not-matter"))
    with pytest.raises(ProviderError):
        run(ClaudeProvider(client=failing).complete([Message("user", "x")]))
    with pytest.raises(ValueError):
        provider.build_request(
            [Message("user", "x")],
            system=None,
            tools=None,
            model=None,
            effort="ultra",
            max_tokens=10,
        )
    with pytest.raises(ValueError):
        provider.build_request(
            [Message("assistant", "x")],
            system=None,
            tools=None,
            model=None,
            effort="low",
            max_tokens=10,
        )


def test_claude_key_never_appears_in_request_and_key_provider_is_optional():
    client, api = fake_client(SimpleNamespace(content=[], usage=None, model=None, stop_reason=None))
    calls = {"n": 0}

    def broker():
        calls["n"] += 1
        return "sk-ant-secret"

    provider = ClaudeProvider(client=client, key_provider=broker)
    r = run(provider.complete([Message("user", "hi")], model="claude-sonnet-5"))
    assert calls["n"] == 0  # injected client -> broker never consulted
    assert (
        "sk-ant" not in str(api.kwargs)
        and r.model == "claude-sonnet-5"
        and r.stop_reason == "end_turn"
    )
