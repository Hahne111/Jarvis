"""Tests for adapters/workspace (Phase 7): path sandbox, versioned writes, diffs, sandboxed runs."""

from __future__ import annotations

import asyncio
import os

import pytest
from adapters.workspace import WorkspaceError, WorkspaceManager
from core.capabilities import InvocationStatus
from core.permissions import ApprovalProof, ProofMethod
from core.runtime import CoreRuntime
from core.verifier import Outcome


def run(coro):
    return asyncio.run(coro)


CONFIRM = ApprovalProof(ProofMethod.UI_CONFIRM, device_id="desk", device_trusted=True)


@pytest.fixture
def rt(tmp_path):
    return CoreRuntime.build(
        f"sqlite:///{tmp_path / 'w.db'}", provider="none", workspace_root=str(tmp_path / "ws")
    )


def kw(mission="m1"):
    return dict(actor="agent", correlation_id=mission, device_trusted=True, device_id="desk")


# ---------------------------------------------------------------- manager: sandbox


def test_path_sandbox_blocks_every_escape(tmp_path):
    m = WorkspaceManager(tmp_path / "root")
    ws = m.workspace("m1")
    for bad in ("../x", "a/../../x", "/etc/passwd", "~/x", ".jarvis/versions/x"):
        with pytest.raises(WorkspaceError):
            m.resolve("m1", bad)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (ws / "link").symlink_to(outside)
    with pytest.raises(WorkspaceError):
        m.read("m1", "link")
    with pytest.raises(WorkspaceError):
        m.workspace("../evil")
    with pytest.raises(WorkspaceError):
        m.read("m1", "missing.txt")
    assert m.resolve("m1", "sub/file.txt").parent.name == "sub"  # not-yet-existing paths are fine
    # another mission cannot see this one's files
    m.write("m1", "note.txt", "hello")
    assert m.list("m2") == [] and [f["path"] for f in m.list("m1")] == ["note.txt"]


def test_versioned_writes_and_diffs(tmp_path):
    m = WorkspaceManager(tmp_path / "root")
    first = m.write("m1", "app.py", "print('v1')\n")
    assert (
        first["created"]
        and first["previous_version"] is None
        and first["sha256"] == m.sha256("print('v1')\n")
    )
    second = m.write("m1", "app.py", "print('v2')\n")
    assert not second["created"] and second["previous_version"].startswith(".jarvis/versions/")
    assert "-print('v1')" in second["diff"] and "+print('v2')" in second["diff"]
    assert "-print('v1')" in m.diff("m1", "app.py")
    assert m.read("m1", "app.py") == "print('v2')\n"
    assert (
        m.file_sha256("m1", "app.py") == m.sha256("print('v2')\n")
        and m.file_sha256("m1", "nope") is None
    )
    assert [f["path"] for f in m.list("m1")] == ["app.py"]  # .jarvis is hidden from listings


def test_runner_allowlist_timeout_and_env(tmp_path):
    m = WorkspaceManager(tmp_path / "root", max_run_seconds=5)
    program = (
        "import os, sys\n"
        "print('hi from', os.getcwd().split(os.sep)[-1])\n"
        "print('env', 'ANTHROPIC_API_KEY' in os.environ)\n"
        "sys.exit(3)\n"
    )
    m.write("m1", "hello.py", program)
    chunks = []

    async def on_out(stream, text):
        chunks.append((stream, text))

    r = run(m.run("m1", "python", ["hello.py"], on_output=on_out))
    assert (
        r.exit_code == 3
        and "hi from m1" in r.stdout
        and "env False" in r.stdout
        and not r.timed_out
    )
    assert chunks and chunks[0][0] == "stdout"
    with pytest.raises(WorkspaceError):
        run(m.run("m1", "rm", ["-rf", "."]))
    with pytest.raises(WorkspaceError):
        run(m.run("m1", "/usr/bin/python3", ["hello.py"]))
    with pytest.raises(WorkspaceError):
        run(m.run("m1", "python", ["../other/x.py"]))
    m.write("m1", "slow.py", "import time\ntime.sleep(30)\n")
    r2 = run(m.run("m1", "python", ["slow.py"], timeout_s=1))
    assert r2.timed_out and r2.exit_code is None and r2.duration_ms < 5000


