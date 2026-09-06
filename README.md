# J.A.R.V.I.S.

Persönlicher, lokal laufender KI-Assistent nach dem `JARVIS Master Blueprint 1.0` (`docs/`). Das Repo enthält zwei Teile:

| Teil | Was | Status |
|------|-----|--------|
| **JARVIS Core** (`core/`, `adapters/`, `voice/`, `apps/`, `skills/`) | Deterministischer Kern: Permission Engine (P0–P6) → Execution Gateway → Verifier → Event Bus; Missionen, Memory, Agents mit austauschbaren Modell-Providern, Desktop/Workspace/Home-Adapter, Web-HUD, Skill Factory, signierte Releases | **1.0.0rc1** – Blueprint-Phasen 0–12 umgesetzt (`docs/STATUS.md`) |
| **Legacy-Prototyp** (`jarvis/`) | Ursprünglicher Voice-Prototyp (Wake → Whisper → LLM + Tools → Kokoro, Web-UI auf :7860) | unverändert (ADR-0001), wird capability-weise hinter den Core migriert |

![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
![Windows | macOS | Linux](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-0078D6)
![License](https://img.shields.io/badge/license-MIT-green)

---

## JARVIS Core

### Prinzipien

- **Core ist das Produkt, Modelle sind austauschbar.** Provider (Claude, Mock, keiner) liefern Tool-Calls nur als Vorschlag; ausgeführt wird ausschließlich über das Execution Gateway nach Allowlist.
- **Jede Seiteneffekt-Capability hat Risk Level und Verifier.** P0 beobachten … P3 Freigabe per Tap … P4 nur mit Passkey/Biometrie auf einem vertrauten Gerät … P6 wird nie ausgeführt. Die Policy kann sich nur verschärfen.
- **Alles ist ein Event.** HUD und API zeigen nur persistierte Events; nach einem Neustart werden Missionen und offene Freigaben aus dem Event-Log wiederhergestellt.
- **Kill Switch.** „Jarvis, stop“ oder `POST /kill` hält jeden Seiteneffekt an; Fortsetzen nur mit starkem Proof.
- **Local-first, keine Secrets im Repo.** Tokens und Keys kommen nur aus der Umgebung; `secret`-Memory erreicht nie einen Cloud-Prompt oder ein Event.

### Schnellstart

```bash
git clone https://github.com/Hahne111/Jarvis.git && cd Jarvis
./install.sh                         # macOS/Linux (Windows: install.bat) – venv + Abhängigkeiten
pip install -r core/requirements.txt # reicht, wenn nur der Core laufen soll (Python 3.12)

JARVIS_PROVIDER=none JARVIS_HOME=fake JARVIS_NEWS=fake python -m core
# HUD:   http://127.0.0.1:7870/hud/   Debug: http://127.0.0.1:7870/debug
```

Im HUD: „echo hello“, „what time is it“, „turn on the kitchen light“, „szene movie“, „wake desktop“ (wartet auf Freigabe), „Jarvis, stop“.

| Schalter | Werte | Zweck |
|----------|-------|-------|
| `JARVIS_PROVIDER` | `claude` (Default, braucht `pip install anthropic` + Key) · `mock` · `none` | Modell-Provider für Agent-Missionen |
| `JARVIS_DESKTOP` | `off` · `fake` · `prototype` | Desktop-Capabilities (echter PC über den Prototyp-Toolstack) |
| `JARVIS_HOME` | `off` · `fake` · `homeassistant` (`JARVIS_HA_URL`, `JARVIS_HA_TOKEN`) | Home Core, Szenen, Lock/Alarm (P4) – `docs/HOME_ASSISTANT.md` |
| `JARVIS_WOL_TARGETS` | JSON-Liste `[{"name","mac","host","port"}]` oder Pfad zu einer JSON-Datei | Wake-on-LAN (`power.wake`, P3) mit Erreichbarkeits-Verifier |
| `JARVIS_NEWS` | `off` · `fake` · `rss` (`JARVIS_NEWS_FEEDS`) | World Intelligence Globe |
| `JARVIS_PUSH` | `off` · `fake` · `webhook` | Push-Benachrichtigungen aufs Handy – `docs/REMOTE.md` |
| `JARVIS_CORE_HOST` | Mesh-IP (nie `0.0.0.0`) | Remote-Zugriff nur signiert; Enrollment `python -m core enroll phone` |
| `JARVIS_SCHEDULER` | `on` (Default) | Geplante Jobs, Watchdog, Daily Brief |

Voice-Loop gegen den Core: `python -m voice` (Mikrofon) oder `JARVIS_VOICE_FAKE=1 python -m voice` (Tastatur). Alle Variablen mit Platzhaltern in `.env.example`.

### Was der Core kann (Phasen 0–12)

- **Missionen & Freigaben** – Text/Voice-Kommando → deterministischer Fast-Path oder Agent → Gateway → Verifier; Approvals im HUD oder am Handy, Handover einer Mission zwischen Geräten.
- **Agents** – Router Fast/Smart/Deep, Budget, Subagent-Rollen (research/implementation/test/verification/security), Coding-Workflow in sandboxed Workspaces: eine Codeänderung gilt erst nach verifiziertem grünem Testlauf als erledigt.
- **Memory („What JARVIS Knows“)** – typisierte Items mit Sensitivität, Korrektur als neue Version, Vergessen, Privacy-Modi normal/private/guest.
- **Desktop, Workspace, Home** – Adapter mit Fake-Backends für Tests/CI und echten Backends (pyautogui-Prototyp, Home Assistant REST, WOL).
- **HUD** – buildfreies Web-HUD (ES-Module), Coding-Modus mit lokal gebündeltem Monaco (nie CDN), Canvas-Globus für Nachrichten mit Quellen/Confidence, responsiv + PWA fürs Handy mit Gerätesignatur (Ed25519 im Browser).
- **Proaktivität** – Scheduler mit Watchdog, Relevance-Gate für Push, Gewohnheits-Vorschläge (nie automatisch aktiv), Daily Brief.
- **Skill Factory** – Skill-SDK, statischer AST-Review (kein OS/Netz/Datei/Core-Zugriff), Sandbox-Tests, versionierte Installation mit Rollback; Install nur nach Owner-Freigabe.
- **Release 1.0** – Ed25519-signierte Archive (`.github/workflows/release.yml`), Updater mit Smoke-Test und Offline-Rollback, verschlüsseltes Backup (scrypt + AES-256-GCM), Regression-Suite mit Golden Scenarios – `docs/RELEASE.md`.

### Tests, Lint, CI

```bash
pytest -q                    # alles (Linux headless: xvfb-run -a pytest -q)
pytest -q tests/core         # Core (nur core/requirements.txt, Python 3.12)
pytest -q tests/regression   # Release/Updater/Backup, Security-Invarianten, Performance-Budgets, Golden Scenarios
ruff format --check . && ruff check .
```

CI (`.github/workflows/ci.yml`): Format + Lint, Secret-Scan (gitleaks), Unit-Tests 3.11, Core-Tests 3.12, Regression-Suite, Build-Smoke. `main` ist geschützt; Änderungen gehen über Feature-Branch → PR → grüne CI.

### Dokumentation

| Datei | Inhalt |
|-------|--------|
| `docs/SPEC.md` | Blueprint kompakt (Source of Truth: `docs/JARVIS_Master_Blueprint_1.0.pdf`) |
| `docs/SECURITY.md` / `docs/PERFORMANCE.md` | Normative Security-Regeln, Latenz-Budgets |
| `docs/STATUS.md` | Aktueller Stand, letzter Milestone, nächster Schritt |
| `docs/HUD_EVENTS.md` | Event-Vertrag zwischen Core und HUD |
| `docs/HOME_ASSISTANT.md` · `docs/REMOTE.md` · `docs/RELEASE.md` | Home-Anbindung, Remote/Mobile-Setup, Install/Update/Rollback/Backup |
| `docs/decisions/` | ADRs (Deltas zur PDF) |
| `CLAUDE.md` | Entwicklungsregeln und Repo-Layout |

---

## Legacy-Prototyp (`jarvis/`)

Der ursprüngliche Voice-Prototyp bleibt unverändert lauffähig (Web-UI auf **http://localhost:7860**) und führt seine Tools noch ohne Permission Engine/Verifier aus (dokumentiert in `docs/SECURITY.md` §8, ADR-0001). Die folgenden Abschnitte beschreiben ihn.

> Say **"Hey Jarvis"** → ask anything → Jarvis sees your screen, controls your apps, searches the web, and speaks back.

### Features

- **Voice-first interaction** — wake word detection ("Hey Jarvis"), natural speech input, streaming TTS responses
- **32 built-in tools** — desktop automation, screen reading (OCR), app control, web search, file ops, system commands
- **Agentic tool chaining** — up to 15 sequential tool calls per request (click → verify → scroll → click → done)
- **Screen vision** — reads your screen via OCR, finds UI elements by text, clicks buttons by coordinates
- **Multiple LLM providers** — Ollama (local), LM Studio, OpenAI, NVIDIA NIM, or any OpenAI-compatible API
- **Iron Man HUD web UI** — real-time chat, tool execution logs, provider management, settings, quick actions
- **Persistent memory** — remembers facts across sessions using SQLite + ChromaDB semantic search
- **Context management** — sliding window with auto-summarization to stay within token limits
- **One-click install** — `install.bat` / `install.sh` sets up everything, `start.bat` / `start.sh` launches

---

### Quick Start

#### Prerequisites

- **Windows 10/11** or **macOS** (Apple Silicon or Intel) — see [macOS](#macos) below
- **Python 3.11+** (3.12 recommended) — [python.org/downloads](https://www.python.org/downloads/)
- **Ollama** — [ollama.com/download](https://ollama.com/download) (for local LLM)

#### 1. Install

```bash
git clone https://github.com/Hahne111/Jarvis.git
cd Jarvis
install.bat
```

This will:
1. Create a Python virtual environment (`.venv`)
2. Install all 18 dependencies
3. Download wake word ONNX models

#### 2. Pull an LLM model

```bash
ollama pull qwen3:8b
```

Any Ollama model works — `qwen3:8b` is a good balance of speed and quality. Smaller options: `qwen3:4b`, `llama3.2:3b`. Larger: `qwen3:14b`, `llama3.1:8b`.

#### 3. Launch

```bash
start.bat       # Windows
./start.sh      # macOS
```

Jarvis will:
- Start the voice listener (wake word detection)
- Open the web UI at **http://localhost:7860**
- Speak "Good morning. Jarvis online."

#### 4. Use it

| Method | How |
|--------|-----|
| **Voice** | Say **"Hey Jarvis"** → wait for "Yes?" → speak your command |
| **Web UI** | Type in the chat bar at the bottom → click Send |
| **Keyboard** | Press **F2** → type command in terminal → Enter |

---

### macOS

Tested target: iMac with Apple Silicon. Whisper runs on CPU (`stt.device: auto` picks CPU when there is no CUDA), Kokoro TTS runs on CPU, Ollama uses the GPU via Metal.

#### Requirements

- [Homebrew](https://brew.sh), then `brew install python@3.12 portaudio espeak-ng` (install.sh does this for you)
- [Ollama](https://ollama.com/download) running (`ollama serve`) with a pulled model (`ollama pull qwen3:8b`)

#### Install & start

```bash
git clone https://github.com/Hahne111/Jarvis.git
cd Jarvis
./install.sh
./start.sh
```

#### Permissions (System Settings → Privacy & Security)

Grant these to the terminal app you start Jarvis from (Terminal, iTerm, VS Code):

| Permission | Needed for |
|------------|------------|
| **Microphone** | wake word + speech input |
| **Accessibility** | `type_text`, `press_key`, `click_at`, `focus_window`, `lock_screen` |
| **Screen Recording** | `read_screen`, `find_on_screen`, `screenshot`, window titles in `get_open_windows` |

macOS prompts on first use; if a tool silently does nothing, check the permission is enabled and restart Jarvis.

#### macOS notes

- Keys in the terminal: **Esc** = stop, **F2** or **t** = type a command, **F3** or **m** = mute/unmute (there is no Insert key on Mac keyboards).
- `set_brightness` needs `brew install brightness`.
- OCR uses Apple's Vision framework (no tesseract needed); `press_key` translates `win` → `cmd` and `alt` → `option`.

---

### Architecture

```
┌─────────────┐     ┌──────────────┐     ┌──────────────┐
│  Wake Word  │────▶│   STT        │────▶│    LLM       │
│  (pyaudio)  │     │ (Whisper)    │     │ (Ollama/API) │
└─────────────┘     └──────────────┘     └──────┬───────┘
                                                │
┌─────────────┐     ┌──────────────┐     ┌──────▼───────┐
│   Web UI    │◀───▶│  Event Bus   │◀────│  Tool Router │
│  (FastAPI)  │     │ (WebSocket)  │     │  (32 tools)  │
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

### Tools (32)

#### Desktop Automation & Vision
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

#### Apps & Web
| Tool | Description |
|------|-------------|
| `open_app` | Launch apps by name (30+ mapped: Chrome, Discord, VS Code, Spotify...) |
| `open_url` | Open URL in default browser |
| `kill_process` | Close a running process |
| `web_search` | Search DuckDuckGo, return top results |
| `fetch_page` | Fetch and extract text from a URL |
| `get_weather` | Current weather for any location |

#### System Control
| Tool | Description |
|------|-------------|
| `set_volume` / `get_volume` | Control system volume (0-100%) |
| `set_brightness` | Screen brightness (laptops) |
| `get_system_info` | CPU, RAM, disk usage |
| `get_clipboard` / `set_clipboard` | Read/write clipboard |
| `show_notification` | Desktop notification (Windows toast / macOS Notification Center) |
| `set_timer` | Countdown timer with notification |
| `lock_screen` | Lock workstation |
| `power_command` | Shutdown, restart, or sleep |

#### Files & Code
| Tool | Description |
|------|-------------|
| `read_file` / `write_file` | Read/write files (sandboxed to allowed paths) |
| `list_files` | List directory contents |
| `run_python` | Execute Python code in sandbox (10s timeout) |
| `delegate_task` | Send subtask to local LLM for parallel processing |

---

### Web UI

The web interface runs at `http://localhost:7860` and provides:

- **Real-time chat** — voice and typed conversations displayed together
- **Arc Reactor status** — animated indicator (listening / thinking / speaking / idle)
- **Tool execution panel** — see every tool call and result as it happens
- **Live event feed** — full timeline of all events
- **Quick actions** — one-click buttons for weather, screenshot, system info, etc.
- **Provider management** — add, edit, delete, and switch LLM providers
- **Settings** — configure TTS voice/speed, STT model, wake word threshold, LLM temperature

---

### Configuration

All settings are in `config.yaml`. You can also change most of them from the web UI's Config tab.

#### Adding a Cloud LLM Provider

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

#### STT Options

```yaml
stt:
  model: small      # tiny (fastest) | base | small (default) | medium | large-v3 (best)
  device: auto      # auto (CUDA if available, else CPU) | cuda | cpu — falls back to CPU if CUDA fails
  compute_type: int8
```

#### TTS Options

```yaml
tts:
  voice: af_heart   # Kokoro voice name
  speed: 1.1        # Speech speed (0.5 = slow, 2.0 = fast)
```

---

### Controls

| Input | Action |
|-------|--------|
| **"Hey Jarvis"** (idle) | Wake up — starts listening for your command |
| **"Hey Jarvis"** (busy) | Stop — interrupts current task/speech |
| **"Stop"** / **"Cancel"** / **"Shut up"** | Voice stop command (after wake word) |
| **Esc** | Keyboard abort — stops everything instantly |
| **F2** (macOS also **t**) | Type a command in the terminal |
| **Insert** (Windows) / **F3** or **m** (macOS) | Toggle mute (TTS on/off) |
| **ABORT button** (web UI) | Stop current task |
| **"stop"** (typed in web chat) | Stop current task |

---

### Project Structure

```
core/        JARVIS Core: events, missions, permissions, capabilities, verifier, intents, api, models, agents, memory, devices, notify, news, scheduler, proactive, skills, release/updater/backup
adapters/    desktop, workspace, home (Fake + echte Backends)
voice/       Voice 0.1 gegen den Core
apps/        Web-HUD (apps/desktop/web)
skills/      Skill-SDK + Beispiel-Skill
tests/       Prototyp-Tests, tests/core, tests/regression
docs/        Blueprint, SPEC, SECURITY, PERFORMANCE, STATUS, RELEASE, REMOTE, HOME_ASSISTANT, ADRs
infra/       Docker Compose (PostgreSQL + pgvector)
release/     nur der öffentliche Release-Schlüssel
```

Legacy-Prototyp:

```
jarvis/
├── config.yaml          # All configuration
├── install.bat          # One-click installer (Windows)
├── start.bat            # Launcher (Windows)
├── install.sh           # One-click installer (macOS)
├── start.sh             # Launcher (macOS)
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

### Troubleshooting

| Issue | Fix |
|-------|-----|
| `cublas64_12.dll not found` | Normal on systems without CUDA toolkit. STT auto-falls back to CPU. |
| Wake word not detecting | Lower `threshold` in config (try 0.3). Check your mic is set as default input. |
| No sound from Jarvis | Check `is_muted` state (Insert / F3 toggles). Check system audio output. |
| STT recording hangs | Mic conflict resolved — wake word mic auto-pauses during recording. |
| Ollama connection error | Make sure Ollama is running (`ollama serve`). Check `base_url` in config. |
| Web UI not updating | Open browser console (F12) — check for `[WS]` log messages. Refresh page. |

---

### Tech Stack

| Component | Technology |
|-----------|------------|
| Wake Word | [openWakeWord](https://github.com/dscripka/openWakeWord) (ONNX) |
| Speech-to-Text | [faster-whisper](https://github.com/SYSTRAN/faster-whisper) |
| Text-to-Speech | [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) |
| LLM | [Ollama](https://ollama.com/) / OpenAI-compatible APIs |
| Memory | SQLite + [ChromaDB](https://www.trychroma.com/) |
| Web UI | [FastAPI](https://fastapi.tiangolo.com/) + WebSocket |
| Desktop Control | [PyAutoGUI](https://pyautogui.readthedocs.io/) + PowerShell OCR (Windows) / Apple Vision OCR (macOS) |

---

## License

MIT
