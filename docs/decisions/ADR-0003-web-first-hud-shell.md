# ADR-0003: Web-first HUD-Shell, vom Core ausgeliefert; Tauri-Wrapper folgt

- **Status:** Accepted
- **Datum:** 2026-09-05
- **Bezug zur PDF:** Blueprint 1.0 §3 (Visuelle Produktsprache), §4.3 (Tauri 2 + React + TypeScript), §21 Phase 6 Schritt 36 (Delta: Reihenfolge)

## Kontext
Die PDF sieht als Desktop-Shell Tauri 2 mit React/TypeScript vor. In der Entwicklungsumgebung dieser Session fehlen Node/pnpm/Rust/Tauri-Toolchain, und CI läuft ohne Display. Gleichzeitig existiert bereits ein vollständiger Event-Vertrag (`docs/HUD_EVENTS.md`) und ein Core, der alles über HTTP/WebSocket bereitstellt. Ein HUD, das nur in einer bestimmten Shell läuft, würde den Vertrag nicht testen.

## Entscheidung
1. Das HUD entsteht **web-first** als reines Browser-Frontend ohne Build-Schritt (`apps/desktop/web/`: HTML, CSS, ES-Module-JavaScript). Der Core liefert es unter `GET /hud` aus (127.0.0.1, wie alles andere).
2. Das HUD rendert ausschließlich persistierte Events und Abfragen des Core (`/ws/events?after_seq`, `/presence`, `/missions`, `/approvals`, `/memory`, `/health`) – keine eigene Zustandserfindung (SECURITY.md §3).
3. Die Tauri-2-Shell (`apps/desktop/src-tauri/`) wird als **Wrapper** um dieselben Web-Assets gebaut, sobald die Toolchain lokal verfügbar ist. React/TypeScript kann dann eingeführt werden, ohne den Event-Vertrag zu ändern.
4. Fluidity-Regeln (PERFORMANCE.md) gelten schon jetzt: Event-Batching pro Animationsframe, keine synchronen Requests im Render-Pfad, kein aggressives Polling (`/health` höchstens alle 5 s), Reconnect über den letzten `seq`.

## Konsequenzen
- Phase-6-Exit „App öffnen und verifizieren“ und „HUD bleibt flüssig während Agent arbeitet“ sind im Browser prüfbar; CI testet die Auslieferung und den Vertrag.
- Kein natives Fenster-/Tray-/Autostart-Verhalten, bis der Tauri-Wrapper existiert (Phase 6 bleibt insoweit offen).
- Designsprache (dunkles Command-Center, Amber/Cyan, zentraler Core) wird als CSS-Tokens angelegt und in der Tauri-Version wiederverwendet.

## Alternativen
- Sofort Tauri/React: verworfen, Toolchain in dieser Umgebung nicht vorhanden; hätte einen ungetesteten Blindflug bedeutet.
- Debug-Dashboard weiter ausbauen: verworfen, es bleibt bewusst ein Entwickler-Werkzeug ohne Designanspruch.
