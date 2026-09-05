"""Fetch the Monaco editor into apps/desktop/web/vendor/monaco/ (Phase 7 step 43).

The HUD never loads code from a CDN (SECURITY.md: local-first, no third-party scripts at
runtime). Monaco is pulled once from the npm registry with a pinned version *and* pinned
integrity hash, extracted to ``vendor/monaco/vs`` (git-ignored) and served by the core under
``/hud/vendor/monaco/vs``. Without it the HUD falls back to a plain textarea editor.

    python apps/desktop/web/vendor/fetch_monaco.py
"""

from __future__ import annotations

import base64
import hashlib
import io
import shutil
import sys
import tarfile
import urllib.request
from pathlib import Path

VERSION = "0.56.0"
URL = f"https://registry.npmjs.org/monaco-editor/-/monaco-editor-{VERSION}.tgz"
SHA512_B64 = (
    "sXboRm3BeBeLm938eaiyLMe0OxzfXIlZvbv4ir/jVgQy1zDhWjgmny0WoN45fuDKhCCQsYMbBJrv/A6jd8aCUg=="
)
PREFIX = "package/min/vs/"
DEST = Path(__file__).resolve().parent / "monaco"


def main() -> int:
    print(f"downloading monaco-editor {VERSION} …")
    with urllib.request.urlopen(URL, timeout=120) as r:  # noqa: S310 - pinned https URL
        blob = r.read()
    digest = base64.b64encode(hashlib.sha512(blob).digest()).decode()
    if digest != SHA512_B64:
        print("integrity mismatch - refusing to install", file=sys.stderr)
        return 2
    if DEST.exists():
        shutil.rmtree(DEST)
    count = 0
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile() or not member.name.startswith(PREFIX):
                continue
            rel = Path(member.name[len("package/min/") :])
            if ".." in rel.parts:
                continue
            target = DEST / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(member)
            if src is None:
                continue
            target.write_bytes(src.read())
            count += 1
    (DEST / "VERSION").write_text(VERSION + "\n")
    print(f"installed {count} files to {DEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
