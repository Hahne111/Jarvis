# release/

Hier liegt ausschließlich der **öffentliche** Release-Schlüssel `jarvis-release.pub` (Ed25519,
base64). Der Updater (`python -m core.updater apply`) installiert nur Archive, deren Signatur
gegen diesen Schlüssel verifiziert – fehlt er, wird jede Installation abgelehnt.

Erzeugen (Owner, einmalig): `python -m core.release keygen <dir>` – siehe `docs/RELEASE.md`.
Der private Schlüssel gehört nie ins Repo, nur als GitHub-Secret `JARVIS_RELEASE_SIGNING_KEY`.
