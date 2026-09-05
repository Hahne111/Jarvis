# tests/test_context.py
import pytest
from unittest.mock import patch

STUB_CONFIG = {
    "context": {
        "max_messages": 10,
        "summarize_after": 8,
        "max_tokens": 2500,
    }
}

def _make_ctx(**kwargs):
    with patch("jarvis.context._load_config", return_value=STUB_CONFIG):
        from jarvis.context import ContextManager
        return ContextManager(**kwargs)

def test_add_message_appends_to_window():
    ctx = _make_ctx()
    ctx.add("user", "hello")
    ctx.add("assistant", "hi there")
    messages = ctx.get_messages()
    assert len(messages) == 2
    assert messages[0]["role"] == "user"
    assert messages[1]["role"] == "assistant"

def test_window_caps_at_max_messages():
    with patch("jarvis.context.chat", return_value="summary"):
        ctx = _make_ctx(max_messages=4, summarize_after=4)
        for i in range(10):
            ctx.add("user", f"message {i}")
        messages = ctx.get_messages()
        assert len(messages) <= 4

def test_window_keeps_newest_messages():
    with patch("jarvis.context.chat", return_value="summary"):
        ctx = _make_ctx(max_messages=3, summarize_after=3)
        for i in range(5):
            ctx.add("user", f"message {i}")
        messages = ctx.get_messages()
        contents = [m["content"] for m in messages]
        assert "message 4" in contents  # newest kept
        assert "message 0" not in contents  # oldest dropped

def test_summarize_called_when_window_fills():
    """When window hits summarize_after, oldest messages are compressed."""
    with patch("jarvis.context.chat") as mock_chat, \
         patch("jarvis.context._load_config", return_value=STUB_CONFIG):
        mock_chat.return_value = "Summary of earlier conversation."
        from jarvis.context import ContextManager
        ctx = ContextManager(max_messages=6, summarize_after=4)
        for i in range(4):
            ctx.add("user", f"msg {i}")
            ctx.add("assistant", f"reply {i}")
        messages = ctx.get_messages()
        assert len(messages) < 8  # compressed

def test_clear_resets_window_and_summary():
    with patch("jarvis.context._load_config", return_value=STUB_CONFIG):
        from jarvis.context import ContextManager
        ctx = ContextManager()
        ctx.add("user", "test")
        ctx._summary = "some summary"
        ctx.clear()
        assert ctx.get_messages() == []
        assert ctx._summary is None

def test_get_messages_prefixes_summary_when_present():
    with patch("jarvis.context._load_config", return_value=STUB_CONFIG):
        from jarvis.context import ContextManager
        ctx = ContextManager()
        ctx._summary = "[Earlier: something happened]"
        ctx.add("user", "hello")
        messages = ctx.get_messages()
        assert messages[0]["role"] == "system"
        assert "Earlier" in messages[0]["content"]
        assert messages[1]["role"] == "user"
