# HUD Event Contract (Phase 6, SPEC §3.2, §5.2, §12.1)

Das HUD (Tauri/React, später Mobile) rendert **ausschließlich persistierte Events** aus dem Core
(SECURITY.md §3 „UI täuscht Status vor“). Es gibt keinen zweiten Zustandskanal.

## Transport
- Replay: `GET /events?after_seq=N&correlation_id=&type_prefix=&limit=`
- Live: `WS /ws/events?after_seq=N` – erst Replay ab `N`, dann Live, lückenlos; jede Zeile `{seq, ...Event}`
- Zustand auf Abruf: `GET /health`, `GET /presence`, `GET /missions`, `GET /approvals`, `GET /memory`
- Reconnect: mit dem letzten gesehenen `seq` neu verbinden (kein eigener Zustand nötig)

## Event Envelope (SPEC §5.2)
`event_id, type, timestamp, source, correlation_id, user_id, device_id, sensitivity, priority, payload, ttl`
`correlation_id` = Mission (bzw. `voice`, `memory`, `presence` für missionslose Ereignisse).
`priority: critical|urgent` ⇒ sofort sichtbar machen (Kill Switch, Approval, Barge-in).

## Welche Events welchen HUD-Modus speisen

| HUD-Bereich (SPEC §3.2) | Event-Typen | Hinweis |
|---|---|---|
| Central AI Core (Listening/Thinking/Speaking/Working) | `presence.changed` | eine Quelle für den Core-Zustand pro `device_id`; `halted` überlagert alles |
| Voice-Feedback | `voice.wake_ack`, `voice.listening`, `voice.transcript` (partial/final), `voice.thinking`, `voice.speaking`, `voice.barge_in`, `voice.idle` | Wake-Ack kommt *vor* jeder Core-Arbeit |
| Mission-Modus (Goal tree, Status, Checkpoints) | `mission.created`, `mission.<status>`, `mission.task.*`, `mission.checkpoint`, `command.received` | Status = letzter `mission.<status>`-Event der Korrelation |
| Agent Rail (Coordinator, Subagents – ohne Chain-of-Thought) | `agent.run.started|step|paused|resumed|finished|budget_exceeded`, `agent.subrun.started|finished`, `agent.tool.proposed|rejected` | `role`, `parent_run_id`, `depth` im Payload |
| Tool-/Capability-Log | `permission.allowed|ask|approved|denied|consumed`, `capability.invoked|succeeded|failed|timeout|denied|awaiting_approval|invalid|halted`, `verification.passed|failed|unknown|skipped` | „succeeded“ ≠ „Ziel erreicht“: erst `verification.passed` zählt |
| Approval-UI | `permission.ask` (offen), `permission.approved|denied` (geschlossen); Aktionen `POST /approvals/{id}/approve|deny` | Proof-Stärke steht im Payload (`required_strength`) |
| Kill Switch | `gateway.halted` (critical), `gateway.resumed`; Aktionen `POST /kill`, `POST /resume` | Resume nur mit starkem Proof |
| Memory-Panel „What JARVIS Knows“ | `memory.written|reinforced|updated|corrected|conflict|forgotten|forgotten_window|pinned|unpinned|made_temporary|expired|context_used|policy_changed`, Daten via `GET /memory` | Events enthalten nie den Wert |
| Performance-Overlay | `telemetry.latency` (`point`, `ms`, `budget_ms`, `within_budget`) | p50/p95 lokal berechnen oder `voice.telemetry.summary_from_log` |
| Coding: Tree/Editor/Diff | `workspace.file.changed` (`path`, `diff`, `sha256`, `actor`), Daten via `GET /workspace/{mission}/files|file|diff`; Speichern `PUT /workspace/{mission}/file` (läuft als `workspace.write` durch das Gateway, `actor=owner:<device>`) | Editor: Monaco nur aus `/hud/vendor` (lokal, `fetch_monaco.py`), sonst Textarea |
| Coding: Terminal | `workspace.run.started` (`command`, `args`), `workspace.run.output` (`stream`, `chunk`), `workspace.run.finished` (`exit_code`, `timed_out`, `duration_ms`) | stderr hervorheben; Exit-Zeile am Ende |
| Coding: Quality | `workspace.run.finished` + `verification.passed|failed` (`capability=workspace.run`), `agent.run.finished` (`outcome`, z. B. `not_verified`) | „grün“ = letzter Run Exit 0 **und** `verification.passed` |
| Coding: Artifacts | `artifact.created` (`path`, `size`, `sha256`, `run_id`) beim Abschluss einer Coding-Mission | Öffnen über `/file`, Vorschau über `/preview/<path>` |
| Coding: Preview | `GET /workspace/{mission}/preview/<path>` im `<iframe sandbox="allow-scripts">` | nur Dateien des Missions-Workspaces, CSP-Sandbox, kein Cache |
| Power / WOL | `power.wake.sent` (`name`, `host`, `port`, `actor`; nie die MAC), Erreichbarkeit via Capability `power.status` | `power.wake` ist P3 ⇒ erscheint als `permission.ask` |
| Voice-Satelliten | `voice.transcript|thinking|speaking|idle` mit `device_id=satellite:<id>` (Quelle `satellite`, `POST /satellite/command`) | Presence pro Satellit; Antworttext im `voice.speaking`-Payload |
| Globe / News (Phase 10) | `news.event.created|updated` (NewsEvent: `country`, `lat/lon`, `topics`, `sources[quality]`, `confidence`, `breaking`, `forecast`), `news.refreshed`; Daten via `GET /news`, `GET /news/countries` | Globe/Cards rendern nur Store-Daten; „breaking“ = vorläufig, „forecast“ = Prognose getrennt |
| Scene / Performance | `command.received` mit `intent.capability=news.top` ⇒ HUD wechselt in den Globe-Modus; `telemetry.latency` (`hud_frame`, `globe_frame`, `hud_mode_switch`, `hud_input`) via `POST /telemetry` | Quality Tiers high/medium/low aus eigener Frame-Zeit; Budget 16.7 ms |
| Proactive (Phase 11) | `job.created|started|finished|deleted`, `mission.watchdog` (urgent), `habit.detected`, `automation.suggested|accepted|dismissed`, `brief.ready` (`text`, `sections`), `privacy.changed` (`from`, `to`, `learning`, `satellites_paused`), `notify.suppressed` (Relevance-Gate); Daten via `GET /schedule`, `/suggestions`, `/brief`, `/privacy` | Vorschläge werden nie automatisch aktiviert; Push nur für Kanal `now` (und `opportunistic` bei niedrigen Unterbrechungskosten) |
| Push / Notifications | `notify.sent|failed` (`title`, `body`, `priority`, `tags`, `click`, `channel`; nie Secrets), Daten via `GET /notifications` | gepusht: `permission.ask`, `gateway.halted`, `mission.failed`, `device.revoked` |
| Mission-Handover | `mission.handover` (`from_device`, `to_device`, `status`, `note`, `by`; `device_id` = Ziel) + Checkpoint `handover` in der Mission | Presence: Ziel übernimmt `active_mission` (awaiting_approval/working), Quelle wird idle |
| Devices (Phase 9, ADR-0004) | `device.enrollment.started|failed`, `device.enrolled`, `device.trust.changed`, `device.revoked`, `device.auth.failed`; Daten via `GET /devices` (Fingerprint, Trust, last_seen – nie der Key) | Enrollment-Code kommt nur aus der Antwort von `POST /devices/enroll/start`, nie aus einem Event |
| Home (Phase 8) | `home.device.changed` (`entity_id`, `from`, `to`, `domain`, `service`, `actor`), `home.state.changed` (`from`, `to`, `policy`), Daten via `GET /home` (Räume, Geräte, State, `online`) | Aktionen nur über `/commands` bzw. Capabilities `home.*`; Lock/Alarm/Garage erscheinen als `permission.ask` mit `required_strength=3` |

## Presence-Zustände (`presence.changed`)
`idle | listening | thinking | speaking | working | awaiting_approval | halted` pro `device_id`
(`core` für geräteunabhängige Ereignisse). Ableitung: `voice.*` → listening/thinking/speaking/idle,
`agent.run.*` → working, `permission.ask` → awaiting_approval, `gateway.halted` → halted (alle Geräte).

## Regeln für die Shell (PERFORMANCE.md)
- Renderer und Event-Client in getrennten Threads/Workern; kein synchrones Warten auf den Core.
- Kein UI-Polling: WebSocket-Push; `/health` höchstens alle paar Sekunden.
- Progressive Loading: Core-Zustand sofort, Listen/Globus nachgelagert.
- Frame-Time lokal messen und als `telemetry.latency` (`point=hud_frame`) zurückmelden (später).
