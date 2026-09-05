"""Deterministic Intent Router for Core 0.1.

Fast Path (PERFORMANCE.md §2): a handful of local rules map text to mock capabilities.
Everything else is routed to ``agent`` - the Claude/Agent Runtime arrives in Phase 3
(Commit 010 adds the provider interface). No model is involved here, by design.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from core.capabilities.registry import CapabilityRegistry


@dataclass(frozen=True)
class Intent:
    kind: str  # "capability" | "agent" | "stop"
    capability: str | None = None
    args: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "capability": self.capability,
            "args": self.args,
            "confidence": self.confidence,
            "text": self.text,
        }


_STOP = re.compile(r"^\s*(jarvis[, ]+)?(stop( everything)?|stopp|halt|abbruch)\s*[.!]?\s*$", re.I)
_ECHO = re.compile(r"^\s*(echo|sag|say)\s+(?P<text>.+?)\s*$", re.I | re.S)
_CLOCK = re.compile(r"^\s*(clock|time|uhrzeit|wie sp[aä]t ist es|what time is it)\s*\??\s*$", re.I)
_OPEN = re.compile(r"^\s*(open|öffne|oeffne)\s+(?P<url>https?://\S+)\s*$", re.I)


class IntentRouter:
    def __init__(self, capabilities: CapabilityRegistry) -> None:
        self._caps = capabilities

    def route(self, text: str) -> Intent:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("command text must be a non-empty string")
        if _STOP.match(text):
            return Intent("stop", text=text)
        if (m := _ECHO.match(text)) and "mock.echo" in self._caps:
            return Intent("capability", "mock.echo", {"text": m.group("text")}, text=text)
        if _CLOCK.match(text) and "mock.clock" in self._caps:
            return Intent("capability", "mock.clock", {}, text=text)
        if (m := _OPEN.match(text)) and "mock.open_url" in self._caps:
            return Intent("capability", "mock.open_url", {"url": m.group("url")}, text=text)
        return Intent("agent", confidence=0.0, text=text)
