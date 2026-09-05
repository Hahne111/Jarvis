# JARVIS – Spezifikation (SPEC.md)

> Abgeleitet aus `docs/JARVIS_Master_Blueprint_1.0.pdf` (Master Blueprint 1.0, Stand 19.08.2026).
> Die PDF ist die Source of Truth. Diese Datei ist die kompakte, im Repo lesbare Fassung.
> Abweichungen von der PDF werden ausschließlich als ADR unter `docs/decisions/` festgehalten.
> Aktueller Stand/Phase: `docs/STATUS.md`. Sicherheitsregeln: `docs/SECURITY.md`. Latenz/Fluidität: `docs/PERFORMANCE.md`.

## 0. Die fünf nicht verhandelbaren Prinzipien

1. **JARVIS Core ist das Produkt.** Claude ist ein austauschbarer Intelligence Provider, nicht das System.
2. **Deterministische Kontrolle.** Rechte, Sicherheit, Memory, Geräteidentität und Tool-Ausführung werden von unserer Software kontrolliert, nie nur durch einen Prompt.
3. **Local-first.** Persönliche Daten, Event-State, Geräteinformationen und Langzeit-Memory bleiben möglichst zuhause bzw. auf den eigenen Geräten.
4. **Fluid-first.** Wake Word, HUD, lokale Aktionen und Statusfeedback warten nie auf langsames Cloud-Reasoning.
5. **Build core before spectacle.** Zuerst Gehirn, Permissions, Tools, Memory; danach Voice/HUD; erst dann 3D-Globus, Alexa, Self-Extension, R&D.

Claude Max 20x ist ein Nutzungskontingent, keine Intelligenzstufe. Entwicklung: Claude Code + stärkstes verfügbares Modell mit hohem Effort für Architektur/Security/Datenmodelle/Refactorings/Reviews; für Routine kleinere Modelle oder niedrigerer Effort. Der Runtime-Core besitzt einen **Model Router**, damit Anbieter und Modelle austauschbar bleiben.

## 1. Produktvision

**Master-Definition:** JARVIS ist ein persistenter, multimodaler, proaktiver persönlicher KI-Agent mit eigenem Core, langfristigem Memory, kontrollierter Tool- und Geräteausführung, Multi-Agent-Orchestrierung und dynamischer Präsenz auf Desktop, Handy und Zuhause.

| Bereich | Zielzustand |
|---|---|
| Voice | „Jarvis“ lokal erkannt, sofortiges Feedback, flüssige Duplex-Konversation, Barge-in, kein abgehacktes TTS |
| PC | Wake-on-LAN/Out-of-band, Apps/Dateien/Terminal/Browser steuern, Bildschirm sehen, Aufgaben ausführen und verifizieren |
| Mobile | Unterwegs sprechen, Zuhause steuern, Missionen verfolgen, Freigaben, Live-HUD, Kamera optional als Sensor |
| Smart Home | Home Assistant als Geräte-Nervensystem |
| Alexa/Echo | Nur Satellit/Bridge, nie Kern |
| Memory | Episodisch, semantisch, Projekt, Präferenz, Habit, Relationship, Procedural – mit Quelle, Confidence, Löschbarkeit |
| Learning | Gewohnheiten/Begriffe/Stil lernen, Korrekturen verwerten, Routinen vorschlagen; keine unkontrollierte Core-Selbstmodifikation |
| Coding | Live sehen, welche Dateien/Diffs entstehen, Builds/Tests verfolgen, Preview, iterativ fixen |
| World Intelligence | Interaktive 3D-Erde mit News nach Land/Region, Layern, Quellen/Confidence, Zeitachse |
| UI | Dynamisches Command-Center, AI-Core, Amber/Cyan, kontextabhängige Modi, 60/120 FPS |
| Personality | Ruhig, souverän, präzise, trockener Sarkasmus, kaum Füllsätze, ernst in Notfällen |
| Privacy/Security | Daten lokal, verschlüsselte Verbindungen, device-bound identities, Passkey/Biometrie, Audit, Sandbox, Rollback, Kill Switch |

