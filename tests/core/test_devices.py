"""Tests for core/devices (Phase 9): enrollment, signed requests, trust binding, revocation."""

from __future__ import annotations

import json
import time

import pytest
from adapters.home import FakeHome
from core.api import create_app
from core.devices import (
    DeviceRegistry,
    DeviceType,
    EnrollmentError,
    generate_keypair,
    sign_headers,
    valid_public_key,
)
from core.runtime import CoreRuntime
from fastapi.testclient import TestClient

REMOTE = ("203.0.113.7", 40000)  # a phone somewhere behind the mesh VPN (never loopback)


@pytest.fixture
def rt(tmp_path):
    return CoreRuntime.build(f"sqlite:///{tmp_path / 'dev.db'}", provider="none", home=FakeHome())


@pytest.fixture
def local(rt):
    return TestClient(create_app(rt))


@pytest.fixture
def remote(rt):
    return TestClient(create_app(rt), client=REMOTE)


def signed(client, priv, device_id, method, path, body=None, **kw):
    raw = json.dumps(body).encode() if body is not None else b""
    headers = sign_headers(device_id, priv, method, path, raw, **kw)
    return client.request(
        method, path, content=raw, headers={**headers, "content-type": "application/json"}
    )


def enroll(local, remote, name="phone", trusted=True):
    start = local.post("/devices/enroll/start", json={"name_hint": name, "trusted": trusted}).json()
    priv, pub = generate_keypair()
    dev = remote.post(
        "/devices/enroll", json={"code": start["code"], "name": name, "public_key": pub}
    )
    assert dev.status_code == 200, dev.text
    return dev.json(), priv, pub


# ---------------------------------------------------------------- registry


def test_registry_persists_and_revocation_is_final(tmp_path):
    rt = CoreRuntime.build(f"sqlite:///{tmp_path / 'r.db'}", provider="none")
    reg = rt.devices
    assert reg.count() == 0
    e = reg.start_enrollment(name_hint="phone", type=DeviceType.MOBILE)
    assert len(e.code) == 8 and e.to_dict(with_code=False).get("code") is None
    _, pub = generate_keypair()
    with pytest.raises(EnrollmentError):
        reg.complete_enrollment("NOPE0000", name="x", public_key=pub)
    d = reg.complete_enrollment(e.code.lower(), name="Malte's phone", public_key=pub)
    assert d.active_trust and d.fingerprint and "public_key" not in d.to_dict()
    with pytest.raises(EnrollmentError):  # single use
        reg.complete_enrollment(e.code, name="again", public_key=generate_keypair()[1])
    again = DeviceRegistry(rt.store.engine)  # restart: same table
    assert again.get(d.device_id).name == "Malte's phone" and again.count() == 1
    again.revoke(d.device_id, "lost")
    assert again.get(d.device_id).revoked and not again.get(d.device_id).active_trust
    assert again.set_trusted(d.device_id, True).active_trust is False  # revoked stays untrusted
    assert again.count() == 0 and len(again.list()) == 1
    # brute force: 5 wrong codes close the enrollment
    e2 = reg.start_enrollment()
    for _ in range(5):
        with pytest.raises(EnrollmentError):
            reg.complete_enrollment("00000000", name="x", public_key=pub)
    with pytest.raises(EnrollmentError):
        reg.complete_enrollment(e2.code, name="late", public_key=generate_keypair()[1])
    assert not valid_public_key("not-a-key") and valid_public_key(pub)


# ---------------------------------------------------------------- enrollment over the API


def test_enrollment_flow_and_events(rt, local, remote):
    assert remote.post("/devices/enroll/start", json={}).status_code == 403  # remote can't mint
    start = local.post("/devices/enroll/start", json={"name_hint": "phone"}).json()
    assert "code" in start and start["trusted"]
    ev = [e for _, e in rt.bus.replay(correlation_id="devices")]
    assert ev[-1].type == "device.enrollment.started" and "code" not in json.dumps(ev[-1].payload)
    bad = remote.post(
        "/devices/enroll",
        json={"code": "FFFFFFFF", "name": "x", "public_key": generate_keypair()[1]},
    )
    assert bad.status_code == 403
    badkey = remote.post(
        "/devices/enroll", json={"code": start["code"], "name": "x", "public_key": "QUJD" * 11}
    )
    assert badkey.status_code == 400
    dev, _priv, _pub = enroll(local, remote)
    assert dev["trusted"] and dev["type"] == "mobile"
    listing = local.get("/devices").json()
    assert listing["caller"] == {
        "local": True,
        "signed": False,
        "trusted": False,
        "device_id": None,
    }
    assert [d["device_id"] for d in listing["devices"]] == [dev["device_id"]]
    assert "device.enrolled" in [e.type for _, e in rt.bus.replay(correlation_id="devices")]
    assert local.get("/health").json()["devices"] == 1


