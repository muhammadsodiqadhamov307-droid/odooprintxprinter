param(
    [string]$ProjectRoot = ""
)

$ErrorActionPreference = "Stop"

function Resolve-ProjectRoot {
    param([string]$InputRoot)
    if ($InputRoot) {
        return (Resolve-Path -LiteralPath $InputRoot).Path
    }
    return (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
}

$root = Resolve-ProjectRoot -InputRoot $ProjectRoot
$managerPy = Join-Path $root "agent_manager.py"
$venvPython = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $managerPy)) {
    throw "agent_manager.py not found at $managerPy"
}

if (Test-Path -LiteralPath $venvPython) {
    & $venvPython $managerPy
    exit $LASTEXITCODE
}

$py = Get-Command py.exe -ErrorAction SilentlyContinue
if ($py) {
    & py -3.11 $managerPy
    exit $LASTEXITCODE
}

throw "Could not find local venv Python or py.exe launcher."
