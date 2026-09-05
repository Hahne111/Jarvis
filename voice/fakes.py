"""Deterministic fakes for the voice interfaces (tests, offline development, HUD demos)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from voice.interfaces import Transcript, WakeEvent


class FakeWake:
    """``trigger()`` fires the next ``wait_for_wake()``."""

    def __init__(self) -> None:
        self._event: asyncio.Event | None = None  # created inside the running loop
        self._pending = False

    def trigger(self) -> None:
        self._pending = True
        if self._event is not None:
            self._event.set()

    async def wait_for_wake(self) -> WakeEvent:
        if not self._pending:
            self._event = asyncio.Event()
            await self._event.wait()
            self._event = None
        self._pending = False
        return WakeEvent()


class FakeSTT:
    """Yields scripted turns: each turn is a list of partial texts ending in the final text."""

    def __init__(self, turns: list[list[str]] | None = None, *, delay_s: float = 0.0) -> None:
        self.turns = list(turns or [])
        self.delay_s = delay_s

    def push(self, *texts: str) -> None:
        self.turns.append(list(texts))

    async def listen(self) -> AsyncIterator[Transcript]:  # type: ignore[override]
        if not self.turns:
            yield Transcript("", final=True, confidence=0.0)
            return
        turn = self.turns.pop(0)
        for text in turn[:-1]:
            if self.delay_s:
                await asyncio.sleep(self.delay_s)
            yield Transcript(text, final=False, confidence=0.6)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        yield Transcript(turn[-1], final=True, confidence=0.95)


class FakeTTS:
    """Records phrases; each phrase 'plays' for ``seconds_per_phrase`` unless stopped."""

    def __init__(self, seconds_per_phrase: float = 0.05) -> None:
        self.seconds_per_phrase = seconds_per_phrase
        self.spoken: list[str] = []
        self.completed: list[str] = []
        self.stops = 0
        self._stop: asyncio.Event | None = None  # created per phrase inside the running loop
        self._speaking = False

    @property
    def speaking(self) -> bool:
        return self._speaking

    async def speak(self, phrase: str) -> None:
        self.spoken.append(phrase)
        self._speaking = True
        self._stop = asyncio.Event()
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=self.seconds_per_phrase)
        except TimeoutError:
            self.completed.append(phrase)
        finally:
            self._speaking = False

    async def stop(self) -> None:
        self.stops += 1
        if self._stop is not None:
            self._stop.set()
