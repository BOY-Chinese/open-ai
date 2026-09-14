@echo off
rem =====================================================
rem open-ai 开源版一键安装包 构建脚本 (Windows + PyInstaller)
rem 用法: build_exe.bat   (无交互等价入口: python build_all.py)
rem 产出: dist\open-ai-installer-dev.exe (+ 自动复制到桌面)
rem 日志: installer\build_installer.log
rem
rem ★ 开源版 = 源码分装 (与 portable 用户版的冻结 exe 方案相反):
rem   resources.zip 内是**全部后端源码** (.py / providers / scripts / trae /
rem   pic / tests), 装完用户可直接查看与修改; 安装器负责补装 Python、
rem   建 venv、装依赖、配快捷方式。代码不冻结进 exe —— 包里仅有的 exe 是
rem   桌面端 (Tauri, 前端已内嵌) 与卸载器。
rem =====================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"
set LOGFILE=%~dp0build_installer.log
echo [%date% %time%] ==== open-ai installer build start (源码分装) ==== > "%LOGFILE%"

echo ====================================================
echo  open-ai 开源版安装包 (源码分装) 构建
echo ====================================================

rem ---- 0. python + pyinstaller ----
set PY=python
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] 未找到 python, 请先安装 Python 3.10+
    goto :err
)
%PY% --version >> "%LOGFILE%" 2>&1
for %%D in (pyinstaller pefile pywin32-ctypes) do (
    %PY% -m pip show %%D >nul 2>nul
    if errorlevel 1 %PY% -m pip install %%D -q >> "%LOGFILE%" 2>&1
)

rem ---- 0b. 前置检查: 桌面端存在 + 内嵌前端是已清洗版本 ----
echo [0/5] 检查桌面端产物...
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

rem ---- 1. uninstall.exe (独立卸载程序, onedir) ----
rem onedir 而非 onefile: 无 %TEMP%\_MEI 临时目录, 根治退出时
rem "Failed to remove temporary directory" 弹窗 (VM/杀软锁文件场景), 启动也更快。
echo [1/5] 构建 uninstall.exe (onedir)...
if exist "ico\open-ai.ico" (
    %PY% -m PyInstaller --noconfirm --clean --noupx --windowed ^
        --contents-directory "uninstall_internal" ^
        --name "uninstall" --icon "ico\open-ai.ico" ^
        uninstaller.py >> "%LOGFILE%" 2>&1
) else (
    %PY% -m PyInstaller --noconfirm --clean --noupx --windowed ^
        --contents-directory "uninstall_internal" ^
        --name "uninstall" uninstaller.py >> "%LOGFILE%" 2>&1
)
if errorlevel 1 goto :err
echo   [OK] uninstall.exe (dist\uninstall\)

rem ---- 2. 资源包 (全部后端源码 + trae/lib + pic + 桌面端 + 卸载器) ----
rem build_resources.py 内部有关键条目强制校验 (main.py / loomy / 桌面端 /
rem uninstall.exe ...), 缺一样直接中止 —— 防止打出「装出来跑不起来」的残废包。
echo [2/5] 打包 open-ai 源码资源...
%PY% build_resources.py >> "%LOGFILE%" 2>&1
if errorlevel 1 goto :err
echo   [OK] resources.zip (源码分装, 关键条目校验通过)

rem ---- 3. 安装器 exe (onefile: 内嵌 resources.zip + 空壳 config + 图标) ----
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

rem ---- 4. 产出校验 (体积闸门: 只看「没报错」会放过一个空壳包) ----
echo [4/5] 校验安装包...
if not exist "dist\open-ai-installer-dev.exe" (
    echo [ERROR] dist\open-ai-installer-dev.exe 未生成
    goto :err
)
for %%F in ("dist\open-ai-installer-dev.exe") do set SIZE=%%~zF
echo   安装包大小: !SIZE! 字节
rem 源码 zip 实测 ~30MB (trae/lib 14MB + 桌面端 3.6MB + 卸载器 + 源码),
rem 加上安装器自身运行时约 40MB —— 低于 20MB 必然是资源没打进去。
if !SIZE! LSS 20000000 (
    echo [ERROR] 安装包体积异常偏小 ^(!SIZE! 字节, 预期 ^> 20 MB^) —— 源码资源很可能没打进去
    goto :err
)

rem ---- 5. 复制到桌面 ----
echo [5/5] 复制安装包到桌面...
rem ★ 不要用 %USERPROFILE%\Desktop 猜桌面: 桌面被 OneDrive / 组策略重定向后, 该目录
rem   **存在但已废弃**, 复制过去用户根本看不见。必须问 Windows 要真实路径。
set DESKTOP=
for /f "usebackq delims=" %%D in (`powershell -NoProfile -Command "[Environment]::GetFolderPath('Desktop')"`) do set DESKTOP=%%D
if not defined DESKTOP set DESKTOP=%USERPROFILE%\Desktop
if not exist "%DESKTOP%" set DESKTOP=%USERPROFILE%\Desktop
echo   桌面路径: %DESKTOP%
copy /y "dist\open-ai-installer-dev.exe" "%DESKTOP%\open-ai-installer-dev.exe" >nul
if errorlevel 1 (
    echo [WARN] 复制到桌面失败, 安装包在 dist\ 目录
) else (
    echo   [OK] 已复制到桌面: %DESKTOP%\open-ai-installer-dev.exe
)

echo ====================================================
echo  构建完成!
echo  安装包:   %DESKTOP%\open-ai-installer-dev.exe
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
