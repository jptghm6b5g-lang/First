@echo off
rem Sony a7 V -> Lightroom Classic WLAN-Tethering (Windows)
cd /d "%~dp0"
if not exist .venv (
    echo Erstelle Python-Umgebung ...
    py -3 -m venv .venv || python -m venv .venv || (echo Python 3 nicht gefunden: https://www.python.org/downloads/ & pause & exit /b 1)
    .venv\Scripts\python -m pip install --quiet -r requirements.txt
)
if not exist config.toml copy config.example.toml config.toml >nul
.venv\Scripts\python -m a7v_tether %*
pause
