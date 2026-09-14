@echo off
chcp 936 >nul
rem =====================================================
rem open-ai 一键安装包 构建脚本 (Windows + PyInstaller)
rem 用法: build_exe.bat [dev^|portable]  缺省 dev; portable 产用户版安装包
rem =====================================================
rem 产出: dist\open-ai-installer-%CHANNEL%.exe (+ 自动复制到桌面)
rem 日志: installer\build_installer.log
rem
rem 打包方案: 品牌 exe 全部内嵌 Python/Node 解释器, 用户机器零依赖。
rem   open-ai-daemon.exe   Broker 独立入口 (start.bat / 自启)
rem   open-ai-gateway.exe  网关 :8000, 兼 Broker 宿主 (main.py --broker)
rem   open-ai-task.exe     短命任务 (签到 / 采集 / 登录)
rem   open-ai.exe          控制 CLI (start|stop|restart|status|doctor)
rem   open-ai-trae.exe     node 品牌化副本 (Trae 通道)
rem   desktop\open-ai-desktop.exe   Tauri 界面 (随 resources.zip 落位, 不在此构建)
rem
rem ★ 桌面端必须先构建好, 跑:
rem     cd desktop-ui && npm run build
rem     powershell -ExecutionPolicy Bypass -File tools\build-tauri-release.ps1
rem   本脚本第 0 步会检查它存在, 并用「前端 bundle 指纹」确认内嵌的是已清洗版本。
rem =====================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"
rem ---- 通道参数: build_exe.bat [dev^|portable] ----
rem 决定产物名 open-ai-installer-<通道>.exe 与版本资源 InternalName。
set CHANNEL=%~1
if "%CHANNEL%"=="" set CHANNEL=dev

set LOGFILE=%~dp0build_installer.log
echo [%date% %time%] ==== open-ai installer build start (channel=%CHANNEL%) ==== > "%LOGFILE%"

echo ====================================================
echo  open-ai 一键安装包 (%CHANNEL% 通道) 构建
echo ====================================================

rem ---- 0a. python + pyinstaller ----
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
for %%D in (pefile pywin32-ctypes) do (
    %PY% -m pip show %%D >nul 2>nul
    if errorlevel 1 %PY% -m pip install %%D -q >> "%LOGFILE%" 2>&1
)

rem ---- 0b. 前置检查: 桌面端存在 + 内嵌前端是已清洗版本 ----
echo [0/7] 检查桌面端产物...
if not exist "..\desktop\open-ai-desktop.exe" (
    echo [ERROR] 缺少 ..\desktop\open-ai-desktop.exe
    echo         先执行: powershell -ExecutionPolicy Bypass -File desktop-ui\tools\build-tauri-release.ps1
    goto :err
)
rem ★ 光看「文件在不在」不够 —— 桌面上可能躺着上一次构建的 exe, 而那次用的
rem   是清洗前的演示数据 (含真实网关 api_key)。前端产物名是内容哈希, 拿它当
rem   指纹就能唯一确定内嵌的是哪一版 dist; 指纹不过直接中止, 不产半成品包。
%PY% check_desktop_bundle.py "..\desktop\open-ai-desktop.exe" >> "%LOGFILE%" 2>&1
if errorlevel 1 (
    echo [ERROR] 桌面端内嵌的前端不是已清洗版本 ^(详见 build_installer.log^)
    echo         重跑: cd desktop-ui ^&^& npm run build ^&^& powershell -File tools\build-tauri-release.ps1
    goto :err
)
echo   [OK] desktop\open-ai-desktop.exe 就位 (前端指纹校验通过)

rem ---- 1. 版本资源 ----
echo [1/7] 生成版本资源...
%PY% verblock.py %CHANNEL% >> "%LOGFILE%" 2>&1
if errorlevel 1 goto :err

rem ---- 2. uninstall.exe (独立卸载程序) ----
echo [2/7] 构建 uninstall.exe...
if exist "ico\open-ai.ico" (
    %PY% -m PyInstaller --noconfirm --clean --onefile --windowed ^
        --name "uninstall" --icon "ico\open-ai.ico" ^
        --version-file ver_uninstall.py ^
        uninstaller.py >> "%LOGFILE%" 2>&1
) else (
    %PY% -m PyInstaller --noconfirm --clean --onefile --windowed ^
        --name "uninstall" --version-file ver_uninstall.py uninstaller.py >> "%LOGFILE%" 2>&1
)
if errorlevel 1 goto :err
copy /y "dist\uninstall.exe" "uninstall.exe" >nul
echo   [OK] uninstall.exe

