"""Capability Registry, mock capabilities and Execution Gateway (Phase 1/2, Commit 007)."""

from core.capabilities.gateway import ExecutionGateway, GatewayHalted, Invocation, InvocationStatus
from core.capabilities.manifest import CapabilityInputError, CapabilityManifest
from core.capabilities.mocks import register_mocks
from core.capabilities.registry import (
    Capability,
    CapabilityConflict,
    CapabilityHealth,
    CapabilityNotFound,
    CapabilityRegistry,
)

__all__ = [
    "Capability",
    "CapabilityConflict",
    "CapabilityHealth",
    "CapabilityInputError",
    "CapabilityManifest",
    "CapabilityNotFound",
    "CapabilityRegistry",
    "ExecutionGateway",
    "GatewayHalted",
    "Invocation",
    "InvocationStatus",
    "register_mocks",
]
