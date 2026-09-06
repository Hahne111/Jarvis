"""Tests for the Skill Factory (Phase 12 steps 76/77): SDK, review, sandbox, install, rollback."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest
from core.api import create_app
from core.capabilities import InvocationStatus
from core.permissions import ApprovalProof, ProofMethod
from core.runtime import CoreRuntime
from core.skills import SkillReviewer
from core.verifier import Outcome
from fastapi.testclient import TestClient
from skills.sdk import ManifestError, load_manifest

EXAMPLE = Path(__file__).resolve().parents[2] / "skills" / "examples" / "hello_world"
CONFIRM = ApprovalProof(ProofMethod.UI_CONFIRM, device_id="desk", device_trusted=True)
KW = dict(actor="owner", correlation_id="m1", device_trusted=True, device_id="desk")


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def rt(tmp_path):
    return CoreRuntime.build(
        f"sqlite:///{tmp_path / 'sk.db'}",
        provider="none",
        workspace_root=str(tmp_path / "ws"),
        skills_root=str(tmp_path / "skills"),
    )


def copy_skill(tmp_path, name="hello_world", version=None, patch=None) -> Path:
    dst = tmp_path / "src" / f"{name}-{version or 'x'}"
    shutil.copytree(EXAMPLE, dst, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    m = json.loads((dst / "manifest.json").read_text())
    if version:
        m["version"] = version
    if patch:
        patch(dst, m)
    (dst / "manifest.json").write_text(json.dumps(m, indent=2))
    return dst


def install(rt, path):
    waiting = run(rt.executor.run("skill.install", {"path": str(path)}, **KW))
    assert waiting.invocation.status is InvocationStatus.AWAITING_APPROVAL
    run(rt.permissions.approve(waiting.invocation.decision_id, CONFIRM))
    return run(
        rt.executor.run(
            "skill.install", {"path": str(path)}, decision_id=waiting.invocation.decision_id, **KW
        )
    )


# ---------------------------------------------------------------- sdk / review


def test_manifest_validation_rejects_unsafe_declarations(tmp_path):
    m = load_manifest(EXAMPLE)
    assert m.name == "hello_world" and {c.name for c in m.capabilities} == {
        "greet",
        "count",
        "time_greeting",
    }
    bad = json.loads((EXAMPLE / "manifest.json").read_text())
    bad["capabilities"][0]["side_effects"] = True  # P0 with side effects
    with pytest.raises(ManifestError, match="never be P0"):
        load_manifest_dict(bad)
    bad = json.loads((EXAMPLE / "manifest.json").read_text())
    bad["capabilities"][1]["verifier"] = None
    with pytest.raises(ManifestError, match="need a verifier"):
        load_manifest_dict(bad)
    bad = json.loads((EXAMPLE / "manifest.json").read_text())
    bad["capabilities"][1]["risk"] = "P6"
    with pytest.raises(ManifestError, match="P6"):
        load_manifest_dict(bad)
    bad = json.loads((EXAMPLE / "manifest.json").read_text())
    bad["name"] = "Hello World"
    with pytest.raises(ManifestError):
        load_manifest_dict(bad)


def load_manifest_dict(d):
    from skills.sdk import SkillManifest

    return SkillManifest.from_dict(d)


def test_static_review_rejects_core_bypass_attempts(tmp_path):
    reviewer = SkillReviewer()
    good = reviewer.review(EXAMPLE)
    assert good.ok and good.sha256 and "skill.py" in good.files, good.to_dict()

    def sneaky_os(dst, m):
        (dst / "skill.py").write_text(
            (dst / "skill.py")
            .read_text()
            .replace(
                "from typing import Any", "from typing import Any\nimport os\nimport subprocess"
            )
        )

    r = reviewer.review(copy_skill(tmp_path, version="1.0.1", patch=sneaky_os))
    rules = {(f.rule, f.message.split("'")[1]) for f in r.findings if f.rule == "import"}
    assert not r.ok and ("import", "os") in rules and ("import", "subprocess") in rules

    def sneaky_core(dst, m):
        (dst / "skill.py").write_text(
            (dst / "skill.py")
            .read_text()
            .replace(
                "from typing import Any",
                "from typing import Any\nfrom core.permissions import Policy",
            )
        )

    assert not reviewer.review(copy_skill(tmp_path, version="1.0.2", patch=sneaky_core)).ok

    def sneaky_calls(dst, m):
        (dst / "skill.py").write_text(
            (dst / "skill.py")
            .read_text()
            .replace(
                '        step = int(args.get("step") or 1)',
                '        step = int(args.get("step") or 1)\n'
                "        open('/etc/passwd')\n        eval('1')\n"
                "        x = ().__class__.__subclasses__()",
            )
        )

    r = reviewer.review(copy_skill(tmp_path, version="1.0.3", patch=sneaky_calls))
    assert {f.rule for f in r.findings} >= {"call", "attribute"}

    def no_tests(dst, m):
        shutil.rmtree(dst / "tests")

    r = reviewer.review(copy_skill(tmp_path, version="1.0.4", patch=no_tests))
    assert any(f.rule == "tests" for f in r.findings)

    def missing_handler(dst, m):
        m["capabilities"].append({"name": "ghost", "description": "x", "risk": "P0"})

    r = reviewer.review(copy_skill(tmp_path, version="1.0.5", patch=missing_handler))
    assert any(f.rule == "capability" and "ghost" in f.message for f in r.findings)
    assert not reviewer.review(tmp_path / "nope").ok


# ---------------------------------------------------------------- install through the gate


def test_install_needs_approval_then_runs_through_gateway_and_verifier(rt, tmp_path):
    assert rt.skills.list() == [] and "skill.hello_world.greet" not in rt.capabilities
    # review + sandbox tests without installing anything
    rev = run(rt.executor.run("skill.review", {"path": str(EXAMPLE), "run_tests": True}, **KW))
    assert rev.ok and rev.invocation.result["ok"] and rev.invocation.result["tests"]["ok"], (
        rev.invocation.result
    )
    assert rt.skills.list() == []
    # untrusted device cannot install (requires device.trusted); trusted owner must still approve
    denied = run(
        rt.executor.run(
            "skill.install",
            {"path": str(EXAMPLE)},
            actor="agent",
            correlation_id="m2",
            device_trusted=False,
        )
    )
    assert denied.invocation.status is InvocationStatus.DENIED
    res = install(rt, EXAMPLE)
    assert res.ok and res.verification.outcome is Outcome.ACHIEVED, res.invocation.error
    assert res.invocation.result["version"] == "1.0.0" and res.invocation.result["tests"]["ok"]
    caps = set(rt.capabilities.names())
    assert {
        "skill.hello_world.greet",
        "skill.hello_world.count",
        "skill.hello_world.time_greeting",
    } <= caps
    listing = rt.skills.list()[0]
    assert listing["active"] == "1.0.0" and listing["enabled"] and listing["versions"] == ["1.0.0"]
    # the skill's capabilities are ordinary capabilities: gateway + verifier
    g = run(rt.executor.run("skill.hello_world.greet", {"name": "Malte"}, **KW))
    assert g.ok and g.invocation.result == {"text": "Hello, Malte."}
    c = run(rt.executor.run("skill.hello_world.count", {"step": 3}, **KW))
    assert (
        c.ok and c.verification.outcome is Outcome.ACHIEVED and c.invocation.result["counter"] == 3
    )
    lie = run(rt.executor.run("skill.hello_world.count", {"step": 500}, **KW))
    assert lie.invocation.ok and lie.verification.outcome is Outcome.NOT_ACHIEVED and not lie.ok
    assert rt.capabilities.get("skill.hello_world.count").manifest.risk.name == "P1"
    # ctx.call goes through the gate: declared capability works, undeclared is refused
    t = run(rt.executor.run("skill.hello_world.time_greeting", {}, **KW))
    assert t.ok and "The core says it is" in t.invocation.result["text"]
    inv_types = [e.type for _, e in rt.bus.replay(correlation_id="m1")]
    assert inv_types.count("capability.invoked") >= 5  # includes the nested mock.clock call
    nested = [
        e
        for _, e in rt.bus.replay(correlation_id="m1")
        if e.type == "capability.invoked" and e.payload["invocation"]["capability"] == "mock.clock"
    ]
    assert nested and nested[0].payload["invocation"]["actor"] == "skill:hello_world"
    logs = [e for _, e in rt.bus.replay(type_prefix="skill.log")]
    assert logs and logs[0].payload["skill"] == "hello_world"
    types = [e.type for _, e in rt.bus.replay(correlation_id="skills")]
    assert ["skill.reviewed", "skill.tested", "skill.enabled", "skill.installed"] == [
        t
        for t in types
        if t in ("skill.reviewed", "skill.tested", "skill.enabled", "skill.installed")
    ][:4]
    assert rt.health()["skills"] == 1


def test_skill_cannot_reach_undeclared_capabilities(rt, tmp_path):
    def greedy(dst, m):
        m["uses"] = []  # manifest no longer declares mock.clock

    path = copy_skill(tmp_path, version="1.0.0", patch=greedy)
    assert install(rt, path).ok
    t = run(rt.executor.run("skill.hello_world.time_greeting", {}, **KW))
    assert (
        t.invocation.status is InvocationStatus.FAILED and "did not declare" in t.invocation.error
    )


def test_rejected_skill_is_never_installed(rt, tmp_path):
    def sneaky(dst, m):
        (dst / "skill.py").write_text(
            (dst / "skill.py")
            .read_text()
            .replace("from typing import Any", "from typing import Any\nimport socket")
        )

    bad = copy_skill(tmp_path, version="2.0.0", patch=sneaky)
    res = install(rt, bad)
    assert (
        res.invocation.status is InvocationStatus.FAILED and "review failed" in res.invocation.error
    )
    assert rt.skills.list() == [] and "skill.hello_world.greet" not in rt.capabilities
    assert "skill.rejected" in [e.type for _, e in rt.bus.replay(correlation_id="skills")]

    def failing_tests(dst, m):
        (dst / "tests" / "test_skill.py").write_text("def test_boom():\n    assert False\n")

    res = install(rt, copy_skill(tmp_path, version="2.0.1", patch=failing_tests))
    assert (
        res.invocation.status is InvocationStatus.FAILED and "tests failed" in res.invocation.error
    )
    assert rt.skills.list() == []


def test_upgrade_rollback_disable_and_restart(rt, tmp_path):
    assert install(rt, EXAMPLE).ok

    def v11(dst, m):
        (dst / "skill.py").write_text(
            (dst / "skill.py").read_text().replace('"Hello, {name}."', '"Hi, {name}!"')
        )
        t = dst / "tests" / "test_skill.py"
        t.write_text(t.read_text().replace('"Hello, Malte."', '"Hi, Malte!"'))

    newer = copy_skill(tmp_path, version="1.1.0", patch=v11)
    up = install(rt, newer)
    assert up.ok and up.invocation.result["previous"] == "1.0.0"
    assert (
        run(rt.executor.run("skill.hello_world.greet", {"name": "x"}, **KW)).invocation.result[
            "text"
        ]
        == "Hi, x!"
    )
    assert rt.skills.list()[0]["versions"] == ["1.0.0", "1.1.0"]
    rb = run(rt.executor.run("skill.rollback", {"name": "hello_world"}, **KW))
    assert rb.ok and rb.invocation.result == {
        "skill": "hello_world",
        "from": "1.1.0",
        "to": "1.0.0",
    }
    assert (
        run(rt.executor.run("skill.hello_world.greet", {"name": "x"}, **KW)).invocation.result[
            "text"
        ]
        == "Hello, x."
    )
    again = run(rt.executor.run("skill.rollback", {"name": "hello_world"}, **KW))
    assert (
        again.invocation.status is InvocationStatus.FAILED
        and "no previous" in again.invocation.error
    )
    off = run(rt.executor.run("skill.disable", {"name": "hello_world"}, **KW))
    assert off.ok and "skill.hello_world.greet" not in rt.capabilities
    on = run(rt.executor.run("skill.enable", {"name": "hello_world", "version": "1.1.0"}, **KW))
    assert on.ok and rt.skills.active_version("hello_world") == "1.1.0"
    # restart: the active version comes back from registry.json
    rt2 = CoreRuntime.build(
        rt.db_url,
        provider="none",
        workspace_root=str(tmp_path / "ws"),
        skills_root=str(rt.skills.root),
    )
    assert (
        "skill.hello_world.greet" in rt2.capabilities
        and rt2.skills.active_version("hello_world") == "1.1.0"
    )
    assert (
        run(rt2.executor.run("skill.hello_world.greet", {"name": "y"}, **KW)).invocation.result[
            "text"
        ]
        == "Hi, y!"
    )


def test_skill_api(rt, tmp_path):
    client = TestClient(create_app(rt))
    assert client.get("/skills").json()["count"] == 0
    r = client.post("/skills/review", json={"path": str(EXAMPLE), "run_tests": False}).json()
    assert r["status"] == "completed" and r["result"]["ok"]
    r = client.post("/skills/install", json={"path": str(EXAMPLE)}).json()
    assert r["status"] == "waiting_for_approval"
    ok = client.post(
        f"/approvals/{r['decision_id']}/approve",
        json={"method": "ui_confirm", "device_trusted": True},
    ).json()
    assert ok["decision"]["decision"] == "allow"
    done = client.post(
        "/skills/install", json={"path": str(EXAMPLE)}
    ).json()  # re-run needs a new grant
    assert done["status"] == "waiting_for_approval"
    assert client.get("/skills").json()["count"] in (0, 1)
    remote = TestClient(create_app(rt), client=("203.0.113.3", 1))
    assert remote.post("/skills/install", json={"path": str(EXAMPLE)}).status_code == 403
    assert client.post("/skills/hello_world/bogus").status_code == 400
