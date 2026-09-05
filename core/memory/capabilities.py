"""Memory as capabilities (Phase 4 step 27, correction loop through the normal gate).

    memory.recall    P0  search memory (never returns secret items)
    memory.remember  P1  store an explicit statement            verifier: memory.stored
    memory.correct   P3  replace a memory with a new version    verifier: memory.corrected
    memory.forget    P3  delete a memory                         verifier: memory.gone

Risk levels follow SECURITY.md §1: reading is observe, writing what the owner just said is safe,
changing or deleting what JARVIS believes is sensitive and asks for confirmation.
"""

from __future__ import annotations

from typing import Any

from core.capabilities.gateway import Invocation, current_correlation_id
from core.capabilities.manifest import CapabilityInputError, CapabilityManifest
from core.capabilities.registry import CapabilityRegistry
from core.events.envelope import Sensitivity
from core.memory.model import MemorySource, MemoryType
from core.memory.store import MemoryStore
from core.memory.writer import MemoryWriter
from core.permissions.model import RiskLevel
from core.verifier.model import Outcome
from core.verifier.service import VerifierRegistry

RECALL = CapabilityManifest(
    name="memory.recall",
    version="1.0",
    risk=RiskLevel.P0,
    inputs={"query": "string", "project_scope": "string?", "limit": "integer?"},
    description="Search long-term memory for facts, preferences and project knowledge.",
)
REMEMBER = CapabilityManifest(
    name="memory.remember",
    version="1.0",
    risk=RiskLevel.P1,
    inputs={
        "type": "string",
        "subject": "string",
        "predicate": "string",
        "value": "string",
        "project_scope": "string?",
        "ttl_s": "integer?",
    },
    side_effects=True,
    reversible=True,
    verifier="memory.stored",
    description=(
        "Store something the owner explicitly stated (type: preference|semantic|project|"
        "relationship|procedural|habit|episodic). Never store secrets."
    ),
)
CORRECT = CapabilityManifest(
    name="memory.correct",
    version="1.0",
    risk=RiskLevel.P3,
    inputs={"memory_id": "string", "value": "string"},
    side_effects=True,
    reversible=True,
    verifier="memory.corrected",
    description="Replace a remembered value with a corrected one (keeps the old version).",
)
FORGET = CapabilityManifest(
    name="memory.forget",
    version="1.0",
    risk=RiskLevel.P3,
    inputs={"memory_id": "string"},
    side_effects=True,
    reversible=False,
    verifier="memory.gone",
    description="Delete a memory permanently.",
)

_ALLOWED_TYPES = {t.value for t in MemoryType} - {MemoryType.WORKING.value, MemoryType.VISUAL.value}


def register_memory_capabilities(
    registry: CapabilityRegistry, store: MemoryStore, writer: MemoryWriter
) -> CapabilityRegistry:
    def recall(args: dict[str, Any]) -> dict[str, Any]:
        hits = store.search(
            args["query"],
            project_scope=args.get("project_scope"),
            limit=int(args.get("limit") or 5),
        )
        items = [
            {**i.to_dict(), "score": score}
            for score, i in hits
            if i.sensitivity is not Sensitivity.SECRET
        ]
        return {"items": items, "count": len(items)}

    async def remember(args: dict[str, Any]) -> dict[str, Any]:
        if args["type"] not in _ALLOWED_TYPES:
            raise CapabilityInputError(f"type must be one of {sorted(_ALLOWED_TYPES)}")
        result = await writer.remember(
            args["type"],
            args["subject"],
            args["predicate"],
            args["value"],
            source=MemorySource.EXPLICIT_STATEMENT,
            project_scope=args.get("project_scope"),
            ttl_s=args.get("ttl_s"),
            correlation_id=current_correlation_id.get(),
        )
        return {
            "action": result.action,
            "memory_id": result.item.memory_id if result.item else None,
            "reason": result.reason,
        }

    async def correct(args: dict[str, Any]) -> dict[str, Any]:
        result = await writer.correct(
            args["memory_id"], args["value"], correlation_id=current_correlation_id.get()
        )
        return {
            "action": result.action,
            "memory_id": result.item.memory_id if result.item else None,
        }

    async def forget(args: dict[str, Any]) -> dict[str, Any]:
        return {
            "forgotten": await writer.forget(
                args["memory_id"],
                reason="agent request",
                correlation_id=current_correlation_id.get(),
            )
        }

    registry.register(RECALL, recall)
    registry.register(REMEMBER, remember)
    registry.register(CORRECT, correct)
    registry.register(FORGET, forget)
    return registry


def register_memory_verifiers(verifiers: VerifierRegistry, store: MemoryStore) -> VerifierRegistry:
    def stored(inv: Invocation) -> tuple[Outcome, dict]:
        mid = (inv.result or {}).get("memory_id") if isinstance(inv.result, dict) else None
        item = store.get(mid) if mid else None
        ok = item is not None and item.active and _same(item.value, inv.args.get("value"))
        return (Outcome.ACHIEVED if ok else Outcome.NOT_ACHIEVED), {
            "memory_id": mid,
            "found": item is not None,
        }

    def corrected(inv: Invocation) -> tuple[Outcome, dict]:
        old = store.get(inv.args["memory_id"])
        new_id = (inv.result or {}).get("memory_id") if isinstance(inv.result, dict) else None
        new = store.get(new_id) if new_id else None
        ok = (
            old is not None
            and new is not None
            and old.superseded_by == new.memory_id
            and _same(new.value, inv.args.get("value"))
        )
        return (Outcome.ACHIEVED if ok else Outcome.NOT_ACHIEVED), {
            "old": inv.args["memory_id"],
            "new": new_id,
        }

    def gone(inv: Invocation) -> tuple[Outcome, dict]:
        present = store.get(inv.args["memory_id"]) is not None
        return (Outcome.NOT_ACHIEVED if present else Outcome.ACHIEVED), {"present": present}

    verifiers.register("memory.stored", stored)
    verifiers.register("memory.corrected", corrected)
    verifiers.register("memory.gone", gone)
    return verifiers


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, str) and isinstance(b, str):
        return a.strip().casefold() == b.strip().casefold()
    return a == b
