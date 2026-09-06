# Home Assistant, Voice Satellites und Wake-on-LAN (Phase 8)

Home Assistant (HA) bleibt das Geräte-Gateway (SPEC §11). JARVIS spricht nur mit HA, nie mit
Herstellerprotokollen. Alles läuft durch PermissionEngine → ExecutionGateway → Verifier; das
HUD zeigt nur persistierte Events. Keine Secrets im Repo (SECURITY.md §7).

## 1. Core an Home Assistant anbinden

```bash
# lokal in .env (gitignored) oder in der Umgebung des Core-Prozesses
JARVIS_HOME=homeassistant
JARVIS_HA_URL=http://homeassistant.local:8123
JARVIS_HA_TOKEN=<Long-Lived Access Token aus HA → Profil → Sicherheit>
python -m core
```

- Der Token wird nur aus der Umgebung gelesen (`adapters/home/backend.py`), taucht nie in
  Events, Logs oder `repr()` auf. HA-Fehler 401/403 oder Netzfehler ⇒ `HomeUnavailable`
  ⇒ Capability `FAILED`, nie ein geratener Erfolg.
- `GET /home` liefert Räume (HA-Areas), Geräte und den Home-State. `JARVIS_HOME=fake` liefert
  eine Demo-Wohnung für HUD und Tests.
- Risiko-Stufen: Licht/Schalter/Rollo/Klima/Szene/Home-State **P2** (Verifier = Read-back);
  Schloss/Alarm/Garage **P4** ⇒ vertrauenswürdiges Gerät **und** starker Proof (Passkey).
  Sprache oder ein UI-Klick entriegeln nie (SECURITY.md §4).

## 2. Voice Satellite („Hey Jarvis“ über HA Assist)

HA erkennt das Wake-Word lokal (microWakeWord/Android) und liefert den finalen Text. Der Core
nimmt ihn über `POST /satellite/command` entgegen und antwortet mit einem kurzen Sprechtext:

```json
POST /satellite/command
{"text": "licht an im wohnzimmer", "satellite_id": "kitchen-puck", "device_trusted": false}
→ {"status": "completed", "speech": "Done.", "device_id": "satellite:kitchen-puck", ...}
```

- Ein Satellit ist ein Gerät wie jedes andere: `device_trusted` ist standardmäßig `false`
  (der Owner trägt vertrauenswürdige Satelliten in seiner HA-Konfiguration mit `true` ein).
  Eine Sprachaufnahme erfüllt nie eine P3+-Freigabe: die Antwort lautet dann
  „That needs your confirmation on the HUD or your phone.“ und die Freigabe wartet in
  `/approvals`.
- Events pro Satellit: `voice.transcript` (final), `voice.thinking`, `voice.speaking`,
  `voice.idle` mit `device_id=satellite:<id>` ⇒ Presence pro Gerät im HUD.
- „Jarvis, stop“ vom Satelliten löst den Kill Switch aus (Antwort „Stopped.“).

Beispiel HA-Konfiguration (der Core läuft auf dem Home-Mini-PC im LAN, hier 192.168.1.10):

```yaml
# configuration.yaml
rest_command:
  jarvis_command:
    url: "http://192.168.1.10:7870/satellite/command"
    method: POST
    content_type: "application/json"
    payload: >-
      {"text": {{ text | tojson }}, "satellite_id": {{ satellite | tojson }},
       "device_trusted": false}

# automations.yaml – Assist-Satz „Jarvis, {text}“ an den Core weiterreichen
- alias: Jarvis satellite
  trigger:
    - platform: conversation
      command: "Jarvis {text}"
  action:
    - service: rest_command.jarvis_command
      data:
        text: "{{ trigger.slots.text }}"
        satellite: "{{ trigger.device_id | default('assist') }}"
      response_variable: jarvis
    - set_conversation_response: "{{ jarvis.content.speech }}"
```

## 3. Wake-on-LAN

`power.wake` (P3 ⇒ Bestätigung) sendet ein Magic Packet nur an **konfigurierte** Ziele und gilt
erst als erfolgreich, wenn der Host per TCP antwortet (`power.host_reachable`, bis 20 s).

```bash
JARVIS_WOL_TARGETS='[{"name": "desktop", "mac": "AA:BB:CC:DD:EE:FF",
                      "host": "192.168.1.20", "port": 3389, "broadcast": "192.168.1.255"}]'
# oder: JARVIS_WOL_TARGETS=/etc/jarvis/wol.json
```

- Fast-Path ohne Modell: „wake desktop“, „weck den PC“, „PC einschalten“.
- `power.status` (P0) zeigt Erreichbarkeit; MAC-Adressen erscheinen nie in Events oder
  Ergebnissen (`power.wake.sent` trägt nur Name/Host/Port).
- Voraussetzungen am Ziel: WOL im BIOS/UEFI und im NIC-Treiber aktiv, Rechner am Kabel
  (Blueprint §30: Mainboard/NIC prüfen). Ohne Hardware bleibt der Verifier `NOT_ACHIEVED`.

## 4. Offline-Basics (Schritt 56)

Ohne Cloud-Provider (`JARVIS_PROVIDER=none` oder Ausfall) funktionieren weiterhin: Licht/Szene/
Home-State per Fast-Path, `home.state.set` mit `apply_defaults=true` (Licht-/Klima-Defaults
des States, nur für Domains der State-Policy, nie Schloss/Alarm/Garage), WOL, Kill Switch.
`GET /home` meldet `online:false`, das HUD zeigt „OFFLINE (local basics only)“, sobald HA
selbst nicht erreichbar ist; Aktionen schlagen dann sauber fehl statt zu raten.
