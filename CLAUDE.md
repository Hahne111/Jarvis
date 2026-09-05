# CLAUDE.md – Project JARVIS

Source of Truth: `docs/JARVIS_Master_Blueprint_1.0.pdf` → kompakt in `docs/SPEC.md`. Aktueller Stand: `docs/STATUS.md`. Deltas zur PDF nur als ADR in `docs/decisions/`.

## JARVIS DEVELOPMENT LAWS
1. Read `docs/SPEC.md`, `docs/SECURITY.md`, `docs/PERFORMANCE.md` before architectural changes.
2. Never bypass PermissionEngine or ExecutionGateway.
3. No model/provider-specific logic outside adapters/models.
4. Every side-effecting capability has risk level + verifier.
5. Write/update tests with every behavior change.
6. No secrets in code, prompts, logs, fixtures or commits.
7. Keep UI/audio threads non-blocking.
8. Do not claim a task succeeded before verifier passes.
9. Core security policy may only become stricter without explicit owner approval.
10. If spec and code conflict, stop and report the conflict.

## Arbeitsregel
Eine Session = genau ein abgegrenzter Milestone mit Definition of Done (SPEC §21/§22). Vor dem Coden: betroffene Module und Invarianten, Security-/Performance-Risiken, Plan + Akzeptanztests. Während: minimale, modulare Änderungen; Tests/Lint laufen lassen; keine Platzhalter für Pflichtverhalten. Danach: Diff als skeptischer Maintainer reviewen, Restrisiken nennen, `docs/STATUS.md` aktualisieren. Nicht „fertig“ sagen, bevor Tests und Verifier-Kriterien grün sind.

## Repo-Layout (Ist)
- `jarvis/` – bestehender Voice-Prototyp (Wake → Whisper → LLM+Tools → Kokoro, FastAPI-UI). **Legacy-Pfad, unverändert lassen** (ADR-0001). Wird capability-weise hinter den Core migriert.
- `core/` – neuer JARVIS Core (Phase 1 läuft): `core/events/` (Envelope, EventBus, SQLEventStore) `core/missions/` (Mission/Task-Modelle, State Machine, MissionEngine, Repository) `core/permissions/` (P0–P6, Policy nur verschärfbar, Approval-Workflow, PermissionEngine) `core/capabilities/` (Manifest, Registry, Mocks `mock.echo`/`mock.clock`/`mock.open_url`, ExecutionGateway mit Timeout/Retry/Kill Switch) `core/verifier/` (Outcome, VerifierRegistry, VerificationService, RetryPolicy, VerifiedExecutor), `core/intents/` (deterministischer Fast-Path-Router), `core/api/` (FastAPI: /health, /events, /ws/events, /commands, /missions, /approvals, /kill, /resume, /debug) `core/presence.py` (PresenceService: idle/listening/thinking/speaking/working/awaiting_approval/halted pro Gerät, deterministisch aus Events abgeleitet, `presence.changed`, `GET /presence`; HUD-Event-Vertrag in `docs/HUD_EVENTS.md`) `core/runtime.py` (CoreRuntime verdrahtet alles; `python -m core` startet auf 127.0.0.1:7870) `core/models/` (IntelligenceProvider-Interface, ModelRouter Fast/Smart/Deep, AgentBudget, MockProvider, ClaudeProvider – Anthropic SDK optional via `pip install anthropic`, Key nur über Umgebung/Credential-Broker) und `core/agents/` (AgentCoordinator: Router → Provider → Allowlist → VerifiedExecutor → Budget; pausiert bei Approval und setzt aus dem Event-Log fort; Subagent-Rollen research/implementation/test/verification/security über Tool `agent.delegate`, kontext-isoliert, Tiefe 1, gemeinsames Budget; Coding-Workflow: Rollen binden `workspace.*` (test nur `pytest`/`python` in `workspace.run`), Mission mit Codeänderung gilt erst nach verifiziertem grünem `workspace.run` als COMPLETED, sonst `not_verified`; `artifact.created` pro Workspace-Datei beim Abschluss; `JARVIS_PROVIDER=claude|mock|none`) und `core/memory/` (MemoryItem nach SPEC §8.2, MemoryStore mit lexikalischer Suche + austauschbarem Embedder, MemoryWriter: Privacy-Filter, Reinforcement, Korrektur als neue Version, Forget/Forget-Window/Pin/Temporary; ContextBuilder speist relevante, nicht-geheime Items in den Agent-Systemprompt; Capabilities `memory.recall|remember|correct|forget` über das Gateway; API `/memory*` = „What JARVIS Knows“; Events tragen nie den Wert) vorhanden; state und scheduler folgen. Eigene Abhängigkeiten: `core/requirements.txt`.
- `voice/` – Voice 0.1 (Phase 5): Interfaces `WakeWordDetector`/`SpeechToText`/`TextToSpeech`/`TurnDetector`, `VoiceSession`-Zustandsautomat (idle → wake_ack → listening → thinking → speaking → follow_up) mit `voice.*`-Events, `VoiceBridge` (Wake-Ack zuerst, Transkript → `core/api/commands.run_text_command`, phrase-level TTS-Streaming, Barge-in, „Jarvis, stop“ ⇒ Kill Switch), `LatencyTelemetry` (`telemetry.latency`-Events, p50/p95), `SpokenStyle` (No-Filler, „Done.“), Fakes in `voice/fakes.py`, Prototyp-Adapter in `voice/adapters/prototype.py` (importiert `jarvis/` unverändert). `python -m voice` (Hardware) bzw. `JARVIS_VOICE_FAKE=1 python -m voice` (Tastatur).
- `adapters/desktop/` – Desktop Agent (Phase 6): Capabilities `computer.open_app|list_windows|focus_window|screenshot|type_text|press_key`, `system.info|set_volume|lock_screen` mit Risk Levels und Verifiern (Backend liefert unabhängige Signale oder `None` ⇒ UNKNOWN), `FakeDesktop` (Tests/CI) und `PrototypeDesktop` (wrappt `jarvis/tools` unverändert, lazy import). Aktivierung `JARVIS_DESKTOP=off|fake|prototype` (Default off). Fast-Path-Regeln im Intent Router: „open <app>“, „lock screen“, „volume 30“, „show windows“.
- `adapters/workspace/` – sandboxed Projekt-Workspaces (Phase 7): ein Ordner pro Mission unter `JARVIS_WORKSPACE_ROOT` (Default `jarvis/data/workspaces/<mission_id>`), Pfad-Sandbox (kein `..`, keine absoluten Pfade, Symlinks nie gefolgt), Capabilities `workspace.list|read` (P0), `workspace.write` (P2, Versionskopie unter `.jarvis/versions/`, Verifier `workspace.file_matches`), `workspace.diff` (P0), `workspace.run` (P3 ⇒ Approval; Allowlist python/pytest/node/npm/git…, Timeout, CPU-Limit, bereinigte Umgebung ohne Secrets, Verifier `workspace.run_exit_code`); Events `workspace.file.changed` (mit Diff) und `workspace.run.started|output|finished` für Editor/Terminal-Panels.
- `apps/desktop/web/` – web-first HUD-Shell (ADR-0003): reines HTML/CSS/ES-Module ohne Build, vom Core unter `http://127.0.0.1:7870/hud/` ausgeliefert (`/` leitet dorthin); zentraler AI-Core aus `presence.changed`, Event Rail (WS `after_seq`, Bereichsfilter), Approvals, Missions, Memory, Kill Switch, Latenz-Kacheln, Coding-Modus (Projektbaum, Dateiansicht, Diff aus `workspace.file.changed`, Terminal aus `workspace.run.output`, Preview-iframe über `GET /workspace/{mission}/preview/<path>` mit CSP-Sandbox); Event-Batching pro Frame, kein Polling außer `/health` alle 5 s. Tauri-Wrapper folgt als eigener Milestone.
- `skills/`, `mcp/`, `packages/` – gemäß SPEC §20, entstehen phasenweise.
- `infra/docker/` – Docker Compose (PostgreSQL + pgvector). `.env` lokal aus `.env.example`.
- `tests/` – Prototyp-Tests (`tests/test_*.py`), Core-Tests unter `tests/core/`.
- `docs/` – Blueprint-PDF, SPEC, SECURITY, PERFORMANCE, STATUS, HUD_EVENTS (Event-Vertrag für die Shell), `decisions/ADR-*`.

