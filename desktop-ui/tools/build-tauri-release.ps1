# Tauri 生产构建（release）——使用独立工具链，绕开 rustup
$ErrorActionPreference = 'Continue'
$Root      = 'D:\app\dsh_plugin\open-ai'
$Toolchain = "$Root\toolchain\bin"
$TauriDir  = "$Root\desktop-ui\src-tauri"
$CargoHome = 'C:\Users\Lenovo\.cargo'

$env:CARGO_HOME = $CargoHome
$env:PATH = "$Toolchain;$CargoHome\bin;$env:PATH"

Write-Host "=== 工具链 ==="
& "$Toolchain\rustc.exe" --version

Set-Location $TauriDir
Write-Host "=== cargo build --release ==="
$log = "$TauriDir\build-release.log"
$sw = [System.Diagnostics.Stopwatch]::StartNew()
& "$Toolchain\cargo.exe" build --release --color never 2>&1 | Tee-Object -FilePath $log | Out-Null
$code = $LASTEXITCODE
$sw.Stop()
Write-Host ("=== exit={0} 耗时 {1:N1} 分钟 ===" -f $code, $sw.Elapsed.TotalMinutes)

$exe = "$TauriDir\target\release\open-ai-desktop.exe"
if (Test-Path $exe) {
  Write-Host ("[OK] {0} ({1:N1} MB)" -f $exe, ((Get-Item $exe).Length / 1MB))
} else {
  Write-Host "[FAIL] 未生成 release 产物"
  Get-Content $log -Tail 15
}
