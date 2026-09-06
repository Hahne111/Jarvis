"""Coding workflow tests (Phase 7 step 48/49): green run before done, role limits, artifacts."""

from __future__ import annotations

import asyncio

from core.agents import ROLES, RunOutcome
from core.api import create_app
from core.capabilities import CapabilityRegistry, register_mocks
from core.models import MockProvider, ModelRouter, ModelSpec, ProviderResult, Tier, ToolCallProposal
from core.runtime import CoreRuntime
from fastapi.testclient import TestClient

CALC = "def add(a, b):\n    return a + b\n"
TEST_OK = "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n"
TEST_BAD = "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 4\n"
PROOF = {"method": "ui_confirm", "device_id": "desk", "device_trusted": True}


def run(coro):
    return asyncio.run(coro)


def call(name, args, cid):
    return ToolCallProposal(name, args, call_id=cid)


def build(tmp_path, script):
    return CoreRuntime.build(
        f"sqlite:///{tmp_path / 'c.db'}",
        providers={"mock": MockProvider(script)},
        router=ModelRouter([ModelSpec("mock-model", "mock", Tier.FRONTIER, supports_effort=False)]),
        workspace_root=str(tmp_path / "ws"),
    )


def types(rt, mid):
    return [e.type for _, e in rt.bus.replay(correlation_id=mid)]


def test_roles_expose_workspace_run_only_where_allowed(tmp_path):
    caps = register_mocks(CapabilityRegistry())
    from adapters.workspace import WorkspaceManager, register_workspace
    from core.events import EventBus
    from core.verifier import VerifierRegistry

    register_workspace(caps, VerifierRegistry(), EventBus(), WorkspaceManager(tmp_path / "ws"))
    allow = frozenset(caps.names())
    assert "workspace.run" in ROLES["test"].filter_allowlist(allow, caps)
    assert "workspace.run" not in ROLES["verification"].filter_allowlist(allow, caps)
    assert "workspace.write" not in ROLES["verification"].filter_allowlist(allow, caps)
    assert "workspace.run" in ROLES["implementation"].filter_allowlist(allow, caps)
    assert ROLES["test"].allows_run("pytest") and ROLES["test"].allows_run("python")
    assert not ROLES["test"].allows_run("npm") and ROLES["implementation"].allows_run("npm")


def test_coding_mission_completes_only_after_a_verified_green_run(tmp_path):
    script = [
        ProviderResult(
            "",
            tool_calls=(
                call("workspace.write", {"path": "calc.py", "content": CALC}, "w1"),
                call("workspace.write", {"path": "test_calc.py", "content": TEST_OK}, "w2"),
            ),
            stop_reason="tool_use",
        ),
        ProviderResult(
            "",
            tool_calls=(
                call(
                    "workspace.run",
                    {"command": "python", "args": ["-m", "pytest", "-q", "test_calc.py"]},
                    "r1",
                ),
            ),
            stop_reason="tool_use",
        ),
        ProviderResult("Tests are green. Done."),
    ]
    rt = build(tmp_path, script)
    client = TestClient(create_app(rt))
    out = client.post(
        "/commands",
        json={"text": "write calc.add with a test", "device_id": "desk", "device_trusted": True},
    ).json()
    assert out["status"] == "waiting_for_approval"  # workspace.run is P3
    mid, did = out["mission_id"], out["decision_id"]
    assert (tmp_path / "ws" / mid / "calc.py").read_text() == CALC
    res = client.post(f"/approvals/{did}/approve", json=PROOF).json()
    assert res["status"] == "completed" and res["result"] == "Tests are green. Done."
    t = types(rt, mid)
    assert t.count("workspace.run.finished") == 1 and "verification.passed" in t
    artifacts = [
        e.payload for _, e in rt.bus.replay(correlation_id=mid) if e.type == "artifact.created"
    ]
    assert sorted(a["path"] for a in artifacts) == ["calc.py", "test_calc.py"] and all(
        a["sha256"] for a in artifacts
    )
    assert client.get(f"/missions/{mid}").json()["status"] == "completed"


def test_done_without_a_green_run_is_not_accepted(tmp_path):
    script = [
        ProviderResult(
            "",
            tool_calls=(call("workspace.write", {"path": "calc.py", "content": CALC}, "w1"),),
            stop_reason="tool_use",
        ),
        ProviderResult("All done!"),  # no run -> nagged once
        ProviderResult("Really done."),  # still no run -> not verified
    ]
    rt = build(tmp_path, script)
    m = run(rt.missions.create("code"))
    r = run(
        rt.coordinator.run(
            m.mission_id,
            "write calc.py",
            allowlist=set(rt.capabilities.names()),
            device_trusted=True,
        )
    )
    assert r.outcome is RunOutcome.NOT_VERIFIED and "green" in r.error
    nag = [
        msg
        for c in rt.providers["mock"].calls
        for msg in c["messages"]
        if "no verified green run" in msg.content
    ]
    assert nag  # the model was told exactly what is missing
    assert "artifact.created" not in types(rt, m.mission_id)


