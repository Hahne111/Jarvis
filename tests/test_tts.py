# jarvis/tests/test_tts.py
import pytest
import numpy as np
from unittest.mock import patch, MagicMock, call

STUB_CONFIG = {
    "tts": {"voice": "af_heart", "speed": 1.1}
}

def test_tts_speak_returns_audio_bytes():
    """TTS should return audio bytes for valid text."""
    mock_audio = np.ones(100, dtype=np.float32) * 0.1
    with patch("jarvis.tts._get_pipeline") as mock_get_pipeline, \
         patch("jarvis.tts._get_voice", return_value=MagicMock()), \
         patch("jarvis.tts._load_config", return_value=STUB_CONFIG):
        mock_pipeline = MagicMock()
        mock_pipeline.return_value = iter([(None, None, mock_audio)])
        mock_get_pipeline.return_value = mock_pipeline
        from jarvis.tts import speak_to_bytes
        result = speak_to_bytes("Hello, sir.")
        assert isinstance(result, bytes)
        assert len(result) > 0

def test_tts_rejects_empty_string():
    """TTS should raise ValueError for empty input."""
    from jarvis.tts import speak_to_bytes
    with pytest.raises(ValueError):
        speak_to_bytes("")

def test_tts_rejects_whitespace_only():
    """TTS should raise ValueError for whitespace-only input."""
    from jarvis.tts import speak_to_bytes
    with pytest.raises(ValueError):
        speak_to_bytes("   ")

def test_speak_calls_sounddevice():
    """speak() should call sd.play to play audio."""
    mock_audio = np.ones(100, dtype=np.float32) * 0.1
    mock_stream = MagicMock()
    # Stream is active on first call, then inactive — exits the poll loop
    mock_stream.active = False
    with patch("jarvis.tts._get_pipeline") as mock_get_pipeline, \
         patch("jarvis.tts._get_voice", return_value=MagicMock()), \
         patch("jarvis.tts._load_config", return_value=STUB_CONFIG), \
         patch("jarvis.tts.sd") as mock_sd:
        mock_sd.get_stream.return_value = mock_stream
        mock_pipeline = MagicMock()
        mock_pipeline.return_value = iter([(None, None, mock_audio)])
        mock_get_pipeline.return_value = mock_pipeline
        from jarvis.tts import speak
        speak("Engaging thrusters.")
        mock_sd.play.assert_called_once()
