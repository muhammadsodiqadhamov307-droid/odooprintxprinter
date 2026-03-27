param(
    [string]$ServiceName = "OdooPrintAgent"
)

$ErrorActionPreference = "Stop"

$service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if (-not $service) {
    throw "Service '$ServiceName' not found."
}

if ($service.Status -eq "Running") {
    Restart-Service -Name $ServiceName -Force
} else {
    Start-Service -Name $ServiceName
}

Write-Host "Service '$ServiceName' is now $(Get-Service -Name $ServiceName).Status" -ForegroundColor Green
