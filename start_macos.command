#!/bin/bash
# a7v Tether - Sony α7 V -> Lightroom Classic (macOS / Linux)
cd "$(dirname "$0")" || exit 1
if [ ! -d .venv ]; then
    echo "Einmalige Einrichtung …"
    python3 -m venv .venv || { echo "Python 3 nicht gefunden: https://www.python.org/downloads/"; read -r; exit 1; }
fi
.venv/bin/python -m pip install --quiet --disable-pip-version-check -r requirements.txt
exec .venv/bin/python -m a7v_tether "$@"
