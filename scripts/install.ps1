[CmdletBinding()]
param(
    [switch]$ConnectOpenCode,
    [switch]$Demo,
    [switch]$Dev,
    [switch]$NoStart,
    [string]$OpenCodeConfig = (Join-Path $HOME '.config\opencode\opencode.json'),
    [string]$Python
)
$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$VenvPython = Join-Path $Root '.venv\Scripts\python.exe'
$EnvRelative = if ($Demo) { 'runtime\demo\.env' } else { 'runtime\opencode_litellm\.env' }
$EnvFile = Join-Path $Root $EnvRelative

function Invoke-Checked {
    param([Parameter(Mandatory)][string]$Program, [Parameter(ValueFromRemainingArguments)][string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed with exit code ${LASTEXITCODE}: $Program" }
}

Push-Location -LiteralPath $Root
try {
    if (-not (Test-Path -LiteralPath $VenvPython)) {
        if ($Python) {
            Invoke-Checked $Python '-c' 'import sys; assert sys.version_info >= (3,12), sys.version'
            Invoke-Checked $Python '-m' 'venv' '.venv'
        } elseif (Get-Command py -ErrorAction SilentlyContinue) {
            Invoke-Checked 'py' '-3.12' '-c' 'import sys; assert sys.version_info >= (3,12), sys.version'
            Invoke-Checked 'py' '-3.12' '-m' 'venv' '.venv'
        } elseif (Get-Command python -ErrorAction SilentlyContinue) {
            Invoke-Checked 'python' '-c' 'import sys; assert sys.version_info >= (3,12), sys.version'
            Invoke-Checked 'python' '-m' 'venv' '.venv'
        } else {
            throw 'Python 3.12 or newer is required. The installer does not modify system Python.'
        }
    }
    Invoke-Checked $VenvPython '-m' 'pip' 'install' '--disable-pip-version-check' '-r' 'requirements.lock'
    if ($Dev) { Invoke-Checked $VenvPython '-m' 'pip' 'install' '--disable-pip-version-check' '-r' 'requirements-dev.lock' }

    if ($Demo) {
        if (-not (Test-Path -LiteralPath $EnvFile)) {
            Invoke-Checked $VenvPython '-m' 'token_counter' 'setup' '--demo' '--destination' $EnvFile
        }
    } else {
        $PlanFile = Join-Path (Split-Path -Parent $EnvFile) 'connection-plan.json'
        if (-not (Test-Path -LiteralPath $PlanFile)) {
            Invoke-Checked $VenvPython 'scripts\connect_opencode.py' 'prepare' '--config' $OpenCodeConfig '--env' $EnvFile
        }
    }

    Invoke-Checked $VenvPython '-m' 'token_counter' 'check' '--env' $EnvFile
    if (-not $NoStart) { Invoke-Checked $VenvPython '-m' 'token_counter' 'start' '--env' $EnvFile }
    if ($ConnectOpenCode) {
        if ($Demo) { throw '-ConnectOpenCode cannot be used with -Demo.' }
        if ($NoStart) { throw '-ConnectOpenCode requires a running counter; remove -NoStart.' }
        Invoke-Checked $VenvPython 'scripts\connect_opencode.py' 'apply' '--env' $EnvFile
    }
    Write-Host "Installation completed from project root: $Root"
    if ($ConnectOpenCode) { Write-Host 'Restart OpenCode completely and send one new short request.' }
    elseif (-not $Demo) { Write-Host 'Preparation completed. Re-run with -ConnectOpenCode to apply the connection.' }
} finally {
    Pop-Location
}
