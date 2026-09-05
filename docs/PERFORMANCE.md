# JARVIS – Performance und Fluidität (PERFORMANCE.md)

> Aus Master Blueprint 1.0, Abschnitte 3.3, 9.2, 16. „Super flüssig“ ist eine Anforderung, keine Politur am Ende.

## 1. Fluidity Rule

Das UI läuft getrennt vom AI-Backend. Keine Claude-/Modell-Anfrage darf Animation, Eingabe, Scrollen, Globe-Rendering oder Wake-Feedback blockieren. Ziel: 60 FPS Mindestgefühl, 120 FPS auf geeigneter Hardware; WebGPU mit WebGL-2-Fallback.

## 2. Fast Path / Smart Path / Deep Path

```
FAST PATH (Millisekunden)
  Wake, Lautstärke, Licht, App öffnen, Timer, einfacher Gerätestatus
  -> lokale Intent-Regeln -> lokaler Adapter

SMART PATH (Sub-Sekunde bis Sekunden)
  Mehrdeutiger Befehl, Projektkontext, kleines Reasoning
  -> schnelles Modell / gecachter Kontext

DEEP PATH (Sekunden bis Minuten)
  Research, Coding, komplexe Mission
  -> Opus/Sonnet-Agenten -> Tools -> Verifier -> Streaming-Status
```

Simple lokale Befehle brauchen kein Claude (Exit-Kriterium Phase 5).

## 3. Latenzbudget (Zielwerte, keine Garantie)

| Messpunkt | Ziel |
|---|---|
| Wake UI/Ack nach Erkennung | < 150–250 ms wahrgenommenes Feedback |
| Stop / Barge-in | < 150 ms bis Audio stoppt |
| Local safe action dispatch | < 300 ms bis Start |
| Fast intent classification | typisch < 100 ms (lokal / kleines Modell) |
| Erstes TTS-Audio, einfache Antwort | ca. 0,5–1,0 s unter guten Bedingungen |
| HUD frame rate | 60 FPS Minimum, 120 FPS auf geeigneter Hardware |
| Long mission status | sichtbares Event spätestens alle 1–3 s, solange Aktivität besteht |

## 4. Anti-Lag-Regeln

1. UI-Renderer, Audio-Capture, Wake-Word-Worker und Core-Agenten laufen in getrennten Threads/Prozessen.
2. Keine synchrone DB-/Netzwerkoperation im UI-Thread; UI- und Audio-Threads bleiben non-blocking (Development Law 7).
3. Heavy Jobs bekommen CPU/GPU-Limits und niedrigere Priorität als Audio/HUD.
4. Context Caching, Projekt-Indexing und vorgeladene Tool-Kataloge.
5. Agentenstatus wird event-driven gepusht; kein aggressives UI-Polling.
6. Progressive Loading: Globe/3D nach Bedarf, Kerninteraktion sofort.
7. Builds dürfen Systemressourcen nicht so saturieren, dass HUD/Voice ruckeln.

## 5. Telemetrie (lokal)

Gemessen und lokal gespeichert werden mindestens: p50/p95 Wake-Latenz, Zeit bis erstes Audio, Tool-Dispatch-Latenz, Frame Time, Mission-Recovery-Zeit. Latenz-Telemetrie ist Teil von Phase 5 (Voice 0.1); Fluidity-Budgets müssen für 1.0 gemessen vorliegen (SPEC §29). Performance-Regressionen sind Teil der Regression-Suite (Phase 12).

## 6. Voice-spezifisch

Wake, STT, Intent und TTS laufen nicht als serielle Blockkette, sondern als Streaming-Pipeline: phrase-level TTS-Streaming, VAD/Turn Detection, Barge-in bricht Ausgabe sofort ab, warme Services (Modelle bleiben geladen).