## Build / Test / Lint
```bash
# Setup (einmalig): venv + Abhängigkeiten
./install.sh            # macOS/Linux
install.bat             # Windows
# Linux-CI-Systemabhängigkeiten: libportaudio2 python3-tk xvfb (pyautogui/sounddevice brauchen Display + PortAudio)

# Tests
pytest -q               # alles; Linux headless: xvfb-run -a pytest -q
pytest -q tests/core    # nur Core (braucht nur core/requirements.txt, Python 3.12)

# Lint / Format (Konfiguration: ruff.toml)
ruff check .            # Fatal-Regeln im Altcode; strikt für core/ und tests/core/ (core/ruff.toml, tests/core/ruff.toml)
ruff format --check .   # Legacy-Dateien (jarvis/, bestehende tests/test_*.py) ausgenommen

# Datenbank (Phase 1+)
cp .env.example .env    # Werte lokal setzen, .env ist gitignored
docker compose -f infra/docker/docker-compose.yml up -d

# Core starten (HUD http://127.0.0.1:7870/hud/, Debug-Dashboard /debug, DB: jarvis/data/core.db oder JARVIS_CORE_DB_URL)
python -m core                      # Provider: JARVIS_PROVIDER=claude (Default, braucht anthropic SDK + Key) | mock | none
                                    # Desktop: JARVIS_DESKTOP=off (Default) | fake | prototype (echter PC, pyautogui)

# Voice-Loop gegen den Core (Prototyp-Audio-Stack; JARVIS_VOICE_FAKE=1 für Tastatur statt Mikrofon)
python -m voice

# Prototyp starten
./start.sh | start.bat  # Web-UI http://127.0.0.1:7860
```

## Git-Workflow
- `main` ist geschützt (Owner setzt Branch Protection). Nie direkt auf `main` committen.
- Feature-Branch → Commits mit klarem Präfix (`core:`, `security:`, `docs:`, `infra:`, `api:`, `ai:`, `tests:`) → PR → CI grün (format, lint, tests, secret scan, smoke) → Merge.
- Keine Secrets, Memory-DBs (`jarvis/data/`), Logs oder Audio im Repo.

## Security-Regeln (Kurzform, normativ in docs/SECURITY.md)
- P0–P6 Risk Levels; P6 wird nie ausgeführt; Deny ist auf Core-Ebene final.
- Kein Modell erhält direkten Zugriff auf OS, Dateien, Netzwerk oder Secrets. Provider liefern Tool-Calls nur als Vorschlag; Ausführung ausschließlich über ExecutionGateway nach Allowlist (`filter_tool_calls`).
- Kill Switch und Security Policy dürfen von JARVIS nie selbst gelockert werden.
- UI zeigt nur persistierte/echte Events, keine erfundenen Zustände.
- Memory: `secret`-Items gehen nie in einen Cloud-Prompt und nie in Events; `memory.*`-Events enthalten nur Metadaten. Handler erreichen die Missions-Korrelation über `core.capabilities.gateway.current_correlation_id`.
