"""Memory and personalisation (SPEC §8, Phase 4)."""

from core.memory.embedding import Embedder, HashingEmbedder
from core.memory.model import (
    DEFAULT_CONFIDENCE,
    MemoryItem,
    MemorySource,
    MemoryType,
    Retention,
)
from core.memory.store import MemoryStore
from core.memory.writer import MemoryPolicy, MemoryPolicyError, MemoryWriter, WriteResult

__all__ = [
    "DEFAULT_CONFIDENCE",
    "Embedder",
    "HashingEmbedder",
    "MemoryItem",
    "MemoryPolicy",
    "MemoryPolicyError",
    "MemorySource",
    "MemoryStore",
    "MemoryType",
    "MemoryWriter",
    "Retention",
    "WriteResult",
]
