"""VoiceSession state machine and VoiceBridge (SPEC §9, Phase 5 steps 29-35).

    IDLE -> WAKE_ACK -> LISTENING -> THINKING -> SPEAKING -> FOLLOW_UP -> (LISTENING | IDLE)

Fluid-first rules (PERFORMANCE.md): the wake acknowledgement is emitted *before* any Core work;
TTS streams phrase by phrase; barge-in ("Jarvis, stop" / a new wake) stops audio at once and, for
an explicit stop phrase, pulls the Core's kill switch. The bridge never executes tools itself: every
transcript goes through the same text-command pipeline as the HTTP API.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any

from core.api.commands import run_text_command
from core.events.bus import EventBus
from core.events.envelope import DEFAULT_USER_ID, Event, Priority, Sensitivity
from core.runtime import CoreRuntime

from voice.interfaces import SpeechToText, TextToSpeech, WakeEvent, WakeWordDetector
from voice.personality import SpokenStyle, split_phrases
from voice.telemetry import LatencyTelemetry

SOURCE = "voice-session"
_STOP = re.compile(
    r"^\s*(jarvis[, ]+)?(stop( everything)?|stopp|halt|abbruch|sei still|be quiet)\s*[.!]?\s*$",
    re.I,
)


class VoiceState(StrEnum):
    IDLE = "idle"
    WAKE_ACK = "wake_ack"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    FOLLOW_UP = "follow_up"


_TRANSITIONS: dict[VoiceState, frozenset[VoiceState]] = {
    VoiceState.IDLE: frozenset({VoiceState.WAKE_ACK}),
    VoiceState.WAKE_ACK: frozenset({VoiceState.LISTENING, VoiceState.IDLE}),
    VoiceState.LISTENING: frozenset({VoiceState.THINKING, VoiceState.IDLE}),
    VoiceState.THINKING: frozenset({VoiceState.SPEAKING, VoiceState.IDLE}),
    VoiceState.SPEAKING: frozenset({VoiceState.FOLLOW_UP, VoiceState.IDLE, VoiceState.LISTENING}),
    VoiceState.FOLLOW_UP: frozenset({VoiceState.LISTENING, VoiceState.IDLE}),
}


class VoiceSession:
    """Tracks the conversational state and emits ``voice.<state>`` events for HUD/mobile."""

    def __init__(
        self, bus: EventBus, *, device_id: str | None = None, user_id: str = DEFAULT_USER_ID
    ) -> None:
        self._bus = bus
        self.device_id = device_id
        self.user_id = user_id
        self.state = VoiceState.IDLE
        self.turn_id: str | None = None

    async def transition(self, to: VoiceState, **payload: Any) -> None:
        if to not in _TRANSITIONS[self.state]:
            raise ValueError(f"voice session: {self.state.value} -> {to.value} is not allowed")
        payload = {"from": self.state.value, "to": to.value, **payload}
        self.state = to
        await self._bus.publish(
            Event.new(
                f"voice.{to.value}",
                SOURCE,
                payload,
                correlation_id=self.turn_id or "voice",
                user_id=self.user_id,
                device_id=self.device_id,
                sensitivity=Sensitivity.PRIVATE,
                priority=Priority.URGENT if to is VoiceState.WAKE_ACK else Priority.NORMAL,
            )
        )

    async def emit(self, kind: str, **payload: Any) -> None:
        await self._bus.publish(
            Event.new(
                f"voice.{kind}",
                SOURCE,
                payload,
                correlation_id=self.turn_id or "voice",
                user_id=self.user_id,
                device_id=self.device_id,
            )
        )


CommandFn = Callable[[str], Awaitable[dict[str, Any]]]


class VoiceBridge:
    """Wires wake -> STT -> Core command -> TTS with barge-in and telemetry."""

    def __init__(
        self,
        runtime: CoreRuntime,
        *,
        wake: WakeWordDetector,
        stt: SpeechToText,
        tts: TextToSpeech,
        device_id: str | None = "voice",
        device_trusted: bool = True,
        style: SpokenStyle | None = None,
        follow_up: bool = True,
        command_fn: CommandFn | None = None,
        telemetry: LatencyTelemetry | None = None,
    ) -> None:
        self.runtime = runtime
        self.wake = wake
        self.stt = stt
        self.tts = tts
        self.device_id = device_id
        self.device_trusted = device_trusted
        self.style = style or SpokenStyle()
        self.follow_up_enabled = follow_up
        self.session = VoiceSession(runtime.bus, device_id=device_id)
        self.telemetry = telemetry or LatencyTelemetry(runtime.bus)
        self._command = command_fn or self._default_command
        self._speak_task: asyncio.Task | None = None
        self._interrupted = False

    # -- one conversational turn --------------------------------------------------------------

    async def handle_wake(self, wake: WakeEvent | None = None) -> None:
        """Called when the wake word fired: acknowledge first, then listen, think, speak."""
        self.telemetry.mark("wake_detected")
        if self.session.state is not VoiceState.IDLE:
            await self.barge_in(reason="wake")  # a new wake while busy = interrupt
            if self.session.state is not VoiceState.IDLE:
                await self.session.transition(VoiceState.IDLE, reason="wake while busy")
        self.session.turn_id = None
        self._interrupted = False
        await self.session.transition(VoiceState.WAKE_ACK, score=wake.score if wake else None)
        await self.telemetry.record_since("wake_ack", "wake_detected")
        await self.session.transition(VoiceState.LISTENING)
        await self._listen_and_answer()

    async def _listen_and_answer(self) -> None:
        text = await self._listen()
        if not text:
            await self.session.transition(VoiceState.IDLE, reason="nothing heard")
            return
        if _STOP.match(text):
            await self.barge_in(reason=f"stop phrase: {text}")  # also parks the session in IDLE
            await self.runtime.gateway.halt(f"voice stop: {text}")
            return
        await self.session.transition(VoiceState.THINKING, transcript=text)
        self.telemetry.mark("final_transcript")
        result = await self._command(text)
        self.session.turn_id = result.get("mission_id") or self.session.turn_id
        reply = self.render_reply(result)
        await self.session.transition(VoiceState.SPEAKING, reply=reply, status=result.get("status"))
        await self.speak_streamed(reply)
        if not self._interrupted and self.session.state is VoiceState.SPEAKING:
            if self.follow_up_enabled:
                await self.session.transition(VoiceState.FOLLOW_UP)
                await self.session.transition(VoiceState.LISTENING)
                await self._listen_and_answer()
            else:
                await self.session.transition(VoiceState.IDLE, reason="done")

    async def _listen(self) -> str:
        final = ""
        async for t in self.stt.listen():
            await self.session.emit(
                "transcript", text=t.text, final=t.final, confidence=t.confidence
            )
            if t.final:
                final = t.text.strip()
                break
        return final

    # -- speaking / barge-in -------------------------------------------------------------------

    async def speak_streamed(self, text: str) -> None:
        """Phrase-level streaming: first audio starts after the first phrase, not the full text."""
        phrases = split_phrases(text) or [self.style.done_phrase]
        first = True
        for phrase in phrases:
            if self._interrupted or self.session.state is not VoiceState.SPEAKING:
                break
            if first:
                await self.telemetry.record_since("first_audio", "final_transcript")
                first = False
            self._speak_task = asyncio.ensure_future(self.tts.speak(phrase))
            try:
                await self._speak_task
            except asyncio.CancelledError:
                if self._interrupted:  # barge-in cancelled the phrase: stop speaking, keep running
                    break
                raise  # the whole turn was cancelled from outside: propagate
            finally:
                self._speak_task = None

    async def barge_in(self, *, reason: str) -> float | None:
        """Stop audio now; returns the measured stop latency in ms."""
        self.telemetry.mark("barge_in")
        self._interrupted = True  # set before yielding so the running turn cannot continue
        await self.tts.stop()
        task = self._speak_task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        ms = await self.telemetry.record_since("barge_in_stop", "barge_in")
        await self.session.emit("barge_in", reason=reason, stop_ms=ms)
        if self.session.state is not VoiceState.IDLE:
            await self.session.transition(VoiceState.IDLE, reason=reason)
        return ms

    # -- core bridge ---------------------------------------------------------------------------

    async def _default_command(self, text: str) -> dict[str, Any]:
        self.telemetry.mark("dispatch")
        result = await run_text_command(
            self.runtime,
            text,
            user_id=self.session.user_id,
            device_id=self.device_id,
            device_trusted=self.device_trusted,
            source="voice",
        )
        if result.get("route") == "capability":
            await self.telemetry.record_since(
                "local_dispatch", "dispatch", correlation_id=result.get("mission_id")
            )
        return result

    def render_reply(self, result: dict[str, Any]) -> str:
        """Turn a command result into what JARVIS says (SPEC §9.1: no filler, 'Done.')."""
        status = result.get("status")
        serious = SpokenStyle.is_serious(str(result.get("error") or ""))
        if result.get("route") == "stop":
            return "Stopped."
        if status == "completed":
            r = result.get("result")
            if isinstance(r, dict):
                text = r.get("text") or r.get("now") or ""
                if not text and r.get("opened"):
                    text = ""
            else:
                text = str(r or "")
            return self.style.apply(text, serious=serious)
        if status == "waiting_for_approval":
            return "That needs your approval. I have put it on your screen."
        if status == "blocked":
            return "I cannot do that yet: no reasoning provider is configured."
        if status == "halted":
            return "Everything is stopped."
        return self.style.apply(f"That did not work. {result.get('error') or ''}", serious=serious)

    # -- main loop -----------------------------------------------------------------------------

    async def run_forever(self) -> None:
        while True:
            wake = await self.wake.wait_for_wake()
            await self.handle_wake(wake)
