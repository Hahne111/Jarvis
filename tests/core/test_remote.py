"""Tests for Phase 9 remote pieces: push, handover, forwarded callers, CLI enroll, PWA, mobile."""

from __future__ import annotations

import asyncio
import json

import pytest
from core import __main__ as core_main
from core.api import create_app
from core.devices import generate_keypair, sign_headers
from core.notify import FakePush, PushService
from core.runtime import CoreRuntime
from fastapi.testclient import TestClient

REMOTE = ("203.0.113.9", 40001)
KW = dict(actor="owner", correlation_id="m1", device_trusted=True, device_id="desk")


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def push():
    return FakePush()


@pytest.fixture
def rt(tmp_path, push):
    return CoreRuntime.build(
        f"sqlite:///{tmp_path / 'rm.db'}", provider="none", home="fake", push=push
    )


def enroll_phone(rt, local):
    start = local.post("/devices/enroll/start", json={"name_hint": "phone"}).json()
    priv, pub = generate_keypair()
    dev = local.post(
        "/devices/enroll", json={"code": start["code"], "name": "phone", "public_key": pub}
    ).json()
    return dev, priv


def signed(client, priv, device_id, method, path, body=None):
    raw = json.dumps(body).encode() if body is not None else b""
    headers = sign_headers(device_id, priv, method, path, raw)
    return client.request(
        method, path, content=raw, headers={**headers, "content-type": "application/json"}
    )


# ---------------------------------------------------------------- push


def test_push_forwards_only_owner_relevant_events(rt, push):
    local = TestClient(create_app(rt))
    assert local.get("/health").json()["push"] == "fake"
    # an approval that waits for the owner -> high priority push with a deep link
    waiting = run(rt.executor.run("power.wake", {"target": "desktop"}, **KW))
    assert waiting.invocation.decision_id
    assert push.sent[-1].title == "Approval needed" and push.sent[-1].priority == "high"
    assert "power.wake" in push.sent[-1].body and push.sent[-1].click == "/hud/#approvals"
    # kill switch -> urgent
    local.post("/kill")
    assert push.sent[-1].title == "JARVIS stopped" and push.sent[-1].priority == "urgent"
    local.post("/resume", json={"method": "passkey", "device_trusted": True})
    # a failed mission -> default priority
    rt.home.backend.ignoring.add("light.kitchen")
    r = local.post("/commands", json={"text": "turn on the kitchen light", "device_trusted": True})
    assert r.json()["status"] == "failed"
    assert push.sent[-1].title == "Mission failed"
    # noise stays silent: a completed echo produces no push
    n = len(push.sent)
    local.post("/commands", json={"text": "echo quiet please"})
    assert len(push.sent) == n
    # deliveries are events without secrets; the API lists them
    notes = local.get("/notifications").json()
    assert [x["type"] for x in notes][-3:] == ["notify.sent"] * 3
    assert notes[-1]["payload"]["channel"] == "fake" and "token" not in json.dumps(notes)
    # a broken transport never breaks the bus
    push.failing = True
    local.post("/kill")
    assert local.get("/notifications").json()[-1]["type"] == "notify.failed"
    assert local.get("/health").json()["halted"] is True


def test_push_message_mapping_and_webhook_repr():
    from core.events import Event
    from core.notify import WebhookPush

    ev = Event.new("device.revoked", "core-api", {"name": "old phone"}, correlation_id="devices")
    m = PushService.message_for(ev)
    assert m and m.title == "Device revoked" and "old phone" in m.body
    assert PushService.message_for(Event.new("mission.completed", "x", {})) is None
    with pytest.raises(ValueError):
        WebhookPush(url="")
    w = WebhookPush(url="https://ntfy.sh/topic", token="secret-token-value")  # noqa: S106
    assert "secret-token-value" not in repr(w) and "token=set" in repr(w)


# ---------------------------------------------------------------- handover desktop <-> mobile


