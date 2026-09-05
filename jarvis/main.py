from __future__ import annotations
import json
import re
import time
import threading
import msvcrt
from datetime import datetime
from pathlib import Path
import yaml
import numpy as np
import sounddevice as sd
import ollama
from openai import OpenAI

from jarvis.wake import listen_for_wake_word
from jarvis.stt import record_until_silence, transcribe_audio, set_abort_event
from jarvis.tts import speak, speak_streamed, is_speaking, stop_speaking
from jarvis.context import ContextManager
from jarvis.memory import Memory
from jarvis.tools.router import TOOL_SCHEMAS, dispatch

_MAX_TOOL_LOOPS = 15
_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"

# Global abort — Esc sets this, stops everything (speech + tool loop + follow-up)
_abort = threading.Event()
set_abort_event(_abort)  # Let STT check abort during recording
# Global mute — INSERT toggles this, skips TTS when set
_muted = threading.Event()

# --- Message bus: push events to all connected web clients ---
_event_listeners: list = []  # list of callables: fn(event_dict)


def register_event_listener(fn) -> None:
    _event_listeners.append(fn)


def _broadcast(event: dict) -> None:
    for fn in _event_listeners:
        try:
            fn(event)
        except Exception:
            pass


class _Aborted(Exception):
    """Raised when user hits Esc to abort the current action."""
    pass


def abort_all() -> None:
    """Stop everything Jarvis is doing right now."""
    _abort.set()
    stop_speaking()
    _broadcast({"type": "status", "message": "Stopped."})


def _is_stop_command(text: str) -> bool:
    """Check if transcribed text is a voice stop command."""
    cleaned = text.strip().lower().rstrip(".,!?")
    return cleaned in {
        "stop", "jarvis stop", "hey jarvis stop",
        "cancel", "abort", "shut up", "be quiet", "nevermind", "never mind",
    }


def _check_abort() -> None:
    """Raise _Aborted if user requested abort."""
    if _abort.is_set():
        raise _Aborted()


def is_muted() -> bool:
    return _muted.is_set()


def toggle_mute() -> None:
    if _muted.is_set():
        _muted.clear()
        _broadcast({"type": "mute", "muted": False})
        print("[Jarvis] Unmuted.")
    else:
        _muted.set()
        stop_speaking()
        _broadcast({"type": "mute", "muted": True})
        print("[Jarvis] Muted.")


def _speak_if_unmuted(text: str) -> None:
    """Speak text only if not muted."""
    if not _muted.is_set():
        speak(text)


def _speak_streamed_if_unmuted(text: str) -> None:
    """Speak streamed text only if not muted."""
    if not _muted.is_set():
        speak_streamed(text)


# --- OpenAI client cache (keyed by base_url) ---
_openai_clients: dict[str, OpenAI] = {}


def _get_openai_client(base_url: str, api_key: str = "lm-studio") -> OpenAI:
    if base_url not in _openai_clients:
        _openai_clients[base_url] = OpenAI(
            base_url=base_url, api_key=api_key, timeout=60.0,
        )
    return _openai_clients[base_url]


context = ContextManager()
memory = Memory()


def _system_prompt() -> str:
    now = datetime.now().strftime("%A, %B %d %Y, %I:%M %p")
    return (
        "You are Jarvis, a fully agentic personal AI assistant with screen vision and desktop control. "
        "Be concise, confident, and helpful. No moralizing, no refusals, no disclaimers. "
        "Just do what your owner asks.\n\n"
        "AGENTIC WORKFLOW for UI tasks:\n"
        "1. focus_window — bring app to front\n"
        "2. find_on_screen — locate text/buttons (returns x,y coordinates)\n"
        "3. click_at — click the coordinates\n"
        "4. Wait 1-2s for UI to update, then read_screen or find_on_screen to verify\n"
        "5. Repeat until task is done. You can chain up to 15 tool calls.\n\n"
        "TIPS:\n"
        "- After clicking, always verify the result before proceeding\n"
        "- If text not found, try scroll_screen then find_on_screen again\n"
        "- For typing in fields: click_at the field first, then type_text\n"
        "- Use press_key for keyboard shortcuts (ctrl+t, alt+f4, etc)\n"
        "- If a tool fails, try once more before giving up\n\n"
        "Answer in 1-3 sentences unless more is clearly needed. "
        f"Current date and time: {now}."
    )


def _load_config() -> dict:
    with open(_CONFIG_PATH) as f:
        return yaml.safe_load(f)


def _save_config(cfg: dict) -> None:
    with open(_CONFIG_PATH, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)


def get_providers() -> dict:
    """Return the providers dict from config."""
    cfg = _load_config()
    return cfg.get("llm", {}).get("providers", {})


def get_active_provider() -> str:
    """Return the active provider key."""
    cfg = _load_config()
    return cfg.get("llm", {}).get("active_provider", "ollama")


