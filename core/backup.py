"""Encrypted backup / restore (SPEC §18, Phase 12 step 80).

    JARVIS_BACKUP_PASSPHRASE=... python -m core.backup create  <out.jbk> [--data jarvis/data]
    JARVIS_BACKUP_PASSPHRASE=... python -m core.backup verify  <file.jbk>
    JARVIS_BACKUP_PASSPHRASE=... python -m core.backup restore <file.jbk> <target_dir>

Format ``JBK1``: magic(4) | version(1) | salt(16) | nonce(12) | AES-256-GCM(tar.gz) with the
header as additional authenticated data. The key is derived with scrypt (n=2^15, r=8, p=1) from
the passphrase, which is read only from the environment - never from argv or a file in the
repo. A wrong passphrase or a flipped byte fails authentication; nothing partial is written.
Restores never follow absolute paths or ``..`` members.
"""

from __future__ import annotations

import io
import json
import os
import secrets
import sys
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MAGIC = b"JBK1"
FORMAT_VERSION = 1
HEADER_LEN = 4 + 1 + 16 + 12
DEFAULT_INCLUDE = ("core.db", "workspaces", "skills", "settings", "memory", "backups.json")
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**15, 8, 1


class BackupError(RuntimeError):
    pass


def derive_key(passphrase: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    if not passphrase or len(passphrase) < 8:
        raise BackupError("passphrase must have at least 8 characters")
    return Scrypt(salt=salt, length=32, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P).derive(
        passphrase.encode()
    )


def _tar_bytes(data_dir: Path, include: tuple[str, ...]) -> tuple[bytes, list[str]]:
    buf = io.BytesIO()
    files: list[str] = []
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in include:
            p = data_dir / name
            if not p.exists():
                continue
            for q in [p] if p.is_file() else sorted(x for x in p.rglob("*") if x.is_file()):
                if q.is_symlink() or "__pycache__" in q.parts or ".pytest_cache" in q.parts:
                    continue
                rel = q.relative_to(data_dir).as_posix()
                tar.add(str(q), arcname=rel, recursive=False)
                files.append(rel)
        manifest = json.dumps(
            {
                "created_at": datetime.now(UTC).isoformat(),
                "files": files,
                "format": FORMAT_VERSION,
                "core_version": _core_version(),
            },
            indent=2,
        ).encode()
        info = tarfile.TarInfo("BACKUP_MANIFEST.json")
        info.size = len(manifest)
        tar.addfile(info, io.BytesIO(manifest))
    return buf.getvalue(), files


def _core_version() -> str:
    try:
        from core import __version__

        return __version__
    except Exception:
        return "unknown"


def create(
    data_dir: Path | str,
    out_file: Path | str,
    passphrase: str,
    *,
    include: tuple[str, ...] = DEFAULT_INCLUDE,
) -> dict[str, Any]:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    data_dir = Path(data_dir)
    if not data_dir.is_dir():
        raise BackupError(f"data dir not found: {data_dir}")
    plain, files = _tar_bytes(data_dir, include)
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
    header = MAGIC + bytes([FORMAT_VERSION]) + salt + nonce
    key = derive_key(passphrase, salt)
    blob = AESGCM(key).encrypt(nonce, plain, header)
    out = Path(out_file)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".part")
    tmp.write_bytes(header + blob)
    os.replace(tmp, out)
    return {
        "file": str(out),
        "files": len(files),
        "bytes": out.stat().st_size,
        "plain_bytes": len(plain),
    }


def _open(file: Path | str, passphrase: str) -> bytes:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    raw = Path(file).read_bytes()
    if len(raw) < HEADER_LEN + 16 or raw[:4] != MAGIC:
        raise BackupError("not a JARVIS backup file")
    if raw[4] != FORMAT_VERSION:
        raise BackupError(f"unsupported backup format {raw[4]}")
    header, blob = raw[:HEADER_LEN], raw[HEADER_LEN:]
    salt, nonce = header[5:21], header[21:33]
    key = derive_key(passphrase, salt)
    try:
        return AESGCM(key).decrypt(nonce, blob, header)
    except InvalidTag:
        raise BackupError("wrong passphrase or corrupted backup (authentication failed)") from None


def verify(file: Path | str, passphrase: str) -> dict[str, Any]:
    plain = _open(file, passphrase)
    with tarfile.open(fileobj=io.BytesIO(plain), mode="r:gz") as tar:
        names = tar.getnames()
        m = tar.extractfile("BACKUP_MANIFEST.json")
        manifest = json.loads(m.read()) if m else {}
    return {
        "ok": True,
        "files": [n for n in names if n != "BACKUP_MANIFEST.json"],
        "manifest": manifest,
    }


def restore(file: Path | str, passphrase: str, target_dir: Path | str) -> dict[str, Any]:
    plain = _open(file, passphrase)
    target = Path(target_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)
    restored: list[str] = []
    with tarfile.open(fileobj=io.BytesIO(plain), mode="r:gz") as tar:
        for member in tar.getmembers():
            name = member.name
            if name.startswith("/") or ".." in Path(name).parts or member.issym() or member.islnk():
                raise BackupError(f"refusing unsafe member {name!r}")
            if not member.isfile() and not member.isdir():
                continue
            dest = (target / name).resolve()
            if target not in dest.parents and dest != target:
                raise BackupError(f"refusing member outside target: {name!r}")
        for member in tar.getmembers():
            if member.name == "BACKUP_MANIFEST.json":
                continue
            if member.isdir():
                (target / member.name).mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                dest = target / member.name
                dest.parent.mkdir(parents=True, exist_ok=True)
                src = tar.extractfile(member)
                if src is not None:
                    dest.write_bytes(src.read())
                    restored.append(member.name)
    return {"target": str(target), "files": restored}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    passphrase = os.environ.get("JARVIS_BACKUP_PASSPHRASE", "")
    cmd, rest = args[0], args[1:]
    try:
        if cmd == "create":
            data = Path(rest[rest.index("--data") + 1]) if "--data" in rest else Path("jarvis/data")
            out = (
                rest[0]
                if rest and not rest[0].startswith("--")
                else f"jarvis-backup-{datetime.now(UTC):%Y%m%d-%H%M%S}.jbk"
            )
            info = create(data, out, passphrase)
            print(json.dumps(info))
            return 0
        if cmd == "verify":
            print(json.dumps(verify(rest[0], passphrase)["manifest"]))
            return 0
        if cmd == "restore":
            print(json.dumps({"restored": len(restore(rest[0], passphrase, rest[1])["files"])}))
            return 0
    except (BackupError, IndexError) as exc:
        print(f"backup error: {exc}", file=sys.stderr)
        return 1
    print(f"unknown command {cmd!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
