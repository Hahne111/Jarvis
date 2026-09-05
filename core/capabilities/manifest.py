"""Capability Manifest (SPEC §17.2).

name: computer.open_app      inputs: {app_id: string}   side_effects: true
version: 1.0                 requires: [device.trusted]  reversible: false
risk: P1                     verifier: computer.process_running   timeout_ms: 10000
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from core.permissions.model import RiskLevel

_NAME = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "object": (dict,),
    "array": (list,),
}
KNOWN_REQUIREMENTS = frozenset({"device.trusted", "network.online", "owner.present"})


class CapabilityInputError(ValueError):
    """Arguments do not match the manifest's input schema."""


@dataclass(frozen=True)
class CapabilityManifest:
    name: str
    version: str
    risk: RiskLevel
    inputs: dict[str, str] = field(default_factory=dict)  # field -> "string" | "integer?" ...
    requires: tuple[str, ...] = ()
    side_effects: bool = False
    reversible: bool = True
    verifier: str | None = None
    timeout_ms: int = 10_000
    retries: int = 0  # extra attempts on failure/timeout (Phase 2 step 13)
    description: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _NAME.match(self.name):
            raise ValueError(f"invalid capability name {self.name!r} (expected 'domain.action')")
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("version must be a non-empty string")
        object.__setattr__(self, "risk", RiskLevel(self.risk))
        object.__setattr__(self, "requires", tuple(self.requires))
        for req in self.requires:
            if req not in KNOWN_REQUIREMENTS:
                raise ValueError(
                    f"unknown requirement {req!r}; known: {sorted(KNOWN_REQUIREMENTS)}"
                )
        for name, spec in self.inputs.items():
            if spec.rstrip("?") not in _TYPES:
                raise ValueError(f"input {name!r}: unknown type {spec!r}")
        if isinstance(self.timeout_ms, bool) or self.timeout_ms <= 0:
            raise ValueError("timeout_ms must be a positive int")
        if isinstance(self.retries, bool) or self.retries < 0:
            raise ValueError("retries must be >= 0")
        if self.side_effects and self.risk is RiskLevel.P0:
            raise ValueError("a capability with side effects cannot be P0 (observe)")
        if self.side_effects and self.verifier is None:
            raise ValueError("every side-effecting capability needs a verifier (Development Law 4)")

    def validate_inputs(self, args: dict[str, Any]) -> dict[str, Any]:
        """Return a normalised copy of ``args`` or raise CapabilityInputError."""
        if not isinstance(args, dict):
            raise CapabilityInputError("arguments must be an object")
        unknown = sorted(set(args) - set(self.inputs))
        if unknown:
            raise CapabilityInputError(f"unknown arguments: {unknown}")
        out: dict[str, Any] = {}
        for name, spec in self.inputs.items():
            optional = spec.endswith("?")
            expected = _TYPES[spec.rstrip("?")]
            if name not in args or args[name] is None:
                if optional:
                    continue
                raise CapabilityInputError(f"missing required argument {name!r}")
            value = args[name]
            if isinstance(value, bool) and bool not in expected:
                raise CapabilityInputError(f"argument {name!r} must be {spec}")
            if not isinstance(value, expected):
                raise CapabilityInputError(f"argument {name!r} must be {spec}")
            out[name] = value
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "risk": self.risk.name,
            "inputs": dict(self.inputs),
            "requires": list(self.requires),
            "side_effects": self.side_effects,
            "reversible": self.reversible,
            "verifier": self.verifier,
            "timeout_ms": self.timeout_ms,
            "retries": self.retries,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CapabilityManifest:
        risk = d["risk"]
        risk = RiskLevel[risk] if isinstance(risk, str) else RiskLevel(int(risk))
        return cls(
            name=d["name"],
            version=str(d["version"]),
            risk=risk,
            inputs=dict(d.get("inputs") or {}),
            requires=tuple(d.get("requires") or ()),
            side_effects=bool(d.get("side_effects", False)),
            reversible=bool(d.get("reversible", True)),
            verifier=d.get("verifier"),
            timeout_ms=int(d.get("timeout_ms", 10_000)),
            retries=int(d.get("retries", 0)),
            description=str(d.get("description", "")),
        )
