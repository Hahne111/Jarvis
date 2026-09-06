"""Skill lifecycle capabilities (SPEC §15.2: "Neuer Skill: installieren nach Review/Freigabe").

skill.list       P0
skill.review     P0   static review + sandbox tests, nothing installed
skill.install    P3   owner approval; review + tests + versioned install + activate (verifier)
skill.enable     P2   activate an installed version                                   (verifier)
skill.disable    P2   unregister the skill's capabilities                             (verifier)
skill.rollback   P2   activate the previous installed version                         (verifier)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.capabilities.gateway import Invocation, current_actor
from core.capabilities.manifest import CapabilityInputError, CapabilityManifest
from core.capabilities.registry import CapabilityRegistry
from core.permissions.model import RiskLevel
from core.skills.registry import SkillError, SkillRegistry
from core.verifier.model import Outcome
from core.verifier.service import VerifierRegistry

SKILL_MANIFESTS: tuple[CapabilityManifest, ...] = (
    CapabilityManifest(
        name="skill.list",
        version="1.0",
        risk=RiskLevel.P0,
        inputs={},
        description="Installed skills, their versions, active state and capabilities.",
    ),
    CapabilityManifest(
        name="skill.review",
        version="1.0",
        risk=RiskLevel.P0,
        inputs={"path": "string", "run_tests": "boolean?"},
        timeout_ms=180_000,
        description="Static security review of a skill folder (and its sandboxed tests).",
    ),
    CapabilityManifest(
        name="skill.install",
        version="1.0",
        risk=RiskLevel.P3,
        inputs={"path": "string", "skip_tests": "boolean?"},
        requires=("device.trusted",),
        side_effects=True,
        reversible=True,
        verifier="skill.active",
        timeout_ms=300_000,
        description="Review, test, install and activate a skill from a folder. Needs your ok.",
    ),
    CapabilityManifest(
        name="skill.enable",
        version="1.0",
        risk=RiskLevel.P2,
        inputs={"name": "string", "version": "string?"},
        side_effects=True,
        reversible=True,
        verifier="skill.active",
        description="Activate an installed skill (optionally a specific version).",
    ),
    CapabilityManifest(
        name="skill.disable",
        version="1.0",
        risk=RiskLevel.P2,
        inputs={"name": "string"},
        side_effects=True,
        reversible=True,
        verifier="skill.inactive",
        description="Deactivate a skill: its capabilities disappear from the registry.",
    ),
    CapabilityManifest(
        name="skill.rollback",
        version="1.0",
        risk=RiskLevel.P2,
        inputs={"name": "string"},
        side_effects=True,
        reversible=True,
        verifier="skill.active",
        description="Roll a skill back to its previous installed version.",
    ),
)


def register_skill_capabilities(
    registry: CapabilityRegistry, verifiers: VerifierRegistry, skills: SkillRegistry
) -> CapabilityRegistry:
    def guard(fn):
        async def wrapped(args: dict[str, Any]) -> dict[str, Any]:
            try:
                return await fn(args)
            except SkillError as exc:
                raise RuntimeError(
                    str(exc)
                ) from exc  # FAILED with the reason, never a fake success
            except FileNotFoundError as exc:
                raise CapabilityInputError(str(exc)) from exc

        return wrapped

    async def list_(args: dict[str, Any]) -> dict[str, Any]:
        items = skills.list()
        return {"skills": items, "count": len(items)}

    @guard
    async def review(args: dict[str, Any]) -> dict[str, Any]:
        path = _path(args["path"])
        report = skills.review(path)
        out = report.to_dict()
        if args.get("run_tests") and report.ok:
            tests = await skills.test(path, report)
            out["tests"] = {k: v for k, v in tests.items() if k != "stdout"}
        return out

    @guard
    async def install(args: dict[str, Any]) -> dict[str, Any]:
        return await skills.install(
            _path(args["path"]),
            approved_by=current_actor.get() or "owner",
            skip_tests=bool(args.get("skip_tests")),
        )

    @guard
    async def enable(args: dict[str, Any]) -> dict[str, Any]:
        return await skills.activate(str(args["name"]), args.get("version"))

    @guard
    async def disable(args: dict[str, Any]) -> dict[str, Any]:
        return await skills.deactivate(str(args["name"]))

    @guard
    async def rollback(args: dict[str, Any]) -> dict[str, Any]:
        return await skills.rollback(str(args["name"]))

    handlers = {
        "skill.list": list_,
        "skill.review": review,
        "skill.install": install,
        "skill.enable": enable,
        "skill.disable": disable,
        "skill.rollback": rollback,
    }
    for m in SKILL_MANIFESTS:
        registry.register(m, handlers[m.name])

    def active(inv: Invocation) -> tuple[Outcome, dict[str, Any]]:
        result = inv.result or {}
        name = str(result.get("skill") or inv.args.get("name") or "")
        wanted = result.get("version") or result.get("to")
        current = skills.active_version(name)
        ok = (
            current is not None
            and (wanted is None or current == wanted)
            and any(c.startswith(f"skill.{name}.") for c in registry.names())
        )
        return (Outcome.ACHIEVED if ok else Outcome.NOT_ACHIEVED), {
            "skill": name,
            "active": current,
            "wanted": wanted,
        }

    def inactive(inv: Invocation) -> tuple[Outcome, dict[str, Any]]:
        name = str(inv.args.get("name") or "")
        gone = not any(c.startswith(f"skill.{name}.") for c in registry.names())
        return (Outcome.ACHIEVED if gone else Outcome.NOT_ACHIEVED), {"skill": name}

    verifiers.register("skill.active", active)
    verifiers.register("skill.inactive", inactive)
    return registry


def _path(raw: str) -> Path:
    p = Path(str(raw)).expanduser().resolve()
    if not p.is_dir():
        raise FileNotFoundError(f"skill folder not found: {raw}")
    return p