def test_mission_handover_keeps_one_mission_across_devices(rt):
    local = TestClient(create_app(rt))
    remote = TestClient(create_app(rt), client=REMOTE)
    phone, priv = enroll_phone(rt, local)
    pid = phone["device_id"]
    # desktop starts a P3 action -> waits for approval
    out = local.post(
        "/commands", json={"text": "wake desktop", "device_id": "desk", "device_trusted": True}
    ).json()
    assert out["status"] == "waiting_for_approval"
    mid = out["mission_id"]
    # strangers cannot hand over; the owner hands the mission to the phone
    assert remote.post(f"/missions/{mid}/handover", json={"to_device_id": pid}).status_code == 403
    h = local.post(
        f"/missions/{mid}/handover", json={"to_device_id": pid, "note": "leaving"}
    ).json()
    assert h["handover"]["from_device"] == "desk" and h["handover"]["to_device"] == pid
    assert h["mission"]["checkpoints"][-1]["handover"]["to_device"] == pid
    ev = next(e for _, e in rt.bus.replay(correlation_id=mid) if e.type == "mission.handover")
    assert ev.device_id == pid and ev.priority.value == "urgent"
    pres = rt.presence.snapshot()["devices"]
    assert pres[pid]["active_mission"] == mid and pres[pid]["state"] == "awaiting_approval"
    assert pres["desk"]["active_mission"] is None
    # the phone sees the very same mission and approves with a tap (P3 = ui_confirm)
    seen_phone = signed(remote, priv, pid, "GET", f"/missions/{mid}").json()
    seen_desk = local.get(f"/missions/{mid}").json()
    assert seen_phone == seen_desk and seen_phone["status"] == "waiting_for_approval"
    approvals = signed(remote, priv, pid, "GET", "/approvals").json()
    assert approvals and approvals[0]["request"]["correlation_id"] == mid
    ok = signed(
        remote,
        priv,
        pid,
        "POST",
        f"/approvals/{out['decision_id']}/approve",
        {"method": "ui_confirm"},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "completed"
    assert local.get(f"/missions/{mid}").json()["status"] == "completed"
    # unsigned remote tap: refused (no unlocked trusted device behind it)
    again = local.post("/commands", json={"text": "wake desktop", "device_trusted": True}).json()
    assert (
        remote.post(
            f"/approvals/{again['decision_id']}/approve", json={"method": "ui_confirm"}
        ).status_code
        == 403
    )
    assert (
        local.post(f"/missions/{mid}/handover", json={"to_device_id": "nope"}).status_code == 409
    )  # terminal missions cannot move any more
    free = local.post(f"/missions/{again['mission_id']}/handover", json={"to_device_id": "tablet"})
    assert free.status_code == 200 and free.json()["handover"]["to_device"] == "tablet"
    assert local.post("/missions/none/handover", json={"to_device_id": pid}).status_code == 404


# ---------------------------------------------------------------- proxies, CLI, PWA


def test_forwarded_headers_remove_the_local_owner_privilege(rt):
    local = TestClient(create_app(rt))
    assert local.post("/devices/enroll/start", json={}).status_code == 200
    proxied = TestClient(create_app(rt), headers={"X-Forwarded-For": "100.64.0.7"})
    assert proxied.post("/devices/enroll/start", json={}).status_code == 403
    ts = TestClient(create_app(rt), headers={"Tailscale-User-Login": "malte@github"})
    assert ts.get("/devices").json()["caller"]["local"] is False
    r = proxied.post("/commands", json={"text": "echo hi", "device_trusted": True}).json()
    assert local.get(f"/missions/{r['mission_id']}").json()["context"]["device_trusted"] is False


def test_cli_enroll_code_works_against_the_running_core(tmp_path, monkeypatch, capsys):
    db = f"sqlite:///{tmp_path / 'cli.db'}"
    monkeypatch.setenv("JARVIS_CORE_DB_URL", db)
    assert core_main.main(["enroll", "phone", "mobile"]) == 0
    out = capsys.readouterr().out
    code = out.split("enrollment code:")[1].split()[0]
    assert len(code) == 8
    rt = CoreRuntime.build(db, provider="none")
    _, pub = generate_keypair()
    dev = TestClient(create_app(rt)).post(
        "/devices/enroll", json={"code": code, "name": "phone", "public_key": pub}
    )
    assert dev.status_code == 200 and dev.json()["type"] == "mobile"
    # the host guard refuses a world-open bind before uvicorn starts
    monkeypatch.setenv("JARVIS_CORE_HOST", "0.0.0.0")  # noqa: S104 - testing the refusal
    assert core_main.main([]) == 2
    assert "expose the Core" in capsys.readouterr().err


def test_pwa_manifest_and_mobile_hud_are_served(rt):
    local = TestClient(create_app(rt))
    m = local.get("/hud/manifest.webmanifest")
    assert m.status_code == 200 and m.headers["content-type"].startswith(
        "application/manifest+json"
    )
    assert m.json()["display"] == "standalone" and m.json()["start_url"] == "/hud/"
    html = local.get("/hud/").text
    assert 'rel="manifest"' in html and 'id="enrollThisBtn"' in html and "theme-color" in html
    js = local.get("/hud/hud.js").text
    assert "Ed25519" in js and "x-jarvis-signature" in js and "isSecureContext" in js
    css = local.get("/hud/hud.css").text
    assert "@media (max-width:760px)" in css
