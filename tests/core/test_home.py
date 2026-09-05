"""Tests for adapters/home (Phase 8): gateway, verifiers, home safety, states, offline path."""

from __future__ import annotations

import asyncio

import pytest
from adapters.home import (
    HOME_MANIFESTS,
    DeviceRegistry,
    Entity,
    FakeHome,
    HomeAssistantBackend,
    HomeState,
    HomeUnavailable,
)
from core.capabilities import InvocationStatus
from core.permissions import ApprovalProof, ProofMethod
from core.runtime import CoreRuntime
from core.verifier import Outcome


def run(coro):
    return asyncio.run(coro)


CONFIRM = ApprovalProof(ProofMethod.UI_CONFIRM, device_id="phone", device_trusted=True)
PASSKEY = ApprovalProof(ProofMethod.PASSKEY, device_id="phone", device_trusted=True)
VOICE = ApprovalProof(ProofMethod.VOICE, device_id="satellite", device_trusted=True)
KW = dict(actor="owner:phone", correlation_id="m1", device_trusted=True, device_id="phone")


@pytest.fixture
def home():
    return FakeHome()


@pytest.fixture
def rt(tmp_path, home):
    return CoreRuntime.build(f"sqlite:///{tmp_path / 'h.db'}", provider="none", home=home)


def types(rt, cid="m1"):
    return [e.type for _, e in rt.bus.replay(correlation_id=cid)]


# ---------------------------------------------------------------- manifests / safety


def test_manifests_follow_home_safety_rules():
    by = {m.name: m for m in HOME_MANIFESTS}
    for m in HOME_MANIFESTS:
        if m.side_effects:
            assert m.verifier and m.risk.value >= 1  # Law 4
    for name in ("home.lock.set", "home.alarm.set", "home.garage.set"):
        assert by[name].risk.name == "P4" and "device.trusted" in by[name].requires
        assert not by[name].reversible
    for name in ("home.light.set", "home.switch.set", "home.climate.set", "home.cover.set"):
        assert by[name].risk.name == "P2" and by[name].reversible
    assert by["home.list_devices"].risk.name == "P0" and not by["home.list_devices"].side_effects


# ---------------------------------------------------------------- registry / room graph


def test_registry_resolves_rooms_names_and_ids():
    reg = DeviceRegistry(FakeHome().entities.values(), floors={"kitchen": "ground"})
    assert [r.room_id for r in reg.rooms()][:3] == ["bedroom", "garage", "hall"]
    assert (
        reg.rooms()[0].floor is None
        and next(r for r in reg.rooms() if r.room_id == "kitchen").floor == "ground"
    )
    assert [e.entity_id for e in reg.resolve("küche", "light")] == ["light.kitchen"]
    assert [e.entity_id for e in reg.resolve("kitchen")] == [
        "light.kitchen",
        "switch.coffee_machine",
    ]
    assert [e.entity_id for e in reg.resolve("living room light", "light")] == ["light.living_room"]
    assert [e.entity_id for e in reg.resolve("light.kitchen")] == ["light.kitchen"]
    assert reg.resolve("light.kitchen", "switch") == [] and reg.resolve("attic", "light") == []
    assert len(reg.resolve("all", "light")) == 2
    reg.update([Entity("light.attic", "off", {"friendly_name": "Attic light"}, area="Attic")])
    assert reg.entity("light.attic") and "attic" in [r.room_id for r in reg.rooms()]


# ---------------------------------------------------------------- through the gate


