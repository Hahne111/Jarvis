# J.A.R.V.I.S. — Core 1.0.0rc1

Persönlicher, lokal laufender KI-Assistent nach dem **JARVIS Master Blueprint 1.0** (`docs/JARVIS_Master_Blueprint_1.0.pdf`, kompakt in `docs/SPEC.md`). Der Kern ist ein deterministisches System aus Permission Engine, Execution Gateway, Verifier und Event Bus; Sprachmodelle sind austauschbare Provider dahinter. Blueprint-Phasen 0–12 sind umgesetzt, Stand und nächste Schritte stehen in `docs/STATUS.md`.

![Python 3.11+](https://img.shields.io/badge/python-3.11%2B%20(Core%203.12)-blue)
![Windows | macOS | Linux](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-0078D6)
![Version](https://img.shields.io/badge/version-1.0.0rc1-orange)

| Teil | Pfad | Status |
|------|------|--------|
| **JARVIS Core** | `core/`, `adapters/`, `voice/`, `apps/`, `skills/` | 1.0.0rc1 – Phasen 0–12, 292 Tests, CI grün |
| **Legacy-Prototyp** | `jarvis/` | unverändert (ADR-0001), separater Prozess auf Port 7860, siehe [Legacy-Prototyp](#legacy-prototyp-jarvis) |

---

## Inhalt

1. [Prinzipien](#prinzipien)
2. [Installation und Start](#installation-und-start)
3. [Architektur](#architektur)
4. [Permissions P0–P6, Gateway, Verifier](#permissions-p0p6-execution-gateway-verifier)
5. [Missionen, Intents, Agents und Subagents](#missionen-intents-agents-und-subagents)
6. [Memory – „What JARVIS Knows“](#memory--what-jarvis-knows)
7. [HUD: Core, Globe, Coding, Mission](#hud-core-globe-coding-mission)
8. [Voice](#voice)
9. [Desktop, Workspace, Home Assistant](#desktop-workspace-home-assistant)
10. [Mobile, Device Enrollment, Push](#mobile-device-enrollment-push)
11. [Proaktivität](#proaktivität)
12. [Skill Factory](#skill-factory)
13. [Release, Updater, Backup](#release-updater-backup)
14. [API](#api)
15. [Konfiguration](#konfiguration)
16. [Tests, Lint, CI](#tests-lint-ci)
17. [Phasen 0–12](#phasen-012)
18. [Dokumentation](#dokumentation)
19. [Legacy-Prototyp (`jarvis/`)](#legacy-prototyp-jarvis)

---

## Prinzipien

- **Core ist das Produkt, Modelle sind austauschbar.** Provider (Claude, Mock, keiner) liefern Tool-Calls nur als Vorschlag. Ausgeführt wird ausschließlich über das Execution Gateway nach einer expliziten Allowlist pro Mission.
- **Jede Seiteneffekt-Capability hat Risk Level und Verifier.** P0 beobachten … P3 Freigabe per Tap … P4/P5 nur mit Passkey/Biometrie auf einem vertrauten Gerät … P6 wird nie ausgeführt. Die Policy kann sich zur Laufzeit nur verschärfen.
- **Alles ist ein persistiertes Event.** HUD und API zeigen nur echte Events. Nach einem Neustart werden Missionen, offene Freigaben, Home-Zustand und Agent-Läufe aus dem Event-Log wiederhergestellt.
- **Kill Switch.** „Jarvis, stop“ oder `POST /kill` hält jeden Seiteneffekt an; fortgesetzt wird nur mit starkem Proof.
- **Local-first, keine Secrets im Repo.** Tokens und Keys kommen nur aus der Umgebung. `secret`-Memory erreicht nie einen Cloud-Prompt und nie ein Event.
- **Erledigt heißt verifiziert.** Eine Mission mit Codeänderung gilt erst nach einem verifizierten grünen Testlauf als abgeschlossen.

---

## Installation und Start

### Voraussetzungen

- Python **3.12** für den Core (3.11 kompatibel; der Legacy-Prototyp braucht unter Linux 3.11 wegen `tflite-runtime`).
- Für Voice/Desktop-Prototyp-Backends: Mikrofon, unter Linux `libportaudio2 python3-tk xvfb`, unter macOS Homebrew (`install.sh` installiert `python@3.12 portaudio espeak-ng`).
- Optional: Docker (PostgreSQL + pgvector), Ollama (nur Legacy-Prototyp), Anthropic-Key (Agent-Missionen mit Claude).

### Variante A – alles (Core + Voice + Prototyp-Backends)

```bash
git clone https://github.com/Hahne111/Jarvis.git
cd Jarvis
./install.sh        # macOS/Linux: venv .venv, requirements.txt, Wake-Word-Modelle
install.bat         # Windows
source .venv/bin/activate
```

### Variante B – nur der Core (leichtgewichtig)

```bash
git clone https://github.com/Hahne111/Jarvis.git && cd Jarvis
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r core/requirements.txt     # sqlalchemy, fastapi, uvicorn, httpx, cryptography, pytest
pip install anthropic                    # nur wenn JARVIS_PROVIDER=claude genutzt wird
```

### Core starten

```bash
cp .env.example .env                     # Werte lokal setzen, .env ist gitignored
python -m core
```

- HUD: **http://127.0.0.1:7870/hud/** (`/` leitet dorthin), Debug-Dashboard: `http://127.0.0.1:7870/debug`, Health: `GET /health`.
- Datenbank: SQLite `jarvis/data/core.db` (Default) oder `JARVIS_CORE_DB_URL=postgresql://…` mit `docker compose -f infra/docker/docker-compose.yml up -d` (pgvector, gebunden an `127.0.0.1:5432`).
- Bindet an `127.0.0.1:7870`; `JARVIS_CORE_HOST` darf eine Mesh-VPN-IP sein, `0.0.0.0` wird verweigert. Port über `JARVIS_CORE_PORT`.

Demo ohne Hardware und ohne Key:

```bash
JARVIS_PROVIDER=none JARVIS_HOME=fake JARVIS_NEWS=fake JARVIS_DESKTOP=fake python -m core
```

Im HUD ausprobieren: `echo hello` · `what time is it` · `turn on the kitchen light` · `szene movie` · `gute nacht` · `wake desktop` (wartet auf Freigabe) · `Jarvis, stop` (Kill Switch, Resume nur mit Passkey).

### Voice starten

```bash
python -m voice                          # Wake Word → Whisper → Core → Kokoro (Prototyp-Audio-Stack)
JARVIS_VOICE_FAKE=1 python -m voice      # Tastatur statt Mikrofon
```

---

## Architektur

```
 Voice (voice/)        HUD (apps/desktop/web)       Handy / Satellit        Scheduler
 wake → STT → Text     WS /ws/events, REST          signierte Requests      geplante Jobs
        │                      │                           │                     │
        └──────────────────────┴───────────┬───────────────┴─────────────────────┘
                                           ▼
                              core/api  POST /commands  (Caller: Loopback-Owner | Gerät | untrusted)
                                           │
                     ┌─────────────────────┴─────────────────────┐
                     ▼                                           ▼
            Intent Router (Fast Path,                 Agent Coordinator (Model Router →
            deterministisch, ohne Modell)             Provider → Tool-Vorschläge → Allowlist)
                     └─────────────────────┬─────────────────────┘
                                           ▼
                Mission Engine ── Permission Engine (P0–P6, Approval) ── Execution Gateway
                                           │              (Timeout, Retry, Kill Switch)
                                           ▼
                        Capability Registry: mock.* · memory.* · workspace.* · computer.* ·
                        system.* · home.* · power.* · news.* · privacy.* · brief.* · skill.*
                                           │
                                           ▼
                             Verifier (Read-back, Exit-Code, Datei-Hash, TCP-Handshake …)
                                           │
                                           ▼
                     Event Bus + SQL Event Store  →  Presence, Push, Habits, HUD, Recovery
```

Module (Details in `CLAUDE.md`):

| Modul | Aufgabe |
|-------|---------|
| `core/events` | Typisiertes Event-Envelope, EventBus, `SQLEventStore` (SQLite/Postgres), Replay nach `seq`/Korrelation |
| `core/missions` | Mission/Task-Modelle, deterministische State Machine, Repository, Handover |
| `core/permissions` | RiskLevel P0–P6, Policy (nur verschärfbar), Approval-Workflow mit Proof-Stärken, Grants mit TTL |
| `core/capabilities` | Manifest (Inputs, `requires`, `side_effects` ⇒ Verifier Pflicht), Registry, ExecutionGateway |
| `core/verifier` | Outcome achieved/not_achieved/unknown/skipped, VerifierRegistry, RetryPolicy, VerifiedExecutor |
| `core/intents` | Regex-Fast-Path (stop, echo, time, open URL/App, lock, volume, Licht/Szene/Home-State, wake, brief, privacy) |
| `core/models` | `IntelligenceProvider`, ModelRouter Fast/Smart/Deep, Budgets, `MockProvider`, `ClaudeProvider` |
| `core/agents` | AgentCoordinator, Subagent-Rollen, Coding-Workflow, Fortsetzung nach Approval aus dem Event-Log |
| `core/memory` | MemoryItem, MemoryStore + Embedder, MemoryWriter (Privacy, Korrektur, Forget), ContextBuilder |
| `core/devices` | Geräte-Registry, Ed25519-Enrollment, signierte Requests, `Caller`-Auflösung |
| `core/notify` | PushService mit Relevance-Gate, `FakePush`/`WebhookPush` |
| `core/news` | NewsEvent-Modell, Geo/Topics, Dedupe/Cluster/Confidence, Fake/RSS-Quellen |
| `core/scheduler`, `core/proactive` | Jobs + Watchdog, Relevance Engine, Habit Detector, Daily Brief, Privacy-Modi |
| `core/skills` | Skill-Reviewer (AST), versionierte Registry, Sandbox-Tests |
| `core/release`, `core/updater`, `core/backup` | Signierte Archive, Updater mit Rollback, verschlüsseltes Backup |
| `core/presence.py`, `core/runtime.py`, `core/api` | Präsenzzustände pro Gerät, Verdrahtung, FastAPI + WebSocket |
| `adapters/desktop`, `adapters/workspace`, `adapters/home` | Desktop-Capabilities, sandboxed Workspaces, Home Assistant + WOL |
| `voice/` | VoiceSession-Automat, VoiceBridge zum Core, Latenz-Telemetrie |
| `apps/desktop/web` | Build-freies HUD (HTML/CSS/ES-Module) |
| `skills/` | Skill-SDK und Beispiel-Skill |

---

## Permissions P0–P6, Execution Gateway, Verifier

| Level | Bedeutung | Default | Beispiele |
|-------|-----------|---------|-----------|
| P0 observe | nur lesen | allow | `mock.clock`, `workspace.read`, `home.get_state`, `news.top`, `memory.recall` |
| P1 safe | harmlos | allow | `mock.open_url`, `memory.remember`, `computer.open_app` |
| P2 reversible | rückgängig machbar | allow | `workspace.write` (Versionskopie), `home.light.set`, `skill.enable` |
| P3 sensitive | Freigabe per Tap | ask (UI-Confirm) | `workspace.run`, `power.wake`, `skill.install`, `memory.forget` |
| P4 critical | starker Proof + vertrautes Gerät | ask (Passkey/Biometrie) | `home.lock.set`, `home.alarm.set`, `home.garage.set` |
| P5 restricted | starker Proof, gerätegebunden | ask (Passkey/Biometrie) | reserviert |
| P6 forbidden | nie | deny (final) | – |

- **Policy nur verschärfbar:** `Policy(overrides=…)` und `PermissionEngine.tighten()` akzeptieren nur strengere Entscheidungen und kürzere TTLs (Development Law 9).
- **Sprache erfüllt nie P3+.** Voice-Proofs sind schwach; Lock/Alarm/Garage lassen sich nicht per Sprachbefehl öffnen (SECURITY §4).
- **Execution Gateway:** einzige Stelle, die Capabilities ausführt; Input-Validierung gegen das Manifest, Timeout, Retry, Kill Switch (`gateway.halted`), Events `capability.invoked|succeeded|failed|denied|awaiting_approval|halted`.
- **Verifier:** jede Seiteneffekt-Capability nennt einen Verifier (Read-back beim Home, Exit-Code beim Runner, Datei-Hash beim Schreiben, TCP-Handshake beim WOL). Ohne unabhängiges Signal ist das Ergebnis `unknown`, nie „erledigt“.
- **Unsignierte Remote-Aufrufer** sind nie ein vertrautes Gerät: P0 läuft, P3 wird nicht freigebbar, Freigaben/Resume liefern 403.

---

## Missionen, Intents, Agents und Subagents

- **Kommando → Mission.** Jeder Text (HUD, Voice, Satellit, Scheduler) erzeugt eine Mission mit Korrelations-ID; alle Events hängen daran. Zustände: created → planning → running → waiting_for_approval → verifying → completed / failed / canceled / paused / blocked.
- **Fast Path** (`core/intents`): deterministische Regeln ohne Modell, z. B. `echo …`, `what time is it`, `open https://…`, `open <app>`, `lock screen`, `volume 30`, `show windows`, `licht an im wohnzimmer`, `turn on the kitchen light`, `szene movie`, `gute nacht`, `wake desktop`, `brief`, `privat`/`gast`, `Jarvis, stop`.
- **Agent Coordinator** (`core/agents`): ModelRouter wählt Fast/Smart/Deep (`claude-haiku-4-5`, `claude-sonnet-5`, `claude-opus-5` oder `mock-model`), AgentBudget begrenzt Schritte/Tokens/Zeit, Tool-Vorschläge passieren `filter_tool_calls` gegen die Allowlist der Mission, dann VerifiedExecutor. Wartet eine Capability auf Freigabe, pausiert der Lauf (`agent.run.paused`) und setzt nach der Freigabe aus dem Event-Log fort.
- **Subagents:** Rollen research, implementation, test, verification, security über das Tool `agent.delegate`; kontextisoliert, Tiefe 1, gemeinsames Budget. Rollen binden Werkzeuge: implementation `workspace.*`, test nur `pytest`/`python` in `workspace.run`, verification nur lesen/diff.
- **Coding-Workflow:** Codeänderung ⇒ `workspace.file.changed` mit Diff; Mission gilt erst nach verifiziertem grünem `workspace.run` als COMPLETED, sonst `not_verified`; `artifact.created` pro Datei beim Abschluss.
- **Handover:** `POST /missions/{id}/handover` verschiebt dieselbe Mission auf ein anderes Gerät (z. B. Freigabe am Handy).
- **Recovery:** `CoreRuntime.recover()` beim Start baut Missionen, offene Freigaben, Grants, Home-State und Agent-Läufe aus dem Event-Log wieder auf.

Provider: `JARVIS_PROVIDER=claude` (Default; Anthropic SDK via `pip install anthropic`, Key nur aus `ANTHROPIC_API_KEY` bzw. SDK-Login) · `mock` (skriptbarer Offline-Provider) · `none` (nur Fast Path).

---

## Memory – „What JARVIS Knows“

- Items nach SPEC §8.2: Typ (working/episodic/semantic/project/preference/habit/…), Subjekt/Prädikat/Wert, Quelle, Konfidenz, Sensitivität (public/private/secret), Retention (session/durable/temporary mit TTL), Projekt-Scope, Pin.
- `MemoryWriter`: Privacy-Filter (`dont_learn`), Reinforcement bei Wiederholung, **Korrektur als neue Version** (`supersedes`), Forget, Forget-Window (`forget_since`), Temporary, Pin/Unpin.
- `ContextBuilder` speist relevante, nicht-geheime Items in den Agent-Systemprompt. `secret`-Items gehen nie in einen Cloud-Prompt.
- Capabilities `memory.recall` P0, `memory.remember` P1, `memory.correct`/`memory.forget` P3 – alle über das Gateway. Events `memory.written|corrected|forgotten` tragen nur Metadaten, nie den Wert.
- HUD-Panel **WHAT JARVIS KNOWS**, API `/memory*`.

---

## HUD: Core, Globe, Coding, Mission

Build-freie Web-Shell (`apps/desktop/web/`, ADR-0003), vom Core unter `/hud/` ausgeliefert. Ereignisse kommen über `GET /events` (Replay) und `WS /ws/events?after_seq=` (live, lückenlos); pro Frame gebatcht, kein Polling außer `/health` alle 5 s. Event-Vertrag: `docs/HUD_EVENTS.md`.

| Modus | Inhalt |
|-------|--------|
| **CORE** | zentraler AI-Core aus `presence.changed` (idle/listening/thinking/speaking/working/awaiting_approval/halted), Event Rail mit Bereichsfilter, APPROVALS, MISSIONS, WHAT JARVIS KNOWS, HOME, DEVICES, PROACTIVE, Latenz-Kacheln, KILL SWITCH / RESUME |
| **GLOBE** | Canvas-2D-Weltkugel ohne Bibliothek (`globe.js`): Marker aus `/news/countries`, Country Cards, EVIDENCE-Rail mit Quellen und Confidence, Quality Tiers high/medium/low aus der Frame-Zeit, Telemetrie an `POST /telemetry` |
| **CODING** | Projektbaum (FILES), EDITOR (Monaco nur lokal aus `apps/desktop/web/vendor/monaco/`, sonst Textarea), DIFF aus `workspace.file.changed`, TERMINAL aus `workspace.run.output`, PREVIEW als sandboxed iframe, AGENTS aus `agent.*`, QUALITY aus Testläufen + `verification.*`, ARTIFACTS |
| **MISSION** | Verlauf einer Mission mit Checkpoints, Freigaben, Verifikationen |

- Speichern im Editor ⇒ `PUT /workspace/{mission}/file` ⇒ `workspace.write` durch das Gateway, nie direkt ins Dateisystem.
- Monaco: `python apps/desktop/web/vendor/fetch_monaco.py` lädt Version 0.56.0 hash-gepinnt in den gitignorierten Vendor-Ordner; nie CDN.
- Responsiv unter 760 px, PWA (`manifest.webmanifest`), Szenenwechsel per Navigation oder automatisch mit `command.received`.

---

## Voice

`voice/` (Voice 0.1): Interfaces `WakeWordDetector`, `SpeechToText`, `TextToSpeech`, `TurnDetector`; `VoiceSession`-Automat idle → wake_ack → listening → thinking → speaking → follow_up mit `voice.*`-Events; `VoiceBridge` sendet Transkripte an denselben Command-Pfad wie das HUD, streamt TTS phrasenweise, unterstützt Barge-in; „Jarvis, stop“ löst den Kill Switch aus. `LatencyTelemetry` schreibt `telemetry.latency` (p50/p95), `SpokenStyle` hält Antworten kurz („Done.“). Adapter in `voice/adapters/prototype.py` nutzen den Audio-Stack des Prototyps (openWakeWord, faster-whisper, Kokoro) unverändert; `voice/fakes.py` für Tests und `JARVIS_VOICE_FAKE=1`.

Voice-Satelliten (z. B. Home Assistant Assist „Hey Jarvis“) sprechen `POST /satellite/command`; ein Satellit ist untrusted, Sprache erfüllt nie P3+, die Antwort enthält einen kurzen Sprechtext.

---

## Desktop, Workspace, Home Assistant

**Desktop** (`adapters/desktop`, `JARVIS_DESKTOP=off|fake|prototype`): `computer.open_app|list_windows|focus_window|screenshot|type_text|press_key`, `system.info|set_volume|lock_screen`, jeweils mit Risk Level und Verifier (fehlt ein unabhängiges Signal ⇒ `unknown`). `prototype` wrappt `jarvis/tools` (pyautogui) unverändert.

**Workspace** (`adapters/workspace`): ein Ordner pro Mission unter `JARVIS_WORKSPACE_ROOT` (Default `jarvis/data/workspaces/<mission_id>`), Pfad-Sandbox (kein `..`, keine absoluten Pfade, Symlinks nie gefolgt). `workspace.list|read|diff` P0, `workspace.write` P2 (Versionskopie, Verifier Datei-Hash), `workspace.run` P3 (Allowlist python/pytest/node/npm/git…, Timeout, CPU-Limit, bereinigte Umgebung ohne Secrets, Verifier Exit-Code). Events `workspace.file.changed`, `workspace.run.started|output|finished`.

**Home Assistant** (`adapters/home`, `JARVIS_HOME=off|fake|homeassistant`, Anleitung `docs/HOME_ASSISTANT.md`):

- `HomeAssistantBackend` über REST (`/api/states`, `/api/services`), `JARVIS_HA_URL`, Token nur aus `JARVIS_HA_TOKEN` (nie in Events, Logs oder `repr`). `FakeHome` für Tests, CI und Demo.
- `DeviceRegistry` löst Entity/Raum/Name/„all“ auf (mehrdeutig ⇒ Fehler); `HomeStateMachine` Home/Away/Sleep/Work/Movie/Guests/Night/Vacation mit Policies, Rebuild aus `home.state.changed`.
- Capabilities: `home.list_devices|get_state|state.get` P0 · `home.light.set|switch.set|cover.set|climate.set|scene.activate|state.set` P2 mit Read-back-Verifier · `home.lock.set|alarm.set|garage.set` P4 (starker Proof, vertrautes Gerät, nie per Sprache).
- **Wake-on-LAN** (`adapters/home/wol.py`): `power.status` P0, `power.wake` P3; Magic Packets nur an Ziele aus `JARVIS_WOL_TARGETS` (JSON-Liste `name/mac/host/port` oder Pfad zu einer JSON-Datei), Verifier `power.host_reachable` per TCP-Handshake, MAC nie in Events.
- Offline-Basics: Licht, Szenen, Home-State und Fast Path funktionieren ohne Provider und ohne Internet.

---

## Mobile, Device Enrollment, Push

Remote-Setup Schritt für Schritt: `docs/REMOTE.md`. Kurzfassung:

1. Core bleibt auf Loopback und wird per `tailscale serve --bg https / http://127.0.0.1:7870` (oder WireGuard + `JARVIS_CORE_HOST=<mesh-ip>`) erreichbar. `0.0.0.0` verweigert der Core.
2. Enrollment-Code erzeugen: im HUD unter **DEVICES → ENROLL** oder `python -m core enroll phone` (8-stelliger Code, 10 Minuten gültig, einmalig, 5 Versuche).
3. Am Handy das HUD öffnen → **ENROLL THIS DEVICE**: der Browser erzeugt ein nicht exportierbares Ed25519-Schlüsselpaar (WebCrypto, IndexedDB) und signiert ab dann jede Anfrage (`X-Jarvis-Device`, `X-Jarvis-Timestamp`, `X-Jarvis-Nonce`, `X-Jarvis-Signature` über Zeit, Nonce, Methode, Pfad und Body-Hash).
4. Vertrauensmodell (`Caller`, ADR-0004): Loopback ohne Forwarding-Header = lokaler Owner; signiert = Trust aus der Registry (`trusted` setzt der Owner explizit); remote unsigniert = nie trusted. Starke Proofs (Passkey/Biometrie) sind an das signierende Gerät gebunden. Revoke ist final.
5. Push (`JARVIS_PUSH=off|fake|webhook`, z. B. ntfy-Topic in `JARVIS_PUSH_URL`, Token nur in `JARVIS_PUSH_TOKEN`): Freigabe nötig (mit Deep Link auf APPROVALS), Kill Switch, fehlgeschlagene Mission, widerrufenes Gerät. Ein Relevance-Gate unterdrückt Rauschen (`notify.suppressed`); `GET /notifications` listet Zustellungen ohne Secrets.
6. Mission-Handover: Desktop startet, Handy gibt frei – dieselbe Mission, dieselbe Korrelations-ID.

---

## Proaktivität

- **Scheduler** (`core/scheduler`, `JARVIS_SCHEDULER=on` Default): durable Jobs (Textkommando oder P0-Capability, Intervall oder Uhrzeit + Wochentage, Budget). `tick()` startet echte Missionen über denselben Command-Pfad; der Scheduler ist nie ein vertrautes Gerät, riskante Jobs bleiben stehen. Watchdog markiert hängende Missionen (`mission.watchdog`). System-Job „daily brief“ 07:30.
- **Relevance Engine:** ordnet jedes Ereignis einem Kanal zu (now / opportunistic / brief / silent) und gated den Push; Heimatland `JARVIS_HOME_COUNTRY` für News-Relevanz.
- **Habit Detector:** erkennt wiederkehrende Kommandos aus `command.received` und erzeugt `automation.suggested`. Vorschläge werden nie automatisch aktiv; Annahme im HUD (**PROACTIVE**) legt einen Job an.
- **Daily Brief:** `brief.generate` P0 ⇒ `brief.ready`, `GET /brief`; Fast Path „brief“.
- **Privacy-Modi:** `privacy.get|set` normal / private / guest verschärfen die Memory-Policy, pausieren Satelliten und lassen nur kritische Pushes durch; Fast Path „privat“ / „gast“.

---

## Skill Factory

- **SDK** (`skills/sdk/`): `SkillManifest` (Name, Semver, Entrypoint, Capabilities mit Risk P0–P5 – Seiteneffekte ⇒ Verifier Pflicht und nie P0 –, `uses` = erlaubte Core-Capabilities, Tests-Ordner), `SkillContext` als einzige Brücke zur Welt (`call()` nur für deklarierte Capabilities, läuft als Actor `skill:<name>`, nie trusted, durch Permission Engine → Gateway → Verifier), Basisklasse `Skill`. Beispiel: `skills/examples/hello_world/`.
- **Review** (`core/skills/review.py`): AST-Allowlist reiner Module plus `skills.sdk`; abgelehnt werden u. a. `os`, `sys`, `subprocess`, `socket`, `pathlib`, `httpx`, Imports aus `core`/`adapters`, `open/eval/exec/__import__`, Dunder-Zugriffe, Symlinks, zu große Bäume, fehlende Tests oder Handler. SHA-256 des Skill-Baums, Event `skill.reviewed`.
- **Installation** (`core/skills/registry.py`): Review → Sandbox-Tests im Workspace-Runner (CPU/Zeit-Limit, bereinigte Umgebung) → Kopie nach `jarvis/data/skills/<name>/<version>/` → Aktivierung als Capabilities `skill.<name>.<cap>` mit Manifest-Risk und Skill-Verifier. `registry.json` hält aktive Version und History; Disable, Rollback, Restore nach Neustart.
- Capabilities `skill.list|review` P0, `skill.install` P3 (Owner-Freigabe, vertrautes Gerät), `skill.enable|disable|rollback` P2. API `/skills*`. Wurzel `JARVIS_SKILLS_ROOT`.

---

## Release, Updater, Backup

Vollständiger Ablauf inkl. Backup-Drill: `docs/RELEASE.md`.

```bash
# Owner, einmalig: Schlüsselpaar – Public Key nach release/ committen, Private Key als GitHub-Secret
python -m core.release keygen ~/jarvis-release-key

# Release lokal nachvollziehen (CI macht dasselbe bei Tag v*)
python -m core.release archive --version 1.0.0rc1 --out dist
python -m core.release sums dist/jarvis-1.0.0rc1.tar.gz > dist/SHA256SUMS
JARVIS_RELEASE_SIGNING_KEY=... python -m core.release sign dist/jarvis-1.0.0rc1.tar.gz
python -m core.release verify dist/jarvis-1.0.0rc1.tar.gz dist/jarvis-1.0.0rc1.tar.gz.sig

# Update mit Rollback (Install-Root: JARVIS_INSTALL_ROOT, Default ~/.jarvis/app)
python -m core.updater status
python -m core.updater apply dist/jarvis-1.0.0rc1.tar.gz dist/jarvis-1.0.0rc1.tar.gz.sig
python -m core.updater rollback

# Verschlüsseltes Backup von jarvis/data (Passphrase nur aus der Umgebung, >= 8 Zeichen)
export JARVIS_BACKUP_PASSPHRASE='…'
python -m core.backup create  backups/jarvis-$(date +%F).jbk
python -m core.backup verify  backups/jarvis-2026-09-06.jbk
python -m core.backup restore backups/jarvis-2026-09-06.jbk /pfad/zum/neuen/data-dir
```

- **Release-Pipeline** (`.github/workflows/release.yml`): Tag `v*` ⇒ Regressionssuite ⇒ reproduzierbares Archiv `jarvis-<version>.tar.gz` mit `VERSION` ⇒ `SHA256SUMS` ⇒ Ed25519-Signatur (nur wenn das Secret existiert, sonst Warnung) ⇒ GitHub Release.
- **Updater:** verweigert ohne Public Key (`release/jarvis-release.pub`), prüft Signatur und Archiv-Mitglieder (kein `..`, keine Symlinks, nur `jarvis-<version>/`), entpackt nach `versions/<version>/`, führt einen Smoke-Test aus (`import core, adapters, skills` aus dem neuen Baum) und schaltet erst dann `current.json` um. Jeder Fehler lässt `current` unangetastet. Rollback ist offline und ein Befehl. Der laufende Core wird vom Owner neu gestartet.
- **Backup:** Format `JBK1` = AES-256-GCM über ein tar.gz, Schlüssel per scrypt aus der Passphrase, Header als AAD. Falsche Passphrase oder ein gekipptes Byte ⇒ nichts wird geschrieben. Enthalten: `core.db`, `workspaces/`, `skills/`, `settings/`, `memory/`.

Offene Owner-Schritte für 1.0.0: Schlüsselpaar erzeugen und Secret setzen, Tag `v1.0.0rc1`, echter Install-/Update-/Backup-Drill auf der Zielmaschine, Hardware-Abnahme (Home Assistant, WOL, Mikrofon, Handy-Enrollment).

---

## API

Alle Endpunkte liegen auf `http://127.0.0.1:7870`; remote nur signiert (siehe oben).

| Bereich | Endpunkte |
|---------|-----------|
| Kern | `GET /health` · `GET /events` · `WS /ws/events` · `POST /commands` · `GET /presence` · `POST /kill` · `POST /resume` · `GET /debug` |
| Missionen & Freigaben | `GET /missions` · `GET /missions/{id}` · `POST /missions/{id}/handover` · `GET /approvals` · `POST /approvals/{id}/approve\|deny` |
| Memory | `GET /memory` · `GET /memory/{id}` · `POST /memory/{id}/correct\|forget\|pin\|unpin\|temporary` · `GET\|POST /memory/policy` · `POST /memory/dont_learn` · `POST /memory/forget_since` |
| Coding | `GET /workspace/{mission}/files\|file\|diff` · `PUT /workspace/{mission}/file` · `GET /workspace/{mission}/preview/{path}` |
| Home & Geräte | `GET /home` · `POST /satellite/command` · `GET /devices` · `POST /devices/enroll/start` · `POST /devices/enroll` · `POST /devices/{id}/revoke\|trust` · `GET /notifications` |
| Globe | `GET /news` · `GET /news/countries` · `GET /news/{id}` · `POST /news/refresh` · `POST /telemetry` |
| Proaktiv | `GET\|POST /schedule` · `POST /schedule/{job}/{action}` · `GET /suggestions` · `POST /suggestions/scan` · `POST /suggestions/{id}/{action}` · `GET /brief` · `GET\|POST /privacy` |
| Skills | `GET /skills` · `POST /skills/review` · `POST /skills/install` · `POST /skills/{name}/{action}` |

---

## Konfiguration

Alle Variablen mit Platzhaltern in `.env.example`; echte Werte nur lokal in `.env` (gitignored) oder in der Shell.

| Variable | Werte / Default | Zweck |
|----------|-----------------|-------|
| `JARVIS_PROVIDER` | `claude` (Default) · `mock` · `none` | Modell-Provider für Agent-Missionen; Claude braucht `pip install anthropic` und `ANTHROPIC_API_KEY` |
| `JARVIS_CORE_HOST` / `JARVIS_CORE_PORT` | `127.0.0.1` / `7870` | Bind-Adresse (Mesh-IP erlaubt, `0.0.0.0` verweigert) |
| `JARVIS_CORE_DB_URL` | `sqlite:///jarvis/data/core.db` | Event-Store; Postgres per Compose (`POSTGRES_*`) |
| `JARVIS_DESKTOP` | `off` (Default) · `fake` · `prototype` | Desktop-Capabilities |
| `JARVIS_WORKSPACE_ROOT` | `jarvis/data/workspaces` | Sandboxed Workspaces |
| `JARVIS_HOME` / `JARVIS_HA_URL` / `JARVIS_HA_TOKEN` | `off` · `fake` · `homeassistant` | Home Core |
| `JARVIS_WOL_TARGETS` | JSON-Liste oder Pfad | Wake-on-LAN-Ziele |
| `JARVIS_PUSH` / `JARVIS_PUSH_URL` / `JARVIS_PUSH_TOKEN` | `off` · `fake` · `webhook` | Push aufs Handy |
| `JARVIS_NEWS` / `JARVIS_NEWS_FEEDS` | `off` · `fake` · `rss`; `name=url@quality,…` | World Intelligence Globe |
| `JARVIS_SCHEDULER` / `JARVIS_HOME_COUNTRY` | `on` / `DE` | Scheduler-Loop, News-Relevanz |
| `JARVIS_SKILLS_ROOT` | `jarvis/data/skills` | Installierte Skills |
| `JARVIS_INSTALL_ROOT` | `~/.jarvis/app` | Updater |
| `JARVIS_BACKUP_PASSPHRASE` / `JARVIS_RELEASE_SIGNING_KEY` | – | Backup-Passphrase, Release-Private-Key (nur Umgebung) |
| `JARVIS_VOICE_FAKE` | `1` | Voice-Loop per Tastatur |

---

## Tests, Lint, CI

```bash
pytest -q                    # alles (Linux headless: xvfb-run -a pytest -q) – 292 Tests
pytest -q tests/core         # Core (227), nur core/requirements.txt, Python 3.12
pytest -q tests/regression   # Release/Updater/Backup, Security-Invarianten, Performance-Budgets, Golden Scenarios (32)
ruff format --check . && ruff check .   # strikt für core/, adapters/, voice/, tests/core/, tests/regression/
```

CI (`.github/workflows/ci.yml`): Format + Lint · Secret-Scan (gitleaks) · Unit-Tests Python 3.11 (xvfb) · Core-Tests 3.12 · Regression-Suite · Build-Smoke. `main` ist geschützt; Änderungen gehen über Feature-Branch → PR → grüne CI → Merge.

Die Regressionssuite pinnt die 1.0-Garantien: Policy nur strenger, P6 nie ausgeführt, Kill Switch stoppt Seiteneffekte, keine Secrets im Event-Log, unsigniert-remote kann nicht freigeben, Tool-Allowlist, Skill-Review lehnt Bypass ab; Fast Path p95 < 300 ms, Recovery von 40 Missionen < 3 s, HUD < 300 kB ohne CDN; Golden Scenarios nach SPEC §24.1.

---

## Phasen 0–12

| Phase | Inhalt | Stand |
|-------|--------|-------|
| 0 Foundation | Repo, Specs, ADRs, Compose (Postgres/pgvector), `.env.example`, CI | ✔ |
| 1 Brain Core | Event Bus + Persistenz, Mission State Machine, Capability Registry mit Mocks, WebSocket-Stream | ✔ |
| 2 Permission + Execution + Verification | P0–P6, Approval, Execution Gateway, Verifier, Audit, Kill Switch | ✔ |
| 3 Claude Brain / Agent Runtime | `IntelligenceProvider`, Model Routing, Budgets, Subagents, Streaming-Events | ✔ |
| 4 Memory + User Model | Schema, Stores, Writer mit Sensitivity/Retention, Context Builder, Korrektur, „What JARVIS Knows“ | ✔ |
| 5 Voice 0.1 | Wake, STT/TTS-Streaming, Barge-in, Spoken Style, Latenz-Telemetrie | ✔ |
| 6 Desktop Agent + Minimal HUD | Desktop-Capabilities, Presence, web-first HUD-Shell, Approval-UI, Event Rail | ✔ (Tauri-Wrapper offen, ADR-0003) |
| 7 Live Coding | Monaco (lokal), Diff, Terminal, Sandbox-Runner, Preview, Writer/Test/Verifier, Artifacts | ✔ |
| 8 Home Core + Home Assistant | HA-Adapter, Device Registry, Home States, Voice-Satellit, WOL, Offline-Basics | ✔ |
| 9 Mobile + Secure Remote | Mobile HUD/PWA, Enrollment + Keypairs, signierte Requests, Push, Handover | ✔ |
| 10 Dynamic HUD + Globe | Scene Manager, AI-Core-States, News-Datenmodell, Globe, Quality Tiers | ✔ (Canvas-2D statt GPU-3D) |
| 11 Proactivity + Learning | Relevance Engine, Scheduler + Watchdog, Habit Detector, Daily Brief, Privacy-Modi | ✔ |
| 12 Skill Factory + Release 1.0 | Skill SDK, Review/Sandbox/Install, signierte Releases, Updater/Rollback, Backup, Regression-Suite | ✔ (1.0.0rc1) |

---

## Dokumentation

| Datei | Inhalt |
|-------|--------|
| `docs/JARVIS_Master_Blueprint_1.0.pdf`, `docs/SPEC.md` | Source of Truth und kompakte Spezifikation |
| `docs/SECURITY.md`, `docs/PERFORMANCE.md` | Normative Security-Regeln, Latenz-Budgets |
| `docs/STATUS.md` | Aktueller Stand, letzter Milestone, nächster exakter Schritt |
| `docs/HUD_EVENTS.md` | Event-Vertrag zwischen Core und HUD |
| `docs/HOME_ASSISTANT.md`, `docs/REMOTE.md`, `docs/RELEASE.md` | Home-Anbindung, Remote/Mobile, Install/Update/Rollback/Backup |
| `docs/decisions/ADR-0001…0004` | Core neben Prototyp, modularer Monolith, web-first HUD, Geräte-Auth |
| `CLAUDE.md` | Development Laws, Repo-Layout, Build/Test/Lint |
| `release/README.md` | Umgang mit dem Release-Schlüssel |

---

## Legacy-Prototyp (`jarvis/`)

> **Legacy.** Der ursprüngliche Voice-Prototyp bleibt gemäß ADR-0001 unverändert im Repo und läuft als eigener Prozess. Er führt seine 32 Tools **ohne** Permission Engine, Gateway und Verifier aus (dokumentiert in `docs/SECURITY.md` §8) und wird capability-weise hinter den Core migriert. Für neue Funktionen gilt ausschließlich der Core oben; der Core nutzt aus dem Prototyp nur den Audio-Stack (`voice/adapters/prototype.py`) und die Desktop-Tools (`JARVIS_DESKTOP=prototype`).

**Pipeline:** „Hey Jarvis“ (openWakeWord) → Whisper (faster-whisper) → LLM mit Tools (Ollama / OpenAI-kompatible API) → Kokoro TTS; Web-UI mit FastAPI + WebSocket; Memory SQLite + ChromaDB.

**Start**

```bash
ollama pull qwen3:8b          # lokales Modell (oder Cloud-Provider in config.yaml)
./start.sh                    # macOS/Linux → python -m jarvis.main
start.bat                     # Windows
```

Web-UI: **http://localhost:7860**. Bedienung: Voice („Hey Jarvis“ → „Yes?“ → Befehl), Chat-Leiste im Web-UI, **F2** für Tastatureingabe im Terminal.

**Konfiguration:** `config.yaml` (LLM-Provider `llm.providers` mit `type: ollama|openai`, `base_url`, `model`; STT `stt.model|device|compute_type`; TTS `tts.voice|speed`; Wake-Word-`threshold`). Änderbar auch im Config-Tab des Web-UI. API-Keys gehören nicht ins Repo.

**Tools (32):** Desktop/Vision (`read_screen`, `find_on_screen`, `click_at`, `type_text`, `press_key`, `scroll_screen`, `move_mouse`, `focus_window`, `get_open_windows`, `media_control`, `screenshot`), Apps/Web (`open_app`, `open_url`, `kill_process`, `web_search`, `fetch_page`, `get_weather`), System (`set_volume`/`get_volume`, `set_brightness`, `get_system_info`, `get_clipboard`/`set_clipboard`, `show_notification`, `set_timer`, `lock_screen`, `power_command`), Dateien/Code (`read_file`/`write_file`, `list_files`, `run_python`, `delegate_task`).

**Steuerung:** „Hey Jarvis“ im Leerlauf = zuhören, während einer Aufgabe = abbrechen; „Stop“/„Cancel“ nach dem Wake Word; **Esc** = sofortiger Abbruch; **Insert** (Windows) bzw. **F3**/**m** (macOS) = TTS stumm; ABORT-Button im Web-UI.

**macOS:** `install.sh` installiert `python@3.12 portaudio espeak-ng` per Homebrew. Dem startenden Terminal unter Systemeinstellungen → Datenschutz & Sicherheit **Mikrofon**, **Bedienungshilfen** und **Bildschirmaufnahme** erlauben; OCR nutzt Apple Vision, `press_key` übersetzt `win` → `cmd`, `alt` → `option`; `set_brightness` braucht `brew install brightness`.

**Troubleshooting:** `cublas64_12.dll not found` ⇒ normal ohne CUDA, STT fällt auf CPU zurück · Wake Word reagiert nicht ⇒ `threshold` senken (0.3), Standard-Mikrofon prüfen · kein Ton ⇒ Mute-Status und Audioausgabe prüfen · Ollama-Fehler ⇒ `ollama serve` läuft, `base_url` prüfen · Web-UI aktualisiert nicht ⇒ Browser-Konsole auf `[WS]`-Meldungen prüfen, Seite neu laden.

Layout: `jarvis/main.py` (Orchestrator), `wake.py`, `stt.py`, `tts.py`, `web.py`, `context.py`, `memory.py`, `llm.py`, `static/index.html`, `tools/` (router, desktop, app_control, web_search, system, file_ops, code_exec, subagent); Tests in `tests/test_*.py`.

---

## Lizenz

MIT (Angabe aus dem ursprünglichen Prototyp; eine `LICENSE`-Datei liegt noch nicht im Repo).