rem ---- 3. 品牌 exe (daemon / gateway / task / cli) ----
echo [3/7] 构建 open-ai-daemon / gateway / task / cli ... (要几分钟)
for %%S in (open-ai-daemon open-ai-gateway open-ai-task open-ai-cli) do (
    echo   - %%S ...
    %PY% -m PyInstaller --noconfirm --clean %%S.spec >> "%LOGFILE%" 2>&1
    if errorlevel 1 goto :err
)
rem spec 里的 name 才是产物文件名: open-ai-cli.spec 产出 open-ai.exe, 单独搬一次
copy /y "dist\open-ai-daemon.exe" "open-ai-daemon.exe" >nul
copy /y "dist\open-ai-gateway.exe" "open-ai-gateway.exe" >nul
copy /y "dist\open-ai-task.exe" "open-ai-task.exe" >nul
copy /y "dist\open-ai.exe" "open-ai.exe" >nul
echo   [OK] daemon / gateway / task / open-ai.exe (CLI)

rem ---- 3b. open-ai-trae.exe (node 品牌化副本, Trae 后端) ----
echo [3b/7] 构建 open-ai-trae.exe (node 副本)...
%PY% build_trae_shim.py >> "%LOGFILE%" 2>&1
if errorlevel 1 echo   [WARN] 失败 — Trae 通道将禁用, WorkBuddy 不受影响

rem ---- 4. 资源包 (trae/ + pic/ + desktop/ + 卸载器 + 文档) ----
echo [4/7] 打包 open-ai 资源...
%PY% build_resources.py >> "%LOGFILE%" 2>&1
if errorlevel 1 goto :err

rem ---- 5. 安装器 exe ----
echo [5/7] PyInstaller 打包安装器 exe...
if not exist "exes" mkdir "exes"
for %%E in (open-ai-daemon open-ai-gateway open-ai-task open-ai open-ai-trae) do (
    if exist "%%E.exe" copy /y "%%E.exe" "exes\" >nul
)
if exist "ico\open-ai.ico" (
    %PY% -m PyInstaller --noconfirm --clean --onefile --windowed --uac-admin ^
        --name "open-ai-installer-%CHANNEL%" ^
        --icon "ico\open-ai.ico" ^
        --version-file ver_installer.py ^
        --add-data "resources.zip;." ^
        --add-data "config.shell.json;." ^
        --add-data "exes;exes" ^
        --add-data "ico\open-ai.ico;ico" ^
        installer.py >> "%LOGFILE%" 2>&1
) else (
    %PY% -m PyInstaller --noconfirm --clean --onefile --windowed --uac-admin ^
        --name "open-ai-installer-%CHANNEL%" ^
        --add-data "resources.zip;." ^
        --add-data "config.shell.json;." ^
        --add-data "exes;exes" ^
        installer.py >> "%LOGFILE%" 2>&1
)
if errorlevel 1 goto :err

rem ---- 6. 产出校验 (体积闸门: 只看「没报错」会放过一个空壳包) ----
echo [6/7] 校验安装包...
if not exist "dist\open-ai-installer-%CHANNEL%.exe" (
    echo [ERROR] dist\open-ai-installer-%CHANNEL%.exe 未生成
    goto :err
)
for %%F in ("dist\open-ai-installer-%CHANNEL%.exe") do set SIZE=%%~zF
echo   安装包大小: !SIZE! 字节
if !SIZE! LSS 100000000 (
    echo [ERROR] 安装包体积异常偏小 ^(!SIZE! 字节, 预期 ^> 100 MB^) —— 资源很可能没打进去
    goto :err
)

rem ---- 7. 复制到桌面 ----
echo [7/7] 复制安装包到桌面...
rem ★ 不要用 %USERPROFILE%\Desktop 猜桌面: 桌面被 OneDrive / 组策略重定向后, 该目录
rem   **存在但已废弃**, 复制过去用户根本看不见。
rem   本项目实测踩过: 包落进了早已废弃的 %USERPROFILE%\Desktop, 真实桌面在别的盘。
rem   必须问 Windows 要真实路径。
set DESKTOP=
for /f "usebackq delims=" %%D in (`powershell -NoProfile -Command "[Environment]::GetFolderPath('Desktop')"`) do set DESKTOP=%%D
if not defined DESKTOP set DESKTOP=%USERPROFILE%\Desktop
if not exist "%DESKTOP%" set DESKTOP=%USERPROFILE%\Desktop
echo   桌面路径: %DESKTOP%
copy /y "dist\open-ai-installer-%CHANNEL%.exe" "%DESKTOP%\open-ai-installer-%CHANNEL%.exe" >nul
if errorlevel 1 (
    echo [WARN] 复制到桌面失败, 安装包在 dist\ 目录
) else (
    echo   [OK] 已复制到桌面: %DESKTOP%\open-ai-installer-%CHANNEL%.exe
)

echo ====================================================
echo  构建完成!
echo  安装包:   %DESKTOP%\open-ai-installer-%CHANNEL%.exe
echo  详细日志: build_installer.log
echo ====================================================
echo [%date% %time%] ==== BUILD OK ==== >> "%LOGFILE%"
endlocal
exit /b 0

:err
echo.
echo [ERROR] 构建失败, 请查看 build_installer.log
echo [%date% %time%] ==== BUILD FAILED ==== >> "%LOGFILE%"
endlocal
exit /b 1
