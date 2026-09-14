@echo off
rem open-ai 开机自启入口 (双形态自适应)
rem
rem 设计: 自启必须**先把后端拉起来**, 界面只作为第二步。
rem   以前打包安装里这条链路只 start 了桌面端 exe, 而桌面端在打包安装里
rem   一度找不到安装根 (is_root 判据只认 .py 源码) —— 结果开机后只有界面壳
rem   在跑, 8000 端口永远不监听, 表现为「无法连接网关」。两步分开就不依赖
rem   界面能否成功代劳。
rem
rem 本脚本以自身所在目录为根, 不写死任何盘符路径 (旧版硬编码在非开发机直接失效)。
setlocal
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

rem 已带 hidden 参数则不重复开控制台窗口（兼容旧调用约定）
if not "%1"=="hidden" (
    start "" /min cmd /c ""%~f0" hidden"
    exit /b 0
)

cd /d "%ROOT%"

rem ---- 打包安装形态: 控制 CLI 拉后端 + 桌面端最小化驻留托盘 ----
if exist "open-ai.exe" (
    start "" /b ".\open-ai.exe" start
    if exist "desktop\open-ai-desktop.exe" (
        start "" ".\desktop\open-ai-desktop.exe" --minimized
    )
    exit /b 0
)
if exist "open-ai-daemon.exe" (
    start "" /b ".\open-ai-daemon.exe"
    if exist "desktop\open-ai-desktop.exe" (
        start "" ".\desktop\open-ai-desktop.exe" --minimized
    )
    exit /b 0
)

rem ---- 源码形态: 隐藏 PowerShell 引导 (内部会建 shim 并调 bootstrap) ----
if not exist "runtime\Scripts\open-ai-daemon.exe" (
    if exist ".venv\Scripts\python.exe" ".venv\Scripts\python.exe" procname.py >nul 2>nul
)
if exist "%ROOT%\start_hidden.ps1" (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%ROOT%\start_hidden.ps1"
    exit /b 0
)
echo [ERROR] neither packaged exe nor start_hidden.ps1 found in "%ROOT%"
exit /b 1
