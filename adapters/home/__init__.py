"""Home Core capabilities (SPEC §11, Phase 8): Home Assistant adapter, device registry, states."""

from adapters.home.backend import (
    Entity,
    FakeHome,
    HomeAssistantBackend,
    HomeBackend,
    HomeUnavailable,
)
from adapters.home.capabilities import HOME_MANIFESTS, HomeService, register_home
from adapters.home.registry import DeviceRegistry, Room
from adapters.home.states import DEFAULT_POLICIES, HomeState, HomeStateMachine, HomeStatePolicy
from adapters.home.wol import (
    WOL_MANIFESTS,
    FakeNetwork,
    WakeTarget,
    WolService,
    default_wol_targets,
    load_targets,
    register_wol,
    targets_from_env,
)

__all__ = [
    "DEFAULT_POLICIES",
    "HOME_MANIFESTS",
    "WOL_MANIFESTS",
    "DeviceRegistry",
    "Entity",
    "FakeHome",
    "FakeNetwork",
    "HomeAssistantBackend",
    "HomeBackend",
    "HomeService",
    "HomeState",
    "HomeStateMachine",
    "HomeStatePolicy",
    "HomeUnavailable",
    "Room",
    "WakeTarget",
    "WolService",
    "default_wol_targets",
    "load_targets",
    "register_home",
    "register_wol",
    "targets_from_env",
]
