$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    throw 'Run .\setup.ps1 first to create the local Python environment.'
}
Write-Host 'Open http://127.0.0.1:8501 in your browser. Press Ctrl+C to stop.'
& '.\.venv\Scripts\python.exe' -m streamlit run app.py
exit $LASTEXITCODE
