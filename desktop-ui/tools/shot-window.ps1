# 截取 open-ai-desktop 窗口（含精确边界，便于裁剪）
Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class W {
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out R r);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
  public struct R { public int L, T, Rt, B; }
}
"@
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
Write-Host ("window rect: L={0} T={1} W={2} H={3}" -f $r.L, $r.T, $w, $ht)

$bmp = New-Object System.Drawing.Bitmap($w, $ht)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($r.L, $r.T, 0, 0, $bmp.Size)
$out = 'D:\app\dsh_plugin\open-ai\desktop-ui\docs\screenshots\tauri-app.png'
$bmp.Save($out, [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $bmp.Dispose()
Write-Host ("[OK] saved {0}  {1:N0} bytes" -f $out, (Get-Item $out).Length)
