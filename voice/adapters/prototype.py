"""Adapters over the existing voice prototype (``jarvis/wake.py``, ``stt.py``, ``tts.py``).

The prototype is imported lazily and *unchanged* (ADR-0001); its blocking calls run in worker
threads so the asyncio session loop stays responsive. Not exercised in CI (needs a microphone,
speakers, PortAudio and the ML models).
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator

from voice.interfaces import Transcript, WakeEvent


class PrototypeWake:
    """Runs ``jarvis.wake.listen_for_wake_word`` in a daemon thread; each detection wakes once."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[WakeEvent] = asyncio.Queue()
        self._thread: threading.Thread | None = None

    def _start(self) -> None:
        from jarvis.wake import listen_for_wake_word  # lazy: needs openwakeword + mic

        loop = self._loop
        assert loop is not None  # noqa: S101 - programming error guard

        def on_wake() -> None:
            loop.call_soon_threadsafe(self._queue.put_nowait, WakeEvent())

        self._thread = threading.Thread(
            target=listen_for_wake_word, args=(on_wake,), name="wake-word", daemon=True
        )
        self._thread.start()

    async def wait_for_wake(self) -> WakeEvent:
        if self._thread is None:
            self._loop = asyncio.get_running_loop()
            self._start()
        return await self._queue.get()


class PrototypeSTT:
    """``record_until_silence`` + ``transcribe_audio`` in a worker thread (one final transcript)."""

    def __init__(self, max_seconds: int = 30) -> None:
        self.max_seconds = max_seconds
        self._abort = threading.Event()

    def abort(self) -> None:
        self._abort.set()

    async def listen(self) -> AsyncIterator[Transcript]:  # type: ignore[override]
        from jarvis.stt import record_until_silence, set_abort_event, transcribe_audio

        self._abort.clear()
        set_abort_event(self._abort)
        audio = await asyncio.to_thread(record_until_silence, max_seconds=self.max_seconds)
        text = await asyncio.to_thread(transcribe_audio, audio)
        yield Transcript((text or "").strip(), final=True)


class PrototypeTTS:
    """Kokoro TTS via ``jarvis.tts.speak`` (blocking) with the prototype's polling interrupt."""

    def __init__(self) -> None:
        self._speaking = False

    @property
    def speaking(self) -> bool:
        return self._speaking

    async def speak(self, phrase: str) -> None:
        from jarvis.tts import speak

        self._speaking = True
        try:
            await asyncio.to_thread(speak, phrase)
        finally:
            self._speaking = False

    async def stop(self) -> None:
        from jarvis.tts import stop_speaking

        await asyncio.to_thread(stop_speaking)
