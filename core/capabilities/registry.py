"""Capability Registry (SPEC §5.1): every tool/skill with schema, risk, requirements and health."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from core.capabilities.manifest import CapabilityManifest

Handler = Callable[[dict[str, Any]], Any | Awaitable[Any]]


class CapabilityNotFound(KeyError):
    pass


class CapabilityConflict(ValueError):
    pass


@dataclass
class CapabilityHealth:
    status: str = "unknown"  # unknown | ok | failing
    invocations: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    last_error: str | None = None
    last_invoked_at: datetime | None = None

    def record(self, ok: bool, error: str | None = None) -> None:
        self.invocations += 1
        self.last_invoked_at = datetime.now(UTC)
        if ok:
            self.consecutive_failures = 0
            self.status = "ok"
        else:
            self.failures += 1
            self.consecutive_failures += 1
            self.last_error = error
            self.status = "failing" if self.consecutive_failures >= 3 else "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "invocations": self.invocations,
            "failures": self.failures,
            "consecutive_failures": self.consecutive_failures,
            "last_error": self.last_error,
            "last_invoked_at": self.last_invoked_at.isoformat() if self.last_invoked_at else None,
        }


@dataclass
class Capability:
    manifest: CapabilityManifest
    handler: Handler
    health: CapabilityHealth = field(default_factory=CapabilityHealth)

    @property
    def name(self) -> str:
        return self.manifest.name


class CapabilityRegistry:
    def __init__(self) -> None:
        self._caps: dict[str, Capability] = {}

    def register(self, manifest: CapabilityManifest, handler: Handler) -> Capability:
        if not callable(handler):
            raise TypeError("handler must be callable")
        if manifest.name in self._caps:
            raise CapabilityConflict(f"capability {manifest.name!r} is already registered")
        cap = Capability(manifest, handler)
        self._caps[manifest.name] = cap
        return cap

    def unregister(self, name: str) -> bool:
        return self._caps.pop(name, None) is not None

    def get(self, name: str) -> Capability:
        try:
            return self._caps[name]
        except KeyError:
            raise CapabilityNotFound(name) from None

    def __contains__(self, name: object) -> bool:
        return name in self._caps

    def names(self) -> list[str]:
        return sorted(self._caps)

    def manifests(self) -> list[dict[str, Any]]:
        return [self._caps[n].manifest.to_dict() for n in self.names()]

    def health(self) -> dict[str, dict[str, Any]]:
        return {n: self._caps[n].health.to_dict() for n in self.names()}
