param(
    [switch]$CheckOnly,
    [string]$Model = 'deepseek-flash'
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$pythonPath = $env:ALS_PYTHON
if (-not $pythonPath) {
    $localPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
    $existingPython = Join-Path (Split-Path $projectRoot -Parent) 'ALS_MVP\.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $localPython) { $pythonPath = $localPython }
    elseif (Test-Path -LiteralPath $existingPython) { $pythonPath = $existingPython }
    else { throw 'Python environment not found. Follow the local environment steps in README.md first.' }
}
if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'The selected Python executable does not exist.' }

$oldKey = $env:ALS_API_KEY
$oldModel = $env:ALS_MODEL
$oldBaseUrl = $env:ALS_BASE_URL
try {
    if ([string]::IsNullOrWhiteSpace($env:ALS_API_KEY)) {
        $secureKey = Read-Host 'DeepSeek API Key (hidden)' -AsSecureString
        $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
        try { $env:ALS_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) }
        finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
        if ([string]::IsNullOrWhiteSpace($env:ALS_API_KEY)) { throw 'An API Key is required.' }
    }
    $env:ALS_MODEL = $Model
    $env:ALS_BASE_URL = 'https://api.deepseek.com'
    Push-Location -LiteralPath $projectRoot
    try {
        & $pythonPath scripts/check_config.py
        if ($LASTEXITCODE -ne 0) { throw 'Configuration check failed; the app was not started.' }
        if (-not $CheckOnly) {
            Write-Host 'Open http://127.0.0.1:8767/ after the server starts. Keep this window open while testing.'
            & $pythonPath -m app.main
            if ($LASTEXITCODE -ne 0) { throw 'The app stopped with an error.' }
        }
    } finally { Pop-Location }
} finally {
    $env:ALS_API_KEY = $oldKey
    $env:ALS_MODEL = $oldModel
    $env:ALS_BASE_URL = $oldBaseUrl
}
