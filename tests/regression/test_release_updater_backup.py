"""Regression: signed release, updater with rollback, encrypted backup (SPEC §18, Phase 12)."""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest
from core import backup, release
from core.updater import UpdateError, Updater

PASS = "correct horse battery"  # noqa: S105 - test passphrase, not a credential


@pytest.fixture
def keys():
    priv, pub = release.keygen()
    return priv, pub


@pytest.fixture
def src(tmp_path) -> Path:
    """A tiny fake repo: enough for build_archive and for a smoke test to succeed."""
    root = tmp_path / "src"
    for pkg in ("core", "adapters", "skills"):
        (root / pkg).mkdir(parents=True)
        (root / pkg / "__init__.py").write_text('__version__ = "9.9.9"\n')
    (root / "README.md").write_text("fake\n")
    (root / "core" / "__pycache__").mkdir()
    (root / "core" / "__pycache__" / "x.pyc").write_bytes(b"\0")
    return root


def signed_archive(src, keys, version, out) -> tuple[Path, Path]:
    priv, _ = keys
    arc = release.build_archive(version, out, src_root=src)
    sig = arc.with_suffix(arc.suffix + ".sig")
    sig.write_text(release.sign_bytes(arc.read_bytes(), priv))
    return arc, sig


# ---------------------------------------------------------------- release


def test_sign_verify_and_tamper_detection(keys):
    priv, pub = keys
    sig = release.sign_bytes(b"jarvis", priv)
    assert release.verify_bytes(b"jarvis", sig, pub)
    assert not release.verify_bytes(b"jarvis!", sig, pub)
    assert not release.verify_bytes(b"jarvis", sig, release.keygen()[1])
    assert not release.verify_bytes(b"jarvis", "not base64 at all", pub)


def test_archive_is_deterministic_and_skips_caches(src, tmp_path):
    a = release.build_archive("1.2.3", tmp_path / "a", src_root=src)
    b = release.build_archive("1.2.3", tmp_path / "b", src_root=src)
    assert release.sha256_file(a) == release.sha256_file(b)
    with tarfile.open(a) as tar:
        names = tar.getnames()
    assert "jarvis-1.2.3/VERSION" in names and "jarvis-1.2.3/core/__init__.py" in names
    assert not any(".pyc" in n or "__pycache__" in n for n in names)
    assert all(n.startswith("jarvis-1.2.3/") for n in names)
    text = release.sums([a])
    assert text.startswith(release.sha256_file(a)) and text.strip().endswith(a.name)


