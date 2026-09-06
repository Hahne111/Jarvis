"""Run the JARVIS Core process:  python -m core  (binds 127.0.0.1:7870 by default).

    python -m core                 start the API/HUD (JARVIS_CORE_HOST / JARVIS_CORE_PORT)
    python -m core enroll [name]   mint a one-time device enrollment code from the terminal
                                   (bootstraps a machine whose HUD is not on loopback)

Remote access never means a public port: bind to a mesh-VPN address or keep loopback behind
``tailscale serve`` (docs/REMOTE.md). Callers that are not the loopback owner must be enrolled
devices with signed requests (ADR-0004).
"""

from __future__ import annotations

import os
import sys

from core.devices.model import DeviceType

LOOPBACK = ("127.0.0.1", "::1", "localhost")


def _enroll(argv: list[str]) -> int:
    from core.runtime import CoreRuntime

    runtime = CoreRuntime.build(provider="none")
    name = argv[0] if argv else "device"
    kind = DeviceType(argv[1]) if len(argv) > 1 else DeviceType.MOBILE
    e = runtime.devices.start_enrollment(name_hint=name, type=kind, created_by="cli")
    print(f"enrollment code: {e.code}   (valid until {e.expires_at.isoformat()}, single use)")
    print("on the new device: HUD -> DEVICES -> 'enroll this device', or POST /devices/enroll.")
    # The code is stored in the Core database, so a running Core on the same JARVIS_CORE_DB_URL
    # accepts it immediately (headless first-time setups, HUD behind a tunnel).
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "enroll":
        return _enroll(argv[1:])

    import uvicorn

    from core.api.app import create_app
    from core.runtime import CoreRuntime

    runtime = CoreRuntime.build()
    runtime.recover()
    app = create_app(runtime)
    host = os.environ.get("JARVIS_CORE_HOST", "127.0.0.1")
    if host not in LOOPBACK and runtime.devices.count() == 0:
        print(
            f"WARNING: binding to {host} with no enrolled devices - remote callers are untrusted "
            "until you enroll them (HUD -> DEVICES -> ENROLL, see docs/REMOTE.md).",
            file=sys.stderr,
        )
    if host in ("0.0.0.0", "::"):  # noqa: S104 - we refuse it, not bind to it
        print(
            "ERROR: JARVIS_CORE_HOST=0.0.0.0 would expose the Core to every network "
            "(SECURITY.md §2 rule 8). Bind to a mesh-VPN address or use tailscale serve.",
            file=sys.stderr,
        )
        return 2
    uvicorn.run(
        app,
        host=host,
        port=int(os.environ.get("JARVIS_CORE_PORT", "7870")),
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
