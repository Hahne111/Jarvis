"""Installer / updater with rollback (SPEC §18.2, Phase 12 step 79).

    python -m core.updater status  [--root <install_root>]
    python -m core.updater apply   <archive.tar.gz> <archive.tar.gz.sig> [--root R] [--backup-dir B]
    python -m core.updater rollback [--root R]

Layout under the install root (default ``~/.jarvis/app``):

    versions/<version>/   extracted release (jarvis-<version>/... flattened)
    current.json          {"version": "...", "previous": "...", "applied_at": "..."}
    updater.json          history of every apply / rollback / failure

``apply`` verifies the Ed25519 signature against the committed public key, checks the archive
members, extracts into a fresh version directory, runs a smoke test (import the core from that
directory), and only then switches ``current``. Any failure leaves ``current`` untouched and
removes the half-installed version - rollback is one command and never needs the network.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.release import load_public_key, sha256_file, verify_bytes

DEFAULT_ROOT = Path(os.environ.get("JARVIS_INSTALL_ROOT") or Path.home() / ".jarvis" / "app")


class UpdateError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Updater:
    def __init__(
        self,
        root: Path | str = DEFAULT_ROOT,
        *,
        public_key: str | None = None,
        smoke: Callable[[Path], bool] | None = None,
        backup: Callable[[Path], str | None] | None = None,
    ) -> None:
        self.root = Path(root)
        self.versions = self.root / "versions"
        self.versions.mkdir(parents=True, exist_ok=True)
        self._pub = public_key
        self._smoke = smoke or default_smoke
        self._backup = backup

    # -- state -------------------------------------------------------------------------------------

    def current(self) -> dict[str, Any]:
        try:
            return json.loads((self.root / "current.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {"version": None, "previous": None, "applied_at": None}

    def history(self) -> list[dict[str, Any]]:
        try:
            return json.loads((self.root / "updater.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return []

    def _record(self, entry: dict[str, Any]) -> None:
        hist = self.history()
        hist.append({"at": _now(), **entry})
        (self.root / "updater.json").write_text(json.dumps(hist[-200:], indent=2), encoding="utf-8")

    def _set_current(self, version: str | None, previous: str | None) -> None:
        (self.root / "current.json").write_text(
            json.dumps({"version": version, "previous": previous, "applied_at": _now()}, indent=2),
            encoding="utf-8",
        )

    def status(self) -> dict[str, Any]:
        cur = self.current()
        return {
            "root": str(self.root),
            "current": cur["version"],
            "previous": cur["previous"],
            "installed": sorted(p.name for p in self.versions.iterdir() if p.is_dir()),
            "public_key": bool(self._pub or load_public_key()),
            "history": self.history()[-5:],
        }

    def current_dir(self) -> Path | None:
        v = self.current()["version"]
        return self.versions / v if v else None

    # -- apply / rollback --------------------------------------------------------------------------

    def apply(self, archive: Path | str, signature: Path | str) -> dict[str, Any]:
        archive, signature = Path(archive), Path(signature)
        pub = self._pub or load_public_key()
        if not pub:
            raise UpdateError("no release public key available - refusing to install unsigned code")
        data = archive.read_bytes()
        try:
            sig = signature.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise UpdateError("signature file missing") from None
        if not verify_bytes(data, sig, pub):
            self._record(
                {
                    "action": "apply",
                    "archive": archive.name,
                    "ok": False,
                    "error": "invalid signature",
                }
            )
            raise UpdateError("signature verification failed - archive rejected")
        digest = sha256_file(archive)
        version, top = _inspect(archive)
        cur = self.current()
        if cur["version"] == version:
            raise UpdateError(f"version {version} is already current")
        dest = self.versions / version
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        backup_ref = self._backup(self.root) if self._backup else None
        try:
            with tarfile.open(archive, "r:gz") as tar:
                for m in tar.getmembers():
                    _check_member(m, top)
                for m in tar.getmembers():
                    if not m.isfile():
                        continue
                    rel = Path(m.name).relative_to(top)
                    out = dest / rel
                    out.parent.mkdir(parents=True, exist_ok=True)
                    src = tar.extractfile(m)
                    if src is not None:
                        out.write_bytes(src.read())
            if not self._smoke(dest):
                raise UpdateError("smoke test failed for the new version")
        except Exception as exc:
            shutil.rmtree(dest, ignore_errors=True)
            self._record(
                {
                    "action": "apply",
                    "version": version,
                    "ok": False,
                    "error": str(exc)[:200],
                    "current": cur["version"],
                }
            )
            if isinstance(exc, UpdateError):
                raise
            raise UpdateError(f"install failed: {exc}") from exc
        self._set_current(version, cur["version"])
        self._record(
            {
                "action": "apply",
                "version": version,
                "ok": True,
                "sha256": digest,
                "previous": cur["version"],
                "backup": backup_ref,
            }
        )
        return {
            "version": version,
            "previous": cur["version"],
            "sha256": digest,
            "path": str(dest),
            "backup": backup_ref,
        }

    def rollback(self) -> dict[str, Any]:
        cur = self.current()
        prev = cur["previous"]
        if not prev or not (self.versions / prev).is_dir():
            raise UpdateError("no previous version to roll back to")
        if not self._smoke(self.versions / prev):
            raise UpdateError(f"previous version {prev} fails its smoke test - not switching")
        self._set_current(prev, None)
        self._record({"action": "rollback", "from": cur["version"], "to": prev, "ok": True})
        return {"from": cur["version"], "to": prev}


def _inspect(archive: Path) -> tuple[str, str]:
    with tarfile.open(archive, "r:gz") as tar:
        names = tar.getnames()
        if not names:
            raise UpdateError("empty archive")
        top = names[0].split("/")[0]
        if not top.startswith("jarvis-"):
            raise UpdateError("archive does not look like a JARVIS release (jarvis-<version>/)")
        try:
            version = tar.extractfile(f"{top}/VERSION").read().decode().strip()  # type: ignore[union-attr]
        except (KeyError, AttributeError):
            raise UpdateError("archive has no VERSION file") from None
    if not version or "/" in version or ".." in version:
        raise UpdateError("invalid VERSION in archive")
    return version, top


def _check_member(m: tarfile.TarInfo, top: str) -> None:
    if not m.name.startswith(top + "/") and m.name != top:
        raise UpdateError(f"unexpected member outside {top}/: {m.name}")
    parts = Path(m.name).parts
    if ".." in parts or m.name.startswith("/") or m.issym() or m.islnk() or m.isdev():
        raise UpdateError(f"refusing unsafe member {m.name!r}")


def default_smoke(version_dir: Path) -> bool:
    """The new tree must import its core and report a version - from its own directory."""
    try:
        out = subprocess.run(
            [sys.executable, "-c", "import core, adapters, skills; print(core.__version__)"],
            cwd=str(version_dir),
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "PYTHONPATH": str(version_dir)},
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return out.returncode == 0 and bool(out.stdout.strip())


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    root = Path(args[args.index("--root") + 1]) if "--root" in args else DEFAULT_ROOT
    up = Updater(root)
    cmd = args[0]
    try:
        if cmd == "status":
            print(json.dumps(up.status(), indent=2))
            return 0
        if cmd == "apply":
            print(json.dumps(up.apply(args[1], args[2]), indent=2))
            return 0
        if cmd == "rollback":
            print(json.dumps(up.rollback(), indent=2))
            return 0
    except (UpdateError, IndexError) as exc:
        print(f"updater error: {exc}", file=sys.stderr)
        return 1
    print(f"unknown command {cmd!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
