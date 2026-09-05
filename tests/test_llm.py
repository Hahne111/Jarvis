# tests/test_llm.py
from unittest.mock import patch, MagicMock

STUB_CONFIG = {
    "llm": {
        "primary": "qwen3:8b",
        "heavy": "qwen/qwen3.5-35b-a3b",
        "primary_base_url": "http://localhost:11434",
        "heavy_base_url": "http://localhost:1234/v1",
        "temperature": 0.7,
    }
}


def _mock_openai_response(content="response"):
    resp = MagicMock()
    resp.choices = [MagicMock(message=MagicMock(content=content))]
    return resp


def test_chat_returns_string():
    """chat() should return a string response."""
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = _mock_openai_response("Good morning, sir.")
    with patch("jarvis.llm._load_config", return_value=STUB_CONFIG), \
         patch("openai.OpenAI", return_value=mock_client):
        from jarvis.llm import chat
        result = chat([{"role": "user", "content": "Hello"}])
        assert isinstance(result, str)
        assert "Good morning" in result


def test_chat_tries_lmstudio_first():
    """chat() should try LM Studio before falling back to Ollama."""
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = _mock_openai_response("from lmstudio")
    with patch("jarvis.llm._load_config", return_value=STUB_CONFIG), \
         patch("openai.OpenAI", return_value=mock_client):
        from jarvis.llm import chat
        result = chat([{"role": "user", "content": "test"}])
        assert result == "from lmstudio"
        mock_client.chat.completions.create.assert_called_once()


def test_chat_falls_back_to_ollama():
    """chat() should fall back to Ollama when LM Studio fails."""
    mock_ollama = MagicMock()
    mock_ollama.chat.return_value = MagicMock(
        message=MagicMock(content="from ollama")
    )
    with patch("jarvis.llm._load_config", return_value=STUB_CONFIG), \
         patch("openai.OpenAI", side_effect=Exception("connection refused")), \
         patch("ollama.Client", return_value=mock_ollama):
        from jarvis.llm import chat
        result = chat([{"role": "user", "content": "test"}])
        assert result == "from ollama"
