# ADR-0002: Modularer Monolith in Python 3.12 + asyncio; Claude nur als austauschbarer Provider

- **Status:** Accepted
- **Datum:** 2026-09-05
- **Bezug zur PDF:** Blueprint 1.0 §4.1–4.3, §5, §6.2 (bestätigt)

## Kontext
Der Core braucht State Manager, Event Bus, Mission Engine, Capability Registry, Permission Engine, Execution Gateway, Verifier, Scheduler, Model Router und Audit Logger. Microservices ab Tag 1 würden Komplexität ohne Nutzen erzeugen; ein einziges Cloud-Modell als „Gehirn“ würde Vendor Lock-in und Offline-Ausfall bedeuten.

## Entscheidung
1. Version 0.x ist ein **einzelner Python-3.12-Core-Prozess** (asyncio) mit klar getrennten Modulen unter `core/` (api, state, events, missions, permissions, memory, models, agents, capabilities, verifier, scheduler) und stabilen internen Interfaces. Voice, Desktop-Agent und UI dürfen separate Prozesse sein, die über die lokale API/WebSocket sprechen.
2. Persistenz: PostgreSQL + pgvector (per Docker Compose unter `infra/docker/`). In-process Event Bus zuerst; Redis/NATS erst, wenn Verteilung es erfordert.
3. **Claude ist ein `IntelligenceProvider`.** Modell-/Anbieter-spezifische Logik lebt ausschließlich in `core/models/` bzw. Adaptern; der Claude Agent SDK wird hinter `IntelligenceProvider`/`AgentRuntime` gekapselt. Ein Model Router wählt Modell und Effort nach Schwierigkeit, Latenz, Privatsphäre und Kosten; lokale Modelle und deterministische Intents bleiben als Fast Path/Offline-Pfad möglich.
4. Kein Modell erhält direkten Zugriff auf OS, Dateien, Netzwerk oder Secrets. Alle Seiteneffekte laufen durch Permission Engine → Execution Gateway → Verifier und emittieren Events.

## Konsequenzen
- Einfacher Start, eine Testsuite, ein Deployment; spätere Extraktion von Services bleibt durch die Modulgrenzen möglich.
- Jede neue Fähigkeit wird als Capability (Manifest mit Risk Level und Verifier) registriert, nicht als freier Funktionsaufruf aus dem Modell.
- Provider-Wechsel (Opus/Sonnet/lokal) ist Konfiguration, kein Umbau.

## Alternativen
- Microservices/Message-Broker ab Start: verworfen (PDF §4.2).
- Claude Agent SDK direkt als Core-Schleife ohne eigene Interfaces: verworfen (Lock-in, keine deterministische Kontrolle).
