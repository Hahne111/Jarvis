"""Tests for core.missions (Commit 005): state machine, task rules, persistence, replay rebuild."""

from __future__ import annotations

import asyncio

import pytest
from core.events import EventBus, SQLEventStore
from core.missions import (
    MISSION_TRANSITIONS,
    InvalidTransition,
    Mission,
    MissionEngine,
    MissionNotFound,
    MissionRepository,
    MissionStatus,
    TaskNotFound,
    TaskState,
)


def make_engine(url: str | None = None) -> tuple[MissionEngine, EventBus]:
    store = SQLEventStore(url) if url else SQLEventStore.in_memory()
    bus = EventBus(store)
    repo = MissionRepository(engine=store.engine)  # one database for log + snapshots
    return MissionEngine(bus, repo), bus


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- state machine table


def test_transition_table_covers_every_state_and_terminals_have_no_exit():
    assert set(MISSION_TRANSITIONS) == set(MissionStatus)
    for terminal in (MissionStatus.COMPLETED, MissionStatus.FAILED, MissionStatus.CANCELED):
        assert MISSION_TRANSITIONS[terminal] == frozenset()
    # Every non-terminal state can be canceled (kill switch / owner abort).
    for status, targets in MISSION_TRANSITIONS.items():
        if targets:
            assert MissionStatus.CANCELED in targets, status


def test_happy_path_created_to_completed_emits_one_event_per_transition():
    engine, bus = make_engine()
    m = run(engine.create("build game", budget={"time_s": 600}, device_id="desk-pc"))
    assert m.status is MissionStatus.CREATED and m.version == 0
    path = [
        MissionStatus.PLANNING,
        MissionStatus.WAITING_FOR_APPROVAL,
        MissionStatus.RUNNING,
        MissionStatus.VERIFYING,
        MissionStatus.COMPLETED,
    ]
    for status in path:
        m = run(engine.transition(m.mission_id, status, reason=f"-> {status}"))
        assert m.status is status
    assert m.version == len(path)
    assert m.is_terminal

    types = [e.type for _, e in bus.replay(correlation_id=m.mission_id)]
    assert types == [
        "mission.created",
        "mission.planning",
        "mission.waiting_for_approval",
        "mission.running",
        "mission.verifying",
        "mission.completed",
    ]
    last = bus.replay(correlation_id=m.mission_id)[-1][1]
    assert last.payload == {"from": "verifying", "to": "completed", "reason": "-> completed"}
    assert last.device_id == "desk-pc" and last.user_id == "local-owner"


@pytest.mark.parametrize(
    ("frm", "to"),
    [
        (MissionStatus.CREATED, MissionStatus.RUNNING),
        (MissionStatus.CREATED, MissionStatus.COMPLETED),
        (MissionStatus.PLANNING, MissionStatus.COMPLETED),
        (MissionStatus.WAITING_FOR_APPROVAL, MissionStatus.COMPLETED),
        (MissionStatus.RUNNING, MissionStatus.COMPLETED),  # must go through VERIFYING
        (MissionStatus.PAUSED, MissionStatus.VERIFYING),
    ],
)
def test_illegal_transitions_are_rejected_and_not_persisted(frm, to):
    engine, bus = make_engine()
    m = run(engine.create("x"))
    # Drive the mission into `frm` via legal steps.
    legal_paths = {
        MissionStatus.CREATED: [],
        MissionStatus.PLANNING: [MissionStatus.PLANNING],
        MissionStatus.WAITING_FOR_APPROVAL: [
            MissionStatus.PLANNING,
            MissionStatus.WAITING_FOR_APPROVAL,
        ],
        MissionStatus.RUNNING: [MissionStatus.PLANNING, MissionStatus.RUNNING],
        MissionStatus.PAUSED: [MissionStatus.PLANNING, MissionStatus.RUNNING, MissionStatus.PAUSED],
    }
    for s in legal_paths[frm]:
        run(engine.transition(m.mission_id, s))
    before = bus.store.count()
    with pytest.raises(InvalidTransition):
        run(engine.transition(m.mission_id, to))
    assert engine.get(m.mission_id).status is frm
    assert bus.store.count() == before


def test_terminal_missions_are_frozen():
    engine, _ = make_engine()
    m = run(engine.create("x"))
    run(engine.transition(m.mission_id, MissionStatus.CANCELED, reason="owner abort"))
    with pytest.raises(InvalidTransition):
        run(engine.transition(m.mission_id, MissionStatus.PLANNING))
    with pytest.raises(InvalidTransition):
        run(engine.add_task(m.mission_id, "late task"))
    with pytest.raises(InvalidTransition):
        run(engine.checkpoint(m.mission_id, {"note": "too late"}))


def test_unknown_mission_and_invalid_goal():
    engine, _ = make_engine()
    with pytest.raises(MissionNotFound):
        engine.get("nope")
    with pytest.raises(MissionNotFound):
        run(engine.transition("nope", MissionStatus.PLANNING))
    with pytest.raises(ValueError):
        Mission(goal="   ")


# ---------------------------------------------------------------- tasks


