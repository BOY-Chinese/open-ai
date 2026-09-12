@echo off
rem open-ai one-click start (v2.4: broker-managed process tree)
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo [INFO] First run: creating venv...
    where python >nul 2>nul || (echo [ERROR] python not found, install Python 3.10+ & pause & exit /b 1)
    python -m venv .venv || goto :err
    ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple || goto :err
)
rem ensure shims exist (open-ai-*.exe with icon/description), then start broker
".venv\Scripts\python.exe" procname.py
".venv\Scripts\python.exe" bootstrap.py start
echo.
echo [OK] open-ai started.  Gateway http://127.0.0.1:8000  ^|  status: bootstrap.py status
echo      UI  : desktop\open-ai-desktop.exe  (or the "open-ai" desktop shortcut)
echo      Stop: ".venv\Scripts\python.exe" bootstrap.py stop
pause
exit /b 0
:err
echo [ERROR] start failed, see messages above.
pause
exit /b 1
