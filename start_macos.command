#!/bin/bash
# Sony α7 V -> Lightroom Classic WLAN-Tethering (macOS / Linux)
cd "$(dirname "$0")" || exit 1
if [ ! -d .venv ]; then
    echo "Erstelle Python-Umgebung …"
    python3 -m venv .venv || { echo "Python 3 nicht gefunden: https://www.python.org/downloads/"; read -r; exit 1; }
    .venv/bin/python -m pip install --quiet -r requirements.txt
fi
[ -f config.toml ] || cp config.example.toml config.toml
exec .venv/bin/python -m a7v_tether "$@"
