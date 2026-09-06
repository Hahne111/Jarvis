"""Versioned skill registry with sandbox tests, activation and rollback (Phase 12 step 77).

Install pipeline (SPEC §15.1): review (static) -> sandbox tests (workspace runner: CPU/time
limits, scrubbed env) -> copy into ``<root>/<name>/<version>/`` -> activate (import the module,
register ``skill.<name>.<cap>`` capabilities with the manifest's risk, verifiers included).
State (active versions) lives in ``<root>/registry.json``; every step is an event. Rollback
re-activates the previous installed version. Only the ``skill.install`` capability (P3, owner
approval) calls ``install``; nothing in here is reachable for a model directly.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from skills.sdk import Skill, SkillContext, SkillManifest, load_manifest

from core.capabilities.gateway import (
    Invocation,
    current_correlation_id,
    current_device_id,
    current_user_id,
)
from core.capabilities.manifest import CapabilityManifest
from core.capabilities.registry import CapabilityRegistry
from core.events.bus import EventBus
from core.events.envelope import Event
from core.permissions.model import RiskLevel
from core.skills.review import ReviewReport, SkillReviewer
from core.verifier.model import Outcome
from core.verifier.service import VerifierRegistry

SOURCE = "skills"
SDK_SRC = Path(__file__).resolve().parents[2] / "skills" / "sdk" / "__init__.py"

RunTests = Callable[[Path, str], Awaitable[dict[str, Any]]]


class SkillError(RuntimeError):
    pass


class SkillRegistry:
    def __init__(
        self,
        root: Path | str,
        capabilities: CapabilityRegistry,
        verifiers: VerifierRegistry,
        bus: EventBus,
        *,
        call: Callable[..., Awaitable[Any]] | None = None,
        run_tests: RunTests | None = None,
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._caps = capabilities
        self._verifiers = verifiers
        self._bus = bus
        self._call = call  # executor.run(name, args, **kw) - the skill's only door to the world
        self._run_tests = run_tests
        self.reviewer = SkillReviewer()
        self._active: dict[str, dict[str, Any]] = {}  # name -> {version, instance, caps}
        self._state = self._load_state()

    # -- state -------------------------------------------------------------------------------------

    def _state_file(self) -> Path:
        return self.root / "registry.json"

    def _load_state(self) -> dict[str, Any]:
        try:
            return json.loads(self._state_file().read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {"skills": {}}

    def _save_state(self) -> None:
        self._state_file().write_text(
            json.dumps(self._state, indent=2, sort_keys=True), encoding="utf-8"
        )

    def installed(self, name: str) -> list[str]:
        d = self.root / name
        if not d.is_dir():
            return []
        return sorted(
            (p.name for p in d.iterdir() if p.is_dir() and (p / "manifest.json").is_file()),
            key=_semver_key,
        )

    def active_version(self, name: str) -> str | None:
        return self._state["skills"].get(name, {}).get("active")

    def list(self) -> list[dict[str, Any]]:
        out = []
        for name in sorted(
            {*self._state["skills"], *(p.name for p in self.root.iterdir() if p.is_dir())}
        ):
            versions = self.installed(name)
            if not versions:
                continue
            active = self.active_version(name)
            out.append(
                {
                    "name": name,
                    "active": active,
                    "enabled": active is not None and name in self._active,
                    "versions": versions,
                    "capabilities": [
                        c for c in self._caps.names() if c.startswith(f"skill.{name}.")
                    ],
                    "history": self._state["skills"].get(name, {}).get("history", [])[-5:],
                }
            )
        return out

    # -- pipeline ----------------------------------------------------------------------------------

    def review(self, path: Path | str) -> ReviewReport:
        return self.reviewer.review(path)

    async def test(self, path: Path | str, report: ReviewReport | None = None) -> dict[str, Any]:
        """Run the skill's own tests in the sandboxed workspace runner (never in-process)."""
        report = report or self.review(path)
        if not report.ok or report.manifest is None:
            return {"ok": False, "skipped": "review failed"}
        if self._run_tests is None:
            return {"ok": False, "skipped": "no sandbox runner configured"}
        result = await self._run_tests(Path(path), report.manifest.name)
        return result

    async def install(
        self, path: Path | str, *, approved_by: str = "owner", skip_tests: bool = False
    ) -> dict[str, Any]:
        src = Path(path).resolve()  # noqa: ASYNC240 - install is rare, short, owner-approved
        report = self.review(src)
        await self._emit("skill.reviewed", report.to_dict())
        if not report.ok or report.manifest is None:
            await self._emit("skill.rejected", {**report.to_dict(), "stage": "review"})
            raise SkillError("review failed: " + "; ".join(f.message for f in report.findings[:3]))
        m = report.manifest
        tests: dict[str, Any] = (
            {"ok": True, "skipped": "tests skipped by owner"}
            if skip_tests
            else await self.test(src, report)
        )
        await self._emit(
            "skill.tested",
            {
                "skill": m.name,
                "version": m.version,
                **{k: v for k, v in tests.items() if k != "stdout"},
            },
        )
        if not tests.get("ok"):
            await self._emit(
                "skill.rejected",
                {
                    "skill": m.name,
                    "version": m.version,
                    "stage": "tests",
                    **{k: v for k, v in tests.items() if k in ("exit_code", "skipped", "stderr")},
                },
            )
            raise SkillError(f"skill tests failed (exit {tests.get('exit_code')})")
        dest = self.root / m.name / m.version
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(
            src, dest, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc")
        )
        previous = self.active_version(m.name)
        entry = self._state["skills"].setdefault(m.name, {"active": None, "history": []})
        entry["history"].append(
            {
                "at": datetime.now(UTC).isoformat(),
                "version": m.version,
                "sha256": report.sha256,
                "by": approved_by,
                "previous": previous,
            }
        )
        self._save_state()
        await self.activate(m.name, m.version)
        await self._emit(
            "skill.installed",
            {
                "skill": m.name,
                "version": m.version,
                "sha256": report.sha256,
                "previous": previous,
                "by": approved_by,
                "capabilities": [f"skill.{m.name}.{c.name}" for c in m.capabilities],
            },
        )
        return {
            "skill": m.name,
            "version": m.version,
            "sha256": report.sha256,
            "previous": previous,
            "review": report.to_dict(),
            "tests": {k: v for k, v in tests.items() if k != "stdout"},
        }

    async def activate(self, name: str, version: str | None = None) -> dict[str, Any]:
        version = version or self.active_version(name) or (self.installed(name) or [None])[-1]
        if version is None:
            raise SkillError(f"skill {name!r} is not installed")
        skill_dir = self.root / name / version
        manifest = load_manifest(skill_dir)
        if name in self._active:
            self._unregister(name)
        instance = self._load_instance(skill_dir, manifest)
        handlers = instance.handlers()
        verifiers = instance.verifiers()
        registered = []
        registered_verifiers = []
        for cap in manifest.capabilities:
            if cap.name not in handlers:
                raise SkillError(f"skill {name!r} declares {cap.name!r} but provides no handler")
            risk = RiskLevel[cap.risk]
            if cap.side_effects and risk is RiskLevel.P0:
                risk = RiskLevel.P1  # belt and braces: the manifest validator already forbids it
            full = f"skill.{name}.{cap.name}"
            verifier_name = f"skill.{name}.{cap.verifier}" if cap.verifier else None
            self._caps.register(
                CapabilityManifest(
                    name=full,
                    version=manifest.version,
                    risk=risk,
                    inputs=dict(cap.inputs),
                    requires=tuple(cap.requires),
                    side_effects=cap.side_effects,
                    reversible=cap.reversible,
                    verifier=verifier_name,
                    timeout_ms=cap.timeout_ms,
                    description=f"[skill {name} {manifest.version}] {cap.description}",
                ),
                self._wrap_handler(name, manifest, handlers[cap.name]),
            )
            if verifier_name:
                if cap.verifier not in verifiers:
                    self._caps.unregister(full)
                    raise SkillError(f"skill {name!r}: verifier {cap.verifier!r} missing")
                self._verifiers.unregister(verifier_name)  # stale from a previous version
                self._verifiers.register(
                    verifier_name, self._wrap_verifier(verifiers[cap.verifier])
                )
                registered_verifiers.append(verifier_name)
            registered.append(full)
        self._active[name] = {
            "version": version,
            "instance": instance,
            "caps": registered,
            "verifiers": registered_verifiers,
        }
        self._state["skills"].setdefault(name, {"active": None, "history": []})["active"] = version
        self._save_state()
        await self._emit(
            "skill.enabled", {"skill": name, "version": version, "capabilities": registered}
        )
        return {"skill": name, "version": version, "capabilities": registered}

    async def deactivate(self, name: str) -> dict[str, Any]:
        if name not in self._active:
            raise SkillError(f"skill {name!r} is not active")
        version = self._active[name]["version"]
        self._unregister(name)
        self._state["skills"][name]["active"] = None
        self._save_state()
        await self._emit("skill.disabled", {"skill": name, "version": version})
        return {"skill": name, "version": version, "enabled": False}

    async def rollback(self, name: str) -> dict[str, Any]:
        versions = self.installed(name)
        current = self.active_version(name) or (versions[-1] if versions else None)
        candidates = [
            v for v in versions if current is None or _semver_key(v) < _semver_key(current)
        ]
        if not candidates:
            raise SkillError(f"skill {name!r} has no previous version to roll back to")
        target = candidates[-1]
        await self.activate(name, target)
        await self._emit("skill.rolled_back", {"skill": name, "from": current, "to": target})
        return {"skill": name, "from": current, "to": target}

    def restore(self) -> list[str]:
        """On start: re-activate what was active before (state file), skipping broken ones."""
        out = []
        for name, entry in list(self._state["skills"].items()):
            v = entry.get("active")
            if v and (self.root / name / v / "manifest.json").is_file():
                try:
                    import asyncio

                    asyncio.run(self.activate(name, v)) if not _in_loop() else None
                    out.append(name)
                except Exception:  # a broken skill must not stop the Core
                    entry["active"] = None
        self._save_state()
        return out

    # -- internals ---------------------------------------------------------------------------------

    def _unregister(self, name: str) -> None:
        for cap in self._active.get(name, {}).get("caps", []):
            self._caps.unregister(cap)
        for ver in self._active.get(name, {}).get("verifiers", []):
            self._verifiers.unregister(ver)
        self._active.pop(name, None)

    def _load_instance(self, skill_dir: Path, manifest: SkillManifest) -> Skill:
        module_file, _, class_name = manifest.entrypoint.partition(":")
        mod_name = f"jarvis_skill_{manifest.name}_{manifest.version.replace('.', '_')}"
        spec = importlib.util.spec_from_file_location(mod_name, skill_dir / module_file)
        if spec is None or spec.loader is None:
            raise SkillError(f"cannot load {module_file}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)
        cls = getattr(module, class_name, None)
        if cls is None or not issubclass(cls, Skill):
            raise SkillError(f"{class_name} is not a Skill subclass")
        return cls()

    def _context(self, name: str, manifest: SkillManifest) -> SkillContext:
        async def call(capability: str, args: dict[str, Any]) -> dict[str, Any]:
            if self._call is None:
                raise PermissionError("skill calls are not wired")
            res = await self._call(
                capability,
                args,
                actor=f"skill:{name}",
                correlation_id=current_correlation_id.get() or f"skill:{name}",
                user_id=current_user_id.get(),
                device_id=current_device_id.get(),
                device_trusted=False,  # a skill is never a trusted device
            )
            inv = res.invocation
            if not inv.ok:
                raise PermissionError(f"{capability}: {inv.status.value} {inv.error or ''}".strip())
            return dict(inv.result or {})

        async def log(message: str, data: dict[str, Any]) -> None:
            await self._emit(
                "skill.log",
                {
                    "skill": name,
                    "message": message[:200],
                    **{k: str(v)[:200] for k, v in data.items()},
                },
            )

        return SkillContext(name, call=call, log=log, allowed=tuple(manifest.uses))

    def _wrap_handler(self, name: str, manifest: SkillManifest, fn: Any) -> Any:
        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            ctx = self._context(name, manifest)
            result = await fn(ctx, dict(args))
            return dict(result or {})

        return handler

    @staticmethod
    def _wrap_verifier(fn: Any) -> Any:
        def verifier(inv: Invocation) -> tuple[Outcome, dict[str, Any]]:
            achieved, evidence = fn(dict(inv.args), dict(inv.result or {}))
            if achieved is None:
                return Outcome.UNKNOWN, dict(evidence)
            return (Outcome.ACHIEVED if achieved else Outcome.NOT_ACHIEVED), dict(evidence)

        return verifier

    async def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        await self._bus.publish(Event.new(event_type, SOURCE, payload, correlation_id="skills"))


def _semver_key(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in v.split("."))
    except ValueError:
        return (0,)


def _in_loop() -> bool:
    import asyncio

    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def make_sandbox_runner(workspaces: Any) -> RunTests:
    """Sandbox tests via the WorkspaceManager: skill + SDK copied into an isolated workspace,
    ``python -m pytest`` with CPU/time limits and a scrubbed environment."""

    async def run_tests(src: Path, name: str) -> dict[str, Any]:
        wsid = f"skill-review-{name}"
        ws = workspaces.workspace(wsid)
        for child in ws.iterdir():
            if child.name == ".jarvis":
                continue
            shutil.rmtree(child) if child.is_dir() else child.unlink()
        shutil.copytree(
            src,
            ws / "skill_under_test",
            ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"),
        )
        sdk_dir = ws / "skills" / "sdk"
        sdk_dir.mkdir(parents=True, exist_ok=True)
        (ws / "skills" / "__init__.py").write_text("", encoding="utf-8")
        shutil.copy2(SDK_SRC, sdk_dir / "__init__.py")
        (ws / "conftest.py").write_text(
            "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parent))\n"
            "sys.path.insert(0, str(pathlib.Path(__file__).parent / 'skill_under_test'))\n",
            encoding="utf-8",
        )
        r = await workspaces.run(
            wsid,
            "python",
            ["-m", "pytest", "-q", "-p", "no:cacheprovider", "skill_under_test"],
            timeout_s=120,
        )
        return {
            "ok": r.exit_code == 0,
            "exit_code": r.exit_code,
            "timed_out": r.timed_out,
            "duration_ms": r.duration_ms,
            "stdout": r.stdout[-2000:],
            "stderr": r.stderr[-1000:],
        }

    return run_tests
