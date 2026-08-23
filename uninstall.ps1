# open-ai 一键卸载脚本 - 彻底删除 (由账号管理GUI调用)
# =============================================================
# 修复: 之前的版本在 open-ai 目录内运行, 无法删除自身所在目录。
# 本版本改为: 先杀掉相关进程/清理自启与快捷方式,
#            再把删除脚本复制到 TEMP (目录外), 用分离进程从 System32 删除整个目录。
# 用法: powershell -NoProfile -ExecutionPolicy Bypass -File uninstall.ps1
# =============================================================

$ErrorActionPreference = "Continue"
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$targetDir = $dir

Write-Output "[卸载] 开始彻底卸载 open-ai ..."

# ---- 1) 移除开机自启 (启动文件夹中的 open-ai-autostart.bat) ----
$startupDir = [Environment]::GetFolderPath("Startup")
$autostartDst = Join-Path $startupDir "open-ai-autostart.bat"
try {
    if (Test-Path $autostartDst) {
        Remove-Item -Path $autostartDst -Force -ErrorAction Stop
        Write-Output "[OK] 已移除开机自启"
    } else {
        Write-Output "[跳过] 启动文件夹中未找到自启项"
    }
} catch {
    Write-Output "[错误] 移除自启失败: $_"
}

# ---- 2) 移除桌面快捷方式 ----
try {
    $desktopPaths = @(
        (Join-Path ([Environment]::GetFolderPath('Desktop')) 'open-ai.lnk'),
        (Join-Path ([Environment]::GetFolderPath('Desktop')) 'open-ai 账号管理.lnk'),
        (Join-Path ([Environment]::GetFolderPath('Desktop')) 'open-ai 启动网关.lnk')
    )
    foreach ($lnk in $desktopPaths) {
        if (Test-Path $lnk) {
            Remove-Item $lnk -Force -ErrorAction SilentlyContinue
            Write-Output "[OK] 已移除桌面快捷方式: $(Split-Path $lnk -Leaf)"
        }
    }
} catch {
    Write-Output "[跳过] 移除快捷方式失败: $_"
}

# ---- 3) 停止 daemon (按 data/.daemon.pid + 兜底) ----
$pidFile = Join-Path $dir "data\.daemon.pid"
try {
    if (Test-Path $pidFile) {
        $pidVal = (Get-Content $pidFile -Raw).Trim()
        if ($pidVal -match '^\d+$') {
            Stop-Process -Id ([int]$pidVal) -Force -ErrorAction SilentlyContinue
            Write-Output "[OK] 已停止 daemon (PID $pidVal)"
        }
    }
    # 兜底: 杀目标目录下的 python/node 进程
    $esc = [regex]::Escape($targetDir)
    $procs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        ($_.Name -match 'pythonw?\.exe' -or $_.Name -match '^node\.exe$') -and
        $_.CommandLine -match $esc -and $_.ProcessId -ne $PID
    }
    foreach ($p in $procs) {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Output "[OK] 已停止进程 $($p.Name) (PID $($p.ProcessId))"
    }
} catch {
    Write-Output "[跳过] 停止 daemon: $_"
}

# ---- 4) 停止占用 8000 / 18787 的进程 ----
try {
    foreach ($port in 8000, 18787) {
        $conns = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
        foreach ($c in $conns) {
            Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue
            Write-Output "[OK] 已停止端口 $port 进程 (PID $($c.OwningProcess))"
        }
    }
} catch {
    Write-Output "[跳过] 停止端口进程: $_"
}

# ---- 5) 移除计划任务 ----
try {
    foreach ($task in "OpenAI-Watchdog", "OpenAI-DaemonBoot") {
        $existing = Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue
        if ($existing) {
            Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction SilentlyContinue
            Write-Output "[OK] 已移除计划任务 $task"
        }
    }
} catch {
    Write-Output "[跳过] 计划任务处理失败: $_"
}

Write-Output "[卸载] 等待 3 秒释放文件占用 ..."
Start-Sleep -Seconds 3

# ---- 6) 关闭账号管理GUI (自身调用者) ----
try {
    $guis = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.CommandLine -match 'gui_account_manager\.py'
    }
    foreach ($g in $guis) {
        Stop-Process -Id $g.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Output "[OK] 已关闭账号管理界面 (PID $($g.ProcessId))"
    }
} catch {
    Write-Output "[跳过] 关闭界面失败: $_"
}

# ---- 7) 删除整个插件目录 (关键修复: 从目录外删除) ----
Write-Output "[卸载] 准备删除目录: $targetDir"
try {
    $tempScript = Join-Path $env:TEMP ("openai_uninstall_" + [guid]::NewGuid().ToString('N') + ".ps1")
    $delScript = @"
`$ErrorActionPreference = 'Continue'
Start-Sleep -Seconds 1
if (Test-Path '$targetDir') {
    Remove-Item -LiteralPath '$targetDir' -Recurse -Force -ErrorAction SilentlyContinue
}
Remove-Item -LiteralPath '$tempScript' -Force -ErrorAction SilentlyContinue
"@
    Set-Content -Path $tempScript -Value $delScript -Encoding UTF8
    # 从 System32 启动分离进程, 避免占用目标目录
    Start-Process -FilePath "powershell.exe" `
        -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$tempScript`"" `
        -WorkingDirectory "$env:WINDIR\System32" -WindowStyle Hidden
    Write-Output "[OK] 已安排删除目录 (延迟执行, 立即释放本窗口)"
} catch {
    Write-Output "[错误] 目录删除安排失败: $_"
    $failed = $true
}

Write-Output ""
if ($failed) {
    Write-Output "[结果] 卸载已触发, 但有部分步骤出错, 请检查上方日志。"
} else {
    Write-Output "[结果] 卸载完成! 目录将于 1 秒后自动删除。"
}