**Nicht Ziel:** Stimmklon eines realen Schauspielers; Behauptung von Bewusstsein/AGI; unkontrollierte Selbstmodifikation oder Rechteausweitung; Abhängigkeit von einem einzelnen Cloud-Anbieter oder UI-Framework; UI-Spielerei auf Kosten von Zuverlässigkeit, Latenz, Sicherheit.

## 2. Zielerlebnis

- **Wake/Presence:** Auf „Jarvis“ reagiert das aktive Gerät sofort akustisch und visuell. Der Core wählt den besten Ausgabepunkt; andere Geräte bleiben still.
- **Remote-PC:** Mobile Wake → Home Core → WOL → Desktop Agent online → Projektkontext → Build → Verifikation → Live-Status ans Handy.
- **News:** News-Agent dedupliziert, extrahiert Regionen, bewertet Quellen; Globus dreht synchron zur Sprache. Der Globus ist ein Datenraum, keine Animation.
- **Coding-Mission:** Planning → Scaffold → Files → Diffs → Build → Test → Patch → Preview → Verify. Das HUD zeigt nachvollziehbare Arbeit (Pläne, Tool-Calls, Diffs, Tests, Logs, Artefakte), keine versteckten Reasoning-Ketten.

## 3. Visuelle Produktsprache

Dunkles Command-Center, technische Mikrodetails, zentrale Fokusobjekte, Amber/Orange + Cyan, Radar-/Orbit-Motive, transparente Ebenen. Eigene Identität, keine Film-Kopie.

HUD-Modi: Core (Listening/Thinking/Speaking/Working), News (Globe + Country Cards + Evidence Rail), Coding (Monaco + Tree + Diff + Terminal + Tests + Preview), Smart Home (Digital Twin), System (Topologie/Health), Research (Source Graph), Mission (Goal Tree/Agents/Approvals), Idle (minimaler Core).

**Fluidity Rule:** UI läuft getrennt vom AI-Backend. Keine Claude-Anfrage blockiert Animation, Eingabe, Scrollen, Globe oder Wake-Feedback. WebGPU mit WebGL-2-Fallback.

## 4. Gesamtarchitektur

- **Claude ist nicht JARVIS.** JARVIS = Core + State + Event Bus + Memory + Policy Engine + Agent Runtime + Tool Registry + Device Mesh + Voice + UI. Claude liefert Reasoning/Planung.
- **Modularer Monolith zuerst.** Ein Python-Core-Prozess mit klar getrennten Modulen und stabilen Interfaces. Voice, Desktop und UI dürfen separate Prozesse sein. Services werden erst bei Bedarf herausgezogen (siehe ADR-0002).

| Schicht | Empfehlung |
|---|---|
| Core | Python 3.12 + asyncio |
| Agent Runtime | Claude Agent SDK (Python), gekapselt hinter `IntelligenceProvider`/`AgentRuntime` |
| Desktop/Mobile UI | Tauri 2 + React + TypeScript |
| 3D HUD | Three.js / React Three Fiber, WebGPU/WebGL-Fallback |
| Code View | Monaco Editor |
| Database | PostgreSQL + pgvector |
| Cache/Events | In-process zuerst, optional Redis/NATS später |
| Browser | Playwright + API-first Adapter |
| Home | Home Assistant |
| Tool-Protokoll | MCP + interne Capability API |
| Packaging | Tauri bundler + signierte Installer |
| Repo/CI | Privates GitHub + Actions + Releases |

## 5. JARVIS Core (technisches Gehirn)

Jede Aktion durchläuft: Kontext → Planung → Permission → Ausführung → Verifikation → Memory.

