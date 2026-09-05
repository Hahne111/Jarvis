"""Tests for voice/ (Phase 5): state machine, wake-ack first, barge-in, fast path, telemetry."""

from __future__ import annotations

import asyncio
import time

import pytest
from core.runtime import CoreRuntime
from voice import LatencyTelemetry, SpokenStyle, VoiceBridge, VoiceSession, VoiceState
from voice.fakes import FakeSTT, FakeTTS, FakeWake
from voice.interfaces import SilenceTurnDetector
from voice.personality import split_phrases
from voice.telemetry import percentile, summary_from_log


def run(coro):
    return asyncio.run(coro)


def build(tmp_path, turns, *, seconds_per_phrase=0.05, follow_up=False):
    rt = CoreRuntime.build(f"sqlite:///{tmp_path / 'v.db'}", provider="none")
    wake, stt, tts = FakeWake(), FakeSTT(turns), FakeTTS(seconds_per_phrase)
    bridge = VoiceBridge(rt, wake=wake, stt=stt, tts=tts, follow_up=follow_up)
    return rt, bridge, stt, tts


def types(rt, prefix="voice"):
    return [e.type for _, e in rt.bus.replay(type_prefix=prefix)]


# ---------------------------------------------------------------- state machine + personality


def test_session_transitions_and_events(tmp_path):
    rt = CoreRuntime.build(f"sqlite:///{tmp_path / 's.db'}", provider="none")
    s = VoiceSession(rt.bus, device_id="desk")
    with pytest.raises(ValueError):
        run(s.transition(VoiceState.SPEAKING))
    for st in (
        VoiceState.WAKE_ACK,
        VoiceState.LISTENING,
        VoiceState.THINKING,
        VoiceState.SPEAKING,
        VoiceState.FOLLOW_UP,
        VoiceState.IDLE,
    ):
        run(s.transition(st))
    assert types(rt) == [
        "voice.wake_ack",
        "voice.listening",
        "voice.thinking",
        "voice.speaking",
        "voice.follow_up",
        "voice.idle",
    ]
    first = rt.bus.replay(type_prefix="voice")[0][1]
    assert first.priority.value == "urgent" and first.device_id == "desk"


def test_spoken_style_no_filler_short_and_done():
    st = SpokenStyle(max_sentences=2)
    assert (
        st.apply("Certainly! Here is the answer. It is 42. And more. And more.")
        == "Here is the answer. It is 42."
    )
    assert st.apply("Natürlich, gerne. erledigt.") == "Erledigt."
    assert st.apply("") == "Done." and st.apply("Sure!") == "Done."
    assert SpokenStyle.is_serious(
        "there was an accident in the kitchen"
    ) and not SpokenStyle.is_serious("open the window")
    assert split_phrases("One. Two, three; four! Five?") == ["One.", "Two, three; four!", "Five?"]
    long = "a" * 100 + ", " + "b" * 100 + ", " + "c" * 30
    assert [len(p) <= 130 for p in split_phrases(long)] == [True, True, True]
    assert split_phrases("") == []
    td = SilenceTurnDetector()
    assert not td.end_of_turn(silence_ms=500, speech_seen=True, partial="open the")
    assert td.end_of_turn(silence_ms=900, speech_seen=True, partial="open the door")
    assert not td.end_of_turn(silence_ms=900, speech_seen=True, partial="open the door and")
    assert td.end_of_turn(silence_ms=6000, speech_seen=False, partial="")


# ---------------------------------------------------------------- end-to-end turns (fast path)


def test_wake_ack_comes_before_any_core_work_and_local_command_needs_no_provider(tmp_path):
    rt, bridge, _stt, tts = build(tmp_path, [["echo", "echo good", "echo good morning"]])
    run(bridge.handle_wake())
    t = types(rt)
    assert t[:2] == ["voice.wake_ack", "voice.listening"]
    assert t.index("voice.transcript") > t.index("voice.wake_ack")
    all_types = [e.type for _, e in rt.bus.replay()]
    assert all_types.index("voice.wake_ack") < all_types.index("mission.created")
    assert "voice.thinking" in t and "voice.speaking" in t and t[-1] == "voice.idle"
    assert tts.spoken == ["Good morning"] and tts.completed == ["Good morning"]
    assert bridge.session.state is VoiceState.IDLE
    assert rt.missions.list()[0].status.value == "completed"
    assert rt.health()["agent_ready"] is False  # no provider, still worked
    summary = bridge.telemetry.summary()
    assert set(summary) >= {"wake_ack", "first_audio", "local_dispatch"}
    assert summary["wake_ack"]["p50_ms"] < 250
    assert summary_from_log(rt.bus)["wake_ack"]["count"] == 1


