"""Desktop Agent capabilities (SPEC §10.2, Phase 6): apps, windows, screen, input, system."""

from adapters.desktop.backend import DesktopBackend, FakeDesktop, PrototypeDesktop
from adapters.desktop.capabilities import DESKTOP_MANIFESTS, register_desktop

__all__ = [
    "DESKTOP_MANIFESTS",
    "DesktopBackend",
    "FakeDesktop",
    "PrototypeDesktop",
    "register_desktop",
]
