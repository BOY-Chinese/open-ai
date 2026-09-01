@echo off
rem open-ai autostart: launch process broker (gateway + trae node + tasks)
rem v2.4: all child processes are spawned and hosted by the Broker
rem       (Job Object tree + IPC heartbeat + watchdog respawn).
if not "%1"=="hidden" (
    start "" /min cmd /c "%~f0" hidden
    exit /b
)
cd /d "D:\app\dsh_plugin\open-ai"
if not exist "runtime\Scripts\open-ai-daemon.exe" (
    rem runtime not built yet: build it first (idempotent)
    ".venv\Scripts\python.exe" procname.py >nul 2>nul
)
rem idempotent: bootstrap start returns immediately if Broker already runs
start "" /b "D:\app\dsh_plugin\open-ai\runtime\Scripts\open-ai-daemon.exe" "D:\app\dsh_plugin\open-ai\bootstrap.py" start
exit /b 0
