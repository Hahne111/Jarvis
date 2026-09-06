"""Wake-on-LAN service (SPEC §10.1 "Mobile Wake -> Home Core -> WOL -> Desktop", Phase 8 step 54).

``power.wake`` sends a magic packet to a *configured* target only (no free-form MACs from a
model); it is P3 so the owner confirms, and the verifier ``power.host_reachable`` waits for the
host to answer on a TCP port - "online" means a real handshake, not "packet sent" (blueprint
§10.4). Targets come from ``JARVIS_WOL_TARGETS`` (JSON list or path to a JSON file):

    [{"name": "desktop", "mac": "AA:BB:CC:DD:EE:FF", "host": "192.168.1.20", "port": 3389,
      "broadcast": "192.168.1.255"}]

``FakeNetwork`` replaces sockets in tests/CI: hosts become reachable a configurable number of
packets after the first one (or never, for the NOT_ACHIEVED path).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from core.capabilities.gateway import Invocation, current_actor, current_correlation_id
from core.capabilities.manifest import CapabilityInputError, CapabilityManifest
from core.capabilities.registry import CapabilityRegistry
from core.events.bus import EventBus
from core.events.envelope import Event
from core.permissions.model import RiskLevel
from core.verifier.model import Outcome
from core.verifier.service import VerifierRegistry

SOURCE = "power"
_MAC = re.compile(r"^(?:[0-9a-f]{2}[:-]?){5}[0-9a-f]{2}$", re.I)


@dataclass(frozen=True)
class WakeTarget:
    name: str
    mac: str
    host: str
    port: int = 22
    broadcast: str = "255.255.255.255"

    def __post_init__(self) -> None:
        if not _MAC.match(self.mac):
            raise ValueError(f"invalid MAC for {self.name!r}")

    @property
    def mac_bytes(self) -> bytes:
        return bytes.fromhex(re.sub(r"[:-]", "", self.mac))

    def magic_packet(self) -> bytes:
        return b"\xff" * 6 + self.mac_bytes * 16

    def to_dict(self) -> dict[str, Any]:  # the MAC is hardware-identifying: keep it out of events
        return {"name": self.name, "host": self.host, "port": self.port}


def load_targets(spec: str | None) -> list[WakeTarget]:
    """``JARVIS_WOL_TARGETS``: JSON text or a path to a JSON file; empty -> no targets."""
    if not spec or not spec.strip():
        return []
    text = spec
    if not spec.lstrip().startswith("["):
        text = Path(spec).read_text(encoding="utf-8")
    raw = json.loads(text)
    return [
        WakeTarget(**{k: v for k, v in item.items() if k in WakeTarget.__dataclass_fields__})
        for item in raw
    ]


class Network(Protocol):
    async def send_broadcast(self, payload: bytes, address: str, port: int) -> None: ...
    async def tcp_reachable(self, host: str, port: int, timeout_s: float) -> bool | None: ...


class SocketNetwork:
    async def send_broadcast(self, payload: bytes, address: str, port: int) -> None:
        def _send() -> None:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                s.sendto(payload, (address, port))

        await asyncio.to_thread(_send)

    async def tcp_reachable(self, host: str, port: int, timeout_s: float) -> bool | None:
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout_s)
        except (TimeoutError, OSError):
            return False
        writer.close()
        return True


@dataclass
class FakeNetwork:
    """Hosts wake up after ``wake_after`` packets; hosts in ``never`` stay down."""

    wake_after: int = 1
    never: set[str] = field(default_factory=set)
    packets: list[tuple[bytes, str, int]] = field(default_factory=list)
    awake: set[str] = field(default_factory=set)
    _count: dict[bytes, int] = field(default_factory=dict)
    mac_to_host: dict[str, str] = field(default_factory=dict)

    async def send_broadcast(self, payload: bytes, address: str, port: int) -> None:
        self.packets.append((payload, address, port))
        mac = payload[6:12].hex(":")
        self._count[mac.encode()] = self._count.get(mac.encode(), 0) + 1
        host = self.mac_to_host.get(mac)
        if host and host not in self.never and self._count[mac.encode()] >= self.wake_after:
            self.awake.add(host)

    async def tcp_reachable(self, host: str, port: int, timeout_s: float) -> bool | None:
        return host in self.awake


class WolService:
    def __init__(
        self,
        targets: list[WakeTarget],
        bus: EventBus,
        network: Network | None = None,
        *,
        wol_port: int = 9,
        verify_timeout_s: float = 20.0,
        probe_timeout_s: float = 1.5,
    ) -> None:
        self.targets = {t.name.lower(): t for t in targets}
        self.bus = bus
        self.network = network or SocketNetwork()
        self.wol_port = wol_port
        self.verify_timeout_s = verify_timeout_s
        self.probe_timeout_s = probe_timeout_s

    def target(self, name: str) -> WakeTarget:
        key = (name or "").strip().lower()
        if key in self.targets:
            return self.targets[key]
        hits = [t for k, t in self.targets.items() if key and key in k]
        if len(hits) == 1:
            return hits[0]
        raise CapabilityInputError(
            f"unknown wake target {name!r}; configured: {sorted(self.targets) or 'none'}"
        )

    async def reachable(self, t: WakeTarget) -> bool | None:
        return await self.network.tcp_reachable(t.host, t.port, self.probe_timeout_s)

    async def wait_reachable(self, t: WakeTarget) -> bool | None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.verify_timeout_s
        result: bool | None = False
        while True:
            result = await self.reachable(t)
            if result or loop.time() >= deadline:
                return result
            await asyncio.sleep(min(1.0, max(0.0, deadline - loop.time())))


WOL_MANIFESTS: tuple[CapabilityManifest, ...] = (
    CapabilityManifest(
        name="power.status",
        version="1.0",
        risk=RiskLevel.P0,
        inputs={"target": "string?"},
        description="Is a configured machine reachable (TCP probe)? Lists targets when omitted.",
    ),
    CapabilityManifest(
        name="power.wake",
        version="1.0",
        risk=RiskLevel.P3,
        inputs={"target": "string"},
        requires=("device.trusted",),
        side_effects=True,
        reversible=False,
        verifier="power.host_reachable",
        timeout_ms=60_000,
        description="Wake a configured machine by Wake-on-LAN and wait until it answers.",
    ),
)


def register_wol(
    registry: CapabilityRegistry, verifiers: VerifierRegistry, service: WolService
) -> CapabilityRegistry:
    async def status(args: dict[str, Any]) -> dict[str, Any]:
        names = (
            [service.target(args["target"]).name] if args.get("target") else list(service.targets)
        )
        out = []
        for n in names:
            t = service.targets[n.lower()]
            out.append({**t.to_dict(), "reachable": await service.reachable(t)})
        return {"targets": out, "count": len(out)}

    async def wake(args: dict[str, Any]) -> dict[str, Any]:
        t = service.target(args["target"])
        already = await service.reachable(t)
        if already:
            return {**t.to_dict(), "sent": False, "already_online": True}
        await service.network.send_broadcast(t.magic_packet(), t.broadcast, service.wol_port)
        await service.bus.publish(
            Event.new(
                "power.wake.sent",
                SOURCE,
                {**t.to_dict(), "actor": current_actor.get()},
                correlation_id=current_correlation_id.get() or "power",
            )
        )
        return {**t.to_dict(), "sent": True, "already_online": False}

    registry.register(WOL_MANIFESTS[0], status)
    registry.register(WOL_MANIFESTS[1], wake)

    async def host_reachable(inv: Invocation) -> tuple[Outcome, dict[str, Any]]:
        name = str((inv.result or {}).get("name") or inv.args.get("target", ""))
        try:
            t = service.target(name)
        except CapabilityInputError:
            return Outcome.UNKNOWN, {"reason": "no such target"}
        up = await service.wait_reachable(t)
        if up is None:
            return Outcome.UNKNOWN, {"host": t.host, "reason": "no probe signal"}
        return (Outcome.ACHIEVED if up else Outcome.NOT_ACHIEVED), {
            "host": t.host,
            "port": t.port,
            "waited_s": service.verify_timeout_s if not up else None,
        }

    verifiers.register("power.host_reachable", host_reachable)
    return registry


def default_wol_targets() -> list[WakeTarget]:
    """Demo targets for ``JARVIS_HOME=fake`` (never used with the real network)."""
    return [WakeTarget("desktop", "02:00:00:00:00:01", "192.0.2.10", 3389, "192.0.2.255")]


def targets_from_env() -> list[WakeTarget]:
    return load_targets(os.environ.get("JARVIS_WOL_TARGETS"))
