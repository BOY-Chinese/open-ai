# check-ps1.ps1 - Syntax gate for every PowerShell script in the project.
#
# WHY THIS EXISTS
#   Windows PowerShell 5.1 decodes a script file using the ANSI code page (GBK on
#   zh-CN) unless the file starts with a UTF-8 BOM. When a UTF-8 Chinese comment
#   is mis-decoded, its final byte can pair with the following newline into one
#   double-byte character, which silently SWALLOWS the next line of code.
#
#   This project was bitten twice:
#     - build-tauri-release.ps1 lost `$env:TAURI_ENV_DEBUG = 'false'`
#       -> release bundle embedded devUrl -> VM showed ERR_CONNECTION_REFUSED
#     - start_hidden.ps1 (the boot/autostart entry) had 2 hard parse errors
#   Both failures are invisible to the author (the file looks perfect in an
#   editor that reads UTF-8) and only surface at runtime on the user's machine.
#
# USAGE
#   powershell -NoProfile -ExecutionPolicy Bypass -File check-ps1.ps1
#   powershell ... -File check-ps1.ps1 -Root 'D:\app\dsh_plugin\open-ai'
# Exit code 0 = all clean, 1 = at least one file is broken.
param([string]$Root = 'D:\app\dsh_plugin\open-ai')

$ErrorActionPreference = 'Continue'

# Directories that never belong to our own source (vendored / generated).
$skipParts = @('\.venv\', '\toolchain\', '\runtime\', '\node_modules\', '\target\', '\installer\build\', '\installer\dist\')

$files = @()
foreach ($f in (Get-ChildItem -Path $Root -Filter *.ps1 -Recurse -File -ErrorAction SilentlyContinue)) {
    $p = $f.FullName
    $skipIt = $false
    foreach ($s in $skipParts) { if ($p.Contains($s)) { $skipIt = $true; break } }
    if (-not $skipIt) { $files += $f }
}

$bad = 0
foreach ($f in $files) {
    $bytes = [System.IO.File]::ReadAllBytes($f.FullName)
    $hasBom = ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF)
    $nonAscii = $false
    foreach ($b in $bytes) { if ($b -gt 0x7F) { $nonAscii = $true; break } }

    $errs = $null
    $tokens = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile($f.FullName, [ref]$tokens, [ref]$errs)

    $problems = @()
    if (@($errs).Count -gt 0) {
        $problems += ("parse error x{0}: {1}" -f @($errs).Count, (@($errs) | ForEach-Object { $_.Message }) -join '; ')
    }
    if ($nonAscii -and -not $hasBom) {
        $problems += 'contains non-ASCII but has no UTF-8 BOM (PowerShell 5.1 will mis-decode it)'
    }

    if ($problems.Count -gt 0) {
        $bad++
        Write-Host ("[FAIL] {0}" -f $f.FullName)
        foreach ($p in $problems) { Write-Host ("       - {0}" -f $p) }
    } else {
        Write-Host ("[ OK ] {0}" -f $f.FullName)
    }
}

Write-Host ''
Write-Host ("checked {0} script(s), {1} broken" -f $files.Count, $bad)
exit ([int]($bad -gt 0))