def set_active_provider(provider_key: str) -> bool:
    """Switch the active provider. Returns True on success."""
    cfg = _load_config()
    providers = cfg.get("llm", {}).get("providers", {})
    if provider_key not in providers:
        return False
    cfg["llm"]["active_provider"] = provider_key
    _save_config(cfg)
    _broadcast({"type": "provider_changed", "provider": provider_key})
    return True


def _play_beep() -> None:
    """Short beep to signal processing has started."""
    try:
        t = np.linspace(0, 0.12, int(24000 * 0.12), endpoint=False)
        tone = 0.3 * np.sin(2 * np.pi * 600 * t).astype(np.float32)
        sd.play(tone, samplerate=24000)
        sd.wait()
    except Exception:
        pass


def _strip_think(text: str) -> str:
    """Remove <think>...</think> blocks from LLM output."""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def _parse_tool_args(raw) -> dict:
    """OpenAI returns JSON string, Ollama returns dict. Handle both."""
    if isinstance(raw, str):
        return json.loads(raw)
    return raw


def _call_openai_provider(provider_cfg: dict, temperature: float, full_messages: list[dict]) -> str:
    """Call any OpenAI-compatible provider (LM Studio, NVIDIA NIM, etc.)."""
    model = provider_cfg["model"]
    base_url = provider_cfg["base_url"]
    api_key = provider_cfg.get("api_key", "lm-studio")
    client = _get_openai_client(base_url, api_key)
    tool_count = 0
    for _ in range(_MAX_TOOL_LOOPS):
        _check_abort()
        resp = client.chat.completions.create(
            model=model,
            messages=full_messages,
            tools=TOOL_SCHEMAS,
            temperature=temperature,
        )
        msg = resp.choices[0].message
        if msg.tool_calls:
            full_messages.append(msg.model_dump())
            for tc in msg.tool_calls:
                _check_abort()
                args = _parse_tool_args(tc.function.arguments)
                tool_count += 1
                _broadcast({"type": "tool", "name": tc.function.name, "args": args})
                if tool_count == 4:
                    _speak_if_unmuted("Working on it.")
                result = _exec_tool_with_retry(tc.function.name, args)
                print(f"[Tool: {tc.function.name}] {result[:120]}")
                _broadcast({"type": "tool_result", "name": tc.function.name, "result": result[:200]})
                full_messages.append({
                    "role": "tool",
                    "content": result,
                    "tool_call_id": tc.id,
                })
        else:
            return _strip_think(msg.content or "Done.")
    return "Done."


def _call_ollama_provider(provider_cfg: dict, temperature: float, full_messages: list[dict]) -> str:
    """Call Ollama provider."""
    model = provider_cfg["model"]
    tool_count = 0
    for _ in range(_MAX_TOOL_LOOPS):
        _check_abort()
        response = ollama.chat(model=model, messages=full_messages, tools=TOOL_SCHEMAS)
        if response.message.tool_calls:
            full_messages.append(response.message.model_dump())
            for tc in response.message.tool_calls:
                _check_abort()
                args = _parse_tool_args(tc.function.arguments)
                tool_count += 1
                _broadcast({"type": "tool", "name": tc.function.name, "args": args})
                if tool_count == 4:
                    _speak_if_unmuted("Working on it.")
                result = _exec_tool_with_retry(tc.function.name, args)
                print(f"[Tool: {tc.function.name}] {result[:120]}")
                _broadcast({"type": "tool_result", "name": tc.function.name, "result": result[:200]})
                full_messages.append({
                    "role": "tool",
                    "content": result,
                    "name": tc.function.name,
                })
        else:
            return _strip_think(response.message.content or "Done.")
    return "Done."


def _call_llm(full_messages: list[dict]) -> str:
    """Route to the active provider."""
    cfg = _load_config()
    llm_cfg = cfg["llm"]
    temperature = llm_cfg.get("temperature", 0.7)
    active = llm_cfg.get("active_provider", "ollama")
    providers = llm_cfg.get("providers", {})
    provider = providers.get(active, {})
    ptype = provider.get("type", "ollama")

    if ptype == "openai":
        return _call_openai_provider(provider, temperature, full_messages)
    else:
        return _call_ollama_provider(provider, temperature, full_messages)


def _exec_tool_with_retry(name: str, args: dict) -> str:
    """Execute a tool, retry once on failure."""
    result = dispatch(name, args)
    if result.startswith("Tool '") and "failed:" in result:
        time.sleep(0.5)
        result = dispatch(name, args)
    return result