| Modul | Verantwortung |
|---|---|
| State Manager | Benutzer-, Geräte-, Mission-, Gesprächs- und Präsenzzustand |
| Intent Router | Schnelle lokale Befehle von komplexem Reasoning trennen |
| Context Builder | Nur relevanten Kontext aus Memory/App/Projekt/Sensoren zusammenstellen |
| Mission Engine | Ziele in Tasks, Dependencies, Checkpoints, Status |
| Planner | Schritte/Agenten bestimmen, Budget/Risiken berücksichtigen |
| Agent Coordinator | Claude/Subagents starten, stoppen, begrenzen, Ergebnisse zusammenführen |
| Capability Registry | Tools/Skills mit Schema, Risiko, Rechten, Health |
| Permission Engine | Vor jeder Aktion deterministische Policy |
| Execution Gateway | Führt Tool-Aufrufe aus; nie direkter Modellzugriff auf OS/Secrets |
| Verifier | Prüft, ob das reale Ziel erreicht wurde |
| Event Bus | Typisierte Zustandswechsel an HUD, Mobile, Logs, Memory, Automation |
| Scheduler | Zeitjobs, Background Missions, Retry/Backoff |
| Model Router | Opus/Sonnet/lokal je nach Schwierigkeit, Latenz, Privatsphäre, Kosten |
| Audit Logger | Nachvollziehbare, manipulationsgeschützte Historie |

**Event Envelope**

```
Event {
  event_id: UUID
  type: "mission.task.started"
  timestamp: ISO-8601
  source: "coding-agent"
  correlation_id: UUID
  user_id: local-owner
  device_id: optional
  sensitivity: public | private | secret
  priority: background | normal | urgent | critical
  payload: {...}
  ttl: optional
}
```

**Mission State Machine**

```
CREATED -> PLANNING -> WAITING_FOR_APPROVAL -> RUNNING -> VERIFYING -> COMPLETED
                                            -> PAUSED / BLOCKED / FAILED / CANCELED
```
Jede Transition erzeugt ein Event und wird persistiert.

**Definition of Done Core 0.1**
- [ ] Textkommando erreicht Core über lokale API/WebSocket.
- [ ] Intent Router wählt zwischen lokalem Mock-Tool und Claude-Agent.
- [ ] Permission Engine blockiert/erlaubt/fordert Bestätigung.
- [ ] Tool Registry führt mindestens drei Mock-Tools aus.
- [ ] Jede Aktion erzeugt Events und Audit-Logs.
- [ ] Mission bleibt nach Prozessneustart erhalten.
- [ ] Verifier unterscheidet „Tool wurde aufgerufen“ von „Ziel erreicht“.
- [ ] Keine UI außer minimalem Debug-Dashboard nötig.

## 6. Claude-Strategie

| Aufgabe | Modell/Setting |
|---|---|
| Architektur / Security / Datenmodell | stärkstes Modell, effort=max |
| Schwierige Refactorings / Multi-Agent | stärkstes Modell, xhigh/max |
| Normale Feature-Implementierung | mittleres Modell high/xhigh |
| Routing / Klassifikation | kleines Modell oder lokale Regeln |
| Offline / private Basics | lokales Modell + deterministische Intents |

Claude Agent SDK wird hinter `IntelligenceProvider` und `AgentRuntime` gekapselt. Billing/Auth getrennt vom Chat-Abo behandeln; Architektur nie auf ein Abo-Detail festnageln.

Multi-Agent-Standard: **Writer → Verifier → Security/QA**. Parallele Agenten nur bei wirklich unabhängigen Teilproblemen.
Security-sensitiv: Coordinator → Implementer → Security Reviewer → Permission Engine → Executor.

## 7. Security, Rechte, Vertrauen

Permission-Level P0–P6, harte Sicherheitsregeln und Threat Model: siehe `docs/SECURITY.md` (normativ).

## 8. Memory und Personalisierung

Typen: Working, Episodic, Semantic, Project, Preference, Habit, Relationship, Procedural, Visual (optional, nur nach Datenschutzregeln).