def test_clock_and_unknown_and_blocked_replies(tmp_path):
    _rt, bridge, _stt, tts = build(tmp_path, [["what time is it"], ["write me a poem about lamps"]])
    run(bridge.handle_wake())
    assert tts.spoken and tts.spoken[0][:2] == "20"  # ISO timestamp
    run(bridge.handle_wake())
    assert tts.spoken[-1].startswith("I cannot do that yet")
    assert bridge.render_reply({"status": "waiting_for_approval"}).startswith(
        "That needs your approval"
    )
    assert bridge.render_reply({"route": "stop"}) == "Stopped."
    assert bridge.render_reply({"status": "halted"}) == "Everything is stopped."
    assert (
        bridge.render_reply({"status": "failed", "error": "denied"}) == "That did not work. denied"
    )
    assert (
        bridge.render_reply({"status": "completed", "result": {"opened": "https://x"}}) == "Done."
    )


def test_barge_in_stops_audio_fast_and_stop_phrase_pulls_the_kill_switch(tmp_path):
    rt, bridge, stt, tts = build(
        tmp_path,
        [["echo first sentence. second sentence. third sentence."]],
        seconds_per_phrase=0.5,
    )

    async def scenario():
        turn = asyncio.create_task(bridge.handle_wake())
        await asyncio.sleep(0.1)  # first phrase is playing
        assert bridge.session.state is VoiceState.SPEAKING and tts.speaking
        t0 = time.monotonic()
        ms = await bridge.barge_in(reason="test")
        elapsed = (time.monotonic() - t0) * 1000
        await turn
        return ms, elapsed

    ms, elapsed = run(scenario())
    assert ms is not None and ms < 150 and elapsed < 150
    assert tts.spoken == ["First sentence."] and tts.completed == [] and tts.stops >= 1
    assert bridge.session.state is VoiceState.IDLE
    t = types(rt)
    assert "voice.barge_in" in t and t[-1] == "voice.idle" and "voice.follow_up" not in t
    assert not rt.gateway.halted  # a plain interrupt does not halt the core

    stt.push("Jarvis, stop everything")
    run(bridge.handle_wake())
    assert rt.gateway.halted and bridge.session.state is VoiceState.IDLE
    assert [e.type for _, e in rt.bus.replay(type_prefix="gateway")] == ["gateway.halted"]
    stt.push("echo still there?")
    run(bridge.handle_wake())
    assert tts.spoken[-1] == "Everything is stopped."


def test_new_wake_while_speaking_interrupts_and_starts_a_new_turn(tmp_path):
    rt, bridge, _stt, tts = build(
        tmp_path, [["echo one. two. three."], ["echo new"]], seconds_per_phrase=0.4
    )

    async def scenario():
        first = asyncio.create_task(bridge.handle_wake())
        await asyncio.sleep(0.1)
        await bridge.handle_wake()  # wake during speech
        await first

    run(scenario())
    assert tts.spoken == ["One.", "New"] and bridge.session.state is VoiceState.IDLE
    assert types(rt).count("voice.barge_in") == 1 and types(rt).count("voice.wake_ack") == 2


def test_follow_up_mode_listens_again_until_silence(tmp_path):
    rt, bridge, _stt, tts = build(tmp_path, [["echo hi"], ["echo again"]], follow_up=True)
    run(bridge.handle_wake())  # third listen gets the empty transcript -> idle
    assert tts.spoken == ["Hi", "Again"]
    t = types(rt)
    assert t.count("voice.follow_up") == 2 and t[-1] == "voice.idle"


def test_telemetry_percentiles_and_budget_flags(tmp_path):
    rt = CoreRuntime.build(f"sqlite:///{tmp_path / 't.db'}", provider="none")
    tel = LatencyTelemetry(rt.bus)
    for ms in (10, 20, 30, 400):
        run(tel.record("wake_ack", ms))
    s = tel.summary()["wake_ack"]
    assert s["count"] == 4 and s["p50_ms"] == 20 and s["p95_ms"] == 400 and s["budget_ms"] == 250
    flags = [e.payload["within_budget"] for _, e in rt.bus.replay(type_prefix="telemetry")]
    assert flags == [True, True, True, False]
    assert percentile([], 50) == 0.0 and tel.since("never") is None
    assert run(tel.record_since("first_audio", "never")) is None


def test_fake_wake_and_run_forever(tmp_path):
    _rt, bridge, _stt, tts = build(tmp_path, [["echo loop"]])

    async def scenario():
        task = asyncio.create_task(bridge.run_forever())
        await asyncio.sleep(0.01)
        bridge.wake.trigger()
        for _ in range(50):
            await asyncio.sleep(0.02)
            if tts.spoken:
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())
    assert tts.spoken == ["Loop"]
