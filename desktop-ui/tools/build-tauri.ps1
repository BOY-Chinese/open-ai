# open-ai Tauri 2 构建脚本
#
# 使用「手工组装的独立工具链」（open-ai/toolchain），完全绕开 rustup shim：
# 本机 rustup 的 manifest 拉取在国内网络下反复中断，shim 会报 Missing manifest。
$ErrorActionPreference = 'Continue'

$Root      = 'D:\app\dsh_plugin\open-ai'
$Toolchain = "$Root\toolchain\bin"
$TauriDir  = "$Root\desktop-ui\src-tauri"
$env:CARGO_HOME = 'C:\Users\Lenovo\.cargo'

Write-Host "=== 工具链 ==="
if (-not (Test-Path "$Toolchain\rustc.exe")) {
  Write-Host "[FATAL] 未找到 $Toolchain\rustc.exe"
  exit 1
}
& "$Toolchain\rustc.exe" --version
& "$Toolchain\cargo.exe" --version
Write-Host ""

Set-Location $TauriDir
Write-Host "=== cargo build 开始 ==="
$sw = [System.Diagnostics.Stopwatch]::StartNew()
$log = "$TauriDir\build-" + (Get-Date -Format "HHmmss") + ".log"
& "$Toolchain\cargo.exe" build --color never 2>&1 | Tee-Object -FilePath $log
$code = $LASTEXITCODE
$sw.Stop()

Write-Host ""
Write-Host ("=== 结束：exit={0}，耗时 {1:N1} 分钟 ===" -f $code, $sw.Elapsed.TotalMinutes)

$exe = "$TauriDir\target\debug\open-ai-desktop.exe"
if (Test-Path $exe) {
  Write-Host ("[OK] 产物: {0} ({1:N0} bytes)" -f $exe, (Get-Item $exe).Length)
} else {
  Write-Host "[FAIL] 未生成可执行文件"
}
