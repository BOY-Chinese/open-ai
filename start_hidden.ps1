# open-ai - 隐藏窗口后台启动脚本 (Node 后端 18787 + 网关 8000)
$ErrorActionPreference = "Stop"
$dir = $PSScriptRoot
# 日志集中到 logs/ 目录
$logsDir = Join-Path $dir "logs"
New-Item -ItemType Directory -Force -Path $logsDir | Out-Null
$py = Join-Path $dir ".venv\Scripts\python.exe"
$main = Join-Path $dir "main.py"
$log = Join-Path $logsDir "open_api.log"
$errLog = Join-Path $logsDir "open_api_err.log"

# ---- Node 运行时: 用系统 PATH 中的 node, 可自行改为本机 node 绝对路径 ----
$nodeExe = "node"
if (-not (Get-Command $nodeExe -ErrorAction SilentlyContinue)) {
    Write-Output "[Trae] 未找到 node, 请先安装 Node.js 18+"
    $nodeExe = $null
}
$traeServer = Join-Path $dir "trae\server.js"
$traeLog = Join-Path $logsDir "server.log"
$traeErrLog = Join-Path $logsDir "server_err.log"
$traePort = 18787

$gatewayPort = 8000

if (-not (Test-Path $py)) {
    Write-Error "未找到虚拟环境: $py  请先运行 start.bat"
    exit 1
}

# ==== 1. 启动 Trae Node 后端 (18787) ====
$traeListener = Get-NetTCPConnection -State Listen -LocalPort $traePort -ErrorAction SilentlyContinue
if (-not $traeListener) {
    if (-not $nodeExe) {
        Write-Output "[Trae] 跳过: 未找到 node (不影响网关启动)"
    } else {
        try {
            Write-Output "[Trae] 启动 Node 后端 (18787)..."
            $tp = Start-Process -FilePath $nodeExe -ArgumentList $traeServer -WorkingDirectory $dir `
                -WindowStyle Hidden -RedirectStandardOutput $traeLog -RedirectStandardError $traeErrLog `
                -PassThru
            Start-Sleep -Seconds 3
            if ($tp.HasExited) {
                Write-Output "[Trae] 警告: Node 后端启动后退出, 错误: $(Get-Content $traeErrLog -Raw -ErrorAction SilentlyContinue)"
                Write-Output "       请确认 trae/lib/ 目录完整 (sscronet.dll 存在)"
            } else {
                Write-Output "[Trae] Node 后端已启动 (PID $($tp.Id)), http://127.0.0.1:$traePort/v1"
            }
        } catch {
            Write-Output "[Trae] Node 后端启动失败: $_"
        }
    }
} else {
    Write-Output "[Trae] 端口 $traePort 已被占用, 后端可能已在运行 (PID $($traeListener.OwningProcess))"
}

# ==== 启动前自动签到 (内部脚本, 零外部依赖) ====
$signin = Join-Path $dir "scripts\signin_all.py"
try {
    Write-Output "[签到] 执行内部签到 (续期 + WorkBuddy/TRAE 签到)..."
    & $py $signin | Out-Host
} catch {
    Write-Output "[签到] 自动签到失败(不影响网关启动): $_"
}

# ==== 2. 启动 Python 网关 (8000) ====
$listener = Get-NetTCPConnection -State Listen -LocalPort $gatewayPort -ErrorAction SilentlyContinue
if ($listener) {
    Write-Output "[OK] 端口 $gatewayPort 已被占用, 网关可能已在运行 (PID $($listener.OwningProcess)), 跳过启动"
    exit 0
}

try {
    $p = Start-Process -FilePath $py -ArgumentList $main -WorkingDirectory $dir `
        -WindowStyle Hidden -RedirectStandardOutput $log -RedirectStandardError $errLog `
        -PassThru
    Start-Sleep -Seconds 3
    if ($p.HasExited) {
        Write-Error "网关启动后立即退出, 错误日志: $(Get-Content $errLog -Raw -ErrorAction SilentlyContinue)"
        exit 1
    }
    Write-Output "[OK] open-ai 已启动 (PID $($p.Id)), http://127.0.0.1:$gatewayPort/v1/chat/completions"
} catch {
    Write-Error "启动失败: $_"
    exit 1
}
