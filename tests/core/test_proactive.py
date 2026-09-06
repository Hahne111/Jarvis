"""Tests for Phase 11: relevance, scheduler + watchdog, habits/suggestions, brief, privacy."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from core.api import create_app
from core.events import Event
from core.missions import MissionStatus
from core.notify import FakePush
from core.proactive import Context, RelevanceEngine
from core.runtime import CoreRuntime
from core.scheduler import Job
from fastapi.testclient import TestClient

NOW = datetime(2026, 9, 7, 9, 0, tzinfo=UTC)  # a Monday
KW = dict(actor="owner", correlation_id="m1", device_trusted=True, device_id="desk")


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def push():
    return FakePush()


@pytest.fixture
def rt(tmp_path, push):
    return CoreRuntime.build(
        f"sqlite:///{tmp_path / 'p.db'}", provider="none", home="fake", news="fake", push=push
    )


def ev(type_, payload=None, **kw):
    return Event.new(type_, "test", payload or {}, **kw)


# ---------------------------------------------------------------- relevance engine


def test_relevance_channels_and_interruption_cost():
    eng = RelevanceEngine(lambda: Context(home_state="home", hour=12))
    ask = ev("permission.ask", {"decision": {"request": {"risk": 3}, "required_strength": 2}})
    assert eng.assess(ask).channel == "now"
    assert eng.assess(ev("gateway.halted")).channel == "now"
    failed = ev("mission.failed", {"reason": "x"})
    assert eng.assess(failed).channel == "opportunistic"
    assert eng.assess(ev("mission.completed")).channel == "brief"
    assert eng.assess(ev("telemetry.latency", {"point": "hud_frame"})).channel == "silent"
    assert eng.assess(ev("capability.invoked", {})).channel == "silent"
    assert eng.assess(ev("home.device.changed", {"domain": "light"})).channel == "silent"
    assert eng.assess(ev("home.device.changed", {"domain": "lock"})).channel == "opportunistic"
    news = ev(
        "news.event.created",
        {"country": "DE", "topics": ["security"], "breaking": True, "confidence": 0.5},
    )
    assert eng.assess(news).channel == "brief"  # not local -> informative
    local = RelevanceEngine(lambda: Context(home_country="DE", hour=12))
    assert local.assess(news).channel == "opportunistic"
    # at night an important-but-not-critical event waits for the brief; critical breaks through
    night = RelevanceEngine(lambda: Context(home_state="sleep", hour=3))
    assert night.assess(failed).channel == "brief" and night.assess(ask).channel == "now"
    assert night.context().interruption_cost >= 0.9
    a = eng.assess(failed)
    assert RelevanceEngine.pushable(a) and not RelevanceEngine.pushable(night.assess(failed))
    assert set(a.to_dict()) == {
        "channel",
        "urgency",
        "relevance",
        "confidence",
        "interruption_cost",
        "reason",
    }


def test_irrelevant_events_stay_silent_on_the_phone(rt, push):
    client = TestClient(create_app(rt))
    n = len(push.sent)
    client.post("/commands", json={"text": "echo nothing to see"})
    run(rt.executor.run("home.light.set", {"target": "kitchen", "on": True}, **KW))
    client.post("/news/refresh")
    assert len(push.sent) == n  # completed missions, comfort devices, news: silent
    # a failed mission at night is held back for the brief (suppressed, not lost)
    run(rt.executor.run("home.state.set", {"state": "sleep"}, **KW))
    rt.home.backend.ignoring.add("light.living_room")  # it is on and will refuse to turn off
    r = client.post("/commands", json={"text": "turn off the living_room light"}).json()
    assert r["status"] == "failed" and len(push.sent) == n
    types = [e.type for _, e in rt.bus.replay(type_prefix="notify")]
    assert types[-1] == "notify.suppressed"
    # critical still breaks through the night
    client.post("/kill")
    assert push.sent[-1].title == "JARVIS stopped"


# ---------------------------------------------------------------- scheduler + watchdog


def test_jobs_run_through_the_gate_and_survive_restart(tmp_path, push):
    db = f"sqlite:///{tmp_path / 's.db'}"
    rt = CoreRuntime.build(db, provider="none", home="fake", push=push)
    sched = rt.scheduler
    assert [j.name for j in sched.store.list()] == ["daily brief"]  # system default, idempotent
    rt2 = CoreRuntime.build(db, provider="none", home="fake")
    assert len(rt2.scheduler.store.list()) == 1
    with pytest.raises(ValueError):
        Job(name="bad", kind="command", text="x", every_s=10)
    with pytest.raises(ValueError):
        sched.add(Job(name="p3", kind="capability", capability="power.wake", args={}, every_s=60))
    job = sched.add(
        Job(name="lights", kind="command", text="turn on the kitchen light", every_s=60)
    )
    assert job.next_run_at and job.next_run_at > job.created_at
    # not due yet -> nothing runs; move the clock -> the job creates a real mission
    assert run(sched.tick())["ran"] == []
    sched._clock = lambda: datetime.now(UTC) + timedelta(seconds=90)
    out = run(sched.tick())
    assert [r["name"] for r in out["ran"]] == ["lights"] and out["ran"][0]["status"] == "completed"
    mid = out["ran"][0]["mission_id"]
    assert rt.missions.get(mid).status is MissionStatus.COMPLETED
    assert rt.missions.get(mid).device_id.startswith("scheduler:")
    assert rt.home.backend.entities["light.kitchen"].state == "on"
    types = [e.type for _, e in rt.bus.replay(correlation_id=f"job:{job.job_id}")]
    assert types[:2] == ["job.started", "job.finished"]
    # the scheduler is never a trusted device: a trusted-only P3 action is denied, not sneaked in
    wake = sched.add(Job(name="wake", kind="command", text="wake desktop", every_s=60, max_runs=1))
    r = run(sched.run_job(wake))
    assert r["status"] == "failed" and "unmet requirements" in (r["error"] or "")
    assert not rt.permissions.pending()
    assert sched.store.get(wake.job_id).enabled is False  # max_runs reached
    # restart: jobs persist with their state, the loop can tick again
    rt3 = CoreRuntime.build(db, provider="none", home="fake")
    names = {j.name: j for j in rt3.scheduler.store.list()}
    assert names["lights"].runs == 1 and names["wake"].runs == 1 and not names["wake"].enabled
    assert rt3.recover()["missions"] >= 2


def test_watchdog_pauses_stuck_missions(tmp_path):
    rt = CoreRuntime.build(f"sqlite:///{tmp_path / 'w.db'}", provider="none")
    m = run(rt.missions.create("long task", budget={"max_seconds": 60}))
    run(rt.missions.transition(m.mission_id, MissionStatus.PLANNING))
    run(rt.missions.transition(m.mission_id, MissionStatus.RUNNING))
    assert run(rt.scheduler.watchdog(datetime.now(UTC))) == []  # fresh: fine
    late = datetime.now(UTC) + timedelta(seconds=120)
    out = run(rt.scheduler.watchdog(late))
    assert out == [{"mission_id": m.mission_id, "from": "running", "to": "paused"}]
    assert rt.missions.get(m.mission_id).status is MissionStatus.PAUSED
    wd = [e for _, e in rt.bus.replay(correlation_id=m.mission_id) if e.type == "mission.watchdog"]
    assert wd and wd[0].payload["budget_s"] == 60 and wd[0].priority.value == "urgent"
    assert run(rt.scheduler.watchdog(late + timedelta(hours=1))) == []  # paused is not active
    p = run(rt.missions.create("stuck planning"))
    run(rt.missions.transition(p.mission_id, MissionStatus.PLANNING))
    out = run(rt.scheduler.watchdog(datetime.now(UTC) + timedelta(hours=1)))
    assert out[0]["to"] == "failed" and rt.missions.get(p.mission_id).status is MissionStatus.FAILED


def test_scheduler_loop_starts_and_stops_with_the_app(tmp_path):
    rt = CoreRuntime.build(f"sqlite:///{tmp_path / 'l.db'}", provider="none")
    rt.scheduler.interval_s = 0.05
    with TestClient(create_app(rt, scheduler=True)) as client:
        assert client.get("/health").json()["scheduler"]["running"] is True
        for _ in range(50):
            if rt.scheduler.ticks >= 2:
                break
            asyncio.run(asyncio.sleep(0.02))
        assert client.get("/schedule").json()["running"] is True
    assert rt.scheduler.ticks >= 1
    rt_off = CoreRuntime.build(f"sqlite:///{tmp_path / 'l2.db'}", provider="none")
    with TestClient(create_app(rt_off)) as client:
        assert client.get("/health").json()["scheduler"]["running"] is False


# ---------------------------------------------------------------- habits -> suggestions -> jobs


def seed_habit(rt, text, capability, args, days, hour, minute_jitter=(0, 5, -7, 3, 12)):
    weekdays = [d for d in range(14) if (NOW - timedelta(days=d)).weekday() < 5][:days]
    for i, back in enumerate(weekdays):
        ts = (NOW - timedelta(days=back)).replace(hour=hour, minute=10) + timedelta(
            minutes=minute_jitter[i % 5]
        )
        payload = {
            "text": text,
            "intent": {"kind": "capability", "capability": capability, "args": args},
            "mission_id": f"m{i}",
        }
        e = Event.new("command.received", "core-api", payload, correlation_id=f"m{i}")
        run(rt.bus.publish(Event(**{**e.__dict__, "timestamp": ts})))


def test_routine_is_suggested_never_activated_then_accepted_by_owner(rt):
    client = TestClient(create_app(rt))
    # two days are not a habit
    seed_habit(
        rt, "turn on the kitchen light", "home.light.set", {"target": "kitchen", "on": True}, 2, 7
    )
    assert run(rt.habits.scan(NOW)) == []
    # five weekdays around 07:10 -> one suggestion, no job, no automatic action
    seed_habit(
        rt, "turn on the kitchen light", "home.light.set", {"target": "kitchen", "on": True}, 5, 7
    )
    created = run(rt.habits.scan(NOW))
    assert len(created) == 1
    s = created[0]
    assert s.status == "pending" and s.at.startswith("07:") and s.weekdays == (0, 1, 2, 3, 4)
    assert s.evidence["days"] >= 5 and s.confidence >= 0.9
    assert [j.name for j in rt.scheduler.store.list()] == ["daily brief"]  # nothing activated
    types = [e.type for _, e in rt.bus.replay(correlation_id="proactive")]
    assert types == ["habit.detected", "automation.suggested"]
    assert run(rt.habits.scan(NOW)) == []  # not suggested twice
    listing = client.get("/suggestions", params={"status": "pending"}).json()
    assert [x["suggestion_id"] for x in listing] == [s.suggestion_id]
    # the owner accepts -> a job exists with the habit's schedule; the light job needs no preload
    acc = client.post(f"/suggestions/{s.suggestion_id}/accept").json()
    assert acc["suggestion"]["status"] == "accepted" and len(acc["jobs"]) == 1
    job = acc["jobs"][0]
    assert (
        job["text"] == "turn on the kitchen light"
        and job["weekdays"] == [0, 1, 2, 3, 4]
        and job["source"] == "suggestion"
    )
    assert client.post(f"/suggestions/{s.suggestion_id}/accept").status_code == 409
    # a news habit gets a P0 preload job ten minutes earlier (automatic: it only prepares)
    seed_habit(rt, "news", "news.top", {}, 4, 8)
    news_s = run(rt.habits.scan(NOW))[0]
    acc2 = client.post(f"/suggestions/{news_s.suggestion_id}/accept").json()
    kinds = {j["source"]: j for j in acc2["jobs"]}
    assert set(kinds) == {"suggestion", "preload"}
    assert (
        kinds["preload"]["capability"] == "news.refresh"
        and kinds["preload"]["at"] < kinds["suggestion"]["at"]
    )
    # dismiss silences a third habit
    seed_habit(rt, "szene movie", "home.scene.activate", {"target": "movie"}, 3, 20)
    movie = run(rt.habits.scan(NOW))[0]
    assert (
        client.post(f"/suggestions/{movie.suggestion_id}/dismiss").json()["suggestion"]["status"]
        == "dismissed"
    )
    assert run(rt.habits.scan(NOW)) == []
    assert (
        client.get("/schedule").json()["jobs"].__len__() == 4
    )  # brief + routine + news routine + preload
    # remote strangers cannot accept suggestions or add jobs
    remote = TestClient(create_app(rt), client=("203.0.113.5", 1))
    assert (
        remote.post("/schedule", json={"name": "x", "text": "echo x", "every_s": 60}).status_code
        == 403
    )


def test_schedule_api_crud(rt):
    client = TestClient(create_app(rt))
    j = client.post("/schedule", json={"name": "echo", "text": "echo hi", "every_s": 120}).json()
    assert j["enabled"] and j["next_run_at"]
    assert client.post("/schedule", json={"name": "bad", "text": "x"}).status_code == 400
    ran = client.post(f"/schedule/{j['job_id']}/run").json()
    assert ran["status"] == "completed" and ran["mission_id"]
    off = client.post(f"/schedule/{j['job_id']}/disable").json()
    assert off["enabled"] is False
    assert client.post(f"/schedule/{j['job_id']}/enable").json()["enabled"] is True
    assert client.post(f"/schedule/{j['job_id']}/delete").json() == {"deleted": True}
    assert client.post(f"/schedule/{j['job_id']}/run").status_code == 404
    assert client.post(f"/schedule/{j['job_id']}/bogus").status_code == 404


# ---------------------------------------------------------------- brief + privacy


def test_daily_brief_and_privacy_modes(rt, push):
    client = TestClient(create_app(rt))
    client.post("/news/refresh")
    run(rt.executor.run("power.wake", {"target": "desktop"}, **KW))  # leaves an approval waiting
    brief = client.post("/brief").json()
    assert "approval" in brief["text"] and "World:" in brief["text"]
    titles = [s["title"] for s in brief["sections"]]
    assert {"Waiting for you", "World", "Home", "Next"} <= set(titles)
    assert client.get("/brief").json()["text"] == brief["text"]
    assert rt.intents.route("daily brief").capability == "brief.generate"
    assert rt.intents.route("was liegt an?").capability == "brief.generate"
    # privacy: private stops learning from observation and pauses satellites; guest stops all memory
    assert client.get("/privacy").json()["mode"] == "normal"
    out = client.post("/commands", json={"text": "privacy mode on", "device_trusted": True}).json()
    assert out["status"] == "completed" and rt.privacy.mode == "private"
    assert rt.memory_writer.policy.learn_from_observation is False
    assert rt.memory_writer.policy.conversation_memory is True
    sat = client.post(
        "/satellite/command", json={"text": "licht an", "satellite_id": "puck"}
    ).json()
    assert sat["status"] == "paused" and "Privacy" in sat["speech"]
    stop = client.post(
        "/satellite/command", json={"text": "jarvis, stop", "satellite_id": "puck"}
    ).json()
    assert stop["speech"] == "Stopped."
    client.post("/resume", json={"method": "passkey", "device_trusted": True})
    # in private mode only critical pushes go out
    n = len(push.sent)
    rt.home.backend.ignoring.add("light.kitchen")
    client.post("/commands", json={"text": "turn on the kitchen light"})
    assert len(push.sent) == n
    client.post("/commands", json={"text": "guest mode on", "device_trusted": True})
    assert rt.privacy.mode == "guest" and rt.memory_writer.policy.conversation_memory is False
    ev_types = [e.type for _, e in rt.bus.replay(type_prefix="privacy")]
    assert ev_types == ["privacy.changed", "privacy.changed"]
    # restart rebuilds the mode from the log
    again = CoreRuntime.build(rt.db_url, provider="none")
    assert again.privacy.mode == "normal"
    again.recover()
    assert again.privacy.mode == "guest" and again.memory_writer.policy.conversation_memory is False
    client.post("/commands", json={"text": "privacy mode off", "device_trusted": True})
    assert rt.privacy.mode == "normal" and rt.memory_writer.policy.learn_from_observation is True
    hud = client.get("/hud/").text
    assert 'data-tab="proactive"' in hud