def test_release_cli_never_takes_the_private_key_from_argv(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("JARVIS_RELEASE_SIGNING_KEY", raising=False)
    f = tmp_path / "x.bin"
    f.write_bytes(b"x")
    assert release.main(["sign", str(f)]) == 2
    assert not f.with_suffix(".bin.sig").exists()
    assert release.main(["keygen", str(tmp_path / "k")]) == 0
    key = tmp_path / "k" / "jarvis-release.key"
    assert key.exists() and (key.stat().st_mode & 0o077) == 0
    monkeypatch.setenv("JARVIS_RELEASE_SIGNING_KEY", key.read_text().strip())
    assert release.main(["sign", str(f)]) == 0
    sig = tmp_path / "x.bin.sig"
    pub = tmp_path / "k" / "jarvis-release.pub"
    assert release.main(["verify", str(f), str(sig), "--pub", str(pub)]) == 0
    f.write_bytes(b"y")
    assert release.main(["verify", str(f), str(sig), "--pub", str(pub)]) == 1
    assert release.main(["verify", str(f), str(sig), "--pub", str(tmp_path / "missing")]) == 3
    out = capsys.readouterr().out
    assert "NEVER commit" in out and key.read_text().strip() not in out


# ---------------------------------------------------------------- updater


def test_updater_apply_rollback_and_failed_smoke_leave_current_intact(src, keys, tmp_path):
    _, pub = keys
    root = tmp_path / "install"
    smoked: list[str] = []

    def smoke(d: Path) -> bool:
        smoked.append(d.name)
        return (d / "core" / "__init__.py").exists() and d.name != "1.0.2"

    up = Updater(root, public_key=pub, smoke=smoke)
    assert up.status()["current"] is None and up.current_dir() is None
    a1, s1 = signed_archive(src, keys, "1.0.0", tmp_path / "d1")
    r = up.apply(a1, s1)
    assert r["version"] == "1.0.0" and r["previous"] is None and r["sha256"]
    assert up.current()["version"] == "1.0.0" and up.current_dir() == root / "versions" / "1.0.0"
    assert (up.current_dir() / "VERSION").read_text().strip() == "1.0.0"
    with pytest.raises(UpdateError, match="already current"):
        up.apply(a1, s1)

    a2, s2 = signed_archive(src, keys, "1.0.1", tmp_path / "d2")
    assert up.apply(a2, s2)["previous"] == "1.0.0"
    assert up.status()["installed"] == ["1.0.0", "1.0.1"]

    # a version whose smoke test fails never becomes current and leaves no half-install behind
    a3, s3 = signed_archive(src, keys, "1.0.2", tmp_path / "d3")
    with pytest.raises(UpdateError, match="smoke"):
        up.apply(a3, s3)
    assert up.current()["version"] == "1.0.1" and not (root / "versions" / "1.0.2").exists()
    assert up.history()[-1]["ok"] is False and up.history()[-1]["current"] == "1.0.1"

    # rollback is offline and one step
    assert up.rollback() == {"from": "1.0.1", "to": "1.0.0"}
    assert up.current()["version"] == "1.0.0"
    with pytest.raises(UpdateError, match="no previous"):
        up.rollback()
    assert "1.0.0" in smoked and "1.0.2" in smoked


def test_updater_rejects_bad_signatures_missing_key_and_unsafe_archives(src, keys, tmp_path):
    _, pub = keys
    a, s = signed_archive(src, keys, "2.0.0", tmp_path / "d")

    no_key = Updater(tmp_path / "nk", public_key=None, smoke=lambda d: True)
    if release.load_public_key() is None:  # repo has no committed key yet
        with pytest.raises(UpdateError, match="public key"):
            no_key.apply(a, s)

    other = Updater(tmp_path / "o", public_key=release.keygen()[1], smoke=lambda d: True)
    with pytest.raises(UpdateError, match="signature"):
        other.apply(a, s)
    assert other.current()["version"] is None

    up = Updater(tmp_path / "u", public_key=pub, smoke=lambda d: True)
    tampered = tmp_path / "t.tar.gz"
    tampered.write_bytes(a.read_bytes() + b"\0")
    with pytest.raises(UpdateError, match="signature"):
        up.apply(tampered, s)
    with pytest.raises(UpdateError, match="signature file missing"):
        up.apply(a, tmp_path / "nope.sig")

    # a correctly signed archive with a traversal member is still refused
    evil = tmp_path / "evil.tar.gz"
    with tarfile.open(evil, "w:gz") as tar:
        for name, data in (("jarvis-3.0.0/VERSION", b"3.0.0\n"), ("jarvis-3.0.0/../x", b"x")):
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tar.addfile(ti, io.BytesIO(data))
    esig = tmp_path / "evil.sig"
    esig.write_text(release.sign_bytes(evil.read_bytes(), keys[0]))
    with pytest.raises(UpdateError, match=r"unsafe|outside"):
        up.apply(evil, esig)
    assert not (tmp_path / "u" / "versions" / "3.0.0").exists()
    assert up.current()["version"] is None


# ---------------------------------------------------------------- backup


@pytest.fixture
def data(tmp_path) -> Path:
    d = tmp_path / "data"
    (d / "workspaces" / "m1").mkdir(parents=True)
    (d / "core.db").write_bytes(b"sqlite-bytes" * 100)
    (d / "workspaces" / "m1" / "main.py").write_text("print(1)\n")
    (d / "workspaces" / "m1" / "__pycache__").mkdir()
    (d / "workspaces" / "m1" / "__pycache__" / "x.pyc").write_bytes(b"\0")
    (d / "audio.wav").write_bytes(b"not included")
    return d


def test_backup_roundtrip_verify_and_restore(data, tmp_path):
    out = tmp_path / "b" / "j.jbk"
    info = backup.create(data, out, PASS)
    assert info["files"] == 2 and out.exists() and not out.with_suffix(".jbk.part").exists()
    raw = out.read_bytes()
    assert raw[:4] == b"JBK1" and b"sqlite-bytes" not in raw and b"print(1)" not in raw

    v = backup.verify(out, PASS)
    assert v["ok"] and sorted(v["files"]) == ["core.db", "workspaces/m1/main.py"]
    assert v["manifest"]["core_version"] and v["manifest"]["files"] == v["files"]

    target = tmp_path / "restored"
    r = backup.restore(out, PASS, target)
    assert sorted(r["files"]) == ["core.db", "workspaces/m1/main.py"]
    assert (target / "core.db").read_bytes() == (data / "core.db").read_bytes()
    assert (target / "workspaces" / "m1" / "main.py").read_text() == "print(1)\n"
    assert not (target / "audio.wav").exists()


def test_backup_rejects_wrong_passphrase_tampering_and_short_secrets(data, tmp_path):
    out = tmp_path / "j.jbk"
    backup.create(data, out, PASS)
    with pytest.raises(backup.BackupError, match=r"passphrase|authentication"):
        backup.verify(out, "wrong passphrase")
    raw = bytearray(out.read_bytes())
    raw[-1] ^= 0x01
    (tmp_path / "bad.jbk").write_bytes(bytes(raw))
    with pytest.raises(backup.BackupError, match="authentication"):
        backup.restore(tmp_path / "bad.jbk", PASS, tmp_path / "never")
    assert not (tmp_path / "never").exists()
    with pytest.raises(backup.BackupError, match="8 characters"):
        backup.create(data, tmp_path / "x.jbk", "short")
    (tmp_path / "junk.jbk").write_bytes(b"JUNK" * 20)
    with pytest.raises(backup.BackupError, match="not a JARVIS backup"):
        backup.verify(tmp_path / "junk.jbk", PASS)
    with pytest.raises(backup.BackupError, match="not found"):
        backup.create(tmp_path / "missing", tmp_path / "x.jbk", PASS)


def test_backup_restore_refuses_unsafe_members(tmp_path):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        ti = tarfile.TarInfo("../escape.txt")
        ti.size = 1
        tar.addfile(ti, io.BytesIO(b"x"))
    salt, nonce = b"s" * 16, b"n" * 12
    header = backup.MAGIC + bytes([backup.FORMAT_VERSION]) + salt + nonce
    key = backup.derive_key(PASS, salt)
    f = tmp_path / "evil.jbk"
    f.write_bytes(header + AESGCM(key).encrypt(nonce, buf.getvalue(), header))
    with pytest.raises(backup.BackupError, match="unsafe"):
        backup.restore(f, PASS, tmp_path / "t")
    assert not (tmp_path / "escape.txt").exists()


def test_backup_cli_reads_passphrase_only_from_env(data, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("JARVIS_BACKUP_PASSPHRASE", raising=False)
    out = tmp_path / "c.jbk"
    assert backup.main(["create", str(out), "--data", str(data)]) == 1
    assert not out.exists()
    monkeypatch.setenv("JARVIS_BACKUP_PASSPHRASE", PASS)
    assert backup.main(["create", str(out), "--data", str(data)]) == 0
    assert backup.main(["verify", str(out)]) == 0
    assert backup.main(["restore", str(out), str(tmp_path / "r")]) == 0
    printed = capsys.readouterr().out
    assert json.loads(printed.splitlines()[-1]) == {"restored": 2}
    assert PASS not in printed