def test_red_run_keeps_the_mission_open_and_prose_files_need_no_run(tmp_path):
    script = [
        ProviderResult(
            "",
            tool_calls=(
                call("workspace.write", {"path": "calc.py", "content": CALC}, "w1"),
                call("workspace.write", {"path": "test_calc.py", "content": TEST_BAD}, "w2"),
            ),
            stop_reason="tool_use",
        ),
        ProviderResult(
            "",
            tool_calls=(
                call(
                    "workspace.run",
                    {"command": "python", "args": ["-m", "pytest", "-q", "test_calc.py"]},
                    "r1",
                ),
            ),
            stop_reason="tool_use",
        ),
        ProviderResult("Done anyway."),
        ProviderResult("Fine, I give up."),
    ]
    rt = build(tmp_path, script)
    client = TestClient(create_app(rt))
    out = client.post(
        "/commands", json={"text": "write calc with a failing test", "device_trusted": True}
    ).json()
    res = client.post(f"/approvals/{out['decision_id']}/approve", json=PROOF).json()
    assert res["status"] == "failed" and "green" in res["error"]
    assert "verification.failed" in types(rt, out["mission_id"])
    term = [
        e.payload
        for _, e in rt.bus.replay(correlation_id=out["mission_id"])
        if e.type == "workspace.run.output"
    ]
    assert any("assert" in c["chunk"] or "failed" in c["chunk"] for c in term)

    prose = build(
        tmp_path / "p",
        [
            ProviderResult(
                "",
                tool_calls=(
                    call("workspace.write", {"path": "NOTES.md", "content": "# notes\n"}, "w1"),
                ),
                stop_reason="tool_use",
            ),
            ProviderResult("Notes written."),
        ],
    )
    m = run(prose.missions.create("notes"))
    r = run(
        prose.coordinator.run(
            m.mission_id,
            "write notes",
            allowlist=set(prose.capabilities.names()),
            device_trusted=True,
        )
    )
    assert r.ok and "artifact.created" in types(prose, m.mission_id)


def test_phase7_exit_error_is_visible_and_repaired(tmp_path):
    """Blueprint Phase 7 exit: prototype built, run, error visible in the HUD events, repaired."""
    bad_calc = "def add(a, b):\n    return a - b\n"
    pytest_run = call(
        "workspace.run", {"command": "python", "args": ["-m", "pytest", "-q", "test_calc.py"]}, "r"
    )
    seen_failure = {}

    def script(messages):
        n = seen_failure.setdefault("step", 0)
        seen_failure["step"] = n + 1
        if n == 0:
            return ProviderResult(
                "",
                tool_calls=(
                    call("workspace.write", {"path": "calc.py", "content": bad_calc}, "w1"),
                    call("workspace.write", {"path": "test_calc.py", "content": TEST_OK}, "w2"),
                ),
                stop_reason="tool_use",
            )
        if n == 1:
            return ProviderResult("", tool_calls=(pytest_run,), stop_reason="tool_use")
        if n == 2:  # the model sees the red run in the tool result and repairs the code
            last = messages[-1].content
            assert '"verified": "not_achieved"' in last and "assert" in last
            return ProviderResult(
                "",
                tool_calls=(call("workspace.write", {"path": "calc.py", "content": CALC}, "w3"),),
                stop_reason="tool_use",
            )
        if n == 3:
            return ProviderResult("", tool_calls=(pytest_run,), stop_reason="tool_use")
        return ProviderResult("Fixed the subtraction bug; tests are green.")

    rt = build(tmp_path, script)
    client = TestClient(create_app(rt))
    out = client.post("/commands", json={"text": "build calc", "device_trusted": True}).json()
    mid = out["mission_id"]
    res = client.post(f"/approvals/{out['decision_id']}/approve", json=PROOF).json()
    assert res["status"] == "waiting_for_approval"  # red run -> repair -> second run needs P3
    res = client.post(f"/approvals/{res['decision_id']}/approve", json=PROOF).json()
    assert res["status"] == "completed" and "green" in res["result"]
    evs = [e for _, e in rt.bus.replay(correlation_id=mid)]
    finished = [e.payload for e in evs if e.type == "workspace.run.finished"]
    assert [f["exit_code"] for f in finished] == [1, 0]
    ver = [
        e.type
        for e in evs
        if e.payload.get("verification", {}).get("capability") == "workspace.run"
        and e.type != "verification.skipped"  # the awaiting-approval attempt verifies nothing
    ]
    assert ver == ["verification.failed", "verification.passed"]
    assert any(
        e.type == "workspace.run.output" and "assert" in e.payload["chunk"] for e in evs
    )  # the failing test output is visible in the terminal panel
    changed = [e.payload["path"] for e in evs if e.type == "workspace.file.changed"]
    assert changed == ["calc.py", "test_calc.py", "calc.py"]
    assert sorted(e.payload["path"] for e in evs if e.type == "artifact.created") == [
        "calc.py",
        "test_calc.py",
    ]
    assert client.get(f"/missions/{mid}").json()["status"] == "completed"


def test_test_role_cannot_run_arbitrary_commands(tmp_path):
    table = {
        "check": [
            ProviderResult(
                "",
                tool_calls=(
                    call("agent.delegate", {"role": "test", "goal": "run the build"}, "d1"),
                ),
                stop_reason="tool_use",
            ),
            ProviderResult("ok"),
        ],
        "run the build": [
            ProviderResult(
                "",
                tool_calls=(
                    call("workspace.run", {"command": "npm", "args": ["run", "build"]}, "n1"),
                ),
                stop_reason="tool_use",
            ),
            ProviderResult("could not run npm"),
        ],
    }
    seen = {}

    def script(messages):
        goal = messages[0].content
        i = seen.get(goal, 0)
        seen[goal] = i + 1
        return table[goal][i]

    rt = build(tmp_path, script)
    m = run(rt.missions.create("check"))
    r = run(
        rt.coordinator.run(
            m.mission_id, "check", allowlist=set(rt.capabilities.names()), device_trusted=True
        )
    )
    assert r.ok
    rejected = [
        e.payload
        for _, e in rt.bus.replay(correlation_id=m.mission_id)
        if e.type == "agent.tool.rejected"
    ]
    assert rejected and "may not run 'npm'" in rejected[0]["reason"]
    assert "workspace.run.started" not in types(rt, m.mission_id)
