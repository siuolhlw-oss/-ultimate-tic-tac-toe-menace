$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$validatedDatabase = Join-Path $PSScriptRoot 'data\storage-v3\training-6000.sqlite3'
$validationRecord = Join-Path $PSScriptRoot 'data\storage-v3\training-6000.validation.json'
if ((Test-Path -LiteralPath $validatedDatabase) -and (Test-Path -LiteralPath $validationRecord)) {
    $env:UTTT_DB_PATH = $validatedDatabase
} else {
    $env:UTTT_DB_PATH = Join-Path $PSScriptRoot 'data\storage-v3\pilot-1000.sqlite3'
}
$env:UTTT_PLAY_MODE = 'eval'
& (Join-Path $PSScriptRoot '.venv\Scripts\python.exe') -X utf8 -m streamlit run tools\compact_app.py --server.address 127.0.0.1 --server.port 8504
