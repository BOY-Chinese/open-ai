@echo off
rem open-ai GUI - account manager (v2.4: branded single-process runtime)
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo [ERROR] venv not found. Run start.bat first.
    pause
    exit /b 1
)
rem prefer branded shim (open-ai-manager.exe); build on first use
if not exist "runtime\Scripts\open-ai-manager.exe" (
    ".venv\Scripts\python.exe" procname.py >nul 2>nul
)
if exist "runtime\Scripts\open-ai-manager.exe" (
    start "" "runtime\Scripts\open-ai-manager.exe" scripts\gui_account_manager.py
) else (
    start "" ".venv\Scripts\pythonw.exe" scripts\gui_account_manager.py
)
