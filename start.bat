@echo off
rem open-ai 一键启动 (双形态自适应: exe 打包安装 / 源码开发)
setlocal
cd /d "%~dp0"

rem ---- 打包安装形态: 安装根只有品牌 exe, 没有 .venv 也没有 .py 源码 ----
rem 走控制 CLI 拉起 Broker (网关/Trae/定时任务), 再打开界面。
rem ★ 这条分支必须放在最前面: 否则下面那句 .venv\Scripts\python.exe 在装好
rem   安装包的机器上直接报「python not found」, 用户以为软件是坏的。
if exist "open-ai.exe" goto :packaged
if exist "open-ai-daemon.exe" goto :packaged_daemon

rem ---- 源码形态: 首次建 venv + 装依赖, 构建品牌化进程, 再起 Broker ----
if not exist ".venv\Scripts\python.exe" (
    echo [INFO] First run: creating venv...
    where python >nul 2>nul || (echo [ERROR] python not found, install Python 3.10+ & pause & exit /b 1)
    python -m venv .venv || goto :err
    ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple || goto :err
)
".venv\Scripts\python.exe" procname.py
".venv\Scripts\python.exe" bootstrap.py start
echo.
echo [OK] open-ai started.  Gateway http://127.0.0.1:8000  ^|  status: bootstrap.py status
echo      UI  : desktop\open-ai-desktop.exe  (or the "open-ai" desktop shortcut)
echo      Stop: ".venv\Scripts\python.exe" bootstrap.py stop
pause
exit /b 0

:packaged
echo [INFO] packaged install detected - starting broker via open-ai.exe
".\open-ai.exe" start
if errorlevel 1 goto :err
echo.
echo [OK] open-ai started.  Gateway http://127.0.0.1:8000  ^|  status: open-ai.exe status
echo      UI  : desktop\open-ai-desktop.exe  (or the "open-ai" desktop shortcut)
echo      Stop: open-ai.exe stop
pause
exit /b 0

:packaged_daemon
echo [INFO] open-ai.exe (control CLI) missing - starting broker directly
start "" /b ".\open-ai-daemon.exe"
echo [OK] broker launched. UI: desktop\open-ai-desktop.exe
pause
exit /b 0

:err
echo [ERROR] start failed, see messages above.
pause
exit /b 1