```
memory_id: uuid
type: preference
subject: owner
predicate: preferred_editor
value: VS Code
confidence: 0.94
source: explicit_statement | observation | correction
observations: 14
created_at / last_confirmed_at
sensitivity: private
retention: durable
project_scope: optional
```

Lernschleife: Beobachtung → Muster → Hypothese → Confidence → sichere Personalisierung → Feedback. Riskante/weitreichende Automationen erst nach Freigabe permanent. Korrekturen aktualisieren das User Model gezielt.

„What JARVIS Knows“: Liste aller Präferenzen/Routinen/Hypothesen mit Quelle, Confidence, letzter Bestätigung, betroffenen Automationen. Actions: Correct, Forget, Pin, Don't Learn This, Make Temporary. Privacy Mode. „Vergiss die letzten 30 Minuten“ = nachvollziehbarer Delete-Workflow.

## 9. Voice Engine

- Originale Stimme, kein Klon. Phrase-level Streaming, natürliche Prosodie, dynamische Pausen.
- Sarkasmus trocken und selten; bei Notfall/Trauer/Medizin/Security/ernsten Konflikten Humor = 0.
- No-Filler Rule: kein „Natürlich“/„Sehr gerne“; bei simplen Aktionen „Erledigt.“ oder Sound Cue.
- Barge-in: „Jarvis, stopp“ oder neue Frage stoppt Ausgabe sofort. Night/Whisper/Silent Mode; sensible Inhalte auf Kopfhörer/Handy.
- Latenzbudget: siehe `docs/PERFORMANCE.md`.
- Wake Word: eigene plattformabhängige Wake-Schicht; Android für Always-on realistischer, iOS mit Push-to-talk/Shortcut-Fallback. Home Assistant als Prototyping-Pfad für Voice Satellites.

## 10. Device Mesh

- **Home Core:** kleiner x86-Server 24/7 mit Mission State, Memory, Device Registry, Event Bus, Home-Assistant-Anbindung, Remote Gateway.
- **Desktop Agent:** signierter Hintergrunddienst + HUD; Fähigkeiten hinter Capability Registry; API-first, GUI-Automation nur Fallback; „online“ erst nach Heartbeat + Handshake; Workspace restore.
- **Mobile Agent:** Voice, HUD, Push, Mission Status, Approvals, Device Map; Ende-zu-Ende authentifiziert; kein offener Home-Port; Kamera/Location nur opt-in.
- **Alexa/Echo:** Bridge/Satellit; Architektur darf nie davon abhängen.
- **Presence Service:** entscheidet nach Gerät, Raum, Aktivität, Audio-Routing, Privacy, wo der Core erscheint; nur der beste Audio-Satellit antwortet.

## 11. Smart Home und Digital Twin

Home Assistant bleibt Geräte-Gateway. Home States: Home / Away / Sleep / Work / Movie / Guests / Night / Vacation (je mit erlaubten Notifications, Licht/Klima-Defaults, Privacy-Regeln, Speaker-Routing, Device Policies). Digital Twin später. Home Safety: Schloss/Alarm/Garage höhere Permission-Level; Kameradaten lokal und ereignisorientiert; keine Entriegelung nur per Sprachaufnahme.

## 12. Live Coding / Creator Mode

Panels: Project Tree, Editor (Monaco, Diffs), Terminal, Agent Rail (Status, keine Chain-of-Thought), Mission Timeline, Preview, Quality, Artifact.
Coding Safety: Sandbox/isolierter Workspace; Branch/Commit pro Feature, nie unkontrolliert auf `main`; Tests + Verifier vor „fertig“; Dependencies bewertet, Lockfiles versioniert; Builds dürfen HUD/Voice nicht saturieren.

## 13. World Intelligence Globe

Pipeline: Sources → ingest+timestamp → dedupe/cluster → Länder/Regionen/Topics → Source Quality + Confidence → Summary pro Event → geospatial event model → Event Bus → Globe/Cards/Voice.
Interaktion: Top-Events in Sprachreihenfolge, Themen-Layer, Country Brief, Story Evolution, Evidence Mode, Breaking als vorläufig markiert, Prognosen von Fakten getrennt.

