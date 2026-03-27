param(
    [string]$InnoCompilerPath = ""
)

$ErrorActionPreference = "Stop"

function Resolve-Iscc([string]$InputPath) {
    if ($InputPath -and (Test-Path -LiteralPath $InputPath)) {
        return (Resolve-Path -LiteralPath $InputPath).Path
    }

    $candidates = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles}\Inno Setup 6\ISCC.exe"
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            return $candidate
        }
    }

    $cmd = Get-Command iscc.exe -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Path
    }

    throw "ISCC.exe not found. Install Inno Setup 6 and rerun."
}

$iscc = Resolve-Iscc -InputPath $InnoCompilerPath
$issPath = Join-Path $PSScriptRoot "OdooPrintAgent.iss"

if (-not (Test-Path -LiteralPath $issPath)) {
    throw "Installer script not found: $issPath"
}

Write-Host "Building installer with $iscc" -ForegroundColor Cyan
& $iscc $issPath
Write-Host "Build complete. Setup exe is in: $PSScriptRoot" -ForegroundColor Green
