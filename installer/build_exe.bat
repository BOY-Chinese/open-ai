@echo off
rem open-ai 一键安装包 构建脚本 (Windows + PyInstaller) — v2.5 新架构
rem 前置: 已安装 Python 3.10+ 并加入 PATH (脚本自动装 pyinstaller + 依赖)
rem 用法: 双击本脚本 (或根目录 一键构建.bat)
rem 产出: dist\open-ai-installer-dev.exe (+ uninstall.exe + open-ai-launcher.exe)
rem 日志: installer\build_installer.log
setlocal
cd /d "%~dp0"
set LOGFILE=%~dp0build_installer.log
echo [%date% %time%] ==== open-ai installer build start ==== > "%LOGFILE%"

echo ====================================================
echo  open-ai 一键安装包 构建
echo ====================================================

rem ---- 0. 准备 python + pyinstaller ----
set PY=python
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] 未找到 python, 请先安装 Python 3.10+
    goto :err
)
%PY% --version >> "%LOGFILE%" 2>&1
%PY% -m pip show pyinstaller >nul 2>nul
if errorlevel 1 (
    echo [INFO] 安装 pyinstaller...
    %PY% -m pip install pyinstaller -q >> "%LOGFILE%" 2>&1
    if errorlevel 1 goto :err
)
%PY% -m pip show pefile >nul 2>nul
if errorlevel 1 (
    echo [INFO] 安装 pefile...
    %PY% -m pip install pefile -q >> "%LOGFILE%" 2>&1
)
%PY% -m pip show pywin32-ctypes >nul 2>nul
if errorlevel 1 (
    echo [INFO] 安装 pywin32-ctypes...
    %PY% -m pip install pywin32-ctypes -q >> "%LOGFILE%" 2>&1
)

rem ---- 1. 构建 uninstall.exe (onedir) ----
rem onedir 而非 onefile: 无 %TEMP%\_MEI 临时目录, 根治退出时
rem "Failed to remove temporary directory" 弹窗 (VM/杀软锁文件场景), 启动也更快。
echo.
echo [1/5] 构建 uninstall.exe (onedir)...
if exist "ico\open-ai.ico" (
    %PY% -m PyInstaller --noconfirm --clean --noupx --windowed ^
        --contents-directory "uninstall_internal" ^
        --name "uninstall" --icon "ico\open-ai.ico" uninstaller.py >> "%LOGFILE%" 2>&1
) else (
    %PY% -m PyInstaller --noconfirm --clean --noupx --windowed ^
        --contents-directory "uninstall_internal" ^
        --name "uninstall" uninstaller.py >> "%LOGFILE%" 2>&1
)
if errorlevel 1 goto :err
echo [OK] uninstall.exe (dist\uninstall\)

rem ---- 1b. 构建 open-ai-launcher.exe (onedir) ----
echo.
echo [1b] 构建 open-ai-launcher.exe (onedir)...
if exist "ico\open-ai.ico" (
    %PY% -m PyInstaller --noconfirm --clean --noupx --windowed ^
        --contents-directory "launcher_internal" ^
        --name "open-ai-launcher" --icon "ico\open-ai.ico" launcher.py >> "%LOGFILE%" 2>&1
) else (
    %PY% -m PyInstaller --noconfirm --clean --noupx --windowed ^
        --contents-directory "launcher_internal" ^
        --name "open-ai-launcher" launcher.py >> "%LOGFILE%" 2>&1
)
if errorlevel 1 goto :err
echo [OK] open-ai-launcher.exe (dist\open-ai-launcher\)

rem ---- 2. 打包资源 zip ----
echo.
echo [2/5] 打包 open-ai 资源...
python build_resources.py >> "%LOGFILE%" 2>&1
if errorlevel 1 goto :err

rem ---- 3. 构建安装器 exe ----
echo.
echo [3/5] PyInstaller 构建安装器 exe...
if exist "ico\open-ai.ico" (
    %PY% -m PyInstaller --noconfirm --clean --noupx --onefile --windowed --uac-admin ^
        --name "open-ai-installer-dev" --icon "ico\open-ai.ico" ^
        --add-data "resources.zip;." ^
        --add-data "config.shell.json;." ^
        --add-data "ico\open-ai.ico;ico" ^
        installer.py >> "%LOGFILE%" 2>&1
) else (
    %PY% -m PyInstaller --noconfirm --clean --noupx --onefile --windowed --uac-admin ^
        --name "open-ai-installer-dev" ^
        --add-data "resources.zip;." ^
        --add-data "config.shell.json;." ^
        installer.py >> "%LOGFILE%" 2>&1
)
if errorlevel 1 goto :err

rem ---- 4. 复制到桌面 ----
echo.
echo [4/5] 复制安装包到桌面...
set DESKTOP=%USERPROFILE%\Desktop
if not exist "%DESKTOP%" set DESKTOP=%USERPROFILE%\OneDrive\Desktop
if not exist "%DESKTOP%" set DESKTOP=%USERPROFILE%\OneDrive\桌面
if not exist "%DESKTOP%" set DESKTOP=%USERPROFILE%\桌面
copy /y "dist\open-ai-installer-dev.exe" "%DESKTOP%\open-ai-installer-dev.exe" >nul
if errorlevel 1 (
    echo [WARN] 复制到桌面失败, 安装包在 dist\ 目录
) else (
    echo [OK] 安装包已复制到桌面: %DESKTOP%\open-ai-installer-dev.exe
)

rem ---- 5. 收尾 ----
echo.
echo ====================================================
echo  [完成] 构建结果:
echo    - installer\dist\open-ai-installer-dev.exe  (安装包, onefile)
echo    - installer\dist\uninstall\             (卸载程序, onedir)
echo    - installer\dist\open-ai-launcher\      (一键启动器, onedir)
echo    详细日志: installer\build_installer.log
echo ====================================================
pause
exit /b 0
:err
echo.
echo [ERROR] 构建失败, 请查看日志: installer\build_installer.log
pause
exit /b 1
