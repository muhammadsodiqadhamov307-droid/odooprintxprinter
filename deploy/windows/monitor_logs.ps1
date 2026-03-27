param(
    [string]$LogPath = ""
)

$ErrorActionPreference = "Stop"

if (-not $LogPath) {
    $projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
    $LogPath = Join-Path $projectRoot "print_agent.log"
}

if (-not (Test-Path -LiteralPath $LogPath)) {
    New-Item -Path $LogPath -ItemType File -Force | Out-Null
}

Write-Host "Tailing log: $LogPath" -ForegroundColor Cyan
Get-Content -Path $LogPath -Wait -Tail 200
