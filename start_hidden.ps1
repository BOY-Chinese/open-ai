# open-ai - hidden startup (v2.4): launch process broker via bootstrap.py
# All children (gateway :8000, trae node :18787, tasks) are spawned and
# supervised by the Broker (app_runtime.py) inside a Job Object tree.
$ErrorActionPreference = "SilentlyContinue"
$dir = $PSScriptRoot
$brokerShim = Join-Path $dir "runtime\Scripts\open-ai-daemon.exe"
$bootstrap  = Join-Path $dir "bootstrap.py"

if (-not (Test-Path $brokerShim)) {
    # shim missing (fresh install): fall back to venv pythonw
    $pyw = Join-Path $dir ".venv\Scripts\pythonw.exe"
    if (-not (Test-Path $pyw)) { exit 0 }
    Start-Process -FilePath $pyw -ArgumentList "`"$bootstrap`"","start" `
        -WorkingDirectory $dir -WindowStyle Hidden
    exit 0
}

Start-Process -FilePath $brokerShim -ArgumentList "`"$bootstrap`"","start" `
    -WorkingDirectory $dir -WindowStyle Hidden
Write-Output "[OK] open-ai broker launch requested (idempotent)"