def test_light_control_from_phone_is_verified_by_readback(rt, home):
    res = run(rt.executor.run("home.light.set", {"target": "küche", "on": True}, **KW))
    assert res.ok and res.verification.outcome is Outcome.ACHIEVED
    assert home.entities["light.kitchen"].state == "on"
    assert res.invocation.result["changes"][0] == {
        "entity_id": "light.kitchen",
        "from": "off",
        "to": "on",
    }
    changed = [e for _, e in rt.bus.replay(correlation_id="m1") if e.type == "home.device.changed"]
    assert changed and changed[0].payload["actor"] == "owner:phone"
    assert "permission.ask" not in types(rt)  # P2 comfort action: no approval needed

    dim = run(
        rt.executor.run("home.light.set", {"target": "all", "on": True, "brightness": 300}, **KW)
    )
    assert dim.ok and home.entities["light.living_room"].attributes["brightness"] == 255
    assert sorted(dim.invocation.result["entities"]) == ["light.kitchen", "light.living_room"]

    # a stuck relay: the tool "succeeds", the read-back says otherwise -> goal not achieved
    home.ignoring.add("light.kitchen")
    stuck = run(rt.executor.run("home.light.set", {"target": "light.kitchen", "on": False}, **KW))
    assert (
        stuck.invocation.ok and stuck.verification.outcome is Outcome.NOT_ACHIEVED and not stuck.ok
    )

    bad = run(rt.executor.run("home.light.set", {"target": "attic", "on": True}, **KW))
    assert (
        bad.invocation.status is InvocationStatus.FAILED
        and "no light matches" in bad.invocation.error
    )
    read = run(rt.executor.run("home.get_state", {"target": "kitchen"}, **KW))
    assert read.ok and read.invocation.result["count"] == 2
    listing = run(rt.executor.run("home.list_devices", {"domain": "light"}, **KW))
    assert listing.invocation.result["count"] == 2 and listing.invocation.result["online"]


def test_climate_cover_switch_and_scene(rt, home):
    c = run(
        rt.executor.run("home.climate.set", {"target": "living_room", "temperature": 22.5}, **KW)
    )
    assert c.ok and home.entities["climate.living_room"].attributes["temperature"] == 22.5
    hot = run(
        rt.executor.run("home.climate.set", {"target": "living_room", "temperature": 45}, **KW)
    )
    assert hot.invocation.status is InvocationStatus.FAILED and "between" in hot.invocation.error
    sw = run(rt.executor.run("home.switch.set", {"target": "coffee", "on": True}, **KW))
    assert sw.ok and home.entities["switch.coffee_machine"].state == "on"
    blind = run(rt.executor.run("home.cover.set", {"target": "bedroom", "open": False}, **KW))
    assert blind.ok and home.entities["cover.bedroom_blind"].state == "closed"
    # the garage is a security device: the comfort capability refuses it
    g = run(rt.executor.run("home.cover.set", {"target": "garage", "open": True}, **KW))
    assert (
        g.invocation.status is InvocationStatus.FAILED and "home.garage.set" in g.invocation.error
    )
    assert home.entities["cover.garage"].state == "closed"

    home.entities["scene.movie"].attributes["entities"] = {"light.living_room": "off"}
    sc = run(rt.executor.run("home.scene.activate", {"target": "movie", "on": True}, **KW))
    assert sc.invocation.status is InvocationStatus.INVALID  # unknown argument
    sc = run(rt.executor.run("home.scene.activate", {"target": "movie"}, **KW))
    assert sc.ok and home.entities["light.living_room"].state == "off"
    home.ignoring.add("scene.movie")
    sc2 = run(rt.executor.run("home.scene.activate", {"target": "scene.movie"}, **KW))
    assert sc2.verification.outcome is Outcome.NOT_ACHIEVED


