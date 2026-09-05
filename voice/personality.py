"""Voice Personality Contract (SPEC §9.1, Phase 5 step 34), applied to *spoken* output only.

- No-filler rule: strip "Certainly", "Gladly", "Natürlich", "Sehr gerne" ... at the start.
- Simple actions get "Done." (or a sound cue) instead of a sentence.
- Serious contexts (emergency, medical, security, grief) -> humour off is a flag for the model;
  here we only keep the text short and plain.
- Adaptive length: spoken replies are capped by sentences, the full text stays in the HUD/log.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_FILLERS = re.compile(
    r"^\s*(certainly|sure|of course|gladly|absolutely|natürlich|selbstverständlich|sehr gerne|"
    r"gerne|klar|okay|ok|alright|great question|good question)[,!.:\s]+",
    re.IGNORECASE,
)
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_SERIOUS = re.compile(
    r"\b(notruf|emergency|unfall|accident|medic|arzt|krank|verletz|blut|feuer|fire|einbruch|"
    r"intruder|security breach|passwort|password|trauer|gestorben|died|death|suizid|suicide)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SpokenStyle:
    max_sentences: int = 3
    done_phrase: str = "Done."

    @staticmethod
    def is_serious(text: str) -> bool:
        return bool(_SERIOUS.search(text or ""))

    def apply(self, text: str | None, *, serious: bool = False) -> str:
        text = (text or "").strip()
        if not text:
            return self.done_phrase
        while True:
            stripped = _FILLERS.sub("", text).strip()
            if stripped == text:
                break
            text = stripped
        if not text:
            return self.done_phrase
        limit = self.max_sentences if not serious else self.max_sentences + 2
        sentences = _SENTENCE.split(text)
        text = " ".join(sentences[:limit]).strip()
        return text[0].upper() + text[1:] if text else self.done_phrase


def split_phrases(text: str) -> list[str]:
    """Phrase-level streaming units for TTS: sentences, long ones split at commas/semicolons."""
    out: list[str] = []
    for sentence in _SENTENCE.split((text or "").strip()):
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(sentence) <= 120:
            out.append(sentence)
            continue
        buf = ""
        for part in re.split(r"(?<=[,;:])\s+", sentence):
            if buf and len(buf) + len(part) > 120:
                out.append(buf.strip())
                buf = part
            else:
                buf = f"{buf} {part}".strip()
        if buf:
            out.append(buf.strip())
    return out
