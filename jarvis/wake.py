from __future__ import annotations
from pathlib import Path
import threading
import time
import numpy as np
import yaml

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"


def _load_config():
    with open(_CONFIG_PATH) as f:
        return yaml.safe_load(f)


# ─── Mic pause/resume for STT recording ───
# When STT needs to record, it pauses the wake word mic so both don't fight
# over the same hardware device. Wake word detection resumes after recording.

_mic_pause = threading.Event()


def pause_wake_mic() -> None:
    """Pause wake word mic stream to free it for STT recording."""
    _mic_pause.set()


def resume_wake_mic() -> None:
    """Resume wake word mic stream after STT recording is done."""
    _mic_pause.clear()


def listen_for_wake_word(callback) -> None:
    """
    Continuously listen for wake word. Calls callback() when detected.
    Blocking — runs forever in the calling thread.

    - IDLE: wake word → start callback (listen for command)
    - BUSY: wake word → abort (stop talking/processing)

    Short cooldown after normal wake prevents "Yes?" echo from re-triggering.
    Mic is paused/resumed via pause_wake_mic()/resume_wake_mic() during STT.
    """
    import pyaudio
    from openwakeword.model import Model

    cfg = _load_config()["wake_word"]
    oww = Model(wakeword_models=[cfg["model"]], inference_framework="onnx")

    audio = pyaudio.PyAudio()
    mic = audio.open(
        format=pyaudio.paInt16,
        channels=1,
        rate=16000,
        input=True,
        frames_per_buffer=cfg["chunk_size"],
    )

    _busy = threading.Lock()
    _ignore_until = [0.0]

    print("[Jarvis] Listening for wake word...")
    try:
        while True:
            # ── Mic pause: STT is recording, yield the hardware ──
            if _mic_pause.is_set():
                if mic.is_active():
                    mic.stop_stream()
                    print("[Wake] Mic paused for STT recording.")
                while _mic_pause.is_set():
                    time.sleep(0.05)
                # Resume after STT is done
                mic.start_stream()
                oww.reset()  # Clear stale predictions
                print("[Wake] Mic resumed.")
                continue

            try:
                pcm = np.frombuffer(mic.read(cfg["chunk_size"]), dtype=np.int16)
            except Exception:
                time.sleep(0.05)
                continue

            predictions = oww.predict(pcm)
            score = predictions.get(cfg["model"], 0)

            if score < cfg["threshold"]:
                continue

            oww.reset()
            now = time.time()

            # Skip if in cooldown window (prevents echo re-trigger)
            if now < _ignore_until[0]:
                continue

            if _busy.locked():
                # BUSY: wake word = stop everything
                _ignore_until[0] = now + 3.0
                from jarvis.main import abort_all
                abort_all()
                print("[Jarvis] Stopped. (voice interrupt)")
            else:
                # IDLE: normal wake — 2s cooldown to skip "Yes?" echo
                _ignore_until[0] = now + 2.0

                def _run():
                    with _busy:
                        callback()

                t = threading.Thread(target=_run, daemon=True)
                t.start()
    finally:
        mic.stop_stream()
        mic.close()
        audio.terminate()
