"""IntelligenceProvider interface (SPEC §6.2, ADR-0002 §3).

A provider turns messages (+ tool specs) into text and *proposed* tool calls. It never executes
anything: proposals go through PermissionEngine -> ExecutionGateway -> Verifier like every other
side effect (Development Law 2/3). No provider-specific logic lives outside core/models/.
"""

from __future__ import annotations

import abc
import uuid
from dataclasses import dataclass, field
from typing import Any

from core.capabilities.manifest import CapabilityManifest

_JSON_TYPES = {
    "string": "string",
    "integer": "integer",
    "number": "number",
    "boolean": "boolean",
    "object": "object",
    "array": "array",
}


class ProviderError(RuntimeError):
    """The provider could not produce a result (network, auth, malformed response)."""


class ProviderUnavailable(ProviderError):
    """The provider is not configured (missing SDK or credentials)."""


@dataclass(frozen=True)
class Message:
    role: str  # "user" | "assistant" | "tool"
    content: str
    tool_call_id: str | None = None  # for role == "tool"
    name: str | None = None

    def __post_init__(self) -> None:
        if self.role not in ("user", "assistant", "tool"):
            raise ValueError(f"invalid role {self.role!r}")
        if not isinstance(self.content, str):
            raise ValueError("content must be a string")
        if self.role == "tool" and not self.tool_call_id:
            raise ValueError("tool messages need a tool_call_id")


@dataclass(frozen=True)
class ToolSpec:
    """Provider-neutral tool description, derived from a Capability Manifest."""

    name: str
    description: str
    input_schema: dict[str, Any]

    @classmethod
    def from_manifest(cls, manifest: CapabilityManifest) -> ToolSpec:
        props: dict[str, Any] = {}
        required: list[str] = []
        for arg, spec in manifest.inputs.items():
            optional = spec.endswith("?")
            props[arg] = {"type": _JSON_TYPES[spec.rstrip("?")]}
            if not optional:
                required.append(arg)
        schema: dict[str, Any] = {
            "type": "object",
            "properties": props,
            "additionalProperties": False,
        }
        if required:
            schema["required"] = required
        desc = manifest.description or manifest.name
        return cls(
            name=manifest.name,
            description=f"{desc} (risk {manifest.risk.name}, side_effects={manifest.side_effects})",
            input_schema=schema,
        )


@dataclass(frozen=True)
class ToolCallProposal:
    name: str
    args: dict[str, Any]
    call_id: str = field(default_factory=lambda: f"call_{uuid.uuid4().hex[:12]}")

    def to_dict(self) -> dict[str, Any]:
        return {"call_id": self.call_id, "name": self.name, "args": self.args}


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
        }


@dataclass(frozen=True)
class ProviderResult:
    text: str
    tool_calls: tuple[ToolCallProposal, ...] = ()
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    stop_reason: str = "end_turn"
    refused: bool = False
    refusal_category: str | None = None
    cost_usd: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "tool_calls": [t.to_dict() for t in self.tool_calls],
            "usage": self.usage.to_dict(),
            "model": self.model,
            "stop_reason": self.stop_reason,
            "refused": self.refused,
            "refusal_category": self.refusal_category,
            "cost_usd": round(self.cost_usd, 6),
        }


class IntelligenceProvider(abc.ABC):
    name: str = "abstract"

    @abc.abstractmethod
    async def complete(
        self,
        messages: list[Message],
        *,
        system: str | None = None,
        tools: list[ToolSpec] | None = None,
        model: str | None = None,
        effort: str = "high",
        max_tokens: int = 16000,
    ) -> ProviderResult: ...

    @abc.abstractmethod
    def available(self) -> bool: ...


def filter_tool_calls(
    proposals: tuple[ToolCallProposal, ...] | list[ToolCallProposal],
    allowlist: set[str] | frozenset[str],
) -> tuple[list[ToolCallProposal], list[ToolCallProposal]]:
    """Split proposals into (allowed, rejected) by an explicit per-mission allowlist.

    Zero-trust tools: nothing outside the allowlist ever reaches the gateway (SPEC §7, #161).
    """
    allowed = [p for p in proposals if p.name in allowlist]
    rejected = [p for p in proposals if p.name not in allowlist]
    return allowed, rejected