## 14. Proaktivität und Background Missions

Relevance Engine: jedes Event bekommt Dringlichkeit, Relevanz, Confidence, Unterbrechungskosten, Kanal. Kritisch → sofort; wichtig → opportunistisch; informativ → Daily Brief; irrelevant → still.
Missionen laufen persistent mit Checkpoints, Watchdogs und Budgets (Zeit, Tokens, Kosten, Tool-Calls). Predictive Assistance nur für ungefährliche Vorbereitungen.

## 15. Selbstlernen und Selbsterweiterung (kontrolliert)

Skill Factory: Research → Adapter-Design → Skill generieren → Tests → Sandbox → Security Review → Capability Manifest → User Approval → Install → versionierte Registry → Monitoring → Rollback.

| Bereich | Autonomie |
|---|---|
| Antwortstil / UI-Präferenz | automatisch im sicheren Bereich |
| Harmloser Shortcut / Vorschlag | automatisch vorschlagen |
| Routine mit externen Aktionen | nach Freigabe |
| Neuer Skill | entwickeln + testen autonom; installieren nach Review/Freigabe |
| Core-Code | Branch/PR erstellen; niemals heimlich in Produktion |
| Security Policy / Kill Switch | niemals selbst lockern oder abschalten |
| Admin-/Root-Rechte | nur explizit, zeitlich begrenzt, stark bestätigt |

## 16. Performance-Architektur

Fast Path (ms, lokale Intents/Adapter) / Smart Path (sub-second, schnelles Modell, Cache) / Deep Path (Sekunden–Minuten, Agenten + Tools + Verifier + Streaming-Status). Anti-Lag-Regeln und Telemetrie: `docs/PERFORMANCE.md`.

## 17. Datenmodell und Schnittstellen

| Objekt | Wichtige Felder |
|---|---|
| UserProfile | identity, preferences, privacy policy, personality settings |
| Device | id, type, capabilities, trust, room, last_seen, key fingerprint |
| Capability | name, schema, risk level, required permissions, health |
| Mission | goal, status, priority, budget, owner, context, checkpoints |
| Task | mission_id, dependencies, assigned_agent, state, retries |
| AgentRun | provider, model, effort, tools, cost, start/end, outcome |
| PermissionDecision | action, risk, rule, approval proof, expiration |
| MemoryItem | type, subject, value, confidence, source, sensitivity, retention |
| Artifact | file/build/report/preview, hash, path, version |
| Event | typed envelope, correlation, priority, payload |
| AuditRecord | immutable summary of sensitive state change |

Capability Manifest (Beispiel):

```yaml
name: computer.open_app
version: 1.0
risk: P1
inputs:
  app_id: string
requires:
  - device.trusted
side_effects: true
reversible: false
verifier: computer.process_running
timeout_ms: 10000
```

## 18. Privates GitHub, Installer, Eigentum

Quellcode privat auf GitHub; Memory, Secrets, Gespräche, Logs, Home-Daten gehören nicht ins Repo. GitHub Actions → signierter Build → privates Release → Installer (EXE/MSI/DMG/AppImage/Mobile) → lokale Installation mit lokalen persönlichen Daten. Später eigener Updater mit Backup und Rollback.

