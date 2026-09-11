@echo off
rem open-ai autostart (v3.0): 拉起后端进程树 + 托盘 GUI
rem
rem v3.0 变更:
rem   1) 以脚本自身目录为根, 不再硬编码 D:\app\dsh_plugin\open-ai —— 安装到
rem      任意路径都能工作（旧版硬编码路径在非开发机上直接失效）。
rem   2) 改为调用 start_hidden.ps1 拉起「Broker + 托盘 GUI」：托盘图标由 GUI
rem      创建, 旧版只拉 Broker 会导致「后端在跑但托盘无图标」。
setlocal
set "ROOT=%~dp0"
rem 去掉结尾反斜杠, 避免拼接出双斜杠
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

rem 已带 hidden 参数则不重复开控制台窗口（兼容旧调用约定）
if not "%1"=="hidden" (
    start "" /min cmd /c ""%~f0" hidden"
    exit /b 0
)

cd /d "%ROOT%"
if not exist "runtime\Scripts\open-ai-daemon.exe" (
    rem runtime shim 未生成：先构建（幂等），失败也无妨（ps1 有 pythonw 兜底）
    if exist ".venv\Scripts\python.exe" (
        ".venv\Scripts\python.exe" procname.py >nul 2>nul
    )
)

if exist "%ROOT%\start_hidden.ps1" (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden ^
        -File "%ROOT%\start_hidden.ps1"
) else (
    rem 极端兜底：直接拉 broker
    if exist "%ROOT%\runtime\Scripts\open-ai-daemon.exe" (
        start "" /b "%ROOT%\runtime\Scripts\open-ai-daemon.exe" "%ROOT%\bootstrap.py" start
    )
)
exit /b 0
