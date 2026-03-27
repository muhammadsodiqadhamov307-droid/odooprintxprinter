param(
    [string]$ServiceName = "OdooPrintAgent",
    [string]$ProjectRoot = "",
    [string]$NssmPath = "",
    [string]$PythonVersion = "3.11",
    [switch]$SkipPipInstall,
    [switch]$AutoInstallPython
)

$ErrorActionPreference = "Stop"

function Write-Step([string]$Message) {
    Write-Host "`n==> $Message" -ForegroundColor Cyan
}

function Resolve-ProjectRoot {
    param([string]$InputRoot)
    if ($InputRoot) {
        return (Resolve-Path -LiteralPath $InputRoot).Path
    }
    return (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
}

function Find-Nssm {
    param([string]$InputPath, [string]$Root)
    if ($InputPath -and (Test-Path -LiteralPath $InputPath)) {
        return (Resolve-Path -LiteralPath $InputPath).Path
    }
    $candidates = @(
        (Join-Path $Root "deploy\windows\tools\nssm.exe"),
        (Join-Path $Root "nssm.exe")
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
    throw "nssm.exe was not found. Put it at deploy\windows\tools\nssm.exe or install NSSM to PATH."
}

function Test-PythonExe {
    param([string]$ExePath)
    if (-not $ExePath -or -not (Test-Path -LiteralPath $ExePath)) {
        return $false
    }
    try {
        $out = (& $ExePath -c "import sys; print(sys.version); print(sys.executable)" 2>&1 | Out-String).Trim()
        $code = $LASTEXITCODE
        if ($code -ne 0) {
            return $false
        }
        if ($out -notmatch '\d+\.\d+\.\d+') {
            return $false
        }
        if ($out -match 'WindowsApps\\python\.exe') {
            return $false
        }
        return $true
    } catch {
        return $false
    }
}

function Get-InstalledPythonExe {
    param([string]$Version)

    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) {
        try {
            $exe = (& $py.Path -$Version -c "import sys; print(sys.executable)").Trim()
            if ($exe -and (Test-PythonExe -ExePath $exe)) {
                return (Resolve-Path -LiteralPath $exe).Path
            }
        } catch {}
        try {
            $exe = (& $py.Path -3 -c "import sys; print(sys.executable)").Trim()
            if ($exe -and (Test-PythonExe -ExePath $exe)) {
                return (Resolve-Path -LiteralPath $exe).Path
            }
        } catch {}
    }

    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($python) {
        if (Test-PythonExe -ExePath $python.Path) {
            return (Resolve-Path -LiteralPath $python.Path).Path
        }
    }

    $verNoDot = $Version.Replace('.', '')
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Python\Python$verNoDot\python.exe",
        "$env:ProgramFiles\Python$verNoDot\python.exe",
        "$env:ProgramFiles\Python\Python$verNoDot\python.exe",
        "$env:LOCALAPPDATA\Python\pythoncore-$Version-64\python.exe",
        "$env:LOCALAPPDATA\Python\pythoncore-$Version-32\python.exe"
    )
    foreach ($candidate in $candidates) {
        if (Test-PythonExe -ExePath $candidate) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    return $null
}

function Ensure-Python {
    param([string]$Version)
    $existing = Get-InstalledPythonExe -Version $Version
    if ($existing) {
        return $existing
    }

    if (-not $AutoInstallPython) {
        throw "Python is not installed. Re-run with -AutoInstallPython to install automatically."
    }

    Write-Step "Python not found. Installing Python via winget"
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $winget) {
        throw "winget is not available. Install Python manually, then rerun."
    }

    $installArgs = @(
        "install",
        "--id", "Python.Python.$Version",
        "--exact",
        "--source", "winget",
        "--scope", "machine",
        "--accept-source-agreements",
        "--accept-package-agreements"
    )
    & $winget.Path @installArgs | Out-Host
    $firstExit = $LASTEXITCODE
    if ($firstExit -ne 0) {
        Write-Step "winget install failed (exit code $firstExit). Resetting winget sources and retrying"
        & $winget.Path source reset --force | Out-Host
        & $winget.Path source update | Out-Host
        & $winget.Path @installArgs | Out-Host
        if ($LASTEXITCODE -ne 0) {
            $fallbackArgs = @(
                "install",
                "--id", "Python.Python.$Version",
                "--exact",
                "--source", "winget",
                "--accept-source-agreements",
                "--accept-package-agreements"
            )
            & $winget.Path @fallbackArgs | Out-Host
        }
    }

    Start-Sleep -Seconds 2
    $installed = Get-InstalledPythonExe -Version $Version
    if (-not $installed) {
        throw "Python install did not complete successfully. Install Python $Version manually, then rerun setup."
    }
    return $installed
}

