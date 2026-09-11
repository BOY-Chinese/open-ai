# 启动 Tauri 桌面应用（开发模式，需 dev server 在 1420 端口）
$Exe = 'D:\app\dsh_plugin\open-ai\desktop-ui\src-tauri\target\debug\open-ai-desktop.exe'
if (-not (Test-Path $Exe)) { Write-Host "[FATAL] exe not found"; exit 1 }

Start-Process -FilePath $Exe -WorkingDirectory (Split-Path $Exe) | Out-Null
Start-Sleep -Seconds 8

$p = Get-Process -Name 'open-ai-desktop' -ErrorAction SilentlyContinue
if ($p) {
  foreach ($x in $p) {
    $hasWin = $x.MainWindowHandle -ne 0
    Write-Host ("[RUNNING] pid={0}  mem={1:N1} MB  window={2}  title='{3}'" -f `
      $x.Id, ($x.WorkingSet64/1MB), $hasWin, $x.MainWindowTitle)
  }
} else {
  Write-Host "[EXITED] 进程未存活（可能启动即崩溃）"
}
