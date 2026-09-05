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
- `core/` – neuer JARVIS Core (Phase 1 läuft): `core/events/` (Envelope, EventBus, SQLEventStore) `core/missions/` (Mission/Task-Modelle, State Machine, MissionEngine, Repository) `core/permissions/` (P0–P6, Policy nur verschärfbar, Approval-Workflow, PermissionEngine) `core/capabilities/` (Manifest, Registry, Mocks `mock.echo`/`mock.clock`/`mock.open_url`, ExecutionGateway mit Timeout/Retry/Kill Switch) `core/verifier/` (Outcome, VerifierRegistry, VerificationService, RetryPolicy, VerifiedExecutor), `core/intents/` (deterministischer Fast-Path-Router), `core/api/` (FastAPI: /health, /events, /ws/events, /commands, /missions, /approvals, /kill, /resume, /debug) `core/runtime.py` (CoreRuntime verdrahtet alles; `python -m core` startet auf 127.0.0.1:7870) und `core/models/` (IntelligenceProvider-Interface, ModelRouter Fast/Smart/Deep, AgentBudget, MockProvider, ClaudeProvider-Skeleton – Anthropic SDK optional via `pip install anthropic`, Key nur über Umgebung/Credential-Broker) vorhanden; state, memory, models, agents, capabilities, verifier, scheduler folgen. Eigene Abhängigkeiten: `core/requirements.txt`.
- `adapters/`, `voice/`, `apps/`, `skills/`, `mcp/`, `packages/` – gemäß SPEC §20, entstehen phasenweise.
- `infra/docker/` – Docker Compose (PostgreSQL + pgvector). `.env` lokal aus `.env.example`.
- `tests/` – Prototyp-Tests (`tests/test_*.py`), Core-Tests unter `tests/core/`.
- `docs/` – Blueprint-PDF, SPEC, SECURITY, PERFORMANCE, STATUS, `decisions/ADR-*`.

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

# Core starten (Debug-Dashboard http://127.0.0.1:7870/debug, DB: jarvis/data/core.db oder JARVIS_CORE_DB_URL)
python -m core

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