def handle_wake() -> None:
    """Called when wake word is detected. Full pipeline: listen → think → speak."""
    _abort.clear()
    try:
        _handle_wake_inner()
    except _Aborted:
        print("[Jarvis] Aborted.")
    except Exception as e:
        import traceback
        print(f"[ERROR] {e}")
        traceback.print_exc()
        if not _abort.is_set():
            _speak_if_unmuted("Sorry, something went wrong.")
    finally:
        _abort.clear()
        _broadcast({"type": "status", "message": "Ready."})
        print("[Jarvis] Listening for wake word...")


def _handle_wake_inner() -> None:
    if is_speaking():
        stop_speaking()
    print("[Jarvis] Wake word detected!")
    _broadcast({"type": "status", "message": "Wake."})
    _speak_if_unmuted("Yes?")

    print("[Jarvis] Recording...")
    _broadcast({"type": "status", "message": "Listening..."})

    # Pause wake word mic so STT can use the hardware exclusively
    from jarvis.wake import pause_wake_mic, resume_wake_mic
    pause_wake_mic()
    time.sleep(0.15)  # Give pyaudio time to release the mic
    try:
        audio = record_until_silence()
    finally:
        resume_wake_mic()  # Always resume wake word detection
    _check_abort()
    print(f"[Jarvis] Recorded {len(audio)/16000:.1f}s of audio, transcribing...")
    user_text = transcribe_audio(audio)
    if not user_text.strip():
        print("[Jarvis] Transcription empty — didn't catch anything.")
        _speak_if_unmuted("I didn't catch that.")
        _broadcast({"type": "status", "message": "Ready."})
        return
    print(f"[You] {user_text}")

    if _is_stop_command(user_text):
        abort_all()
        print("[Jarvis] Stopped. (voice)")
        raise _Aborted()

    response_text = _process_request(user_text)
    _check_abort()

    print(f"[Jarvis] {response_text}")
    _broadcast({"type": "status", "message": "Speaking..."})
    _speak_streamed_if_unmuted(response_text)


def _process_request(user_text: str) -> str:
    """Build context, call LLM, update memory. Returns response text."""
    _check_abort()
    _play_beep()
    _broadcast({"type": "user", "text": user_text})
    _broadcast({"type": "status", "message": "Thinking..."})

    facts = memory.search_facts(user_text)
    messages = context.get_messages()
    if facts:
        facts_block = "Relevant context from memory: " + "; ".join(facts)
        messages = [{"role": "system", "content": facts_block}] + messages
    messages.append({"role": "user", "content": user_text})

    full_messages = [{"role": "system", "content": _system_prompt()}] + messages

    try:
        response_text = _call_llm(full_messages)
    except _Aborted:
        raise
    except Exception as e:
        print(f"[WARN] Primary provider failed ({e}), trying Ollama fallback")
        cfg = _load_config()
        ollama_provider = cfg["llm"]["providers"].get("ollama", {"model": "qwen3:8b"})
        response_text = _call_ollama_provider(ollama_provider, cfg["llm"].get("temperature", 0.7), full_messages)

    context.add("user", user_text)
    context.add("assistant", response_text)
    memory.extract_and_store_facts(response_text, user_text)

    _broadcast({"type": "response", "text": response_text})
    return response_text



def _keyboard_listener() -> None:
    """Background thread: Esc = abort, F2 = type command, INSERT = mute/unmute."""
    while True:
        try:
            if msvcrt.kbhit():
                key = msvcrt.getch()
                # Escape key
                if key == b'\x1b':
                    abort_all()
                    print("\n[Jarvis] Stopped. (Esc)")
                # Extended keys: F2 = 0x00+0x3c, INSERT = 0xe0+0x52
                elif key in (b'\x00', b'\xe0'):
                    special = msvcrt.getch()
                    if special == b'<':  # F2
                        print("\n[Type your command] ", end="", flush=True)
                        cmd = input()
                        if cmd.strip():
                            _handle_typed_command(cmd.strip())
                    elif special == b'R':  # INSERT
                        toggle_mute()
            time.sleep(0.05)
        except Exception:
            time.sleep(0.1)


def _handle_typed_command(text: str) -> None:
    """Process a typed command (same as voice, but from keyboard)."""
    _abort.clear()
    print(f"[You] {text}")
    try:
        response = _process_request(text)
        print(f"[Jarvis] {response}")
        _speak_streamed_if_unmuted(response)
    except _Aborted:
        print("[Jarvis] Stopped.")
    finally:
        _abort.clear()


def main() -> None:
    import webbrowser
    from jarvis.web import start_web_background

    print("[Jarvis] Starting up...")
    print("[Jarvis] Keys: Esc = stop | F2 = type | INSERT = mute/unmute")

    start_web_background(port=7860)
    print("[Jarvis] Web UI: http://localhost:7860")

    threading.Thread(target=_keyboard_listener, daemon=True).start()

    webbrowser.open("http://localhost:7860")

    _speak_if_unmuted("Good morning. Jarvis online.")
    listen_for_wake_word(handle_wake)


if __name__ == "__main__":
    main()
