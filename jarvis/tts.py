from pathlib import Path
import re
import yaml
import numpy as np
import sounddevice as sd
import threading

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"

def _load_config():
    with open(_CONFIG_PATH) as f:
        return yaml.safe_load(f)

_pipeline = None
_voice_tensor = None
_speaking = threading.Event()  # set while audio is playing
_interrupt = threading.Event()  # set to stop streaming

_LOCAL_PTH = Path.home() / "Downloads" / "kokoro-v1_0.pth"

def _get_pipeline():
    global _pipeline
    if _pipeline is None:
        from kokoro import KPipeline, KModel
        repo_id = "hexgrad/Kokoro-82M"
        if _LOCAL_PTH.exists():
            kmodel = KModel(repo_id=repo_id, model=str(_LOCAL_PTH)).to("cpu").eval()
            _pipeline = KPipeline(lang_code="a", repo_id=repo_id, model=kmodel)
        else:
            _pipeline = KPipeline(lang_code="a", repo_id=repo_id)
    return _pipeline

def _get_voice():
    """Load voice tensor once and cache it."""
    global _voice_tensor
    if _voice_tensor is None:
        import torch
        cfg = _load_config()
        voice_name = cfg["tts"]["voice"]
        local_voice = _LOCAL_PTH.parent / f"{voice_name}.pt"
        if local_voice.exists():
            _voice_tensor = torch.load(str(local_voice), weights_only=True)
        else:
            from huggingface_hub import hf_hub_download
            try:  # prefer the local HF cache; download on first run
                path = hf_hub_download(
                    repo_id="hexgrad/Kokoro-82M",
                    filename=f"voices/{voice_name}.pt",
                    local_files_only=True,
                )
            except Exception:
                path = hf_hub_download(
                    repo_id="hexgrad/Kokoro-82M",
                    filename=f"voices/{voice_name}.pt",
                )
            _voice_tensor = torch.load(path, weights_only=True)
    return _voice_tensor


def _split_sentences(text: str) -> list[str]:
    """Split text into sentences for streaming TTS."""
    parts = re.split(r'(?<=[.!?])\s+', text)
    return [p.strip() for p in parts if p.strip()]


def speak_to_bytes(text: str) -> bytes:
    """Convert text to PCM audio bytes using Kokoro TTS."""
    if not text or not text.strip():
        raise ValueError("Text cannot be empty")
    pipeline = _get_pipeline()
    voice = _get_voice()
    cfg = _load_config()
    speed = cfg["tts"]["speed"]
    audio_chunks = []
    for _, _, audio in pipeline(text, voice=voice, speed=speed):
        if audio is not None:
            audio_chunks.append(audio)
    if not audio_chunks:
        return b""
    combined = np.concatenate(audio_chunks)
    pcm = (combined * 32767).astype(np.int16)
    return pcm.tobytes()


def is_speaking() -> bool:
    """Check if Jarvis is currently speaking."""
    return _speaking.is_set()


def stop_speaking() -> None:
    """Interrupt current speech playback — instant."""
    _interrupt.set()
    sd.stop()
    _speaking.clear()


def _wait_or_interrupt() -> bool:
    """Poll until audio finishes or interrupt is set. Returns True if interrupted."""
    while sd.get_stream() and sd.get_stream().active:
        if _interrupt.is_set():
            sd.stop()
            return True
        _interrupt.wait(0.05)  # 50ms poll
    return _interrupt.is_set()


def speak(text: str) -> None:
    """Speak text aloud. Can be interrupted via stop_speaking()."""
    _interrupt.clear()
    audio_bytes = speak_to_bytes(text)
    if not audio_bytes:
        return
    audio = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32767
    _speaking.set()
    sd.play(audio, samplerate=24000)
    _wait_or_interrupt()
    _speaking.clear()


def speak_streamed(text: str) -> None:
    """Speak text sentence by sentence — starts playing before full generation is done.
    Can be interrupted mid-stream via stop_speaking()."""
    _interrupt.clear()
    sentences = _split_sentences(text)
    if not sentences:
        return

    # Short text — just speak normally
    if len(sentences) == 1:
        speak(text)
        return

    pipeline = _get_pipeline()
    voice = _get_voice()
    cfg = _load_config()
    speed = cfg["tts"]["speed"]

    _speaking.set()
    try:
        for sentence in sentences:
            if _interrupt.is_set():
                break
            chunks = []
            for _, _, audio in pipeline(sentence, voice=voice, speed=speed):
                if _interrupt.is_set():
                    break
                if audio is not None:
                    chunks.append(audio)
            if not chunks or _interrupt.is_set():
                break
            combined = np.concatenate(chunks)
            sd.play(combined, samplerate=24000)
            if _wait_or_interrupt():
                break
    finally:
        sd.stop()  # ensure audio stops even if we break mid-play
        _speaking.clear()
        _interrupt.clear()
