"""Lightweight LLM chat function for internal tasks (context summarization, etc.)."""
from pathlib import Path
import yaml

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"


def _load_config():
    with open(_CONFIG_PATH) as f:
        return yaml.safe_load(f)


def chat(messages: list[dict]) -> str:
    """Send messages to the active LLM provider and return response text.

    Uses the same provider system as main.py — reads active_provider from config.
    Used for internal tasks like context summarization.
    """
    cfg = _load_config()
    llm_cfg = cfg.get("llm", {})
    temperature = llm_cfg.get("temperature", 0.7)
    active = llm_cfg.get("active_provider", "ollama")
    providers = llm_cfg.get("providers", {})
    provider = providers.get(active, {})
    ptype = provider.get("type", "ollama")

    if ptype == "openai":
        try:
            from openai import OpenAI
            client = OpenAI(
                base_url=provider["base_url"],
                api_key=provider.get("api_key", "lm-studio"),
                timeout=15.0,
            )
            resp = client.chat.completions.create(
                model=provider["model"],
                messages=messages,
                temperature=temperature,
            )
            return resp.choices[0].message.content or ""
        except Exception:
            pass  # fall through to Ollama

    # Ollama (default or fallback)
    import ollama
    ollama_cfg = providers.get("ollama", provider)
    model = ollama_cfg.get("model", "qwen3:8b")
    try:
        response = ollama.chat(model=model, messages=messages)
        return response.message.content or ""
    except Exception as e:
        return f"[LLM error: {e}]"
