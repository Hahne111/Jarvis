#!/usr/bin/env bash
# J.A.R.V.I.S. launcher for macOS — counterpart of start.bat
cd "$(dirname "$0")"
if [ ! -f .venv/bin/activate ]; then
    echo "[ERROR] .venv not found. Run ./install.sh first."
    exit 1
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m jarvis.main
