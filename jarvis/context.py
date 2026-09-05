from __future__ import annotations
from collections import deque
from pathlib import Path
import yaml

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"

def _load_config():
    with open(_CONFIG_PATH) as f:
        return yaml.safe_load(f)

from jarvis.llm import chat  # noqa: E402 — must be module-level for patching

class ContextManager:
    def __init__(
        self,
        max_messages: int | None = None,
        summarize_after: int | None = None,
    ):
        cfg = _load_config()["context"]
        self.max_messages = max_messages if max_messages is not None else cfg["max_messages"]
        self.summarize_after = summarize_after if summarize_after is not None else cfg["summarize_after"]
        if self.summarize_after > self.max_messages:
            raise ValueError(
                f"summarize_after ({self.summarize_after}) must be <= max_messages ({self.max_messages})"
            )
        self._window: deque[dict] = deque(maxlen=self.max_messages)
        self._summary: str | None = None

    def add(self, role: str, content: str) -> None:
        """Add a message. Auto-summarizes oldest messages when window fills."""
        self._window.append({"role": role, "content": content})
        if len(self._window) >= self.summarize_after:
            self._compress_oldest()

    def _compress_oldest(self) -> None:
        """Summarize and drop the oldest half of the window."""
        half = max(1, len(self._window) // 2)
        to_compress = [self._window.popleft() for _ in range(half)]
        text = "\n".join(f"{m['role']}: {m['content']}" for m in to_compress)
        prompt = f"Summarize this conversation in 2-3 sentences:\n{text}"
        summary = chat([{"role": "user", "content": prompt}])
        self._summary = f"[Earlier context: {summary}]"

    def get_messages(self) -> list[dict]:
        """Return context window, prefixed with summary if present."""
        messages = list(self._window)
        if self._summary:
            messages = [{"role": "system", "content": self._summary}] + messages
        return messages

    def clear(self) -> None:
        self._window.clear()
        self._summary = None
