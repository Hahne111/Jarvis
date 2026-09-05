# ADR-0001: Neuer JARVIS Core entsteht neben dem bestehenden Voice-Prototyp

- **Status:** Accepted
- **Datum:** 2026-09-05
- **Bezug zur PDF:** Blueprint 1.0 §20 (Repository-Struktur), §21 Phase 0/1, §31 (Delta zur PDF)

## Kontext
Die PDF beschreibt ein neues Monorepo (`core/`, `adapters/`, `voice/`, `apps/`, …). Dieses Repository enthält jedoch bereits einen funktionierenden Voice-Prototyp: das Python-Paket `jarvis/` (Wake Word → Whisper STT → LLM mit 32 Tools → Kokoro TTS, FastAPI-Web-UI, 33 Tests) mit Installern für Windows und macOS. Dieser Prototyp ist das, was „Voice/HUD/Desktop-Tools“ heute schon kann, verletzt aber die Core-Prinzipien der PDF: Tools werden direkt nach LLM-Tool-Call ausgeführt, ohne Permission Engine, Execution Gateway, Verifier, Event Bus oder persistente Missionen.

Ein Big-Bang-Umbau des Prototyps würde funktionierende Features zerstören und gegen „Build core before spectacle“ verstoßen.

## Entscheidung
1. Der bestehende Prototyp bleibt als **unverändertes, lauffähiges Paket `jarvis/`** erhalten (inkl. `config.yaml`, `install.*`, `start.*`, `tests/test_*.py`). Er ist Referenz und späterer Lieferant für `voice/` (wake/stt/tts) und `adapters/desktop/`.
2. Der neue Core wird gemäß PDF **zusätzlich** als Paket `core/` aufgebaut (Phase 1 ff.), mit eigenen Tests unter `tests/core/`. Adapter folgen unter `adapters/`, Voice-Module unter `voice/`, sobald sie hinter den Core gezogen werden.
3. Migration erfolgt **capability-weise**: Eine Fähigkeit des Prototyps gilt erst dann als „im Core“, wenn sie über Capability Registry, Permission Engine, Execution Gateway und Verifier läuft und Events emittiert. Bis dahin ist der Prototyp ein isolierter Legacy-Pfad (siehe SECURITY.md §8).
4. Lint/Format-Regeln greifen streng für neuen Code (`core/`, `adapters/`, `voice/`, `tests/core/`); für `jarvis/` und die bestehenden Tests gelten nur Fatal-Regeln (Syntaxfehler, undefinierte Namen), damit kein ungefragtes Reformatieren stattfindet.
5. Setup-Befehl für Phase 0 bleibt `./install.sh` (macOS/Linux) bzw. `install.bat` (Windows); Tests laufen mit `pytest`. Python-Ziel für den Core ist 3.12 (PDF §4.3); der Prototyp unterstützt weiterhin 3.11+.

## Konsequenzen
- Positiv: Nichts Bestehendes bricht; der Core kann testgetrieben ohne Hardware (Mikrofon, GPU) entstehen; klare Grenze zwischen „Legacy-Prototyp“ und „Core-konform“.
- Negativ: Zeitweise zwei parallele Wege (Prototyp-Tool-Loop und Core). Doppelstrukturen sind nur während der Migration erlaubt und werden pro Capability abgebaut.
- Folgearbeit: Wenn eine Capability migriert ist, wird sie im Prototyp durch den Core-Aufruf ersetzt oder entfernt (eigener Commit, eigene Tests).

## Alternativen
- **Prototyp sofort in die Zielstruktur umziehen:** verworfen, hoher Bruchrisiko ohne Core-Nutzen.
- **Neues, leeres Repository:** verworfen, Prototyp-Wissen (Wake/STT/TTS/Desktop-Tools, Installer, Tests) ginge verloren bzw. müsste dupliziert werden.