def test_security_devices_need_strong_proof_never_voice(rt, home):
    unlock = run(rt.executor.run("home.lock.set", {"target": "front door", "locked": False}, **KW))
    assert unlock.invocation.status is InvocationStatus.AWAITING_APPROVAL
    did = unlock.invocation.decision_id
    assert home.entities["lock.front_door"].state == "locked"
    with pytest.raises(Exception, match=r"strong|proof|insufficient"):
        run(rt.permissions.approve(did, VOICE))  # SECURITY.md §4: never unlock on voice alone
    with pytest.raises(Exception, match=r"strong|proof|insufficient"):
        run(rt.permissions.approve(did, CONFIRM))
    assert home.entities["lock.front_door"].state == "locked"
    run(rt.permissions.approve(did, PASSKEY))
    done = run(
        rt.executor.run(
            "home.lock.set", {"target": "front door", "locked": False}, decision_id=did, **KW
        )
    )
    assert done.ok and home.entities["lock.front_door"].state == "unlocked"
    # untrusted device: denied outright, no ask
    untrusted = run(
        rt.executor.run(
            "home.garage.set",
            {"target": "garage", "open": True},
            actor="agent",
            correlation_id="m2",
            device_trusted=False,
            device_id="tv",
        )
    )
    assert untrusted.invocation.status is InvocationStatus.DENIED
    arm = run(rt.executor.run("home.alarm.set", {"target": "alarm", "mode": "armed_away"}, **KW))
    run(rt.permissions.approve(arm.invocation.decision_id, PASSKEY))
    armed = run(
        rt.executor.run(
            "home.alarm.set",
            {"target": "alarm", "mode": "armed_away"},
            decision_id=arm.invocation.decision_id,
            **KW,
        )
    )
    assert armed.ok and home.entities["alarm_control_panel.home"].state == "armed_away"
    badmode = run(rt.executor.run("home.alarm.set", {"target": "alarm", "mode": "panic"}, **KW))
    assert badmode.invocation.status is InvocationStatus.AWAITING_APPROVAL  # gate first
    run(rt.permissions.approve(badmode.invocation.decision_id, PASSKEY))
    badmode = run(
        rt.executor.run(
            "home.alarm.set",
            {"target": "alarm", "mode": "panic"},
            decision_id=badmode.invocation.decision_id,
            **KW,
        )
    )
    assert (
        badmode.invocation.status is InvocationStatus.FAILED and "mode" in badmode.invocation.error
    )
    assert home.entities["alarm_control_panel.home"].state == "armed_away"


def test_home_states_are_evented_and_survive_restart(rt, tmp_path, home):
    assert rt.home.states.current is HomeState.HOME
    r = run(rt.executor.run("home.state.set", {"state": "movie"}, **KW))
    assert r.ok and r.invocation.result["policy"]["speaker_routing"] == "tv"
    ev = [e for _, e in rt.bus.replay(correlation_id="m1") if e.type == "home.state.changed"]
    assert ev[0].payload["from"] == "home" and ev[0].payload["to"] == "movie"
    bad = run(rt.executor.run("home.state.set", {"state": "party"}, **KW))
    assert (
        bad.invocation.status is InvocationStatus.FAILED
        and "unknown home state" in bad.invocation.error
    )
    assert rt.home.states.current is HomeState.MOVIE
    run(rt.executor.run("home.state.set", {"state": "away"}, **KW))
    assert (
        rt.home.states.policy().microphones_muted
        and "news" not in rt.home.states.policy().notifications
    )

    again = CoreRuntime.build(f"sqlite:///{tmp_path / 'h.db'}", provider="none", home=FakeHome())
    assert again.home.states.current is HomeState.HOME
    assert again.recover()["home_state"] == "away"
    assert again.health()["home"] == "away"


def test_offline_gateway_fails_cleanly_and_kill_switch_holds(rt, home):
    home.offline = True
    r = run(rt.executor.run("home.light.set", {"target": "kitchen", "on": True}, **KW))
    assert r.invocation.status is InvocationStatus.FAILED and "offline" in r.invocation.error
    assert r.verification.outcome is not Outcome.ACHIEVED
    lst = run(rt.executor.run("home.list_devices", {}, **KW))
    assert lst.invocation.status is InvocationStatus.FAILED
    home.offline = False
    run(rt.gateway.halt("test"))
    halted = run(rt.executor.run("home.light.set", {"target": "kitchen", "on": True}, **KW))
    assert (
        halted.invocation.status is InvocationStatus.HALTED
        and home.entities["light.kitchen"].state == "off"
    )


