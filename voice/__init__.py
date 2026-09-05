"""JARVIS Voice 0.1 (SPEC §9, Phase 5): wake -> STT -> Core -> TTS with barge-in.

The voice layer is a client of the Core: it never executes anything itself. Wake, STT and TTS are
interfaces; the prototype in ``jarvis/`` is wrapped unchanged in ``voice/adapters/prototype.py``.
"""

from voice.interfaces import (
    SpeechToText,
    TextToSpeech,
    Transcript,
    TurnDetector,
    WakeEvent,
    WakeWordDetector,
)
from voice.personality import SpokenStyle
from voice.session import VoiceBridge, VoiceSession, VoiceState
from voice.telemetry import LatencyTelemetry

__all__ = [
    "LatencyTelemetry",
    "SpeechToText",
    "SpokenStyle",
    "TextToSpeech",
    "Transcript",
    "TurnDetector",
    "VoiceBridge",
    "VoiceSession",
    "VoiceState",
    "WakeEvent",
    "WakeWordDetector",
]
