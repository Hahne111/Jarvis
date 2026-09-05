"""Verifier for the mock capabilities: ``mock.url_recorded`` checks OPENED_URLS independently."""

from __future__ import annotations

from core.capabilities import mocks
from core.capabilities.gateway import Invocation
from core.verifier.model import Outcome
from core.verifier.service import VerifierRegistry


def url_recorded(invocation: Invocation) -> tuple[Outcome, dict]:
    url = invocation.args.get("url")
    recorded = url in mocks.OPENED_URLS
    return (Outcome.ACHIEVED if recorded else Outcome.NOT_ACHIEVED), {
        "url": url,
        "recorded": recorded,
    }


def register_mock_verifiers(registry: VerifierRegistry) -> VerifierRegistry:
    registry.register("mock.url_recorded", url_recorded)
    return registry
