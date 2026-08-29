@echo off
rem =====================================================
rem open-ai 一键构建安装包 (生成exe并自动放到桌面)
rem =====================================================
chcp 936 >nul
cd /d "%~dp0installer"
echo 即将构建 open-ai 一键安装包...
echo 这将下载 pyinstaller (首次) 并打包, 需要几分钟.
echo.
pause
call build_exe.bat

echo.
echo [一键构建] 构建流程已结束, 关闭本窗口前请查看上方日志.
pause
