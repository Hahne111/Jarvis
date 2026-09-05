"""Voice component interfaces (SPEC §9, Phase 5 steps 29-32).

Everything is async and streaming-friendly so the session loop never blocks on audio work
(PERFORMANCE.md §4, Development Law 7). Implementations: ``voice.fakes`` (tests/offline) and
``voice.adapters.prototype`` (the existing openWakeWord / faster-whisper / Kokoro code).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol


@dataclass(frozen=True)
class WakeEvent:
    keyword: str = "hey_jarvis"
    score: float = 1.0
    device_id: str | None = None
    detected_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class Transcript:
    text: str
    final: bool
    confidence: float = 1.0
    language: str | None = None


class WakeWordDetector(Protocol):
    async def wait_for_wake(self) -> WakeEvent:
        """Block until the wake word is heard. Must be cancellable."""
        ...


class SpeechToText(Protocol):
    def listen(self) -> AsyncIterator[Transcript]:
        """Yield partial transcripts and finally one ``final=True`` transcript for the turn."""
        ...


class TextToSpeech(Protocol):
    async def speak(self, phrase: str) -> None:
        """Speak one phrase; returns when the phrase finished or ``stop()`` interrupted it."""
        ...

    async def stop(self) -> None:
        """Stop audio immediately (< 150 ms budget). Idempotent."""
        ...

    @property
    def speaking(self) -> bool: ...


class TurnDetector(Protocol):
    """VAD / end-of-turn policy (step 30): decides when the user has finished speaking."""

    def end_of_turn(self, *, silence_ms: int, speech_seen: bool, partial: str) -> bool: ...


@dataclass(frozen=True)
class SilenceTurnDetector:
    """Default: a turn ends after ``silence_after_speech_ms`` of silence once speech was heard,
    a bit longer when the partial text looks unfinished (SPEC §27 #33 sentence completion)."""

    silence_after_speech_ms: int = 800
    unfinished_extra_ms: int = 600
    max_silence_without_speech_ms: int = 6000

    def end_of_turn(self, *, silence_ms: int, speech_seen: bool, partial: str) -> bool:
        if not speech_seen:
            return silence_ms >= self.max_silence_without_speech_ms
        limit = self.silence_after_speech_ms
        if partial.rstrip().endswith((",", "und", "and", "aber", "but", "-")):
            limit += self.unfinished_extra_ms
        return silence_ms >= limit
