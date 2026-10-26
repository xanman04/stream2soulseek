@echo off
rem Start S2S from this repository's virtual environment. Double-click it in Explorer.
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo The virtual environment is missing. See README.md, "Development".
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" -m s2s.app
