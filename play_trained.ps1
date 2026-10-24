$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$pilotDatabase = Join-Path $PSScriptRoot 'data\selfplay-1000\training.sqlite3'
if (-not (Test-Path -LiteralPath $pilotDatabase)) {
    throw 'Run train_1000.cmd first.'
}
if (-not (Test-Path -LiteralPath 'data\selfplay-1000\completion.json')) {
    throw 'Training or verification is not complete. Run train_1000.cmd to continue.'
}
$env:UTTT_DB_PATH = $pilotDatabase
$env:UTTT_PLAY_MODE = 'eval'
Write-Host 'Open http://127.0.0.1:8502 to play the trained models. Press Ctrl+C to stop.'
& '.\.venv\Scripts\python.exe' -m streamlit run app.py --server.port 8502
exit $LASTEXITCODE
