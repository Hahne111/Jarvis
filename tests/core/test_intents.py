"""Tests for core.intents (Commit 009): deterministic fast-path routing, no model involved."""

from __future__ import annotations

import pytest
from core.capabilities import CapabilityRegistry, register_mocks
from core.intents import IntentRouter


@pytest.fixture
def router() -> IntentRouter:
    return IntentRouter(register_mocks(CapabilityRegistry()))


@pytest.mark.parametrize(
    ("text", "capability", "args"),
    [
        ("echo hello world", "mock.echo", {"text": "hello world"}),
        ("Sag Guten Morgen", "mock.echo", {"text": "Guten Morgen"}),
        ("clock", "mock.clock", {}),
        ("Wie spät ist es?", "mock.clock", {}),
        ("what time is it", "mock.clock", {}),
        ("open https://example.org/x?y=1", "mock.open_url", {"url": "https://example.org/x?y=1"}),
        ("öffne http://a.b", "mock.open_url", {"url": "http://a.b"}),
    ],
)
def test_fast_path_rules(router, text, capability, args):
    intent = router.route(text)
    assert intent.kind == "capability" and intent.capability == capability and intent.args == args
    assert intent.confidence == 1.0 and intent.text == text


@pytest.mark.parametrize("text", ["stop", "Jarvis, stop everything", "STOPP!", "halt", "abbruch"])
def test_stop_is_recognised(router, text):
    assert router.route(text).kind == "stop"


@pytest.mark.parametrize(
    "text", ["write me a game", "open the pod bay doors", "open notepad", "echo", "recherchiere X"]
)
def test_everything_else_goes_to_the_agent_path(router, text):
    intent = router.route(text)
    assert intent.kind == "agent" and intent.capability is None and intent.confidence == 0.0


def test_rules_only_route_to_registered_capabilities():
    router = IntentRouter(CapabilityRegistry())  # no mocks registered
    assert router.route("echo hi").kind == "agent"
    with pytest.raises(ValueError):
        router.route("  ")
