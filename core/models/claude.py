"""ClaudeProvider: Anthropic Messages API adapter behind IntelligenceProvider (Commit 010).

Skeleton for Phase 3: request shaping and response parsing are complete and unit-tested with a
fake client; the real SDK is imported lazily (``pip install anthropic``). Rules:
- The API key never enters prompts, logs or events. It is resolved by the SDK from the environment
  (ANTHROPIC_API_KEY / `ant auth login` profile) or by an injected ``key_provider`` (credential
  broker, SECURITY.md §2 rule 2) and passed to the client constructor only.
- Tool calls are returned as *proposals*; nothing is executed here.
- Adaptive thinking + ``output_config.effort``; server-side refusal fallbacks enabled by default.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from core.models.provider import (
    IntelligenceProvider,
    Message,
    ProviderError,
    ProviderResult,
    ProviderUnavailable,
    ToolCallProposal,
    ToolSpec,
    Usage,
)
from core.models.router import DEFAULT_MODELS, EFFORT_LEVELS, ModelSpec

log = logging.getLogger("jarvis.core.models.claude")

DEFAULT_MODEL = "claude-opus-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ClaudeProvider(IntelligenceProvider):
    name = "claude"

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        client: Any | None = None,
        key_provider: Callable[[], str | None] | None = None,
        refusal_fallbacks: bool = True,
        thinking_display: str = "omitted",
        models: tuple[ModelSpec, ...] = DEFAULT_MODELS,
    ) -> None:
        self._model = model
        self._client = client
        self._key_provider = key_provider
        self._fallbacks = refusal_fallbacks
        self._thinking_display = thinking_display
        self._specs = {m.id: m for m in models}

    # -- availability --------------------------------------------------------------------------

    def available(self) -> bool:
        if self._client is not None:
            return True
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return False
        return True

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                from anthropic import AsyncAnthropic
            except ImportError as exc:
                raise ProviderUnavailable(
                    "anthropic SDK not installed (pip install anthropic)"
                ) from exc
            key = self._key_provider() if self._key_provider else None
            # A None key lets the SDK resolve credentials itself (env / auth profile).
            self._client = AsyncAnthropic(api_key=key) if key else AsyncAnthropic()
        return self._client

    # -- request shaping -----------------------------------------------------------------------

    def build_request(
        self,
        messages: list[Message],
        *,
        system: str | None,
        tools: list[ToolSpec] | None,
        model: str | None,
        effort: str,
        max_tokens: int,
    ) -> dict[str, Any]:
        if effort not in EFFORT_LEVELS:
            raise ValueError(f"effort must be one of {EFFORT_LEVELS}")
        if not messages or messages[0].role != "user":
            raise ValueError("conversation must start with a user message")
        req: dict[str, Any] = {
            "model": model or self._model,
            "max_tokens": max_tokens,
            "messages": [self._to_api_message(m) for m in messages],
            "thinking": {"type": "adaptive", "display": self._thinking_display},
            "output_config": {"effort": effort},
        }
        if system:
            req["system"] = [
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ]
        if tools:
            req["tools"] = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.input_schema,
                    "strict": True,
                }
                for t in tools
            ]
        if self._fallbacks:
            req["betas"] = [FALLBACK_BETA]
            req["fallbacks"] = "default"
        return req

    @staticmethod
    def _to_api_message(m: Message) -> dict[str, Any]:
        if m.role == "tool":
            return {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content}
                ],
            }
        return {"role": m.role, "content": m.content}

    # -- completion ----------------------------------------------------------------------------

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
        req = self.build_request(
            messages, system=system, tools=tools, model=model, effort=effort, max_tokens=max_tokens
        )
        client = self._get_client()
        try:
            api = client.beta.messages if self._fallbacks else client.messages
            response = await api.create(**req)
        except Exception as exc:  # SDK errors are provider errors; never leak request details
            raise ProviderError(f"{type(exc).__name__}: {exc}") from exc
        return self.parse_response(response, requested_model=req["model"])

    def parse_response(self, response: Any, *, requested_model: str) -> ProviderResult:
        texts: list[str] = []
        calls: list[ToolCallProposal] = []
        for block in getattr(response, "content", []) or []:
            btype = getattr(block, "type", None)
            if btype == "text":
                texts.append(block.text)
            elif btype == "tool_use":
                calls.append(
                    ToolCallProposal(
                        name=block.name, args=dict(block.input or {}), call_id=block.id
                    )
                )
        u = getattr(response, "usage", None)
        usage = Usage(
            input_tokens=int(getattr(u, "input_tokens", 0) or 0),
            output_tokens=int(getattr(u, "output_tokens", 0) or 0),
            cache_read_tokens=int(getattr(u, "cache_read_input_tokens", 0) or 0),
            cache_write_tokens=int(getattr(u, "cache_creation_input_tokens", 0) or 0),
        )
        served = getattr(response, "model", None) or requested_model
        stop = getattr(response, "stop_reason", None) or "end_turn"
        refused = stop == "refusal"
        category = None
        if refused:
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details is not None else None
        spec = self._specs.get(served)
        cost = spec.cost(usage) if spec else 0.0
        return ProviderResult(
            text="".join(texts),
            tool_calls=tuple(calls),
            usage=usage,
            model=served,
            stop_reason=stop,
            refused=refused,
            refusal_category=category,
            cost_usd=cost,
        )