def test_tasks_respect_dependencies_mission_state_and_retries():
    engine, bus = make_engine()
    m = run(engine.create("ship feature"))
    t1 = run(engine.add_task(m.mission_id, "write code", assigned_agent="implementer"))
    t2 = run(engine.add_task(m.mission_id, "run tests", dependencies=[t1.task_id]))
    with pytest.raises(TaskNotFound):
        run(engine.add_task(m.mission_id, "bad deps", dependencies=["ghost"]))
    with pytest.raises(ValueError):
        run(engine.add_task(m.mission_id, "  "))

    # Tasks cannot start unless the mission is RUNNING.
    with pytest.raises(InvalidTransition):
        run(engine.set_task_state(m.mission_id, t1.task_id, TaskState.RUNNING))
    run(engine.transition(m.mission_id, MissionStatus.PLANNING))
    run(engine.transition(m.mission_id, MissionStatus.RUNNING))

    # t2 depends on t1 -> cannot start before t1 is completed.
    with pytest.raises(InvalidTransition):
        run(engine.set_task_state(m.mission_id, t2.task_id, TaskState.RUNNING))
    run(engine.set_task_state(m.mission_id, t1.task_id, TaskState.RUNNING))
    run(engine.set_task_state(m.mission_id, t1.task_id, TaskState.FAILED, reason="flaky"))
    t1 = run(engine.set_task_state(m.mission_id, t1.task_id, TaskState.PENDING))  # retry
    assert t1.retries == 1
    run(engine.set_task_state(m.mission_id, t1.task_id, TaskState.RUNNING))
    run(engine.set_task_state(m.mission_id, t1.task_id, TaskState.COMPLETED))
    with pytest.raises(InvalidTransition):  # completed is terminal for a task
        run(engine.set_task_state(m.mission_id, t1.task_id, TaskState.RUNNING))
    t2 = run(engine.set_task_state(m.mission_id, t2.task_id, TaskState.RUNNING))
    assert t2.state is TaskState.RUNNING
    with pytest.raises(TaskNotFound):
        run(engine.set_task_state(m.mission_id, "ghost", TaskState.RUNNING))

    types = [e.type for _, e in bus.replay(correlation_id=m.mission_id, type_prefix="mission.task")]
    assert types == [
        "mission.task.added",
        "mission.task.added",
        "mission.task.running",
        "mission.task.failed",
        "mission.task.pending",
        "mission.task.running",
        "mission.task.completed",
        "mission.task.running",
    ]


# ---------------------------------------------------------------- persistence / recovery


def test_mission_survives_restart_via_snapshot(tmp_path):
    url = f"sqlite:///{tmp_path / 'core.db'}"
    engine, _ = make_engine(url)
    m = run(engine.create("persist me", priority="urgent", context={"project": "atlas"}))
    run(engine.transition(m.mission_id, MissionStatus.PLANNING))
    t = run(engine.add_task(m.mission_id, "step 1"))
    run(engine.checkpoint(m.mission_id, {"note": "plan ready"}))

    # "Process restart": new engine, new bus, same database file.
    engine2, _ = make_engine(url)
    loaded = engine2.get(m.mission_id)
    assert loaded.status is MissionStatus.PLANNING
    assert loaded.priority.value == "urgent"
    assert loaded.context == {"project": "atlas"}
    assert [x.task_id for x in loaded.tasks] == [t.task_id]
    assert loaded.checkpoints[0]["note"] == "plan ready"
    assert loaded.version == 3
    # ...and it keeps working after the restart.
    run(engine2.transition(m.mission_id, MissionStatus.RUNNING))
    assert engine2.get(m.mission_id).status is MissionStatus.RUNNING
    assert [x.mission_id for x in engine2.list(MissionStatus.RUNNING)] == [m.mission_id]


def test_rebuild_from_event_log_reproduces_snapshots(tmp_path):
    url = f"sqlite:///{tmp_path / 'core.db'}"
    engine, bus = make_engine(url)
    a = run(engine.create("alpha"))
    b = run(engine.create("beta", device_id="phone"))
    run(engine.transition(a.mission_id, MissionStatus.PLANNING))
    run(engine.transition(a.mission_id, MissionStatus.RUNNING))
    t = run(engine.add_task(a.mission_id, "t"))
    run(engine.set_task_state(a.mission_id, t.task_id, TaskState.RUNNING))
    run(engine.set_task_state(a.mission_id, t.task_id, TaskState.COMPLETED))
    run(engine.checkpoint(a.mission_id, {"note": "half"}))
    run(engine.transition(b.mission_id, MissionStatus.CANCELED))
    expected = {m.mission_id: m.to_dict() for m in engine.list()}

    # Lose the snapshot table entirely, keep the log.
    engine._repo.delete_all()
    assert engine.list() == []
    n = run(engine.rebuild_from_log())
    assert n == 2
    rebuilt = {m.mission_id: m.to_dict() for m in engine.list()}
    assert rebuilt == expected

    # Unrelated events with the mission prefix do not break the rebuild.
    run(bus.emit("mission.task.running", "rogue", {"task_id": "x", "to": "running"}))
    assert run(engine.rebuild_from_log()) == 2
