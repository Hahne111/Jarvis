"""Device registry: durable device records + single-use enrollment codes (Phase 9 step 58).

The table shares the Core engine (like missions/memory). Enrollment codes live in memory: they
are short-lived (10 min), single-use, and the owner mints them from an already trusted place
(the local HUD or a signed trusted device). The code itself never appears in an event.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Column, Integer, MetaData, String, Table, Text, select, update
from sqlalchemy.engine import Engine

from core.devices.model import Device, DeviceType

metadata = MetaData()

devices_table = Table(
    "devices",
    metadata,
    Column("device_id", String(36), primary_key=True),
    Column("name", String(120), nullable=False),
    Column("type", String(16), nullable=False),
    Column("public_key", Text, nullable=False),
    Column("trusted", Integer, nullable=False),
    Column("enrolled_at", String(40), nullable=False),
    Column("last_seen", String(40), nullable=True),
    Column("revoked_at", String(40), nullable=True),
    Column("revoked_reason", Text, nullable=True),
)

ENROLLMENT_TTL_S = 600
ENROLLMENT_MAX_ATTEMPTS = 5


class EnrollmentError(ValueError):
    pass


@dataclass
class Enrollment:
    enrollment_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    code: str = field(default_factory=lambda: secrets.token_hex(4).upper())  # 8 hex chars
    name_hint: str = ""
    type: DeviceType = DeviceType.MOBILE
    trusted: bool = True
    created_by: str = "local"
    expires_at: datetime = field(
        default_factory=lambda: datetime.now(UTC) + timedelta(seconds=ENROLLMENT_TTL_S)
    )
    attempts: int = 0
    used: bool = False

    def to_dict(self, *, with_code: bool) -> dict[str, Any]:
        d = {
            "enrollment_id": self.enrollment_id,
            "name_hint": self.name_hint,
            "type": self.type.value,
            "trusted": self.trusted,
            "expires_at": self.expires_at.isoformat(),
        }
        if with_code:
            d["code"] = self.code
        return d


class DeviceRegistry:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        metadata.create_all(engine)
        self._enrollments: dict[str, Enrollment] = {}

    # -- devices -------------------------------------------------------------------------------

    def add(self, device: Device) -> Device:
        with self._engine.begin() as conn:
            conn.execute(devices_table.insert().values(**device.to_row()))
        return device

    def get(self, device_id: str) -> Device | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(devices_table).where(devices_table.c.device_id == device_id)
            ).first()
        return Device.from_row(row) if row else None

    def by_public_key(self, public_key: str) -> Device | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(devices_table).where(devices_table.c.public_key == public_key)
            ).first()
        return Device.from_row(row) if row else None

    def list(self, *, include_revoked: bool = True) -> list[Device]:
        with self._engine.connect() as conn:
            rows = conn.execute(select(devices_table).order_by(devices_table.c.enrolled_at)).all()
        devices = [Device.from_row(r) for r in rows]
        return devices if include_revoked else [d for d in devices if not d.revoked]

    def count(self) -> int:
        return len(self.list(include_revoked=False))

    def touch(self, device_id: str, when: datetime | None = None) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                update(devices_table)
                .where(devices_table.c.device_id == device_id)
                .values(last_seen=(when or datetime.now(UTC)).isoformat())
            )

    def set_trusted(self, device_id: str, trusted: bool) -> Device:
        with self._engine.begin() as conn:
            conn.execute(
                update(devices_table)
                .where(devices_table.c.device_id == device_id)
                .values(trusted=1 if trusted else 0)
            )
        d = self.get(device_id)
        if d is None:
            raise KeyError(device_id)
        return d

    def revoke(self, device_id: str, reason: str | None = None) -> Device:
        d = self.get(device_id)
        if d is None:
            raise KeyError(device_id)
        if not d.revoked:
            with self._engine.begin() as conn:
                conn.execute(
                    update(devices_table)
                    .where(devices_table.c.device_id == device_id)
                    .values(
                        revoked_at=datetime.now(UTC).isoformat(),
                        revoked_reason=reason,
                        trusted=0,
                    )
                )
        return self.get(device_id)  # type: ignore[return-value]

    # -- enrollment ----------------------------------------------------------------------------

    def start_enrollment(
        self,
        *,
        name_hint: str = "",
        type: DeviceType = DeviceType.MOBILE,
        trusted: bool = True,
        created_by: str = "local",
    ) -> Enrollment:
        self._prune()
        e = Enrollment(name_hint=name_hint, type=type, trusted=trusted, created_by=created_by)
        self._enrollments[e.enrollment_id] = e
        return e

    def pending_enrollments(self) -> list[Enrollment]:
        self._prune()
        return [e for e in self._enrollments.values() if not e.used]

    def complete_enrollment(self, code: str, *, name: str, public_key: str) -> Device:
        self._prune()
        now = datetime.now(UTC)
        code = (code or "").strip().upper()
        match = next((e for e in self._enrollments.values() if not e.used), None)
        for e in self._enrollments.values():
            if e.used or e.expires_at <= now:
                continue
            if secrets.compare_digest(e.code, code):
                if self.by_public_key(public_key) is not None:
                    raise EnrollmentError("this key is already enrolled")
                e.used = True
                device = Device(
                    name=name or e.name_hint or e.type.value,
                    type=e.type,
                    public_key=public_key,
                    trusted=e.trusted,
                )
                return self.add(device)
        # wrong code: count against every open enrollment (brute force protection)
        for e in self._enrollments.values():
            if not e.used:
                e.attempts += 1
                if e.attempts >= ENROLLMENT_MAX_ATTEMPTS:
                    e.used = True
        if match is None:
            raise EnrollmentError("no enrollment is open")
        raise EnrollmentError("invalid or expired enrollment code")

    def _prune(self) -> None:
        now = datetime.now(UTC)
        for k in [k for k, e in self._enrollments.items() if e.used or e.expires_at <= now]:
            self._enrollments.pop(k, None)
