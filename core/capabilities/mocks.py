"""Mock capabilities for Core 0.1 (SPEC §21 Phase 1 step 9): ``echo``, ``clock``, ``open_url``.

They exercise the full path registry -> permission -> gateway -> events without touching the OS.
``open_url`` only *records* the intent; a real browser adapter arrives with adapters/desktop.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from core.capabilities.manifest import CapabilityInputError, CapabilityManifest
from core.capabilities.registry import CapabilityRegistry
from core.permissions.model import RiskLevel

ECHO = CapabilityManifest(
    name="mock.echo",
    version="1.0",
    risk=RiskLevel.P0,
    inputs={"text": "string"},
    description="Returns the given text. Observe-level, no side effects.",
)

CLOCK = CapabilityManifest(
    name="mock.clock",
    version="1.0",
    risk=RiskLevel.P0,
    inputs={},
    description="Returns the current UTC time.",
)

OPEN_URL = CapabilityManifest(
    name="mock.open_url",
    version="1.0",
    risk=RiskLevel.P1,
    inputs={"url": "string"},
    requires=("device.trusted",),
    side_effects=True,
    reversible=True,
    verifier="mock.url_recorded",
    timeout_ms=2_000,
    description="Records the intent to open a URL (no real browser is launched).",
)

OPENED_URLS: list[str] = []


def echo(args: dict[str, Any]) -> dict[str, Any]:
    return {"text": args["text"]}


def clock(args: dict[str, Any]) -> dict[str, Any]:
    return {"now": datetime.now(UTC).isoformat()}


def open_url(args: dict[str, Any]) -> dict[str, Any]:
    parts = urlsplit(args["url"])
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise CapabilityInputError("url must be an absolute http(s) URL")
    OPENED_URLS.append(args["url"])
    return {"opened": args["url"], "mock": True}


def register_mocks(registry: CapabilityRegistry) -> CapabilityRegistry:
    registry.register(ECHO, echo)
    registry.register(CLOCK, clock)
    registry.register(OPEN_URL, open_url)
    return registry