Datenpfade (Windows): `C:\Program Files\JARVIS\` (Binaries), `%LOCALAPPDATA%\JARVIS\` (Cache/Logs), `%APPDATA%\JARVIS\` (Settings), verschlüsseltes lokales Datenvolumen (Memory/Index/Secrets-Metadaten), Home Core PostgreSQL (synchronisierter State).

## 19. Hardware/Software

Dev-PC: 8+ Cores, 32 GB RAM (16 min.), 1 TB NVMe, GPU optional, gutes Mikrofon, Gigabit. Home Core: x86 Mini-PC, 4–8 Cores, 16–32 GB, 512 GB+ NVMe, Ethernet, optional UPS.
Software: Git/GitHub, Claude Code, Python 3.12 (+uv), Node 20+ (+pnpm), Rust + Tauri, Docker Compose, PostgreSQL + pgvector, IDE, Home Assistant (Home-Phase), Mesh VPN (Remote-Phase), optional Android-Testgerät.

## 20. Repository-Struktur (Ziel)

```
jarvis/
├─ apps/ (desktop, mobile, admin)
├─ core/ (api, state, events, missions, permissions, memory, models, agents, capabilities, verifier, scheduler)
├─ adapters/ (desktop, browser, home_assistant, news, github, alexa_bridge)
├─ voice/ (wake, stt, tts, routing)
├─ skills/  mcp/  packages/ (protocol, ui-kit, skill-sdk)
├─ infra/ (docker, home-core, ci)
├─ docs/ (SPEC.md, SECURITY.md, PERFORMANCE.md, decisions/ADR-0001...)
├─ tests/
└─ CLAUDE.md
```
Ist-Zustand und Migrationspfad des bestehenden Voice-Prototyps (`jarvis/`-Paket): siehe ADR-0001.

## 21. Build-Reihenfolge (der wichtigste Teil)

**Nicht mit dem HUD anfangen.** Zuerst die unsichtbare Systemwahrheit: State, Missionen, Events, Permissions, Tools, Verifikation, Memory.

| Phase | Inhalt | Exit-Kriterien |
|---|---|---|
| 0 Foundation | Repo, Toolchain, Specs, ADRs, Compose (Postgres/pgvector), `.env.example`, CI (format, lint, tests, secret scan, smoke) | Clean clone → ein Setup-Befehl → Tests grün; keine Secrets im Repo; ADRs vorhanden |
| 1 Brain Core 0.1 | Core-Prozess + Health, typisierter Event Bus + Persistenz, Mission State Machine, Capability Registry mit Mock `echo`/`clock`/`open_url`, WebSocket-Eventstream | Mission überlebt Restart; Events korreliert/replaybar; Mock-Tool nur nach Permission |
| 2 Permission + Execution + Verification | P0–P6 im Code, Policy Evaluator + Approval, Execution Gateway + Timeout/Retry, Verifier-Interface, Audit-Log, Kill Switch | Riskante Aktion wartet auf Approval; Denied nicht umgehbar; Verifier markiert Tool-Erfolg als Ziel-Fehler |
| 3 Claude Brain / Agent Runtime | `IntelligenceProvider`, Agent-SDK-Adapter, Model Routing, Budgets, Subagents, Streaming-Events | Komplexe Mock-Mission; Tool-Allowlist eingehalten; Abbruch/Timeout/Retry |
| 4 Memory + User Model | Schema + Vektor-Retrieval, Stores, Writer mit Sensitivity/Retention, Context Builder, Korrektur-Loop, „What JARVIS Knows“-API | Fakten korrekt retrieved; Vergessen reproduzierbar; Confidence/Source sichtbar |
| 5 Voice 0.1 | Wake-Worker, VAD, Streaming STT/TTS, Barge-in, Personality Contract, Latenz-Telemetrie | Wake sofort; Barge-in schnell; simple lokale Befehle ohne Claude |
| 6 Desktop Agent + Minimal HUD | Tauri/React Shell, Presence, Desktop Service, Screen Capture, sichere Terminal/Browser-Capabilities, Approval UI, Event Rail | App öffnen + verifizieren; HUD flüssig während Agent arbeitet |
| 7 Live Coding | Monaco + Tree, Diff-Streaming, Terminal-Panels, Sandbox-Runner, Preview, Writer/Test/Verifier, Artifact Registry | Prototyp erstellt/gebaut/gestartet/verifiziert; Fehler sichtbar und repariert |
| 8 Home Core + Home Assistant | Core auf Mini-PC, HA-Adapter, Device Registry, Voice Satellite, WOL, Home States, Offline-Pfade | Licht per Handy/PC; WOL; Cloud-Ausfall zerstört Home-Basics nicht |
| 9 Mobile + Secure Remote | Mobile HUD/Voice, Enrollment + Keypairs, Mesh VPN, Push, Passkey/Biometrie, Mission Handover | Sichere Testaktion von unterwegs; Revoke; Mission geräteübergreifend identisch |
| 10 Dynamic HUD + Globe | GPU Scene Manager, AI-Core-States, 3D-Globe, News-Datenmodell, adaptive Panels, Quality Tiers | News drehen korrekt; 60 FPS; UI zeigt echte Events |
| 11 Proactivity + Learning | Relevance Engine, Habit Detector, Vorschläge, Watchdogs, Daily Brief, Preloading, Privacy/Guest Modes | Irrelevantes still; Routine vorgeschlagen, nicht aktiviert; Background Mission überlebt Restart |
| 12 Skill Factory + Release 1.0 | Skill SDK + Manifest, Sandbox/Review/Install, signierte Release-Pipeline, Installer/Updater/Rollback, verschlüsseltes Backup, Regression-Suite | Clean install; Update + Rollback; Skill ohne Core-Bypass; 1.0-Suite grün |

## 22. Die ersten 10 Commits

| # | Commit | Resultat |
|---|---|---|
| 001 | chore: bootstrap monorepo | Repo, README, private note, `.gitignore`, Tooling |
| 002 | docs: add master spec and ADR template | PDF, SPEC.md, SECURITY.md, PERFORMANCE.md |
| 003 | infra: postgres compose | PostgreSQL/pgvector + Migrations-Skeleton |
| 004 | core: typed event bus | Envelope, in-process pub/sub, Persistenz |
| 005 | core: mission state machine | Mission/Task-Tabellen + Transition-Tests |
| 006 | security: permission engine | P0–P6, deny/ask/allow, Unit-Tests |
| 007 | core: capability registry | Manifest, Mock-Tools, Execution Gateway |
| 008 | core: verifier interface | Outcome Checks, Retry Policy |
| 009 | api: websocket event stream | Debug-Client sieht echte Events |
| 010 | ai: claude provider skeleton | Agent-SDK-Adapter hinter Interface, noch keine echten OS-Tools |

## 23. Arbeitsregeln mit Claude Code

- Niemals „Baue JARVIS komplett“ in einem Durchgang. **Eine Session = ein abgegrenzter Milestone mit Definition of Done.**
- Development Laws: `CLAUDE.md`.
- Writer-Verifier-Workflow: Agent A implementiert; Agent B sucht in Spec + Diff Bugs, Sicherheitsumgehungen, Races, fehlende Tests; Agent C (kritische Module) Security/Performance Review; Coordinator akzeptiert erst nach grünem Testset und abgearbeiteten Findings.

## 24. Test- und Qualitätsstrategie

Unit (State Transitions, Policies, Parser, Memory Scoring, Routing) · Contract (Capability-Schemas, Agent-Events, Device-Protokoll, API) · Integration (Core + DB + Agent SDK + Adapter) · Simulation (Fake Home/PC/News) · Security (Prompt Injection, Permission Bypass, Secret Leakage, gefälschte Device-Events) · Chaos (Restart, Netzverlust, Tool-Delay, Cache-Korruption, Agent-Timeout) · Voice · UI (60-FPS-Budget) · E2E · Recovery.

Golden Scenarios 1.0: Voice Fast Path · Remote PC wake → verified → mobile notification · Coding Mission mit Verifier · World News → Globe → Evidence · Home Scene verifiziert · Risky Command → Biometrie → Audit + Undo · Internet down → lokale Basics · Core Restart → Mission Recovery · Memory Correction sichtbar · Kill Switch stoppt alles.

## 25. Risikoregister (Auszug)

Zu früh zu viel UI → Core zuerst, minimal debug UI. Claude als Monolith-Gehirn → Provider-Abstraktion + Fast Path. Voice-Latenz → Streaming, lokale Wake/Intents, warme Services. Falsche Autonomie → Risk Levels, Approvals, Verifier, Audit, Undo. Memory-Müllhalde → typisiertes Memory mit Confidence/Decay/Source/Retention. Multi-Agent-Overkill → Writer-Verifier Standard. Alexa-Limits → Bridge. iOS-Wake-Limits → Android zuerst. Self-Modification → PR + Tests + Owner Approval, Policy immutable. Cloud-Ausfall → lokale Fallbacks, persistenter State.

## 26. 30-Tage-Startplan (Rahmen)

Tage 1–2 Foundation · 3–7 Brain Core · 8–10 Security · 11–14 Claude-Integration · 15–19 Memory · 20–24 Voice-Prototyp · 25–30 Desktop Shell.

## 27–28. Feature-Backlog (200+)

Der vollständige Backlog (Performance, Voice, Attention/Memory, Missions/Agents, HUD, Device Mesh, Home, Coding, World Intelligence, Security, Personality, R&D) steht in der PDF, Abschnitte 27–28. Features werden nach Core-Reife priorisiert, nicht blind nacheinander eingebaut. Der Backlog dient dazu, spätere Fähigkeiten architektonisch nicht zu blockieren.

## 29. Abnahmekriterien JARVIS 1.0

Reliability (24h Home Core, Recovery-Pfad, keine stille Mission-Verluste) · Voice (Wake, duplex-ish, Barge-in, cleanes TTS, Fast Path) · Desktop (Apps/Files/Browser/Terminal im Scope, Screen Context, Verifier) · Mobile (Remote, Notification, Approval, shared Mission State) · Home (HA + mehrere Geräteklassen + offline) · Memory (Search, Correction, Forget, Source/Confidence, Profile) · Agent (Research/Coding/Verification, Budgets, graceful failure) · UI (Core, Mission-, Coding-Mode, Globe, gemessene Fluidity) · Security (P0–P6, Passkey/Biometrie, Audit, Secret Isolation, Kill Switch) · Distribution (signierter Installer, Updater, Rollback, verschlüsseltes Backup) · Personality · Ownership (private Installation, keine persönlichen Daten im Repo).

## 30. Offene Entscheidungen (vor den jeweiligen Phasen)

- [ ] Haupt-PC: Windows-Version, Mainboard/NIC, WOL-Support.
- [ ] Handy: Android oder iPhone.
- [ ] Vorhandene Alexa/Echo-Modelle.
- [ ] Vorhandene Smart-Home-Geräte/Hersteller.
- [ ] Separater Home-Core/Mini-PC oder vorhandene Hardware.
- [ ] Lokale Kameraverarbeitung + Privacy-Retention.
- [ ] TTS-Stimme als originale JARVIS-Identität.
- [ ] Welche Aktionen P3/P4/P5 genau brauchen, was immer verboten bleibt.
- [ ] Backup-Ziel.
- [ ] Budget für Claude API / Voice- / News-Dienste im Dauerbetrieb.

## 31. Handoff-Protokoll

Wird die PDF erneut hochgeladen: als Master-Spezifikation behandeln, zuerst `docs/STATUS.md` (Version, Phase, letztes Exit-Kriterium, nächster Task) lesen. Kernarchitektur nur mit ADR ändern. Neue Entscheidungen als Delta zur PDF festhalten.

## 33. Master-Entscheidung

Wir beginnen nicht mit Alexa, 3D-Erde oder Sprach-Sarkasmus. Wir beginnen mit Phase 0 und dann Phase 1: Event Bus, Mission State, Capability Registry, Permission Engine, Execution Gateway, Verifier. Danach Memory, Claude, Voice, Desktop, Home, Mobile, volles Interface. Ein sauberer Kern macht fast jedes spätere Feature zu einer Capability, einem Adapter, Agenten oder UI-Modus.