# ---------------------------------------------------------------- signed requests


def test_signed_requests_bind_trust_and_reject_tampering(rt, local, remote):
    dev, priv, _pub = enroll(local, remote)
    did = dev["device_id"]
    # unsigned remote: allowed, but never trusted even if it claims to be
    r = remote.post(
        "/commands", json={"text": "turn on the kitchen light", "device_trusted": True}
    ).json()
    assert r["status"] == "completed"  # P2 needs no trust
    cmd = [e for _, e in rt.bus.replay(type_prefix="command.received")][-1]
    assert cmd.payload["intent"]["capability"] == "home.light.set"
    r = remote.post(
        "/commands", json={"text": "wake desktop", "device_id": "spoof", "device_trusted": True}
    ).json()
    assert r["status"] in ("blocked", "failed")  # no WOL registered with a FakeHome instance anyway
    # signed remote: the registry decides trust; the body claim is ignored
    body = {"text": "echo signed hello", "device_trusted": False}
    r = signed(remote, priv, did, "POST", "/commands", body)
    assert r.status_code == 200 and r.json()["status"] == "completed"
    cmd = [e for _, e in rt.bus.replay(type_prefix="command.received")][-1]
    assert cmd.device_id == did
    mission = local.get(f"/missions/{r.json()['mission_id']}").json()
    assert mission["context"]["device_trusted"] is True
    # tampered body, replay, stale timestamp, unknown device, missing headers
    raw = json.dumps(body).encode()
    h = sign_headers(did, priv, "POST", "/commands", raw)
    tampered = remote.post(
        "/commands",
        content=json.dumps({**body, "text": "echo evil"}).encode(),
        headers={**h, "content-type": "application/json"},
    )
    assert tampered.status_code == 401 and "invalid signature" in tampered.text
    ok = remote.post("/commands", content=raw, headers={**h, "content-type": "application/json"})
    assert ok.status_code == 200
    replay = remote.post(
        "/commands", content=raw, headers={**h, "content-type": "application/json"}
    )
    assert replay.status_code == 401 and "replayed" in replay.text
    stale = signed(remote, priv, did, "POST", "/commands", body, ts=time.time() - 3600)
    assert stale.status_code == 401 and "window" in stale.text
    other_priv, _ = generate_keypair()
    forged = signed(remote, other_priv, did, "POST", "/commands", body)
    assert forged.status_code == 401
    unknown = signed(remote, priv, "no-such-device", "POST", "/commands", body)
    assert unknown.status_code == 401 and "unknown device" in unknown.text
    partial = remote.post("/commands", json=body, headers={"x-jarvis-device": did})
    assert partial.status_code == 401 and "missing" in partial.text
    failures = [e for _, e in rt.bus.replay(type_prefix="device.auth.failed")]
    assert len(failures) >= 5 and failures[0].payload["device_id"] == did
    assert local.get("/devices").json()["devices"][0]["last_seen"] is not None


