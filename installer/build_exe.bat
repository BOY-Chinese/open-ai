@echo off
rem =====================================================
rem open-ai 一键安装包 构建脚本 (Windows + PyInstaller)
rem =====================================================
rem 前置: 已安装 Python 3.10+ 并加入 PATH (脚本会自动装 pyinstaller)
rem 用法: 双击运行本脚本
rem 产出: dist\open-ai-installer.exe + dist\uninstall.exe + 自动复制到桌面
rem =====================================================
setlocal
cd /d "%~dp0"

echo ====================================================
echo  open-ai 一键安装包 构建
echo ====================================================

rem ---- 0. 找 python + pyinstaller ----
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

rem ---- 1. 先构建 uninstall.exe (独立卸载程序) ----
echo.
echo [1/5] 构建 uninstall.exe (独立卸载程序)...
if exist "ico\open-ai.ico" (
    %PY% -m PyInstaller --noconfirm --clean --onefile --windowed ^
        --name "uninstall" ^
        --icon "ico\open-ai.ico" ^
        uninstaller.py
) else (
    %PY% -m PyInstaller --noconfirm --clean --onefile --windowed ^
        --name "uninstall" ^
        uninstaller.py
)
if errorlevel 1 goto :err
rem 把 uninstall.exe 拷到 installer 目录, 供资源打包
copy /y "dist\uninstall.exe" "uninstall.exe" >nul
if errorlevel 1 goto :err
echo [OK] uninstall.exe 已生成

rem ---- 2. 打包资源 zip (含 uninstall.exe) ----
echo.
echo [2/5] 打包 open-ai 资源...
python build_resources.py
if errorlevel 1 goto :err

rem ---- 3. 构建安装器 exe ----
echo.
echo [3/5] PyInstaller 打包安装器 exe...
if exist "ico\open-ai.ico" (
    %PY% -m PyInstaller --noconfirm --clean --onefile --windowed --uac-admin ^
        --name "open-ai-installer" ^
        --icon "ico\open-ai.ico" ^
        --add-data "resources.zip;." ^
        --add-data "config.shell.json;." ^
        --add-data "ico\open-ai.ico;ico" ^
        installer.py
) else (
    %PY% -m PyInstaller --noconfirm --clean --onefile --windowed --uac-admin ^
        --name "open-ai-installer" ^
        --add-data "resources.zip;." ^
        --add-data "config.shell.json;." ^
        installer.py
)
if errorlevel 1 goto :err

rem ---- 4. 复制到桌面 ----
echo.
echo [4/5] 复制安装包到桌面...
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
echo [5/5] 构建完成!
echo ====================================================
echo  安装包:   %DESKTOP%\open-ai-installer.exe
echo  卸载程序: dist\uninstall.exe (会随安装包内置)
echo  (若桌面没有, 在 dist\open-ai-installer.exe)
echo ====================================================
dir "dist\open-ai-installer.exe" "dist\uninstall.exe" | findstr "exe"
pause
goto :eof

:err
echo.
echo [ERROR] 构建失败, 请检查上方日志.
pause
exit /b 1
