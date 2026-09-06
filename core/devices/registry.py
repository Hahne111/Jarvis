"""Device registry: durable device records + single-use enrollment codes (Phase 9 step 58).

Both tables share the Core engine (like missions/memory), so a code minted from the terminal
(``python -m core enroll``) works against the running Core. Codes are short-lived (10 min),
single-use, closed after 5 wrong attempts, and the owner mints them from an already trusted
place (loopback HUD, CLI on the machine, or a signed trusted device). The code itself never
appears in an event.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Column, Integer, MetaData, String, Table, Text, delete, select, update
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

enrollments_table = Table(
    "device_enrollments",
    metadata,
    Column("enrollment_id", String(36), primary_key=True),
    Column("code", String(16), nullable=False),
    Column("name_hint", String(120), nullable=False),
    Column("type", String(16), nullable=False),
    Column("trusted", Integer, nullable=False),
    Column("created_by", String(120), nullable=False),
    Column("expires_at", String(40), nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("used", Integer, nullable=False),
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

    def to_row(self) -> dict[str, Any]:
        return {
            "enrollment_id": self.enrollment_id,
            "code": self.code,
            "name_hint": self.name_hint,
            "type": self.type.value,
            "trusted": 1 if self.trusted else 0,
            "created_by": self.created_by,
            "expires_at": self.expires_at.isoformat(),
            "attempts": self.attempts,
            "used": 1 if self.used else 0,
        }

    @classmethod
    def from_row(cls, row: Any) -> Enrollment:
        return cls(
            enrollment_id=row.enrollment_id,
            code=row.code,
            name_hint=row.name_hint,
            type=DeviceType(row.type),
            trusted=bool(row.trusted),
            created_by=row.created_by,
            expires_at=datetime.fromisoformat(row.expires_at),
            attempts=int(row.attempts),
            used=bool(row.used),
        )


class DeviceRegistry:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        metadata.create_all(engine)

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

    # -- enrollment (durable, shared with the CLI) --------------------------------------------

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
        with self._engine.begin() as conn:
            conn.execute(enrollments_table.insert().values(**e.to_row()))
        return e

    def pending_enrollments(self) -> list[Enrollment]:
        self._prune()
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(enrollments_table).where(enrollments_table.c.used == 0)
            ).all()
        return [Enrollment.from_row(r) for r in rows]

    def complete_enrollment(self, code: str, *, name: str, public_key: str) -> Device:
        self._prune()
        code = (code or "").strip().upper()
        open_ = self.pending_enrollments()
        for e in open_:
            if secrets.compare_digest(e.code, code):
                if self.by_public_key(public_key) is not None:
                    raise EnrollmentError("this key is already enrolled")
                self._mark(e.enrollment_id, used=True)
                device = Device(
                    name=name or e.name_hint or e.type.value,
                    type=e.type,
                    public_key=public_key,
                    trusted=e.trusted,
                )
                return self.add(device)
        # wrong code: count against every open enrollment (brute force protection)
        for e in open_:
            attempts = e.attempts + 1
            self._mark(e.enrollment_id, attempts=attempts, used=attempts >= ENROLLMENT_MAX_ATTEMPTS)
        if not open_:
            raise EnrollmentError("no enrollment is open")
        raise EnrollmentError("invalid or expired enrollment code")

    def _mark(
        self, enrollment_id: str, *, used: bool | None = None, attempts: int | None = None
    ) -> None:
        values: dict[str, Any] = {}
        if used is not None:
            values["used"] = 1 if used else 0
        if attempts is not None:
            values["attempts"] = attempts
        with self._engine.begin() as conn:
            conn.execute(
                update(enrollments_table)
                .where(enrollments_table.c.enrollment_id == enrollment_id)
                .values(**values)
            )

    def _prune(self) -> None:
        now = datetime.now(UTC).isoformat()
        with self._engine.begin() as conn:
            conn.execute(
                delete(enrollments_table).where(
                    (enrollments_table.c.used == 1) | (enrollments_table.c.expires_at <= now)
                )
            )
