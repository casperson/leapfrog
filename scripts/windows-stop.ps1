$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$pidFile = Join-Path $repoRoot ".leapfrog-windows.pid"

if (-not (Test-Path $pidFile)) {
  Write-Host "No Windows background PID file found. Leapfrog may already be stopped."
  exit 0
}

$pidText = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1).Trim()
if (-not $pidText) {
  Remove-Item $pidFile -ErrorAction SilentlyContinue
  Write-Host "PID file was empty. Removed stale PID file."
  exit 0
}

$process = Get-Process -Id ([int]$pidText) -ErrorAction SilentlyContinue
if (-not $process) {
  Remove-Item $pidFile -ErrorAction SilentlyContinue
  Write-Host "Leapfrog process $pidText is not running. Removed stale PID file."
  exit 0
}

Stop-Process -Id $process.Id
Remove-Item $pidFile -ErrorAction SilentlyContinue
Write-Host "Stopped Leapfrog background process $($process.Id)."
