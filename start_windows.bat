@echo off
rem a7v Tether - Sony a7 V -> Lightroom Classic (Windows)
cd /d "%~dp0"
if not exist .venv (
    echo Einmalige Einrichtung ...
    py -3 -m venv .venv 2>nul || python -m venv .venv || (echo Python 3 nicht gefunden: https://www.python.org/downloads/ & pause & exit /b 1)
)
.venv\Scripts\python -m pip install --quiet --disable-pip-version-check -r requirements.txt
.venv\Scripts\python -m a7v_tether %*
pause
