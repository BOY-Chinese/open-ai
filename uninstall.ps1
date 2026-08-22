# open-ai 一键卸载脚本 - 彻底删除 (由账号管理GUI调用)
# 作用: 停止所有相关进程 + 移除开机自启/计划任务 + 删除整个插件目录
# 用法: powershell -NoProfile -ExecutionPolicy Bypass -File uninstall.ps1

$ErrorActionPreference = "Continue"
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$failed = $false

Write-Output "[卸载] 开始彻底卸载 open-ai ..."

# 1) 移除开机自启 (启动文件夹中的 open-ai-autostart.bat)
$startupDir = [Environment]::GetFolderPath("Startup")
$autostartDst = Join-Path $startupDir "open-ai-autostart.bat"
try {
    if (Test-Path $autostartDst) {
        Remove-Item -Path $autostartDst -Force
        Write-Output "[OK] 已移除开机自启: $autostartDst"
    } else {
        Write-Output "[跳过] 启动文件夹中未找到自启项"
    }
} catch {
    Write-Output "[错误] 移除自启失败: $_"
    $failed = $true
}

# 2) 停止 daemon 进程 (由 PID 文件定位, 现位于 data/ 目录)
$pidFile = Join-Path $dir "data\.daemon.pid"
try {
    if (Test-Path $pidFile) {
        $pidVal = (Get-Content $pidFile -Raw).Trim()
        if ($pidVal -match '^\d+$') {
            $p = Get-Process -Id ([int]$pidVal) -ErrorAction SilentlyContinue
            if ($p) {
                Stop-Process -Id ([int]$pidVal) -Force
                Write-Output "[OK] 已停止 daemon 进程 (PID $pidVal)"
            } else {
                Write-Output "[跳过] daemon 进程 (PID $pidVal) 不存在"
            }
        }
    }
} catch {
    Write-Output "[错误] 停止 daemon 失败: $_"
    $failed = $true
}

# 3) 停止占用 8000 / 18787 端口的进程 (网关 + Node 后端)
try {
    foreach ($port in 8000, 18787) {
        $conns = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
        foreach ($c in $conns) {
            $ownPid = $c.OwningProcess
            try {
                $own = Get-Process -Id $ownPid -ErrorAction SilentlyContinue
                $pname = if ($own) { $own.ProcessName } else { "PID$ownPid" }
                Stop-Process -Id $ownPid -Force -ErrorAction SilentlyContinue
                Write-Output "[OK] 已停止端口 $port 占用进程 ($pname / PID $ownPid)"
            } catch {
                Write-Output "[跳过] 端口 $port 进程 PID $ownPid 已退出"
            }
        }
    }
} catch {
    Write-Output "[错误] 停止端口进程失败: $_"
    $failed = $true
}

# 4) 移除计划任务 (OpenAI-Watchdog / OpenAI-DaemonBoot, 若存在)
try {
    foreach ($task in "OpenAI-Watchdog", "OpenAI-DaemonBoot") {
        $existing = Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue
        if ($existing) {
            Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction SilentlyContinue
            Write-Output "[OK] 已移除计划任务 $task"
        } else {
            Write-Output "[跳过] 计划任务 $task 不存在"
        }
    }
} catch {
    Write-Output "[跳过] 计划任务处理失败(可忽略): $_"
}

# 5) 停止托管网关/daemon 的 pythonw / node (按命令行匹配, 防止误杀无关进程)
try {
    $targets = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        ($_.Name -match 'pythonw?\.exe' -or $_.Name -match '^node\.exe$') -and
        ($_.CommandLine -match 'open-ai' -or $_.CommandLine -match 'main\.py' -or
         $_.CommandLine -match 'daemon\.py' -or $_.CommandLine -match 'watchdog_boot\.py' -or
         $_.CommandLine -match 'trae\\server\.js' -or $_.CommandLine -match 'server\.js')
    }
    foreach ($t in $targets) {
        if ($t.ProcessId -eq $PID) { continue }  # 跳过当前进程
        Stop-Process -Id $t.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Output "[OK] 已停止进程 $($t.Name) (PID $($t.ProcessId))"
    }
} catch {
    Write-Output "[跳过] 进程遍历失败(可忽略): $_"
}

# 6) 强制终止占用本目录的 python (GUI 本身), 稍后延迟执行全局删除
#    先关闭 GUI: 查找命令行包含 gui_account_manager.py 的 pythonw 并终止
try {
    $guis = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.CommandLine -match 'gui_account_manager\.py'
    }
    foreach ($g in $guis) {
        Stop-Process -Id $g.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Output "[OK] 已停止账号管理界面 (PID $($g.ProcessId))"
    }
} catch {
    Write-Output "[跳过] 停止界面失败(可忽略): $_"
}

Write-Output "[卸载] 等待 2 秒释放文件占用 ..."
Start-Sleep -Seconds 2

# 7) 删除整个插件目录 (彻底卸载)
try {
    if (Test-Path $dir) {
        # 尝试普通删除
        Remove-Item -Path $dir -Recurse -Force -ErrorAction Stop
        Write-Output "[OK] 已删除插件目录: $dir"
    } else {
        Write-Output "[跳过] 插件目录不存在"
    }
} catch {
    Write-Output "[警告] 直接删除目录失败, 尝试延迟删除..."
    try {
        # 延迟删除: 启动一个分离进程稍后删除本目录
        $delScript = "Start-Sleep -Seconds 3; Remove-Item -LiteralPath '$dir' -Recurse -Force -ErrorAction SilentlyContinue"
        Start-Process -FilePath "powershell.exe" -ArgumentList "-NoProfile -ExecutionPolicy Bypass -Command `"$delScript`"" -WindowStyle Hidden
        Write-Output "[OK] 已安排延迟删除目录: $dir"
    } catch {
        Write-Output "[错误] 目录删除失败: $_"
        $failed = $true
    }
}

Write-Output ""
if ($failed) {
    Write-Output "[结果] 卸载完成, 但部分步骤出现错误, 请检查上方日志。"
} else {
    Write-Output "[结果] 彻底卸载完成! 所有文件、配置、账号与密钥均已清除。"
}