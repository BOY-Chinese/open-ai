# check-tray.ps1 - Runtime proof that the tray icon actually exists.
#
# WHY
#   "Tray icon missing" is invisible to the build: the app compiles, the window
#   renders, and only the notification area stays empty. The user hit exactly
#   that on a VM, so we verify at runtime instead of trusting the code.
#
# HOW
#   The Rust `tray-icon` crate registers a window class named `tray_icon_app`.
#   That window is created by CreateWindowExW with a NULL parent, i.e. it is a
#   normal (but never shown) TOP-LEVEL window - NOT a message-only window, so
#   FindWindowEx(HWND_MESSAGE, ...) finds nothing. We therefore walk every
#   top-level window with EnumWindows and match both class name and owning pid.
#
# WHAT IT PROVES
#   The window only exists as part of the icon-creation path, and a failed
#   Shell_NotifyIcon(NIM_ADD) makes `build_tray` return Err, which aborts the
#   app during setup. So "process alive + tray window owned by it" means the
#   icon was handed to the shell.
param(
    [Parameter(Mandatory = $true)][string]$ProcessName,
    [string]$ClassName = 'tray_icon_app'
)

$ErrorActionPreference = 'Stop'

Add-Type @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
public static class TrayProbe {
    private delegate bool EnumProc(IntPtr hWnd, IntPtr lParam);

    [DllImport("user32.dll")] private static extern bool EnumWindows(EnumProc cb, IntPtr lParam);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern int GetClassNameW(IntPtr hWnd, StringBuilder buf, int max);
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);

    public static List<string> Find(uint targetPid, string cls) {
        var hits = new List<string>();
        EnumWindows(delegate(IntPtr hWnd, IntPtr lParam) {
            var sb = new StringBuilder(256);
            GetClassNameW(hWnd, sb, sb.Capacity);
            if (sb.ToString() == cls) {
                uint pid;
                GetWindowThreadProcessId(hWnd, out pid);
                if (pid == targetPid) hits.Add(hWnd.ToString());
            }
            return true;
        }, IntPtr.Zero);
        return hits;
    }
}
'@

$procs = @(Get-Process -Name $ProcessName -ErrorAction SilentlyContinue)
if ($procs.Count -eq 0) {
    Write-Host ("[FAIL] 进程未运行: {0}" -f $ProcessName)
    exit 1
}

$total = 0
foreach ($p in $procs) {
    $hits = [TrayProbe]::Find([uint32]$p.Id, $ClassName)
    Write-Host ("pid {0,-6} {1,-18} tray窗口 {2} 个" -f $p.Id, $p.ProcessName, $hits.Count)
    $total += $hits.Count
}

# 交叉验证（Windows 11）：通知区域登记表里应能看到本程序的 exe
$reg = 'HKCU:\Control Panel\NotifyIconSettings'
if (Test-Path $reg) {
    $exe = ($procs | Select-Object -First 1).Path
    if ($exe) {
        $regHit = $false
        foreach ($k in Get-ChildItem $reg -ErrorAction SilentlyContinue) {
            $v = (Get-ItemProperty $k.PSPath -ErrorAction SilentlyContinue).ExecutablePath
            if ($v -and ($v -ieq $exe)) { $regHit = $true; break }
        }
        $label = if ($regHit) { '已登记' } else { '未登记（多被系统归入溢出区，非错误）' }
        Write-Host ("通知区域登记表: {0}" -f $label)
    }
}

if ($total -gt 0) {
    Write-Host ("[ OK ] 托盘图标已创建（{0} 个 {1} 顶层窗口）" -f $total, $ClassName)
    exit 0
}
Write-Host ("[FAIL] 未发现 {0} 窗口 —— 托盘图标未创建" -f $ClassName)
exit 1
