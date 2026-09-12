# install-local-shortcut.ps1 - Point the desktop shortcut at the NEW Tauri desktop app.
#
# WHY
#   The shortcut used to launch <root>\open-ai-launcher.exe, which hard-coded the
#   legacy Python/tkinter GUI. After v3.0 moved the whole UI to desktop-ui
#   (Tauri + React) the shortcut silently kept opening the OLD window.
#   This script repoints it at the desktop build and keeps the icon.
#
# The target path mirrors the INSTALLED layout (<root>\desktop\open-ai-desktop.exe),
# so the local machine and a fresh VM install run the exact same entry point.
param(
    [string]$Root = 'D:\app\dsh_plugin\open-ai',
    [string]$ShortcutName = 'open-ai.lnk'
)

$ErrorActionPreference = 'Stop'

$exe = Join-Path $Root 'desktop\open-ai-desktop.exe'
if (-not (Test-Path $exe)) {
    Write-Host "[FAIL] 未找到桌面端产物: $exe"
    Write-Host "       请先运行 desktop-ui\tools\build-tauri-release.ps1"
    exit 1
}

$icon = Join-Path $Root 'pic\open-ai.ico'
if (-not (Test-Path $icon)) { $icon = $exe }

$desktop = [Environment]::GetFolderPath('Desktop')
$lnkPath = Join-Path $desktop $ShortcutName

$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut($lnkPath)   # 已存在则读取，保留其余属性
$lnk.TargetPath       = $exe
$lnk.WorkingDirectory = Split-Path $exe
$lnk.IconLocation     = "$icon,0"
$lnk.Description      = 'open-ai 账号管理'
$lnk.Save()

# 读回校验：快捷方式写了不等于写对了（路径含空格/中文时尤其容易出错）
$check = $shell.CreateShortcut($lnkPath)
Write-Host ("[OK] {0}" -f $lnkPath)
Write-Host ("     Target : {0}" -f $check.TargetPath)
Write-Host ("     WorkDir: {0}" -f $check.WorkingDirectory)
Write-Host ("     Icon   : {0}" -f $check.IconLocation)

if ($check.TargetPath -ne $exe) {
    Write-Host '[FAIL] 写回校验不一致'
    exit 1
}
