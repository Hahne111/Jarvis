# HUD vendor files

Third-party front-end code is **never** loaded from a CDN at runtime. Anything the HUD needs is
fetched once, pinned by version and integrity hash, and served by the core from this folder
(`/hud/vendor/...`). The fetched files are git-ignored.

| Package | Fetch | Used by |
|---|---|---|
| monaco-editor 0.56.0 | `python apps/desktop/web/vendor/fetch_monaco.py` | Coding-mode editor (`/hud/vendor/monaco/vs`). Without it the HUD uses a plain textarea editor. |
