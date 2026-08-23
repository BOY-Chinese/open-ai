@echo off
rem open-ai autostart: start gateway + no-window daemon (self-heal + daily signin)
rem 隐藏本控制台窗口
if not "%1"=="hidden" (
    start "" /min cmd /c "%~f0" hidden
    exit /b
)
cd /d "D:\app\dsh_plugin\open-ai"
rem start gateway (python + node backends)
start "" powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "D:\app\dsh_plugin\open-ai\start_hidden.ps1"
rem start no-window daemon (pythonw, no console popup): self-heal + daily signin
start "" "D:\app\dsh_plugin\open-ai\.venv\Scripts\pythonw.exe" "D:\app\dsh_plugin\open-ai\daemon.py"
exit /b 0
