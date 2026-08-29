@echo off
rem 用 GBK 代码页, 匹配本脚本编码, 避免被外层 chcp 65001 干扰导致中文乱码/闪退
chcp 936 >nul
rem =====================================================
rem open-ai 一键安装包 构建脚本 (Windows + PyInstaller)
rem =====================================================
rem 前置: 已安装 Python 3.10+ 并加入 PATH (脚本会自动装 pyinstaller + 依赖)
rem 用法: 双击运行本脚本 (或由根目录 一键构建.bat 调用)
rem 产出: dist\open-ai-installer.exe + dist\uninstall.exe + 自动复制到桌面
rem 日志: installer\build_installer.log
rem =====================================================
setlocal
cd /d "%~dp0"
set LOGFILE=%~dp0build_installer.log
echo [%date% %time%] ==== open-ai installer build start ==== > "%LOGFILE%"

echo ====================================================
echo  open-ai 一键安装包 构建
echo ====================================================

rem ---- 0. 找 python + pyinstaller + 依赖 ----
set PY=python
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] 未找到 python, 请先安装 Python 3.10+
    goto :err
)
%PY% --version >> "%LOGFILE%" 2>&1
rem 检查/安装 pyinstaller
%PY% -m pip show pyinstaller >nul 2>nul
if errorlevel 1 (
    echo [INFO] 安装 pyinstaller...
    %PY% -m pip install pyinstaller -q >> "%LOGFILE%" 2>&1
    if errorlevel 1 goto :err
)
rem 补齐 pyinstaller 的 Windows 依赖 (pefile / pywin32-ctypes), 缺失会报 ModuleNotFoundError
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

rem ---- 1. 先构建 uninstall.exe (独立卸载程序) ----
echo.
echo [1/5] 构建 uninstall.exe (独立卸载程序)...
if exist "ico\open-ai.ico" (
    %PY% -m PyInstaller --noconfirm --clean --onefile --windowed ^
        --name "uninstall" ^
        --icon "ico\open-ai.ico" ^
        uninstaller.py >> "%LOGFILE%" 2>&1
) else (
    %PY% -m PyInstaller --noconfirm --clean --onefile --windowed ^
        --name "uninstall" ^
        uninstaller.py >> "%LOGFILE%" 2>&1
)
if errorlevel 1 goto :err
rem 把 uninstall.exe 拷到 installer 目录, 供资源打包
copy /y "dist\uninstall.exe" "uninstall.exe" >nul
if errorlevel 1 goto :err
echo [OK] uninstall.exe 已生成

rem ---- 1b. 构建 open-ai-launcher.exe (一键启动器, 无窗口) ----
echo.
echo [1b] 构建 open-ai-launcher.exe (一键启动器, 无窗口)...
if exist "ico\open-ai.ico" (
    %PY% -m PyInstaller --noconfirm --clean --onefile --windowed ^
        --name "open-ai-launcher" ^
        --icon "ico\open-ai.ico" ^
        launcher.py >> "%LOGFILE%" 2>&1
) else (
    %PY% -m PyInstaller --noconfirm --clean --onefile --windowed ^
        --name "open-ai-launcher" ^
        launcher.py >> "%LOGFILE%" 2>&1
)
if errorlevel 1 goto :err
copy /y "dist\open-ai-launcher.exe" "open-ai-launcher.exe" >nul
if errorlevel 1 goto :err
echo [OK] open-ai-launcher.exe 已生成

rem ---- 2. 打包资源 zip (含 uninstall.exe + launcher) ----
echo.
echo [2/6] 打包 open-ai 资源...
python build_resources.py >> "%LOGFILE%" 2>&1
if errorlevel 1 goto :err

rem ---- 3. 构建安装器 exe ----
echo.
echo [3/6] PyInstaller 打包安装器 exe...
if exist "ico\open-ai.ico" (
    %PY% -m PyInstaller --noconfirm --clean --onefile --windowed --uac-admin ^
        --name "open-ai-installer" ^
        --icon "ico\open-ai.ico" ^
        --add-data "resources.zip;." ^
        --add-data "config.shell.json;." ^
        --add-data "ico\open-ai.ico;ico" ^
        installer.py >> "%LOGFILE%" 2>&1
) else (
    %PY% -m PyInstaller --noconfirm --clean --onefile --windowed --uac-admin ^
        --name "open-ai-installer" ^
        --add-data "resources.zip;." ^
        --add-data "config.shell.json;." ^
        installer.py >> "%LOGFILE%" 2>&1
)
if errorlevel 1 goto :err

rem ---- 4. 复制到桌面 ----
echo.
echo [4/6] 复制安装包到桌面...
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
echo [5/6] 构建完成!
echo ====================================================
echo  安装包:   %DESKTOP%\open-ai-installer.exe
echo  卸载程序: dist\uninstall.exe (会随安装包内置)
echo  详细日志: build_installer.log
echo  (若桌面没有, 在 dist\open-ai-installer.exe)
echo ====================================================
dir "dist\open-ai-installer.exe" "dist\uninstall.exe" | findstr "exe"
pause
goto :eof

:err
echo.
echo [ERROR] 构建失败, 请查看 build_installer.log
echo [%time%] ==== BUILD FAILED ==== >> "%LOGFILE%"
echo.
echo ===== 最近日志 =====
type "%LOGFILE%"
echo ====================
pause
exit /b 1
