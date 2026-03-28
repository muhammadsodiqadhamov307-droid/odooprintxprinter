param(
    [string]$ServiceName = "OdooPrintAgent",
    [string]$NssmPath = "",
    [switch]$RemoveShortcuts
)

$ErrorActionPreference = "Stop"

function Find-Nssm {
    param([string]$InputPath)
    if ($InputPath -and (Test-Path -LiteralPath $InputPath)) {
        return (Resolve-Path -LiteralPath $InputPath).Path
    }
    $projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
    $candidates = @(
        (Join-Path $projectRoot "deploy\windows\tools\nssm.exe"),
        (Join-Path $projectRoot "nssm.exe")
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    $cmd = Get-Command nssm.exe -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Path
    }
    throw "nssm.exe not found."
}

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).
    IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    throw "Run this script as Administrator."
}

$nssm = Find-Nssm -InputPath $NssmPath
$service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($service) {
    try { & $nssm stop $ServiceName confirm | Out-Null } catch {}
    Start-Sleep -Seconds 1
    & $nssm remove $ServiceName confirm | Out-Null
    Write-Host "Removed service: $ServiceName" -ForegroundColor Green
} else {
    Write-Host "Service not found: $ServiceName"
}

if ($RemoveShortcuts) {
    $desktopLocations = @(
        [Environment]::GetFolderPath("Desktop"),
        [Environment]::GetFolderPath("CommonDesktopDirectory")
    ) | Where-Object { $_ } | Select-Object -Unique
    $shortcutNames = @(
        "Odoo Print Agent Logs.lnk",
        "Odoo Print Agent Restart.lnk",
        "Odoo Print Agent Manager.lnk"
    )
    foreach ($desktop in $desktopLocations) {
        foreach ($shortcutName in $shortcutNames) {
            $shortcut = Join-Path $desktop $shortcutName
            if (Test-Path -LiteralPath $shortcut) {
                Remove-Item -LiteralPath $shortcut -Force
            }
        }
    }
    Write-Host "Desktop shortcuts removed."
}
