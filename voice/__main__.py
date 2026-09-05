"""Run the voice loop against an in-process Core:  python -m voice

Uses the prototype adapters (openWakeWord, faster-whisper, Kokoro). Set JARVIS_PROVIDER as for
``python -m core``; JARVIS_VOICE_FAKE=1 runs a keyboard-driven fake loop without audio hardware.
"""

from __future__ import annotations

import asyncio
import os

from core.runtime import CoreRuntime

from voice.session import VoiceBridge


async def _fake_loop(runtime: CoreRuntime) -> None:
    from voice.fakes import FakeSTT, FakeTTS, FakeWake

    wake, stt, tts = FakeWake(), FakeSTT(), FakeTTS(seconds_per_phrase=0.3)
    bridge = VoiceBridge(runtime, wake=wake, stt=stt, tts=tts, follow_up=False)
    print("fake voice: type a command (or 'stop'), empty line to quit")
    while True:
        text = await asyncio.to_thread(input, "you> ")
        if not text:
            return
        stt.push(text)
        await bridge.handle_wake()
        print("jarvis>", " ".join(tts.spoken[-3:]))
        print("latency:", bridge.telemetry.summary())


async def _real_loop(runtime: CoreRuntime) -> None:
    from voice.adapters.prototype import PrototypeSTT, PrototypeTTS, PrototypeWake

    bridge = VoiceBridge(runtime, wake=PrototypeWake(), stt=PrototypeSTT(), tts=PrototypeTTS())
    await bridge.run_forever()


def main() -> None:
    runtime = CoreRuntime.build()
    runtime.recover()
    if os.environ.get("JARVIS_VOICE_FAKE") == "1":
        asyncio.run(_fake_loop(runtime))
    else:
        asyncio.run(_real_loop(runtime))


if __name__ == "__main__":
    main()
