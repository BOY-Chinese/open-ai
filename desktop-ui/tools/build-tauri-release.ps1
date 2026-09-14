# Tauri 生产构建（v3.0 修正版）
#
# 关键修正：必须让 tauri-build 以「生产模式」运行，否则即使启用了
# custom-protocol，仍可能嵌入 devUrl。tauri-build 依据 TAURI_ENV_DEBUG 判断：
#   TAURI_ENV_DEBUG=false  → 生产（用 frontendDist 内嵌资源）
#   unset / true           → 开发（连 devUrl 127.0.0.1:1420）
$ErrorActionPreference = 'Continue'
# 仓库根 = 本脚本所在目录 (desktop-ui/tools) 上溯两级。
# ★ 不再写死 D:\app\... —— 那是开发机的私有布局, 别人 clone 下来必然跑不通。
$Root      = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Toolchain = "$Root\toolchain\bin"
$TauriDir  = "$Root\desktop-ui\src-tauri"
# CARGO_HOME 默认落在用户目录下, 而该目录名就是本机登录用户名 —— 不能写死
$CargoHome = if ($env:CARGO_HOME) { $env:CARGO_HOME } else { Join-Path $env:USERPROFILE '.cargo' }

$env:CARGO_HOME = $CargoHome
$env:PATH = "$Toolchain;$CargoHome\bin;$env:PATH"

# ★ 把构建期绝对路径从二进制里剥掉。
#   rustc 会把 panic 位置等信息以**字符串字面量**留在 exe 里, 于是最终用户的
#   安装包中会躺着 C:\Users\<name>\.cargo\registry\... 这类开发机路径
#   (换机即失效, 且泄漏本机用户名 —— 隐私检查会拦它)。
#   --remap-path-prefix 把这些前缀重写成中性名字, 不影响功能, 只影响 panic 回栈
#   里显示的路径。两条都要: 依赖走 CargoHome, 本项目源码走 $Root。
$env:RUSTFLAGS = (
    "--remap-path-prefix=$CargoHome=/.cargo " +
    "--remap-path-prefix=$Root=/open-ai"
)

# ★ 生产模式标志（这是「虚拟机打不开页面」那次失败的关键）
$env:TAURI_ENV_DEBUG = 'false'
$env:TAURI_ENV_TARGET_TRIPLE = 'x86_64-pc-windows-msvc'

# ── 断言：上面两行必须真的生效 ─────────────────────────────
# PowerShell 5.1 会以 ANSI(GBK) 解码「无 BOM 的 UTF-8 脚本」，中文注释的
# 最后一个字节可能与紧随的换行配成双字节字符，从而**整行吞掉下一条语句**。
# 本项目已踩过：$env:TAURI_ENV_DEBUG 被吞 → 打包态重新嵌入 devUrl
# → 虚拟机 ERR_CONNECTION_REFUSED。此处把「静默失效」变成立即失败。
# （本文件必须保存为 UTF-8 with BOM；desktop-ui/tools/check-ps1.ps1 可批量校验）
if ($env:TAURI_ENV_DEBUG -ne 'false') {
    throw "TAURI_ENV_DEBUG 未生效（当前值：'$env:TAURI_ENV_DEBUG'）。请确认本文件为 UTF-8 with BOM。"
}

Write-Host "=== 工具链 ==="
& "$Toolchain\rustc.exe" --version
Write-Host "TAURI_ENV_DEBUG=$env:TAURI_ENV_DEBUG (false=生产构建)"

Set-Location $TauriDir
Write-Host "=== cargo build --release (custom-protocol 生效) ==="
$log = "$TauriDir\build-release.log"
$sw = [System.Diagnostics.Stopwatch]::StartNew()
& "$Toolchain\cargo.exe" build --release --features custom-protocol --color never 2>&1 |
    Tee-Object -FilePath $log | Out-Null
$code = $LASTEXITCODE
$sw.Stop()
Write-Host ("=== exit={0} 耗时 {1:N1} 分钟 ===" -f $code, $sw.Elapsed.TotalMinutes)

$exe = "$TauriDir\target\release\open-ai-desktop.exe"

# ★ 必须用 $code 判定成败，而不是「exe 是否存在」：
#   cargo 失败时 target/release 里往往还躺着**上一次**的旧 exe，
#   若只看文件存在，脚本会复制一个陈旧产物并打印 [OK] ——
#   本项目已因此把旧包当成新包交付过一次（exit=101 仍报成功）。
if ($code -ne 0) {
    Write-Host ("[FAIL] cargo 构建失败 (exit={0})，未产出新 exe；保留旧产物不动" -f $code)
    Get-Content $log -Tail 25
    exit 1
}

if (Test-Path $exe) {
    Write-Host ("[OK] {0} ({1:N1} MB)" -f $exe, ((Get-Item $exe).Length / 1MB))

    # 同步到 <项目根>\desktop\ —— 与安装包内的目录布局完全一致，
    # 这样「本机开发」与「虚拟机安装」跑的是同一套路径：
    #   本机快捷方式 / start_hidden.ps1 自启 / installer launcher 都指向它
    $local = "$Root\desktop"
    New-Item -ItemType Directory -Force -Path $local | Out-Null
    $dst = "$local\open-ai-desktop.exe"

    # 正在运行的实例会锁住目标 exe，直接复制必然失败
    # （失败若不检查，脚本照样打印 [OK] —— 本项目已踩过不止一次）
    $running = @(Get-Process -Name 'open-ai-desktop' -ErrorAction SilentlyContinue)
    if ($running.Count -gt 0) {
        Write-Host ("[INFO] 发现 {0} 个运行中的桌面端实例，先关闭以便替换二进制" -f $running.Count)
        $running | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
    }

    try {
        Copy-Item $exe $dst -Force -ErrorAction Stop
    } catch {
        Write-Host ("[FAIL] 同步到 {0} 失败: {1}" -f $dst, $_.Exception.Message)
        exit 1
    }

    # 核对字节数：复制「没报错」不等于「复制对了」
    $srcLen = (Get-Item $exe).Length
    $dstLen = (Get-Item $dst).Length
    if ($srcLen -ne $dstLen) {
        Write-Host ("[FAIL] 同步后大小不一致：源 {0} / 目标 {1}" -f $srcLen, $dstLen)
        exit 1
    }
    Write-Host ("[OK] 已同步到 {0} ({1:N1} MB)" -f $dst, ($dstLen / 1MB))
} else {
    Write-Host "[FAIL] cargo 返回成功但未生成 release 产物"
    Get-Content $log -Tail 20
    exit 1
}
