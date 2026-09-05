# Jarvis — Open Source AI Voice Assistant

Local Iron Man-style AI voice assistant for Windows.
Wake word → listen → think → speak → follow-up. Web UI at localhost:7860.

## Requirements

- **GPU**: NVIDIA GPU with 6GB+ VRAM (recommended) or CPU-only mode
- **RAM**: 16GB+ recommended
- **TTS**: Kokoro-82M (local, CPU)
- **STT**: Whisper small (local, CPU)
- **Wake word**: openWakeWord "hey_jarvis"
- **LLM**: Configurable providers via Web UI Settings:
  - `ollama` — Any Ollama model (default)
  - Add OpenAI-compatible providers (LM Studio, NVIDIA NIM, OpenAI, etc.) from the Config tab
- **Python**: 3.12+

## Quick Start

```bash
# 1. Clone
git clone <repo-url> jarvis
cd jarvis

# 2. One-click install
install.bat

# 3. Install Ollama and pull a model
# https://ollama.com
ollama pull qwen3:8b

# 4. Run
start.bat
```

Web UI opens at `http://localhost:7860`. Add providers in the **Config** tab.

## Key Files

| File | Purpose |
|------|---------|
| `jarvis/main.py` | Core orchestrator — wake, LLM routing, abort system, mute, keyboard listener, event broadcast |
| `jarvis/web.py` | FastAPI + WebSocket backend, provider/mute REST APIs |
| `jarvis/static/index.html` | Iron Man HUD Web UI |
| `jarvis/tts.py` | Kokoro TTS with polling-based interrupt |
| `jarvis/stt.py` | Whisper STT with speech-gated silence detection |
| `jarvis/wake.py` | openWakeWord listener |
| `jarvis/context.py` | Sliding window context manager |
| `jarvis/memory.py` | SQLite + ChromaDB fact storage |
| `jarvis/tools/router.py` | 31 tools mapped (including subagent) |
| `config.yaml` | All config — providers, TTS, STT, wake word, tools |
| `requirements.txt` | Dependencies |
| `tests/` | 32 tests |

## Architecture

```
Voice: Wake word → record_until_silence → transcribe → _process_request → speak_streamed
Web:   WebSocket → _process_request → broadcast to all clients
Keyboard: F2 → input() → _process_request → speak_streamed
```

## 31 Tools

Web search, weather, screen vision (OCR), mouse/keyboard automation, app control (30+ apps),
file operations (sandboxed), code execution (sandboxed), volume, brightness, clipboard,
power management, notifications, timers, subagent delegation, and more.

## Keyboard Controls

- **Esc** = abort everything
- **F2** = type a command in terminal
- **INSERT** = toggle mute/unmute

## License

MIT
