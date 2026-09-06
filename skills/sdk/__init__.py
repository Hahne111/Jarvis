"""JARVIS Skill SDK (SPEC §15, Phase 12 step 76).

A skill is a small Python module plus a ``manifest.json``. It never touches the OS, the network
or files: the only door to the world is ``SkillContext.call(capability, args)``, which goes
through the Core's Permission Engine -> Execution Gateway -> Verifier like every other action.
The reviewer (core/skills/review.py) enforces this statically before anything is installed.

    # skill.py
    from skills.sdk import Skill, SkillContext

    class HelloSkill(Skill):
        async def greet(self, ctx: SkillContext, args: dict) -> dict:
            return {"text": f"Hello, {args.get('name', 'world')}"}

        def handlers(self):
            return {"greet": self.greet}

Installed capabilities are named ``skill.<skill_name>.<capability>``; their risk level comes
from the manifest and can never be lower than P1 when the capability declares side effects.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SDK_VERSION = "1.0"
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,40}$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
RISKS = ("P0", "P1", "P2", "P3", "P4", "P5")


class ManifestError(ValueError):
    pass


@dataclass(frozen=True)
class SkillCapability:
    name: str
    description: str
    risk: str = "P1"
    inputs: dict[str, str] = field(default_factory=dict)
    side_effects: bool = False
    reversible: bool = True
    verifier: str | None = None  # name of a verifier the skill provides (required for side effects)
    requires: tuple[str, ...] = ()  # e.g. ("device.trusted",)
    timeout_ms: int = 10_000

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "risk": self.risk,
            "inputs": dict(self.inputs),
            "side_effects": self.side_effects,
            "reversible": self.reversible,
            "verifier": self.verifier,
            "requires": list(self.requires),
            "timeout_ms": self.timeout_ms,
        }


@dataclass(frozen=True)
class SkillManifest:
    name: str
    version: str
    description: str
    author: str
    entrypoint: str  # "skill.py:HelloSkill"
    capabilities: tuple[SkillCapability, ...]
    uses: tuple[str, ...] = ()  # core capabilities the skill may call through ctx.call
    tests: str = "tests"
    min_core: str = "0.5.0"
    sdk: str = SDK_VERSION

    def validate(self) -> None:
        if not NAME_RE.match(self.name):
            raise ManifestError("name must match ^[a-z][a-z0-9_]{2,40}$")
        if not SEMVER_RE.match(self.version):
            raise ManifestError("version must be semver MAJOR.MINOR.PATCH")
        if ":" not in self.entrypoint:
            raise ManifestError("entrypoint must be 'module.py:ClassName'")
        if not self.capabilities:
            raise ManifestError("a skill must declare at least one capability")
        seen = set()
        for c in self.capabilities:
            if not NAME_RE.match(c.name):
                raise ManifestError(f"capability name {c.name!r} is invalid")
            if c.name in seen:
                raise ManifestError(f"duplicate capability {c.name!r}")
            seen.add(c.name)
            if c.risk not in RISKS:
                raise ManifestError(
                    f"capability {c.name!r}: risk must be one of {RISKS} (P6 is never installable)"
                )
            if c.side_effects and c.risk == "P0":
                raise ManifestError(f"capability {c.name!r}: side effects can never be P0")
            if c.side_effects and not c.verifier:
                raise ManifestError(f"capability {c.name!r}: side effects need a verifier")
        for u in self.uses:
            if not re.match(r"^[a-z][a-z0-9_.]*$", u):
                raise ManifestError(f"uses entry {u!r} is not a capability name")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "entrypoint": self.entrypoint,
            "capabilities": [c.to_dict() for c in self.capabilities],
            "uses": list(self.uses),
            "tests": self.tests,
            "min_core": self.min_core,
            "sdk": self.sdk,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SkillManifest:
        try:
            caps = tuple(
                SkillCapability(
                    name=c["name"],
                    description=c.get("description", ""),
                    risk=str(c.get("risk", "P1")).upper(),
                    inputs=dict(c.get("inputs") or {}),
                    side_effects=bool(c.get("side_effects", False)),
                    reversible=bool(c.get("reversible", True)),
                    verifier=c.get("verifier"),
                    requires=tuple(c.get("requires") or ()),
                    timeout_ms=int(c.get("timeout_ms", 10_000)),
                )
                for c in d.get("capabilities", [])
            )
            m = cls(
                name=str(d["name"]),
                version=str(d["version"]),
                description=str(d.get("description", "")),
                author=str(d.get("author", "unknown")),
                entrypoint=str(d["entrypoint"]),
                capabilities=caps,
                uses=tuple(d.get("uses") or ()),
                tests=str(d.get("tests", "tests")),
                min_core=str(d.get("min_core", "0.5.0")),
                sdk=str(d.get("sdk", SDK_VERSION)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ManifestError(f"invalid manifest: {exc}") from None
        m.validate()
        return m


def load_manifest(path: Path | str) -> SkillManifest:
    p = Path(path)
    if p.is_dir():
        p = p / "manifest.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ManifestError(f"manifest not found: {p}") from None
    except json.JSONDecodeError as exc:
        raise ManifestError(f"manifest is not valid JSON: {exc}") from None
    return SkillManifest.from_dict(data)


# ---------------------------------------------------------------------------- runtime bridge

CallFn = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
LogFn = Callable[[str, dict[str, Any]], Awaitable[None]]


class SkillContext:
    """Everything a skill may do at runtime. Constructed by the Core, never by the skill."""

    def __init__(self, skill: str, *, call: CallFn, log: LogFn, allowed: tuple[str, ...]) -> None:
        self.skill = skill
        self._call = call
        self._log = log
        self._allowed = allowed

    async def call(self, capability: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """Invoke a core capability through the gateway (permission + verifier apply)."""
        if capability not in self._allowed:
            raise PermissionError(
                f"skill {self.skill!r} did not declare {capability!r} in manifest.uses"
            )
        return await self._call(capability, dict(args or {}))

    async def log(self, message: str, **data: Any) -> None:
        await self._log(message, data)


class Skill:
    """Base class. Override ``handlers`` (and ``verifiers`` for side-effecting capabilities)."""

    def handlers(
        self,
    ) -> dict[str, Callable[[SkillContext, dict[str, Any]], Awaitable[dict[str, Any]]]]:
        raise NotImplementedError

    def verifiers(
        self,
    ) -> dict[
        str, Callable[[dict[str, Any], dict[str, Any] | None], tuple[bool | None, dict[str, Any]]]
    ]:
        """name -> fn(args, result) -> (achieved | None for unknown, evidence)."""
        return {}


__all__ = [
    "NAME_RE",
    "SDK_VERSION",
    "ManifestError",
    "Skill",
    "SkillCapability",
    "SkillContext",
    "SkillManifest",
    "load_manifest",
]
