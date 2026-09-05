"""MockProvider: scripted results for tests and offline development. No network, ever."""

from __future__ import annotations

from collections.abc import Callable

from core.models.provider import (
    IntelligenceProvider,
    Message,
    ProviderError,
    ProviderResult,
    ToolSpec,
    Usage,
)

Script = list[ProviderResult] | Callable[[list[Message]], ProviderResult]


class MockProvider(IntelligenceProvider):
    name = "mock"

    def __init__(self, script: Script | None = None) -> None:
        self._script = script if script is not None else []
        self.calls: list[dict] = []

    def available(self) -> bool:
        return True

    async def complete(
        self,
        messages: list[Message],
        *,
        system: str | None = None,
        tools: list[ToolSpec] | None = None,
        model: str | None = None,
        effort: str = "high",
        max_tokens: int = 16000,
    ) -> ProviderResult:
        self.calls.append(
            {
                "messages": list(messages),
                "system": system,
                "tools": [t.name for t in tools or []],
                "model": model,
                "effort": effort,
                "max_tokens": max_tokens,
            }
        )
        if callable(self._script):
            return self._script(messages)
        if not self._script:
            last = messages[-1].content if messages else ""
            return ProviderResult(text=f"[mock] {last}", usage=Usage(10, 5), model=model or "mock")
        if len(self.calls) > len(self._script):
            raise ProviderError("mock script exhausted")
        return self._script[len(self.calls) - 1]
