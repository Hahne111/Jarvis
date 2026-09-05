# JARVIS – STATUS

Current version: 0.0.1 (Foundation)
Current phase: PHASE 0 – Foundation / Source of Truth (abgeschlossen, CI-Lauf auf GitHub noch zu bestätigen)
Last completed milestone: Phase 0 – Blueprint als Source of Truth im Repo (PDF, SPEC.md, SECURITY.md, PERFORMANCE.md, ADR-0000..0002, CLAUDE.md), Docker Compose PostgreSQL/pgvector + `.env.example`, CI (format, lint, tests, secret scan, smoke), lokale Testsuite grün (33/33).
Active branch: claude/leg-los-lkj4yb
Known blockers:
- **Repo-Sichtbarkeit:** `Hahne111/Jarvis` ist aktuell PUBLIC. Blueprint §18/Phase 0 verlangt ein privates Repository (Spec, Memory-Konzepte, spätere Releases). Owner-Entscheidung: Repo auf private stellen oder Blueprint-PDF/SPEC bewusst öffentlich lassen.
- Branch-Schutz für `main` muss der Owner in den GitHub-Repo-Einstellungen setzen (PDF Phase 0, Schritt 1); Workflow: Feature-Branch → PR → CI grün → Merge.
- Blueprint §30: offene Hardware-/Geräte-/Voice-Entscheidungen stehen aus; blockieren Phase 1 nicht.
Open security findings:
- Legacy-Voice-Prototyp (`jarvis/`) führt Tools ohne Permission Engine/Verifier aus (dokumentiert in SECURITY.md §8, ADR-0001). Wird durch Phase 1–2 behoben, nicht durch Patch des Prototyps.
Next exact task: Phase 1, Commit 004 „core: typed event bus“ – Paket `core/` anlegen (`core/events/`): Event-Envelope (SPEC §5.2) als typisiertes Modell, in-process Pub/Sub, durable Persistenz (Postgres, Fallback SQLite für Tests), Tests unter `tests/core/`. Danach 005 Mission State Machine.
Last benchmark: n/a (Telemetrie ab Phase 5)
Last backup/restore drill: n/a (ab Phase 12)
