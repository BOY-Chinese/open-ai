@echo off
rem =====================================================
rem open-ai 一键构建安装包 (构建 exe 自动放到桌面)
rem =====================================================
chcp 936 >nul
cd /d "%~dp0installer"
echo 开始构建 open-ai 一键安装包...
echo 将使用 PyInstaller (首次自动安装), 请耐心等待.
echo.
pause
call build_exe.bat

echo.
echo [一键构建] 构建流程已结束, 请查看上方日志.
pause
