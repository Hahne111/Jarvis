"""Home backends: the device-gateway side of the home capabilities (SPEC §11, Phase 8 step 51).

Home Assistant stays the device nervous system; JARVIS talks to *it*, not to vendors. Two
backends share one small protocol:

* ``FakeHome`` - deterministic in-memory entities/rooms for tests, CI and HUD demos. It can be
  told to be offline or to have devices that silently ignore commands (the verifier catches it).
* ``HomeAssistantBackend`` - REST client (``/api/states``, ``/api/services``) over httpx. The
  long-lived token comes only from the environment (``JARVIS_HA_TOKEN``) or the credential
  broker; it is never logged, never put into events and never repr()'d.

Verifiers ask the backend for an independent read-back after every action. When the backend is
unreachable the read-back is ``None`` -> the verifier reports UNKNOWN, never success.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

SECURITY_DOMAINS = frozenset({"lock", "alarm_control_panel"})
GARAGE_CLASSES = frozenset({"garage", "gate", "door"})


class HomeUnavailable(RuntimeError):
    """The device gateway cannot be reached (offline path, SPEC Phase 8 exit criterion)."""


@dataclass
class Entity:
    entity_id: str
    state: str
    attributes: dict[str, Any] = field(default_factory=dict)
    area: str | None = None

    @property
    def domain(self) -> str:
        return self.entity_id.split(".", 1)[0]

    @property
    def name(self) -> str:
        return str(self.attributes.get("friendly_name") or self.entity_id.split(".", 1)[-1])

    @property
    def device_class(self) -> str | None:
        dc = self.attributes.get("device_class")
        return str(dc) if dc else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "domain": self.domain,
            "name": self.name,
            "state": self.state,
            "area": self.area,
            "device_class": self.device_class,
            "attributes": {
                k: v
                for k, v in self.attributes.items()
                if k in ("brightness", "temperature", "current_temperature", "unit_of_measurement")
            },
        }


class HomeBackend(Protocol):
    name: str

    async def list_entities(self) -> list[Entity]: ...
    async def get_state(self, entity_id: str) -> Entity | None: ...
    async def call_service(self, domain: str, service: str, data: dict[str, Any]) -> None: ...


# ---------------------------------------------------------------------------- fake backend


def default_entities() -> list[Entity]:
    """A small two-room home: enough for every capability and verifier path."""
    return [
        Entity("light.kitchen", "off", {"friendly_name": "Kitchen light"}, area="kitchen"),
        Entity(
            "light.living_room",
            "on",
            {"friendly_name": "Living room light", "brightness": 200},
            area="living_room",
        ),
        Entity("switch.coffee_machine", "off", {"friendly_name": "Coffee machine"}, area="kitchen"),
        Entity(
            "climate.living_room",
            "heat",
            {"friendly_name": "Living room thermostat", "temperature": 20.0},
            area="living_room",
        ),
        Entity("scene.movie", "unknown", {"friendly_name": "Movie scene"}, area="living_room"),
        Entity("lock.front_door", "locked", {"friendly_name": "Front door"}, area="hall"),
        Entity(
            "alarm_control_panel.home",
            "disarmed",
            {"friendly_name": "Alarm"},
            area="hall",
        ),
        Entity(
            "cover.garage",
            "closed",
            {"friendly_name": "Garage door", "device_class": "garage"},
            area="garage",
        ),
        Entity("cover.bedroom_blind", "open", {"friendly_name": "Bedroom blind"}, area="bedroom"),
    ]


@dataclass
class FakeHome:
    """Deterministic home model. ``ignoring`` entities accept commands but never change state
    (a stuck relay); ``offline`` makes every call fail like an unreachable gateway."""

    entities: dict[str, Entity] = field(
        default_factory=lambda: {e.entity_id: e for e in default_entities()}
    )
    ignoring: set[str] = field(default_factory=set)
    offline: bool = False
    calls: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    name: str = "fake"

    def _check(self) -> None:
        if self.offline:
            raise HomeUnavailable("home gateway offline")

    async def list_entities(self) -> list[Entity]:
        self._check()
        return list(self.entities.values())

    async def get_state(self, entity_id: str) -> Entity | None:
        self._check()
        return self.entities.get(entity_id)

    async def call_service(self, domain: str, service: str, data: dict[str, Any]) -> None:
        self._check()
        self.calls.append((domain, service, dict(data)))
        eid = data.get("entity_id")
        ent = self.entities.get(str(eid)) if eid else None
        if ent is None:
            raise ValueError(f"unknown entity {eid!r}")
        if ent.domain != domain:
            raise ValueError(f"{eid} is not a {domain}")
        if eid in self.ignoring:
            return  # the "device" swallows the command: read-back will show no change
        if domain in ("light", "switch") and service in ("turn_on", "turn_off"):
            ent.state = "on" if service == "turn_on" else "off"
            if service == "turn_on" and "brightness" in data:
                ent.attributes["brightness"] = int(data["brightness"])
        elif domain == "climate" and service == "set_temperature":
            ent.attributes["temperature"] = float(data["temperature"])
        elif domain == "scene" and service == "turn_on":
            ent.state = datetime.now(UTC).isoformat()
            for target, state in ent.attributes.get("entities", {}).items():
                if target in self.entities:
                    self.entities[target].state = state
        elif domain == "lock" and service in ("lock", "unlock"):
            ent.state = "locked" if service == "lock" else "unlocked"
        elif domain == "alarm_control_panel" and service.startswith("alarm_"):
            ent.state = {
                "alarm_arm_away": "armed_away",
                "alarm_arm_home": "armed_home",
                "alarm_arm_night": "armed_night",
                "alarm_disarm": "disarmed",
            }[service]
        elif domain == "cover" and service in ("open_cover", "close_cover"):
            ent.state = "open" if service == "open_cover" else "closed"
        else:
            raise ValueError(f"unsupported service {domain}.{service}")


# ---------------------------------------------------------------------- home assistant


class HomeAssistantBackend:
    """Minimal Home Assistant REST client. Token only from the environment/credential broker."""

    name = "homeassistant"

    def __init__(
        self, url: str | None = None, token: str | None = None, *, timeout_s: float = 5.0
    ) -> None:
        self._url = url or os.environ.get("JARVIS_HA_URL") or "http://homeassistant.local:8123"
        self._url = self._url.rstrip("/")
        self._token = token or os.environ.get("JARVIS_HA_TOKEN") or ""
        self._timeout = timeout_s
        self._client: Any = None

    def __repr__(self) -> str:  # never leak the token
        return f"HomeAssistantBackend(url={self._url!r}, token={'set' if self._token else 'unset'})"

    def _http(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(
                base_url=self._url,
                headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
                timeout=self._timeout,
            )
        return self._client

    async def _request(self, method: str, path: str, json: dict[str, Any] | None = None) -> Any:
        import httpx

        try:
            r = await self._http().request(method, path, json=json)
        except httpx.HTTPError as exc:  # connection refused, timeout, DNS ...
            raise HomeUnavailable(f"home assistant unreachable: {type(exc).__name__}") from None
        if r.status_code in (401, 403):
            raise HomeUnavailable("home assistant rejected the token")
        if r.status_code >= 400:
            raise ValueError(f"home assistant error {r.status_code} for {method} {path}")
        return r.json() if r.content else None

    @staticmethod
    def _entity(raw: dict[str, Any]) -> Entity:
        attrs = dict(raw.get("attributes") or {})
        return Entity(
            entity_id=str(raw["entity_id"]),
            state=str(raw.get("state")),
            attributes=attrs,
            area=attrs.get("area_id") or attrs.get("area"),
        )

    async def list_entities(self) -> list[Entity]:
        return [self._entity(x) for x in await self._request("GET", "/api/states")]

    async def get_state(self, entity_id: str) -> Entity | None:
        try:
            raw = await self._request("GET", f"/api/states/{entity_id}")
        except ValueError:
            return None  # 404: unknown entity
        return self._entity(raw) if raw else None

    async def call_service(self, domain: str, service: str, data: dict[str, Any]) -> None:
        await self._request("POST", f"/api/services/{domain}/{service}", json=data)
