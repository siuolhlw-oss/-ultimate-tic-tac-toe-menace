$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath 'exports\game-atlas\index.html')) {
    throw 'Run .venv\Scripts\python.exe tools\build_game_atlas.py first.'
}
Write-Host 'Open http://127.0.0.1:8503 to explore the 1,000 games. Press Ctrl+C to stop.'
& '.\.venv\Scripts\python.exe' -m http.server 8503 --bind 127.0.0.1 --directory 'exports\game-atlas'
exit $LASTEXITCODE
