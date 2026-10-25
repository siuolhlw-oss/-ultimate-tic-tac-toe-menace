param(
    [ValidateSet('Start', 'Resume', 'Status', 'Stop')][string]$Action = 'Status',
    [string]$Directory,
    [string]$Python
)
$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
# 本專案的隔離 worktree 共用主工作區虛擬環境及資料夾。
if (!(Test-Path -LiteralPath (Join-Path $projectRoot '.venv\Scripts\python.exe'))) {
    $candidateRoot = Split-Path (Split-Path $projectRoot -Parent) -Parent
    if (Test-Path -LiteralPath (Join-Path $candidateRoot '.venv\Scripts\python.exe')) {
        $projectRoot = $candidateRoot
    }
}
if (!$Python) { $Python = Join-Path $projectRoot '.venv\Scripts\python.exe' }
if (!$Directory) { $Directory = Join-Path $projectRoot 'data\selfplay-million-compact' }
$Python = (Resolve-Path -LiteralPath $Python).Path
$Directory = (Resolve-Path -LiteralPath $Directory).Path
$entry = Join-Path $PSScriptRoot 'tools\train_million.py'
if ($Action -eq 'Status' -or $Action -eq 'Stop') {
    & $Python -B -X utf8 $entry $Action.ToLower() --directory $Directory
    exit $LASTEXITCODE
}
$command = if ($Action -eq 'Resume') { 'resume' } else { 'run' }
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
$stdout = Join-Path $Directory "runner-$stamp.log"
$stderr = Join-Path $Directory "runner-$stamp.error.log"
$arguments = '-B -X utf8 "{0}" {1} --directory "{2}"' -f $entry, $command, $Directory
$process = Start-Process -FilePath $Python -ArgumentList $arguments -WorkingDirectory $PSScriptRoot `
    -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
$record = @{ pid = $process.Id; launched_at = (Get-Date).ToString('o'); command = $command;
    entry = $entry; directory = $Directory; stdout = $stdout; stderr = $stderr }
$record | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Directory 'background.json') -Encoding UTF8
Start-Sleep -Seconds 2
$process.Refresh()
if ($process.HasExited) {
    Get-Content -LiteralPath $stderr
    Get-Content -LiteralPath $stdout -Tail 3
    if ($process.ExitCode -ne 0) { throw '訓練程序已退出，請依上列錯誤處理。' }
} else {
    $record | ConvertTo-Json
    & $Python -B -X utf8 $entry status --directory $Directory
}
