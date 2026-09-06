"""Device model (SPEC §17.1 ``Device``, §10 Device Mesh, Phase 9 step 58).

A device is anything that talks to the Core from outside the process: the desktop HUD, a phone,
a voice satellite, a server. Every enrolled device owns an Ed25519 keypair; the Core stores only
the *public* key. ``trusted`` is what ``device.trusted`` requirements and approval proofs refer
to; a revoked device is never trusted again (a new enrollment creates a new identity).
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class DeviceType(StrEnum):
    DESKTOP = "desktop"
    MOBILE = "mobile"
    SATELLITE = "satellite"
    HUD = "hud"
    SERVER = "server"


def fingerprint(public_key_b64: str) -> str:
    """Short, stable identifier of a public key (what the HUD shows; never the key itself)."""
    raw = base64.b64decode(public_key_b64)
    return hashlib.sha256(raw).hexdigest()[:16]


@dataclass
class Device:
    name: str
    type: DeviceType
    public_key: str  # base64 of the raw 32-byte Ed25519 public key
    trusted: bool = True
    device_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    enrolled_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_seen: datetime | None = None
    revoked_at: datetime | None = None
    revoked_reason: str | None = None

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None

    @property
    def active_trust(self) -> bool:
        return self.trusted and not self.revoked

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.public_key)

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "name": self.name,
            "type": self.type.value,
            "fingerprint": self.fingerprint,
            "trusted": self.active_trust,
            "enrolled_at": self.enrolled_at.isoformat(),
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
            "revoked_reason": self.revoked_reason,
        }

    def to_row(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "name": self.name,
            "type": self.type.value,
            "public_key": self.public_key,
            "trusted": 1 if self.trusted else 0,
            "enrolled_at": self.enrolled_at.isoformat(),
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
            "revoked_reason": self.revoked_reason,
        }

    @classmethod
    def from_row(cls, row: Any) -> Device:
        return cls(
            device_id=row.device_id,
            name=row.name,
            type=DeviceType(row.type),
            public_key=row.public_key,
            trusted=bool(row.trusted),
            enrolled_at=datetime.fromisoformat(row.enrolled_at),
            last_seen=datetime.fromisoformat(row.last_seen) if row.last_seen else None,
            revoked_at=datetime.fromisoformat(row.revoked_at) if row.revoked_at else None,
            revoked_reason=row.revoked_reason,
        )
