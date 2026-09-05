"""Device registry + room graph (SPEC §11, Phase 8 step 52).

Entities from the gateway are grouped into rooms; rooms into floors (optional ``floors`` map, e.g.
from a small JSON file next to the config). The registry resolves natural targets such as
"kitchen", "kitchen light" or a full ``entity_id`` deterministically - no model involved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from adapters.home.backend import Entity

_ALIASES = {
    "küche": "kitchen",
    "kueche": "kitchen",
    "wohnzimmer": "living_room",
    "living room": "living_room",
    "schlafzimmer": "bedroom",
    "flur": "hall",
    "hallway": "hall",
    "diele": "hall",
    "bad": "bathroom",
    "büro": "office",
    "buero": "office",
    "garage": "garage",
}


def norm(text: str) -> str:
    t = text.strip().lower()
    t = _ALIASES.get(t, t)
    return re.sub(r"[\s-]+", "_", t)


@dataclass
class Room:
    room_id: str
    floor: str | None = None
    entities: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"room_id": self.room_id, "floor": self.floor, "entities": list(self.entities)}


class DeviceRegistry:
    def __init__(
        self, entities: list[Entity] | None = None, floors: dict[str, str] | None = None
    ) -> None:
        self._floors = dict(floors or {})
        self._entities: dict[str, Entity] = {}
        self._rooms: dict[str, Room] = {}
        self.update(entities or [])

    # -- building ----------------------------------------------------------------------------

    def update(self, entities: list[Entity]) -> None:
        for e in entities:
            self._entities[e.entity_id] = e
            room_id = norm(e.area) if e.area else "unassigned"
            room = self._rooms.setdefault(room_id, Room(room_id, self._floors.get(room_id)))
            if e.entity_id not in room.entities:
                room.entities.append(e.entity_id)

    # -- queries -----------------------------------------------------------------------------

    def rooms(self) -> list[Room]:
        return [self._rooms[k] for k in sorted(self._rooms)]

    def entity(self, entity_id: str) -> Entity | None:
        return self._entities.get(entity_id)

    def devices(self, room: str | None = None, domain: str | None = None) -> list[Entity]:
        out = []
        for e in self._entities.values():
            if room and norm(e.area or "unassigned") != norm(room):
                continue
            if domain and e.domain != domain:
                continue
            out.append(e)
        return sorted(out, key=lambda e: e.entity_id)

    def resolve(self, target: str, domain: str | None = None) -> list[Entity]:
        """entity_id -> [that]; room name -> all matching in room; device name -> best matches."""
        if not target:
            return []
        if "." in target and target in self._entities:
            e = self._entities[target]
            return [e] if domain is None or e.domain == domain else []
        key = norm(target)
        if key in ("all", "alle", "everything", "überall", "ueberall"):
            return self.devices(domain=domain)
        if key in self._rooms:
            return self.devices(room=key, domain=domain)
        hits = [
            e
            for e in self._entities.values()
            if (domain is None or e.domain == domain)
            and (key in norm(e.name) or key in norm(e.entity_id.split(".", 1)[-1]))
        ]
        return sorted(hits, key=lambda e: e.entity_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rooms": [r.to_dict() for r in self.rooms()],
            "devices": [e.to_dict() for e in self.devices()],
        }
