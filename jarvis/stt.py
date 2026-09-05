from pathlib import Path
import yaml
import numpy as np

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"

def _load_config():
    with open(_CONFIG_PATH) as f:
        return yaml.safe_load(f)

_model = None
_model_device = None  # track what device the model is on

try:
    from faster_whisper import WhisperModel
except ImportError:
    WhisperModel = None  # type: ignore

def _resolve_device(device: str) -> str:
    """'auto' -> 'cuda' if a CUDA device is available, else 'cpu' (macOS has no CUDA)."""
    if device != "auto":
        return device
    try:
        import ctranslate2
        return "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
    except Exception:
        return "cpu"

def _get_model(force_cpu=False):
    global _model, _model_device
    if _model is not None and not force_cpu:
        return _model
    cfg = _load_config()
    stt = cfg["stt"]
    device = "cpu" if force_cpu else _resolve_device(stt["device"])
    compute_type = "int8" if force_cpu else stt["compute_type"]
    try:
        _model = WhisperModel(stt["model"], device=device, compute_type=compute_type)
        _model_device = device
        print(f"[STT] Loaded {stt['model']} on {device}")
    except Exception as e:
        if device != "cpu":
            print(f"[STT] {device} failed to load ({e}), falling back to CPU")
            _model = WhisperModel(stt["model"], device="cpu", compute_type="int8")
            _model_device = "cpu"
            print(f"[STT] Loaded {stt['model']} on cpu")
        else:
            raise
    return _model

def transcribe_audio(audio: np.ndarray) -> str:
    """Transcribe a float32 numpy audio array (16kHz mono) to text."""
    try:
        model = _get_model()
        segments, _ = model.transcribe(audio, beam_size=5, language="en")
        return " ".join(seg.text.strip() for seg in segments).strip()
    except Exception as e:
        # If CUDA worked for loading but fails during inference, retry on CPU
        if _model_device != "cpu":
            print(f"[STT] {_model_device} transcription failed ({e}), reloading on CPU")
            try:
                model = _get_model(force_cpu=True)
                segments, _ = model.transcribe(audio, beam_size=5, language="en")
                return " ".join(seg.text.strip() for seg in segments).strip()
            except Exception as e2:
                print(f"[STT] CPU transcription also failed: {e2}")
                return ""
        print(f"[STT] Transcription failed: {e}")
        return ""

# Global abort event — set by main.py so recording can be interrupted
_abort_event = None

def set_abort_event(event) -> None:
    """Register the global abort event so recording checks it."""
    global _abort_event
    _abort_event = event

def record_until_silence(
    sample_rate: int = 16000,
    silence_threshold: float = 0.012,
    max_seconds: int = 30,
) -> np.ndarray:
    """Record microphone audio until silence is detected. Returns float32 array.
    Waits for speech to start before counting silence.
    Can be interrupted via the abort event (Esc key)."""
    import sounddevice as sd

    chunk = int(sample_rate * 0.3)  # 300ms chunks
    recording = []
    silent_chunks = 0
    silent_chunks_needed = 7  # ~2.1s of silence to stop
    heard_speech = False
    speech_threshold = 0.015  # louder than silence — confirms user is talking

    with sd.InputStream(samplerate=sample_rate, channels=1, dtype="float32") as stream:
        while True:
            # Check abort every chunk (~300ms)
            if _abort_event and _abort_event.is_set():
                print("[STT] Recording aborted.")
                break

            data, _ = stream.read(chunk)
            flat = data.flatten()
            recording.append(flat)
            rms = float(np.sqrt(np.mean(flat ** 2)))

            if rms >= speech_threshold:
                if not heard_speech:
                    print(f"[STT] Speech detected (rms={rms:.4f})")
                heard_speech = True
                silent_chunks = 0
            elif heard_speech and rms < silence_threshold:
                silent_chunks += 1

            if heard_speech and silent_chunks >= silent_chunks_needed:
                print("[STT] Silence detected, stopping recording.")
                break
            if len(recording) * chunk >= max_seconds * sample_rate:
                print(f"[STT] Max recording time reached ({max_seconds}s)")
                break

    if not recording:
        return np.zeros(chunk, dtype=np.float32)
    return np.concatenate(recording)
