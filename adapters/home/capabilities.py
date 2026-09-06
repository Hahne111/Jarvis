"""Home capabilities (SPEC §11, §17.2, SECURITY.md §4) - every side effect has a verifier.

home.list_devices    P0
home.get_state       P0
home.state.get       P0
home.light.set       P2  reversible                       verifier home.state_is
home.switch.set      P2  reversible                       verifier home.state_is
home.cover.set       P2  reversible (blinds; not garage)  verifier home.state_is
home.climate.set     P2  reversible                       verifier home.state_is
home.scene.activate  P2                                   verifier home.scene_activated
home.state.set       P2  reversible (Home/Away/Sleep/...) verifier home.mode_is
home.lock.set        P4  trusted device, strong proof     verifier home.state_is
home.alarm.set       P4  trusted device, strong proof     verifier home.state_is
home.garage.set      P4  trusted device, strong proof     verifier home.state_is

Home Safety: lock/alarm/garage are P4 -> the policy demands a *strong* proof (passkey/biometric),
so a voice recording alone can never unlock anything. Targets are resolved deterministically by
the DeviceRegistry ("kitchen", "kitchen light", "light.kitchen"); an ambiguous target is an
input error, never a guess. The verifier always reads the state back from the gateway.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from core.capabilities.gateway import Invocation, current_actor, current_correlation_id
from core.capabilities.manifest import CapabilityInputError, CapabilityManifest
from core.capabilities.registry import CapabilityRegistry
from core.events.bus import EventBus
from core.events.envelope import Event
from core.permissions.model import RiskLevel
from core.verifier.model import Outcome
from core.verifier.service import VerifierRegistry

from adapters.home.backend import GARAGE_CLASSES, Entity, HomeBackend, HomeUnavailable
from adapters.home.registry import DeviceRegistry
from adapters.home.states import HomeState, HomeStateMachine, HomeStatePolicy

SOURCE = "home"
TRUSTED = ("device.trusted",)
ALARM_MODES = ("armed_away", "armed_home", "armed_night", "disarmed")

HOME_MANIFESTS: tuple[CapabilityManifest, ...] = (
    CapabilityManifest(
        name="home.list_devices",
        version="1.0",
        risk=RiskLevel.P0,
        inputs={"room": "string?", "domain": "string?"},
        description="List rooms and devices known to the home (optionally filtered).",
    ),
    CapabilityManifest(
        name="home.get_state",
        version="1.0",
        risk=RiskLevel.P0,
        inputs={"target": "string"},
        description="Read the current state of a device, a room or an entity_id.",
    ),
    CapabilityManifest(
        name="home.state.get",
        version="1.0",
        risk=RiskLevel.P0,
        inputs={},
        description="Current home state (home/away/sleep/work/movie/guests/night/vacation).",
    ),
    CapabilityManifest(
        name="home.light.set",
        version="1.0",
        risk=RiskLevel.P2,
        inputs={"target": "string", "on": "boolean", "brightness": "integer?"},
        side_effects=True,
        reversible=True,
        verifier="home.state_is",
        description="Turn a light (or all lights of a room) on/off, optional brightness 0-255.",
    ),
    CapabilityManifest(
        name="home.switch.set",
        version="1.0",
        risk=RiskLevel.P2,
        inputs={"target": "string", "on": "boolean"},
        side_effects=True,
        reversible=True,
        verifier="home.state_is",
        description="Turn a switch/plug on or off.",
    ),
    CapabilityManifest(
        name="home.cover.set",
        version="1.0",
        risk=RiskLevel.P2,
        inputs={"target": "string", "open": "boolean"},
        side_effects=True,
        reversible=True,
        verifier="home.state_is",
        description="Open/close blinds or shutters (garage doors need home.garage.set).",
    ),
    CapabilityManifest(
        name="home.climate.set",
        version="1.0",
        risk=RiskLevel.P2,
        inputs={"target": "string", "temperature": "number"},
        side_effects=True,
        reversible=True,
        verifier="home.state_is",
        description="Set the target temperature of a thermostat (5-30 °C).",
    ),
    CapabilityManifest(
        name="home.scene.activate",
        version="1.0",
        risk=RiskLevel.P2,
        inputs={"target": "string"},
        side_effects=True,
        reversible=False,
        verifier="home.scene_activated",
        description="Activate a scene by name or scene entity_id.",
    ),
    CapabilityManifest(
        name="home.state.set",
        version="1.0",
        risk=RiskLevel.P2,
        inputs={"state": "string", "apply_defaults": "boolean?"},
        side_effects=True,
        reversible=True,
        verifier="home.mode_is",
        description=(
            "Set the home state: home, away, sleep, work, movie, guests, night, vacation. "
            "apply_defaults=true also applies the state's light/climate defaults (only for "
            "domains the state's device policy allows; never locks, alarms or garages)."
        ),
    ),
    CapabilityManifest(
        name="home.lock.set",
        version="1.0",
        risk=RiskLevel.P4,
        inputs={"target": "string", "locked": "boolean"},
        requires=TRUSTED,
        side_effects=True,
        reversible=False,
        verifier="home.state_is",
        description="Lock or unlock a door. Needs a strong proof from the owner.",
    ),
    CapabilityManifest(
        name="home.alarm.set",
        version="1.0",
        risk=RiskLevel.P4,
        inputs={"target": "string", "mode": "string"},
        requires=TRUSTED,
        side_effects=True,
        reversible=False,
        verifier="home.state_is",
        description="Arm/disarm the alarm: armed_away, armed_home, armed_night, disarmed.",
    ),
    CapabilityManifest(
        name="home.garage.set",
        version="1.0",
        risk=RiskLevel.P4,
        inputs={"target": "string", "open": "boolean"},
        requires=TRUSTED,
        side_effects=True,
        reversible=False,
        verifier="home.state_is",
        description="Open or close the garage door. Needs a strong proof from the owner.",
    ),
)


class HomeService:
    """Backend + registry + state machine, shared by capabilities, verifiers and the API."""

    def __init__(
        self,
        backend: HomeBackend,
        bus: EventBus,
        *,
        registry: DeviceRegistry | None = None,
        states: HomeStateMachine | None = None,
    ) -> None:
        self.backend = backend
        self.bus = bus
        self.registry = registry or DeviceRegistry()
        self.states = states or HomeStateMachine()
        self._synced = False

    # -- registry sync (lazy: the gateway may be offline at start) ---------------------------

    async def sync(self, force: bool = False) -> bool:
        """Refresh the registry from the gateway. Offline with nothing cached -> HomeUnavailable;
        offline with a cached registry -> False (stale but usable for reads)."""
        if self._synced and not force:
            return True
        try:
            self.registry.update(await self.backend.list_entities())
        except HomeUnavailable:
            if not self._synced:
                raise
            return False
        self._synced = True
        return True

    async def entity(self, entity_id: str) -> Entity | None:
        """Fresh read-back from the gateway (verifier path); keeps the registry current."""
        e = await self.backend.get_state(entity_id)
        if e is not None:
            self.registry.update([e])
        return e

    async def resolve_one(self, target: str, domain: str) -> Entity:
        await self.sync()
        hits = self.registry.resolve(target, domain)
        if not hits:
            raise CapabilityInputError(f"no {domain} matches {target!r}")
        if len(hits) > 1 and not ("." in target and target in {h.entity_id for h in hits}):
            names = ", ".join(h.entity_id for h in hits)
            raise CapabilityInputError(f"{target!r} is ambiguous for {domain}: {names}")
        return hits[0]

    async def resolve_many(self, target: str, domain: str) -> list[Entity]:
        await self.sync()
        hits = self.registry.resolve(target, domain)
        if not hits:
            raise CapabilityInputError(f"no {domain} matches {target!r}")
        return hits

    def rebuild(self) -> None:
        """Home state from the log (start-up), like presence: last home.state.changed wins."""
        for _, ev in self.bus.replay(type_prefix="home.state.changed"):
            self.states.set(ev.payload["to"], at=ev.timestamp.isoformat())

    def snapshot(self) -> dict[str, Any]:
        return {
            "backend": self.backend.name,
            "synced": self._synced,
            "state": self.states.to_dict(),
            **self.registry.to_dict(),
        }


def register_home(
    registry: CapabilityRegistry, verifiers: VerifierRegistry, service: HomeService
) -> CapabilityRegistry:
    async def emit(event_type: str, payload: dict[str, Any]) -> None:
        await service.bus.publish(
            Event.new(
                event_type,
                SOURCE,
                {**payload, "actor": current_actor.get()},
                correlation_id=current_correlation_id.get() or "home",
            )
        )

    def guard(fn):
        async def wrapped(args: dict[str, Any]) -> dict[str, Any]:
            try:
                return await fn(args)
            except HomeUnavailable as exc:
                raise RuntimeError(str(exc)) from exc  # FAILED, never a guessed success
            except ValueError as exc:
                raise CapabilityInputError(str(exc)) from exc

        return wrapped

    async def act(
        entities: list[Entity], domain: str, svc: str, data: dict[str, Any], want: dict[str, Any]
    ) -> dict[str, Any]:
        changed = []
        for e in entities:
            before = e.state
            await service.backend.call_service(domain, svc, {"entity_id": e.entity_id, **data})
            after = await service.entity(e.entity_id)
            changed.append(
                {"entity_id": e.entity_id, "from": before, "to": after.state if after else None}
            )
            await emit("home.device.changed", {**changed[-1], "domain": domain, "service": svc})
        return {"entities": [c["entity_id"] for c in changed], "changes": changed, "want": want}

    # -- reads -----------------------------------------------------------------------------

    @guard
    async def list_devices(args: dict[str, Any]) -> dict[str, Any]:
        online = await service.sync(force=True)
        devices = service.registry.devices(args.get("room"), args.get("domain"))
        return {
            "online": online,
            "rooms": [r.to_dict() for r in service.registry.rooms()],
            "devices": [d.to_dict() for d in devices],
            "count": len(devices),
        }

    @guard
    async def get_state(args: dict[str, Any]) -> dict[str, Any]:
        await service.sync()
        hits = service.registry.resolve(args["target"])
        if not hits:
            raise CapabilityInputError(f"nothing matches {args['target']!r}")
        fresh = [await service.entity(h.entity_id) or h for h in hits]
        return {"devices": [e.to_dict() for e in fresh], "count": len(fresh)}

    async def state_get(args: dict[str, Any]) -> dict[str, Any]:
        return service.states.to_dict()

    # -- P2 comfort actions ------------------------------------------------------------------

    @guard
    async def light_set(args: dict[str, Any]) -> dict[str, Any]:
        ents = await service.resolve_many(args["target"], "light")
        data: dict[str, Any] = {}
        if args.get("brightness") is not None and args["on"]:
            data["brightness"] = max(0, min(255, int(args["brightness"])))
        want = {"state": "on" if args["on"] else "off", **data}
        return await act(ents, "light", "turn_on" if args["on"] else "turn_off", data, want)

    @guard
    async def switch_set(args: dict[str, Any]) -> dict[str, Any]:
        ents = await service.resolve_many(args["target"], "switch")
        want = {"state": "on" if args["on"] else "off"}
        return await act(ents, "switch", "turn_on" if args["on"] else "turn_off", {}, want)

    @guard
    async def cover_set(args: dict[str, Any]) -> dict[str, Any]:
        ents = await service.resolve_many(args["target"], "cover")
        for e in ents:
            if (e.device_class or "") in GARAGE_CLASSES:
                raise CapabilityInputError(
                    f"{e.entity_id} is a {e.device_class}: use home.garage.set"
                )
        want = {"state": "open" if args["open"] else "closed"}
        return await act(ents, "cover", "open_cover" if args["open"] else "close_cover", {}, want)

    @guard
    async def climate_set(args: dict[str, Any]) -> dict[str, Any]:
        temp = float(args["temperature"])
        if not 5.0 <= temp <= 30.0:
            raise CapabilityInputError("temperature must be between 5 and 30 °C")
        ents = await service.resolve_many(args["target"], "climate")
        want = {"temperature": temp}
        return await act(ents, "climate", "set_temperature", {"temperature": temp}, want)

    @guard
    async def scene_activate(args: dict[str, Any]) -> dict[str, Any]:
        e = await service.resolve_one(args["target"], "scene")
        before = e.state
        await service.backend.call_service("scene", "turn_on", {"entity_id": e.entity_id})
        after = await service.entity(e.entity_id)
        await emit(
            "home.device.changed",
            {
                "entity_id": e.entity_id,
                "from": before,
                "to": after.state if after else None,
                "domain": "scene",
                "service": "turn_on",
            },
        )
        return {"entity_id": e.entity_id, "before": before, "after": after.state if after else None}

    async def state_set(args: dict[str, Any]) -> dict[str, Any]:
        try:
            wanted = HomeState(str(args["state"]).lower())
        except ValueError:
            raise CapabilityInputError(
                f"unknown home state {args['state']!r}; one of {[s.value for s in HomeState]}"
            ) from None
        now = datetime.now(UTC).isoformat()
        old, new = service.states.set(wanted, at=now)
        policy = service.states.policy()
        await emit(
            "home.state.changed", {"from": old.value, "to": new.value, "policy": policy.to_dict()}
        )
        applied: list[dict[str, Any]] = []
        if args.get("apply_defaults"):
            applied = await apply_state_defaults(policy)
        return {"from": old.value, "to": new.value, "policy": policy.to_dict(), "applied": applied}

    async def apply_state_defaults(policy: HomeStatePolicy) -> list[dict[str, Any]]:
        """Offline basics (step 56): a state change may set lights/climate - never security."""
        try:
            await service.sync(force=True)
        except HomeUnavailable:
            return [{"skipped": "home gateway offline"}]
        applied: list[dict[str, Any]] = []
        lighting = {
            "off": ("turn_off", {}),
            "dim": ("turn_on", {"brightness": 80}),
            "night": ("turn_on", {"brightness": 25}),
        }.get(policy.lighting)
        if "light" in policy.device_policy and lighting:
            svc, data = lighting
            lights = service.registry.devices(domain="light")
            want = {"state": "off" if svc == "turn_off" else "on", **data}
            try:
                applied.extend((await act(lights, "light", svc, data, want))["changes"])
            except (HomeUnavailable, ValueError) as exc:
                applied.append({"skipped": f"lights: {exc}"})
        if "climate" in policy.device_policy:
            thermostats = service.registry.devices(domain="climate")
            try:
                res = await act(
                    thermostats,
                    "climate",
                    "set_temperature",
                    {"temperature": policy.climate_c},
                    {"temperature": policy.climate_c},
                )
                applied.extend(res["changes"])
            except (HomeUnavailable, ValueError) as exc:
                applied.append({"skipped": f"climate: {exc}"})
        return applied

    # -- P4 security actions (SECURITY.md §4) ------------------------------------------------

    @guard
    async def lock_set(args: dict[str, Any]) -> dict[str, Any]:
        e = await service.resolve_one(args["target"], "lock")
        want = {"state": "locked" if args["locked"] else "unlocked"}
        return await act([e], "lock", "lock" if args["locked"] else "unlock", {}, want)

    @guard
    async def alarm_set(args: dict[str, Any]) -> dict[str, Any]:
        mode = str(args["mode"]).lower()
        if mode not in ALARM_MODES:
            raise CapabilityInputError(f"mode must be one of {ALARM_MODES}")
        e = await service.resolve_one(args["target"], "alarm_control_panel")
        svc = "alarm_disarm" if mode == "disarmed" else f"alarm_arm_{mode.split('_', 1)[1]}"
        return await act([e], "alarm_control_panel", svc, {}, {"state": mode})

    @guard
    async def garage_set(args: dict[str, Any]) -> dict[str, Any]:
        e = await service.resolve_one(args["target"], "cover")
        if (e.device_class or "") not in GARAGE_CLASSES:
            raise CapabilityInputError(f"{e.entity_id} is not a garage/gate: use home.cover.set")
        want = {"state": "open" if args["open"] else "closed"}
        return await act([e], "cover", "open_cover" if args["open"] else "close_cover", {}, want)

    handlers = {
        "home.list_devices": list_devices,
        "home.get_state": get_state,
        "home.state.get": state_get,
        "home.light.set": light_set,
        "home.switch.set": switch_set,
        "home.cover.set": cover_set,
        "home.climate.set": climate_set,
        "home.scene.activate": scene_activate,
        "home.state.set": state_set,
        "home.lock.set": lock_set,
        "home.alarm.set": alarm_set,
        "home.garage.set": garage_set,
    }
    for manifest in HOME_MANIFESTS:
        registry.register(manifest, handlers[manifest.name])

    # -- verifiers: independent read-back from the gateway; unreachable -> UNKNOWN -----------

    async def state_is(inv: Invocation) -> tuple[Outcome, dict[str, Any]]:
        result = inv.result or {}
        want = result.get("want") or {}
        evidence: dict[str, Any] = {"want": want, "readback": []}
        try:
            for eid in result.get("entities", []):
                e = await service.backend.get_state(eid)
                if e is None:
                    return Outcome.UNKNOWN, {**evidence, "reason": f"{eid} not readable"}
                ok = ("state" not in want or e.state == want["state"]) and all(
                    (e.attributes.get(k) == v) for k, v in want.items() if k != "state"
                )
                evidence["readback"].append({"entity_id": eid, "state": e.state, "ok": ok})
                if not ok:
                    return Outcome.NOT_ACHIEVED, evidence
        except HomeUnavailable as exc:
            return Outcome.UNKNOWN, {**evidence, "reason": str(exc)}
        if not result.get("entities"):
            return Outcome.UNKNOWN, {**evidence, "reason": "no entities in result"}
        return Outcome.ACHIEVED, evidence

    async def scene_activated(inv: Invocation) -> tuple[Outcome, dict[str, Any]]:
        result = inv.result or {}
        try:
            e = await service.backend.get_state(str(result.get("entity_id")))
        except HomeUnavailable as exc:
            return Outcome.UNKNOWN, {"reason": str(exc)}
        if e is None:
            return Outcome.UNKNOWN, {"reason": "scene not readable"}
        changed = e.state != result.get("before") and e.state not in ("unknown", "unavailable")
        return (Outcome.ACHIEVED if changed else Outcome.NOT_ACHIEVED), {
            "before": result.get("before"),
            "now": e.state,
        }

    def mode_is(inv: Invocation) -> tuple[Outcome, dict[str, Any]]:
        wanted = str(inv.args.get("state", "")).lower()
        return (
            Outcome.ACHIEVED if service.states.current.value == wanted else Outcome.NOT_ACHIEVED
        ), {"current": service.states.current.value}

    verifiers.register("home.state_is", state_is)
    verifiers.register("home.scene_activated", scene_activated)
    verifiers.register("home.mode_is", mode_is)
    return registry
