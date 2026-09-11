# open-ai 完整打包（自动化版，无 pause）— v3.0
# 与 build_exe.bat 等价，但可无人值守执行；产物与桌面复制逻辑保持一致。
$ErrorActionPreference = 'Continue'
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Here

$Py = 'python'
$Log = Join-Path $Here 'build_installer.log'
"==== open-ai installer build (v3.0) ====" | Out-File -FilePath $Log -Encoding utf8

function Step($n, $msg) { Write-Host "[$n] $msg" }

# ---- 0. 依赖 ----
foreach ($pkg in @('pyinstaller', 'pefile', 'pywin32-ctypes')) {
    & $Py -m pip show $pkg *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  installing $pkg ..."
        & $Py -m pip install $pkg -q 2>&1 | Out-File -Append $Log
    }
}

$Icon = Join-Path $Here 'ico\open-ai.ico'
$IconArg = if (Test-Path $Icon) { @('--icon', $Icon) } else { @() }

# ---- 1. uninstall.exe (onedir) ----
Step '1/4' 'uninstall.exe'
& $Py -m PyInstaller --noconfirm --clean --noupx --windowed `
    --contents-directory 'uninstall_internal' --name 'uninstall' @IconArg uninstaller.py *>> $Log
if ($LASTEXITCODE -ne 0) { Write-Host '[ERROR] uninstall 构建失败'; exit 1 }
Write-Host '  ok'

# ---- 2. open-ai-launcher.exe (onedir) ----
Step '2/4' 'open-ai-launcher.exe'
& $Py -m PyInstaller --noconfirm --clean --noupx --windowed `
    --contents-directory 'launcher_internal' --name 'open-ai-launcher' @IconArg launcher.py *>> $Log
if ($LASTEXITCODE -ne 0) { Write-Host '[ERROR] launcher 构建失败'; exit 1 }
Write-Host '  ok'

# ---- 3. resources.zip（含桌面端） ----
Step '3/4' 'resources.zip (含 desktop/ 桌面端)'
& $Py build_resources.py 2>&1 | Tee-Object -FilePath $Log -Append | ForEach-Object { Write-Host "  $_" }
if ($LASTEXITCODE -ne 0) { Write-Host '[ERROR] resources 打包失败'; exit 1 }

# ---- 4. 安装器 exe (onefile) ----
Step '4/4' 'open-ai-installer-dev.exe'
& $Py -m PyInstaller --noconfirm --clean --noupx --onefile --windowed --uac-admin `
    --name 'open-ai-installer-dev' @IconArg `
    --add-data 'resources.zip;.' --add-data 'config.shell.json;.' --add-data 'ico\open-ai.ico;ico' `
    installer.py *>> $Log
if ($LASTEXITCODE -ne 0) { Write-Host '[ERROR] 安装器构建失败'; exit 1 }

$Out = Join-Path $Here 'dist\open-ai-installer-dev.exe'
if (Test-Path $Out) {
    Write-Host ("[DONE] {0} ({1:N1} MB)" -f $Out, ((Get-Item $Out).Length / 1MB))
} else {
    Write-Host '[ERROR] 未生成安装器'
    exit 1
}