def test_fast_path_intents_and_api(rt, home):
    from core.api import create_app
    from fastapi.testclient import TestClient

    client = TestClient(create_app(rt))
    for text, target, on in (
        ("licht an im wohnzimmer", "wohnzimmer", True),
        ("turn on the kitchen light", "kitchen", True),
        ("Schalte das Licht in der Küche aus", "küche", False),
        ("kitchen lights off", "kitchen", False),
        ("licht aus", "all", False),
    ):
        i = rt.intents.route(text)
        assert (i.capability, i.args) == ("home.light.set", {"target": target, "on": on}), text
    assert rt.intents.route("szene movie").args == {"target": "movie"}
    assert rt.intents.route("gute nacht").args == {"state": "sleep"}
    assert rt.intents.route("set home mode to away").args == {"state": "away"}
    assert (
        rt.intents.route("open vscode").capability == "computer.open_app"
        or rt.intents.route("open vscode").kind == "agent"
    )

    out = client.post(
        "/commands",
        json={"text": "turn on the kitchen light", "device_id": "phone", "device_trusted": True},
    ).json()
    assert out["status"] == "completed" and home.entities["light.kitchen"].state == "on"
    snap = client.get("/home").json()
    assert snap["enabled"] and snap["online"] and snap["state"]["state"] == "home"
    assert any(d["entity_id"] == "light.kitchen" and d["state"] == "on" for d in snap["devices"])
    assert [r["room_id"] for r in snap["rooms"]][:2] == ["bedroom", "garage"]
    assert client.get("/health").json()["home"] == "home"


def test_home_disabled_by_default(tmp_path):
    rt = CoreRuntime.build(f"sqlite:///{tmp_path / 'n.db'}", provider="none")
    assert rt.home is None and "home.light.set" not in rt.capabilities
    assert rt.intents.route("licht an").kind == "agent"
    from core.api import create_app
    from fastapi.testclient import TestClient

    assert TestClient(create_app(rt)).get("/home").json() == {"enabled": False}


# ---------------------------------------------------------------- home assistant client


def test_home_assistant_backend_never_leaks_the_token(monkeypatch):
    monkeypatch.setenv("JARVIS_HA_TOKEN", "unit-test-token-value")
    monkeypatch.setenv("JARVIS_HA_URL", "http://127.0.0.1:9/")
    b = HomeAssistantBackend(timeout_s=0.2)
    assert "unit-test-token-value" not in repr(b) and "token=set" in repr(b)
    with pytest.raises(HomeUnavailable):  # nothing listens on port 9 -> offline path, no crash
        run(b.list_entities())


def test_home_assistant_backend_parses_states(monkeypatch):
    import httpx

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.headers.get("authorization")))
        if request.url.path == "/api/states":
            return httpx.Response(
                200,
                json=[
                    {
                        "entity_id": "light.kitchen",
                        "state": "off",
                        "attributes": {"friendly_name": "Kitchen", "area_id": "kitchen"},
                    }
                ],
            )
        if request.url.path == "/api/states/light.kitchen":
            return httpx.Response(
                200, json={"entity_id": "light.kitchen", "state": "on", "attributes": {}}
            )
        if request.url.path == "/api/states/light.nope":
            return httpx.Response(404, json={"message": "not found"})
        if request.url.path.startswith("/api/services/"):
            return httpx.Response(200, json=[])
        if request.url.path == "/api/forbidden":
            return httpx.Response(401)
        return httpx.Response(500)

    b = HomeAssistantBackend("http://ha.test", "tok")
    b._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://ha.test",
        headers={"Authorization": "Bearer tok"},
    )
    ents = run(b.list_entities())
    assert (
        ents[0].entity_id == "light.kitchen"
        and ents[0].area == "kitchen"
        and ents[0].name == "Kitchen"
    )
    assert (
        run(b.get_state("light.kitchen")).state == "on" and run(b.get_state("light.nope")) is None
    )
    run(b.call_service("light", "turn_on", {"entity_id": "light.kitchen"}))
    assert calls[-1][:2] == ("POST", "/api/services/light/turn_on") and calls[-1][2] == "Bearer tok"
    with pytest.raises(HomeUnavailable):
        run(b._request("GET", "/api/forbidden"))
