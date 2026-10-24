param([string]$Python = '')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$pythonArgs = @()
if (-not $Python) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $Python = (Get-Command py).Source
        $pythonArgs = @('-3.12')
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        $Python = (Get-Command python).Source
    } else {
        $bundledPython = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
        if (Test-Path -LiteralPath $bundledPython) { $Python = $bundledPython }
        else { throw 'Please install Python 3.12, or run setup.ps1 -Python C:\path\python.exe' }
    }
}
& $Python @pythonArgs -c 'import sys; assert sys.version_info[:2] == (3, 12), "Use Python 3.12 for the verified environment."'
if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 is required.' }
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    & $Python @pythonArgs -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Could not create .venv.' }
}
& '.\.venv\Scripts\python.exe' -m pip install -r requirements-lock.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
Write-Host 'Setup complete. Run .\start.ps1 and open http://127.0.0.1:8501'