# ---------------------------------------------------------------- through the gate


def test_workspace_capabilities_through_gateway_and_verifiers(rt):
    w = run(
        rt.executor.run("workspace.write", {"path": "src/main.py", "content": "print(1)\n"}, **kw())
    )
    assert w.ok and w.verification.outcome is Outcome.ACHIEVED and w.invocation.result["created"]
    changed = [
        e for _, e in rt.bus.replay(correlation_id="m1") if e.type == "workspace.file.changed"
    ]
    assert (
        len(changed) == 1
        and "+print(1)" in changed[0].payload["diff"]
        and changed[0].payload["actor"] == "agent"
    )

    r = run(rt.executor.run("workspace.read", {"path": "src/main.py"}, **kw()))
    assert r.ok and r.invocation.result["content"] == "print(1)\n"
    ls = run(rt.executor.run("workspace.list", {}, **kw()))
    assert [f["path"] for f in ls.invocation.result["files"]] == ["src", "src/main.py"]
    bad = run(rt.executor.run("workspace.read", {"path": "../../etc/passwd"}, **kw()))
    assert bad.invocation.status is InvocationStatus.FAILED and "traversal" in bad.invocation.error
    other = run(rt.executor.run("workspace.list", {}, **kw("m2")))
    assert other.invocation.result["count"] == 0  # per-mission isolation

    run(
        rt.executor.run("workspace.write", {"path": "src/main.py", "content": "print(2)\n"}, **kw())
    )
    d = run(rt.executor.run("workspace.diff", {"path": "src/main.py"}, **kw()))
    assert d.ok and d.invocation.result["changed"] and "-print(1)" in d.invocation.result["diff"]

    # run needs approval (P3)
    waiting = run(
        rt.executor.run("workspace.run", {"command": "python", "args": ["src/main.py"]}, **kw())
    )
    assert waiting.invocation.status is InvocationStatus.AWAITING_APPROVAL
    run(rt.permissions.approve(waiting.invocation.decision_id, CONFIRM))
    done = run(
        rt.executor.run(
            "workspace.run",
            {"command": "python", "args": ["src/main.py"]},
            decision_id=waiting.invocation.decision_id,
            **kw(),
        )
    )
    assert (
        done.ok
        and done.invocation.result["exit_code"] == 0
        and "2" in done.invocation.result["stdout"]
    )
    types = [e.type for _, e in rt.bus.replay(correlation_id="m1", type_prefix="workspace.run")]
    assert (
        types[0] == "workspace.run.started"
        and "workspace.run.output" in types
        and types[-1] == "workspace.run.finished"
    )

    # failing program -> tool ran, goal not achieved
    run(
        rt.executor.run(
            "workspace.write", {"path": "fail.py", "content": "raise SystemExit(2)\n"}, **kw()
        )
    )
    w2 = run(rt.executor.run("workspace.run", {"command": "python", "args": ["fail.py"]}, **kw()))
    run(rt.permissions.approve(w2.invocation.decision_id, CONFIRM))
    failed = run(
        rt.executor.run(
            "workspace.run",
            {"command": "python", "args": ["fail.py"]},
            decision_id=w2.invocation.decision_id,
            **kw(),
        )
    )
    assert (
        failed.invocation.ok
        and failed.verification.outcome is Outcome.NOT_ACHIEVED
        and not failed.ok
    )
    denied = run(
        rt.executor.run("workspace.run", {"command": "bash", "args": ["-c", "id"]}, **kw())
    )
    run(rt.permissions.approve(denied.invocation.decision_id, CONFIRM))
    rej = run(
        rt.executor.run(
            "workspace.run",
            {"command": "bash", "args": ["-c", "id"]},
            decision_id=denied.invocation.decision_id,
            **kw(),
        )
    )
    assert rej.invocation.status is InvocationStatus.FAILED and "allowlist" in rej.invocation.error
    assert os.path.isdir(os.path.join(rt.workspaces.root, "m1", ".jarvis", "versions"))


