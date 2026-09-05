# JARVIS – STATUS

Current version: 0.1.0.dev0 (Brain Core 0.1 in Arbeit)
Current phase: PHASE 1 – Brain Core 0.1 (Commit 004 erledigt; 005–009 offen)
Last completed milestone: Commit 004 „core: typed event bus“ – `core/events/`: validiertes Event-Envelope (SPEC §5.2, frozen dataclass, JSON-Roundtrip, Korrelationskette via `follow_up`), `SQLEventStore` (SQLAlchemy Core, SQLite/Postgres, append-only mit `seq`-Ordnung, idempotent per event_id, Replay nach Korrelation/Typ-Präfix/seq), `EventBus` (persistiert vor Zustellung, Muster `*`/`prefix.*`/exakt, sync+async Handler, Fehlerisolation, `replay_to` für State-Rebuild nach Restart). 24 Core-Tests unter `tests/core/`, strikte Ruff-Regeln nur für neuen Code.
Previously: Phase 0 – Blueprint als Source of Truth im Repo (PDF, SPEC.md, SECURITY.md, PERFORMANCE.md, ADR-0000..0002, CLAUDE.md), Docker Compose PostgreSQL/pgvector + `.env.example`, CI (format, lint, tests, secret scan, smoke). Exit-Kriterien: Clean clone → `pip install -r requirements.txt` → 33/33 Tests grün (GitHub Actions Run 33957387633 auf Python 3.11, lokal auch auf 3.12 ohne openWakeWord); gitleaks ohne Fund; ADR-0000..0002 vorhanden.
Note: Volle requirements.txt ist unter Linux nur mit Python 3.11 installierbar (openWakeWord → tflite-runtime ohne 3.12-Wheels); Windows/macOS sind nicht betroffen. Core (`core/`) zielt auf 3.12 und bekommt ab Phase 1 einen eigenen schlanken CI-Job.
Active branch: claude/leg-los-lkj4yb
Known blockers:
- **Repo-Sichtbarkeit:** `Hahne111/Jarvis` ist aktuell PUBLIC. Blueprint §18/Phase 0 verlangt ein privates Repository (Spec, Memory-Konzepte, spätere Releases). Owner-Entscheidung: Repo auf private stellen oder Blueprint-PDF/SPEC bewusst öffentlich lassen.
- Branch-Schutz für `main` muss der Owner in den GitHub-Repo-Einstellungen setzen (PDF Phase 0, Schritt 1); Workflow: Feature-Branch → PR → CI grün → Merge.
- Blueprint §30: offene Hardware-/Geräte-/Voice-Entscheidungen stehen aus; blockieren Phase 1 nicht.
Open security findings:
- Legacy-Voice-Prototyp (`jarvis/`) führt Tools ohne Permission Engine/Verifier aus (dokumentiert in SECURITY.md §8, ADR-0001). Wird durch Phase 1–2 behoben, nicht durch Patch des Prototyps.
Next exact task: Phase 1, Commit 005 „core: mission state machine“ – `core/missions/`: Mission/Task-Modelle (SPEC §17.1), Zustandsautomat CREATED → PLANNING → WAITING_FOR_APPROVAL → RUNNING → VERIFYING → COMPLETED plus PAUSED/BLOCKED/FAILED/CANCELED (SPEC §5.3), jede Transition emittiert ein Event über den EventBus und wird persistiert (Tabellen via SQLAlchemy, gleiche Engine wie Store); Rebuild des Mission-States aus dem Event-Log nach Restart; Transition-Tests. Danach 006 Permission Engine (P0–P6), 007 Capability Registry + Mock-Tools `echo`/`clock`/`open_url`, 008 Verifier, 009 WebSocket-Eventstream.
Last benchmark: n/a (Telemetrie ab Phase 5)
Last backup/restore drill: n/a (ab Phase 12)
