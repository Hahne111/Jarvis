"""Signed release tooling (SPEC §18, Phase 12 step 78).

    python -m core.release keygen <dir>                 Ed25519 keypair (private stays OUT of git)
    python -m core.release archive --version V --out D  reproducible source archive + VERSION file
    python -m core.release sums <files...>              SHA256SUMS text
    python -m core.release sign <file>                  <file>.sig (JARVIS_RELEASE_SIGNING_KEY)
    python -m core.release verify <file> <file>.sig     against release/jarvis-release.pub

The private key lives only in the owner's GitHub Actions secret (or their keychain); the public
key is committed under ``release/``. The updater refuses anything it cannot verify.
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import subprocess
import sys
import tarfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_KEY_FILE = REPO_ROOT / "release" / "jarvis-release.pub"
ARCHIVE_DIRS = (
    "core",
    "adapters",
    "voice",
    "apps",
    "skills",
    "docs",
    "infra",
    "release",
    "requirements.txt",
    "pytest.ini",
    "ruff.toml",
    "CLAUDE.md",
    "README.md",
    "install.sh",
    "install.bat",
    "start.sh",
    "start.bat",
)


# ---------------------------------------------------------------------------- keys / signatures


def keygen() -> tuple[str, str]:
    from core.devices.auth import generate_keypair

    return generate_keypair()


def sign_bytes(data: bytes, private_key_b64: str) -> str:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv = Ed25519PrivateKey.from_private_bytes(base64.b64decode(private_key_b64))
    return base64.b64encode(priv.sign(data)).decode()


def verify_bytes(data: bytes, signature_b64: str, public_key_b64: str) -> bool:
    from core.devices.auth import verify_signature

    return verify_signature(public_key_b64.strip(), signature_b64.strip(), data)


def load_public_key(path: Path | None = None) -> str | None:
    p = path or PUBLIC_KEY_FILE
    try:
        text = p.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return text or None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sums(paths: list[Path]) -> str:
    return "".join(f"{sha256_file(p)}  {p.name}\n" for p in paths)


# ---------------------------------------------------------------------------- archive


def build_archive(version: str, out_dir: Path, *, src_root: Path = REPO_ROOT) -> Path:
    """Deterministic tar.gz ``jarvis-<version>/...`` from the tracked source dirs + VERSION."""
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"jarvis-{version}.tar.gz"
    top = f"jarvis-{version}"
    files: list[Path] = []
    for entry in ARCHIVE_DIRS:
        p = src_root / entry
        if p.is_file():
            files.append(p)
        elif p.is_dir():
            files.extend(
                q
                for q in sorted(p.rglob("*"))
                if q.is_file()
                and "__pycache__" not in q.parts
                and ".pytest_cache" not in q.parts
                and not q.name.endswith(".pyc")
                and "vendor/monaco" not in q.as_posix()
            )
    with tarfile.open(out, "w:gz", format=tarfile.PAX_FORMAT) as tar:
        info = tarfile.TarInfo(f"{top}/VERSION")
        data = (version + "\n").encode()
        info.size = len(data)
        info.mtime = 0
        info.mode = 0o644
        tar.addfile(info, io.BytesIO(data))
        for f in files:
            rel = f.relative_to(src_root).as_posix()
            ti = tar.gettarinfo(str(f), arcname=f"{top}/{rel}")
            ti.mtime = 0
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ""
            with f.open("rb") as fh:
                tar.addfile(ti, fh)
    return out


def git_describe(default: str) -> str:
    try:
        out = subprocess.run(
            ["git", "describe", "--tags", "--always"],  # noqa: S607 - git on PATH in CI/dev
            capture_output=True,
            text=True,
            check=True,
            cwd=REPO_ROOT,
        )
        return out.stdout.strip() or default
    except (OSError, subprocess.CalledProcessError):
        return default


# ---------------------------------------------------------------------------- cli


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, rest = args[0], args[1:]
    if cmd == "keygen":
        out_dir = Path(rest[0]) if rest else Path(".")
        out_dir.mkdir(parents=True, exist_ok=True)
        priv, pub = keygen()
        key_file = out_dir / "jarvis-release.key"
        key_file.write_text(priv + "\n", encoding="utf-8")
        os.chmod(key_file, 0o600)
        (out_dir / "jarvis-release.pub").write_text(pub + "\n", encoding="utf-8")
        print(f"public key  -> {out_dir / 'jarvis-release.pub'}  (commit under release/)")
        print(f"private key -> {key_file}  (NEVER commit; secret JARVIS_RELEASE_SIGNING_KEY)")
        return 0
    if cmd == "archive":
        version = _opt(rest, "--version") or git_describe("0.0.0")
        out_dir = Path(_opt(rest, "--out") or "dist")
        path = build_archive(version, out_dir)
        print(path)
        return 0
    if cmd == "sums":
        sys.stdout.write(sums([Path(p) for p in rest]))
        return 0
    if cmd == "sign":
        key = os.environ.get("JARVIS_RELEASE_SIGNING_KEY")
        if not key:
            print(
                "JARVIS_RELEASE_SIGNING_KEY is not set (private key never comes from argv)",
                file=sys.stderr,
            )
            return 2
        for p in rest:
            sig = sign_bytes(Path(p).read_bytes(), key)
            Path(p + ".sig").write_text(sig + "\n", encoding="utf-8")
            print(f"signed {p} -> {p}.sig")
        return 0
    if cmd == "verify":
        if len(rest) < 2:
            print("usage: verify <file> <sig> [--pub <pubfile>]", file=sys.stderr)
            return 2
        pub_path = _opt(rest, "--pub")
        pub = load_public_key(Path(pub_path) if pub_path else None)
        if pub is None:
            print(
                "no public key (release/jarvis-release.pub missing) - cannot verify",
                file=sys.stderr,
            )
            return 3
        ok = verify_bytes(
            Path(rest[0]).read_bytes(), Path(rest[1]).read_text(encoding="utf-8"), pub
        )
        print("signature OK" if ok else "signature INVALID")
        return 0 if ok else 1
    print(f"unknown command {cmd!r}", file=sys.stderr)
    return 2


def _opt(rest: list[str], name: str) -> str | None:
    if name in rest:
        i = rest.index(name)
        if i + 1 < len(rest):
            return rest[i + 1]
    return None


if __name__ == "__main__":
    raise SystemExit(main())
