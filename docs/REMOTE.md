# Secure Remote: Mesh-VPN, Mobile-HUD, Push (Phase 9)

Regel (SECURITY.md §2, Regel 8): **Der Core öffnet nie einen Port ins Internet.** Unterwegs
erreichst du ihn nur über einen privaten Tunnel. Jeder Aufrufer, der nicht der lokale Owner am
Loopback ist, muss ein enrolltes Gerät mit signierten Requests sein (ADR-0004).

## 1. Tunnel: Tailscale (empfohlen) oder WireGuard

**Variante A – `tailscale serve` (HTTPS, Core bleibt auf Loopback):**

```bash
# auf dem Home-Core (Mini-PC), Core läuft wie gewohnt auf 127.0.0.1:7870
tailscale serve --bg https / http://127.0.0.1:7870
# → https://<mini-pc>.<tailnet>.ts.net/hud/  (gültiges Zertifikat, nur im Tailnet erreichbar)
```

Der Core sieht dann `127.0.0.1` als TCP-Peer, aber `tailscale serve` setzt Forwarding-Header
(`X-Forwarded-For`, `Tailscale-User-Login`). Solche Requests gelten **nicht** als lokaler Owner
(`core/devices/auth.py`), d. h. auch das HUD im Tailnet muss enrollt sein. HTTPS ist Pflicht für
die Browser-Signatur (WebCrypto gibt es nur in sicheren Kontexten: `https://` oder `localhost`).

**Variante B – direkt an die Tunnel-IP binden (WireGuard/Tailscale ohne serve):**

```bash
JARVIS_CORE_HOST=100.101.102.103 python -m core   # nur die Mesh-IP, nie 0.0.0.0
```

`JARVIS_CORE_HOST=0.0.0.0` wird vom Core verweigert. Ohne HTTPS kann das Handy-HUD nicht
signieren; dann bleibt es untrusted (Lesen/Fast-Path-P2 ok, keine Freigaben). Für Freigaben von
unterwegs Variante A nutzen.

## 2. Handy enrollen

1. Am Desktop-HUD (Loopback) **DEVICES → ENROLL** klicken oder im Terminal
   `python -m core enroll phone` – zeigt einen 8-stelligen Code (10 Minuten, einmalig).
2. Auf dem Handy `https://<mini-pc>.<tailnet>.ts.net/hud/` öffnen, **DEVICES → enroll this
   device**, Code + Namen eingeben. Der Browser erzeugt ein nicht exportierbares Ed25519-Paar
   (WebCrypto, IndexedDB); nur der Public Key geht zum Core.
3. Ab jetzt signiert das Handy jede Anfrage; `device_trusted` kommt aus der Registry. Als PWA
   „Zum Startbildschirm“ hinzufügen (`manifest.webmanifest`, standalone).

Gestohlen/verloren: am Desktop **DEVICES → revoke** – jede signierte Anfrage des Geräts endet
sofort mit 401, ein Re-Trust ist nicht möglich (neu enrollen = neue Identität).

## 3. Freigaben von unterwegs

- P3 (`ui_confirm`): Tap im Handy-HUD, wenn das Handy enrollt und trusted ist.
- P4/P5 (`passkey`/`biometric`): nur von einem signierten trusted Gerät oder lokal. Die
  Gerätesignatur ist der Stand-in für die WebAuthn-Assertion (ADR-0004).
- Sprache erfüllt nie eine Freigabe (Satellit, Handy-Mikro).

## 4. Push

```bash
JARVIS_PUSH=webhook
JARVIS_PUSH_URL=https://ntfy.sh/<dein-privates-topic>     # oder ein HA-Webhook
JARVIS_PUSH_TOKEN=<optional Bearer-Token>                # nur lokal setzen
```

Gepusht werden nur: Freigabe nötig (`permission.ask`), Kill Switch, fehlgeschlagene Mission,
widerrufenes Gerät. Jede Zustellung ist ein Event (`notify.sent|failed`, `GET /notifications`)
ohne Secrets oder Memory-Werte. `JARVIS_PUSH=fake` für Tests.

## 5. Mission-Handover Desktop ↔ Handy

Missionen leben im Core, nicht im Gerät: `GET /missions/{id}` ist auf jedem Gerät identisch,
Freigaben sind geräteübergreifend sichtbar. `POST /missions/{id}/handover {"to_device_id"}`
(Owner oder trusted Gerät) verschiebt die Zuständigkeit sichtbar (Event `mission.handover`,
Presence `active_mission` wandert zum Zielgerät); die laufende Arbeit wird nicht unterbrochen.