function Ensure-Venv {
    param(
        [string]$BasePythonExe,
        [string]$Root,
        [switch]$SkipPip
    )
    $venvDir = Join-Path $Root ".venv"
    $venvPython = Join-Path $venvDir "Scripts\python.exe"

    if (-not (Test-Path -LiteralPath $venvPython)) {
        Write-Step "Creating local virtual environment"
        & $BasePythonExe -m venv $venvDir 2>&1 | Out-Host
        if ($LASTEXITCODE -ne 0) {
            throw "Python failed while creating virtual environment (exit code $LASTEXITCODE) at $venvDir"
        }
    }

    if (-not (Test-Path -LiteralPath $venvPython)) {
        throw "Failed to create virtual environment at $venvDir"
    }

    if (-not $SkipPip) {
        Write-Step "Installing/updating Python dependencies in local venv"
        & $venvPython -m pip install --upgrade pip | Out-Host
        & $venvPython -m pip install -r (Join-Path $Root "requirements.txt") | Out-Host
    }

    return (Resolve-Path -LiteralPath $venvPython).Path
}

function Remove-ServiceIfExists {
    param([string]$SvcName, [string]$NssmExe)
    $service = Get-Service -Name $SvcName -ErrorAction SilentlyContinue
    if ($service) {
        Write-Step "Removing existing service $SvcName"
        try { & $NssmExe stop $SvcName confirm | Out-Null } catch {}
        Start-Sleep -Seconds 1
        & $NssmExe remove $SvcName confirm | Out-Null
    }
}

function New-DesktopShortcut {
    param(
        [string]$ShortcutName,
        [string]$TargetPath,
        [string]$Arguments,
        [string]$WorkingDirectory,
        [string]$IconLocation = ""
    )
    $desktop = [Environment]::GetFolderPath("Desktop")
    $shortcutPath = Join-Path $desktop "$ShortcutName.lnk"
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $TargetPath
    $shortcut.Arguments = $Arguments
    $shortcut.WorkingDirectory = $WorkingDirectory
    if ($IconLocation -and (Test-Path -LiteralPath $IconLocation)) {
        $shortcut.IconLocation = $IconLocation
    } else {
        $shortcut.IconLocation = "$env:SystemRoot\System32\shell32.dll,220"
    }
    $shortcut.Save()
}

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).
    IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    throw "Run this script as Administrator."
}

$root = Resolve-ProjectRoot -InputRoot $ProjectRoot
$agentPath = Join-Path $root "print_agent.py"
$managerPy = Join-Path $root "agent_manager.py"
$managerIcon = Join-Path $root "deploy\windows\assets\app_logo.ico"
$managerVbs = Join-Path $root "deploy\windows\run_manager.vbs"
$wscriptExe = Join-Path $env:SystemRoot "System32\wscript.exe"
$stdoutPath = Join-Path $root "print_agent_service_stdout.log"
$stderrPath = Join-Path $root "print_agent_service_stderr.log"

if (-not (Test-Path -LiteralPath $agentPath)) {
    throw "print_agent.py not found at $agentPath"
}

Write-Step "Checking Python"
$basePythonExe = Ensure-Python -Version $PythonVersion
$pythonExe = Ensure-Venv -BasePythonExe $basePythonExe -Root $root -SkipPip:$SkipPipInstall
Write-Host "Using Python executable: $pythonExe" -ForegroundColor Yellow

Write-Step "Finding NSSM"
$nssm = Find-Nssm -InputPath $NssmPath -Root $root
Write-Host "Using NSSM: $nssm" -ForegroundColor Yellow

Remove-ServiceIfExists -SvcName $ServiceName -NssmExe $nssm

Write-Step "Installing Windows service $ServiceName"
& $nssm install $ServiceName $pythonExe
& $nssm set $ServiceName AppParameters "-u print_agent.py"
& $nssm set $ServiceName AppDirectory $root
& $nssm set $ServiceName DisplayName "Odoo Custom Print Agent"
& $nssm set $ServiceName Description "Print agent for Odoo PoS custom thermal printing."
& $nssm set $ServiceName Start SERVICE_AUTO_START
& $nssm set $ServiceName AppExit Default Restart
& $nssm set $ServiceName AppRestartDelay 5000
& $nssm set $ServiceName AppStdout $stdoutPath
& $nssm set $ServiceName AppStderr $stderrPath
& $nssm set $ServiceName AppRotateFiles 1
& $nssm set $ServiceName AppRotateOnline 1
& $nssm set $ServiceName AppRotateSeconds 86400

Write-Step "Starting service"
& $nssm start $ServiceName

Write-Step "Creating desktop shortcuts"
if ((Test-Path -LiteralPath $managerVbs) -and (Test-Path -LiteralPath $wscriptExe)) {
    New-DesktopShortcut -ShortcutName "Odoo Print Agent Manager" -TargetPath $wscriptExe -Arguments "`"$managerVbs`" `"$root`"" -WorkingDirectory $root -IconLocation $managerIcon
} else {
    $pythonwExe = Join-Path (Split-Path -Path $pythonExe -Parent) "pythonw.exe"
    if (Test-Path -LiteralPath $pythonwExe) {
        New-DesktopShortcut -ShortcutName "Odoo Print Agent Manager" -TargetPath $pythonwExe -Arguments "`"$managerPy`"" -WorkingDirectory $root -IconLocation $managerIcon
    } else {
        New-DesktopShortcut -ShortcutName "Odoo Print Agent Manager" -TargetPath $pythonExe -Arguments "`"$managerPy`"" -WorkingDirectory $root -IconLocation $managerIcon
    }
}

Write-Step "Done"
Write-Host "Service installed: $ServiceName" -ForegroundColor Green
Write-Host "Project root: $root"