def test_secure_remote_action_and_stolen_device_revoke(rt, local, remote):
    """Phase 9 exit: a secure test action from away; a stolen device is revoked immediately."""
    dev, priv, _pub = enroll(local, remote)
    did = dev["device_id"]
    home = rt.home.backend
    # 1) remote, unsigned: strong proofs are refused outright
    ask = local.post(
        "/commands", json={"text": "unlock the front door", "device_trusted": True}
    ).json()
    assert ask["status"] == "blocked"  # no provider: the deep path is blocked, use the capability
    res = rt.executor
    import asyncio

    waiting = asyncio.run(
        res.run(
            "home.lock.set",
            {"target": "front door", "locked": False},
            actor="owner",
            correlation_id="m9",
            device_trusted=True,
            device_id=did,
        )
    )
    decision_id = waiting.invocation.decision_id
    denied = remote.post(
        f"/approvals/{decision_id}/approve", json={"method": "passkey", "device_trusted": True}
    )
    assert denied.status_code == 403 and "strong proof" in denied.text
    weak = signed(
        remote, priv, did, "POST", f"/approvals/{decision_id}/approve", {"method": "ui_confirm"}
    )
    assert weak.status_code == 409 and "weaker" in weak.text  # P4 needs STRONG even when signed
    voice = signed(
        remote, priv, did, "POST", f"/approvals/{decision_id}/approve", {"method": "voice"}
    )
    assert voice.status_code == 409
    # 2) signed trusted phone with a passkey: approved; the door state is verified by read-back
    ok = signed(
        remote,
        priv,
        did,
        "POST",
        f"/approvals/{decision_id}/approve",
        {"method": "passkey", "reference": "assertion-1"},
    )
    assert ok.status_code == 200, ok.text
    approved = [e for _, e in rt.bus.replay(type_prefix="permission.approved")][-1]
    assert approved.payload["decision"]["approval_proof"]["device_id"] == did
    assert approved.payload["decision"]["approval_proof"]["device_trusted"] is True
    done = asyncio.run(
        res.run(
            "home.lock.set",
            {"target": "front door", "locked": False},
            actor="owner",
            correlation_id="m9",
            device_trusted=True,
            device_id=did,
            decision_id=decision_id,
        )
    )
    assert done.ok and home.entities["lock.front_door"].state == "unlocked"
    # 3) the phone is stolen: revoke from the local HUD -> every signed request dies at once
    revoked = local.post(f"/devices/{did}/revoke", json={"reason": "stolen"}).json()
    assert revoked["revoked_at"] and not revoked["trusted"]
    dead = signed(remote, priv, did, "POST", "/commands", {"text": "echo still here"})
    assert dead.status_code == 401 and "revoked" in dead.text
    again = local.post(f"/devices/{did}/trust", json={"trusted": True})
    assert again.status_code == 409  # revocation is final
    assert remote.post(f"/devices/{did}/revoke", json={}).status_code == 403  # strangers can't
    types = [e.type for _, e in rt.bus.replay(correlation_id="devices")]
    assert "device.revoked" in types


def test_untrusted_device_and_local_owner_paths(rt, local, remote):
    dev, priv, _pub = enroll(local, remote, name="tv", trusted=False)
    did = dev["device_id"]
    assert not dev["trusted"]
    r = signed(
        remote, priv, did, "POST", "/commands", {"text": "echo hi", "device_trusted": True}
    ).json()
    assert r["status"] == "completed"
    assert local.get(f"/missions/{r['mission_id']}").json()["context"]["device_trusted"] is False
    # the owner promotes it from the local HUD, later demotes it again
    assert local.post(f"/devices/{did}/trust", json={"trusted": True}).json()["trusted"] is True
    r = signed(remote, priv, did, "POST", "/commands", {"text": "echo hi"}).json()
    assert local.get(f"/missions/{r['mission_id']}").json()["context"]["device_trusted"] is True
    assert (
        signed(remote, priv, did, "POST", f"/devices/{did}/trust", {"trusted": False}).status_code
        == 200
    )
    # local owner keeps working unsigned (loopback), remote strangers cannot deny or resume
    assert local.post("/kill").json()["halted"]
    assert remote.post("/resume", json={"method": "passkey"}).status_code == 403
    assert local.post("/resume", json={"method": "passkey", "device_trusted": True}).json() == {
        "halted": False
    }
    # satellite enrolled as a device: trust comes from the registry, not the HA payload
    sat, spriv, _ = enroll(local, remote, name="kitchen-puck", trusted=True)
    r = signed(
        remote,
        spriv,
        sat["device_id"],
        "POST",
        "/satellite/command",
        {"text": "echo sat", "satellite_id": "kitchen-puck", "device_trusted": False},
    ).json()
    assert r["device_id"] == sat["device_id"] and r["status"] == "completed"
    hud = local.get("/hud/").text
    assert 'data-tab="devices"' in hud and 'id="enrollBtn"' in hud
