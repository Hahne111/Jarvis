"""Device registry, enrollment and signed-request authentication (Phase 9, SPEC §10/§17)."""

from core.devices.auth import (
    HDR_DEVICE,
    HDR_NONCE,
    HDR_SIG,
    HDR_TS,
    AuthError,
    Caller,
    DeviceAuthenticator,
    generate_keypair,
    sign_headers,
    valid_public_key,
    verify_signature,
)
from core.devices.model import Device, DeviceType, fingerprint
from core.devices.registry import DeviceRegistry, Enrollment, EnrollmentError

__all__ = [
    "HDR_DEVICE",
    "HDR_NONCE",
    "HDR_SIG",
    "HDR_TS",
    "AuthError",
    "Caller",
    "Device",
    "DeviceAuthenticator",
    "DeviceRegistry",
    "DeviceType",
    "Enrollment",
    "EnrollmentError",
    "fingerprint",
    "generate_keypair",
    "sign_headers",
    "valid_public_key",
    "verify_signature",
]
