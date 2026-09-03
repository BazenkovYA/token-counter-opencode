param([string]$EnvFile = "$PSScriptRoot\runtime\opencode_litellm\.env")
$ErrorActionPreference = 'Stop'
Push-Location -LiteralPath $PSScriptRoot
try { & "$PSScriptRoot\.venv\Scripts\python.exe" -m token_counter status --env $EnvFile; exit $LASTEXITCODE }
finally { Pop-Location }
