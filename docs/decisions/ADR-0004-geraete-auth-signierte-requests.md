# ADR-0004: Geräte-Authentifizierung über Ed25519-signierte Requests; Loopback = lokaler Owner

- Status: akzeptiert
- Datum: 2026-09-06
- Bezug: Blueprint Phase 9 (Schritte 58, 61), SPEC §10, SECURITY.md §2 (Regeln 1, 8), §3

## Kontext

Bis Phase 8 konnte jeder HTTP-Client gegenüber dem Core behaupten, ein vertrauenswürdiges Gerät zu
sein (`device_trusted: true` im Request-Body). Das war tragbar, solange der Core ausschließlich
auf `127.0.0.1` lauschte. Für Mobile + Secure Remote (Phase 9) muss der Core über einen privaten
Tunnel erreichbar sein; damit braucht `device.trusted` eine kryptografische Grundlage, und starke
Freigaben (Passkey/Biometrie) müssen an ein enrolltes Gerät gebunden sein.

## Entscheidung

1. **Geräte-Registry mit Schlüsselpaaren.** Jedes Gerät (Desktop, Mobile, Satellit, HUD, Server)
   wird einmalig enrollt: der Owner erzeugt an einer bereits vertrauenswürdigen Stelle (lokales
   HUD oder signiertes vertrauenswürdiges Gerät) einen Einmal-Code (8 Hex-Zeichen, 10 Minuten,
   maximal 5 Fehlversuche). Das neue Gerät sendet Code + Ed25519-Public-Key. Der Core speichert
   nur den Public Key; der Code erscheint nie in einem Event.
2. **Signierte Requests.** Header `X-Jarvis-Device`, `X-Jarvis-Timestamp`, `X-Jarvis-Nonce`,
   `X-Jarvis-Signature` über `"{ts}\n{nonce}\n{METHOD}\n{path}\n{sha256(body)}"`; Zeitfenster
   ±120 s, Nonce nur einmal. Der Core leitet `device_trusted` **aus der Registry** ab; die
   Behauptung im Body wird ignoriert.
3. **Loopback = lokaler Owner.** Unsignierte Requests werden nur von `127.0.0.1`/`::1` als
   Owner behandelt (das HUD auf derselben Maschine gilt als entsperrtes vertrauenswürdiges
   Gerät). Unsignierte Requests von anderen Adressen sind erlaubt, aber **nie** vertrauenswürdig
   und dürfen keine Freigaben erteilen, Deny/Resume auslösen oder Geräte verwalten.
4. **Starke Proofs nur gebunden.** `passkey`/`biometric`/`hardware_key` werden nur von einem
   signierten, vertrauenswürdigen Gerät oder vom lokalen Owner akzeptiert; `ui_confirm` nur
   lokal oder von einem vertrauenswürdigen Gerät; `voice` bleibt Komfortsignal.
5. **Revoke ist final.** Ein widerrufenes Gerät wird sofort bei jedem signierten Request
   abgewiesen (401), kann nicht wieder vertraut werden und muss neu enrollt werden (neue
   Identität). Events: `device.enrollment.started|failed`, `device.enrolled`,
   `device.trust.changed`, `device.revoked`, `device.auth.failed`.

## Delta zur PDF

Die PDF nennt „Keypairs“ und „Passkey/Biometrie“. Die Gerätesignatur ist der Stand-in für eine
WebAuthn-Assertion, bis das Mobile-HUD (Tauri/PWA) Plattform-Authenticatoren anbinden kann; der
API-Vertrag (Proof-Methode, Gerätebindung, Stärke) ändert sich dadurch nicht. Der Remote-Zugang
selbst läuft über einen privaten Mesh-VPN (SECURITY.md §2 Regel 8, `docs/REMOTE.md`); der Core
öffnet keinen Port ins Internet.

## Konsequenzen

- Policy wird ausschließlich strenger (Law 9): lokale Nutzung bleibt unverändert, Remote-Nutzung
  braucht Enrollment.
- Neue Abhängigkeit `cryptography` (Ed25519; später auch AES-GCM für verschlüsselte Backups).
- Tests: `tests/core/test_devices.py` (Enrollment, Signatur, Replay, Manipulation, Trust-Bindung,
  Revoke, Remote-Freigabe mit Passkey).
