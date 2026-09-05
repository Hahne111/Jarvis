#!/usr/bin/env bash
# J.A.R.V.I.S. installer for macOS (Apple Silicon / Intel) — counterpart of install.bat
set -euo pipefail
cd "$(dirname "$0")"

echo
echo " ============================================"
echo "  J.A.R.V.I.S. - AI Voice Assistant Installer"
echo " ============================================"
echo

# --- Python 3.11+ (3.12 recommended) ---
PYTHON=""
for candidate in python3.12 python3.13 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
        PYTHON="$candidate"
        break
    fi
done
if [ -z "$PYTHON" ]; then
    echo "[ERROR] Python 3.11+ not found. Install it with: brew install python@3.12"
    exit 1
fi
echo "Using $($PYTHON --version) ($(command -v "$PYTHON"))"

# --- Homebrew dependencies (macOS) ---
if [ "$(uname -s)" = "Darwin" ]; then
    if ! command -v brew >/dev/null 2>&1; then
        echo "[ERROR] Homebrew not found. Install from https://brew.sh and re-run."
        exit 1
    fi
    for pkg in portaudio espeak-ng; do   # portaudio: pyaudio/sounddevice, espeak-ng: Kokoro phonemizer fallback
        if ! brew list --versions "$pkg" >/dev/null 2>&1; then
            echo "[brew] Installing $pkg..."
            brew install "$pkg"
        fi
    done
    if ! command -v ollama >/dev/null 2>&1; then
        echo "[!] Ollama not found. Install from https://ollama.com/download (or: brew install ollama)"
    fi
    # Let pip build pyaudio against Homebrew's portaudio
    export CFLAGS="-I$(brew --prefix portaudio)/include ${CFLAGS:-}"
    export LDFLAGS="-L$(brew --prefix portaudio)/lib ${LDFLAGS:-}"
fi

echo
echo "[1/4] Creating virtual environment..."
"$PYTHON" -m venv .venv

echo "[2/4] Activating environment..."
# shellcheck disable=SC1091
source .venv/bin/activate

echo "[3/4] Installing dependencies (this may take a few minutes)..."
pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet

echo "[4/4] Downloading wake word models..."
python -c "from openwakeword import utils; utils.download_models()" 2>/dev/null || true
python - <<'PY' 2>/dev/null || true
import openwakeword, os, urllib.request
d = os.path.join(os.path.dirname(openwakeword.__file__), 'resources', 'models')
os.makedirs(d, exist_ok=True)
for f in ['melspectrogram.onnx', 'embedding_model.onnx']:
    p = os.path.join(d, f)
    if not os.path.exists(p):
        urllib.request.urlretrieve('https://github.com/dscripka/openWakeWord/raw/main/openwakeword/resources/models/' + f, p)
PY

echo
echo " ============================================"
echo "  Installation complete!"
echo " ============================================"
echo
echo " To start Jarvis, run:  ./start.sh"
echo " Or:  source .venv/bin/activate && python -m jarvis.main"
echo
echo " Web UI will open at: http://localhost:7860"
echo " macOS: grant Microphone, Accessibility and Screen Recording"
echo " permissions to your terminal app when prompted (System Settings > Privacy & Security)."
echo
