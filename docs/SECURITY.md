# JARVIS – Security, Rechte und Vertrauen (SECURITY.md)

> Normative Sicherheitsregeln aus Master Blueprint 1.0, Abschnitt 7 (plus 11.3, 12.2, 15.2).
> Regel 9 der Development Laws (`CLAUDE.md`): Die Core-Security-Policy darf ohne explizite Owner-Freigabe nur strenger werden.

## 1. Permission-Level

| Level | Klasse | Beispiele | Standardverhalten |
|---|---|---|---|
| **P0** | Observe | Bildschirmstatus, Gerätezustand, Read-only-Sensoren | automatisch, protokolliert |
| **P1** | Safe | App öffnen, Licht schalten, lokale Suche | automatisch |
| **P2** | Reversible | Datei verschieben, Fenster anordnen | automatisch + Undo/Log |
| **P3** | Sensitive | E-Mail senden, Kalender ändern, Nachricht senden | Bestätigung je Kontext |
| **P4** | Critical | Software installieren, Deploy, Shutdown mit laufenden Jobs | starke Bestätigung / biometrisch |
| **P5** | Restricted | Secrets, Admin-/Security-Einstellungen | spezielle Policies, oft device-bound |
| **P6** | Forbidden | explizit gesperrte Aktionen | nie ausführen |

Jede side-effecting Capability trägt ein Risk Level und einen Verifier (Development Law 4). Welche konkreten Aktionen P3/P4/P5 brauchen und was dauerhaft P6 ist, wird vor Phase 2 vom Owner festgelegt (offene Entscheidung, SPEC §30).

## 2. Harte Sicherheitsregeln

1. **Voice-ID ist Komfortsignal, kein Authenticator.** Kritische Freigaben nur über entsperrtes vertrauenswürdiges Gerät, Passkey oder Biometrie.
2. **Secrets über Credential Broker / OS Keychain / Vault.** Das Modell sieht Schlüssel möglichst nie im Klartext; keine Secrets in Prompts, Logs, Fixtures, Code oder Commits.
3. **Temporäre Capabilities pro Mission.** Rechte laufen ab.
4. **Shell/OS-Aktionen nur in Sandbox oder über allowlisted Adapter.** Keine „Claude hat einfach Admin“-Architektur.
5. **Deterministische Gates vor jedem Tool-Aufruf** (PreToolUse-/Hook-artig): blockieren oder auf „Ask“ setzen, unabhängig vom Modell.
6. **Audit für jeden kritischen State Change**, inklusive vorherigem Zustand und möglichst Rollback-Punkt.
7. **Globaler Kill Switch:** „Jarvis, stop everything“ + UI-/Hardware-Schalter. Der Core stoppt aktive Agenten unabhängig vom Modell. Der Kill Switch darf von JARVIS niemals selbst gelockert oder abgeschaltet werden.
8. **Remote-Zugriff nie über offen ins Internet gestellte Admin-Ports.** Privater Mesh-VPN/WireGuard oder gleichwertiger Zero-Trust-Tunnel.
9. **Kein direkter Modellzugriff auf OS oder Secrets.** Alles läuft durch Permission Engine und Execution Gateway (Development Law 2).
10. **Das Modell kann eine verweigerte Aktion nicht umgehen.** Deny ist final auf Core-Ebene (Exit-Kriterium Phase 2).

## 3. Threat Model

| Risiko | Gegenmaßnahme |
|---|---|
| Prompt Injection aus Webseiten/Dateien | untrusted-content labels, Tool Scope, Allowlists, Verifier, keine Secrets im Prompt |
| Fehlinterpretierter Voice-Befehl | Confidence + risikobasierte Bestätigung + Gerätekontext |
| Falsches „fertig“ | unabhängiger Verifier + Outcome Checks |
| Kompromittiertes Smart Device | Netzsegmentierung/VLAN, Home-Assistant-Gateway, begrenzte Capabilities |
| Gestohlenes Handy | OS Lock, Device Key Revocation, Passkeys, Remote Revoke |
| Agent-Endlosschleife | Zeit-/Token-/Kosten-Budgets, Watchdog, Checkpoints |
| Datenverlust | versionierte verschlüsselte Backups, Snapshots, Restore Drills |
| UI täuscht Status vor | UI rendert nur server-signierte/persistierte Events, keine erfundenen Animationen |

## 4. Home Safety

- Türschloss, Alarm, Garagentor erhalten höhere Permission-Level als Licht/Musik.
- Kameradaten standardmäßig lokal und ereignisorientiert; klare Retention.
- Keine automatische Entriegelung nur aufgrund einer Sprachaufnahme.
- Power/Heizung/Netzwerk-Aktionen mit sicheren Zustandsmodellen und Fallbacks.

## 5. Coding Safety

- Neue Projekte/Skills zuerst in Sandbox oder isoliertem Workspace.
- Branch/Commit pro Feature; keine unkontrollierten Änderungen auf `main`.
- Tests und Verifier vor „fertig“; Dependencies bewertet, Lockfiles versioniert.
- Builds dürfen HUD/Voice nicht saturieren.

## 6. Selbstmodifikation

| Bereich | Erlaubt |
|---|---|
| Antwortstil / UI-Präferenz | automatisch im sicheren Bereich |
| Harmloser Shortcut / Vorschlag | automatisch vorschlagen |
| Routine mit externen Aktionen | nur nach Owner-Freigabe |
| Neuer Skill | entwickeln + testen autonom; installieren nach Review/Freigabe |
| Core-Code | Branch/PR; niemals heimlich in Produktion |
| Security Policy / Kill Switch | niemals selbst lockern oder abschalten |
| Admin-/Root-Rechte | nur explizit, zeitlich begrenzt, stark bestätigt |

## 7. Repository-Hygiene

- Keine Secrets, persönlichen Daten, Memory-DBs, Logs oder Home-Daten im Repo (`.gitignore`: `.env`, `jarvis/data/`, `*.log`, `*.wav`).
- Konfiguration über `.env` (lokal) nach Vorlage `.env.example` (nur Platzhalter).
- CI führt einen Secret Scan (gitleaks) über die gesamte Historie aus; ein Fund blockiert den Merge.
- Security-Tests (Prompt Injection, Permission Bypass, Secret Leakage, gefälschte Device-Events) sind Teil der Test-Strategie (SPEC §24).

## 8. Bestehender Voice-Prototyp (Hinweis)

Das bestehende `jarvis/`-Paket (Voice-Prototyp, siehe ADR-0001) führt Tools direkt nach LLM-Tool-Call aus, ohne Permission Engine und Verifier; Web UI ohne Auth, deshalb an `127.0.0.1` gebunden. Dieser Prototyp erfüllt die Regeln dieses Dokuments noch nicht und wird schrittweise hinter den neuen Core (Phase 1–2) gezogen. Bis dahin gilt: nur lokal, nur auf vertrauenswürdigen Geräten betreiben.
