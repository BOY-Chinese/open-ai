# open-ai - hidden startup (v3.0)
#
# 职责：开机/登录时静默拉起「后端进程树 + 托盘界面」。
#
# 为什么要启动界面（v3.0）：
#   托盘图标由**界面进程**创建 —— 裸 Broker 进程没有任何托盘能力。
#   只拉起 Broker 会表现为「后端已在跑，但托盘里没有图标」：
#   用户既看不到运行状态，也没有恢复界面 / 退出的入口。
#   桌面端带 --minimized 启动 → 窗口不弹出，只留托盘图标。
#
# 界面只有一个入口：desktop\open-ai-desktop.exe（Tauri + React）。
# 旧的 Python tkinter GUI（gui_account_manager.py / tray_icon.py /
# open-ai-manager.exe）已在 v3.0 整条删除，这里不再有任何 Python 界面兜底分支。
#
# 幂等性：
#   - Broker 侧由 bootstrap.py 单实例锁保证（已在跑则直接返回）
#   - 界面侧由 Tauri 单实例插件保证：已有实例时新进程会唤醒既有窗口后退出，
#     不会出现「双托盘 + 双窗口」
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

# ── 2) 界面：桌面端（唯一入口）──
# 稍等 Broker 就绪再拉界面，避免首启时界面抢在网关前面显示「未连接」
Start-Sleep -Milliseconds 800

$desktopExe = Join-Path $dir "desktop\open-ai-desktop.exe"

if (Test-Path $desktopExe) {
    # --minimized = 只驻留托盘不弹窗；托盘图标由桌面端自带
    Start-Process -FilePath $desktopExe -ArgumentList "--minimized" `
        -WorkingDirectory (Split-Path $desktopExe) -WindowStyle Hidden
    Write-Output "[OK] open-ai broker + desktop UI launch requested (tray, idempotent)"
} else {
    Write-Output "[WARN] 未找到 desktop\open-ai-desktop.exe，仅启动了后端（界面无法打开）"
}
