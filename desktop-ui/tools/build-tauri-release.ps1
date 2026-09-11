# Tauri 生产构建（v3.0 修正版）
#
# 关键修正：必须让 tauri-build 以「生产模式」运行，否则即使启用了
# custom-protocol，仍可能嵌入 devUrl。tauri-build 依据 TAURI_ENV_DEBUG 判断：
#   TAURI_ENV_DEBUG=false  → 生产（用 frontendDist 内嵌资源）
#   unset / true           → 开发（连 devUrl 127.0.0.1:1420）
$ErrorActionPreference = 'Continue'
$Root      = 'D:\app\dsh_plugin\open-ai'
$Toolchain = "$Root\toolchain\bin"
$TauriDir  = "$Root\desktop-ui\src-tauri"
$CargoHome = 'C:\Users\Lenovo\.cargo'

$env:CARGO_HOME = $CargoHome
$env:PATH = "$Toolchain;$CargoHome\bin;$env:PATH"

# ★ 生产模式标志（这是本次失败的关键）
$env:TAURI_ENV_DEBUG = 'false'
$env:TAURI_ENV_TARGET_TRIPLE = 'x86_64-pc-windows-msvc'

Write-Host "=== 工具链 ==="
& "$Toolchain\rustc.exe" --version
Write-Host "TAURI_ENV_DEBUG=$env:TAURI_ENV_DEBUG (false=生产构建)"

Set-Location $TauriDir
Write-Host "=== cargo build --release (custom-protocol 生效) ==="
$log = "$TauriDir\build-release.log"
$sw = [System.Diagnostics.Stopwatch]::StartNew()
& "$Toolchain\cargo.exe" build --release --features custom-protocol --color never 2>&1 |
    Tee-Object -FilePath $log | Out-Null
$code = $LASTEXITCODE
$sw.Stop()
Write-Host ("=== exit={0} 耗时 {1:N1} 分钟 ===" -f $code, $sw.Elapsed.TotalMinutes)

$exe = "$TauriDir\target\release\open-ai-desktop.exe"
if (Test-Path $exe) {
    Write-Host ("[OK] {0} ({1:N1} MB)" -f $exe, ((Get-Item $exe).Length / 1MB))
} else {
    Write-Host "[FAIL] 未生成 release 产物"
    Get-Content $log -Tail 20
}
