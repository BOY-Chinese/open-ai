@echo off
rem open-ai 一键启动 (Node 后端 18787 + 网关 8000)
cd /d "%~dp0"
set PY=python
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] 未找到 python, 请先安装 Python 3.10+
    pause
    exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
    echo [INFO] 首次运行, 正在创建虚拟环境...
    %PY% -m venv .venv
    if errorlevel 1 goto :err
)
echo [INFO] 检查依赖...
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
if errorlevel 1 goto :err

rem ===== 1. 启动 Trae Node 后端 (18787) =====
set NODE_CMD=node
where node >nul 2>nul
if errorlevel 1 (
    echo [WARN] 未找到 node, Trae 后端跳过 (WorkBuddy 网关仍可用)
    goto :gateway
)
netstat -ano | findstr "LISTENING" | findstr ":18787 " >nul 2>nul
if errorlevel 1 (
    echo [INFO] 启动 Trae Node 后端 (18787)...
    start "" "%NODE_CMD%" "trae\server.js"
    timeout /t 3 /nobreak >nul
) else (
    echo [INFO] Trae 后端已在运行 (18787)
)

:gateway
rem ===== 2. 启动网关 (8000) =====
netstat -ano | findstr "LISTENING" | findstr ":8000 " >nul 2>nul
if errorlevel 1 (
    echo [INFO] 启动网关 (8000)...
    ".venv\Scripts\python.exe" main.py %*
) else (
    echo [INFO] 网关已在运行 (8000), 前台启动跳过
    pause
)
exit /b 0
:err
echo [ERROR] 启动失败, 请检查上方日志
pause
exit /b 1
