# 截取 open-ai-desktop 窗口（含精确边界，便于裁剪）
#
# ★ DPI 感知必须显式开启（v3.0.1 修复）
#   PowerShell 默认不是 DPI-aware，进程内所有窗口坐标都会被系统**虚拟化**：
#   GetWindowRect 返回逻辑像素（如 1195×817），而 CopyFromScreen 也按逻辑像素
#   取图 —— 两者看似自洽，但在 150% 缩放的机器上，实际窗口是 1792×1226 物理像素，
#   于是截出来的图右边和下边各被切掉约三分之一：表格只剩前两列，
#   却被当成「界面就是这样」。本机实测踩到，故在取任何坐标前先 SetProcessDPIAware()。
Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class W {
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out R r);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
  public struct R { public int L, T, Rt, B; }
}
"@

# 必须早于任何 GetWindowRect / CopyFromScreen 调用
[W]::SetProcessDPIAware() | Out-Null

$p = Get-Process -Name 'open-ai-desktop' -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $p) { Write-Host "[FATAL] app not running"; exit 1 }

$h = $p.MainWindowHandle
[W]::ShowWindow($h, 9) | Out-Null      # SW_RESTORE
[W]::SetForegroundWindow($h) | Out-Null
Start-Sleep -Milliseconds 1200

$r = New-Object W+R
[W]::GetWindowRect($h, [ref]$r) | Out-Null
$w = $r.Rt - $r.L
$ht = $r.B - $r.T
Write-Host ("window rect(物理像素): L={0} T={1} W={2} H={3}" -f $r.L, $r.T, $w, $ht)

$bmp = New-Object System.Drawing.Bitmap($w, $ht)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($r.L, $r.T, 0, 0, $bmp.Size)
# 仓库根 = 本脚本所在目录 (desktop-ui/tools) 上溯两级
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$out = Join-Path $Root 'desktop-ui\docs\screenshots\tauri-app.png'
$bmp.Save($out, [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $bmp.Dispose()
Write-Host ("[OK] saved {0}  {1:N0} bytes" -f $out, (Get-Item $out).Length)
