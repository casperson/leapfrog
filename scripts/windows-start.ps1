param(
  [string]$DataDir = "",
  [string]$ListenHost = "",
  [string]$Port = ""
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$leapfrogExe = Join-Path $repoRoot ".venv\Scripts\leapfrog.exe"
$pidFile = Join-Path $repoRoot ".leapfrog-windows.pid"
$stdoutLog = Join-Path $repoRoot "leapfrog.out.log"
$stderrLog = Join-Path $repoRoot "leapfrog.err.log"

if (-not (Test-Path $leapfrogExe)) {
  throw "Could not find $leapfrogExe. Run the install steps first so the virtualenv and Leapfrog entrypoint exist."
}

if (Test-Path $pidFile) {
  $existingPid = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1).Trim()
  if ($existingPid) {
    $existingProcess = Get-Process -Id ([int]$existingPid) -ErrorAction SilentlyContinue
    if ($existingProcess) {
      Write-Host "Leapfrog is already running in the background (PID $existingPid)."
      Write-Host "Stop it first with: powershell -ExecutionPolicy Bypass -File .\scripts\windows-stop.ps1"
      exit 0
    }
  }
  Remove-Item $pidFile -ErrorAction SilentlyContinue
}

if (Test-Path $stdoutLog) {
  Move-Item $stdoutLog "$stdoutLog.prev" -Force
}
if (Test-Path $stderrLog) {
  Move-Item $stderrLog "$stderrLog.prev" -Force
}

$originalEnv = @{
  LEAPFROG_DATA = $env:LEAPFROG_DATA
  LEAPFROG_HOST = $env:LEAPFROG_HOST
  LEAPFROG_PORT = $env:LEAPFROG_PORT
}

try {
  if ($DataDir) {
    $env:LEAPFROG_DATA = $DataDir
  }
  if ($ListenHost) {
    $env:LEAPFROG_HOST = $ListenHost
  }
  if ($Port) {
    $env:LEAPFROG_PORT = $Port
  }

  $process = Start-Process `
    -FilePath $leapfrogExe `
    -WorkingDirectory $repoRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdoutLog `
    -RedirectStandardError $stderrLog `
    -PassThru
}
finally {
  $env:LEAPFROG_DATA = $originalEnv.LEAPFROG_DATA
  $env:LEAPFROG_HOST = $originalEnv.LEAPFROG_HOST
  $env:LEAPFROG_PORT = $originalEnv.LEAPFROG_PORT
}

Set-Content -Path $pidFile -Value $process.Id
Start-Sleep -Seconds 2

if ($process.HasExited) {
  Remove-Item $pidFile -ErrorAction SilentlyContinue
  Write-Host "Leapfrog exited during startup. Check $stderrLog and $stdoutLog for details."
  exit 1
}

Write-Host "Leapfrog started in the background."
Write-Host "PID file: $pidFile"
Write-Host "Stdout log: $stdoutLog"
Write-Host "Stderr log: $stderrLog"
