# open-ai - hidden startup (v3.0)
#
# 职责：开机/登录时静默拉起「后端进程树 + 托盘 GUI」。
#
# 为什么要启动 GUI（v3.0 修复）：
#   托盘图标由 GUI 进程（gui_account_manager.py）创建 —— 裸 Broker 进程没有任何
#   托盘能力。v2.4 的自启只拉起 Broker，导致「后端已在跑但托盘没有图标」：
#   用户既看不到运行状态，也没有恢复界面 / 退出的入口。
#   GUI 带 --minimized 启动 → 窗口不弹出，只留托盘图标。
#
# 幂等性：
#   - Broker 侧由 bootstrap.py 单实例锁保证（已在跑则直接返回）
#   - GUI 侧由 named mutex (Local\open-ai.gui.mutex) 保证：已有实例时新进程
#     会唤醒既有窗口并立即退出，不会出现双托盘
$ErrorActionPreference = "SilentlyContinue"
$dir = $PSScriptRoot

# ── 1) 后端：Broker 进程树（gateway :8000 + trae node :18787 + tasks）──
$brokerShim = Join-Path $dir "runtime\Scripts\open-ai-daemon.exe"
$bootstrap  = Join-Path $dir "bootstrap.py"

if (Test-Path $brokerShim) {
    Start-Process -FilePath $brokerShim -ArgumentList "`"$bootstrap`"","start" `
        -WorkingDirectory $dir -WindowStyle Hidden
} else {
    # shim 缺失（全新安装未生成）：回落 venv pythonw
    $pyw = Join-Path $dir ".venv\Scripts\pythonw.exe"
    if (Test-Path $pyw) {
        Start-Process -FilePath $pyw -ArgumentList "`"$bootstrap`"","start" `
            -WorkingDirectory $dir -WindowStyle Hidden
    }
}

# ── 2) 图形界面：v3.0 优先拉起桌面端（Tauri），缺失时回退 Python 托盘 GUI ──
# 稍等 Broker 就绪再拉界面，避免首启时界面抢在网关前面显示「未运行」
Start-Sleep -Milliseconds 800

$desktopExe  = Join-Path $dir "desktop\open-ai-desktop.exe"
$managerShim = Join-Path $dir "runtime\Scripts\open-ai-manager.exe"
$guiScript   = Join-Path $dir "scripts\gui_account_manager.py"
$pyw         = Join-Path $dir ".venv\Scripts\pythonw.exe"

if (Test-Path $desktopExe) {
    # 新桌面端（Tauri + React）：自带窗口，无需 --minimized 语义
    Start-Process -FilePath $desktopExe -WorkingDirectory (Split-Path $desktopExe) `
        -WindowStyle Hidden
    Write-Output "[OK] open-ai broker + desktop UI launch requested (idempotent)"
} elseif (Test-Path $managerShim) {
    Start-Process -FilePath $managerShim -ArgumentList "`"$guiScript`"","--minimized" `
        -WorkingDirectory $dir -WindowStyle Hidden
    Write-Output "[OK] open-ai broker + tray GUI launch requested (manager shim)"
} elseif (Test-Path $pyw) {
    Start-Process -FilePath $pyw -ArgumentList "`"$guiScript`"","--minimized" `
        -WorkingDirectory $dir -WindowStyle Hidden
    Write-Output "[OK] open-ai broker + tray GUI launch requested (pythonw fallback)"
} else {
    Write-Output "[WARN] GUI 入口缺失，仅启动了后端"
}
