@echo off
rem open-ai 账号管理 - 图形界面版 (账号 / API管理 / 设置 / 操作日志)
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] 未找到虚拟环境, 请先运行 start.bat
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" scripts\gui_account_manager.py
