"""Signed requests + caller resolution (Phase 9 steps 58/61; SECURITY.md §2 rules 1 and 8).

Every remote request from an enrolled device is signed with its Ed25519 key over

    "{timestamp}\n{nonce}\n{METHOD}\n{path}\n{sha256(body)}"

and carries ``X-Jarvis-Device``, ``X-Jarvis-Timestamp``, ``X-Jarvis-Nonce``,
``X-Jarvis-Signature``. The Core accepts a signature only inside a ±120 s window and never twice
(nonce cache), and it derives ``device_trusted`` from the registry - a client can no longer
*claim* to be trusted. Unsigned requests are honoured only from the loopback interface (the
local owner in front of the machine, ADR-0004); from anywhere else they are untrusted.

Strong proofs (passkey/biometric/hardware key) are accepted only from a signed *trusted*
device or from loopback; WebAuthn assertions can later replace the device signature without
changing this contract.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass
from typing import Any

from core.devices.model import Device
from core.devices.registry import DeviceRegistry

HDR_DEVICE = "x-jarvis-device"
HDR_TS = "x-jarvis-timestamp"
HDR_NONCE = "x-jarvis-nonce"
HDR_SIG = "x-jarvis-signature"
MAX_SKEW_S = 120
LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})


class AuthError(Exception):
    def __init__(self, reason: str, *, device_id: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.device_id = device_id


def canonical(ts: str, nonce: str, method: str, path: str, body: bytes) -> bytes:
    digest = hashlib.sha256(body or b"").hexdigest()
    return f"{ts}\n{nonce}\n{method.upper()}\n{path}\n{digest}".encode()


# ---------------------------------------------------------------- keys (client side helpers)


def generate_keypair() -> tuple[str, str]:
    """(private_key_b64, public_key_b64) - raw 32-byte Ed25519 keys, base64."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv = Ed25519PrivateKey.generate()
    raw_priv = priv.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    raw_pub = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return base64.b64encode(raw_priv).decode(), base64.b64encode(raw_pub).decode()


def sign_headers(
    device_id: str,
    private_key_b64: str,
    method: str,
    path: str,
    body: bytes = b"",
    *,
    ts: float | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    """Headers a device client attaches to a request (used by tests and the mobile client)."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv = Ed25519PrivateKey.from_private_bytes(base64.b64decode(private_key_b64))
    ts_s = str(int(ts if ts is not None else time.time()))
    nonce = nonce or secrets.token_urlsafe(12)
    sig = priv.sign(canonical(ts_s, nonce, method, path, body))
    return {
        HDR_DEVICE: device_id,
        HDR_TS: ts_s,
        HDR_NONCE: nonce,
        HDR_SIG: base64.b64encode(sig).decode(),
    }


def verify_signature(public_key_b64: str, signature_b64: str, message: bytes) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
        pub.verify(base64.b64decode(signature_b64), message)
        return True
    except (InvalidSignature, ValueError):
        return False


def valid_public_key(public_key_b64: str) -> bool:
    try:
        raw = base64.b64decode(public_key_b64, validate=True)
    except Exception:
        return False
    if len(raw) != 32:
        return False
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        Ed25519PublicKey.from_public_bytes(raw)
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------- caller resolution


@dataclass(frozen=True)
class Caller:
    """Who is talking to the API and what the Core can *prove* about them."""

    local: bool
    device: Device | None = None
    client_host: str | None = None

    @property
    def signed(self) -> bool:
        return self.device is not None

    @property
    def trusted(self) -> bool:
        return self.device.active_trust if self.device else False

    def device_id(self, claimed: str | None) -> str | None:
        return self.device.device_id if self.device else claimed

    def effective_trust(self, claimed: bool) -> bool:
        """Signed -> registry decides; loopback -> the local owner's claim; remote -> never."""
        if self.device is not None:
            return self.device.active_trust
        return bool(claimed) if self.local else False

    @property
    def may_prove_strongly(self) -> bool:
        return self.local or (self.signed and self.trusted)

    def to_dict(self) -> dict[str, Any]:
        return {
            "local": self.local,
            "signed": self.signed,
            "trusted": self.trusted,
            "device_id": self.device.device_id if self.device else None,
        }


class DeviceAuthenticator:
    def __init__(self, registry: DeviceRegistry, *, max_skew_s: int = MAX_SKEW_S) -> None:
        self._registry = registry
        self._skew = max_skew_s
        self._nonces: dict[str, float] = {}

    def resolve(
        self, headers: dict[str, str], method: str, path: str, body: bytes, client_host: str | None
    ) -> Caller:
        h = {k.lower(): v for k, v in headers.items()}
        local = (client_host or "") in LOCAL_HOSTS
        device_id = h.get(HDR_DEVICE)
        if not device_id:
            return Caller(local=local, client_host=client_host)
        ts, nonce, sig = h.get(HDR_TS), h.get(HDR_NONCE), h.get(HDR_SIG)
        if not (ts and nonce and sig):
            raise AuthError("missing signature headers", device_id=device_id)
        device = self._registry.get(device_id)
        if device is None:
            raise AuthError("unknown device", device_id=device_id)
        if device.revoked:
            raise AuthError("device revoked", device_id=device_id)
        try:
            ts_i = int(ts)
        except ValueError:
            raise AuthError("bad timestamp", device_id=device_id) from None
        now = time.time()
        if abs(now - ts_i) > self._skew:
            raise AuthError("timestamp outside the allowed window", device_id=device_id)
        self._prune(now)
        key = f"{device_id}:{nonce}"
        if key in self._nonces:
            raise AuthError("replayed nonce", device_id=device_id)
        if not verify_signature(device.public_key, sig, canonical(ts, nonce, method, path, body)):
            raise AuthError("invalid signature", device_id=device_id)
        self._nonces[key] = now + self._skew * 2
        self._registry.touch(device_id)
        return Caller(local=local, device=device, client_host=client_host)

    def _prune(self, now: float) -> None:
        if len(self._nonces) > 10_000 or secrets.randbelow(64) == 0:
            for k in [k for k, exp in self._nonces.items() if exp < now]:
                self._nonces.pop(k, None)