def test_workspace_views_and_sandboxed_preview_for_the_hud(rt):
    from core.api import create_app
    from fastapi.testclient import TestClient

    client = TestClient(create_app(rt))
    run(
        rt.executor.run(
            "workspace.write",
            {"path": "index.html", "content": "<h1>hi</h1><script>document.title='x'</script>"},
            **kw(),
        )
    )
    run(rt.executor.run("workspace.write", {"path": "app.js", "content": "console.log(1)"}, **kw()))
    files = client.get("/workspace/m1/files").json()["files"]
    assert [f["path"] for f in files] == ["app.js", "index.html"]
    assert (
        client.get("/workspace/m1/file", params={"path": "app.js"}).json()["content"]
        == "console.log(1)"
    )
    assert client.get("/workspace/m1/file", params={"path": "../x"}).status_code == 400
    assert client.get("/workspace/m1/file", params={"path": "nope.js"}).status_code == 404
    first_diff = client.get("/workspace/m1/diff", params={"path": "app.js"}).json()["diff"]
    assert first_diff.startswith("--- a/app.js") and "+console.log(1)" in first_diff  # new file
    assert client.get("/workspace/bad id!/files").status_code == 400

    r = client.get("/workspace/m1/preview/index.html")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert (
        r.headers["content-security-policy"].startswith("sandbox")
        and r.headers["cache-control"] == "no-store"
    )
    assert (
        client.get("/workspace/m1/preview/app.js")
        .headers["content-type"]
        .startswith("text/javascript")
    )
    assert client.get("/workspace/m1/preview/").status_code == 200  # directory -> index.html
    assert client.get("/workspace/m1/preview/../../etc/passwd").status_code == 404
    assert client.get("/workspace/m1/preview/missing.png").status_code == 404
    assert client.get("/workspace/m2/preview/index.html").status_code == 404  # other mission
    hud = client.get("/hud/").text
    assert 'id="codingPanel"' in hud and "previewFrame" in hud and 'sandbox="allow-scripts"' in hud
    for tab in ("agents", "quality", "artifacts"):
        assert f'data-tab="{tab}"' in hud  # agent rail, quality and artifact panels (SPEC §12.1)
    js = client.get("/hud/hud.js").text
    assert "/hud/vendor/monaco/vs/loader.js" in js  # Monaco only from the local vendor dir
    assert "https://" not in js and "http://" not in js  # no third-party script at runtime


def test_editor_save_goes_through_the_gate(rt):
    from core.api import create_app
    from fastapi.testclient import TestClient

    client = TestClient(create_app(rt))
    assert (
        client.put("/workspace/nope/file", json={"path": "a.py", "content": ""}).status_code == 404
    )
    m = run(rt.missions.create("edit"))
    mid = m.mission_id
    body = {"path": "app.py", "content": "print(1)\n", "device_id": "hud", "device_trusted": True}
    r = client.put(f"/workspace/{mid}/file", json=body).json()
    assert r["status"] == "completed" and r["verification"]["outcome"] == "achieved"
    assert r["invocation"]["actor"] == "owner:hud"
    assert client.get(f"/workspace/{mid}/file", params={"path": "app.py"}).json()["content"] == (
        "print(1)\n"
    )
    r2 = client.put(f"/workspace/{mid}/file", json={**body, "content": "print(2)\n"}).json()
    assert r2["status"] == "completed" and not r2["result"]["created"]
    assert r2["result"]["diff_lines"] > 0  # the full diff travels in workspace.file.changed
    changed = [
        e for _, e in rt.bus.replay(correlation_id=mid) if e.type == "workspace.file.changed"
    ]
    assert len(changed) == 2 and changed[-1].payload["actor"] == "owner:hud"
    assert client.put(f"/workspace/{mid}/file", json={**body, "path": "../x"}).status_code == 400
    # nothing bypasses the gate: the kill switch blocks editor saves too
    run(rt.gateway.halt("test"))
    r3 = client.put(f"/workspace/{mid}/file", json=body).json()
    assert r3["status"] == "halted"
