# Release, Update, Rollback, Backup (SPEC §18, §29)

Alles hier läuft ohne Netzwerk außer dem Download des Release-Archivs. Der Updater installiert
nur, was mit dem committeten Release-Schlüssel (`release/jarvis-release.pub`) verifiziert wurde.

## Einmalig: Release-Schlüssel (Owner)

```bash
python -m core.release keygen ~/jarvis-release-key
# public key  -> ~/jarvis-release-key/jarvis-release.pub  → nach release/ kopieren und committen
# private key -> ~/jarvis-release-key/jarvis-release.key  → NIE committen
```

Den Inhalt von `jarvis-release.key` als GitHub-Actions-Secret `JARVIS_RELEASE_SIGNING_KEY`
hinterlegen. Ohne Secret baut `.github/workflows/release.yml` ein unsigniertes Archiv und warnt;
der Updater lehnt es ab.

## Release erzeugen

```bash
git tag v1.0.0 && git push origin v1.0.0
```

Workflow `Release`: Regressionssuite (`tests/core` + `tests/regression`) → reproduzierbares
`dist/jarvis-<version>.tar.gz` (Quellordner `core adapters voice apps skills docs infra release`,
Requirements, Start-Skripte, `VERSION`) → `SHA256SUMS` → `*.sig` (Ed25519) → GitHub Release.

Lokal identisch:

```bash
python -m core.release archive --version 1.0.0 --out dist
python -m core.release sums dist/jarvis-1.0.0.tar.gz > dist/SHA256SUMS
JARVIS_RELEASE_SIGNING_KEY=... python -m core.release sign dist/jarvis-1.0.0.tar.gz
python -m core.release verify dist/jarvis-1.0.0.tar.gz dist/jarvis-1.0.0.tar.gz.sig
```

## Clean Install

```bash
git clone <repo> && cd Jarvis && ./install.sh      # venv + Abhängigkeiten (Windows: install.bat)
python -m core                                     # HUD: http://127.0.0.1:7870/hud/
```

Datenverzeichnis: `jarvis/data/` (Event-Log `core.db`, Workspaces, Skills). Konfiguration über
`.env` aus `.env.example`; Secrets nur über Umgebung/Credential-Broker.

## Update mit Rollback

```bash
python -m core.updater status
python -m core.updater apply dist/jarvis-1.0.1.tar.gz dist/jarvis-1.0.1.tar.gz.sig
python -m core.updater rollback
```

Ablauf von `apply`: Signatur prüfen → Archiv-Mitglieder prüfen (kein `..`, keine Symlinks, nur
`jarvis-<version>/`) → nach `<install_root>/versions/<version>/` entpacken → Smoke-Test
(`import core, adapters, skills` aus dem neuen Baum) → erst dann `current.json` umschalten.
Jeder Fehler lässt `current` unangetastet und entfernt den halb installierten Baum.
`rollback` schaltet auf `previous` zurück (ebenfalls nach Smoke-Test), ohne Netzwerk.
Install-Root: `JARVIS_INSTALL_ROOT` (Default `~/.jarvis/app`). Der laufende Core wird vom Owner
neu gestartet; der Updater beendet keine Prozesse.

## Backup und Restore (verschlüsselt)

```bash
export JARVIS_BACKUP_PASSPHRASE='…mindestens 8 Zeichen…'
python -m core.backup create  backups/jarvis-$(date +%F).jbk        # aus jarvis/data
python -m core.backup verify  backups/jarvis-2026-09-06.jbk
python -m core.backup restore backups/jarvis-2026-09-06.jbk /pfad/zum/neuen/data-dir
```

Format `JBK1`: AES-256-GCM über ein tar.gz, Schlüssel per scrypt aus der Passphrase, Header als
AAD. Falsche Passphrase oder ein gekipptes Byte ⇒ Authentifizierung schlägt fehl, nichts wird
geschrieben. Enthalten: `core.db`, `workspaces/`, `skills/`, `settings/`, `memory/`. Die
Passphrase kommt ausschließlich aus der Umgebung.

### Backup-Drill (vor jedem Release)

1. `create` gegen das echte `jarvis/data`.
2. `verify` mit derselben Passphrase; Manifest muss Dateien und `core_version` zeigen.
3. `restore` in ein leeres Verzeichnis, Core mit `JARVIS_CORE_DB_URL=sqlite:///<dir>/core.db`
   starten, `GET /health` und `GET /missions` prüfen.
4. Restore-Verzeichnis löschen. Der Drill ist in `tests/regression/test_release_updater_backup.py`
   automatisiert (synthetische Daten).

## Regressionssuite

`tests/regression/` läuft in CI (Job `regression`) und im Release-Workflow:

- `test_release_updater_backup.py` – Signieren/Verifizieren, manipulierte Archive, Updater
  apply/rollback, fehlgeschlagener Smoke lässt `current` stehen, Backup-Roundtrip.
- `test_security_invariants.py` – Policy nur verschärfbar, P6 nie ausgeführt, Kill Switch stoppt
  Seiteneffekte, keine Secrets im Event-Log, unsigniert-remote kann nicht freigeben, Skill-Review
  lehnt Bypass ab.
- `test_performance_budgets.py` – Budgets aus `docs/PERFORMANCE.md` (Event-Publish, Fast-Path,
  Recovery, HUD-Asset-Größe).
- `test_golden_scenarios.py` – SPEC §24.1 End-to-End-Szenarien gegen Fake-Adapter.
