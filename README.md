# J.A.R.V.I.S. — Local AI Voice Assistant

A fully local, agentic AI voice assistant with wake word detection, screen vision, desktop control, and an Iron Man-inspired web UI. Runs entirely on your machine — no cloud required.

> Say **"Hey Jarvis"** → ask anything → Jarvis sees your screen, controls your apps, searches the web, and speaks back.

![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
![Windows](https://img.shields.io/badge/platform-Windows-0078D6)
![License](https://img.shields.io/badge/license-MIT-green)

---

## Features

- **Voice-first interaction** — wake word detection ("Hey Jarvis"), natural speech input, streaming TTS responses
- **31 built-in tools** — desktop automation, screen reading (OCR), app control, web search, file ops, system commands
- **Agentic tool chaining** — up to 15 sequential tool calls per request (click → verify → scroll → click → done)
- **Screen vision** — reads your screen via OCR, finds UI elements by text, clicks buttons by coordinates
- **Multiple LLM providers** — Ollama (local), LM Studio, OpenAI, NVIDIA NIM, or any OpenAI-compatible API
- **Iron Man HUD web UI** — real-time chat, tool execution logs, provider management, settings, quick actions
- **Persistent memory** — remembers facts across sessions using SQLite + ChromaDB semantic search
- **Context management** — sliding window with auto-summarization to stay within token limits
- **One-click install** — `install.bat` sets up everything, `start.bat` launches

---
Coffe helps: https://buymeacoffee.com/azzren
---

## Quick Start

### Prerequisites

- **Windows 10/11**
- **Python 3.11+** — [python.org/downloads](https://www.python.org/downloads/)
- **Ollama** — [ollama.com/download](https://ollama.com/download) (for local LLM)

### 1. Install

```bash
git clone https://github.com/PanPenek/jarvis.git
cd jarvis
install.bat
```

This will:
1. Create a Python virtual environment (`.venv`)
2. Install all 18 dependencies
3. Download wake word ONNX models

### 2. Pull an LLM model

```bash
ollama pull qwen3:8b
```

Any Ollama model works — `qwen3:8b` is a good balance of speed and quality. Smaller options: `qwen3:4b`, `llama3.2:3b`. Larger: `qwen3:14b`, `llama3.1:8b`.

### 3. Launch

```bash
start.bat
```

Jarvis will:
- Start the voice listener (wake word detection)
- Open the web UI at **http://localhost:7860**
- Speak "Good morning. Jarvis online."

### 4. Use it

| Method | How |
|--------|-----|
| **Voice** | Say **"Hey Jarvis"** → wait for "Yes?" → speak your command |
| **Web UI** | Type in the chat bar at the bottom → click Send |
| **Keyboard** | Press **F2** → type command in terminal → Enter |

---

## Architecture

```
┌─────────────┐     ┌──────────────┐     ┌──────────────┐
│  Wake Word  │────▶│   STT        │────▶│    LLM       │
│  (pyaudio)  │     │ (Whisper)    │     │ (Ollama/API) │
└─────────────┘     └──────────────┘     └──────┬───────┘
                                                │
┌─────────────┐     ┌──────────────┐     ┌──────▼───────┐
│   Web UI    │◀───▶│  Event Bus   │◀────│  Tool Router │
│  (FastAPI)  │     │ (WebSocket)  │     │  (31 tools)  │
└─────────────┘     └──────────────┘     └──────┬───────┘
                                                │
                    ┌──────────────┐     ┌──────▼───────┐
                    │   Memory     │     │    TTS       │
                    │(SQLite+Chroma│     │  (Kokoro)    │
                    └──────────────┘     └──────────────┘
```

**Pipeline:** Wake word → Record speech → Transcribe (Whisper) → LLM with tools → Speak response (Kokoro)

**All components are local.** No data leaves your machine unless you configure a cloud LLM provider.

---

## Tools (31)

### Desktop Automation & Vision
| Tool | Description |
|------|-------------|
| `read_screen` | Screenshot + OCR — returns all visible text with coordinates |
| `find_on_screen` | Find text/button on screen, returns (x, y) coordinates |
| `click_at` | Click at screen coordinates (left/right/double click) |
| `type_text` | Type text at current cursor position |
| `press_key` | Keyboard shortcuts (ctrl+c, alt+tab, win+d, etc.) |
| `scroll_screen` | Scroll up/down |
| `move_mouse` | Move cursor to coordinates |
| `focus_window` | Bring window to foreground by title |
| `get_open_windows` | List all open window titles |
| `media_control` | Play/pause/next/previous/mute media |
| `screenshot` | Save screenshot to Desktop |

### Apps & Web
| Tool | Description |
|------|-------------|
| `open_app` | Launch apps by name (30+ mapped: Chrome, Discord, VS Code, Spotify...) |
| `open_url` | Open URL in default browser |
| `kill_process` | Close a running process |
| `web_search` | Search DuckDuckGo, return top results |
| `fetch_page` | Fetch and extract text from a URL |
| `get_weather` | Current weather for any location |

### System Control
| Tool | Description |
|------|-------------|
| `set_volume` / `get_volume` | Control system volume (0-100%) |
| `set_brightness` | Screen brightness (laptops) |
| `get_system_info` | CPU, RAM, disk usage |
| `get_clipboard` / `set_clipboard` | Read/write clipboard |
| `show_notification` | Windows toast notification |
| `set_timer` | Countdown timer with notification |
| `lock_screen` | Lock workstation |
| `power_command` | Shutdown, restart, or sleep |

### Files & Code
| Tool | Description |
|------|-------------|
| `read_file` / `write_file` | Read/write files (sandboxed to allowed paths) |
| `list_files` | List directory contents |
| `run_python` | Execute Python code in sandbox (10s timeout) |
| `delegate_task` | Send subtask to local LLM for parallel processing |

---

## Web UI

The web interface runs at `http://localhost:7860` and provides:

- **Real-time chat** — voice and typed conversations displayed together
- **Arc Reactor status** — animated indicator (listening / thinking / speaking / idle)
- **Tool execution panel** — see every tool call and result as it happens
- **Live event feed** — full timeline of all events
- **Quick actions** — one-click buttons for weather, screenshot, system info, etc.
- **Provider management** — add, edit, delete, and switch LLM providers
- **Settings** — configure TTS voice/speed, STT model, wake word threshold, LLM temperature

---

## Configuration

All settings are in `config.yaml`. You can also change most of them from the web UI's Config tab.

### Adding a Cloud LLM Provider

Open the web UI → Config tab → **Add Provider**, or edit `config.yaml`:

```yaml
llm:
  active_provider: nvidia   # Switch to this provider
  providers:
    ollama:
      type: ollama
      label: Ollama (Local)
      model: qwen3:8b
    nvidia:
      type: openai
      label: NVIDIA NIM
      model: meta/llama-3.1-70b-instruct
      base_url: https://integrate.api.nvidia.com/v1
      api_key: nvapi-xxxx
    lmstudio:
      type: openai
      label: LM Studio
      model: local-model
      base_url: http://localhost:1234/v1
```

Any OpenAI-compatible API works (LM Studio, vLLM, Together AI, Groq, etc.)

### STT Options

```yaml
stt:
  model: small      # tiny (fastest) | base | small (default) | medium | large-v3 (best)
  device: cuda      # cuda (GPU) or cpu — auto-falls back to CPU if CUDA fails
  compute_type: int8
```

### TTS Options

```yaml
tts:
  voice: af_heart   # Kokoro voice name
  speed: 1.1        # Speech speed (0.5 = slow, 2.0 = fast)
```

---

## Controls

| Input | Action |
|-------|--------|
| **"Hey Jarvis"** (idle) | Wake up — starts listening for your command |
| **"Hey Jarvis"** (busy) | Stop — interrupts current task/speech |
| **"Stop"** / **"Cancel"** / **"Shut up"** | Voice stop command (after wake word) |
| **Esc** | Keyboard abort — stops everything instantly |
| **F2** | Type a command in the terminal |
| **Insert** | Toggle mute (TTS on/off) |
| **ABORT button** (web UI) | Stop current task |
| **"stop"** (typed in web chat) | Stop current task |

---

## Project Structure

```
jarvis/
├── config.yaml          # All configuration
├── install.bat          # One-click installer
├── start.bat            # Launcher
├── requirements.txt     # Python dependencies
│
├── jarvis/
│   ├── main.py          # Core orchestrator — pipeline, abort, events
│   ├── wake.py          # Wake word detection (openWakeWord)
│   ├── stt.py           # Speech-to-text (faster-whisper)
│   ├── tts.py           # Text-to-speech (Kokoro)
│   ├── web.py           # Web UI server (FastAPI + WebSocket)
│   ├── context.py       # Sliding window context manager
│   ├── memory.py        # Long-term memory (SQLite + ChromaDB)
│   ├── llm.py           # Internal LLM calls (summarization)
│   ├── static/
│   │   └── index.html   # Iron Man HUD web interface
│   └── tools/
│       ├── router.py    # Tool registry and dispatch
│       ├── desktop.py   # Screen/mouse/keyboard automation
│       ├── app_control.py  # App launching, URL opening
│       ├── web_search.py   # DuckDuckGo, weather, page fetch
│       ├── system.py    # Volume, brightness, clipboard, power
│       ├── file_ops.py  # Sandboxed file read/write/list
│       ├── code_exec.py # Python code execution sandbox
│       └── subagent.py  # Task delegation to local LLM
│
└── tests/               # Unit tests (pytest)
```

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| `cublas64_12.dll not found` | Normal on systems without CUDA toolkit. STT auto-falls back to CPU. |
| Wake word not detecting | Lower `threshold` in config (try 0.3). Check your mic is set as default input. |
| No sound from Jarvis | Check `is_muted` state (Insert key toggles). Check system audio output. |
| STT recording hangs | Mic conflict resolved — wake word mic auto-pauses during recording. |
| Ollama connection error | Make sure Ollama is running (`ollama serve`). Check `base_url` in config. |
| Web UI not updating | Open browser console (F12) — check for `[WS]` log messages. Refresh page. |

---

## Tech Stack

| Component | Technology |
|-----------|------------|
| Wake Word | [openWakeWord](https://github.com/dscripka/openWakeWord) (ONNX) |
| Speech-to-Text | [faster-whisper](https://github.com/SYSTRAN/faster-whisper) |
| Text-to-Speech | [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) |
| LLM | [Ollama](https://ollama.com/) / OpenAI-compatible APIs |
| Memory | SQLite + [ChromaDB](https://www.trychroma.com/) |
| Web UI | [FastAPI](https://fastapi.tiangolo.com/) + WebSocket |
| Desktop Control | [PyAutoGUI](https://pyautogui.readthedocs.io/) + PowerShell OCR |

---

## License

MIT
