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
_OPEN_APP = re.compile(
    r"^\s*(open|öffne|oeffne|start|starte|launch)\s+(?P<app>[a-z0-9 ._-]{2,40})\s*$", re.I
)
_LOCK = re.compile(
    r"^\s*(lock( the)?( screen| pc| computer)?"
    r"|sperre?( den)?( bildschirm| pc| rechner)?)\s*[.!]?\s*$",
    re.I,
)
_VOLUME = re.compile(r"^\s*(volume|lautst[aä]rke)( auf| to)?\s+(?P<level>\d{1,3})\s*%?\s*$", re.I)
_ROOM = r"[a-zäöüß][a-zäöüß_ ]{1,30}"
# Home fast path (SPEC §11 / Phase 8 step 56: offline basics without any model)
_LIGHT_A = re.compile(  # "turn the kitchen light on", "küche licht aus", "licht an"
    rf"^\s*((turn|switch|schalte?|mach)\s+)?((the|das|die)\s+)?(?P<room>{_ROOM}?)?\s*"
    r"(lights?|licht)\s+(?P<on>on|off|an|aus|ein)\s*[.!]?\s*$",
    re.I,
)
_LIGHT_B = re.compile(  # "turn on the kitchen lights", "schalte das licht im flur aus"
    rf"^\s*(turn|switch|schalte?|mach)\s+(?P<on>on|off|an|aus|ein)\s+((the|das|die)\s+)?"
    rf"(?P<room>{_ROOM}?)?\s*(lights?|licht)\s*[.!]?\s*$",
    re.I,
)
_LIGHT_C = re.compile(  # "licht an im wohnzimmer", "lights off in the kitchen"
    rf"^\s*(lights?|licht)\s+(?P<on>on|off|an|aus|ein)(\s+(im|in der|in dem|in the|in)\s+"
    rf"(?P<room>{_ROOM}))?\s*[.!]?\s*$",
    re.I,
)
_LIGHT_D = re.compile(  # "schalte das licht in der küche aus", "turn the light in the kitchen off"
    r"^\s*(turn|switch|schalte?|mach)\s+((the|das|die)\s+)?(lights?|licht)\s+"
    rf"(im|in der|in dem|in the|in)\s+(?P<room>{_ROOM}?)\s+(?P<on>on|off|an|aus|ein)\s*[.!]?\s*$",
    re.I,
)
_SCENE = re.compile(
    rf"^\s*((activate|aktiviere|start|starte)\s+)?(scene|szene)\s+(?P<name>{_ROOM})\s*[.!]?\s*$",
    re.I,
)
_HOME_STATE = re.compile(
    r"^\s*((set|setze)\s+)?(home\s*)?(state|mode|modus|status)(\s+to|\s+auf)?\s+"
    r"(?P<state>home|away|sleep|work|movie|guests|night|vacation)\s*[.!]?\s*$",
    re.I,
)
_HOME_PHRASES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"^\s*(ich bin (wieder )?(zu ?hause|da)|i'?m (back )?home)\s*[.!]?\s*$", re.I),
        "home",
    ),
    (
        re.compile(
            r"^\s*(ich (gehe|bin weg)|i'?m (leaving|out|away)|bin dann weg)\s*[.!]?\s*$", re.I
        ),
        "away",
    ),
    (re.compile(r"^\s*(gute nacht|good ?night|schlafenszeit)\s*[.!]?\s*$", re.I), "sleep"),
    (re.compile(r"^\s*(filmabend|movie (time|night|mode)|kino ?modus)\s*[.!]?\s*$", re.I), "movie"),
)
_WAKE = re.compile(  # "wake desktop", "weck den pc", "pc einschalten", "schalte den rechner ein"
    r"^\s*((wake( up)?|weck(e)?)\s+((the|den|das|my|meinen?)\s+)?(?P<t1>[a-z0-9_-]{2,32})"
    r"|(schalte?\s+)?((the|den|das|meinen?)\s+)?(?P<t2>pc|rechner|computer|desktop|server|nas)"
    r"\s+(einschalten|ein|anschalten|an))\s*[.!]?\s*$",
    re.I,
)
_WAKE_ALIASES = {
    "pc": "desktop",
    "rechner": "desktop",
    "computer": "desktop",
    "workstation": "desktop",
}
_NEWS = (
    re.compile(  # "news", "nachrichten", "what's important today", "news germany", "news about ai"
        r"^\s*((what'?s|was ist|what is)\s+(heute\s+)?(weltweit\s+)?(important|wichtig|los)"
        r"(\s+(today|heute|in the world|weltweit))?"
        r"|(show\s+(me\s+)?|zeig(e)?\s+(mir\s+)?)?(the\s+|die\s+)?"
        r"(news|nachrichten|world news|weltnachrichten|schlagzeilen|headlines))"
        r"(\s+(about|über|ueber|zu|on)\s+(?P<topic>[a-zäöü ]{2,30}))?"
        r"(\s+(from|aus|in|for|für)\s+(?P<country>[a-zäöüß .-]{2,40}))?\s*[?.!]?\s*$",
        re.I,
    )
)
_TOPIC_WORDS = {
    "ai": "ai",
    "ki": "ai",
    "artificial intelligence": "ai",
    "künstliche intelligenz": "ai",
    "tech": "tech",
    "technology": "tech",
    "technologie": "tech",
    "technik": "tech",
    "politics": "politics",
    "politik": "politics",
    "economy": "economy",
    "wirtschaft": "economy",
    "security": "security",
    "sicherheit": "security",
    "war": "security",
    "krieg": "security",
    "climate": "climate",
    "klima": "climate",
    "health": "health",
    "gesundheit": "health",
    "science": "science",
    "wissenschaft": "science",
    "sports": "sports",
    "sport": "sports",
}
_BRIEF = re.compile(
    r"^\s*((daily|morning|tages)[- ]?(brief(ing)?)|briefing|brief me|was liegt an|what'?s up today|"
    r"was steht heute an)\s*[?.!]?\s*$",
    re.I,
)
_PRIVACY = re.compile(
    r"^\s*((privacy|privat)[- ]?(mode|modus)\s*(?P<onoff>on|off|an|aus|ein)"
    r"|(guest|gäste|gaeste)[- ]?(mode|modus)\s*(?P<gonoff>on|off|an|aus|ein)?"
    r"|(?P<normal>normal(er)? (mode|modus)|privacy off|privatmodus aus))\s*[?.!]?\s*$",
    re.I,
)
_WINDOWS = re.compile(
    r"^\s*(list|show|zeige?)( the| die| alle)?( open| offenen)? (windows|fenster)\s*[?.]?\s*$", re.I
)


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
        if _LOCK.match(text) and "system.lock_screen" in self._caps:
            return Intent("capability", "system.lock_screen", {}, text=text)
        if (m := _VOLUME.match(text)) and "system.set_volume" in self._caps:
            return Intent(
                "capability", "system.set_volume", {"level": int(m.group("level"))}, text=text
            )
        if (home := self._home_intent(text)) is not None:
            return home
        if _BRIEF.match(text) and "brief.generate" in self._caps:
            return Intent("capability", "brief.generate", {"reason": "voice"}, text=text)
        if (m := _PRIVACY.match(text)) and "privacy.set" in self._caps:
            if m.group("normal") or (m.group("onoff") or "").lower() in ("off", "aus"):
                mode = "normal"
            elif m.group("onoff"):
                mode = "private"
            else:
                mode = "normal" if (m.group("gonoff") or "").lower() in ("off", "aus") else "guest"
            return Intent("capability", "privacy.set", {"mode": mode}, text=text)
        if (m := _NEWS.match(text)) and "news.top" in self._caps:
            args: dict[str, Any] = {}
            topic = (m.group("topic") or "").strip().lower()
            if topic in _TOPIC_WORDS:
                args["topic"] = _TOPIC_WORDS[topic]
            elif topic:
                args["country"] = topic  # "news about germany"
            if m.group("country"):
                args["country"] = m.group("country").strip()
            return Intent("capability", "news.top", args, text=text)
        if _WINDOWS.match(text) and "computer.list_windows" in self._caps:
            return Intent("capability", "computer.list_windows", {}, text=text)
        if (m := _OPEN_APP.match(text)) and "computer.open_app" in self._caps:
            return Intent(
                "capability",
                "computer.open_app",
                {"app": m.group("app").strip()},
                text=text,
                confidence=0.8,
            )
        return Intent("agent", confidence=0.0, text=text)

    def _home_intent(self, text: str) -> Intent | None:
        if "home.light.set" in self._caps:
            for rx in (_LIGHT_D, _LIGHT_B, _LIGHT_C, _LIGHT_A):
                if m := rx.match(text):
                    room = (m.group("room") or "").strip().lower() or "all"
                    on = m.group("on").lower() in ("on", "an", "ein")
                    return Intent(
                        "capability", "home.light.set", {"target": room, "on": on}, text=text
                    )
        if (m := _WAKE.match(text)) and "power.wake" in self._caps:
            target = (m.group("t1") or m.group("t2") or "").lower()
            target = _WAKE_ALIASES.get(target, target)
            return Intent("capability", "power.wake", {"target": target}, text=text)
        if (m := _SCENE.match(text)) and "home.scene.activate" in self._caps:
            return Intent(
                "capability", "home.scene.activate", {"target": m.group("name").strip()}, text=text
            )
        if "home.state.set" in self._caps:
            if m := _HOME_STATE.match(text):
                return Intent(
                    "capability", "home.state.set", {"state": m.group("state").lower()}, text=text
                )
            for rx, state in _HOME_PHRASES:
                if rx.match(text):
                    return Intent("capability", "home.state.set", {"state": state}, text=text)
        return None
