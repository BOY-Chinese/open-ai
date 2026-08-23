@echo off
rem =====================================================
rem open-ai 一键安装包 构建脚本 (Windows + PyInstaller)
rem =====================================================
rem 前置: 已安装 Python 3.10+ 并加入 PATH (脚本会自动装 pyinstaller)
rem 用法: 双击运行本脚本
rem 产出: dist\open-ai-installer.exe + 自动复制到桌面
rem =====================================================
setlocal
cd /d "%~dp0"

echo ====================================================
echo  open-ai 一键安装包 构建
echo ====================================================

rem ---- 1. 打包资源 zip ----
echo.
echo [1/4] 打包 open-ai 资源...
python build_resources.py
if errorlevel 1 goto :err

rem ---- 2. 找 python + pyinstaller ----
set PY=python
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] 未找到 python, 请先安装 Python 3.10+
    goto :err
)
%PY% -m pip show pyinstaller >nul 2>nul
if errorlevel 1 (
    echo [INFO] 安装 pyinstaller...
    %PY% -m pip install pyinstaller -q
    if errorlevel 1 goto :err
)

rem ---- 3. PyInstaller 打包 ----
echo.
echo [2/4] PyInstaller 打包 exe (资源较大, 需几分钟)...
rem 需先确保 ico 存在 (若缺则提示)
if not exist "ico\open-ai.ico" (
    echo [WARN] 未找到 ico\open-ai.ico, 跳过图标
    %PY% -m PyInstaller --noconfirm --clean --onefile --windowed --uac-admin ^
        --name "open-ai-installer" ^
        --add-data "resources.zip;." ^
        --add-data "config.shell.json;." ^
        installer.py
) else (
    %PY% -m PyInstaller --noconfirm --clean --onefile --windowed --uac-admin ^
        --name "open-ai-installer" ^
        --icon "ico\open-ai.ico" ^
        --add-data "resources.zip;." ^
        --add-data "config.shell.json;." ^
        --add-data "ico\open-ai.ico;ico" ^
        installer.py
)
if errorlevel 1 goto :err

rem ---- 4. 复制到桌面 ----
echo.
echo [3/4] 复制安装包到桌面...
set DESKTOP=%USERPROFILE%\Desktop
if not exist "%DESKTOP%" set DESKTOP=%USERPROFILE%\OneDrive\Desktop
if not exist "%DESKTOP%" set DESKTOP=%USERPROFILE%\OneDrive\桌面
if not exist "%DESKTOP%" set DESKTOP=%USERPROFILE%\桌面
copy /y "dist\open-ai-installer.exe" "%DESKTOP%\open-ai-installer.exe" >nul
if errorlevel 1 (
    echo [WARN] 复制到桌面失败, 安装包在 dist\ 目录
) else (
    echo [OK] 已复制到桌面: %DESKTOP%\open-ai-installer.exe
)

rem ---- 5. 完成 ----
echo.
echo [4/4] 构建完成!
echo ====================================================
echo  安装包: %DESKTOP%\open-ai-installer.exe
echo  (若桌面没有, 在 dist\open-ai-installer.exe)
echo  把这个 exe 发给用户, 双击即可一键安装.
echo ====================================================
dir "dist\open-ai-installer.exe" | findstr "open-ai"
pause
goto :eof

:err
echo.
echo [ERROR] 构建失败, 请检查上方日志.
pause
exit /b 1
