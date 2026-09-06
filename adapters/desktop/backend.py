"""Desktop backends: the OS-facing side of the desktop capabilities.

``FakeDesktop`` keeps an in-memory model (tests, CI, HUD demos). ``PrototypeDesktop`` wraps the
existing tool functions in ``jarvis/tools`` unchanged (ADR-0001); those are only imported when
the backend is used, so the Core keeps running headless without pyautogui.

A backend also answers the verifiers' questions (process running? window focused? typed?). When
it has no independent signal it returns ``None`` -> the verifier reports UNKNOWN, never success.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, Protocol


class DesktopBackend(Protocol):
    def open_app(self, name: str) -> str: ...
    def process_running(self, name: str) -> bool | None: ...
    def list_windows(self) -> list[str]: ...
    def focus_window(self, title: str) -> str: ...
    def focused_window(self) -> str | None: ...
    def screenshot(self, filename: str = "") -> str: ...
    def type_text(self, text: str) -> str: ...
    def press_key(self, keys: str) -> str: ...
    def last_input(self) -> tuple[str, str] | None: ...
    def set_volume(self, level: int) -> str: ...
    def get_volume(self) -> int | None: ...
    def lock_screen(self) -> str: ...
    def is_locked(self) -> bool | None: ...
    def system_info(self) -> str: ...


@dataclass
class FakeDesktop:
    """Deterministic desktop model. ``failing`` names apps that refuse to start."""

    known_apps: set[str] = field(
        default_factory=lambda: {"notepad", "vscode", "browser", "terminal"}
    )
    failing: set[str] = field(default_factory=set)
    running: set[str] = field(default_factory=set)
    windows: list[str] = field(default_factory=lambda: ["Desktop"])
    focused: str | None = "Desktop"
    volume: int = 50
    locked: bool = False
    inputs: list[tuple[str, str]] = field(default_factory=list)
    screenshots: list[str] = field(default_factory=list)

    def open_app(self, name: str) -> str:
        key = name.lower().strip()
        if key not in self.known_apps:
            raise ValueError(f"unknown app {name!r}")
        if key in self.failing:
            return (
                f"Tried to launch {name}"  # lies like a real launcher can: verifier will catch it
            )
        self.running.add(key)
        title = f"{key.title()} - main"
        if title not in self.windows:
            self.windows.append(title)
        self.focused = title
        return f"Opened {name}"

    def process_running(self, name: str) -> bool | None:
        return name.lower().strip() in self.running

    def list_windows(self) -> list[str]:
        return list(self.windows)

    def focus_window(self, title: str) -> str:
        for w in self.windows:
            if title.lower() in w.lower():
                self.focused = w
                return f"Focused {w}"
        return f"No window matching {title!r}"

    def focused_window(self) -> str | None:
        return self.focused

    def screenshot(self, filename: str = "") -> str:
        path = filename or f"/tmp/fake_screenshot_{len(self.screenshots) + 1}.png"  # noqa: S108 - fake
        self.screenshots.append(path)
        return path

    def type_text(self, text: str) -> str:
        if self.locked:
            return "Screen is locked"
        self.inputs.append(("text", text))
        return f"Typed {len(text)} characters"

    def press_key(self, keys: str) -> str:
        if self.locked:
            return "Screen is locked"
        self.inputs.append(("key", keys))
        return f"Pressed {keys}"

    def last_input(self) -> tuple[str, str] | None:
        return self.inputs[-1] if self.inputs else None

    def set_volume(self, level: int) -> str:
        self.volume = max(0, min(100, int(level)))
        return f"Volume set to {self.volume}%"

    def get_volume(self) -> int | None:
        return self.volume

    def lock_screen(self) -> str:
        self.locked = True
        return "Screen locked"

    def is_locked(self) -> bool | None:
        return self.locked

    def system_info(self) -> str:
        return "FakeDesktop 1.0 | CPU 3% | RAM 40%"


class PrototypeDesktop:
    """Wraps ``jarvis/tools`` (pyautogui, osascript/PowerShell). Blocking calls are used only from
    the gateway's worker threads via ``asyncio.to_thread`` in the capability handlers."""

    def open_app(self, name: str) -> str:
        from jarvis.tools.app_control import open_app

        return open_app(name)

    def process_running(self, name: str) -> bool | None:
        try:
            import psutil  # optional; the prototype does not depend on it
        except ImportError:
            return None
        needle = name.lower()
        return any(
            needle in (p.info.get("name") or "").lower() for p in psutil.process_iter(["name"])
        )

    def list_windows(self) -> list[str]:
        from jarvis.tools.desktop import get_open_windows

        raw = get_open_windows()
        return [line.strip("- ").strip() for line in raw.splitlines() if line.strip()]

    def focus_window(self, title: str) -> str:
        from jarvis.tools.desktop import focus_window

        return focus_window(title)

    def focused_window(self) -> str | None:
        return None  # no reliable cross-platform signal in the prototype

    def screenshot(self, filename: str = "") -> str:
        from jarvis.tools.desktop import screenshot

        result = screenshot(filename)
        m = re.search(r"(\S+\.png)", result)
        return m.group(1) if m else result

    def type_text(self, text: str) -> str:
        from jarvis.tools.desktop import type_text

        return type_text(text)

    def press_key(self, keys: str) -> str:
        from jarvis.tools.desktop import press_key

        return press_key(keys)

    def last_input(self) -> tuple[str, str] | None:
        return None

    def set_volume(self, level: int) -> str:
        from jarvis.tools.system import set_volume

        return set_volume(level)

    def get_volume(self) -> int | None:
        from jarvis.tools.system import get_volume

        m = re.search(r"(\d+)", get_volume())
        return int(m.group(1)) if m else None

    def lock_screen(self) -> str:
        from jarvis.tools.system import lock_screen

        return lock_screen()

    def is_locked(self) -> bool | None:
        return None

    def system_info(self) -> str:
        from jarvis.tools.system import get_system_info

        return get_system_info()


async def in_thread(fn: Any, *args: Any) -> Any:
    """Run a blocking backend call off the event loop (Development Law 7)."""
    return await asyncio.to_thread(fn, *args)
