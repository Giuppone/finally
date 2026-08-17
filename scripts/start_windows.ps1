<#
.SYNOPSIS
    Start FinAlly (Windows). Idempotent: safe to run repeatedly.

.EXAMPLE
    .\scripts\start_windows.ps1
    .\scripts\start_windows.ps1 -Build
    .\scripts\start_windows.ps1 -Open
#>
[CmdletBinding()]
param(
    [switch]$Build,
    [switch]$Open,
    [int]$Port = 8000
)

# NOT "Stop". Every docker command here writes progress to stderr, and under `Stop`
# PowerShell 5.1 promotes a native command's stderr to a terminating NativeCommandError —
# so `-Build` died on the first line docker printed, before it had built anything. Each
# docker call below already checks $LASTEXITCODE, which is the reliable signal for a native
# executable; "Stop" only added a way to fail on success.
$ErrorActionPreference = "Continue"

$Image     = "finally:latest"
$Container = "finally"
$Volume    = "finally-data"
$Root      = Split-Path -Parent $PSScriptRoot

Set-Location $Root

docker info *> $null
if (-not $?) {
    Write-Error "Docker is not running. Start Docker Desktop and try again."
    exit 1
}

# The backend fails fast without OPENROUTER_API_KEY unless LLM_MOCK=true (PLAN.md §5), so
# catch a missing .env here rather than letting the container exit a second later.
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Warning "No .env found; created one from .env.example."
    Write-Warning "Add OPENROUTER_API_KEY (or set LLM_MOCK=true) to .env, then re-run."
    exit 1
}

$existingImage = docker images -q $Image
if ($Build -or [string]::IsNullOrWhiteSpace($existingImage)) {
    Write-Host "Building $Image ..."
    docker build -t $Image .
    if ($LASTEXITCODE -ne 0) { Write-Error "docker build failed."; exit 1 }
}

# Idempotence: remove any previous container, running or stopped. The volume is untouched,
# so the portfolio survives.
$existing = docker ps -aq -f "name=^$Container$"
if (-not [string]::IsNullOrWhiteSpace($existing)) {
    Write-Host "Removing the previous $Container container (data volume is kept) ..."
    docker rm -f $Container *> $null
}

docker volume create $Volume *> $null

Write-Host "Starting $Container on port $Port ..."
docker run -d --name $Container -p "${Port}:8000" --env-file .env -v "${Volume}:/app/db" --restart unless-stopped $Image *> $null
if ($LASTEXITCODE -ne 0) { Write-Error "docker run failed."; exit 1 }

Write-Host -NoNewline "Waiting for the app to become healthy "
foreach ($attempt in 1..60) {
    $status = docker inspect -f "{{.State.Health.Status}}" $Container 2>$null
    if ($status -eq "healthy") {
        Write-Host ""
        Write-Host "FinAlly is running: http://localhost:$Port"
        if ($Open) { Start-Process "http://localhost:$Port" }
        exit 0
    }

    $running = docker inspect -f "{{.State.Running}}" $Container 2>$null
    if ($running -ne "true") {
        Write-Host ""
        Write-Error "The container exited. Logs:"
        docker logs $Container
        exit 1
    }

    Write-Host -NoNewline "."
    Start-Sleep -Seconds 2
}

Write-Host ""
Write-Error "Timed out waiting for a healthy status. Recent logs:"
docker logs --tail 40 $Container
exit 1
